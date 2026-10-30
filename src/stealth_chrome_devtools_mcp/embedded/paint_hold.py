"""THE one home for keeping a driven background tab painting (F-1019).

F-1011's focus emulation makes a background tab READ ``visible``, but the
compositor of a hidden widget still makes no frames for a page that has something to
paint: such a page got ~1 frame per second from ``requestAnimationFrame`` against
~120 per 2 s in the foreground (T-5903; a page that changes nothing was never held
back, which is why a bare ``rAF`` counter missed it). ``Page.startScreencast`` marks
the tab as captured, and Chrome then keeps its compositor producing. With an
``everyNthFrame`` that is never reached no frame is encoded and no
``Page.screencastFrame`` event is sent (measured: 0 events, no CPU beyond the page's
own painting).

**That painting is the cost.** A painting background tab runs at the page's rate
instead of 1 Hz: 3 such tabs held cost ~140% of a core against ~5% unheld, 10 tabs
~138% against ~10% (F-1019). And a hold is not confined to its tab: measured with 4
painting tabs, holding one took it from 1 to 50 fps and its three siblings from 1 to
~20 fps each, back to 3 on release, so while ANY tab is held the painting tabs of its
window run too. So the hold is not given to every tab. It goes to a tab a TOOL is
acting on (:func:`hold`, called from ``tab_binding.tab_for_caller``, the one place
every page tool resolves its tab) and ends :data:`LINGER_SECONDS` after the last call
on it; a window nobody drives stays throttled. A tab closed through ``close_tab`` is
forgotten (:func:`forget`); one that closed any other way or whose browser went away
is released when its timer expires, which is at most one linger.

One timer task per held tab, all in the event loop, so there is no lock: a call on a
held tab only moves its deadline; the entry is claimed BEFORE the first await, so
concurrent calls on one tab send one start. Best effort throughout: a refusal is
logged, never raised. A start that is cancelled or times out may still reach Chrome,
so its entry keeps a timer that sends the stop (and a wedged tab pays the budget once,
not on every call); a start Chrome refused leaves no entry, and only the hold's own
entry is ever dropped, never a newer one. A caller's own ``Page.startScreencast`` via
``execute_cdp_command`` takes the tab over (:func:`note_command`): no hold, no stop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nodriver as uc

from stealth_chrome_devtools_mcp.embedded import tab_open
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from nodriver import Browser, Tab

#: How long a tab stays held after the last tool call on it. A module constant (and
#: the test seam), not a setting: a longer one keeps a finished tab painting.
LINGER_SECONDS = 10.0

#: ``Page.startScreencast`` ``everyNthFrame`` that is never reached (an int32 maximum
#: is over a year of 60 Hz frames), so the hold costs no encoded frame and no event.
_NEVER_A_FRAME = 2**31 - 1

#: The most one hold command may cost the tool call it runs ahead of: a wedged
#: renderer must cost every page tool two seconds and a warning, never a hang.
SEND_BUDGET_S = 2.0


@dataclass
class _Hold:
    tab: Tab
    deadline: float
    task: asyncio.Task[None] | None = field(default=None)


#: target id -> the hold on it. Entries leave through :func:`_expire` or :func:`forget`.
_held: dict[str, _Hold] = {}

#: Targets whose caller started a screencast of their own (``execute_cdp_command``):
#: Chrome has one screencast per tab, so the hold must neither replace it nor stop it.
#: The state lives ON the tab's browser (``tab_open.set_caller_owned``), so it goes
#: with the browser however it goes. This set is only for a tab with no browser (a
#: hand-built one), removed by :func:`forget` and the caller's stop.
_browserless_caller_owned: set[str] = set()


def _is_caller_owned(tab: Tab, key: str) -> bool:
    browser = getattr(tab, "browser", None)
    if browser is None:
        return key in _browserless_caller_owned
    return tab_open.is_caller_owned(browser, key)


def _set_caller_owned(tab: Tab, key: str, owned: bool) -> None:
    browser = getattr(tab, "browser", None)
    if browser is not None:
        tab_open.set_caller_owned(browser, key, owned)
    elif owned:
        _browserless_caller_owned.add(key)
    else:
        _browserless_caller_owned.discard(key)


def _key(tab: Tab) -> str:
    return str(getattr(getattr(tab, "target", None), "target_id", ""))


async def hold(tab: Tab) -> None:
    """Keep *tab* painting until :data:`LINGER_SECONDS` after the last call."""
    key = _key(tab)
    entry: _Hold | None = None
    try:
        now = asyncio.get_running_loop().time()
        if _is_caller_owned(tab, key):
            return
        current = _held.get(key)
        if current is not None and current.tab is tab:
            current.deadline = now + LINGER_SECONDS
            return
        if current is not None:
            await _release_rediscovered(key, current)
        entry = _held[key] = _Hold(tab, now + LINGER_SECONDS)
        await asyncio.wait_for(
            tab.send(
                uc.cdp.page.start_screencast(
                    format_="jpeg", quality=1, every_nth_frame=_NEVER_A_FRAME
                )
            ),
            SEND_BUDGET_S,
        )
        _arm(key, entry)
    except BaseException as error:  # a cancelled start leaves no stuck entry
        # Cancelled or timed out, the start may still reach Chrome (the transport
        # shields it): keep the entry and let the timer send the stop. That also
        # makes a wedged tab cost its first call the budget, not every call.
        if isinstance(error, (asyncio.CancelledError, TimeoutError)):
            _arm(key, entry)
        elif entry is not None and _held.get(key) is entry:
            del _held[key]  # Chrome refused the start: nothing to release
        if isinstance(error, Exception):
            debug_logger.log_warning(
                "paint_hold", "hold", f"{type(error).__name__}: {error}"
            )
        else:
            raise


def _arm(key: str, entry: _Hold | None) -> None:
    """Give *entry* its release timer, if it is still the hold on *key*."""
    if entry is not None and entry.task is None and _held.get(key) is entry:
        entry.task = asyncio.create_task(_expire(key, entry))


async def _release_rediscovered(key: str, old: _Hold) -> None:
    """A different Tab object for one target is a new CDP session (a rediscovered
    target): end the old session's timer and, if its connection is still open, its
    screencast too (bounded, best effort) rather than assume it died with it."""
    forget(key)
    if not getattr(old.tab, "closed", False):
        await _stop(old.tab, "_release_rediscovered")


async def _stop(tab: Tab, where: str) -> None:
    try:
        await asyncio.wait_for(tab.send(uc.cdp.page.stop_screencast()), SEND_BUDGET_S)
    except Exception as error:  # noqa: BLE001  PERMANENT(the tab usually closed first: nothing to release, and the timer must end either way)
        debug_logger.log_info("paint_hold", where, f"{type(error).__name__}: {error}")


def forget(target_id: str, browser: Browser | None = None) -> None:
    """Drop the hold on a tab that was closed: its timer ends, nothing is sent.
    Given the tab's *browser*, its caller-owned mark goes too."""
    _browserless_caller_owned.discard(str(target_id))
    if browser is not None:
        tab_open.set_caller_owned(browser, str(target_id), False)
    entry = _held.pop(str(target_id), None)
    if entry is not None and entry.task is not None:
        entry.task.cancel()


def note_command(tab: Tab, method: object) -> None:
    """A caller's ``Page.startScreencast`` / ``stopScreencast`` (from
    ``execute_cdp_command``, the one place a caller's CDP command is dispatched):
    Chrome has one screencast per tab, so theirs replaces the hold, which then
    leaves the tab alone (no start over it, no stop that would end it) until they
    stop it. Their stop ends the hold's too; the next call holds again."""
    started = method is uc.cdp.page.start_screencast
    if not started and method is not uc.cdp.page.stop_screencast:
        return
    key = _key(tab)
    forget(key)
    _set_caller_owned(tab, key, started)


async def _expire(key: str, entry: _Hold) -> None:
    loop = asyncio.get_running_loop()
    while (remaining := entry.deadline - loop.time()) > 0:  # noqa: ASYNC110  PERMANENT(a deadline every call moves; an Event would need a reset per call)
        await asyncio.sleep(remaining)
    if _held.get(key) is not entry:
        return
    del _held[key]
    await _stop(entry.tab, "_expire")
