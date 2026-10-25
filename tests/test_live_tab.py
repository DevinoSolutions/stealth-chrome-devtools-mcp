"""F-963 -- a stale TRACKED tab must not fail a spawn on a running session.

``spawn_browser(session="fleet")`` on a session that was already running failed
with ``Failed to setup network interception: server rejected WebSocket
connection: HTTP 500``. ``_adopted_instance_record`` armed interception on the
TRACKED tab, and a caller had closed that tab: Chrome answers a CDP socket
opened on a gone target id with HTTP 500. ``BrowserManager.live_tab`` is the one
recovery (the presence check ``get_navigation_tab`` already had, minus its
recycle threshold), and the adopted record now asks it.

Hermetic: a real ``BrowserManager`` over a ``FakeBrowser``; the interceptor is a
double that refuses a gone target id exactly as Chrome does.
"""

import pytest

from fakes import FakeAttachedTab, FakeBrowser, FakeTab, fake_target
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserOptions
from stealth_chrome_devtools_mcp.embedded.tool_sections import browser_management

INSTANCE_ID = "i-fleet"
GONE = {"target_id": "T-closed-by-a-caller", "url": "about:blank", "title": ""}
OTHER = {"target_id": "T-another-callers", "url": "https://fake.test/x", "title": "x"}


class _ChromeLikeInterceptor:
    """``setup_interception`` as Chrome treats it: a socket on a target id that no
    longer exists is refused with the HTTP 500 the field report quotes."""

    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser
        self.armed: list[str] = []

    async def setup_interception(self, tab, _instance_id, _block_resources=None):
        target_id = str(tab.target.target_id)
        if target_id not in {str(t.target.target_id) for t in self._browser.tabs}:
            raise RuntimeError(
                "Failed to setup network interception: "
                "server rejected WebSocket connection: HTTP 500"
            )
        self.armed.append(target_id)


def _manager(*, tracked_alive: bool):
    tracked = FakeAttachedTab(fake_target(**GONE))
    survivor = FakeAttachedTab(fake_target(**OTHER))
    browser = FakeBrowser(tabs=[survivor, *([tracked] if tracked_alive else [])])
    manager = BrowserManager()
    manager._instances[INSTANCE_ID] = {
        "browser": browser,
        "tab": tracked,
        "instance": BrowserInstance(instance_id=INSTANCE_ID),
        "options": BrowserOptions(user_data_dir="C:/sessions/fleet"),
        "navigation_count": 0,
    }
    return manager, browser, tracked, survivor


async def test_an_adopted_instance_whose_tracked_tab_is_gone_gets_a_fresh_armed_tab(
    patched_server,
):
    """THE pin. Armed on the tracked tab (``get_tab``), the interceptor refuses
    and the spawn fails; armed on ``live_tab``'s, it succeeds."""
    manager, browser, tracked, _survivor = _manager(tracked_alive=False)
    interceptor = _ChromeLikeInterceptor(browser)
    patched_server(browser_manager=manager, network_interceptor=interceptor)

    record = await browser_management._adopted_instance_record(INSTANCE_ID, ["*.png"])

    assert record["instance_id"] == INSTANCE_ID
    fresh = manager._instances[INSTANCE_ID]["tab"]
    assert fresh is not tracked
    assert interceptor.armed == [str(fresh.target.target_id)]
    assert browser.opened == ["about:blank"]


async def test_recovery_never_closes_another_callers_tab(patched_server):
    manager, browser, _tracked, survivor = _manager(tracked_alive=False)
    patched_server(
        browser_manager=manager, network_interceptor=_ChromeLikeInterceptor(browser)
    )

    await browser_management._adopted_instance_record(INSTANCE_ID, None)

    assert survivor in browser.tabs
    assert not [f for f in browser.cdp_frames if f["method"] == "Target.closeTarget"]


async def test_a_live_tracked_tab_is_kept_and_nothing_is_opened(patched_server):
    manager, browser, tracked, _survivor = _manager(tracked_alive=True)
    interceptor = _ChromeLikeInterceptor(browser)
    patched_server(browser_manager=manager, network_interceptor=interceptor)

    await browser_management._adopted_instance_record(INSTANCE_ID, None)

    assert manager._instances[INSTANCE_ID]["tab"] is tracked
    assert interceptor.armed == [GONE["target_id"]]
    assert browser.opened == []


class TestLiveTab:
    async def test_it_has_no_recycle_threshold(self):
        """``get_navigation_tab`` recycles a tab after N navigations; ``live_tab``
        is only the presence check, so a busy live tab is returned as it is."""
        manager, browser, tracked, _survivor = _manager(tracked_alive=True)
        manager._instances[INSTANCE_ID]["navigation_count"] = 10_000

        assert await manager.live_tab(INSTANCE_ID) is tracked
        assert browser.opened == []

    async def test_get_navigation_tab_still_recycles_past_its_threshold(self):
        manager, browser, _gone, _survivor = _manager(tracked_alive=False)
        tracked = FakeTab(target_id="T-busy")  # closeable, as a real Tab is
        browser.tabs.append(tracked)
        manager._instances[INSTANCE_ID]["tab"] = tracked
        manager._instances[INSTANCE_ID]["navigation_count"] = (
            manager.NAVIGATION_RECYCLE_THRESHOLD
        )

        replaced = await manager.get_navigation_tab(INSTANCE_ID)

        assert replaced is not tracked
        assert browser.opened == ["about:blank"]

    async def test_an_unknown_instance_is_none(self):
        assert await BrowserManager().live_tab("nope") is None

    @pytest.mark.parametrize("alive", [True, False])
    async def test_get_navigation_tab_answers_live_tab(self, alive):
        manager, _browser, tracked, _survivor = _manager(tracked_alive=alive)

        answer = await manager.get_navigation_tab(INSTANCE_ID)

        assert (answer is tracked) is alive
