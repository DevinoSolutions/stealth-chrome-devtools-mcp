"""RELEASE-FIX-A C6 (A4) + T-6206 (F-L): hermetic proof of the JS→Python binding
round-trip and of WHO may use it.

The old ``create_python_binding`` returned ``success: true`` while wiring
nothing — it overrode ``window[name]`` and called ``chrome.runtime.sendMessage``
(an extension API), and no ``Runtime.bindingCalled`` handler ever existed. These
tests prove the genuine round-trip WITHOUT a browser: driving the handlers with
real ``BindingCalled`` / ``ExecutionContextDestroyed`` events must run the
registered Python function and dispatch a matching ``<name>_response_<callId>``
event back into the page.

T-6206 (F-L): the binding runs host Python and ``Runtime.addBinding`` is visible
to every frame of the tab for as long as the tab lives, so a binding answers
only the execution context that was current when it was created, on the tab it
was created on, and goes away with that context. The pins below are the refusal
cases: a foreign execution context, another tab or instance, the binding after
its page navigated.

The live-Chrome end-to-end proof (JS-visible return value == Python's return) is
``tests/test_e2e_functions_hooks.py::test_python_binding_is_callable_from_javascript``.
"""

from __future__ import annotations

import asyncio
import collections
import json
import types
from typing import Any

import pytest
from nodriver import cdp

from stealth_chrome_devtools_mcp.embedded import python_binding
from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import (
    CDPFunctionExecutor,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

runtime = cdp.runtime
page = cdp.page
CREATING_CONTEXT = 7
CREATING_UNIQUE = "unique-7"


def _event(name: str, call_id: str, args: list[Any], context: int) -> Any:
    """A real ``Runtime.bindingCalled`` event, as Chrome sends it."""
    return runtime.BindingCalled(
        name=name,
        payload=json.dumps({"callId": call_id, "args": args}),
        execution_context_id=runtime.ExecutionContextId(context),
    )


def _created(
    context: int,
    unique: str | None = None,
    frame: str = "T-a",
    is_default: bool = True,
) -> Any:
    """``Runtime.executionContextCreated``, parsed by nodriver from the wire shape."""
    return runtime.ExecutionContextCreated.from_json(
        {
            "context": {
                "id": context,
                "origin": "https://page.example",
                "name": "",
                "uniqueId": unique or f"unique-{context}",
                "auxData": {
                    "isDefault": is_default,
                    "type": "default" if is_default else "isolated",
                    "frameId": frame,
                },
            }
        }
    )


def _destroyed(context: int, unique: str | None = None) -> Any:
    return runtime.ExecutionContextDestroyed.from_json(
        {
            "executionContextId": context,
            "executionContextUniqueId": unique or f"unique-{context}",
        }
    )


def _navigated(frame: str = "T-a", parent: str | None = None) -> Any:
    """``Page.frameNavigated``; a frame with a ``parent`` is a child frame."""
    frame_json = {
        "id": frame,
        "loaderId": "loader-1",
        "url": "https://page.example/",
        "domainAndRegistry": "page.example",
        "securityOrigin": "https://page.example",
        "mimeType": "text/html",
        "secureContextType": "Secure",
        "crossOriginIsolatedContextType": "NotIsolated",
        "gatedAPIFeatures": [],
    }
    if parent is not None:
        frame_json["parentId"] = parent
    return page.FrameNavigated.from_json({"frame": frame_json, "type": "Navigation"})


class _FakeConnection:
    """What nodriver's ``Connection`` keeps: ``handlers`` is a dict of event
    type -> list of callbacks, and ``add_handler`` appends to the type's list."""

    def __init__(self) -> None:
        self.handlers: dict[type, list[Any]] = collections.defaultdict(list)
        self.closed = False  # nodriver's ``Connection.closed``: the socket is shut

    def add_handler(self, event_type, handler):
        self.handlers[event_type].append(handler)

    def handler_count(self) -> int:
        return sum(len(callbacks) for callbacks in self.handlers.values())

    async def deliver(self, event: Any) -> None:
        """Hand *event* to every handler registered for its type, the way
        nodriver's listener does (a plain call), and let what they scheduled
        finish."""
        # The LIVE list, as nodriver's listener walks it: a handler that removes
        # itself during dispatch makes the next one be skipped.
        for handler in self.handlers[type(event)]:
            outcome = handler(event)
            if asyncio.iscoroutine(outcome) or isinstance(outcome, asyncio.Task):
                await outcome
        for _ in range(3):
            await asyncio.sleep(0)


class _FakeTab(_FakeConnection):
    """Answers the CDP commands a binding install and a call issue.

    nodriver CDP commands are generators that yield a ``{"method", "params"}``
    dict on first advance — ``send`` records that frame and answers by method.
    The page's contexts are reported the way Chrome reports them: as
    ``executionContextCreated`` events, replayed only by a ``Runtime.enable``
    that follows a ``Runtime.disable`` (an enable of an enabled domain does
    nothing). No command answer carries a context id: the page's execution
    context is only ever learned from those events.

    ``on_send[method]`` runs while that command is in flight, before it is
    answered — where a page navigates under a call.
    """

    def __init__(
        self,
        target_id: str = "T-a",
        context: int | None = CREATING_CONTEXT,
        browser: Any = None,
    ):
        super().__init__()
        self.browser = browser if browser is not None else _FakeBrowserOf()
        self.target = types.SimpleNamespace(target_id=target_id)
        self.main: tuple[int, str] | None = (
            None if context is None else (context, f"unique-{context}")
        )
        self.frames: list[dict[str, Any]] = []
        self.wrapper_value: Any = {"success": True}
        self.runtime_enabled = True
        self.on_send: dict[str, Any] = {}

    async def send(self, cdp_command):
        frame = next(cdp_command)
        cdp_command.close()
        self.frames.append(frame)
        method = frame["method"]
        result: Any = None
        if method == "Runtime.disable":
            self.runtime_enabled = False
        elif method == "Runtime.enable" and not self.runtime_enabled:
            self.runtime_enabled = True
            if self.main is not None:
                await self.deliver(_created(*self.main, frame=self.target.target_id))
        elif method == "Runtime.evaluate":
            result = self._evaluate(frame["params"])
        if method in self.on_send:
            await self.on_send[method]()
        return result

    def _evaluate(self, params: dict[str, Any]) -> Any:
        if "window.__" in params["expression"]:  # the page-side wrapper
            unique = params.get("uniqueContextId")
            if unique is not None and (self.main is None or unique != self.main[1]):
                raise RuntimeError("Cannot find context with specified id")
            return (
                runtime.RemoteObject(type_="object", value=self.wrapper_value),
                None,
            )
        return (runtime.RemoteObject(type_="undefined"), None)

    def methods(self) -> list[str]:
        return [f["method"] for f in self.frames]

    def dispatched(self) -> list[str]:
        """Expressions of the response events dispatched into the page."""
        return [
            f["params"]["expression"]
            for f in self.frames
            if f["method"] == "Runtime.evaluate"
            and f["params"]["expression"].startswith("window.dispatchEvent")
        ]

    async def navigate(self, context: int, unique: str | None = None) -> None:
        """The page loads another document: its context ends and a new one starts."""
        old = self.main
        self.main = (context, unique or f"unique-{context}")
        if old is not None:
            await self.deliver(_destroyed(*old))
        await self.deliver(_navigated(self.target.target_id))
        await self.deliver(_created(*self.main, frame=self.target.target_id))


class _FakeBrowserOf:
    """The browser a tab reports: its own ``connection`` is where Chrome's
    ``Target.targetDestroyed`` arrives."""

    def __init__(self) -> None:
        self.connection = _FakeConnection()

    async def destroy(self, target_id: str) -> None:
        await self.connection.deliver(
            cdp.target.TargetDestroyed(target_id=cdp.target.TargetID(target_id))
        )


def _detail_of(expression: str) -> dict[str, Any]:
    return json.loads(expression.split("detail: ", 1)[1].rsplit("}))", 1)[0])


@pytest.fixture
def warnings(monkeypatch):
    """Every ``log_warning`` message, captured."""
    seen: list[str] = []
    monkeypatch.setattr(
        debug_logger, "log_warning", lambda c, m, message, *a, **k: seen.append(message)
    )
    return seen


async def _install(tab, name, function, bindings):
    return await python_binding.install_binding(tab, name, bindings, function)


def test_wrapper_uses_cdp_binding_not_sendmessage():
    """The wrapper must route through the CDP binding channel (a single JSON
    string), NOT the bogus ``chrome.runtime.sendMessage`` the old code used."""
    script = python_binding.build_wrapper_script("fixtureBinding")
    assert "chrome.runtime.sendMessage" not in script
    assert "JSON.stringify" in script
    # It preserves and calls the raw addBinding-created function.
    assert "__fixtureBinding_binding" in script
    assert "fixtureBinding_response_" in script


async def test_creating_context_call_runs_python_and_dispatches_response():
    """A call from the context the binding was created in runs the Python fn
    with the decoded args and dispatches ``<name>_response_<callId>`` carrying
    the result."""
    calls = []

    def add(a, b):
        calls.append((a, b))
        return a + b

    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "add", add, bindings)

    await tab.deliver(_event("add", "x", [2, 3], CREATING_CONTEXT))

    assert calls == [(2, 3)]
    (expr,) = tab.dispatched()
    assert "add_response_x" in expr
    detail = _detail_of(expr)
    assert detail["success"] is True
    assert detail["result"] == 5


async def test_foreign_execution_context_is_refused_and_python_never_runs(warnings):
    """A cross-origin iframe, another frame or a later document calls the same
    ``window[name]`` — Chrome reports a DIFFERENT executionContextId. The call
    is dropped: the Python never runs, nothing is dispatched, and the WARNING
    names the contexts but not what the call carried."""
    ran = []
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "steal", lambda *a: ran.append(a), bindings)
    frames_before = len(tab.frames)

    await tab.deliver(_event("steal", "c1", ["s3cret-token"], CREATING_CONTEXT + 1))

    assert ran == []
    assert len(tab.frames) == frames_before  # no dispatch back into the page
    assert len(warnings) == 1
    assert str(CREATING_CONTEXT + 1) in warnings[0]
    assert str(CREATING_CONTEXT) in warnings[0]
    assert "s3cret-token" not in warnings[0]
    assert ("T-a", "steal") in bindings  # a refusal does not unregister


async def test_refusal_is_logged_once_per_calling_context(warnings):
    """A hostile page can mint contexts at will; one WARNING per context, and a
    bounded number of them, keeps the log from being its to flood."""
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "b", lambda *a: None, bindings)

    for _ in range(3):
        await tab.deliver(_event("b", "c", [], 100))
    assert len(warnings) == 1
    for context in range(200, 300):
        await tab.deliver(_event("b", "c", [], context))
    assert len(warnings) == python_binding._MAX_LOGGED_REFUSALS


async def test_binding_is_per_tab_and_another_tab_cannot_reach_it(warnings):
    """Two tabs (here two instances) create the SAME binding name. The registry
    keeps both — the second no longer overwrites the first — each handler runs
    only its own function, and a call carrying the OTHER tab's context is
    refused."""
    ran = []
    bindings = python_binding.Bindings()
    tab_a = _FakeTab("T-a", context=7)
    tab_b = _FakeTab("T-b", context=21)
    await _install(tab_a, "helper", lambda: ran.append("a"), bindings)
    await _install(tab_b, "helper", lambda: ran.append("b"), bindings)

    assert set(bindings) == {("T-a", "helper"), ("T-b", "helper")}

    await tab_a.deliver(_event("helper", "1", [], 21))  # B's context, A's binding
    assert ran == []
    await tab_a.deliver(_event("helper", "2", [], 7))
    await tab_b.deliver(_event("helper", "3", [], 21))
    assert ran == ["a", "b"]


async def test_navigation_drops_the_binding(warnings):
    """The creating context is destroyed (any navigation or reload): the binding
    leaves the registry, ``Runtime.removeBinding`` is sent, and a later call —
    from the old context id or the new document's — runs nothing."""
    ran = []
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "nav", lambda: ran.append(1), bindings)

    await tab.deliver(_destroyed(CREATING_CONTEXT))

    assert bindings == {}
    assert tab.methods().count("Runtime.removeBinding") == 1
    await tab.deliver(_event("nav", "1", [], CREATING_CONTEXT))
    await tab.deliver(_event("nav", "2", [], CREATING_CONTEXT + 5))
    assert ran == []


async def test_destroyed_event_naming_the_unique_id_drops_it():
    """The unique id is system-wide: a destroyed event carrying it ends the
    binding whatever integer id the event reports."""
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "uid", lambda: 1, bindings)

    await tab.deliver(_destroyed(CREATING_CONTEXT + 40, unique=CREATING_UNIQUE))

    assert bindings == {}


async def test_other_contexts_ending_do_not_drop_it():
    """An iframe's context going away is not the page's navigation."""
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "keep", lambda: 1, bindings)

    await tab.deliver(_destroyed(CREATING_CONTEXT + 1))

    assert ("T-a", "keep") in bindings
    assert "Runtime.removeBinding" not in tab.methods()


async def test_contexts_cleared_drops_the_binding():
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "clr", lambda: 1, bindings)

    await tab.deliver(runtime.ExecutionContextsCleared())

    assert bindings == {}
    assert "Runtime.removeBinding" in tab.methods()


async def test_recreating_a_name_replaces_it_and_runs_once():
    """Re-creating a name on the same tab leaves ONE live binding: the old one
    is dropped first, so a call runs the new function exactly once."""
    ran = []
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "same", lambda: ran.append("old"), bindings)
    await _install(tab, "same", lambda: ran.append("new"), bindings)

    await tab.deliver(_event("same", "1", [], CREATING_CONTEXT))

    assert ran == ["new"]
    assert set(bindings) == {("T-a", "same")}
    assert tab.methods().count("Runtime.removeBinding") == 1  # the old one


async def test_a_late_event_for_the_old_document_leaves_the_replacement_alone():
    """After a navigation the binding is made again for the new document; a late
    (repeated) destroyed event for the OLD document cannot unsubscribe it, and a
    call from the old context is refused."""
    ran = []
    bindings = python_binding.Bindings()
    tab = _FakeTab(context=7)
    await _install(tab, "same", lambda: ran.append("old"), bindings)
    await tab.navigate(9)
    assert bindings == {}
    await _install(tab, "same", lambda: ran.append("new"), bindings)
    removals = tab.methods().count("Runtime.removeBinding")

    await tab.deliver(_event("same", "1", [], 7))  # the old document: refused
    await tab.deliver(_event("same", "2", [], 9))
    await tab.deliver(_destroyed(7))  # late event for the old document

    assert ran == ["new"]
    assert ("T-a", "same") in bindings
    assert tab.methods().count("Runtime.removeBinding") == removals


async def test_no_execution_context_means_no_binding():
    """No default context for the main frame reported by Chrome fails closed:
    nothing is registered or exposed."""
    bindings = python_binding.Bindings()
    tab = _FakeTab(context=None)

    with pytest.raises(ToolError, match="execution context"):
        await _install(tab, "x", lambda: 1, bindings)

    assert bindings == {}
    assert "Runtime.addBinding" not in tab.methods()


async def test_the_context_comes_from_the_replayed_created_events():
    """The context is learned from ``executionContextCreated``, which Chrome
    replays only on a real enable: the tab's watch disables and re-enables
    Runtime (handlers already attached) BEFORE anything is exposed."""
    bindings = python_binding.Bindings()
    tab = _FakeTab(context=41)

    await _install(tab, "ctx", lambda: 1, bindings)

    methods = tab.methods()
    assert methods[:2] == ["Runtime.disable", "Runtime.enable"]
    assert methods.index("Runtime.enable") < methods.index("Runtime.addBinding")
    bound = bindings[("T-a", "ctx")]
    assert (bound.context_id, bound.unique_id) == (41, "unique-41")


async def test_only_the_main_frames_default_context_is_taken():
    """An isolated world of the main frame and a default context of a child frame
    are not the page's own script: with only those on offer there is no binding,
    and a later main default context is the one taken."""
    bindings = python_binding.Bindings()
    tab = _FakeTab(context=None)

    async def create_noise():
        await tab.deliver(_created(50, frame="T-a", is_default=False))
        await tab.deliver(_created(51, frame="child-frame"))

    tab.on_send["Runtime.enable"] = create_noise
    with pytest.raises(ToolError, match="execution context"):
        await _install(tab, "iso", lambda: 1, bindings)
    assert bindings == {}

    tab.main = (52, "unique-52")
    await tab.deliver(_created(52))  # the main frame's default context
    await _install(tab, "iso", lambda: 1, bindings)
    assert bindings[("T-a", "iso")].context_id == 52


async def test_a_context_with_the_same_id_and_another_unique_id_drops_it():
    """Ids are per-isolate counters: another process's context reuses the
    integer. Its ``uniqueId`` differs, so the binding is dropped even though no
    destroyed event ever arrived — and the integer no longer matches anything."""
    ran = []
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "dup", lambda: ran.append(1), bindings)

    await tab.deliver(_created(CREATING_CONTEXT, unique="another-process", frame="x"))

    assert bindings == {}
    assert tab.methods().count("Runtime.removeBinding") == 1
    await tab.deliver(_event("dup", "1", [], CREATING_CONTEXT))
    assert ran == []


async def test_the_same_context_reported_again_does_not_drop_it():
    """The replay (or a repeat) of the binding's own context — same id, same
    uniqueId — is not another document."""
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "keep", lambda: 1, bindings)

    await tab.deliver(_created(CREATING_CONTEXT, CREATING_UNIQUE))
    await tab.deliver(_created(CREATING_CONTEXT + 1, frame="child-frame"))

    assert ("T-a", "keep") in bindings


async def test_main_frame_navigation_drops_it_but_a_child_frames_does_not():
    """``Page.frameNavigated`` of the main frame ends the document even if no
    context event is seen; a child frame navigating is not the page's."""
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "nv", lambda: 1, bindings)

    await tab.deliver(_navigated("child-frame", parent="T-a"))
    assert ("T-a", "nv") in bindings

    await tab.deliver(_navigated("T-a"))
    assert bindings == {}
    assert tab.methods().count("Runtime.removeBinding") == 1


async def test_a_navigation_between_the_read_and_addbinding_fails_closed():
    """The page navigates while ``Runtime.addBinding`` is in flight: the context
    read a moment ago is gone. The creation raises, nothing stays registered, and
    ``Runtime.removeBinding`` takes the binding back out of the page."""
    bindings = python_binding.Bindings()
    tab = _FakeTab(context=7)

    async def navigate_now():
        await tab.navigate(9)

    tab.on_send["Runtime.addBinding"] = navigate_now
    with pytest.raises(ToolError, match="page changed"):
        await _install(tab, "race", lambda: 1, bindings)
    await asyncio.sleep(0)

    assert bindings == {}
    assert "Runtime.removeBinding" in tab.methods()
    assert not any(
        "window.__race_binding" in f.get("params", {}).get("expression", "")
        for f in tab.frames
    )  # the wrapper was never injected


async def test_a_navigation_during_the_wrapper_injection_fails_closed():
    """The same, after the binding was added: the wrapper must not be reported
    installed for a page that is no longer the one the binding answers."""
    bindings = python_binding.Bindings()
    tab = _FakeTab(context=7)

    async def navigate_now():
        await tab.navigate(9)

    tab.on_send["Runtime.evaluate"] = navigate_now
    with pytest.raises(ToolError):
        await _install(tab, "race2", lambda: 1, bindings)
    await asyncio.sleep(0)

    assert bindings == {}
    assert "Runtime.removeBinding" in tab.methods()


async def test_one_handler_set_per_tab_however_many_bindings():
    """Every create used to add three more handlers to the tab for its whole
    life; the tab's one watch dispatches by registry lookup instead, so the
    count is fixed — and each binding still reaches only its own function."""
    ran = []
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "first", lambda: ran.append("first"), bindings)
    baseline = tab.handler_count()

    for i in range(10):
        await _install(tab, f"b{i}", lambda i=i: ran.append(i), bindings)
        await _install(tab, "first", lambda: ran.append("first"), bindings)

    assert tab.handler_count() == baseline == 5
    await tab.deliver(_event("b3", "1", [], CREATING_CONTEXT))
    await tab.deliver(_event("first", "2", [], CREATING_CONTEXT))
    assert ran == [3, "first"]


async def test_a_new_connection_to_the_same_target_gets_its_own_handlers():
    """A tab object that is not the one the watch was made for (a reconnect)
    cannot rely on its handlers or on bindings made on the old connection: the
    old ones are dropped and the new tab is watched."""
    bindings = python_binding.Bindings()
    old = _FakeTab()
    await _install(old, "x", lambda: 1, bindings)
    new = _FakeTab()

    await _install(new, "y", lambda: 1, bindings)

    assert set(bindings) == {("T-a", "y")}
    assert new.handler_count() == 5
    assert "Runtime.removeBinding" in new.methods()  # x, off the new connection


async def test_a_replaced_watch_stops_listening_on_the_old_tab():
    """The watch of a connection that was replaced is not left attached to the
    old tab (nor to its browser): the old tab no longer feeds it events, and a
    listener somebody else put on the same event type survives."""
    bindings = python_binding.Bindings()
    old = _FakeTab()
    foreign = []
    old.add_handler(runtime.BindingCalled, foreign.append)
    await _install(old, "x", lambda: 1, bindings)
    old_watch = bindings.watches["T-a"]
    assert old.handler_count() == 6
    assert old.browser.connection.handler_count() == 1
    new = _FakeTab()

    await _install(new, "y", lambda: 1, bindings)

    assert bindings.watches["T-a"] is not old_watch
    assert old.handlers[runtime.BindingCalled] == [foreign.append]
    assert old.handler_count() == 1  # the foreign listener only
    assert old.browser.connection.handler_count() == 0
    assert new.handler_count() == 5
    assert new.browser.connection.handler_count() == 1


async def test_a_destroyed_target_is_reaped_with_its_watch():
    """``Target.targetDestroyed`` ends the tab's watch: its bindings leave the
    registry, it leaves ``watches`` (and with it the reference to the Tab) and it
    takes its handlers off the tab and the browser's connection."""
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "x", lambda: 1, bindings)
    other = _FakeTab("T-b", context=21, browser=tab.browser)
    await _install(other, "x", lambda: 2, bindings)
    assert set(bindings.watches) == {"T-a", "T-b"}

    await tab.browser.destroy("T-a")

    assert set(bindings.watches) == {"T-b"}
    assert set(bindings) == {("T-b", "x")}
    assert tab.handler_count() == 0
    assert other.handler_count() == 5  # the other tab is untouched
    assert len(tab.browser.connection.handlers[cdp.target.TargetDestroyed]) == 1


async def test_reaping_a_watch_does_not_skip_the_handler_after_it():
    """The reap runs inside the browser connection's dispatch, which walks the
    live handler list. Two tabs on one browser, the first destroyed: a listener
    registered after the first watch's must still see the event, and so must the
    second watch (it ignores a foreign id, but it must be called)."""
    bindings = python_binding.Bindings()
    first = _FakeTab("T-a", context=7)
    await _install(first, "x", lambda: 1, bindings)
    seen = []
    first.browser.connection.add_handler(
        cdp.target.TargetDestroyed, lambda e: seen.append("sibling")
    )
    second = _FakeTab("T-b", context=21, browser=first.browser)
    await _install(second, "x", lambda: 2, bindings)

    await first.browser.destroy("T-a")

    assert seen == ["sibling"]
    assert set(bindings.watches) == {"T-b"}
    assert first.handler_count() == 0
    await first.browser.destroy("T-b")  # the second watch was still attached
    assert bindings.watches == {}


async def test_a_target_destroyed_during_install_leaves_no_watch_behind():
    """The tab closes while a replaced connection's stale bindings are being
    dropped: the new watch is already listening, reaps itself, and the install
    fails closed rather than leaving a watch for a dead tab."""
    bindings = python_binding.Bindings()
    old = _FakeTab()
    await _install(old, "x", lambda: 1, bindings)
    new = _FakeTab()

    async def tab_closes():
        await new.browser.destroy("T-a")

    new.on_send["Runtime.removeBinding"] = tab_closes
    with pytest.raises(ToolError, match="closed"):
        await _install(new, "y", lambda: 1, bindings)

    assert bindings.watches == {}
    assert bindings == {}
    assert new.handler_count() == 0


async def test_a_closed_browsers_watches_are_reaped_on_the_next_install():
    """``close_instance`` kills the browser: no ``targetDestroyed`` arrives for
    its tabs. The next install, on any tab, reaps the watches whose browser
    connection is closed, with their bindings and handlers."""
    bindings = python_binding.Bindings()
    dead = _FakeTab("T-a", context=7)
    await _install(dead, "x", lambda: 1, bindings)
    dead.browser.connection.closed = True  # the process was killed

    live = _FakeTab("T-b", context=21)
    await _install(live, "x", lambda: 2, bindings)

    assert set(bindings.watches) == {"T-b"}
    assert set(bindings) == {("T-b", "x")}
    assert dead.handler_count() == 0
    assert live.handler_count() == 5


async def test_a_target_destroyed_during_the_replay_fails_the_install_cleanly():
    bindings = python_binding.Bindings()
    tab = _FakeTab()

    async def tab_closes():
        await tab.browser.destroy("T-a")

    tab.on_send["Runtime.enable"] = tab_closes
    with pytest.raises(ToolError, match="closed"):
        await _install(tab, "x", lambda: 1, bindings)

    assert bindings.watches == {}
    assert bindings == {}


async def test_a_tab_destroyed_then_reopened_under_its_id_is_watched_afresh():
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "x", lambda: 1, bindings)
    await tab.browser.destroy("T-a")
    assert bindings.watches == {}

    again = _FakeTab()
    ran = []
    await _install(again, "x", lambda: ran.append(1), bindings)
    await again.deliver(_event("x", "c", [], CREATING_CONTEXT))

    assert ran == [1]
    assert bindings.watches["T-a"].tab is again


async def test_failed_install_leaves_nothing_registered():
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    tab.wrapper_value = None

    with pytest.raises(ToolError, match="Failed to install"):
        await _install(tab, "x", lambda: 1, bindings)

    assert bindings == {}
    assert "Runtime.removeBinding" in tab.methods()


async def test_binding_called_ignores_other_bindings_name():
    """A handler filtered to 'add' must ignore an event for a different binding
    (prevents cross-processing when several bindings live on one tab)."""
    ran = []
    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "add", lambda *a: ran.append(a), bindings)
    frames_before = len(tab.frames)

    await tab.deliver(_event("other", "y", [1], CREATING_CONTEXT))

    assert ran == []
    assert len(tab.frames) == frames_before


async def test_call_python_from_js_supports_coroutine_functions():
    """Async bound functions are awaited."""

    async def amul(a, b):
        return a * b

    bound = python_binding.BoundPython(
        "amul", amul, "T-a", CREATING_CONTEXT, CREATING_UNIQUE
    )
    outcome = await python_binding.call_python_from_js(bound, [4, 5])
    assert outcome["success"] is True
    assert outcome["result"] == 20


async def test_binding_result_error_is_dispatched_not_raised():
    """A Python function that raises yields an error response event (the page
    promise rejects), never an unhandled handler exception."""

    def boom():
        raise ValueError("kaboom")

    bindings = python_binding.Bindings()
    tab = _FakeTab()
    await _install(tab, "boom", boom, bindings)

    await tab.deliver(_event("boom", "z", [], CREATING_CONTEXT))

    (expr,) = tab.dispatched()
    detail = _detail_of(expr)
    assert detail["success"] is False
    assert "kaboom" in detail["error"]


async def test_executor_registry_is_per_tab_and_lists_names_once():
    """``get_function_executor_info`` still lists binding NAMES, once each, and
    the executor's registry no longer lets one tab's binding replace another's."""
    executor = CDPFunctionExecutor()
    tab_a = _FakeTab("T-a", context=7)
    tab_b = _FakeTab("T-b", context=21)

    await executor.create_python_binding(tab_a, "helper", lambda: "a")
    await executor.create_python_binding(tab_b, "helper", lambda: "b")
    await executor.create_python_binding(tab_b, "other", lambda: "o")

    assert set(executor._python_bindings) == {
        ("T-a", "helper"),
        ("T-b", "helper"),
        ("T-b", "other"),
    }
    info = await executor.get_function_executor_info()
    assert info["python_bindings"] == ["helper", "other"]
