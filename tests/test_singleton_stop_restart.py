"""Pinning tests for M8: the recovery CLI's singleton-side primitives
(_terminate_backend extraction, stop_backend, restart_backend). One growing
file per plan_M8's own sequencing (Step 1's identity test lands here; Steps
4/5's matrix/restart tests are added to this same file as those land;
Amendment A1's restart-under-squat cases extend it further).

Step 1 (_terminate_backend extraction): `_clear_stale_backend`'s pid-resolve
+ terminate + port-release-wait body (previously inlined) is extracted into
a standalone `_terminate_backend(port) -> bool` so `stop` can reuse the exact
terminate mechanism minus eviction's "skip if reusable" guard. Behavior-
preserving refactor: `_clear_stale_backend`'s own coverage
(test_singleton_version_aware.py's TestClearStaleBackend,
test_proxy_backend_death.py) must stay green UNCHANGED - confirmed separately,
not duplicated here.

The mandated recycled-pid refusal test (plan SS5.1): a plain sleeper (no
stealth_chrome_devtools_mcp/--transport in its cmdline) recorded as the
"backend" pid must NEVER be terminated - _is_our_backend's identity check is
what stands between a legitimate recovery action and killing an arbitrary
process that happens to have been assigned a recycled pid.
"""

import json
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import psutil
import pytest
import singleton


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(
        singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
    )
    # LOCK_FILE is bound at module import (like SERVER_STATE_FILE), so patching
    # STATE_DIR alone does not redirect it - patch it explicitly. Without this,
    # tests reaching the real _exclusive_lock() open the REAL user lock file:
    # FileNotFoundError on a clean CI runner, cross-talk with a live backend on dev.
    monkeypatch.setattr(
        singleton, "LOCK_FILE", tmp_path / "singleton.lock", raising=False
    )
    return tmp_path


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn_marked_sleeper():
    """A real subprocess whose cmdline satisfies _is_our_backend (contains
    both stealth_chrome_devtools_mcp AND --transport), even though it never
    runs the actual backend - _is_our_backend only inspects cmdline() text,
    so the extra argv tokens are sufficient to stand in for a real backend
    identity without spawning the real (heavy) server module.

    Spawn-verified: under machine load a just-Popen'd child can die at birth
    (spawn failure) or take long enough to initialize that psutil transiently
    sees no cmdline - either way _is_our_backend would refuse a legitimate
    terminate and the test would flake (observed in a contended full-suite
    run). Poll until the identity markers are visible to psutil, respawning
    a child that died before becoming visible, so callers always receive a
    sleeper that _is_our_backend provably recognizes."""
    for _ in range(3):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(120)",
                "stealth_chrome_devtools_mcp",
                "--transport",
                "http",
            ]
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # died at birth - respawn
            try:
                if "--transport" in psutil.Process(proc.pid).cmdline():
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    pytest.fail("could not spawn a psutil-visible marked sleeper in 3 attempts")


def _spawn_plain_sleeper():
    """Same sleeper shape, no identity markers - the recycled-pid stand-in
    _is_our_backend must refuse to terminate. Spawn-verified like the marked
    sleeper (its tests assert the child SURVIVES, so a child that died at
    birth under load would flake them the same way)."""
    for _ in range(3):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # died at birth - respawn
            try:
                if psutil.Process(proc.pid).cmdline():
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    pytest.fail("could not spawn a psutil-visible plain sleeper in 3 attempts")


@contextmanager
def _tracking_lock(calls: list, got: bool):
    """Stand-in for _exclusive_lock() that records entry/exit into `calls`
    instead of taking a real file lock, so restart's ordering test can assert
    the lock is held for the whole terminate->spawn->wait sequence without a
    real LOCK_FILE."""
    calls.append("lock-enter")
    try:
        yield got
    finally:
        calls.append("lock-exit")


def _bind_and_listen() -> socket.socket:
    """A real, foreign-by-construction listener on a throwaway ephemeral
    port (duplicated from test_singleton_port_fallback.py's convention):
    this test process's cmdline never satisfies _is_our_backend, so
    _backend_pid_on_port(port) is None for it - exactly the "socket open,
    not ours" shape _port_is_foreign_held checks for. Caller closes it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


class TestTerminateBackendIdentity:
    def test_terminates_marked_sleeper_via_recorded_pid(self, isolated_state):
        proc = _spawn_marked_sleeper()
        try:
            # Nothing listening on this port -> forces the recorded-pid
            # fallback path (not the by-port LISTEN path).
            port = _free_closed_port()
            (isolated_state / "server.json").write_text(
                json.dumps({"port": port, "version": "1.2.1", "pid": proc.pid})
            )

            result = singleton._terminate_backend(port)

            proc.wait(timeout=10)
            assert result is True
            assert proc.poll() is not None  # terminated
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_refuses_plain_sleeper_recycled_pid(self, isolated_state):
        proc = _spawn_plain_sleeper()
        try:
            port = _free_closed_port()
            (isolated_state / "server.json").write_text(
                json.dumps({"port": port, "version": "1.2.1", "pid": proc.pid})
            )

            result = singleton._terminate_backend(port)

            assert result is False
            # The recycled-pid nightmare: an unrelated process recorded under
            # a stale pid must survive untouched.
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_returns_false_when_nothing_to_terminate(self, isolated_state):
        port = _free_closed_port()
        # No server.json at all.
        assert singleton._terminate_backend(port) is False


class TestSpawnEnvScrub:
    """M8-2: a backend spawned via _start_server_process must always reap its
    own orphaned browsers, even when the CLI-invoking parent process has
    STEALTH_MCP_NO_AUTO_RECOVERY set (cli.py's os.environ.setdefault, so a CLI
    subcommand's own import of the server module doesn't trigger recovery).
    Without this scrub, a `restart`-spawned backend would silently inherit the
    flag and skip ProcessCleanup's recovery-on-init."""

    def test_scrubs_no_auto_recovery_from_child_env(self, isolated_state, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_NO_AUTO_RECOVERY", "1")
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        fake_proc = MagicMock()
        fake_proc.pid = 4242
        captured = MagicMock(return_value=fake_proc)
        monkeypatch.setattr(singleton.subprocess, "Popen", captured)

        singleton._start_server_process(4321)

        _, kwargs = captured.call_args
        assert "env" in kwargs
        assert "STEALTH_MCP_NO_AUTO_RECOVERY" not in kwargs["env"]

    def test_child_env_otherwise_matches_parent(self, isolated_state, monkeypatch):
        """The scrub removes exactly one key - it must not silently drop the
        rest of the parent's environment (e.g. PATH), which would break the
        child's ability to locate its own interpreter/DLLs."""
        # Deliberately NOT STEALTH_MCP_*-prefixed: settings.py loudly rejects
        # any unrecognized STEALTH_MCP_* key, which would fail this test for
        # an unrelated reason if the canary used that prefix.
        monkeypatch.setenv("STEALTH_MCP_NO_AUTO_RECOVERY", "1")
        monkeypatch.setenv("SINGLETON_TEST_CANARY", "canary-value")
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        fake_proc = MagicMock()
        fake_proc.pid = 4242
        captured = MagicMock(return_value=fake_proc)
        monkeypatch.setattr(singleton.subprocess, "Popen", captured)

        singleton._start_server_process(4321)

        _, kwargs = captured.call_args
        assert kwargs["env"]["SINGLETON_TEST_CANARY"] == "canary-value"


class TestStopBackend:
    """M8-4: stop_backend() = _exclusive_lock -> _terminate_backend(port) ->
    clear server.json/PORT_FILE -> (result, pid). The verb x state matrix
    (plan SS5.2), driven by stubbing _probe_backend_status (M1's one shared
    liveness vocabulary - binding ruling (a): no new health check anywhere)
    plus a real marked/plain sleeper standing in for "our identifiable
    backend", so the identity-safety property (recycled-pid refusal) is
    exercised end-to-end here too, not just at the _terminate_backend layer
    (M8-1)."""

    def test_responsive_backend_is_stopped_and_state_cleared(
        self, isolated_state, monkeypatch
    ):
        proc = _spawn_marked_sleeper()
        try:
            port = _free_closed_port()
            singleton.PORT_FILE.write_text(str(port))
            singleton._write_server_state(port=port, version="1.2.1", pid=proc.pid)
            monkeypatch.setattr(
                singleton, "_probe_backend_status", lambda: ("responsive", port)
            )

            result, pid = singleton.stop_backend()

            proc.wait(timeout=10)
            assert result == "stopped"
            assert pid == proc.pid
            assert proc.poll() is not None
            assert singleton._read_server_state() is None
            assert not singleton.PORT_FILE.exists()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_wedged_backend_is_stopped(self, isolated_state, monkeypatch):
        proc = _spawn_marked_sleeper()
        try:
            port = _free_closed_port()
            singleton._write_server_state(port=port, version="1.2.1", pid=proc.pid)
            monkeypatch.setattr(
                singleton, "_probe_backend_status", lambda: ("wedged", port)
            )

            result, pid = singleton.stop_backend()

            proc.wait(timeout=10)
            assert result == "stopped"
            assert proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_down_backend_reports_already_stopped(self, isolated_state, monkeypatch):
        port = _free_closed_port()
        singleton._write_server_state(port=port, version="1.2.1", pid=4242)
        monkeypatch.setattr(singleton, "_probe_backend_status", lambda: ("down", port))
        # Deterministic "the recorded pid no longer exists" - avoids relying
        # on a magic pid number that happens not to be in use.
        monkeypatch.setattr(
            singleton.psutil,
            "Process",
            MagicMock(side_effect=singleton.psutil.NoSuchProcess(4242)),
        )

        result, pid = singleton.stop_backend()

        assert result == "already stopped"
        assert pid is None
        assert singleton._read_server_state() is None

    def test_none_reports_not_running(self, isolated_state, monkeypatch):
        monkeypatch.setattr(singleton, "_probe_backend_status", lambda: ("none", None))

        result, pid = singleton.stop_backend()

        assert result == "not running"
        assert pid is None

    def test_lock_contended_reports_busy(self, isolated_state, monkeypatch):
        monkeypatch.setattr(
            singleton, "_probe_backend_status", lambda: ("responsive", 19222)
        )

        @contextmanager
        def _fake_contended_lock():
            yield False

        monkeypatch.setattr(singleton, "_exclusive_lock", _fake_contended_lock)

        result, pid = singleton.stop_backend()

        assert result == "busy"
        assert pid is None

    def test_recycled_foreign_pid_is_not_killed(self, isolated_state, monkeypatch):
        proc = _spawn_plain_sleeper()
        try:
            port = _free_closed_port()
            singleton._write_server_state(port=port, version="1.2.1", pid=proc.pid)
            monkeypatch.setattr(
                singleton, "_probe_backend_status", lambda: ("responsive", port)
            )

            result, pid = singleton.stop_backend()

            assert result == "already stopped"
            # The recycled-pid nightmare: an unrelated process recorded under
            # a stale pid must survive untouched, even via the full stop
            # verb's orchestration (not just _terminate_backend directly).
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait(timeout=5)


class TestRestartBackend:
    """M8-5+M8-8: restart_backend() = _exclusive_lock -> _terminate_backend(
    port) -> _select_backend_port(port) -> _start_server_process(port) ->
    _wait_for_server(port), all under the SAME lock cold start uses (plan_M8
    SS2.1-B / Amendment A1). No second spawn path, no new kill logic. Unlike
    stop_backend, restart does not consult _probe_backend_status() up front
    to short-circuit: its job is unconditional - evict whatever is on the
    target port (nothing, if already down) and bring a fresh backend up.
    The TERMINATE target is the port recorded in server.json, else
    DEFAULT_PORT; the SPAWN port then routes through _select_backend_port
    (M8-8/A1) so a squatter that moved onto the dead backend's port during
    the outage is survived, same as cold start - see
    TestRestartPortSelection below for that behavior's own pinning tests.

    The final state reported is _probe_backend_status()'s, read AFTER the
    lock releases (M1's one liveness vocabulary - binding ruling: no new
    health check anywhere) - a restart that comes back wedged/down must be
    visible, not assumed "responsive". Pinned by the third test below.
    """

    def test_terminate_then_spawn_ordering_under_the_lock(
        self, isolated_state, monkeypatch
    ):
        # Pre-write a recorded port (a real free ephemeral port, never the
        # literal 19222) so the REAL (unmocked) _select_backend_port call
        # inside restart_backend reads isolated state instead of probing the
        # real DEFAULT_PORT on this machine (hermeticity rule shared with
        # test_singleton_port_fallback.py).
        recorded_port = _free_closed_port()
        singleton._write_server_state(port=recorded_port, version="1.2.1", pid=1111)

        calls: list = []
        monkeypatch.setattr(
            singleton, "_exclusive_lock", lambda: _tracking_lock(calls, True)
        )
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: calls.append("terminate")
        )

        def _fake_spawn(port):
            calls.append("spawn")
            # Mimics the real _start_server_process's contract: the spawn
            # itself is what rewrites server.json with the fresh pid.
            singleton._write_server_state(port=port, version="1.2.1", pid=4242)

        monkeypatch.setattr(singleton, "_start_server_process", _fake_spawn)
        monkeypatch.setattr(
            singleton, "_wait_for_server", lambda port: calls.append("wait")
        )
        monkeypatch.setattr(
            singleton, "_probe_backend_status", lambda: ("responsive", 19222)
        )

        result = singleton.restart_backend()

        # The selector runs for real here (not mocked) but appends nothing
        # to `calls` - it is a pure read+decide, not part of the observable
        # terminate/spawn/wait ordering.
        assert calls == ["lock-enter", "terminate", "spawn", "wait", "lock-exit"]
        assert result == ("responsive", 4242)

    def test_busy_on_lock_contention(self, isolated_state, monkeypatch):
        terminate_called = []
        spawn_called = []
        monkeypatch.setattr(
            singleton, "_exclusive_lock", lambda: _tracking_lock([], False)
        )
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: terminate_called.append(port)
        )
        monkeypatch.setattr(
            singleton, "_start_server_process", lambda port: spawn_called.append(port)
        )

        result = singleton.restart_backend()

        assert result == ("busy", None)
        assert terminate_called == []
        assert spawn_called == []

    def test_final_state_is_the_reporters_not_assumed_responsive(
        self, isolated_state, monkeypatch
    ):
        # Same hermeticity pre-write as the ordering test above - the real
        # _select_backend_port call must read isolated state, never probe
        # the real DEFAULT_PORT.
        recorded_port = _free_closed_port()
        singleton._write_server_state(port=recorded_port, version="1.2.1", pid=1111)

        monkeypatch.setattr(
            singleton, "_exclusive_lock", lambda: _tracking_lock([], True)
        )
        monkeypatch.setattr(singleton, "_terminate_backend", lambda port: None)

        def _fake_spawn(port):
            singleton._write_server_state(port=port, version="1.2.1", pid=4242)

        monkeypatch.setattr(singleton, "_start_server_process", _fake_spawn)
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: None)
        # The backend comes back wedged - restart_backend must report that,
        # not the "responsive" the ordering test above pinned.
        monkeypatch.setattr(
            singleton, "_probe_backend_status", lambda: ("wedged", 19222)
        )

        result = singleton.restart_backend()

        assert result == ("wedged", 4242)


class TestRestartPortSelection:
    """M8-8/A1 restart bullet: restart_backend's SPAWN port routes through
    _select_backend_port() AFTER _terminate_backend, so a squatter that
    moved onto the dead backend's port during the outage is survived - the
    same "keep target unless foreign" policy cold start uses (plan_M8
    SSA1.1 restart bullet, SSA1.4 Step M8-8). The TERMINATE target (read
    before the lock) is untouched by this - only the spawn's port changes."""

    def test_recorded_port_is_rebound_when_still_free(
        self, isolated_state, monkeypatch
    ):
        """The common case: a recorded port that is still free (nothing
        foreign squatting it) after the terminate is kept by selection and
        rebound. DEFAULT_PORT/19222 is never even consulted once a recorded
        port exists, so this needs no real squat on "the default" to pin -
        the plan's "default squatted" half of case (1) collapses to this."""
        recorded_port = _free_closed_port()
        singleton._write_server_state(port=recorded_port, version="1.2.1", pid=1111)

        # Deterministic "not foreign-held": a _free_closed_port() is only
        # PROBABLY still free - under machine load another process can rebind
        # it inside the probe window (observed once as a full-suite flake).
        # Stubbing the socket probe pins the policy ("keep the recorded port
        # unless foreign") without racing the OS; the real-socket foreign
        # case is the next test, whose squatter is HELD for the duration.
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: False)
        monkeypatch.setattr(
            singleton, "_exclusive_lock", lambda: _tracking_lock([], True)
        )
        monkeypatch.setattr(singleton, "_terminate_backend", lambda port: None)
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: None)
        monkeypatch.setattr(
            singleton, "_probe_backend_status", lambda: ("responsive", recorded_port)
        )

        spawned_on = {}

        def _fake_spawn(port):
            spawned_on["port"] = port
            singleton._write_server_state(port=port, version="1.2.1", pid=4242)

        monkeypatch.setattr(singleton, "_start_server_process", _fake_spawn)

        singleton.restart_backend()

        assert spawned_on["port"] == recorded_port

    def test_recorded_port_now_foreign_held_falls_back(
        self, isolated_state, monkeypatch
    ):
        """A squatter took the recorded port while the old backend was dead
        (e.g. during the outage restart is recovering from) - selection must
        fall back to a fresh free port, same as cold start's squatter
        survival, rather than trying to rebind a port something else now
        owns. server.json ends up recording the NEW port, not the squatted
        one - the spawn (stubbed here, matching the real contract) is what
        writes it."""
        squatter = _bind_and_listen()
        try:
            squatted_port = squatter.getsockname()[1]
            singleton._write_server_state(port=squatted_port, version="1.2.1", pid=1111)

            monkeypatch.setattr(
                singleton, "_exclusive_lock", lambda: _tracking_lock([], True)
            )
            # No real kill: the recorded pid (1111) is not the squatter, and
            # _terminate_backend is stubbed regardless - nothing real is
            # touched by this test.
            monkeypatch.setattr(singleton, "_terminate_backend", lambda port: None)
            monkeypatch.setattr(singleton, "_wait_for_server", lambda port: None)
            monkeypatch.setattr(
                singleton, "_probe_backend_status", lambda: ("responsive", 0)
            )

            spawned_on = {}

            def _fake_spawn(port):
                spawned_on["port"] = port
                singleton._write_server_state(port=port, version="1.2.1", pid=4242)

            monkeypatch.setattr(singleton, "_start_server_process", _fake_spawn)

            singleton.restart_backend()

            assert spawned_on["port"] != squatted_port
            assert singleton._read_server_state()["port"] == spawned_on["port"]
        finally:
            squatter.close()

    def test_normal_case_return_shape_unchanged_vs_m8_5(
        self, isolated_state, monkeypatch
    ):
        """Selection must not change restart_backend's return contract in
        the common (recorded port still free) case - the same (status, pid)
        shape M8-5 pinned, still produced once M8-8's selection call sits
        between terminate and spawn."""
        recorded_port = _free_closed_port()
        singleton._write_server_state(port=recorded_port, version="1.2.1", pid=1111)

        # Same deterministic not-foreign stub as the rebind test above.
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: False)
        monkeypatch.setattr(
            singleton, "_exclusive_lock", lambda: _tracking_lock([], True)
        )
        monkeypatch.setattr(singleton, "_terminate_backend", lambda port: None)
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: None)
        monkeypatch.setattr(
            singleton, "_probe_backend_status", lambda: ("responsive", recorded_port)
        )

        def _fake_spawn(port):
            singleton._write_server_state(port=port, version="1.2.1", pid=4242)

        monkeypatch.setattr(singleton, "_start_server_process", _fake_spawn)

        result = singleton.restart_backend()

        assert result == ("responsive", 4242)
