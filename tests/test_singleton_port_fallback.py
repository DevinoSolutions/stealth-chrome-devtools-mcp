"""Pinning tests for M8 Amendment A1: the F-509 auto-port-fallback.

``_select_backend_port(preferred)`` is the port-selection POLICY consumed
once, synchronously, at the ``ensure_server_running`` boundary (plan_M8
SSA1.3): prefer the port recorded in ``server.json`` (so eviction/restart
land where a prior backend ran), else ``preferred``; keep that target when it
is free or held by OUR OWN backend (eviction rebinds it there); a FOREIGN
occupant (the ``_port_is_foreign_held`` predicate) - or a target the OS
FORBIDS us outright (``proxy_forwarder._port_is_forbidden``, the field
residual pinned by this file's second half) - forces an OS-assigned fallback.

Both routes leave through ``proxy_forwarder.bindable_port``, which owns the
port-ACQUISITION half (and the existing ``_free_port`` picker it delegates
to); selection POLICY stays whole in ``_select_backend_port``, which passes
its verdict down as ``force_new``. The split also keeps singleton.py at its
1000-LOC budget (tools/check_file_budgets.py), which it already sat exactly
on: this fix cost that file a net zero lines.

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

import logging
import socket
import threading
from unittest.mock import MagicMock

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    proxy_forwarder,
    singleton,
)


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


def _bind_without_listen() -> socket.socket:
    """A port that is OCCUPIED while looking perfectly free: bound, never
    listened on, so nothing accepts a connection (every probe in singleton.py
    reads "free, nothing there") yet a second bind fails with EADDRINUSE.
    Caller closes it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock


def _forbid_binds_on(monkeypatch, forbidden_port: int) -> None:
    """Make ``forbidden_port`` behave like a Windows RESERVED port, and only
    that port.

    Injection is the only way to express this case: a reserved range cannot
    be created without admin on Windows and does not exist at all on
    Linux/macOS, so no real socket can produce the verdict. The raised
    exception is the field one - ``PermissionError``/errno 13 is exactly what
    the Sentry event shows Python surfacing for WinError 10013.

    Scoped to one port BY DESIGN: `socket.socket` is what the fallback picker
    (`proxy_forwarder._free_port`) uses to obtain the replacement port, so a
    blanket stub would forbid the cure along with the disease.
    """
    real_socket = socket.socket

    class _ReservedPortSocket(real_socket):
        def bind(self, address):
            if address[1] == forbidden_port:
                raise PermissionError(
                    13,
                    "an attempt was made to access a socket in a way "
                    "forbidden by its access permissions",
                )
            return super().bind(address)

    monkeypatch.setattr(socket, "socket", _ReservedPortSocket)


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
        running on this machine: stub the probes _select_backend_port
        delegates to, rather than binding the literal port.

        THREE probes now, not two: `_port_is_forbidden` is the one that
        really binds, so leaving it live would aim a real bind at the real
        19222 on whatever machine runs this - the exact cross-talk the
        module docstring's hermeticity rule exists to prevent."""
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: False)
        monkeypatch.setattr(singleton, "_backend_pid_on_port", lambda port: None)
        monkeypatch.setattr(proxy_forwarder, "_port_is_forbidden", lambda port: False)

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
            # SOFT golden updated with F-808's schema v2 (same commit): the
            # claim is unchanged - server.json is the single source of truth
            # for the chosen port - but the port now lives on a recorded
            # backend entry rather than at the record's top level.
            recorded = backend_registry.first_backend(singleton._read_server_state())
            assert recorded["port"] == fallback
        finally:
            squatter.close()


class TestForbiddenTargetFallsBack:
    """F-509's residual, from the field: Sentry
    STEALTH-CHROME-DEVTOOLS-MCP-2J, 2 events on an EXTERNAL user's Windows
    box, release 2.0.6 -

        [Errno 13] error while attempting to bind on address
        ('127.0.0.1', 19222): [winerror 10013] an attempt was made to
        access a socket in a way forbidden by its access permissions

    19222 fell inside a Windows EXCLUDED port range (Hyper-V/WinNAT reserve
    ranges; `netsh interface ipv4 show excludedportrange protocol=tcp`).
    Nothing was listening there, so `_port_is_foreign_held` - a CONNECT
    probe - correctly read "free", selection kept 19222, and uvicorn then
    died on bind() inside the child, every single start.

    The trigger is a PERMISSION verdict only, never "in use": see
    TestOccupiedIsNotForbidden below for why that distinction is
    load-bearing rather than pedantic.
    """

    def test_a_forbidden_target_is_not_selected(self, isolated_state, monkeypatch):
        target = _free_closed_port()
        _forbid_binds_on(monkeypatch, target)

        assert singleton._select_backend_port(target) != target

    def test_the_fallback_is_logged_with_both_ports(
        self, isolated_state, monkeypatch, caplog
    ):
        target = _free_closed_port()
        _forbid_binds_on(monkeypatch, target)

        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            selected = singleton._select_backend_port(target)

        logged = "\n".join(
            r.getMessage() for r in caplog.records if r.name == "stealth.proxy"
        )
        assert str(target) in logged
        assert str(selected) in logged

    def test_a_permitted_target_is_kept(self, isolated_state, monkeypatch):
        """The common case, unchanged: nothing foreign there, the OS permits
        the port -> that port, no fallback, no warning."""
        preferred = _free_closed_port()
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: False)

        assert singleton._select_backend_port(preferred) == preferred

    def test_the_predicate_answers_true_only_for_a_forbidden_port(self, monkeypatch):
        free_port = _free_closed_port()
        assert not proxy_forwarder._port_is_forbidden(free_port)

        _forbid_binds_on(monkeypatch, free_port)
        assert proxy_forwarder._port_is_forbidden(free_port)


class TestOccupiedIsNotForbidden:
    """THE regression guard for how this fix was first written wrong.

    A first cut asked "can I bind this?" and treated ANY bind failure as
    disqualifying. That reads as strictly safer and is not: every proxy in a
    startup herd runs this selection at once, so the probes collide with EACH
    OTHER's momentary probe sockets, and eleven of twelve sessions "fall
    back" to private ports that nothing will ever bind - then poll them for
    the full 120s BACKEND_READY_TIMEOUT. Measured, not theorised:
    tests/test_startup_herd.py went from 23s green to a 240s hard timeout,
    and back to green once the predicate was narrowed to PermissionError.

    The same conflation would divert a healthy restart off our OWN backend's
    port, which is legitimately in use by us (SSA1.5).
    """

    def test_a_port_bound_without_a_listener_is_not_forbidden(self):
        holder = _bind_without_listen()
        try:
            occupied = holder.getsockname()[1]
            assert not singleton._server_is_healthy(occupied), (
                "precondition: an unlistened port looks FREE to the connect probe"
            )

            assert not proxy_forwarder._port_is_forbidden(occupied)
        finally:
            holder.close()

    def test_a_port_with_a_live_listener_is_not_forbidden(self):
        listener = _bind_and_listen()
        try:
            assert not proxy_forwarder._port_is_forbidden(listener.getsockname()[1])
        finally:
            listener.close()

    def test_our_own_backend_keeps_its_port_although_it_holds_it(
        self, isolated_state, monkeypatch
    ):
        """End-to-end through the selector, on a REALLY bound port: ours, so
        not foreign, and occupied-not-forbidden, so kept."""
        listener = _bind_and_listen()
        try:
            ours = listener.getsockname()[1]
            monkeypatch.setattr(singleton, "_backend_pid_on_port", lambda port: 4242)

            assert singleton._select_backend_port(ours) == ours
        finally:
            listener.close()


class TestForbiddenFallbackReachesTheSpawn:
    """The chosen port must flow everywhere the default would have. Same
    claim as TestStartServerProcessRecordsSelectedPort above, for the
    forbidden-target cause rather than the foreign-squatter one."""

    def test_child_argv_and_server_json_both_get_the_fallback(
        self, isolated_state, monkeypatch
    ):
        forbidden = _free_closed_port()
        with monkeypatch.context() as reserved:
            _forbid_binds_on(reserved, forbidden)
            fallback = singleton._select_backend_port(forbidden)
        assert fallback != forbidden

        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        fake_proc = MagicMock()
        fake_proc.pid = 4242
        captured_popen = MagicMock(return_value=fake_proc)
        monkeypatch.setattr(singleton.subprocess, "Popen", captured_popen)

        singleton._start_server_process(fallback)

        cmd_args = captured_popen.call_args.args[0]
        assert cmd_args[cmd_args.index("--port") + 1] == str(fallback)
        recorded = backend_registry.first_backend(singleton._read_server_state())
        assert recorded["port"] == fallback

    def test_cold_start_thread_and_return_value_agree_on_the_fallback(
        self, isolated_state, monkeypatch
    ):
        """ensure_server_running's return value is the proxy's connect
        target; the thread's arg is the spawn port. A forbidden default must
        move BOTH, in lock-step, or the proxy dials a port nothing will ever
        bind for the full 120s BACKEND_READY_TIMEOUT."""
        forbidden = _free_closed_port()
        _forbid_binds_on(monkeypatch, forbidden)
        captured = {}
        got_arg = threading.Event()

        def _fake_cold_start(port):
            captured["port"] = port
            got_arg.set()

        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(singleton, "_start_backend_holding_lock", _fake_cold_start)

        returned = singleton.ensure_server_running(port=forbidden)

        assert got_arg.wait(timeout=5), "cold-start thread never ran"
        assert captured["port"] == returned
        assert returned != forbidden
