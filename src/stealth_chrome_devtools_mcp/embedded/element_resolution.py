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


async def _resolve_with_recovery(what: str, resolve: Callable[[], Awaitable[_T]]) -> _T:
    """Run ``resolve``, re-running it on either known nodriver resolve race.

    ``resolve`` must build a *fresh* awaitable on each call so the retry lands on
    a freshly fetched document nodeId.
    """
    attempt = 0
    while True:
        try:
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


async def _xpath_matches(
    tab: Tab,
    expression: str,
    timeout: float | None = None,  # noqa: ASYNC109  plan_M4ph1
) -> list[Element]:
    """One ``tab.xpath`` round trip, with nodriver's ``None`` placeholders dropped.

    ``Tab.xpath`` is typed ``List[Optional[Element]]``; no caller of this module
    should have to defend against a ``None`` inside a match list.
    """
    matches = (
        await tab.xpath(expression)
        if timeout is None
        else await tab.xpath(expression, timeout=timeout)
    )
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


async def resolve_element(
    tab: Tab,
    selector: str,
    timeout: float | None = None,  # noqa: ASYNC109  plan_M4ph1
) -> Element | None:
    """``tab.select(selector, timeout=...)`` with stale-document recovery.

    An XPath ``selector`` (see :func:`xpath_expression`) resolves through
    ``tab.xpath`` instead and yields its first match. ``timeout`` is in seconds
    (nodriver's unit) for both; ``None`` uses nodriver's default.
    """
    expression = xpath_expression(selector)
    if expression is not None:

        async def _do_xpath() -> Element | None:
            matches = await _xpath_matches(tab, expression, timeout)
            return matches[0] if matches else None

        return await _resolve_with_recovery(f"xpath {expression!r}", _do_xpath)

    async def _do() -> Element | None:
        if timeout is None:
            return await tab.select(selector)
        return await tab.select(selector, timeout=timeout)

    return await _resolve_with_recovery(f"select {selector!r}", _do)


async def resolve_by_text(
    tab: Tab,
    text: str,
    best_match: bool = True,
    timeout: float | None = None,  # noqa: ASYNC109  plan_M4ph1
) -> Element | None:
    """``tab.find(text, ...)`` with stale-document recovery."""

    async def _do() -> Element | None:
        if timeout is None:
            return await tab.find(text, best_match=best_match)
        return await tab.find(text, best_match=best_match, timeout=timeout)

    return await _resolve_with_recovery(f"find {text!r}", _do)


async def resolve_elements(tab: Tab, selector: str) -> list[Element]:
    """``tab.select_all(selector)`` with stale-document recovery.

    The multi-``Element`` counterpart to :func:`resolve_element`: returns the
    full match list of live ``Element`` objects (with ``.attrs``/``.text_all``/
    ``.get_position()``), or an empty list on a genuine zero-match. A -32000
    stale-node race re-resolves against a fresh document; a persistent one
    surfaces after ``_MAX_RESOLVES`` exactly like the single-element path. This
    is also the path that hits nodriver's handler-cleanup ``KeyError``, since
    ``select_all`` awaits the tab between attempts.

    An XPath ``selector`` resolves through ``tab.xpath`` under the same recovery.
    """
    expression = xpath_expression(selector)
    if expression is not None:

        async def _do_xpath() -> list[Element]:
            return await _xpath_matches(tab, expression)

        return await _resolve_with_recovery(f"xpath {expression!r}", _do_xpath)

    async def _do() -> list[Element]:
        return await tab.select_all(selector)

    return await _resolve_with_recovery(f"select_all {selector!r}", _do)


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

        return await _resolve_with_recovery(f"xpath {expression!r}", _do_xpath)

    async def _do() -> list[NodeId]:
        doc = await tab.send(cdp.dom.get_document())
        return await tab.send(cdp.dom.query_selector_all(doc.node_id, selector))

    return await _resolve_with_recovery(f"query_selector_all {selector!r}", _do)
