"""THE one home for "who may talk to the HTTP backend" (F-1018): a bearer token.

F-1013's Host/Origin guard keeps a WEB PAGE out of the backend. It does nothing
about another process on the machine: any local process, and any other OS user,
could ``initialize`` a session and call ``create_python_binding`` (``exec`` in
the backend) or read cookies. This module closes that with the smallest thing
that does: a random secret in a file only the current OS user can read, which
the backend demands as ``Authorization: Bearer <secret>`` and every first-party
client sends.

**One file, one token, per user — not per backend.** The backend registry holds
one entry per (display context, identity), so a headless and a desktop backend
and two source identities coexist. A token minted at each backend's startup would
make the newest one lock out every client of its siblings, and ``stealthy`` would
have to know WHICH backend's token to send. So the file is created once, by the
first backend that starts (:func:`ensure`, run before it binds its port, which is
why no client ever finds a listening backend without a token file), and REUSED
while it is well-formed; every backend and client of the user reads the same
file. Rotating it is deleting it and restarting the backends.

**Where it lives and who can read it.** ``<state dir>/backend.token``. POSIX:
mode ``0600``, created that way (never widened and narrowed after the secret is
written). Windows: the default ACL inherits Modify for other principals — on the
measuring machine that included a ``CodexSandboxUsers`` group — so the file is
created empty, its DACL is replaced with a protected one holding the current
user's SID alone (full control) in a single ``SetNamedSecurityInfoW`` call, and
only THEN is the secret written. The secret
is written to a scratch name and hard-linked into place, so a concurrent reader
sees the whole file or no file, and the loser of a creation race reads the
winner's token instead of overwriting it.

**Fail closed on the server, fail soft on the client.** :func:`ensure` raises
``ToolError`` (the one error convention) when it cannot make the file owner-only
or finds a file it must not trust, and the backend does not serve: an
unauthenticated backend is exactly what this exists to prevent. :func:`read` and
:func:`auth_headers` never raise — a client with no readable token sends no
header and the backend answers 401, which is the true, diagnosable outcome
(:data:`UNAUTHORIZED`), rather than a crash in the proxy's cold start.

**What it does not do.** It is a shared secret for a loopback socket, not a
channel binding. A client sends it to ``127.0.0.1:<recorded port>`` without
being able to prove who answers, so another user who binds a dead backend's port
before the next launch can read it from the first probe. It still opens nothing
else, and the window needs the backend to be down. The same account's own
processes can read the file, as they can read the browser profiles.

A leaf: stdlib at module level, ``starlette`` (already a dependency of FastMCP)
only inside the factory that needs it, and ``backend_registry`` for the state
dir and ``tool_errors`` for the error. It imports neither ``server`` nor any
client; the clients (``backend_client``, ``backend_probe``) import it.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import stat
import sys
import time
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import backend_registry
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.middleware import Middleware
    from starlette.types import ASGIApp, Receive, Scope, Send

_logger = logging.getLogger("stealth.backend_token")

TOKEN_FILE_NAME = "backend.token"  # noqa: S105  PERMANENT(a file NAME, not a secret)

#: The one request header, and the scheme the backend accepts in it.
AUTH_HEADER = "authorization"
SCHEME = "Bearer"

#: What a 401 from the backend means, in the words every client shows. One
#: sentence so the CLI, the proxy log and the docs cannot drift apart.
UNAUTHORIZED = (
    "the backend refused the request: missing or wrong bearer token "
    f"(read from <state dir>/{TOKEN_FILE_NAME}; a backend started by another "
    "OS user or under another home has a different one)"
)

#: ``secrets.token_urlsafe(32)`` is 43 characters of this alphabet. A file that
#: is shorter or longer or holds anything else (empty, hand-edited, appended to,
#: truncated) is not a token and is replaced rather than trusted.
_TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")

#: At most one refusal is logged per this many seconds: a local process hammering
#: the port must not turn the backend's log into the thing it is flooded through.
_LOG_EVERY_SECONDS = 60.0

# Win32 values, spelled out because the type gate also runs on Linux.
_SDDL_REVISION_1 = 1
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_TOKEN_QUERY = 0x0008  # TOKEN_QUERY
_TOKEN_USER = 1  # TOKEN_INFORMATION_CLASS.TokenUser
_TOKEN_OWNER = 4  # TOKEN_INFORMATION_CLASS.TokenOwner


def token_path() -> Path:
    """The token file. Resolved on every call, not at import, so a test (or an
    isolated backend under another home) that redirects the state dir is honoured."""
    return backend_registry.STATE_DIR / TOKEN_FILE_NAME


# ---------------------------------------------------------------- the file


def ensure(path: Path | None = None) -> str:
    """The user's token, created if it does not exist yet (server side).

    Raises ``ToolError`` when the file cannot be made owner-only or is not ours.
    """
    path = path or token_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create the state dir {path.parent}: {exc}") from exc
    _refuse_a_foreign_file(path)
    existing = _read_file(path)
    if existing is not None:
        _owner_only(path)  # idempotent: tightens a file someone widened
        return existing
    return _create(path)


def read(path: Path | None = None) -> str | None:
    """The token a client should send, or ``None``. Never raises."""
    return _read_file(path or token_path())


def auth_headers(path: Path | None = None) -> dict[str, str]:
    """``{"Authorization": "Bearer <token>"}``, or ``{}`` when there is no token.

    ``{}`` and not an error: against a backend that predates the token (an old
    adopted backend) no header is the correct request, and against one that
    enforces it the 401 names the cause (:data:`UNAUTHORIZED`).
    """
    token = read(path)
    return {AUTH_HEADER: f"{SCHEME} {token}"} if token else {}


def _read_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None
    return text if _TOKEN_RE.fullmatch(text) else None


def _refuse_a_foreign_file(path: Path) -> None:
    """A token file another user owns is not ours to trust or to tighten.

    POSIX compares the file's uid with ours; Windows compares the owner SID with
    the SIDs this process makes files with (:func:`_file_owner_sid`,
    :func:`_own_sids`). A file that is not there is nothing to refuse, and on
    Windows an owner that cannot be read is refused (fail closed) rather than
    assumed to be ours.
    """
    if sys.platform == "win32":
        try:
            owner = _file_owner_sid(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ToolError(
                _unverifiable(path, f"its owner could not be read ({exc})")
            ) from exc
        try:
            mine = _own_sids()
        except OSError as exc:
            raise ToolError(
                _unverifiable(path, f"this process's SID could not be read ({exc})")
            ) from exc
        if owner not in mine:
            raise ToolError(_foreign_owner(path, f"SID {owner}"))
        return
    try:
        owner_uid = path.stat().st_uid
    except OSError:
        return
    if owner_uid != os.geteuid():
        raise ToolError(_foreign_owner(path, f"uid {owner_uid}"))


def _unverifiable(path: Path, why: str) -> str:
    return (
        f"{path}: {why}; refusing to serve with a token that may be readable by "
        "another user. Remove it and start the backend again."
    )


def _foreign_owner(path: Path, who: str) -> str:
    return (
        f"{path} is owned by another user ({who}); refusing to serve "
        "with a token that user can read. Remove it and start the backend again."
    )


def _create(path: Path) -> str:
    """Write a new token: scratch file made owner-only FIRST, secret second,
    hard link into place so the final name is never half-written."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    scratch = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            _owner_only(scratch)  # before the secret is in the file
            handle.write(token)
        try:
            os.link(scratch, path)
        except FileExistsError:
            # Lost the race to another backend: theirs is the token. A
            # file that is NOT a token (empty, truncated, hand-edited) is
            # replaced instead of trusted; the answer is then whatever is in
            # the file now, so a second repairer converges on the last writer.
            winner = _read_file(path)
            if winner is None:
                scratch.replace(path)
                winner = _read_file(path) or token
            return winner
        else:
            return token
    except OSError as exc:
        raise ToolError(f"cannot create the backend token file {path}: {exc}") from exc
    finally:
        scratch.unlink(missing_ok=True)


def _owner_only(path: Path) -> None:
    """Make ``path`` readable and writable by the current user alone, or raise."""
    try:
        if sys.platform == "win32":
            _owner_only_windows(path)
        else:
            path.chmod(0o600)
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise ToolError(f"{path} is mode {mode:o} and could not be narrowed")
    except OSError as exc:
        raise ToolError(f"cannot restrict {path} to the current user: {exc}") from exc


def _owner_only_windows(path: Path) -> None:
    """Replace the file's DACL, in one call, with a protected one holding a single
    ACE: full control for the current user's SID. Nothing is inherited and every
    explicit ACE already on the file is dropped, so a widened file is narrowed by
    the same call that secures a new one (``icacls /grant:r`` replaces only the
    named principal's entry, which would leave a widened ACL widened)."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    # D:P = a protected DACL (no inheritance); one allow-ACE, FA = full control.
    sddl = f"D:P(A;;FA;;;{_user_sid()})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise OSError("ConvertStringSecurityDescriptorToSecurityDescriptorW failed")
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise OSError("GetSecurityDescriptorDacl failed")
        failed = advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if failed:
            raise OSError(f"SetNamedSecurityInfoW failed (error {failed})")
    finally:
        kernel32.LocalFree(descriptor)


def _own_sids() -> frozenset[str]:
    """The SIDs a file this process creates can be owned by: the token's user and
    its default owner. They differ in an elevated admin process, whose files are
    owned by the Administrators group (``S-1-5-32-544``), so comparing with the user
    alone would refuse the backend's own token on its next start."""
    return frozenset({_user_sid(), _token_sid(_TOKEN_OWNER)})


def _user_sid() -> str:
    """The current process token's user SID as a string (``S-1-5-21-...``)."""
    return _token_sid(_TOKEN_USER)


def _token_sid(info_class: int) -> str:
    """The SID a ``TOKEN_USER`` or ``TOKEN_OWNER`` of this process's token names.
    Both structures start with the SID pointer."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process_token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(process_token)
    ):
        raise OSError("OpenProcessToken failed")
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(
            process_token, info_class, None, 0, ctypes.byref(needed)
        )
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            process_token, info_class, buffer, needed, ctypes.byref(needed)
        ):
            raise OSError("GetTokenInformation failed")
        return _sid_string(ctypes.c_void_p.from_buffer(buffer).value)
    finally:
        kernel32.CloseHandle(process_token)


def _sid_string(sid_pointer: int | None) -> str:
    """A SID, by pointer, as its string form (``S-1-5-21-...``). ``OSError`` when
    the conversion fails, including for a NULL pointer."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
        raise OSError("ConvertSidToStringSidW failed")
    try:
        return str(text.value)
    finally:
        kernel32.LocalFree(text)


def _file_owner_sid(path: Path) -> str:
    """The SID that OWNS ``path`` as a string. ``FileNotFoundError`` when it is
    not there; ``OSError`` for any other failure to read the owner."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    pointer = ctypes.POINTER(ctypes.c_void_p)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    failed = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if failed in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
        raise FileNotFoundError(str(path))
    if failed:
        raise OSError(f"GetNamedSecurityInfoW failed (error {failed})")
    try:
        # The owner SID points INTO the descriptor, so read it before freeing.
        return _sid_string(owner.value)
    finally:
        kernel32.LocalFree(descriptor)


# ---------------------------------------------------------------- the guard


class BearerTokenMiddleware:
    """Pure ASGI: every HTTP request must carry ``Authorization: Bearer <token>``.

    Compared in constant time. A missing header, another scheme and a wrong value
    are all the same 401, so the answer says nothing about which. Lifespan scopes
    pass through (the app must still start), websockets are closed.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"{SCHEME} {token}".encode("ascii")
        self._last_logged = float("-inf")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and self._allowed(scope):
            await self.app(scope, receive, send)
            return
        self._note_refusal(scope)
        if scope["type"] == "http":
            await _unauthorized(send)
        else:
            await send({"type": "websocket.close", "code": 1008})

    def _allowed(self, scope: Scope) -> bool:
        presented = next(
            (v for k, v in scope["headers"] if k.lower() == AUTH_HEADER.encode()),
            b"",
        )
        return hmac.compare_digest(presented, self._expected)

    def _note_refusal(self, scope: Scope) -> None:
        now = time.monotonic()
        if now - self._last_logged < _LOG_EVERY_SECONDS:
            return
        self._last_logged = now
        _logger.warning(
            "refused a request with no valid bearer token: %s %s",
            scope.get("method", scope["type"]),
            scope.get("path", ""),
        )


async def _unauthorized(send: Send) -> None:
    body = b'{"error":"unauthorized"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", SCHEME.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def middleware() -> Middleware:
    """The Starlette ``Middleware`` the backend's app is built with. Creates the
    token file if it is not there yet (:func:`ensure`) and raises if it cannot."""
    from starlette.middleware import Middleware

    return Middleware(BearerTokenMiddleware, token=ensure())
