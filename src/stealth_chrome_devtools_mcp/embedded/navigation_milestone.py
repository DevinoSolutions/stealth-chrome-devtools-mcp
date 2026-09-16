"""THE one home for "this navigation has reached the milestone the caller asked
for" (F-881).

``navigate(wait_until=...)`` promises to answer at ``load``, at
``domcontentloaded`` or at ``networkidle``. Until this module it kept none of
them: the wait was ``tab.wait(cdp.page.LoadEventFired)``, and nodriver 0.47's
``Tab.wait(t)`` takes a DURATION — ``if not t: t = 0.5; await ...`` — so a truthy
argument of any kind skips the wait outright (measured: 0.01 to 0.06 ms). The
``tab.get(url)`` in front of it is ``send(Page.navigate)`` plus ``Tab.wait()``,
which returns on the FIRST navigation event — the commit — or, when nothing has
enabled the ``Page`` domain on the tab (the shipped product's case), after a flat
0.5 s of silence. So ``document.title`` was read at commit plus whatever the
scheduler allowed, and on a loaded Windows runner that was before the parser had
reached ``<title>`` (two full gates: ``assert '' == 'Alpha'``).

**What the milestone is keyed on.** ``Page.lifecycleEvent`` carries the
``loaderId`` of the document it belongs to, and ``Page.navigate`` answers with
the ``loaderId`` of the navigation it started. Three measured facts (finding
§2d) shape :func:`navigate`:

* the response and the new document's events are NOT ordered — ``DOMContentLoaded``
  arrived 0.3 ms before the response and ``load`` 0.3 ms after — so the
  listener is armed BEFORE the send and remembers every ``(loaderId, name)`` it
  saw, and the answer is "seen already" or "wait for it", never "arm and hope";
* a navigation Chrome could not perform (``errorText`` set) still commits its
  error page under the SAME ``loaderId`` and fires ``load`` for it, so the wait
  ends and F-802/F-833's ``chrome-error://`` detector reads the landing as
  before;
* a same-document navigation answers ``loaderId: null`` and fires nothing —
  there is nothing to wait for and the tool returns at the response.

An older document's ``load`` (a page still finishing when the new navigation
was sent) arrives with the older ``loaderId`` and does not count.

``networkidle`` is F-787's fixed sleep, moved here byte-for-byte so
``wait_until`` has one home; it now starts at the committed document (``init``)
rather than at ``tab.get``'s 0.5 s, and it is still not a quiescence wait —
that finding stays open, and ``Page.lifecycleEvent``'s own ``networkIdle`` is
one table row away when it is taken up.

The listener is removed in a ``finally`` through nodriver's own
``remove_handler``, which is ``del self.handlers[evt]`` (the whole event type,
F-824's race): a concurrent ``Tab.wait()`` on the same tab may have deleted it
first, and that ``KeyError`` is tolerated here rather than surfaced as a failed
navigation. No deadline is enforced here — the caller wraps the whole thing in
its navigation budget, exactly as it wrapped ``tab.get``; *budget_seconds* is
handed in only so F-787's sleep can be clipped to it as it always was.

**What a cancelled attempt still tells the caller.** The caller's ``wait_for``
cancels this coroutine on timeout, so nothing can be RETURNED from a timed-out
attempt — :class:`Progress` is the one field it writes on the way, ``accepted``:
``Page.navigate`` answered, i.e. Chrome took the navigation on THIS tab. That is
the line between the two timeouts ``BrowserManager.navigate`` used to treat as
one. Before it, the tab may be stale or racing and the one-shot recovery on a
fresh tab (F-824's budget) is the right answer. After it, the page is Chrome's —
committed and slow, committed and never reaching ``load``, or a download
(``net::ERR_ABORTED``, nothing commits) — and replacing the tab would throw away
a page that exists, or trigger the download twice, and spend a second full
budget doing it. So a timeout after acceptance is REPORTED, never retried.

A leaf: ``nodriver`` and ``tool_errors`` only; the tab arrives as an argument.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nodriver import cdp

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module a leaf
    from nodriver import Tab

#: ``wait_until`` -> the ``Page.lifecycleEvent`` name that marks it for the
#: navigation's own ``loaderId``. ``init`` is the commit.
MILESTONES: dict[str, str] = {
    "load": "load",
    "domcontentloaded": "DOMContentLoaded",
    "networkidle": "init",
}

#: F-787: ``networkidle`` is this fixed sleep after commit, not a network wait.
NETWORKIDLE_SLEEP_SECONDS = 2.0


@dataclass
class Progress:
    """What one attempt got as far as, readable after the caller cancelled it.

    ``accepted``: ``Page.navigate`` answered — Chrome took the navigation on this
    tab, so a timeout from here on is the page's, not a stale tab's.
    """

    accepted: bool = False


def require(wait_until: str) -> str:
    """The lifecycle event name *wait_until* stands for, or ``ToolError``.

    Called by the tool BEFORE its retry loop, so a caller's typo costs no CDP
    send and no "attempt failed" log line — it used to mean ``load`` silently,
    which then meant nothing at all.
    """
    milestone = MILESTONES.get(wait_until)
    if milestone is None:
        accepted = ", ".join(repr(name) for name in MILESTONES)
        raise ToolError(
            f"wait_until={wait_until!r} is not a navigation milestone; "
            f"accepted values are {accepted}."
        )
    return milestone


async def navigate(
    tab: Tab,
    url: str,
    wait_until: str,
    budget_seconds: float,
    progress: Progress | None = None,
) -> None:
    """Send ``Page.navigate`` for *url* and return once THAT navigation has
    reached the milestone *wait_until* names, marking *progress* on the way.
    """
    milestone = require(wait_until)
    progress = progress if progress is not None else Progress()
    loop = asyncio.get_running_loop()
    started = loop.time()
    seen: set[tuple[str, str]] = set()
    wanted: tuple[str, str] | None = None
    reached = loop.create_future()

    def on_lifecycle(
        event: cdp.page.LifecycleEvent, _connection: object = None
    ) -> None:
        key = (str(event.loader_id), event.name)
        seen.add(key)
        if key == wanted and not reached.done():
            reached.set_result(None)

    tab.add_handler(cdp.page.LifecycleEvent, on_lifecycle)
    try:
        # nodriver re-sends Page.enable ahead of this on EVERY navigation: once
        # the listener below is removed, its next send forgets `cdp.page` from
        # `enabled_domains` (connection.py's _register_handlers), so the domain
        # reads as new each time. Both commands are idempotent, ~1 ms together,
        # and that beats tracking either flag per tab.
        await tab.send(cdp.page.set_lifecycle_events_enabled(enabled=True))
        _frame_id, loader_id, _error_text = await tab.send(cdp.page.navigate(url))
        progress.accepted = True
        if loader_id is None:
            return  # same-document: nothing will fire (measured)
        wanted = (str(loader_id), milestone)
        if wanted not in seen:
            await reached
    finally:
        with contextlib.suppress(KeyError):
            tab.remove_handler(cdp.page.LifecycleEvent, on_lifecycle)

    if wait_until == "networkidle":
        remaining = budget_seconds - (loop.time() - started)
        await asyncio.sleep(max(0.0, min(NETWORKIDLE_SLEEP_SECONDS, remaining)))
