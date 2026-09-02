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

**What it is not.** It never decides that anything is alive or dead. It only
answers "has this window been spent", so the ONE gate that owns the dead-vs-busy
policy keeps owning it, and ``proxy_selfheal``'s single heal path is untouched:
starvation moves when a verdict is reached, never what happens after one.
"""

from __future__ import annotations

import time

from stealth_chrome_devtools_mcp.observability import capture_lifecycle


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
# F-827 vocabulary. A stretched window is a DECISION — "I declined to believe
# this timeout" — and without it the CONDEMNED/HEALED/TEARDOWN series has no
# denominator for how often starvation nearly caused one.
STRETCHED_EVENT = "proxy: patience extended under starvation"


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
        if self._reported or self._peak < REPORT_FACTOR:
            return
        self._reported = True
        capture_lifecycle(
            STRETCHED_EVENT,
            patience=self._patience,
            elapsed=round(elapsed, 1),
            lag_factor=round(self._peak, 2),
        )
