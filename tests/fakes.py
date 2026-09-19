"""Canonical hermetic test harness for the M6 characterization suite.

ONE home (dedup / conventions lens) for the fake DOM/tab/browser/BrowserManager
doubles, a fake in-memory storage, the single in-process tool invoker, and the
golden-normalisation helpers. **No test logic lives here** — only reusable
mechanism. Every M6 test module imports from here; a second hand-rolled tab mock
in a test module is a defect.

Two tab-interaction seams the cloners use are both faked:

* ``FakeTab.evaluate(js)`` — the **JS-eval path** (the canonical engine's
  structure/events/animations/assets/related_files aspects). Returns a canned
  value; a substring→value map lets one tab answer several distinct ``evaluate``
  calls.
* ``FakeTab.send(cdp_obj)`` — the **CDP path** (``cdp_element_cloner`` styles).
  nodriver CDP commands are *generators*; the
  canned response is keyed by the generator's ``co_name`` (e.g. ``get_document``)
  which is stable and call-order-independent. The generator is closed so it is
  never left un-iterated.

The in-process invoker follows the repo's established FastMCP seam: a registered
tool is a ``FunctionTool`` whose original coroutine (or plain function, for the 5
sync hook-doc tools) is ``.fn``. ``call_tool`` unwraps it and awaits only when
the result is awaitable, so it drives both the 89 async and 5 sync tools
identically.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import socket
from pathlib import Path
from types import GeneratorType, SimpleNamespace
from typing import Any, ClassVar

import nodriver.cdp.dom as cdp_dom
import nodriver.cdp.network as cdp_network
import nodriver.cdp.page as cdp_page
import nodriver.cdp.runtime as cdp_runtime
import nodriver.cdp.target as cdp_target

#: "this double was not told to answer anything unusual" — distinct from every
#: value a test might legitimately want it to answer with, ``None`` included.
_UNSET = object()

#: The two frames :class:`FakeTab` models. A real subframe's lifecycle events
#: carry the SUBFRAME's id (measured, F-882), which is what makes a wait's frame
#: filter observable at all.
MAIN_FRAME = "F-main"
IFRAME = "F-child"
#: What a superseding loader's id carries, so the fake can tell which document
#: the tab is showing without a second bookkeeping field.
SUPERSEDED_MARK = "-sup"
#: The one ``errorText`` under which OUR loader never commits (a download, or a
#: page that navigated itself away while we were still pending).
ABORTED_NAVIGATION = "net::ERR_ABORTED"
#: ``supersede_after`` for the abort that IS a supersession: no document of ours
#: ever commits, and the one that aborted us takes its place.
ABORTED_SUPERSESSION = "aborted"

# ---------------------------------------------------------------------------
# Module signature guards (shared by the on-disk record modules)
# ---------------------------------------------------------------------------


def _public_functions(module: Any) -> list[tuple[str, Any]]:
    """Every function *module* defines itself, excluding privates and imports."""
    return [
        (name, obj)
        for name, obj in vars(module).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == module.__name__
    ]


def _is_path_param(param: Any) -> bool:
    """A parameter that selects an on-disk record, by name or by annotation.

    Name alone was enough while the only callers were the two registry modules,
    whose parameters are literally ``path``/``paths``. ``spawn_exhaustion``
    takes the same kind of argument under a domain name (``pid_file``), so the
    annotation counts too — otherwise the guard reads as vacuous there and
    fails for the wrong reason.
    """
    return param.name in ("path", "paths") or "Path" in str(param.annotation)


def _takes_a_path(func: Any) -> bool:
    return any(
        _is_path_param(param) for param in inspect.signature(func).parameters.values()
    )


def assert_no_default_paths(module: Any) -> None:
    """Assert no public function of *module* defaults a path parameter.

    Both on-disk record modules (``backend_registry``, ``browser_pid_registry``)
    state the same corollary in their docstrings, so the check belongs here
    rather than copied into each test file. The caller's binding is what selects
    the file: redirecting it at runtime is what the hermetic fixtures do, and it
    is the only thing keeping a test run from editing the developer's live
    ``~/.stealth-mcp``. A function that defaulted its path would bind its own
    module global at def-time and silently ignore that redirection.

    Three ways to offend, all caught: naming the parameter ``path``/``paths``
    and giving it a default, annotating ANY parameter as a Path and giving it a
    default (``pid_file: Path | None = None`` would escape a name-only check),
    and defaulting ANY parameter to a Path (a future
    ``record=SERVER_STATE_FILE`` would escape both).

    The companion assertion is that the sweep actually visited a path-taking
    function — a renamed module, a broken ``__module__`` filter, or an API that
    stopped taking paths would otherwise leave this passing vacuously.
    """
    functions = _public_functions(module)
    offenders = [
        f"{name}({param})"
        for name, func in functions
        for param in inspect.signature(func).parameters.values()
        if (_is_path_param(param) and param.default is not inspect.Parameter.empty)
        or isinstance(param.default, Path)
    ]
    assert offenders == [], (
        f"path parameters must stay required, but {offenders} default theirs"
    )
    visited = [name for name, func in functions if _takes_a_path(func)]
    assert visited, (
        f"vacuous guard: no public function of {module.__name__} takes a path "
        "parameter — has the module been renamed or its API changed?"
    )


# ---------------------------------------------------------------------------
# In-process tool invoker (THE one way to drive a tool in a test)
# ---------------------------------------------------------------------------


async def call_tool(server_mod: Any, name: str, /, **kwargs: Any) -> Any:
    """Invoke the registered tool ``name`` on ``server_mod`` in-process.

    Unwraps the FastMCP ``.fn`` seam (``getattr(fn, "fn", fn)`` — a no-op if the
    attribute is already the raw callable) and awaits only awaitable results, so
    the same call drives async and sync tools alike. No transport, no Chrome.
    """
    tool_obj = getattr(server_mod, name)
    fn = getattr(tool_obj, "fn", tool_obj)
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def pretend_display_context(monkeypatch: Any, token: str) -> None:
    """State a test's display premise instead of inheriting the runner's desktop.

    ``spawn_browser`` refuses a headed spawn from a context that cannot show a
    window (F-808), so without this ANY hermetic headed-spawn test would pass on
    a developer's desktop and fail on a DISPLAY-less CI cell — the same test
    asserting two different things. Patch the one source token: everything else
    (``can_show_windows``) derives from it, so a test can never pin an impossible
    pair. Use ``display_context.HEADLESS`` / ``UNVERIFIED`` for the named cases.
    """
    from stealth_chrome_devtools_mcp.embedded import display_context

    monkeypatch.setattr(display_context, "display_context", lambda: token)


def v2_record(**backends: dict) -> dict:
    """A schema-v2 ``server.json`` record from ``context=entry`` kwargs, where
    ``_`` in a kwarg name reads as ``-`` (``win_session_1`` → ``win-session-1``,
    the token `display_context()` actually produces).

    Here rather than in a test module because both
    `test_cli_status_wedged.py` and `test_probe_backend_status.py` build the
    same record shape (F-868), and a record builder that disagreed with itself
    between two files is exactly the drift this module exists to prevent. It
    writes the schema literally, deliberately: `backend_registry.record_backend`
    is the code under test in several of those cases, so a fixture that went
    through it could not express a record that function would never write — a
    hand-edited one, or a pre-supersede pair.

    Since F-886 the writer emits schema v3 (a LIST of entries, so one display
    context can hold two clients' backends) and v2 is the LEGACY keyed shape
    every 2.0.4-2.1.8 record has. Deliberately still v2 here: every reader that
    consumes this builder goes through `backends_in`, so these fixtures are also
    what keeps the v2 read path exercised — the path an upgrading user's record
    takes on its first start.
    """
    return {
        "schema": 2,
        "backends": {ctx.replace("_", "-"): entry for ctx, entry in backends.items()},
    }


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
        # Set by :meth:`FakeBrowser.get` for a tab it opened, so ``close()`` can
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
        self._held: list[tuple[str, str, str]] = []
        self.navigations = 0
        # The milestones the CURRENT document has reached. A tab that exists is
        # showing a loaded document until it is navigated. Tracked for ONE
        # loader at a time (``_current_loader``), so a subframe's events, an
        # older document's and the enable-time replay's cannot move it.
        self._reached: set[str] = {"init", "DOMContentLoaded", "load"}
        self._current_loader: str | None = None
        self._superseded = False

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
        self.url = url
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


# ---------------------------------------------------------------------------
# JS-aspect transport fidelity (F-846 animations, F-872 the other four)
# ---------------------------------------------------------------------------


def js_aspect_answer(payload: Any) -> str:
    """What a REAL tab hands back for a cloner JS aspect script (F-872).

    ``nodriver``'s ``Tab.evaluate`` always requests deep serialization, so an
    object literal comes back as BiDi ``RemoteValue`` nodes
    (``[[key, {type, value}], …]``) at EVERY depth — a plain dict is a shape the
    transport cannot produce. All six aspect scripts therefore end in
    ``JSON.stringify``; the ONE home for encoding that in a test is here, so no
    fixture can quietly re-encode the bug the fix removed.
    """
    return json.dumps(payload)


# The marker present in ``embedded/js/extract_animations.js`` — the substring an
# ``evaluate_map`` keys on to answer THAT script and no other.
ANIMATION_JS_MARKER = "animation-facts"


def animation_evaluate_map(facts: Any) -> dict[str, str]:
    """``evaluate_map`` answering the animations script the way a REAL tab does.

    The animations collector ends in ``JSON.stringify`` (F-846): a string is the
    one shape that survives ``tab.evaluate`` — a non-primitive comes back as CDP
    deep-serialization instead. So the fake must answer with the **JSON string**,
    never the dict. A fake that returned the dict would re-encode the very bug
    the transport fix removes, and every golden captured through it would prove
    nothing. This helper is the one home for that fidelity: tests state the facts
    as a dict and the encoding happens here, once.
    """
    return {ANIMATION_JS_MARKER: json.dumps(facts)}


# ---------------------------------------------------------------------------
# ``Runtime.evaluate`` answers (the F-832 execute_script seam)
# ---------------------------------------------------------------------------


def js_result(
    value: Any = None,
    type_: str = "object",
    subtype: str | None = None,
    unserializable_value: str | None = None,
    description: str | None = None,
) -> tuple[cdp_runtime.RemoteObject, None]:
    """The ``(result, exceptionDetails)`` pair Chrome answers a successful
    ``Runtime.evaluate`` with — built from nodriver's own constructor.

    Mirrors what Chrome actually sends under ``returnByValue``: ``undefined``
    carries no ``value``; ``null`` is an object with ``subtype="null"``;
    ``Infinity``/``NaN`` arrive as ``unserializableValue``; a value Chrome cannot
    send at all arrives as type + ``description`` only.
    """
    return (
        cdp_runtime.RemoteObject(
            type_=type_,
            subtype=subtype,
            value=value,
            unserializable_value=(
                None
                if unserializable_value is None
                else cdp_runtime.UnserializableValue(unserializable_value)
            ),
            description=description,
        ),
        None,
    )


class JsPromise:
    """A script whose value is a **Promise**, as Chrome answers it (F-883).

    Not a canned answer but a canned *settlement*: what
    ``Runtime.evaluate(returnByValue=true)`` hands back depends on whether the
    caller asked for ``awaitPromise``, and the whole of F-883 is that the two
    are different answers for the same script.

    * ``awaitPromise: false`` — Chrome does not look at the settlement. It
      serializes the Promise OBJECT by value, and a ``Promise`` has no own
      enumerable properties, so the answer is ``{}`` (measured, Chrome 152).
      That is why a lost value and a REJECTION and a genuine ``return {}`` were
      the same three bytes on the wire.
    * ``awaitPromise: true`` — Chrome waits and answers with the settlement:
      *settled* for a resolution, or ``exceptionDetails`` for a rejection.

    *settled* is the ``(result, exceptionDetails)`` pair the resolution or the
    rejection produces, i.e. a ``js_result``/``js_threw`` — so a test states the
    settlement once and the DOUBLE decides which of the two answers the code
    under test earned.
    """

    def __init__(self, settled: tuple[cdp_runtime.RemoteObject, object | None]) -> None:
        self.settled = settled

    #: What Chrome sends for an un-awaited Promise under ``returnByValue``.
    UNAWAITED: ClassVar[tuple[cdp_runtime.RemoteObject, None]] = (
        cdp_runtime.RemoteObject(
            type_="object",
            subtype="promise",
            class_name="Promise",
            value={},
            description="Promise",
        ),
        None,
    )


def js_promise(
    value: Any = None, type_: str = "object", rejects: str | None = None
) -> JsPromise:
    """A :class:`JsPromise` resolving to *value*, or rejecting with *rejects*."""
    return JsPromise(
        js_threw(rejects) if rejects is not None else js_result(value, type_)
    )


def js_threw(
    description: str,
) -> tuple[cdp_runtime.RemoteObject, cdp_runtime.ExceptionDetails]:
    """The pair Chrome answers with when the evaluated script THREW.

    Chrome sends BOTH a result object (the thrown value) and ``exceptionDetails``
    — which is why a reader that trusts the result reports a throw as a success
    (F-795). Built from nodriver's constructors so a guard reading the wrong
    field names cannot stay green.
    """
    thrown = cdp_runtime.RemoteObject(
        type_="object",
        class_name=description.split(":", maxsplit=1)[0],
        description=description,
    )
    return (
        thrown,
        cdp_runtime.ExceptionDetails(
            exception_id=1,
            text="Uncaught",
            line_number=0,
            column_number=0,
            exception=thrown,
        ),
    )


# ---------------------------------------------------------------------------
# Fake target-listing seam (nodriver ``Browser.tabs`` entries)
# ---------------------------------------------------------------------------


def fake_target(
    target_id: str = "T1",
    url: str = "https://fake.test/page",
    title: str = "Fake Page",
    type_: str = "page",
) -> SimpleNamespace:
    """A nodriver ``cdp.target.TargetInfo`` double.

    This is the metadata ``list_tabs`` reads off every entry of ``Browser.tabs``,
    and the metadata ``Browser.update_targets()`` refreshes in place.

    ``target_id`` is a real :class:`nodriver.cdp.target.TargetID` (a ``str``
    subclass), not a bare ``str``: every by-id CDP command serialises it with
    ``target_id.to_json()`` (``cdp/target.py:258``), so a bare ``str`` here would
    make the F-775 by-id pins pass against a fake that could not exist.
    """
    return SimpleNamespace(
        target_id=cdp_target.TargetID(target_id), url=url, title=title, type_=type_
    )


def fake_element(node_id: int = 1, **attrs: Any) -> SimpleNamespace:
    """A nodriver element double carrying a REAL ``cdp.dom.NodeId``.

    The exact twin of :func:`fake_target`'s reasoning, one level down. ``NodeId``
    is an ``int`` subclass whose only addition is ``to_json()``, and every by-node
    CDP command serialises with ``node_id.to_json()`` (``cdp/css.py:1814``), so a
    bare ``int`` here is an element that could not exist: production's
    ``_resolve_node_id`` returns ``element.node.node_id``, always a real
    ``NodeId``.

    ``NodeId(n) == n``, so assertions comparing against a plain int still hold.
    """
    return SimpleNamespace(node_id=cdp_dom.NodeId(node_id), **attrs)


class FakeTextField:
    """A nodriver ``Element`` double for a text control, PAGE-BACKED (F-873).

    Models the one thing the typing path depends on and nothing else: a CDP key
    event carrying ``text`` inserts that text at the caret — **only if the
    control accepts typed characters**. ``accepts=False`` is the whole of
    F-873's "reported success, typed nothing" class: a ``readonly`` input, a
    ``range``/``date``/``color`` control, or a page whose script cancels the
    key. In every one of them Chrome DELIVERS the events and the value never
    moves (measured on Chrome 152 — see the finding's §2 matrix), which is why
    a double that simply appended whatever it was sent could not express the
    defect at all.

    ``"\\r"``/``"\\n"`` are deliberately NOT inserted into a single-line
    control: a literal newline character is dropped by Chrome (measured), so
    the ONLY thing that can produce a newline or a submit is a real Enter key
    press, which is what the Enter pins assert against ``FakeTab.cdp_frames``.

    ``content_editable=True`` is the OTHER control shape the typing path has to
    hold: such an element has no ``.value`` at all (measured — it is
    ``undefined``), it carries its text in ``textContent``, and it takes an
    Enter as a newline where a single-line ``<input>`` drops it. The double
    reports itself as ``editable`` in the read-back so the surrounding contract
    is pinnable here; whether the JS picks the right property is the page's to
    evaluate and its witness is a real Chrome
    (``tests/test_e2e_hard_dom.py::test_contenteditable_and_multiselect``).

    The read-back answer is COMPUTED from this object's own state, never
    supplied by the test, so no fixture here can quietly encode the bug.
    """

    def __init__(
        self,
        value: str = "",
        accepts: bool = True,
        content_editable: bool = False,
        multiline: bool = False,
    ) -> None:
        self.value = value
        self.accepts = accepts
        self.content_editable = content_editable
        self.multiline = multiline
        self.focused = False
        self.apply_calls: list[str] = []

    async def focus(self) -> None:
        self.focused = True

    async def apply(self, js_function: str, *args: Any, **kwargs: Any) -> Any:
        """``Element.apply`` — ``Runtime.callFunctionOn(returnByValue=True)``.

        Answers the three functions the typing path sends: the focus call, the
        programmatic clear, and the read-back (whose answer is a JSON STRING,
        which is what ``return_by_value`` really hands back).
        """
        self.apply_calls.append(js_function)
        if "focus()" in js_function:
            self.focused = True
            return None
        if "elem.value = ''" in js_function:
            self.value = ""
            return None
        if "JSON.stringify" in js_function:
            return json.dumps({"editable": self.content_editable, "text": self.value})
        return None

    def receive(self, params: dict[str, Any]) -> None:
        """Insert one key event's ``text``, the way the renderer would."""
        text = params.get("text")
        if not text or params.get("type") == "keyUp":
            return
        self.insert(text)

    def insert(self, text: str) -> None:
        """Commit *text* at the caret — the ONE place this double changes value.

        Both entry seams land here: a key event carrying ``text``
        (``Input.dispatchKeyEvent``) and ``paste_text``'s single
        ``Input.insertText`` (F-876). One home, because the whole point of the
        double is that a control which refuses one refuses the other, which is
        what the F-876 matrix measured on Chrome 152.
        """
        if not text or not self.accepts:
            return
        if text in ("\r", "\n") and not (self.multiline or self.content_editable):
            return
        self.value += "\n" if text == "\r" else text


class FakeClickTarget:
    """A nodriver ``Element`` double for a CLICK target, PAGE-BACKED (F-876).

    ``FakeTextField``'s sibling, one interaction over. It models the two things
    the click path depends on and nothing else:

    * **where the click goes** — ``Element.mouse_click`` clicks
      ``Position(quads[0]).center``, i.e. the centre of the element's FIRST box.
      This double is given that box (``rect``) and computes the point from it,
      so no test can state a point the geometry does not produce. An element
      with ``rendered=False`` has no box at all (``display:none``): nodriver's
      ``get_position`` raises there and the product falls back to the synthetic
      ``Element.click``, which is what ``mouse_click_error`` expresses.
    * **what is under that point** — ``document.elementFromPoint``. Measured on
      Chrome 152 (F-876 §2c): it names an overlay for a covered target, ``BODY``
      for a ``pointer-events:none`` / zero-size / ``visibility:hidden`` target,
      and the target itself for a ``disabled`` one (whose click Chrome still
      suppresses — which is why ``disabled`` is a separate fact and not a
      hit-test outcome).

    ``disabled`` here is what ``:disabled`` MATCHES, not the ``elem.disabled``
    IDL attribute: a ``<button>`` inside a ``<fieldset disabled>`` reports
    ``elem.disabled === false``, matches ``:disabled``, hit-tests to itself and
    receives nothing (measured). The double carries the one that decides the
    answer, so a test can express that shape without a real fieldset.

    The aim answer is a JSON **string** COMPUTED from this object's own state —
    never supplied by a test — for the same reason ``FakeTextField``'s read-back
    is: a fixture that hands over the answer can quietly encode the bug.

    ``text`` exists only so a pin can assert it never reaches the record: an
    overlay is frequently a consent banner or a modal and its words are the
    page's, not the tool's to echo.
    """

    def __init__(  # noqa: PLR0913  PERMANENT(one field per measured DOM fact)
        self,
        tag: str = "button",
        element_id: str = "target",
        classes: tuple[str, ...] = (),
        rect: tuple[float, float, float, float] = (10.0, 20.0, 40.0, 20.0),
        hit: tuple[str, str, tuple[str, ...]] | None = None,
        disabled: bool = False,
        pointer_events: str = "auto",
        visibility: str = "visible",
        rendered: bool = True,
        text: str = "SECRET-BUTTON-LABEL",
        mouse_click_error: Exception | None = None,
        aim_answer: Any = _UNSET,
        viewport: tuple[float, float] = (1280.0, 720.0),
    ) -> None:
        self.viewport = viewport
        self.tag = tag
        self.element_id = element_id
        self.classes = tuple(classes)
        self.rect = rect
        self.hit = hit
        self.disabled = disabled
        self.pointer_events = pointer_events
        self.visibility = visibility
        self.rendered = rendered
        self.text = text
        self.mouse_click_error = mouse_click_error
        self._aim_answer = aim_answer
        self.calls: list[str] = []
        self.apply_calls: list[str] = []

    # -- the shape vocabulary, the only thing said about any element ---------
    def _self_shape(self) -> dict[str, Any]:
        return {"tag": self.tag, "id": self.element_id, "classes": list(self.classes)}

    def _in_viewport(self, x: float, y: float) -> bool:
        """``document.elementFromPoint`` answers ``null`` outside the viewport.

        Measured (F-876 §2c): an ``absolute; left:-500px`` button keeps a real
        33.5 x 21 box at a NEGATIVE point, ``scroll_into_view`` does not bring it
        back, and the hit-test there is ``null`` — a different fact from "another
        element was on top", which is why the double derives it from the geometry
        rather than letting a test assert it directly.
        """
        width, height = self.viewport
        return 0 <= x <= width and 0 <= y <= height

    def _aim(self) -> str:
        left, top, width, height = self.rect if self.rendered else (0.0, 0.0, 0.0, 0.0)
        point = (
            {"x": left + width / 2, "y": top + height / 2} if self.rendered else None
        )
        visible_point = point is not None and self._in_viewport(point["x"], point["y"])
        if not visible_point:
            hit: dict[str, Any] | None = None
        elif self.hit is None:
            hit = self._self_shape()
        else:
            tag, element_id, classes = self.hit
            hit = {"tag": tag, "id": element_id, "classes": list(classes)}
        return json.dumps(
            {
                "rendered": self.rendered,
                "rect": {
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                },
                "point": point,
                "target": self._self_shape(),
                "hit": hit,
                "hit_is_target": visible_point and self.hit is None,
                "disabled": self.disabled,
                "pointer_events": self.pointer_events,
                "visibility": self.visibility,
            }
        )

    # -- the nodriver Element surface the click path touches -----------------
    async def scroll_into_view(self) -> None:
        self.calls.append("scroll_into_view")

    async def mouse_click(self) -> None:
        self.calls.append("mouse_click")
        if self.mouse_click_error is not None:
            raise self.mouse_click_error

    async def click(self) -> None:
        self.calls.append("click")

    async def apply(self, js_function: str, *args: Any, **kwargs: Any) -> Any:
        self.apply_calls.append(js_function)
        if "elementFromPoint" in js_function:
            self.calls.append("aim")
            if self._aim_answer is not _UNSET:
                return self._aim_answer
            return self._aim()
        return None


class FakeSelect:
    """A nodriver ``Element`` double for a ``<select>``, PAGE-BACKED (F-877).

    ``FakeTextField``'s and ``FakeClickTarget``'s sibling, one control over. It
    models the DOM semantics the selection path depends on and nothing else:

    * an ``<option>`` has a ``value``, a ``text`` and a ``label`` and they can
      all differ (measured, Chrome 152: ``<option label="LabelOne">TextOne``
      answers ``.text == "TextOne"``, ``.label == "LabelOne"``);
    * ``.text`` is the **collapsed** text, not ``textContent`` (measured: an
      option written over three lines as ``\\n  Spaced   Out\\n`` answers
      ``.text == "Spaced Out"``), so this double takes the collapsed form
      directly and a test that wants the raw one is asking the wrong layer —
      the witness for the collapsing itself is a real Chrome
      (``tests/test_e2e_select_upload_verification.py``);
    * assigning ``selectedIndex`` fires **nothing** on its own. The ``input``
      and ``change`` pair, in that order and bubbling, is what Chrome's own
      typeahead produced for a real selection (measured), so this double
      records exactly what the code under test dispatched and a pin can assert
      the pair rather than trusting it;
    * the selection is a **SET**, and the spec's ``selectedIndex`` SETTER
      selects exactly the option it names and deselects every other. That is
      why the state here is a tuple and not a single int: a ``<select
      multiple>`` holding ``[0, 2]`` still answers ``selectedIndex == 0`` after
      index 0 is assigned, so a double that modelled the selection as one
      number could not express the write that silently drops option 2 while
      that number does not budge.

    The answer to both reads is COMPUTED from this object's own state, never
    supplied by a test, so no fixture here can quietly encode the bug.

    ``read_cap`` is the product's ``MAX_OPTIONS`` truncation, stated by the test
    rather than imported: the read answers at most that many options while
    ``option_count`` stays the control's true length, exactly as the real script
    does. ``None`` by default, so no pin that does not ask for it moves.

    ``on_selected`` is the page: a callable invoked right after the events are
    dispatched, with this double as its argument. It exists so a pin can model
    the page that resets the control inside its own ``change`` handler — which
    a read-back taken *before* the events could not see.
    """

    #: One ``<option>`` as ``(value, text)``, ``(value, text, label)`` or
    #: ``(value, text, label, disabled)``. ``label`` defaults to ``text``,
    #: which is what ``HTMLOptionElement.label`` does when the attribute is
    #: absent (measured).
    DEFAULT_OPTIONS = (("one", "Alpha"), ("two", "Beta"), ("three", "Gamma"))

    def __init__(  # noqa: PLR0913  PERMANENT(one field per modelled DOM fact)
        self,
        options: tuple[tuple[str, ...], ...] = DEFAULT_OPTIONS,
        selected_index: int = 0,
        multiple: bool = False,
        tag: str = "select",
        on_selected: Any = None,
        answer: Any = _UNSET,
        selected: tuple[int, ...] | None = None,
        read_cap: int | None = None,
    ) -> None:
        self.tag = tag
        self.multiple = multiple
        self.read_cap = read_cap
        self.options = [self._option(i, raw) for i, raw in enumerate(options)]
        #: ``selectedOptions``' indices, ascending. ``selected=`` states the set
        #: directly — the only way to express a real multi-selection — while
        #: ``selected_index=`` is the one-option shorthand every other pin uses.
        self.selected: list[int] = (
            sorted(selected)
            if selected is not None
            else ([] if selected_index < 0 else [selected_index])
        )
        self.on_selected = on_selected
        self._answer = answer
        self.events: list[str] = []
        self.apply_calls: list[str] = []
        self.keys_sent: list[str] = []

    async def send_keys(self, text: str) -> None:
        """nodriver's ``Element.send_keys`` — what the shipped ``text=`` arm
        called, and the reason a request for a ``<select disabled>`` moved a
        DIFFERENT select. It records and does nothing else: the double's job
        here is to make "nothing ever typed" assertable, not to re-implement
        Chrome's typeahead."""
        self.keys_sent.append(text)

    @staticmethod
    def _option(index: int, raw: tuple[str, ...]) -> dict[str, Any]:
        value, text = raw[0], raw[1]
        label = raw[2] if len(raw) > 2 else text
        disabled = bool(raw[3]) if len(raw) > 3 else False
        return {
            "index": index,
            "value": value,
            "text": text,
            "label": label,
            "disabled": disabled,
        }

    @property
    def selected_indexes(self) -> list[int]:
        """``selectedOptions``' indices."""
        return list(self.selected)

    @property
    def selected_index(self) -> int:
        """``HTMLSelectElement.selectedIndex`` — the FIRST selected option, or
        ``-1`` when none is, which is the state ``select.value = "nope"`` leaves
        behind (measured)."""
        return self.selected[0] if self.selected else -1

    @selected_index.setter
    def selected_index(self, index: int) -> None:
        """The spec's setter: selects exactly that option, deselects every other.

        On a multiple select holding ``[0, 2]``, assigning ``0`` leaves the
        GETTER answering ``0`` while the set shrinks to ``[0]`` — which is why
        "did anything move" can only be asked of the set.
        """
        self.selected = [] if index < 0 else [index]

    def _state(self) -> dict[str, Any]:
        return {
            "selected_index": self.selected_index,
            "selected_count": len(self.selected_indexes),
            "selected_indexes": list(self.selected_indexes),
            "option_count": len(self.options),
            "multiple": self.multiple,
        }

    def _read(self) -> str:
        if self.tag != "select":
            return json.dumps(
                {
                    "tag": self.tag,
                    "is_select": False,
                    "multiple": False,
                    "option_count": 0,
                    "selected_index": -1,
                    "selected_count": 0,
                    "selected_indexes": [],
                    "options": [],
                }
            )
        readable = (
            self.options if self.read_cap is None else self.options[: self.read_cap]
        )
        return json.dumps(
            dict(
                self._state(),
                tag=self.tag,
                is_select=True,
                options=[dict(o) for o in readable],
            )
        )

    def _apply(self, js_function: str) -> str:
        """Set ``selectedIndex``, then fire ``input`` and ``change``.

        The wanted ``{index, value}`` is read out of the script the product
        sent, because ``Element.apply`` carries no arguments — a criterion
        reaches the page embedded in the function source or not at all. The
        ``value`` is re-checked against the option that is there NOW, exactly as
        the product's own script does: the options can be replaced between the
        read and the write (a dependent dropdown repopulating), and an index
        resolved against the old list addresses a different option in the new
        one.
        """
        match = re.search(r"const want = (\{.*?\});", js_function, re.DOTALL)
        want = json.loads(match.group(1)) if match else {}
        index = want.get("index", -1)
        option = self.options[index] if 0 <= index < len(self.options) else None
        if option is None or option["value"] != want.get("value"):
            return json.dumps(dict(self._state(), applied=False, stale=True))
        before = self.selected_indexes
        self.selected_index = index
        if before != self.selected_indexes:
            # Chrome fires nothing when the selection lands where it already
            # was — measured: a typeahead query that resolves to the currently
            # selected option produces no ``change`` at all. The comparison is
            # of the SET, never of ``selectedIndex``: see that setter.
            self.events.extend(("input", "change"))
            if self.on_selected is not None:
                self.on_selected(self)
        return json.dumps(dict(self._state(), applied=True, stale=False))

    async def apply(self, js_function: str, *args: Any, **kwargs: Any) -> Any:
        self.apply_calls.append(js_function)
        if self._answer is not _UNSET:
            return self._answer
        if "dispatchEvent" in js_function:
            return self._apply(js_function)
        return self._read()


class FakeFileInput:
    """A nodriver ``Element`` double for an ``<input type="file">`` (F-877).

    One measured rule, and it is the whole of the upload defect: when the input
    has no ``multiple`` attribute and more than one path is sent,
    ``DOM.setFileInputFiles`` **succeeds** — the raw CDP call answers ``None``,
    no error anywhere — and Chrome keeps only the **first** file. Measured on
    Chrome 152 against a freshly loaded page: two paths in, ``files.length ==
    1``. A double that simply stored whatever it was handed could not express
    that, which is exactly the answer ``upload_file`` reported.

    ``size`` is ``len(name)`` — a stand-in, because a hermetic double has no
    file to stat. It exists so a pin can prove ``total_bytes`` is read from the
    ``FileList`` rather than composed from the request.
    """

    def __init__(
        self,
        multiple: bool = False,
        files: tuple[str, ...] = (),
        tag: str = "input",
        accepts: bool = True,
        answer: Any = _UNSET,
    ) -> None:
        self.multiple = multiple
        self.files = list(files)
        self.tag = tag
        self.accepts = accepts
        self.attrs = {"type": "file", "id": "upload"}
        self.tag_name = tag
        self._answer = answer
        self.sent: list[tuple[str, ...]] = []
        self.apply_calls: list[str] = []

    async def send_file(self, *paths: str) -> None:
        """``DOM.setFileInputFiles``, with Chrome's measured truncation rule."""
        self.sent.append(tuple(paths))
        if not self.accepts:
            return
        names = [Path(str(p)).name for p in paths]
        self.files = names if self.multiple else names[:1]

    async def apply(self, js_function: str, *args: Any, **kwargs: Any) -> Any:
        self.apply_calls.append(js_function)
        if self._answer is not _UNSET:
            return self._answer
        return json.dumps(
            {
                "tag": self.tag,
                "has_files": self.tag == "input",
                "count": len(self.files),
                "multiple": self.multiple,
                "total_bytes": sum(len(n) for n in self.files),
            }
        )


class FakeDiscoveredTarget:
    """nodriver's raw ``Connection``, exactly as ``Browser.tabs`` yields it after
    a rediscovery (the F-771 shape).

    ``Browser.update_targets()`` appends a ``Connection`` — **not** a ``Tab`` —
    for every target it did not already know about, and ``Browser.tabs`` returns
    it anyway (it filters on ``type_ == "page"`` despite the ``List[Tab]``
    annotation). Two behaviours matter and both are modelled here:

    * **not awaitable** — only ``Tab`` defines ``__await__``, so awaiting this
      raises ``TypeError: object ... can't be used in 'await' expression``;
    * **attribute fall-through** — ``Connection.__getattr__`` delegates to
      ``self.target``, so ``.url``/``.title`` still resolve to REAL values. A
      listing that returns blank urls is therefore a product defect, not an
      unavoidable consequence of the object type.
    """

    def __init__(self, target: Any) -> None:
        self.target = target

    def __getattr__(self, item: str) -> Any:
        # ``self.__dict__`` (not ``self.target``) — attribute access inside a
        # ``__getattr__`` would recurse for anything not yet set.
        return getattr(self.__dict__["target"], item)


class FakeAttachedTab(FakeDiscoveredTarget):
    """nodriver's ``Tab``: a ``Connection`` that additionally defines
    ``__await__`` (which resolves to ``Tab.wait()``).

    Counts awaits in ``awaited`` so a test can pin that a metadata-only read
    pays no per-tab lifecycle wait.
    """

    def __init__(self, target: Any) -> None:
        super().__init__(target)
        self.awaited = 0

    def __await__(self) -> Any:
        async def _wait() -> None:
            self.awaited += 1

        return _wait().__await__()


# ---------------------------------------------------------------------------
# Fake browser + browser manager
# ---------------------------------------------------------------------------


class FakeBrowser:
    """A fake nodriver browser for the ``list_instances`` liveness path (F-611).

    ``_browser_process_is_alive`` inspects ``_process.poll()`` first, then falls
    back to ``_process_pid`` (psutil). Model the cases:

    * ``FakeBrowser(alive=True)``  → ``_process.poll()`` returns ``None`` (alive)
    * ``FakeBrowser(alive=False)`` → ``_process.poll()`` returns ``0`` (exited)
    * ``FakeBrowser(alive=None, pid=<int>)`` → no ``_process``; psutil pid path
    * ``FakeBrowser(alive=None)`` → no ``_process``, no pid → defaults to alive

    ``tabs`` seeds the target-listing seam (``list_tabs``/``switch_to_tab``/
    ``close_tab`` read it after ``update_targets()``); seed it with
    :class:`FakeAttachedTab` / :class:`FakeDiscoveredTarget`.

    ``connection`` is the BROWSER-level websocket (nodriver ``Browser.connection``,
    ``core/browser.py:437``) — the one every by-id Target-domain command travels
    over. It is a :class:`FakeTab`, so ``connection.send_calls`` /
    ``connection.cdp_frames`` record the command name AND its arguments.

    ``get(url, new_tab=True)`` appends the tab it creates to ``tabs`` and records
    the call in ``get_calls``, so a test can assert that a code path opened NO
    extra tab (the F-775a leak).

    ``main_tab`` is what an ATTACH hands back (F-888) — nodriver's
    ``Browser.main_tab``, which the adoption path reads to decide whether a
    re-attached browser is usable at all.
    """

    def __init__(
        self,
        alive: bool | None = True,
        pid: int | None = None,
        tabs: list[Any] | None = None,
        opened_tab: Any = None,
        update_targets_stalls: bool = False,
        main_tab: Any = None,
    ) -> None:
        if alive is None:
            self._process = None
        else:
            code = None if alive else 0
            self._process = SimpleNamespace(poll=lambda: code, returncode=code)
        self._process_pid = pid
        self.target = SimpleNamespace(url="https://fake.test/page")
        self.tabs = list(tabs or [])
        self.update_targets_calls = 0
        self.connection = FakeTab(url="ws://fake.test/devtools/browser")
        self.get_calls: list[tuple[str, bool]] = []
        self._opened_tab = opened_tab
        self._update_targets_stalls = update_targets_stalls
        # nodriver's ``Browser.main_tab`` — the tab an ATTACH hands back (F-888).
        # ``None`` unless a test seeds it, because a browser we connected to and
        # that reports no tab is a real case the adoption path must refuse rather
        # than register half an instance for.
        self.main_tab = main_tab

    async def get(self, url: str, new_tab: bool = False) -> FakeTab:
        """nodriver's ``Browser.get``.

        Seed ``opened_tab`` to say what the opened tab answers (e.g. a landing
        on a ``chrome-error://`` page); otherwise a plain tab at *url* is made.
        Either way the tab is stamped with its opener, so closing it removes it
        from ``tabs`` — the difference between "the failure path closed the tab"
        and "the failure path leaked it".
        """
        self.get_calls.append((url, new_tab))
        tab = self._opened_tab or FakeTab(
            url=url, target_id=f"T-opened-{len(self.get_calls)}"
        )
        if new_tab:
            tab.opened_by = self
            self.tabs.append(tab)
        return tab

    async def update_targets(self) -> None:
        """nodriver's target refresh. The real one rewrites every known
        ``target``'s metadata in place from a fresh ``Target.getTargets``, which
        is precisely why a metadata-listing loop has nothing left to await.

        Seed ``update_targets_stalls=True`` for a WEDGED browser: the refresh is
        a real CDP round trip, so a browser whose devtools websocket has stopped
        answering never completes it. That is the one case a listing over N
        instances must survive (F-874), and a fake that returned promptly could
        not express it.
        """
        self.update_targets_calls += 1
        if self._update_targets_stalls:
            await asyncio.Event().wait()


class FakeBrowserManager:
    """Seedable stand-in for the module-global ``browser_manager`` singleton.

    ``get_tab``/``get_browser``/``list_instances`` are async (the tools await
    them). ``list_instances`` returns the seeded instance objects verbatim; seed
    with :func:`fake_instance`.
    """

    def __init__(
        self,
        instances: list[Any] | None = None,
        tabs: dict[str, Any] | None = None,
        browsers: dict[str, Any] | None = None,
        spawn_instance: Any = None,
        spawn_diagnostics: dict[str, Any] | None = None,
        navigate_result: Any = None,
    ) -> None:
        self._instances = list(instances or [])
        self._tabs = dict(tabs or {})
        self._browsers = dict(browsers or {})
        self._spawn_instance = spawn_instance
        self._spawn_diagnostics = (
            spawn_diagnostics if spawn_diagnostics is not None else {}
        )
        self._navigate_result = navigate_result
        self.spawn_calls: list[Any] = []
        self.navigate_calls: list[dict[str, Any]] = []

    async def list_instances(self) -> list[Any]:
        return list(self._instances)

    async def get_tab(self, instance_id: str) -> Any:
        return self._tabs.get(instance_id)

    async def get_active_tab(self, instance_id: str) -> Any:
        """The real manager's ``get_active_tab`` is ``get_tab`` under another
        name (``browser_manager.py``: "the instance's stored active tab"), so the
        double delegates rather than growing a second seedable store that could
        disagree with ``tabs=``."""
        return self._tabs.get(instance_id)

    async def get_browser(self, instance_id: str) -> Any:
        return self._browsers.get(instance_id)

    async def spawn_browser(self, options: Any) -> Any:
        """Record the ``BrowserOptions`` the tool built (to assert param
        forwarding) and return the seeded fake instance."""
        self.spawn_calls.append(options)
        if self._spawn_instance is None:
            raise AssertionError(
                "seed spawn_instance to use FakeBrowserManager.spawn_browser"
            )
        return self._spawn_instance

    async def navigate(self, **kwargs: Any) -> Any:
        """Record the navigation the tool requested and return the seeded
        result verbatim — the seam for pinning what ``navigate`` does with a
        manager payload it cannot influence (e.g. an error-page URL, F-802)."""
        self.navigate_calls.append(kwargs)
        if self._navigate_result is None:
            raise AssertionError(
                "seed navigate_result to use FakeBrowserManager.navigate"
            )
        return self._navigate_result

    async def get_spawn_diagnostics(self, instance_id: str) -> dict[str, Any]:
        return dict(self._spawn_diagnostics)


def fake_instance(
    instance_id: str = "i1",
    state: str = "active",
    last_navigated_url: str = "https://fake.test/page",
    last_navigated_title: str = "Fake Page",
) -> SimpleNamespace:
    """A minimal instance object with the attributes ``list_instances`` reads.

    The pair is named as ``BrowserInstance`` names it (F-874): it is what the
    last navigation reported, NOT what the instance is showing. Where a test
    wants the live answer it seeds a tab through ``tabs=``/``browsers=``, which
    is the only thing that can carry one.
    """
    return SimpleNamespace(
        instance_id=instance_id,
        state=state,
        last_navigated_url=last_navigated_url,
        last_navigated_title=last_navigated_title,
    )


# ---------------------------------------------------------------------------
# Fake in-memory storage (mirrors the real singleton's public surface)
# ---------------------------------------------------------------------------


class FakeStorage:
    """In-memory double for ``in_memory_storage``.

    Mirrors the real public API (``get``/``set``/``list_instances``/``clear_all``/
    ``remove_instance``/``get_instance``/``store_instance``) so it can stand in
    for the shared singleton without mutating real cross-test state.
    """

    def __init__(
        self,
        instances: dict[str, Any] | None = None,
        kv: dict[str, Any] | None = None,
    ) -> None:
        self._instances = dict(instances or {})
        self._kv = dict(kv or {})

    def list_instances(self) -> dict[str, Any]:
        return {"instances": dict(self._instances)}

    def get(self, key: str, default: Any = None) -> Any:
        return self._kv.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._kv[key] = value

    def clear_all(self) -> None:
        self._instances.clear()
        self._kv.clear()

    def remove_instance(self, instance_id: str) -> None:
        del self._instances[instance_id]

    def get_instance(self, instance_id: str) -> Any:
        return self._instances.get(instance_id)

    def store_instance(self, instance_id: str, data: Any) -> None:
        self._instances[instance_id] = data


# ---------------------------------------------------------------------------
# Golden normalisation (one documented home for the volatile-field policy)
# ---------------------------------------------------------------------------

# Keys whose VALUES are non-deterministic across runs/machines (wall-clock time,
# absolute paths, random ids). A golden that embedded a real one of these would
# be a flake/portability bug, so both capture and compare replace the value with
# a fixed ``<KEY>`` sentinel. The set is passed per-call because e.g. a seeded
# progressive store uses a FIXED element_id (deterministic — do not normalise).
DEFAULT_VOLATILE_KEYS: tuple[str, ...] = ("timestamp", "file_path")


def normalize_golden(
    obj: Any, volatile_keys: tuple[str, ...] = DEFAULT_VOLATILE_KEYS
) -> Any:
    """Recursively replace volatile dict values with ``<KEY>`` sentinels.

    Applied identically at capture and compare time (see
    :func:`load_or_capture_golden`). Non-dict/list scalars pass through.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key in volatile_keys:
                out[key] = f"<{key.upper()}>"
            else:
                out[key] = normalize_golden(value, volatile_keys)
        return out
    if isinstance(obj, list):
        return [normalize_golden(item, volatile_keys) for item in obj]
    return obj


def as_jsonable(obj: Any) -> Any:
    """Round-trip through JSON so tuples/SimpleNamespace/etc. compare equal to a
    loaded golden (tuples become lists, non-serialisable objects become str)."""
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


def load_or_capture_golden(path: Any, obj: Any) -> Any:
    """Load the committed golden at ``path``, or capture ``obj`` as the golden on
    first run (when the file does not yet exist).

    Characterization goldens are *defined* by the current tree, so the first
    capture is authoritative; thereafter the committed file is the reference an
    intentional M5a/M5b change updates via a reviewed diff. ``obj`` must already
    be normalised + jsonable so capture and compare are byte-consistent.
    """
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Chrome's process-singleton artefacts (F-871)
# ---------------------------------------------------------------------------
# ONE home for writing what Chrome writes into a profile directory, because a
# test that hand-rolls a `SingletonLock` will hand-roll a WRONG one: Chrome's
# lock is a SYMLINK whose target is the string `<hostname>-<pid>` -- it is a
# claim about a pid, not a file that exists -- and `embedded/profile_lock.py`
# reads it the way Chromium's `ParseProcessSingletonLock` does.


def write_singleton(
    profile_dir: Path, content: str, name: str = "SingletonLock"
) -> Path:
    """Write one singleton artefact as a symlink, or as a plain file where the
    platform refuses one (creating a symlink is privileged on Windows).
    ``profile_lock`` reads both forms, so a test means the same thing on either.
    """
    path = profile_dir / name
    try:
        path.symlink_to(content)
    except (OSError, NotImplementedError):
        path.write_text(content, encoding="utf-8")
    return path


def held_profile(profile_dir: Path) -> Path:
    """Make *profile_dir* look like one a LIVE browser is holding, by naming a
    pid that is certainly running: this test's own."""
    return write_singleton(profile_dir, f"{socket.gethostname()}-{os.getpid()}")
