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

Style follows the plan_E2E suite: ``e2e_helpers`` mechanism, bounded waits, every
spawn closed in ``finally``. Hermetic: ``about:blank`` and ``window.open`` only.
"""

from __future__ import annotations

import asyncio
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
#: The test seam for ``paint_hold.LINGER_SECONDS``.
SHORT_LINGER_SECONDS = 2.0

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

        control = await _frames(iid, foreground)
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
    that was driven falls back ``LINGER_SECONDS`` after the last call.
    """
    monkeypatch.setattr(paint_hold, "LINGER_SECONDS", SHORT_LINGER_SECONDS)
    new_tab, close = get_fn("new_tab"), get_fn("close_instance")
    execute = get_fn("execute_script")
    iid = await _spawn(tmp_path, headless)
    try:
        driven = (await new_tab(instance_id=iid, url="about:blank"))["tab_id"]
        undriven = (await new_tab(instance_id=iid, url="about:blank"))["tab_id"]
        await new_tab(instance_id=iid, url="about:blank")  # both are now background

        # The un-driven tab: its loop is started straight through CDP, never a tool.
        browser = await runtime.browser_manager.get_browser(iid)
        await tab_open.find(browser, undriven).evaluate(
            START_LOOP_JS.replace("return true;", "true;"), return_by_value=True
        )
        assert await _undriven_rate(iid, undriven) <= MAX_UNHELD_FRAMES

        # A tool call starts the driven tab's loop: it is held from that call on.
        await execute(instance_id=iid, script=START_LOOP_JS, tab_id=driven)
        assert await _undriven_rate(iid, driven) >= MIN_FRAMES

        # Without another call it falls back after the linger.
        await asyncio.sleep(SHORT_LINGER_SECONDS + 1)
        assert await _undriven_rate(iid, driven) <= MAX_UNHELD_FRAMES
        assert await _undriven_rate(iid, undriven) <= MAX_UNHELD_FRAMES

        # A new call holds it again.
        await execute(instance_id=iid, script="return 1", tab_id=driven)
        assert await _undriven_rate(iid, driven) >= MIN_FRAMES
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
