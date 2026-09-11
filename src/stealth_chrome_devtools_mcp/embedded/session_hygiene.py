"""THE one home for "this MCP session was abandoned by its client — reap it" (F-862).

The backend's MCP layer (``mcp.server.streamable_http_manager``) keeps one
transport, one ``ServerSession``, one task group and one run of the per-session
lifespan for every ``mcp-session-id`` it ever handed out, and forgets a session
in exactly two cases: the client's own DELETE, or an idle timeout FastMCP never
configures. Nothing else ends it — not the client's TCP connection closing, not
its process dying. Every stdio proxy's watchdog opens such a session on the
backend every 2 s (``singleton._backend_http_ready`` sends a real ``initialize``)
and DELETEs it best-effort on the same 2 s budget, so a DELETE that times out
under load is a session the backend keeps forever, at ~0.11 MB each. Sixty-two
proxies issue 31 of those a second; a 3 % failure rate is 6.7 GB in 18.5 h, which
is what the 2026-09-11 backend measured (finding F-862).

:class:`HygienicSessionManager` is that manager with a sweep. Every
``SWEEP_INTERVAL_SECONDS`` it walks the live sessions; one that has NO standing
GET event stream and has made no request for ``ABANDONED_AFTER_SECONDS`` is
terminated through the transport's own ``terminate()`` (the same path a DELETE
takes, so a reaped id answers 404 exactly as a deleted one). The GET stream is
the discriminator: the MCP client opens it right after ``initialize`` and holds
it for the life of the session, so a live proxy — even one idle for hours — is
never touched, while a probe whose DELETE was lost, or a proxy that died, has no
stream and goes quiet. The window is long on purpose (a probe session lives
milliseconds) so nothing a real client does can look abandoned.

:func:`install` is the seam: FastMCP builds the manager as
``fastmcp.server.http.StreamableHTTPSessionManager(...)`` inside
``create_streamable_http_app``, by module attribute, so binding this class to that
name before ``mcp.run(transport="http")`` is the ONE place the substitution
happens. ``tests/test_session_hygiene.py`` pins that FastMCP still constructs it
that way. Called from ``embedded/server.py``'s http branch as
``rt.session_hygiene.install()``. A leaf: imports no other embedded module.
"""

from __future__ import annotations

import contextlib
import logging
import time
import weakref
from typing import TYPE_CHECKING

import anyio
from mcp.server.streamable_http import GET_STREAM_KEY
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from starlette.types import Receive, Scope, Send

# A probe session lives milliseconds; a live proxy holds its GET stream. Five
# minutes of silence with no stream is a client that is gone, not one that is
# thinking. Read at sweep time so a test can shrink them.
ABANDONED_AFTER_SECONDS = 300.0
SWEEP_INTERVAL_SECONDS = 30.0

_SESSION_HEADER = b"mcp-session-id"
_logger = logging.getLogger("stealth.backend")
_managers: list[weakref.ref[HygienicSessionManager]] = []


class HygienicSessionManager(StreamableHTTPSessionManager):
    """The MCP session manager, plus the sweep that reaps abandoned sessions."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.last_seen: dict[str, float] = {}
        self.reaped_total = 0
        _managers.append(weakref.ref(self))

    def note_activity(self, session_id: str, *, now: float | None = None) -> None:
        self.last_seen[session_id] = time.monotonic() if now is None else now

    async def handle_request(self, scope: Scope, receive: Receive, send: Send) -> None:
        session_id = _session_id(scope.get("headers") or ())
        if session_id:
            self.note_activity(session_id)
        await super().handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with super().run(), anyio.create_task_group() as tasks:
            tasks.start_soon(self._sweep_forever)
            try:
                yield
            finally:
                tasks.cancel_scope.cancel()

    async def _sweep_forever(self) -> None:
        while True:
            await anyio.sleep(SWEEP_INTERVAL_SECONDS)
            await self.sweep_once()

    async def sweep_once(self, *, now: float | None = None) -> list[str]:
        """Terminate every abandoned session; return the ids reaped this pass."""
        now = time.monotonic() if now is None else now
        for session_id in [
            s for s in self.last_seen if s not in self._server_instances
        ]:
            del self.last_seen[session_id]
        reaped: list[str] = []
        for session_id, transport in list(self._server_instances.items()):
            if GET_STREAM_KEY in transport._request_streams:
                self.last_seen[session_id] = now  # an open event stream IS activity
                continue
            if (
                now - self.last_seen.setdefault(session_id, now)
                < ABANDONED_AFTER_SECONDS
            ):
                continue
            self._server_instances.pop(session_id, None)
            self.last_seen.pop(session_id, None)
            try:
                await transport.terminate()
            except Exception as error:  # noqa: BLE001  PERMANENT(a sweep must not die on one bad session)
                _logger.warning(
                    "session hygiene: terminating abandoned session %s failed: %r",
                    session_id,
                    error,
                )
                continue
            reaped.append(session_id)
        if reaped:
            self.reaped_total += len(reaped)
            _logger.info(
                "session hygiene: reaped %d abandoned MCP session(s) "
                "(no event stream, silent > %.0fs); %d live remain, %d reaped so far",
                len(reaped),
                ABANDONED_AFTER_SECONDS,
                len(self._server_instances),
                self.reaped_total,
            )
        return reaped


def _session_id(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() == _SESSION_HEADER:
            return value.decode("latin-1")
    return None


def install() -> type[HygienicSessionManager]:
    """Bind the hygienic manager to the name FastMCP constructs. Idempotent."""
    from fastmcp.server import http

    http.StreamableHTTPSessionManager = HygienicSessionManager
    return HygienicSessionManager


def active_manager() -> HygienicSessionManager | None:
    """The most recently constructed manager that is still alive (tests, CLI)."""
    for ref in reversed(_managers):
        manager = ref()
        if manager is not None:
            return manager
    return None
