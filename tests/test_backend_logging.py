"""Pinning tests for M3-2: backend boot-time logging wiring.

``bootstrap_backend_process_logging()`` is the single call embedded/server.py's
``__main__`` block makes before anything else (F-303's in-process half): it
installs the ``stealth.backend`` file handler, then a ``sys.excepthook``/
``threading.excepthook`` pair that record a fatal exception before the process
dies, plus ``faulthandler`` for hard/C-level faults that never reach Python's
exception machinery at all.

Tested as a direct unit, not by spawning the real backend subprocess + FastMCP
startup — that class of test is reserved for ``@pytest.mark.integration``
(matches the existing convention, e.g.
``test_singleton_version_aware.py::TestStaleBackendEvictionEndToEnd``), and
would slow every non-integration run for no additional pin strength: the
excepthook/threading.excepthook contract is exercised identically whether or
not FastMCP is actually running.
"""

import faulthandler
import logging
import os
import sys
import threading

import logging_setup
import pytest
from logging_setup import bootstrap_backend_process_logging


def _raise_injected_fatal():
    raise ValueError("injected fatal")


@pytest.fixture(autouse=True)
def _restore_process_globals():
    """Isolate the process-global state this module mutates. Restoring
    faulthandler carefully matters: pytest-timeout also uses it, so leaving it
    repointed at a deleted tmp_path file would break later tests' crash dumps.
    """
    was_fault_enabled = faulthandler.is_enabled()
    prior_excepthook = sys.excepthook
    prior_thread_excepthook = threading.excepthook
    yield
    logger = logging.getLogger("stealth.backend")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    sys.excepthook = prior_excepthook
    threading.excepthook = prior_thread_excepthook
    faulthandler.disable()
    if was_fault_enabled:
        faulthandler.enable()
    logging_setup._bootstrapped_roles.clear()


class TestBootstrapBackendProcessLogging:
    def test_creates_backend_log_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        log_path = bootstrap_backend_process_logging()
        assert log_path == tmp_path / f"backend-{os.getpid()}.log"

    def test_installs_sys_excepthook(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        bootstrap_backend_process_logging()
        assert sys.excepthook is not sys.__excepthook__

    def test_sys_excepthook_records_fatal_exception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        log_path = bootstrap_backend_process_logging()
        try:
            _raise_injected_fatal()
        except ValueError:
            exc_info = sys.exc_info()
        sys.excepthook(*exc_info)
        for handler in logging.getLogger("stealth.backend").handlers:
            handler.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "injected fatal" in text
        assert "ValueError" in text

    def test_installs_threading_excepthook(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        bootstrap_backend_process_logging()
        assert threading.excepthook is not threading.__excepthook__

    def test_threading_excepthook_records_fatal_exception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        log_path = bootstrap_backend_process_logging()

        def _boom():
            raise RuntimeError("thread blew up")

        t = threading.Thread(target=_boom, name="injected-fatal-thread")
        t.start()
        t.join()
        for handler in logging.getLogger("stealth.backend").handlers:
            handler.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "thread blew up" in text
        assert "injected-fatal-thread" in text

    def test_enables_faulthandler(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        bootstrap_backend_process_logging()
        assert faulthandler.is_enabled()

    def test_idempotent_across_repeated_calls(self, tmp_path, monkeypatch):
        # embedded/server.py can be loaded twice via runpy; a second call must
        # not raise or stack a second excepthook layer indefinitely.
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        bootstrap_backend_process_logging()
        bootstrap_backend_process_logging()  # must not raise
        assert len(logging.getLogger("stealth.backend").handlers) == 1
