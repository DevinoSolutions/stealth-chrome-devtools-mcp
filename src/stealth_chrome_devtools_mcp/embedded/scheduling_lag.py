"""THE one home for "was this process scheduled fairly, and what does a time
budget owe it when it was not" (F-856).

Every liveness verdict in this tree bottoms out in a wall-clock deadline. That
arithmetic says something about the BACKEND only while the prober itself is
being scheduled: a timeout means "it did not answer in 10 seconds" only if we
were awake to hear an answer for most of those 10 seconds. On 2026-09-02, on a
machine at 3,445 processes with the CPU pegged at 100%, we were not.
``singleton._same_identity_backend_ready`` spent its ``REUSE_PATIENCE_SECONDS``
without hearing the backend that had answered its six previous confirmations,
condemned it, and every Claude Code session sharing it saw CONNECTION_CLOSED.

**The signal is self-measured, and it is free.** The reuse gate's wait loop
already sleeps between probes. That sleep IS the calibration probe: ask for
0.25s, wake at 0.25s + delta, and ``actual / requested`` is this process's own
scheduling lag — no psutil, no system-wide CPU poll, no background thread, and
nothing that can be true of a machine that is not actually starving us. On an
idle machine the ratio is 1.0 and :class:`FairWindow` is, line for line, the
wall-clock deadline it replaced.

**Patience is charged in fair seconds.** Elapsed wall time is divided by the
observed lag factor before it is deducted from the budget: a second observed
while the scheduler was delivering a third of our requested timing costs a
third of a second of patience. Because the factor is clamped to
``MAX_STRETCH``, the window can never outlive ``patience * MAX_STRETCH`` of
wall time — elastic, never infinite, and terminating without a second stopping
rule to keep in step with the first.

**A second witness for a prober whose target is starved BY PRIORITY (F-1007).**
``browser_health``'s probe target is a background renderer, which Chrome runs at IDLE
priority. The backend itself runs at NORMAL priority, so on a saturated host its own
sleeps wake on time (measured 2026-10-20: 64 normal-priority spinners on 32 cpus left
``asyncio.sleep`` lag at 1.05x while an IDLE-priority counter ran at 4% of its idle
rate): the self-measured signal above cannot see that starvation. :class:`IdleCanary`
measures what starves the renderer: a tiny child process that lowers ITS OWN priority
class to IDLE and wakes every :data:`CANARY_NAP_SECONDS`, running DURING the probe
window. ``factor`` is the window's length over the time the wakes it kept would have
taken, so a canary that kept up reads 1.0 and one that got a quarter of its wakes
reads 4.0 (clamped to :data:`MAX_STRETCH`), judged by the same :func:`is_starved`.
Anything it cannot establish (no child, no ready line, an exit, a window under
:data:`CANARY_MIN_WINDOW_SECONDS`) is ``None``: no evidence, never an excuse.

**What it is not.** It never decides that anything is alive or dead. It only
answers "has this window been spent", so the ONE gate that owns the dead-vs-busy
policy keeps owning it, and ``proxy_selfheal``'s single heal path is untouched:
starvation moves when a verdict is reached, never what happens after one.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import backend_eviction
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.observability import capture_lifecycle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


# The one timing seam, as module functions rather than constructor arguments:
# one place for a test to steer, and no second way to inject a clock.
def _now() -> float:
    return time.monotonic()


def _wait(seconds: float) -> None:
    time.sleep(seconds)


# How much wall time a window may buy, as a multiple of its patience. Sized
# from the incident that produced this module: the confirmation windows that
# SUCCEEDED did so repeatedly across eleven minutes, so the backend was
# answering intermittently at a cadence a modestly wider window covers, and the
# one that failed needed more than 60s under a load that was stretching this
# process's own sleeps several-fold. 4x also stays honest against the budgets
# around it — the proxy's BACKEND_READY_TIMEOUT is 120s, in-flight calls simply
# wait while the window is stretched, and the outcome being avoided is not a
# slow call but a hard teardown of every session on the backend. Deliberately
# NOT a STEALTH_MCP_* knob: an operator cannot know their own scheduler's lag
# better than the process measuring it (F-853's rule, same reasoning).
MAX_STRETCH = 4.0
# Lag worth telling someone about. Below this the stretch is noise and the
# report would fire on healthy machines, which is how a warning stops meaning
# anything.
REPORT_FACTOR = 2.0
# What the IDLE canary asks of the scheduler between two wakes. Short enough that a
# probe window holds ~100 of them, long enough that a fair scheduler keeps timer
# granularity (about 15 ms on Windows) well under :data:`REPORT_FACTOR`.
CANARY_NAP_SECONDS = 0.1
# A window shorter than this says nothing: a probe that timed out inside it was not
# waiting on a scheduler (the probe's own timeout is 15 s).
CANARY_MIN_WINDOW_SECONDS = 3.0
# How long ``start`` waits for the child to announce itself, so the window a probe
# is read over always covers the WHOLE probe (the child starts at NORMAL priority,
# but behind a launcher and a fresh interpreter, which a saturated host stretches).
# A canary that has not announced itself in this time gives no reading, and the
# probe is no later than it is without one by more than this.
CANARY_READY_SECONDS = 5.0
# The POSIX nice value Chromium gives a backgrounded renderer on desktop Linux
# (``kBackgroundPriority = 5`` in base/process/process_linux.cc; 19 is ChromeOS
# only). Windows is IDLE_PRIORITY_CLASS, Chromium's ``Priority::kBestEffort``
# (base/process/process_win.cc). macOS backgrounds by task policy, which a nice
# value only approximates.
CANARY_NICE = 5
# The child outlives its parent by at most this, whatever happens to the parent.
CANARY_LIFETIME_SECONDS = 60.0
# What stop() grants the child to be reaped, and what factor() grants the reader to
# drain the pipe (a wake the loop has not read yet is not a missing wake).
CANARY_REAP_SECONDS = 2.0
CANARY_DRAIN_SECONDS = 0.05
# The child. ``ready`` is printed at NORMAL priority, before the class is lowered, so
# that a starved canary is told apart from one that never started; a failed lowering
# exits, which the parent reads as "no evidence". POSIX lowers only the CHILD (never
# the backend), and a pipe that closes ends it.
_CANARY_SOURCE = f"""
import os, sys, time
print("ready", flush=True)
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x40):
        sys.exit(1)
else:
    os.setpriority(os.PRIO_PROCESS, 0, {CANARY_NICE})
end = time.monotonic() + {CANARY_LIFETIME_SECONDS}
while time.monotonic() < end:
    time.sleep({CANARY_NAP_SECONDS})
    print(".", flush=True)
"""
# F-827 vocabulary. A stretched window is a DECISION — "I declined to believe
# this timeout" — and without it the CONDEMNED/HEALED/TEARDOWN series has no
# denominator for how often starvation nearly caused one.
STRETCHED_EVENT = "proxy: patience extended under starvation"


def is_starved(factor: float | None) -> bool:
    """True when a lag factor is material: at or above :data:`REPORT_FACTOR`. THE
    one threshold, so the report below and every caller agree on what "starved"
    means. ``None`` (no reading) is never starved."""
    return factor is not None and factor >= REPORT_FACTOR


async def _spawn_canary() -> asyncio.subprocess.Process:
    """The one seam for starting the child, so a test steers it."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        _CANARY_SOURCE,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


class IdleCanary:
    """An IDLE-priority child woken every :data:`CANARY_NAP_SECONDS`, counted while
    a probe runs. Use it as ``async with idle_canary() as canary`` around the probe
    and read ``await canary.factor()`` before leaving. Never raises."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ready_at: float | None = None
        self._announced = asyncio.Event()  # set when the child spoke or the pipe ended
        self._wakes = 0

    async def start(self) -> None:
        """Spawn the child and wait up to :data:`CANARY_READY_SECONDS` for it to
        announce itself; one that does not is stopped, and ``factor`` says nothing."""
        try:
            self._proc = await _spawn_canary()
            self._reader = asyncio.create_task(self._count(self._proc))
            await asyncio.wait_for(self._announced.wait(), CANARY_READY_SECONDS)
        except Exception as error:  # noqa: BLE001  PERMANENT(F-1007: a canary that cannot start is no evidence, never an error in the probe loop)
            reason = type(error).__name__
        else:
            if self._ready_at is not None:
                return
            reason = "exited before announcing itself"
        await self.stop()
        debug_logger.log_debug("scheduling_lag", "canary", f"not started: {reason}")

    async def _count(self, proc: asyncio.subprocess.Process) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            async for line in stream:
                if self._ready_at is None:
                    self._ready_at = _now()
                    self._announced.set()
                elif line.strip() == b".":
                    self._wakes += 1
        except (OSError, ValueError) as error:
            debug_logger.log_debug(
                "scheduling_lag", "canary", f"pipe ended: {type(error).__name__}"
            )
        finally:
            self._announced.set()  # a pipe that ended without a word ends the wait

    async def factor(self) -> float | None:
        """How starved the canary was since it announced itself, as ``expected
        wakes / kept wakes`` clamped to ``[1.0, MAX_STRETCH]``, or ``None`` when it
        cannot say."""
        return await self.factor_since(0, None)

    def window(self) -> CanaryWindow:
        """A reading of THIS canary from now on (T-1665): a canary shared by many
        probes must not let a burst before one probe's window excuse that probe."""
        return CanaryWindow(self, self._wakes, _now())

    async def factor_since(self, wakes: int, start: float | None) -> float | None:
        """:meth:`factor` over the window that began at *start* with *wakes* already
        kept (``None``: the whole life of the canary)."""
        await asyncio.sleep(CANARY_DRAIN_SECONDS)
        if self._proc is None or self._ready_at is None:
            return None
        if self._proc.returncode is not None:
            return None  # it exited: it did not get to run, which is no evidence
        window = _now() - (self._ready_at if start is None else start)
        if window < CANARY_MIN_WINDOW_SECONDS:
            return None
        kept = max(self._wakes - wakes, 1) * CANARY_NAP_SECONDS
        return min(max(window / kept, 1.0), MAX_STRETCH)

    async def stop(self) -> None:
        proc, reader = self._proc, self._reader
        self._proc = self._reader = None
        if reader is not None:
            reader.cancel()
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), CANARY_REAP_SECONDS)
            except TimeoutError:
                debug_logger.log_warning(
                    "scheduling_lag", "canary", f"pid {proc.pid} not reaped yet"
                )


class CanaryWindow:
    """An :class:`IdleCanary` read from the moment :meth:`IdleCanary.window` took it:
    ``await window.factor()`` is the canary's factor over THAT stretch only."""

    def __init__(self, canary: IdleCanary, wakes: int, start: float) -> None:
        self._canary = canary
        self._wakes = wakes
        self._start = start

    async def factor(self) -> float | None:
        return await self._canary.factor_since(self._wakes, self._start)


@contextlib.asynccontextmanager
async def idle_canary() -> AsyncIterator[IdleCanary]:
    """An :class:`IdleCanary` that is started on entry and killed on exit."""
    canary = IdleCanary()
    try:
        await canary.start()  # inside, so a cancel during the spawn still reaps it
        yield canary
    finally:
        await canary.stop()


class ExcuseBudget:
    """How long "starved, not dead" may be believed about one subject in a row.

    Starvation excuses a failed probe; it must not do so forever, or a real freeze
    that merely coincides with a busy host is never acted on. A run of excuses is
    honoured for :data:`backend_eviction.STARVED_CEILING_SECONDS` (F-967's "slow, not
    wedged" ceiling, the one such number), measured from the run's first excuse by
    the *clock* it is given; :meth:`reset` ends the run (a healthy round, a heal)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._since: dict[str, float] = {}

    def grant(self, key: str) -> float | None:
        """Seconds the run for *key* has lasted, or ``None`` once it has lasted
        longer than the ceiling (a round AT the ceiling is still excused)."""
        spent = self._clock() - self._since.setdefault(key, self._clock())
        return spent if spent <= backend_eviction.STARVED_CEILING_SECONDS else None

    def reset(self, key: str) -> None:
        self._since.pop(key, None)

    def tracked(self) -> set[str]:
        return set(self._since)


class FairWindow:
    """A patience window whose budget is spent in fairly scheduled seconds.

    Used exactly as the wall-clock deadline it replaced::

        window = FairWindow(patience)
        while not probe():
            if window.expired():
                return False
            window.nap(0.25)

    :meth:`nap` is both the pause between probes and the measurement that pays
    for it; :meth:`expired` charges the time since it was last asked. A window
    that is never napped therefore never stretches — which is what makes the
    single-shot discovery path (``patience=0.0``) identical to what it was.
    """

    def __init__(self, patience: float) -> None:
        self._patience = float(patience)
        self._budget = float(patience)
        self._started = _now()
        self._mark = self._started
        self._factor = 1.0
        self._peak = 1.0
        self._reported = False

    def expired(self) -> bool:
        """True once the budget is spent. Charges the time since the last ask,
        discounted by the lag measured most recently — so the discount always
        rests on an observation already made, never on one the caller hopes
        for."""
        now = _now()
        self._budget -= (now - self._mark) / self._factor
        self._mark = now
        if self._budget > 0.0 and now - self._started > self._patience:
            self._report(now - self._started)
        return self._budget <= 0.0

    def nap(self, seconds: float) -> None:
        """Pause between probes, and measure what the pause actually cost."""
        if seconds <= 0.0:
            return
        before = _now()
        _wait(seconds)
        self._factor = min(max((_now() - before) / seconds, 1.0), MAX_STRETCH)
        self._peak = max(self._peak, self._factor)

    def _report(self, elapsed: float) -> None:
        """Say once, per window, that this window has already outlived the
        deadline it replaced — and only when the lag that bought the extra time
        was material enough to be worth a reader's attention.

        ``capture_lifecycle`` is THE one non-exception report and promises never
        to raise, so it is called directly: a second never-raise wrapper here
        would be a second way to do something that already has a home.
        """
        if self._reported or not is_starved(self._peak):
            return
        self._reported = True
        capture_lifecycle(
            STRETCHED_EVENT,
            patience=self._patience,
            elapsed=round(elapsed, 1),
            lag_factor=round(self._peak, 2),
        )
