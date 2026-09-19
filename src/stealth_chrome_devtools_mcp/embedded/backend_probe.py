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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_logger = logging.getLogger("stealth.proxy")

#: The only answer that proves the MCP layer will accept a client's session. A
#: freshly bound uvicorn socket answers 4xx while FastMCP's session manager is
#: still starting, so "any HTTP response" is not the test.
_READY_STATUS = 200

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
