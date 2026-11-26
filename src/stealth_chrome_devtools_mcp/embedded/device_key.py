"""The login vault's second factor (T-22092): a secret this OS account keeps.

``vault_crypto`` mixes 32 random bytes into the vault key, and this leaf is where
those bytes live: with the operating system, tied to the ACCOUNT, never to a
piece of hardware. A copy of ``vault.json`` taken to another machine, or read by
another account, cannot be opened even with the passphrase. Changing RAM, a disk
or a board changes nothing; a new machine, a new account or a reinstalled OS
loses the secret, which is what ``stealthy login export`` is for.

**Where the secret is, per platform** (the header's ``device`` object says which):

- Windows, ``{"store": "dpapi", "blob": "<b64>"}``: the secret encrypted by DPAPI
  (``CryptProtectData``) for the current user. The blob is in the vault file
  itself, so there is no second file to lose, and it opens only for this user on
  this Windows installation.
- macOS, ``{"store": "keychain", "id": "<hex>"}``: a generic password in the
  login keychain, through ``security``.
- Linux, ``{"store": "secret-service", "id": "<hex>"}``: an item in the Secret
  Service (GNOME Keyring, KWallet), through ``secret-tool``.

**No store is not an error.** A server without a Secret Service has nowhere an
account-bound secret could live, and a file beside the vault would be copied
with it. :func:`create` answers ``None`` there and the vault is protected by its
passphrase alone, which ``init`` and ``status`` say.

**A store is believed only after it gave the secret back.** :func:`create` reads
what it just stored and compares, so a keychain that accepts a write and then
refuses the read never becomes the only holder of a vault's key.

The secret is passed to ``security`` and ``secret-tool`` on stdin, never as an
argument: an argument is readable in the process list.
"""

from __future__ import annotations

import base64
import contextlib
import secrets
import shutil
import subprocess
import sys

#: A header's ``device`` object.
Descriptor = dict[str, object]

SECRET_BYTES = 32
_SERVICE = "stealth-chrome-devtools-mcp login vault"
#: Mixed into the DPAPI blob so another program's unprotect call, which would
#: pass no entropy, does not open it by accident. Not a secret.
_ENTROPY = b"stealth-chrome-devtools-mcp vault device key v1"
_TOOL_TIMEOUT_S = 15.0
_ID_BYTES = 16

MOVED = (
    "this vault is tied to another machine or OS account, or this account's key "
    "store no longer has its key; restore a copy made with `stealthy login export`"
)


#: Not the same thing as MOVED: the vault may be fine and the store merely shut.
UNREACHABLE = (
    "this account's key store did not answer (locked, not running, or its tool "
    "is missing), so the vault's second key could not be read; unlock the "
    "keychain or keyring and try again. The vault file is unchanged"
)
#: ``security find-generic-password``: errSecItemNotFound.
_KEYCHAIN_NOT_FOUND = 44
_TOOL_STORES = ("keychain", "secret-service")


class DeviceKeyError(Exception):
    """The store gave no key for this vault: its message is :data:`MOVED` (no
    such key here) or :data:`UNREACHABLE` (the store did not answer). Never
    carries a secret."""


def store_name() -> str | None:
    """Which store this machine would use for a NEW vault, or ``None``."""
    native = _native()
    tool = {"keychain": "security", "secret-service": "secret-tool"}.get(native)
    return native if tool is None or shutil.which(tool) else None


def _native() -> str:
    """The store this PLATFORM has, whether or not its tool is installed."""
    if sys.platform == "win32":
        return "dpapi"
    return "keychain" if sys.platform == "darwin" else "secret-service"


def create() -> Descriptor | None:
    """Make a secret, hand it to this account's store and return the descriptor
    that finds it again; ``None`` when there is no store or it did not give the
    secret back."""
    store = store_name()
    if store is None:
        return None
    secret = secrets.token_bytes(SECRET_BYTES)
    try:
        if store == "dpapi":
            descriptor: Descriptor = {"store": store, "blob": _b64(_protect(secret))}
        else:
            descriptor = {"store": store, "id": secrets.token_hex(_ID_BYTES)}
            _tool_store(descriptor, secret)
        kept = load(descriptor) == secret
    except (DeviceKeyError, OSError):
        return None
    return descriptor if kept else None


def load(descriptor: object) -> bytes:
    """The secret ``descriptor`` names, or :class:`DeviceKeyError`."""
    store = descriptor.get("store") if isinstance(descriptor, dict) else None
    if store != _native() or not isinstance(descriptor, dict):
        raise DeviceKeyError(MOVED)  # another platform's store, or no descriptor
    try:
        if store == "dpapi":
            blob = descriptor.get("blob")
            if not isinstance(blob, str):
                raise DeviceKeyError(MOVED)
            secret = _unprotect(base64.b64decode(blob, validate=True))
        elif store_name() is None:
            raise DeviceKeyError(UNREACHABLE)  # this platform's tool is not installed
        else:
            found = _tool_run(_lookup_argv(descriptor), lookup_in=store)
            secret = bytes.fromhex(found.strip())
    except (OSError, ValueError) as exc:
        raise DeviceKeyError(MOVED) from exc
    if len(secret) != SECRET_BYTES:
        raise DeviceKeyError(MOVED)
    return secret


def forget(descriptor: object) -> None:
    """Drop the secret from the store once its vault is gone. A DPAPI blob lives
    in the vault file and goes with it, so there is nothing to drop; a store
    that will not answer keeps an item that opens nothing."""
    if not isinstance(descriptor, dict):
        return
    store = descriptor.get("store")
    if store not in _TOOL_STORES or store != store_name():
        return
    with contextlib.suppress(DeviceKeyError):
        if store == "keychain":
            item = _keychain_item(descriptor)
            _tool_run(["security", "delete-generic-password", *item])
        else:
            _tool_run(["secret-tool", "clear", *_secret_item(descriptor)])


# ------------------------------------------------------- macOS and Linux tools


def _item_id(descriptor: Descriptor) -> str:
    item = descriptor.get("id")
    if not isinstance(item, str) or not item.isalnum():
        raise DeviceKeyError(MOVED)
    return item


def _keychain_item(descriptor: Descriptor) -> list[str]:
    return ["-a", _item_id(descriptor), "-s", _SERVICE]


def _secret_item(descriptor: Descriptor) -> list[str]:
    return ["service", _SERVICE, "account", _item_id(descriptor)]


def _lookup_argv(descriptor: Descriptor) -> list[str]:
    if descriptor.get("store") == "keychain":
        return ["security", "find-generic-password", *_keychain_item(descriptor), "-w"]
    return ["secret-tool", "lookup", *_secret_item(descriptor)]


def _tool_store(descriptor: Descriptor, secret: bytes) -> None:
    if descriptor.get("store") == "keychain":
        # ``security -i`` reads its commands from stdin, so the secret is never
        # an argument of any process.
        command = (
            f'add-generic-password -a {_item_id(descriptor)} -s "{_SERVICE}" '
            f"-w {secret.hex()}\n"
        )
        _tool_run(["security", "-i"], command)
    else:
        _tool_run(
            ["secret-tool", "store", "--label", _SERVICE, *_secret_item(descriptor)],
            secret.hex(),
        )


def _tool_run(
    argv: list[str], stdin: str | None = None, lookup_in: str | None = None
) -> str:
    """Run one key-store command and return its stdout. *lookup_in* names the
    store a lookup is asked of, so "no such item" (:data:`MOVED`) is told from a
    store that did not answer (:data:`UNREACHABLE`)."""
    try:
        done = subprocess.run(  # noqa: S603  PERMANENT(a fixed OS key-store tool found on PATH, with an argv built here; the secret travels on stdin)
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        done = None
    if done is None:
        # Raised out here so the error has no context: a timeout's own exception
        # carries whatever the tool had printed, which for a lookup is the key.
        raise DeviceKeyError(UNREACHABLE)
    if done.returncode == 0:
        return done.stdout
    absent = (lookup_in == "keychain" and done.returncode == _KEYCHAIN_NOT_FOUND) or (
        lookup_in == "secret-service"
        and done.returncode == 1
        and not done.stderr.strip()
    )
    raise DeviceKeyError(MOVED if absent else UNREACHABLE)


# ---------------------------------------------------------------- Windows DPAPI


def _dpapi(function: str, data: bytes) -> bytes:
    """``CryptProtectData`` or ``CryptUnprotectData`` (one signature) on *data*."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = (("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char)))

    def blob(raw: bytes) -> Blob:
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    call = getattr(crypt32, function)
    call.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    call.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    source, entropy, out = blob(data), blob(_ENTROPY), Blob()
    no_ui = 0x1  # CRYPTPROTECT_UI_FORBIDDEN: fail, never open a dialog
    if not call(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy),
        None,
        None,
        no_ui,
        ctypes.byref(out),
    ):
        raise OSError(ctypes.get_last_error(), "DPAPI refused")
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        kernel32.LocalFree(out.data)


def _protect(secret: bytes) -> bytes:
    return _dpapi("CryptProtectData", secret)


def _unprotect(blob: bytes) -> bytes:
    return _dpapi("CryptUnprotectData", blob)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")
