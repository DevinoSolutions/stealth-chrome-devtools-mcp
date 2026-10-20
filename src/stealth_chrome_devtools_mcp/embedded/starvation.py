"""Is a probe that timed out a FROZEN browser, or a STARVED one (F-1007)?

``browser_health`` probes a ``background: true`` target, and Chrome runs a renderer
that nothing is looking at at IDLE priority. On a host whose CPU is saturated the
``Runtime.evaluate`` of a perfectly healthy browser then times out (measured: 60 s
with 48 spinners), and two such rounds in a row would close and relaunch a browser a
person is signed in to.

The browser PROCESS runs at normal priority and still answers a fresh connection's
``Browser.getVersion`` in milliseconds, so a renderer-level timeout alone cannot tell
the two apart: F-963's freeze left exactly that too. What can is the HOST. A probe
failure counts as a starved browser, and is not counted toward the heal, only when
both hold: the browser process answers, and the machine's CPU is saturated
(:data:`SATURATED_PERCENT` over :data:`SAMPLE_SECONDS`). Either one failing keeps the
old verdict, so a browser whose process is dead too, or a freeze on an idle machine,
heals exactly as before; and once the host calms the next failed probe counts.

A leaf: stdlib, ``psutil`` and ``debug_logger``. The browser-level question and the
host-load reading arrive as arguments, so the one place that decides is hermetic.
"""

import asyncio
from collections.abc import Awaitable, Callable

import psutil

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

#: Machine-wide CPU use (percent) at or above which an unanswered renderer probe is
#: put down to the host. A saturated machine is the one the measurement was taken on.
SATURATED_PERCENT = 90.0

#: The window the machine's CPU use is averaged over.
SAMPLE_SECONDS = 1.0

#: How long the browser process gets to answer ``Browser.getVersion``. A process
#: that needs longer than this is not answering in a way that excuses its renderers.
BROWSER_ANSWER_SECONDS = 5.0


def host_cpu_percent() -> float:
    """Machine-wide CPU use over :data:`SAMPLE_SECONDS`. Blocks that long, so it is
    only ever called off the event loop."""
    return psutil.cpu_percent(interval=SAMPLE_SECONDS)


async def verdict(
    browser_answers: Callable[[], Awaitable[None]],
    host_load: Callable[[], float] = host_cpu_percent,
) -> str | None:
    """``None`` unless the browser process answers while the host is saturated;
    then a counts-only phrase for the log (``host cpu 97%``). Never raises: a load
    that cannot be read is no excuse."""
    try:
        await asyncio.wait_for(browser_answers(), BROWSER_ANSWER_SECONDS)
        percent = await asyncio.to_thread(host_load)
    except Exception as error:  # noqa: BLE001  PERMANENT(F-1007: every way of not answering keeps the old verdict, the heal)
        debug_logger.log_debug(
            "starvation", "verdict", f"not starved: {type(error).__name__}"
        )
        return None
    return f"host cpu {percent:.0f}%" if percent >= SATURATED_PERCENT else None
