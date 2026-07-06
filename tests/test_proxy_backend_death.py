"""The stdio proxy must not hang forever when the backend dies mid-session.

The proxy already tears down when the *client* disconnects. But there was no
symmetric handling for the *backend* dying: `from_backend`'s stream closed while
`to_backend` stayed parked, `run_backend` never returned, and `pump_client` kept
buffering client requests that could never be answered. A tool call issued after
the backend died therefore never got a response — the MCP client blocked with no
timeout (an unbounded loop for the AI driving it).

The fix arms a bounded backend-liveness monitor once the backend is confirmed up;
when the backend it proxies to vanishes, the proxy tears down so the client sees
a clean disconnect and reconnects (respawning a fresh backend) instead of hanging.

`TestWatchBackendLiveness` unit-tests the monitor's decision logic (fast, no
backend). `TestProxyExitsOnBackendDeath` reproduces the real hang end-to-end.
"""

import socket

import anyio
import anyio.lowlevel
import pytest
import singleton


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _init_msg(req_id):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    req = JSONRPCRequest(
        jsonrpc="2.0",
        id=req_id,
        method="initialize",
        params={
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "backend-death-test", "version": "1"},
        },
    )
    return SessionMessage(message=JSONRPCMessage(req))


def _initialized_note():
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

    note = JSONRPCNotification(
        jsonrpc="2.0", method="notifications/initialized", params={}
    )
    return SessionMessage(message=JSONRPCMessage(note))


def _tools_list_msg(req_id):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    req = JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/list", params={})
    return SessionMessage(message=JSONRPCMessage(req))


class TestWatchBackendLiveness:
    @pytest.mark.asyncio
    async def test_returns_after_consecutive_unhealthy_checks(self):
        calls = {"n": 0}

        def health():
            calls["n"] += 1
            return False  # backend is gone on every check

        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        with anyio.fail_after(2):
            await singleton._watch_backend_liveness(
                port=1,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=health,
                sleep=tiny_sleep,
            )

        assert calls["n"] == 3  # tore down after exactly 3 consecutive failures

    @pytest.mark.asyncio
    async def test_resets_counter_on_recovery(self):
        # A single healthy check between failures must reset the counter, so a
        # transient blip never tears down a still-live backend.
        results = [False, False, True, False, False, False]
        idx = {"i": 0}

        def health():
            v = results[idx["i"]]
            idx["i"] += 1
            return v

        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        with anyio.fail_after(2):
            await singleton._watch_backend_liveness(
                port=1,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=health,
                sleep=tiny_sleep,
            )

        assert idx["i"] == 6  # needed all 6 checks: the True reset the run of failures

    @pytest.mark.asyncio
    async def test_does_not_return_while_healthy(self):
        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        done = anyio.Event()

        async with anyio.create_task_group() as tg:

            async def run():
                await singleton._watch_backend_liveness(
                    port=1,
                    interval=0.0,
                    failures_before_teardown=3,
                    is_healthy=lambda: True,
                    sleep=tiny_sleep,
                )
                done.set()

            tg.start_soon(run)
            await anyio.sleep(0.1)
            assert not done.is_set(), "monitor tore down a healthy backend"
            tg.cancel_scope.cancel()


@pytest.mark.integration
class TestProxyExitsOnBackendDeath:
    """End-to-end reproduction: the proxy MUST tear down (not hang) when the
    backend dies mid-session. On the un-fixed code the proxy parks forever on the
    dead backend and this times out — the exact unbounded hang."""

    @pytest.mark.asyncio
    async def test_proxy_returns_when_backend_dies_midsession(self, tmp_path):
        import os
        import subprocess
        import sys

        from singleton import _proxy_streams

        port = _free_port()
        env = dict(os.environ)
        env["STEALTH_MCP_BROWSER_SESSION_ROOT"] = str(tmp_path / "sessions")
        env["STEALTH_BROWSER_DEBUG"] = "false"

        backend = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "stealth_chrome_devtools_mcp",
                "--transport",
                "http",
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        try:
            c2p_tx, c2p_rx = anyio.create_memory_object_stream(50)
            p2c_tx, p2c_rx = anyio.create_memory_object_stream(50)
            proxy_returned = anyio.Event()

            async with anyio.create_task_group() as tg:

                async def run_proxy():
                    await _proxy_streams(c2p_rx, p2c_tx, port)
                    proxy_returned.set()

                tg.start_soon(run_proxy)

                # Real handshake so the backend session is genuinely live and the
                # proxy is fully wired to it.
                await c2p_tx.send(_init_msg(1))
                await c2p_tx.send(_initialized_note())
                await c2p_tx.send(_tools_list_msg(2))
                with anyio.fail_after(90):
                    await p2c_rx.receive()  # local initialize answer
                    await p2c_rx.receive()  # tools/list from the real backend

                # The backend dies mid-session.
                backend.kill()
                backend.wait(timeout=10)

                # A request now can never be answered by the dead backend. The
                # proxy must notice the backend is gone and return within a bounded
                # time — on the old code it parks forever and this times out.
                await c2p_tx.send(_tools_list_msg(3))

                with anyio.fail_after(30):
                    await proxy_returned.wait()

                tg.cancel_scope.cancel()
        finally:
            if backend.poll() is None:
                backend.kill()
                try:
                    backend.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
