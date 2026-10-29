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
  race is not logged at ERROR;
* **removed but still referenced** -- the page's own ``isConnected`` false is
  the same departure as Chrome's reply, never "not rendered": re-resolved
  before any input, and never answered with the synthetic ``el.click()``.

Hermetic: ``FakeTab`` + ``FakeClickTarget``, whose ``detached`` /
``detach_on`` model WHEN the page removed the element; the error follows.
``disconnected`` models a removed node that still resolves, whose reads
succeed and answer ``isConnected`` false.
"""

from __future__ import annotations

import logging

import pytest
from nodriver.core.connection import ProtocolException

from fakes import FakeClickTarget, FakeTab, left_the_document
from stealth_chrome_devtools_mcp.embedded import click_target, element_resolution
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


async def test_a_plain_click_that_fails_after_its_press_is_never_clicked_again():
    """Review HIGH. An element with no hit-testable point (here: off the
    viewport) took nodriver's ``mouse_click``, which sends the press AND the
    release before it returns; a plain error after them reached the synthetic
    fallback, and the page took a SECOND click. RED before: dispatch came back
    ``synthetic`` with the fallback run. The boundary is the first input, as
    recorded by ``click_target.InputSent``, never the error's type."""
    target = FakeClickTarget(
        rect=(-500.0, 20.0, 40.0, 20.0),
        fail_on={"mouseReleased": (RuntimeError("socket hiccup"), True)},
    )
    tab = _ReplacingTab(target, FakeClickTarget())
    synthetic_attempts: list[str] = []
    deliver = target.click

    async def attempted_click() -> None:
        synthetic_attempts.append("click")
        await deliver()

    target.click = attempted_click

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    assert "may have reached the page" in str(caught.value), str(caught.value)
    assert len(_presses(target)) == 1
    assert synthetic_attempts == []
    assert len(tab.select_calls) == 1


class _LeavesAfterAim(FakeClickTarget):
    """The page re-renders right after the aim read: the box read that follows
    it, still before any input, is the first thing to find the element gone."""

    async def apply(self, js_function, *args, **kwargs):
        answer = await super().apply(js_function, *args, **kwargs)
        if "elementFromPoint" in js_function:
            self.detached = True
        return answer


async def test_an_element_gone_before_the_first_input_inside_the_click_is_re_run():
    """The re-run boundary is the first INPUT, not the call into
    ``click_target.click``: its box read and probe arm send nothing."""
    gone = _LeavesAfterAim()
    fresh = FakeClickTarget()
    tab = _ReplacingTab(gone, fresh)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert gone.landings == []
    assert len(_presses(fresh)) == 1
    assert len(tab.select_calls) == 2
    assert result["dispatch"] == "coordinate"


# ---------------------------------------------------------------------------
# one deadline across the re-runs (review MED)
# ---------------------------------------------------------------------------


class _LateTab(FakeTab):
    """Answers each resolution from a script, then nothing."""

    def __init__(self, *answers) -> None:
        super().__init__(select_result=None)
        self._answers = list(answers)

    async def query_selector(self, selector, *args, **kwargs):
        self.select_calls.append(selector)
        return self._answers.pop(0) if self._answers else None


async def test_the_re_runs_spend_one_deadline(monkeypatch):
    """RED before: each re-run handed the caller's WHOLE timeout to a fresh
    resolution, so a late element that left and did not come back cost 1.5 s +
    a new 2 s wait on a 2 s budget. The clock is the module's timing seam."""
    clock = [0.0]

    async def advance(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(element_resolution, "_now", lambda: clock[0])
    monkeypatch.setattr(element_resolution, "_sleep", advance)
    monkeypatch.setattr(element_resolution, "_POLL_SECONDS", 0.5)
    tab = _LateTab(None, None, None, FakeClickTarget(detached=True))

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR, timeout=2000)

    assert "Element not found" in str(caught.value), str(caught.value)
    assert clock[0] <= 2.0, clock[0]


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
# (c2) removed but still referenced: the page says it left (headed-run defect)
# ---------------------------------------------------------------------------


async def test_the_aim_script_reads_the_fact_the_double_answers():
    """The double's ``connected`` stands for this script's ``isConnected`` read;
    a pin built on one must not outlive the other."""
    assert "elem.isConnected" in click_target.AIM_JS
    assert "connected: connected" in click_target.AIM_JS


async def test_an_element_the_page_removed_is_re_resolved_not_clicked_synthetically():
    """Measured, headed real Chrome, a ``#b`` whose container re-renders every
    25 ms: 14 of 15 clicks answered ``dispatch: synthetic, reason: not-rendered``
    and a capture-phase listener counted 0 mousedown and 0 click. The removed
    node still resolves, so the aim read SUCCEEDS with no client rects. RED
    before: ``el.click()`` ran on it and the tool reported success."""
    gone = FakeClickTarget(disconnected=True)
    fresh = FakeClickTarget()
    tab = _ReplacingTab(gone, fresh)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert len(tab.select_calls) == 2
    assert gone.landings == [] and "click" not in gone.calls
    assert len(_presses(fresh)) == 1, fresh.landings
    assert "click" not in fresh.calls
    assert result["dispatch"] == "coordinate"
    assert result["reason"] is None


async def test_an_element_removed_on_every_attempt_says_nothing_was_clicked():
    """RED before: the first removed node took the synthetic click and the tool
    answered success; nothing was re-resolved and nothing said it left."""
    targets = [FakeClickTarget(disconnected=True) for _ in range(_MAX_RESOLVES + 2)]
    tab = _ReplacingTab(*targets)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "nothing was clicked" in message, message
    assert len(tab.select_calls) == _MAX_RESOLVES
    assert all(t.landings == [] and "click" not in t.calls for t in targets)


async def test_removed_on_every_attempt_spends_one_deadline(monkeypatch):
    """The re-run a removed node causes spends the caller's ONE deadline: a
    node that turned up late (1.5 s into a 2 s budget) and had left leaves the
    next resolution only what is left. RED before: the late node took the
    synthetic click and the call answered success."""
    clock = [0.0]

    async def advance(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(element_resolution, "_now", lambda: clock[0])
    monkeypatch.setattr(element_resolution, "_sleep", advance)
    monkeypatch.setattr(element_resolution, "_POLL_SECONDS", 0.5)
    late = FakeClickTarget(disconnected=True)
    tab = _LateTab(None, None, None, late)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR, timeout=2000)

    assert "Element not found" in str(caught.value), str(caught.value)
    assert clock[0] <= 2.0, clock[0]
    assert late.landings == [] and "click" not in late.calls


class _RemovedAfterAim(FakeClickTarget):
    """The page removes the node right after the aim read (it still resolves):
    the quad read that follows fails with the error a ``display:none`` node
    gets, which is what reached the synthetic fallback."""

    async def apply(self, js_function, *args, **kwargs):
        answer = await super().apply(js_function, *args, **kwargs)
        if "elementFromPoint" in js_function:
            self.disconnected = True
        return answer


async def test_an_element_removed_after_the_aim_is_not_clicked_synthetically():
    """RED before: the quad error went to ``el.click()`` on the removed node
    and the answer was ``dispatch: synthetic``."""
    gone = _RemovedAfterAim()
    fresh = FakeClickTarget()
    tab = _ReplacingTab(gone, fresh)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert gone.landings == [] and "click" not in gone.calls
    assert len(_presses(fresh)) == 1
    assert len(tab.select_calls) == 2
    assert result["dispatch"] == "coordinate"


async def test_a_connected_display_none_target_still_gets_the_synthetic_click():
    """Regression guard (F-876): connected with no box is ``display:none``,
    where the synthetic ``el.click()`` is the only thing that reaches it. It is
    not a departure and is not re-resolved."""
    hidden = FakeClickTarget(
        rendered=False, mouse_click_error=RuntimeError("could not find position")
    )
    spare = FakeClickTarget()
    tab = _ReplacingTab(hidden, spare)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert result["dispatch"] == "synthetic"
    assert result["reason"] == "not-rendered"
    assert "click" in hidden.calls
    assert hidden.landings == []
    assert len(tab.select_calls) == 1
    assert spare.landings == [] and "click" not in spare.calls


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
