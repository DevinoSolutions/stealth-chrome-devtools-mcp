"""Tests for narrowed exception handling — verifies correct errors are caught
and unexpected errors bubble up instead of being silently swallowed.

Validates that:
- Specific exception types are caught where expected
- Unexpected exception types propagate (not silently eaten)
- Cleanup paths log warnings instead of silently passing
- Memory leak fixes (instance_hooks, _instance_filters) work on close
- Debug logger caps enforce bounded growth
"""

import asyncio
import os
import sys
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import psutil
import pytest

from debug_logger import DebugLogger
from dynamic_hook_system import DynamicHookSystem
from network_interceptor import NetworkInterceptor
from persistent_storage import InMemoryStorage


# ---------------------------------------------------------------------------
# DebugLogger — bounded growth
# ---------------------------------------------------------------------------


class TestDebugLoggerCaps:
    def test_info_capped_at_max(self):
        """_info list should never exceed MAX_INFO entries."""
        logger = DebugLogger()
        logger.enable()
        for i in range(logger.MAX_INFO + 500):
            logger.log_info("test", "method", f"message {i}")
        assert len(logger._info) <= logger.MAX_INFO

    def test_errors_capped_at_max(self):
        """_errors list should never exceed MAX_ERRORS entries."""
        logger = DebugLogger()
        logger.enable()
        for i in range(logger.MAX_ERRORS + 200):
            # Each error needs a unique signature to avoid dedup
            logger.log_error("test", "method", Exception(f"unique error {i}"))
        assert len(logger._errors) <= logger.MAX_ERRORS

    def test_warnings_capped_at_max(self):
        """_warnings list should never exceed MAX_WARNINGS entries."""
        logger = DebugLogger()
        logger.enable()
        for i in range(logger.MAX_WARNINGS + 200):
            logger.log_warning("test", "method", f"warning {i}")
        assert len(logger._warnings) <= logger.MAX_WARNINGS

    def test_seen_errors_capped_at_max(self):
        """_seen_errors set should be cleared when it reaches MAX_SEEN_ERRORS."""
        logger = DebugLogger()
        logger.enable()
        for i in range(logger.MAX_SEEN_ERRORS + 100):
            logger.log_error("test", "method", Exception(f"error {i}"))
        assert len(logger._seen_errors) <= logger.MAX_SEEN_ERRORS

    def test_cap_preserves_newest_entries(self):
        """When capped, the newest entries should be kept, oldest dropped."""
        logger = DebugLogger()
        logger.enable()
        for i in range(logger.MAX_INFO + 100):
            logger.log_info("test", "method", f"msg-{i}")
        last_msg = logger._info[-1]["message"]
        assert last_msg == f"msg-{logger.MAX_INFO + 99}"
        first_msg = logger._info[0]["message"]
        assert first_msg != "msg-0"  # oldest should have been evicted

    def test_disabled_logger_does_not_accumulate(self):
        """When disabled, nothing should be stored."""
        logger = DebugLogger()
        # not calling enable()
        for i in range(100):
            logger.log_info("test", "method", f"msg {i}")
            logger.log_warning("test", "method", f"warn {i}")
            logger.log_error("test", "method", Exception(f"err {i}"))
        assert len(logger._info) == 0
        assert len(logger._warnings) == 0
        assert len(logger._errors) == 0

    def test_stderr_catches_only_os_and_value_errors(self):
        """_emit_stderr should catch OSError/ValueError but not other types."""
        logger = DebugLogger()
        logger._enabled = True

        # OSError should be caught silently
        with patch("builtins.print", side_effect=OSError("broken pipe")):
            logger._emit_stderr("test")  # should not raise

        # ValueError should be caught silently
        with patch("builtins.print", side_effect=ValueError("closed file")):
            logger._emit_stderr("test")  # should not raise

        # TypeError should NOT be caught — it's unexpected
        with patch("builtins.print", side_effect=TypeError("bad arg")):
            with pytest.raises(TypeError):
                logger._emit_stderr("test")


# ---------------------------------------------------------------------------
# DynamicHookSystem — instance lifecycle
# ---------------------------------------------------------------------------


class TestDynamicHookSystemInstanceLifecycle:
    def test_add_and_remove_instance(self):
        """Adding then removing an instance should leave no trace."""
        system = DynamicHookSystem()
        system.add_instance("inst-1")
        assert "inst-1" in system.instance_hooks
        system.remove_instance("inst-1")
        assert "inst-1" not in system.instance_hooks

    def test_remove_nonexistent_instance_is_safe(self):
        """Removing an instance that was never added should not raise."""
        system = DynamicHookSystem()
        system.remove_instance("never-existed")  # should not raise

    def test_remove_cleans_up_completely(self):
        """After remove, the instance_hooks dict should not grow."""
        system = DynamicHookSystem()
        for i in range(100):
            iid = f"inst-{i}"
            system.add_instance(iid)
            system.remove_instance(iid)
        assert len(system.instance_hooks) == 0

    def test_double_add_does_not_duplicate(self):
        """Adding the same instance twice should not create duplicate entries."""
        system = DynamicHookSystem()
        system.add_instance("inst-1")
        system.instance_hooks["inst-1"].append("hook-a")
        system.add_instance("inst-1")  # should not reset the list
        assert "hook-a" in system.instance_hooks["inst-1"]


# ---------------------------------------------------------------------------
# NetworkInterceptor — filter cleanup on close
# ---------------------------------------------------------------------------


class TestNetworkInterceptorFilterCleanup:
    @pytest.fixture
    def interceptor(self):
        return NetworkInterceptor()

    @pytest.mark.asyncio
    async def test_clear_instance_data_removes_filters(self, interceptor):
        """clear_instance_data should clean up _instance_filters too."""
        iid = "test-instance"
        await interceptor.set_capture_filters(iid, include_types=["XHR"])
        assert iid in interceptor._instance_filters

        await interceptor.clear_instance_data(iid)
        assert iid not in interceptor._instance_filters

    @pytest.mark.asyncio
    async def test_clear_instance_data_removes_requests_and_responses(
        self, interceptor
    ):
        """clear_instance_data should remove all requests/responses for instance."""
        iid = "test-instance"
        async with interceptor._lock:
            interceptor._instance_requests[iid] = ["req-1", "req-2"]
            interceptor._requests["req-1"] = MagicMock()
            interceptor._requests["req-2"] = MagicMock()
            interceptor._responses["req-1"] = MagicMock()

        await interceptor.clear_instance_data(iid)

        assert iid not in interceptor._instance_requests
        assert "req-1" not in interceptor._requests
        assert "req-2" not in interceptor._requests
        assert "req-1" not in interceptor._responses

    @pytest.mark.asyncio
    async def test_clear_nonexistent_instance_is_safe(self, interceptor):
        """Clearing a non-existent instance should not raise."""
        await interceptor.clear_instance_data("never-existed")

    @pytest.mark.asyncio
    async def test_filters_do_not_leak_across_instances(self, interceptor):
        """Closing one instance should not affect another's filters."""
        await interceptor.set_capture_filters("inst-a", include_types=["XHR"])
        await interceptor.set_capture_filters("inst-b", include_types=["Fetch"])

        await interceptor.clear_instance_data("inst-a")

        assert "inst-a" not in interceptor._instance_filters
        assert "inst-b" in interceptor._instance_filters


# ---------------------------------------------------------------------------
# PersistentStorage — instance cleanup
# ---------------------------------------------------------------------------


class TestPersistentStorageCleanup:
    def test_remove_instance_cleans_up(self):
        storage = InMemoryStorage()
        storage.store_instance("inst-1", {"state": "ready"})
        assert storage.get_instance("inst-1") is not None
        storage.remove_instance("inst-1")
        assert storage.get_instance("inst-1") is None

    def test_remove_nonexistent_is_safe(self):
        storage = InMemoryStorage()
        storage.remove_instance("never-existed")  # should not raise

    def test_clear_all_removes_everything(self):
        storage = InMemoryStorage()
        storage.store_instance("a", {"state": "ready"})
        storage.store_instance("b", {"state": "ready"})
        storage.set("progressive_elements", {"elem-1": {}})
        storage.clear_all()
        assert storage.get_instance("a") is None
        assert storage.get_instance("b") is None

    def test_generic_key_value_storage(self):
        storage = InMemoryStorage()
        storage.set("my_key", {"data": 42})
        assert storage.get("my_key") == {"data": 42}
        assert storage.get("missing", "default") == "default"


# ---------------------------------------------------------------------------
# Exception type specificity — verify unexpected errors propagate
# ---------------------------------------------------------------------------


class TestExceptionSpecificity:
    """Verify that narrowed except clauses don't accidentally catch
    unexpected exception types that should bubble up as bugs."""

    def test_process_cleanup_create_time_catches_psutil_errors(self):
        """track_browser_process should handle psutil errors for create_time."""
        from process_cleanup import ProcessCleanup

        with patch.object(ProcessCleanup, "_setup_cleanup_handlers", lambda self: None):
            with patch.object(
                ProcessCleanup, "_recover_orphaned_processes", lambda self: None
            ):
                pc = ProcessCleanup.__new__(ProcessCleanup)
                pc.pid_file = Path("/tmp/test_pids.json")
                pc.tracked_pids = set()
                pc.browser_processes = {}
                pc.orphan_profile_max_age_seconds = 21600
                pc._init_time = 0

        mock_process = MagicMock()
        mock_process.pid = 99999

        # psutil.NoSuchProcess should be caught
        with patch("psutil.Process", side_effect=psutil.NoSuchProcess(99999)):
            pc.track_browser_process("inst-1", mock_process)
            assert "inst-1" in pc.browser_processes
            assert pc.browser_processes["inst-1"]["create_time"] is None

    def test_proxy_forwarder_close_catches_connection_errors(self):
        """_close_writer should handle OSError/ConnectionError."""
        from proxy_forwarder import AuthenticatedProxyForwarder

        writer = MagicMock()
        writer.is_closing.return_value = False
        writer.close = MagicMock()

        # OSError on wait_closed should be caught
        writer.wait_closed = AsyncMock(side_effect=OSError("connection reset"))
        forwarder = AuthenticatedProxyForwarder.__new__(AuthenticatedProxyForwarder)
        asyncio.run(forwarder._close_writer(writer))  # should not raise

        # BrokenPipeError on wait_closed should be caught
        writer.wait_closed = AsyncMock(side_effect=BrokenPipeError())
        asyncio.run(forwarder._close_writer(writer))  # should not raise

    def test_proxy_forwarder_close_propagates_unexpected_errors(self):
        """_close_writer should NOT catch unexpected errors like ValueError."""
        from proxy_forwarder import AuthenticatedProxyForwarder

        writer = MagicMock()
        writer.is_closing.return_value = False
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=ValueError("unexpected"))

        forwarder = AuthenticatedProxyForwarder.__new__(AuthenticatedProxyForwarder)
        with pytest.raises(ValueError):
            asyncio.run(forwarder._close_writer(writer))


# ---------------------------------------------------------------------------
# BrowserManager — _browser_process_is_alive exception specificity
# ---------------------------------------------------------------------------


class TestBrowserProcessIsAlive:
    def test_oserror_on_poll_returns_fallback(self):
        """OSError during poll() should be caught, falling through to returncode check."""
        from browser_manager import BrowserManager

        browser = MagicMock()
        mock_process = MagicMock()
        mock_process.poll.side_effect = OSError("bad fd")
        mock_process.returncode = None  # means still running
        browser._process = mock_process

        result = BrowserManager._browser_process_is_alive(browser)
        assert result is True  # falls through to returncode check

    def test_unexpected_error_on_poll_propagates(self):
        """Non-OSError during poll() should NOT be caught — it's a bug."""
        from browser_manager import BrowserManager

        browser = MagicMock()
        mock_process = MagicMock()
        mock_process.poll.side_effect = TypeError("unexpected bug")
        browser._process = mock_process

        with pytest.raises(TypeError):
            BrowserManager._browser_process_is_alive(browser)

    def test_no_process_no_pid_returns_true(self):
        """When browser has no _process and no _process_pid, assume alive."""
        from browser_manager import BrowserManager

        browser = MagicMock(spec=[])  # no _process, no _process_pid attrs
        result = BrowserManager._browser_process_is_alive(browser)
        assert result is True


# ---------------------------------------------------------------------------
# BrowserManager — discard_instance_unlocked cleanup
# ---------------------------------------------------------------------------


class TestDiscardInstanceCleanup:
    def test_keyerror_on_storage_remove_is_safe(self):
        """If persistent_storage.remove_instance raises KeyError, it should be caught."""
        from browser_manager import BrowserManager

        manager = BrowserManager()
        data = {"instance": MagicMock()}
        data["instance"].state = "ready"

        with patch("browser_manager.process_cleanup") as mock_pc:
            with patch("browser_manager.persistent_storage") as mock_ps:
                mock_ps.remove_instance.side_effect = KeyError("already gone")
                with patch("browser_manager.dynamic_hook_system") as mock_dh:
                    # Should not raise
                    manager._discard_instance_unlocked("inst-1", data, "test")

    def test_unexpected_error_on_finalize_propagates(self):
        """If process finalize raises an unexpected error (e.g. RuntimeError),
        it should propagate since we only catch (OSError, psutil.Error, KeyError)."""
        from browser_manager import BrowserManager

        manager = BrowserManager()
        data = {"instance": MagicMock()}
        data["instance"].state = "ready"

        with patch("browser_manager.process_cleanup") as mock_pc:
            mock_pc.finalize_browser_process.side_effect = RuntimeError("unexpected")
            with patch("browser_manager.persistent_storage"):
                with patch("browser_manager.dynamic_hook_system"):
                    with pytest.raises(RuntimeError):
                        manager._discard_instance_unlocked("inst-1", data, "test")


# ---------------------------------------------------------------------------
# Integration: spawn/close cycle leaves no leaked state
# ---------------------------------------------------------------------------


class TestLeakPrevention:
    def test_dynamic_hooks_no_leak_after_many_cycles(self):
        """Simulating many add/remove cycles should leave no leaked entries."""
        system = DynamicHookSystem()
        for i in range(1000):
            iid = f"instance-{i}"
            system.add_instance(iid)
            system.remove_instance(iid)
        assert len(system.instance_hooks) == 0

    @pytest.mark.asyncio
    async def test_network_interceptor_no_leak_after_many_cycles(self):
        """Simulating many filter set/clear cycles should leave no leaked filters."""
        interceptor = NetworkInterceptor()
        for i in range(100):
            iid = f"instance-{i}"
            await interceptor.set_capture_filters(iid, include_types=["XHR"])
            async with interceptor._lock:
                interceptor._instance_requests[iid] = [f"req-{i}"]
            await interceptor.clear_instance_data(iid)

        assert len(interceptor._instance_filters) == 0
        assert len(interceptor._instance_requests) == 0
        assert len(interceptor._requests) == 0
        assert len(interceptor._responses) == 0

    def test_debug_logger_bounded_under_heavy_load(self):
        """Logger should stay bounded even under sustained heavy logging."""
        logger = DebugLogger()
        logger.enable()
        for i in range(10000):
            logger.log_info("load", "test", f"entry {i}")
        assert len(logger._info) <= logger.MAX_INFO

        for i in range(5000):
            logger.log_warning("load", "test", f"warning {i}")
        assert len(logger._warnings) <= logger.MAX_WARNINGS

        for i in range(3000):
            logger.log_error("load", "test", Exception(f"error {i}"))
        assert len(logger._errors) <= logger.MAX_ERRORS
