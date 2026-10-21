"""The fake DOM tab (JS-eval + CDP seams), its scrolling variant and their constants.

Split out of :mod:`fakes` (T-5431); that module re-exports every name here, so
tests keep writing ``from fakes import ...``.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import GeneratorType
from typing import Any

import nodriver.cdp.network as cdp_network
import nodriver.cdp.page as cdp_page
from nodriver.core.connection import ProtocolException

from fakes_consts import (
    ABORTED_NAVIGATION,
    ABORTED_SUPERSESSION,
    CHROME_ERROR_PAGE,
    IFRAME,
    MAIN_FRAME,
    SUPERSEDED_MARK,
    TARGET_SWAPPED_ERROR,
)
from fakes_elements import FakeClickTarget, FakeTextField, fake_target
from fakes_js import JsPromise, js_result

# ---------------------------------------------------------------------------
# Fake DOM tab — covers BOTH cloner seams (JS-eval + CDP)
# ---------------------------------------------------------------------------


def cdp_command_name(cdp_obj: Any) -> str:
    """Stable key for a nodriver CDP command.

    nodriver's ``uc.cdp.<domain>.<command>(...)`` returns a *generator*; its
    ``gi_code.co_name`` is the command name (``get_document``, ``enable``, …).
    Falls back to ``__name__`` / type name for any non-generator command object.
    """
    code = getattr(cdp_obj, "gi_code", None)
    if code is not None:
        return code.co_name
    return getattr(cdp_obj, "__name__", type(cdp_obj).__name__)


class FakeTab:
    """A fake nodriver tab recording ``evaluate``/``send`` and returning canned
    responses. Instantaneous returns → ``_with_cdp_timeout``'s ``wait_for`` never
    fires (hermetic + zero-flake).

    Args:
        url: value for both ``.url`` and ``.target.url`` (engines read either).
        evaluate_result: default value returned by ``evaluate`` for any JS.
        evaluate_map: optional {substring: value}; first substring found in the
            JS expression wins over ``evaluate_result``.
        cdp_responses: {command_name: value_or_callable}; a callable is invoked
            with the command name and returns the response.
        lifecycle: WHEN a navigation's ``Page.lifecycleEvent``s reach the
            handlers, relative to the ``Page.navigate`` response — see
            :meth:`_navigate` (F-881).
        title_at_load: a page whose ``document.title`` is ``""`` until its
            ``load`` has been delivered, then this value (F-881).
        title_at_dcl: the same for ``DOMContentLoaded`` — what a ``<title>`` in
            the markup gives, since the parser sets it before DCL.

    **The navigation model (F-881).** ``send(cdp.page.navigate(url))`` answers
    the way Chrome does — ``(frameId, loaderId, errorText)`` — and delivers the
    new document's ``init`` / ``DOMContentLoaded`` / ``load`` lifecycle events
    to every handler registered for ``cdp.page.LifecycleEvent``. Measured on
    Chrome 152 (the finding's §2d): those events are NOT ordered against the
    response — ``DOMContentLoaded`` arrived 0.3 ms before it and ``load`` 0.3 ms
    after — so the fake can deliver them either side of it:

    * ``lifecycle="before"`` (default): synchronously, before the response
      returns — a wait armed after the response would never see them;
    * ``lifecycle="after"``: on the running loop, after the response — a wait
      that read the page at the response reads it before ``load``;
    * ``lifecycle="never"``: nothing is delivered (a transfer that never ends).

    A URL that differs only by fragment from the current one is a same-document
    navigation: ``loaderId`` is ``None`` and no lifecycle event fires (measured).
    ``stale_load_for`` names a loader id whose ``load`` is ALSO delivered (after
    the response), modelling an older document still finishing when the new
    navigation was sent — a wait not keyed on the response's loader id would
    take it. ``last_milestone`` is the last event the new document ever reaches
    (``"init"`` for a transfer that commits and hangs, F-787's route;
    ``"DOMContentLoaded"`` for a page whose subresources never finish).

    **Supersession (F-882).** ``supersede_after`` names the milestone of OUR
    document after which a DIFFERENT document commits in the SAME frame — the
    head-script ``location.replace`` (``"init"``: ours never reaches ``load``),
    the JS challenge, the ``meta refresh`` and the self-reload (``"load"``: ours
    finished first). Our document's later milestones are then never delivered,
    exactly as Chrome stops sending them for a destroyed document.
    ``supersede_count`` chains more than one; ``supersede_last_milestone`` is how
    far the FINAL one gets; ``supersede_url`` / ``title_after_supersede`` are
    what ``window.location.href`` / ``document.title`` read once it has landed.
    ``iframe_loader`` delivers a whole ``init``/``DOMContentLoaded``/``load`` set
    under a DIFFERENT ``frameId`` (measured: a same-origin iframe's events carry
    the subframe's id, never the main frame's), so a wait that did not filter on
    the frame would end on a document the caller never asked for.
    ``navigate_error`` is the ``errorText`` ``Page.navigate`` answers with — and
    for ``net::ERR_ABORTED`` (a download) nothing commits and no event ever
    fires, which is measured and is why the tool must answer rather than wait.
    Any other ``errorText`` commits Chrome's error page under OUR loader, so the
    lifecycle is the ordinary one and ``location.href`` then reads
    :data:`CHROME_ERROR_PAGE` while ``target.url`` keeps the URL asked for.

    **The hold.** ``supersede_held`` keeps the replacement BACK until the test
    calls :meth:`deliver_supersession`. It exists for the nodes that pin "the
    wait ended at OUR document" while a replacement is on its way: scheduled on
    the loop, the replacement advances the fake's page whenever the host gives
    the product a turn, and how many turns fall between a milestone landing and
    the tool's landing read is a property of the asyncio implementation
    (whether ``wait_for`` wraps its awaitable in a Task) and of scheduling —
    such a node was green on one interpreter and red on three CI lanes. Held,
    the page cannot move until the test says so, and a rule that needed the
    replacement to finish simply times out, which is the RED those nodes want.

    **The swap under the read (F-882e).** ``landing_swaps`` is how many times the
    post-navigation read (the one expression carrying BOTH ``location.href`` and
    ``document.title``) is answered with :data:`TARGET_SWAPPED_ERROR` before it
    answers at all — Chrome's reply to a ``Runtime.evaluate`` whose document was
    replaced while it was in flight. Each refusal DELIVERS whatever
    ``supersede_held`` was holding first, so the replacement really is what took
    the read's document away: a raise with a page that never moved would model
    an error with no swap behind it, and a pin built on that could not tell a
    re-read of the NEW document from a re-read of the old one.

    **The replay.** ``send(Page.setLifecycleEventsEnabled(true))`` delivers the
    CURRENT document's whole lifecycle again, under the name ``commit`` where a
    live navigation says ``init`` (measured, Chrome 152). It is modelled on
    every tab because every real navigation meets it: a reader that treated any
    ``load`` for an unknown loader as its own would return at the page it was
    leaving. ``replay_loader`` names the loader it arrives under.
    """

    def __init__(
        self,
        url: str = "https://fake.test/page",
        evaluate_result: Any = None,
        evaluate_map: dict[str, Any] | None = None,
        cdp_responses: dict[str, Any] | None = None,
        select_result: Any = None,
        target_id: str = "T-faketab",
        lifecycle: str = "before",
        title_at_load: str | None = None,
        title_at_dcl: str | None = None,
        stale_load_for: str | None = None,
        last_milestone: str = "load",
        supersede_after: str | None = None,
        supersede_count: int = 1,
        supersede_last_milestone: str = "load",
        supersede_url: str | None = None,
        title_after_supersede: str | None = None,
        iframe_loader: bool = False,
        navigate_error: str | None = None,
        replay_loader: str = "L-replay",
        supersede_held: bool = False,
        landing_swaps: int = 0,
    ) -> None:
        self.url = url
        # ``fake_target`` (defined below) — a Tab's ``.target`` is a real
        # ``TargetInfo``, so the double carries a real ``TargetID`` too.
        self.target = fake_target(target_id=target_id, url=url)
        self.awaited = 0
        self._evaluate_result = evaluate_result
        self._evaluate_map = evaluate_map or {}
        self._cdp_responses = cdp_responses or {}
        self._select_result = select_result
        self.closed = False
        # OUR websocket, not the page: see :meth:`disconnect`.
        self.disconnected = False
        # Set by the :class:`FakeBrowser` that opened this tab, so ``close()`` can
        # drop it from that browser's listing the way a real close does.
        self.opened_by: Any = None
        self.evaluate_calls: list[str] = []
        self.send_calls: list[str] = []
        self.get_calls: list[str] = []
        self.move_calls: list[str] = []
        self.select_calls: list[str] = []
        self.cdp_frames: list[dict[str, Any]] = []
        self.handlers: list[tuple[Any, Any]] = []
        self._lifecycle = lifecycle
        self._title_at_load = title_at_load
        self._title_at_dcl = title_at_dcl
        self._stale_load_for = stale_load_for
        self._last_milestone = last_milestone
        self._supersede_after = supersede_after
        self._supersede_count = supersede_count
        self._supersede_last_milestone = supersede_last_milestone
        self._supersede_url = supersede_url
        self._title_after_supersede = title_after_supersede
        self._iframe_loader = iframe_loader
        self._navigate_error = navigate_error
        self._replay_loader = replay_loader
        self._supersede_held = supersede_held
        self._landing_swaps = landing_swaps
        self._held: list[tuple[str, str, str]] = []
        self.navigations = 0
        # The milestones the CURRENT document has reached. A tab that exists is
        # showing a loaded document until it is navigated. Tracked for ONE
        # loader at a time (``_current_loader``), so a subframe's events, an
        # older document's and the enable-time replay's cannot move it.
        self._reached: set[str] = {"init", "DOMContentLoaded", "load"}
        self._current_loader: str | None = None
        self._superseded = False

    @property
    def type_(self) -> str:
        """nodriver's ``Connection`` answers ``type_`` off its target (F-1003)."""
        return self.target.type_

    # -- the navigation model (F-881) ---------------------------------------

    def _deliver(self, event: Any) -> None:
        """Hand *event* to every handler registered for its type, the way
        nodriver's listener does: ``callback(event, connection)`` first, then
        ``callback(event)`` for a handler that takes only the event."""
        for event_type, handler in list(self.handlers):
            if event_type is not type(event):
                continue
            try:
                handler(event, self)
            except TypeError:
                handler(event)

    def _lifecycle_event(self, frame_id: str, loader_id: str, name: str) -> Any:
        return cdp_page.LifecycleEvent(
            frame_id=cdp_page.FrameId(frame_id),
            loader_id=cdp_network.LoaderId(loader_id),
            name=name,
            timestamp=cdp_network.MonotonicTime(0.0),
        )

    def _reach(self, milestones: list[tuple[str, str, str]]) -> None:
        """Deliver the first of *milestones* now and the rest one loop iteration
        each — Chrome sends them as separate frames, and nodriver's ``Tab.wait``
        returns on the FIRST one, which is how a reader that trusted it came to
        read the page at ``init`` rather than at ``load``."""
        if not milestones:
            return
        frame_id, loader_id, name = milestones[0]
        if frame_id == MAIN_FRAME and name == "init":
            # A new document in the main frame: it is what the tab shows now.
            self._current_loader = loader_id
            self._reached = set()
            if SUPERSEDED_MARK in loader_id:
                self._superseded = True
                if self._supersede_url is not None:
                    self.url = self.target.url = self._supersede_url
        if frame_id == MAIN_FRAME and loader_id == self._current_loader:
            self._reached.add(name)
        self._deliver(self._lifecycle_event(frame_id, loader_id, name))
        rest = milestones[1:]
        if rest:
            if self._lifecycle == "before":
                self._reach(rest)
            else:
                asyncio.get_running_loop().call_soon(self._reach, rest)

    def _schedule(self, milestones: list[tuple[str, str, str]]) -> None:
        """Deliver *milestones* on the side of the ``Page.navigate`` response
        ``lifecycle`` names — the measured orderings are both real (F-881)."""
        if not milestones:
            return
        if self._lifecycle == "before":
            self._reach(milestones)
        else:
            asyncio.get_running_loop().call_soon(self._reach, milestones)

    def _supersession_milestones(self, loader_id: str) -> list[tuple[str, str, str]]:
        """The documents that took our place, plus (optionally) a subframe's."""
        order = ["init", "DOMContentLoaded", "load"]
        events: list[tuple[str, str, str]] = []
        if self._iframe_loader:
            events += [(IFRAME, f"{loader_id}-iframe", name) for name in order]
        for step in range(1, self._supersede_count + 1):
            final = step == self._supersede_count
            last = self._supersede_last_milestone if final else "init"
            loader = f"{loader_id}{SUPERSEDED_MARK}{step}"
            events += [
                (MAIN_FRAME, loader, name) for name in order[: order.index(last) + 1]
            ]
        return events

    def _document_milestones(
        self, loader_id: str
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
        """Our document's events, and what replaced it (F-882) — two lists,
        because a test may hold the second back (``supersede_held``)."""
        order = ["init", "DOMContentLoaded", "load"]
        ours = order[: order.index(self._last_milestone) + 1]
        if self._supersede_after is None or self._supersede_after not in ours:
            return [(MAIN_FRAME, loader_id, name) for name in ours], []
        # Our document stops where the replacement takes over; Chrome sends no
        # further milestone for a document that no longer exists.
        cut = ours.index(self._supersede_after) + 1
        return (
            [(MAIN_FRAME, loader_id, name) for name in ours[:cut]],
            self._supersession_milestones(loader_id),
        )

    def _hold_or(self, later: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        """*later* for delivery now, or nothing when the test asked to hold it."""
        if self._supersede_held:
            self._held.extend(later)
            return []
        return later

    def deliver_supersession(self) -> None:
        """Deliver what ``supersede_held`` kept back — all of it, synchronously.

        No ``call_soon``: the test's next line already sees the moved page, so
        "the replacement was real and would have been next" is one assertion
        and not a drain loop tuned to a host.
        """
        held, self._held = self._held, []
        for event in held:
            self._reach([event])

    def _navigate(self, url: str) -> tuple[Any, Any, Any]:
        """``Page.navigate``'s answer, and the new document's lifecycle."""
        same_document = "#" in url and url.split("#", 1)[0] == self.url.split("#", 1)[0]
        if self._navigate_error == ABORTED_NAVIGATION:
            # Measured: OUR loader never commits. Either nothing at all follows
            # (a download — the tab keeps showing the page it was already on, so
            # ``.url`` is deliberately NOT moved here), or the document that
            # aborted us commits in its place, which is what
            # ``supersede_after=ABORTED_SUPERSESSION`` models.
            self.navigations += 1
            loader_id = f"L{self.navigations}"
            if self._supersede_after == ABORTED_SUPERSESSION:
                self._schedule(self._hold_or(self._supersession_milestones(loader_id)))
            return (
                cdp_page.FrameId(MAIN_FRAME),
                cdp_network.LoaderId(loader_id),
                self._navigate_error,
            )
        failed = self._navigate_error is not None and not same_document
        self.url = CHROME_ERROR_PAGE if failed else url
        self.target.url = url
        if same_document:
            return (cdp_page.FrameId(MAIN_FRAME), None, None)
        self.navigations += 1
        loader_id = f"L{self.navigations}"
        milestones: list[tuple[str, str, str]] = []
        if self._stale_load_for is not None:
            milestones.append((MAIN_FRAME, self._stale_load_for, "load"))
        if self._lifecycle != "never":
            own_events, later = self._document_milestones(loader_id)
            milestones.extend(own_events)
            milestones.extend(self._hold_or(later))
        self._schedule(milestones)
        return (
            cdp_page.FrameId(MAIN_FRAME),
            cdp_network.LoaderId(loader_id),
            self._navigate_error,
        )

    def _replay_lifecycle(self) -> None:
        """What ``Page.setLifecycleEventsEnabled(true)`` really does (F-882):
        re-send the CURRENT document's whole lifecycle, ``commit`` standing in
        for ``init``, under the loader of the page being LEFT."""
        for name in ("commit", "DOMContentLoaded", "load"):
            self._deliver(self._lifecycle_event(MAIN_FRAME, self._replay_loader, name))

    def _title_now(self) -> str | None:
        """What ``document.title`` reads for the modelled page, or ``None`` when
        no page is modelled (the canned answers apply)."""
        if self._title_at_load is None and self._title_at_dcl is None:
            return None
        if self._superseded and self._title_after_supersede is not None:
            return self._title_after_supersede if "load" in self._reached else ""
        if self._title_at_load is not None and "load" in self._reached:
            return self._title_at_load
        if self._title_at_dcl is not None and "DOMContentLoaded" in self._reached:
            return self._title_at_dcl
        return ""

    async def wait(self, t: Any = None) -> None:
        """nodriver 0.47's ``Tab.wait(t)``, as it IS (F-881).

        *t* is a DURATION. Its body is ``if not t: t = 0.5; await asyncio.wait(
        [first-of-five-navigation-events, sleep(t)])`` — so a truthy argument of
        any kind (``tab.wait(cdp.page.LoadEventFired)``, an event CLASS) skips
        the wait outright, which is how ``navigate``'s ``load`` wait came to
        cost 0.02 ms. Modelled exactly: nothing for a truthy *t*, one loop
        iteration for none — when :meth:`_navigate`'s first event lands.
        """
        self.awaited += 1
        if not t:
            await asyncio.sleep(0)

    def __await__(self) -> Any:
        """nodriver's ``Tab.__await__`` (→ ``Tab.wait()``). Only ``Tab`` defines
        it — see :class:`FakeDiscoveredTarget`, which deliberately does not."""
        return self.wait().__await__()

    def _answer_for_js(self, expression: str) -> Any:
        """The canned answer for a JS expression — ONE home for it.

        Both eval seams route here: ``evaluate()`` (nodriver's helper) and
        ``send(cdp.runtime.evaluate(...))`` (the raw command ``execute_script``
        uses since F-832). A test says "this JS answers with X" once, whichever
        seam the code under test happens to take.
        """
        if "location.href" in expression and "document.title" in expression:
            # The post-navigation landing read (F-882): ONE round trip, so the
            # url and the title it answers with are always the same document's.
            if self._landing_swaps > 0:
                self._landing_swaps -= 1
                # The replacement lands FIRST, then the read fails: that order
                # is what makes the refusal evidence of a swap (F-882e).
                self.deliver_supersession()
                raise ProtocolException(TARGET_SWAPPED_ERROR)
            title = self._title_now()
            if title is None:
                title = self._evaluate_map.get("document.title", "")
            return json.dumps([self.url, title])
        if expression.strip() == "document.title" and self._title_now() is not None:
            return self._title_now()
        for needle, resp in self._evaluate_map.items():
            if needle in expression:
                return resp
        if self._evaluate_result is None and expression.strip() in (
            "window.location.href",
            "location.href",
        ):
            # Where the modelled page IS — ``.url`` moves with every navigation
            # the fake answers (F-881); an explicit map entry above still wins.
            return self.url
        return self._evaluate_result

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        self.evaluate_calls.append(expression)
        return self._answer_for_js(expression)

    async def get(self, url: str, *args: Any, **kwargs: Any) -> FakeTab:
        """nodriver's ``Tab.get``, as weak as the real one (F-881).

        The real one is ``send(Page.navigate)`` then ``Tab.wait()``, which
        returns on the FIRST navigation event (or after 0.5 s when the ``Page``
        domain was never enabled — the shipped product's case), so the tab it
        hands back is committed but not loaded. Modelled as the same send plus
        ONE loop iteration, which is exactly when :meth:`_navigate`'s ``init``
        lands. Returns ``self``, as ``Tab.get`` returns the tab it navigated.
        """
        self.get_calls.append(url)
        await self.send(cdp_page.navigate(url))
        await self
        return self

    async def back(self) -> None:
        """nodriver's ``Tab.back`` — and it is deliberately as weak as the real
        one: ``Tab.back()`` is a bare ``Runtime.evaluate("window.history.back()")``
        that returns BEFORE Chrome has committed anything and leaves ``.url``
        untouched. A fake that flipped ``.url`` here would model a browser that
        does not exist and would hide exactly the race F-833's guard has to
        survive. Where the move LANDS is stated by the caller, through the same
        ``window.location.href`` answer the product reads it from.
        """
        self.move_calls.append("back")

    async def forward(self) -> None:
        """nodriver's ``Tab.forward`` — see :meth:`back`."""
        self.move_calls.append("forward")

    async def reload(self, *args: Any, **kwargs: Any) -> None:
        """nodriver's ``Tab.reload`` (``Page.reload``) — see :meth:`back`.

        Takes ``*args``/``**kwargs`` because the real signature carries
        ``ignore_cache``/``script_to_evaluate_on_load``; recording only the fact
        of the reload keeps F-800 (``ignore_cache`` dropped by the tool) visible
        as its own defect rather than accidentally pinned here.
        """
        self.move_calls.append("reload")

    async def close(self) -> None:
        """nodriver's ``Tab.close`` (``Target.closeTarget``).

        Drops the tab from the listing of the browser that opened it, so a test
        can assert a failure path left NO orphan behind rather than merely that
        ``close()`` was called.
        """
        self.closed = True
        if self.opened_by is not None and self in self.opened_by.tabs:
            self.opened_by.tabs.remove(self)

    async def disconnect(self) -> None:
        """nodriver's ``Connection.disconnect`` — every ``Tab`` IS a Connection.

        A different verb from ``close`` above and modelled separately because the
        product distinguishes them: ``Tab.close`` is ``Target.closeTarget`` (the
        page goes away), while ``disconnect`` cancels the listener task and closes
        OUR websocket, leaving the page and the browser exactly as they were. The
        F-888 cleanup path may only ever do the second. Deliberately NOT named
        ``aclose``: nodriver has no such method, and since ``Connection.__getattr__``
        delegates unknown attributes to ``self.target``, a double that answered
        one would hide production calling a method that does not exist.
        """
        self.disconnected = True

    async def query_selector(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """The nodriver element-resolution seam used by the CDP styles path and
        ``clone_element_complete``. Returns the configured ``select_result``
        (e.g. a ``node_id``-carrying element), or ``None`` for the not-found path.

        This is ``query_selector`` and deliberately NOT ``select``: since F-884
        ``element_resolution`` calls only nodriver's SINGLE-SHOT surfaces,
        because ``select``/``find``/``select_all``/``xpath`` each bundle a poll
        loop into the query and holding the per-tab document lock across one
        froze every other DOM call on that tab for nodriver's whole 10 s
        default. Offering ``select`` here would let that regression back in
        unnoticed; an ``AttributeError`` is the point.
        """
        self.select_calls.append(selector)
        return self._select_result

    def add_handler(self, event_type: Any, handler: Any) -> None:
        """The nodriver CDP-event subscription seam (``Fetch.RequestPaused``, …).

        Records the (event_type, handler) pair so a test can assert *how many*
        handlers a code path registered, not just that it sent a command.
        """
        self.handlers.append((event_type, handler))

    def remove_handler(self, event_type: Any, handler: Any = None) -> None:
        """nodriver's ``Connection.remove_handler``, with its real semantics
        (F-824, F-881): the *handler* argument is ignored (the library's
        ``cb == self`` comparison never matches) and EVERY handler for the event
        type is dropped — ``del self.handlers[evt]`` — so a type with no handler
        left raises ``KeyError`` carrying the event class, which is exactly what
        an overlapping ``Tab.wait()`` hands the other waiter."""
        if not any(evt is event_type for evt, _ in self.handlers):
            raise KeyError(event_type)
        self.handlers = [(evt, h) for evt, h in self.handlers if evt is not event_type]

    async def send(self, cdp_obj: Any, *args: Any, **kwargs: Any) -> Any:
        name = cdp_command_name(cdp_obj)
        self.send_calls.append(name)
        frame: dict[str, Any] | None = None
        if isinstance(cdp_obj, GeneratorType):
            # Advancing once yields the request frame ({"method", "params"}), so a
            # test can assert the *arguments* of a CDP command.
            #
            # Nothing here is swallowed, deliberately. A nodriver command builds
            # its frame WHILE being advanced, so an argument it cannot serialize
            # (a raw dict where a ``to_json()``-bearing type is required) raises on
            # exactly this line — and a bare ``except Exception: pass`` around it
            # is what let Sentry STEALTH-…-1P ship green through 43 tests. Only
            # StopIteration is tolerated, and only because a command that yields no
            # frame has nothing to record.
            #
            # The ``isinstance`` gate is the whole tolerance: a non-generator
            # double (a Mock, which answers ``callable(obj.close)`` truthfully)
            # is skipped outright rather than advanced and forgiven.
            try:
                frame = next(cdp_obj)
                self.cdp_frames.append(frame)
            except StopIteration:
                pass
            cdp_obj.close()  # never leave the generator un-iterated
        if name == "insert_text" and frame:
            # Chrome's own routing for ``Input.insertText``: the text is inserted
            # at the caret of the FOCUSED element, and a control that refuses
            # typed characters refuses an insert too (F-876 measured all five —
            # readonly, range, date, color, a non-editable div — taking the
            # insert and moving nothing). Same seam, same field, per COMMAND.
            field = self._select_result
            if isinstance(field, FakeTextField) and field.focused:
                field.insert(frame["params"].get("text", ""))
        if name == "dispatch_mouse_event" and frame:
            # Chrome's own routing for pointer input (F-1017): the page receives
            # it, at the point the device-metrics override maps it to.
            clicked = self._select_result
            if isinstance(clicked, FakeClickTarget):
                clicked.receive_mouse(frame["params"])
        if name == "dispatch_key_event" and frame:
            # Chrome's own routing: a key event carrying ``text`` is inserted at
            # the caret of the FOCUSED element (F-873). Modelled per EVENT, not
            # per character, so a path that dispatches both a ``keyDown`` with
            # ``text`` and a separate ``char`` double-inserts here exactly as it
            # double-fires ``keypress`` in a real Chrome (measured, 152).
            field = self._select_result
            if isinstance(field, FakeTextField) and field.focused:
                field.receive(frame["params"])
        if name == "evaluate" and name not in self._cdp_responses and frame:
            # ``Runtime.evaluate`` reaches the SAME canned answers as
            # ``evaluate()`` — see ``_answer_for_js``. An explicit
            # ``cdp_responses["evaluate"]`` still wins, so no existing test moves.
            # A bare canned value is wrapped the way Chrome would answer it (a
            # ``(RemoteObject, exceptionDetails)`` pair) so a test can state the
            # answer once, whichever seam the code under test takes; an answer
            # that is already such a pair (``js_result``/``js_threw``) passes
            # through untouched.
            answer = self._answer_for_js(frame["params"]["expression"])
            if isinstance(answer, JsPromise):
                # F-883: the SAME script answers two different ways, and which
                # one it gets is the flag under test. Read off the frame, never
                # off a constructor argument — a double that answered the
                # settlement regardless would be green for the defect.
                awaited = bool(frame["params"].get("awaitPromise"))
                return answer.settled if awaited else JsPromise.UNAWAITED
            if answer is None or isinstance(answer, tuple):
                return answer
            return js_result(answer)
        if name == "set_lifecycle_events_enabled":
            # Chrome replays the CURRENT document's lifecycle here (F-882).
            self._replay_lifecycle()
        if name == "navigate" and name not in self._cdp_responses and frame:
            # ``Page.navigate`` — the navigation model (F-881, class docstring).
            return self._navigate(frame["params"]["url"])
        resp = self._cdp_responses.get(name, None)
        return resp(name) if callable(resp) else resp


class ScrollingTab(FakeTab):
    """A :class:`FakeTab` that models a scrolling document (F-875).

    ``scroll_page`` is the one tool whose answer is about a value the PAGE owns
    and that moves on its own, so a canned ``evaluate_result`` cannot express
    what it has to be held to. This double owns that value and applies the same
    rules Chrome does:

    * the position-read script is answered with a **JSON string**, because
      ``Tab.evaluate`` always requests deep serialization and an object literal
      would arrive as BiDi ``RemoteValue`` nodes (the F-869/F-872 trap — see
      :func:`js_aspect_answer`);
    * ``window.scrollTo`` / ``window.scrollBy`` move the position, clamped to
      ``[0, max]`` exactly as a real scroller clamps it, so "the page is one
      viewport tall" is modelled by geometry (``doc_height == viewport_height``)
      rather than by a flag;
    * ``behavior: 'smooth'`` does NOT arrive instantly. The animation is advanced
      one step per POSITION READ, which is what makes a mid-flight read
      deterministic without a real clock: a product that reads once and returns
      sees ``smooth_steps``-th of the way, the measured F-875 shortfall;
    * the scroll answers ``{moves, supported}`` and arms an ``ended`` latch that
      the read reports, exactly as ``scroll_position``'s scroll wrapper does.
      ``ended`` is set when the animation REACHES its target — never before —
      which is what makes ``stall_at`` meaningful: with a stall the position
      repeats while ``ended`` is still 0, so a settle that stops on repeated
      reads is caught and one that waits for the latch is not;
    * ``nested_id=…`` makes the page an **app shell** (F-878): the document
      scroller has nothing to scroll, the geometry belongs to a nested ``div``,
      and — the part that matters — a ``window`` scroll therefore moves
      NOTHING, exactly as real Chrome reported for
      ``html,body{overflow:hidden}`` + a full-viewport ``div{overflow:auto}``.
      Which element a script addresses is read off the script itself
      (``_el([…])`` vs ``window``), so the double never has to be told which
      product version is driving it. **The latch follows the same target**: a
      scroll that addresses the nested div latches only if the listener was
      armed on the div, and a ``window`` scroll only if it was armed on
      ``window`` — which is what real Chrome does (F-878 measured that an
      element's ``scrollend`` does not bubble to ``window``, and a document's is
      never dispatched at ``document.scrollingElement``), and it is what makes an
      arm-on-the-wrong-target bug visible here rather than only in the browser.


    Nothing here is written from the defect: the scripts are interpreted as
    Chrome interprets them, the scroller pick is answered by Chrome's rule
    rather than the product's, and the read answers with the geometry the page
    would report.
    """

    #: The prefix every script this double interprets as a QUESTION starts with:
    #: ``scroll_position``'s reads and its scroller pick are both one
    #: ``JSON.stringify`` round trip (the scroll scripts NAME
    #: ``document.scrollingElement`` too, so the element is not the marker).
    #: Named once, here, like :data:`ANIMATION_JS_MARKER`.
    POSITION_JS_MARKER = "JSON.stringify("

    #: What separates the two ``JSON.stringify`` questions (F-878): only the
    #: SCROLLER pick asks the page for computed overflow. The read never does.
    SCROLLER_JS_MARKER = "getComputedStyle"

    #: ``…scrollBy({top: N, left: M, …})`` — the two deltas, signed. The
    #: receiver is deliberately not matched: ``window`` for a document scroller
    #: and the resolved element for a nested one are the same operation, and
    #: WHICH one moves is decided by :meth:`_drives_nested`.
    _BY = re.compile(r"\.scrollBy\(\{top:\s*(-?\d+),\s*left:\s*(-?\d+)")
    #: ``…scrollTo({top: <expr>, left: N, …})`` — the vertical target as
    #: written; any ``…scrollHeight`` in it means "the bottom" (the target is an
    #: expression, not a fixed string, and it may itself contain commas, so the
    #: capture is non-greedy up to the one ``left:``).
    _TO = re.compile(r"\.scrollTo\(\{top:\s*(.+?),\s*left:\s*(-?\d+)")
    #: The resolver call the product emits for a NESTED scroller, and the index
    #: path inside it: ``_el([1,0])`` — ``_el(null)`` is the document.
    _EL = re.compile(r"_el\(\s*(null|\[[\d,\s]*\])\s*\)")
    #: Does this ``JSON.stringify`` round trip SCROLL? Since F-875 the scroll is
    #: a wrapper that also arms a latch and reports ``{moves, supported}``, so it
    #: is a ``JSON.stringify`` like the read and the pick, and the scroll CALL is
    #: what tells them apart.
    _SCROLL_CALL = re.compile(r"\.scroll(?:To|By)\(\{")
    #: ``var T=…;`` — the object the product armed ``scrollend`` on AND scrolls.
    #: Read separately from :meth:`_drives_nested` on purpose: the two agreeing
    #: is the product's job, not this double's assumption.
    _TARGET_BIND = re.compile(r"var T=([^;]+);")

    def __init__(
        self,
        *,
        doc_height: int = 8016,
        viewport_height: int = 977,
        doc_width: int = 1280,
        viewport_width: int = 1280,
        scroll_y: int = 0,
        scroll_x: int = 0,
        smooth_steps: int = 4,
        never_settles: bool = False,
        growing_content: int = 0,
        nested_id: str | None = None,
        nested_classes: tuple[str, ...] = ("shell",),
        nested_path: tuple[int, ...] = (1, 0),
        document_height: int | None = None,
        document_width: int | None = None,
        stale_path: bool = False,
        stall_at: int = 0,
        stall_reads: int = 0,
        scrollend_supported: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.doc_height = doc_height
        self.viewport_height = viewport_height
        self.doc_width = doc_width
        self.viewport_width = viewport_width
        self.scroll_y = scroll_y
        self.scroll_x = scroll_x
        self.smooth_steps = smooth_steps
        #: When set, this page is an APP SHELL (F-878): the geometry above
        #: belongs to a nested ``div`` and the DOCUMENT scroller has nothing to
        #: scroll at all — the shape measured on real Chrome 152 for
        #: ``html,body{overflow:hidden}`` + a full-viewport ``div{overflow:auto}``
        #: (``html``: ``max_y 0``, ``overflow: hidden``; the shell: ``max_y
        #: 7023``). ``window.scrollTo`` therefore moves NOTHING here, which is
        #: what makes the pre-F-878 product measurably wrong against this double
        #: rather than merely unexercised.
        self.nested_id = nested_id
        self.nested_classes = nested_classes
        self.nested_path = nested_path
        #: The DOCUMENT's own content box, when it is NOT the scroller. Default
        #: ``None`` means exactly one viewport — nothing to scroll, the app
        #: shell above. Setting it makes the page BOTH-scrollable (fixture d: a
        #: 400 px ``overflow:auto`` box inside an 8000 px document), which is
        #: the only shape that can tell rule 1 (the ``scrollingElement``
        #: precedence) from rule 2. The document then has its OWN offsets and
        #: its own smooth flight, because it is a different scroll container.
        self.document_height = document_height
        self.document_width = document_width
        self.doc_scroll_y = 0
        self.doc_scroll_x = 0
        #: The chosen element is no longer in the document when the READ gets
        #: there — a page that re-rendered mid-scroll. ``_RESOLVE_JS`` falls
        #: back to the document scroller, so every later round trip addresses
        #: the DOCUMENT however the path is spelled, which is what makes the
        #: record name what it read rather than what it picked.
        self.stale_path = stale_path
        #: A page whose content keeps arriving never stops moving — the
        #: budget-exhaustion case. One pixel per read is enough to model it.
        self.never_settles = never_settles
        #: Pixels of content appended per read WITHOUT the viewport moving: the
        #: lazy-loading page that has stopped scrolling but is still filling in.
        #: Its offset is stable and its EXTENT is not, which is the one shape
        #: that tells an offset comparison from a whole-``Position`` one.
        self.growing_content = growing_content
        #: The F-875/CI-35046780659 shape: from the ``stall_at``-th read of a
        #: flight, the position REPEATS for ``stall_reads`` reads and then
        #: resumes. On a real page that is the renderer's main thread blocked
        #: while the compositor keeps scrolling — measured stall length equals
        #: the long task, so it is unbounded and no count of agreeing reads can
        #: see through it.
        self.stall_at = stall_at
        self.stall_reads = stall_reads
        #: ``'onscrollend' in window``. ``False`` drives the read-agreement
        #: fallback, the only path where ``START_GRACE_SECONDS`` still matters.
        self.scrollend_supported = scrollend_supported
        self._reads_in_flight = 0
        self._stalled = 0
        self._ended = False
        #: Which container the last scroll armed its ``scrollend`` listener on.
        #: Only that container's arrival sets the latch (:meth:`_arrive`).
        self._armed_nested = False
        self._flight: tuple[int, int] | None = None
        self._doc_flight: tuple[int, int] | None = None
        #: Every position read, in order — so a test can count round trips.
        self.position_reads: list[str] = []
        #: Every scroller pick, in order — so a test can pin that it is ONE per
        #: call and not one per settle poll (F-878 measured 2.59 ms per pick on
        #: a 6007-element page, which is why it is picked once).
        self.scroller_picks: list[str] = []

    @property
    def max_scroll_y(self) -> int:
        return max(0, self.doc_height - self.viewport_height)

    @property
    def max_scroll_x(self) -> int:
        return max(0, self.doc_width - self.viewport_width)

    @property
    def document_is_the_scroller(self) -> bool:
        """Is there no nested scroller at all? Then everything is the document."""
        return self.nested_id is None

    @property
    def doc_content_height(self) -> int:
        if self.document_is_the_scroller:
            return self.doc_height
        return (
            self.viewport_height
            if self.document_height is None
            else (self.document_height)
        )

    @property
    def doc_content_width(self) -> int:
        if self.document_is_the_scroller:
            return self.doc_width
        return (
            self.viewport_width
            if self.document_width is None
            else (self.document_width)
        )

    @property
    def doc_max_scroll_y(self) -> int:
        return max(0, self.doc_content_height - self.viewport_height)

    @property
    def doc_max_scroll_x(self) -> int:
        return max(0, self.doc_content_width - self.viewport_width)

    def _target_of(
        self, expression: str, *, x: int, y: int, max_x: int, max_y: int, content: int
    ) -> tuple[int, int] | None:
        """The (x, y) a scroll script asks OF ONE CONTAINER, or ``None``.

        *content* is that container's ``scrollHeight`` — what a ``scrollTo``
        naming ``…scrollHeight`` heads for. Every bound is passed in, because
        since F-878 a page can have two containers and a ``window`` script must
        clamp against the DOCUMENT's extent even when a nested div is taller.
        """
        by = self._BY.search(expression)
        if by is not None:
            target_x, target_y = x + int(by.group(2)), y + int(by.group(1))
        else:
            to = self._TO.search(expression)
            if to is None:
                return None
            top = to.group(1).strip()
            target_x = int(to.group(2))
            target_y = content if "scrollHeight" in top else int(top)
        return (
            max(0, min(int(target_x), max_x)),
            max(0, min(int(target_y), max_y)),
        )

    def _step(
        self, flight: tuple[int, int] | None, x: int, y: int
    ) -> tuple[int, int, tuple[int, int] | None, bool]:
        """One animation frame of *flight* from (*x*, *y*).

        Returns ``(x, y, flight, arrived)`` — ``arrived`` is the frame on which
        this container REACHED its target, i.e. the frame Chrome would dispatch
        ``scrollend`` on.
        """
        if flight is None:
            return (x, y, None, False)
        target_x, target_y = flight
        step_x = -(-abs(target_x - x) // self.smooth_steps)
        step_y = -(-abs(target_y - y) // self.smooth_steps)
        x += min(step_x, abs(target_x - x)) * (1 if target_x >= x else -1)
        y += min(step_y, abs(target_y - y)) * (1 if target_y >= y else -1)
        arrived = (x, y) == flight
        return (x, y, None if arrived else flight, arrived)

    def _arrive(self, *, nested: bool) -> None:
        """A container reached its target — does the armed listener SEE it?

        Chrome's answer, measured on the F-878 fixtures (Chrome 152): an
        element's ``scrollend`` is dispatched at that element and does NOT
        bubble to ``window``; a document's reaches ``window`` and ``document``
        and is NEVER dispatched at ``document.scrollingElement``. So a latch
        armed on the wrong object never fires, and the settle would spend its
        whole budget. Modelling that here rather than assuming it is what makes
        an arm-on-the-wrong-target bug a RED test instead of a browser-only one.
        """
        if nested == self._armed_nested:
            self._ended = True

    def _advance(self) -> None:
        """One animation frame's worth of movement, charged per read."""
        if self.growing_content:
            self.doc_height += self.growing_content
        if self.never_settles:
            self.scroll_y = min(self.scroll_y + 1, self.max_scroll_y)
            self.doc_height += 1  # the content that keeps arriving
            return
        if self._flight is None and self._doc_flight is None:
            return
        self._reads_in_flight += 1
        # The renderer stalled: the value the read can see does not advance,
        # while the scroll itself has NOT finished. This is the F-875 /
        # CI-35046780659 shape, and it is what tells a settle that waits for the
        # page's own end-of-scroll from one that stops on repeated reads.
        if (
            self.stall_reads
            and self._reads_in_flight >= self.stall_at
            and self._stalled < self.stall_reads
        ):
            self._stalled += 1
            return
        self.scroll_x, self.scroll_y, self._flight, nested_arrived = self._step(
            self._flight, self.scroll_x, self.scroll_y
        )
        self.doc_scroll_x, self.doc_scroll_y, self._doc_flight, doc_arrived = (
            self._step(self._doc_flight, self.doc_scroll_x, self.doc_scroll_y)
        )
        if nested_arrived:
            # The primary state is the NESTED element only when there is one.
            self._arrive(nested=not self.document_is_the_scroller)
        if doc_arrived:
            self._arrive(nested=False)

    def _drives_nested(self, expression: str) -> bool:
        """Does this script address the NESTED scroller rather than the window?

        ``_el([…])`` is the resolver the product emits for a nested scroller and
        ``_el(null)``/no call at all is the document. On a page that has no
        nested scroller the answer is always ``False`` and the geometry is the
        document's, so every pre-F-878 test reads exactly as it did.

        ``stale_path`` is the one case where the SCRIPT says nested and the
        answer is ``False``: ``_RESOLVE_JS`` falls back to the document scroller
        when the path resolves to nothing, and this models that fallback rather
        than the spelling.
        """
        if self.nested_id is None or self.stale_path:
            return False
        found = self._EL.search(expression)
        return found is not None and found.group(1) != "null"

    def _armed_on_nested(self, expression: str) -> bool:
        """Was the ``scrollend`` listener armed on the nested element?

        Read off the ``var T=…`` binding, NOT off which container the script
        moves — so a product that scrolls the div and listens on ``window``
        (or the reverse) is caught here rather than only in a browser. Under
        ``stale_path`` the binding still SAYS ``_el([…])`` but resolves to the
        document element, which is not a target a document scroll's ``scrollend``
        is ever dispatched at (measured) — so it is not "nested" either, and
        nothing latches. That costs nothing in practice, because a stale path
        lands on a document whose ``moves`` is already false.
        """
        bound = self._TARGET_BIND.search(expression)
        if bound is None:
            return False
        target = bound.group(1)
        if "_el(" not in target:
            return False
        return not self.stale_path and "null" not in target

    def _scroller_answer(self, expression: str) -> str:
        """The page's answer to the scroller pick — Chrome's rule, not the product's.

        Rule 1 first, because that is the order Chrome's own geometry imposes:
        if the DOCUMENT can move on the asked-for axis it is the scroller, and
        that is true of a plain page (F-878 fixture a) and of a page that has a
        nested scroller as well (fixture d). Only when the document cannot move
        — ``html,body{overflow:hidden}``, measured as ``max_y 0`` — does the
        pick fall through to the one nested ``div{overflow:auto}``.
        """
        axis = "x" if "'x'" in expression else "y"
        doc_extent = self.doc_max_scroll_x if axis == "x" else self.doc_max_scroll_y
        if doc_extent > 0 or self.document_is_the_scroller:
            return json.dumps({"path": None, "document": True})
        return json.dumps({"path": list(self.nested_path), "document": False})

    def _read_answer(self, expression: str) -> str:
        """The geometry, identity and end-latch of the element the read addressed.

        ``ended`` is the LATCH, not a per-element fact: the page keeps one
        (``window.__stealthMcpScroll``) whatever it armed the listener on, so
        the read reports it regardless of which element it is reading.
        """
        ended = self._ended and self.scrollend_supported
        if not self._drives_nested(expression):
            return json.dumps(
                {
                    "x": self.doc_scroll_x
                    if not self.document_is_the_scroller
                    else self.scroll_x,
                    "y": self.doc_scroll_y
                    if not self.document_is_the_scroller
                    else self.scroll_y,
                    "max_x": self.doc_max_scroll_x,
                    "max_y": self.doc_max_scroll_y,
                    "tag": "html",
                    "id": "",
                    "classes": [],
                    "document": True,
                    "ended": ended,
                }
            )
        return json.dumps(
            {
                "x": self.scroll_x,
                "y": self.scroll_y,
                "max_x": self.max_scroll_x,
                "max_y": self.max_scroll_y,
                "tag": "div",
                "id": self.nested_id,
                "classes": list(self.nested_classes),
                "document": False,
                "ended": ended,
            }
        )

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        self.evaluate_calls.append(expression)
        if expression.startswith(self.POSITION_JS_MARKER):
            if self.SCROLLER_JS_MARKER in expression:
                self.scroller_picks.append(expression)
                return self._scroller_answer(expression)
            if self._SCROLL_CALL.search(expression):
                return self._apply_scroll(expression)
            self.position_reads.append(expression)
            self._advance()
            return self._read_answer(expression)
        return self._answer_for_js(expression)

    def _apply_scroll(self, expression: str) -> Any:
        """The scroll round trip: arm the latch and scroll ONE container.

        Which container the script drives is read off the script (``_el([…])``
        vs ``window``); which one the LISTENER was armed on is read off the
        ``var T=…`` binding, independently, so the two can disagree — and when
        they do, nothing ever latches. See :meth:`_arrive`.
        """
        nested = self._drives_nested(expression)
        # WHICH state holds the offsets, and whether that state is a NESTED
        # element, are two questions: on a page with no nested scroller at all
        # the document IS the scroller and its offsets live in the primary
        # state, but nothing about it is nested.
        on_primary = nested or self.document_is_the_scroller
        if on_primary:
            bounds = {
                "x": self.scroll_x,
                "y": self.scroll_y,
                "max_x": self.max_scroll_x,
                "max_y": self.max_scroll_y,
                "content": self.doc_height,
            }
            here = (self.scroll_x, self.scroll_y)
        else:
            bounds = {
                "x": self.doc_scroll_x,
                "y": self.doc_scroll_y,
                "max_x": self.doc_max_scroll_x,
                "max_y": self.doc_max_scroll_y,
                "content": self.doc_content_height,
            }
            here = (self.doc_scroll_x, self.doc_scroll_y)
        target = self._target_of(expression, **bounds)
        if target is None:  # pragma: no cover - the regex already matched
            return self._answer_for_js(expression)

        moves = target != here
        self._ended = False
        self._reads_in_flight = 0
        self._stalled = 0
        self._armed_nested = self._armed_on_nested(expression)
        flight = None
        if moves:
            if "'smooth'" in expression:
                flight = target
            else:
                here = target
        if on_primary:
            self.scroll_x, self.scroll_y = here
            self._flight = flight
        else:
            self.doc_scroll_x, self.doc_scroll_y = here
            self._doc_flight = flight
        if moves and flight is None:
            # An instant scroll is over before the evaluate returns.
            self._arrive(nested=nested)
        return json.dumps({"moves": moves, "supported": self.scrollend_supported})
