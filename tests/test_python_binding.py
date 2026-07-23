"""RELEASE-FIX-A C6 (A4): hermetic proof of the JS→Python binding round-trip.

The old ``create_python_binding`` returned ``success: true`` while wiring
nothing — it overrode ``window[name]`` and called ``chrome.runtime.sendMessage``
(an extension API), and no ``Runtime.bindingCalled`` handler ever existed. These
tests prove the genuine round-trip WITHOUT a browser: driving ``on_binding_called``
with a synthetic ``BindingCalled`` event must run the registered Python function
and dispatch a matching ``<name>_response_<callId>`` event back into the page.

The live-Chrome end-to-end proof (JS-visible return value == Python's return) is
``tests/test_e2e_functions_hooks.py::test_python_binding_is_callable_from_javascript``.
"""

from __future__ import annotations

import json

from stealth_chrome_devtools_mcp.embedded import python_binding


class _FakeBindingCalled:
    """Minimal ``Runtime.bindingCalled`` double: name + JSON payload string."""

    def __init__(self, name: str, payload: str):
        self.name = name
        self.payload = payload


class _FakeTab:
    """Records the CDP command generators passed to ``send`` for inspection."""

    def __init__(self):
        self.sent = []

    async def send(self, cdp_command):
        self.sent.append(cdp_command)


def _expression_of(cdp_command) -> str:
    """Pull the ``expression`` param out of a Runtime.evaluate command generator.

    nodriver CDP commands are generators that yield a ``{"method", "params"}``
    dict on first advance — this reads that without a real connection.
    """
    request = next(cdp_command)
    return request["params"]["expression"]


def test_wrapper_uses_cdp_binding_not_sendmessage():
    """The wrapper must route through the CDP binding channel (a single JSON
    string), NOT the bogus ``chrome.runtime.sendMessage`` the old code used."""
    script = python_binding.build_wrapper_script("fixtureBinding")
    assert "chrome.runtime.sendMessage" not in script
    assert "JSON.stringify" in script
    # It preserves and calls the raw addBinding-created function.
    assert "__fixtureBinding_binding" in script
    assert "fixtureBinding_response_" in script


async def test_binding_called_invokes_python_and_dispatches_response():
    """A synthetic BindingCalled event runs the Python fn with the decoded args
    and dispatches ``<name>_response_<callId>`` carrying the result."""
    calls = []

    def add(a, b):
        calls.append((a, b))
        return a + b

    bindings = {"add": add}
    tab = _FakeTab()
    event = _FakeBindingCalled("add", json.dumps({"callId": "x", "args": [2, 3]}))

    await python_binding.on_binding_called(tab, event, "add", bindings)

    # (a) the Python function ran with the decoded args
    assert calls == [(2, 3)]
    # (b) exactly one dispatch happened, carrying the response event + result
    assert len(tab.sent) == 1
    expr = _expression_of(tab.sent[0])
    assert "add_response_x" in expr
    detail = json.loads(expr.split("detail: ", 1)[1].rsplit("}))", 1)[0])
    assert detail["success"] is True
    assert detail["result"] == 5


async def test_binding_called_ignores_other_bindings_name():
    """A handler filtered to 'add' must ignore an event for a different binding
    (prevents cross-processing when several bindings live on one tab)."""
    ran = []
    bindings = {"add": lambda *a: ran.append(a)}
    tab = _FakeTab()
    event = _FakeBindingCalled("other", json.dumps({"callId": "y", "args": [1]}))

    await python_binding.on_binding_called(tab, event, "add", bindings)

    assert ran == []
    assert tab.sent == []


async def test_call_python_from_js_reports_unknown_binding():
    """An unknown binding name is reported, not raised."""
    outcome = await python_binding.call_python_from_js({}, "missing", [])
    assert outcome == {"success": False, "error": "Unknown binding: missing"}


async def test_call_python_from_js_supports_coroutine_functions():
    """Async bound functions are awaited."""

    async def amul(a, b):
        return a * b

    outcome = await python_binding.call_python_from_js({"amul": amul}, "amul", [4, 5])
    assert outcome["success"] is True
    assert outcome["result"] == 20


async def test_binding_result_error_is_dispatched_not_raised():
    """A Python function that raises yields an error response event (the page
    promise rejects), never an unhandled handler exception."""

    def boom():
        raise ValueError("kaboom")

    tab = _FakeTab()
    event = _FakeBindingCalled("boom", json.dumps({"callId": "z", "args": []}))
    await python_binding.on_binding_called(tab, event, "boom", {"boom": boom})

    assert len(tab.sent) == 1
    expr = _expression_of(tab.sent[0])
    detail = json.loads(expr.split("detail: ", 1)[1].rsplit("}))", 1)[0])
    assert detail["success"] is False
    assert "kaboom" in detail["error"]
