"""F-875 — ``scroll_page`` must report what happened, not that it dispatched.

Measured on 2.1.6 against real Chrome 152 (the finding's §1), the tool returned
``True`` in three states it cannot tell apart:

* the page had nothing to scroll (``maxScroll == 0``: a Cloudflare interstitial
  one viewport tall) — ``scrollY`` stayed ``0`` and the answer was ``true``;
* a smooth scroll was still in flight — 4910 of 7039 px on an 8016 px document
  when the fixed ``asyncio.sleep(0.5)`` ended, and ``true`` already returned;
* the scroll actually arrived.

These pins hold the tool to the difference. They drive
``DOMHandler.scroll_page`` directly through :class:`fakes.ScrollingTab`, which
models the page's own scroll geometry (see its docstring for why the read is
answered as a JSON *string* and why smooth advances per read) — so what is
pinned is the product's reading of a moving value, not a canned answer.
"""

from __future__ import annotations

import time

import pytest

from fakes import ScrollingTab
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = pytest.mark.asyncio

#: The finding's measured geometry: an 8016 px document in a 977 px viewport,
#: so ``max_scroll_y`` is 7039 — the number a smooth scroll was 2129 px short of.
DOC_HEIGHT = 8016
VIEWPORT_HEIGHT = 977
MAX_SCROLL_Y = DOC_HEIGHT - VIEWPORT_HEIGHT


def _one_viewport_tall() -> ScrollingTab:
    """The Cloudflare-interstitial shape: nothing to scroll (finding §1c)."""
    return ScrollingTab(doc_height=977, viewport_height=977)


# ---------------------------------------------------------------------------
# The two states that used to answer ``True``
# ---------------------------------------------------------------------------


async def test_nothing_to_scroll_is_reported_not_claimed():
    """``max_scroll_y == 0`` is an honest page, and an honest ``scrolled: False``.

    The finding is explicit (§4, §5) that this must NOT raise and must NOT be a
    ``False`` that reads as a failure: a one-viewport page is a legitimate page.
    """
    tab = _one_viewport_tall()

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=True)

    assert record["scrolled"] is False
    assert record["at_edge"] is True
    assert record["max_scroll_y"] == 0
    assert record["scroll_y_before"] == 0
    assert record["scroll_y_after"] == 0
    assert record["settled"] is True


async def test_smooth_scroll_is_waited_out_not_napped_through():
    """A smooth scroll that needs several frames is reported at where it LANDED.

    ``smooth_steps=4`` means one read sees 25 % of the way — the 2.1.6 shape.
    The settle polls until two consecutive reads agree, so the record carries
    ``max_scroll_y``, not a quarter of it.
    """
    tab = ScrollingTab(
        doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT, smooth_steps=4
    )

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=True)

    assert record["scroll_y_after"] == MAX_SCROLL_Y
    assert record["scrolled"] is True
    assert record["at_edge"] is True
    assert record["settled"] is True
    # More than one read, or the settle was a nap with extra steps.
    assert len(tab.position_reads) > 2


async def test_a_mid_flight_stall_is_not_a_finished_scroll():
    """Two agreeing reads are not a stop condition — CI gate run 35046780659.

    On macOS/ARM64 the record reported ``scroll_y_after: 3498`` while the page
    read 4898 immediately after: two reads agreed 1400 px from the end, in the
    fast part of an ease-out curve. A plain document smooth scroll runs on the
    COMPOSITOR thread while ``window.scrollY`` is read on the MAIN thread, so a
    blocked main thread makes the reads go stale while the scroll keeps going —
    measured here on Chrome 152, the run of agreeing mid-flight reads lasts
    exactly as long as the renderer's long task (120 ms -> 121 ms, 250 -> 250,
    400 -> 400), i.e. unbounded.

    So the settle waits for the PAGE to say the scroll ended. This fake stalls
    for three reads mid-flight and then resumes; the old rule stopped on the
    stalled value, which is what makes this pin load-bearing.
    """
    tab = ScrollingTab(
        doc_height=DOC_HEIGHT,
        viewport_height=VIEWPORT_HEIGHT,
        smooth_steps=8,
        stall_at=2,
        stall_reads=3,
    )

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=True)

    assert record["scroll_y_after"] == MAX_SCROLL_Y, record
    assert record["settled"] is True
    assert record["at_edge"] is True
    # The stall really did repeat a mid-flight value the old rule would have
    # stopped on, rather than the fake quietly never stalling.
    assert tab.stall_reads == 3
    assert len(tab.position_reads) > 8


async def test_without_scrollend_the_settle_falls_back_to_read_agreement():
    """A browser with no ``onscrollend`` still settles, by the weaker rule.

    ``'onscrollend' in window`` is the feature test (measured on Chrome 152:
    ``'scrollend' in window`` is ``false`` there — the event is not an own
    property of ``window``, the handler is). When it is absent there is nothing
    better than read agreement, and ``START_GRACE_SECONDS`` still guards the
    start; this pin keeps that path alive rather than hanging for the budget.
    """
    tab = ScrollingTab(
        doc_height=DOC_HEIGHT,
        viewport_height=VIEWPORT_HEIGHT,
        smooth_steps=2,
        scrollend_supported=False,
    )

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=True)

    assert record["settled"] is True
    assert record["scroll_y_after"] == MAX_SCROLL_Y
    assert record["settle_seconds"] < 2.0, record


async def test_a_page_that_only_grew_is_not_a_page_that_scrolled():
    """Extent up, offset unmoved: ``scrolled`` is about the VIEWPORT.

    A lazy-loading page appends content while standing perfectly still. The
    record compares offsets for exactly this reason — comparing whole readings
    answered ``scrolled: true`` with ``scroll_y_before == scroll_y_after`` in
    the same record, which is the class of untruth this finding closes.
    """
    tab = ScrollingTab(
        doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT, growing_content=1000
    )

    record = await DOMHandler.scroll_page(tab, direction="top", smooth=True)

    assert record["scrolled"] is False
    assert record["scroll_y_before"] == record["scroll_y_after"] == 0
    assert record["scroll_x_before"] == record["scroll_x_after"] == 0
    # ...and the extent really did move under it, so this is not a no-op page.
    assert record["max_scroll_y"] > DOC_HEIGHT - VIEWPORT_HEIGHT
    # The extent reported is the FINAL read's — the freshest one there is.
    assert record["max_scroll_y"] == tab.max_scroll_y


async def test_a_growing_page_that_stopped_moving_settles_at_once():
    """The settle agrees on OFFSETS, so a still-loading page is not a hang.

    An infinite-scroll page grows for as long as it is allowed to. Settling on
    the whole reading meant such a page never agreed with itself: every scroll
    spent the entire 10 s budget and then reported ``settled: false`` about a
    viewport that had stopped moving in the first 100 ms.
    """
    tab = ScrollingTab(
        doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT, growing_content=500
    )

    record = await DOMHandler.scroll_page(
        tab, direction="down", amount=500, smooth=False
    )

    assert record["settled"] is True
    assert record["scrolled"] is True
    assert record["scroll_y_after"] == 500
    assert record["settle_seconds"] < 1.0, record
    assert record["max_scroll_y"] == tab.max_scroll_y


async def test_a_scroll_that_never_settles_says_so_within_budget(monkeypatch):
    """A page whose content keeps arriving exhausts the budget — and reports it.

    ``settled: False`` is the third state the bare ``True`` could not express:
    "we ran out of budget mid-scroll", distinct from "arrived" and from "there
    was nothing to scroll" (finding §4).
    """
    from stealth_chrome_devtools_mcp.embedded import scroll_position

    monkeypatch.setattr(scroll_position, "SETTLE_BUDGET_SECONDS", 0.3)
    tab = ScrollingTab(
        doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT, never_settles=True
    )

    record = await DOMHandler.scroll_page(tab, direction="down", amount=500)

    assert record["settled"] is False
    assert record["scrolled"] is True
    assert record["settle_seconds"] >= 0.3


# ---------------------------------------------------------------------------
# The record says what was asked and what was read
# ---------------------------------------------------------------------------


async def test_the_record_names_the_request_it_answers():
    tab = ScrollingTab(doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT)

    record = await DOMHandler.scroll_page(
        tab, direction="down", amount=300, smooth=False
    )

    assert record["direction"] == "down"
    assert record["amount"] == 300
    assert record["smooth"] is False
    assert record["scroll_y_before"] == 0
    assert record["scroll_y_after"] == 300


async def test_both_axes_are_reported_so_a_horizontal_scroll_is_not_a_no_op():
    """``direction='right'`` moves X; a Y-only record would call it unscrolled."""
    tab = ScrollingTab(
        doc_height=977, viewport_height=977, doc_width=4000, viewport_width=1000
    )

    record = await DOMHandler.scroll_page(
        tab, direction="right", amount=500, smooth=False
    )

    assert record["scroll_x_before"] == 0
    assert record["scroll_x_after"] == 500
    assert record["max_scroll_x"] == 3000
    assert record["scrolled"] is True
    assert record["at_edge"] is False


async def test_already_at_the_requested_edge_is_at_edge_without_scrolled():
    """At ``y == 0`` a ``top`` scroll changes nothing — and both say so."""
    tab = ScrollingTab(doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT)

    record = await DOMHandler.scroll_page(tab, direction="top", smooth=True)

    assert record["scrolled"] is False
    assert record["at_edge"] is True
    assert record["settled"] is True


async def test_an_instant_scroll_stays_on_the_fast_path():
    """The settle must not cost what the old fixed nap cost a smooth scroll.

    Bounded generously (1 s) against CI scheduling jitter; the point is that an
    instant scroll does not pay the settle BUDGET, which is tens of times this.
    """
    tab = ScrollingTab(doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT)

    started = time.monotonic()
    record = await DOMHandler.scroll_page(
        tab, direction="bottom", amount=0, smooth=False
    )
    elapsed = time.monotonic() - started

    assert record["scroll_y_after"] == MAX_SCROLL_Y
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# F-878 — the page whose real scroller is a nested element
#
# F-875 §7.5 left this open and F-878 measured it: twelve ``data:`` fixtures
# against real Chrome 152, on which ``document.scrollingElement`` alone is right
# 4 times out of 12. The geometry below is the measured app shell — ``html``
# with ``overflow: hidden`` and ``max_y 0``, a full-viewport ``div#shell`` that
# can move 7023 px — and ``ScrollingTab``'s nested mode applies Chrome's rule to
# it, so ``window.scrollTo`` moves nothing here exactly as it moves nothing
# there.
# ---------------------------------------------------------------------------

#: The measured app shell (F-878 §3.1, fixture b).
SHELL_HEIGHT = 8000
SHELL_VIEWPORT = 977
SHELL_MAX_Y = SHELL_HEIGHT - SHELL_VIEWPORT


def _app_shell(**kwargs) -> ScrollingTab:
    """``html,body{overflow:hidden}`` + one full-viewport ``div#shell``."""
    return ScrollingTab(
        doc_height=SHELL_HEIGHT,
        viewport_height=SHELL_VIEWPORT,
        nested_id="shell",
        nested_classes=("shell",),
        **kwargs,
    )


async def test_an_app_shell_is_scrolled_not_reported_as_unscrollable():
    """The finding: `body{overflow:hidden}` + a scrolling `div` must move.

    Before F-878 this answered ``scrolled: false``, ``max_scroll_y: 0``,
    ``at_edge: true`` — honest since F-875, and useless: the tool could not move
    the content of the layout every SPA starter template ships.
    """
    tab = _app_shell()

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=True)

    assert record["scrolled"] is True
    assert record["scroll_y_before"] == 0
    assert record["scroll_y_after"] == SHELL_MAX_Y
    assert record["max_scroll_y"] == SHELL_MAX_Y
    assert record["at_edge"] is True
    assert record["settled"] is True


async def test_the_record_names_the_element_it_drove():
    """A pick is a judgement, so the caller gets to see it (finding §5.2).

    Shape only — a tag, an id, the classes — never the element's text.
    """
    tab = _app_shell()

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=False)

    assert record["scroller_is_document"] is False
    assert record["scroller"] == {"tag": "div", "id": "shell", "classes": ["shell"]}


async def test_a_plain_document_is_still_named_as_the_document():
    """The control: rule 1 stops at ``document.scrollingElement`` and says so."""
    tab = ScrollingTab(doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT)

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=False)

    assert record["scroller_is_document"] is True
    assert record["scroller"] == {"tag": "html", "id": "", "classes": []}
    assert record["scroll_y_after"] == MAX_SCROLL_Y


async def test_the_committed_matrix_is_the_findings_twelve_fixtures():
    """``F878_MATRIX`` is what makes §3 reproducible — so it needs a witness.

    The matrix constants carry no assertions of their own (only four of the
    twelve do), which is the point: they exist so a future reader can re-measure
    the finding from the repo. That leaves nothing to notice a fixture dropped
    from the dict or a constant renamed out of it, and the reproducibility claim
    would quietly become false. Pinned HERE rather than beside the constants
    because this file has no ``integration`` mark: the e2e module is deselected
    by the pre-push lane (``-m "not integration"``), so a pin living there would
    not run on the one gate that always runs.
    """
    from test_e2e_scroll_page_verification import F878_MATRIX

    assert set(F878_MATRIX) == {
        "a_control",
        "b_app_shell",
        "c_two_panes",
        "d_nested_in_scrolling_doc",
        "e_snap_document",
        "e2_snap_nested",
        "f_quirks",
        "g_shell_with_grid",
        "h_shell_under_scrim",
        "i_three_columns",
        "j_h_strip",
        "k_stray_overflow",
    }
    # Each is a distinct page: a copy-pasted constant would still count twelve.
    assert len(set(F878_MATRIX.values())) == 12
    # And f is the ONE quirks-mode fixture, so it must carry no doctype.
    assert "DOCTYPE" not in F878_MATRIX["f_quirks"]
    assert all(
        "DOCTYPE" in page for key, page in F878_MATRIX.items() if key != "f_quirks"
    )


async def test_a_scrolling_document_wins_over_a_nested_scroller():
    """Rule 1 is a PRECEDENCE: fixture d's 400 px box must not win (F-878 §4).

    What this pin holds is the WIRING — that when the page answers "the
    document", the tool drives the document, reports the document's offsets and
    leaves the nested container exactly where it was. It cannot hold the RULE
    itself: ``ScrollingTab`` is not a JS engine, so it applies Chrome's rule to
    its own geometry rather than executing ``SCROLLER_JS``. Swapping rules 1 and
    2 in the product is caught by
    ``test_e2e_scroll_page_verification.py::test_a_scrolling_document_is_the_page_even_with_a_nested_scroller``,
    where real Chrome runs the real script.

    (The double gives both containers the same viewport height; fixture d's box
    is really 400 px tall. Nothing here depends on that number — only on both
    containers being able to move.)
    """
    tab = ScrollingTab(
        doc_height=5000,  # the nested box's content
        viewport_height=SHELL_VIEWPORT,
        nested_id="box",
        nested_classes=("box",),
        document_height=8000,  # ...inside a document that scrolls too
    )
    assert tab.doc_max_scroll_y > 0 and tab.max_scroll_y > 0, "both must be scrollable"

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=False)

    assert record["scroller_is_document"] is True, record
    assert record["scroller"] == {"tag": "html", "id": "", "classes": []}
    assert record["scrolled"] is True
    assert record["scroll_y_after"] == record["max_scroll_y"] == tab.doc_max_scroll_y
    # The box never moved: driving it would have been the defect rule 1 prevents.
    assert tab.scroll_y == 0, tab.scroll_y


async def test_a_stale_path_reports_the_element_it_actually_read():
    """The record names what the READ found, never what the PICK hoped for.

    ``_RESOLVE_JS`` falls back to the document scroller when the chosen
    element is no longer in the document — a page that re-rendered mid-scroll.
    Both shapes that produces are here: the identity is the document's, and
    ``scroller_is_document`` agrees with it. A record that kept naming
    ``div#shell`` while reading ``html`` would be F-875's defect in a new place.
    """
    tab = _app_shell(stale_path=True)

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=False)

    assert record["scroller_is_document"] is True, record
    assert record["scroller"] == {"tag": "html", "id": "", "classes": []}
    # The pick still asked for the shell — this is a fallback, not a re-pick.
    assert tab.scroller_picks, tab.evaluate_calls
    assert any("_el([1, 0])" in e for e in tab.evaluate_calls), tab.evaluate_calls
    # And the record is honest about the document it fell back to: it cannot move.
    assert record["max_scroll_y"] == 0
    assert record["scrolled"] is False


async def test_a_document_scroller_is_still_driven_through_window():
    """Rule 1 is a precedence, and the SCROLL script it produces is F-875's.

    **Green before the fix as well as after, deliberately** — it does not pin
    the new behaviour, it pins that the old behaviour survived it. The measured
    reason (finding §4): if the document can move, the document IS the page, and
    keeping ``window`` in the generated scroll JS is the mechanical form of "the
    control fixture is unchanged". (The scroll scripts only — ``READ_JS`` did
    move, to the element's ``scrollLeft``/``scrollTop`` plus the resolver.)
    """
    tab = ScrollingTab(doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT)

    await DOMHandler.scroll_page(tab, direction="bottom", smooth=False)

    scrolls = [e for e in tab.evaluate_calls if ".scrollTo(" in e or ".scrollBy(" in e]
    assert scrolls, tab.evaluate_calls
    # The scroll CALL, inside F-875's latch wrapper: still `window.scrollTo`,
    # and the resolver is not even present.
    assert all("window.scrollTo({top: E.scrollHeight" in e for e in scrolls), scrolls
    assert all("_el(" not in e for e in scrolls), scrolls
    # And the `scrollend` listener is armed on `window` — the ONE target a
    # document scroll's `scrollend` actually reaches (F-878 measured that it is
    # never dispatched at `document.scrollingElement`).
    assert all("var T=window;" in e for e in scrolls), scrolls


async def test_the_scrollend_listener_is_armed_on_the_element_that_scrolls():
    """One right target per scroller kind, and both wrong choices hang silently.

    Measured on Chrome 152 across the F-878 fixtures: an ELEMENT's ``scrollend``
    is dispatched at that element and does NOT bubble to ``window`` or
    ``document``; a DOCUMENT's reaches ``window`` and ``document`` and is NEVER
    dispatched at ``document.scrollingElement``. So a listener on ``window`` for
    an app shell — or on the element for a plain page — can never fire, and the
    settle would spend its whole 10 s budget and then report ``settled: false``
    about a scroll that finished in a second. Nothing raises; it is exactly the
    silent class this pair of findings exists to close.

    The binding is asserted rather than the behaviour because the behaviour is
    the browser's; ``ScrollingTab`` latches only when the armed target matches
    the container that moved, so the behavioural half is held by every other
    pin in this file.
    """
    shell = _app_shell()
    await DOMHandler.scroll_page(shell, direction="bottom", smooth=True)
    nested_scrolls = [e for e in shell.evaluate_calls if ".scrollTo(" in e]
    assert nested_scrolls, shell.evaluate_calls
    assert all("var T=_el([1, 0]);" in e for e in nested_scrolls), nested_scrolls
    assert all("var T=window;" not in e for e in nested_scrolls), nested_scrolls

    plain = ScrollingTab(doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT)
    await DOMHandler.scroll_page(plain, direction="bottom", smooth=True)
    doc_scrolls = [e for e in plain.evaluate_calls if ".scrollTo(" in e]
    assert doc_scrolls, plain.evaluate_calls
    assert all("var T=window;" in e for e in doc_scrolls), doc_scrolls
    assert all("_el(" not in e for e in doc_scrolls), doc_scrolls


async def test_a_stalled_nested_scroll_is_not_a_finished_one():
    """F-875's jank case, on F-878's app shell — the two fixes must compose.

    A blocked main thread makes consecutive reads agree for as long as the block
    lasts (measured 120 ms → 121, 250 → 250, 400 → 400: unbounded), and that is
    what failed CI on macOS/ARM64 for a DOCUMENT scroll. A nested scroller is
    driven by the same compositor and read on the same main thread, so it has
    the same exposure — and it is only safe here because the latch is armed on
    the div, which is where Chrome dispatches its ``scrollend``.
    """
    tab = _app_shell(smooth_steps=6, stall_at=2, stall_reads=8)

    record = await DOMHandler.scroll_page(tab, direction="bottom", smooth=True)

    assert record["scroller_is_document"] is False, record
    assert record["scroll_y_after"] == SHELL_MAX_Y, record
    assert record["scrolled"] is True
    assert record["settled"] is True
    assert record["at_edge"] is True


async def test_the_scroller_is_picked_once_per_call_not_once_per_poll():
    """Measured at 2.59 ms per pick on a 6007-element page (finding §4).

    A full 10 s settle polls ~200 times; re-selecting each time would spend half
    a second of the page's main thread answering a question whose answer does
    not change.
    """
    tab = _app_shell(smooth_steps=4)

    await DOMHandler.scroll_page(tab, direction="bottom", smooth=True)

    assert len(tab.scroller_picks) == 1, tab.scroller_picks
    assert len(tab.position_reads) > 2


async def test_a_nested_scroller_is_asked_for_on_the_direction_s_own_axis():
    """``right`` moves X, so the pick is an X-axis question (finding §4, j).

    Fixture j is a strip with ``overflow-x: auto; overflow-y: hidden``: neither
    candidate heuristic could see it, because both were written for one axis.
    """
    tab = ScrollingTab(
        doc_height=977,
        viewport_height=977,
        doc_width=9000,
        viewport_width=1888,
        nested_id="strip",
        nested_classes=("strip",),
    )

    record = await DOMHandler.scroll_page(
        tab, direction="right", amount=500, smooth=False
    )

    assert tab.scroller_picks and "'x'" in tab.scroller_picks[0], tab.scroller_picks
    assert record["scrolled"] is True
    assert record["scroll_x_after"] == 500
    assert record["max_scroll_x"] == 9000 - 1888
    assert record["scroller_is_document"] is False


async def test_an_invalid_request_still_costs_no_round_trip_with_the_pick_in_front():
    """The pick is now the FIRST round trip, so it must validate before making it.

    **Green before the fix as well as after, deliberately** — it does not pin
    the new behaviour, it pins that F-875's two refusals (an unknown direction,
    a negative amount) survived it. Both are decided from the request alone, and
    putting a round trip in front of them would have quietly retired both pins
    while leaving them passing against the OLD first call.
    """
    tab = _app_shell()

    with pytest.raises(ToolError) as bad_direction:
        await DOMHandler.scroll_page(tab, direction="sideways")
    assert str(bad_direction.value) == "Invalid scroll direction: sideways"

    with pytest.raises(ToolError) as bad_amount:
        await DOMHandler.scroll_page(tab, direction="up", amount=-500)
    assert "Invalid scroll amount: -500" in str(bad_amount.value)

    assert tab.evaluate_calls == []


async def test_an_unreadable_scroller_pick_raises_rather_than_guessing():
    """The pick is a round trip like any other, and speaks the same convention.

    Falling back to "the document" on an unreadable answer would report a record
    about an element nobody chose — F-875's defect in a new place.
    """

    class _MuteTab(ScrollingTab):
        async def evaluate(self, expression, *args, **kwargs):
            if self.SCROLLER_JS_MARKER in expression:
                return None
            return await super().evaluate(expression, *args, **kwargs)

    tab = _MuteTab(doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.scroll_page(tab, direction="down")

    assert type(caught.value) is ToolError
    assert "Failed to scroll page" not in str(caught.value)


# ---------------------------------------------------------------------------
# Transport + error convention
# ---------------------------------------------------------------------------


async def test_every_position_read_is_one_json_stringify_round_trip():
    """The F-869/F-872 idiom: a deep-serialized object literal is not readable."""
    tab = ScrollingTab(doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT)

    await DOMHandler.scroll_page(tab, direction="down", amount=100, smooth=False)

    assert tab.position_reads
    for expression in tab.position_reads:
        assert expression.startswith("JSON.stringify(")


async def test_an_unreadable_position_raises_tool_error():
    """An answer that is not the promised JSON is operational failure, not ``0``.

    Reporting ``scroll_y: 0`` for a read that did not happen would be the same
    class of lie this finding closes.
    """

    class _MuteTab(ScrollingTab):
        async def evaluate(self, expression, *args, **kwargs):
            # The READ only. Since F-878 the scroller PICK is a
            # ``JSON.stringify`` round trip too and it happens FIRST, so muting
            # every one of them would make this pin about the pick's message
            # instead of the read's — a different claim wearing the same name.
            if (
                expression.startswith(self.POSITION_JS_MARKER)
                and self.SCROLLER_JS_MARKER not in expression
            ):
                return None
            return await super().evaluate(expression, *args, **kwargs)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.scroll_page(_MuteTab(), direction="down")

    assert type(caught.value) is ToolError
    # The leaf's own message, NOT re-wrapped: "Failed to scroll page: <it>"
    # doubled the sentence and dropped the cause.
    assert str(caught.value).startswith("Could not read the page's scroll position")
    assert "Failed to scroll page" not in str(caught.value)


async def test_a_negative_amount_is_rejected_not_silently_inverted():
    """The direction carries the sign; the amount is a distance.

    Before F-875 a negative amount had two wrong readings — ``down`` with
    ``-500`` silently scrolled UP, and ``up`` with ``-500`` interpolated
    ``top: --500`` and died as a JS syntax error. Treating it as a magnitude
    would only move the silence (the record would say ``direction: "up"`` about
    a page that went down), so it is refused, before any round trip.
    """
    tab = ScrollingTab(doc_height=DOC_HEIGHT, viewport_height=VIEWPORT_HEIGHT)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.scroll_page(tab, direction="up", amount=-500)

    assert type(caught.value) is ToolError
    assert "Invalid scroll amount: -500" in str(caught.value)
    assert tab.evaluate_calls == []


async def test_at_edge_tolerates_the_one_pixel_the_two_roundings_can_differ_by():
    """``Math.round(scrollY)`` and an integer extent can disagree by one.

    Under fractional zoom the offset can round down while the extent rounds up,
    so a page resting at its true bottom reads one pixel short of it. Reporting
    ``at_edge: false`` for a page that cannot go further would be a new small
    lie, so the far edge carries exactly one pixel of slack — and the near edge
    carries none, because ``scrollY`` is never below zero.
    """
    from stealth_chrome_devtools_mcp.embedded.scroll_position import Position

    assert Position(0, 7038, 0, 7039).at_edge("bottom") is True
    assert Position(0, 7037, 0, 7039).at_edge("bottom") is False
    assert Position(7038, 0, 7039, 0).at_edge("right") is True
    assert Position(0, 1, 0, 7039).at_edge("top") is False
    assert Position(0, 0, 0, 7039).at_edge("top") is True


async def test_every_documented_direction_has_one_script_and_one_edge():
    """The six names the tool documents, each answered by the ONE table.

    A direction needs both a script and an edge; they live in one table so a
    seventh cannot arrive in half of it. What this pin adds is that the six the
    docstring promises are the six that exist, and that each produces distinct
    JS — a copy-pasted row would otherwise scroll the wrong way in silence.
    """
    from stealth_chrome_devtools_mcp.embedded import scroll_position

    assert set(scroll_position.DIRECTIONS) == {
        "down",
        "up",
        "left",
        "right",
        "top",
        "bottom",
    }
    scripts = [
        scroll_position.script(d, 500, smooth=False)
        for d in sorted(scroll_position.DIRECTIONS)
    ]
    assert len(set(scripts)) == len(scripts)


async def test_an_invalid_direction_costs_no_round_trip():
    """The direction is rejected before the page is asked anything."""
    tab = ScrollingTab()

    with pytest.raises(ToolError) as caught:
        await DOMHandler.scroll_page(tab, direction="sideways")

    assert str(caught.value) == "Invalid scroll direction: sideways"
    assert tab.evaluate_calls == []
