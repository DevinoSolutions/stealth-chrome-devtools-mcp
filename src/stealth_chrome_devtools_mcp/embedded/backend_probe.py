"""THE one home for "ask the backend's MCP endpoint whether it is genuinely
ready", in both the shapes this tree needs.

Extracted from ``singleton`` by the F-889 review, which needed room in a file at
exactly its LOC budget — and which found the extraction already named. That
file's own docstring had carried it as a standing debt since plan_M1:

    The ~10 duplicated lines of that twin's ``initialize`` shape are deliberate
    (plan_M1 SS2.2 #4: M1/M3 regions stay disjoint); consolidating is a finding.

This is that consolidation. The two callers keep their DIFFERENT shapes, because
the difference is real and load-bearing:

* :func:`ready` is ONE attempt, synchronous, so a sync caller (discovery, the
  CLI) can call it directly and the watchdog can drive it off-thread;
* :func:`await_ready` POLLS to a deadline, asynchronously, because a cold start
  has to be waited out rather than sampled.

What they no longer duplicate is the thing that was actually copied: the
``initialize`` REQUEST — its JSON-RPC envelope, its negotiated protocol version,
its two headers, and the throwaway-session ``DELETE`` that keeps a probe from
leaking one MCP session per call. A second spelling of that payload is how a
probe comes to prove something subtly different from what the backend will
actually do for a client.

**Why an ``initialize`` and not a socket connect** (F-301/F-501): a wedged
backend — dispatch loop dead, socket still open — passes a connect every time,
so the sole auto-recovery watchdog never armed against the exact failure it
exists for. And not "any HTTP response" either: a freshly bound uvicorn socket
answers 4xx while FastMCP's session manager is still starting, and forwarding to
it then fails with 400. Only a 200 to a real ``initialize`` proves the MCP layer
will accept a client's session.

**The steady heartbeat is the exception, and only because it is steady**
(F-960). Every stdio proxy's watchdog asks every ~2 s, for as long as it lives.
Paid as an ``initialize`` that is a new TCP connection (TIME_WAIT) and a new MCP
session, ServerSession and task group on the backend per beat: 38 proxies left
~5,600 sessions listed and ~2,200 sockets in TIME_WAIT. :class:`Heartbeat` asks
:data:`HEALTH_PATH` instead — a route on the SAME app and event loop that
answers 200 only while the session manager is running, and creates no session —
over ONE keep-alive connection. It proves the same two things the
``initialize`` was chosen for: a wedged loop cannot answer, and a manager still
starting answers 503. Against a backend that predates the route (404) it falls
back to :func:`ready`, so mixed versions keep working. The cold-start poll and
the identity gate stay on ``initialize``: they are rare, and a first contact
should prove the MCP layer end to end.

**Never raises.** Every failure — connection refused, timeout, a malformed
answer — resolves to False. That is ``singleton._server_is_healthy``'s
fail-closed contract: a probe error reads as "not ready" and never propagates,
and the CALLER decides when repeated failures are worth a WARNING. DEBUG here,
because both of these fire routinely during an ordinary cold start.

A leaf: ``httpx`` and ``mcp.types`` (imported lazily, inside the functions, so
the stdio proxy's cold start does not pay for them before it needs them) and
nothing of ours. The URL arrives as an argument, so nothing here decides which
backend is being asked about.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx

_logger = logging.getLogger("stealth.proxy")

#: THE path the backend serves MCP on, passed to ``mcp.run(path=…)`` by the
#: backend and used in every URL the proxy builds (F-943). Explicit because the
#: library's DEFAULT moved under us: fastmcp 2.11 served ``/mcp/`` and
#: redirected ``/mcp``, 2.14 serves ``/mcp`` and 307s ``/mcp/`` — and a probe
#: that does not follow redirects then never sees its 200, so every proxy timed
#: out on a backend that was up. Pinned to the 2.11 spelling so a proxy and a
#: backend from either side of the bump still agree.
MCP_PATH = "/mcp/"

#: THE path the backend answers the session-free liveness question on (F-960),
#: registered by ``session_hygiene.install`` and asked by :class:`Heartbeat`.
#: Nowhere near :data:`MCP_PATH`, so no MCP request can ever be routed to it.
HEALTH_PATH = "/_stealth/health"

#: The only answer that proves the MCP layer will accept a client's session. A
#: freshly bound uvicorn socket answers 4xx while FastMCP's session manager is
#: still starting, so "any HTTP response" is not the test.
_READY_STATUS = 200

#: What a backend that predates :data:`HEALTH_PATH` answers there.
_NO_ROUTE_STATUS = 404

#: What a probe calls itself, so a backend's logs can tell the two apart from
#: each other and from a real client.
LIVENESS_CLIENT = "liveness-probe"
READINESS_CLIENT = "readiness-probe"

#: The poll cadence :func:`await_ready` starts at and the ceiling it backs off
#: to. Unchanged from the loop this moved out of.
_POLL_START_SECONDS = 0.1
_POLL_MAX_SECONDS = 1.0
#: The per-attempt transport budget inside the polling loop.
_POLL_ATTEMPT_SECONDS = 10.0

_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _request(client_name: str) -> dict[str, object]:
    """THE one ``initialize`` payload. Both probes send exactly this."""
    from mcp.types import DEFAULT_NEGOTIATED_VERSION

    return {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": DEFAULT_NEGOTIATED_VERSION,
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "0"},
        },
    }


def ready(url: str, timeout: float, *, client_name: str = LIVENESS_CLIENT) -> bool:
    """One synchronous attempt: True iff ``url`` answers an ``initialize`` 200."""
    import httpx

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.post(url, json=_request(client_name), headers=_HEADERS)
            if resp.status_code != _READY_STATUS:
                return False
            _discard_session(client.delete, url, resp.headers.get("mcp-session-id"))
            return True
    except Exception as exc:  # noqa: BLE001  PERMANENT(fail-closed: a probe error is "not ready")
        _logger.debug("liveness probe attempt failed", exc_info=exc)
        return False


def health_url(mcp_url: str) -> str:
    """The :data:`HEALTH_PATH` URL on the same backend ``mcp_url`` points at."""
    from urllib.parse import urlsplit, urlunsplit

    return urlunsplit(
        urlsplit(mcp_url)._replace(path=HEALTH_PATH, query="", fragment="")
    )


class Heartbeat:
    """The steady liveness probe: one keep-alive connection, no MCP session.

    One per watchdog (F-960). :meth:`alive` is :func:`ready`'s contract — one
    synchronous attempt, never raises, False on any failure — so it drops into
    the same off-thread slot. It is safe to call from whichever worker thread
    the watchdog's ``to_thread`` picks: the client is built under a lock and
    ``httpx.Client`` is itself thread-safe.

    A failed beat discards the client, so the next one opens a fresh
    connection rather than trusting a socket the failure may have poisoned.
    """

    def __init__(self, mcp_url: str) -> None:
        self._mcp_url = mcp_url
        self._health_url = health_url(mcp_url)
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._legacy = False

    def alive(self, timeout: float) -> bool:
        """True iff the backend's session manager is running (see the module doc)."""
        if self._legacy:
            if ready(self._mcp_url, timeout):
                return True
            self._legacy = False  # a replaced backend may serve the route now
            return False
        try:
            resp = self._connection().get(self._health_url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001  PERMANENT(fail-closed: a probe error is "not alive")
            _logger.debug("liveness heartbeat failed", exc_info=exc)
            self.close()
            return False
        if resp.status_code == _NO_ROUTE_STATUS:
            _logger.debug(
                "backend has no %s route; probing with initialize", HEALTH_PATH
            )
            self._legacy = True
            return self.alive(timeout)
        return resp.status_code == _READY_STATUS

    def close(self) -> None:
        """Drop the connection. Best-effort; a later :meth:`alive` reopens it."""
        with self._lock:
            client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception:  # noqa: BLE001  PERMANENT(closing a probe connection that is being discarded anyway)
            _logger.debug("heartbeat connection close failed", exc_info=True)

    def _connection(self) -> httpx.Client:
        import httpx

        with self._lock:
            if self._client is None:
                self._client = httpx.Client()
            return self._client


async def await_ready(
    url: str, deadline_seconds: float, *, client_name: str = READINESS_CLIENT
) -> bool:
    """Poll ``url`` until it answers an ``initialize`` 200, or the deadline."""
    import time

    import anyio
    import httpx

    deadline = time.monotonic() + deadline_seconds
    interval = _POLL_START_SECONDS
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_POLL_ATTEMPT_SECONDS)
    ) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.post(
                    url, json=_request(client_name), headers=_HEADERS
                )
                if resp.status_code == _READY_STATUS:
                    await _discard_session_async(
                        client.delete, url, resp.headers.get("mcp-session-id")
                    )
                    return True
            except Exception:  # noqa: BLE001  PERMANENT(expected during a cold start)
                _logger.debug("backend readiness probe attempt failed", exc_info=True)
            await anyio.sleep(interval)
            interval = min(interval * 1.5, _POLL_MAX_SECONDS)
    return False


def _discard_session(
    delete: Callable[..., object], url: str, session_id: str | None
) -> None:
    """Terminate the throwaway session the probe just minted.

    Best-effort and never fatal: the probe has ALREADY succeeded by the time
    this runs, so a failure here must not turn a ready backend into a not-ready
    answer. Without it every probe leaks one MCP session on the backend, which
    is the accumulation F-862's sweep exists to clean up after.
    """
    if not session_id:
        return
    try:
        delete(url, headers={**_HEADERS, "mcp-session-id": session_id})
    except Exception:  # noqa: BLE001  PERMANENT(cleanup after an already-successful probe)
        _logger.debug("probe session cleanup failed", exc_info=True)


async def _discard_session_async(
    delete: Callable[..., Awaitable[object]], url: str, session_id: str | None
) -> None:
    """:func:`_discard_session`, awaited — same contract, same reasons."""
    if not session_id:
        return
    try:
        await delete(url, headers={**_HEADERS, "mcp-session-id": session_id})
    except Exception:  # noqa: BLE001  PERMANENT(cleanup after an already-successful probe)
        _logger.debug("probe session cleanup failed", exc_info=True)
