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
logged, never raised.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nodriver as uc

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from nodriver import Tab

#: How long a tab stays held after the last tool call on it. A module constant (and
#: the test seam), not a setting: a longer one keeps a finished tab painting.
LINGER_SECONDS = 10.0

#: ``Page.startScreencast`` ``everyNthFrame`` that is never reached (an int32 maximum
#: is over a year of 60 Hz frames), so the hold costs no encoded frame and no event.
_NEVER_A_FRAME = 2**31 - 1


@dataclass
class _Hold:
    tab: Tab
    deadline: float
    task: asyncio.Task[None] | None = field(default=None)


#: target id -> the hold on it. Entries leave through :func:`_expire` or :func:`forget`.
_held: dict[str, _Hold] = {}


async def hold(tab: Tab) -> None:
    """Keep *tab* painting until :data:`LINGER_SECONDS` after the last call."""
    try:
        key = str(tab.target.target_id)
        deadline = asyncio.get_running_loop().time() + LINGER_SECONDS
        current = _held.get(key)
        if current is not None and current.tab is tab:
            current.deadline = deadline
            return
        # A different Tab object for the same target is a new CDP session (a
        # rediscovered target): the old session's screencast died with it.
        forget(key)
        entry = _held[key] = _Hold(tab, deadline)
        await tab.send(
            uc.cdp.page.start_screencast(
                format_="jpeg", quality=1, every_nth_frame=_NEVER_A_FRAME
            )
        )
        if _held.get(key) is entry:
            entry.task = asyncio.create_task(_expire(key, entry))
    except Exception as error:  # noqa: BLE001  PERMANENT(a paint hold must never cost a tool call: the tab works throttled, so a refusal is logged only)
        _held.pop(str(getattr(getattr(tab, "target", None), "target_id", "")), None)
        debug_logger.log_warning(
            "paint_hold", "hold", f"{type(error).__name__}: {error}"
        )


def forget(target_id: str) -> None:
    """Drop the hold on a tab that was closed: its timer ends, nothing is sent."""
    entry = _held.pop(str(target_id), None)
    if entry is not None and entry.task is not None:
        entry.task.cancel()


async def _expire(key: str, entry: _Hold) -> None:
    loop = asyncio.get_running_loop()
    while (remaining := entry.deadline - loop.time()) > 0:  # noqa: ASYNC110  PERMANENT(a deadline every call moves; an Event would need a reset per call)
        await asyncio.sleep(remaining)
    if _held.get(key) is not entry:
        return
    del _held[key]
    try:
        await entry.tab.send(uc.cdp.page.stop_screencast())
    except Exception as error:  # noqa: BLE001  PERMANENT(the tab usually closed first: nothing to release, and the timer must end either way)
        debug_logger.log_info(
            "paint_hold", "_expire", f"{type(error).__name__}: {error}"
        )
