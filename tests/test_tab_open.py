"""F-940 — ``tab_open.open_tab`` opens a tab whether or not the browser's
session still discovers targets.

nodriver's ``Browser.get(new_tab=True)`` finds the tab it opened in
``browser.targets``, which only the ``targetCreated`` handler fills, and a
browser-level websocket that reconnected never asks Chrome for that event
again. These pin the opener against both sessions, without Chrome; the real
reconnect is ``tests/test_e2e_new_tab_after_reconnect.py``.
"""

from __future__ import annotations

from nodriver import Connection, Tab, cdp

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import tab_open


def _methods(browser: FakeBrowser) -> list[str]:
    return [frame["method"] for frame in browser.connection.cdp_frames]


def _entries_for(browser: FakeBrowser, target_id: object) -> list[object]:
    return [entry for entry in browser.targets if entry.target.target_id == target_id]


def _arrives_during_get_target_info(browser: FakeBrowser, make_entry) -> None:
    """Register *make_entry(info)* for the new target while ``getTargetInfo``
    is in flight — what a concurrent ``update_targets`` or a late
    ``targetCreated`` does in the one await the opener yields there."""
    answer = browser.connection._cdp_responses["get_target_info"]

    def _answer(name: str) -> object:
        info = answer(name)
        browser.targets.append(make_entry(info))
        return info

    browser.connection._cdp_responses["get_target_info"] = _answer


async def test_a_healthy_session_hands_back_the_tab_its_handler_registered():
    opened = FakeTab(url="https://fake.test/opened", target_id="T-new")
    browser = FakeBrowser(opened_tab=opened)

    tab = await tab_open.open_tab(browser, "https://fake.test/opened")

    assert tab is opened
    assert _methods(browser) == ["Target.createTarget"]
    assert browser.connection.cdp_frames[0]["params"] == {
        "url": "https://fake.test/opened",
        "newWindow": False,
        "enableBeginFrameControl": True,
    }
    assert _entries_for(browser, "T-new") == [opened]
    assert browser.update_targets_calls == 1


async def test_a_session_that_lost_discovery_still_gets_a_real_tab():
    """THE hermetic pin. Nothing registers the new target, so nodriver's own
    lookup raised ``StopIteration``; the opener asks Chrome instead and builds
    the ``Tab`` exactly as the ``targetCreated`` handler would have."""
    browser = FakeBrowser(discovers_targets=False)

    tab = await tab_open.open_tab(browser, "about:blank")

    assert type(tab) is Tab
    assert _methods(browser) == ["Target.createTarget", "Target.getTargetInfo"]
    assert tab.target.target_id == "T-opened-1"
    assert isinstance(tab.target, cdp.target.TargetInfo)
    assert tab.websocket_url == "ws://127.0.0.1:9222/devtools/page/T-opened-1"
    assert tab.browser is browser
    assert _entries_for(browser, "T-opened-1") == [tab]
    assert browser.update_targets_calls == 1


async def test_a_bare_connection_registered_meanwhile_is_replaced_in_place():
    """``update_targets`` registers what it discovers as a bare ``Connection``,
    which cannot be awaited or closed; the target must end up listed once, as
    the ``Tab``."""
    browser = FakeBrowser(discovers_targets=False)
    _arrives_during_get_target_info(
        browser,
        lambda info: Connection("ws://127.0.0.1:9222/devtools/page/x", target=info),
    )

    tab = await tab_open.open_tab(browser, "about:blank")

    assert type(tab) is Tab
    assert _entries_for(browser, "T-opened-1") == [tab]


async def test_a_tab_registered_meanwhile_is_the_one_handed_back():
    """A ``targetCreated`` that arrives late registers its own ``Tab``; building
    a second one beside it would list the target twice."""
    browser = FakeBrowser(discovers_targets=False)
    registered: list[Tab] = []

    def _late_tab(info: cdp.target.TargetInfo) -> Tab:
        registered.append(
            Tab("ws://127.0.0.1:9222/devtools/page/late", target=info, browser=browser)
        )
        return registered[0]

    _arrives_during_get_target_info(browser, _late_tab)

    tab = await tab_open.open_tab(browser, "about:blank")

    assert tab is registered[0]
    assert _entries_for(browser, "T-opened-1") == [tab]
