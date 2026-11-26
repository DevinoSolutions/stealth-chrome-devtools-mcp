"""Shared mechanism for the login vault's tests (T-19366): entry builders and a
vault file under a temporary directory. No test logic lives here."""

from __future__ import annotations

import contextlib

from stealth_chrome_devtools_mcp.embedded import (
    login_seal,
    secret_registry,
    vault_crypto,
    vault_store,
)

PASSPHRASE = "correct horse"
LOOPBACK_ENV = "STEALTH_MCP_VAULT_ALLOW_HTTP_LOOPBACK"


def password_entry(**overrides):
    entry = {
        "kind": "password",
        "site": "example.com",
        "login_url": "https://accounts.example.com/signin",
        "hosts": ["accounts.example.com"],
        "username": "someone@example.com",
        "password": "Tr0ub4dor&3-marker",
        "show_username": False,
        "frozen": None,
    }
    entry.update(overrides)
    return entry


def provider_entry(**overrides):
    entry = {"kind": "provider", "site": "shop.example", "via": "work"}
    entry.update(overrides)
    return entry


def make_vault(entries, passphrase=PASSPHRASE):
    """Write ``vault_store.VAULT_PATH`` (point it at a tmp path first) and return
    the key. The KDF cost is whatever ``vault_crypto.SCRYPT_N`` is at the time:
    patch it down in the caller."""
    header = vault_crypto.new_header()
    key = vault_crypto.derive_key(passphrase, header)
    vault_crypto.write_file(
        vault_store.VAULT_PATH, vault_crypto.seal(key, header, {"entries": entries})
    )
    return key


def unlocked_vault(entries):
    """Make the vault, unlock it, and return the key."""
    key = make_vault(entries)
    vault_store.unlock(key, None)
    return key


@contextlib.contextmanager
def spawn_before_the_vault():
    """Spawn a browser the way one that was started BEFORE the vault existed was.

    Vault mode (``vault_mode``) refuses a ``user_data_dir`` PATH and every switch off
    its allow-list, while a vault file exists. These tests need a profile at a known
    path, or a switch a login must refuse (a browser an older backend started), so
    the vault file is set aside for the spawn and put back after it. The key, if any,
    stays in memory."""
    path = vault_store.VAULT_PATH
    aside = path.with_name(path.name + ".aside")
    present = path.exists()
    if present:
        path.replace(aside)
    try:
        yield
    finally:
        if present:
            aside.replace(path)


def reset():
    vault_store.lock()
    secret_registry.clear()
    login_seal._depth.clear()
    login_seal._ports.clear()
    login_seal._tabs.clear()
    login_seal._unclosed.clear()
    login_seal._holds.clear()
    login_seal._barred.clear()
    login_seal._internal.clear()
    login_seal._webui.clear()
    login_seal._released.clear()


def sealed_state():
    """What ``login_seal`` still holds, for the message of an ``any_sealing`` assert
    (a red CI run then names the set instead of saying ``assert not True``)."""
    names = (
        "_depth",
        "_unclosed",
        "_holds",
        "_barred",
        "_internal",
        "_webui",
        "_released",
    )
    return {
        name: getattr(login_seal, name) for name in names if getattr(login_seal, name)
    }


def fake_account_store(monkeypatch, *, keeps=True, kind="keychain"):
    """Stand a dict in for the macOS keychain (or, ``kind="secret-service"``, a
    Linux keyring): ``device_key`` runs its real command lines against it, so
    what is checked is the argv and the stdin it builds. ``keeps=False`` is a
    store that accepts a write and then hands back something else. Returns the
    dict (item id -> hex secret). It replaces ``_tool_run``, so exit codes and
    timeouts are ``test_device_key``'s, against ``subprocess.run`` itself."""
    import shlex

    from stealth_chrome_devtools_mcp.embedded import device_key

    items = {}
    service = device_key._SERVICE

    def answer(verb, item, secret):
        if verb == "add":
            items[item] = secret if keeps else "00" * 32
            return ""
        if item not in items:
            raise device_key.DeviceKeyError(device_key.MOVED)
        return items[item] if verb == "find" else items.pop(item) and ""

    def security(argv, stdin=None, lookup_in=None):
        assert argv[0] == "security", argv
        words = shlex.split(stdin) if argv[1] == "-i" else argv[1:]
        flags = dict(zip(words[1::2], words[2::2], strict=False))
        assert flags["-s"] == service
        verb = words[0].removesuffix("-generic-password")
        assert verb in {"add", "find", "delete"}, words
        assert (verb == "add") == (argv == ["security", "-i"]), "secret on stdin only"
        assert (lookup_in == "keychain") == (verb == "find")
        return answer(verb, flags["-a"], flags.get("-w"))

    def secret_tool(argv, stdin=None, lookup_in=None):
        tool, verb, *rest = argv
        assert tool == "secret-tool", argv
        if verb == "store":
            assert rest[:2] == ["--label", service]
            rest = rest[2:]
        assert rest[:3] == ["service", service, "account"], argv
        assert len(rest) == 4, "the secret is never an argument"
        assert (lookup_in == "secret-service") == (verb == "lookup")
        verbs = {"store": "add", "lookup": "find", "clear": "delete"}
        return answer(verbs[verb], rest[3], stdin)

    run = security if kind == "keychain" else secret_tool
    monkeypatch.setattr(device_key, "_native", lambda: kind)
    monkeypatch.setattr(device_key, "store_name", lambda: kind)
    monkeypatch.setattr(device_key, "_tool_run", run)
    return items
