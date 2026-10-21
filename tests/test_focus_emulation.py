"""F-1011: every tab the backend drives is told to report itself focused and visible.

A minimized window or a background tab is hidden to the page: ``rAF`` stops and
timers clamp to 1 Hz (measured on Chrome 155: 49 ticks/5 s in the foreground, 5
ticks/5 s hidden, 47 ticks with ``Emulation.setFocusEmulationEnabled``). The call
is sent from the ONE home that already arms a tab's per-target overrides,
``BrowserManager._apply_tab_overrides``, which the spawn tab (``_apply_post_launch``)
and every tab tracked after it (``_arm_tracked_tab``: ``new_tab``, ``switch_tab``,
the replaced main tab) go through; a re-attached browser's tab is pinned in
``test_browser_reattach``. Best effort: a refusal must never fail a spawn or a tab.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded import (
    browser_manager,
    tab_open,
    window_sizing,
)
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.tool_sections import tabs

FOCUS = "Emulation.setFocusEmulationEnabled"
IID = "iid-1"


def focus_frames(tab: FakeTab) -> list[dict[str, Any]]:
    return [f for f in tab.cdp_frames if f.get("method") == FOCUS]


def refusing_tab() -> FakeTab:
    """A tab whose focus-emulation command raises when sent, as Chrome would on a
    target that is going away."""

    def refuse(name: str) -> None:
        raise RuntimeError("Emulation domain refused")

    return FakeTab(cdp_responses={"set_focus_emulation_enabled": refuse})


class _StubBrowser:
    # Truthy so the desktop_launch pid shim (a delegated-launch path) is skipped.
    _process = object()


@pytest.fixture
def manager(monkeypatch) -> BrowserManager:
    """A ``BrowserManager`` whose ``_apply_post_launch`` neighbours are stubbed,
    leaving the tab's own overrides as the live behaviour."""
    monkeypatch.setattr(process_cleanup, "track_browser_process", lambda *a, **kw: None)

    async def no_measure(tab, options):
        return window_sizing.WindowSizeMetrics()

    monkeypatch.setattr(window_sizing, "apply_and_measure", no_measure)

    async def no_reconcile(tab, executable):
        return None

    monkeypatch.setattr(
        browser_manager, "reconcile_launched_browser_version", no_reconcile
    )
    instance = BrowserManager()
    instance._instances[IID] = {"options": BrowserOptions()}
    return instance


async def _post_launch(manager: BrowserManager, tab: FakeTab):
    return await manager._apply_post_launch(
        browser=_StubBrowser(),
        tab=tab,
        options=BrowserOptions(),
        instance_id=IID,
        actual_user_data_dir=None,
        uses_custom_data_dir=False,
        browser_executable="chrome",
    )


async def test_the_spawn_tab_gets_focus_emulation(manager):
    tab = FakeTab()
    await _post_launch(manager, tab)
    frames = focus_frames(tab)
    assert len(frames) == 1
    assert frames[0]["params"] == {"enabled": True}


async def test_a_refused_focus_emulation_does_not_fail_the_spawn(manager):
    tab = refusing_tab()
    timezone_id, _metrics = await _post_launch(manager, tab)
    assert timezone_id is None
    assert tab.send_calls.count("set_focus_emulation_enabled") == 1


async def test_a_failing_header_command_cannot_skip_focus_emulation(manager):
    """Focus goes first: the spawn fails on the header error, but a tab tracked
    later (``_arm_tracked_tab`` logs and carries on) must still have been told."""
    tab = FakeTab(cdp_responses={"set_extra_http_headers": lambda n: 1 / 0})
    options = BrowserOptions(extra_headers={"X-A": "b"})
    with pytest.raises(ZeroDivisionError):
        await manager._apply_tab_overrides(tab, options)
    assert len(focus_frames(tab)) == 1


@pytest.fixture
def new_tab_world(manager, monkeypatch):
    """``new_tab`` end to end against the real manager: only the browser round trip
    that creates the target and the landing check are replaced."""
    tab = FakeTab()
    tab.target = SimpleNamespace(target_id="t-new", type_="page", title="")
    browser = SimpleNamespace()

    async def open_tab(_browser, url):
        return tab

    async def landing_ok(*args, **kwargs):
        return None

    async def require_browser(_manager, instance_id):
        return browser

    monkeypatch.setattr(tab_open, "open_tab", open_tab)
    monkeypatch.setattr(tabs, "_require_landing_ok", landing_ok)
    monkeypatch.setattr(tabs, "_require_browser", require_browser)

    async def get_instance(instance_id):
        # No liveness probe: the entry carries no real browser process.
        return manager._instances.get(instance_id)

    monkeypatch.setattr(manager, "get_instance", get_instance)
    monkeypatch.setattr(rt, "browser_manager", manager)
    return tab


async def test_new_tab_gets_focus_emulation(new_tab_world):
    result = await tabs.new_tab(IID)
    assert result["tab_id"] == "t-new"
    frames = focus_frames(new_tab_world)
    assert len(frames) == 1
    assert frames[0]["params"] == {"enabled": True}


async def test_a_refused_focus_emulation_does_not_fail_new_tab(
    new_tab_world, monkeypatch
):
    refusing = refusing_tab()
    refusing.target = new_tab_world.target

    async def open_tab(_browser, url):
        return refusing

    monkeypatch.setattr(tab_open, "open_tab", open_tab)
    result = await tabs.new_tab(IID)
    assert result["tab_id"] == "t-new"
    assert refusing.send_calls.count("set_focus_emulation_enabled") == 1
