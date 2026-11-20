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

**A tab that would not close stays sealed.** :func:`end` takes the target the
login could not close (``unclosed``): it is kept sealed after the login is over,
so an agent still cannot read it, and the flow says so. It is dropped from here
only by the backend restarting.

**The creation window.** The login's tab exists in Chrome (and in nodriver's
target list) a moment before the flow has its id and seals it. :func:`hold_new`
covers that moment by sealing "every target that was not there before"; the flow
releases it in the same step that seals the tab by id. A login tab was reachable
through ``list_tabs`` and ``execute_script`` in that moment before this existed.

**Popups.** A page the sealed tab opens (``window.open``, ``target=_blank``) is
sealed by :func:`seal_popup` from the ``targetCreated`` event and closed by
``tab_open``; :func:`release` drops it once Chrome no longer lists it.

**Barred targets.** :func:`bar` hides a target that is not a login's tab but must
not be reachable either: a DevTools frontend or ``chrome://inspect`` page
(``debug_port_guard.inspector_url``), whose script can send raw CDP to every tab.
A barred target is refused and unlisted like a sealed one, whether or not any login
is running (:func:`is_sealed`), and :func:`login_in_flight` tells the guard when to
close such a window and not merely hide it. ``go_back`` and ``go_forward`` are
:data:`INSTANCE_WIDE` too: history can return a tab to a page the URL tools refuse.
A barred window with no login running is refused with :data:`INSPECTOR_REFUSAL`,
which must not claim that a login is running.

**Browser-internal pages while a login runs.** :func:`mark_internal` notes a target
whose committed URL is a ``chrome:`` / ``chrome-extension:`` / ``devtools:`` ... page
(``debug_port_guard.internal_url``). Such a target is sealed (:func:`is_sealed`)
ONLY while a login is running in its instance, so it is hidden and refused for
exactly that long and ordinary ``chrome://`` use is not touched.

In memory, like ``tab_binding`` and ``session_lease``: a backend restart forgets
every seal, and so does the login that was holding it (it died with the process).
A leaf: ``tool_errors`` only.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import vault_mode
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The one sentence a refused call gets. It names no instance and no tab: the
#: caller already knows both, and an error that echoed a sealed target id would
#: tell a model which target to go after by other means.
REFUSAL = "a vault login is running in this browser; retry when it finishes"
#: The sentence for a target that is barred while NO login runs: a browser
#: inspector (a DevTools window), which no tool may use, login or not.
INSPECTOR_REFUSAL = (
    "that tab is a browser inspector (a DevTools window), which no tool can use"
)

#: Tools that can reach a sealed tab without naming it, refused for the whole
#: instance while a seal is in progress.
INSTANCE_WIDE = frozenset(
    {"execute_cdp_command", "switch_tab", "go_back", "go_forward"}
)

_lock = threading.Lock()
_depth: dict[str, int] = {}
#: Per instance with a login in progress, its browser's debug port (``None``: not
#: said, which counts for every browser). What ``login_in_flight_on`` reads.
_ports: dict[str, int | None] = {}
_tabs: dict[str, set[str]] = {}
_unclosed: dict[str, set[str]] = {}
#: Per instance, one frozenset of the targets that existed before each login tab
#: was being opened (:func:`hold_new`), until the tab is sealed.
_holds: dict[str, list[frozenset[str]]] = {}
#: Targets that are a browser inspector (a DevTools window, ``chrome://inspect``),
#: in ANY instance: never listed and never addressable by a tool, whether or not a
#: login is running (``debug_port_guard`` fills it from the browser's own events).
_barred: set[str] = set()
#: Targets at a browser-internal URL (``chrome://``, ``chrome-extension://``, ...,
#: ``debug_port_guard.internal_url``), in ANY instance. They are hidden and refused
#: only WHILE a login runs (:func:`is_sealed`): outside one, ordinary
#: ``chrome://version`` use is untouched.
_internal: set[str] = set()
#: Targets that are ``chrome://``, ``chrome-untrusted://`` or ``devtools://`` pages:
#: sealed while VAULT MODE is on, login or not (T-19366, audit 6 N6).
_webui: set[str] = set()
#: Popups of a login tab that the guard has let run (``runIfWaitingForDebugger``
#: answered). See :func:`wait_popup_released`.
_released: set[str] = set()
#: How often the wait for a popup's release looks, in seconds.
_RELEASE_POLL_S = 0.01


def begin(instance_id: str, port: int | None = None) -> None:
    """Mark ``instance_id`` as having a seal in progress (re-entrant). ``port`` is
    its browser's debug port, which is how the guard of THAT browser (and no other)
    learns a login is running; a login begun with none counts for every browser."""
    with _lock:
        _depth[instance_id] = _depth.get(instance_id, 0) + 1
        _tabs.setdefault(instance_id, set())
        _ports[instance_id] = port


def seal_tab(instance_id: str, target_id: str) -> None:
    """Seal ``target_id`` in an instance that :func:`begin` already marked."""
    with _lock:
        if instance_id not in _depth:
            raise RuntimeError("no vault login is in progress for this instance")
        _tabs[instance_id].add(str(target_id))


def end(instance_id: str, unclosed: str | None = None) -> None:
    """One login in ``instance_id`` is finished; the seal and its tabs are
    dropped when it was the last. A no-op when none was in progress.

    ``unclosed`` is a sealed target the login could not close: it stays sealed."""
    with _lock:
        if unclosed is not None:
            _unclosed.setdefault(instance_id, set()).add(str(unclosed))
        depth = _depth.get(instance_id, 0) - 1
        if depth > 0:
            _depth[instance_id] = depth
            return
        _depth.pop(instance_id, None)
        _ports.pop(instance_id, None)
        _tabs.pop(instance_id, None)


def hold_new(instance_id: str, known: Iterable[str]) -> frozenset[str]:
    """From now until :func:`release_new`, every target of ``instance_id`` that is
    NOT in ``known`` counts as sealed, and returns the token to release.

    Closes the window in which the login's own tab exists in Chrome but is not
    sealed yet: Chrome announces a new target before the call that created it has
    answered, and a tool (``list_tabs``, ``execute_script``) that runs in between
    would see a tab nobody has sealed. The flow holds the browser's CURRENT targets
    as the known ones, opens its tab, seals it by id and releases; a tab a caller
    opened in those few milliseconds is hidden for them too and listed afterwards."""
    token = frozenset(str(target) for target in known)
    with _lock:
        _holds.setdefault(instance_id, []).append(token)
    return token


def release_new(instance_id: str, token: frozenset[str]) -> None:
    """End one :func:`hold_new`; a no-op for a token already released."""
    with _lock:
        held = _holds.get(instance_id, [])
        for index, candidate in enumerate(held):
            if candidate is token:
                del held[index]
                break
        if not held:
            _holds.pop(instance_id, None)


def seal_popup(opener_id: str, popup_id: str) -> bool:
    """Seal ``popup_id``, a page the sealed tab ``opener_id`` opened, in the
    instance that holds the opener; ``False`` when no instance does.

    It is sealed from the event that announced it, so no tool can name it before
    the backend has closed it; :func:`release` drops it once it is gone. Until then
    it counts like a tab that would not close."""
    opener = str(opener_id)
    with _lock:
        for instance_id in (*_tabs, *_unclosed):
            if opener in _tabs.get(instance_id, ()) or opener in _unclosed.get(
                instance_id, ()
            ):
                _unclosed.setdefault(instance_id, set()).add(str(popup_id))
                return True
    return False


def popup_released(target_id: str, opener_id: object = None) -> None:
    """The guard has let ``target_id`` run: it is no longer paused. Only a page that
    belongs to a login (:func:`owns`) is noted, so the set stays as small as the
    popups of a login in flight."""
    if owns(target_id, None if opener_id is None else str(opener_id)):
        with _lock:
            _released.add(str(target_id))


async def wait_popup_released(target_id: str, seconds: float) -> None:
    """Wait until the guard has let the popup run, at most ``seconds``; at once when
    no guard is watching any browser.

    A popup of the login's tab is born PAUSED (the guard's browser-wide auto-attach
    waits for the debugger), and the page that opened it, a key handler inside
    ``window.open``, waits for it to exist. Closing it BEFORE the guard has let it
    run, and before its own commands were answered, left the opener inside
    ``window.open`` for good: the key press never returned and the login ran out
    of its budget (measured, Windows and macOS, T-19366: the popup's resume was
    ANSWERED after its close in every stalled run and never answered in the runs
    that passed). So it is closed after it was let run, never before."""
    from stealth_chrome_devtools_mcp.embedded import (
        debug_port_guard,  # the guard imports this module
    )

    for _ in range(int(seconds / _RELEASE_POLL_S)):
        if not debug_port_guard.protected_ports() or str(target_id) in _released:
            return
        await asyncio.sleep(_RELEASE_POLL_S)


def release(target_id: str) -> None:
    """``target_id`` is gone from the browser: it no longer needs sealing."""
    target = str(target_id)
    with _lock:
        _released.discard(target)
        for instance_id in list(_unclosed):
            _unclosed[instance_id].discard(target)
            if not _unclosed[instance_id]:
                del _unclosed[instance_id]


def owns(target_id: str, opener_id: str | None = None) -> bool:
    """Whether ``target_id`` belongs to a login: its own tab (sealed, or created
    after a login began opening it and not yet sealed by id), or a page whose
    opener (``opener_id``) is one. Any instance."""
    target = str(target_id)
    with _lock:
        for instance_id in {*_tabs, *_unclosed, *_holds}:
            owned = _tabs.get(instance_id, set()) | _unclosed.get(instance_id, set())
            if target in owned or (opener_id is not None and str(opener_id) in owned):
                return True
            if any(target not in known for known in _holds.get(instance_id, ())):
                return True
    return False


def bar(target_id: str) -> None:
    """``target_id`` is an inspector: no tool may list or name it from now on."""
    with _lock:
        _barred.add(str(target_id))


def unbar(target_id: str) -> None:
    """``target_id`` is gone, or no longer an inspector."""
    with _lock:
        _barred.discard(str(target_id))


def mark_webui(target_id: str, webui: bool) -> None:
    """Note whether ``target_id`` is a browser WebUI page (``debug_port_guard``'s
    ``webui_url``). Such a target is sealed (:func:`is_sealed`) while vault mode is
    on: hidden from the tools and refused by ``tab_id``, with or without a login.

    Marked ONLY while vault mode is on (audit 7 F5): nothing outside the mode reads
    the mark (:func:`is_sealed` asks the mode too), and a mark would make
    :func:`any_sealing` true, so every tool call of a user with no vault would bind
    its arguments for a page that is never refused. A vault made LATER costs nothing:
    the keeper judges every open target again on each pass while the mode is on, so a
    page that was already open is marked within ``debug_port_guard.KEEP_SECONDS``
    (the same window as every other rule that starts with the vault)."""
    marked = webui and vault_mode.active()
    with _lock:
        (_webui.add if marked else _webui.discard)(str(target_id))


def mark_internal(target_id: str, internal: bool) -> None:
    """Record whether ``target_id`` is at a browser-internal URL (``False`` once it
    is not, or is gone)."""
    with _lock:
        (_internal.add if internal else _internal.discard)(str(target_id))


def login_in_flight(instance_id: str | None = None) -> bool:
    """Whether a login is running or opening its tab in ``instance_id``, or in any
    instance when none is named (a call that names none)."""
    if instance_id is None:
        return bool(_depth or _holds)
    return instance_id in _depth or bool(_holds.get(instance_id))


def login_in_flight_on(port: int) -> bool:
    """Whether a login is running or opening its tab in the browser that listens
    on ``port``: what the guard of that browser asks before it closes a DevTools
    window, so a login in one browser never closes a person's window in another."""
    with _lock:
        return any(
            _ports.get(instance_id) in (port, None)
            for instance_id in {*_depth, *_holds}
        )


def keep(instance_id: str, target_id: str) -> None:
    """Keep ``target_id`` sealed for good in ``instance_id``: a tab the backend
    opened and could not close, that no login holds any more."""
    with _lock:
        _unclosed.setdefault(instance_id, set()).add(str(target_id))


def sealing(instance_id: str) -> bool:
    """Whether a login is in progress in ``instance_id``."""
    return instance_id in _depth


def any_sealing() -> bool:
    """Whether any instance has a login in progress, or a sealed tab left over.
    Two dict truthiness checks: the registry wrapper asks it on every tool call
    and does nothing more while it is false."""
    return bool(_depth or _unclosed or _holds or _barred or _webui)


def is_sealed(instance_id: str, target_id: str | None) -> bool:
    """Whether ``target_id`` is a sealed tab of ``instance_id``."""
    if not target_id:
        return False
    target = str(target_id)
    return (
        target in _barred
        or (target in _internal and login_in_flight(instance_id))
        or (target in _webui and vault_mode.active())
        or target in _tabs.get(instance_id, ())
        or target in _unclosed.get(instance_id, ())
        or any(target not in known for known in _holds.get(instance_id, ()))
    )


def refuse_if_sealed(tool: str, kwargs: dict[str, object]) -> None:
    """Raise :class:`ToolError` when this call must not run.

    ``kwargs`` are the call's arguments by name (``instance_id``, ``tab_id``). A
    call that names no usable instance, or an instance with no seal in progress,
    passes."""
    instance_id = kwargs.get("instance_id")
    if not isinstance(instance_id, str):
        return
    tab_id = kwargs.get("tab_id")
    if sealing(instance_id) and tool in INSTANCE_WIDE:
        raise ToolError(REFUSAL)
    if isinstance(tab_id, str):
        _refuse_tab(instance_id, tab_id)


def _refuse_tab(instance_id: str, tab_id: str, hint: str = "") -> None:
    """Raise when ``tab_id`` is a sealed tab of ``instance_id``; ``hint`` is added to
    the two sentences that name a page the caller can simply leave (a WebUI page, an
    inspector), never to "a login is running"."""
    if not is_sealed(instance_id, tab_id):
        return
    # An inspector is refused for what it is, login or not: "a login is
    # running" would be untrue and send the caller to wait for nothing.
    barred_only = tab_id in _barred and not login_in_flight(instance_id)
    if tab_id in _webui and not barred_only and not login_in_flight(instance_id):
        raise ToolError(vault_mode.webui_refusal() + hint)
    raise ToolError(INSPECTOR_REFUSAL + hint if barred_only else REFUSAL)


#: Said when a call that names no tab would act on one it may not use (T-19366
#: audit 7 F1): the caller's way out is a tab of its own.
CURRENT_TAB_HINT = (
    " The tab this call would act on is not one you opened: call new_tab, or pass "
    "the tab_id of a tab you did."
)


def refuse_current_tab(instance_id: str, tab: object) -> None:
    """Raise when the tab a call resolved WITHOUT a ``tab_id`` (the caller's bound
    tab, or the instance's current one) is sealed or barred while vault mode is on:
    a person can leave the browser on a ``chrome://`` page, and ``go_back`` can reach
    one, so ``refuse_if_sealed`` (which reads only a ``tab_id`` the call passed) is
    not the only place to ask.

    With no vault it does nothing and never touches ``tab`` (one ``lstat``). With
    one, a tab whose target id cannot be read (a rediscovered raw connection) is
    REFUSED: "cannot tell what this is" is not "allowed", and it must not surface as
    an ``AttributeError`` either."""
    if not vault_mode.active():
        return
    target_id = getattr(getattr(tab, "target", None), "target_id", None)
    if not target_id:
        raise ToolError(
            vault_mode.refusal_text(
                "a tab this backend cannot identify",
                "it cannot be shown not to be a browser-internal page",
            )
            + CURRENT_TAB_HINT
        )
    if any_sealing():
        _refuse_tab(instance_id, str(target_id), CURRENT_TAB_HINT)
