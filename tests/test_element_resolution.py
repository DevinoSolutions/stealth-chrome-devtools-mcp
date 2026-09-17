"""Pins for element_resolution: recovery from the CDP -32000 stale-document race.

nodriver resolves selectors in two non-atomic CDP calls; a DOM.documentUpdated
between them invalidates the document nodeId and query_selector raises
ProtocolException "Could not find node with given id [code: -32000]". The
resolution helpers must: re-resolve on that specific signal (bounded), pass
success straight through, and propagate any other error unchanged. These are
hermetic (a fake Tab) so they run in the fast unit lane, not the browser lane.
"""

import asyncio
import gc
import inspect

import pytest
from nodriver import cdp
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded import element_resolution
from stealth_chrome_devtools_mcp.embedded.element_resolution import (
    _MAX_RESOLVES,
    query_selector_all,
    refresh_element,
    resolve_by_text,
    resolve_element,
    resolve_elements,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    # The recovery backoff is real; zero it so the unit lane stays fast while the
    # real asyncio.sleep(0) code path still runs.
    monkeypatch.setattr(element_resolution, "_SETTLE_SECONDS", 0.0)
    # Same for F-884's wait: zero the default budget so a pin whose fake answers
    # a falsy value (a genuine zero-match) makes exactly ONE query, as it did
    # when nodriver owned the polling. A pin that wants the loop asks for a
    # budget explicitly.
    monkeypatch.setattr(element_resolution, "_DEFAULT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(element_resolution, "_POLL_SECONDS", 0.0)


def _stale():
    # Mirrors the real CDP error: str() contains the -32000 marker text.
    return ProtocolException(
        {"message": "Could not find node with given id", "code": -32000}
    )


def _other():
    return ProtocolException({"message": "Some unrelated CDP failure", "code": -32601})


def _dom_error(code=None):
    # F-828 / STEALTH-CHROME-DEVTOOLS-MCP-3F: Blink's OTHER reply for a query it
    # could not complete. `DOM.querySelector` reached the renderer and the query
    # itself failed there, so Chromium answers with its blanket
    # ``ServerError("DOM Error while querying")`` instead of the node-id text.
    # 3F arrives with no ``code`` key at all (bare ``str()``); the same message
    # in STEALTH-…-23 carries -32000 — so the MESSAGE is the only stable signal.
    error = {"message": "DOM Error while querying"}
    if code is not None:
        error["code"] = code
    return ProtocolException(error)


def _handler_race():
    # Mirrors nodriver's own bookkeeping race: Tab.wait() registers the
    # page-event handlers and its finally-block does a bare
    # ``del self.handlers[evt_dom]``; a concurrent wait() on the same tab has
    # already dropped the key, so the KeyError's arg is the CDP event CLASS.
    return KeyError(cdp.page.FrameStoppedLoading)


def _pop(effects):
    effect = effects.pop(0)
    if isinstance(effect, Exception):
        raise effect
    return effect


class _FakeTab:
    def __init__(self, *, select=None, select_all=None, find=None, send=None):
        self._select = list(select or [])
        self._select_all = list(select_all or [])
        self._find = list(find or [])
        self._send = list(send or [])
        self.select_calls = 0
        self.select_all_calls = 0
        self.find_calls = 0
        self.send_calls = 0

    # The SINGLE-SHOT nodriver surfaces. Since F-884 this module never calls
    # ``select``/``select_all``/``find``/``xpath``: those bundle a poll loop
    # into the query, and holding the document lock across one froze the tab
    # for nodriver's whole default. A fake that still offered them would let a
    # regression back in silently, so it offers only what production may call.
    async def query_selector(self, selector):
        self.select_calls += 1
        return _pop(self._select)

    async def query_selector_all(self, selector):
        self.select_all_calls += 1
        return _pop(self._select_all)

    async def find_element_by_text(self, text, best_match=True):
        self.find_calls += 1
        return _pop(self._find)

    async def find_elements_by_text(self, expression):
        self.select_all_calls += 1
        return _pop(self._select_all)

    async def send(self, _cmd):
        self.send_calls += 1
        return _pop(self._send)


class _Doc:
    node_id = 1


@pytest.mark.asyncio
async def test_resolve_element_recovers_from_transient_stale_node():
    sentinel = object()
    tab = _FakeTab(select=[_stale(), sentinel])
    assert await resolve_element(tab, "#btn") is sentinel
    assert tab.select_calls == 2  # failed once, recovered on a fresh document


@pytest.mark.asyncio
async def test_resolve_element_passes_success_through_without_retry():
    sentinel = object()
    tab = _FakeTab(select=[sentinel])
    assert await resolve_element(tab, "#btn") is sentinel
    assert tab.select_calls == 1


@pytest.mark.asyncio
async def test_resolve_element_propagates_non_stale_error_immediately():
    tab = _FakeTab(select=[_other()])
    with pytest.raises(ProtocolException, match="unrelated"):
        await resolve_element(tab, "#btn")
    assert tab.select_calls == 1  # not a stale-node error -> no retry


@pytest.mark.asyncio
async def test_resolve_element_is_bounded_when_stale_persists():
    tab = _FakeTab(select=[_stale() for _ in range(_MAX_RESOLVES)])
    with pytest.raises(ProtocolException, match="Could not find node with given id"):
        await resolve_element(tab, "#btn")
    assert tab.select_calls == _MAX_RESOLVES  # bounded; genuine failure surfaces


@pytest.mark.asyncio
async def test_the_timeout_is_spent_here_and_never_handed_to_nodriver():
    """``timeout`` used to be forwarded into ``tab.select``; now it bounds OUR loop.

    That is the F-884 fix, not an incidental change: nodriver spends a timeout
    by polling INSIDE one call, and this module holds the tab's document lock
    for the duration of that call. The budget has to be spent where the lock is
    not held, so the single-shot query nodriver is left with takes no timeout at
    all — and a fake that accepted one would hide a regression to the old shape.
    """
    tab = _FakeTab(select=[object()])
    signature = inspect.signature(tab.query_selector)
    assert "timeout" not in signature.parameters

    elapsed = []

    async def _observe(seconds):
        elapsed.append(seconds)

    clock = iter([0.0, 0.0, 9.0])
    misses = _FakeTab(select=[None, None])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(element_resolution, "_now", lambda: next(clock))
        patch.setattr(element_resolution, "_sleep", _observe)
        patch.setattr(element_resolution, "_POLL_SECONDS", 0.5)
        assert await resolve_element(misses, "#btn", timeout=2.5) is None

    # One sleep, at the module's own interval, then the 9.0 read passes 2.5.
    assert elapsed == [0.5]
    assert misses.select_calls == 2


@pytest.mark.asyncio
async def test_resolve_by_text_recovers_from_stale_node():
    sentinel = object()
    tab = _FakeTab(find=[_stale(), sentinel])
    assert await resolve_by_text(tab, "Submit") is sentinel
    assert tab.find_calls == 2


@pytest.mark.asyncio
async def test_resolve_elements_recovers_from_nodriver_handler_race():
    # STEALTH-CHROME-DEVTOOLS-MCP-2R: select_all -> `await self` -> Tab.wait(),
    # whose handler cleanup raises KeyError(<cdp event class>) under concurrency.
    # Transient by construction: the same call succeeds on the retry.
    nodes = [object()]
    tab = _FakeTab(select_all=[_handler_race(), nodes])
    assert await resolve_elements(tab, ".row") == nodes
    assert tab.select_all_calls == 2


@pytest.mark.asyncio
async def test_resolve_elements_raises_readable_tool_error_when_race_persists():
    # STEALTH-CHROME-DEVTOOLS-MCP-2S: the bare KeyError used to escape as
    # "<class 'nodriver.cdp.page.FrameStoppedLoading'>" — useless to a caller.
    tab = _FakeTab(select_all=[_handler_race() for _ in range(_MAX_RESOLVES)])
    with pytest.raises(ToolError) as excinfo:
        await resolve_elements(tab, ".row")
    message = str(excinfo.value)
    assert "FrameStoppedLoading" in message
    assert "nodriver" in message
    assert ".row" in message
    assert "<class " not in message  # never the raw class repr
    assert tab.select_all_calls == _MAX_RESOLVES  # bounded, same as the -32000 path


@pytest.mark.asyncio
async def test_resolve_element_recovers_from_nodriver_handler_race():
    # The single-element path inherits the same recovery (one shared structure).
    sentinel = object()
    tab = _FakeTab(select=[_handler_race(), sentinel])
    assert await resolve_element(tab, "#btn") is sentinel
    assert tab.select_calls == 2


@pytest.mark.asyncio
async def test_unrelated_key_error_is_neither_retried_nor_converted():
    # A KeyError from anywhere else is a real defect: it must not be silently
    # retried, nor dressed up as a nodriver race.
    tab = _FakeTab(select_all=[KeyError("foo")])
    with pytest.raises(KeyError, match="foo"):
        await resolve_elements(tab, ".row")
    assert tab.select_all_calls == 1


@pytest.mark.asyncio
async def test_key_error_naming_a_non_nodriver_class_is_not_retried():
    # Scoped to nodriver.cdp classes: a KeyError whose arg happens to be some
    # other class is still a genuine failure.
    tab = _FakeTab(select_all=[KeyError(dict)])
    with pytest.raises(KeyError):
        await resolve_elements(tab, ".row")
    assert tab.select_all_calls == 1


def test_the_codeless_dom_error_is_the_shape_sentry_reports():
    """The library contract, measured: ``ProtocolException.__str__`` appends
    ``[code: N]`` only when the CDP error object carried one, so the same failure
    reaches the classifier as two different strings. A classifier that matched on
    the code would miss half of them; one that matches the message sees both."""
    assert str(_dom_error()) == "DOM Error while querying"
    assert "[code" not in str(_dom_error())
    assert str(_dom_error(code=-32000)) == "DOM Error while querying [code: -32000]"


@pytest.mark.asyncio
async def test_resolve_element_recovers_from_a_codeless_dom_error():
    # F-828: the exact 3F shape — no code, a message the -32000 marker misses.
    sentinel = object()
    tab = _FakeTab(select=[_dom_error(), sentinel])
    assert await resolve_element(tab, "#btn") is sentinel
    assert tab.select_calls == 2


@pytest.mark.asyncio
async def test_resolve_elements_recovers_from_a_dom_error_carrying_the_code():
    # The same message WITH -32000 (STEALTH-…-23, the select_all path).
    nodes = [object()]
    tab = _FakeTab(select_all=[_dom_error(code=-32000), nodes])
    assert await resolve_elements(tab, ".row") == nodes
    assert tab.select_all_calls == 2


@pytest.mark.asyncio
async def test_dom_error_recovery_is_bounded_exactly_like_the_node_id_one():
    tab = _FakeTab(select=[_dom_error() for _ in range(_MAX_RESOLVES)])
    with pytest.raises(ProtocolException, match="DOM Error while querying"):
        await resolve_element(tab, "#btn")
    assert tab.select_calls == _MAX_RESOLVES  # widened reach, unchanged bound


@pytest.mark.asyncio
async def test_an_unrelated_runtime_error_is_never_retried():
    # A genuinely fatal error is not a race: it must surface on the first try.
    tab = _FakeTab(select=[RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        await resolve_element(tab, "#btn")
    assert tab.select_calls == 1


@pytest.mark.asyncio
async def test_query_selector_all_recovers_from_stale_node():
    # Each attempt sends get_document then query_selector_all. First attempt:
    # get_document ok, query_selector_all stale -> re-resolve; second succeeds.
    nodes = [10, 11]
    tab = _FakeTab(send=[_Doc(), _stale(), _Doc(), nodes])
    assert await query_selector_all(tab, ".x") == nodes
    assert tab.send_calls == 4


# ---------------------------------------------------------------------------
# F-884: two resolutions on ONE tab are never in flight at the same time
# ---------------------------------------------------------------------------

_RESOLVED = object()


class _RacingTab:
    """A tab that answers an OVERLAPPING resolution the way Chrome 152 does.

    Faithful to the measured chain, not to a convenient stand-in. Every
    resolution is ``DOM.getDocument`` followed by a query using the node id it
    returned, and ``getDocument`` resets this CDP session's node-id bindings --
    so a second resolution entering while a first is mid-flight kills the
    first's id and its query raises "Could not find node with given id".
    nodriver answers THAT by sending ``DOM.disable()`` before re-raising, which
    itself fails once a sibling has already disabled the agent, and the
    resulting "DOM agent hasn't been enabled" REPLACES the stale-node text --
    which is why the error modelled here is the masked one and not the
    recoverable one ``_STALE_NODE_MARKERS`` would catch.

    It also records the high-water mark, so the pin can assert the property
    that actually matters (mutual exclusion) rather than merely the absence of
    a raise on one scheduling.
    """

    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0
        self.select_calls = 0

    async def query_selector(self, selector):
        self.select_calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Two yields: a resolution is two awaited CDP round trips, and a
            # single yield would give a sibling nowhere to interleave.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if self.in_flight > 1:
                raise ProtocolException(
                    {"message": "DOM agent hasn't been enabled", "code": -32000}
                )
            return _RESOLVED
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_concurrent_resolutions_on_one_tab_are_serialised():
    """Three concurrent resolutions on one tab: none races, all succeed.

    Before F-884 this raised ``-32000 "DOM agent hasn't been enabled"`` -- and
    that text is deliberately NOT in ``_STALE_NODE_MARKERS``, so the recovery
    loop could not absorb it. The fix removes the race rather than widening the
    marker list, which is what ``max_in_flight`` pins: retrying three colliding
    resolutions until they happened to miss each other would also make the
    raise go away, and would not be this fix.
    """
    tab = _RacingTab()

    results = await asyncio.gather(*(resolve_element(tab, "#btn") for _ in range(3)))

    assert results == [_RESOLVED] * 3
    assert tab.max_in_flight == 1, "two resolutions overlapped on one tab"
    assert tab.select_calls == 3, "no attempt needed a retry"


@pytest.mark.asyncio
async def test_the_lock_is_per_tab_so_two_tabs_still_resolve_concurrently():
    """The cure must not serialise the whole browser.

    The node-id table Chrome resets is per CDP session and a session is per
    tab, so two tabs share no state and must share no lock. One global lock
    would pass the pin above and quietly cost every multi-tab caller.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingTab:
        async def query_selector(self, selector):
            started.set()
            await release.wait()
            return _RESOLVED

    slow, quick = _BlockingTab(), _FakeTab(select=[_RESOLVED])

    slow_call = asyncio.create_task(resolve_element(slow, "#slow"))
    await started.wait()

    # The other tab must answer while the first still holds its own lock.
    assert await resolve_element(quick, "#quick") is _RESOLVED

    release.set()
    assert await slow_call is _RESOLVED


@pytest.mark.asyncio
async def test_one_tab_gets_exactly_one_lock_and_loses_it_when_collected():
    """Same tab, same lock -- and no entry outlives the tab that needed it.

    The table is keyed by ``id(tab)`` because nodriver's ``Connection`` defines
    ``__eq__`` without ``__hash__``, which makes a ``Tab`` unhashable. That key
    is only safe because the finalizer drops the entry, so a recycled id can
    never inherit a dead tab's lock.
    """
    tab = _FakeTab()
    first = element_resolution._document_lock(tab)
    assert element_resolution._document_lock(tab) is first

    key = id(tab)
    assert key in element_resolution._DOCUMENT_LOCKS
    del tab, first
    gc.collect()
    assert key not in element_resolution._DOCUMENT_LOCKS


@pytest.mark.asyncio
async def test_the_settle_sleep_between_attempts_does_not_hold_the_lock():
    """A churning tab must still let a sibling in between two of its own tries.

    The lock is taken per ATTEMPT, not around the retry loop; holding it across
    the backoff would let one unlucky resolution block the tab for the whole
    bounded recovery.
    """
    held_during_sleep = None

    async def _observe(_seconds):
        nonlocal held_during_sleep
        held_during_sleep = element_resolution._document_lock(tab).locked()

    tab = _FakeTab(select=[_stale(), _RESOLVED])
    with pytest.MonkeyPatch.context() as patch:
        # The MODULE seam, not ``asyncio.sleep``: this module declares one
        # timing seam (``_now``/``_sleep``) and the backoff goes through it like
        # every other wait here, so a pin that patched ``asyncio`` would pass
        # whether or not that stayed true.
        patch.setattr(element_resolution, "_sleep", _observe)
        patch.setattr(element_resolution, "_SETTLE_SECONDS", 0.01)
        assert await resolve_element(tab, "#btn") is _RESOLVED

    assert held_during_sleep is False


@pytest.mark.asyncio
async def test_the_backoff_goes_through_this_modules_one_timing_seam():
    """Patching ``_sleep`` must be enough to stop the module from sleeping.

    The companion to the pin above, stated positively: the declared seam is the
    ONLY way this module waits, so a caller that replaces it sees every wait.
    """
    slept = []

    async def _record(seconds):
        slept.append(seconds)

    tab = _FakeTab(select=[_stale(), _RESOLVED])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(element_resolution, "_sleep", _record)
        patch.setattr(element_resolution, "_SETTLE_SECONDS", 0.25)
        assert await resolve_element(tab, "#btn") is _RESOLVED

    assert slept == [0.25], slept


@pytest.mark.asyncio
async def test_refresh_element_takes_the_same_lock_as_a_resolution():
    """``Element.update()`` is a ``DOM.getDocument`` and must not run unlocked.

    This is the site that made the first version of the fix insufficient: a
    lock inside the resolution helpers alone still lost 1 of 15 real-Chrome
    mixed resolutions, because ``dom_handler.query_elements`` reached
    ``elem.update()`` once per returned element. Nothing about the LOCK is
    visible from the resolution side, so the pin observes it from inside
    ``update`` itself.
    """
    locked_during_update = None

    class _Element:
        async def update(self):
            nonlocal locked_during_update
            locked_during_update = element_resolution._document_lock(tab).locked()

    tab = _FakeTab()
    await refresh_element(tab, _Element())

    assert locked_during_update is True


@pytest.mark.asyncio
async def test_a_waiting_resolution_does_not_hold_the_lock_between_tries():
    """The B1 blocker: a waiter must not freeze every other DOM call on the tab.

    The first cut of F-884 held the lock across ``tab.select``, whose poll loop
    is inside the same call, so a ``wait_for_element`` given a ONE second
    timeout held the tab for nodriver's 10 s default and a sibling
    ``query_elements`` went from 0.25 s to 10.56 s. The lock must be free while
    this module sleeps between tries.
    """
    observed = []

    async def _observe(_seconds):
        observed.append(element_resolution._document_lock(tab).locked())

    # Three misses then a hit, so the loop sleeps three times.
    tab = _FakeTab(select=[None, None, None, _RESOLVED])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(element_resolution, "_sleep", _observe)
        patch.setattr(element_resolution, "_DEFAULT_WAIT_SECONDS", 30.0)
        assert await resolve_element(tab, "#late") is _RESOLVED

    assert observed == [False, False, False], observed
    assert tab.select_calls == 4


@pytest.mark.asyncio
async def test_the_wait_is_bounded_by_the_callers_timeout_not_nodrivers():
    """``timeout`` means what it says, and ``timeout=0`` is exactly one query.

    ``wait_for_element`` owns its own poll loop and now passes ``timeout=0``;
    before F-884 it passed nothing, so each of its turns carried nodriver's
    10 s default *inside* the caller's budget.
    """
    single = _FakeTab(select=[None])
    assert await resolve_element(single, "#nope", timeout=0) is None
    assert single.select_calls == 1, "timeout=0 must not poll"

    clock = iter([0.0, 0.0, 1.0, 2.0, 99.0])
    waiting = _FakeTab(select=[None, None, None, None])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(element_resolution, "_now", lambda: next(clock))
        assert await resolve_element(waiting, "#nope", timeout=1.5) is None
    # Deadline is 1.5: the reads 1.0 keeps going, 2.0 stops.
    assert waiting.select_calls == 3


@pytest.mark.asyncio
async def test_resolve_element_never_calls_nodrivers_bundled_wait():
    """A fake that offers only single-shot surfaces is the pin (F-884).

    ``Tab.select``/``find``/``select_all``/``xpath`` each wrap a poll loop
    around the query; calling one under the document lock is the regression
    this whole change exists to prevent, so production must not reach them and
    an ``AttributeError`` here is the proof.
    """
    tab = _FakeTab(select=[_RESOLVED])
    assert not hasattr(tab, "select")
    assert not hasattr(tab, "xpath")
    assert await resolve_element(tab, "#btn") is _RESOLVED


@pytest.mark.asyncio
async def test_refresh_element_tolerates_a_node_that_cannot_be_updated():
    """A rediscovered target yields a node with no ``update`` (F-771).

    The two call sites carried a ``hasattr`` guard each; folding it in here is
    what lets them be one line, so the tolerance has to live in this one home.
    """
    tab = _FakeTab()
    await refresh_element(tab, None)
    await refresh_element(tab, object())


def _disabled_agent():
    # What Chrome answers a ``DOM.disable`` on a session whose DOM agent is not
    # enabled — measured on Chrome 152. nodriver sends that call as the LAST
    # statement of ``find_elements_by_text``, after the answer is built.
    return ProtocolException(
        {"message": "DOM agent hasn't been enabled", "code": -32000}
    )


@pytest.mark.asyncio
async def test_a_failed_trailing_disable_does_not_discard_a_finished_xpath_search():
    """F-884 F1: the XPath path still sends ``dom.disable`` and it can raise.

    ``Tab.xpath`` swallows that exact call in a ``finally``, commenting that it
    "sometimes raises"; resolving through ``find_elements_by_text`` directly
    loses the swallow, and there the call is nodriver's last statement — so an
    uncaught failure would replace a search that had ALREADY SUCCEEDED with a
    bare -32000, the masking shape this finding exists to remove. One repeat
    recovers it, because the search is an idempotent read whose own
    ``getDocument`` re-enables the agent.
    """
    found = object()
    tab = _FakeTab(select_all=[_disabled_agent(), [found]])

    assert await resolve_elements(tab, "//div") == [found]
    assert tab.select_all_calls == 2


@pytest.mark.asyncio
async def test_the_trailing_disable_tolerance_is_bounded_to_one_repeat():
    """A second failure is not a race any more, so it reaches the caller."""
    tab = _FakeTab(select_all=[_disabled_agent(), _disabled_agent()])

    with pytest.raises(ProtocolException, match="DOM agent"):
        await resolve_elements(tab, "//div")
    assert tab.select_all_calls == 2


def test_a_disabled_agent_error_is_never_a_recoverable_race():
    """The tolerance is ``_xpath_matches``', never ``_STALE_NODE_MARKERS``'.

    Widening the marker list is what this finding rejected: the disabled-agent
    error is the MASK, and it says nothing about whether the query raced, so
    re-resolving on it would restore the pre-fix behaviour by the other door.
    """
    assert element_resolution.recoverable_race(_disabled_agent()) is None


@pytest.mark.asyncio
async def test_a_disabled_agent_error_on_the_css_path_still_propagates():
    """The tolerance is scoped to the one call that can emit it, not the module."""
    tab = _FakeTab(select=[_disabled_agent()])

    with pytest.raises(ProtocolException, match="DOM agent"):
        await resolve_element(tab, "#btn")
    assert tab.select_calls == 1


@pytest.mark.asyncio
async def test_resolve_elements_spends_the_callers_timeout_like_its_siblings():
    """F-884 F5: the multi-element path honours a deadline too, in the one home.

    It could not before: the wait was ``select_all``'s, bundled into the query.
    Now it is :func:`_wait_for`'s, so the same value means the same thing here
    as on ``resolve_element`` — ``0`` is exactly one query, and a budget polls.
    """
    tab = _FakeTab(select_all=[[], []])
    assert await resolve_elements(tab, ".row", timeout=0) == []
    assert tab.select_all_calls == 1

    found = object()
    tab = _FakeTab(select_all=[[], [found]])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(element_resolution, "_DEFAULT_WAIT_SECONDS", 0.0)
        assert await resolve_elements(tab, ".row", timeout=30.0) == [found]
    assert tab.select_all_calls == 2
