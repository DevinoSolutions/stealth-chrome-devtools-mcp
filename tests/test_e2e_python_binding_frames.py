"""T-6561 (#278 review): WHICH frames a ``create_python_binding`` answers, against
real Chrome.

``Runtime.addBinding`` exposes ``window[name]`` to every frame of the tab, and
the Python behind it runs with real builtins in the backend (T-6206, F-L). The
hermetic pins in ``test_python_binding.py`` drive the handler with synthetic
events; this walks the three frame shapes that matter through a real browser:

* a same-origin ``about:blank`` iframe calling ``window.parent``'s binding is
  answered -- the call runs in the creating context, which is what a binding is
  for;
* a ``srcdoc`` iframe calling the binding from its own context is refused;
* a cross-origin iframe calling it from its own context is refused.

The oracle is the Python function's own call log (``builtins._pb_calls``, the
backend and this test share a process), not the page: a refused call returns
nothing to the page, so only the log can say it never ran. Each frame reports
``typeof window.pyProbe`` to its parent so "refused" cannot be mistaken for "the
binding never reached that frame", and a final answered call proves the earlier
refusals were handled and not merely still in flight.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import time

import pytest
from nodriver import cdp

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    runtime,
    sandbox_kwargs,
    wait_for_js,
    warmup_once,
)

pytestmark = integration_pytestmark()

_BINDING_CODE = (
    "def pyProbe(a, b):\n"
    "    import builtins\n"
    "    builtins._pb_calls.append((a, b))\n"
    "    return a + b\n"
)


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture()
def call_log():
    builtins._pb_calls = []
    try:
        yield builtins._pb_calls
    finally:
        del builtins._pb_calls


async def _evaluate(iid: str, expression: str) -> dict:
    return await get_fn("execute_cdp_command")(
        instance_id=iid,
        command="evaluate",
        params={
            "expression": expression,
            "await_promise": True,
            "return_by_value": True,
        },
    )


async def _call_from_parent(iid: str, expression: str) -> None:
    """Call the binding through ``expression`` and assert Python's 40 + 2 came back."""
    answered = await _evaluate(iid, expression)
    assert answered["success"] is True, answered
    assert "42" in json.dumps(answered, default=str), answered


async def test_binding_answers_same_origin_blank_frame_and_refuses_the_rest(
    fixture_origin_pair, call_log
):
    origin_a, origin_b = fixture_origin_pair
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{origin_a}/hooks.html")
        created = await get_fn("create_python_binding")(
            instance_id=iid, binding_name="pyProbe", python_code=_BINDING_CODE
        )
        assert created.get("success") is True, created
        await eval_js(
            iid,
            "window.__probes = [];"
            "window.addEventListener('message', e => window.__probes.push(e.data));"
            "true",
        )

        # Answered: an about:blank frame inherits the parent's origin and calls
        # the parent's own function, so the call runs in the creating context.
        await eval_js(
            iid,
            "window.__blank = document.body.appendChild("
            "document.createElement('iframe')); true",
        )
        await _call_from_parent(
            iid, "window.__blank.contentWindow.eval('window.parent.pyProbe(40, 2)')"
        )
        assert call_log == [(40, 2)]

        # Refused: each frame calls the binding from its own execution context.
        await _evaluate(
            iid,
            "fetch('/binding_probe.html').then(r => r.text()).then(html => {"
            "const f = document.createElement('iframe');"
            "f.name = 'srcdoc'; f.srcdoc = html; document.body.appendChild(f);"
            "return true;})",
        )
        await eval_js(
            iid,
            "(() => { const f = document.createElement('iframe');"
            f"f.name = 'cross'; f.src = '{origin_b}/binding_probe.html';"
            "document.body.appendChild(f); return true; })()",
        )
        assert await wait_for_js(iid, "window.__probes.length", 2, timeout=15.0) == 2
        probes = json.loads(await eval_js(iid, "JSON.stringify(window.__probes)"))
        assert sorted((p["frame"], p["exposed"], p["threw"]) for p in probes) == [
            ("cross", "function", None),
            ("srcdoc", "function", None),
        ], probes

        # A later answered call: events are handled in order, so by now the two
        # refused ones were decided and never ran the function.
        await _call_from_parent(iid, "window.pyProbe(40, 2)")
        assert call_log == [(40, 2), (40, 2)]
    finally:
        await close(instance_id=iid)


async def test_closing_a_tab_reaps_its_watch_and_leaves_nodrivers_handler(
    fixture_origin_pair,
):
    """The reap of #278: a closed tab's ``TabWatch`` leaves ``Bindings.watches``
    (with its reference to the Tab) when Chrome reports ``Target.targetDestroyed``,
    and removing the watch's handler does not take nodriver's own handler for the
    same event off the browser's connection with it."""
    origin_a, _ = fixture_origin_pair
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        tab_id = (
            await get_fn("new_tab")(instance_id=iid, url=f"{origin_a}/hooks.html")
        )["tab_id"]
        assert await get_fn("switch_tab")(instance_id=iid, tab_id=tab_id) is True
        created = await get_fn("create_python_binding")(
            instance_id=iid, binding_name="pyProbe", python_code=_BINDING_CODE
        )
        assert created.get("success") is True, created
        bindings = runtime.cdp_function_executor._python_bindings
        assert tab_id in bindings.watches
        assert (tab_id, "pyProbe") in bindings

        assert await get_fn("close_tab")(instance_id=iid, tab_id=tab_id) is True

        deadline = time.monotonic() + 10.0
        while tab_id in bindings.watches and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert tab_id not in bindings.watches
        assert (tab_id, "pyProbe") not in bindings
        browser = await runtime.browser_manager.get_browser(iid)
        # Settled: the deferred detach has run by the time the loop has waited.
        await asyncio.sleep(0.2)
        callbacks = browser.connection.handlers[cdp.target.TargetDestroyed]
        assert callbacks == [browser._handle_target_update], callbacks
    finally:
        await close(instance_id=iid)
