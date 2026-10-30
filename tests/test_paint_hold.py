"""F-1019: a background tab a tool drives is held as captured, for a bounded time.

``Page.startScreencast`` with a frame that never comes keeps a hidden tab's compositor
producing frames (T-5903), at the page's full painting cost, so the hold goes only to a
tab a tool is acting on and ends ``LINGER_SECONDS`` after the last call. The real-Chrome
side is ``test_e2e_background_tab_frames.py``; this file pins the bookkeeping: one start
per tab however many calls, a moving deadline, release, no leaked task, a refusal that
never reaches the tool, a cancelled or failed start never sticks or drops a newer hold,
and a caller's own screencast is left alone.
"""

from __future__ import annotations

import asyncio
import gc
import weakref

import nodriver as uc
import pytest

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded import paint_hold, tab_binding, tab_open
from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import (
    CDPFunctionExecutor,
)

START = "Page.startScreencast"
STOP = "Page.stopScreencast"


@pytest.fixture(autouse=True)
def _short_linger(monkeypatch):
    monkeypatch.setattr(paint_hold, "LINGER_SECONDS", 0.2)
    yield
    for key in list(paint_hold._held):
        paint_hold.forget(key)
    getattr(
        paint_hold, "_browserless_caller_owned", set()
    ).clear()  # a caller's screencast must not leak into the next test


async def test_a_driven_tab_is_held_with_a_frame_that_never_comes():
    tab = FakeTab(target_id="t-1")
    await paint_hold.hold(tab)
    (frame,) = tab.sent(START)
    assert frame["params"]["everyNthFrame"] == paint_hold._NEVER_A_FRAME > 10**9


class SlowTab(FakeTab):
    """A tab whose commands take a loop turn, as Chrome's do."""

    async def send(self, cdp_obj, *args, **kwargs):
        await asyncio.sleep(0.01)
        return await super().send(cdp_obj, *args, **kwargs)


async def test_concurrent_calls_on_one_tab_send_one_start():
    tab = SlowTab(target_id="t-1")
    await asyncio.gather(*(paint_hold.hold(tab) for _ in range(8)))
    assert len(tab.sent(START)) == 1
    assert len(paint_hold._held) == 1


async def test_a_call_moves_the_deadline_and_the_release_follows_the_last_call():
    tab = FakeTab(target_id="t-1")
    await paint_hold.hold(tab)
    await asyncio.sleep(0.15)
    await paint_hold.hold(tab)  # 0.15 s in: the 0.2 s linger restarts
    await asyncio.sleep(0.15)
    assert tab.sent(STOP) == []  # 0.3 s after the first call, 0.15 after the last
    await asyncio.sleep(0.2)
    assert len(tab.sent(STOP)) == 1
    assert paint_hold._held == {}
    assert len(tab.sent(START)) == 1


async def test_a_released_tab_is_held_again_by_the_next_call():
    tab = FakeTab(target_id="t-1")
    await paint_hold.hold(tab)
    await asyncio.sleep(0.4)
    await paint_hold.hold(tab)
    assert len(tab.sent(START)) == 2
    assert len(tab.sent(STOP)) == 1


async def test_a_closed_tab_is_forgotten_and_its_timer_task_ends():
    tab = FakeTab(target_id="t-1")
    await paint_hold.hold(tab)
    task = paint_hold._held["t-1"].task
    tab_binding.forget_tab("iid", "t-1")
    await asyncio.sleep(0)
    assert paint_hold._held == {}
    assert task.cancelled()
    await asyncio.sleep(0.3)
    assert tab.sent(STOP) == []


async def test_a_new_session_for_the_same_target_is_held_afresh():
    old, new = FakeTab(target_id="t-1"), FakeTab(target_id="t-1")
    await paint_hold.hold(old)
    old_task = paint_hold._held["t-1"].task
    await paint_hold.hold(new)
    await asyncio.sleep(0)
    assert old_task.cancelled()
    assert len(new.sent(START)) == 1
    assert paint_hold._held["t-1"].tab is new


async def test_a_refused_start_never_reaches_the_tool_and_leaves_no_entry():
    tab = FakeTab(target_id="t-1", cdp_responses={"start_screencast": lambda n: 1 / 0})
    await paint_hold.hold(tab)
    assert paint_hold._held == {}


async def test_a_refused_stop_still_ends_the_timer():
    tab = FakeTab(target_id="t-1", cdp_responses={"stop_screencast": lambda n: 1 / 0})
    await paint_hold.hold(tab)
    task = paint_hold._held["t-1"].task
    await asyncio.sleep(0.4)
    assert task.done() and not task.cancelled()
    assert paint_hold._held == {}


async def test_every_page_tool_resolves_its_tab_through_the_hold(monkeypatch):
    """``tab_for_caller`` is THE resolver: whatever tab it returns is held."""
    tab = FakeTab(target_id="t-1")

    async def resolve(manager, instance_id):
        return tab

    monkeypatch.setattr(tab_binding, "_resolve", resolve)
    assert await tab_binding.tab_for_caller(object(), "iid") is tab
    assert len(tab.sent(START)) == 1


async def test_no_tab_is_no_hold(monkeypatch):
    async def resolve(manager, instance_id):
        return None

    monkeypatch.setattr(tab_binding, "_resolve", resolve)
    assert await tab_binding.tab_for_caller(object(), "iid") is None
    assert paint_hold._held == {}


class WedgedTab(FakeTab):
    """A tab whose renderer never answers."""

    async def send(self, cdp_obj, *args, **kwargs):
        await asyncio.Event().wait()


async def test_a_wedged_tab_costs_the_tool_a_bounded_wait_not_a_hang(monkeypatch):
    monkeypatch.setattr(paint_hold, "SEND_BUDGET_S", 0.05)
    tab = WedgedTab(target_id="t-1")
    await asyncio.wait_for(paint_hold.hold(tab), 1.0)
    # The start may still land when the renderer wakes, so the entry keeps its timer
    # (which sends the stop); it is not dropped as a refusal is.
    assert paint_hold._held["t-1"].task is not None


async def test_a_wedged_tab_pays_the_budget_once_not_on_every_call(monkeypatch):
    monkeypatch.setattr(paint_hold, "SEND_BUDGET_S", 0.3)
    tab = WedgedTab(target_id="t-1")
    await paint_hold.hold(tab)
    loop = asyncio.get_running_loop()
    began = loop.time()
    await paint_hold.hold(tab)
    assert loop.time() - began < 0.1


class GatedTab(FakeTab):
    """A tab whose start_screencast waits on ``gate`` and then fails if ``fail``."""

    def __init__(self, *args, fail=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = asyncio.Event()
        self.fail = fail

    async def send(self, cdp_obj, *args, **kwargs):
        if cdp_obj.gi_code.co_name == "start_screencast":
            await self.gate.wait()
            if self.fail:
                raise ConnectionError("refused")
        return await super().send(cdp_obj, *args, **kwargs)


async def test_a_cancelled_start_never_leaves_a_stuck_hold():
    """CancelledError during the start used to leave an entry with no timer, so every
    later call only moved its deadline and the tab painted until it closed."""
    tab = GatedTab(target_id="t-1")
    call = asyncio.create_task(paint_hold.hold(tab))
    await asyncio.sleep(0.02)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    entry = paint_hold._held["t-1"]
    assert entry.task is not None  # the cancel still arms the release
    tab.gate.set()
    await asyncio.sleep(0.4)  # linger 0.2 s
    assert paint_hold._held == {}
    assert len(tab.sent(STOP)) == 1


async def test_a_cancelled_start_replaced_by_a_newer_hold_leaves_the_newer_alone():
    old, new = GatedTab(target_id="t-1"), FakeTab(target_id="t-1")
    call = asyncio.create_task(paint_hold.hold(old))
    await asyncio.sleep(0.02)
    await paint_hold.hold(new)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert paint_hold._held["t-1"].tab is new
    assert paint_hold._held["t-1"].task is not None


async def test_a_start_failure_racing_a_re_hold_does_not_drop_the_newer_hold():
    """The failure path popped ``_held[key]`` by key, so it dropped the entry of a
    re-hold that replaced it, orphaning that hold's timer and its screencast."""
    old, new = GatedTab(target_id="t-1", fail=True), FakeTab(target_id="t-1")
    first = asyncio.create_task(paint_hold.hold(old))
    await asyncio.sleep(0.02)
    await paint_hold.hold(new)  # replaces the in-flight entry
    newer = paint_hold._held["t-1"]
    old.gate.set()
    await first  # the old start fails now, and is logged only
    assert paint_hold._held.get("t-1") is newer
    assert newer.task is not None
    await asyncio.sleep(0.4)
    assert len(new.sent(STOP)) == 1  # and it is released on time


async def test_forgetting_a_tab_during_its_start_leaves_nothing_behind():
    tab = GatedTab(target_id="t-1")
    call = asyncio.create_task(paint_hold.hold(tab))
    await asyncio.sleep(0.02)
    paint_hold.forget("t-1")
    tab.gate.set()
    await call
    assert paint_hold._held == {}
    assert [t for t in asyncio.all_tasks() if "_expire" in repr(t)] == []  # no timer
    await asyncio.sleep(0.3)
    assert tab.sent(STOP) == []  # a closed tab: nothing to release


async def test_a_rediscovered_target_stops_the_old_session_if_it_is_still_open():
    old, new = FakeTab(target_id="t-1"), FakeTab(target_id="t-1")
    await paint_hold.hold(old)
    await paint_hold.hold(new)
    assert len(old.sent(STOP)) == 1
    assert len(new.sent(START)) == 1


async def test_a_rediscovered_target_sends_nothing_to_a_closed_old_session():
    old, new = FakeTab(target_id="t-1"), FakeTab(target_id="t-1")
    await paint_hold.hold(old)
    old.closed = True
    await paint_hold.hold(new)
    assert old.sent(STOP) == []


async def test_a_refused_stop_of_the_old_session_still_holds_the_new_one():
    old = FakeTab(target_id="t-1", cdp_responses={"stop_screencast": lambda n: 1 / 0})
    new = FakeTab(target_id="t-1")
    await paint_hold.hold(old)
    await paint_hold.hold(new)
    assert paint_hold._held["t-1"].tab is new


async def test_a_callers_own_screencast_is_neither_overridden_nor_stopped():
    tab = FakeTab(target_id="t-1")
    executor = CDPFunctionExecutor()
    await paint_hold.hold(tab)
    result = await executor.execute_cdp_command(
        tab, "Page.startScreencast", {"format": "png"}
    )
    assert result["success"]
    assert paint_hold._held == {}
    await paint_hold.hold(tab)  # the next tool call must not start over theirs
    await asyncio.sleep(0.4)
    assert len(tab.sent(START)) == 2  # the hold's, then the caller's: nothing else
    assert tab.sent(STOP) == []


async def test_a_callers_stop_hands_the_tab_back_to_the_hold():
    tab = FakeTab(target_id="t-1")
    executor = CDPFunctionExecutor()
    await executor.execute_cdp_command(tab, "Page.startScreencast", {})
    await executor.execute_cdp_command(tab, "Page.stopScreencast", {})
    await paint_hold.hold(tab)
    assert "t-1" in paint_hold._held
    assert len(tab.sent(START)) == 2  # theirs, then the hold's


async def test_a_failed_caller_screencast_command_does_not_take_the_tab():
    tab = FakeTab(target_id="t-1", cdp_responses={"start_screencast": lambda n: 1 / 0})
    executor = CDPFunctionExecutor()
    result = await executor.execute_cdp_command(tab, "Page.startScreencast", {})
    assert not result["success"]
    await paint_hold.hold(tab)
    assert paint_hold._held == {} and "t-1" not in paint_hold._browserless_caller_owned


async def test_a_stale_timer_never_releases_a_newer_hold():
    old, new = FakeTab(target_id="t-1"), FakeTab(target_id="t-1")
    stale = paint_hold._Hold(old, deadline=0.0)
    paint_hold._held["t-1"] = newer = paint_hold._Hold(new, deadline=1e12)
    await paint_hold._expire("t-1", stale)
    assert paint_hold._held["t-1"] is newer
    assert old.sent(STOP) == [] and new.sent(STOP) == []


def _info(target_id, type_):
    return uc.cdp.target.TargetInfo(
        target_id=uc.cdp.target.TargetID(target_id),
        type_=type_,
        title="",
        url="about:blank",
        attached=True,
        can_access_opener=False,
    )


class _RealTab(uc.Tab):
    """A real nodriver ``Tab`` whose ``send`` records instead of reaching Chrome."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log = []

    async def send(self, cdp_obj, *args, **kwargs):
        self.log.append(cdp_obj)


def _real_browser():
    browser = uc.Browser(uc.Config(headless=True))  # built, never started
    browser.target = _info("browser-1", "browser")
    return browser


def _real_tab(browser, target_id):
    return _RealTab(target=_info(target_id, "page"), parent=browser, auto_attach=False)


async def _caller_screencast(tab):
    await CDPFunctionExecutor().execute_cdp_command(tab, START, {})


def _owned(browser, target_id):
    return tab_open.is_caller_owned(browser, target_id)


async def test_a_live_caller_screencast_on_a_live_tab_is_not_overridden():
    browser = _real_browser()
    tab = _real_tab(browser, "t-1")
    await _caller_screencast(tab)
    assert _owned(browser, "t-1")
    sent = len(tab.log)
    await paint_hold.hold(tab)
    assert paint_hold._held == {} and len(tab.log) == sent  # the hold sent nothing


async def test_close_instance_style_discard_takes_the_state_with_the_browser():
    browser = _real_browser()
    await _caller_screencast(_real_tab(browser, "t-1"))
    tab_open.mark_closed(browser)  # what close_instance does first
    ref = weakref.ref(browser)
    del browser
    gc.collect()
    assert ref() is None


async def test_crash_style_discard_leaves_nothing_behind_for_the_dead_browser():
    """No ``mark_closed`` and no ``update_targets``: the instance was only dropped
    (``_discard_instance_unlocked``). Its ids must no longer count as caller-owned
    (the same id on another browser is held again) and nothing of ours may keep the
    dead Browser alive."""
    browser = _real_browser()
    await _caller_screencast(_real_tab(browser, "t-1"))
    ref = weakref.ref(browser)
    del browser
    gc.collect()
    assert ref() is None
    successor = _real_tab(_real_browser(), "t-1")
    await paint_hold.hold(successor)
    assert "t-1" in paint_hold._held


async def test_close_tab_clears_the_callers_screencast_mark_on_a_live_browser():
    browser = _real_browser()
    tab = _real_tab(browser, "t-1")
    await _caller_screencast(tab)
    await paint_hold.hold(tab)
    assert paint_hold._held == {}  # theirs, so no hold
    tab_binding.forget_tab("iid", "t-1", browser=browser)  # what close_tab does
    await paint_hold.hold(_real_tab(browser, "t-1"))  # the id is not theirs any more
    assert "t-1" in paint_hold._held


async def test_the_close_tab_tool_clears_the_mark_on_the_browser_it_closed(monkeypatch):
    from stealth_chrome_devtools_mcp.embedded import tool_runtime
    from stealth_chrome_devtools_mcp.embedded.tool_sections import tabs

    browser = _real_browser()
    await _caller_screencast(_real_tab(browser, "t-1"))
    assert tab_open.is_caller_owned(browser, "t-1")

    class _Manager:
        async def get_browser(self, instance_id, touch_activity=False):
            return browser

        async def close_tab(self, instance_id, tab_id):
            return True

    monkeypatch.setattr(tool_runtime, "browser_manager", _Manager())
    assert await tabs.close_tab("iid", "t-1") is True
    assert not tab_open.is_caller_owned(browser, "t-1")
