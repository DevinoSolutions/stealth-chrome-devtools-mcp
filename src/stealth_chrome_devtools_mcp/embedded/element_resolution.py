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

The recovery is keyed to the exact -32000 signal and bounded: a genuinely
absent selector still surfaces as the normal not-found/timeout after the final
attempt, and any other ``ProtocolException`` propagates unchanged.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

from nodriver import cdp
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nodriver import Element, Tab
    from nodriver.cdp.dom import NodeId

_T = TypeVar("_T")

# Exact CDP message fragment for a stale/invalidated document or node id
# (JSON-RPC code -32000). Matched on message text: nodriver surfaces the numeric
# code only inside the stringified exception.
_STALE_NODE_MARKER = "Could not find node with given id"

# A DOM.documentUpdated burst settles within a few frames, so a small bounded
# number of re-resolves clears the race; past that the selector is treated as
# genuinely unresolvable and the stale-node error is surfaced to the caller.
_MAX_RESOLVES = 3
_SETTLE_SECONDS = 0.05


def _is_stale_node_error(exc: ProtocolException) -> bool:
    return _STALE_NODE_MARKER in str(exc)


async def _resolve_with_recovery(what: str, resolve: Callable[[], Awaitable[_T]]) -> _T:
    """Run ``resolve``, re-running it on the -32000 stale-document race.

    ``resolve`` must build a *fresh* awaitable on each call so the retry lands on
    a freshly fetched document nodeId.
    """
    attempt = 0
    while True:
        try:
            return await resolve()
        except ProtocolException as exc:
            if not _is_stale_node_error(exc):
                raise
            attempt += 1
            if attempt >= _MAX_RESOLVES:
                raise
            debug_logger.log_warning(
                "element_resolution",
                "_resolve_with_recovery",
                f"document node invalidated mid-resolve ({what}); re-resolving on "
                f"a fresh document (attempt {attempt}/{_MAX_RESOLVES})",
                context={"what": what, "attempt": attempt},
            )
            # Let the documentUpdated burst settle before re-resolving.
            await asyncio.sleep(_SETTLE_SECONDS * attempt)


async def resolve_element(
    tab: Tab,
    selector: str,
    timeout: float | None = None,  # noqa: ASYNC109  plan_M4ph1
) -> Element | None:
    """``tab.select(selector, timeout=...)`` with stale-document recovery.

    ``timeout`` is in seconds (nodriver's unit); ``None`` uses nodriver's
    default.
    """

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


async def query_selector_all(tab: Tab, selector: str) -> list[NodeId]:
    """``DOM.getDocument`` + ``DOM.querySelectorAll`` with stale-document recovery.

    Returns the raw node-id list. Both CDP calls run inside the recovery, so a
    -32000 on either triggers a full fresh re-resolve.
    """

    async def _do() -> list[NodeId]:
        doc = await tab.send(cdp.dom.get_document())
        return await tab.send(cdp.dom.query_selector_all(doc.node_id, selector))

    return await _resolve_with_recovery(f"query_selector_all {selector!r}", _do)
