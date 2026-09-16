"""THE one home for "this navigation has reached the milestone the caller asked
for" (F-881, F-882).

``navigate(wait_until=...)`` promises to answer at ``load``, at
``domcontentloaded`` or at ``networkidle``. Until F-881 it kept none of them:
the wait was ``tab.wait(cdp.page.LoadEventFired)``, and nodriver 0.47's
``Tab.wait(t)`` takes a DURATION — ``if not t: t = 0.5; await ...`` — so a truthy
argument of any kind skips the wait outright (measured: 0.01 to 0.06 ms). The
``tab.get(url)`` in front of it is ``send(Page.navigate)`` plus ``Tab.wait()``,
which returns on the FIRST navigation event — the commit — or, when nothing has
enabled the ``Page`` domain on the tab (the shipped product's case), after a flat
0.5 s of silence. So ``document.title`` was read at commit plus whatever the
scheduler allowed, and on a loaded Windows runner that was before the parser had
reached ``<title>`` (two full gates: ``assert '' == 'Alpha'``).

**What the milestone is keyed on: the FRAME's loader chain, not one loaderId.**
``Page.lifecycleEvent`` carries the ``frameId`` and the ``loaderId`` of the
document it belongs to, and ``Page.navigate`` answers with both for the
navigation it started. F-881 waited for the milestone under exactly the answered
``loaderId``, which is right until the document Chrome committed for it is
REPLACED before it gets there — a head-script ``location.replace``, a JS
challenge that re-navigates itself, a client-side redirect. The replacement
commits under a NEW ``loaderId``, fires its own ``load``, and the wait for ours
burned the caller's whole budget while the browser sat on a finished page
(F-882: three of ten ordinary sites, 30 s each). So this module follows the
CHAIN: the milestone is satisfied when the LATEST document committed in our
frame has reached it. Measured on Chrome 152 (finding F-882 §2):

* a real commit is ``Page.lifecycleEvent`` name ``init``; the superseding
  document's ``init`` landed 20.3-22.5 ms after ours across all four shapes;
* the response and the new document's events are NOT ordered — ``DOMContentLoaded``
  arrived 0.3 ms before the response and ``load`` 0.3 ms after — so the
  listener is armed BEFORE the send and every event is BUFFERED, and the answer
  is "seen already" or "wait for it", never "arm and hope";
* a **subframe**'s lifecycle events carry the SUBFRAME's ``frameId`` (measured:
  a same-origin iframe that replaced itself produced two loaders under
  ``frameId`` 907517DE…, parent 966836E7…, while the main frame committed once),
  so filtering on the frame ``Page.navigate`` returned is what keeps an iframe's
  loader out of the chain;
* ``Page.setLifecycleEventsEnabled(true)`` REPLAYS the current document's whole
  lifecycle — with the name ``commit`` where a live navigation says ``init``,
  under the loaderId of the document showing at that moment. It is sent BEFORE
  ``Page.navigate``, so the replay is always about the page we are LEAVING;
  keying commits on ``init`` alone is what makes it impossible to read as a
  supersession;
* a navigation Chrome could not perform (``errorText`` set) still commits its
  error page under the SAME ``loaderId`` and fires ``load`` for it, so the wait
  ends and F-802/F-833's ``chrome-error://`` detector reads the landing as
  before — **except** ``net::ERR_ABORTED``, below;
* a same-document navigation answers ``loaderId: null`` and fires nothing —
  there is nothing to wait for and the tool returns at the response.

A foreign ``init`` is admitted to the chain only AFTER ours has committed, so a
document that was already finishing when we sent (and an older document's
``load``, which carries an older loaderId and belongs to no chain member) can
never end this wait. Ours always commits: a non-aborted ``errorText`` commits an
error page under our own loaderId, and the one case that commits nothing is
answered rather than waited on.

**``net::ERR_ABORTED`` is bounded by a grace, never by the caller's budget.**
The abort has two measured meanings and they need different answers. A URL that
serves ``Content-Disposition: attachment`` answers ``Page.navigate`` in 9-13 ms
with ``errorText='net::ERR_ABORTED'``, commits nothing, fires nothing, and
leaves the tab exactly where it was. But the displayed page navigating ITSELF
away while our navigation is still pending aborts it too — and there the
replacement is the answer: over six runs its ``init`` landed 11.6-14.1 ms after
the response in four and BEFORE the response in the other two (the same
unordered delivery F-881 measured). So an abort seeds an EMPTY chain, the first
commit in that frame is adopted as its head, and only a grace that passes with
nothing in it is really a download — :data:`ABORTED_GRACE_SECONDS`, clipped to
whatever is left of the caller's budget so a small ``timeout`` still gets the
named answer rather than a bare cancellation.

``networkidle`` is F-787's fixed sleep, moved here byte-for-byte so
``wait_until`` has one home; it is keyed on the FIRST commit — its milestone IS
``init``, so the chain is satisfied the moment our own document commits and a
later supersession can no longer reach it. That is deliberate and unchanged by
F-882: ``networkidle`` is not a quiescence wait and never was, so making it
outlast a redirect would be a new promise rather than a kept one. F-787 stays
open, and ``Page.lifecycleEvent``'s own ``networkIdle`` is one table row away
when it is taken up.

The listener is removed in a ``finally`` through nodriver's own
``remove_handler``, which is ``del self.handlers[evt]`` (the whole event type,
F-824's race): a concurrent ``Tab.wait()`` on the same tab may have deleted it
first, and that ``KeyError`` is tolerated here rather than surfaced as a failed
navigation. No deadline is enforced here — the caller wraps the whole thing in
its navigation budget, exactly as it wrapped ``tab.get``; *budget_seconds* is
handed in only so F-787's sleep can be clipped to it as it always was.

**What a cancelled attempt still tells the caller.** The caller's ``wait_for``
cancels this coroutine on timeout, so nothing can be RETURNED from a timed-out
attempt — :class:`Progress` is what it writes on the way, and every field is a
fact the caller cannot otherwise have: ``accepted`` (``Page.navigate`` answered,
i.e. Chrome took the navigation on THIS tab), ``committed`` (our own document
reached ``init``) and ``superseded`` (how many later documents committed in the
frame while we waited). ``accepted`` is the line between the two timeouts
``BrowserManager.navigate`` used to treat as one. Before it, the tab may be
stale or racing and the one-shot recovery on a fresh tab (F-824's budget) is the
right answer. After it, the page is Chrome's — committed and slow, committed and
never reaching ``load``, or replaced again and again — and replacing the tab
would throw away a page that exists and spend a second full budget doing it. So
a timeout after acceptance is REPORTED, never retried; :meth:`Progress.describe`
is the one phrasing of that report, and it exists because the warning line it
feeds used to end in an empty ``TimeoutError`` message.

A leaf: ``nodriver`` and ``tool_errors`` only; the tab arrives as an argument.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nodriver import cdp

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module a leaf
    from nodriver import Tab

#: ``wait_until`` -> the ``Page.lifecycleEvent`` name that marks it for the
#: document the navigation is following. ``init`` is the commit.
MILESTONES: dict[str, str] = {
    "load": "load",
    "domcontentloaded": "DOMContentLoaded",
    "networkidle": "init",
}

#: F-787: ``networkidle`` is this fixed sleep after commit, not a network wait.
NETWORKIDLE_SLEEP_SECONDS = 2.0

#: The ``Page.lifecycleEvent`` name a document commits under. Never ``commit``,
#: which is what ``Page.setLifecycleEventsEnabled`` replays the CURRENT document
#: under (F-882 §2) — the page we are leaving, not one that replaced us.
COMMIT = "init"

#: The one ``errorText`` under which our own loader never commits (F-882 §2f).
ABORTED = "net::ERR_ABORTED"

#: How long an abort waits for the document that took its place before it is
#: reported as a download. Measured worst case 14.1 ms over six runs of the
#: pre-emption shape (and two of the six had already committed BEFORE the
#: response arrived); 1 s is ~70x that, and it is the whole cost a real download
#: pays — against the 30 s the caller's default budget used to cost it. Clipped
#: to HALF the remaining budget, so it can never outlive the caller's own
#: deadline and lose the named answer to a bare cancellation.
ABORTED_GRACE_SECONDS = 1.0


@dataclass
class Progress:
    """What one attempt got as far as, readable after the caller cancelled it.

    ``accepted``: ``Page.navigate`` answered — Chrome took the navigation on this
    tab, so a timeout from here on is the page's, not a stale tab's.
    ``committed``: our own document reached ``init``.
    ``superseded``: how many LATER documents committed in the same frame — the
    one number that separates "this page is slow" from "this page keeps
    replacing itself".
    """

    accepted: bool = False
    committed: bool = False
    superseded: int = 0

    def describe(self) -> str:
        """This attempt's facts as one clause, for the caller's warning line.

        Never the URL and never anything the page authored: the three fields
        are ours, and the caller already names the url it was given.
        """
        if not self.accepted:
            return "Chrome never answered Page.navigate"
        parts = ["accepted", "committed" if self.committed else "never committed"]
        if self.superseded:
            parts.append(f"superseded by {self.superseded} later document(s)")
        return ", ".join(parts)


@dataclass
class _Chain:
    """The documents that have committed in ONE frame since this navigation.

    ``own`` is the loaderId ``Page.navigate`` answered with, and ``None`` when it
    aborted and will never commit; each later entry is a document that replaced
    the one before it. :attr:`reached` asks only about ``loaders[-1]``, because
    that is the document the tab is actually showing — the whole point of F-882.
    """

    frame_id: str
    milestone: str
    progress: Progress
    own: str | None
    loaders: list[str] = field(default_factory=list)
    seen: set[tuple[str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.own is not None:
            self.loaders.append(self.own)

    def consider(self, frame_id: str, loader_id: str, name: str) -> None:
        """Fold one ``Page.lifecycleEvent`` into the chain."""
        if frame_id != self.frame_id:
            return  # a subframe's loader is never this navigation's (measured)
        # A document that is not ours committing in our frame supersedes ours
        # only AFTER ours had its turn (see the module docstring) — or, when
        # ours aborted and never will, it is simply what took our place.
        if (
            name == COMMIT
            and loader_id not in self.loaders
            and (self.progress.committed or self.own is None)
        ):
            self.loaders.append(loader_id)
            self.progress.superseded = len(self.loaders) - (self.own is not None)
        if loader_id not in self.loaders:
            return  # an older document's event, or the replay's
        self.seen.add((loader_id, name))
        if loader_id == self.own and name == COMMIT:
            self.progress.committed = True

    @property
    def reached(self) -> bool:
        return bool(self.loaders) and (self.loaders[-1], self.milestone) in self.seen


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


#: The landing read: ONE round trip for BOTH fields (F-882). Two ``tab.evaluate``
#: calls straddled the ``meta refresh`` shape on real Chrome and answered with
#: the FIRST document's ``url`` and the SECOND document's ``title`` — a record no
#: document ever had. It is ``JSON.stringify`` for the same reason
#: ``page_storage``'s read is (F-844/F-869): ``Tab.evaluate`` always sends deep
#: ``SerializationOptions`` and returns the value RAW, so a returned array would
#: arrive as BiDi ``RemoteValue`` nodes.
LANDING_JS = "JSON.stringify([location.href, document.title])"


async def landing(tab: Tab) -> tuple[str, str]:
    """Where this navigation ended up: ``(url, title)`` of ONE document.

    Raises rather than degrading: an unreadable landing would otherwise be
    reported as a page at ``""`` with no title, which is the shape F-802 closed.
    The message carries the answer's TYPE and LENGTH, never its content — a
    url can carry a session token.
    """
    answer = await tab.evaluate(LANDING_JS)
    if isinstance(answer, str):
        with contextlib.suppress(ValueError, TypeError):
            url, title = json.loads(answer)
            return str(url), str(title)
    raise ToolError(
        "The page did not answer the post-navigation read with the JSON it was "
        f"asked for (got {type(answer).__name__}, "
        f"{len(answer) if isinstance(answer, str) else 0} chars)."
    )


def _aborted_error(url: str) -> ToolError:
    """The one message for a navigation Chrome accepted and then abandoned."""
    return ToolError(
        f"Navigation to {url} was aborted by Chrome ({ABORTED}) and no document "
        "was committed: the URL served a download (Content-Disposition: "
        "attachment), an empty body the browser does not navigate to, or an "
        "external protocol handler. The tab is still showing the previous page."
    )


async def navigate(
    tab: Tab,
    url: str,
    wait_until: str,
    budget_seconds: float,
    progress: Progress | None = None,
) -> None:
    """Send ``Page.navigate`` for *url* and return once the LATEST document in
    that frame's loader chain has reached the milestone *wait_until* names,
    marking *progress* on the way.
    """
    milestone = require(wait_until)
    progress = progress if progress is not None else Progress()
    loop = asyncio.get_running_loop()
    started = loop.time()
    stream: list[tuple[str, str, str]] = []
    arrived = asyncio.Event()

    def on_lifecycle(
        event: cdp.page.LifecycleEvent, _connection: object = None
    ) -> None:
        stream.append((str(event.frame_id), str(event.loader_id), event.name))
        arrived.set()

    tab.add_handler(cdp.page.LifecycleEvent, on_lifecycle)
    try:
        # nodriver re-sends Page.enable ahead of this on EVERY navigation: once
        # the listener below is removed, its next send forgets `cdp.page` from
        # `enabled_domains` (connection.py's _register_handlers), so the domain
        # reads as new each time. Both commands are idempotent, ~1 ms together,
        # and that beats tracking either flag per tab.
        await tab.send(cdp.page.set_lifecycle_events_enabled(enabled=True))
        frame_id, loader_id, error_text = await tab.send(cdp.page.navigate(url))
        progress.accepted = True
        aborted = error_text == ABORTED
        if loader_id is None and not aborted:
            return  # same-document: nothing will fire (measured)
        chain = _Chain(
            str(frame_id),
            milestone,
            progress,
            None if aborted else str(loader_id),
        )
        # From HERE, not from the call: the abort arrives when the page
        # navigates, which is a second or more into a pending navigation, so a
        # grace anchored at `started` would already be spent on arrival and
        # would call every pre-empted navigation a download. And half of what is
        # LEFT, never all of it: a grace that expires at the caller's own
        # deadline loses the race to the enclosing wait_for, and the named
        # answer it exists to produce is replaced by a bare timeout.
        left_of_budget = max(0.0, budget_seconds - (loop.time() - started))
        grace = loop.time() + min(ABORTED_GRACE_SECONDS, left_of_budget / 2)
        cursor = 0
        while not chain.reached:
            # Cleared BEFORE the drain, so an event appended while we are
            # folding still wakes the next wait rather than being lost.
            arrived.clear()
            while cursor < len(stream) and not chain.reached:
                chain.consider(*stream[cursor])
                cursor += 1
            if chain.reached:
                break
            if aborted and not chain.loaders:
                left = grace - loop.time()
                if left <= 0:
                    raise _aborted_error(url)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(arrived.wait(), left)
            else:
                await arrived.wait()
    finally:
        with contextlib.suppress(KeyError):
            tab.remove_handler(cdp.page.LifecycleEvent, on_lifecycle)

    if wait_until == "networkidle":
        remaining = budget_seconds - (loop.time() - started)
        await asyncio.sleep(max(0.0, min(NETWORKIDLE_SLEEP_SECONDS, remaining)))
