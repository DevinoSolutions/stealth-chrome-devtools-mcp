"""Selector resolution that survives CDP document-node invalidation.

nodriver resolves a selector in two *non-atomic* CDP round-trips:
``DOM.getDocument`` returns the document root nodeId, then
``DOM.querySelector(root, selector)`` uses it (``Tab.select`` / ``Tab.find`` and
the raw ``DOM.querySelectorAll`` path both do this). When a
``DOM.documentUpdated`` fires between the two -- which any DOM mutation triggers
(a click that starts a fetch, a dynamic re-render, a clone target inside live
content) -- CDP invalidates the just-fetched nodeId and the second call raises
``ProtocolException: Could not find node with given id [code: -32000]``.

That error's defined meaning is "the nodeId is stale; re-fetch the document".
The correct handling is to re-resolve against a fresh document, which nodriver
does on the next call (it re-issues ``DOM.getDocument``). This module is the one
place selectors resolve through, so every selector-driven tool inherits the
recovery instead of surfacing an intermittent -32000 to callers -- and to real
users -- under DOM churn.

nodriver has a second, unrelated race with the same "retry clears it" shape, so
it recovers here too. ``Tab.select_all`` awaits the tab, and ``Tab.wait``
registers page-event handlers (``FrameStoppedLoading`` and friends) then drops
them in a ``finally`` via ``Connection.remove_handler``, whose cleanup is a bare
``del self.handlers[evt_dom]``. That delete removes the whole key rather than
just this handler, so when two ``wait``s overlap on one tab the second one's
cleanup finds the key gone and raises ``KeyError(<cdp event class>)``.

The recovery is keyed to those two exact signals and bounded: a genuinely
absent selector still surfaces as the normal not-found/timeout after the final
attempt, and any other ``ProtocolException`` -- or any ``KeyError`` not naming a
``nodriver.cdp`` event class -- propagates unchanged.

``recoverable_race`` is the one home for "is this exception one of those two".
Paths that resolve no selector ask it too rather than re-listing the signals:
``browser_manager.navigate`` reaches the handler-cleanup ``KeyError`` through
``Tab.get`` (which awaits the tab) and consults this function to decide whether
its own single stale-tab retry applies (F-824).

One document at a time: why resolving is serialised per tab (F-884)
-------------------------------------------------------------------
Retrying is the right handling for a race the page LOST. It is the wrong
handling for a race this module STARTS, and every resolution below starts one:
``DOM.getDocument`` does not merely read the document, it **resets this CDP
session's node-id bindings**. Measured on Chrome 152 -- a session's own
re-fetch kills the ids it just handed out, while a *second* connection to the
same target leaves them untouched, so the binding table is per CDP session,
which is per nodriver ``Connection``/``Tab`` object. Two overlapping
resolutions on one tab therefore always lose: A fetches the document, B fetches
it and resets the table, A's query uses an id that no longer exists.

That much the retry above could absorb. What it cannot absorb is nodriver's
handling of the resulting error. ``Tab.query_selector`` / ``query_selector_all``
answer a ``ProtocolException`` by sending ``DOM.disable()`` *before* re-raising,
and that send is itself a round trip that fails -- with ``-32000 "DOM agent
hasn't been enabled"`` -- once a sibling has already disabled the agent. The
failure REPLACES the stale-node error, so ``recoverable_race`` never sees the
marker it classifies on and the caller gets a bare -32000 instead of a retry.
Measured at three concurrent ``wait_for_element`` calls on one tab: every round
raised it, and at five concurrent, three of five did.

So the fix is not a wider marker list -- it is to stop generating the race.
``_document_lock`` gives each tab one ``asyncio.Lock``, held around exactly
ONE thing: nodriver's single-shot ``getDocument`` + query pair
(``Tab.query_selector`` / ``query_selector_all`` / ``find_element_by_text`` /
``find_elements_by_text``). The lock's scope is the tab OBJECT because the
state it guards is, and it is taken by every function here, so no
selector-resolving path can opt out.

The WAIT is this module's, and it happens outside the lock
-----------------------------------------------------------
nodriver's ``select``/``find``/``select_all``/``xpath`` bundle a poll loop into
the same call as the query. The first cut of this fix held the lock around
those, and the review measured what that costs through the real handlers: a
``wait_for_element`` with a ONE-second caller timeout held the tab's lock for
10.5 s (nodriver's default wait), a sibling ``query_elements`` that answered in
0.25 s answered in 10.56 s, and a waiter for ``#late`` starved the concurrent
click that would have CREATED ``#late`` -- ``True @ 1.08 s`` became
``False @ 10.71 s``, a wrong answer rather than a slow one. Four concurrent
absent-selector resolutions blew the tool's own 30 s ``_with_cdp_timeout``.

So the polling is done here, in ``_wait_for``: one locked single-shot query,
then -- with the lock released -- a ``_POLL_SECONDS`` sleep, bounded by the
caller's own deadline, and again. A concurrent action lands between polls; a
waiter never holds the tab hostage; a caller's ``timeout`` means what it says.
The defaults and the interval are nodriver's own, so a caller that passed no
timeout waits exactly as long as it did before. ``timeout=0`` is one query.

One default, both languages. nodriver waits 10 s in ``select``/``find``/
``select_all`` and 2.5 s in ``xpath``, for no stated reason; this module
advertises ONE contract for CSS and XPath (see the detection contract below)
and now spends ONE budget for both. The cost is named rather than hidden: a
genuinely absent XPath takes 10 s to answer "not found" where it used to take
2.5 s. A caller that wants less passes ``timeout``.

What that still costs, and what it does not:

* the lock is held for one CDP round trip pair at a time, so concurrent
  resolutions interleave at that granularity; a genuinely slow single query
  (a huge document) still delays a sibling by its own duration;
* it does NOT make a node id safe to hold across calls. ``query_selector_all``
  hands back raw ids and its caller owns them once the lock is released -- the
  cloner engine holds one across five further CDP calls, and a sibling
  resolution between any two of them still invalidates it. That exposure is
  named in ``audit/stage2/finding_F884_concurrent_dom_queries.md`` §6 and is
  not this lock's job;
* it is not re-entrant, and nothing here needs it to be: no function in this
  module calls another, so a resolution never nests inside a held lock.

CSS or XPath: the detection contract (F-831)
--------------------------------------------
Every selector-taking tool advertises "CSS selector or XPath", so choosing
between the two languages is part of resolving a selector and lives here, not
in a tool body. ``xpath_expression`` is THE one place that choice is made; it is
purely syntactic (it never inspects the page) and therefore deterministic:

* an explicit ``xpath=`` prefix (case-insensitive) is XPath, and the prefix is
  stripped before dispatch -- ``xpath=./tr[1]`` is how a caller asks for a form
  the unprefixed rules below deliberately do not claim;
* otherwise a selector whose first non-space character is ``/`` or ``(`` is
  XPath: ``//a``, ``/html/body``, ``(//div)[1]``. No CSS selector may begin with
  either character, so nothing is taken from CSS -- and ``//`` (all the deleted
  ``query_elements`` branch ever accepted) still lands here;
* everything else is CSS: ``#id``, ``.cls``, ``div > a``, ``a[href]``. Note
  ``.`` stays CSS -- ``.foo`` is a class selector far more often than a relative
  XPath, which is what the explicit prefix exists for.

XPath resolves through nodriver's ``Tab.xpath`` (``Element`` results) or CDP's
``DOM.performSearch``/``getSearchResults`` (raw node ids), both inside the very
same ``_resolve_with_recovery`` the CSS paths use -- one retry loop, one
classifier, both languages.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import weakref
from typing import TYPE_CHECKING, TypeVar

from nodriver import cdp
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nodriver import Element, Tab
    from nodriver.cdp.dom import NodeId

_T = TypeVar("_T")

# Exact CDP message fragments for a query the document invalidated underneath.
# Matched on message TEXT and never on the numeric code: nodriver only fills
# ``ProtocolException.code`` from the CDP error object, and the same failure
# arrives both ways -- STEALTH-CHROME-DEVTOOLS-MCP-23 carries -32000 while
# STEALTH-CHROME-DEVTOOLS-MCP-3F carries no code at all (F-828).
_STALE_NODE_MARKERS = (
    # The nodeId fetched by DOM.getDocument no longer exists when
    # DOM.querySelector[All] uses it.
    "Could not find node with given id",
    # Blink's blanket reply when the query reached the renderer and failed
    # THERE (``ServerError("DOM Error while querying")``) -- what a document
    # swapped out mid-query surfaces as. A re-resolve against a fresh document
    # is the same correct handling; a query that fails for a permanent reason
    # simply fails again and surfaces unchanged after the bounded retries.
    "DOM Error while querying",
)

# Package of the CDP event classes nodriver keys its handler table by. A KeyError
# carrying one of these classes is nodriver's own bookkeeping race, never a
# lookup miss in our code or a caller's.
_CDP_EVENT_PACKAGE = "nodriver.cdp"

# A DOM.documentUpdated burst settles within a few frames, so a small bounded
# number of re-resolves clears the race; past that the selector is treated as
# genuinely unresolvable and the stale-node error is surfaced to the caller.
_MAX_RESOLVES = 3
_SETTLE_SECONDS = 0.05

# The wait this module owns (F-884). Both numbers are nodriver's own, so a
# caller that passed no timeout waits exactly as long as it did when nodriver
# did the polling: ``Tab.select``/``find``/``select_all`` default to 10 s and
# sleep 0.5 s between tries. They are NOT env knobs -- a caller that wants a
# different bound passes ``timeout``.
_DEFAULT_WAIT_SECONDS = 10.0
_POLL_SECONDS = 0.5


# --- One document at a time, per tab (F-884) ---------------------------------
# Keyed by ``id(tab)`` and not by the tab itself because nodriver's
# ``Connection`` defines ``__eq__`` and no ``__hash__``, which makes every
# ``Tab`` unhashable -- it cannot key a dict, weak or otherwise. The finalizer
# is what makes the id safe: the entry is dropped when the tab is collected, so
# a recycled id can never be handed a dead tab's lock.
_DOCUMENT_LOCKS: dict[int, asyncio.Lock] = {}


def _document_lock(tab: Tab) -> asyncio.Lock:
    """THE one lock serialising ``DOM.getDocument`` + its query on ``tab``.

    See the module docstring for why the scope is the tab object: the node-id
    bindings this guards are per CDP session, and a session is per ``Tab``.
    """
    key = id(tab)
    lock = _DOCUMENT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _DOCUMENT_LOCKS[key] = lock
        weakref.finalize(tab, _DOCUMENT_LOCKS.pop, key, None)
    return lock


# --- The XPath detection contract -------------------------------------------
# Explicit opt-in, honoured case-insensitively and stripped before dispatch.
_XPATH_PREFIX = "xpath="
# Characters no CSS selector may begin with, and every unprefixed XPath does:
# ``/`` opens an absolute or descendant path (``/html/body``, ``//a``) and ``(``
# opens a grouped one (``(//div)[1]``).
_XPATH_LEADING_CHARS = ("/", "(")


def xpath_expression(selector: str) -> str | None:
    """The XPath in ``selector``, or ``None`` when ``selector`` is CSS.

    THE one place that question is answered — see the module docstring for the
    contract. Deterministic and purely syntactic: it never touches the page.
    """
    candidate = selector.strip()
    if candidate[: len(_XPATH_PREFIX)].lower() == _XPATH_PREFIX:
        expression = candidate[len(_XPATH_PREFIX) :].strip()
        if not expression:
            raise ToolError(f"Selector {selector!r} carries no XPath expression")
        return expression
    if candidate[:1] in _XPATH_LEADING_CHARS:
        return candidate
    return None


def is_xpath(selector: str) -> bool:
    """Whether ``selector`` is an XPath under :func:`xpath_expression`'s contract."""
    return xpath_expression(selector) is not None


def _is_stale_node_error(exc: ProtocolException) -> bool:
    message = str(exc)
    return any(marker in message for marker in _STALE_NODE_MARKERS)


def _raced_cdp_event(exc: KeyError) -> str | None:
    """Name of the CDP event class in a nodriver handler-cleanup ``KeyError``.

    ``None`` for every other ``KeyError`` — a missing dict key raised anywhere
    else is a real defect and must not be retried or reworded.
    """
    if not exc.args:
        return None
    key = exc.args[0]
    if not isinstance(key, type):
        return None
    module = getattr(key, "__module__", "") or ""
    if module != _CDP_EVENT_PACKAGE and not module.startswith(f"{_CDP_EVENT_PACKAGE}."):
        return None
    return key.__name__


def recoverable_race(exc: BaseException) -> str | None:
    """Describe ``exc`` if it is one of the two known nodriver races, else ``None``.

    THE one place that question is answered. It takes any exception (not just the
    two types this module catches) because the same races surface on paths that
    resolve no selector at all: ``Tab.get`` awaits the tab, so
    ``browser_manager.navigate`` hits the identical handler-cleanup ``KeyError``
    and asks this function rather than listing the signals a second time (F-824).
    """
    if isinstance(exc, ProtocolException):
        if _is_stale_node_error(exc):
            return "document node invalidated mid-resolve"
        return None
    if isinstance(exc, KeyError):
        event = _raced_cdp_event(exc)
        if event is not None:
            return f"transient nodriver event-handler race ({event})"
    return None


async def _resolve_with_recovery(
    tab: Tab, what: str, resolve: Callable[[], Awaitable[_T]]
) -> _T:
    """Run ``resolve`` under ``tab``'s document lock, re-running it on either race.

    ``resolve`` must build a *fresh* awaitable on each call so the retry lands on
    a freshly fetched document nodeId, and must be a SINGLE-SHOT query — the
    waiting is :func:`_wait_for`'s and happens outside the lock (F-884).

    The lock is taken per ATTEMPT rather than around the whole loop, so the
    settle sleep below never holds it — a tab whose document is churning must
    still let a sibling in between two of its own tries.
    """
    attempt = 0
    while True:
        try:
            async with _document_lock(tab):
                return await resolve()
        except (ProtocolException, KeyError) as exc:
            race = recoverable_race(exc)
            if race is None:
                raise
            attempt += 1
            if attempt >= _MAX_RESOLVES:
                if isinstance(exc, KeyError):
                    # str(KeyError(cls)) is just the class repr, which tells a
                    # caller nothing — name the condition instead.
                    raise ToolError(
                        f"{race} while resolving {what}; re-resolved {attempt} "
                        f"times without clearing it"
                    ) from exc
                raise
            debug_logger.log_warning(
                "element_resolution",
                "_resolve_with_recovery",
                f"{race} ({what}); re-resolving on a fresh document "
                f"(attempt {attempt}/{_MAX_RESOLVES})",
                context={"what": what, "attempt": attempt},
            )
            # Let the documentUpdated burst (or the handler-table churn) settle
            # before re-resolving.
            await asyncio.sleep(_SETTLE_SECONDS * attempt)


async def _wait_for(
    tab: Tab,
    what: str,
    query: Callable[[], Awaitable[_T]],
    timeout: float | None,  # noqa: ASYNC109  plan_M4ph1
) -> _T:
    """Poll ``query`` until it finds something, or the caller's deadline passes.

    THE one home for "wait for this selector to appear" (F-884). ``query`` is a
    SINGLE-SHOT locked resolution; the sleep between tries happens with the lock
    RELEASED, which is the whole point — nodriver's ``select``/``find``/
    ``select_all`` bundle their own poll loop into the same call as the query,
    and holding the lock across that froze every other DOM operation on the tab
    for nodriver's default 10 s no matter what the caller asked for. Measured on
    real Chrome before this loop existed: a ``wait_for_element`` given a ONE
    second timeout held the lock 10.5 s, a sibling ``query_elements`` went from
    0.25 s to 10.56 s, and a waiter for an element a concurrent click would
    have created answered ``False`` because it starved that click.

    ``timeout`` is in seconds, ``None`` means nodriver's default (so a caller
    that passed nothing waits exactly as long as it always did) and ``0`` means
    exactly one query. A falsy answer (``None``, ``[]``) is "not yet"; the last
    one is returned when the deadline passes, so a genuine zero-match still
    surfaces as the empty answer callers already expect rather than a raise.
    """
    budget = _DEFAULT_WAIT_SECONDS if timeout is None else timeout
    deadline = _now() + budget
    while True:
        found = await _resolve_with_recovery(tab, what, query)
        if found:
            return found
        if _now() >= deadline:
            return found
        await _sleep(_POLL_SECONDS)


async def _xpath_matches(tab: Tab, expression: str) -> list[Element]:
    """ONE ``DOM.performSearch`` XPath query — no waiting, no ``dom.disable``.

    ``Tab.xpath`` is not used: it wraps ``find_all`` in its own poll loop (the
    thing :func:`_wait_for` now owns) and brackets the whole thing in
    ``dom.enable()``/``dom.disable()``, so under the lock it would still burn a
    caller's budget in one indivisible call. The pair below is what
    ``Tab.xpath`` is built on anyway, and :func:`_xpath_node_ids` has always
    used it directly.

    ``Tab.xpath`` is typed ``List[Optional[Element]]``; no caller of this module
    should have to defend against a ``None`` inside a match list, so the
    placeholders nodriver leaves for nodes it could not build are dropped here.
    """
    matches = await tab.find_elements_by_text(expression)
    return [match for match in matches if match is not None]


async def _xpath_node_ids(tab: Tab, expression: str) -> list[NodeId]:
    """``DOM.performSearch`` + ``DOM.getSearchResults`` — CDP's XPath node query.

    This is the same pair nodriver's own ``Tab.xpath`` is built on, used
    directly here because this path owes its caller raw node ids rather than
    ``Element`` wrappers.
    """
    await tab.send(cdp.dom.get_document())
    search_id, count = await tab.send(cdp.dom.perform_search(expression, True))
    try:
        if not count:
            return []
        return list(await tab.send(cdp.dom.get_search_results(search_id, 0, count)))
    finally:
        # A search-result set is per-document bookkeeping: once the document it
        # was taken against is gone, discarding it fails and there is nothing
        # left to leak. Never let that mask the query's own outcome.
        with contextlib.suppress(ProtocolException):
            await tab.send(cdp.dom.discard_search_results(search_id))


async def refresh_element(tab: Tab, element: Element | None) -> None:
    """``Element.update()`` under ``tab``'s document lock (F-884).

    ``update`` reads as "re-read this element" and is nothing of the kind: it
    sends ``DOM.getDocument`` and re-resolves the node against the answer, so
    it resets the very per-session node-id table every resolution above depends
    on. Called outside the lock it invalidates a concurrent resolution's node
    id exactly as a second resolution would — and ``query_elements`` called it
    once per returned element, so ONE listing of twenty elements reset the
    table twenty times while a sibling was mid-query. That is why this is the
    one home for it and why no caller may reach ``element.update()`` directly.

    Deliberately NOT inside ``_resolve_with_recovery``: a stale node here means
    the element the caller already holds is gone from the document, which is an
    answer about that element, not a race worth re-running — and a retry loop
    around a per-element call would triple the cost of every listing.

    Tolerates an ``element`` that has no ``update`` (a rediscovered target
    yields a raw ``Connection``-backed node, F-771) and a ``None``, so the two
    call sites are one line each rather than one line and a guard.
    """
    if element is None or not hasattr(element, "update"):
        return
    async with _document_lock(tab):
        await element.update()


async def resolve_element(
    tab: Tab,
    selector: str,
    timeout: float | None = None,  # noqa: ASYNC109  plan_M4ph1
) -> Element | None:
    """The first element matching ``selector``, waiting for it to appear.

    An XPath ``selector`` (see :func:`xpath_expression`) resolves through
    ``DOM.performSearch`` instead and yields its first match. ``timeout`` is in
    seconds for both; ``None`` keeps nodriver's 10 s default and ``0`` is a
    single query. Each try is one LOCKED single-shot query and the waiting
    happens between them with the lock released (F-884) — never
    ``tab.select``, whose bundled poll loop would hold the tab for its whole
    default no matter what the caller asked for.
    """
    expression = xpath_expression(selector)
    if expression is not None:

        async def _do_xpath() -> Element | None:
            matches = await _xpath_matches(tab, expression)
            return matches[0] if matches else None

        return await _wait_for(tab, f"xpath {expression!r}", _do_xpath, timeout)

    async def _do() -> Element | None:
        return await tab.query_selector(selector)

    return await _wait_for(tab, f"select {selector!r}", _do, timeout)


async def resolve_by_text(
    tab: Tab,
    text: str,
    best_match: bool = True,
    timeout: float | None = None,  # noqa: ASYNC109  plan_M4ph1
) -> Element | None:
    """The element containing ``text``, waiting for it to appear.

    ``tab.find_element_by_text`` is the single-shot form of ``tab.find``; the
    wait is :func:`_wait_for`'s, outside the lock (F-884).
    """

    async def _do() -> Element | None:
        return await tab.find_element_by_text(text, best_match)

    return await _wait_for(tab, f"find {text!r}", _do, timeout)


async def resolve_elements(tab: Tab, selector: str) -> list[Element]:
    """Every element matching ``selector``, waiting for the first to appear.

    The multi-``Element`` counterpart to :func:`resolve_element`: returns the
    full match list of live ``Element`` objects (with ``.attrs``/``.text_all``/
    ``.get_position()``), or an empty list on a genuine zero-match. A -32000
    stale-node race re-resolves against a fresh document; a persistent one
    surfaces after ``_MAX_RESOLVES`` exactly like the single-element path.

    ``tab.query_selector_all`` is the single-shot form of ``tab.select_all``,
    so the wait is :func:`_wait_for`'s and happens outside the lock (F-884).
    That also retires the second race this module used to recover from on this
    path: ``select_all`` reached nodriver's handler-cleanup ``KeyError`` only
    because it awaited the tab between its own tries, and it no longer does.
    An XPath ``selector`` resolves through ``DOM.performSearch``.
    """
    expression = xpath_expression(selector)
    if expression is not None:

        async def _do_xpath() -> list[Element]:
            return await _xpath_matches(tab, expression)

        return await _wait_for(tab, f"xpath {expression!r}", _do_xpath, None)

    async def _do() -> list[Element]:
        # nodriver answers a missing content_document with a bare ``return``,
        # so ``None`` is possible where the annotation says list.
        return await tab.query_selector_all(selector) or []

    return await _wait_for(tab, f"select_all {selector!r}", _do, None)


async def query_selector_all(tab: Tab, selector: str) -> list[NodeId]:
    """``DOM.getDocument`` + ``DOM.querySelectorAll`` with stale-document recovery.

    Returns the raw node-id list. Both CDP calls run inside the recovery, so a
    -32000 on either triggers a full fresh re-resolve. An XPath ``selector``
    runs ``DOM.performSearch`` + ``DOM.getSearchResults`` instead, under the
    same recovery.
    """
    expression = xpath_expression(selector)
    if expression is not None:

        async def _do_xpath() -> list[NodeId]:
            return await _xpath_node_ids(tab, expression)

        return await _resolve_with_recovery(tab, f"xpath {expression!r}", _do_xpath)

    async def _do() -> list[NodeId]:
        doc = await tab.send(cdp.dom.get_document())
        return await tab.send(cdp.dom.query_selector_all(doc.node_id, selector))

    return await _resolve_with_recovery(tab, f"query_selector_all {selector!r}", _do)


# --- The one timing seam (the ``scroll_position``/``scheduling_lag`` pattern) --
# Module functions, not imports at the call site, so a test can replace them
# without a running clock and without patching ``asyncio`` itself.


def _now() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)
