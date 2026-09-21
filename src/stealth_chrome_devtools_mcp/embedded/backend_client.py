"""THE one home for "call a tool on the backend at this URL, and read what it
answered" (F-891).

Its sibling is :mod:`backend_probe`, and the boundary between them is exactly
one question each. ``backend_probe`` asks *is this backend ready* — a raw
JSON-RPC ``initialize`` POST, no client library, no session kept, the answer a
bool, and it never raises. This module asks *do this*: it opens a real MCP
session with the official ``mcp`` SDK, calls a tool or lists the surface, and
hands back what came out. Neither may grow the other's question — a liveness
answer built on a full session would cost a handshake per probe, and a tool call
built on a hand-rolled POST would be a second spelling of the protocol.

**One session per call, and it is always terminated.** Every public function
here opens a session through :func:`opened` and closes it on the way out, which
is where the SDK's ``terminate_on_close=True`` sends the ``DELETE``. F-862's
sweep exists to reap sessions whose client vanished, and a CLI that left one
behind per invocation would be asking that sweep to clean up after it — so the
termination is not left to the sweep, and it is not re-spelled either: the SDK
already knows how to end its own session. A verb that makes two calls therefore
makes two sessions; that is deliberate, because the alternative is every verb
owning a session's lifetime and one of them eventually forgetting.

**Reading the answer is one function** (:func:`result_value`) and it is pure, so
the shapes can be pinned without a backend. Three of them are real and were
measured on the 2026-09-19 prototype that this feature replaces:

* ``structuredContent`` carries the answer and ``content`` is EMPTY — the common
  case. A reader that reached for ``content[0].text`` first raised ``IndexError``
  on every call that worked.
* a tool whose return is not a dict (``list_instances`` returns a LIST) arrives
  wrapped by FastMCP as ``{"result": [...]}``. Unwrapped only when ``result`` is
  the SOLE key, so a tool that genuinely answers with a ``result`` field beside
  others is left intact.
* text content as the fallback, parsed as JSON when it is JSON.

**One of the two things here is not about calling a tool**: :func:`http_client`
is THE transport seam for everything in this tree that opens an MCP session over
HTTP, and since F-900 that includes the stdio proxy's BRIDGE, which is not a
call at all. The bridge does not use :func:`opened` — it is a transparent pipe
and owns its own streams — but it must not build a second httpx client, so it
asks this seam with :data:`BRIDGE_READ_TIMEOUT`. Everything else in this module
stays the CLI's.

A leaf: the ``mcp`` SDK, ``httpx`` and stdlib, all imported lazily inside the
functions that need them (``backend_probe``'s reason — an ops verb that never
talks to a backend must not pay for the client). The URL arrives as an argument,
so nothing here decides WHICH backend is being driven; that is the caller's one
selection.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx
    from mcp import ClientSession

#: What this client calls itself in its ``initialize``, so a backend's logs can
#: tell a CLI call from a real MCP session and from a liveness probe
#: (``backend_probe.LIVENESS_CLIENT``, whose ``"0"`` this version copies — the
#: number identifies the CLIENT, and this one has no release of its own).
#: Reached through ``ClientSession(client_info=…)``, which is the SDK's spelling
#: of the ``clientInfo`` block ``backend_probe._request`` writes by hand.
CLIENT_NAME = "stealthy-cli"
CLIENT_VERSION = "0"

#: The default per-call budget. Deliberately generous and deliberately NOT a
#: `STEALTH_MCP_*` knob: it bounds a human waiting at a terminal, not a product
#: decision, and `--timeout` overrides it per invocation. A spawn on a cold
#: machine is the slow case the number is sized for.
DEFAULT_TIMEOUT_SECONDS = 180.0

#: The handshake's own budget, distinct from the tool's: reaching a backend that
#: is up should either happen quickly or be reported, and folding it into
#: ``--timeout`` would make a 300 s tool call wait 300 s to find out the socket
#: is gone.
CONNECT_TIMEOUT_SECONDS = 30.0

#: The read clock for the STDIO PROXY'S BRIDGE, which is this seam's second
#: consumer (F-900) — and it is ``None``, meaning no read deadline at all.
#:
#: A bridge is not a call. It holds a standing GET event stream for the life of
#: a Claude Code session, and the backend has nothing to send on that stream
#: while the session is quiet — ``mcp.server.streamable_http`` emits no SSE
#: keepalive. So ANY read deadline is a deadline on being IDLE. Measured: at the
#: SDK defaults the bridge inherited (``read=300``), the stream times out, and
#: ``handle_get_stream`` retries it ``MAX_RECONNECTION_ATTEMPTS`` (2) times and
#: then RETURNS — at DEBUG, telling the client nothing. After ~601 s of quiet a
#: live proxy holds no event stream, which is precisely the discriminator
#: :mod:`session_hygiene` uses to decide a session was ABANDONED (F-862), whose
#: docstring promises "a live proxy — even one idle for hours — is never
#: touched". A finite-but-larger number only moves that clock; ``None`` removes
#: it.
#:
#: What bounds the bridge instead is what already did: the F-820 watchdog
#: decides the backend is dead and ``proxy_selfheal`` heals, and an individual
#: tool call is bounded by ``tool_runtime._clamp_timeout`` + ``_with_cdp_
#: timeout`` at the tool body. A transport read timeout would be a SECOND answer
#: to "is the backend still there" — and it answers wrong, because an idle
#: session is not a dead backend.
BRIDGE_READ_TIMEOUT: float | None = None


class BackendCallError(Exception):
    """The backend answered, and the answer was a tool failure.

    Distinct from every transport error on purpose: this one means the round
    trip worked and the TOOL said no, which is the caller's problem to fix and
    exits 1. A connection that never opened is not this.
    """


def result_value(structured: object, texts: list[str]) -> object:
    """THE one reading of a tool's answer. Pure; see the module docstring for
    why each of the three shapes is real."""
    import json

    if isinstance(structured, dict) and tuple(structured) == ("result",):
        return structured["result"]
    if structured is not None:
        return structured
    if not texts:
        return None
    text = "\n".join(texts)
    try:
        return json.loads(text)
    except ValueError:
        return text


def _texts(content: object) -> list[str]:
    """Every text block in a tool result, in order; other block kinds (images,
    embedded resources) are not text and are skipped rather than stringified."""
    blocks = content if isinstance(content, list) else []
    return [
        block.text for block in blocks if isinstance(getattr(block, "text", None), str)
    ]


def _failure_message(name: str, structured: object, texts: list[str]) -> str:
    """What to tell the operator when ``isError`` came back true.

    The tool's own words, because they are the diagnostic — this is the same
    text the MCP client shows an agent. Nothing is added to it beyond the tool's
    name, and nothing is logged here: the payload is the caller's own data and
    the backend has already recorded the failure (F-835).
    """
    if texts:
        return "\n".join(texts)
    if structured is not None:
        import json

        return json.dumps(structured, default=str)
    return f"{name} failed and said nothing about why"


def http_client(read_seconds: float | None) -> httpx.AsyncClient:
    """THE one transport every session in this tree rides on, and the ONE seam a
    test replaces to drive the real SDK against a fake server.

    It is a named module function rather than an inline constructor for the
    reason ``scroll_position._now``/``_sleep`` are: the thing worth substituting
    is the transport, not the protocol, and a pin that swapped the protocol
    would be pinning a double instead of the SDK.

    The two clocks are the whole of what is decided here, and they are two
    because ``httpx.Timeout(connect_and_write, read=…)`` is two. Connecting
    always keeps :data:`CONNECT_TIMEOUT_SECONDS`, so a backend whose socket is
    gone is reported in seconds rather than at the end of a long read budget.
    ``follow_redirects=True`` is the MCP default (``create_mcp_http_client``)
    and is kept so nothing about this transport is narrower than the one the SDK
    would have built.

    **``read_seconds`` and not ``budget_seconds``, because there are now two
    consumers and only one of them has a budget** (F-900). A CLI verb passes the
    per-call budget a human is waiting on; the stdio proxy's bridge passes
    :data:`BRIDGE_READ_TIMEOUT`, which is ``None`` — the argument for that is at
    the constant. Extending this function rather than adding a second one is
    convention 4 read literally: a second constructor would be a second place
    the connect clock, ``follow_redirects`` and the client's construction are
    decided, which is the drift this seam exists to prevent. ``None`` is not a
    new concept here either — it is httpx's own spelling of "no deadline" on the
    same clock this parameter has always set.
    """
    import httpx

    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(CONNECT_TIMEOUT_SECONDS, read=read_seconds),
    )


@asynccontextmanager
async def opened(
    url: str, *, budget_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> AsyncIterator[ClientSession]:
    """An initialized MCP session against ``url``, terminated on the way out.

    ``terminate_on_close=True`` is passed EXPLICITLY although it is also the
    SDK's default: the DELETE is the thing this context manager exists to
    guarantee, and a default is not a guarantee — it is a value that can change
    in a dependency bump without anything here failing.

    **``streamable_http_client``, not ``streamablehttp_client``** (F-891
    review S2). At the pinned ``mcp`` 1.27.1 the latter is
    ``@deprecated("Use `streamable_http_client` instead.")``; measured, it still
    honours ``timeout``/``sse_read_timeout`` — it builds
    ``httpx.Timeout(timeout, read=sse_read_timeout)`` and hands the client down
    — so this is a migration and not a bug fix. The replacement takes the
    ``httpx.AsyncClient`` itself, which is why :func:`http_client` exists and
    why the budget is set there. Note the ownership rule that comes with it: a
    client we PASS is a client the SDK does not close, so it is entered here.

    The budget's name is ``budget_seconds`` and not ``timeout`` on
    ``navigation_milestone``'s precedent: a parameter called ``timeout`` on an
    async function reads as "wrap me in ``asyncio.timeout``" (ASYNC109), and
    this one is the opposite — it is handed DOWN to the transport, which is the
    only layer that can bound a reply without abandoning it.
    """
    from mcp import ClientSession as Session
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import Implementation

    async with (
        http_client(budget_seconds) as client,
        streamable_http_client(url, http_client=client, terminate_on_close=True) as (
            read,
            write,
            _,
        ),
        Session(
            read,
            write,
            client_info=Implementation(name=CLIENT_NAME, version=CLIENT_VERSION),
        ) as session,
    ):
        await session.initialize()
        yield session


async def call_tool(
    url: str,
    name: str,
    arguments: dict[str, object],
    *,
    budget_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> object:
    """Call one tool and return its answer, raising on a tool failure."""
    from datetime import timedelta

    async with opened(url, budget_seconds=budget_seconds) as session:
        result = await session.call_tool(
            name, arguments, read_timeout_seconds=timedelta(seconds=budget_seconds)
        )
    texts = _texts(result.content)
    structured = result.structuredContent
    if result.isError:
        raise BackendCallError(_failure_message(name, structured, texts))
    return result_value(structured, texts)


async def list_tools(
    url: str, *, budget_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> list[dict[str, object]]:
    """The LIVE tool surface of the backend at ``url``, in the order it serves
    them, as plain dicts so no caller has to know the SDK's model types."""
    async with opened(url, budget_seconds=budget_seconds) as session:
        listed = await session.list_tools()
    return [
        {
            "name": tool.name,
            "title": tool.title,
            "description": tool.description,
        }
        for tool in listed.tools
    ]
