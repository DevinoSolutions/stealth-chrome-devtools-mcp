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
from typing import Any

import nodriver.cdp.dom as cdp_dom
import nodriver.cdp.runtime as cdp_runtime
import nodriver.cdp.target as cdp_target

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
    """

    def __init__(
        self,
        url: str = "https://fake.test/page",
        evaluate_result: Any = None,
        evaluate_map: dict[str, Any] | None = None,
        cdp_responses: dict[str, Any] | None = None,
        select_result: Any = None,
        target_id: str = "T-faketab",
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

    def __await__(self) -> Any:
        """nodriver's ``Tab.__await__`` (→ ``Tab.wait()``). Only ``Tab`` defines
        it — see :class:`FakeDiscoveredTarget`, which deliberately does not."""

        async def _wait() -> None:
            self.awaited += 1

        return _wait().__await__()

    def _answer_for_js(self, expression: str) -> Any:
        """The canned answer for a JS expression — ONE home for it.

        Both eval seams route here: ``evaluate()`` (nodriver's helper) and
        ``send(cdp.runtime.evaluate(...))`` (the raw command ``execute_script``
        uses since F-832). A test says "this JS answers with X" once, whichever
        seam the code under test happens to take.
        """
        for needle, resp in self._evaluate_map.items():
            if needle in expression:
                return resp
        return self._evaluate_result

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        self.evaluate_calls.append(expression)
        return self._answer_for_js(expression)

    async def get(self, url: str, *args: Any, **kwargs: Any) -> FakeTab:
        """nodriver's ``Tab.get`` — the navigation seam.

        Updates ``.url`` the way a real navigation does, so a caller that reads
        the tab back after navigating sees where it went. Returns ``self``, as
        ``Tab.get`` returns the tab it navigated.
        """
        self.get_calls.append(url)
        self.url = url
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

    async def select(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """The nodriver element-resolution seam used by the CDP styles path and
        ``clone_element_complete``. Returns the configured ``select_result``
        (e.g. a ``node_id``-carrying element), or ``None`` for the not-found path.
        """
        self.select_calls.append(selector)
        return self._select_result

    def add_handler(self, event_type: Any, handler: Any) -> None:
        """The nodriver CDP-event subscription seam (``Fetch.RequestPaused``, …).

        Records the (event_type, handler) pair so a test can assert *how many*
        handlers a code path registered, not just that it sent a command.
        """
        self.handlers.append((event_type, handler))

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
            if answer is None or isinstance(answer, tuple):
                return answer
            return js_result(answer)
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
      sees ``smooth_steps``-th of the way, the measured F-875 shortfall.

    Nothing here is written from the defect: the scripts are interpreted as
    Chrome interprets them, and the read answers with the geometry the page
    would report.
    """

    #: The prefix that identifies the product's position-read script and no
    #: other JS: ``scroll_position.READ_JS`` is the only expression ``scroll_page``
    #: evaluates that is a ``JSON.stringify`` (the scroll scripts NAME
    #: ``document.scrollingElement`` too, so the element is not the marker).
    #: Named once, here, like :data:`ANIMATION_JS_MARKER`.
    POSITION_JS_MARKER = "JSON.stringify("

    #: ``window.scrollBy({top: N, left: M, …})`` — the two deltas, signed.
    _BY = re.compile(r"window\.scrollBy\(\{top:\s*(-?\d+),\s*left:\s*(-?\d+)")
    #: ``window.scrollTo({top: <expr>, left: N, …})`` — the vertical target as
    #: written; any ``…scrollHeight`` in it means "the bottom" (the product
    #: targets ``document.scrollingElement``, so the expression is not a fixed
    #: string and only the property it ends in is matched on).
    _TO = re.compile(r"window\.scrollTo\(\{top:\s*([^,]+),\s*left:\s*(-?\d+)")

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
        #: A page whose content keeps arriving never stops moving — the
        #: budget-exhaustion case. One pixel per read is enough to model it.
        self.never_settles = never_settles
        #: Pixels of content appended per read WITHOUT the viewport moving: the
        #: lazy-loading page that has stopped scrolling but is still filling in.
        #: Its offset is stable and its EXTENT is not, which is the one shape
        #: that tells an offset comparison from a whole-``Position`` one.
        self.growing_content = growing_content
        self._flight: tuple[int, int] | None = None
        #: Every position read, in order — so a test can count round trips.
        self.position_reads: list[str] = []

    @property
    def max_scroll_y(self) -> int:
        return max(0, self.doc_height - self.viewport_height)

    @property
    def max_scroll_x(self) -> int:
        return max(0, self.doc_width - self.viewport_width)

    def _clamped(self, x: int, y: int) -> tuple[int, int]:
        return (
            max(0, min(int(x), self.max_scroll_x)),
            max(0, min(int(y), self.max_scroll_y)),
        )

    def _target_of(self, expression: str) -> tuple[int, int] | None:
        """The (x, y) a scroll script asks for, or ``None`` if it is not one."""
        by = self._BY.search(expression)
        if by is not None:
            return self._clamped(
                self.scroll_x + int(by.group(2)), self.scroll_y + int(by.group(1))
            )
        to = self._TO.search(expression)
        if to is None:
            return None
        top = to.group(1).strip()
        y = self.doc_height if "scrollHeight" in top else int(top)
        return self._clamped(int(to.group(2)), y)

    def _advance(self) -> None:
        """One animation frame's worth of movement, charged per read."""
        if self.growing_content:
            self.doc_height += self.growing_content
        if self.never_settles:
            self.scroll_y = min(self.scroll_y + 1, self.max_scroll_y)
            self.doc_height += 1  # the content that keeps arriving
            return
        if self._flight is None:
            return
        target_x, target_y = self._flight
        step_x = -(-abs(target_x - self.scroll_x) // self.smooth_steps)
        step_y = -(-abs(target_y - self.scroll_y) // self.smooth_steps)
        self.scroll_x += min(step_x, abs(target_x - self.scroll_x)) * (
            1 if target_x >= self.scroll_x else -1
        )
        self.scroll_y += min(step_y, abs(target_y - self.scroll_y)) * (
            1 if target_y >= self.scroll_y else -1
        )
        if (self.scroll_x, self.scroll_y) == self._flight:
            self._flight = None

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        self.evaluate_calls.append(expression)
        if expression.startswith(self.POSITION_JS_MARKER):
            self.position_reads.append(expression)
            self._advance()
            return json.dumps(
                {
                    "x": self.scroll_x,
                    "y": self.scroll_y,
                    "max_x": self.max_scroll_x,
                    "max_y": self.max_scroll_y,
                }
            )
        target = self._target_of(expression)
        if target is None:
            return self._answer_for_js(expression)
        if "'smooth'" in expression:
            self._flight = None if target == (self.scroll_x, self.scroll_y) else target
        else:
            self.scroll_x, self.scroll_y = target
            self._flight = None
        return None


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
        if not self.accepts:
            return
        if text in ("\r", "\n") and not (self.multiline or self.content_editable):
            return
        self.value += "\n" if text == "\r" else text


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
    """

    def __init__(
        self,
        alive: bool | None = True,
        pid: int | None = None,
        tabs: list[Any] | None = None,
        opened_tab: Any = None,
        update_targets_stalls: bool = False,
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
