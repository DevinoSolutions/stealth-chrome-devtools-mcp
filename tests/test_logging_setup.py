"""Pinning tests for the M3 observability file-logging module.

``logging_setup.py`` is the ONE place log-WRITING configuration lives:
handlers, formatters, rotation, correlation-id stamping, log-dir resolution,
and pruning (``observability.py`` is the separate error-SHIPPING home and is
untouched by this module). These tests pin: a log file is created and
receives records, the installer is idempotent (no duplicate handler on
re-call), the ``STEALTH_MCP_LOG_DIR`` override is honored, and
``CorrelationIdFilter`` stamps the active ``correlation_id_var`` onto every
record.
"""

import logging
import os

import pytest

from stealth_chrome_devtools_mcp.embedded import singleton
from stealth_chrome_devtools_mcp.embedded.logging_setup import (
    configure_logging,
    correlation_id_var,
    new_correlation_id,
    prune_old_logs,
    resolve_log_dir,
)


@pytest.fixture(autouse=True)
def _cleanup_stealth_loggers():
    """Every test starts clean and leaves no open file handle behind (an open
    ``RotatingFileHandler`` would lock its file and break Windows tmp_path
    cleanup)."""
    yield
    manager = logging.Logger.manager
    for name in list(manager.loggerDict):
        if name.startswith("stealth."):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()


class TestResolveLogDir:
    def test_uses_state_dir_convention_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STEALTH_MCP_LOG_DIR", raising=False)
        monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
        assert resolve_log_dir() == tmp_path / "logs"

    def test_respects_env_override(self, tmp_path, monkeypatch):
        override = tmp_path / "custom-logs"
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(override))
        assert resolve_log_dir() == override

    def test_is_pure_never_creates_directory(self, tmp_path, monkeypatch):
        override = tmp_path / "not-yet-created"
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(override))
        resolve_log_dir()
        assert not override.exists()


class TestConfigureLogging:
    def test_creates_log_file_with_pid_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        log_path = configure_logging("backend")
        assert log_path == tmp_path / f"backend-{os.getpid()}.log"

    def test_emits_info_line_that_lands_in_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        log_path = configure_logging("backend")
        logging.getLogger("stealth.backend").info("hello from the pinning test")
        for handler in logging.getLogger("stealth.backend").handlers:
            handler.flush()
        assert "hello from the pinning test" in log_path.read_text(encoding="utf-8")

    def test_idempotent_no_second_handler_on_recall(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        configure_logging("backend")
        first_count = len(logging.getLogger("stealth.backend").handlers)
        configure_logging("backend")
        second_count = len(logging.getLogger("stealth.backend").handlers)
        assert first_count == 1
        assert second_count == 1

    def test_recall_does_not_replace_handler_object(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        configure_logging("proxy-idem")
        before = logging.getLogger("stealth.proxy-idem").handlers[0]
        configure_logging("proxy-idem")
        after = logging.getLogger("stealth.proxy-idem").handlers[0]
        assert before is after

    def test_never_raises_when_log_dir_unwritable(self, tmp_path, monkeypatch):
        # A file where a directory is expected makes mkdir(parents=True) raise
        # OSError/NotADirectoryError — configure_logging must degrade silently
        # (plan_M3 risk #7: logging setup must never take down the process).
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(blocker / "logs"))
        configure_logging("backend")  # must not raise


class TestCorrelationIdFilter:
    def test_filter_stamps_current_context_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
        configure_logging("correlation-test")
        token = correlation_id_var.set("abc123")
        try:
            logging.getLogger("stealth.correlation-test").info("stamped line")
        finally:
            correlation_id_var.reset(token)
        for handler in logging.getLogger("stealth.correlation-test").handlers:
            handler.flush()
        log_path = tmp_path / f"correlation-test-{os.getpid()}.log"
        assert "[abc123]" in log_path.read_text(encoding="utf-8")

    def test_default_correlation_id_is_dash(self):
        assert correlation_id_var.get() == "-"

    def test_new_correlation_id_returns_distinct_short_ids(self):
        a = new_correlation_id()
        b = new_correlation_id()
        assert a != b
        assert len(a) <= 16  # short, not a full UUID


class TestPruneOldLogs:
    def test_prune_caps_file_count(self, tmp_path):
        for i in range(5):
            (tmp_path / f"backend-{i}.log").write_text("x")
        prune_old_logs(tmp_path, keep_days=7, keep_files=2)
        remaining = list(tmp_path.glob("*.log"))
        assert len(remaining) <= 2

    def test_prune_never_raises_on_missing_dir(self, tmp_path):
        prune_old_logs(tmp_path / "does-not-exist", keep_days=7, keep_files=2)
