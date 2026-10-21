"""F-1012: a popup the PAGE opens is armed when Chrome creates it, not when a tool
first moves onto it.

``window.open`` and ``target=_blank`` create a target no tool asked for. F-1011 armed
every tab the backend tracks (focus emulation, headers, timezone, every armer), but a
popup was only reached through ``switch_tab``, so until then it was hidden: no
``rAF``, 1 Hz timers. ``tab_open.arm_popups`` listens for ``Target.targetCreated`` on
the browser connection and calls the ONE arming home, ``_arm_tracked_tab``. Events are
delivered with ``FakeTab._deliver`` (nodriver's own dispatch: ``callback(event, conn)``)
and carry a real ``cdp.target.TargetInfo``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nodriver import cdp

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import (
    browser_manager,
    tab_open,
    window_sizing,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.network_interceptor import (
    NetworkInterceptor,
)
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

FOCUS = "Emulation.setFocusEmulationEnabled"
IID = "iid-1"


def focus_frames(tab: FakeTab) -> list[dict[str, Any]]:
    return [f for f in tab.cdp_frames if f.get("method") == FOCUS]


def created(
    target_id: str, *, type_: str = "page", opener: str | None = "T-opener"
) -> cdp.target.TargetCreated:
    """Chrome's ``Target.targetCreated`` for a target with, or without, an opener."""
    return cdp.target.TargetCreated(
        cdp.target.TargetInfo(
            target_id=cdp.target.TargetID(target_id),
            type_=type_,
            title="",
            url="about:blank",
            attached=False,
            can_access_opener=opener is not None,
            opener_id=None if opener is None else cdp.target.TargetID(opener),
        )
    )


async def settle() -> None:
    """Let every popup arming task the listener scheduled run to its end."""
    await asyncio.sleep(0)
    if tab_open._POPUP_TASKS:
        await asyncio.gather(*tab_open._POPUP_TASKS)
        await asyncio.sleep(0)  # the tasks' done-callbacks drop them from the set


@pytest.fixture
def manager(monkeypatch) -> BrowserManager:
    instance = BrowserManager()
    instance._instances[IID] = {"options": BrowserOptions()}

    async def get_instance(instance_id):
        return instance._instances.get(instance_id)

    monkeypatch.setattr(instance, "get_instance", get_instance)
    return instance


@pytest.fixture
def launched(monkeypatch) -> tuple[FakeBrowser, FakeTab]:
    """A browser for the REAL ``_apply_post_launch`` (the spawn path); only its
    neighbours that need a real Chrome are stubbed."""
    monkeypatch.setattr(process_cleanup, "track_browser_process", lambda *a, **kw: None)

    async def no_measure(tab, options):
        return window_sizing.WindowSizeMetrics()

    async def no_reconcile(tab, executable):
        return None

    monkeypatch.setattr(window_sizing, "apply_and_measure", no_measure)
    monkeypatch.setattr(
        browser_manager, "reconcile_launched_browser_version", no_reconcile
    )
    main = FakeTab(target_id="T-main")
    return FakeBrowser(main_tab=main), main


async def spawn(manager: BrowserManager, browser: FakeBrowser, main: FakeTab) -> None:
    await manager._apply_post_launch(
        browser=browser,
        tab=main,
        options=BrowserOptions(),
        instance_id=IID,
        actual_user_data_dir=None,
        uses_custom_data_dir=False,
        browser_executable="chrome",
    )


def popup_in(browser: FakeBrowser, target_id: str, **kwargs: Any) -> FakeTab:
    """The ``Tab`` nodriver's own handler registers for a created target, ahead of
    ours (it was installed at ``Browser.start``)."""
    tab = FakeTab(target_id=target_id, **kwargs)
    browser.tabs.append(tab)
    return tab


async def test_a_page_opened_popup_is_armed_without_a_switch_tab(manager, launched):
    browser, main = launched
    await spawn(manager, browser, main)
    popup = popup_in(browser, "T-popup")

    browser.connection._deliver(created("T-popup"))
    await settle()

    frames = focus_frames(popup)
    assert len(frames) == 1
    assert frames[0]["params"] == {"enabled": True}


async def test_the_popup_gets_the_rest_of_the_arming_too(manager, launched):
    """The listener calls the one home, so a popup gets its headers and every
    armer, not focus emulation alone: a second, lesser way to arm would be a defect."""
    browser, main = launched
    manager._instances[IID]["options"] = BrowserOptions(extra_headers={"X-A": "b"})
    armed: list[tuple[str, str]] = []

    async def armer(tab, instance_id):
        armed.append((str(tab.target.target_id), instance_id))

    manager._tab_armers = (armer,)
    await spawn(manager, browser, main)
    popup = popup_in(browser, "T-popup")

    browser.connection._deliver(created("T-popup"))
    await settle()

    assert ("T-popup", IID) in armed
    assert "set_extra_http_headers" in popup.send_calls


async def test_a_tab_the_backend_opened_itself_is_not_armed_twice(manager, launched):
    """``open_tab`` makes a target with no opener; its caller arms it. The listener
    must leave it alone or ``new_tab`` would send everything twice."""
    browser, main = launched
    await spawn(manager, browser, main)
    own = popup_in(browser, "T-own")

    browser.connection._deliver(created("T-own", opener=None))
    await settle()

    assert focus_frames(own) == []


@pytest.mark.parametrize("type_", ["service_worker", "iframe", "shared_worker"])
async def test_only_a_page_target_is_a_popup(manager, launched, type_):
    browser, main = launched
    await spawn(manager, browser, main)
    other = popup_in(browser, "T-other")

    browser.connection._deliver(created("T-other", type_=type_))
    await settle()

    assert focus_frames(other) == []


async def test_a_refused_call_breaks_neither_the_popup_nor_the_listener(
    manager, launched
):
    browser, main = launched
    manager._instances[IID]["options"] = BrowserOptions(extra_headers={"X-A": "b"})
    await spawn(manager, browser, main)

    def refuse(name: str) -> None:
        raise RuntimeError("Emulation domain refused")

    refused = popup_in(
        browser, "T-refused", cdp_responses={"set_focus_emulation_enabled": refuse}
    )
    healthy = popup_in(browser, "T-healthy")

    browser.connection._deliver(created("T-refused"))
    browser.connection._deliver(created("T-healthy"))
    await settle()

    assert refused.send_calls.count("set_focus_emulation_enabled") == 1
    assert "set_extra_http_headers" in refused.send_calls
    assert len(focus_frames(healthy)) == 1


async def test_a_popup_that_vanished_before_it_could_be_armed_is_not_an_error(
    manager, launched
):
    """Ad popups close within milliseconds. Nodriver's handler has dropped the target
    by the time the task runs; nothing is raised and nothing is armed."""
    browser, main = launched
    await spawn(manager, browser, main)

    browser.connection._deliver(created("T-gone"))
    await settle()

    assert not tab_open._POPUP_TASKS


async def test_a_failing_armer_is_logged_not_raised(manager, launched):
    browser, main = launched

    async def broken(tab, instance_id):
        raise RuntimeError("armer broke")

    manager._tab_armers = (broken,)
    await spawn(manager, browser, main)
    popup = popup_in(browser, "T-popup")

    browser.connection._deliver(created("T-popup"))
    await settle()  # gather would raise here if the task had died with the error

    assert len(focus_frames(popup)) == 1


async def test_arming_a_popup_again_on_switch_tab_adds_no_second_capture_handler(
    manager, launched
):
    """``switch_tab`` reaches ``_arm_tracked_tab`` for the same tab later. Network
    capture is the armer that registers handlers, and it is keyed by target id."""
    browser, main = launched
    interceptor = NetworkInterceptor()
    interceptor._armed_with[IID] = []
    manager._tab_armers = (interceptor.arm_tab,)
    await spawn(manager, browser, main)
    popup = popup_in(browser, "T-popup")

    browser.connection._deliver(created("T-popup"))
    await settle()
    registered = len(popup.handlers)
    assert registered == 2  # RequestWillBeSent + ResponseReceived

    await manager._arm_tracked_tab(IID, popup)  # what switch_tab does

    assert len(popup.handlers) == registered


async def test_a_failing_instance_lookup_is_logged_not_raised(
    manager, launched, monkeypatch
):
    """``_arm_tracked_tab`` reads the instance OUTSIDE its own try, so a lookup that
    raises (a dead browser) reaches the listener's task: it must end there, logged,
    not as a never-retrieved task exception."""
    browser, main = launched
    await spawn(manager, browser, main)
    popup_in(browser, "T-popup")

    async def dead(instance_id):
        raise RuntimeError("browser is gone")

    monkeypatch.setattr(manager, "get_instance", dead)

    browser.connection._deliver(created("T-popup"))
    await settle()  # gather would raise here if the task had died with the error
