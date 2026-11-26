"""The login vault's file format (T-19366): scrypt key, an account-bound second
factor (T-22092), AES-256-GCM, header as AAD.

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

**Format, version 2** adds ``"device": {...}`` beside ``kdf`` (and to the
associated data). It names a 32-byte secret this OS account keeps
(:mod:`device_key`), and the key becomes ``HMAC-SHA256(device secret, label +
scrypt key)``: the passphrase AND the account are both needed, so a copy of the
file cannot be guessed at offline on another machine. A machine with no key
store still writes version 1, and version 1 is still read: ``stealthy login
bind`` upgrades one, ``stealthy login export`` writes one to carry elsewhere.

**One failure type, no detail.** Wrong key, flipped byte, changed header, bad
base64, wrong version and a file that is not JSON all raise :class:`VaultError`
with a message that says what KIND of thing failed and never echoes the
passphrase, the key or any content; GCM cannot (and must not) tell a wrong key
from a tampered file.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from stealth_chrome_devtools_mcp.embedded import (
    atomic_file,
    backend_token,
    device_key,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: A header or a document: the JSON object the file holds.
Document = dict[str, object]

VERSION = 1
#: A vault whose key also needs this OS account's secret (``device``).
BOUND_VERSION = 2
_BOUND_LABEL = b"stealth-chrome-devtools-mcp vault key v2\x00"
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


class VaultMoved(VaultError):  # noqa: N818  PERMANENT(it names what happened to the file, as VaultChanged does)
    """The vault is bound to an OS account whose key store has no key for it."""


def refusal(exc: VaultError) -> str:
    """The one sentence a person is told when a vault did not open."""
    return str(exc) if isinstance(exc, VaultMoved) else _BAD_KEY


class VaultChanged(VaultError):  # noqa: N818  PERMANENT(the name its callers share: it says the file changed, not that something failed)
    """The file was replaced by someone else since it was read."""


#: How long a writer waits for another's lock, and when a lock counts as abandoned
#: (a writer holds it for milliseconds; a crash can leave one behind).
_LOCK_WAIT_S = 5.0
_LOCK_STALE_S = 30.0
_LOCK_POLL_S = 0.02


def new_header(*, bound: bool = True) -> Document:
    """A header for a NEW vault: the current cost parameters, a random salt and,
    when *bound* and this account has a key store, a new device secret in it."""
    header: Document = {
        "v": VERSION,
        "kdf": {
            "name": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": _b64(secrets.token_bytes(_SALT_BYTES)),
        },
    }
    device = device_key.create() if bound else None
    if device is not None:
        header.update(v=BOUND_VERSION, device=device)
    return header


def header_of(document: Document) -> Document:
    """The part of ``document`` a key is derived from and that every rewrite
    keeps: ``v``, ``kdf`` and, in a bound vault, ``device``."""
    header: Document = {"v": document["v"], "kdf": copy.deepcopy(document["kdf"])}
    if "device" in document:
        header["device"] = copy.deepcopy(document["device"])
    return header


def is_bound(document: Document) -> bool:
    return isinstance(document, dict) and document.get("v") == BOUND_VERSION


def derive_key(passphrase: str, header: Document) -> bytes:
    """The 32-byte key for ``passphrase`` under ``header``: scrypt, then the
    device secret mixed in when the vault is bound (asked for first, so a vault
    from another machine is refused before 134 MB of scrypt)."""
    n, r, p, salt = _kdf_parameters(header)
    device = None
    if is_bound(header):
        try:
            device = device_key.load(header.get("device"))
        except device_key.DeviceKeyError as exc:
            raise VaultMoved(str(exc)) from None
    stretched = _stretch(passphrase, n, r, p, salt)
    if device is None:
        return stretched
    return hmac.new(device, _BOUND_LABEL + stretched, hashlib.sha256).digest()


def _stretch(passphrase: str, n: int, r: int, p: int, salt: bytes) -> bytes:
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
    return {**header_of(header), "nonce": _b64(nonce), "ciphertext": _b64(ciphertext)}


def open_sealed(key: bytes, document: Document) -> Document:
    """The plaintext of ``document`` under ``key``, or :class:`VaultError`."""
    if not isinstance(document, dict) or not _version_fits(document):
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


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """An exclusive lock on writing ``path``, shared by every process that uses
    :func:`write_file` (the backend's freeze and the CLI): a sibling ``.lock``
    file made with ``O_EXCL``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            break
        except FileExistsError:
            try:
                abandoned = time.time() - lock.stat().st_mtime > _LOCK_STALE_S
            except FileNotFoundError:
                continue
            if abandoned:
                lock.unlink(missing_ok=True)
            elif time.monotonic() >= deadline:
                raise VaultError(
                    "the vault file is being written by another process"
                ) from None
            else:
                time.sleep(_LOCK_POLL_S)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def write_file(path: Path, document: Document, expect: object = None) -> None:
    """Publish ``document`` at ``path`` through ``atomic_file`` as a PRIVATE file (a
    sibling temp made owner-only BEFORE any byte is written, then an atomic replace),
    so a reader sees the whole old file or the whole new one and no other OS user
    ever sees either.

    The replace happens under :func:`_file_lock`. ``expect`` is the ``nonce`` of
    the document the new one was made from: when the file on disk no longer has
    it, somebody else wrote in between and :class:`VaultChanged` is raised with
    nothing written (every :func:`seal` draws a fresh nonce, so the nonce names
    one version of the file)."""
    with _file_lock(path):
        if expect is not None:
            try:
                current = read_file(path).get("nonce")
            except VaultError:
                current = None
            if current != expect:
                raise VaultChanged("the vault file changed since it was read")
        atomic_file.write_text(
            path,
            json.dumps(document, separators=(",", ":")),
            make_parents=False,
            narrow=backend_token.owner_only,
        )


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


def _version_fits(document: Document) -> bool:
    """Version 2 has a ``device`` object and version 1 has none: a file cannot
    claim one version and carry the other's header."""
    if document.get("v") == BOUND_VERSION:
        return isinstance(document.get("device"), dict)
    return document.get("v") == VERSION and "device" not in document


def _aad(header: Document) -> bytes:
    return json.dumps(header_of(header), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise TypeError("not a base64 string")
    return base64.b64decode(value, validate=True)  # binascii.Error is a ValueError
