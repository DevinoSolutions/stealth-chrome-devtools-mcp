"""F-839: a session-scoped console event must not kill the shared backend.

On 2026-08-30 18:43:57 the healthy backend serving every live session (worker
pid 163320, port 19222) logged ``Received signal 21, initiating cleanup...``
and shut down cleanly. Signal 21 on Windows Python is **SIGBREAK =
CTRL_BREAK_EVENT**, a console control event: it can only arrive through a
shared console (``GenerateConsoleCtrlEvent`` / ``os.kill(pgid,
CTRL_BREAK_EVENT)``). Four proxies then struck out in lockstep and every
attached Claude session disconnected at once.

No product path sends SIGBREAK — eviction and the CLI ``stop``/``restart``
verbs all reach ``psutil.Process.terminate()`` (TerminateProcess), which runs
no handler at all. So the only senders left are session-scoped: a closing
terminal, or a client killing its child process tree on session exit. A
process shared by N sessions had its lifetime tethered to one of them.

The fix is to IGNORE SIGBREAK in the backend, keeping SIGTERM/SIGINT shutdown
semantics (F-809's hand-off) untouched. The pins below are, in order:

* the disposition pin — SIGBREAK installs ``SIG_IGN``, SIGTERM/SIGINT still
  install ``_signal_handler`` (hermetic, and forced to run on POSIX too by
  synthesising ``signal.SIGBREAK``);
* the F-809 idempotency pin — a second install must still not record our own
  disposition as ``previous``;
* the detachment pin — the real spawn asks for
  ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``, the flags that are supposed
  to make a console event unable to reach the backend in the first place;
* the end-to-end console-immunity test — a real backend spawned through the
  real ``singleton._start_server_process``, from a launcher process in its own
  process group, survives a CTRL_BREAK_EVENT delivered to that group. The
  launcher's own death is asserted first, so the event is proven delivered and
  the test cannot pass vacuously.
"""

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import backend_registry, singleton
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

# Ports a real user backend may be listening on right now. The integration test
# binds an ephemeral port and refuses to proceed if it ever lands on one.
FORBIDDEN_PORTS = (19222, 52554)


@pytest.fixture()
def cleanup():
    """A ProcessCleanup built with the house idiom (handler setup stubbed)."""
    with patch.object(ProcessCleanup, "_setup_cleanup_handlers"):
        return ProcessCleanup()


@pytest.fixture()
def restored_signal_dispositions():
    """Snapshot and restore the real dispositions for the tests that install
    them for real. Leaving the test process's handlers mutated would make every
    later Ctrl+C in the same session behave differently."""
    saved = {}
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            saved[signum] = signal.getsignal(signum)
    try:
        yield
    finally:
        for signum, disposition in saved.items():
            if disposition is not None:
                signal.signal(signum, disposition)


# ---------------------------------------------------------------------------
# The disposition pin.
# ---------------------------------------------------------------------------


class TestSigbreakIsIgnoredNotHandled:
    def test_sigbreak_installs_sig_ign_while_sigterm_sigint_shut_down(
        self, cleanup, monkeypatch
    ):
        """Hermetic and platform-neutral: ``SIGBREAK`` is synthesised where it
        does not exist, so the POSIX lane guards the Windows behaviour too (CI
        is ubuntu-only, and this defect is Windows-only)."""
        monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
        installed: dict[int, object] = {}

        with (
            patch("signal.signal", side_effect=lambda s, h: installed.setdefault(s, h)),
            patch("atexit.register"),
        ):
            cleanup._setup_cleanup_handlers()

        assert installed[signal.SIGBREAK] is signal.SIG_IGN
        assert installed[signal.SIGTERM] == cleanup._signal_handler
        assert installed[signal.SIGINT] == cleanup._signal_handler

    @pytest.mark.skipif(
        not hasattr(signal, "SIGBREAK"), reason="SIGBREAK is Windows-only"
    )
    def test_the_real_installed_disposition_is_sig_ign(
        self, cleanup, restored_signal_dispositions
    ):
        """The same claim against the interpreter itself — ``signal.signal`` is
        not mocked here, so a fix that only looks right cannot satisfy it."""
        with patch("atexit.register"):
            cleanup._setup_cleanup_handlers()

        assert signal.getsignal(signal.SIGBREAK) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) == cleanup._signal_handler
        assert signal.getsignal(signal.SIGINT) == cleanup._signal_handler

    def test_a_second_install_still_records_the_original(self, cleanup, monkeypatch):
        """F-809's re-install guard must survive the change: SIGBREAK's recorded
        ``previous`` stays the disposition we displaced, never our own
        ``SIG_IGN`` (runpy double-loads the server module)."""
        monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
        sentinel = object()
        installed: dict[int, object] = {}

        def fake_signal(signum, handler):
            previous = installed.get(signum, sentinel)
            installed[signum] = handler
            return previous

        with patch("signal.signal", side_effect=fake_signal), patch("atexit.register"):
            cleanup._setup_cleanup_handlers()
            cleanup._setup_cleanup_handlers()

        assert cleanup._previous_signal_handlers[signal.SIGBREAK] is sentinel
        assert installed[signal.SIGBREAK] is signal.SIG_IGN


# ---------------------------------------------------------------------------
# The detachment pin — the flags that are supposed to make the event
# unreachable in the first place. SIG_IGN is the second line of defence.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console semantics (F-839)")
def test_the_backend_is_spawned_detached_and_in_its_own_group(tmp_path, monkeypatch):
    """``_start_server_process`` must keep asking for both flags. Hermetic: the
    Popen, the state write and the log dir are all diverted, so the real
    ``~/.stealth-mcp`` record of a live user backend is never touched."""
    monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(singleton, "_ensure_state_dir", lambda: None)
    monkeypatch.setattr(singleton, "_write_server_state", lambda *a, **k: None)
    recorded: dict[str, object] = {}

    class _FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        recorded.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    singleton._start_server_process(51999)

    flags = recorded["creationflags"]
    assert flags & subprocess.DETACHED_PROCESS
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP


# ---------------------------------------------------------------------------
# End-to-end console immunity. Windows only: CTRL_BREAK_EVENT does not exist
# anywhere else, and neither does the defect.
# ---------------------------------------------------------------------------

_LAUNCHER = """\
import sys, time
from stealth_chrome_devtools_mcp.embedded import singleton

singleton._start_server_process(int(sys.argv[1]))
print("SPAWNED", flush=True)
time.sleep(300)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _terminate_tree(pid: int) -> None:
    """Best-effort teardown of everything the test spawned."""
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return
    victims = [*proc.children(recursive=True), proc]
    for victim in victims:
        with contextlib.suppress(psutil.Error):
            victim.terminate()
    psutil.wait_procs(victims, timeout=10)
    for victim in victims:
        with contextlib.suppress(psutil.Error):
            if victim.is_running():
                victim.kill()


def _isolated_env(home: Path, log_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    # Path.home() drives STATE_DIR, and it is read at import time in the child —
    # this is the only way the child's server.json / browser_pids.json stay out
    # of the real ~/.stealth-mcp, where a live user backend is recorded.
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["STEALTH_MCP_LOG_DIR"] = str(log_dir)
    # The spawned backend runs activate() for real (singleton strips
    # NO_AUTO_RECOVERY from the child env, which is the whole point — that is
    # what installs the handlers). Age 0 disables the shared-temp profile sweep
    # so a hermetic test never reaps another process's uc_* directory. The key
    # is unprefixed: that is this field's validation_alias in settings.py.
    env["BROWSER_ORPHAN_PROFILE_MAX_AGE"] = "0"
    return env


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="Windows console semantics (F-839)")
def test_a_console_break_to_another_group_does_not_kill_the_backend(tmp_path):
    port = _free_port()
    assert port not in FORBIDDEN_PORTS, f"ephemeral port collided with {port}"
    home = tmp_path / "home"
    home.mkdir()
    log_dir = tmp_path / "logs"
    launcher_py = tmp_path / "launcher.py"
    launcher_py.write_text(_LAUNCHER, encoding="utf-8")
    out_path = tmp_path / "launcher.out"

    backend_pid = None
    with out_path.open("w", encoding="utf-8") as out:
        launcher = subprocess.Popen(
            [sys.executable, str(launcher_py), str(port)],
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_isolated_env(home, log_dir),
            # Its OWN group, so the break below can never reach this test's.
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            assert launcher.pid not in (0, os.getpid())

            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if singleton._backend_http_ready(port, timeout=2.0):
                    break
                if launcher.poll() is not None:
                    pytest.fail(
                        "launcher died before the backend answered:\n"
                        + out_path.read_text(encoding="utf-8", errors="replace")
                    )
                time.sleep(1.0)
            else:
                pytest.fail("backend never answered an initialize probe")

            # Read the isolated record through its one home, not by hand.
            state = backend_registry.read_record(home / ".stealth-mcp" / "server.json")
            entry = backend_registry.backend_on_port(state, port) or {}
            backend_pid = entry.get("pid")
            assert backend_pid and psutil.pid_exists(backend_pid)

            # Deliver the console event to the LAUNCHER's group only.
            os.kill(launcher.pid, signal.CTRL_BREAK_EVENT)

            # The control: the launcher must actually die FROM the event (it
            # sleeps 300s otherwise, and a console kill is never exit 0).
            # Without this the test would pass whenever the event went nowhere.
            assert launcher.wait(timeout=30) != 0

            time.sleep(3.0)
            assert singleton._backend_http_ready(port, timeout=5.0), (
                "the shared backend died with the console session that spawned "
                "it (F-839): " + _tail_backend_logs(log_dir)
            )
        finally:
            if backend_pid:
                _terminate_tree(backend_pid)
            if launcher.poll() is None:
                _terminate_tree(launcher.pid)


def _tail_backend_logs(log_dir: Path) -> str:
    chunks = []
    for path in sorted(log_dir.glob("backend*.log")):
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks.append(f"--- {path.name} ---\n" + "\n".join(text.splitlines()[-40:]))
    return "\n".join(chunks) or "(no backend logs)"
