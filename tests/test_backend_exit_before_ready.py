"""T-14185 - a backend this proxy launched that EXITS before it is ready ends the wait.

On 2026-11-04, after a reboot, every cold start of the shared backend died at import
time (``ImportError: MemoryStore requires py-key-value-aio[memory]``). The cause was in
``backend-boot.log`` and nowhere a person or a client looks: each stdio proxy waited out
the whole 120 s ``BACKEND_READY_TIMEOUT`` for a process that had been dead since the
second after its spawn, logged only ``backend did not become ready``, and answered no
call at all.

What this file pins, each against the seams the defect lives in (``_start_server_process``
for the launch, ``_await_backend_http`` for the wait, ``_proxy_streams`` for the client):

  * a launched backend that exits ends the wait at once, False, and the ERROR in the
    proxy log carries the exit code, the boot log's path and the ImportError line - the
    bytes THIS backend wrote, not an older crash's;
  * a backend that is merely slow, and one this process did not launch, keep the
    whole deadline;
  * the client's call is answered, with the cause, while the backend is down;
  * what lands in the client's error is capped and scrubbed.

Everything is hermetic: the "backend" is a short-lived ``sys.executable`` child that
writes to a boot log under ``tmp_path`` (``backend_launch.spawn`` is the one seam
replaced), and the readiness target is a stdlib HTTP stub or a closed port.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_exit,
    backend_launch,
    proxy_selfheal,
    singleton,
)

IMPORT_ERROR = "ImportError: MemoryStore requires py-key-value-aio[memory]"
_CRASH = (
    "import sys\n"
    "sys.stderr.write('Traceback (most recent call last):\\n')\n"
    "sys.stderr.write('  File \"x.py\", line 1, in <module>\\n')\n"
    f"sys.stderr.write('{IMPORT_ERROR}\\n')\n"
    "sys.stderr.flush()\n"
    "sys.exit(1)\n"
)
_SILENT_EXIT = "import sys\nsys.exit(3)\n"
_ALIVE = "import time\ntime.sleep(120)\n"
STALE_CRASH = "ValueError: an older boot's crash, not this backend's"


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(autouse=True)
def _clean_exit_table(monkeypatch):
    monkeypatch.setattr(backend_exit, "_watched", {})
    monkeypatch.setattr(backend_exit, "_verdict", {"failure": None})


@pytest.fixture()
def state(tmp_path, monkeypatch):
    """``_start_server_process`` with every side effect diverted into ``tmp_path``."""
    monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(singleton, "_ensure_state_dir", lambda: None)
    monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
    boot_log = tmp_path / "logs" / "backend-boot.log"
    boot_log.parent.mkdir()
    boot_log.write_text(f"Traceback (most recent call last):\n{STALE_CRASH}\n")
    return boot_log


@pytest.fixture()
def children(monkeypatch):
    """``backend_launch.spawn`` replaced by "run this python source into the boot log"."""
    procs: list[subprocess.Popen] = []
    plan = {"code": _CRASH}

    def fake_spawn(_cmd, _env, boot_log):
        with boot_log.open("ab") as out:
            proc = subprocess.Popen(
                [sys.executable, "-c", plan["code"]],
                stdout=out,
                stderr=out,
                stdin=subprocess.DEVNULL,
            )
        procs.append(proc)
        return backend_launch.Launched(proc.pid, "test")

    monkeypatch.setattr(backend_launch, "spawn", fake_spawn)
    yield plan
    for proc in procs:
        proc.kill()
        proc.wait(10)


def _url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp/"


class _Stub:
    """A backend that answers ``initialize`` 200 once ``serving`` is set."""

    def __init__(self):
        self.serving = threading.Event()
        serving = self.serving

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802  stdlib override, PERMANENT(interface)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200 if serving.is_set() else 503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"jsonrpc":"2.0","id":0,"result":{}}')

            def do_DELETE(self):  # noqa: N802  stdlib override, PERMANENT(interface)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def serve_after(self, seconds: float) -> None:
        threading.Timer(seconds, self.serving.set).start()


@pytest.fixture()
def stub():
    backend = _Stub()
    yield backend
    backend.server.shutdown()
    backend.server.server_close()


async def _timed(coro) -> tuple[object, float]:
    started = time.monotonic()
    result = await coro
    return result, time.monotonic() - started


class TestALaunchedBackendThatExitsEndsTheWait:
    async def test_the_wait_returns_early_and_the_error_names_the_import_error(
        self, state, children, caplog
    ):
        port = _closed_port()
        singleton._start_server_process(port)

        with caplog.at_level(logging.ERROR, logger="stealth.proxy"):
            ready, took = await _timed(singleton._await_backend_http(_url(port), 10.0))

        assert ready is False
        assert took < 7.0, f"waited {took:.1f}s of a 10s deadline for a dead backend"
        said = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= 40)
        assert IMPORT_ERROR in said
        assert "exited with code 1" in said
        assert str(state) in said
        assert STALE_CRASH not in said, "an older boot's crash is not this one's"

    async def test_an_exit_that_wrote_nothing_does_not_borrow_an_older_crash(
        self, state, children, caplog
    ):
        children["code"] = _SILENT_EXIT
        port = _closed_port()
        singleton._start_server_process(port)

        with caplog.at_level(logging.ERROR, logger="stealth.proxy"):
            ready, took = await _timed(singleton._await_backend_http(_url(port), 10.0))

        assert ready is False
        assert took < 7.0
        said = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= 40)
        assert "exited with code 3" in said
        assert "no exception line" in said
        assert STALE_CRASH not in said

    async def test_the_exit_is_reported_once_and_does_not_blame_a_later_wait(
        self, state, children, stub
    ):
        singleton._start_server_process(stub.port)
        first, _ = await _timed(singleton._await_backend_http(_url(stub.port), 10.0))
        assert first is False  # the stub is not serving yet and our child is dead

        stub.serve_after(1.0)  # another proxy's backend is coming up on that port
        second, _ = await _timed(singleton._await_backend_http(_url(stub.port), 10.0))
        assert second is True


class TestOtherBackendsKeepTheWholeDeadline:
    async def test_a_slow_but_alive_backend_is_waited_for(self, state, children, stub):
        children["code"] = _ALIVE
        singleton._start_server_process(stub.port)
        stub.serve_after(1.5)

        ready, took = await _timed(singleton._await_backend_http(_url(stub.port), 15.0))

        assert ready is True
        assert 1.0 <= took < 10.0

    async def test_a_backend_we_did_not_launch_is_unaffected(
        self, state, children, stub
    ):
        # A dead child on ANOTHER port is the only launch this process has made.
        singleton._start_server_process(_closed_port())
        stub.serve_after(1.5)

        ready, took = await _timed(singleton._await_backend_http(_url(stub.port), 15.0))

        assert ready is True
        assert took >= 1.0

    async def test_with_no_launch_at_all_the_deadline_is_the_deadline(self):
        ready, took = await _timed(
            singleton._await_backend_http(_url(_closed_port()), 1.5)
        )

        assert ready is False
        assert took >= 1.4


class TestTheCauseIsBoundedAndScrubbed:
    def test_it_is_capped_and_a_secret_shape_is_redacted(self, tmp_path):
        log = tmp_path / "boot.log"
        long_tail = "word " * 200
        log.write_text(
            "Traceback (most recent call last):\n"
            '  File "a.py", line 1, in <module>\n'
            "RuntimeError: bad dsn=https://abc123@sentry.example/1 "
            f"token=hunter2 {long_tail}\n"
        )

        cause = backend_exit._cause(log, 0)

        assert cause is not None
        assert cause.startswith("RuntimeError: bad dsn=")
        assert len(cause) <= backend_exit.CAUSE_MAX_CHARS
        assert "hunter2" not in cause
        assert "abc123" not in cause

    def test_only_the_tail_of_a_huge_log_is_read(self, tmp_path):
        log = tmp_path / "boot.log"
        log.write_bytes(
            b"ValueError: buried\n" + b"." * (4 * backend_exit.TAIL_BYTES) + b"\n"
        )

        assert backend_exit._cause(log, 0) is None


def _request(req_id, method):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    req = JSONRPCRequest(jsonrpc="2.0", id=req_id, method=method, params={})
    return SessionMessage(message=JSONRPCMessage(req))


def _notification(method):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

    note = JSONRPCNotification(jsonrpc="2.0", method=method)
    return SessionMessage(message=JSONRPCMessage(note))


class _Sink:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class TestWaitRefusing:
    async def test_without_a_known_failure_queued_calls_are_left_for_the_backend(self):
        tx, rx = anyio.create_memory_object_stream(8)
        await tx.send(_request(7, "tools/call"))
        sink = _Sink()

        await proxy_selfheal.PendingCalls(rx).wait_refusing(sink, 0.05, None)

        assert sink.sent == []
        assert rx.receive_nowait().message.root.id == 7  # still queued

    async def test_with_a_failure_every_request_is_answered_and_the_rest_dropped(self):
        tx, rx = anyio.create_memory_object_stream(8)
        for message in (
            _request(1, "initialize"),
            _notification("notifications/initialized"),
            _request(2, "tools/list"),
        ):
            await tx.send(message)
        sink = _Sink()

        pending = proxy_selfheal.PendingCalls(rx)
        await pending.wait_refusing(sink, 0.2, "the backend exited: BOOM")
        await tx.send(_request(3, "tools/call"))  # arrives later in the same wait
        await pending.wait_refusing(sink, 0.2, "the backend exited: BOOM")

        answered = {m.message.root.id: m.message.root.error.message for m in sink.sent}
        assert sorted(answered) == [2, 3]
        assert all("BOOM" in text for text in answered.values())
        assert "'tools/list'" in answered[2]


class TestTheClientIsTold:
    async def test_a_call_made_while_the_backend_keeps_crashing_is_answered_with_why(
        self, state, children, monkeypatch
    ):
        """The whole path, ``_proxy_streams`` included: the proxy launched a backend
        that dies at import time, every heal relaunches the same crash, and the
        client's ``tools/call`` - which used to wait for ever - gets the ImportError.

        Bounded: on a build without the fix the call is never answered, so the
        ``fail_after`` below is the RED, 20 s and not the 120 s deadline."""
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCMessage, JSONRPCRequest

        port = _closed_port()
        monkeypatch.setattr(proxy_selfheal, "HEAL_ATTEMPT_SECONDS", 3.0)
        monkeypatch.setattr(proxy_selfheal, "HEAL_BACKOFF_SECONDS", 0.05)
        monkeypatch.setattr(proxy_selfheal, "RETRY_BASE_SECONDS", 0.05)
        monkeypatch.setattr(proxy_selfheal, "RETRY_MAX_SECONDS", 0.2)

        async def never_stale(*_a, **_k):
            await anyio.sleep_forever()

        monkeypatch.setattr(singleton, "_watch_backend_liveness", never_stale)

        def relaunch(dead_port):
            singleton._start_server_process(dead_port)
            return dead_port

        monkeypatch.setattr(singleton, "ensure_server_running", relaunch)
        singleton._start_server_process(port)  # the launch run_stdio_proxy made

        c2p_tx, c2p_rx = anyio.create_memory_object_stream(50)
        p2c_tx, p2c_rx = anyio.create_memory_object_stream(50)
        init = JSONRPCRequest(
            jsonrpc="2.0",
            id=1,
            method="initialize",
            params={
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        )
        async with anyio.create_task_group() as tg:
            tg.start_soon(singleton._proxy_streams, c2p_rx, p2c_tx, port)
            await c2p_tx.send(SessionMessage(message=JSONRPCMessage(init)))
            with anyio.fail_after(20):
                assert (await p2c_rx.receive()).message.root.id == 1  # local answer
                await c2p_tx.send(_request(2, "tools/call"))
                refusal = (await p2c_rx.receive()).message.root
            tg.cancel_scope.cancel()

        assert refusal.id == 2
        assert IMPORT_ERROR in refusal.error.message
        assert "exited with code 1" in refusal.error.message
        assert "'tools/call' was not run" in refusal.error.message
