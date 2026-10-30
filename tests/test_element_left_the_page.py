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
  before any input, and never answered with the synthetic ``el.click()``;
* **removed but still referenced, in a listing or a state read** (T-10284) --
  ``update()`` succeeds for such a node, so ``refresh_element`` also reads
  ``isConnected`` and ``query_elements`` / ``get_element_state`` treat it as
  gone, never as a visible element with no box;
* **a connectivity read that FAILED** (Codex on T-10284) -- only the page's own
  ``no`` token is a departure. A getter that throws (an Error, or ``throw
  true``, which ``Element.apply`` hands back as a plain ``True``) or a script
  that answers anything but its tokens is a ``ToolError`` that names the read:
  never "connected", never "left", never a silent drop from a listing.

Hermetic: ``FakeTab`` + ``FakeClickTarget``, whose ``detached`` /
``detach_on`` model WHEN the page removed the element; the error follows.
``disconnected`` models a removed node that still resolves, whose reads
succeed and answer ``isConnected`` false.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from pathlib import Path

import pytest
from nodriver.core.connection import ProtocolException

import fakes_elements
from fakes import FakeClickTarget, FakeTab, left_the_document
from stealth_chrome_devtools_mcp.embedded import (
    click_target,
    dom_handler,
    element_resolution,
)
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.element_resolution import (
    _MAX_RESOLVES,
    refresh_element,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = pytest.mark.asyncio

SELECTOR = "#target"
DETACHED_EP = "Node with given id does not belong to the document"
#: The connectivity tokens spelled out, so the module still collects against a
#: parent that has no such constants (the RED run);
#: ``test_the_double_speaks_the_products_connectivity_vocabulary`` keeps them equal.
YES = "stealth.connected:yes"
NO = "stealth.connected:no"
THREW = "stealth.connected:threw"


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

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "left the page while it was being clicked" in message, message
    assert "Query the element again" in message, message
    assert len(tab.select_calls) == 1
    assert spare.landings == []
    # ATTEMPTS, not deliveries: a synthetic call on a gone node fails before it
    # clicks, so a delivery count cannot see the fallback being tried. The
    # double records every attempt, through either sender.
    assert "synthetic" not in hovered.calls


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

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    assert "may have reached the page" in str(caught.value), str(caught.value)
    assert len(_presses(target)) == 1
    assert "synthetic" not in target.calls
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
    """The double's ``connected`` stands for this script's connectivity token;
    a pin built on one must not outlive the other."""
    assert click_target.CONNECTIVITY_JS in click_target.AIM_JS
    assert "const state = connectivity(elem);" in click_target.AIM_JS
    assert "connected: state" in click_target.AIM_JS


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
    assert hidden.calls.count("synthetic") == 1
    assert hidden.calls.count("click") == 1
    assert hidden.landings == []
    assert len(tab.select_calls) == 1
    assert spare.landings == [] and "click" not in spare.calls


# ---------------------------------------------------------------------------
# (c3) the synthetic click checks and clicks in ONE page-side call (Codex HIGH)
# ---------------------------------------------------------------------------


def _hidden(**kwargs) -> FakeClickTarget:
    """A connected ``display:none`` target: no box, so the synthetic path."""
    return FakeClickTarget(
        rendered=False,
        mouse_click_error=RuntimeError("could not find position"),
        **kwargs,
    )


async def test_the_synthetic_script_checks_before_it_clicks():
    """The double reads the script's answers off the script itself; what it
    cannot read is that the real script asks for connectivity BEFORE it clicks,
    in the same function, clicks only on the ``yes`` token, and catches what the
    page's ``click()`` throws."""
    js = click_target.SYNTHETIC_JS
    read = "const state = connectivity(elem);"
    detached = (
        f'if (state === "{click_target.CONNECTED_NO}") '
        f'return "{click_target.SYNTHETIC_DETACHED}";'
    )
    unreadable = (
        f'if (state !== "{click_target.CONNECTED_YES}") '
        f'return "{click_target.SYNTHETIC_UNREAD}";'
    )
    caught = f'try {{ click.call(elem); }} catch (e) {{ return "{click_target.SYNTHETIC_THREW}"; }}'
    assert js.index(read) < js.index(detached) < js.index(unreadable)
    assert js.index(unreadable) < js.index(caught)


async def test_an_element_removed_after_the_last_read_is_never_clicked_synthetically():
    """Codex review HIGH: a connected ``display:none`` button passed every check
    and the page's next re-render removed it before ``el.click()`` ran -- on
    192baaa that was AFTER the second aim, inside nodriver's ``Element.click``.
    The check and the click are now one page-side call, so the page answers
    ``false`` and the attempt is re-run. RED on 192baaa: ``el.click()`` ran on
    the removed node and the answer was ``dispatch: synthetic``."""
    hidden = _hidden(removed_before_click=True)
    fresh = FakeClickTarget()
    tab = _ReplacingTab(hidden, fresh)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert "click" not in hidden.calls
    assert hidden.calls.count("synthetic") == 1
    assert len(_presses(fresh)) == 1
    assert len(tab.select_calls) == 2
    assert result["dispatch"] == "coordinate"


async def test_the_page_answering_false_on_every_attempt_clicks_nothing():
    targets = [_hidden(removed_before_click=True) for _ in range(_MAX_RESOLVES + 2)]
    tab = _ReplacingTab(*targets)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "nothing was clicked" in message, message
    assert len(tab.select_calls) == _MAX_RESOLVES
    assert all("click" not in t.calls and t.landings == [] for t in targets)
    assert [t.calls.count("synthetic") for t in targets] == [1, 1, 1, 0, 0]


async def test_false_then_true_is_re_run_once_and_clicked_once():
    removed = _hidden(removed_before_click=True)
    hidden = _hidden()
    tab = _ReplacingTab(removed, hidden)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert len(tab.select_calls) == 2
    assert "click" not in removed.calls
    assert hidden.calls.count("click") == 1
    assert result["dispatch"] == "synthetic"


class _HangingSynthetic(FakeClickTarget):
    """The synthetic call's reply never comes back."""

    async def apply(self, js_function, *args, **kwargs):
        if js_function == click_target.SYNTHETIC_JS:
            self.calls.append("synthetic")
            await asyncio.Event().wait()
        return await super().apply(js_function, *args, **kwargs)


@pytest.mark.parametrize("failure", ["transport", "timeout"])
async def test_a_synthetic_call_that_fails_is_never_re_run(monkeypatch, failure):
    """No boolean came back, so the page may have run ``el.click()``: never
    re-run, never repeated. RED on 192baaa because the failure is modelled on
    the one sender that now exists; the rule is the post-input rule."""
    monkeypatch.setattr(click_target, "INPUT_BUDGET_S", 0.05)
    if failure == "transport":
        hidden = _hidden(synthetic_error=ConnectionError("socket closed"))
    else:
        hidden = _HangingSynthetic(
            rendered=False, mouse_click_error=RuntimeError("could not find position")
        )
    spare = FakeClickTarget()
    tab = _ReplacingTab(hidden, spare)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "may have reached the page" in message, message
    assert len(tab.select_calls) == 1
    assert hidden.calls.count("synthetic") == 1
    assert spare.landings == [] and "synthetic" not in spare.calls


@pytest.mark.parametrize(
    ("answer", "clicked", "sent"),
    [(False, False, False), (True, True, True)],
)
async def test_synthetic_click_records_input_unless_the_page_said_false(
    answer, clicked, sent
):
    """``InputSent`` after the one synthetic sender: only the page's definite
    ``false`` -- ``el.click()`` never ran -- leaves it clear for a re-run."""
    target = _hidden(removed_before_click=not answer)
    ledger = click_target.InputSent()

    assert await click_target.synthetic_click(target, SELECTOR, ledger) is clicked
    assert ledger.sent is sent


async def test_synthetic_click_records_input_when_no_answer_came_back():
    target = _hidden(synthetic_error=ConnectionError("socket closed"))
    ledger = click_target.InputSent()

    with pytest.raises(ConnectionError):
        await click_target.synthetic_click(target, SELECTOR, ledger)
    assert ledger.sent is True


async def test_synthetic_click_refuses_an_answer_that_is_not_a_boolean():
    class _Blank(FakeClickTarget):
        async def apply(self, js_function, *args, **kwargs):
            return None  # a script that THREW lands here as None

    ledger = click_target.InputSent()
    with pytest.raises(ToolError) as caught:
        await click_target.synthetic_click(_Blank(), SELECTOR, ledger)
    assert "may have reached the page" in str(caught.value)
    assert ledger.sent is True


# ---------------------------------------------------------------------------
# (c4) only the script's own "detached" answer means nothing ran (Codex re-review)
# ---------------------------------------------------------------------------


def _same_node(target: FakeClickTarget) -> _ReplacingTab:
    """A page that did NOT replace the element: every resolution finds it."""
    return _ReplacingTab(*([target] * (_MAX_RESOLVES + 1)))


async def test_a_click_that_throws_false_after_clicking_is_never_re_run():
    """Codex re-review HIGH: ``click()`` overridden to call the native click and
    then ``throw false``. ``Element.apply`` hands back the thrown ``false`` as a
    plain value, which on 5a30f2a meant "the node had left": the click was
    re-run three times and the page took three activations. RED on 5a30f2a."""
    hidden = _hidden(click_throws=False)
    tab = _same_node(hidden)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "click handler" in message and "threw" in message, message
    assert "may have reached the page" in message, message
    assert hidden.calls.count("click") == 1
    assert len(tab.select_calls) == 1


#: ``click_target.SYNTHETIC_DETACHED`` spelled out, so the module still collects
#: against a parent that has no such constant (the RED run);
#: ``test_the_detached_token_is_the_one_the_tests_throw`` keeps them equal.
DETACHED_TOKEN = "stealth.click.synthetic:detached"


async def test_the_detached_token_is_the_one_the_tests_throw():
    assert click_target.SYNTHETIC_DETACHED == DETACHED_TOKEN


@pytest.mark.parametrize(
    "thrown",
    [False, True, 0, "", None, {"x": 1}, DETACHED_TOKEN],
    ids=["false", "true", "zero", "empty", "undefined", "object", "the-token"],
)
async def test_a_click_that_throws_any_value_is_never_re_run(thrown):
    hidden = _hidden(click_throws=thrown)
    tab = _same_node(hidden)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "threw" in message and "may have reached the page" in message, message
    assert hidden.calls.count("click") == 1
    assert hidden.calls.count("synthetic") == 1
    assert len(tab.select_calls) == 1


@pytest.mark.parametrize(
    "answer", [True, False, None], ids=["true", "false", "undefined"]
)
async def test_an_answer_that_is_not_a_token_is_never_re_run(answer):
    """``undefined`` arrives from ``Element.apply`` as ``None``."""
    hidden = _hidden(synthetic_answer=answer)
    tab = _same_node(hidden)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "not one of its own answers" in message, message
    assert "may have reached the page" in message, message
    assert hidden.calls.count("synthetic") == 1
    assert len(tab.select_calls) == 1


async def test_only_the_exact_detached_token_leaves_the_ledger_clear():
    exact = click_target.InputSent()
    near = click_target.InputSent()

    detached = _hidden(synthetic_answer=click_target.SYNTHETIC_DETACHED)
    assert await click_target.synthetic_click(detached, SELECTOR, exact) is False
    assert exact.sent is False

    almost = _hidden(synthetic_answer=click_target.SYNTHETIC_DETACHED + " ")
    with pytest.raises(ToolError):
        await click_target.synthetic_click(almost, SELECTOR, near)
    assert near.sent is True


async def test_a_click_handler_that_threw_sets_the_ledger():
    ledger = click_target.InputSent()
    with pytest.raises(ToolError) as caught:
        await click_target.synthetic_click(
            _hidden(click_throws=False), SELECTOR, ledger
        )
    assert "click handler" in str(caught.value)
    assert ledger.sent is True


# ---------------------------------------------------------------------------
# (c5) a <form>'s named controls shadow ANY member (Codex round 5)
# ---------------------------------------------------------------------------

#: Everything a connectivity read can hand back that is NOT one of the two
#: tokens: each one a read that did not succeed (Codex on T-10284).
_NOT_AN_ANSWER = [
    THREW,
    True,
    False,
    None,
    0,
    1,
    "",
    "true",
    {},
    YES.upper(),
    YES + " ",
]
_NOT_AN_ANSWER_IDS = [
    "threw-token",
    "thrown-true",
    "plain-false",
    "undefined",
    "zero",
    "one",
    "empty",
    "string-true",
    "shadowed-object",
    "shouting",
    "padded",
]

#: Every member the two scripts must never read off the target directly.
_SHADOWABLE = ("isConnected", "click", "getClientRects")


def _form(**kwargs) -> FakeClickTarget:
    """A ``display:none`` ``<form>`` whose controls are named after members."""
    return _hidden(tag="form", form_controls=_SHADOWABLE, **kwargs)


async def test_the_click_scripts_read_the_target_only_through_native_accessors():
    """RED on ca45646: both scripts read ``elem.isConnected`` and call
    ``elem.getClientRects()`` / ``elem.click()``, which a form's named control
    shadows with no page script involved."""
    for name, js in (
        ("AIM_JS", click_target.AIM_JS),
        ("SYNTHETIC_JS", click_target.SYNTHETIC_JS),
        ("ARM_PROBE_JS", click_target.ARM_PROBE_JS),
    ):
        assert re.search(r"\b(?:elem|node|owner|r)\.[A-Za-z]", js) is None, name
    natives = (
        "getClientRects",
        "matches",
        "closest",
        "contains",
        "getRootNode",
    )
    for native in natives:
        assert f"native(elem, '{native}')" in click_target.AIM_JS, native
    assert "native(doc, 'elementFromPoint')" in click_target.AIM_JS
    # The connectivity read is the one shared fragment, which reads the native
    # accessor itself (CONNECTIVITY_JS) -- neither script has its own.
    assert click_target.CONNECTIVITY_JS in click_target.AIM_JS
    assert click_target.CONNECTIVITY_JS in click_target.SYNTHETIC_JS
    assert "const click = native(elem, 'click');" in click_target.SYNTHETIC_JS
    assert "click.call(elem)" in click_target.SYNTHETIC_JS


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [(YES, True), (NO, False)],
    ids=["yes", "no"],
)
async def test_the_two_page_answers_are_the_only_verdicts(answer, verdict):
    assert click_target.connectivity(answer, "the target") is verdict


@pytest.mark.parametrize("answer", _NOT_AN_ANSWER, ids=_NOT_AN_ANSWER_IDS)
async def test_nothing_but_the_two_tokens_is_a_verdict(answer):
    """The page's ``threw`` token, a ``true`` a getter threw (which
    ``Element.apply`` hands back as an ordinary ``True``), ``None`` from a thrown
    Error, a shadowed object, a near-miss spelling: each is a read that did not
    succeed, so none of them is a verdict about the node."""
    with pytest.raises(ToolError) as caught:
        click_target.connectivity(answer, "the target")

    assert "could not read whether the target is still in its document" in str(
        caught.value
    )


async def test_an_aim_answer_with_no_connected_field_is_a_read_error():
    """A field the script did not report is not "not connected": nothing was
    asked, so nothing is re-resolved and nothing is sent."""
    record = json.dumps(
        {"rendered": False, "rect": {"left": 0, "top": 0, "width": 0, "height": 0}}
    )
    target = FakeClickTarget(aim_answer=record)
    tab = _ReplacingTab(target)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    assert "could not read whether" in str(caught.value), caught.value
    assert len(tab.select_calls) == 1
    assert target.landings == [] and "click" not in target.calls


async def test_a_removed_form_that_shadows_is_connected_is_re_resolved():
    """Codex round 5, its exact shape: ``<form><input name=isConnected>`` that a
    re-render removed but still references. RED on ca45646: the aim read
    ``connected: {}``, ``connected()`` accepted it, the synthetic script read
    the truthy control, clicked the removed form and answered CLICKED."""
    removed = _hidden(tag="form", form_controls=("isConnected",), disconnected=True)
    fresh = FakeClickTarget()
    tab = _ReplacingTab(removed, fresh)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert "click" not in removed.calls
    assert len(tab.select_calls) == 2
    assert len(_presses(fresh)) == 1
    assert result["dispatch"] == "coordinate"


async def test_a_removed_form_that_shadows_every_member_is_re_resolved():
    """RED on ca45646: ``elem.getClientRects()`` called the control and the aim
    read threw, so the call failed with "could not read where a click on it
    would land" instead of re-resolving."""
    removed = _form(disconnected=True)
    fresh = FakeClickTarget()
    tab = _ReplacingTab(removed, fresh)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert "click" not in removed.calls
    assert len(tab.select_calls) == 2
    assert result["dispatch"] == "coordinate"


async def test_a_form_removed_after_the_aim_is_never_clicked_synthetically():
    """The synthetic script's own read is the one that matters here: the aim
    saw a connected form, the page removed it before the synthetic click ran,
    and a shadowed ``isConnected`` would have read the truthy control."""
    removed = _hidden(
        tag="form", form_controls=("isConnected",), removed_before_click=True
    )
    fresh = FakeClickTarget()
    tab = _ReplacingTab(removed, fresh)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert "click" not in removed.calls
    assert removed.calls.count("synthetic") == 1
    assert len(tab.select_calls) == 2
    assert result["dispatch"] == "coordinate"


async def test_a_connected_form_that_shadows_every_member_is_clicked_once():
    """RED on ca45646: the aim's ``elem.getClientRects()`` threw on the control
    and nothing was clicked; a connected ``display:none`` form is exactly what
    the synthetic fallback exists for."""
    form = _form()
    tab = _same_node(form)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert result["dispatch"] == "synthetic"
    assert result["reason"] == "not-rendered"
    assert form.calls.count("click") == 1
    assert form.calls.count("synthetic") == 1
    assert len(tab.select_calls) == 1


# ---------------------------------------------------------------------------
# (d) a listing drops an element that left, without an ERROR
# ---------------------------------------------------------------------------


class _ListedElement(FakeClickTarget):
    """A listed element whose ``update`` fails the way EP's does when it is
    ``gone`` (Chrome dropped it); everything else is ``FakeClickTarget``."""

    def __init__(self, tag: str, *, gone: bool = False) -> None:
        super().__init__(tag=tag, element_id="", text=tag)
        self._gone = gone

    async def update(self):
        if self._gone:
            raise left_the_document(DETACHED_EP)
        return await super().update()


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


# ---------------------------------------------------------------------------
# (e) a node the page removed but still references (T-10284)
# ---------------------------------------------------------------------------
#
# ``Element.update()`` re-resolves by backend node id and that SUCCEEDS for a
# detached node, so before this ``refresh_element`` answered "still there" for
# it: ``query_elements`` listed it as visible (an empty computed style passes
# ``display !== 'none'``) with no bounding box, and ``get_element_state`` read
# ``is_visible: True`` and then failed at ``get_position`` with "Could not
# compute content quads." The fake answers the connectivity read ONLY for a
# script carrying the shipped fragment, with that fragment's token for the
# page's state; anything else it answers with nothing, as a script that threw.


class _TabOf:
    """A tab that resolves exactly the elements it was given, for both reads."""

    def __init__(self, *elements) -> None:
        self._elements = list(elements)

    async def query_selector_all(self, selector, *args, **kwargs):
        return list(self._elements)

    async def query_selector(self, selector, *args, **kwargs):
        return self._elements[0] if self._elements else None


@pytest.fixture
def backend_log(caplog):
    """Every record on ``stealth.backend``, whatever its level, once each."""
    backend = logging.getLogger("stealth.backend")
    backend.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="stealth.backend")
    try:
        yield lambda: list(dict.fromkeys(caplog.records))
    finally:
        backend.removeHandler(caplog.handler)


def _item(element_id: str, **kwargs) -> FakeClickTarget:
    return FakeClickTarget(tag="li", element_id=element_id, text=element_id, **kwargs)


def _ids(results) -> list[str]:
    return [r.attributes["id"] for r in results]


@pytest.mark.parametrize("visible_only", [True, False])
async def test_query_elements_drops_a_node_the_page_removed_but_still_references(
    visible_only, backend_log, backend_errors
):
    """RED on f9fe86e: ``update()`` succeeded for the removed ``<li>``, so it was
    listed (visible, with ``bounding_box`` None) beside the live one."""
    removed = _item("removed", disconnected=True)
    live = _item("live")

    results = await DOMHandler.query_elements(
        _TabOf(removed, live), "li", visible_only=visible_only
    )

    assert _ids(results) == ["live"]
    assert results[0].bounding_box == {
        "x": 10.0,
        "y": 20.0,
        "width": 40.0,
        "height": 20.0,
    }
    assert backend_errors() == []
    dropped = [
        r
        for r in backend_log()
        if "left the document after resolution" in r.getMessage()
    ]
    assert [r.levelno for r in dropped] == [logging.INFO]


async def test_a_node_that_leaves_after_the_refresh_is_never_listed_as_visible():
    """The window between the connectivity read and the visibility read: the
    page removes the node there. An empty computed style passes the style tests,
    so the visibility script must ask ``isConnected`` itself, through the same
    answer. RED on f9fe86e: it was listed as visible."""

    class _RemovedAfterTheRefresh(FakeClickTarget):
        async def apply(self, js_function, *args, **kwargs):
            answer = await super().apply(js_function, *args, **kwargs)
            if "getComputedStyle" not in js_function:
                self.disconnected = True
            return answer

    gone = _RemovedAfterTheRefresh(tag="li", element_id="gone", text="gone")

    results = await DOMHandler.query_elements(_TabOf(gone), "li", visible_only=True)

    assert results == []


@pytest.mark.parametrize("visible_only", [True, False])
async def test_a_connected_node_is_listed_unchanged_with_one_connectivity_read(
    visible_only,
):
    live = _item("live")

    results = await DOMHandler.query_elements(
        _TabOf(live), "li", visible_only=visible_only
    )

    assert _ids(results) == ["live"]
    assert results[0].is_visible is True
    assert results[0].bounding_box is not None
    reads = [js for js in live.apply_calls if js == click_target.CONNECTED_JS]
    assert len(reads) == 1
    assert len(live.apply_calls) == (2 if visible_only else 1)


async def test_a_connected_display_none_node_is_still_filtered_by_visible_only():
    hidden = _item("hidden", rendered=False)

    assert await DOMHandler.query_elements(_TabOf(hidden), "li") == []
    listed = await DOMHandler.query_elements(_TabOf(hidden), "li", visible_only=False)
    assert _ids(listed) == ["hidden"]


async def test_get_element_state_of_a_removed_but_referenced_node_says_it_left():
    """RED on f9fe86e: ``is_visible: True`` was computed, then ``get_position``
    failed with "Could not compute content quads."."""
    removed = _item("removed", disconnected=True)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.get_element_state(_TabOf(removed), SELECTOR)

    assert "left the page after it was resolved" in str(caught.value)
    assert "content quads" not in str(caught.value)


async def test_get_element_state_of_a_connected_node_is_unchanged():
    live = _item("live")

    state = await DOMHandler.get_element_state(_TabOf(live), SELECTOR)

    assert state["tag_name"] == "li"
    assert state["attributes"] == {"id": "live"}
    assert state["is_visible"] is True
    assert state["position"].center == (30.0, 30.0)


async def test_refresh_element_reads_connectivity_once_and_only_after_update():
    removed = _item("removed", disconnected=True)
    live = _item("live")
    gone = _ListedElement("li", gone=True)

    assert await refresh_element(FakeTab(), removed) is False
    assert await refresh_element(FakeTab(), live) is True
    assert await refresh_element(FakeTab(), gone) is False
    assert removed.apply_calls == [click_target.CONNECTED_JS]
    assert live.apply_calls == [click_target.CONNECTED_JS]
    # Chrome's own reply already said it: nothing left to ask the page.
    assert gone.apply_calls == []


def _package_root() -> Path:
    return Path(click_target.__file__).parents[1]


def _folded(node: ast.AST) -> str | None:
    """The string a constant, an f-string or a ``'a' + 'b'`` evaluates to."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [_folded(value) for value in node.values]
        return "".join(part for part in parts if part is not None)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        return None if left is None or right is None else left + right
    return None


def _string_constants(path: Path) -> list[tuple[int, str]]:
    """Every string a module's code can hand to a page, as (line, text).

    The WHOLE module (T-10284 review): every string at any depth except a
    docstring, whatever its quote style. The pieces of an f-string are joined
    and a ``'is' + 'Connected'`` is folded, so splitting a spelling across
    constants does not hide it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scopes = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, scopes)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found = []
    for node in ast.walk(tree):
        text = None if id(node) in docstrings else _folded(node)
        if text:
            found.append((node.lineno, text))
    return found


def _package_scripts() -> list[tuple[str, int, str]]:
    """Every ``(file, line, text)`` the package could run in a page: all string
    constants of every ``.py`` under the package (recursively, so
    ``tool_sections/`` and the rest of the tree are in) and every ``.js`` file."""
    root = _package_root()
    scripts = [
        (path.relative_to(root).as_posix(), line, text)
        for path in sorted(root.rglob("*.py"))
        for line, text in _string_constants(path)
    ]
    scripts += [
        (path.relative_to(root).as_posix(), 1, path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.js"))
    ]
    return scripts


async def test_the_one_connectivity_read_builds_every_script_that_asks():
    for name, js in (
        ("CONNECTED_JS", click_target.CONNECTED_JS),
        ("SYNTHETIC_JS", click_target.SYNTHETIC_JS),
        ("VISIBLE_JS", element_resolution.VISIBLE_JS),
        ("AIM_JS", click_target.AIM_JS),
    ):
        assert click_target.CONNECTIVITY_JS in js, name
        assert re.search(r"\belem\.isConnected\b", js) is None, name


#: Any spelling of the property: single, double or back quotes, ``.isConnected``,
#: ``["isConnected"]``, ``function (e) { return e.isConnected }`` all contain the
#: word; ``'is' + 'Connected'`` and a template literal with a literal part are
#: folded into it first (:func:`_normalised`). The one place that may is the
#: shared fragment.
_IS_CONNECTED = re.compile(r"isConnected")

_QUOTE = r"['\"`]"
_AFFIXED = r"(?:[A-Z]\w*)?(?:Element|Node|Root|Document|Text|Fragment)"

#: A DOM interface's GLOBAL by name: any capitalised identifier ending in
#: ``Element`` / ``Node`` / ``Root`` / ``Document`` / ``Text`` / ``Fragment``
#: (``HTMLInputElement``, ``SVGElement``, ``ShadowRoot``, ``DocumentFragment``)
#: and those words themselves (``Node``, ``Element``, ``Document``, ``Text``),
#: with ``EventTarget``; bare, after ``window.`` / ``globalThis.`` / ``self.``
#: (the word boundary finds the name after the dot) or as ``window['X']``.
_DOM_NAME = rf"(?:\b{_AFFIXED}\b|\bEventTarget\b|\[\s*{_QUOTE}{_AFFIXED}{_QUOTE}\s*\])"
#: What a page's own ``class Node {}`` / ``var Node = 1`` breaks: reading
#: ``prototype`` (``X.prototype``, ``X?.prototype``, ``X["prototype"]``,
#: ``Reflect.get(X, 'prototype')``, ``const {prototype} = X``), reading a constant
#: (``Node.ELEMENT_NODE``) and ``instanceof X`` / ``instanceof window.X``.
_GLOBAL_INTERFACE_READ = re.compile(
    rf"{_DOM_NAME}\s*(?:\?\.|\.)\s*(?:prototype\b|[A-Z][A-Z0-9_]+\b)"
    rf"|{_DOM_NAME}\s*(?:\?\.)?\s*\[\s*{_QUOTE}prototype"
    rf"|\bReflect\s*\.\s*get\s*\(\s*{_DOM_NAME}\s*,\s*{_QUOTE}prototype"
    rf"|\{{[^}}]*\bprototype\b[^}}]*\}}\s*=\s*{_DOM_NAME}"
    rf"|\binstanceof\s+(?:(?:window|globalThis|self)\s*\.\s*)?{_DOM_NAME}"
)

#: ``'a' + 'b'`` and ``${"b"}`` are strings a script is free to build; the
#: census reads what they build.
_CONCATENATION = re.compile(rf"{_QUOTE}\s*\+\s*{_QUOTE}")
_TEMPLATE_PART = re.compile(rf"\$\{{\s*{_QUOTE}([^'\"`]*){_QUOTE}\s*\}}")


def _normalised(text: str) -> str:
    """*text* with literal concatenations and literal template parts folded."""
    previous = None
    while previous != text:
        previous = text
        text = _TEMPLATE_PART.sub(r"\1", _CONCATENATION.sub("", text))
    return text


def _census_hits(text: str) -> list[str]:
    """What the census flags in one string: every spelling of ``isConnected`` and
    every read of a DOM interface through its global."""
    text = _normalised(text)
    return [m.group(0) for m in _IS_CONNECTED.finditer(text)] + [
        m.group(0) for m in _GLOBAL_INTERFACE_READ.finditer(text)
    ]


async def test_the_census_sees_the_whole_package():
    """The census is only as good as what it scans (review finding 5): it used
    to glob ``embedded/*.py`` and skip ``tool_sections/`` and the rest."""
    files = {name for name, _, _ in _package_scripts()}
    assert "embedded/click_target.py" in files
    assert "embedded/page_natives.py" in files
    assert "server.py" in files
    assert any(name.startswith("embedded/tool_sections/") for name in files), files
    assert any(name.endswith(".js") for name in files)


async def test_no_other_script_in_the_package_reads_is_connected():
    """Codex finding 4, widened by review finding 5: ``isConnected`` is spelled,
    in any form, in exactly ONE string of the whole package -- the shared
    fragment -- and in no ``.js`` file."""
    spelled = [
        (name, text)
        for name, _, text in _package_scripts()
        if _IS_CONNECTED.search(_normalised(text))
    ]
    assert [name for name, _ in spelled] == ["embedded/click_target.py"], [
        name for name, _ in spelled
    ]
    only = spelled[0][1]
    assert only.count("isConnected") == 1
    assert len(_census_hits(only)) == 1
    assert "native(node, 'isConnected').call(node)" in only
    assert only.strip().startswith("const connectivity = (node) =>")


async def test_no_script_in_the_package_reads_a_dom_interface_through_its_global():
    """Review finding 1: a page that declares a top-level ``class Node {}`` made
    every ``Object.getOwnPropertyDescriptor(Node.prototype, 'isConnected')``
    throw. No script anywhere in the package may read ``Node.prototype`` (or any
    interface's): the members come from the node's own prototype chain, through
    ``page_natives.NATIVE_JS``, the one helper."""
    spelled = [
        (name, line, hit)
        for name, line, text in _package_scripts()
        for hit in _GLOBAL_INTERFACE_READ.findall(_normalised(text))
    ]
    assert spelled == []
    # Imported here, not at the top: the module still collects against a parent
    # that has no ``page_natives`` (the RED run).
    from stealth_chrome_devtools_mcp.embedded import page_natives

    assert "Object.getPrototypeOf(obj)" in page_natives.NATIVE_JS
    for name, js in (
        ("CONNECTIVITY_JS", click_target.CONNECTIVITY_JS),
        ("ROOTS_JS", page_natives.ROOTS_JS),
        ("NATIVES_JS", page_natives.NATIVES_JS),
        ("SYNTHETIC_JS", click_target.SYNTHETIC_JS),
        ("ARM_PROBE_JS", click_target.ARM_PROBE_JS),
    ):
        assert page_natives.NATIVE_JS in js, name


async def test_native_takes_the_farthest_owner_and_stops_before_the_last_prototype():
    """Review finding 1 (MED). ``native`` used to return the FIRST owner up the
    chain, so a page's custom element with its own ``get isConnected()`` /
    ``get id()`` / ``click()``, a ``setPrototypeOf`` or an accessor added on
    ``HTMLElement.prototype`` steered every read without replacing a built-in
    member. The walk takes the FARTHEST owner (the interface's own member: a
    page can only add NEARER ones) and never reads the chain's last prototype
    (``Object.prototype``, whose own prototype is ``null``), so pollution there
    is ignored too. What the walk does in V8 is shown in real Chrome by
    ``tests/test_e2e_connectivity_read.py``; this pins the text, and the double
    answers only a script carrying exactly it."""
    from stealth_chrome_devtools_mcp.embedded import page_natives

    squashed = fakes_elements._squash(page_natives.NATIVE_JS)
    # the whole chain is walked, an owner is remembered, none is returned early
    assert "letfound;" in squashed
    assert "found=own.get!==undefined?own.get:own.value;" in squashed
    assert squashed.endswith("returnfound;};")
    assert "returnown" not in squashed
    # the last prototype is never read
    stop = "if(Object.getPrototypeOf(proto)===null)break;"
    assert stop in squashed
    assert squashed.index(stop) < squashed.index("Object.getOwnPropertyDescriptor")
    # and the walk still starts one step up from the node
    assert "letproto=Object.getPrototypeOf(obj);" in squashed
    assert fakes_elements._CONNECTIVITY_SHAPE.count(squashed) == 1


_CENSUS_SPELLINGS = [
    "Object.getOwnPropertyDescriptor(Node.prototype, 'x').get",
    "Element . prototype . matches",
    "Document['prototype'].elementFromPoint",
    "r instanceof ShadowRoot",
    "function (e) { return e.isConnected }",
    'e["isConnected"]',
    "`isConnected`",
    # Review finding 3: spellings the first census missed.
    "elem['is' + 'Connected']",
    'elem["is"+"Conn"+"ected"]',
    'elem[`is${"Connected"}`]',
    "Node?.prototype",
    "Node?.['prototype']",
    "Reflect.get(Node,'prototype')",
    "const {prototype} = Element",
    "const { prototype: p } = ShadowRoot",
    "x instanceof window.ShadowRoot",
    "x instanceof globalThis.Node",
    "HTMLInputElement.prototype",
    "SVGElement.prototype",
    "Text.prototype",
    "DocumentFragment.prototype",
    "Node.ELEMENT_NODE",
    "window['Node'].prototype",
    "window.Node.prototype",
    "EventTarget.prototype",
]


@pytest.mark.parametrize("spelling", _CENSUS_SPELLINGS)
async def test_the_census_patterns_catch_every_spelling(spelling):
    assert _census_hits(spelling), spelling


@pytest.mark.parametrize(
    "legitimate",
    [
        "Object.getPrototypeOf(obj)",
        "document.elementFromPoint(x, y)",
        "ElementInfo.prototype",
        "Array.prototype.slice.call(list)",
        "const nodeName = native(elem, 'nodeName');",
        "nodeType.call(root) === 11",
        "'ELEMENT_NODE'",
    ],
)
async def test_the_census_flags_nothing_legitimate(legitimate):
    assert _census_hits(legitimate) == []


async def test_the_census_reads_split_and_formatted_strings(tmp_path):
    module = tmp_path / "m.py"
    module.write_text(
        '"""docstring: isConnected"""\n'
        "a = 'e.is' + 'Connected'\n"
        "b = f\"{x}.isConn\" 'ected'\n"
        "def f():\n"
        '    "also a docstring Node.prototype"\n'
        "    return 'plain'\n",
        encoding="utf-8",
    )
    texts = [text for _, text in _string_constants(module)]
    assert any(_IS_CONNECTED.search(t) for t in texts)
    assert sum(1 for t in texts if _IS_CONNECTED.search(t)) >= 2
    assert not any("docstring" in t for t in texts)


async def test_the_double_speaks_the_products_connectivity_vocabulary():
    """The double carries its own copy of the tokens and of the fragment (it is
    an independent witness); it answers a script only if that script carries
    the copy, so the copy has to be the product's text."""
    assert (YES, NO, THREW) == (
        click_target.CONNECTED_YES,
        click_target.CONNECTED_NO,
        click_target.CONNECTED_THREW,
    )
    assert (YES, NO, THREW) == (
        fakes_elements.CONNECTED_YES,
        fakes_elements.CONNECTED_NO,
        fakes_elements.CONNECTED_THREW,
    )
    assert (
        fakes_elements._squash(click_target.CONNECTIVITY_JS)
        == fakes_elements._CONNECTIVITY_SHAPE
    )


# ---------------------------------------------------------------------------
# (f) a connectivity read that FAILED is not an answer about the node (Codex)
# ---------------------------------------------------------------------------
#
# ``Element.apply`` hands back ``result[0].value`` and drops ``exceptionDetails``:
# a getter that does ``throw true`` arrives as a plain ``True``, and one that
# throws an Error as ``None``. Only the script's own tokens are answers, so a
# read that threw can neither complete the check nor mean the node left.

UNREADABLE = "could not read whether the element is still in its document"


class _Answers(FakeClickTarget):
    """A node whose connectivity script answers whatever it is told to."""

    def __init__(self, answer) -> None:
        super().__init__(tag="li", element_id="odd", text="odd")
        self._answer = answer

    async def apply(self, js_function, *args, **kwargs):
        self.apply_calls.append(js_function)
        return self._answer


@pytest.mark.parametrize("answer", _NOT_AN_ANSWER, ids=_NOT_AN_ANSWER_IDS)
async def test_a_connectivity_answer_that_is_no_token_is_a_read_error(answer):
    """The refresh neither completes (``True``) nor classifies a departure
    (``False``): it raises, and every tool that reads through it reports it."""
    with pytest.raises(ToolError, match=UNREADABLE):
        await refresh_element(FakeTab(), _Answers(answer))

    with pytest.raises(ToolError) as state:
        await DOMHandler.get_element_state(_TabOf(_Answers(answer)), SELECTOR)
    assert UNREADABLE in str(state.value)
    assert "left the page" not in str(state.value)

    for visible_only in (True, False):
        with pytest.raises(ToolError) as listing:
            await DOMHandler.query_elements(
                _TabOf(_Answers(answer)), "li", visible_only=visible_only
            )
        assert "element 0 of 'li'" in str(listing.value), visible_only
        assert UNREADABLE in str(listing.value), visible_only


@pytest.mark.parametrize("disconnected", [True, False], ids=["removed", "connected"])
async def test_a_getter_that_throws_is_a_read_error_whether_or_not_the_node_left(
    disconnected,
):
    """Codex findings 1 and 2. ``Node.prototype.isConnected`` replaced by a getter
    that throws: on a REMOVED node the thrown ``true`` completed the check and
    the node listed again; on a CONNECTED node the thrown Error read as a
    departure and the node was dropped. Both are a read that failed."""
    broken = FakeClickTarget(
        tag="li",
        element_id="broken",
        text="broken",
        getter_throws=True,
        disconnected=disconnected,
    )
    live = _item("live")

    with pytest.raises(ToolError, match=UNREADABLE):
        await refresh_element(FakeTab(), broken)
    for visible_only in (True, False):
        with pytest.raises(ToolError) as listing:
            await DOMHandler.query_elements(
                _TabOf(broken, live), "li", visible_only=visible_only
            )
        assert UNREADABLE in str(listing.value), visible_only
    with pytest.raises(ToolError) as state:
        await DOMHandler.get_element_state(_TabOf(broken), SELECTOR)
    assert UNREADABLE in str(state.value)
    assert "left the page" not in str(state.value)


async def test_a_read_error_in_a_listing_is_not_logged_as_a_dropped_element(
    backend_log,
):
    broken = FakeClickTarget(tag="li", element_id="broken", getter_throws=True)

    with pytest.raises(ToolError):
        await DOMHandler.query_elements(_TabOf(broken), "li", visible_only=False)

    assert [
        r
        for r in backend_log()
        if "left the document after resolution" in r.getMessage()
    ] == []


@pytest.mark.parametrize(
    "mutant",
    [
        "(elem) => {\n    __FRAGMENT__\n"
        "    const ignored = connectivity(elem);\n    return true;\n}",
        "(elem) => {\n    return true;\n}",
        "(elem) => {\n    __FRAGMENT__\n    return connectivity(elem) !== null;\n}",
    ],
    ids=["codex-mutation", "no-read-at-all", "returns-a-boolean-of-it"],
)
@pytest.mark.parametrize("disconnected", [True, False], ids=["removed", "connected"])
async def test_a_connectivity_script_that_stops_returning_its_token_is_a_read_error(
    monkeypatch, mutant, disconnected
):
    """Codex finding 3, its exact mutation: ``CONNECTED_JS`` changed to compute
    the read and ``return true``. The double answers only a script that returns
    the fragment's own token, so the mutant answers nothing and the refresh
    fails -- for a removed node and a connected one alike."""
    monkeypatch.setattr(
        click_target,
        "CONNECTED_JS",
        mutant.replace("__FRAGMENT__", click_target.CONNECTIVITY_JS),
    )
    node = _item("node", disconnected=disconnected)

    with pytest.raises(ToolError, match=UNREADABLE):
        await refresh_element(FakeTab(), node)


async def test_a_visibility_read_whose_connectivity_threw_is_a_read_error():
    """The window after the refresh: the page replaces the getter with one that
    throws, and the node is also gone. The visibility read hands back the token
    it read, and a failed read is neither "not visible" nor a silent skip: the
    listing fails, saying which element (review finding 2)."""

    class _BreaksAfterTheRefresh(FakeClickTarget):
        async def apply(self, js_function, *args, **kwargs):
            answer = await super().apply(js_function, *args, **kwargs)
            if "getComputedStyle" not in js_function:
                self.getter_throws = True
                self.disconnected = True
            return answer

    node = _BreaksAfterTheRefresh(tag="li", element_id="node", text="node")

    with pytest.raises(ToolError, match=r"element 0 of 'li': could not read whether"):
        await DOMHandler.query_elements(_TabOf(node), "li", visible_only=True)

    # It got as far as the visibility read: the refresh did not drop it first.
    assert element_resolution.VISIBLE_JS in node.apply_calls


async def test_a_cdp_failure_during_the_connectivity_read_keeps_its_old_meaning():
    """Not the page's answer, so not a token: Chrome's own departure reply is
    still a departure, and any other protocol failure still propagates."""

    class _Fails(FakeClickTarget):
        def __init__(self, error: ProtocolException) -> None:
            super().__init__(tag="li")
            self._error = error

        async def apply(self, js_function, *args, **kwargs):
            raise self._error

    gone = _Fails(left_the_document(DETACHED_EP))
    assert await refresh_element(FakeTab(), gone) is False
    with pytest.raises(ProtocolException, match="Some unrelated CDP failure"):
        await refresh_element(
            FakeTab(),
            _Fails(ProtocolException({"message": "Some unrelated CDP failure"})),
        )


async def test_a_click_on_a_node_whose_connectivity_cannot_be_read_sends_nothing():
    target = FakeClickTarget(getter_throws=True)
    tab = _same_node(target)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    assert "could not read whether '#target' is still in its document" in str(
        caught.value
    )
    assert len(tab.select_calls) == 1
    assert target.landings == [] and "click" not in target.calls
    assert "synthetic" not in target.calls


async def test_the_synthetic_script_clicks_only_on_the_yes_token():
    """Finding 2 (T-10284 review): a connectivity read that failed is answered
    with the script's own ``unread`` token, not the ``threw`` one the page's
    click handler gives. RED on 1c7761e: the message said the page's click
    handler "threw ... it may have reached the page" although ``click.call``
    never ran."""
    ledger = click_target.InputSent()
    unreadable = _hidden(getter_throws=True)

    with pytest.raises(ToolError) as caught:
        await click_target.synthetic_click(unreadable, SELECTOR, ledger)

    message = str(caught.value)
    assert message == (
        "could not read whether '#target' is still in its document; "
        "nothing was clicked, and it was not repeated"
    )
    assert "click handler" not in message and "reached the page" not in message
    assert "click" not in unreadable.calls
    # A read that failed proves nothing about whether the page took a click (its
    # own getter ran), so the ledger is set and the attempt is never re-run (the
    # unchanged rule).
    assert ledger.sent is True


async def test_the_synthetic_script_has_four_answers_of_its_own():
    answers = {
        click_target.SYNTHETIC_DETACHED,
        click_target.SYNTHETIC_CLICKED,
        click_target.SYNTHETIC_THREW,
        click_target.SYNTHETIC_UNREAD,
    }
    assert len(answers) == 4
    for answer in answers:
        assert f'"{answer}"' in click_target.SYNTHETIC_JS, answer
    unread = f'if (state !== "{click_target.CONNECTED_YES}") return "{click_target.SYNTHETIC_UNREAD}";'
    assert unread in click_target.SYNTHETIC_JS


class _UnreadableAtTheSyntheticClick(FakeClickTarget):
    """Readable for the aim, unreadable for the synthetic script: the page's
    getter starts throwing in the window between the two reads."""

    async def apply(self, js_function, *args, **kwargs):
        if js_function == click_target.SYNTHETIC_JS:
            self.getter_throws = True
        return await super().apply(js_function, *args, **kwargs)


async def test_click_element_whose_synthetic_read_fails_says_nothing_was_clicked():
    target = _UnreadableAtTheSyntheticClick(
        rendered=False, mouse_click_error=RuntimeError("could not find position")
    )
    spare = FakeClickTarget()
    tab = _ReplacingTab(target, spare)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.click_element(tab, SELECTOR)

    message = str(caught.value)
    assert "could not read whether '#target' is still in its document" in message
    assert "nothing was clicked" in message
    assert "click handler" not in message
    # Not re-resolved and not repeated: the attempt is spent.
    assert len(tab.select_calls) == 1
    assert "click" not in target.calls and spare.landings == []


async def test_a_click_handler_that_throws_is_still_the_threw_answer():
    """The other half of finding 2: the page's own ``click()`` throwing keeps
    the ``threw`` token and its wording."""
    target = _hidden(click_throws="boom")
    with pytest.raises(ToolError) as caught:
        await click_target.synthetic_click(target, SELECTOR, click_target.InputSent())

    assert "click handler" in str(caught.value)
    assert "may have reached the page" in str(caught.value)


async def test_a_removed_form_that_shadows_is_connected_is_dropped_from_a_listing():
    """A ``<form><input name=isConnected>`` the page removed: read as
    ``elem.isConnected`` it is the control, a truthy object, and the removed
    form would list as connected. The native getter says false."""
    removed = FakeClickTarget(
        tag="form",
        element_id="removed",
        form_controls=("isConnected",),
        disconnected=True,
    )
    live = FakeClickTarget(
        tag="form", element_id="live", form_controls=("isConnected",)
    )

    for visible_only in (True, False):
        results = await DOMHandler.query_elements(
            _TabOf(removed, live), "form", visible_only=visible_only
        )
        assert _ids(results) == ["live"], visible_only


async def test_a_removed_form_that_shadows_is_connected_has_left_the_page():
    removed = FakeClickTarget(
        tag="form", form_controls=("isConnected",), disconnected=True
    )
    live = FakeClickTarget(tag="form", form_controls=("isConnected",))

    assert await refresh_element(FakeTab(), removed) is False
    assert await refresh_element(FakeTab(), live) is True
    with pytest.raises(ToolError, match="left the page after it was resolved"):
        await DOMHandler.get_element_state(_TabOf(removed), SELECTOR)


# ---------------------------------------------------------------------------
# (g) a page that declares its own global named ``Node`` (T-10284 review)
# ---------------------------------------------------------------------------
#
# ``class Node {}`` / ``function Node(){}`` / ``var Node = 1`` at the top level
# of a page (a linked-list demo) replaces the ``Node`` global. A script that
# read ``Node.prototype`` then threw on EVERY element: the connectivity read
# answered ``threw`` and ``query_elements`` / ``get_element_state`` always
# raised where 2.1.53 answered, and ``click_element``'s natives threw uncaught.
# ``page_declares_node`` makes the double model that page: a script that still
# reads ``Node.prototype`` gets the failure, one that walks the node's own
# prototype chain does not.


def _on_node_page(element_id: str = "node", **kwargs) -> FakeClickTarget:
    return FakeClickTarget(
        tag="li",
        element_id=element_id,
        text=element_id,
        page_declares_node=True,
        **kwargs,
    )


@pytest.mark.parametrize("visible_only", [True, False])
async def test_query_elements_on_a_page_declaring_node_lists_as_on_any_page(
    visible_only,
):
    """RED on 1c7761e: ``ToolError: element 0 of 'li': could not read whether
    the element is still in its document`` for every element of the page."""
    live = _on_node_page("live")
    removed = _on_node_page("removed", disconnected=True)

    results = await DOMHandler.query_elements(
        _TabOf(removed, live), "li", visible_only=visible_only
    )

    assert _ids(results) == ["live"]
    assert results[0].is_visible is True


async def test_get_element_state_on_a_page_declaring_node_answers_as_on_any_page():
    state = await DOMHandler.get_element_state(_TabOf(_on_node_page("live")), SELECTOR)

    assert state["attributes"] == {"id": "live"}
    assert state["is_visible"] is True
    with pytest.raises(ToolError, match="left the page after it was resolved"):
        await DOMHandler.get_element_state(
            _TabOf(_on_node_page("gone", disconnected=True)), SELECTOR
        )


async def test_click_element_on_a_page_declaring_node_clicks_once():
    """RED on 1c7761e: ``NATIVES_JS`` read ``Node.prototype`` outside any
    ``try`` -- the aim threw and the click never happened."""
    target = _on_node_page("button")
    tab = _ReplacingTab(target)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert result["dispatch"] == "coordinate"
    assert len(_presses(target)) == 1


async def test_click_element_on_a_page_declaring_node_still_re_resolves_a_removed_node():
    removed = _on_node_page("gone", disconnected=True)
    fresh = _on_node_page("fresh")
    tab = _ReplacingTab(removed, fresh)

    result = await DOMHandler.click_element(tab, SELECTOR)

    assert result["dispatch"] == "coordinate"
    assert removed.landings == [] and len(_presses(fresh)) == 1
    assert len(tab.select_calls) == 2


async def test_the_synthetic_click_on_a_page_declaring_node_clicks_once():
    hidden = _on_node_page(
        "hidden",
        rendered=False,
        mouse_click_error=RuntimeError("could not find position"),
    )

    result = await DOMHandler.click_element(_ReplacingTab(hidden), SELECTOR)

    assert result["dispatch"] == "synthetic"
    assert hidden.calls.count("click") == 1


async def test_the_double_fails_a_script_that_still_reads_the_node_global():
    """The double's own contract (finding 1): under ``page_declares_node`` a
    script that reads ``Node.prototype`` inside the connectivity ``try`` answers
    ``threw``, one that reads it anywhere else throws uncaught (``None``), and
    the shipped scripts, which read neither, are answered as on any page."""
    page = _on_node_page("x")
    regressed = click_target.CONNECTED_JS.replace(
        "native(node, 'isConnected')",
        "Object.getOwnPropertyDescriptor(Node.prototype, 'isConnected').get",
    )
    assert regressed != click_target.CONNECTED_JS
    assert await page.apply(regressed) == THREW
    outside = "(elem) => { return Node.prototype.getRootNode.call(elem); }"
    assert await page.apply(outside) is None
    assert await page.apply(click_target.CONNECTED_JS) == YES
    # ... and only a page that declares Node is affected.
    assert await FakeClickTarget().apply(regressed) is None


@pytest.mark.parametrize("on_node_page", [False, True], ids=["plain", "declares-node"])
async def test_wait_for_element_visible_is_not_satisfied_by_a_removed_node(
    on_node_page,
):
    """Finding 4. ``wait_for_element(visible=True)`` kept its own style-only
    script, a second visibility check: a node the page removed but still
    references has an empty computed style, so it was "visible" and the wait
    returned True. RED on 1c7761e: ``True``."""
    removed = FakeClickTarget(
        tag="li",
        element_id="removed",
        disconnected=True,
        page_declares_node=on_node_page,
    )

    waited = await DOMHandler.wait_for_element(
        _TabOf(removed), "li", timeout=100, visible=True
    )

    assert waited is False
    assert removed.apply_calls == [element_resolution.VISIBLE_JS]


@pytest.mark.parametrize("on_node_page", [False, True], ids=["plain", "declares-node"])
async def test_wait_for_element_visible_returns_for_a_connected_node(on_node_page):
    live = FakeClickTarget(tag="li", element_id="live", page_declares_node=on_node_page)

    assert await DOMHandler.wait_for_element(
        _TabOf(live), "li", timeout=100, visible=True
    )
    assert live.apply_calls == [element_resolution.VISIBLE_JS]


@pytest.mark.parametrize(
    "answer",
    [THREW, None, "stealth.connected:maybe", 3],
    ids=["threw-token", "none", "other-string", "number"],
)
async def test_wait_for_element_raises_for_a_visibility_read_that_failed(answer):
    """Review finding 2. ``VISIBLE_JS`` answered ``false`` for a failed read, so
    ``wait_for_element(visible=True)`` waited out its timeout and said ``False``
    where 2.1.53 (style-only script) said ``True``. A failed read is never a
    departure and never a silent answer: it raises, as every other tool does."""
    node = _Answers(answer)

    with pytest.raises(ToolError, match="could not read whether 'li' is"):
        await DOMHandler.wait_for_element(
            _TabOf(node), "li", timeout=5000, visible=True
        )

    assert node.apply_calls == [element_resolution.VISIBLE_JS]


async def test_wait_for_element_raises_at_once_for_a_getter_that_throws():
    """The same through the double's own model of a page whose getter throws:
    one read, no polling to the timeout."""
    node = FakeClickTarget(tag="li", element_id="odd", getter_throws=True)

    with pytest.raises(ToolError, match="could not read whether"):
        await DOMHandler.wait_for_element(
            _TabOf(node), "li", timeout=5000, visible=True
        )

    assert node.apply_calls == [element_resolution.VISIBLE_JS]


async def test_the_read_only_scripts_are_sent_without_a_user_gesture():
    """Review finding 5, MEASURED in real Chrome (``test_e2e_connectivity_read``):
    ``Element.apply`` sends ``userGesture=True`` and one connectivity read of one
    node gave the page user activation. The connectivity, visibility and aim reads
    go through ``page_natives.read`` (``userGesture`` false); the synthetic click,
    which clicks, keeps ``Element.apply``."""
    node = _item("node")

    await refresh_element(FakeTab(), node)
    await DOMHandler.query_elements(_TabOf(node), "li", visible_only=True)
    await DOMHandler.wait_for_element(_TabOf(node), "li", timeout=100, visible=True)
    await click_target.aim(node, "li")

    assert node.read_gestures == [False] * 5
    assert node.apply_calls == [
        click_target.CONNECTED_JS,
        click_target.CONNECTED_JS,
        element_resolution.VISIBLE_JS,
        element_resolution.VISIBLE_JS,
        click_target.AIM_JS,
    ]
    await click_target.synthetic_click(node, "li", click_target.InputSent())
    assert node.read_gestures == [False] * 5, "the click is not a read"
    assert node.apply_calls[-1] == click_target.SYNTHETIC_JS


async def test_a_read_reaches_the_page_through_the_librarys_own_commands():
    """The double answers ``DOM.resolveNode`` and ``Runtime.callFunctionOn`` built
    by nodriver's own generators, and the pair they return is what the product
    unpacks: a value, ``None`` for a thrown Error, a thrown primitive as itself."""
    from stealth_chrome_devtools_mcp.embedded import page_natives

    for answer in (YES, "x", 0, False, None, {"a": [1]}):
        assert await page_natives.read(_Answers(answer), "(elem) => 1") == answer


async def test_wait_for_element_without_visible_asks_the_page_nothing():
    removed = FakeClickTarget(tag="li", element_id="removed", disconnected=True)

    assert await DOMHandler.wait_for_element(
        _TabOf(removed), "li", timeout=100, visible=False
    )
    assert removed.apply_calls == []


async def test_no_second_visibility_script_is_left_in_dom_handler():
    """``element_resolution.VISIBLE_JS`` is the only script of the two modules
    that tests ``getComputedStyle`` for visibility; ``dom_handler`` has none."""
    handler = Path(dom_handler.__file__).read_text(encoding="utf-8")
    resolution = Path(element_resolution.__file__).read_text(encoding="utf-8")
    assert handler.count("getComputedStyle") == 0
    assert resolution.count("getComputedStyle") == 1
    assert "VISIBLE_JS" not in handler.replace("node_is_visible", "")
