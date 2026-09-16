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

        # The tool's read and the page's own answer, independently.
        assert record["scroll_y_after"] == await eval_js(iid, "window.scrollY")
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
        assert await eval_js(iid, "window.scrollY") == 0
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
        assert await eval_js(iid, "window.scrollY") == 0

        # An instant scroll on the same page is the fast path: no animation can
        # start, so the settle must not spend its budget waiting for one.
        instant = await scroll_page(
            instance_id=iid, direction="down", amount=500, smooth=False
        )
        assert instant["scrolled"] is False
        assert instant["settle_seconds"] < 1.0, instant
    finally:
        await close(instance_id=iid)
