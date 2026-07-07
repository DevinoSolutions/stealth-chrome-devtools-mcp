"""Pinning tests for M10a-0: DebugLogger.log_debug, a ring-less bridge.

Team-lead ruling (2026-07-06, resolving the M10a-7 DEBUG-level gap): add
``log_debug`` to ``DebugLogger`` but WITHOUT an in-memory ring. A ring would
(a) change ``get_debug_view``'s return shape -- a tool contract that must stay
byte-stable -- and (b) pay lock+append on hot per-event paths even when the
record is dropped, which is exactly what the droppable-by-default DEBUG sites
(plan_M3 §7 risk 3: per-event network/hook handlers) must not do. ``log_debug``
therefore only calls ``stealth.backend``'s stdlib ``.debug(...)`` -- the
logger's own level check drops it for free at the default level -- and keeps
the existing gated stderr echo for consistency with log_error/log_warning/
log_info. No lock is taken: no shared state is mutated.
"""

import logging

from debug_logger import DebugLogger


class TestLogDebugEmitsWhenLevelAllows:
    """Pin (a): log_debug reaches stealth.backend as a DEBUG record when the
    logger's level is opened up (i.e. STEALTH_MCP_LOG_LEVEL=DEBUG)."""

    def test_emits_debug_record_when_logger_level_is_debug(self):
        records = []

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("stealth.backend")
        handler = _ListHandler()
        logger.addHandler(handler)
        prior_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            log = DebugLogger()
            log.log_debug("net", "intercept", "expected fallback taken")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prior_level)

        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG
        assert "expected fallback taken" in records[0].getMessage()


class TestLogDebugDroppedAtDefaultLevel:
    """Pin (b): at the DEFAULT logger level (no explicit DEBUG opt-in), a
    log_debug call produces NO record -- this is the droppable-by-default
    property plan_M3 §7 risk 3 requires for per-event network/hook sites."""

    def test_no_record_at_default_level(self):
        records = []

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("stealth.backend")
        handler = _ListHandler()
        logger.addHandler(handler)
        # Deliberately NOT calling logger.setLevel(DEBUG) here: this is the
        # "default" case. A fresh, un-configured "stealth.backend" logger
        # defaults to NOTSET (effective level falls through to the root
        # logger's WARNING), so DEBUG records must not reach the handler.
        try:
            log = DebugLogger()
            log.log_debug("net", "intercept", "should not appear anywhere")
        finally:
            logger.removeHandler(handler)

        assert records == []


class TestGetDebugViewShapeUnchangedByLogDebug:
    """Pin (c): log_debug keeps no in-memory ring, so get_debug_view's shape
    (keys, counts) is byte-identical whether or not log_debug was ever called
    -- unlike log_error/log_warning/log_info, it contributes no entries."""

    def test_get_debug_view_has_no_debug_keys_or_counts(self):
        log = DebugLogger()
        baseline_view = log.get_debug_view()

        log.log_debug("net", "intercept", "one")
        log.log_debug("net", "intercept", "two")
        log.log_debug("net", "intercept", "three")

        after_view = log.get_debug_view()

        assert after_view == baseline_view
        assert "debug" not in after_view["summary"]
        assert "total_debug" not in after_view["summary"]
        assert "all_debug" not in after_view
        assert "recent_debug" not in after_view


class TestLogDebugStderrEchoGating:
    """Pin (d): the gated stderr echo (shared with log_error/log_warning/
    log_info via _emit_stderr) only fires when enable() has been called --
    log_debug does not bypass that gate."""

    def test_stderr_echo_silent_until_enabled(self, capsys):
        log = DebugLogger()
        log.log_debug("comp", "meth", "quiet-debug")
        assert "quiet-debug" not in capsys.readouterr().err

    def test_stderr_echo_fires_once_enabled(self, capsys):
        log = DebugLogger()
        log.enable()
        capsys.readouterr()  # discard the enable() banner
        log.log_debug("comp", "meth", "loud-debug")
        assert "loud-debug" in capsys.readouterr().err
