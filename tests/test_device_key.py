"""The vault's second factor (T-22092): ``device_key`` and format version 2.

The real store is exercised where it can be without leaving anything behind:
Windows DPAPI keeps nothing outside the blob it returns. The macOS and Linux
tools are driven through ``vault_helpers.fake_account_store``, which checks the
command lines ``device_key`` builds; neither tool runs here.
"""

from __future__ import annotations

import base64
import copy
import sys

import pytest

from stealth_chrome_devtools_mcp.embedded import device_key, vault_crypto
from vault_helpers import fake_account_store

#: Captured at import, before ``conftest`` stands every test on "no store".
REAL_STORE_NAME = device_key.store_name

windows = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows")


@pytest.fixture(autouse=True)
def _cheap_kdf(monkeypatch):
    monkeypatch.setattr(vault_crypto, "SCRYPT_N", 2**10)


def sealed(passphrase="pw"):
    header = vault_crypto.new_header()
    key = vault_crypto.derive_key(passphrase, header)
    return key, vault_crypto.seal(key, header, {"entries": {"a": {}}})


def test_without_a_store_a_new_vault_is_version_1_and_passphrase_only():
    header = vault_crypto.new_header()
    assert header["v"] == vault_crypto.VERSION
    assert "device" not in header
    assert not vault_crypto.is_bound(header)


@windows
def test_dpapi_gives_the_secret_back_and_refuses_a_changed_blob(monkeypatch):
    monkeypatch.setattr(device_key, "store_name", REAL_STORE_NAME)
    descriptor = device_key.create()
    assert descriptor is not None
    assert descriptor["store"] == "dpapi"
    secret = device_key.load(descriptor)
    assert len(secret) == device_key.SECRET_BYTES
    assert device_key.load(descriptor) == secret
    raw = base64.b64decode(descriptor["blob"])
    assert secret not in raw
    changed = bytearray(raw)
    changed[-5] ^= 1
    with pytest.raises(device_key.DeviceKeyError):
        device_key.load({"store": "dpapi", "blob": base64.b64encode(changed).decode()})


@windows
def test_a_vault_bound_with_dpapi_opens_and_names_no_second_file(monkeypatch):
    monkeypatch.setattr(device_key, "store_name", REAL_STORE_NAME)
    key, document = sealed()
    assert document["v"] == vault_crypto.BOUND_VERSION
    again = vault_crypto.derive_key("pw", document)
    assert again == key
    assert vault_crypto.open_sealed(again, document) == {"entries": {"a": {}}}


def test_a_tool_store_gets_the_secret_on_stdin_and_gives_it_back(monkeypatch):
    items = fake_account_store(monkeypatch)
    descriptor = device_key.create()
    assert descriptor is not None
    assert descriptor["store"] == "keychain"
    assert bytes.fromhex(items[descriptor["id"]]) == device_key.load(descriptor)
    device_key.forget(descriptor)
    assert items == {}
    with pytest.raises(device_key.DeviceKeyError):
        device_key.load(descriptor)


def test_a_store_that_hands_back_something_else_is_not_believed(monkeypatch):
    fake_account_store(monkeypatch, keeps=False)
    assert device_key.create() is None
    assert vault_crypto.new_header()["v"] == vault_crypto.VERSION


@pytest.mark.parametrize(
    "descriptor",
    [
        None,
        "keychain",
        {},
        {"store": "secret-service", "id": "ab12"},  # another platform's store
        {"store": "dpapi", "blob": "AAAA"},
        {"store": "dpapi", "blob": 7},
        {"store": "keychain"},
        {"store": "keychain", "id": 'a" -s other'},  # never reaches a command line
        {"store": "keychain", "id": "absent"},
    ],
)
def test_a_descriptor_this_account_cannot_answer_is_refused(monkeypatch, descriptor):
    fake_account_store(monkeypatch)
    with pytest.raises(device_key.DeviceKeyError) as refusal:
        device_key.load(descriptor)
    assert str(refusal.value) == device_key.MOVED


def test_a_bound_vault_needs_the_passphrase_and_the_accounts_secret(monkeypatch):
    items = fake_account_store(monkeypatch)
    key, document = sealed()
    assert document["v"] == vault_crypto.BOUND_VERSION
    assert vault_crypto.open_sealed(vault_crypto.derive_key("pw", document), document)

    # The right secret, a wrong passphrase.
    with pytest.raises(vault_crypto.VaultError) as wrong:
        vault_crypto.open_sealed(vault_crypto.derive_key("other", document), document)
    assert vault_crypto.refusal(wrong.value).startswith("wrong passphrase")

    # The passphrase's own scrypt key, without the secret, is not the vault key.
    alone = vault_crypto.derive_key("pw", {"v": 1, "kdf": document["kdf"]})
    assert alone != key
    with pytest.raises(vault_crypto.VaultError):
        vault_crypto.open_sealed(alone, document)

    # The file copied to an account whose store has no such item.
    items.clear()
    with pytest.raises(vault_crypto.VaultMoved) as moved:
        vault_crypto.derive_key("pw", document)
    assert vault_crypto.refusal(moved.value) == device_key.MOVED


def test_the_device_object_is_authenticated_and_fits_its_version(monkeypatch):
    fake_account_store(monkeypatch)
    key, document = sealed()
    other = device_key.create()

    swapped = copy.deepcopy(document)
    swapped["device"] = other
    with pytest.raises(vault_crypto.VaultError):
        vault_crypto.open_sealed(key, swapped)

    for change in ({"v": 1}, {"device": "x"}):
        broken = {**copy.deepcopy(document), **change}
        with pytest.raises(vault_crypto.VaultError, match="not a valid vault"):
            vault_crypto.open_sealed(key, broken)
    headless = copy.deepcopy(document)
    del headless["device"]
    with pytest.raises(vault_crypto.VaultError, match="not a valid vault"):
        vault_crypto.open_sealed(key, headless)


def test_a_rewrite_keeps_the_device_object(monkeypatch):
    fake_account_store(monkeypatch)
    key, document = sealed()
    again = vault_crypto.seal(key, vault_crypto.header_of(document), {"entries": {}})
    assert again["device"] == document["device"]
    assert again["nonce"] != document["nonce"]
    assert vault_crypto.open_sealed(key, again) == {"entries": {}}
