"""The stdio proxy must answer `initialize` locally and instantly.

This is the fix for the recurring "MCP server disconnected / connection timed
out after 30000ms" failure: under load (or a cold file cache) the backend's
heavy import can exceed Claude Code's 30s connection timeout. By answering the
`initialize` handshake in the proxy — without waiting for the backend — the
connection is always established immediately, and only later requests
(tools/list, tool calls) wait for the backend to finish starting.

These are pure in-memory tests: no browser, no backend, no real stdio.
"""

import socket

import anyio
import pytest

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCResponse

from singleton import _proxy_streams


def _free_port() -> int:
    """A port with nothing listening — so the proxy's backend never answers."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _initialize_msg(req_id, protocol_version=None):
    params = {"capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    if protocol_version is not None:
        params["protocolVersion"] = protocol_version
    req = JSONRPCRequest(jsonrpc="2.0", id=req_id, method="initialize", params=params)
    return SessionMessage(message=JSONRPCMessage(req))


async def _drive_initialize(protocol_version):
    """Run the proxy against a dead port, send initialize, return its response."""
    client_to_proxy_tx, client_to_proxy_rx = anyio.create_memory_object_stream(10)
    proxy_to_client_tx, proxy_to_client_rx = anyio.create_memory_object_stream(10)
    port = _free_port()  # nothing is listening here

    async with anyio.create_task_group() as tg:
        tg.start_soon(_proxy_streams, client_to_proxy_rx, proxy_to_client_tx, port)

        await client_to_proxy_tx.send(_initialize_msg(1, protocol_version))

        # The whole point: a reply arrives fast despite no backend being up.
        with anyio.fail_after(5):
            reply = await proxy_to_client_rx.receive()

        tg.cancel_scope.cancel()  # stop the background backend-retry loop

    return reply.message.root


class TestFastHandshake:
    @pytest.mark.asyncio
    async def test_initialize_answered_without_backend(self):
        inner = await _drive_initialize("2025-03-26")

        assert isinstance(inner, JSONRPCResponse)
        assert inner.id == 1
        # echoes the client's requested protocol version
        assert inner.result["protocolVersion"] == "2025-03-26"
        # advertises tools (the real list is fetched later from the backend)
        assert "tools" in inner.result["capabilities"]
        assert inner.result["serverInfo"]["name"] == "stealth-chrome-devtools-mcp"

    @pytest.mark.asyncio
    async def test_initialize_falls_back_to_default_version(self):
        from mcp.types import DEFAULT_NEGOTIATED_VERSION

        inner = await _drive_initialize(None)  # client omits protocolVersion

        assert isinstance(inner, JSONRPCResponse)
        assert inner.result["protocolVersion"] == DEFAULT_NEGOTIATED_VERSION


class TestEnsureServerRunningNonBlocking:
    """ensure_server_running must NOT block on the backend cold start — that
    blocking wait was what let Claude Code's 30s connection timeout fire."""

    def test_returns_existing_without_starting(self):
        import singleton
        from unittest.mock import patch

        with patch.object(singleton, "_find_running_server", return_value=12345):
            with patch.object(singleton.threading, "Thread") as thread_cls:
                port = singleton.ensure_server_running(port=19222)

        assert port == 12345
        thread_cls.assert_not_called()  # already up → no startup thread

    def test_does_not_block_on_cold_start(self):
        import time
        import singleton
        from unittest.mock import patch

        # Simulate a backend that takes "forever" to come up. The old code
        # blocked here for up to 30s; the new code must return immediately.
        def slow_start(port):
            time.sleep(5)

        with patch.object(singleton, "_find_running_server", return_value=None):
            with patch.object(singleton, "_start_backend_holding_lock", slow_start):
                t0 = time.monotonic()
                port = singleton.ensure_server_running(port=19222)
                elapsed = time.monotonic() - t0

        assert port == 19222
        assert elapsed < 0.5, f"ensure_server_running blocked {elapsed:.2f}s on startup"


def _initialized_notification():
    from mcp.types import JSONRPCNotification

    note = JSONRPCNotification(
        jsonrpc="2.0", method="notifications/initialized", params={}
    )
    return SessionMessage(message=JSONRPCMessage(note))


def _tools_list_msg(req_id):
    req = JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/list", params={})
    return SessionMessage(message=JSONRPCMessage(req))


@pytest.mark.integration
class TestFastHandshakeEndToEnd:
    """End-to-end against a real backend: local initialize, then a forwarded
    tools/list that returns the real tool catalog — and the backend's duplicate
    initialize response must be swallowed (client sees exactly one)."""

    @pytest.mark.asyncio
    async def test_initialize_local_then_tools_list_forwarded(self, tmp_path):
        import os
        import subprocess
        import sys

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

            replies = []
            async with anyio.create_task_group() as tg:
                tg.start_soon(_proxy_streams, c2p_rx, p2c_tx, port)

                # Full client handshake + first real request.
                await c2p_tx.send(_initialize_msg(1, "2025-03-26"))
                await c2p_tx.send(_initialized_notification())
                await c2p_tx.send(_tools_list_msg(2))

                # initialize answers locally & instantly (no backend wait).
                with anyio.fail_after(5):
                    first = await p2c_rx.receive()
                replies.append(first.message.root)

                # tools/list waits for the backend cold start, then returns.
                with anyio.fail_after(90):
                    second = await p2c_rx.receive()
                replies.append(second.message.root)

                tg.cancel_scope.cancel()

            init_reply, tools_reply = replies
            # exactly one initialize response (id=1) — backend's dup swallowed
            assert isinstance(init_reply, JSONRPCResponse)
            assert init_reply.id == 1
            assert (
                init_reply.result["serverInfo"]["name"] == "stealth-chrome-devtools-mcp"
            )
            # tools/list (id=2) came from the real backend with real tools
            assert isinstance(tools_reply, JSONRPCResponse)
            assert tools_reply.id == 2
            assert len(tools_reply.result["tools"]) > 0
            tool_names = {t["name"] for t in tools_reply.result["tools"]}
            assert "spawn_browser" in tool_names
        finally:
            backend.terminate()
            try:
                backend.wait(timeout=10)
            except subprocess.TimeoutExpired:
                backend.kill()


@pytest.mark.integration
class TestProxyExitsOnClientDisconnect:
    """The stdio proxy MUST exit when the client (Claude Code) disconnects.

    The leak that made the server unusable: when stdin hits EOF, ``pump_client``
    ends but ``run_backend``'s ``from_backend`` loop keeps reading the still-open
    backend stream forever, so the proxy process never returns. Over days of
    Claude Code restarts/reconnects these pile up (we measured 250 leaked stealth
    python processes on one machine) until handles/memory are exhausted and a
    fresh spawn wedges for hours. Closing the client stream must tear the proxy
    down and return promptly.
    """

    @pytest.mark.asyncio
    async def test_proxy_returns_when_client_stream_closes(self, tmp_path):
        import os
        import subprocess
        import sys

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

                # A real handshake so the backend stream is genuinely live and
                # from_backend is parked on it (the exact leak condition).
                await c2p_tx.send(_initialize_msg(1, "2025-03-26"))
                await c2p_tx.send(_initialized_notification())
                await c2p_tx.send(_tools_list_msg(2))
                with anyio.fail_after(90):
                    await p2c_rx.receive()  # local initialize answer
                    await p2c_rx.receive()  # tools/list from the real backend

                # The client (Claude Code) disconnects: stdin closes -> EOF.
                await c2p_tx.aclose()

                # The proxy must notice the dead client and return. On the old
                # code it parks on the live backend stream forever and this times
                # out — that is the leaked process.
                with anyio.fail_after(10):
                    await proxy_returned.wait()

                tg.cancel_scope.cancel()
        finally:
            backend.terminate()
            try:
                backend.wait(timeout=10)
            except subprocess.TimeoutExpired:
                backend.kill()


@pytest.mark.integration
class TestEntrypointExitsOnDisconnect:
    """The REAL entrypoint process must exit when the client disconnects.

    This is the end-to-end guard the isolated ``_proxy_streams`` test above could
    not provide. The leak had TWO causes: ``_proxy_streams`` not tearing down the
    backend side, AND mcp's ``stdio_server`` holding its teardown open until the
    write stream is closed. The unit test only covered the first and passed while
    the real entrypoint still hung. This runs ``python -m
    stealth_chrome_devtools_mcp``, does the handshake, closes stdin, and asserts
    the process exits — the hang it guards against is not platform-specific.
    """

    def test_stdio_entrypoint_exits_when_stdin_closes(self, tmp_path):
        import json
        import os
        import queue
        import subprocess
        import sys
        import threading

        import psutil

        port = _free_port()
        env = dict(os.environ)
        env["STEALTH_MCP_BROWSER_SESSION_ROOT"] = str(tmp_path / "sessions")
        env["STEALTH_MCP_NO_AUTO_RECOVERY"] = "1"

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "stealth_chrome_devtools_mcp",
                "--singleton-port",
                str(port),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
        spawned = []
        try:
            answers = queue.Queue()

            def _reader():
                try:
                    for line in proc.stdout:
                        answers.put(line)
                finally:
                    answers.put(None)

            threading.Thread(target=_reader, daemon=True).start()

            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "entrypoint-test", "version": "1"},
                },
            }
            proc.stdin.write(json.dumps(init) + "\n")
            proc.stdin.flush()

            # The proxy answers initialize locally and instantly.
            reply = answers.get(timeout=30)
            assert reply and "serverInfo" in reply, f"no initialize answer: {reply!r}"

            # Capture the singleton backend it started so we can reap it after.
            try:
                spawned = psutil.Process(proc.pid).children(recursive=True)
            except psutil.Error:
                spawned = []

            proc.stdin.close()  # client disconnects: stdin hits EOF

            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "stdio entrypoint did not exit within 15s of client disconnect "
                    "— the proxy process leak regressed"
                )
        finally:
            if proc.poll() is None:
                proc.kill()
            for child in spawned:
                try:
                    child.kill()
                except psutil.Error:
                    pass
