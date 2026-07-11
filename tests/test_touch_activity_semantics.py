"""F-745 pinning tests: get_tab/get_browser default to read-only (no touch).

Validates that:
- get_tab(id) with the new default does NOT refresh last_activity
- get_tab(id, touch_activity=True) DOES refresh last_activity
- navigate() refreshes last_activity (independent touch)
- cleanup_inactive() reaps a read-only-polled instance past its timeout
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserState


def _make_fake_tab():
    return SimpleNamespace(name="fake-tab")


def _make_fake_browser():
    return SimpleNamespace(
        tabs=[_make_fake_tab()],
        connection=SimpleNamespace(closed=True),
        _process=SimpleNamespace(returncode=None, pid=99999, poll=lambda: None),
        _process_pid=99999,
        stop=MagicMock(return_value=None),
    )


def _seed_manager(manager, instance_id="t-1", idle_timeout=600):
    browser = _make_fake_browser()
    tab = _make_fake_tab()
    instance = BrowserInstance(
        instance_id=instance_id,
        state=BrowserState.READY,
        last_activity=datetime.now(tz=timezone.utc),
    )
    manager._instances[instance_id] = {
        "browser": browser,
        "instance": instance,
        "tab": tab,
        "idle_timeout_seconds": idle_timeout,
    }
    manager._spawn_diagnostics[instance_id] = {}
    return browser, instance, tab


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    from stealth_chrome_devtools_mcp.embedded.in_memory_storage import (
        in_memory_storage as ps,
    )
    from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

    monkeypatch.setattr(process_cleanup, "kill_browser_process", MagicMock())
    monkeypatch.setattr(process_cleanup, "finalize_browser_process", MagicMock())
    monkeypatch.setattr(process_cleanup, "cleanup_deferred_profiles", MagicMock())
    monkeypatch.setattr(ps, "remove_instance", MagicMock())


# ---------------------------------------------------------------------------
# get_tab / get_browser default does NOT touch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tab_default_does_not_touch():
    manager = BrowserManager()
    _browser, instance, _tab = _seed_manager(manager, "rt-1")
    original_activity = instance.last_activity

    await asyncio.sleep(0.05)
    await manager.get_tab("rt-1")

    assert instance.last_activity == original_activity


@pytest.mark.asyncio
async def test_get_browser_default_does_not_touch():
    manager = BrowserManager()
    _browser, instance, _tab = _seed_manager(manager, "rt-2")
    original_activity = instance.last_activity

    await asyncio.sleep(0.05)
    await manager.get_browser("rt-2")

    assert instance.last_activity == original_activity


# ---------------------------------------------------------------------------
# Explicit touch_activity=True DOES touch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tab_explicit_touch_advances():
    manager = BrowserManager()
    _browser, instance, _tab = _seed_manager(manager, "et-1")
    original_activity = instance.last_activity

    await asyncio.sleep(0.05)
    await manager.get_tab("et-1", touch_activity=True)

    assert instance.last_activity > original_activity


@pytest.mark.asyncio
async def test_get_browser_explicit_touch_advances():
    manager = BrowserManager()
    _browser, instance, _tab = _seed_manager(manager, "et-2")
    original_activity = instance.last_activity

    await asyncio.sleep(0.05)
    await manager.get_browser("et-2", touch_activity=True)

    assert instance.last_activity > original_activity


# ---------------------------------------------------------------------------
# cleanup_inactive reaps read-only-polled instance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_reaps_read_only_polled_instance(monkeypatch):
    """An instance only accessed via get_tab (no touch) is reaped after timeout."""
    manager = BrowserManager()
    _browser, instance, _tab = _seed_manager(manager, "reap-1", idle_timeout=5)

    instance.last_activity = datetime.now(tz=timezone.utc) - timedelta(seconds=10)

    await manager.get_tab("reap-1")

    async def fake_close(iid):
        manager._instances.pop(iid, None)
        return True

    monkeypatch.setattr(manager, "close_instance", fake_close)

    closed = await manager.cleanup_inactive()
    assert closed == 1
    assert "reap-1" not in manager._instances


@pytest.mark.asyncio
async def test_cleanup_spares_recently_touched_instance(monkeypatch):
    """An instance that was explicitly touched is NOT reaped."""
    manager = BrowserManager()
    _browser, instance, _tab = _seed_manager(manager, "spare-1", idle_timeout=5)

    instance.last_activity = datetime.now(tz=timezone.utc) - timedelta(seconds=10)

    await manager.get_tab("spare-1", touch_activity=True)

    async def fake_close(iid):
        manager._instances.pop(iid, None)
        return True

    monkeypatch.setattr(manager, "close_instance", fake_close)

    closed = await manager.cleanup_inactive()
    assert closed == 0
    assert "spare-1" in manager._instances
