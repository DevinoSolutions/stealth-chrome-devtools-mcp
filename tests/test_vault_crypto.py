"""Vault file format (T-19366): scrypt key, AES-256-GCM, header bound as AAD."""

from __future__ import annotations

import base64
import json
import sys

import pytest

from stealth_chrome_devtools_mcp.embedded import vault_crypto

MARKER = "hunter2-marker"


@pytest.fixture
def fast_kdf(monkeypatch):
    """scrypt at n=2**10: the real cost is ~0.3 s a derivation."""
    monkeypatch.setattr(vault_crypto, "SCRYPT_N", 2**10)


def _document(passphrase="pw", plaintext=None):
    header = vault_crypto.new_header()
    key = vault_crypto.derive_key(passphrase, header)
    return key, header, vault_crypto.seal(key, header, plaintext or {"entries": {}})


def test_round_trip(tmp_path, fast_kdf):
    h = vault_crypto.new_header()
    k = vault_crypto.derive_key("pw", h)
    assert len(k) == 32
    vault_crypto.write_file(
        tmp_path / "v.json", vault_crypto.seal(k, h, {"entries": {}})
    )
    opened = vault_crypto.open_sealed(k, vault_crypto.read_file(tmp_path / "v.json"))
    assert opened == {"entries": {}}


def test_header_shape(fast_kdf):
    header = vault_crypto.new_header()
    assert header["v"] == 1
    assert header["kdf"]["name"] == "scrypt"
    assert (header["kdf"]["n"], header["kdf"]["r"], header["kdf"]["p"]) == (2**10, 8, 1)
    assert len(base64.b64decode(header["kdf"]["salt"])) == 16


def test_default_cost_is_the_documented_one():
    assert (vault_crypto.SCRYPT_N, vault_crypto.SCRYPT_R, vault_crypto.SCRYPT_P) == (
        2**17,
        8,
        1,
    )


def test_wrong_passphrase_fails(fast_kdf):
    _key, header, document = _document("right")
    other = vault_crypto.derive_key("other", header)
    with pytest.raises(vault_crypto.VaultError):
        vault_crypto.open_sealed(other, document)


def test_a_flipped_ciphertext_byte_fails(fast_kdf):
    key, _header, document = _document()
    raw = bytearray(base64.b64decode(document["ciphertext"]))
    raw[0] ^= 1
    document["ciphertext"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(vault_crypto.VaultError):
        vault_crypto.open_sealed(key, document)


def test_a_changed_header_fails(fast_kdf):
    key, _header, document = _document()
    document["kdf"]["n"] = document["kdf"]["n"] * 2
    with pytest.raises(vault_crypto.VaultError):
        vault_crypto.open_sealed(key, document)


def test_every_write_uses_a_new_nonce(fast_kdf):
    key, header, first = _document()
    second = vault_crypto.seal(key, header, {"entries": {}})
    assert first["nonce"] != second["nonce"]
    assert len(base64.b64decode(first["nonce"])) == 12


def test_the_plaintext_is_not_in_the_file(tmp_path, fast_kdf):
    key, header, _ = _document()
    path = tmp_path / "v.json"
    document = vault_crypto.seal(key, header, {"entries": {"a": {"password": MARKER}}})
    vault_crypto.write_file(path, document)
    assert MARKER.encode() not in path.read_bytes()


@pytest.mark.parametrize("junk", ["", "not json", "[]", '"s"'])
def test_read_file_rejects_a_file_that_is_not_a_document(tmp_path, junk):
    path = tmp_path / "v.json"
    path.write_text(junk)
    with pytest.raises(vault_crypto.VaultError):
        vault_crypto.read_file(path)


def test_read_file_rejects_a_missing_file(tmp_path):
    with pytest.raises(vault_crypto.VaultError):
        vault_crypto.read_file(tmp_path / "absent.json")


@pytest.mark.parametrize("field", ["v", "kdf", "nonce", "ciphertext"])
def test_a_document_missing_a_field_is_a_vault_error(fast_kdf, field):
    key, _header, document = _document()
    del document[field]
    with pytest.raises(vault_crypto.VaultError):
        vault_crypto.open_sealed(key, document)


def test_an_unknown_version_is_refused(fast_kdf):
    key, _header, document = _document()
    document["v"] = 2
    with pytest.raises(vault_crypto.VaultError):
        vault_crypto.open_sealed(key, document)


def test_error_messages_carry_no_passphrase(fast_kdf):
    _key, header, document = _document("a-very-secret-pass")
    other = vault_crypto.derive_key("another-secret-pass", header)
    with pytest.raises(vault_crypto.VaultError) as caught:
        vault_crypto.open_sealed(other, document)
    assert "secret-pass" not in str(caught.value)


def test_write_replaces_atomically_and_leaves_no_scratch_file(tmp_path, fast_kdf):
    path = tmp_path / "v.json"
    key, header, first = _document()
    vault_crypto.write_file(path, first)
    vault_crypto.write_file(path, vault_crypto.seal(key, header, {"entries": {"x": 1}}))
    assert vault_crypto.open_sealed(key, vault_crypto.read_file(path)) == {
        "entries": {"x": 1}
    }
    assert [p.name for p in tmp_path.iterdir()] == ["v.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_file_is_owner_only(tmp_path, fast_kdf):
    path = tmp_path / "v.json"
    _key, _header, document = _document()
    vault_crypto.write_file(path, document)
    assert path.stat().st_mode & 0o077 == 0


def test_the_written_file_is_the_documented_json(tmp_path, fast_kdf):
    path = tmp_path / "v.json"
    _key, _header, document = _document()
    vault_crypto.write_file(path, document)
    assert set(json.loads(path.read_text())) == {"v", "kdf", "nonce", "ciphertext"}
