"""T-10034 (Sentry STEALTH-…-ER and -EP): the element leaves the page mid-tool.

Both reports are one page race: the element a tool resolved is removed or
replaced before the tool is done with it, and Chrome answers the next
``DOM.resolveNode`` with "No node with given id found" (ER, under
``click_target.aim``) or "Node with given id does not belong to the document"
(EP, under ``refresh_element``). The contract pinned here:

* **click, before any input** -- everything up to the call into
  ``click_target.click`` only reads or scrolls, so it is re-run against the
  element the page holds NOW, bounded by ``element_resolution``'s own bound,
  and the click lands exactly once;
* **click, after input may have gone out** -- never re-run and never answered
  with the synthetic ``el.click()``: a clear ``ToolError`` telling the caller
  to query the element again;
* **listing** -- ``query_elements`` drops an element that left, and a page
  race is not logged at ERROR.

Hermetic: ``FakeTab`` + ``FakeClickTarget``, whose ``detached`` /
``detach_on`` model WHEN the page removed the element; the error follows.
"""

from __future__ import annotations

import logging

import pytest
from nodriver.core.connection import ProtocolException

from fakes import FakeClickTarget, FakeTab, left_the_document
from stealth_chrome_devtools_mcp.embedded import element_resolution
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.element_resolution import (
    _MAX_RESOLVES,
    refresh_element,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = pytest.mark.asyncio

SELECTOR = "#target"
DETACHED_EP = "Node with given id does not belong to the document"


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    # Same zeroing as tests/test_element_resolution.py: the settle and the wait
    # are real code paths, only their durations go.
    monkeypatch.setattr(element_resolution, "_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(element_resolution, "_DEFAULT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(element_resolution, "_POLL_SECONDS", 0.0)


class _ReplacingTab(FakeTab):
    """A page that re-renders: each resolution answers the element it holds NOW.

    ``FakeTab`` routes pointer input to ``select_result``, so input reaches
    whichever element the latest resolution handed out, as on a real page.
    """

    def __init__(self, *targets: FakeClickTarget) -> None:
        super().__init__(select_result=targets[0])
        self._queue = list(targets)

    async def query_selector(self, selector, *args, **kwargs):
        if self._queue:
            self._select_result = self._queue.pop(0)
        return await super().query_selector(selector, *args, **kwargs)


def _presses(target: FakeClickTarget) -> list[tuple[str, float, float, bool]]:
    return [land for land in target.landings if land[0] == "mousePressed"]


# ---------------------------------------------------------------------------
# (a) gone before any input: re-resolved, clicked once
# ---------------------------------------------------------------------------


async def test_an_element_gone_while_aiming_is_re_resolved_and_clicked_once():
    """RED before the fix: the aim's ``Element.apply`` raised the ER error and
    the tool answered ``Failed to click element: No node with given id found``
    for a button the page had merely re-rendered."""
    gone = FakeClickTarget(detached=True)
    fresh = FakeClickTarget()
    tab = _ReplacingTab(gone, fresh)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert len(tab.select_calls) == 2
    assert _presses(gone) == []
    assert len(_presses(fresh)) == 1, fresh.landings
    assert "click" not in gone.calls and "click" not in fresh.calls
    assert result["dispatch"] == "coordinate"


# ---------------------------------------------------------------------------
# (b) gone after input may have gone out: never re-run
# ---------------------------------------------------------------------------


async def test_an_element_gone_after_a_probe_move_is_not_retried():
    """A probe ``mouseMoved`` is input: the page may have acted on the hover. So
    the click is not re-run on a fresh element and the synthetic fallback is
    not tried (RED before: it was, and its error was the one reported)."""
    hovered = FakeClickTarget(detach_on="mouseMoved")
    spare = FakeClickTarget()
    tab = _ReplacingTab(hovered, spare)
    # ATTEMPTS, not deliveries: nodriver 0.50's ``Element.click`` resolves the
    # node first and fails there, so a delivery count cannot see the fallback
    # being tried. The product must not try it at all.
    synthetic_attempts: list[str] = []
    deliver = hovered.click

    async def attempted_click() -> None:
        synthetic_attempts.append("click")
        await deliver()

    hovered.click = attempted_click

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "left the page while it was being clicked" in message, message
    assert "Query the element again" in message, message
    assert len(tab.select_calls) == 1
    assert spare.landings == []
    assert synthetic_attempts == []


async def test_a_delivered_press_is_never_repeated_when_the_element_leaves():
    """The press reached the page, the element went with it, and the press's
    reply is the departure. The click count stays ONE."""
    pressed = FakeClickTarget(
        detach_on="mousePressed",
        fail_on={"mousePressed": (left_the_document(), True)},
    )
    spare = FakeClickTarget()
    tab = _ReplacingTab(pressed, spare)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    assert "left the page while it was being clicked" in str(caught.value)
    assert len(_presses(pressed)) == 1
    assert spare.landings == []
    assert len(tab.select_calls) == 1
    assert "click" not in pressed.calls
    assert pressed.held is False


async def test_an_element_that_leaves_on_its_own_mousedown_is_a_clicked_element():
    """The click removing its own target is a click that worked, not a failure
    -- the read between press and release is the only thing that fails."""
    removed = FakeClickTarget(detach_on="mousePressed")
    tab = _ReplacingTab(removed, FakeClickTarget())

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert len(_presses(removed)) == 1
    assert len(tab.select_calls) == 1
    assert result["dispatch"] == "coordinate"


# ---------------------------------------------------------------------------
# (c) the re-resolve is bounded
# ---------------------------------------------------------------------------


async def test_the_re_resolve_is_bounded_and_says_nothing_was_clicked():
    targets = [FakeClickTarget(detached=True) for _ in range(_MAX_RESOLVES + 2)]
    tab = _ReplacingTab(*targets)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "nothing was clicked" in message, message
    assert len(tab.select_calls) == _MAX_RESOLVES
    assert all(t.landings == [] and "click" not in t.calls for t in targets)


# ---------------------------------------------------------------------------
# (d) a listing drops an element that left, without an ERROR
# ---------------------------------------------------------------------------


class _ListedElement:
    def __init__(self, tag: str, *, gone: bool = False) -> None:
        self.tag_name = tag
        self.text_all = tag
        self.attrs: dict[str, str] = {}
        self._gone = gone

    async def update(self):
        if self._gone:
            raise left_the_document(DETACHED_EP)
        return self

    async def get_position(self):
        return None


class _ListingTab:
    def __init__(self, elements):
        self._elements = elements

    async def query_selector_all(self, selector, *args, **kwargs):
        return list(self._elements)


@pytest.fixture
def backend_errors(caplog):
    """ERROR records on ``stealth.backend``, where EP was logged. The handler is
    attached to that logger itself so a ``propagate=False`` elsewhere cannot
    hide a record."""
    backend = logging.getLogger("stealth.backend")
    backend.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="stealth.backend")
    try:
        yield lambda: [r for r in caplog.records if r.levelno >= logging.ERROR]
    finally:
        backend.removeHandler(caplog.handler)


async def test_query_elements_drops_an_element_that_left_without_an_error(
    backend_errors,
):
    tab = _ListingTab([_ListedElement("div", gone=True), _ListedElement("span")])

    results = await DOMHandler.query_elements(tab, "div, span", visible_only=False)

    assert [r.tag_name for r in results] == ["span"]
    assert backend_errors() == []


async def test_refresh_element_answers_whether_the_element_is_still_there():
    assert await refresh_element(FakeTab(), _ListedElement("div")) is True
    assert await refresh_element(FakeTab(), _ListedElement("div", gone=True)) is False
    assert await refresh_element(FakeTab(), None) is True


async def test_refresh_element_still_raises_any_other_protocol_error():
    class _Broken(_ListedElement):
        async def update(self):
            raise ProtocolException({"message": "Some unrelated CDP failure"})

    with pytest.raises(ProtocolException):
        await refresh_element(FakeTab(), _Broken("div"))


async def test_get_element_state_of_an_element_that_left_says_so():
    class _OneTab:
        async def query_selector(self, selector, *args, **kwargs):
            return _ListedElement("div", gone=True)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.get_element_state(_OneTab(), SELECTOR)

    assert "left the page after it was resolved" in str(caught.value)
