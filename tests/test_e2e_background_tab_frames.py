"""T-5903 E2E - a background tab a tool is driving still gets frames (real Chrome).

Chrome tells a page in a background tab it is ``hidden``: no ``requestAnimationFrame``
(so no animation, and nothing that waits for a paint) and 1 Hz timers. F-1011 keeps
every driven tab ``visible`` with ``Emulation.setFocusEmulationEnabled`` and F-1012
arms a page-opened popup the same way, but the hermetic tier can only check that the
CDP command was SENT; whether Chrome then really paints a background tab is only
visible here.

Two things hold a background tab back, and each has its own pin. Unarmed, it reads
``hidden`` and paints nothing (F-1011). Armed but visible-by-emulation, a page that
PAINTS every frame (an animation, a progress bar, anything waiting for a paint)
still got ~1 frame per second, because the compositor of a hidden widget makes no
frames for it; a page that changes nothing was not held back, which is why a bare
frame counter never saw it (T-5903, F-1019). ``paint_hold`` marks a tab captured
while a tool is acting on it, and releases it ``LINGER_SECONDS`` after the last call,
because painting is the cost: ~45% of a core per painting background tab.

Measured on Chrome 155, Windows, 2 s windows: ~110-120 frames held, 2 not held,
~120 in the foreground tab. The bar is a quarter of the foreground rate: a loaded
runner lowers the count, never to the unheld tab's 2.

The fall back is read on a PAIR, the driven tab and an untouched control measured at
the same time, and is backed by a check that counts no frame at all: once the linger
and the stop's send budget are over, the driven tab is no longer held and
``Page.stopScreencast`` was sent to it. Frame counts need Chrome to throttle a
background tab, which the macOS CI runner does only some of the time (T-10026).

Style follows the plan_E2E suite: ``e2e_helpers`` mechanism, bounded waits, every
spawn closed in ``finally``. Hermetic: ``about:blank`` and ``window.open`` only.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    runtime,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import display_context, paint_hold, tab_open

pytestmark = integration_pytestmark()

#: Frames a tab must paint in :data:`MEASURE_MS`. A throttled tab paints 2-10, a held
#: one ~110; a quarter of the foreground rate leaves a slow runner room.
MIN_FRAMES = 30
MAX_UNHELD_FRAMES = 15
MEASURE_MS = 2000
#: A popup is armed from a ``Target.targetCreated`` event, so its first frames may
#: precede the arming; poll this long before calling the tab hidden.
ARMING_BUDGET_SECONDS = 15.0
#: The test seam for ``paint_hold.LINGER_SECONDS``. It must comfortably EXCEED
#: :data:`MEASURE_MS`: a reading of a held tab starts a CDP round trip after the call
#: that held it and runs :data:`MEASURE_MS`, so a linger of the same length is over
#: before the reading is (macOS headed read 14 frames against the 30 bar at 2.0 s).
#: It also leaves room for one retry reading while armed (:func:`_held_rate`).
SHORT_LINGER_SECONDS = 5.0
#: What a held reading keeps back from the end of the linger (the clock starts BEFORE
#: the holding call, so this covers the hold timer's own scheduling, not the call).
LINGER_MARGIN_SECONDS = 0.5
#: Readings of :data:`MEASURE_MS` each allowed for a tab to fall back once its hold
#: ends (:func:`_fallen_back_rate`): Chrome keeps painting background tabs for a
#: few seconds after a hold is released, and longer on a slow runner (T-10026).
FALLBACK_READINGS = 4

#: Paints the page every frame (a page that changes nothing is not held back) and counts.
START_LOOP_JS = """
window.__frames = 0;
(function frame() {
  window.__frames++;
  document.body.textContent = "frame " + window.__frames;
  requestAnimationFrame(frame);
})();
return true;
"""

#: :data:`START_LOOP_JS` as a CDP expression that stops once ``window.__stopped`` is set.
PROBE_LOOP_JS = START_LOOP_JS.replace("return true;", "true;").replace(
    "requestAnimationFrame(frame);",
    "if (!window.__stopped) requestAnimationFrame(frame);",
)

FRAMES_JS = f"""
return await new Promise(resolve => {{
  let frames = 0, live = true;
  // Writing the page every frame is the point: a page that changes nothing is
  // not held back, one that PAINTS is (F-1019).
  const frame = () => {{
    frames++;
    document.body.textContent = "frame " + frames;
    if (live) requestAnimationFrame(frame);
  }};
  requestAnimationFrame(frame);
  setTimeout(() => {{
    live = false;
    resolve({{frames, visibility: document.visibilityState}});
  }}, {MEASURE_MS});
}});
"""


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


def _modes() -> list:
    """Headless always; headed only where a window could be seen (F-808)."""
    headed = pytest.param(
        False,
        id="headed",
        marks=pytest.mark.skipif(
            not display_context.can_show_windows(),
            reason="no display to show a headed window on",
        ),
    )
    return [pytest.param(True, id="headless"), headed]


async def _frames(iid: str, tab_id: str) -> dict:
    """Frames in :data:`MEASURE_MS`, measured THROUGH a tool call (so it drives the tab)."""
    execute = get_fn("execute_script")
    r = await execute(instance_id=iid, script=FRAMES_JS, tab_id=tab_id)
    assert isinstance(r, dict) and r.get("success") is True, r
    return r["result"]


async def _frames_when_armed(iid: str, tab_id: str) -> dict:
    """:func:`_frames` until it clears the bar, bounded; the last answer if never."""
    deadline = time.monotonic() + ARMING_BUDGET_SECONDS
    while True:
        answer = await _frames(iid, tab_id)
        if answer["frames"] >= MIN_FRAMES or time.monotonic() >= deadline:
            return answer
        await asyncio.sleep(0.25)


async def _undriven_rate(iid: str, tab_id: str) -> int:
    """Frames the loop in the tab counts in :data:`MEASURE_MS`, read straight from the
    page and NOT through a tool, so reading it does not drive (hold) the tab."""
    browser = await runtime.browser_manager.get_browser(iid)
    tab = tab_open.find(browser, tab_id)

    async def counted() -> int:
        value = await tab.evaluate("window.__frames", return_by_value=True)
        return int(getattr(value, "value", value))

    before = await counted()
    await asyncio.sleep(MEASURE_MS / 1000)
    return await counted() - before


async def _held_rate(iid: str, tab_id: str, held_at: float) -> int:
    """Frames in :data:`MEASURE_MS` of a tab a tool JUST acted on, read straight from
    the page (no tool call, so nothing re-holds it). Arming is not instant, so a
    reading under the bar is repeated while a whole further reading still ends inside
    the linger; bounded, and never past it: the hold is not extended to make it pass.
    *held_at* is ``time.monotonic()`` taken BEFORE the holding call, so a slow call
    shortens the window instead of letting a retry run past the hold."""
    hold_ends = held_at + SHORT_LINGER_SECONDS - LINGER_MARGIN_SECONDS
    best = 0
    while True:
        best = max(best, await _undriven_rate(iid, tab_id))
        if best >= MIN_FRAMES or time.monotonic() + MEASURE_MS / 1000 > hold_ends:
            return best


async def _display_paints(iid: str, tab_id: str) -> int:
    """Best frame count of a foreground tab NO tool touches, polled up to
    :data:`ARMING_BUDGET_SECONDS`: whether this display paints a window at all."""
    browser = await runtime.browser_manager.get_browser(iid)
    tab = tab_open.find(browser, tab_id)
    await tab.evaluate(PROBE_LOOP_JS, return_by_value=True)
    best, deadline = 0, time.monotonic() + ARMING_BUDGET_SECONDS
    try:
        while best < MIN_FRAMES and time.monotonic() < deadline:
            best = max(best, await _undriven_rate(iid, tab_id))
    finally:
        # A foreground loop left running is a variable the other tabs' readings
        # never had: the first CI run with it running read an undriven background
        # tab at 120 frames headless on Linux, where it had read throttled before.
        await tab.evaluate("window.__stopped = true", return_by_value=True)
    return best


def _skip_if_display_does_not_paint(headless: bool, painted: int) -> None:
    """The premise every node rests on is a PLATFORM fact: the display paints a
    foreground tab. Headed Chrome 156 on the Linux CI cell's bare Xvfb (no window
    manager) painted 0 frames in every tab of one run while Chrome 155 there
    painted ~120, so it is measured; where the display paints, a held tab that
    does not is a product failure and still fails."""
    if not headless and sys.platform.startswith("linux") and painted < MIN_FRAMES:
        pytest.skip(
            f"headed Chrome on this Linux display painted {painted} frames in "
            f"{MEASURE_MS} ms in a foreground tab no tool touched (bare Xvfb, no "
            f"window manager): there is nothing to hold or throttle here"
        )


async def _fallen_back_rate(iid: str, tab_id: str) -> int:
    """The tab's undriven rate once it has fallen back: readings are repeated while
    they are over the bar, for at most :data:`FALLBACK_READINGS`, and the last one
    is returned. Bounded, so a hold that is never released still fails."""
    rate = await _undriven_rate(iid, tab_id)
    for _ in range(FALLBACK_READINGS - 1):
        if rate <= MAX_UNHELD_FRAMES:
            break
        rate = await _undriven_rate(iid, tab_id)
    return rate


def _sent_methods(monkeypatch, tab) -> list[str]:
    """Spy on *tab*'s ``send``: the CDP method of every command its transport takes,
    in order. A pass-through: the caller's own command generator is driven, and only
    the method of its first step is read on the way."""
    sent: list[str] = []
    original = tab.send

    def tapped(cdp_obj):
        command = next(cdp_obj)
        sent.append(command.get("method", ""))
        reply = yield command
        try:
            cdp_obj.send(reply)
        except StopIteration as done:
            return done.value
        raise RuntimeError("a CDP command generator asked for a second reply")

    async def spy(cdp_obj, *args, **kwargs):
        return await original(tapped(cdp_obj), *args, **kwargs)

    monkeypatch.setattr(tab, "send", spy)
    return sent


async def _assert_released(tab_id: str, sent: list[str], by: float) -> None:
    """The release, read without counting a frame: by *by* (a ``time.monotonic()``)
    the tab is no longer held AND ``Page.stopScreencast`` went to it. Polled, so a
    release that comes early passes early. ``paint_hold`` has no public query, so
    this reads its private ``_held`` (target id -> hold), as tests/test_paint_hold.py
    does; *sent* is :func:`_sent_methods`' record."""
    while True:
        held = tab_id in paint_hold._held
        stopped = "Page.stopScreencast" in sent
        if (not held and stopped) or time.monotonic() >= by:
            break
        await asyncio.sleep(0.1)
    assert not held and stopped, (
        f"the hold on the driven tab was not released "
        f"{SHORT_LINGER_SECONDS + paint_hold.SEND_BUDGET_S:.1f} s after the last "
        f"call (linger + the stop's send budget): still held={held}, "
        f"Page.stopScreencast sent={stopped}"
    )


def _decide_fallback(driven_rate: int, control_rate: int) -> None:
    """The fall back, decided on the PAIR read at the same time. The control is a
    tab no tool touched, so it reads whether Chrome throttles a background tab at
    that moment. Throttled control and an unthrottled driven tab: a hold that was
    never released, a FAIL everywhere. Both throttled: a pass. An unthrottled
    control means the premise every frame count rests on was lost mid-test: on
    macOS (whose CI runner throttles only some of the time, T-10026) that skips by
    name with both numbers, elsewhere (measured throttled) it FAILS. A hold left
    on reads as that last case too, because a held tab un-throttles its painting
    siblings (F-1019; measured here with the release removed: driven 114, control
    57), which is why :func:`_assert_released` runs first, on every platform."""
    if control_rate > MAX_UNHELD_FRAMES:
        message = (
            f"the throttle premise was lost on this host mid-test: after the linger "
            f"the untouched control tab counted {control_rate} frames in "
            f"{MEASURE_MS} ms (throttled is <= {MAX_UNHELD_FRAMES}) while the driven "
            f"tab, whose hold was released, counted {driven_rate}, so no frame "
            f"count tells a released hold here (or another tab's hold is "
            f"un-throttling its siblings)"
        )
        if sys.platform == "darwin":
            pytest.skip(message)
        pytest.fail(message)
    assert driven_rate <= MAX_UNHELD_FRAMES, (
        f"a hold that was never released: after the linger the driven tab counted "
        f"{driven_rate} frames in {MEASURE_MS} ms while the untouched control tab, "
        f"read at the same time, counted {control_rate} (throttled is "
        f"<= {MAX_UNHELD_FRAMES})"
    )


async def _spawn(tmp_path, headless: bool) -> str:
    result = await get_fn("spawn_browser")(
        headless=headless,
        user_data_dir=str(tmp_path / "bg-frames"),
        **sandbox_kwargs(),
    )
    return result["instance_id"]


@pytest.mark.parametrize("headless", _modes())
async def test_a_driven_background_tab_keeps_painting(tmp_path, headless):
    """``new_tab`` twice: the first tab is a background tab; a tool call acts on it."""
    new_tab, close = get_fn("new_tab"), get_fn("close_instance")
    iid = await _spawn(tmp_path, headless)
    try:
        background = (await new_tab(instance_id=iid, url="about:blank"))["tab_id"]
        foreground = (await new_tab(instance_id=iid, url="about:blank"))["tab_id"]

        _skip_if_display_does_not_paint(
            headless, await _display_paints(iid, foreground)
        )
        control = await _frames_when_armed(iid, foreground)
        assert control["frames"] >= MIN_FRAMES, f"control tab did not paint: {control}"

        answer = await _frames_when_armed(iid, background)
        assert answer["visibility"] == "visible", answer
        assert answer["frames"] >= MIN_FRAMES, (
            f"a driven background tab painted {answer['frames']} frames in "
            f"{MEASURE_MS} ms against {control['frames']} in the foreground tab: "
            f"{answer}"
        )
    finally:
        await close(instance_id=iid)


@pytest.mark.parametrize("headless", _modes())
async def test_an_undriven_background_tab_stays_throttled_until_a_tool_acts(
    tmp_path, headless, monkeypatch
):
    """The cost bound: painting is paid for only while a tool is driving the tab.

    A tab no tool acts on stays at its throttled rate, a driven one paints, and one
    that was driven falls back ``LINGER_SECONDS`` after the last call. The fall
    back is checked twice: without frames (the hold is gone and
    ``Page.stopScreencast`` was sent, on every platform), and by frames, on the
    driven tab and an untouched control read at the same time. "Stays throttled" is
    measured, not assumed: where Chrome does not throttle an untouched background
    tab (the macOS CI runner has no display), at startup or after the linger, there
    is nothing for a hold to lift, so the frame counts skip by name there and FAIL
    elsewhere; the product claim (a tool-driven background tab paints) is the two
    other nodes', which run there.
    """
    monkeypatch.setattr(paint_hold, "LINGER_SECONDS", SHORT_LINGER_SECONDS)
    new_tab, close = get_fn("new_tab"), get_fn("close_instance")
    execute = get_fn("execute_script")
    iid = await _spawn(tmp_path, headless)
    try:
        driven = (await new_tab(instance_id=iid, url="about:blank"))["tab_id"]
        undriven = (await new_tab(instance_id=iid, url="about:blank"))["tab_id"]
        # Both are now background.
        foreground = (await new_tab(instance_id=iid, url="about:blank"))["tab_id"]

        # The un-driven tab: its loop is started straight through CDP, never a tool.
        browser = await runtime.browser_manager.get_browser(iid)
        await tab_open.find(browser, undriven).evaluate(
            START_LOOP_JS.replace("return true;", "true;"), return_by_value=True
        )
        # The control's premise is a PLATFORM fact, not a product one: a tab no tool
        # touched (nothing here holds it) must read throttled. Chrome on the macOS CI
        # runner has no display link (``CVDisplayLinkCreateWithCGDisplay failed``)
        # and does not throttle a hidden tab's rAF: 62 frames headless, 121 headed
        # per 2 s with nothing holding it. So the premise is MEASURED, and the
        # throttle-dependent assertions run only where it is observable; the product
        # claim (a tool-driven background tab ticks) is the two other nodes'.
        # Nothing here is observable without the throttle: the held rate measured
        # after the call is then the runner's own, 17 frames per 2 s against the
        # untouched tab's 62 on one macOS run, so a bar on it measures the runner.
        # The exemption is macOS-only: on Windows and Linux (measured throttled) an
        # unthrottled undriven tab is a product regression and must FAIL, not skip.
        # One 2 s reading can pass on a brief stall (a GPU process restart), so the
        # premise is read AGAIN after the linger, on the control (_decide_fallback).
        undriven_rate = await _undriven_rate(iid, undriven)
        if sys.platform == "darwin" and undriven_rate > MAX_UNHELD_FRAMES:
            pytest.skip(
                f"Chrome on this macOS host does not throttle an undriven background "
                f"tab: its rAF loop counted {undriven_rate} frames in {MEASURE_MS} ms "
                f"with no tool acting on it (throttled is <= {MAX_UNHELD_FRAMES}), so "
                f"there is no throttle for a hold to lift here"
            )
        assert undriven_rate <= MAX_UNHELD_FRAMES, (
            f"undriven background tab counted {undriven_rate} frames"
        )
        # Measured AFTER the undriven reading, so no probe loop precedes it.
        _skip_if_display_does_not_paint(
            headless, await _display_paints(iid, foreground)
        )

        # A tool call starts the driven tab's loop: it is held from that call on.
        # The spy is cleared at the holding call, so an earlier hold's stop (were
        # there one) cannot answer for this one.
        driven_tab = tab_open.find(browser, driven)
        sent = _sent_methods(monkeypatch, driven_tab)
        held_at = time.monotonic()
        await execute(instance_id=iid, script=START_LOOP_JS, tab_id=driven)
        # The hold's deadline is set inside the call, so it ends by the linger after
        # the call returns, and the stop then has its send budget.
        released_by = (
            time.monotonic()
            + SHORT_LINGER_SECONDS
            + paint_hold.SEND_BUDGET_S
            + LINGER_MARGIN_SECONDS
        )
        hold = paint_hold._held.get(driven)  # private, as in _assert_released
        assert hold is not None and hold.tab is driven_tab, (
            "the tool call did not hold the tab the spy watches"
        )
        assert await _held_rate(iid, driven, held_at) >= MIN_FRAMES

        # Without another call it falls back after the linger. "Falls back" is
        # bounded, not instant: for a few seconds after a hold ends, Chrome keeps
        # painting background tabs (T-10026: on Windows the UNTOUCHED tab read 56-57
        # frames when read first at linger + 1 s; the macOS publish runner read the
        # driven tab at 45 there). So the driven tab and the untouched control are
        # read at the SAME time, each re-read while over the bar, and decided as a
        # pair (:func:`_decide_fallback`): a lone reading of the driven tab cannot
        # tell a hold left on from a runner that stopped throttling mid-test (macOS
        # read 79 on the 2.1.51 publish gate after passing the startup premise). The
        # release is also checked without frames, on every platform and BEFORE the
        # pair can skip (:func:`_assert_released`). A hold left on fails either way.
        driven_rate, control_rate = await asyncio.gather(
            _fallen_back_rate(iid, driven), _fallen_back_rate(iid, undriven)
        )
        await _assert_released(driven, sent, released_by)
        _decide_fallback(driven_rate, control_rate)

        # A new call holds it again.
        held_at = time.monotonic()
        await execute(instance_id=iid, script="return 1", tab_id=driven)
        assert await _held_rate(iid, driven, held_at) >= MIN_FRAMES
    finally:
        await close(instance_id=iid)


@pytest.mark.parametrize("headless", _modes())
async def test_a_driven_popup_in_the_background_keeps_painting(tmp_path, headless):
    """A page-opened popup (F-1012) that a later tab covers, then a tool acts on."""
    new_tab, close = get_fn("new_tab"), get_fn("close_instance")
    execute, list_tabs = get_fn("execute_script"), get_fn("list_tabs")
    iid = await _spawn(tmp_path, headless)
    try:
        opener = (await new_tab(instance_id=iid, url="about:blank"))["tab_id"]
        known = {t["tab_id"] for t in await list_tabs(instance_id=iid)}
        await execute(
            instance_id=iid,
            script="window.open('about:blank#popup', '_blank'); return true;",
            tab_id=opener,
        )
        popup = None
        deadline = time.monotonic() + ARMING_BUDGET_SECONDS
        while popup is None and time.monotonic() < deadline:
            fresh = [
                t["tab_id"]
                for t in await list_tabs(instance_id=iid)
                if t["tab_id"] not in known
            ]
            popup = fresh[0] if fresh else None
            await asyncio.sleep(0.25)
        assert popup is not None, "the page-opened popup never became a tracked tab"
        # Something else in front, so the popup is the background one.
        await new_tab(instance_id=iid, url="about:blank")

        answer = await _frames_when_armed(iid, popup)
        assert answer["visibility"] == "visible", answer
        assert answer["frames"] >= MIN_FRAMES, answer
    finally:
        await close(instance_id=iid)
