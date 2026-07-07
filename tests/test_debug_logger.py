"""Behavioral tests for DebugLogger (no browser, fresh instances).

The load-bearing guarantees: recording is unconditional (plan_M3/F-182 —
enable()/disable() gate only the legacy stderr echo, not whether data is
captured), the three buffers are hard-capped so they cannot grow without
bound, and identical errors are de-duplicated while still being counted.
These are the memory-safety properties the server relies on during
long-lived sessions.
"""

from debug_logger import DebugLogger


class TestEnableGating:
    """F-182: recording is unconditional (not gated behind enable()) so the
    default install is never a silent black box. enable()/disable() now
    govern ONLY the legacy stderr echo — see TestStderrEchoGating below."""

    def test_recording_is_unconditional_even_without_enable(self):
        log = DebugLogger()
        log.log_info("comp", "meth", "hello")
        log.log_warning("comp", "meth", "warn")
        log.log_error("comp", "meth", ValueError("x"))
        view = log.get_debug_view()
        assert view["summary"]["total_info"] == 1
        assert view["summary"]["total_warnings"] == 1
        assert view["summary"]["total_errors"] == 1

    def test_disable_does_not_stop_recording(self):
        log = DebugLogger()
        log.enable()
        log.log_info("comp", "meth", "one")
        log.disable()
        log.log_info("comp", "meth", "two")
        assert log.get_debug_view()["summary"]["total_info"] == 2


class TestStderrEchoGating:
    """enable()/disable() now govern ONLY the legacy stderr echo (recording
    itself is unconditional — see TestEnableGating above)."""

    def test_stderr_echo_silent_until_enabled(self, capsys):
        log = DebugLogger()
        log.log_info("comp", "meth", "quiet")
        assert "quiet" not in capsys.readouterr().err

    def test_stderr_echo_stops_after_disable(self, capsys):
        log = DebugLogger()
        log.enable()
        log.log_info("comp", "meth", "one")
        capsys.readouterr()  # discard the "one" echo + the enable() banner
        log.disable()
        log.log_info("comp", "meth", "two")
        assert "two" not in capsys.readouterr().err


class TestBufferCaps:
    def test_info_buffer_capped_to_most_recent(self):
        log = DebugLogger()
        log.MAX_INFO = 3
        log.enable()
        for i in range(6):
            log.log_info("comp", "meth", f"msg-{i}")
        view = log.get_debug_view_paginated(max_info=100)
        assert view["summary"]["total_info"] == 3
        assert [e["message"] for e in view["all_info"]] == ["msg-3", "msg-4", "msg-5"]

    def test_warning_buffer_capped(self):
        log = DebugLogger()
        log.MAX_WARNINGS = 2
        log.enable()
        for i in range(5):
            log.log_warning("comp", "meth", f"w-{i}")
        assert log.get_debug_view()["summary"]["total_warnings"] == 2


class TestErrorDedup:
    def test_identical_errors_deduped_but_counted(self):
        log = DebugLogger()
        log.enable()
        for _ in range(3):
            log.log_error("comp", "meth", ValueError("same"))
        view = log.get_debug_view()
        # Only one stored entry...
        assert view["summary"]["total_errors"] == 1
        # ...but every occurrence is counted in stats.
        assert view["summary"]["stats"]["comp.meth.errors"] == 3

    def test_distinct_errors_are_kept_separately(self):
        log = DebugLogger()
        log.enable()
        log.log_error("comp", "meth", ValueError("a"))
        log.log_error("comp", "meth", ValueError("b"))
        assert log.get_debug_view()["summary"]["total_errors"] == 2

    def test_seen_error_set_clears_at_cap(self):
        log = DebugLogger()
        log.MAX_SEEN_ERRORS = 2
        log.enable()
        log.log_error("comp", "meth", ValueError("a"))
        log.log_error("comp", "meth", ValueError("b"))
        # seen set now at cap (2); the next distinct error triggers a clear
        # before insert, so the set never exceeds the cap.
        log.log_error("comp", "meth", ValueError("c"))
        assert len(log._seen_errors) <= log.MAX_SEEN_ERRORS


class TestViewAndClear:
    def test_paginated_view_limits_and_summarizes(self):
        log = DebugLogger()
        log.enable()
        log.log_error("api", "call", ValueError("boom"))
        log.log_warning("api", "call", "slow")
        for i in range(3):
            log.log_info("dom", "query", f"i-{i}")

        view = log.get_debug_view_paginated(max_errors=1, max_warnings=1, max_info=1)
        assert view["summary"]["total_info"] == 3
        assert len(view["all_info"]) == 1  # limited
        assert view["summary"]["error_types"] == {"ValueError": 1}
        breakdown = view["component_breakdown"]
        assert breakdown["api"]["errors"] == 1 and breakdown["api"]["warnings"] == 1
        assert breakdown["dom"]["calls"] == 3

    def test_clear_empties_all_buffers(self):
        log = DebugLogger()
        log.enable()
        log.log_error("comp", "meth", ValueError("x"))
        log.log_info("comp", "meth", "y")
        log.clear_debug_view()
        view = log.get_debug_view()
        assert view["summary"]["total_errors"] == 0
        assert view["summary"]["total_info"] == 0
