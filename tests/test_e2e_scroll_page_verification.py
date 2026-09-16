"""F-875 and F-878 against real Chrome — the settle, and WHICH element scrolls.

The hermetic half is ``tests/test_scroll_page_verification.py``; a fake can be
made to say anything, so what only Chrome can prove is that:

* a **smooth** scroll really does arrive after the tool answers — F-875
  measured 4910 of 7039 px on an 8016 px document when the old fixed
  ``asyncio.sleep(0.5)`` ended, so a record whose ``scroll_y_after`` equals the
  page's own ``window.scrollY`` AND equals ``max_scroll_y`` is the fix;
* Chrome's own ``document.scrollingElement`` extent agrees with the tool's
  ``max_scroll_y`` — the read is measured against the element CSSOM View says
  ``scrollTo`` moves, not against a guess;
* a document exactly one viewport tall answers ``max_scroll_y: 0`` and does not
  raise (F-875 §4/§5: a one-viewport page is a legitimate page);
* **the scroller PICK** is the rule F-878 measured and not another one. This is
  the half that can ONLY live here: ``ScrollingTab`` applies Chrome's rule to
  its own geometry rather than executing ``scroll_position.SCROLLER_JS``, so a
  hermetic pin holds the tool's WIRING while real Chrome running the real
  script is what holds the RULE. Swapping rules 1 and 2 in ``SCROLLER_JS``
  leaves every hermetic pin green and fails
  :func:`test_a_scrolling_document_is_the_page_even_with_a_nested_scroller`.

All twelve fixtures of the F-878 matrix are declared below, in the finding's own
order and lettering, so the measurement is REPRODUCIBLE from the repo rather
than only from the finding's prose. The ones carrying assertions are the ones a
candidate heuristic gets wrong; the rest are here to be re-measured.

Every page is a ``data:`` URL, so there is no network and no fixture server, and
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

# ---------------------------------------------------------------------------
# The F-878 matrix, entire. Twelve pages, the finding's own lettering, so the
# measurement in ``audit/stage2/finding_F878_scroll_page_nested_scroller.md``
# can be re-run from the repo. Measured viewport when it was taken: 1888x977.
# ---------------------------------------------------------------------------

_DOC = "data:text/html,<!DOCTYPE html>"

#: **a** — CONTROL: a plain document scroller in standards mode.
F878_A_CONTROL = _DOC + (
    "<title>a</title><body style='margin:0'><div style='height:8000px'></div>"
)

#: **b** — the app shell. ``html,body{overflow:hidden}`` plus one full-viewport
#: ``div{overflow:auto}``: the default layout of every SPA starter template, and
#: the page ``window.scrollTo`` cannot move at all. THE headline case.
F878_B_APP_SHELL = _DOC + (
    "<title>b</title><style>html,body{margin:0;height:100%;overflow:hidden}"
    ".shell{height:100vh;overflow:auto}</style>"
    "<body><div class='shell' id='shell'><div style='height:8000px'></div></div>"
)

#: **c** — a sidebar and a main pane, 25 % / 75 %, different heights. Which one
#: is "the page"?
F878_C_TWO_PANES = _DOC + (
    "<title>c</title><style>html,body{margin:0;height:100%;overflow:hidden}"
    ".row{display:flex;height:100vh}.side{width:25%;overflow:auto}"
    ".main{width:75%;overflow:auto}</style><body><div class='row'>"
    "<div class='side' id='side'><div style='height:3000px'></div></div>"
    "<div class='main' id='main'><div style='height:9000px'></div></div></div>"
)

#: **d** — a 400 px ``div{overflow:auto}`` inside a document that ALSO scrolls:
#: the case where the nested scroller must LOSE, and the only fixture that can
#: tell rule 1 (the ``scrollingElement`` precedence) from rule 2.
F878_D_NESTED_IN_SCROLLING_DOC = _DOC + (
    "<title>d</title><style>body{margin:0}.box{height:400px;overflow:auto}</style>"
    "<body><div style='height:600px'></div>"
    "<div class='box' id='box'><div style='height:5000px'></div></div>"
    "<div style='height:6000px'></div>"
)

#: **e** — document-level ``scroll-snap-type: y mandatory``. Does snapping break
#: the document path? (It does not.)
F878_E_SNAP_DOCUMENT = _DOC + (
    "<title>e</title><style>html{scroll-snap-type:y mandatory}body{margin:0}"
    "section{height:100vh;scroll-snap-align:start}</style><body>"
    "<section></section><section></section><section></section><section></section>"
)

#: **e2** — snap AND nested at once: the shell IS the snap container.
F878_E2_SNAP_NESTED = _DOC + (
    "<title>e2</title><style>html,body{margin:0;height:100%;overflow:hidden}"
    ".deck{height:100vh;overflow:auto;scroll-snap-type:y mandatory}"
    "section{height:100vh;scroll-snap-align:start}</style><body>"
    "<div class='deck' id='deck'><section></section><section></section>"
    "<section></section><section></section></div>"
)

#: **f** — no doctype, so quirks mode: ``document.scrollingElement`` is ``body``,
#: not ``html``. (The one fixture that must NOT carry ``_DOC``.)
F878_F_QUIRKS = (
    "data:text/html,<title>f</title>"
    "<body style='margin:0'><div style='height:8000px'></div>"
)

#: **g** — the shell with its OWN scrollable widget at the viewport centre. This
#: is where the centre-point heuristic loses: its first scrollable ancestor from
#: the middle of the screen is the grid, so "scroll the page to the bottom"
#: would scroll a table inside the page.
F878_G_SHELL_WITH_GRID = _DOC + (
    "<title>g</title><style>html,body{margin:0;height:100%;overflow:hidden}"
    ".shell{height:100vh;overflow:auto}"
    ".grid{height:60vh;overflow:auto;margin:20vh 10vw}</style>"
    "<body><div class='shell' id='shell'><div style='height:10vh'></div>"
    "<div class='grid' id='grid'><div style='height:4000px'></div></div>"
    "<div style='height:6000px'></div></div>"
)

#: **h** — the shell under a ``position:fixed;inset:0`` scrim (a chat widget's
#: backdrop, a cookie banner's overlay). ``elementFromPoint`` returns what is
#: PAINTED at the centre, so the centre-point heuristic walks to nothing.
F878_H_SHELL_UNDER_SCRIM = _DOC + (
    "<title>h</title><style>html,body{margin:0;height:100%;overflow:hidden}"
    ".shell{height:100vh;overflow:auto}.scrim{position:fixed;inset:0}</style>"
    "<body><div class='shell' id='shell'><div style='height:8000px'></div></div>"
    "<div class='scrim' id='scrim'></div>"
)

#: **i** — a three-pane mail layout, 20 % / 35 % / 45 %. No pane reaches half
#: the viewport, so a largest-area rule WITH a coverage floor finds nothing.
F878_I_THREE_COLUMNS = _DOC + (
    "<title>i</title><style>html,body{margin:0;height:100%;overflow:hidden}"
    ".row{display:flex;height:100vh}.rail{width:20%;overflow:auto}"
    ".list{width:35%;overflow:auto}.reader{width:45%;overflow:auto}</style><body>"
    "<div class='row'><div class='rail' id='rail'><div style='height:2000px'></div>"
    "</div><div class='list' id='list'><div style='height:5000px'></div></div>"
    "<div class='reader' id='reader'><div style='height:9000px'></div></div></div>"
)

#: **j** — a HORIZONTAL-only scroller. Both candidate heuristics were written
#: for ``overflow-y`` and could not see it at all.
F878_J_H_STRIP = _DOC + (
    "<title>j</title><style>html,body{margin:0;height:100%;overflow:hidden}"
    ".strip{height:100vh;overflow-x:auto;overflow-y:hidden;white-space:nowrap}"
    "</style><body><div class='strip' id='strip'>"
    "<div style='display:inline-block;width:9000px;height:50px'></div></div>"
)

#: **k** — the document overflows by 20 px (a stray sibling) while the real
#: content lives in a full shell. The shell is narrower than ``body`` by exactly
#: the scrollbar's width — 0.8 % — which is what a plain ``area >`` comparison
#: let decide the contest.
F878_K_STRAY_OVERFLOW = _DOC + (
    "<title>k</title><style>html,body{margin:0;height:100%;overflow-y:auto}"
    ".shell{height:100vh;overflow:auto}.stray{height:20px}</style>"
    "<body><div class='shell' id='shell'><div style='height:8000px'></div></div>"
    "<div class='stray'></div>"
)

#: Every fixture above, so a re-measurement can walk the matrix by name. Not
#: consumed by an assertion here on purpose — it is the reproduction handle.
F878_MATRIX = {
    "a_control": F878_A_CONTROL,
    "b_app_shell": F878_B_APP_SHELL,
    "c_two_panes": F878_C_TWO_PANES,
    "d_nested_in_scrolling_doc": F878_D_NESTED_IN_SCROLLING_DOC,
    "e_snap_document": F878_E_SNAP_DOCUMENT,
    "e2_snap_nested": F878_E2_SNAP_NESTED,
    "f_quirks": F878_F_QUIRKS,
    "g_shell_with_grid": F878_G_SHELL_WITH_GRID,
    "h_shell_under_scrim": F878_H_SHELL_UNDER_SCRIM,
    "i_three_columns": F878_I_THREE_COLUMNS,
    "j_h_strip": F878_J_H_STRIP,
    "k_stray_overflow": F878_K_STRAY_OVERFLOW,
}


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
        await navigate_and_settle(iid, F878_B_APP_SHELL)

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
        # The decisive one for the F-875 merge: `scrollend` for an ELEMENT
        # scroll is dispatched at that element and does NOT bubble to `window`,
        # so a latch armed on `window` here could never fire and this would be
        # `False` after the whole 10 s budget. Bounded explicitly so a
        # budget-burn reads as a failure rather than as a slow pass.
        assert record["settled"] is True, record
        assert record["settle_seconds"] < 5.0, record
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


async def test_a_scrolling_document_is_the_page_even_with_a_nested_scroller(
    tmp_empty_root,
):
    """Rule 1 is a PRECEDENCE, and this is the pin that holds it (F-878 §4).

    Fixture d: a 400 px ``overflow:auto`` box inside a ~7000 px scrolling
    document. BOTH can move, so this is the only shape that can tell rule 1
    from rule 2 — and it is the reason rule 1 exists, because any "largest
    scrollable wins" rule without it has to be talked out of choosing the box.

    This is the load-bearing test for the RULE, not just the wiring: real Chrome
    evaluates the real ``scroll_position.SCROLLER_JS``, so swapping rules 1 and
    2 in the product fails HERE. The hermetic twin
    (``test_a_scrolling_document_wins_over_a_nested_scroller``) cannot, because
    ``ScrollingTab`` is not a JS engine.
    """
    spawn = get_fn("spawn_browser")
    scroll_page = get_fn("scroll_page")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, F878_D_NESTED_IN_SCROLLING_DOC)

        # The premise, from Chrome: the document AND the box can both move.
        assert (
            await eval_js(
                iid,
                "Math.round(document.scrollingElement.scrollHeight"
                " - document.scrollingElement.clientHeight)",
            )
            > 0
        )
        box_extent = await eval_js(
            iid,
            "(function(b){return Math.round(b.scrollHeight-b.clientHeight);})"
            "(document.getElementById('box'))",
        )
        assert box_extent > 0, box_extent

        record = await scroll_page(instance_id=iid, direction="bottom", smooth=True)

        assert record["scroller_is_document"] is True, record
        assert record["scroller"]["id"] == "", record
        assert record["scrolled"] is True
        assert record["scroll_y_after"] == record["max_scroll_y"] > 0, record
        # The DOCUMENT moved...
        assert record["scroll_y_after"] == await eval_js(
            iid, "Math.round(window.scrollY)"
        )
        # ...and the 400 px box did not. Choosing it would be the defect.
        assert (
            await eval_js(iid, "Math.round(document.getElementById('box').scrollTop)")
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
        await navigate_and_settle(iid, F878_G_SHELL_WITH_GRID)

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
        await navigate_and_settle(iid, F878_J_H_STRIP)

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
