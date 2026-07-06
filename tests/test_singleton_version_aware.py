"""Version-aware singleton reuse (issue #14).

The singleton discovery must only reuse a backend it can confirm is the SAME
version as the running package. A stale backend left over from a previous (now
upgraded) version must never be silently reused — otherwise an upgraded user
keeps running old backend code while the handshake reports the new version.

Pure in-memory tests: a real listening socket stands in for a live backend, so
`_server_is_healthy` passes without any HTTP/browser/backend process.
"""

import json
import os
import socket
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
import singleton


def _listening_socket():
    """A real socket accepting connections, so the health probe succeeds."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    return s, s.getsockname()[1]


def _free_port() -> int:
    """A port that is momentarily free (for a real backend to bind)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point singleton state at tmp_path so tests never touch ~/.stealth-mcp."""
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(
        singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
    )
    return tmp_path


class TestVersionAwareReuse:
    def test_reuses_backend_with_matching_version(self, isolated_state, monkeypatch):
        sock, port = _listening_socket()
        try:
            monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
            (isolated_state / "server.json").write_text(
                json.dumps({"port": port, "version": "1.2.1", "pid": os.getpid()})
            )
            (isolated_state / "server.port").write_text(str(port))
            assert singleton._find_running_server() == port
        finally:
            sock.close()

    def test_ignores_backend_with_mismatched_version(self, isolated_state, monkeypatch):
        sock, port = _listening_socket()
        try:
            monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
            # Recorded backend is a stale older version, but still socket-healthy.
            (isolated_state / "server.json").write_text(
                json.dumps({"port": port, "version": "1.1.0", "pid": os.getpid()})
            )
            # Legacy PORT_FILE also present (the pre-fix code would reuse via this).
            (isolated_state / "server.port").write_text(str(port))
            assert singleton._find_running_server() is None
        finally:
            sock.close()

    def test_ignores_legacy_backend_without_recorded_version(
        self, isolated_state, monkeypatch
    ):
        sock, port = _listening_socket()
        try:
            monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
            # Only the legacy PORT_FILE exists (backend started by <= 1.2.0); its
            # version cannot be confirmed, so it must not be reused.
            (isolated_state / "server.port").write_text(str(port))
            assert singleton._find_running_server() is None
        finally:
            sock.close()

    def test_ignores_unhealthy_matching_version(self, isolated_state, monkeypatch):
        # A recorded backend whose port is dead must not be reused even if the
        # version matches (nothing is listening on this port).
        sock, port = _listening_socket()
        sock.close()  # free the port -> unhealthy
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        (isolated_state / "server.json").write_text(
            json.dumps({"port": port, "version": "1.2.1", "pid": os.getpid()})
        )
        assert singleton._find_running_server() is None


class TestServerStatePersistence:
    def test_write_then_read_roundtrips(self, isolated_state):
        singleton._write_server_state(port=12345, version="9.9.9", pid=4242)
        assert singleton._read_server_state() == {
            "port": 12345,
            "version": "9.9.9",
            "pid": 4242,
        }

    def test_written_state_makes_backend_reusable(self, isolated_state, monkeypatch):
        sock, port = _listening_socket()
        try:
            monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
            singleton._write_server_state(port=port, version="1.2.1", pid=os.getpid())
            assert singleton._find_running_server() == port
        finally:
            sock.close()

    def test_start_server_process_records_current_version_and_pid(
        self, isolated_state, monkeypatch
    ):
        from unittest.mock import MagicMock

        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        fake_proc = MagicMock()
        fake_proc.pid = 4242
        monkeypatch.setattr(
            singleton.subprocess, "Popen", MagicMock(return_value=fake_proc)
        )

        singleton._start_server_process(4321)

        state = singleton._read_server_state()
        assert state["port"] == 4321
        assert state["version"] == "1.2.1"
        assert state["pid"] == 4242


class TestBackendIdentity:
    """Safety: eviction must positively identify OUR HTTP backend and nothing
    else — not the stdio proxy, not a foreign process, not a recycled pid."""

    def _proc_with_cmdline(self, cmdline):
        proc = MagicMock()
        proc.cmdline.return_value = cmdline
        return proc

    def test_true_for_http_backend(self, monkeypatch):
        proc = self._proc_with_cmdline(
            [
                "python",
                "-m",
                "stealth_chrome_devtools_mcp",
                "--transport",
                "http",
                "--port",
                "19222",
                "--host",
                "127.0.0.1",
            ]
        )
        monkeypatch.setattr(singleton.psutil, "Process", MagicMock(return_value=proc))
        assert singleton._is_our_backend(4242) is True

    def test_false_for_stdio_proxy(self, monkeypatch):
        # Same module name, but the stdio proxy has no --transport. Killing it
        # would take down the very session asking for a backend.
        proc = self._proc_with_cmdline(
            ["python", "-m", "stealth_chrome_devtools_mcp", "--singleton-port", "19222"]
        )
        monkeypatch.setattr(singleton.psutil, "Process", MagicMock(return_value=proc))
        assert singleton._is_our_backend(4242) is False

    def test_false_for_unrelated_process(self, monkeypatch):
        proc = self._proc_with_cmdline(["node", "server.js", "--transport", "http"])
        monkeypatch.setattr(singleton.psutil, "Process", MagicMock(return_value=proc))
        assert singleton._is_our_backend(4242) is False

    def test_false_when_process_gone(self, monkeypatch):
        monkeypatch.setattr(
            singleton.psutil,
            "Process",
            MagicMock(side_effect=singleton.psutil.NoSuchProcess(4242)),
        )
        assert singleton._is_our_backend(4242) is False

    def test_false_for_non_int_pid(self):
        assert singleton._is_our_backend(None) is False


class TestBackendPidOnPort:
    def _conn(self, port, pid, status):
        c = MagicMock()
        c.laddr = MagicMock()
        c.laddr.port = port
        c.pid = pid
        c.status = status
        return c

    def test_returns_pid_of_our_listener(self, monkeypatch):
        conns = [self._conn(19222, 4242, singleton.psutil.CONN_LISTEN)]
        monkeypatch.setattr(
            singleton.psutil, "net_connections", MagicMock(return_value=conns)
        )
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: pid == 4242)
        assert singleton._backend_pid_on_port(19222) == 4242

    def test_ignores_foreign_listener(self, monkeypatch):
        # A non-ours process holding the port must never be returned for kill.
        conns = [self._conn(19222, 5555, singleton.psutil.CONN_LISTEN)]
        monkeypatch.setattr(
            singleton.psutil, "net_connections", MagicMock(return_value=conns)
        )
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: False)
        assert singleton._backend_pid_on_port(19222) is None

    def test_ignores_listener_on_other_port(self, monkeypatch):
        conns = [self._conn(9999, 4242, singleton.psutil.CONN_LISTEN)]
        monkeypatch.setattr(
            singleton.psutil, "net_connections", MagicMock(return_value=conns)
        )
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: True)
        assert singleton._backend_pid_on_port(19222) is None

    def test_returns_none_when_psutil_fails(self, monkeypatch):
        monkeypatch.setattr(
            singleton.psutil,
            "net_connections",
            MagicMock(side_effect=singleton.psutil.AccessDenied()),
        )
        assert singleton._backend_pid_on_port(19222) is None


class TestClearStaleBackend:
    def test_no_op_when_reusable_backend_present(self, monkeypatch):
        # If the port already holds a reusable same-version backend, nothing may
        # be terminated (must not even look up a pid to kill).
        monkeypatch.setattr(singleton, "_find_running_server", lambda: 19222)
        looked_up = []
        monkeypatch.setattr(
            singleton, "_backend_pid_on_port", lambda port: looked_up.append(port)
        )
        singleton._clear_stale_backend(19222)
        assert looked_up == []

    def test_terminates_stale_backend_on_port(self, monkeypatch):
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(singleton, "_backend_pid_on_port", lambda port: 4242)
        proc = MagicMock()
        monkeypatch.setattr(singleton.psutil, "Process", MagicMock(return_value=proc))
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: False)
        singleton._clear_stale_backend(19222)
        proc.terminate.assert_called_once()

    def test_no_op_when_nothing_to_kill(self, monkeypatch):
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(singleton, "_backend_pid_on_port", lambda port: None)
        monkeypatch.setattr(singleton, "_read_server_state", lambda: None)
        # Must not raise even when there is nothing on the port.
        singleton._clear_stale_backend(19222)


class TestStartBackendHoldingLockEvicts:
    def test_clears_stale_before_starting(self, monkeypatch):
        @contextmanager
        def fake_lock():
            yield True

        calls = []
        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(
            singleton,
            "_clear_stale_backend",
            lambda port: calls.append(("clear", port)),
        )
        monkeypatch.setattr(
            singleton,
            "_start_server_process",
            lambda port: calls.append(("start", port)),
        )
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)

        singleton._start_backend_holding_lock(19222)

        # Eviction MUST happen before the fresh backend is started, else the new
        # process cannot bind the port and the proxy falls back to the stale one.
        assert calls == [("clear", 19222), ("start", 19222)]

    def test_does_not_clear_when_reusable_backend_appears(self, monkeypatch):
        @contextmanager
        def fake_lock():
            yield True

        calls = []
        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_find_running_server", lambda: 19222)
        monkeypatch.setattr(
            singleton, "_clear_stale_backend", lambda port: calls.append("clear")
        )
        monkeypatch.setattr(
            singleton, "_start_server_process", lambda port: calls.append("start")
        )
        singleton._start_backend_holding_lock(19222)
        assert calls == []  # a same-version backend is already up; leave it alone


@pytest.mark.integration
class TestStaleBackendEvictionEndToEnd:
    """End-to-end proof of issue #14: a REAL version-mismatched backend is
    positively identified and terminated so an upgraded session can bind a fresh
    one on the same port. The unit tests mock psutil; this runs it for real
    against an actual backend process (no browser needed).
    """

    def test_clear_stale_backend_terminates_real_backend(self, tmp_path, monkeypatch):
        import subprocess
        import sys
        import time

        monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
        monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
        monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")

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
            assert singleton._wait_for_server(port), "backend never became healthy"

            # Record it as a STALE (older-version) backend, as a pre-upgrade
            # release would have (here: no version field vs. our real version).
            (tmp_path / "server.json").write_text(
                json.dumps({"port": port, "version": "0.0.0-old", "pid": backend.pid})
            )
            # Discovery must refuse to reuse the stale backend.
            assert singleton._find_running_server() is None

            # The upgraded session evicts it.
            singleton._clear_stale_backend(port)

            for _ in range(50):
                if backend.poll() is not None:
                    break
                time.sleep(0.1)
            assert backend.poll() is not None, "stale backend was not terminated"
            assert not singleton._server_is_healthy(port), "port still bound"
        finally:
            if backend.poll() is None:
                backend.kill()
