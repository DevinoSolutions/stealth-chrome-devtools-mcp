"""THE one home for keeping a clone from rotating Google's session cookies (F-939).

**The incident.** Google's ``__Secure-1PSIDTS`` / ``__Secure-3PSIDTS`` are
rotated by whoever holds the session: a Google page embeds a hidden
``accounts.google.com/RotateCookiesPage`` iframe whose script POSTs
``accounts.google.com/RotateCookies``, and each answer replaces the pair. A clone
is a COPY of the master's jar, so N clones are N independent holders of one
session, each rotating from the same starting cookies. Google sees one chain
forked N ways (or an already-rotated cookie replayed), reads it as cookie theft
and revokes the whole session, master included. Measured 2026-10-05: about ten
clones went to Google within 30 s and the account died everywhere; Apple and
every non-Google site stayed signed in.

**The rule.** Only the master rotates. A clone is disposable and its Google
cookies only have to be good for its own life, so every request a clone makes to
a rotation endpoint is failed before it leaves the browser.

**What is blocked** (:data:`BLOCKED_URL_PATTERNS`, ``Fetch.enable`` URL
wildcards, ``*`` matches anything; the path is case-sensitive, so both spellings
are listed; the pattern ends in ``*`` so ``RotateCookiesPage`` — the iframe whose
script makes the POST — is covered by the same entry as ``RotateCookies``):

* ``accounts.google.com/RotateCookies`` and ``/RotateCookiesPage`` — the PSIDTS
  rotation a Google page triggers (the endpoint community Gemini clients call to
  keep ``__Secure-1PSIDTS`` fresh);
* ``accounts.youtube.com/RotateCookies`` / ``/RotateCookiesPage`` — the same page
  embedded from YouTube;
* ``accounts.google.com/RotateBoundCookies`` — ``GaiaUrls::rotate_bound_cookies_url``
  in Chromium, the DBSC refresh endpoint (``__Host-GAPS``). That one is made by
  Chrome's browser process, which no CDP target sees, so what actually stops it is
  F-937's ``--disable-features`` list; it stays here so a page that calls it
  directly is blocked too.

**How it covers every target.** A page's own CDP connection only sees that page.
This module opens ONE extra browser-level CDP connection and sends
``Target.setAutoAttach{autoAttach, waitForDebuggerOnStart, flatten}``. Chrome then
attaches it to every top-level target (existing tabs, new tabs, popups, shared
and service workers), PAUSED before they run, and each attached session gets
``Fetch.enable`` for exactly those patterns (a matching request is paused and
failed with ``BlockedByClient``; everything else flows untouched and sends this
connection nothing) plus its own ``setAutoAttach`` — which is how the
out-of-process iframes (``accounts.google.com`` is one on every Google page) and
dedicated workers beneath it are reached. The session is resumed only after that
session is configured, so there is no window in which a target runs unguarded.
A same-process iframe's requests go through its parent page's session.
Auto-clones are never re-adopted after a backend restart (the adoption rule only
takes persistent profiles, ``auto_clone`` false), so a re-attached browser is a
master or a named session, which is meant to rotate: no re-arm site exists. If this
connection dies Chrome resumes every paused target, so a dead guard can never
hang a tab.

Close to a leaf: ``websockets`` (nodriver's own dependency), ``debug_logger`` and
``settings``. The browser arrives as an argument; it knows nothing about roles.
"""

import asyncio
import contextlib
import itertools
import json
from collections.abc import Sequence
from typing import Protocol

import websockets.asyncio.client
import websockets.exceptions
from websockets.asyncio.client import ClientConnection

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.settings import get_settings


class GuardedBrowser(Protocol):
    """The slice of a nodriver ``Browser`` the guard reads."""

    @property
    def websocket_url(self) -> str: ...


#: The hosts that serve a rotation page, and the two path spellings Google uses.
_HOSTS = ("accounts.google.com", "accounts.youtube.com")
_PATHS = ("RotateCookies", "rotateCookies", "RotateBoundCookies", "rotateBoundCookies")

#: Every ``Fetch.enable`` pattern the guard installs. See the module
#: docstring for what each one is.
BLOCKED_URL_PATTERNS = tuple(
    f"*://{host}/{path}*" for host in _HOSTS for path in _PATHS
)

#: How long :meth:`RotationGuard.start` waits for the targets that exist at
#: attach time to be configured. A bound, not a promise: a slow target is
#: configured late, never skipped.
SETTLE_TIMEOUT_SECONDS = 5.0

_OPEN_TIMEOUT_SECONDS = 5.0

#: Guards whose connection is open. The module holds them so nothing but the
#: browser's death ends one: the reader task removes its guard when the socket
#: closes, which is what a killed or closed browser does.
_LIVE: set["RotationGuard"] = set()


def enabled() -> bool:
    """Whether clones are kept from rotating, i.e. the opt-out is NOT set."""
    return not get_settings().allow_clone_google_rotation


class RotationGuard:
    """One browser-level CDP connection that blocks the rotation endpoints on
    every target of one browser. Create with :func:`arm`."""

    def __init__(self, patterns: Sequence[str] = BLOCKED_URL_PATTERNS) -> None:
        self._patterns = list(patterns)
        self._ids = itertools.count(1)
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: set[int] = set()
        self._pending_by_session: dict[object, set[int]] = {}
        self._fetch_enable_ids: set[int] = set()
        self._settled = asyncio.Event()
        self.sessions = 0

    async def start(self, websocket_url: str) -> None:
        """Connect, attach to every existing and future target, and return once
        the targets that exist now are configured (bounded by
        :data:`SETTLE_TIMEOUT_SECONDS`)."""
        ws = await websockets.asyncio.client.connect(
            websocket_url,
            max_size=None,
            ping_interval=None,
            open_timeout=_OPEN_TIMEOUT_SECONDS,
            proxy=None,  # a system proxy must never sit between us and loopback
        )
        self._ws = ws
        _LIVE.add(self)
        self._reader = asyncio.create_task(self._read(ws))
        await self._send_auto_attach(None)
        await self._wait_settled()

    async def close(self) -> None:
        """Drop the connection. Chrome resumes anything still paused."""
        _LIVE.discard(self)
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def _send(
        self,
        method: str,
        params: dict[str, object] | None = None,
        session_id: str | None = None,
    ) -> None:
        message: dict[str, object] = {"id": next(self._ids), "method": method}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        self._pending.add(message["id"])
        self._pending_by_session.setdefault(session_id, set()).add(message["id"])
        if method == "Fetch.enable":
            self._fetch_enable_ids.add(message["id"])
        self._settled.clear()
        if self._ws is not None:
            await self._ws.send(json.dumps(message))

    async def _send_auto_attach(self, session_id: str | None) -> None:
        await self._send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
            session_id,
        )

    async def _configure(self, session_id: str, waiting: bool) -> None:
        """Guard one attached session, then let it run. The commands of a session
        are answered in order, so the resume cannot overtake the block."""
        self.sessions += 1
        try:
            await self._send(
                "Fetch.enable",
                {
                    "patterns": [
                        {"urlPattern": pattern, "requestStage": "Request"}
                        for pattern in self._patterns
                    ]
                },
                session_id,
            )
            await self._send_auto_attach(session_id)
        finally:
            if waiting:
                await self._send("Runtime.runIfWaitingForDebugger", None, session_id)

    async def _read(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                try:
                    await self._handle(json.loads(raw))
                except (ValueError, KeyError, TypeError) as err:
                    debug_logger.log_warning(
                        "google_rotation_guard",
                        "_read",
                        f"skipped a malformed CDP frame: {type(err).__name__}",
                    )
        except websockets.exceptions.ConnectionClosed:
            pass  # the browser closed: the normal end of a guard
        except Exception as err:  # noqa: BLE001  PERMANENT(F-939): a broken guard connection is logged, Chrome resumes every target
            debug_logger.log_warning(
                "google_rotation_guard", "_read", f"guard connection broke: {err!r}"
            )
        finally:
            _LIVE.discard(self)
            self._settled.set()
            # Closing is what makes Chrome resume the targets it holds paused
            # under waitForDebuggerOnStart; an open socket with no reader would
            # leave every new target frozen.
            with contextlib.suppress(Exception):
                await ws.close()

    async def _handle(self, message: dict[str, object]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if "id" in message:
            self._settle(message)
        elif method == "Fetch.requestPaused":
            await self._send(
                "Fetch.failRequest",
                {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                str(message["sessionId"]),
            )
        elif method == "Target.attachedToTarget":
            await self._configure(
                str(params["sessionId"]), bool(params.get("waitingForDebugger"))
            )
        elif method == "Target.detachedFromTarget":
            gone = params.get("sessionId")
            self._pending -= self._pending_by_session.pop(gone, set())
            self._release_if_idle()

    def _settle(self, message: dict[str, object]) -> None:
        """Account one reply; a refused ``Fetch.enable`` is a clone left unguarded
        on that target, which is worth a warning (shape only, never Chrome's text)."""
        reply_id = message["id"]
        self._pending.discard(reply_id)
        if "error" in message and reply_id in self._fetch_enable_ids:
            debug_logger.log_warning(
                "google_rotation_guard",
                "_settle",
                "Chrome refused Fetch.enable on a target; it is not guarded",
            )
        self._fetch_enable_ids.discard(reply_id)
        self._release_if_idle()

    def _release_if_idle(self) -> None:
        if not self._pending:
            self._settled.set()

    async def _wait_settled(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._settled.wait(), SETTLE_TIMEOUT_SECONDS)


async def arm(
    browser: GuardedBrowser, patterns: Sequence[str] = BLOCKED_URL_PATTERNS
) -> RotationGuard | None:
    """Start a guard on *browser*, or None when it could not be started. Never
    raises: a clone without the guard is a clone that may rotate, which is what
    every clone did before F-939 and is not a reason to fail a spawn."""
    guard = RotationGuard(patterns)
    try:
        await guard.start(browser.websocket_url)
    except Exception as err:  # noqa: BLE001  PERMANENT(F-939): any failure is a warning, never a failed spawn
        debug_logger.log_warning(
            "google_rotation_guard",
            "arm",
            f"could not guard the clone against Google cookie rotation: {err!r}",
        )
        await guard.close()
        return None
    return guard
