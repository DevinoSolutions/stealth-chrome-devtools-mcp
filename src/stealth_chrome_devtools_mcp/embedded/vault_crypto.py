"""The login vault's file format (T-19366): scrypt key, AES-256-GCM, header as AAD.

A leaf with no knowledge of entries: it turns a passphrase and a JSON-able
plaintext into a document and back, and reads and writes that document as a file
only the current OS user can open. :mod:`vault_store` owns what the plaintext
means.

**Format, version 1** (``vault.json``)::

    {"v": 1, "kdf": {"name": "scrypt", "n": ..., "r": 8, "p": 1, "salt": "<b64>"},
     "nonce": "<b64 12 bytes>", "ciphertext": "<b64>"}

The key is ``hashlib.scrypt(passphrase, salt)``, 32 bytes. The ciphertext is
AES-256-GCM and its associated data is the canonical JSON of ``{"v", "kdf"}``,
so a header swapped under an intact ciphertext (a cheaper ``n``, another salt)
fails authentication. A fresh nonce is drawn on EVERY :func:`seal`: the key is
reused across writes, so a repeated nonce would break GCM.

**One failure type, no detail.** Wrong key, flipped byte, changed header, bad
base64, wrong version and a file that is not JSON all raise :class:`VaultError`
with a message that says what KIND of thing failed and never echoes the
passphrase, the key or any content; GCM cannot (and must not) tell a wrong key
from a tampered file.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import secrets
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from stealth_chrome_devtools_mcp.embedded import atomic_file, backend_token

if TYPE_CHECKING:
    from pathlib import Path

#: A header or a document: the JSON object the file holds.
Document = dict[str, object]

VERSION = 1
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**17, 8, 1

_KEY_BYTES = 32
_SALT_BYTES = 16
_NONCE_BYTES = 12
#: scrypt needs ~128*n*r bytes: 134 MB at the defaults. The cap leaves headroom
#: for the default and refuses a header that asks for more (a tampered file must
#: not make the CLI or the backend allocate gigabytes before authentication).
_SCRYPT_MAXMEM = 2**28
_MIN_N, _MAX_R, _MAX_P = 2, 32, 16

_BAD_FILE = "the vault file is not a valid vault document"
_BAD_KEY = "wrong passphrase, or the vault file was changed"


class VaultError(Exception):
    """Wrong key, tampered file, bad format. Never carries a secret."""


def new_header() -> Document:
    """A header for a NEW vault: the current cost parameters and a random salt."""
    return {
        "v": VERSION,
        "kdf": {
            "name": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": _b64(secrets.token_bytes(_SALT_BYTES)),
        },
    }


def derive_key(passphrase: str, header: Document) -> bytes:
    """The 32-byte key for ``passphrase`` under ``header``'s KDF parameters."""
    n, r, p, salt = _kdf_parameters(header)
    try:
        return hashlib.scrypt(
            passphrase.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=_SCRYPT_MAXMEM,
            dklen=_KEY_BYTES,
        )
    except ValueError as exc:
        raise VaultError(_BAD_FILE) from exc


def seal(key: bytes, header: Document, plaintext: Document) -> Document:
    """``header`` plus a FRESH nonce and the encrypted ``plaintext``."""
    nonce = secrets.token_bytes(_NONCE_BYTES)
    body = json.dumps(plaintext, separators=(",", ":")).encode("utf-8")
    try:
        ciphertext = AESGCM(key).encrypt(nonce, body, _aad(header))
    except (ValueError, TypeError) as exc:
        raise VaultError("the vault key is not a 32-byte key") from exc
    return {
        "v": header["v"],
        "kdf": copy.deepcopy(header["kdf"]),
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext),
    }


def open_sealed(key: bytes, document: Document) -> Document:
    """The plaintext of ``document`` under ``key``, or :class:`VaultError`."""
    if not isinstance(document, dict) or document.get("v") != VERSION:
        raise VaultError(_BAD_FILE)
    try:
        nonce = _unb64(document["nonce"])
        ciphertext = _unb64(document["ciphertext"])
        aad = _aad(document)
    except (KeyError, TypeError, ValueError) as exc:
        raise VaultError(_BAD_FILE) from exc
    try:
        body = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        raise VaultError(_BAD_KEY) from None
    except (ValueError, TypeError) as exc:
        raise VaultError(_BAD_FILE) from exc
    try:
        plaintext = json.loads(body)
    except ValueError as exc:
        raise VaultError(_BAD_FILE) from exc
    if not isinstance(plaintext, dict):
        raise VaultError(_BAD_FILE)
    return plaintext


def read_file(path: Path) -> Document:
    """The document at ``path``. :class:`VaultError` when it is missing or is not
    a JSON object (it does not check the fields: :func:`open_sealed` does)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VaultError("there is no vault file yet") from exc
    except (OSError, ValueError) as exc:
        raise VaultError(_BAD_FILE) from exc
    if not isinstance(document, dict):
        raise VaultError(_BAD_FILE)
    return document


def write_file(path: Path, document: Document) -> None:
    """Publish ``document`` at ``path``: a sibling scratch file made owner-only
    BEFORE any byte is written, then an atomic replace, so a reader sees the
    whole old file or the whole new one and no other OS user ever sees either."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            backend_token.owner_only(scratch)
            handle.write(json.dumps(document, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        atomic_file.commit(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)


def _kdf_parameters(header: Document) -> tuple[int, int, int, bytes]:
    """``(n, r, p, salt)`` from ``header``, or :class:`VaultError`. The bounds
    keep a tampered header from asking for a different KDF or unbounded memory."""
    kdf = header.get("kdf") if isinstance(header, dict) else None
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise VaultError(_BAD_FILE)
    n, r, p = kdf.get("n"), kdf.get("r"), kdf.get("p")
    if not (type(n) is int and type(r) is int and type(p) is int):
        raise VaultError(_BAD_FILE)
    if n < _MIN_N or n & (n - 1) or not 1 <= r <= _MAX_R or not 1 <= p <= _MAX_P:
        raise VaultError(_BAD_FILE)
    try:
        return n, r, p, _unb64(kdf["salt"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VaultError(_BAD_FILE) from exc


def _aad(header: Document) -> bytes:
    return json.dumps(
        {"v": header["v"], "kdf": header["kdf"]}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise TypeError("not a base64 string")
    return base64.b64decode(value, validate=True)  # binascii.Error is a ValueError
