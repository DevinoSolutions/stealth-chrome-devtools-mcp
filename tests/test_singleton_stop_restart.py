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
