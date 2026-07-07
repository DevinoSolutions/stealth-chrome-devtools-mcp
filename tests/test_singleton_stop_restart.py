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
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
import singleton


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(
        singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
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
    identity without spawning the real (heavy) server module."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "stealth_chrome_devtools_mcp",
            "--transport",
            "http",
        ]
    )


def _spawn_plain_sleeper():
    """Same sleeper shape, no identity markers - the recycled-pid stand-in
    _is_our_backend must refuse to terminate."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


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
