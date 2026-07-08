"""Pinning tests for M8 Amendment A1: the F-509 auto-port-fallback.

``_select_backend_port(preferred)`` is the port-selection POLICY consumed
once, synchronously, at the ``ensure_server_running`` boundary (plan_M8
SSA1.3): prefer the port recorded in ``server.json`` (so eviction/restart
land where a prior backend ran), else ``preferred``; keep that target when it
is free or held by OUR OWN backend (eviction rebinds it there); only a
FOREIGN occupant (the ``_port_is_foreign_held`` predicate) forces an
OS-assigned fallback via the one existing port-picker,
``proxy_forwarder._free_port()`` - no new picker, no port-range scan.

HERMETICITY: this is a real developer machine, not a clean CI runner - a
live ``stealth-chrome-devtools-mcp`` backend may genuinely be running (e.g.
a separate, real Claude Code session using this exact server), recorded in
the REAL ``~/.stealth-mcp/server.json`` on a port that is not 19222. So
these tests never bind or probe the literal ``DEFAULT_PORT`` (19222) against
the real network: every squatted-port case binds a throwaway ephemeral port
and passes it as ``preferred``, and the one true "still 19222" regression
guard is pinned via stubs instead of a real socket. ``isolated_state``
redirects ``STATE_DIR``/``SERVER_STATE_FILE``/``PORT_FILE`` into ``tmp_path``
so no test reads or writes the real state file either.
"""

import socket
import threading
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


def _bind_and_listen() -> socket.socket:
    """A real, foreign-by-construction listener on a throwaway ephemeral
    port: this test process's cmdline never satisfies _is_our_backend, so
    _backend_pid_on_port(port) is None for it - exactly the "socket open,
    not ours" shape _port_is_foreign_held checks for. Caller closes it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


class TestSelectBackendPort:
    """(a)-(c): _select_backend_port's three cases, all via existing
    helpers - no new port logic."""

    def test_squatted_preferred_returns_a_different_free_port(self, isolated_state):
        squatter = _bind_and_listen()
        try:
            squatted_port = squatter.getsockname()[1]

            selected = singleton._select_backend_port(squatted_port)

            # No re-probe of `selected` here: _free_port() just handed it
            # out, and probing a released ephemeral port races other
            # processes under machine load (the flake class this file exists
            # to avoid). The pinned property is only "not the squatted port".
            assert selected != squatted_port
        finally:
            squatter.close()

    def test_preferred_free_returns_preferred(self, isolated_state, monkeypatch):
        preferred = _free_closed_port()  # bound then closed: free right now

        # Deterministic "not foreign-held": under machine load another
        # process can rebind a just-released ephemeral port inside the probe
        # window (observed once as a full-suite flake in the sibling
        # stop/restart file). Stub the socket probe; the real-socket foreign
        # case is test_squatted_preferred_returns_a_different_free_port.
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: False)

        selected = singleton._select_backend_port(preferred)

        assert selected == preferred

    def test_default_arg_regression_guard_still_19222(
        self, isolated_state, monkeypatch
    ):
        """Plan_M8 SSA1.6's explicit regression guard ("still 19222"), pinned
        WITHOUT touching the real port 19222 or a real backend that may be
        running on this machine: stub the two probes _select_backend_port
        delegates to, rather than binding the literal port."""
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: False)
        monkeypatch.setattr(singleton, "_backend_pid_on_port", lambda port: None)

        assert singleton._select_backend_port() == singleton.DEFAULT_PORT

    def test_our_own_backend_on_target_keeps_target(self, isolated_state, monkeypatch):
        target = _free_closed_port()
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: True)
        monkeypatch.setattr(singleton, "_backend_pid_on_port", lambda port: 4242)

        selected = singleton._select_backend_port(target)

        assert selected == target


class TestEnsureServerRunningPortFallback:
    """(a), integration half: A1's core design point (SSA1.3) - the SAME
    chosen port must reach both the daemon thread's spawn arg AND
    ensure_server_running's return value (the proxy's connect target),
    because selection runs synchronously at the ensure_server_running
    boundary, not inside the thread."""

    def test_cold_start_thread_and_return_value_agree_on_fallback_port(
        self, isolated_state, monkeypatch
    ):
        squatter = _bind_and_listen()
        try:
            squatted_preferred = squatter.getsockname()[1]
            captured = {}
            got_arg = threading.Event()

            def _fake_cold_start(port):
                captured["port"] = port
                got_arg.set()

            monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
            monkeypatch.setattr(
                singleton, "_start_backend_holding_lock", _fake_cold_start
            )

            returned = singleton.ensure_server_running(port=squatted_preferred)

            assert got_arg.wait(timeout=5), "cold-start thread never ran"
            assert captured["port"] == returned
            assert returned != squatted_preferred  # selection actually fell back
        finally:
            squatter.close()


class TestStartServerProcessRecordsSelectedPort:
    """(d): server.json is the single source of truth for the chosen port
    (SSA1.3 rejected-alternative #4) - the real spawn must record exactly
    the selected port, and the spawned child's own --port argument must
    agree."""

    def test_server_json_and_child_cmd_both_record_the_fallback(
        self, isolated_state, monkeypatch
    ):
        squatter = _bind_and_listen()
        try:
            squatted = squatter.getsockname()[1]
            fallback = singleton._select_backend_port(squatted)
            assert fallback != squatted  # sanity: selection actually fell back

            monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
            fake_proc = MagicMock()
            fake_proc.pid = 4242
            captured_popen = MagicMock(return_value=fake_proc)
            monkeypatch.setattr(singleton.subprocess, "Popen", captured_popen)

            singleton._start_server_process(fallback)

            cmd_args = captured_popen.call_args.args[0]
            assert cmd_args[cmd_args.index("--port") + 1] == str(fallback)
            assert singleton._read_server_state()["port"] == fallback
        finally:
            squatter.close()
