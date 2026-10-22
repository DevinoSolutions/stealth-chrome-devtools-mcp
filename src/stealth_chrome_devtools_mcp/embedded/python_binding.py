"""JS→Python binding: the genuine ``Runtime.bindingCalled`` round-trip (A4).

Extracted from ``cdp_function_executor.py`` (which is at its LOC budget) so the
executor keeps only a thin delegation. This module owns the whole JS↔Python
binding concern:

* :func:`build_wrapper_script` — the page-side wrapper that turns
  ``window[name](...args)`` into a Promise;
* :func:`install_binding` — ``Runtime.addBinding`` + the ``bindingCalled``
  handler + wrapper injection;
* :func:`on_binding_called` — the handler that runs the Python function and
  dispatches the result back into the page;
* :func:`call_python_from_js` — the Python-side dispatch (moved here);
* :func:`drop_binding` — the end of a binding's life.

**Who may call (T-6206, F-L).** ``Runtime.addBinding`` is exposed to every
frame of the tab and survives navigations, and the Python behind it runs in the
backend process with real builtins — so a binding that answered any caller would
hand host-Python execution to every script the tab ever loads, cross-origin
iframes included. A binding is therefore bound to ONE tab (the registry key is
``(target_id, name)``, not a name shared by every instance) and to the
execution context that was current when it was created: ``bindingCalled``
carries the calling ``executionContextId`` and a call from any other context is
dropped with a WARNING naming the contexts, never the payload. When that
context is destroyed (any navigation or reload of the document) the binding is
dropped: ``Runtime.removeBinding`` plus the registry entry. Page script in the
creating context itself can still call it — that is what a binding is for; the
tool description says so.

How the round-trip actually works (the old code never wired it): ``addBinding``
exposes ``window[name]`` as a function that, when called with a **single
string**, emits ``Runtime.bindingCalled``. The wrapper preserves that raw
function, then overrides ``window[name]`` with a Promise-returning shim that
calls the raw binding with ``JSON.stringify({callId, args})``. The handler runs
the Python function and dispatches a ``<name>_response_<callId>`` CustomEvent
that the shim is listening for. The previous implementation instead overrode
``window[name]`` and called ``window.chrome.runtime.sendMessage`` (an extension
API, not the CDP channel), so no binding ever fired.

Leaf module: imports only ``debug_logger``, ``tool_errors``, and nodriver — it
must never import ``server`` (the ``tab`` is passed in as an argument).
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import nodriver as uc
from nodriver import Tab

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

# Contexts already named in a refusal warning, per binding: a hostile page can
# mint iframes at will, and one WARNING per context would let it flood the log.
_MAX_LOGGED_REFUSALS = 16


@dataclass(eq=False)
class BoundPython:
    """One binding: the function, the tab, and the one context allowed to call it."""

    name: str
    function: Callable[..., Any]
    target_id: str
    context_id: int
    refused: set[int] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, str]:
        return (self.target_id, self.name)


Bindings = dict[tuple[str, str], BoundPython]


def build_wrapper_script(binding_name: str) -> str:
    """Return the page-side wrapper installed for ``binding_name``.

    Saves the raw CDP binding (``window[name]`` created by ``addBinding``) under
    a private slot, then overrides ``window[name]`` with a Promise wrapper that
    invokes the raw binding with a single JSON string — the only shape a CDP
    binding accepts — and resolves when the Python side dispatches the matching
    ``<name>_response_<callId>`` event.
    """
    return f"""
    (function() {{
        if (!window.__{binding_name}_binding) {{
            window.__{binding_name}_binding = window.{binding_name};
        }}
        window.{binding_name} = function(...args) {{
            return new Promise((resolve, reject) => {{
                const callId = Math.random().toString(36).substr(2, 9);
                const evtName = `{binding_name}_response_${{callId}}`;
                window.addEventListener(evtName, function(event) {{
                    if (event.detail.success) {{
                        resolve(event.detail.result);
                    }} else {{
                        reject(new Error(event.detail.error));
                    }}
                }}, {{ once: true }});
                window.__{binding_name}_binding(
                    JSON.stringify({{ callId: callId, args: args }})
                );
            }});
        }};
        return {{
            success: true,
            binding_name: '{binding_name}',
            available_as: 'window.{binding_name}'
        }};
    }})()
    """


async def call_python_from_js(bound: BoundPython, args: list[Any]) -> dict[str, Any]:
    """Run the bound Python function for a JS call and return the outcome dict.

    Sync and coroutine functions are both supported. Never raises: a failure is
    reported as ``{"success": False, "error": ...}`` so the handler can always
    dispatch a response back to the waiting page promise.
    """
    try:
        if asyncio.iscoroutinefunction(bound.function):
            result = await bound.function(*args)
        else:
            result = bound.function(*args)
        return {
            "success": True,
            "result": result,
            "binding_name": bound.name,
            "args": args,
        }
    except Exception as e:
        debug_logger.log_error("python_binding", "call_python_from_js", e)
        return {
            "success": False,
            "error": str(e),
            "binding_name": bound.name,
            "args": args,
        }


async def _dispatch_response(
    tab: Tab, binding_name: str, call_id: str, outcome: dict[str, Any]
) -> None:
    """Dispatch the ``<name>_response_<callId>`` CustomEvent carrying ``outcome``
    back into the page so the wrapper's one-shot listener resolves/rejects."""
    evt_name = f"{binding_name}_response_{call_id}"
    try:
        detail = json.dumps(outcome)
    except (TypeError, ValueError):
        detail = json.dumps(
            {"success": False, "error": "binding result is not JSON-serializable"}
        )
    dispatch_js = (
        f"window.dispatchEvent(new CustomEvent({json.dumps(evt_name)}, "
        f"{{ detail: {detail} }}))"
    )
    try:
        await tab.send(
            uc.cdp.runtime.evaluate(expression=dispatch_js, return_by_value=True)
        )
    except Exception as e:
        debug_logger.log_warning("python_binding", "_dispatch_response", str(e))


def _refuse(bound: BoundPython, calling_context: Any) -> None:
    """Log a refused call once per calling context, with no payload values."""
    if calling_context in bound.refused or len(bound.refused) >= _MAX_LOGGED_REFUSALS:
        return
    bound.refused.add(calling_context)
    debug_logger.log_warning(
        "python_binding",
        "on_binding_called",
        f"refused a call to binding {bound.name!r} on tab {bound.target_id} from "
        f"execution context {calling_context}; it answers only context "
        f"{bound.context_id}, the one current when it was created",
    )


async def on_binding_called(
    tab: Tab, event: Any, bound: BoundPython, bindings: Bindings
) -> None:
    """Handle one ``Runtime.bindingCalled`` event for ``bound``.

    Each installed binding registers its own handler filtered to its name, so
    handlers for different bindings on the same tab never cross-process an event.
    A call is answered only while ``bound`` is still the registered binding for
    its tab and only from the execution context it was created in; anything
    else is dropped without running the Python function.
    """
    if event.name != bound.name or bindings.get(bound.key) is not bound:
        return  # another binding's event, or this one has been dropped
    calling_context = getattr(event, "execution_context_id", None)
    if calling_context != bound.context_id:
        _refuse(bound, calling_context)
        return
    try:
        payload = json.loads(event.payload)
        call_id = payload["callId"]
        args = payload.get("args", [])
    except (ValueError, KeyError, TypeError) as e:
        debug_logger.log_warning(
            "python_binding",
            "on_binding_called",
            f"malformed payload for {bound.name}: {e}",
        )
        return
    outcome = await call_python_from_js(bound, args)
    await _dispatch_response(tab, bound.name, call_id, outcome)


async def drop_binding(tab: Tab, bindings: Bindings, bound: BoundPython) -> None:
    """End ``bound``: remove it from the registry and unsubscribe the tab.

    Idempotent. A binding that is no longer the registered one for its tab (it
    was dropped, or replaced by a newer binding of the same name) is left alone,
    so a late context-destroyed event for an old document cannot unsubscribe
    its replacement.
    """
    if bindings.get(bound.key) is not bound:
        return
    del bindings[bound.key]
    try:
        await tab.send(uc.cdp.runtime.remove_binding(name=bound.name))
    except Exception as e:
        debug_logger.log_warning("python_binding", "drop_binding", str(e))
    debug_logger.log_info(
        "python_binding",
        "drop_binding",
        f"dropped binding {bound.name!r} on tab {bound.target_id}",
    )


async def _current_context_id(tab: Tab) -> int:
    """The execution context the page's own script runs in right now.

    A throwing evaluate answers with ``exceptionDetails.executionContextId``.
    Raises :class:`ToolError` when Chrome does not name one: a binding that
    cannot be tied to a context must not exist.
    """
    _, details = await tab.send(
        uc.cdp.runtime.evaluate(expression="throw null", return_by_value=True)
    )
    context_id = getattr(details, "execution_context_id", None)
    if context_id is None:
        raise ToolError("Could not identify the page's execution context")
    return int(context_id)


async def install_binding(
    tab: Tab,
    binding_name: str,
    bindings: Bindings,
    python_function: Callable[..., Any],
) -> dict[str, Any]:
    """Wire the genuine round-trip for ``binding_name`` on ``tab``.

    Registers the binding for this tab and its current execution context, the
    ``bindingCalled`` and context-lifetime handlers, and injects the page-side
    wrapper. A binding of the same name already on this tab is replaced. Raises
    :class:`ToolError` on failure. The handlers stay on the tab for its life
    (nodriver's ``remove_handler`` drops every handler of a type); once the
    binding is dropped they find nothing registered and return.
    """
    try:
        target_id = str(tab.target.target_id)
        previous = bindings.get((target_id, binding_name))
        if previous is not None:
            await drop_binding(tab, bindings, previous)
        bound = BoundPython(
            binding_name, python_function, target_id, await _current_context_id(tab)
        )
        bindings[bound.key] = bound

        async def on_destroyed(event: Any) -> None:
            if event.execution_context_id == bound.context_id:
                await drop_binding(tab, bindings, bound)

        async def on_cleared(_event: Any) -> None:
            await drop_binding(tab, bindings, bound)

        try:
            await tab.send(uc.cdp.runtime.add_binding(name=binding_name))
            tab.add_handler(
                uc.cdp.runtime.BindingCalled,
                lambda event: asyncio.create_task(
                    on_binding_called(tab, event, bound, bindings)
                ),
            )
            tab.add_handler(uc.cdp.runtime.ExecutionContextDestroyed, on_destroyed)
            tab.add_handler(uc.cdp.runtime.ExecutionContextsCleared, on_cleared)
            result = await tab.send(
                uc.cdp.runtime.evaluate(
                    expression=build_wrapper_script(binding_name),
                    return_by_value=True,
                    await_promise=True,
                )
            )
            if result and result[0] and result[0].value:
                return result[0].value
            raise ToolError(f"Failed to install binding {binding_name!r}")
        except BaseException:
            await drop_binding(tab, bindings, bound)
            raise
    except ToolError:
        raise
    except Exception as e:
        debug_logger.log_error("python_binding", "install_binding", e)
        raise ToolError(f"Failed to create binding {binding_name!r}: {e!s}") from e
