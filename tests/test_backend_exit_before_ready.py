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
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_exit,
    backend_launch,
    backend_probe,
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


def _cold_start_threads() -> list[threading.Thread]:
    """Every ``_start_backend_holding_lock`` thread still running: each holds the
    cold-start lock, then reaches ``_start_server_process``."""
    return [
        t
        for t in threading.enumerate()
        if getattr(t, "_target", None) is singleton._start_backend_holding_lock
    ]


#: How long teardown waits for the cold-start threads a test caused. A thread is
#: bounded by ``_wait_for_server``'s deadline plus the reuse gate's patience, so this
#: is a ceiling a healthy run never reaches.
_DRAIN_SECONDS = 120.0


class _Pinned:
    """Patches that stay in force until nothing the test started can still use them.

    ``monkeypatch`` undoes at teardown whatever has happened to the threads a test
    caused, and ``ensure_server_running`` starts a daemon thread per call (one per
    heal attempt). A thread that outlives the test then runs the REAL
    ``_start_server_process`` against the REAL ``~/.stealth-mcp``: it launches a real
    ``python -m stealth_chrome_devtools_mcp`` backend, which logs to the real
    ``backend-<pid>.log`` and writes the real ``heartbeat-<port>.json``. Three such
    backends were found after this branch's test runs. So everything a cold start
    reaches is pinned here, undone only after the threads are gone, and left in place
    (the test then errors) when one is not."""

    def __init__(self) -> None:
        self.plan = {"code": _CRASH}
        self.procs: list[subprocess.Popen] = []
        self.closed = threading.Event()
        self.gate = threading.Lock()
        self.entered = 0  # ``ensure_server_running`` calls not yet returned
        self._undo: list = []

    def setattr(self, target, name, value) -> None:
        old = getattr(target, name)
        setattr(target, name, value)
        self._undo.append(lambda: setattr(target, name, old))

    def setenv(self, name: str, value: str) -> None:
        old = os.environ.get(name)
        os.environ[name] = value
        self._undo.append(
            lambda: (
                os.environ.pop(name, None)
                if old is None
                else os.environ.__setitem__(name, old)
            )
        )

    def restore(self) -> None:
        while self._undo:
            self._undo.pop()()


@pytest.fixture(autouse=True)
def pinned(tmp_path):
    """Every test of this file launches backends only through a fake ``spawn``, over
    paths under ``tmp_path``, and neither is undone while a thread can still reach it.

    The fake RAISES once teardown has waited for the threads, so a spawn that arrives
    later starts nothing."""
    pin = _Pinned()
    real_spawn = backend_launch.spawn

    def fake_spawn(_cmd, _env, boot_log):
        if pin.closed.is_set():
            raise RuntimeError("a backend spawn was reached after the test ended")
        with boot_log.open("ab") as out:
            proc = subprocess.Popen(
                [sys.executable, "-c", pin.plan["code"]],
                stdout=out,
                stderr=out,
                stdin=subprocess.DEVNULL,
            )
        pin.procs.append(proc)
        return backend_launch.Launched(proc.pid, "test")

    pin.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path / "logs"))
    pin.setattr(singleton, "STATE_DIR", tmp_path)
    pin.setattr(singleton, "LOCK_FILE", tmp_path / "singleton.lock")
    pin.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    pin.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    pin.setattr(singleton, "_ensure_state_dir", lambda: None)
    pin.setattr(singleton, "_server_version", lambda: "1.2.1")
    pin.setattr(backend_launch, "spawn", fake_spawn)

    real_ensure = singleton.ensure_server_running

    def counted_ensure(*args, **kwargs):
        """A heal calls this from a worker thread, and it is what STARTS a cold-start
        thread: counted, so teardown can tell "none left" from "one about to start"."""
        with pin.gate:
            if pin.closed.is_set():
                raise RuntimeError("a backend start was requested after the test ended")
            pin.entered += 1
        try:
            return real_ensure(*args, **kwargs)
        finally:
            with pin.gate:
                pin.entered -= 1

    pin.setattr(singleton, "ensure_server_running", counted_ensure)
    yield pin
    deadline = time.monotonic() + _DRAIN_SECONDS
    while time.monotonic() < deadline:
        with pin.gate:
            if not pin.entered and not _cold_start_threads():
                break
        time.sleep(0.1)
    with pin.gate:
        pin.closed.set()  # from here a late spawn or start raises instead of acting
    leaked = _cold_start_threads() or pin.entered
    for proc in pin.procs:
        proc.kill()
        proc.wait(10)
    if not leaked:
        pin.restore()  # nothing can reach any of it any more
    assert not leaked, (
        "a cold-start thread outlived the test (every patch left in place)"
    )
    assert backend_launch.spawn is real_spawn


@pytest.fixture()
def state(tmp_path, pinned):
    """The boot log ``_start_server_process`` writes to, holding an older boot's crash."""
    boot_log = tmp_path / "logs" / "backend-boot.log"
    boot_log.parent.mkdir()
    boot_log.write_text(f"Traceback (most recent call last):\n{STALE_CRASH}\n")
    return boot_log


@pytest.fixture()
def children(pinned):
    """The fake backend's source (``pinned``'s ``plan``): "run this into the boot log"."""
    return pinned.plan


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


async def _wait_until(predicate, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            return False
        await anyio.sleep(0.05)
    return True


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

    async def test_the_exit_stays_recorded_for_every_wait_that_follows(
        self, state, children, caplog
    ):
        """A heal that cannot take the lock spawns nothing, so the wait it runs next
        must still see the exit the FIRST wait consumed under a1d54126."""
        port = _closed_port()
        singleton._start_server_process(port)
        first, _ = await _timed(singleton._await_backend_http(_url(port), 10.0))

        with caplog.at_level(logging.ERROR, logger="stealth.proxy"):
            second, took = await _timed(singleton._await_backend_http(_url(port), 10.0))

        assert first is False
        assert second is False
        assert took < 5.0
        assert IMPORT_ERROR in "\n".join(r.getMessage() for r in caplog.records)
        assert backend_exit.latest() is not None

    async def test_a_new_spawn_keeps_the_verdict_until_it_is_known_ready_or_gone(
        self, state, children, stub
    ):
        port = _closed_port()
        singleton._start_server_process(port)
        died, _ = await _timed(singleton._await_backend_http(_url(port), 10.0))
        assert died is False
        said = backend_exit.latest()
        assert said is not None

        children["code"] = _ALIVE
        singleton._start_server_process(port)  # the retry's spawn
        assert backend_exit.latest() == said, "the retry wait still refuses with why"
        assert backend_exit.exited(port) is False  # the record is the NEW child's

        stub.serving.set()  # any ready backend ends the story
        ready, _ = await _timed(singleton._await_backend_http(_url(stub.port), 10.0))
        assert ready is True
        assert backend_exit.latest() is None

    async def test_a_stale_exit_does_not_abort_a_wait_on_a_backend_we_did_not_launch(
        self, state, children, stub, caplog
    ):
        """Our child crashed on P; another proxy then brought a good backend up on P.
        While it boots it answers 503, and the stale record must not read that as
        "our child is gone" - nothing the wait saw proves it, and under load nothing
        else would ever clear the record."""
        singleton._start_server_process(stub.port)  # a crashed child, on P
        assert await _wait_until(lambda: backend_exit.exited(stub.port))
        stub.serve_after(1.5)  # P answers 503 until then, then 200

        with caplog.at_level(logging.ERROR, logger="stealth.proxy"):
            ready, took = await _timed(
                singleton._await_backend_http(_url(stub.port), 15.0)
            )

        assert ready is True
        assert took >= 1.0
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert backend_exit.latest() is None, "nothing may be refused"

    async def test_a_poll_that_connected_and_timed_out_is_someone_elses_too(
        self, state, children, monkeypatch
    ):
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(5)  # accepts at the kernel and never answers: a read timeout
        port = silent.getsockname()[1]
        try:
            singleton._start_server_process(port)
            assert await _wait_until(lambda: backend_exit.exited(port))
            monkeypatch.setattr(backend_probe, "_POLL_ATTEMPT_SECONDS", 0.3)

            ready, took = await _timed(singleton._await_backend_http(_url(port), 2.5))

            assert ready is False
            assert took >= 2.0, "it kept its deadline instead of blaming our child"
            assert backend_exit.exited(port) is False  # the stale record was dropped
        finally:
            silent.close()

    def test_a_spawn_that_cannot_be_watched_drops_the_old_record(
        self, state, children, monkeypatch
    ):
        port = _closed_port()
        singleton._start_server_process(port)
        deadline = time.monotonic() + 10
        while not backend_exit.exited(port) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert backend_exit.exited(port)

        def denied(_pid):
            raise psutil.AccessDenied(_pid)

        monkeypatch.setattr(psutil, "Process", denied)
        children["code"] = _ALIVE
        singleton._start_server_process(port)

        assert backend_exit.exited(port) is False

    def test_forget_drops_only_the_verdict_that_names_its_port(self):
        backend_exit._verdict["failure"] = backend_exit.Failure(1, "x", None, 5)

        backend_exit.forget(6)
        assert backend_exit.latest() is not None
        backend_exit.forget(5)
        assert backend_exit.latest() is None

    async def test_the_cold_start_lock_wait_gives_up_when_the_child_is_gone(
        self, state, children, monkeypatch
    ):
        """``_wait_for_server`` holds the cold-start lock; it used to sit out the whole
        ``STARTUP_TIMEOUT`` for a process that had already exited."""
        port = _closed_port()
        singleton._start_server_process(port)

        ready, took = await _timed(
            anyio.to_thread.run_sync(singleton._wait_for_server, port, 20)
        )

        assert ready is False
        assert took < 8.0, f"held the lock {took:.1f}s of 20s for a dead backend"


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

    @pytest.mark.parametrize(
        ("line", "secret"),
        [
            ("RuntimeError: API_TOKEN=abc123 was rejected", "abc123"),
            ("RuntimeError: DB_PASSWORD=hunter2", "hunter2"),
            ("RuntimeError: Authorization: Bearer shorttok", "shorttok"),
            ('RuntimeError: token = "a b c" was bad', "b c"),
            ("RuntimeError: secret='two words' was bad", "words"),
            ("RuntimeError: sentry dsn: https://k@o.example/1", "k@o.example"),
        ],
    )
    def test_upper_snake_and_bearer_secret_shapes_are_redacted(
        self, tmp_path, line, secret
    ):
        log = tmp_path / "boot.log"
        log.write_text(f"Traceback (most recent call last):\n{line}\n")

        cause = backend_exit._cause(log, 0)

        assert cause is not None
        assert cause.startswith("RuntimeError:")
        assert secret not in cause
        assert "<redacted>" in cause

    def test_the_import_error_text_survives_the_scrub(self, tmp_path):
        log = tmp_path / "boot.log"
        log.write_text(f"Traceback (most recent call last):\n{IMPORT_ERROR}\n")

        assert backend_exit._cause(log, 0) == IMPORT_ERROR

    def test_a_log_rolled_since_the_spawn_names_no_cause(self, tmp_path):
        log = tmp_path / "boot.log"
        log.write_text(f"{STALE_CRASH}\n")  # a new, shorter file: another boot's

        assert backend_exit._cause(log, offset=10_000) is None

    def test_only_the_tail_of_a_huge_log_is_read(self, tmp_path):
        log = tmp_path / "boot.log"
        log.write_bytes(
            b"ValueError: buried\n" + b"." * (4 * backend_exit.TAIL_BYTES) + b"\n"
        )

        assert backend_exit._cause(log, 0) is None


class _Proc:
    """A ``psutil.Process`` whose ``wait(0)`` does what the test says."""

    def __init__(self, outcome):
        self.outcome = outcome

    def wait(self, _timeout):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class TestWhatCountsAsExited:
    @pytest.mark.parametrize(
        ("outcome", "gone"),
        [
            (7, True),
            (psutil.NoSuchProcess(1), True),
            (psutil.ZombieProcess(1), True),
            (psutil.TimeoutExpired(0), False),
            (psutil.AccessDenied(1), False),
            (psutil.Error(), False),
        ],
    )
    def test_only_a_wait_that_ended_or_a_missing_process_is_an_exit(
        self, outcome, gone
    ):
        assert backend_exit._exit_of(_Proc(outcome))[0] is gone


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


class TestNothingRealCanBeStartedFromThisFile:
    """The seams ``pinned`` puts in place are the ones a cold start would use."""

    def test_the_spawn_seam_is_the_fake_and_every_path_is_under_tmp_path(
        self, tmp_path, pinned
    ):
        assert backend_launch.spawn.__name__ == "fake_spawn"
        for path in (
            singleton.STATE_DIR,
            singleton.LOCK_FILE,
            singleton.PORT_FILE,
            singleton.SERVER_STATE_FILE,
        ):
            assert tmp_path in path.parents or path == tmp_path
        from stealth_chrome_devtools_mcp.embedded import logging_setup

        assert tmp_path in logging_setup.resolve_log_dir().parents

    def test_a_spawn_or_a_start_after_teardown_raises(self, tmp_path, pinned):
        pinned.closed.set()

        with pytest.raises(RuntimeError, match="after the test ended"):
            backend_launch.spawn(["x"], {}, tmp_path / "boot.log")
        with pytest.raises(RuntimeError, match="after the test ended"):
            singleton.ensure_server_running(_closed_port())
        assert pinned.procs == []
        pinned.closed.clear()  # the fixture's own teardown still has to drain


class TestTheRefusalIsPortScoped:
    """``drive`` refuses the client's calls with the exit only when it is the exit of
    the port the last heal actually waited on."""

    DEAD = 41901  # the port our launched backend died on
    MOVED = 41902  # where the heal went instead (``_select_backend_port``'s fallback)

    async def _drive_one_retry_wait(self, monkeypatch, *, heal_port):
        """``drive`` with a dead generation, a heal that waits on ``heal_port`` and
        never finds it ready, and the verdict standing on ``DEAD``; one ``tools/call``
        queued. Returns ``(sink, queue)`` after a few retry waits."""
        monkeypatch.setattr(proxy_selfheal, "HEAL_BACKOFF_SECONDS", 0.01)
        monkeypatch.setattr(proxy_selfheal, "RETRY_BASE_SECONDS", 0.05)
        monkeypatch.setattr(proxy_selfheal, "RETRY_MAX_SECONDS", 0.1)
        backend_exit._verdict["failure"] = backend_exit.Failure(
            1, IMPORT_ERROR, None, self.DEAD
        )
        tx, rx = anyio.create_memory_object_stream(8)
        await tx.send(_request(5, "tools/call"))
        sink = _Sink()

        async def connect(_url, _replay, _armed):
            return  # never ready: the generation ends at once

        async def watch(_port):
            return

        async def never_ready(_url, _seconds):
            return False

        with anyio.move_on_after(1.0):
            await proxy_selfheal.drive(
                port=self.DEAD,
                url_for=lambda p: f"http://127.0.0.1:{p}/mcp/",
                connect=connect,
                watch=watch,
                confirm_alive=lambda _port: False,
                replay=lambda: None,
                pending=proxy_selfheal.PendingCalls(rx),
                client_write=sink,
                ensure_running=lambda _dead: heal_port,
                await_ready=never_ready,
            )
        return sink, rx

    async def test_a_verdict_for_another_port_refuses_nothing(self, monkeypatch):
        sink, rx = await self._drive_one_retry_wait(monkeypatch, heal_port=self.MOVED)

        assert sink.sent == [], "the heal moved on: P's exit says nothing about P2"
        assert rx.receive_nowait().message.root.id == 5  # still queued for the backend

    async def test_a_verdict_for_the_port_the_heal_waited_on_refuses(self, monkeypatch):
        sink, _rx = await self._drive_one_retry_wait(monkeypatch, heal_port=self.DEAD)

        assert [m.message.root.id for m in sink.sent] == [5]
        assert IMPORT_ERROR in sink.sent[0].message.root.error.message

    async def test_a_heal_that_found_no_port_refuses_nothing(self, monkeypatch):
        sink, _rx = await self._drive_one_retry_wait(monkeypatch, heal_port=None)

        assert sink.sent == []

    async def test_heal_backend_reports_the_port_it_waits_on(self):
        told: list[int | None] = []
        waited: list[str] = []

        async def never_ready(url, _seconds):
            waited.append(url)
            return False

        healed = await proxy_selfheal.heal_backend(
            self.DEAD,
            ensure_running=lambda _dead: self.MOVED,
            await_ready=never_ready,
            url_for=lambda p: f"http://127.0.0.1:{p}/mcp/",
            on_port=told.append,
        )

        assert healed is None
        assert told[0] is None  # a heal starts with no port waited on
        assert told[1:] == [self.MOVED] * proxy_selfheal.HEAL_ATTEMPTS
        assert all(str(self.MOVED) in url for url in waited)


async def _drain_cold_start_threads(seconds: float = 60.0) -> bool:
    """Wait until every cold-start thread the test caused is done - while the test's
    patches are still in force. True iff none is left."""
    return await _wait_until(lambda: not _cold_start_threads(), seconds)


class TestTheClientIsTold:
    async def test_the_first_refusal_arrives_within_one_heal_attempt(
        self, state, children, pinned, monkeypatch
    ):
        """The REAL wiring, ``ensure_server_running`` included: the cold-start lock,
        its daemon thread and ``_wait_for_server`` are where a1d54126 lost its "at
        once". The holder sat in ``_wait_for_server`` for ``STARTUP_TIMEOUT`` with
        the lock, so the first heal spawned nothing, found no record (the first wait
        had consumed it) and waited out a whole heal attempt blind - and
        ``begin_wait`` cleared the verdict, so the retry loop had nothing to refuse
        with. Only ``backend_launch.spawn`` and the paths are replaced.

        Deadlines are shrunk (a refused connect to a closed port costs ~2 s on
        Windows, so a fast build still needs ~15 s here: the bound is 4x that), and
        bounded: on a1d54126 the refusal comes after more than one heal attempt, so
        the ``< HEAL`` assertion is the RED, with a ``fail_after`` so a build that
        never answers cannot hang the suite. The drain runs in a ``finally``, with
        its own bound, so it also runs when that ``fail_after`` fires."""
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCMessage, JSONRPCRequest

        heal_attempt = 60.0
        pinned.setattr(singleton, "_clear_stale_backend", lambda _port: [])
        # Default arguments are bound at definition: STARTUP_TIMEOUT 30, READY 120.
        pinned.setattr(singleton._wait_for_server, "__defaults__", (70,))
        pinned.setattr(singleton._await_backend_http, "__defaults__", (heal_attempt,))
        monkeypatch.setattr(proxy_selfheal, "HEAL_ATTEMPT_SECONDS", heal_attempt)
        monkeypatch.setattr(proxy_selfheal, "HEAL_BACKOFF_SECONDS", 0.05)
        monkeypatch.setattr(proxy_selfheal, "RETRY_BASE_SECONDS", 0.05)
        monkeypatch.setattr(proxy_selfheal, "RETRY_MAX_SECONDS", 0.2)

        async def never_stale(*_a, **_k):
            await anyio.sleep_forever()

        pinned.setattr(singleton, "_watch_backend_liveness", never_stale)

        # What run_stdio_proxy does at boot: the lock thread spawns and waits.
        port = singleton.ensure_server_running(_closed_port())

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
        started = time.monotonic()
        drained = False
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(singleton._proxy_streams, c2p_rx, p2c_tx, port)
                await c2p_tx.send(SessionMessage(message=JSONRPCMessage(init)))
                with anyio.fail_after(heal_attempt * 2):
                    assert (await p2c_rx.receive()).message.root.id == 1  # local
                    await c2p_tx.send(_request(2, "tools/call"))
                    refusal = (await p2c_rx.receive()).message.root
                took = time.monotonic() - started
                tg.cancel_scope.cancel()
        finally:
            # Also when the body FAILED or timed out: this is the path that leaked.
            with anyio.CancelScope(shield=True):
                drained = await _drain_cold_start_threads()

        assert refusal.id == 2
        assert IMPORT_ERROR in refusal.error.message
        assert "exited with code 1" in refusal.error.message
        assert "'tools/call' was not run" in refusal.error.message
        assert took < heal_attempt, f"first refusal after {took:.1f}s"
        assert drained, (
            "a cold-start thread outlived the test: it would spawn a REAL backend"
        )
