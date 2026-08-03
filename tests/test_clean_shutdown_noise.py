"""F-809: a clean backend stop must produce no ERROR-level noise.

Sentry issues ``-1J`` / ``-1H``: on POSIX every graceful stop
(``stealth-chrome-devtools stop``, or a singleton eviction — both reach
``singleton._terminate_backend`` → SIGTERM) shipped 1-3 ERROR events, because

* ``process_cleanup`` installed its signal handler *after* uvicorn's
  ``capture_signals`` installed ``handle_exit``, **replacing** it — so
  ``should_exit`` was never set and uvicorn's graceful path was dead code, and
  the handler's ``sys.exit(0)`` unwound ``run_forever`` from inside ``select()``
  (``"Exception in 'lifespan' protocol"`` / ``"Application shutdown failed."`` /
  ``"Session … crashed"``); and
* FastMCP 2.11.2 hard-codes ``timeout_graceful_shutdown: 0``, and
  ``asyncio.wait_for(coro, 0)`` always raises on CPython 3.12, so uvicorn logged
  ``"Cancel N running task(s), timeout graceful shutdown exceeded"`` at ERROR on
  every graceful HTTP stop.

The fixes are a signal **hand-off** and a positive graceful-shutdown timeout —
never log filtering, which would also hide a genuinely failed shutdown. The
end-to-end pin below therefore asserts the *positive* marker
``Application shutdown complete``, so no suppression-shaped "fix" can satisfy it.
"""

import ast
import os
import re
import runpy
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from stealth_chrome_devtools_mcp import server as shim
from stealth_chrome_devtools_mcp.embedded import server
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

# ---------------------------------------------------------------------------
# N1 + N2 — the signal hand-off (hermetic: no real handler is ever installed
# except in the one test that fakes ``signal.signal`` outright).
# ---------------------------------------------------------------------------


@pytest.fixture()
def cleanup():
    """A ProcessCleanup built with the house idiom (handler setup stubbed)."""
    with patch.object(ProcessCleanup, "_setup_cleanup_handlers"):
        return ProcessCleanup()


class TestSetupRecordsDisplacedHandlers:
    def test_setup_records_the_handler_it_displaces(self, cleanup):
        """``signal.signal`` returns the disposition it replaces — that return
        value is the only way back to uvicorn's ``handle_exit``."""
        displaced: dict[int, object] = {}
        installed: dict[int, object] = {}

        def fake_signal(signum, handler):
            installed[signum] = handler
            return displaced.setdefault(signum, f"previous-{signum}")

        with (
            patch("signal.signal", side_effect=fake_signal),
            patch("atexit.register") as mock_register,
        ):
            cleanup._setup_cleanup_handlers()

        mock_register.assert_called_once_with(cleanup._cleanup_all_tracked)
        assert installed, "no signal disposition was installed at all"
        assert set(installed) == set(cleanup._previous_signal_handlers)
        for signum, previous in displaced.items():
            assert cleanup._previous_signal_handlers[signum] == previous
        for handler in installed.values():
            assert handler == cleanup._signal_handler

    def test_setup_still_covers_sigterm_and_sigint(self, cleanup):
        installed: list[int] = []

        with (
            patch("signal.signal", side_effect=lambda s, h: installed.append(s)),
            patch("atexit.register"),
        ):
            cleanup._setup_cleanup_handlers()

        assert signal.SIGTERM in installed
        assert signal.SIGINT in installed

    def test_a_second_install_never_records_our_own_handler(self, cleanup):
        """Installing twice in one process is a live shape here (the server
        module is loaded twice under runpy). Without the guard the second
        install records OUR handler as ``previous``, and ``_signal_handler``
        then delegates to itself — unbounded recursion on the first signal."""
        installed: dict[int, object] = {}

        def fake_signal(signum, handler):
            previous = installed.get(signum, signal.SIG_DFL)
            installed[signum] = handler
            return previous

        with (
            patch("signal.signal", side_effect=fake_signal),
            patch("atexit.register"),
        ):
            cleanup._setup_cleanup_handlers()
            cleanup._setup_cleanup_handlers()

        assert cleanup._previous_signal_handlers
        for previous in cleanup._previous_signal_handlers.values():
            assert previous != cleanup._signal_handler
            assert previous == signal.SIG_DFL


class TestSignalHandoff:
    def test_delegates_to_the_displaced_handler_and_does_not_exit(self, cleanup):
        """The whole point: uvicorn's ``handle_exit`` must still run, and the
        handler must RETURN into the interrupted frame so uvicorn's own graceful
        shutdown unwinds the loop normally."""
        calls: list[str] = []
        cleanup._previous_signal_handlers[signal.SIGTERM] = lambda signum, frame: (
            calls.append("delegate")
        )

        with patch.object(
            ProcessCleanup,
            "_cleanup_all_tracked",
            lambda self: calls.append("cleanup"),
        ):
            # No pytest.raises: a SystemExit escaping here IS the bug.
            cleanup._signal_handler(signal.SIGTERM, None)

        # Delegate first — it is a cheap flag set, and a slow or failing cleanup
        # must never strand the server with should_exit unset.
        assert calls == ["delegate", "cleanup"]

    @pytest.mark.parametrize(
        "previous",
        [signal.SIG_DFL, signal.SIG_IGN, None],
        ids=["sig_dfl", "sig_ign", "nothing_recorded"],
    )
    def test_no_graceful_owner_keeps_the_1x_exit(self, cleanup, previous):
        """Standalone stdio has no server loop to hand back to: keep the 1.x
        ``cleanup(); sys.exit(0)`` behaviour verbatim."""
        calls: list[str] = []
        if previous is not None:
            cleanup._previous_signal_handlers[signal.SIGTERM] = previous

        with patch.object(
            ProcessCleanup,
            "_cleanup_all_tracked",
            lambda self: calls.append("cleanup"),
        ):
            with pytest.raises(SystemExit) as excinfo:
                cleanup._signal_handler(signal.SIGTERM, None)

        assert excinfo.value.code == 0
        assert calls == ["cleanup"]

    def test_default_int_handler_is_not_delegated_to(self, cleanup):
        """SIGINT's prior disposition under stdio *is* ``default_int_handler``.
        Delegating would swap today's clean exit 0 for a KeyboardInterrupt
        unwind, so it must be excluded explicitly."""
        calls: list[str] = []
        cleanup._previous_signal_handlers[signal.SIGINT] = signal.default_int_handler

        with patch.object(
            ProcessCleanup,
            "_cleanup_all_tracked",
            lambda self: calls.append("cleanup"),
        ):
            with pytest.raises(SystemExit) as excinfo:
                cleanup._signal_handler(signal.SIGINT, None)

        assert excinfo.value.code == 0
        assert calls == ["cleanup"]

    def test_cleanup_runs_once_across_repeated_signals(self, cleanup):
        """A second SIGTERM arriving mid-``rmtree`` must not re-enter the
        cleanup — but it must still reach uvicorn (its force-quit path)."""
        calls: list[str] = []
        cleanup._previous_signal_handlers[signal.SIGTERM] = lambda signum, frame: (
            calls.append("delegate")
        )

        with patch.object(
            ProcessCleanup,
            "_cleanup_all_tracked",
            lambda self: calls.append("cleanup"),
        ):
            cleanup._signal_handler(signal.SIGTERM, None)
            cleanup._signal_handler(signal.SIGTERM, None)

        assert calls.count("cleanup") == 1
        assert calls.count("delegate") == 2

    def test_a_raising_cleanup_still_hands_the_signal_off(self, cleanup):
        """A cleanup failure must be logged, not propagated out of a signal
        handler — and must not cost the server its graceful stop."""
        calls: list[str] = []
        cleanup._previous_signal_handlers[signal.SIGTERM] = lambda signum, frame: (
            calls.append("delegate")
        )

        def boom(self):
            raise RuntimeError("cleanup exploded")

        with (
            patch.object(ProcessCleanup, "_cleanup_all_tracked", boom),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup"
                ".debug_logger.log_error"
            ) as mock_log_error,
        ):
            cleanup._signal_handler(signal.SIGTERM, None)

        assert calls == ["delegate"]
        assert mock_log_error.call_count == 1


class TestInitCarriesTheNewState:
    def test_init_starts_with_no_recorded_handlers(self):
        with patch.object(ProcessCleanup, "_setup_cleanup_handlers"):
            pc = ProcessCleanup()
        assert pc._previous_signal_handlers == {}
        assert pc._shutdown_in_progress is False


# ---------------------------------------------------------------------------
# N3 — the graceful-shutdown timeout FastMCP hard-codes to 0.
# ---------------------------------------------------------------------------

_SERVER_SOURCE = Path(server.__file__)


def _http_mcp_run_call() -> ast.Call:
    """The ``mcp.run(transport="http", ...)`` call in the ``__main__`` block.

    Source-level because the call lives under ``if __name__ == "__main__":`` and
    is unreachable by import.
    """
    tree = ast.parse(_SERVER_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "run"):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "transport"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == "http"
            ):
                return node
    raise AssertionError("no mcp.run(transport='http', ...) call found in server.py")


class TestGracefulShutdownTimeout:
    def test_http_run_passes_a_graceful_shutdown_timeout(self):
        call = _http_mcp_run_call()
        uvicorn_config = next(
            (kw.value for kw in call.keywords if kw.arg == "uvicorn_config"), None
        )
        assert uvicorn_config is not None, (
            "mcp.run(transport='http') must pass uvicorn_config — FastMCP "
            "hard-codes timeout_graceful_shutdown=0, which makes uvicorn ERROR-log "
            "'timeout graceful shutdown exceeded' on every clean stop (F-809)"
        )
        assert isinstance(uvicorn_config, ast.Dict)
        keys = [k.value for k in uvicorn_config.keys if isinstance(k, ast.Constant)]
        assert "timeout_graceful_shutdown" in keys

    def test_the_graceful_shutdown_timeout_is_positive_and_bounded(self):
        """Positivity, not the literal value, is the contract — tuning the
        number must not need a golden update. ``None`` is uvicorn's "wait
        forever", which an open SSE stream would ride all the way to SIGKILL.
        The ceiling is ``singleton._terminate_backend``'s 5 s wait window."""
        timeout = server._GRACEFUL_SHUTDOWN_SECONDS
        assert isinstance(timeout, (int, float))
        assert timeout > 0
        assert timeout < 5


# ---------------------------------------------------------------------------
# N4 — Ctrl+C must not escape the top-level shim as a KeyboardInterrupt.
#
# The shutdown itself is already clean by then: uvicorn's ``capture_signals``
# restores the pre-serve dispositions on the way out and re-raises every signal
# it captured, so SIGINT arrives at Python's interrupt handler AFTER
# "Application shutdown complete". Unhandled it reaches ``sys.excepthook``, and
# Sentry's ExcepthookIntegration ships one unhandled-error event per Ctrl+C.
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_argv(monkeypatch):
    """argv that routes ``main()`` past the stdio-proxy branch to ``runpy``."""
    monkeypatch.setattr(
        sys, "argv", ["stealth-chrome-devtools-mcp", "--transport", "http"]
    )


def _must_not_run(*args, **kwargs):
    raise AssertionError("the wrong branch of main() ran")


class TestCtrlCDoesNotEscapeTheShim:
    """``__main__.py``, the ``stealth-chrome-devtools-mcp`` console script and
    ``stealth-chrome-devtools serve`` all run this one ``main()``. Both of its
    exits are covered — the ``runpy`` branch AND the stdio-proxy branch, which
    returns before ``runpy`` is ever reached."""

    def test_keyboard_interrupt_becomes_a_quiet_systemexit_130(
        self, monkeypatch, http_argv
    ):
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(runpy, "run_path", interrupted)

        # A KeyboardInterrupt escaping here IS the bug: it is a BaseException,
        # so pytest.raises(SystemExit) would not swallow it.
        with pytest.raises(SystemExit) as excinfo:
            shim.main()

        assert excinfo.value.code == 130

    def test_a_ctrl_c_in_the_stdio_proxy_is_quiet_too(self, monkeypatch):
        """``serve`` with no ``--http`` is the DEFAULT verb, it never reaches
        ``runpy``, and Ctrl+C is the only way to stop a foreground serve — so
        this is the likeliest interrupt in the product, not an edge case."""
        from stealth_chrome_devtools_mcp.embedded import singleton

        def interrupted(port):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            sys, "argv", ["stealth-chrome-devtools-mcp", "--transport", "stdio"]
        )
        monkeypatch.setattr(singleton, "ensure_server_running", lambda port: port)
        monkeypatch.setattr(singleton, "run_stdio_proxy", interrupted)
        monkeypatch.setattr(runpy, "run_path", _must_not_run)

        with pytest.raises(SystemExit) as excinfo:
            shim.main()

        assert excinfo.value.code == 130

    def test_a_normal_run_is_unchanged(self, monkeypatch, http_argv):
        calls: list[tuple] = []
        monkeypatch.setattr(runpy, "run_path", lambda *a, **k: calls.append((a, k)))

        assert shim.main() is None
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# End-to-end: SIGTERM a real HTTP backend and read its log. POSIX only —
# Windows' Popen.terminate() is TerminateProcess and runs no handler at all,
# which is why 1J/1H are Linux-only in the first place.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics (F-809)")
def test_sigterm_on_a_real_backend_is_a_clean_shutdown(tmp_path):
    port = _free_port()
    log_path = tmp_path / "backend.log"
    env = dict(os.environ)
    # Path.home() drives STATE_DIR — keep the child out of the real ~/.stealth-mcp.
    env["HOME"] = str(tmp_path)
    # conftest setdefaults this to "1" and the child inherits it; activate()
    # would then return BEFORE installing any handler, uvicorn's would survive,
    # and this test would pass for entirely the wrong reason. Production is
    # unaffected: singleton._start_backend_holding_lock pops the key outright.
    env["STEALTH_MCP_NO_AUTO_RECOVERY"] = "0"

    cmd = [
        sys.executable,
        "-m",
        "stealth_chrome_devtools_mcp",
        "--transport",
        "http",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]

    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT, stdin=None, env=env
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if "Application startup complete" in log_path.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
                if proc.poll() is not None:
                    pytest.fail(
                        "backend died during startup:\n"
                        + log_path.read_text(encoding="utf-8", errors="replace")
                    )
                time.sleep(0.25)
            else:
                pytest.fail("backend never reported startup complete")

            proc.terminate()  # SIGTERM — exactly what _terminate_backend sends
            returncode = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    log = log_path.read_text(encoding="utf-8", errors="replace")
    # Two clean codes, not one. uvicorn 0.35's ``capture_signals`` restores the
    # pre-serve dispositions and then ``signal.raise_signal``s every signal
    # ``handle_exit`` captured — against SIG_DFL, so a delegated SIGTERM leaves
    # the process terminated BY SIGTERM (-15), which is the conventional and
    # correct outcome for a signalled server. (The spec draft's flat "exit 0"
    # predates that check; its own §3 cites this same re-raise as the reason the
    # drop-our-handler shape was rejected.) Exit 0 stays valid for a build where
    # nothing re-raises. Anything else — 1, 3, -SIGKILL — is a real failure.
    assert returncode in (0, -signal.SIGTERM), f"unclean exit {returncode}:\n{log}"
    # The POSITIVE marker: this is what makes the pin immune to a fix that
    # merely silences the ERROR records instead of shutting down cleanly.
    assert "Application shutdown complete" in log, log
    assert not re.findall(r"(?m)^(?:ERROR|CRITICAL)", log), log
    assert "Traceback" not in log, log
