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

**A 401 is its own verdict, and not a strike** (F-1018 follow-up). The backend
answered, so it is alive; what is wrong is OUR credential (the token file deleted,
unreadable behind an antivirus lock, or another user's). :meth:`Heartbeat.verdict`
says :data:`VERDICT_UNAUTHORIZED` for it, :meth:`Heartbeat.alive` stays True, and the
watchdog never strikes a healthy backend for something only this client can fix —
which, with the confirmation probe 401ing the same way, used to condemn a backend
that owned browsers. The cause is logged at WARNING once per run of 401s, not per beat.

**The same three verdicts for every other reader** (T-6550). :func:`probe` is the one
attempt that tells them apart; :func:`ready` is ``probe == alive`` for the callers that
need "can I talk to it" (the CLI's ``responsive``/``wedged`` ladder, the port-selection
and release-gate probes). ``singleton._same_identity_backend_ready`` is the reuse gate
and the cold-start and self-heal paths consult it, so it asks :func:`probe` and treats
:data:`VERDICT_UNAUTHORIZED` as a backend of ours that is merely refusing this client:
never evicted, never replaced, never answered DEAD. :func:`await_ready` logs the same
cause once while it keeps polling.

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
import time
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import backend_token

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

#: What a backend that enforces the bearer token answers a client without it
#: (``backend_token``): proof that it is alive and that THIS client cannot talk to it.
_UNAUTHORIZED_STATUS = 401

#: The three things a :class:`Heartbeat` beat can conclude.
VERDICT_ALIVE = "alive"
VERDICT_UNAUTHORIZED = "unauthorized"
VERDICT_DEAD = "dead"

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


def _headers() -> dict[str, str]:
    """What every probe request sends: the JSON-RPC content types plus F-1018's
    bearer token (empty when there is none, which an old backend ignores and a
    new one answers 401 - "not ready", the same fail-closed verdict)."""
    return {**_HEADERS, **backend_token.auth_headers()}


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


def _is_ours_401(resp: httpx.Response) -> bool:
    """True iff ``resp`` is a 401 the bearer-token middleware wrote: its own body
    and ``www-authenticate`` (``backend_token.is_our_refusal``), which any other
    auth-protected server on the port does not send. THE one place that judges it."""
    return resp.status_code == _UNAUTHORIZED_STATUS and backend_token.is_our_refusal(
        resp.headers, resp.content
    )


def _verdict_of(resp: httpx.Response) -> str:
    if _is_ours_401(resp):
        return VERDICT_UNAUTHORIZED
    return VERDICT_ALIVE if resp.status_code == _READY_STATUS else VERDICT_DEAD


def probe(url: str, timeout: float, *, client_name: str = LIVENESS_CLIENT) -> str:
    """One synchronous attempt: :data:`VERDICT_ALIVE` iff ``url`` answers an
    ``initialize`` 200, :data:`VERDICT_UNAUTHORIZED` iff it answers with OUR
    backend's own 401 (alive, and refusing only us), else :data:`VERDICT_DEAD` -
    a 401 in anyone else's words is a stranger on the port. Never raises."""
    import httpx

    verdict = VERDICT_DEAD
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.post(url, json=_request(client_name), headers=_headers())
            verdict = _verdict_of(resp)
            if verdict == VERDICT_ALIVE:
                _discard_session(client.delete, url, resp.headers.get("mcp-session-id"))
    except Exception as exc:  # noqa: BLE001  PERMANENT(fail-closed: a probe error is "not ready")
        _logger.debug("liveness probe attempt failed", exc_info=exc)
    _note(url, verdict)
    _last.seen = (url, verdict)
    return verdict


def ready(url: str, timeout: float, *, client_name: str = LIVENESS_CLIENT) -> bool:
    """One synchronous attempt: True iff ``url`` answers an ``initialize`` 200.

    "Can I talk to it", and a 401 is no: for "is it alive" ask :func:`probe`."""
    return probe(url, timeout, client_name=client_name) == VERDICT_ALIVE


def refused(url: str, timeout: float = 2.0) -> bool:
    """True iff ``url`` answers OUR backend's 401: up, and refusing only us.
    Answered in a 401's own time, so the budget is short (a display path)."""
    return probe(url, timeout) == VERDICT_UNAUTHORIZED


#: How long one run of 401s from the same backend is waited out before only a
#: restart cures it: a token file regenerated under a backend that keeps the old
#: one in memory 401s every client for ever. Past this, ``refusal`` says EVICT -
#: and ``backend_eviction.clear_stale`` still spares a backend that owns browsers.
PERSISTENT_UNAUTHORIZED_SECONDS = 120.0

#: Set while the current run of 401s has been said, cleared by any other answer, so
#: the WARNING is once per run and not once per probe (a cold start polls for
#: seconds). Heartbeat keeps its own per-instance flag: it is ONE watchdog's run.
_unauthorized_said = threading.Event()
#: url -> when its current run of 401s began; any other answer ends the run.
_unauthorized_since: dict[str, float] = {}
#: The clock the run is measured on (a seam: the suite sets it).
_clock = time.monotonic
#: The last ``(url, verdict)`` this thread's :func:`probe` reached, for :func:`refusal`.
_last = threading.local()


def _note(url: str, verdict: str) -> None:
    """Record one answer: the WARNING once per run of 401s, and when the run began."""
    if verdict != VERDICT_UNAUTHORIZED:
        _unauthorized_said.clear()
        _unauthorized_since.pop(url, None)
        return
    _unauthorized_since.setdefault(url, _clock())
    if not _unauthorized_said.is_set():
        _unauthorized_said.set()
        _logger.warning(
            "%s; the backend is answering, so it is not treated as dead",
            backend_token.UNAUTHORIZED,
        )


def refusal(url: str, *, ours: Callable[[], bool]) -> bool | None:
    """What the reuse gate does with the answer THIS thread's last :func:`probe`
    of ``url`` got, consuming it: ``None`` when it was no 401 of ours or ``ours()``
    cannot vouch that the process answering is our own backend (a stranger on the
    port is a stranger, and gets no more trust for answering 401); ``True`` to
    reuse a backend that is merely refusing us; ``False`` once that has gone on
    past :data:`PERSISTENT_UNAUTHORIZED_SECONDS`, so only a restart cures it.

    Reads the probe the gate just made, so a gate pass costs ONE ``initialize``."""
    seen, _last.seen = getattr(_last, "seen", None), None
    if seen != (url, VERDICT_UNAUTHORIZED) or not ours():
        return None
    since = _unauthorized_since.get(url, _clock())
    return _clock() - since < PERSISTENT_UNAUTHORIZED_SECONDS


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
        self._said_unauthorized = False

    def alive(self, timeout: float) -> bool:
        """False only for :data:`VERDICT_DEAD`: the session manager is running, or
        the backend answered and refused us (:data:`VERDICT_UNAUTHORIZED`, which is
        not a strike). See the module doc; :meth:`verdict` says which."""
        return self.verdict(timeout) != VERDICT_DEAD

    def verdict(self, timeout: float) -> str:
        """One beat: :data:`VERDICT_ALIVE`, :data:`VERDICT_UNAUTHORIZED` or
        :data:`VERDICT_DEAD`. Never raises."""
        if self._legacy:
            verdict = probe(self._mcp_url, timeout)
            if verdict == VERDICT_DEAD:
                self._legacy = False  # a replaced backend may serve the route now
            return verdict
        try:
            resp = self._connection().get(
                self._health_url, timeout=timeout, headers=backend_token.auth_headers()
            )
        except Exception as exc:  # noqa: BLE001  PERMANENT(fail-closed: a probe error is "not alive")
            _logger.debug("liveness heartbeat failed", exc_info=exc)
            self.close()
            self._said_unauthorized = False  # a 401 after an outage is a new run
            return VERDICT_DEAD
        if resp.status_code == _NO_ROUTE_STATUS:
            _logger.debug(
                "backend has no %s route; probing with initialize", HEALTH_PATH
            )
            self._legacy = True
            return self.verdict(timeout)
        if _is_ours_401(resp):
            if not self._said_unauthorized:
                self._said_unauthorized = True
                _logger.warning(
                    "liveness heartbeat: %s; the backend is answering, so this is "
                    "not counted as a failed beat",
                    backend_token.UNAUTHORIZED,
                )
            return VERDICT_UNAUTHORIZED
        self._said_unauthorized = False
        return _verdict_of(resp)

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
                    url, json=_request(client_name), headers=_headers()
                )
                verdict = _verdict_of(resp)
                _note(url, verdict)
                if verdict == VERDICT_ALIVE:
                    await _discard_session_async(
                        client.delete, url, resp.headers.get("mcp-session-id")
                    )
                    return True
            except Exception:  # noqa: BLE001  PERMANENT(expected during a cold start)
                _logger.debug("backend readiness probe attempt failed", exc_info=True)
                _note(url, VERDICT_DEAD)
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
        delete(url, headers={**_headers(), "mcp-session-id": session_id})
    except Exception:  # noqa: BLE001  PERMANENT(cleanup after an already-successful probe)
        _logger.debug("probe session cleanup failed", exc_info=True)


async def _discard_session_async(
    delete: Callable[..., Awaitable[object]], url: str, session_id: str | None
) -> None:
    """:func:`_discard_session`, awaited — same contract, same reasons."""
    if not session_id:
        return
    try:
        await delete(url, headers={**_headers(), "mcp-session-id": session_id})
    except Exception:  # noqa: BLE001  PERMANENT(cleanup after an already-successful probe)
        _logger.debug("probe session cleanup failed", exc_info=True)
