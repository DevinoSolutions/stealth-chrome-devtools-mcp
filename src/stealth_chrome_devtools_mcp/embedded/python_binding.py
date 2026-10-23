"""JS→Python binding: the genuine ``Runtime.bindingCalled`` round-trip (A4).

Extracted from ``cdp_function_executor.py`` (which is at its LOC budget) so the
executor keeps only a thin delegation. This module owns the whole JS↔Python
binding concern:

* :func:`build_wrapper_script` — the page-side wrapper that turns
  ``window[name](...args)`` into a Promise;
* :func:`install_binding` — ``Runtime.addBinding`` + wrapper injection, on the
  tab's one :class:`TabWatch`;
* :func:`on_binding_called` — the handler that runs the Python function and
  dispatches the result back into the page;
* :func:`call_python_from_js` — the Python-side dispatch (moved here);
* :func:`drop_binding` — the end of a binding's life.

**Who may call (T-6206, F-L).** ``Runtime.addBinding`` is exposed to every
frame of the tab and survives navigations, and the Python behind it runs in the
backend process with real builtins — so a binding that answered any caller would
hand host-Python execution to every script the tab ever loads, cross-origin
iframes included. A binding is therefore bound to ONE tab (the registry key is
``(target_id, name)``, not a name shared by every instance) and to ONE execution
context: the main frame's default context, as the protocol reports it. Each tab
carries one :class:`TabWatch` — a single handler set that tracks
``Runtime.executionContextCreated`` (replayed for every existing context when
``Runtime.enable`` is re-issued) and keeps that context's ``id`` AND its
``uniqueId``, because ids are per-isolate counters that repeat across renderer
processes and only ``uniqueId`` is system-wide. ``bindingCalled`` carries the
calling ``executionContextId`` and a call from any other context is dropped
with a WARNING naming the contexts, never the payload. The binding is dropped
(``Runtime.removeBinding`` plus the registry entry) on ANY of: that context
destroyed, ``executionContextsCleared``, a context created with the same id but
another ``uniqueId``, or a main-frame ``Page.frameNavigated`` — and a creation
that sees any of these between reading the context and finishing the install
fails closed. Page script in the creating context itself, same-origin frames
included, can still call it — that is what a binding is for; the tool
description says so.

A watch also ends with its target: ``Target.targetDestroyed`` (reported on the
browser's connection, never the page's) forgets the tab's bindings, detaches the
watch and removes it from ``Bindings.watches``, which would otherwise keep every
closed tab. A watch replaced by a new connection to the same target is detached
the same way.

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
import contextlib
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
    unique_id: str
    refused: set[int] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, str]:
        return (self.target_id, self.name)


class Bindings(dict[tuple[str, str], BoundPython]):
    """The live bindings, keyed ``(target_id, name)``, and each tab's one watch."""

    def __init__(self) -> None:
        super().__init__()
        self.watches: dict[str, TabWatch] = {}


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

    A call is answered only while ``bound`` is still the registered binding for
    its tab and only from the execution context it was created in; anything
    else is dropped without running the Python function.
    """
    if bindings.get(bound.key) is not bound:
        return  # dropped while the event was in flight
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


def _forget(bindings: Bindings, bound: BoundPython) -> bool:
    """Take ``bound`` out of the registry; False when it is no longer the
    registered binding for its tab and name (dropped, or replaced)."""
    if bindings.get(bound.key) is not bound:
        return False
    del bindings[bound.key]
    return True


async def _unexpose(tab: Tab, bound: BoundPython) -> None:
    """``Runtime.removeBinding`` for a binding already out of the registry."""
    try:
        await tab.send(uc.cdp.runtime.remove_binding(name=bound.name))
    except Exception as e:
        debug_logger.log_warning("python_binding", "drop_binding", str(e))
    debug_logger.log_info(
        "python_binding",
        "drop_binding",
        f"dropped binding {bound.name!r} on tab {bound.target_id}",
    )


async def drop_binding(tab: Tab, bindings: Bindings, bound: BoundPython) -> None:
    """End ``bound``: remove it from the registry and unsubscribe the tab.

    Idempotent. A binding that is no longer the registered one for its tab (it
    was dropped, or replaced by a newer binding of the same name) is left alone,
    so a late context-destroyed event for an old document cannot unsubscribe
    its replacement.
    """
    if _forget(bindings, bound):
        await _unexpose(tab, bound)


@dataclass(eq=False)
class TabWatch:
    """The ONE handler set of a tab, whatever number of bindings it carries.

    Every handler is synchronous so that, in nodriver's listener, it has run for
    every event that precedes a command's reply before that command returns.
    ``main`` is ``(id, unique_id)`` of the main frame's default context as last
    reported, ``None`` when none is known.
    """

    tab: Tab
    bindings: Bindings
    target_id: str
    main: tuple[int, str] | None = None
    replayed: bool = False
    _removals: set[asyncio.Task[None]] = field(default_factory=set)
    _listening: list[tuple[Any, type, Callable[[Any], Any]]] = field(
        default_factory=list
    )

    def _listen(self, connection: Any, event_type: type, handler: Callable) -> None:
        connection.add_handler(event_type, handler)
        self._listening.append((connection, event_type, handler))

    def attach(self) -> None:
        rt = uc.cdp.runtime
        self._listen(self.tab, rt.BindingCalled, self._on_binding_called)
        self._listen(self.tab, rt.ExecutionContextCreated, self._on_created)
        self._listen(self.tab, rt.ExecutionContextDestroyed, self._on_destroyed)
        self._listen(self.tab, rt.ExecutionContextsCleared, self._on_cleared)
        self._listen(self.tab, uc.cdp.page.FrameNavigated, self._on_navigated)
        # The page's own connection never reports its target's end; the browser's
        # does, and it is what lets the watch (and the Tab it holds) go.
        browser = getattr(self.tab, "browser", None)
        if browser is not None:
            self._listen(
                browser.connection,
                uc.cdp.target.TargetDestroyed,
                self._on_target_destroyed,
            )

    def detach(self) -> None:
        """Take this watch's handlers off the connections it was attached to.

        By hand, not ``Connection.remove_handler``: that deletes the event type's
        whole handler list, which on the browser connection is nodriver's own
        target bookkeeping and on the tab is every other listener.
        """
        for connection, event_type, handler in self._listening:
            with contextlib.suppress(KeyError, ValueError):
                connection.handlers[event_type].remove(handler)
        self._listening.clear()

    async def replay_contexts(self) -> None:
        """Make Chrome report every existing context again.

        ``Runtime.enable`` sends ``executionContextCreated`` for each existing
        context, but only on a real enable and the domain is normally enabled
        already, so it is disabled first. Run once per watch, with the handlers
        attached; the replies come after the events they follow.
        """
        await self.tab.send(uc.cdp.runtime.disable())
        await self.tab.send(uc.cdp.runtime.enable())
        self.replayed = True

    def _tab_bindings(self) -> list[BoundPython]:
        return [b for b in self.bindings.values() if b.target_id == self.target_id]

    def drop(self, bound: BoundPython) -> None:
        """Forget ``bound`` now (so the next event already finds nothing) and
        remove it from the page in the background."""
        if _forget(self.bindings, bound):
            task = asyncio.create_task(_unexpose(self.tab, bound))
            self._removals.add(task)
            task.add_done_callback(self._removals.discard)

    def _on_binding_called(self, event: Any) -> Any:
        bound = self.bindings.get((self.target_id, event.name))
        if bound is None:
            return None  # another script's binding, or one already dropped
        return asyncio.create_task(
            on_binding_called(self.tab, event, bound, self.bindings)
        )

    def _on_created(self, event: Any) -> None:
        context = event.context
        context_id, unique_id = int(context.id_), str(context.unique_id)
        aux = context.aux_data or {}
        is_main = bool(aux.get("isDefault")) and aux.get("frameId") == self.target_id
        if is_main:
            self.main = (context_id, unique_id)
        for bound in self._tab_bindings():
            # A context with this binding's id but another uniqueId is another
            # process's context (ids repeat); a NEW main context means the page
            # is another document. Either way this binding's context is gone.
            if bound.unique_id != unique_id and (
                context_id == bound.context_id or is_main
            ):
                self.drop(bound)

    def _on_destroyed(self, event: Any) -> None:
        context_id = int(event.execution_context_id)
        unique_id = str(event.execution_context_unique_id)
        if self.main is not None and (context_id, unique_id) == self.main:
            self.main = None
        for bound in self._tab_bindings():
            if bound.unique_id == unique_id or bound.context_id == context_id:
                self.drop(bound)

    def _on_cleared(self, _event: Any) -> None:
        self.main = None
        for bound in self._tab_bindings():
            self.drop(bound)

    def _on_navigated(self, event: Any) -> None:
        if event.frame.parent_id is None:  # the main frame
            for bound in self._tab_bindings():
                self.drop(bound)

    def _on_target_destroyed(self, event: Any) -> None:
        """The tab is gone: forget its bindings (there is no page left to
        ``removeBinding`` on), leave its connections and leave the registry."""
        if str(event.target_id) != self.target_id:
            return
        for bound in self._tab_bindings():
            _forget(self.bindings, bound)
        self.detach()
        if self.bindings.watches.get(self.target_id) is self:
            del self.bindings.watches[self.target_id]


async def _watch_for(tab: Tab, bindings: Bindings) -> TabWatch:
    """The watch of ``tab``, attached and replayed (made on first use)."""
    target_id = str(tab.target.target_id)
    watch = bindings.watches.get(target_id)
    if watch is None or watch.tab is not tab:
        # First use, or a new connection to the same target: bindings made on the
        # old connection do not exist on this one, and its watch must stop
        # listening on a tab nobody drives any more.
        if watch is not None:
            watch.detach()
        for stale in [b for b in bindings.values() if b.target_id == target_id]:
            await drop_binding(tab, bindings, stale)
        watch = bindings.watches[target_id] = TabWatch(tab, bindings, target_id)
        watch.attach()
    if not watch.replayed:
        await watch.replay_contexts()
    return watch


def _ensure_current(bindings: Bindings, bound: BoundPython) -> None:
    """Fail closed when the context ``bound`` was made for ended since."""
    if bindings.get(bound.key) is not bound:
        raise ToolError(
            f"The page changed while binding {bound.name!r} was being created; "
            "create it again once the page has loaded"
        )


async def install_binding(
    tab: Tab,
    binding_name: str,
    bindings: Bindings,
    python_function: Callable[..., Any],
) -> dict[str, Any]:
    """Wire the genuine round-trip for ``binding_name`` on ``tab``.

    Registers the binding for this tab and the main frame's default context,
    then ``Runtime.addBinding`` and the page-side wrapper. The binding is in the
    registry before the first await after the context was read, so the tab's
    watch drops it if that context ends meanwhile, and the install then fails
    closed. A binding of the same name already on this tab is replaced. Raises
    :class:`ToolError` on failure.
    """
    try:
        target_id = str(tab.target.target_id)
        previous = bindings.get((target_id, binding_name))
        if previous is not None:
            await drop_binding(tab, bindings, previous)
        watch = await _watch_for(tab, bindings)
        if watch.main is None:
            raise ToolError("Could not identify the page's execution context")
        bound = BoundPython(binding_name, python_function, target_id, *watch.main)
        bindings[bound.key] = bound
        try:
            await tab.send(uc.cdp.runtime.add_binding(name=binding_name))
            _ensure_current(bindings, bound)
            result = await tab.send(
                uc.cdp.runtime.evaluate(
                    expression=build_wrapper_script(binding_name),
                    return_by_value=True,
                    await_promise=True,
                    unique_context_id=bound.unique_id,
                )
            )
            _ensure_current(bindings, bound)
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
