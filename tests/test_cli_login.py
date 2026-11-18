"""``stealthy login``: a person's side of the login vault (T-19366).

Driven through ``cli.main`` so the parser, the dispatch and the verb are one path.
Prompts are scripted (``getpass.getpass``, ``input``, ``sys.stdin``); the backend,
where a test needs one, is a real Starlette app carrying the real vault routes and
the real bearer-token middleware, so an unlock is the same request the CLI sends.
"""

from __future__ import annotations

import base64
import getpass
import io
import json

import pytest
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from stealth_chrome_devtools_mcp import cli, cli_login
from stealth_chrome_devtools_mcp.embedded import (
    backend_token,
    vault_crypto,
    vault_routes,
    vault_store,
)
from vault_helpers import PASSPHRASE, make_vault, password_entry, provider_entry, reset

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PASSWORD = "Tr0ub4dor&3-marker"
URL = "https://accounts.example.com/signin"


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool = True) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_crypto, "SCRYPT_N", 2**10)
    monkeypatch.setattr(vault_store, "VAULT_PATH", tmp_path / "vault.json")
    monkeypatch.setattr(vault_store, "AUDIT_PATH", tmp_path / "audit.jsonl")
    reset()
    # No backend unless a test installs one: never touch a real one.
    monkeypatch.setattr(cli_login, "_request", _no_backend)
    yield
    reset()


def _no_backend(*_args, **_kwargs):
    raise cli_login._NoBackendError("no backend is serving this shell (state: none)")


class Terminal:
    """Scripted answers for the hidden prompts and ``input``."""

    def __init__(self, monkeypatch, capsys) -> None:
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.prompts: list[str] = []

    def run(self, *argv, hidden=(), typed=(), stdin: FakeStdin | None = None):
        self.prompts = []
        answers = iter(hidden)
        lines = iter(typed)

        def getpass_(prompt="Password: ", stream=None):
            self.prompts.append(prompt)
            return next(answers)

        self.monkeypatch.setattr(getpass, "getpass", getpass_)
        self.monkeypatch.setattr("builtins.input", lambda _prompt="": next(lines))
        self.monkeypatch.setattr("sys.stdin", stdin or FakeStdin())
        code = cli.main(["login", *argv])
        out = self.capsys.readouterr()
        self.out, self.err = out.out, out.err
        return code


@pytest.fixture
def term(monkeypatch, capsys):
    return Terminal(monkeypatch, capsys)


def decrypted():
    document = vault_crypto.read_file(vault_store.VAULT_PATH)
    key = vault_crypto.derive_key(PASSPHRASE, document)
    return vault_crypto.open_sealed(key, document)["entries"]


def init(term):
    assert term.run("init", hidden=[PASSPHRASE, PASSPHRASE]) == 0


def add_work(term, **extra):
    args = [
        "add",
        "work",
        "--site",
        "example.com",
        "--login-url",
        URL,
        "--user",
        "a@b.co",
    ]
    return term.run(*args, hidden=[PASSPHRASE, PASSWORD, PASSWORD], **extra)


def test_the_verb_is_one_dispatch_entry_and_one_parser():
    assert cli._DISPATCH["login"] is cli_login.cmd_login
    assert cli_login.DISPATCH["login"] is cli_login.cmd_login
    offered = cli.build_parser()._subparsers._group_actions[0].choices
    assert "login" in offered


def test_init_add_list_round_trip_prints_no_password_and_no_username(term):
    init(term)
    assert vault_store.VAULT_PATH.exists()
    assert add_work(term) == 0
    assert term.run("list", hidden=[PASSPHRASE]) == 0
    assert "work" in term.out
    assert "example.com" in term.out
    assert "accounts.example.com" in term.out
    assert PASSWORD not in term.out + term.err
    assert "a@b.co" not in term.out + term.err
    assert "ok" in term.out


def test_list_with_usernames_prints_the_username_and_still_no_password(term):
    init(term)
    add_work(term)
    assert term.run("list", "--usernames", hidden=[PASSPHRASE]) == 0
    assert "a@b.co" in term.out
    assert PASSWORD not in term.out + term.err


def test_the_vault_file_holds_neither_the_password_nor_the_label(term):
    init(term)
    add_work(term)
    raw = vault_store.VAULT_PATH.read_bytes()
    assert PASSWORD.encode() not in raw
    assert b"example.com" not in raw
    assert decrypted()["work"]["password"] == PASSWORD


def test_the_login_url_host_is_the_default_allowed_host(term):
    init(term)
    add_work(term)
    assert decrypted()["work"]["hosts"] == ["accounts.example.com"]


def test_an_explicit_host_list_replaces_the_default(term):
    init(term)
    args = ["add", "w", "--site", "example.com", "--login-url", URL, "--user", "u"]
    args += ["--host", "Accounts.Example.com", "--host", "sso.example.com"]
    assert term.run(*args, hidden=[PASSPHRASE, PASSWORD, PASSWORD]) == 0
    assert decrypted()["w"]["hosts"] == ["accounts.example.com", "sso.example.com"]


def test_add_has_no_password_value_option(term, capsys):
    init(term)
    for tail in (["--password", "x"], ["--password"], ["--password=x"]):
        with pytest.raises(SystemExit) as stop:
            term.run("add", "w", "--site", "s", "--login-url", URL, *tail)
        assert stop.value.code == 2
    assert "work" not in decrypted()


def test_a_password_cannot_come_from_the_environment(term, monkeypatch):
    monkeypatch.setenv("VAULT_PASSWORD", PASSWORD)
    init(term)
    # The only password sources are the hidden prompt and --password-stdin.
    assert term.run(
        "add", "w", "--site", "s", "--login-url", URL, "--user", "u",
        hidden=[PASSPHRASE, "typed-one", "typed-one"],
    ) == 0  # fmt: skip
    assert decrypted()["w"]["password"] == "typed-one"


@pytest.mark.parametrize("verb", ["init", "unlock"])
def test_init_and_unlock_refuse_a_stdin_that_is_not_a_terminal(term, verb):
    code = term.run(verb, hidden=[PASSPHRASE, PASSPHRASE], stdin=FakeStdin(tty=False))
    assert code == 2
    assert term.prompts == []  # it never even asked
    assert "terminal" in term.err
    assert not vault_store.VAULT_PATH.exists()


def test_init_refuses_to_overwrite_a_vault(term):
    init(term)
    before = vault_store.VAULT_PATH.read_bytes()
    assert term.run("init", hidden=["other", "other"]) == 1
    assert "already exists" in term.err
    assert vault_store.VAULT_PATH.read_bytes() == before


def test_init_with_two_different_passphrases_creates_nothing(term):
    assert term.run("init", hidden=["one", "two"]) == 1
    assert not vault_store.VAULT_PATH.exists()


def test_a_wrong_passphrase_changes_nothing(term):
    init(term)
    add_work(term)
    before = vault_store.VAULT_PATH.read_bytes()
    assert term.run("remove", "work", hidden=["not the passphrase"]) == 1
    assert "wrong passphrase" in term.err
    assert vault_store.VAULT_PATH.read_bytes() == before


def test_a_missing_vault_says_how_to_make_one(term):
    assert term.run("list", hidden=[PASSPHRASE]) == 1
    assert "stealthy login init" in term.err


def test_two_different_password_entries_store_nothing(term):
    init(term)
    code = term.run(
        "add", "w", "--site", "s", "--login-url", URL, "--user", "u",
        hidden=[PASSPHRASE, "one", "two"],
    )  # fmt: skip
    assert code == 1
    assert "w" not in decrypted()


def test_password_stdin_reads_the_first_line_and_needs_a_user(term):
    init(term)
    ok = term.run(
        "add", "w", "--site", "s", "--login-url", URL, "--user", "u", "--password-stdin",
        hidden=[PASSPHRASE], stdin=FakeStdin(PASSWORD + "\nsecond line\n"),
    )  # fmt: skip
    assert ok == 0
    assert decrypted()["w"]["password"] == PASSWORD
    bad = term.run(
        "add", "x", "--site", "s", "--login-url", URL, "--password-stdin",
        hidden=[PASSPHRASE], stdin=FakeStdin("pw\n"),
    )  # fmt: skip
    assert bad == 2
    assert "x" not in decrypted()


def test_the_username_is_asked_for_when_not_given(term):
    init(term)
    code = term.run(
        "add", "w", "--site", "s", "--login-url", URL,
        hidden=[PASSPHRASE, PASSWORD, PASSWORD], typed=["typed@user.co"],
    )  # fmt: skip
    assert code == 0
    assert decrypted()["w"]["username"] == "typed@user.co"


def test_a_bad_entry_is_refused_before_anything_is_written(term):
    init(term)
    before = vault_store.VAULT_PATH.read_bytes()
    code = term.run(
        "add", "w", "--site", "s", "--login-url", "http://accounts.example.com/", "--user", "u",
        hidden=[PASSPHRASE, PASSWORD, PASSWORD],
    )  # fmt: skip
    assert code == 1
    assert "nothing was changed" in term.err
    assert PASSWORD not in term.err
    assert vault_store.VAULT_PATH.read_bytes() == before


def test_add_refuses_an_existing_label(term):
    init(term)
    add_work(term)
    assert add_work(term) == 1
    assert "already exists" in term.err


def test_a_provider_entry_asks_for_no_secret(term):
    init(term)
    add_work(term)
    code = term.run(
        "add", "shop", "--site", "shop.example", "--via", "work", "--allow-consent",
        hidden=[PASSPHRASE],
    )  # fmt: skip
    assert code == 0
    assert term.prompts == ["passphrase: "]
    assert decrypted()["shop"] == {
        "kind": "provider",
        "site": "shop.example",
        "via": "work",
        "allow_consent": True,
    }


def test_a_provider_entry_refuses_flags_that_only_fit_a_password_before_asking(term):
    init(term)
    code = term.run(
        "add",
        "shop",
        "--site",
        "s",
        "--via",
        "work",
        "--user",
        "u",
        hidden=[PASSPHRASE],
    )
    assert code == 2
    assert term.prompts == []


def test_a_provider_must_name_a_password_entry(term):
    init(term)
    code = term.run("add", "shop", "--site", "s", "--via", "ghost", hidden=[PASSPHRASE])
    assert code == 1
    assert "via must name a password entry" in term.err


def test_list_shows_providers_and_frozen_entries(term):
    stamp = "2026-11-18T10:00:00+00:00"
    make_vault(
        {
            "work": password_entry(frozen=stamp),
            "shop": provider_entry(via="work"),
        }
    )
    assert term.run("list", hidden=[PASSPHRASE]) == 0
    assert f"FROZEN {stamp}" in term.out
    assert "via work" in term.out
    assert "Tr0ub4dor" not in term.out + term.err


def test_edit_clears_frozen_and_changes_only_what_it_was_given(term):
    make_vault({"work": password_entry(frozen="2026-11-18T10:00:00+00:00")})
    assert (
        term.run("edit", "work", "--site", "renamed.example", hidden=[PASSPHRASE]) == 0
    )
    entry = decrypted()["work"]
    assert entry["frozen"] is None
    assert entry["site"] == "renamed.example"
    assert entry["password"] == password_entry()["password"]


def test_edit_with_no_changes_unfreezes(term):
    make_vault({"work": password_entry(frozen="2026-11-18T10:00:00+00:00")})
    assert term.run("edit", "work", hidden=[PASSPHRASE]) == 0
    assert decrypted()["work"]["frozen"] is None


def test_edit_password_flag_asks_for_a_new_password(term):
    make_vault({"work": password_entry()})
    code = term.run(
        "edit", "work", "--password", hidden=[PASSPHRASE, "new-pw", "new-pw"]
    )
    assert code == 0
    assert decrypted()["work"]["password"] == "new-pw"


def test_edit_hosts_replace_the_list_and_show_username_toggles(term):
    make_vault({"work": password_entry()})
    args = [
        "edit",
        "work",
        "--host",
        "accounts.example.com",
        "--host",
        "sso.example.com",
    ]
    assert term.run(*args, "--show-username", hidden=[PASSPHRASE]) == 0
    entry = decrypted()["work"]
    assert entry["hosts"] == ["accounts.example.com", "sso.example.com"]
    assert entry["show_username"] is True
    assert term.run("edit", "work", "--no-show-username", hidden=[PASSPHRASE]) == 0
    assert decrypted()["work"]["show_username"] is False


def test_edit_refuses_a_flag_that_does_not_fit_the_kind(term):
    make_vault({"work": password_entry(), "shop": provider_entry()})
    assert term.run("edit", "shop", "--user", "u", hidden=[PASSPHRASE]) == 2
    assert term.run("edit", "work", "--via", "x", hidden=[PASSPHRASE]) == 2


def test_edit_an_unknown_label(term):
    make_vault({"work": password_entry()})
    assert term.run("edit", "ghost", hidden=[PASSPHRASE]) == 1
    assert "no login" in term.err


def test_remove_removes(term):
    make_vault({"work": password_entry(), "other": password_entry()})
    assert term.run("remove", "other", hidden=[PASSPHRASE]) == 0
    assert list(decrypted()) == ["work"]


def test_remove_refuses_a_login_a_provider_depends_on(term):
    make_vault({"work": password_entry(), "shop": provider_entry(via="work")})
    before = vault_store.VAULT_PATH.read_bytes()
    assert term.run("remove", "work", hidden=[PASSPHRASE]) == 1
    assert "via must name a password entry" in term.err
    assert vault_store.VAULT_PATH.read_bytes() == before


def test_remove_an_unknown_label(term):
    make_vault({"work": password_entry()})
    assert term.run("remove", "ghost", hidden=[PASSPHRASE]) == 1


def test_a_cancelled_prompt_changes_nothing_and_exits_130(term, monkeypatch):
    make_vault({"work": password_entry()})
    before = vault_store.VAULT_PATH.read_bytes()

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(getpass, "getpass", interrupted)
    monkeypatch.setattr("sys.stdin", FakeStdin())
    assert cli.main(["login", "remove", "work"]) == 130
    assert vault_store.VAULT_PATH.read_bytes() == before


# ------------------------------------------------------------ the running backend


@pytest.fixture
def backend(monkeypatch):
    """A real backend app: the vault routes behind the bearer-token middleware."""
    server = FastMCP("cli-login-test")
    vault_routes.install(server)
    app = server.http_app(
        path="/mcp/",
        middleware=[Middleware(backend_token.BearerTokenMiddleware, token=TOKEN)],
    )
    client = TestClient(app)
    sent: list[tuple[str, str]] = []

    def request(method, path, body=None):
        sent.append((method, path))
        reply = client.request(method, path, json=body, headers=AUTH)
        return reply.status_code, reply.json()

    monkeypatch.setattr(cli_login, "_request", request)
    return sent


def test_unlock_hands_the_key_to_the_backend_and_prints_no_secret(term, backend):
    make_vault({"work": password_entry()})
    assert term.run("unlock", hidden=[PASSPHRASE]) == 0
    assert vault_store.status()["unlocked"] is True
    assert vault_store.status()["expires_in"] is None
    assert "1 logins" in term.out
    assert PASSPHRASE not in term.out + term.err
    assert PASSWORD not in term.out + term.err
    assert [m for m, _ in backend] == ["POST"]


def test_unlock_for_sets_the_expiry(term, backend):
    make_vault({"work": password_entry()})
    assert term.run("unlock", "--for", "8h", hidden=[PASSPHRASE]) == 0
    assert 28000 < vault_store.status()["expires_in"] <= 28800


def test_unlock_with_a_wrong_passphrase_never_reaches_the_backend(term, backend):
    make_vault({"work": password_entry()})
    assert term.run("unlock", hidden=["wrong"]) == 1
    assert backend == []
    assert vault_store.status()["unlocked"] is False


@pytest.mark.parametrize("text", ["0", "-5h", "soon", "8x", ""])
def test_a_bad_duration_is_a_usage_error(term, text):
    with pytest.raises(SystemExit) as stop:
        term.run("unlock", "--for", text, hidden=[PASSPHRASE])
    assert stop.value.code == 2


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30", 30), ("30s", 30), ("5m", 300), ("8h", 28800), ("1d", 86400)],
)
def test_durations(text, seconds):
    assert cli_login._duration(text) == seconds


def test_unlock_without_a_backend_exits_3(term):
    make_vault({"work": password_entry()})
    assert term.run("unlock", hidden=[PASSPHRASE]) == 3
    assert "no backend" in term.err


def test_lock_drops_the_key_and_status_says_so(term, backend):
    key = make_vault({"work": password_entry()})
    vault_store.unlock(key, None)
    assert term.run("status") == 0
    assert "unlocked until it exits" in term.out
    assert term.run("lock") == 0
    assert vault_store.status()["unlocked"] is False
    assert term.run("status") == 0
    assert "backend: locked" in term.out


def test_status_and_lock_without_a_backend_are_news_not_errors(term):
    make_vault({"work": password_entry()})
    assert term.run("status") == 0
    assert "exists" in term.out
    assert "no backend" in term.out
    assert term.run("lock") == 0


def test_an_edit_reloads_an_unlocked_backend_and_keeps_its_remaining_time(
    term, backend
):
    key = make_vault({"work": password_entry()})
    vault_store.unlock(key, 600)
    assert (
        term.run(
            "edit", "work", "--password", hidden=[PASSPHRASE, "fresh-pw", "fresh-pw"]
        )
        == 0
    )
    assert ("POST", vault_routes.UNLOCK_PATH) in backend
    left = vault_store.status()["expires_in"]
    assert left is not None
    assert left <= 600  # a limited unlock never becomes unlimited
    from stealth_chrome_devtools_mcp.embedded import secret_registry

    assert secret_registry.redact("x fresh-pw y") == "x [vault-secret] y"


def test_a_write_does_not_unlock_a_locked_backend(term, backend):
    make_vault({"work": password_entry()})
    assert term.run("remove", "work", hidden=[PASSPHRASE]) == 0
    assert vault_store.status()["unlocked"] is False
    assert [m for m, _ in backend] == ["GET"]


def test_a_write_with_no_backend_just_succeeds(term):
    make_vault({"work": password_entry()})
    assert term.run("remove", "work", hidden=[PASSPHRASE]) == 0
    assert term.err == ""


def test_the_key_is_sent_as_base64_of_32_bytes(term, monkeypatch):
    make_vault({"work": password_entry()})
    seen = {}

    def request(method, path, body=None):
        seen["body"] = body
        return 200, {"unlocked": True, "entries": 1}

    monkeypatch.setattr(cli_login, "_request", request)
    assert term.run("unlock", hidden=[PASSPHRASE]) == 0
    assert len(base64.b64decode(seen["body"]["key"])) == 32
    assert seen["body"]["ttl_seconds"] is None


# ------------------------------------------------------------------------- audit


def test_audit_prints_the_last_lines(term):
    lines = [
        {
            "time": f"t{n}",
            "label": "work",
            "instance": "i",
            "caller": "c",
            "outcome": "logged_in",
        }
        for n in range(5)
    ]
    vault_store.AUDIT_PATH.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\nnot json\n", encoding="utf-8"
    )
    assert term.run("audit", "-n", "2") == 0
    assert term.out.splitlines() == ["t4  work  i  c  logged_in", "not json"]


def test_audit_with_no_log_yet(term):
    assert term.run("audit") == 0
    assert "no login has been attempted" in term.out
