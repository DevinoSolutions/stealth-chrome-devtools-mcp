"""F-940 / F-1012 -- ``tab_open``: a new tab is found through ``update_targets``, and
Target discovery is switched on for the events popups and tab ends ride on.

nodriver 0.50's ``Browser.update_targets`` asks ``Target.getTargets`` and builds a
real ``Tab`` for every target it has not seen, so ``open_tab`` never depends on a
``targetCreated`` event (the 0.47 lookup died with ``StopIteration`` once the
browser websocket had reconnected; the real wire is
``tests/test_e2e_new_tab_after_reconnect.py``). These pin it without Chrome.
"""

from __future__ import annotations

import asyncio

import pytest
from nodriver import cdp

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import tab_open
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = pytest.mark.asyncio


def _methods(browser: FakeBrowser) -> list[str]:
    return [frame["method"] for frame in browser.cdp_frames]


def _entries_for(browser: FakeBrowser, target_id: object) -> list[object]:
    return [entry for entry in browser.targets if entry.target.target_id == target_id]


async def test_a_new_tab_is_the_one_update_targets_registers():
    opened = FakeTab(url="https://fake.test/opened", target_id="T-new")
    browser = FakeBrowser(opened_tab=opened)

    tab = await tab_open.open_tab(browser, "https://fake.test/opened")

    assert tab is opened
    assert _methods(browser) == ["Target.createTarget"]
    assert browser.cdp_frames[0]["params"] == {
        "url": "https://fake.test/opened",
        "newWindow": False,
        "enableBeginFrameControl": True,
    }
    assert _entries_for(browser, "T-new") == [opened]
    assert browser.update_targets_calls == 1


async def test_a_tab_that_closed_before_it_was_listed_is_a_named_error():
    browser = FakeBrowser(lists_new_targets=False)

    with pytest.raises(ToolError, match="did not list it"):
        await tab_open.open_tab(browser, "about:blank")


async def test_a_refused_create_target_raises_what_chrome_said():
    browser = FakeBrowser(create_target_error=RuntimeError("refused"))

    with pytest.raises(RuntimeError, match="refused"):
        await tab_open.open_tab(browser, "about:blank")
    assert browser.update_targets_calls == 0


async def test_find_reads_what_update_targets_registered():
    page = FakeTab(target_id="T-page")
    browser = FakeBrowser(tabs=[page])

    assert tab_open.find(browser, "T-page") is page
    assert tab_open.find(browser, "T-missing") is None


async def test_arm_popups_turns_target_discovery_on():
    """0.50's ``Browser.start`` never sends it, and without it Chrome sends the
    browser connection no ``targetCreated`` / ``targetDestroyed`` at all (measured on
    Chrome 154, ``tests/test_e2e_background_tab_frames.py``)."""
    browser = FakeBrowser()

    async def arm(*_args: object) -> None:
        return None

    await tab_open.arm_popups(browser, arm)

    (frame,) = [
        f for f in browser.cdp_frames if f["method"] == "Target.setDiscoverTargets"
    ]
    assert frame["params"] == {"discover": True}


async def test_a_browser_that_refuses_discovery_is_logged_not_failed(caplog):
    browser = FakeBrowser()

    def refuse(_name: str) -> None:
        raise RuntimeError("Target domain refused")

    browser._cdp_responses["set_discover_targets"] = refuse

    async def arm(*_args: object) -> None:
        return None

    with caplog.at_level("WARNING"):
        await tab_open.arm_popups(browser, arm)  # does not raise

    assert "could not enable Target discovery" in caplog.text
    assert any(evt is cdp.target.TargetCreated for evt, _ in browser.handlers)


async def test_a_discovery_that_never_answers_is_bounded(monkeypatch):
    monkeypatch.setattr(tab_open, "POPUP_ARM_BUDGET_SECONDS", 0.05)
    browser = FakeBrowser()

    async def hang(*_a, **_k):
        await asyncio.Event().wait()

    browser.send = hang

    async def arm(*_args: object) -> None:
        return None

    await asyncio.wait_for(tab_open.arm_popups(browser, arm), 5.0)
