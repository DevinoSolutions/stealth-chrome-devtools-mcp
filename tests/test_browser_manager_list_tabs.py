"""RELEASE-FIX-E (F-771) — ``list_tabs`` survives tab rediscovery.

The real-Chrome journey lives in ``test_e2e_interaction.py`` (the ~15-minute
integration lane). This module keeps the same guarantee on the FAST unit lane by
driving the real :class:`BrowserManager` against the one thing that breaks it: a
``Browser.tabs`` list holding both an attached ``Tab`` and a rediscovered raw
``Connection``.

Two failure modes are pinned, and the second one matters more:

1. **the crash** — ``await tab`` raises ``TypeError: object ... can't be used in
   'await' expression`` for a ``Connection`` (only ``Tab`` defines ``__await__``);
2. **the silent lie** — a listing that survives the crash but reports blank
   ``url``/``title`` is strictly worse than the crash, because a caller acts on
   it. ``Connection.__getattr__`` delegates to ``self.target``, so real values
   ARE available; every field here is therefore asserted BY VALUE, never by
   presence.

See ``audit/stage2/plan_RELEASE_FIX_E.md``.
"""

from __future__ import annotations

import pytest

from fakes import FakeAttachedTab, FakeBrowser, FakeDiscoveredTarget, fake_target
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

INSTANCE_ID = "i1"

# The tab the browser created in-process and still holds as a ``Tab``.
ATTACHED = {
    "target_id": "T-attached",
    "url": "https://fake.test/index.html",
    "title": "fixture-index-page",
}
# The tab ``update_targets()`` re-appended as a raw ``Connection`` after a close.
REDISCOVERED = {
    "target_id": "T-rediscovered",
    "url": "https://fake.test/interact.html",
    "title": "fixture-interact-page",
}


@pytest.fixture()
def manager_and_browser(monkeypatch):
    """A real ``BrowserManager`` whose one instance holds a post-close browser."""
    attached = FakeAttachedTab(fake_target(**ATTACHED))
    rediscovered = FakeDiscoveredTarget(fake_target(**REDISCOVERED))
    browser = FakeBrowser(tabs=[attached, rediscovered])
    manager = BrowserManager()

    async def _get_browser(instance_id: str, touch_activity: bool = False):
        return browser if instance_id == INSTANCE_ID else None

    monkeypatch.setattr(manager, "get_browser", _get_browser)
    return manager, browser, attached


async def test_list_tabs_lists_a_rediscovered_connection(manager_and_browser):
    """The crash pin: a ``Connection`` in ``browser.tabs`` must not blow up."""
    manager, _browser, _attached = manager_and_browser

    tabs = await manager.list_tabs(INSTANCE_ID)

    assert [t["tab_id"] for t in tabs] == [
        ATTACHED["target_id"],
        REDISCOVERED["target_id"],
    ]


async def test_list_tabs_metadata_is_real_for_a_rediscovered_connection(
    manager_and_browser,
):
    """The silent-lie pin: every field asserted BY VALUE, for BOTH object types.

    ``getattr(tab, "url", "") or ""`` would happily return ``""`` if the fix ever
    regressed into papering over a missing attribute with a default. It must
    return the target's real url instead.
    """
    manager, _browser, _attached = manager_and_browser

    tabs = {t["tab_id"]: t for t in await manager.list_tabs(INSTANCE_ID)}

    for expected in (ATTACHED, REDISCOVERED):
        record = tabs[expected["target_id"]]
        assert record["url"] == expected["url"]
        assert record["title"] == expected["title"]
        assert record["type"] == "page"


async def test_list_tabs_refreshes_targets_without_awaiting_each_tab(
    manager_and_browser,
):
    """``update_targets()`` already refreshed every field the loop reads, so the
    loop awaits nothing. Awaiting a ``Tab`` costs up to 0.5s EACH (``Tab.wait()``
    races the lifecycle event against an ``asyncio.sleep(0.5)``), so a per-tab
    await would make listing N tabs pay N waits for data already in hand.
    """
    manager, browser, attached = manager_and_browser

    await manager.list_tabs(INSTANCE_ID)

    assert browser.update_targets_calls == 1
    assert attached.awaited == 0


async def test_list_tabs_returns_empty_for_unknown_instance(manager_and_browser):
    """Unchanged contract: no browser → empty list (not an error shape)."""
    manager, _browser, _attached = manager_and_browser

    assert await manager.list_tabs("nope") == []
