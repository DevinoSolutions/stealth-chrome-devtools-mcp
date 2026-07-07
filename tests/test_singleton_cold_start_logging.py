"""Pinning tests for M3-6's F-183 cold-start tracing.

Before this change, ``_start_backend_holding_lock``'s ``except Exception:
pass`` swallowed every cold-start failure with no trace anywhere (F-183's
primary handler) - control flow is unchanged (M10a's rule: add a log line,
leave the sentinel), so the pin is that the failure now reaches
``proxy-<pid>.log``.
"""

import logging
import os
from contextlib import contextmanager

import pytest
import singleton


@pytest.fixture()
def isolated_proxy_log(tmp_path, monkeypatch):
    monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path))
    logger = logging.getLogger("stealth.proxy")
    prior_propagate = logger.propagate
    yield tmp_path
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = prior_propagate  # configure_logging() sets it False


@pytest.fixture()
def captured_proxy_records():
    """Direct handler attachment - works regardless of propagate, unlike
    caplog (which relies on propagation to root and is therefore the wrong
    tool once configure_logging has set propagate=False - by design, to
    avoid double-handled lines)."""
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("stealth.proxy")
    handler = _ListHandler()
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


class TestColdStartLogging:
    def test_coldstart_failure_is_logged(self, isolated_proxy_log, monkeypatch):
        from logging_setup import configure_logging

        configure_logging("proxy")

        @contextmanager
        def fake_lock():
            yield True

        def _boom(port):
            raise RuntimeError("injected cold-start failure for this test")

        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(singleton, "_clear_stale_backend", _boom)

        singleton._start_backend_holding_lock(
            19999
        )  # must not raise (still best-effort)

        proxy_log = isolated_proxy_log / f"proxy-{os.getpid()}.log"
        assert proxy_log.exists()
        text = proxy_log.read_text(encoding="utf-8")
        assert "injected cold-start failure for this test" in text
        assert "RuntimeError" in text
        assert "backend cold start failed" in text

    def test_server_version_fallback_logs_at_debug(
        self, captured_proxy_records, monkeypatch
    ):
        def _fake_version(name):
            raise ModuleNotFoundError("no importlib.metadata for this test")

        monkeypatch.setattr("importlib.metadata.version", _fake_version)

        result = singleton._server_version()

        assert result == "0.0.0"
        assert any(
            "resolve installed package version" in record.getMessage()
            for record in captured_proxy_records
        )
