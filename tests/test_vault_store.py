"""The login vault's entries, in-memory unlock, freeze and audit (T-19366)."""

from __future__ import annotations

import json
import re

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    secret_registry,
    vault_crypto,
    vault_store,
)
from stealth_chrome_devtools_mcp.settings import get_settings

PASSPHRASE = "correct horse"
PASSWORD = "Tr0ub4dor&3-marker"
LOOPBACK_ENV = "STEALTH_MCP_VAULT_ALLOW_HTTP_LOOPBACK"


def password_entry(**overrides):
    entry = {
        "kind": "password",
        "site": "example.com",
        "login_url": "https://accounts.example.com/signin",
        "hosts": ["accounts.example.com"],
        "username": "someone@example.com",
        "password": PASSWORD,
        "show_username": False,
        "frozen": None,
    }
    entry.update(overrides)
    return entry


def provider_entry(**overrides):
    entry = {"kind": "provider", "site": "shop.example", "via": "work"}
    entry.update(overrides)
    return entry


def set_loopback(monkeypatch, value):
    """The test variable, through the one Settings home (cached per process)."""
    monkeypatch.setenv(LOOPBACK_ENV, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def vault(tmp_path, monkeypatch):
    """A vault file under tmp_path with the KDF at n=2**10, locked on both ends."""
    monkeypatch.setattr(vault_crypto, "SCRYPT_N", 2**10)
    monkeypatch.setattr(vault_store, "VAULT_PATH", tmp_path / "vault.json")
    monkeypatch.setattr(vault_store, "AUDIT_PATH", tmp_path / "vault-audit.jsonl")
    monkeypatch.delenv(LOOPBACK_ENV, raising=False)
    get_settings.cache_clear()
    secret_registry.clear()
    vault_store.lock()
    yield
    vault_store.lock()
    secret_registry.clear()
    get_settings.cache_clear()


def make_vault(entries=None, passphrase=PASSPHRASE):
    header = vault_crypto.new_header()
    key = vault_crypto.derive_key(passphrase, header)
    plaintext = {"entries": {"work": password_entry()} if entries is None else entries}
    vault_crypto.write_file(
        vault_store.VAULT_PATH, vault_crypto.seal(key, header, plaintext)
    )
    return key


# ------------------------------------------------------------ validation


def rule_error(entries):
    with pytest.raises(ValueError) as caught:
        vault_store.validate(entries)
    return str(caught.value)


def test_a_valid_vault_passes():
    vault_store.validate({"work": password_entry(), "shop": provider_entry()})


@pytest.mark.parametrize(
    "label", ["", "Work", "-work", "work_x", "w" * 64, "a b", "wörk", "../x"]
)
def test_a_bad_label_is_refused_and_named(label):
    message = rule_error({label: password_entry()})
    assert repr(label) in message
    assert "label" in message


def test_the_longest_label_is_accepted():
    vault_store.validate({"a" + "b" * 62: password_entry()})


def test_an_unknown_kind_is_refused_and_names_the_label():
    message = rule_error({"work": password_entry(kind="token")})
    assert "'work'" in message
    assert "kind" in message


@pytest.mark.parametrize("field", ["username", "password"])
@pytest.mark.parametrize("bad", [None, "", 3])
def test_a_password_entry_needs_a_username_and_password(field, bad):
    message = rule_error({"work": password_entry(**{field: bad})})
    assert "'work'" in message
    assert field in message
    assert PASSWORD not in message


def test_a_missing_field_is_named():
    entry = password_entry()
    del entry["password"]
    assert "password" in rule_error({"work": entry})


@pytest.mark.parametrize(
    "url",
    [
        "http://accounts.example.com/",
        "ftp://accounts.example.com/",
        "accounts.example.com",
        "https://",
        "https://user:pw@accounts.example.com/",
        "",
        None,
    ],
)
def test_the_login_url_must_be_https(url):
    message = rule_error({"work": password_entry(login_url=url)})
    assert "'work'" in message
    assert "login_url" in message


def test_loopback_http_is_refused_without_the_test_variable():
    entry = password_entry(login_url="http://127.0.0.1:8123/", hosts=["127.0.0.1"])
    assert "login_url" in rule_error({"work": entry})


def test_loopback_http_is_accepted_with_the_test_variable(monkeypatch):
    set_loopback(monkeypatch, "1")
    entry = password_entry(login_url="http://127.0.0.1:8123/", hosts=["127.0.0.1"])
    vault_store.validate({"work": entry})


def test_the_test_variable_does_not_admit_other_http_hosts(monkeypatch):
    set_loopback(monkeypatch, "1")
    entry = password_entry(login_url="http://evil.example.com/")
    assert "login_url" in rule_error({"work": entry})


@pytest.mark.parametrize(
    "hosts",
    [
        [],
        "accounts.example.com",
        ["https://accounts.example.com"],
        ["accounts.example.com:443"],
        ["Accounts.Example.com"],
        ["*.example.com"],
        ["accounts.example.com/path"],
        ["accounts.example.com."],
        [""],
        ["accounts.example.com", 3],
    ],
)
def test_hosts_must_be_bare_lowercase_hostnames(hosts):
    message = rule_error({"work": password_entry(hosts=hosts)})
    assert "'work'" in message
    assert "hosts" in message


def test_hosts_must_include_the_login_urls_host():
    message = rule_error({"work": password_entry(hosts=["other.example.com"])})
    assert "'work'" in message
    assert "accounts.example.com" in message


def test_the_login_url_host_is_compared_in_lower_case():
    entry = password_entry(login_url="https://Accounts.Example.com/x")
    vault_store.validate({"work": entry})


def test_a_provider_must_name_a_password_entry():
    message = rule_error({"shop": provider_entry(via="nowhere")})
    assert "'shop'" in message
    assert "via" in message


def test_a_provider_cannot_go_via_another_provider():
    message = rule_error(
        {
            "work": password_entry(),
            "mid": provider_entry(via="work"),
            "shop": provider_entry(via="mid"),
        }
    )
    assert "'shop'" in message
    assert "via" in message


def test_a_provider_via_a_password_entry_passes():
    vault_store.validate({"work": password_entry(), "shop": provider_entry()})


def test_a_site_is_required():
    message = rule_error({"work": password_entry(site="")})
    assert "site" in message


def test_entries_must_be_an_object():
    with pytest.raises(ValueError):
        vault_store.validate([])


# ------------------------------------------------------------ unlock / lock


def test_unlock_with_the_right_key_returns_the_entry_count():
    key = make_vault({"work": password_entry(), "shop": provider_entry()})
    assert vault_store.unlock(key, None) == 2
    assert vault_store.status()["unlocked"] is True


def test_unlock_with_a_wrong_key_raises_and_stays_locked():
    make_vault()
    wrong = vault_crypto.derive_key("other", vault_crypto.new_header())
    with pytest.raises(vault_crypto.VaultError):
        vault_store.unlock(wrong, None)
    assert vault_store.status()["unlocked"] is False
    assert secret_registry.redact(PASSWORD) == PASSWORD


def test_unlock_without_a_vault_file_raises():
    key = vault_crypto.derive_key(PASSPHRASE, vault_crypto.new_header())
    with pytest.raises(vault_crypto.VaultError):
        vault_store.unlock(key, None)


def test_a_failed_unlock_leaves_an_unlocked_vault_unlocked():
    key = make_vault()
    vault_store.unlock(key, None)
    wrong = vault_crypto.derive_key("other", vault_crypto.new_header())
    with pytest.raises(vault_crypto.VaultError):
        vault_store.unlock(wrong, None)
    assert vault_store.status()["unlocked"] is True
    assert secret_registry.redact(PASSWORD) != PASSWORD


@pytest.mark.parametrize("ttl", [0, -1, -0.5])
def test_a_non_positive_ttl_is_refused(ttl):
    key = make_vault()
    with pytest.raises(ValueError):
        vault_store.unlock(key, ttl)
    assert vault_store.status()["unlocked"] is False


def test_status_before_and_after():
    assert vault_store.status() == {
        "exists": False,
        "unlocked": False,
        "expires_in": None,
    }
    key = make_vault()
    assert vault_store.status() == {
        "exists": True,
        "unlocked": False,
        "expires_in": None,
    }
    vault_store.unlock(key, None)
    assert vault_store.status() == {
        "exists": True,
        "unlocked": True,
        "expires_in": None,
    }


def test_status_reports_the_time_left(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(vault_store.time, "monotonic", lambda: clock[0])
    key = make_vault()
    vault_store.unlock(key, 60)
    clock[0] += 20
    left = vault_store.status()["expires_in"]
    assert 39 <= left <= 40


def test_the_ttl_locks_the_vault_when_it_passes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(vault_store.time, "monotonic", lambda: clock[0])
    key = make_vault()
    vault_store.unlock(key, 60)
    clock[0] += 59
    assert vault_store.status()["unlocked"] is True
    clock[0] += 2
    assert vault_store.status()["unlocked"] is False
    assert secret_registry.redact(PASSWORD) == PASSWORD


@pytest.mark.parametrize("call", ["public_entries", "entry", "freeze"])
def test_an_expired_vault_refuses_reads_and_writes(monkeypatch, call):
    clock = [1000.0]
    monkeypatch.setattr(vault_store.time, "monotonic", lambda: clock[0])
    key = make_vault()
    vault_store.unlock(key, 10)
    clock[0] += 11
    with pytest.raises(vault_store.VaultLocked):
        if call == "public_entries":
            vault_store.public_entries()
        else:
            getattr(vault_store, call)("work")


def test_lock_forgets_the_key():
    key = make_vault()
    vault_store.unlock(key, None)
    vault_store.lock()
    with pytest.raises(vault_store.VaultLocked):
        vault_store.public_entries()
    assert vault_store.status()["unlocked"] is False


def test_unlock_registers_the_passwords_and_lock_forgets_them():
    key = make_vault()
    assert secret_registry.redact(PASSWORD) == PASSWORD
    vault_store.unlock(key, None)
    assert PASSWORD not in secret_registry.redact(f"x {PASSWORD} y")
    vault_store.lock()
    assert secret_registry.redact(PASSWORD) == PASSWORD


# ------------------------------------------------------------ reading


def test_public_entries_never_carry_a_password_or_a_hidden_username():
    key = make_vault({"work": password_entry(), "shop": provider_entry()})
    vault_store.unlock(key, None)
    rows = vault_store.public_entries()
    assert [row["label"] for row in rows] == ["shop", "work"]
    text = json.dumps(rows)
    assert PASSWORD not in text
    assert all("password" not in row for row in rows)
    assert "someone@example.com" not in text
    assert all("username" not in row for row in rows)


def test_public_entries_show_the_username_only_when_asked():
    key = make_vault({"work": password_entry(show_username=True)})
    vault_store.unlock(key, None)
    (row,) = vault_store.public_entries()
    assert row["username"] == "someone@example.com"
    assert PASSWORD not in json.dumps(row)


def test_public_entry_shape():
    key = make_vault({"work": password_entry(), "shop": provider_entry()})
    vault_store.unlock(key, None)
    shop, work = vault_store.public_entries()
    assert shop == {
        "label": "shop",
        "kind": "provider",
        "site": "shop.example",
        "via": "work",
        "frozen": None,
    }
    assert work == {
        "label": "work",
        "kind": "password",
        "site": "example.com",
        "frozen": None,
    }


def test_public_entries_when_locked_raise():
    make_vault()
    with pytest.raises(vault_store.VaultLocked):
        vault_store.public_entries()


def test_entry_returns_the_password_as_a_secret():
    key = make_vault()
    vault_store.unlock(key, None)
    entry = vault_store.entry("work")
    assert isinstance(entry["password"], secret_registry.Secret)
    assert entry["password"].reveal() == PASSWORD
    assert PASSWORD not in repr(entry)
    assert entry["username"] == "someone@example.com"
    assert entry["hosts"] == ["accounts.example.com"]


def test_entry_for_a_provider_has_no_password():
    key = make_vault({"work": password_entry(), "shop": provider_entry()})
    vault_store.unlock(key, None)
    assert "password" not in vault_store.entry("shop")


def test_entry_unknown_label_is_a_key_error():
    key = make_vault()
    vault_store.unlock(key, None)
    with pytest.raises(KeyError):
        vault_store.entry("nowhere")


def test_entry_when_locked_raises():
    make_vault()
    with pytest.raises(vault_store.VaultLocked):
        vault_store.entry("work")


def test_a_returned_entry_cannot_edit_the_vault():
    key = make_vault()
    vault_store.unlock(key, None)
    vault_store.entry("work")["hosts"].append("evil.example.com")
    assert vault_store.entry("work")["hosts"] == ["accounts.example.com"]


# ------------------------------------------------------------ freeze


def test_freeze_marks_the_entry_and_survives_lock_and_unlock():
    key = make_vault()
    vault_store.unlock(key, None)
    assert vault_store.entry("work")["frozen"] is None
    vault_store.freeze("work")
    stamp = vault_store.entry("work")["frozen"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\+00:00|Z)", stamp)
    vault_store.lock()
    vault_store.unlock(key, None)
    assert vault_store.entry("work")["frozen"] == stamp
    assert vault_store.public_entries()[0]["frozen"] == stamp


def test_freeze_rewrites_the_file_with_a_new_nonce_and_keeps_the_header():
    key = make_vault()
    before = vault_crypto.read_file(vault_store.VAULT_PATH)
    vault_store.unlock(key, None)
    vault_store.freeze("work")
    after = vault_crypto.read_file(vault_store.VAULT_PATH)
    assert after["nonce"] != before["nonce"]
    assert after["kdf"] == before["kdf"]
    opened = vault_crypto.open_sealed(key, after)
    assert opened["entries"]["work"]["password"] == PASSWORD
    assert opened["entries"]["work"]["frozen"]


def test_freeze_unknown_label_is_a_key_error():
    key = make_vault()
    vault_store.unlock(key, None)
    with pytest.raises(KeyError):
        vault_store.freeze("nowhere")


def test_freeze_when_locked_raises_and_writes_nothing():
    make_vault()
    before = vault_store.VAULT_PATH.read_bytes()
    with pytest.raises(vault_store.VaultLocked):
        vault_store.freeze("work")
    assert vault_store.VAULT_PATH.read_bytes() == before


def _another_writer_lands(monkeypatch, make_change):
    """Run ``make_change(key)`` (a CLI-style write of a NEW version of the file)
    right after the first read of the file: the window the review named."""
    real = vault_store._read_document
    state = {"done": False}

    def read():
        document = real()
        if not state["done"]:
            state["done"] = True
            make_change()
        return document

    monkeypatch.setattr(vault_store, "_read_document", read)


def test_freeze_does_not_erase_an_edit_that_lands_during_it(monkeypatch):
    """The CLI edits ANOTHER entry between freeze's read and its replace: the
    edit survives, and so does the freeze (it is applied again to the new file)."""
    key = make_vault({"work": password_entry(), "other": password_entry()})
    vault_store.unlock(key, None)

    def cli_edit():
        document = vault_crypto.read_file(vault_store.VAULT_PATH)
        plain = vault_crypto.open_sealed(key, document)
        plain["entries"]["other"]["username"] = "edited@example.com"
        header = {"v": document["v"], "kdf": document["kdf"]}
        vault_crypto.write_file(
            vault_store.VAULT_PATH, vault_crypto.seal(key, header, plain)
        )

    _another_writer_lands(monkeypatch, cli_edit)
    vault_store.freeze("work")

    entries = vault_crypto.open_sealed(
        key, vault_crypto.read_file(vault_store.VAULT_PATH)
    )["entries"]
    assert entries["other"]["username"] == "edited@example.com"
    assert entries["work"]["frozen"]


def test_freeze_gives_up_when_the_file_never_holds_still(monkeypatch):
    key = make_vault()
    vault_store.unlock(key, None)

    def always_changing():
        document = vault_crypto.read_file(vault_store.VAULT_PATH)
        plain = vault_crypto.open_sealed(key, document)
        header = {"v": document["v"], "kdf": document["kdf"]}
        vault_crypto.write_file(
            vault_store.VAULT_PATH, vault_crypto.seal(key, header, plain)
        )

    real = vault_store._read_document

    def read():
        document = real()
        always_changing()
        return document

    monkeypatch.setattr(vault_store, "_read_document", read)
    with pytest.raises(vault_crypto.VaultError, match="kept changing"):
        vault_store.freeze("work")
    monkeypatch.setattr(vault_store, "_read_document", real)
    document = vault_crypto.read_file(vault_store.VAULT_PATH)
    assert not vault_crypto.open_sealed(key, document)["entries"]["work"]["frozen"]


def test_freeze_keeps_every_top_level_key_of_the_plaintext():
    header = vault_crypto.new_header()
    key = vault_crypto.derive_key(PASSPHRASE, header)
    plain = {"entries": {"work": password_entry()}, "future": {"kept": 1}}
    vault_crypto.write_file(
        vault_store.VAULT_PATH, vault_crypto.seal(key, header, plain)
    )
    vault_store.unlock(key, None)
    vault_store.freeze("work")
    opened = vault_crypto.open_sealed(
        key, vault_crypto.read_file(vault_store.VAULT_PATH)
    )
    assert opened["future"] == {"kept": 1}
    assert opened["entries"]["work"]["frozen"]


def test_a_write_expecting_an_old_version_is_refused_and_writes_nothing():
    key = make_vault()
    path = vault_store.VAULT_PATH
    first = vault_crypto.read_file(path)
    header = {"v": first["v"], "kdf": first["kdf"]}
    vault_crypto.write_file(path, vault_crypto.seal(key, header, {"entries": {}}))
    before = path.read_bytes()
    with pytest.raises(vault_crypto.VaultChanged):
        vault_crypto.write_file(
            path,
            vault_crypto.seal(key, header, {"entries": {"x": 1}}),
            expect=first["nonce"],
        )
    assert path.read_bytes() == before


def test_the_write_lock_is_released_and_a_stale_one_is_taken_over(monkeypatch):
    key = make_vault()
    path = vault_store.VAULT_PATH
    lock = path.with_name(f".{path.name}.lock")
    document = vault_crypto.read_file(path)
    header = {"v": document["v"], "kdf": document["kdf"]}
    vault_crypto.write_file(path, vault_crypto.seal(key, header, {"entries": {}}))
    assert not lock.exists()
    # a lock a crashed writer left behind, older than the stale limit
    lock.write_text("")
    import os
    import time

    old = time.time() - vault_crypto._LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    vault_crypto.write_file(path, vault_crypto.seal(key, header, {"entries": {}}))
    assert not lock.exists()


def test_a_live_lock_makes_a_writer_wait_and_then_refuse(monkeypatch):
    key = make_vault()
    path = vault_store.VAULT_PATH
    lock = path.with_name(f".{path.name}.lock")
    lock.write_text("")  # fresh: another writer is in its critical section
    monkeypatch.setattr(vault_crypto, "_LOCK_WAIT_S", 0.1)
    document = vault_crypto.read_file(path)
    header = {"v": document["v"], "kdf": document["kdf"]}
    before = path.read_bytes()
    with pytest.raises(vault_crypto.VaultError, match="another process"):
        vault_crypto.write_file(path, vault_crypto.seal(key, header, {"entries": {}}))
    assert path.read_bytes() == before
    assert lock.exists()  # not ours: left alone


# ------------------------------------------------------------ audit


def test_the_audit_line_is_json_with_exactly_five_keys():
    vault_store.audit("work", "instance-1", "caller-9", "logged_in")
    (line,) = vault_store.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert set(record) == {"time", "label", "instance", "caller", "outcome"}
    assert record["label"] == "work"
    assert record["instance"] == "instance-1"
    assert record["caller"] == "caller-9"
    assert record["outcome"] == "logged_in"
    assert re.match(r"\d{4}-\d\d-\d\dT", record["time"])


def test_audit_lines_append():
    vault_store.audit("a", "i1", "c", "logged_in")
    vault_store.audit("b", "i2", "c", "rejected")
    lines = vault_store.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["label"] for x in lines] == ["a", "b"]


def test_audit_lines_never_hold_the_password_or_username():
    key = make_vault()
    vault_store.unlock(key, None)
    vault_store.audit("work", "i", "c", "logged_in")
    text = vault_store.AUDIT_PATH.read_text(encoding="utf-8")
    assert PASSWORD not in text
    assert "someone@example.com" not in text


def test_audit_masks_a_registered_secret_in_any_field():
    key = make_vault()
    vault_store.unlock(key, None)
    vault_store.audit("work", "i", f"caller-{PASSWORD}", "logged_in")
    assert PASSWORD not in vault_store.AUDIT_PATH.read_text(encoding="utf-8")


def test_an_unwritable_audit_path_does_not_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(vault_store, "AUDIT_PATH", blocker / "audit.jsonl")
    vault_store.audit("work", "i", "c", "logged_in")  # must not raise


def test_loopback_helper_follows_the_variable(monkeypatch):
    assert vault_store.loopback_allowed() is False
    set_loopback(monkeypatch, "1")
    assert vault_store.loopback_allowed() is True
    set_loopback(monkeypatch, "0")
    assert vault_store.loopback_allowed() is False
