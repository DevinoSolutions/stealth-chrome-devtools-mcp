"""THE one home for "this MCP session was abandoned by its client — reap it" (F-862).

The backend's MCP layer (``mcp.server.streamable_http_manager``) lists one
transport per ``mcp-session-id`` it ever handed out. Up to mcp 1.28 it pruned
that list in exactly two places: an idle timeout FastMCP never configures, and a
crashed session — which explicitly SKIPS terminated ones. (mcp 1.30 DOES default
a 30-minute idle timeout: a session with no request in flight for that long is
reaped, and an open GET stream counts as in flight, so a live proxy holding its
stream is never reaped. A client with no stream, the CLI or a probe, gets a 404
after it and re-initializes, which ``proxy_selfheal`` handles. The sweep below
stays for its shorter window and for terminated entries.) A client's DELETE only marks
the transport terminated (so the id answers 404); the entry stays forever. A
client that simply goes away — TCP closed, process dead — leaves everything:
transport, ``ServerSession``, task group, the per-session lifespan. Every stdio
proxy's watchdog opens such a session on the backend every 2 s
(``singleton._backend_http_ready`` sends a real ``initialize``) and DELETEs it
best-effort, so on a 62-session fleet 31 sessions a second join the list for
good: measured at 2-7 KB each when the DELETE lands and 0.12 MB each when it is
lost. Two million probes a day is the 6.7 GB the 2026-09-11 backend showed after
18.5 h, with no lost DELETE assumed (finding F-862).

F-960 closes the same leak from both ends. The watchdog no longer opens a session
per beat at all (``backend_probe.Heartbeat`` asks :func:`_health` below, a route
that creates none). And for a proxy still running the old probe, a DELETE now
drops the session from the table when it lands (``handle_request``) instead of
leaving the terminated entry for the sweep: with 38 old proxies that was ~19
sessions a second listed for ``ABANDONED_AFTER_SECONDS`` each, ~5,600 at once.

:class:`HygienicSessionManager` is that manager with a sweep. Every
``SWEEP_INTERVAL_SECONDS`` it walks the live sessions; one that has NO standing
GET event stream and has made no request for ``ABANDONED_AFTER_SECONDS`` is
terminated through the transport's own ``terminate()`` (the same path a DELETE
takes, so a reaped id answers 404 exactly as a deleted one). The GET stream is
the discriminator: the MCP client opens it right after ``initialize`` and holds
it for the life of the session, so a live proxy — even one idle for hours — is
never touched, while a probe whose DELETE was lost, or a proxy that died, has no
stream and goes quiet — and so does a session the client DID delete, whose
terminated transport the layer would otherwise list forever. The window is long
on purpose (a probe session lives milliseconds) so nothing a real client does
can look abandoned.

:func:`install` is the seam: FastMCP builds the manager as
``fastmcp.server.http.FastMCPStreamableHTTPSessionManager(...)`` inside
``create_streamable_http_app``, by module attribute, so binding this class to that
name before ``mcp.run(transport="http")`` is the ONE place the substitution
happens. fastmcp 2 built the SDK's ``StreamableHTTPSessionManager`` there; under
fastmcp 3 binding that old name left the sweep silently unbuilt (F-946), so this
class now extends FastMCP's subclass and keeps its per-session event-store
scoping. ``tests/test_session_hygiene.py`` pins that FastMCP still constructs it
that way. Called from ``embedded/server.py``'s http branch as
``rt.session_hygiene.install(mcp)``, which also registers the health route.
Imports one embedded module, the ``backend_probe`` leaf, for the route's path.
"""

from __future__ import annotations

import contextlib
import logging
import time
import weakref
from typing import TYPE_CHECKING

import anyio
from fastmcp.server.http import FastMCPStreamableHTTPSessionManager
from mcp.server.streamable_http import GET_STREAM_KEY
from starlette.responses import JSONResponse

from stealth_chrome_devtools_mcp.embedded.backend_probe import HEALTH_PATH

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import Receive, Scope, Send

# A probe session lives milliseconds; a live proxy holds its GET stream. Five
# minutes of silence with no stream is a client that is gone, not one that is
# thinking. Read at sweep time so a test can shrink them.
ABANDONED_AFTER_SECONDS = 300.0
SWEEP_INTERVAL_SECONDS = 30.0
# F-1023: how long a backend spawned by a cold start (``--exit-if-unadopted``) waits
# for its FIRST client before it concludes nobody is coming. The same five minutes as
# above, for the same reason: it is "gone", not "thinking" (a proxy waits at most
# BACKEND_READY_TIMEOUT, 120 s, from its own start, and connects the moment we answer).
UNADOPTED_AFTER_SECONDS = ABANDONED_AFTER_SECONDS
# How often a pending decision looks again while a browser re-attach pass is running.
REATTACH_POLL_SECONDS = 5.0

_SESSION_HEADER = b"mcp-session-id"
_logger = logging.getLogger("stealth.backend")
_managers: list[weakref.ref[HygienicSessionManager]] = []
_routed: weakref.WeakSet[FastMCP] = weakref.WeakSet()


class HygienicSessionManager(FastMCPStreamableHTTPSessionManager):
    """The MCP session manager, plus the sweep that reaps abandoned sessions."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.last_seen: dict[str, float] = {}
        self.reaped_total = 0
        # F-1023: any request has reached /mcp, even once. Set on ENTRY to
        # handle_request, so a request still in flight counts, and an ``initialize``
        # probe counts as much as a proxy (a probe means someone is looking for this
        # backend, and the failure it can cause is the safe one: it stays up).
        self.ever_served = False
        _managers.append(weakref.ref(self))

    def note_activity(self, session_id: str, *, now: float | None = None) -> None:
        self.last_seen[session_id] = time.monotonic() if now is None else now

    @property
    def running(self) -> bool:
        """True between ``run()`` entering and the task group being cancelled."""
        group = self._task_group
        return group is not None and not group.cancel_scope.cancel_called

    async def handle_request(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.ever_served = True
        session_id = _session_id(scope.get("headers") or ())
        if session_id:
            self.note_activity(session_id)
        await super().handle_request(scope, receive, send)
        if session_id and scope.get("method") == "DELETE":
            self._forget_if_deleted(session_id)

    def _forget_if_deleted(self, session_id: str) -> None:
        """Drop a session its client just DELETEd (F-960).

        The SDK's DELETE only marks the transport terminated and leaves the entry
        listed, relying on the sweep to remove it ``ABANDONED_AFTER_SECONDS``
        later. Dropping it now changes only the 404's body: a terminated
        transport answered "Session has been terminated", an unknown id answers
        "Session not found", both 404, which is what the client reads.
        """
        transport = self._server_instances.get(session_id)
        if transport is None or not transport.is_terminated:
            return  # refused (bad headers) or already gone: nothing was deleted
        del self._server_instances[session_id]
        self._session_owners.pop(session_id, None)
        self.last_seen.pop(session_id, None)

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
                "(no event stream, silent > %.0fs); %d still listed, %d reaped so far",
                len(reaped),
                ABANDONED_AFTER_SECONDS,
                len(self._server_instances),
                self.reaped_total,
            )
        return reaped


def _never_reattaching() -> bool:
    return False


async def exit_if_unadopted(
    has_browsers: Callable[[], Awaitable[bool]],
    stop: Callable[[], None],
    reattaching: Callable[[], bool] = _never_reattaching,
) -> bool:
    """Stop this backend if no client ever came for it (F-1023); True iff it did.

    A cold start spawns a backend for a proxy, and the proxy can be gone before the
    backend ever listens (a slow, swapping machine; a client that quit). Nothing else
    ends such a backend: the browser idle reaper has no browsers to reap and the
    liveness machinery only judges backends someone is talking to, so it sat idle for
    as long as the machine stayed up (measured 2026-10-21: ~140 MB, no client).

    After :data:`UNADOPTED_AFTER_SECONDS` it ends the process through *stop* only when
    BOTH hold: no request ever reached ``/mcp`` (so no one adopted it, and an adopted
    backend is never touched however quiet it later goes), and it hosts no browser (a
    persistent-profile browser a previous backend left, adopted by F-888, is a reason to
    stay). "A request" is ANY request, an ``initialize`` probe included: a probe means
    someone is looking for this backend, and counting it fails safe (the backend stays).
    The ``/_stealth/health`` heartbeat is not a request to ``/mcp`` and does not count.

    A re-attach pass still running (*reattaching*) is waited out first: its browsers are
    not listed until it adopts them, so ``has_browsers`` would answer no for a backend
    that is about to host them. Both answers are read AGAIN after the awaits, directly
    before *stop*, because a request or a pass that began while ``has_browsers`` was
    suspended must abort the exit. Anything unclear leaves it running. Never raises into
    the lifespan."""
    await anyio.sleep(UNADOPTED_AFTER_SECONDS)
    while reattaching():  # noqa: ASYNC110  PERMANENT(the pass publishes a lock, not an event, and it ends within minutes)
        await anyio.sleep(REATTACH_POLL_SECONDS)
    manager = active_manager()
    if manager is None or manager.ever_served:
        return False
    try:
        if await has_browsers():
            return False
    except Exception as error:  # noqa: BLE001  PERMANENT(an unanswerable question keeps the backend)
        _logger.warning("unadopted-backend check could not list browsers: %r", error)
        return False
    if manager.ever_served or reattaching():
        return False
    _logger.warning(
        "no client reached this backend in %.0fs and it hosts no browser; exiting "
        "(F-1023)",
        UNADOPTED_AFTER_SECONDS,
    )
    stop()
    return True


def _session_id(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() == _SESSION_HEADER:
            return value.decode("latin-1")
    return None


async def _health(_request: Request) -> Response:
    """200 iff this process's MCP session manager is running; creates no session.

    Served by the same app and event loop as the MCP endpoint, so a loop that
    cannot turn cannot answer, and a manager that has not started (or has been
    cancelled) answers 503. That is everything the ``initialize`` probe proved,
    without minting a session per ask (F-960).
    """
    manager = active_manager()
    if manager is not None and manager.running:
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "session manager not running"}, status_code=503)


def install(server: FastMCP | None = None) -> type[HygienicSessionManager]:
    """Bind the hygienic manager to the name FastMCP constructs, and give
    ``server`` the health route. Idempotent."""
    from fastmcp.server import http

    http.FastMCPStreamableHTTPSessionManager = HygienicSessionManager
    if server is not None and server not in _routed:
        server.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)(
            _health
        )
        _routed.add(server)
    return HygienicSessionManager


def active_manager() -> HygienicSessionManager | None:
    """The most recently constructed manager that is still alive (tests, CLI)."""
    for ref in reversed(_managers):
        manager = ref()
        if manager is not None:
            return manager
    return None
