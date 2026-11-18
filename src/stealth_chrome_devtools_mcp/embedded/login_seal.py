"""Sealed tabs: the one place that knows which tabs the vault login owns (T-19366).

A vault login (``login_flow``) types a password into a tab the model must not be
able to read, script, screenshot, navigate or hook. This module owns exactly one
fact, which instances have a seal in progress and which ``(instance, target)``
pairs are sealed in them, and the check that turns that fact into a refusal.
It drives no browser; the flow calls :func:`begin`, :func:`seal_tab` and
:func:`end`, and three places call back to ask:

* ``ToolRegistry.section_tool``'s wrapper, for EVERY tool, through
  :func:`refuse_if_sealed` (so a tool added tomorrow is covered the day it is
  registered; there is no per-tool list to forget);
* ``tabs.list_tabs`` and the ``browser://`` resources, which leave a sealed tab
  out through :func:`is_sealed`.

**Two kinds of refusal.** A call naming a sealed ``tab_id`` is refused. And while
ANY seal is in progress in an instance, :data:`INSTANCE_WIDE` tools are refused
whatever tab they name: ``execute_cdp_command`` can send ``Target.*`` commands
that reach every tab (attach, evaluate, screenshot), and ``switch_tab`` brings a
tab to the front and rebinds the caller; neither needs the sealed tab's id.

**Re-entrant.** :func:`begin` counts, so a provider entry that runs the flow for
its ``via`` entry inside its own (and two logins racing in one instance) hold the
seal until the LAST of them calls :func:`end`; an ``end`` can never drop a seal
another login still needs. :func:`end` without a :func:`begin` does nothing, so
it is safe in a ``finally``.

In memory, like ``tab_binding`` and ``session_lease``: a backend restart forgets
every seal, and so does the login that was holding it (it died with the process).
A leaf: ``tool_errors`` only.
"""

from __future__ import annotations

import threading

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: The one sentence a refused call gets. It names no instance and no tab: the
#: caller already knows both, and an error that echoed a sealed target id would
#: tell a model which target to go after by other means.
REFUSAL = "a vault login is running in this browser; retry when it finishes"

#: Tools that can reach a sealed tab without naming it, refused for the whole
#: instance while a seal is in progress.
INSTANCE_WIDE = frozenset({"execute_cdp_command", "switch_tab"})

_lock = threading.Lock()
_depth: dict[str, int] = {}
_tabs: dict[str, set[str]] = {}


def begin(instance_id: str) -> None:
    """Mark ``instance_id`` as having a seal in progress (re-entrant)."""
    with _lock:
        _depth[instance_id] = _depth.get(instance_id, 0) + 1
        _tabs.setdefault(instance_id, set())


def seal_tab(instance_id: str, target_id: str) -> None:
    """Seal ``target_id`` in an instance that :func:`begin` already marked."""
    with _lock:
        if instance_id not in _depth:
            raise RuntimeError("no vault login is in progress for this instance")
        _tabs[instance_id].add(str(target_id))


def end(instance_id: str) -> None:
    """One login in ``instance_id`` is finished; the seal and its tabs are
    dropped when it was the last. A no-op when none was in progress."""
    with _lock:
        depth = _depth.get(instance_id, 0) - 1
        if depth > 0:
            _depth[instance_id] = depth
            return
        _depth.pop(instance_id, None)
        _tabs.pop(instance_id, None)


def sealing(instance_id: str) -> bool:
    """Whether a login is in progress in ``instance_id``."""
    return instance_id in _depth


def any_sealing() -> bool:
    """Whether any instance has a login in progress. One dict truthiness check:
    the registry wrapper asks it on every tool call and does nothing more while
    it is false."""
    return bool(_depth)


def is_sealed(instance_id: str, target_id: str | None) -> bool:
    """Whether ``target_id`` is a sealed tab of ``instance_id``."""
    if not target_id:
        return False
    return str(target_id) in _tabs.get(instance_id, ())


def refuse_if_sealed(tool: str, kwargs: dict[str, object]) -> None:
    """Raise :class:`ToolError` when this call must not run.

    ``kwargs`` are the call's arguments by name (``instance_id``, ``tab_id``). A
    call that names no usable instance, or an instance with no seal in progress,
    passes."""
    instance_id = kwargs.get("instance_id")
    if not isinstance(instance_id, str) or not sealing(instance_id):
        return
    tab_id = kwargs.get("tab_id")
    if tool in INSTANCE_WIDE or (
        isinstance(tab_id, str) and is_sealed(instance_id, tab_id)
    ):
        raise ToolError(REFUSAL)
