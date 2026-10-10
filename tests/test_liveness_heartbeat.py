"""Pins for F-960: the watchdog heartbeat stops minting an MCP session and a TCP
connection per beat.

Every stdio proxy's watchdog asks the backend "are you alive" every ~2 s. It used
to ask with a real ``initialize`` over a NEW ``httpx.Client`` each time: one new
TCP connection (left in TIME_WAIT) and one new MCP session, ServerSession and
task group on the backend per beat. The SDK's DELETE only marks the transport
terminated, so the entry stayed listed until the 300 s sweep. Measured on the
owner's machine with ~38 proxies attached: ~5,600 sessions listed, ~560 reaped
per sweep, ~2,200 sockets in TIME_WAIT.

The fix has three parts, each pinned here over REAL streamable HTTP (uvicorn on a
free 127.0.0.1 port, a tiny FastMCP app, nothing of the live backend):

* the backend serves ``HEALTH_PATH``, which answers 200 only while the session
  manager is running and creates no session;
* ``backend_probe.Heartbeat`` asks it over ONE keep-alive connection, and falls
  back to the ``initialize`` probe against a backend that predates the route;
* a DELETE drops the session from the manager's table at once, for proxies that
  are still running the old probe.

The measurement is taken on the server side — ``len(_server_instances)`` for
sessions listed, and a counter on uvicorn's ``connection_made`` for TCP
connections — so it sees what a backend sees, not what a client believes.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport

from stealth_chrome_devtools_mcp.embedded import (
    backend_probe,
    session_hygiene,
    singleton,
)
from stealth_chrome_devtools_mcp.embedded.backend_probe import (
    HEALTH_PATH,
    MCP_PATH,
    Heartbeat,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BEATS = 20
TIMEOUT = 5.0


@dataclass
class Backend:
    """What a test can see of the in-process backend it is probing."""

    port: int
    connections: list[int]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{MCP_PATH}"

    @property
    def sessions(self) -> int:
        manager = session_hygiene.active_manager()
        assert manager is not None, "FastMCP did not build OUR manager"
        return len(manager._server_instances)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def _serving(monkeypatch, *, health_route: bool = True) -> AsyncIterator[Backend]:
    """A tiny FastMCP app on a free port, counting the TCP connections it accepts."""
    import uvicorn
    from sse_starlette.sse import AppStatus
    from uvicorn.protocols.http.h11_impl import H11Protocol

    # Stopping an in-process uvicorn sets sse_starlette's PROCESS-GLOBAL
    # ``should_exit``; left set, every later SSE reply in this process dies.
    monkeypatch.setattr(AppStatus, "should_exit", False)
    monkeypatch.setattr(session_hygiene, "_managers", [])

    connections: list[int] = []
    accepted = H11Protocol.connection_made

    def counting(self, transport):
        connections.append(1)
        return accepted(self, transport)

    monkeypatch.setattr(H11Protocol, "connection_made", counting)

    tiny = FastMCP("heartbeat-e2e")

    @tiny.tool
    def echo(text: str) -> str:
        return text

    session_hygiene.install(tiny if health_route else None)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            tiny.http_app(path=MCP_PATH),
            host="127.0.0.1",
            port=port,
            log_level="error",
            http="h11",
        )
    )
    serve = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.1)
        assert server.started
        yield Backend(port, connections)
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(serve, timeout=10)


async def _beats(probe, n: int = BEATS) -> list[bool]:
    """``n`` beats, each off-thread exactly as the watchdog drives them."""
    return [await asyncio.to_thread(probe, TIMEOUT) for _ in range(n)]


async def _initialize(url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        return await client.post(
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


# --- the heartbeat: no session, one connection -------------------------------


async def test_n_heartbeats_list_no_session_and_use_one_connection(monkeypatch):
    async with _serving(monkeypatch) as backend:
        beat = Heartbeat(backend.url)
        try:
            assert await _beats(beat.alive) == [True] * BEATS
        finally:
            beat.close()

        assert backend.sessions == 0, "a heartbeat must not mint an MCP session"
        assert len(backend.connections) == 1, "ONE keep-alive connection"


async def test_the_initialize_probe_still_pays_a_connection_per_ask(monkeypatch):
    """The control: the same measurement DOES see the old cost, so the
    one-connection assertion above is not vacuous."""
    async with _serving(monkeypatch) as backend:
        results = await _beats(lambda t: backend_probe.ready(backend.url, t))

        assert results == [True] * BEATS
        assert len(backend.connections) == BEATS


async def test_the_measurement_sees_listed_sessions_when_the_delete_is_not_honoured(
    monkeypatch,
):
    """The control for the session count, on the pre-F-960 behaviour (a DELETE
    that leaves the entry listed): the old probe leaks one session per ask."""
    monkeypatch.setattr(
        session_hygiene.HygienicSessionManager,
        "_forget_if_deleted",
        lambda self, session_id: None,
    )
    async with _serving(monkeypatch) as backend:
        await _beats(lambda t: backend_probe.ready(backend.url, t))

        assert backend.sessions == BEATS


async def test_the_default_watchdog_beats_without_leaking(monkeypatch, tmp_path):
    """The wiring, end to end: ``singleton._watch_backend_liveness`` with its
    DEFAULT check, N ticks against a live backend."""
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    closed: list[bool] = []
    real_close = Heartbeat.close
    monkeypatch.setattr(
        Heartbeat, "close", lambda self: (closed.append(True), real_close(self))
    )

    class StopWatchingError(Exception):
        pass

    ticks = 0

    async def tick(_interval: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > BEATS:
            raise StopWatchingError
        await anyio.lowlevel.checkpoint()

    async with _serving(monkeypatch) as backend:
        with pytest.raises(StopWatchingError):
            await singleton._watch_backend_liveness(
                backend.port,
                interval=0.0,
                failures_before_teardown=3,
                sleep=tick,
                confirm_probe=lambda: True,
            )

        assert ticks == BEATS + 1, "every tick before the stop was a healthy beat"
        assert backend.sessions == 0
        assert len(backend.connections) == 1
        assert closed == [True], "the watchdog's connection is closed when it ends"


# --- the route ---------------------------------------------------------------


async def test_the_route_is_200_while_the_manager_runs_and_503_otherwise(monkeypatch):
    monkeypatch.setattr(session_hygiene, "_managers", [])
    tiny = FastMCP("route-only")
    session_hygiene.install(tiny)
    app = tiny.http_app(path=MCP_PATH)

    async def ask() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get(HEALTH_PATH)

    assert (await ask()).status_code == 503, "lifespan has not started the manager"
    async with app.lifespan(app):
        assert (await ask()).status_code == 200
    assert (await ask()).status_code == 503, "the manager was stopped"


async def test_the_route_creates_no_session_and_cannot_collide_with_mcp(monkeypatch):
    assert not HEALTH_PATH.startswith(MCP_PATH.rstrip("/") + "/")
    assert HEALTH_PATH != MCP_PATH
    async with _serving(monkeypatch) as backend:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(f"http://127.0.0.1:{backend.port}{HEALTH_PATH}")
        assert resp.status_code == 200
        assert "mcp-session-id" not in resp.headers
        assert backend.sessions == 0


def test_install_registers_the_route_once_per_server():
    tiny = FastMCP("once")
    session_hygiene.install(tiny)
    session_hygiene.install(tiny)

    assert [getattr(r, "path", None) for r in tiny._additional_http_routes] == [
        HEALTH_PATH
    ]


def test_a_wedged_backend_that_accepts_but_never_answers_is_not_alive():
    """F-301/F-501's reason for an app-level probe: a socket that connects and
    then says nothing must read as dead, and the next beat must start clean."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        beat = Heartbeat(f"http://127.0.0.1:{listener.getsockname()[1]}{MCP_PATH}")
        try:
            assert beat.alive(0.5) is False
            assert beat._client is None, "a failed beat discards its connection"
        finally:
            beat.close()


def test_a_closed_port_is_not_alive():
    beat = Heartbeat(f"http://127.0.0.1:{_free_port()}{MCP_PATH}")
    assert beat.alive(0.5) is False


# --- a newer proxy, an older backend ------------------------------------------


async def test_a_backend_without_the_route_is_still_probed_by_initialize(monkeypatch):
    async with _serving(monkeypatch, health_route=False) as backend:
        beat = Heartbeat(backend.url)
        try:
            assert await _beats(beat.alive, 5) == [True] * 5
            assert beat._legacy is True, "the 404 switched it to the initialize probe"
        finally:
            beat.close()
        assert backend.sessions == 0, "and the DELETE keeps even that path clean"


async def test_a_legacy_backend_that_dies_reads_dead_and_retries_the_route(monkeypatch):
    async with _serving(monkeypatch, health_route=False) as backend:
        beat = Heartbeat(backend.url)
        assert await asyncio.to_thread(beat.alive, TIMEOUT) is True
        assert beat._legacy is True
        port = backend.port
    # The backend is gone; a closed port fails the fallback probe.
    gone = Heartbeat(f"http://127.0.0.1:{port}{MCP_PATH}")
    gone._legacy = True
    assert gone.alive(0.5) is False
    assert gone._legacy is False, "re-test the route next beat: it may be a new backend"


# --- defence in depth: an old proxy's DELETE ----------------------------------


async def test_a_delete_unlists_the_session_at_once_and_the_id_still_answers_404(
    monkeypatch,
):
    async with _serving(monkeypatch) as backend:
        async with Client(StreamableHttpTransport(backend.url)) as live:
            assert (await live.call_tool("echo", {"text": "hi"})).data == "hi"
            before = backend.sessions

            resp = await _initialize(backend.url)
            assert resp.status_code == 200
            session_id = resp.headers["mcp-session-id"]
            assert backend.sessions == before + 1

            async with httpx.AsyncClient(timeout=TIMEOUT) as raw:
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "mcp-session-id": session_id,
                }
                deleted = await raw.delete(backend.url, headers=headers)
                assert deleted.status_code == 200
                assert backend.sessions == before, (
                    "listed until the sweep, before F-960"
                )

                again = await raw.post(
                    backend.url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
                    headers=headers,
                )
                assert again.status_code == 404

            assert (await live.call_tool("echo", {"text": "still"})).data == "still"
        assert backend.sessions == 0, "the SDK client's own DELETE on exit unlists too"


async def test_a_delete_the_transport_refuses_does_not_unlist(monkeypatch):
    """Only a session that WAS terminated is forgotten: a DELETE the transport
    rejects (an unsupported protocol version) leaves the live session alone."""
    async with _serving(monkeypatch) as backend:
        resp = await _initialize(backend.url)
        session_id = resp.headers["mcp-session-id"]

        async with httpx.AsyncClient(timeout=TIMEOUT) as raw:
            refused = await raw.delete(
                backend.url,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "mcp-session-id": session_id,
                    "mcp-protocol-version": "1999-01-01",
                },
            )
        assert refused.status_code == 400
        assert backend.sessions == 1


def test_forgetting_a_deleted_session_clears_every_table():
    manager = session_hygiene.HygienicSessionManager(app=object())  # type: ignore[arg-type]

    class Terminated:
        is_terminated = True

    manager._server_instances["s"] = Terminated()  # type: ignore[assignment]
    manager._session_owners["s"] = object()  # type: ignore[assignment]
    manager.last_seen["s"] = 1.0

    manager._forget_if_deleted("s")

    assert not manager._server_instances
    assert not manager._session_owners
    assert not manager.last_seen


def test_health_url_swaps_only_the_path():
    assert (
        backend_probe.health_url("http://127.0.0.1:4242/mcp/?x=1")
        == f"http://127.0.0.1:4242{HEALTH_PATH}"
    )


def test_heartbeat_is_thread_safe_to_construct_concurrently():
    beat = Heartbeat(f"http://127.0.0.1:{_free_port()}{MCP_PATH}")
    seen: list[object] = []
    threads = [
        threading.Thread(target=lambda: seen.append(beat._connection()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert len({id(c) for c in seen}) == 1, "one client however many threads ask"
    finally:
        beat.close()
