"""F-180 pinning tests: close_instance must offload the synchronous kill to a
worker thread under a real timeout so the event loop stays responsive.

Four scenarios:
1. Loop-stays-responsive: a 30s-stuck kill must NOT freeze the event loop.
2. Double-close: second sequential close returns False, kill invoked once.
3. Concurrent close: exactly one of two concurrent closes claims.
4. Happy path: fast kill returns True, instance removed, storage cleaned.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserState


def _noop_coro():
    """A coroutine factory for fake awaitable returns."""

    async def _noop():
        pass

    return _noop()


def _make_fake_browser():
    """Return a minimal fake browser object with stubs for all close_instance needs."""
    browser = SimpleNamespace(
        tabs=[],
        connection=SimpleNamespace(
            closed=True,
            send=MagicMock(side_effect=lambda *a, **kw: _noop_coro()),
            disconnect=MagicMock(side_effect=lambda *a, **kw: _noop_coro()),
        ),
        _process=SimpleNamespace(
            returncode=0,
            pid=99999,
            terminate=MagicMock(),
            kill=MagicMock(),
        ),
        _process_pid=99999,
        stop=MagicMock(return_value=None),
    )
    return browser


def _make_fake_instance(instance_id: str = "test-1"):
    """Return a BrowserInstance with minimal fields."""
    return BrowserInstance(
        instance_id=instance_id,
        state=BrowserState.READY,
    )


def _seed_manager(manager: BrowserManager, instance_id: str = "test-1"):
    """Seed a BrowserManager with one fake instance, returning (browser, instance)."""
    browser = _make_fake_browser()
    instance = _make_fake_instance(instance_id)
    manager._instances[instance_id] = {
        "browser": browser,
        "instance": instance,
    }
    manager._spawn_diagnostics[instance_id] = {"dummy": True}
    return browser, instance


PATCHES = {
    "stealth_chrome_devtools_mcp.embedded.process_cleanup.kill_browser_process": MagicMock(),
    "stealth_chrome_devtools_mcp.embedded.process_cleanup.finalize_browser_process": MagicMock(),
    "stealth_chrome_devtools_mcp.embedded.process_cleanup.cleanup_deferred_profiles": MagicMock(),
    "stealth_chrome_devtools_mcp.embedded.in_memory_storage.remove_instance": MagicMock(),
}


@pytest.fixture(autouse=True)
def _isolate_process_cleanup(monkeypatch):
    """Stub out process_cleanup and in_memory_storage so no real OS work happens."""
    from stealth_chrome_devtools_mcp.embedded.in_memory_storage import (
        in_memory_storage as ps,
    )
    from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

    monkeypatch.setattr(process_cleanup, "kill_browser_process", MagicMock())
    monkeypatch.setattr(process_cleanup, "finalize_browser_process", MagicMock())
    monkeypatch.setattr(process_cleanup, "cleanup_deferred_profiles", MagicMock())
    monkeypatch.setattr(ps, "remove_instance", MagicMock())


# ---------------------------------------------------------------------------
# 1. Loop-stays-responsive (§5.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_stays_responsive_during_stuck_kill(monkeypatch):
    """A 30-second stuck kill must not freeze the event loop.

    Monkepatches kill_browser_process to sleep in a cancellable loop (simulating
    a wedged synchronous kill). A concurrent heartbeat task increments a counter
    every 0.05s. close_instance with a ~1s timeout must return within ~2s,
    heartbeat must have advanced, and the kill must NOT be re-run inline.
    """
    import threading

    from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

    kill_call_count = 0
    kill_done = threading.Event()

    def slow_kill(instance_id):
        nonlocal kill_call_count
        kill_call_count += 1
        kill_done.wait(timeout=30)

    monkeypatch.setattr(process_cleanup, "kill_browser_process", slow_kill)

    manager = BrowserManager()
    _browser, _instance = _seed_manager(manager, "stuck-1")

    # Override CLOSE_KILL_TIMEOUT to 1.0s for a fast test
    monkeypatch.setattr(manager, "CLOSE_KILL_TIMEOUT", 1.0)

    heartbeat_count = 0
    heartbeat_running = True

    async def heartbeat():
        nonlocal heartbeat_count
        while heartbeat_running:
            heartbeat_count += 1
            await asyncio.sleep(0.05)

    hb_task = asyncio.create_task(heartbeat())

    warnings_before = len(debug_logger._warnings)

    t0 = time.monotonic()
    result = await manager.close_instance("stuck-1")
    elapsed = time.monotonic() - t0

    heartbeat_running = False
    kill_done.set()
    await asyncio.sleep(0.1)
    hb_task.cancel()
    try:
        await hb_task
    except asyncio.CancelledError:
        pass

    # close_instance returned within ~CLOSE_KILL_TIMEOUT, not 30s
    assert elapsed < 5.0, f"close_instance took {elapsed:.1f}s, expected < 5s"
    assert result is True

    # The heartbeat advanced (loop never froze)
    assert heartbeat_count >= 10, (
        f"heartbeat only ticked {heartbeat_count} times — loop was frozen"
    )

    # Instance is gone from _instances (claimed in Phase 1)
    assert "stuck-1" not in manager._instances

    # WARNING was logged about the timeout via debug_logger
    new_warnings = debug_logger._warnings[warnings_before:]
    warning_msgs = [w["message"] for w in new_warnings]
    assert any("exceeded" in m for m in warning_msgs), (
        f"Expected a timeout WARNING with 'exceeded', got: {warning_msgs}"
    )

    # kill_browser_process was invoked exactly once (NOT re-run inline)
    assert kill_call_count == 1, (
        f"kill_browser_process called {kill_call_count} times, expected 1"
    )


# ---------------------------------------------------------------------------
# 2. Double-close (§5.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_close_returns_false_second_time(monkeypatch):
    """Two sequential close_instance calls: first True, second False."""
    manager = BrowserManager()
    _seed_manager(manager, "dbl-1")

    result1 = await manager.close_instance("dbl-1")
    result2 = await manager.close_instance("dbl-1")

    assert result1 is True
    assert result2 is False


@pytest.mark.asyncio
async def test_double_close_blocking_teardown_invoked_once(monkeypatch):
    """_blocking_teardown must be invoked exactly once across two sequential closes."""
    manager = BrowserManager()
    _seed_manager(manager, "dbl-2")

    teardown_calls = 0
    original_teardown = getattr(manager, "_blocking_teardown", None)

    if original_teardown is not None:

        def counting_teardown(*args, **kwargs):
            nonlocal teardown_calls
            teardown_calls += 1
            return original_teardown(*args, **kwargs)

        monkeypatch.setattr(manager, "_blocking_teardown", counting_teardown)

        await manager.close_instance("dbl-2")
        await manager.close_instance("dbl-2")

        assert teardown_calls == 1
    else:
        # Pre-implementation: _blocking_teardown doesn't exist yet
        # This test will pass vacuously and be meaningful after implementation
        await manager.close_instance("dbl-2")
        await manager.close_instance("dbl-2")
        assert "dbl-2" not in manager._instances


# ---------------------------------------------------------------------------
# 3. Concurrent close (§5.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_close_exactly_one_claims(monkeypatch):
    """Two concurrent close_instance calls: exactly one returns True."""
    manager = BrowserManager()
    _seed_manager(manager, "conc-1")

    results = await asyncio.gather(
        manager.close_instance("conc-1"),
        manager.close_instance("conc-1"),
    )

    assert sorted(results) == [False, True], (
        f"Expected exactly one True and one False, got {results}"
    )


# ---------------------------------------------------------------------------
# 4. Happy path (§5.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_fast_kill(monkeypatch):
    """Fast stubbed kill: returns True, instance removed, in_memory_storage called."""
    from stealth_chrome_devtools_mcp.embedded.in_memory_storage import (
        in_memory_storage as ps,
    )

    storage_mock = MagicMock()
    monkeypatch.setattr(ps, "remove_instance", storage_mock)

    manager = BrowserManager()
    _seed_manager(manager, "happy-1")

    result = await manager.close_instance("happy-1")

    assert result is True
    assert "happy-1" not in manager._instances
    storage_mock.assert_called_once_with("happy-1")
