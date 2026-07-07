"""Pinning tests for M3-4: unconditional recording + stealth.backend file
bridge, plus Amendment A1's lock-reentrancy fix.

Before this change, ``DebugLogger.log_error``/``log_warning``/``log_info``
were no-ops unless ``.enable()`` had been called (F-182: the default install
is a black box). After: recording is ALWAYS on; ``_enabled`` now governs only
the legacy ``_emit_stderr`` echo (pinned in ``test_debug_logger.py``'s
rewritten ``TestEnableGating``/new ``TestStderrEchoGating`` — not repeated
here). Every record is also emitted to ``logging.getLogger("stealth.backend")``
so the durable file gets it regardless of whether any debug tool is ever
called (F-304): the diagnostic data is no longer trapped behind the tools
that hang with a wedged backend. The file emit is NOT deduped — every
occurrence reaches disk even when the in-memory ring collapses repeats
(F-204's cross-session-suppression harm is neutralized at this layer).

Amendment A1 (F-764): ``export_to_file_paginated`` acquires the (previously
non-reentrant) ``self._lock`` then calls ``get_debug_view_paginated``, which
re-enters ``with self._lock:`` — an unconditional self-deadlock. This MUST
land in this same step because Step 4's unconditional recording would
otherwise widen the deadlock to every ``log_*`` call once something is
holding the lock via ``export_to_file_paginated``. Fixed by
``Lock`` -> ``RLock`` (lock-type + test only; scope-limited per the plan).
"""

import logging
import threading

import pytest
from debug_logger import DebugLogger


@pytest.fixture()
def captured_backend_records():
    """Capture stealth.backend records without touching real handlers/files."""
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
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


class TestBackendFileBridge:
    def test_log_error_emits_to_stealth_backend_without_enable(
        self, captured_backend_records
    ):
        log = DebugLogger()  # not enabled
        log.log_error("api", "call", ValueError("boom"))
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.ERROR
        assert "boom" in record.getMessage()

    def test_log_warning_emits_to_stealth_backend_without_enable(
        self, captured_backend_records
    ):
        log = DebugLogger()
        log.log_warning("api", "call", "slow")
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.WARNING
        assert "slow" in record.getMessage()

    def test_log_info_emits_to_stealth_backend_without_enable(
        self, captured_backend_records
    ):
        log = DebugLogger()
        log.log_info("api", "call", "tick")
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.INFO
        assert "tick" in record.getMessage()

    def test_deduped_error_still_emits_every_occurrence_to_stealth_backend(
        self, captured_backend_records
    ):
        # The in-memory ring collapses repeats (dedup); the durable file emit
        # must NOT — every session's every error reaches disk regardless of
        # in-memory suppression (plan_M3 F-204 rationale).
        log = DebugLogger()
        log.log_error("api", "call", ValueError("same"))
        log.log_error("api", "call", ValueError("same"))
        log.log_error("api", "call", ValueError("same"))
        assert len(captured_backend_records) == 3
        assert log.get_debug_view()["summary"]["total_errors"] == 1  # ring: deduped
        assert log.get_debug_view()["summary"]["stats"]["api.call.errors"] == 3


class TestLockReentrancy:
    """Amendment A1 / F-764."""

    def test_export_to_file_paginated_does_not_deadlock(self, tmp_path):
        log = DebugLogger()
        log.log_info("comp", "meth", "hello")
        log.log_error("comp", "meth", ValueError("boom"))

        result = {}

        def _export():
            result["path"] = log.export_to_file_paginated(
                str(tmp_path / "debug_log.json")
            )

        t = threading.Thread(target=_export, daemon=True)
        t.start()
        t.join(timeout=5)

        assert not t.is_alive(), (
            "export_to_file_paginated deadlocked: it acquires self._lock then "
            "calls get_debug_view_paginated, which re-enters `with self._lock:` "
            "-- a non-reentrant Lock deadlocks here (F-764 / Amendment A1)."
        )
        assert result.get("path") == str(tmp_path / "debug_log.json")
