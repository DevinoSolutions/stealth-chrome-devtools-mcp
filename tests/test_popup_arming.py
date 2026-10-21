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
import logging
from typing import Any

import pytest
from nodriver import Connection, Tab, cdp

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import (
    browser_connect,
    browser_manager,
    process_exit,
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
#: What the popup budget is shrunk to, and how long a test lets that play out.
SHORT_POPUP_BUDGET = 0.05
TEST_WAIT_BUDGET = 5.0


class HangingTab(FakeTab):
    """A popup that closed mid-arm: nodriver's listener ended on the closed socket
    and never resolves the pending command, so ``send`` never returns."""

    async def send(self, cdp_obj, *args, **kwargs):
        await asyncio.Event().wait()


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
def manager() -> BrowserManager:
    """A real manager with its real ``get_instance`` (``launched`` fakes only the
    liveness probe): a stand-in lookup would hide an arming that runs before the
    instance is stored."""
    return BrowserManager()


@pytest.fixture
def launched(monkeypatch) -> tuple[FakeBrowser, FakeTab]:
    """A browser for the REAL ``spawn_browser`` (so the listener is registered by the
    code that registers it); only the steps that need a real Chrome are stubbed."""
    monkeypatch.setattr(process_cleanup, "track_browser_process", lambda *a, **kw: None)

    async def no_measure(tab, options):
        return {"actual": None}

    async def no_reconcile(tab, executable):
        return None

    async def no_hooks(self, tab, instance_id):
        return True

    main = FakeTab(target_id="T-main")
    browser = FakeBrowser(main_tab=main)

    async def launch(self, *args, **kwargs):
        return browser

    async def first_page(_browser):
        return main

    monkeypatch.setattr(window_sizing, "apply_and_measure", no_measure)
    monkeypatch.setattr(
        browser_manager, "reconcile_launched_browser_version", no_reconcile
    )
    monkeypatch.setattr(
        BrowserManager, "_resolve_launch_args", lambda *a, **k: ([], "chrome", [])
    )
    monkeypatch.setattr(BrowserManager, "_launch_browser", launch)
    monkeypatch.setattr(BrowserManager, "_setup_dynamic_hooks", no_hooks)
    monkeypatch.setattr(browser_connect, "first_page", first_page)
    monkeypatch.setattr(process_exit, "browser_is_alive", lambda _browser: True)
    return browser, main


async def spawn(
    manager: BrowserManager,
    browser: FakeBrowser,
    main: FakeTab,
    options: BrowserOptions | None = None,
) -> None:
    await manager.spawn_browser(options or BrowserOptions(), instance_id=IID)


def listeners(browser: FakeBrowser) -> list[Any]:
    """The ``Target.targetCreated`` handlers registered on the browser connection."""
    return [
        h for evt, h in browser.connection.handlers if evt is cdp.target.TargetCreated
    ]


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
    armed: list[tuple[str, str]] = []

    async def armer(tab, instance_id):
        armed.append((str(tab.target.target_id), instance_id))

    manager._tab_armers = (armer,)
    await spawn(manager, browser, main, BrowserOptions(extra_headers={"X-A": "b"}))
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
    await spawn(manager, browser, main, BrowserOptions(extra_headers={"X-A": "b"}))

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
    armed: list[object] = []

    async def armer(tab, instance_id):
        armed.append(tab)

    manager._tab_armers = (armer,)
    await spawn(manager, browser, main)
    wires = [browser.connection, *browser.tabs]
    sent = [len(wire.cdp_frames) for wire in wires]

    browser.connection._deliver(created("T-gone"))
    await settle()

    assert not tab_open._POPUP_TASKS
    assert armed == []
    assert [len(wire.cdp_frames) for wire in wires] == sent  # not one CDP frame


async def test_a_popup_that_never_answers_is_given_up_on_and_forgotten(
    manager, launched, monkeypatch, caplog
):
    """A popup that closes while its command is in flight is never answered. Without
    a bound the task would pin the Tab, its websocket and the instance for good."""
    monkeypatch.setattr(tab_open, "POPUP_ARM_BUDGET_SECONDS", SHORT_POPUP_BUDGET)
    browser, main = launched
    await spawn(manager, browser, main)
    browser.tabs.append(HangingTab(target_id="T-hung"))

    with caplog.at_level(logging.WARNING):
        browser.connection._deliver(created("T-hung"))
        await asyncio.wait_for(settle(), TEST_WAIT_BUDGET)

    assert not tab_open._POPUP_TASKS
    assert "gave up" in caplog.text


async def test_a_popup_nodriver_registered_as_a_bare_connection_is_armed_as_a_tab(
    manager, launched, monkeypatch
):
    """``update_targets`` registers what it discovers as a bare ``Connection``, which
    cannot be awaited or closed. ``find`` swaps in a ``Tab`` through ``_register``,
    and that is what gets armed, listed once in place of the bare entry."""
    browser, main = launched
    armed: list[object] = []

    async def armer(tab, instance_id):
        armed.append(tab)

    async def no_overrides(tab, options):
        return None

    manager._tab_armers = (armer,)
    await spawn(manager, browser, main)
    monkeypatch.setattr(manager, "_apply_tab_overrides", no_overrides)
    info = created("T-bare").target_info
    bare = Connection("ws://127.0.0.1:9222/devtools/page/T-bare", target=info)
    browser.tabs.append(bare)

    browser.connection._deliver(created("T-bare"))
    await settle()

    assert len(armed) == 1
    assert type(armed[0]) is Tab
    assert [t for t in browser.tabs if str(t.target.target_id) == "T-bare"] == armed


async def test_the_listener_is_registered_once_per_browser(manager, launched):
    """A second listener would arm every popup twice."""
    browser, main = launched
    await spawn(manager, browser, main)

    assert len(listeners(browser)) == 1


async def test_the_listener_is_registered_after_the_instance_is_stored(
    manager, launched, monkeypatch
):
    """``_arm_tracked_tab`` finds no instance before the store and arms nothing, so a
    popup in that window would be silently skipped."""
    browser, main = launched
    stored: list[bool] = []
    real = tab_open.arm_popups

    def spy(target, arm, *args):
        stored.append(args[0] in arm.__self__._instances)
        real(target, arm, *args)

    monkeypatch.setattr(tab_open, "arm_popups", spy)
    await spawn(manager, browser, main)

    assert stored == [True]


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
