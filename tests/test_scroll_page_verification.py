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
            if expression.startswith(self.POSITION_JS_MARKER):
                return None
            return await super().evaluate(expression, *args, **kwargs)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.scroll_page(_MuteTab(), direction="down")

    assert type(caught.value) is ToolError
    assert "Failed to scroll page" in str(caught.value)


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

    assert "Invalid scroll direction: sideways" in str(caught.value)
    assert tab.evaluate_calls == []
