"""F-875 against real Chrome — the settle, and the page with nothing to scroll.

The hermetic half is ``tests/test_scroll_page_verification.py``; a fake can be
made to say anything, so what only Chrome can prove is that:

* a **smooth** scroll really does arrive after the tool answers — the finding
  measured 4910 of 7039 px on an 8016 px document when the old fixed
  ``asyncio.sleep(0.5)`` ended, so a record whose ``scroll_y_after`` equals the
  page's own ``window.scrollY`` AND equals ``max_scroll_y`` is the fix;
* Chrome's own ``document.scrollingElement`` extent agrees with the tool's
  ``max_scroll_y`` — the read is measured against the element CSSOM View says
  ``scrollTo`` moves, not against a guess;
* a document exactly one viewport tall answers ``max_scroll_y: 0`` and does not
  raise (finding §4/§5: a one-viewport page is a legitimate page).

Both pages are ``data:`` URLs, so there is no network and no fixture server, and
the profile root is a temp dir (``tmp_empty_root``) so nothing here can touch the
developer's real ``~/.stealth-mcp`` session root.
"""

from __future__ import annotations

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

#: 8000 px of content in a zero-margin body — the finding's page shape, minus
#: its exact height (the viewport is the runner's, so ``max_scroll_y`` is
#: derived from the page rather than asserted as a number).
TALL = (
    "data:text/html,<title>Tall</title>"
    "<body style='margin:0'><div style='height:8000px'></div>"
)

#: Content that cannot overflow any viewport: the Cloudflare-interstitial shape.
SHORT = (
    "data:text/html,<title>Short</title>"
    "<body style='margin:0'><div style='height:4px'></div>"
)

#: F-878 fixture b — the app shell. ``html,body{overflow:hidden}`` plus one
#: full-viewport ``div{overflow:auto}``: the default layout of every SPA starter
#: template, and the page ``window.scrollTo`` cannot move at all.
APP_SHELL = (
    "data:text/html,<!DOCTYPE html><title>Shell</title>"
    "<style>html,body{margin:0;height:100%;overflow:hidden}"
    ".shell{height:100vh;overflow:auto}</style>"
    "<body><div class='shell' id='shell'><div style='height:8000px'></div></div>"
)

#: F-878 fixture g — the same shell with its OWN scrollable widget at the
#: viewport centre. This is where the centre-point heuristic loses: its first
#: scrollable ancestor from the middle of the screen is the grid, so "scroll the
#: page to the bottom" would scroll a table inside the page.
SHELL_WITH_GRID = (
    "data:text/html,<!DOCTYPE html><title>Grid</title>"
    "<style>html,body{margin:0;height:100%;overflow:hidden}"
    ".shell{height:100vh;overflow:auto}"
    ".grid{height:60vh;overflow:auto;margin:20vh 10vw}</style>"
    "<body><div class='shell' id='shell'><div style='height:10vh'></div>"
    "<div class='grid' id='grid'><div style='height:4000px'></div></div>"
    "<div style='height:6000px'></div></div>"
)

#: F-878 fixture j — a HORIZONTAL-only scroller. Both candidate heuristics were
#: written for ``overflow-y`` and could not see it at all.
H_STRIP = (
    "data:text/html,<!DOCTYPE html><title>Strip</title>"
    "<style>html,body{margin:0;height:100%;overflow:hidden}"
    ".strip{height:100vh;overflow-x:auto;overflow-y:hidden;white-space:nowrap}"
    "</style><body><div class='strip' id='strip'>"
    "<div style='display:inline-block;width:9000px;height:50px'></div></div>"
)


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def test_a_smooth_scroll_is_reported_where_it_landed(tmp_empty_root):
    spawn = get_fn("spawn_browser")
    scroll_page = get_fn("scroll_page")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, TALL)

        record = await scroll_page(instance_id=iid, direction="bottom", smooth=True)

        # The tool's read and the page's own answer, independently. Rounded on
        # both sides: the record's offsets are ``Math.round``ed (a fractional
        # zoom or a non-integer device pixel ratio makes ``window.scrollY``
        # fractional), so a raw comparison would pin the runner's DPR.
        assert record["scroll_y_after"] == await eval_js(
            iid, "Math.round(window.scrollY)"
        )
        # Chrome's own extent, read the way CSSOM View defines the scroller.
        assert record["max_scroll_y"] == await eval_js(
            iid,
            "Math.round(document.scrollingElement.scrollHeight"
            " - document.scrollingElement.clientHeight)",
        )
        assert record["max_scroll_y"] > 0, record
        assert record["scroll_y_after"] == record["max_scroll_y"], record
        assert record["scrolled"] is True
        assert record["at_edge"] is True
        assert record["settled"] is True
        # The whole point: it waited. A 0.5 s nap would have answered short.
        assert record["settle_seconds"] > 0

        # And back up, smoothly, to the other edge.
        back = await scroll_page(instance_id=iid, direction="top", smooth=True)
        assert back["scroll_y_after"] == 0
        assert back["scrolled"] is True
        assert back["at_edge"] is True
        assert await eval_js(iid, "Math.round(window.scrollY)") == 0
    finally:
        await close(instance_id=iid)


async def test_a_page_one_viewport_tall_reports_nothing_to_scroll(tmp_empty_root):
    spawn = get_fn("spawn_browser")
    scroll_page = get_fn("scroll_page")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, SHORT)

        record = await scroll_page(instance_id=iid, direction="bottom", smooth=True)

        assert record["max_scroll_y"] == 0, record
        assert record["scroll_y_before"] == 0
        assert record["scroll_y_after"] == 0
        assert record["scrolled"] is False
        assert record["at_edge"] is True
        assert record["settled"] is True
        assert await eval_js(iid, "Math.round(window.scrollY)") == 0

        # An instant scroll on the same page is the fast path: no animation can
        # start, so the settle must not spend its budget waiting for one.
        instant = await scroll_page(
            instance_id=iid, direction="down", amount=500, smooth=False
        )
        assert instant["scrolled"] is False
        assert instant["settle_seconds"] < 1.0, instant
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# F-878 — the nested scroller, against the browser that has to agree
# ---------------------------------------------------------------------------


async def test_an_app_shell_is_scrolled_even_though_the_window_cannot_be(
    tmp_empty_root,
):
    """`body{overflow:hidden}` + a scrolling `div`: the finding's headline case.

    Only Chrome can prove the two halves that matter: that the shell really
    moved (its own ``scrollTop``), and that the WINDOW did not — so a record
    reporting 7023 px cannot have come from the document path.
    """
    spawn = get_fn("spawn_browser")
    scroll_page = get_fn("scroll_page")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, APP_SHELL)

        # The premise, from Chrome: the document scroller cannot move at all.
        assert (
            await eval_js(
                iid,
                "Math.round(document.scrollingElement.scrollHeight"
                " - document.scrollingElement.clientHeight)",
            )
            == 0
        )

        record = await scroll_page(instance_id=iid, direction="bottom", smooth=True)

        assert record["scroller_is_document"] is False, record
        assert record["scroller"]["id"] == "shell", record
        assert record["scroller"]["tag"] == "div", record
        assert record["max_scroll_y"] > 0, record
        assert record["scroll_y_after"] == record["max_scroll_y"], record
        assert record["scrolled"] is True
        assert record["at_edge"] is True
        assert record["settled"] is True
        # The element's own answer, and the window's.
        assert record["scroll_y_after"] == await eval_js(
            iid, "Math.round(document.getElementById('shell').scrollTop)"
        )
        assert await eval_js(iid, "Math.round(window.scrollY)") == 0

        back = await scroll_page(instance_id=iid, direction="top", smooth=True)
        assert back["scroll_y_after"] == 0
        assert back["scrolled"] is True
        assert (
            await eval_js(iid, "Math.round(document.getElementById('shell').scrollTop)")
            == 0
        )
    finally:
        await close(instance_id=iid)


async def test_the_shell_wins_over_its_own_centred_widget(tmp_empty_root):
    """Outer beats inner: the grid at the viewport centre is not "the page".

    The centre-point heuristic picks ``div#grid`` here (measured, finding §3.3);
    the largest-viewport-clipped-area rule picks the shell, and the grid must be
    left exactly where it was.
    """
    spawn = get_fn("spawn_browser")
    scroll_page = get_fn("scroll_page")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, SHELL_WITH_GRID)

        record = await scroll_page(instance_id=iid, direction="bottom", smooth=True)

        assert record["scroller"]["id"] == "shell", record
        assert record["scrolled"] is True
        assert record["scroll_y_after"] == record["max_scroll_y"] > 0, record
        # The widget inside it never moved.
        assert (
            await eval_js(iid, "Math.round(document.getElementById('grid').scrollTop)")
            == 0
        )
    finally:
        await close(instance_id=iid)


async def test_a_horizontal_only_scroller_is_found_on_the_x_axis(tmp_empty_root):
    """The pick takes its axis from the direction, so `right` can see a strip.

    And the same page answered about `bottom` honestly reports that there is no
    vertical scroller on it at all — the strip is ``overflow-y: hidden``.
    """
    spawn = get_fn("spawn_browser")
    scroll_page = get_fn("scroll_page")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, H_STRIP)

        sideways = await scroll_page(
            instance_id=iid, direction="right", amount=500, smooth=False
        )
        assert sideways["scroller"]["id"] == "strip", sideways
        assert sideways["scrolled"] is True
        assert sideways["scroll_x_after"] == 500, sideways
        assert sideways["max_scroll_x"] > 0, sideways
        assert sideways["scroll_x_after"] == await eval_js(
            iid, "Math.round(document.getElementById('strip').scrollLeft)"
        )

        down = await scroll_page(instance_id=iid, direction="bottom", smooth=False)
        assert down["scroller_is_document"] is True, down
        assert down["max_scroll_y"] == 0, down
        assert down["scrolled"] is False
    finally:
        await close(instance_id=iid)
