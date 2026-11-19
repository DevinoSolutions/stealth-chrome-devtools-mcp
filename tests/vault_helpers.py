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


def sealed_state():
    """What ``login_seal`` still holds, for the message of an ``any_sealing`` assert
    (a red CI run then names the set instead of saying ``assert not True``)."""
    names = ("_depth", "_unclosed", "_holds", "_barred", "_internal", "_webui")
    return {
        name: getattr(login_seal, name) for name in names if getattr(login_seal, name)
    }
