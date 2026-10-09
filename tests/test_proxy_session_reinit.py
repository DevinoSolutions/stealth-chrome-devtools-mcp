"""F-959: after a heal, or after any 404 from the backend, the stdio proxy opens a
fresh backend session and resends the refused call once.

**The defect (2026-10-09).** Proxy 150780 logged "backend healed: re-bridging to
port 34654" at 17:22:08, and every stealth call after that answered "Session
terminated". F-838's heal replayed the client's ``initialize`` and nothing after
it. The SDK client opens a session's standing GET event stream only when it
SENDS ``notifications/initialized``. So the healed session had no event stream,
and F-862's sweep reaps a session with no stream that has been quiet for
``ABANDONED_AFTER_SECONDS`` (300 s; the backend's log started reaping at
17:27:32). From then on each call got a 404. The SDK turns that into
``{"code": 32600, "message": "Session terminated"}``, and nothing re-initialized.

Two changes, each pinned here:

* the heal replays ``notifications/initialized`` too, so a healed session is
  one the sweep spares (``TestAHealedSessionIsNotReaped``);
* a 404 on a call ends the generation, a fresh session is opened on the backend
  that answered, and the refused call is resent ONCE. A 404 proves the backend
  never ran the call, so resending is safe even for a non-idempotent one
  (``TestAKilledBackendCostsNoCall``, ``TestAReapedSessionCostsNoCall``).

Real transport, in-process: the SDK client and server are real, and so are
FastMCP's HTTP app, our session-hygiene manager and uvicorn on an OS-assigned
loopback port. The pattern is ``test_session_hygiene.py``'s end-to-end node. Only
the proxy's knowledge of the outside world is ours: which port a heal finds, and
when the watchdog condemns. No real backend, port or ``~/.stealth-mcp`` record is
touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

import anyio
import pytest
from fastmcp import FastMCP
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from stealth_chrome_devtools_mcp.embedded import (
    backend_probe,
    proxy_selfheal,
    session_hygiene,
    singleton,
)

# Harness bounds, never product deadlines.
STEP_BOUND = 30.0
# Longer than the shrunken reap window plus a sweep, so a session the sweep
# would reap is reaped before the next call.
QUIET_FOR = 1.5
REINIT_LINE = "no longer knows this session"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _frame(payload: dict) -> SessionMessage:
    return SessionMessage(message=JSONRPCMessage.model_validate(payload))


def _initialize(req_id: int) -> SessionMessage:
    return _frame(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "f959", "version": "1"},
            },
        }
    )


def _initialized() -> SessionMessage:
    return _frame({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _echo(req_id: int, text: str) -> SessionMessage:
    return _frame(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": text}},
        }
    )


class _Backend:
    """One real streamable-HTTP MCP backend on a loopback port."""

    def __init__(self, port: int) -> None:
        import uvicorn

        tiny = FastMCP("f959-backend")

        @tiny.tool
        def echo(text: str) -> str:
            return text

        self.port = port
        self.server = uvicorn.Server(
            uvicorn.Config(
                tiny.http_app(path=backend_probe.MCP_PATH),
                host="127.0.0.1",
                port=port,
                log_level="error",
                timeout_graceful_shutdown=1,
            )
        )
        self._serve: asyncio.Task | None = None
        self.manager = None

    async def start(self) -> _Backend:
        self._serve = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started:
                break
            await asyncio.sleep(0.05)
        assert self.server.started, f"backend on {self.port} never started"
        self.manager = session_hygiene.active_manager()
        return self

    async def kill(self) -> None:
        """Stop serving. Open event streams get the one-second grace
        ``timeout_graceful_shutdown`` allows and are then cut, and the app's
        lifespan ends every session it held."""
        from sse_starlette.sse import AppStatus

        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self._serve, timeout=10)
        # sse_starlette copies a stopping uvicorn's ``should_exit`` into its
        # PROCESS-GLOBAL ``AppStatus``, after which every SSE response in the
        # process ends at once, including the next backend's. One backend per
        # process never meets this; two in one process do, so it is reset.
        AppStatus.should_exit = False

    def sessions(self) -> int:
        return len(self.manager._server_instances)


class _Client:
    """The MCP client's end of the proxy's stdio, as memory streams."""

    def __init__(self) -> None:
        self.c2p_tx, self.c2p_rx = anyio.create_memory_object_stream(50)
        self.p2c_tx, self.p2c_rx = anyio.create_memory_object_stream(50)

    async def send(self, msg: SessionMessage) -> None:
        await self.c2p_tx.send(msg)

    async def reply_to(self, req_id: int) -> dict:
        with anyio.fail_after(STEP_BOUND):
            while True:
                msg = await self.p2c_rx.receive()
                inner = msg.message.root
                if getattr(inner, "id", None) == req_id:
                    return inner.model_dump(by_alias=True, exclude_none=True)

    async def echo(self, req_id: int, text: str) -> dict:
        await self.send(_echo(req_id, text))
        return await self.reply_to(req_id)


def _text(reply: dict) -> str:
    assert "result" in reply, f"the call was not served: {reply}"
    return reply["result"]["content"][0]["text"]


@pytest.fixture()
def world(monkeypatch, caplog):
    """The proxy's outside world: a hygiene window short enough to watch, the
    port a heal finds, and a watchdog that condemns only when told to."""
    from sse_starlette.sse import AppStatus

    # An earlier node that stopped its own in-process uvicorn may have left
    # this set (see ``_Backend.kill``), and then no SSE reply would arrive.
    monkeypatch.setattr(AppStatus, "should_exit", False)
    monkeypatch.setattr(session_hygiene, "ABANDONED_AFTER_SECONDS", 0.5)
    monkeypatch.setattr(session_hygiene, "SWEEP_INTERVAL_SECONDS", 0.1)
    session_hygiene.install()

    state = {"heal_to": None, "condemn": anyio.Event()}

    def ensure_running(_preferred):
        return state["heal_to"]

    async def ready(_url, *_a, **_kw):
        return True

    async def watch(_port, **_kw):
        await state["condemn"].wait()
        state["condemn"] = anyio.Event()  # the replacement is condemned only on cue

    monkeypatch.setattr(singleton, "ensure_server_running", ensure_running)
    monkeypatch.setattr(singleton, "_await_backend_http", ready)
    monkeypatch.setattr(singleton, "_watch_backend_liveness", watch)
    monkeypatch.setattr(singleton, "_same_identity_backend_ready", lambda _p: False)
    caplog.set_level(logging.WARNING, logger="stealth.proxy")
    return state


async def _open(client: _Client, port: int, tg) -> None:
    tg.start_soon(singleton._proxy_streams, client.c2p_rx, client.p2c_tx, port)
    await client.send(_initialize(0))
    await client.reply_to(0)  # answered locally
    await client.send(_initialized())


async def _await_log(caplog, text: str) -> None:
    with anyio.fail_after(STEP_BOUND):
        while not any(text in r.getMessage() for r in caplog.records):
            await anyio.sleep(0.05)


def _reinits(caplog) -> int:
    return sum(REINIT_LINE in r.getMessage() for r in caplog.records)


class TestAHealedSessionIsNotReaped:
    async def test_a_session_healed_onto_another_backend_survives_the_sweep(
        self, world, caplog
    ):
        """The owner's incident: the backend dies, the proxy heals onto
        another one, and the chat goes quiet for longer than the reap window."""
        first = await _Backend(_free_port()).start()
        second = None
        client = _Client()
        try:
            async with anyio.create_task_group() as tg:
                await _open(client, first.port, tg)
                assert _text(await client.echo(1, "before")) == "before"

                await first.kill()
                second = await _Backend(_free_port()).start()
                world["heal_to"] = second.port
                world["condemn"].set()
                # Sent after the re-bridge, so generation 1 cannot pick it up.
                await _await_log(caplog, "backend healed")

                assert _text(await client.echo(2, "healed")) == "healed"
                await anyio.sleep(QUIET_FOR)

                assert second.sessions() == 1, (
                    "the healed session was reaped: it has no event stream, so "
                    "notifications/initialized was not replayed"
                )
                assert _text(await client.echo(3, "after quiet")) == "after quiet"
                assert _reinits(caplog) == 0, (
                    "the call after the quiet spell needed a re-initialize; the "
                    "healed session should have survived on its own"
                )
                tg.cancel_scope.cancel()
        finally:
            for backend in (first, second):
                if backend is not None:
                    await backend.kill()


class TestAKilledBackendCostsNoCall:
    async def test_a_backend_killed_mid_session_and_replaced_on_its_port(
        self, world, caplog
    ):
        """The backend is killed and a new one takes its port before the
        watchdog has a verdict. The next call reaches a backend that has never
        heard of the session and gets a 404. The proxy opens a new session and
        resends the call, so the client gets the result."""
        port = _free_port()
        backend = await _Backend(port).start()
        client = _Client()
        try:
            async with anyio.create_task_group() as tg:
                await _open(client, port, tg)
                assert _text(await client.echo(1, "before")) == "before"

                await backend.kill()
                backend = await _Backend(port).start()
                world["heal_to"] = port

                assert _text(await client.echo(2, "after kill")) == "after kill"
                assert _reinits(caplog) == 1
                # ...and the fresh session is a whole one: it survives the sweep.
                await anyio.sleep(QUIET_FOR)
                assert _text(await client.echo(3, "still")) == "still"
                assert _reinits(caplog) == 1
                tg.cancel_scope.cancel()
        finally:
            await backend.kill()


class TestAReapedSessionCostsNoCall:
    async def test_a_session_the_backend_terminated_is_reopened(self, world, caplog):
        """The backend is alive but has dropped the session (the sweep, or a
        DELETE). Two calls in a row each come back with a result, not "Session
        terminated"."""
        backend = await _Backend(_free_port()).start()
        client = _Client()
        world["heal_to"] = backend.port
        try:
            async with anyio.create_task_group() as tg:
                await _open(client, backend.port, tg)
                assert _text(await client.echo(1, "before")) == "before"

                for transport in list(backend.manager._server_instances.values()):
                    await transport.terminate()

                assert _text(await client.echo(2, "reopened")) == "reopened"
                assert _text(await client.echo(3, "again")) == "again"
                assert _reinits(caplog) == 1
                tg.cancel_scope.cancel()
        finally:
            await backend.kill()


def _terminated(req_id):
    from mcp.types import ErrorData, JSONRPCError

    return JSONRPCError(
        jsonrpc="2.0",
        id=req_id,
        error=ErrorData(
            code=proxy_selfheal.SESSION_TERMINATED_CODE,
            message=proxy_selfheal.SESSION_TERMINATED_MESSAGE,
        ),
    )


class TestResendOnce:
    """The bookkeeping under the resend, without a transport."""

    def test_a_refused_call_is_held_once_and_then_handed_to_the_client(self):
        pending = proxy_selfheal.PendingCalls()
        call = _echo(7, "x")
        pending.track(call.message.root, call)

        assert pending.session_lost(_terminated(7))
        assert pending.idle() and pending.session_was_lost
        assert pending.take_retries() == [call]
        assert pending.take_retries() == []

        pending.track(call.message.root, call)  # resent on the fresh session
        assert not pending.session_lost(_terminated(7)), "resent more than once"

    def test_only_the_sdks_exact_answer_counts(self):
        from mcp.types import ErrorData, JSONRPCError

        pending = proxy_selfheal.PendingCalls()
        call = _echo(8, "x")
        pending.track(call.message.root, call)
        for code, message in [
            (-32600, proxy_selfheal.SESSION_TERMINATED_MESSAGE),
            (proxy_selfheal.SESSION_TERMINATED_CODE, "Session terminated by tool"),
        ]:
            other = JSONRPCError(
                jsonrpc="2.0", id=8, error=ErrorData(code=code, message=message)
            )
            assert not pending.session_lost(other)
        assert not pending.idle()

    def test_a_call_answered_after_its_resend_can_be_resent_again_later(self):
        pending = proxy_selfheal.PendingCalls()
        call = _echo(9, "x")
        pending.track(call.message.root, call)
        assert pending.session_lost(_terminated(9))
        pending.track(call.message.root, call)
        pending.settle(call.message.root)  # answered: the id is free again
        pending.track(call.message.root, call)
        assert pending.session_lost(_terminated(9))
