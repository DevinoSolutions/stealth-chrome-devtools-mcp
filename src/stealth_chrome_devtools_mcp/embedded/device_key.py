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


class DeviceKeyError(Exception):
    """The store has no key for this vault. Never carries a secret."""


def store_name() -> str | None:
    """Which store this machine would use for a NEW vault, or ``None``."""
    if sys.platform == "win32":
        return "dpapi"
    if sys.platform == "darwin":
        return "keychain" if shutil.which("security") else None
    return "secret-service" if shutil.which("secret-tool") else None


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
    except (DeviceKeyError, OSError, subprocess.SubprocessError):
        return None
    return descriptor if kept else None


def load(descriptor: object) -> bytes:
    """The secret ``descriptor`` names, or :class:`DeviceKeyError`."""
    if not isinstance(descriptor, dict):
        raise DeviceKeyError(MOVED)
    store = descriptor.get("store")
    try:
        if store == "dpapi" and sys.platform == "win32":
            blob = descriptor.get("blob")
            if not isinstance(blob, str):
                raise DeviceKeyError(MOVED)
            secret = _unprotect(base64.b64decode(blob, validate=True))
        elif store in {"keychain", "secret-service"} and store == store_name():
            secret = bytes.fromhex(_tool_run(_lookup_argv(descriptor)).strip())
        else:
            raise DeviceKeyError(MOVED)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise DeviceKeyError(MOVED) from exc
    if len(secret) != SECRET_BYTES:
        raise DeviceKeyError(MOVED)
    return secret


def forget(descriptor: object) -> None:
    """Drop the secret from the store when its vault is deleted. A DPAPI blob
    lives in the vault file and goes with it; a store that will not answer keeps
    an item that opens nothing."""
    if not isinstance(descriptor, dict) or descriptor.get("store") != store_name():
        return
    argv = {
        "keychain": [
            "security",
            "delete-generic-password",
            *_keychain_item(descriptor),
        ],
        "secret-service": ["secret-tool", "clear", *_secret_item(descriptor)],
    }.get(str(descriptor.get("store")))
    if argv:
        # A store that will not answer keeps an item that opens nothing.
        with contextlib.suppress(OSError, subprocess.SubprocessError, DeviceKeyError):
            _tool_run(argv)


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


def _tool_run(argv: list[str], stdin: str | None = None) -> str:
    done = subprocess.run(  # noqa: S603  PERMANENT(a fixed OS key-store tool found on PATH, with an argv built here; the secret travels on stdin)
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=_TOOL_TIMEOUT_S,
        check=False,
    )
    if done.returncode != 0:
        raise DeviceKeyError(MOVED)
    return done.stdout


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
