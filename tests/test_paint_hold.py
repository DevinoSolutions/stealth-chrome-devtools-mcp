"""F-1019: a background tab a tool drives is held as captured, for a bounded time.

``Page.startScreencast`` with a frame that never comes keeps a hidden tab's compositor
producing frames (T-5903), at the page's full painting cost, so the hold goes only to a
tab a tool is acting on and ends ``LINGER_SECONDS`` after the last call. The real-Chrome
side is ``test_e2e_background_tab_frames.py``; this file pins the bookkeeping: one start
per tab however many calls, a moving deadline, release, no leaked task, a refusal that
never reaches the tool.
"""

from __future__ import annotations

import asyncio

import pytest

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded import paint_hold, tab_binding

START = "Page.startScreencast"
STOP = "Page.stopScreencast"


@pytest.fixture(autouse=True)
def _short_linger(monkeypatch):
    monkeypatch.setattr(paint_hold, "LINGER_SECONDS", 0.2)
    yield
    for key in list(paint_hold._held):
        paint_hold.forget(key)


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
