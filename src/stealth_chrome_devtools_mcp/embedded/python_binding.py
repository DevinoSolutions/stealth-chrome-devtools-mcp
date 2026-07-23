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
* :func:`call_python_from_js` — the Python-side dispatch (moved here).

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
from typing import Any

import nodriver as uc
from nodriver import Tab

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


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


async def call_python_from_js(
    bindings: dict[str, Callable[..., Any]], binding_name: str, args: list[Any]
) -> dict[str, Any]:
    """Run the bound Python function for a JS call and return the outcome dict.

    Sync and coroutine functions are both supported. Never raises: a failure is
    reported as ``{"success": False, "error": ...}`` so the handler can always
    dispatch a response back to the waiting page promise.
    """
    try:
        if binding_name not in bindings:
            return {"success": False, "error": f"Unknown binding: {binding_name}"}
        python_function = bindings[binding_name]
        if asyncio.iscoroutinefunction(python_function):
            result = await python_function(*args)
        else:
            result = python_function(*args)
        return {
            "success": True,
            "result": result,
            "binding_name": binding_name,
            "args": args,
        }
    except Exception as e:
        debug_logger.log_error("python_binding", "call_python_from_js", e)
        return {
            "success": False,
            "error": str(e),
            "binding_name": binding_name,
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


async def on_binding_called(
    tab: Tab, event: Any, binding_name: str, bindings: dict[str, Callable[..., Any]]
) -> None:
    """Handle one ``Runtime.bindingCalled`` event for ``binding_name``.

    Each installed binding registers its own handler filtered to its name, so
    handlers for different bindings on the same tab never cross-process an event.
    """
    if event.name != binding_name:
        return  # another binding's event; not ours
    try:
        payload = json.loads(event.payload)
        call_id = payload["callId"]
        args = payload.get("args", [])
    except (ValueError, KeyError, TypeError) as e:
        debug_logger.log_warning(
            "python_binding",
            "on_binding_called",
            f"malformed payload for {binding_name}: {e}",
        )
        return
    outcome = await call_python_from_js(bindings, binding_name, args)
    await _dispatch_response(tab, binding_name, call_id, outcome)


async def install_binding(
    tab: Tab, binding_name: str, bindings: dict[str, Callable[..., Any]]
) -> dict[str, Any]:
    """Wire the genuine round-trip for ``binding_name`` on ``tab``.

    Registers the CDP binding, the ``bindingCalled`` handler, and injects the
    page-side wrapper. ``bindings`` is the caller's live registry (already
    holding ``binding_name``). Raises :class:`ToolError` on failure.
    """
    try:
        await tab.send(uc.cdp.runtime.add_binding(name=binding_name))
        tab.add_handler(
            uc.cdp.runtime.BindingCalled,
            lambda event: asyncio.create_task(
                on_binding_called(tab, event, binding_name, bindings)
            ),
        )
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
    except ToolError:
        raise
    except Exception as e:
        debug_logger.log_error("python_binding", "install_binding", e)
        raise ToolError(f"Failed to create binding {binding_name!r}: {e!s}") from e
