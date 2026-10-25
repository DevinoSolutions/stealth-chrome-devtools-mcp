"""THE one home for opening a tab and for the targets Chrome opens (F-940, F-1012).

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
seen, so opening a tab never depends on an event. The ``Tab`` registered by an
event, the bare ``Connection`` fallback and the replace-in-place bookkeeping that
F-940 and F-771/F-775 needed are gone with the thing they worked around.

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
from typing import TYPE_CHECKING

from nodriver import Browser, Tab, cdp

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

#: Strong refs to the in-flight popup arming tasks (RUF006).
_POPUP_TASKS: set[asyncio.Task[None]] = set()

#: How long one popup's arming, or the switching on of discovery, may take. A popup
#: that closes mid-arm leaves its pending command unanswered for good (nodriver's
#: listener ends on the closed socket and resolves nothing), so without a bound the
#: task, the Tab and its websocket would be pinned for the life of the instance.
POPUP_ARM_BUDGET_SECONDS = 5.0


async def open_tab(browser: Browser, url: str) -> Tab:
    """Open *url* in a new tab of *browser* and return its ``Tab``.

    The arguments to ``createTarget`` are nodriver's own, so the tab is the one
    ``Browser.get(url, new_tab=True)`` would have opened; ``update_targets`` then
    registers it, exactly as that call does.
    """
    target_id = await browser.send(
        cdp.target.create_target(url, new_window=False, enable_begin_frame_control=True)
    )
    await browser.update_targets()
    tab = find(browser, str(target_id))
    if tab is None:
        raise ToolError(
            f"Chrome opened a tab (target {target_id}) but did not list it "
            "among its targets; it may have closed again at once."
        )
    return tab


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

    It also turns on Target discovery (see the module docstring), which is what
    makes Chrome send ``targetCreated`` and ``targetDestroyed`` at all. Enabling it
    makes Chrome replay one ``targetCreated`` per EXISTING target, so a popup that
    was already open when a browser is re-attached (F-888) is armed too. A browser
    that does not answer is logged and left without popup arming, not failed: the
    instance works without it.
    """

    def on_created(event: cdp.target.TargetCreated, *_: object) -> None:
        info = event.target_info
        if info.type_ != "page" or info.opener_id is None:
            return
        task = asyncio.create_task(_arm_popup(browser, info.target_id, arm, args))
        _POPUP_TASKS.add(task)
        task.add_done_callback(_POPUP_TASKS.discard)

    browser.add_handler(cdp.target.TargetCreated, on_created)
    try:
        await asyncio.wait_for(
            browser.send(cdp.target.set_discover_targets(True)),
            POPUP_ARM_BUDGET_SECONDS,
        )
    except Exception as error:  # noqa: BLE001  PERMANENT(discovery is best effort: an instance without popup arming still works, and the reason is logged)
        debug_logger.log_warning(
            "tab_open",
            "arm_popups",
            f"could not enable Target discovery ({type(error).__name__}); "
            "popups will not be armed on arrival",
        )


async def _arm_popup(
    browser: Browser,
    target_id: cdp.target.TargetID,
    arm: Callable[..., Awaitable[None]],
    args: tuple[object, ...],
) -> None:
    try:
        # Nothing registers a target from the event: ask Chrome for it.
        await asyncio.wait_for(browser.update_targets(), POPUP_ARM_BUDGET_SECONDS)
        tab = find(browser, str(target_id))
        if tab is not None:
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
