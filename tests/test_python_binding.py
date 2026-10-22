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
CREATING_CONTEXT = 7


def _event(name: str, call_id: str, args: list[Any], context: int) -> Any:
    """A real ``Runtime.bindingCalled`` event, as Chrome sends it."""
    return runtime.BindingCalled(
        name=name,
        payload=json.dumps({"callId": call_id, "args": args}),
        execution_context_id=runtime.ExecutionContextId(context),
    )


def _destroyed(context: int) -> Any:
    return runtime.ExecutionContextDestroyed(
        execution_context_id=runtime.ExecutionContextId(context),
        execution_context_unique_id=f"unique-{context}",
    )


class _FakeTab:
    """Answers the CDP commands a binding install and a call issue.

    nodriver CDP commands are generators that yield a ``{"method", "params"}``
    dict on first advance — ``send`` records that frame and answers by method:
    a ``Runtime.evaluate`` of ``throw null`` with the exception details Chrome
    gives it (carrying the page's current execution context), the wrapper with
    its success value, anything else with nothing.
    """

    def __init__(self, target_id: str = "T-a", context: int | None = CREATING_CONTEXT):
        self.target = types.SimpleNamespace(target_id=target_id)
        self.context = context
        self.frames: list[dict[str, Any]] = []
        self.handlers: list[tuple[type, Any]] = []
        self.wrapper_value: Any = {"success": True}

    async def send(self, cdp_command):
        frame = next(cdp_command)
        cdp_command.close()
        self.frames.append(frame)
        if frame["method"] != "Runtime.evaluate":
            return None
        expression = frame["params"]["expression"]
        if expression == "throw null":
            context = (
                None
                if self.context is None
                else runtime.ExecutionContextId(self.context)
            )
            return (
                runtime.RemoteObject(type_="object"),
                runtime.ExceptionDetails(
                    exception_id=1,
                    text="Uncaught",
                    line_number=0,
                    column_number=0,
                    execution_context_id=context,
                ),
            )
        if "window.__" in expression:  # the page-side wrapper
            return (
                runtime.RemoteObject(type_="object", value=self.wrapper_value),
                None,
            )
        return (runtime.RemoteObject(type_="undefined"), None)

    def add_handler(self, event_type, handler):
        self.handlers.append((event_type, handler))

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

    async def deliver(self, event: Any) -> None:
        """Hand *event* to every handler registered for its type, the way
        nodriver's listener does, and let what they scheduled finish."""
        for event_type, handler in list(self.handlers):
            if event_type is not type(event):
                continue
            outcome = handler(event)
            if asyncio.iscoroutine(outcome) or isinstance(outcome, asyncio.Task):
                await outcome


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

    bindings: python_binding.Bindings = {}
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
    bindings: python_binding.Bindings = {}
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
    bindings: python_binding.Bindings = {}
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
    bindings: python_binding.Bindings = {}
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
    bindings: python_binding.Bindings = {}
    tab = _FakeTab()
    await _install(tab, "nav", lambda: ran.append(1), bindings)

    await tab.deliver(_destroyed(CREATING_CONTEXT))

    assert bindings == {}
    assert tab.methods().count("Runtime.removeBinding") == 1
    await tab.deliver(_event("nav", "1", [], CREATING_CONTEXT))
    await tab.deliver(_event("nav", "2", [], CREATING_CONTEXT + 5))
    assert ran == []


async def test_other_contexts_ending_do_not_drop_it():
    """An iframe's context going away is not the page's navigation."""
    bindings: python_binding.Bindings = {}
    tab = _FakeTab()
    await _install(tab, "keep", lambda: 1, bindings)

    await tab.deliver(_destroyed(CREATING_CONTEXT + 1))

    assert ("T-a", "keep") in bindings
    assert "Runtime.removeBinding" not in tab.methods()


async def test_contexts_cleared_drops_the_binding():
    bindings: python_binding.Bindings = {}
    tab = _FakeTab()
    await _install(tab, "clr", lambda: 1, bindings)

    await tab.deliver(runtime.ExecutionContextsCleared())

    assert bindings == {}
    assert "Runtime.removeBinding" in tab.methods()


async def test_recreating_a_name_replaces_it_and_runs_once():
    """Re-creating a name on the same tab (after a navigation, say) leaves ONE
    live binding: the old handlers stay on the tab but are inert, so a call runs
    the new function exactly once, and a late destroyed event for the OLD
    document cannot unsubscribe its replacement."""
    ran = []
    bindings: python_binding.Bindings = {}
    tab = _FakeTab(context=7)
    await _install(tab, "same", lambda: ran.append("old"), bindings)
    tab.context = 9
    await _install(tab, "same", lambda: ran.append("new"), bindings)
    removals = tab.methods().count("Runtime.removeBinding")

    await tab.deliver(_event("same", "1", [], 7))  # the old document: refused
    await tab.deliver(_event("same", "2", [], 9))
    await tab.deliver(_destroyed(7))  # late event for the old document

    assert ran == ["new"]
    assert ("T-a", "same") in bindings
    assert tab.methods().count("Runtime.removeBinding") == removals


async def test_no_execution_context_means_no_binding():
    """Chrome not naming the context fails closed: nothing is registered or
    exposed."""
    bindings: python_binding.Bindings = {}
    tab = _FakeTab(context=None)

    with pytest.raises(ToolError, match="execution context"):
        await _install(tab, "x", lambda: 1, bindings)

    assert bindings == {}
    assert "Runtime.addBinding" not in tab.methods()


async def test_failed_install_leaves_nothing_registered():
    bindings: python_binding.Bindings = {}
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
    bindings: python_binding.Bindings = {}
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

    bound = python_binding.BoundPython("amul", amul, "T-a", CREATING_CONTEXT)
    outcome = await python_binding.call_python_from_js(bound, [4, 5])
    assert outcome["success"] is True
    assert outcome["result"] == 20


async def test_binding_result_error_is_dispatched_not_raised():
    """A Python function that raises yields an error response event (the page
    promise rejects), never an unhandled handler exception."""

    def boom():
        raise ValueError("kaboom")

    bindings: python_binding.Bindings = {}
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
