"""Pins for F-862: the backend reaps MCP sessions their client abandoned.

Every stdio proxy's watchdog opens a throwaway MCP session on the backend every
2 s (``singleton._backend_http_ready``: a real ``initialize``) and terminates it
best-effort with a DELETE on the same 2 s budget. A DELETE that times out leaves
a session the MCP layer keeps FOREVER — ``StreamableHTTPSessionManager`` removes
a session only on its own DELETE or on an idle timeout FastMCP never sets — and
each one holds a transport, a ``ServerSession``, its task group and the
per-session lifespan: measured at ~0.11 MB for an initialize-only session, ~0.4
MB with a ``tools/list``. Sixty-two proxies probe 31 times a second; a 3 % DELETE
failure rate is 6.7 GB in 18.5 h, which is what the 2026-09-11 backend showed.

``session_hygiene.HygienicSessionManager`` is the MCP session manager with a
sweep: a session with NO standing GET stream (a live proxy always holds one —
the MCP client opens it right after ``initialize``) and no request for
``ABANDONED_AFTER_SECONDS`` is terminated through the transport's own
``terminate()``. Hermetic: the sweep is exercised against fake transports with
an injected clock, and the end-to-end pin runs a tiny FastMCP app in-process
over real streamable HTTP — no Chrome, no ``~/.stealth-mcp``.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp.server.lowlevel import Server
from mcp.server.streamable_http import GET_STREAM_KEY

from stealth_chrome_devtools_mcp.embedded import session_hygiene
from stealth_chrome_devtools_mcp.embedded.session_hygiene import (
    ABANDONED_AFTER_SECONDS,
    HygienicSessionManager,
)


class FakeTransport:
    """Just the surface the sweep reads: streams, termination."""

    def __init__(self, *, get_stream: bool = False):
        self._request_streams: dict = {GET_STREAM_KEY: object()} if get_stream else {}
        self.terminated = 0

    @property
    def is_terminated(self) -> bool:
        return self.terminated > 0

    async def terminate(self) -> None:
        self.terminated += 1


def _manager() -> HygienicSessionManager:
    return HygienicSessionManager(app=Server("hygiene-test"))


@pytest.mark.asyncio
async def test_a_young_streamless_session_is_kept():
    manager = _manager()
    orphan = FakeTransport()
    manager._server_instances["orphan"] = orphan

    await manager.sweep_once(now=1000.0)
    await manager.sweep_once(now=1000.0 + ABANDONED_AFTER_SECONDS - 1)

    assert orphan.terminated == 0
    assert "orphan" in manager._server_instances


@pytest.mark.asyncio
async def test_an_abandoned_session_is_terminated_and_forgotten():
    """THE leak: no GET stream, nothing heard for the whole window."""
    manager = _manager()
    orphan = FakeTransport()
    manager._server_instances["orphan"] = orphan

    await manager.sweep_once(now=1000.0)
    reaped = await manager.sweep_once(now=1000.0 + ABANDONED_AFTER_SECONDS)

    assert reaped == ["orphan"]
    assert orphan.terminated == 1
    assert "orphan" not in manager._server_instances
    assert "orphan" not in manager.last_seen, "the tracker must not leak either"


@pytest.mark.asyncio
async def test_a_session_holding_its_get_stream_is_never_reaped():
    """A live proxy's session, idle for hours: the open event stream IS activity."""
    manager = _manager()
    live = FakeTransport(get_stream=True)
    manager._server_instances["live"] = live

    await manager.sweep_once(now=0.0)
    await manager.sweep_once(now=ABANDONED_AFTER_SECONDS * 1000)

    assert live.terminated == 0
    assert "live" in manager._server_instances


@pytest.mark.asyncio
async def test_a_request_resets_the_clock():
    manager = _manager()
    quiet = FakeTransport()
    manager._server_instances["quiet"] = quiet

    await manager.sweep_once(now=0.0)
    manager.note_activity("quiet", now=ABANDONED_AFTER_SECONDS - 1)
    await manager.sweep_once(now=ABANDONED_AFTER_SECONDS + 1)

    assert quiet.terminated == 0

    await manager.sweep_once(now=2 * ABANDONED_AFTER_SECONDS)

    assert quiet.terminated == 1


@pytest.mark.asyncio
async def test_one_failing_terminate_does_not_stop_the_sweep():
    manager = _manager()

    class Exploding(FakeTransport):
        async def terminate(self) -> None:
            raise RuntimeError("boom")

    manager._server_instances["bad"] = Exploding()
    good = FakeTransport()
    manager._server_instances["good"] = good

    await manager.sweep_once(now=0.0)
    reaped = await manager.sweep_once(now=ABANDONED_AFTER_SECONDS)

    assert good.terminated == 1
    assert "good" in reaped
    assert "bad" not in manager._server_instances, "dropped from the table even so"


def test_install_is_what_fastmcp_constructs():
    """The pin on the seam: FastMCP builds the manager by THIS module attribute."""
    import inspect

    from fastmcp.server import http

    session_hygiene.install()

    assert http.StreamableHTTPSessionManager is HygienicSessionManager
    assert "StreamableHTTPSessionManager(" in inspect.getsource(
        http.create_streamable_http_app
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _abandon_one(url: str) -> str:
    """A liveness-probe-shaped session whose DELETE never happens."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 200, resp.text
        return resp.headers["mcp-session-id"]


@pytest.mark.asyncio
async def test_end_to_end_abandoned_probes_are_reaped_and_the_live_client_is_not(
    monkeypatch,
):
    """Real streamable HTTP, in-process: five abandoned probe sessions vanish
    within the window, the connected client keeps working, and a reaped id
    answers 404 exactly as a DELETE'd one would."""
    import uvicorn

    monkeypatch.setattr(session_hygiene, "ABANDONED_AFTER_SECONDS", 0.5)
    monkeypatch.setattr(session_hygiene, "SWEEP_INTERVAL_SECONDS", 0.1)
    session_hygiene.install()

    tiny = FastMCP("hygiene-e2e")

    @tiny.tool
    def echo(text: str) -> str:
        return text

    port = _free_port()
    app = tiny.http_app(path="/mcp/")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    serve = asyncio.create_task(server.serve())
    url = f"http://127.0.0.1:{port}/mcp/"
    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.1)
        assert server.started

        async with Client(StreamableHttpTransport(url)) as client:
            assert (await client.call_tool("echo", {"text": "hi"})).data == "hi"
            orphans = [await _abandon_one(url) for _ in range(5)]
            manager = session_hygiene.active_manager()
            assert manager is not None, "FastMCP did not build OUR manager"
            assert len(manager._server_instances) == 6

            await asyncio.sleep(1.5)  # > window + sweep interval

            assert len(manager._server_instances) == 1
            assert (
                await client.call_tool("echo", {"text": "still here"})
            ).data == "still here"

            async with httpx.AsyncClient(timeout=10) as raw:
                resp = await raw.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "mcp-session-id": orphans[0],
                    },
                )
            assert resp.status_code == 404, (resp.status_code, resp.text)
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(serve, timeout=10)
