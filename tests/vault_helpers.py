"""Shared mechanism for the login vault's tests (T-19366): entry builders and a
vault file under a temporary directory. No test logic lives here."""

from __future__ import annotations

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


def reset():
    vault_store.lock()
    secret_registry.clear()
    login_seal._depth.clear()
    login_seal._tabs.clear()
    login_seal._unclosed.clear()
    login_seal._holds.clear()
