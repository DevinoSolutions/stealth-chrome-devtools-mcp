"""THE one home for opening a tab, for the targets Chrome opens, and for talking to the
browser-level connection (F-940, F-1012, T-8501).

Three places open a tab — the ``new_tab`` tool, ``BrowserManager._replace_main_tab``
(which ``navigate`` takes on every 25th navigation and whenever its tracked tab
went stale) and ``login_persistence``'s settings tab.

**nodriver 0.50 owns the lookup, so this module no longer does (T-8501).** On
0.47 a new tab was found through ``Target.targetCreated``, an event Chrome only
sends on a session that asked for it with ``Target.setDiscoverTargets``, and a
dropped-and-reconnected browser socket never asked again: the lookup died with
``RuntimeError: coroutine raised StopIteration`` (F-940, Sentry -B5/-B4/-B0). On
0.50 ``Browser.update_targets`` (``browser.py``:498) asks ``Target.getTargets``
and builds a real ``Tab(target=…, parent=browser)`` for every target it has not
seen, so opening a tab never depends on an event.

**A dropped browser socket is permanent in 0.50, so recovery is ours.** ``send``
re-attaches only when ``not self.attached or not self.socket``
(``connection.py``:397); a dropped socket is non-``None`` with ``close_code`` set,
so every later browser-level command raises ``ConnectionClosed`` for the life of
the instance. :func:`send` and :func:`update_targets` are the ONE way the rest of
the package talks to the browser connection: on a socket whose ``close_code`` is
set, or a ``ConnectionClosed``, they re-attach once (:func:`reattach`) and retry
once. ``attach`` calls ``handlers.clear()`` (``connection.py``:276), so
:func:`reattach` puts back every handler that was registered (the popup listener
below and ``python_binding``'s ``targetDestroyed`` watch are the two) and sends
``setDiscoverTargets`` again through :func:`_discover`, the same call
:func:`arm_popups` makes. A closed socket fails the send with
``ConnectionClosed``, so ``close_instance`` needs no liveness test of its own to
keep honest. A connection WE closed
(:func:`mark_closed`, set by ``close_instance`` before it ends the socket) is never
reopened: after ``aclose``
the socket is ``None``, which 0.50 would silently reopen for a lingering task.

**Discovery is ours to switch on (F-1012).** 0.50's ``Browser.start`` does NOT send
``Target.setDiscoverTargets`` (nothing in the package does), so without it Chrome
sends the browser connection no ``targetCreated`` and no ``targetDestroyed``:
measured on Chrome 154, a ``window.open`` popup raised no event until discovery
was enabled and then raised exactly one. :func:`arm_popups` enables it, once per
instance, because the popup arming below and ``python_binding``'s tab-end watch
are both listeners on those two events. Nothing in nodriver registers a target
from the event any more, so a listener that wants the ``Tab`` asks
``update_targets`` first (:func:`_arm_popup`).

**Popups (F-1012).** A tab the PAGE opens is the one kind nobody opens through
:func:`open_tab`; :func:`arm_popups` hands it to the arming home when Chrome
announces it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

from nodriver import Browser, Tab, cdp
from websockets.exceptions import ConnectionClosed

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator

_T = TypeVar("_T")

#: Strong refs to the in-flight popup arming tasks (RUF006).
_POPUP_TASKS: set[asyncio.Task[None]] = set()


@dataclass(eq=False)
class _State:
    """What this module remembers about one browser, kept ON the browser object so
    it lives and dies with it (a test double or a ``Browser`` alike)."""

    #: Set by :func:`mark_closed`: this backend ended the connection; never reopen.
    closed: bool = False
    #: One re-attach at a time: callers that find the socket dropped together must
    #: produce one new connection, not one each.
    reattaching: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The ``targetCreated`` handler :func:`arm_popups` registered, so a second call
    #: replaces it instead of adding a second one.
    popup_handler: Callable[..., None] | None = None
    #: Target ids of tabs the backend opened for itself (``login_persistence``'s
    #: settings tab), which are never a caller's page to land on.
    internal: set[str] = field(default_factory=set)


def _state(browser: Browser) -> _State:
    state = getattr(browser, "_tab_open_state", None)
    if not isinstance(state, _State):  # a Mock answers every attribute
        state = _State()
        # Not a WeakSet or an id-keyed dict: one needs a weakref-able double, the
        # other never lets go.
        browser._tab_open_state = state
    return state


#: How long one popup's arming, the switching on of discovery, or one re-attach may
#: take. A popup that closes mid-arm leaves its pending command unanswered for good
#: (nodriver's listener ends on the closed socket and resolves nothing), so without
#: a bound the task, the Tab and its websocket would be pinned for the life of the
#: instance.
POPUP_ARM_BUDGET_SECONDS = 5.0


_CLOSED_TEXT = "the browser connection was closed by this backend"


def mark_closed(browser: Browser) -> None:
    """Record that this backend ended *browser*'s connection: never reopen it.

    Called BEFORE the socket is closed (``close_instance``, ``cdp_attach.close``,
    ``devtools_port``), so a task still in flight cannot slip in between.
    """
    _state(browser).closed = True


def mark_internal(browser: Browser, tab: Tab) -> None:
    """Record that *tab* is the backend's own, not a page a caller works in."""
    _state(browser).internal.add(str(tab.target.target_id))


def visible_tabs(browser: Browser, excluding: str | None = None) -> list[Tab]:
    """``browser.tabs`` without the backend's own (:func:`mark_internal`) and
    without the target *excluding*: the tabs a caller's stored tab may fall back to
    when it is closed (nodriver drops a destroyed target only on
    ``Target.targetDestroyed``, so the closed id is excluded explicitly)."""
    skip = {*_state(browser).internal, str(excluding)}
    return [t for t in browser.tabs if str(t.target.target_id) not in skip]


def is_closed(browser: Browser) -> bool:
    """Whether :func:`mark_closed` was called for *browser*."""
    return _state(browser).closed


def _dropped(browser: Browser) -> bool:
    """Whether the socket is gone or closed — the state 0.50's ``send`` never
    repairs (``connection.py``:397 looks only at ``attached`` and ``socket``)."""
    if not hasattr(browser, "socket"):
        return False
    socket = browser.socket
    return socket is None or isinstance(getattr(socket, "close_code", None), int)


async def reattach(browser: Browser) -> None:
    """Open a new browser-level connection, keeping the handlers registered on it.

    Raises ``ConnectionError`` for a connection :func:`mark_closed` ended. Once,
    under a lock, and a no-op if another caller already did it. ``attach`` takes
    its browser branch only with no ``target`` (``connection.py``:261), so the old
    ``target`` and ``session_id`` are cleared for the call and put back if it
    fails.
    """
    if is_closed(browser):
        raise ConnectionError(_CLOSED_TEXT)
    async with _state(browser).reattaching:
        if is_closed(browser):
            raise ConnectionError(_CLOSED_TEXT)
        if not _dropped(browser):
            return
        handlers = {kind: list(each) for kind, each in browser.handlers.items()}
        target, session_id = browser.target, browser.session_id
        await browser.aclose()
        browser.target = browser.session_id = None
        try:
            await asyncio.wait_for(browser.attach(), POPUP_ARM_BUDGET_SECONDS)
        except BaseException:
            await browser.aclose()
            browser.target, browser.session_id = target, session_id
            raise
        for kind, each in handlers.items():
            for callback in each:
                browser.add_handler(kind, callback)
        if handlers.get(cdp.target.TargetCreated) or handlers.get(
            cdp.target.TargetDestroyed
        ):
            await _discover(browser)
        debug_logger.log_info(
            "tab_open", "reattach", "re-attached a dropped browser connection"
        )


async def _live(browser: Browser, call: Callable[[], Awaitable[_T]]) -> _T:
    """Run *call*, re-attaching first if the socket dropped, and once more if the
    write finds it closed (``ConnectionClosed`` comes from the write, so nothing was
    sent).

    A send already IN FLIGHT when the link drops is not retried: its caller gets the
    builtin ``ConnectionError`` ``_fail_pending_futures`` sets (``connection.py``:480)
    because ``createTarget`` and ``closeTarget`` are not idempotent and Chrome may
    have run them. While a close is still in progress ``close_code`` is ``None``, so
    :func:`reattach` finds nothing to do and the retried write fails once more; the
    failure is raised, and the next call finds the code set.
    """
    if is_closed(browser):
        raise ConnectionError(_CLOSED_TEXT)
    if _dropped(browser):
        await reattach(browser)
    try:
        return await call()
    except ConnectionClosed:
        await reattach(browser)
        return await call()


async def send(
    browser: Browser, command: Callable[..., Generator[dict, dict, _T]], *args: object
) -> _T:
    """Send ``command(*args)`` on the BROWSER connection; return its parsed reply.

    The builder and its arguments, not the command: a CDP command is a generator,
    spent by the first attempt, and the retry needs a new one.
    """
    return await _live(browser, lambda: browser.send(command(*args)))


async def update_targets(browser: Browser) -> None:
    """``Browser.update_targets`` over a connection that is re-attached if dropped."""
    await _live(browser, browser.update_targets)


async def open_tab(browser: Browser, url: str, *, background: bool = False) -> Tab:
    """Open *url* in a new tab of *browser* and return its ``Tab``.

    The arguments to ``createTarget`` are nodriver's own, so the tab is the one
    ``Browser.get(url, new_tab=True)`` would have opened; ``update_targets`` then
    registers it, exactly as that call does.

    A tab is opened in front by default, and the tab it covers stops reporting its
    window: on LINUX a headless page's ``window.outerHeight`` falls to its
    ``innerHeight`` for as long as another tab of the same window is in front
    (measured on Chrome 155, 900x600 requested: 600 before, 513 with an
    ``about:blank`` tab opened in front, 600 again once it closed; Windows headless
    read 600 in all three runs). *background* opens it behind, which leaves the
    caller's page and its window metrics alone. It is for the tab
    ``login_persistence``'s exchange opens for itself; a tab a caller asked for is
    theirs to look at, and the other tabs the backend opens on its own initiative
    (``_replace_main_tab``, ``tab_binding``) stay in front.
    """
    target_id = await send(
        browser,
        lambda: cdp.target.create_target(
            url,
            new_window=False,
            enable_begin_frame_control=True,
            background=True if background else None,
        ),
    )
    await update_targets(browser)
    tab = find(browser, str(target_id))
    if tab is None:
        raise ToolError(
            f"Chrome opened a tab (target {target_id}) but did not list it "
            "among its targets; it may have closed again at once."
        )
    return tab


async def _discover(browser: Browser) -> None:
    """Switch on Target discovery, which is what makes Chrome send
    ``targetCreated`` and ``targetDestroyed`` at all. Bounded; raises on failure."""
    await asyncio.wait_for(
        browser.send(cdp.target.set_discover_targets(True)), POPUP_ARM_BUDGET_SECONDS
    )


async def arm_popups(
    browser: Browser, arm: Callable[..., Awaitable[None]], *args: object
) -> None:
    """Arm every popup the PAGE opens, the moment Chrome creates it (F-1012).

    ``window.open`` and ``target=_blank`` make a target no tool asked for, so
    nothing armed it until a caller ``switch_tab``-ed onto it, and until then it was
    hidden: no ``rAF``, 1 Hz timers (F-1011). This listens for
    ``Target.targetCreated`` on the browser connection and calls ``arm(*args, tab)``;
    callers pass the ONE arming home (``BrowserManager._arm_tracked_tab``), which a
    later ``switch_tab`` reaches again harmlessly. Call it AFTER the instance is
    stored: arming a popup of an instance ``get_instance`` does not know yet is
    skipped. Only a page target with an ``openerId`` is a popup: a tab ``open_tab``
    creates has none and is armed by its caller.

    Idempotent per browser: a second call replaces the first call's listener. It
    also turns on Target discovery (see the module docstring). Enabling it makes
    Chrome replay one ``targetCreated`` per EXISTING target, so a popup that was
    already open when a browser is re-attached (F-888) is armed too. A browser that
    does not answer is logged and left without popup arming, not failed: the
    instance works without it. Discovery is also what ends a closed tab's paused
    hook handlers (``DynamicHookSystem._listen_for_destroyed``), which the warning
    says.
    """

    def on_created(event: cdp.target.TargetCreated, *_: object) -> None:
        info = event.target_info
        if info.type_ != "page" or info.opener_id is None or is_closed(browser):
            return
        task = asyncio.create_task(_arm_popup(browser, info.target_id, arm, args))
        _POPUP_TASKS.add(task)
        task.add_done_callback(_POPUP_TASKS.discard)

    state = _state(browser)
    if state.popup_handler is not None:
        browser.remove_handler(cdp.target.TargetCreated, state.popup_handler)
    state.popup_handler = on_created
    browser.add_handler(cdp.target.TargetCreated, on_created)
    try:
        await _discover(browser)
    except Exception as error:  # noqa: BLE001  PERMANENT(discovery is best effort: an instance without popup arming still works, and the reason is logged)
        debug_logger.log_warning(
            "tab_open",
            "arm_popups",
            f"could not enable Target discovery ({type(error).__name__}); "
            "popups will not be armed on arrival, and a closed tab's paused-request "
            "handlers will wait for Chrome to end its session, not end at once",
        )


async def _arm_popup(
    browser: Browser,
    target_id: cdp.target.TargetID,
    arm: Callable[..., Awaitable[None]],
    args: tuple[object, ...],
) -> None:
    try:
        # Nothing registers a target from the event: ask Chrome for it.
        await asyncio.wait_for(update_targets(browser), POPUP_ARM_BUDGET_SECONDS)
        tab = find(browser, str(target_id))
        if tab is not None and not is_closed(browser):
            await asyncio.wait_for(arm(*args, tab), POPUP_ARM_BUDGET_SECONDS)
    except TimeoutError:
        debug_logger.log_warning(
            "tab_open",
            "_arm_popup",
            f"arming a popup took over {POPUP_ARM_BUDGET_SECONDS:g} s; gave up "
            "(it most likely closed first)",
        )
    except Exception as error:  # noqa: BLE001  PERMANENT(a popup that closed or refused is not worth a task error: the tab works, arming is best effort)
        debug_logger.log_warning(
            "tab_open", "_arm_popup", f"{type(error).__name__}: {error}"
        )


def find(browser: Browser, tab_id: str) -> Tab | None:
    """The open tab whose target id is *tab_id*, as a ``Tab``; ``None`` if none.

    Reads what ``Browser.update_targets`` has registered (F-962: a caller's page
    tools act on any tab it picks), so a caller that may be ahead of it asks
    ``update_targets`` first.
    """
    return next(
        (t for t in browser.tabs if str(t.target.target_id) == str(tab_id)), None
    )
