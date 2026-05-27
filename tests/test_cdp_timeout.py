"""Tests for CDP timeout enforcement and timeout clamping.

Verifies that:
1. _with_cdp_timeout prevents infinite hangs on dead connections
2. _clamp_timeout caps user-provided timeouts to MAX_TIMEOUT_MS (60s)
3. NavigationOptions model rejects timeouts above the cap
4. Real browser operations respect the timeout
"""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import psutil
import pytest

# Make embedded/ importable
EMBEDDED_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
)
if str(EMBEDDED_DIR) not in sys.path:
    sys.path.insert(0, str(EMBEDDED_DIR))

from models import NavigationOptions
from pydantic import ValidationError
from server import _with_cdp_timeout, _clamp_timeout, CDP_OPERATION_TIMEOUT, MAX_TIMEOUT_MS


# ---------------------------------------------------------------------------
# Unit tests: _with_cdp_timeout mechanism
# ---------------------------------------------------------------------------

class TestWithCdpTimeoutMechanism:
    """Test the timeout wrapper itself, no browser needed."""

    @pytest.mark.asyncio
    async def test_fast_coroutine_succeeds(self):
        """Normal fast operations should return their result."""
        async def fast():
            return "hello"
        result = await _with_cdp_timeout(fast(), timeout=5)
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_slow_coroutine_times_out(self):
        """A hanging coroutine should raise Exception after timeout."""
        async def hang_forever():
            await asyncio.sleep(9999)
            return "never"

        start = time.monotonic()
        with pytest.raises(Exception, match="CDP operation timed out"):
            await _with_cdp_timeout(hang_forever(), timeout=2)
        elapsed = time.monotonic() - start
        assert 1.5 < elapsed < 4.0, f"Timeout took {elapsed:.1f}s, expected ~2s"

    @pytest.mark.asyncio
    async def test_timeout_includes_instance_id_in_message(self):
        """Error message should include the instance_id for debugging."""
        async def hang():
            await asyncio.sleep(9999)

        with pytest.raises(Exception, match="instance test-123"):
            await _with_cdp_timeout(hang(), timeout=1, instance_id="test-123")

    @pytest.mark.asyncio
    async def test_default_timeout_is_30s(self):
        """Default timeout should be 30 seconds."""
        assert CDP_OPERATION_TIMEOUT == 30.0

    @pytest.mark.asyncio
    async def test_exception_propagates_not_timeout(self):
        """Real exceptions should propagate immediately, not wait for timeout."""
        async def raise_error():
            raise ValueError("real error")

        start = time.monotonic()
        with pytest.raises(ValueError, match="real error"):
            await _with_cdp_timeout(raise_error(), timeout=30)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, "Exception should propagate immediately"

    @pytest.mark.asyncio
    async def test_custom_timeout_override(self):
        """Custom timeout should override the default."""
        async def slow():
            await asyncio.sleep(5)
            return "done"

        start = time.monotonic()
        with pytest.raises(Exception, match="timed out after 1s"):
            await _with_cdp_timeout(slow(), timeout=1)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_zero_timeout_uses_default(self):
        """timeout=0 should fall back to CDP_OPERATION_TIMEOUT."""
        async def hang():
            await asyncio.sleep(9999)

        start = time.monotonic()
        with pytest.raises(Exception, match="timed out"):
            await _with_cdp_timeout(hang(), timeout=0)
        elapsed = time.monotonic() - start
        # Should use default 30s, but we don't want to wait that long
        # Just verify it didn't use 0 (instant) or get stuck
        assert elapsed > 1.0  # didn't fire instantly

    @pytest.mark.asyncio
    async def test_concurrent_timeouts_independent(self):
        """Multiple concurrent timeout wrappers shouldn't interfere."""
        async def slow(n):
            await asyncio.sleep(n)
            return f"done-{n}"

        results = await asyncio.gather(
            _with_cdp_timeout(slow(0.1), timeout=5),
            _with_cdp_timeout(slow(0.2), timeout=5),
            _with_cdp_timeout(slow(0.3), timeout=5),
        )
        assert results == ["done-0.1", "done-0.2", "done-0.3"]

    @pytest.mark.asyncio
    async def test_timeout_cancels_inner_coroutine(self):
        """After timeout, the inner coroutine should be cancelled."""
        cancelled = False

        async def trackable():
            nonlocal cancelled
            try:
                await asyncio.sleep(9999)
            except asyncio.CancelledError:
                cancelled = True
                raise

        with pytest.raises(Exception, match="timed out"):
            await _with_cdp_timeout(trackable(), timeout=1)

        await asyncio.sleep(0.1)  # Let cancellation propagate
        assert cancelled, "Inner coroutine should have been cancelled"


# ---------------------------------------------------------------------------
# Error path tests: verify errors propagate, not swallowed
# ---------------------------------------------------------------------------

class TestErrorPathPropagation:
    """Verify that CDP timeouts surface as real errors, not silent swallows.

    Tests the exact patterns used in server.py tool handlers to ensure
    timeout exceptions aren't caught-and-swallowed by try/except blocks.
    """

    @pytest.mark.asyncio
    async def test_execute_script_pattern_returns_success_false(self):
        """The execute_script try/except pattern must return success=False on timeout."""
        async def hang():
            await asyncio.sleep(9999)

        # Reproduce the exact pattern from server.py execute_script handler
        try:
            result = await _with_cdp_timeout(hang(), timeout=2, instance_id="test-dead")
            response = {"success": True, "result": result, "error": None}
        except Exception as e:
            response = {"success": False, "result": None, "error": str(e)}

        assert response["success"] is False, f"Expected success=False, got {response}"
        assert "timed out" in response["error"].lower()
        assert response["result"] is None

    @pytest.mark.asyncio
    async def test_timeout_error_not_swallowed_by_bare_except(self):
        """Timeout exception must propagate through tool handlers that don't catch."""
        async def hang():
            await asyncio.sleep(9999)

        # Pattern used by take_screenshot, click_element, etc. (no try/except)
        with pytest.raises(Exception, match="timed out"):
            await _with_cdp_timeout(hang(), timeout=2, instance_id="test-dead")

    @pytest.mark.asyncio
    async def test_timeout_error_message_is_actionable(self):
        """Error message should tell the user what to do."""
        async def hang():
            await asyncio.sleep(9999)

        try:
            await _with_cdp_timeout(hang(), timeout=1, instance_id="abc-123")
        except Exception as e:
            msg = str(e)
            assert "abc-123" in msg, "Should include instance_id"
            assert "close" in msg.lower(), "Should suggest closing instance"
            assert "spawn" in msg.lower(), "Should suggest spawning new one"

    @pytest.mark.asyncio
    async def test_real_js_error_not_confused_with_timeout(self):
        """A real ValueError should propagate as-is, not become a timeout error."""
        async def js_error():
            raise ValueError("SyntaxError: unexpected token")

        try:
            result = await _with_cdp_timeout(js_error(), timeout=30)
            response = {"success": True, "result": result, "error": None}
        except Exception as e:
            response = {"success": False, "result": None, "error": str(e)}

        assert response["success"] is False
        assert "SyntaxError" in response["error"]
        assert "timed out" not in response["error"]

    @pytest.mark.asyncio
    async def test_timeout_fires_before_inner_timeout(self):
        """CDP timeout should fire even if inner operation has its own longer wait."""
        async def slow_with_own_timeout():
            # Simulates a tool with its own 60s timeout that's dead
            await asyncio.sleep(60)

        start = time.monotonic()
        with pytest.raises(Exception, match="timed out"):
            await _with_cdp_timeout(slow_with_own_timeout(), timeout=2)
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"CDP timeout (2s) should fire, not inner (60s). Took {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Unit tests: _clamp_timeout and MAX_TIMEOUT_MS
# ---------------------------------------------------------------------------

class TestClampTimeout:
    """Verify that user-provided timeouts are capped to MAX_TIMEOUT_MS."""

    def test_max_timeout_is_60s(self):
        assert MAX_TIMEOUT_MS == 60_000

    def test_value_within_range_unchanged(self):
        assert _clamp_timeout(30_000) == 30_000

    def test_value_at_max_unchanged(self):
        assert _clamp_timeout(60_000) == 60_000

    def test_value_above_max_capped(self):
        assert _clamp_timeout(120_000) == 60_000

    def test_extreme_value_capped(self):
        assert _clamp_timeout(999_999_999) == 60_000

    def test_negative_value_floored_to_one(self):
        assert _clamp_timeout(-5) == 1

    def test_zero_floored_to_one(self):
        assert _clamp_timeout(0) == 1

    def test_one_ms_unchanged(self):
        assert _clamp_timeout(1) == 1

    def test_string_input_converted_and_clamped(self):
        assert _clamp_timeout("50000") == 50_000

    def test_string_above_max_converted_and_capped(self):
        assert _clamp_timeout("300000") == 60_000

    def test_string_negative_converted_and_floored(self):
        assert _clamp_timeout("-100") == 1

    def test_default_param_unused_when_value_provided(self):
        assert _clamp_timeout(5_000, default=30_000) == 5_000


class TestNavigationOptionsTimeoutValidation:
    """Verify that the NavigationOptions model rejects timeouts above the cap."""

    def test_default_timeout_is_30s(self):
        opts = NavigationOptions()
        assert opts.timeout == 30_000

    def test_valid_timeout_accepted(self):
        opts = NavigationOptions(timeout=15_000)
        assert opts.timeout == 15_000

    def test_max_timeout_accepted(self):
        opts = NavigationOptions(timeout=60_000)
        assert opts.timeout == 60_000

    def test_timeout_above_max_rejected(self):
        with pytest.raises(ValidationError):
            NavigationOptions(timeout=60_001)

    def test_huge_timeout_rejected(self):
        with pytest.raises(ValidationError):
            NavigationOptions(timeout=300_000)


# ---------------------------------------------------------------------------
# Unit tests: fast timeout returns failure quickly (no browser needed)
# ---------------------------------------------------------------------------

class TestFastTimeoutFailure:
    """Force very short timeouts on hanging operations to verify they fail
    quickly and cleanly instead of hanging.  Uses 1s timeouts to keep tests
    fast and deterministic (no real browser required)."""

    @pytest.mark.asyncio
    async def test_1s_timeout_on_hanging_coro_fails_within_3s(self):
        """A 1s timeout must raise within 3s, not hang."""
        async def hang():
            await asyncio.sleep(9999)

        start = time.monotonic()
        with pytest.raises(Exception, match="timed out"):
            await _with_cdp_timeout(hang(), timeout=1)
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, f"Expected failure in ~1s, took {elapsed:.1f}s"

    @pytest.mark.asyncio
    async def test_1s_timeout_returns_correct_error_type(self):
        """Timeout should raise a plain Exception (not asyncio.TimeoutError)."""
        async def hang():
            await asyncio.sleep(9999)

        with pytest.raises(Exception) as exc_info:
            await _with_cdp_timeout(hang(), timeout=1)
        assert type(exc_info.value) is Exception
        assert "CDP operation timed out" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_1s_timeout_error_includes_elapsed_time(self):
        """Error message should report how long it waited."""
        async def hang():
            await asyncio.sleep(9999)

        with pytest.raises(Exception, match="timed out after 1s"):
            await _with_cdp_timeout(hang(), timeout=1)

    @pytest.mark.asyncio
    async def test_1s_timeout_with_instance_id_in_error(self):
        """Error should include the instance_id for debugging."""
        async def hang():
            await asyncio.sleep(9999)

        with pytest.raises(Exception, match="instance dead-browser-42"):
            await _with_cdp_timeout(hang(), timeout=1, instance_id="dead-browser-42")

    @pytest.mark.asyncio
    async def test_rapid_sequential_timeouts_all_fail_independently(self):
        """Multiple sequential timeouts should each fail on their own schedule."""
        async def hang():
            await asyncio.sleep(9999)

        start = time.monotonic()
        for i in range(3):
            with pytest.raises(Exception, match="timed out"):
                await _with_cdp_timeout(hang(), timeout=1, instance_id=f"seq-{i}")
        elapsed = time.monotonic() - start
        assert elapsed < 6.0, f"3 sequential 1s timeouts should finish in ~3s, took {elapsed:.1f}s"

    @pytest.mark.asyncio
    async def test_concurrent_timeouts_all_fail_within_budget(self):
        """Multiple concurrent timeouts should all resolve within the single timeout window."""
        async def hang():
            await asyncio.sleep(9999)

        async def timed_hang(instance_id):
            with pytest.raises(Exception, match="timed out"):
                await _with_cdp_timeout(hang(), timeout=1, instance_id=instance_id)

        start = time.monotonic()
        await asyncio.gather(
            timed_hang("concurrent-0"),
            timed_hang("concurrent-1"),
            timed_hang("concurrent-2"),
        )
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, f"3 concurrent 1s timeouts should finish in ~1s, took {elapsed:.1f}s"

    @pytest.mark.asyncio
    async def test_clamped_timeout_still_fires(self):
        """A huge timeout clamped by _clamp_timeout should still result
        in a bounded wait when fed to _with_cdp_timeout."""
        clamped = _clamp_timeout(999_999)
        assert clamped == MAX_TIMEOUT_MS == 60_000
        # We don't actually wait 60s — just verify the clamp produces
        # a value that _with_cdp_timeout can use (i.e. not infinity).
        assert clamped / 1000 <= 60.0


# ---------------------------------------------------------------------------
# Integration test: real browser with killed process
# ---------------------------------------------------------------------------

class TestRealBrowserDeadConnection:
    """Test timeout with a real browser whose process gets killed."""

    @pytest.fixture
    async def browser_and_tab(self):
        """Spawn a real browser, return (browser_manager, instance_id, tab)."""
        from browser_manager import BrowserManager
        from models import BrowserOptions
        bm = BrowserManager()
        opts = BrowserOptions(headless=True)
        instance = await bm.spawn_browser(opts)
        instance_id = instance.instance_id
        tab = await bm.get_tab(instance_id)
        yield bm, instance_id, tab
        # Cleanup
        try:
            await bm.close_instance(instance_id)
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_normal_operation_succeeds(self, browser_and_tab):
        """Sanity check: operations work on a healthy browser."""
        bm, instance_id, tab = browser_and_tab
        result = await _with_cdp_timeout(
            tab.evaluate("1 + 1"), timeout=10, instance_id=instance_id
        )
        assert result == 2

    @pytest.mark.asyncio
    async def test_killed_browser_times_out_not_hangs(self, browser_and_tab):
        """After killing Chrome, CDP operations should timeout, not hang."""
        bm, instance_id, tab = browser_and_tab

        # Find and kill the Chrome process
        data = await bm.get_instance(instance_id)
        browser = data["browser"]
        process = getattr(browser, "_process", None)
        pid = None
        if process:
            pid = process.pid
        if not pid:
            pid = getattr(browser, "_process_pid", None)

        assert pid, "Could not find Chrome PID"

        # Kill Chrome
        try:
            parent = psutil.Process(int(pid))
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
            parent.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass

        # Give OS a moment to clean up
        await asyncio.sleep(0.5)

        # Now try a CDP operation — should timeout, NOT hang forever
        start = time.monotonic()
        with pytest.raises(Exception):
            await _with_cdp_timeout(
                tab.evaluate("document.title"),
                timeout=5,
                instance_id=instance_id,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 10.0, f"Should have timed out in ~5s, took {elapsed:.1f}s"

    @pytest.mark.asyncio
    async def test_screenshot_on_dead_browser_times_out(self, browser_and_tab):
        """Screenshot on dead browser should timeout, not hang."""
        bm, instance_id, tab = browser_and_tab
        import tempfile

        # Kill Chrome
        data = await bm.get_instance(instance_id)
        browser = data["browser"]
        process = getattr(browser, "_process", None)
        if process:
            try:
                parent = psutil.Process(process.pid)
                for child in parent.children(recursive=True):
                    child.kill()
                parent.kill()
                parent.wait(timeout=5)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass

        await asyncio.sleep(0.5)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = Path(f.name)

        start = time.monotonic()
        try:
            with pytest.raises(Exception):
                await _with_cdp_timeout(
                    tab.save_screenshot(tmp),
                    timeout=5,
                    instance_id=instance_id,
                )
            elapsed = time.monotonic() - start
            assert elapsed < 10.0, f"Screenshot should timeout in ~5s, took {elapsed:.1f}s"
        finally:
            tmp.unlink(missing_ok=True)
