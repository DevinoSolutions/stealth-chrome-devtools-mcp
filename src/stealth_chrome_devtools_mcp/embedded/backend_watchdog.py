"""THE one home for the stdio proxy's mid-session liveness watchdog — the SLOW
witness that a backend has stopped being usable.

Extracted from ``singleton`` by F-856, which needed room in a file already at
its LOC budget. The gate forced the question; the answer was already right.
This is a self-contained algorithm (strikes, then a confirmation phase) that
takes every collaborator through an argument, so it belongs beside the module
that wires it, not inside it. It is a leaf: the two probes arrive as
parameters, so nothing here imports ``singleton``, and the dead-vs-busy policy
stays single-homed in ``singleton._same_identity_backend_ready``.

Its history, unchanged by the move:

* **F-501** — the fast check used to be a bare socket connect, which a wedged
  backend (dispatch loop dead, socket still open) always passes, so the sole
  auto-recovery watchdog never armed against the exact failure it exists for.
  It is now an app-level ``initialize`` probe, driven off-thread by the caller
  (a blocking httpx call run inline would freeze the stdio pump for up to
  ``LIVENESS_PROBE_TIMEOUT`` every ``interval``).
* **F-820** — those strikes no longer condemn on their own. Under fleet load a
  healthy shared backend answers slower than 2s for 20-40s at a time, and whole
  waves of proxies tore down in the same second while it went on serving. They
  now only open a CONFIRMATION phase whose verdict comes from the SAME gate the
  cold-start lock trusts, so there is no second busy-vs-dead policy here.
* **F-856** — that gate now spends its patience in fairly scheduled seconds
  (``scheduling_lag``), so a confirmation reached while this process itself was
  not being scheduled is no longer mistaken for evidence about the backend. The
  strikes were left untouched: they never condemned on their own, so their
  timing did not look like the thing to make elastic.
* **F-889** — it was. Measured 2026-09-18: the machine had 2.4 GB free of 125.7
  and 114 stdio proxies paged out to ~0 MB, so the STRIKE phase's ``sleep(2.0)``
  and each 2 s probe were spent on a process that was awake for almost none of
  them, while the backend they condemned answered ``initialize`` in 227 ms with
  no error in its own log. 829 condemnations in seven days, and every Claude
  Code session on the machine saw "Connection closed" at once. Two things
  changed here, and the second is why the first is not merely a longer wait:

  (a) a strike run may not conclude until a :class:`~scheduling_lag.FairWindow`
  of ``interval * (failures_before_teardown - 1)`` has been SPENT — charged in
  fair seconds, so a run that reached three strikes without this process being
  scheduled defers instead of condemning. On an idle machine nothing changes:
  the per-tick charge is ``interval * (1 + probe / nap_actual)``, which is
  ``>= interval`` always, so the remaining ticks always spend the window and the
  human-pinned ~12 s hard-down detection stands. Under starvation it stretches
  and still TERMINATES, because ``MAX_STRETCH`` bounds the window at 4x its
  patience in wall seconds.

  (b) a condemnation now needs TWO witnesses, and the second one is the backend.
  ``heartbeat`` reports how long ago the backend said its own event loop was
  turning (``backend_liveness.self_report``: one small local JSON read, no HTTP,
  no socket, no thread). A fresh self-report against a failed client probe means
  "I am starved", never "it is dead", so the run RESETS. A stale or absent one
  falls through to the confirmation phase exactly as before — which is what
  keeps a mixed fleet, where the backend is 2.1.9 and stamps nothing, behaving
  precisely as it does today.

Both collaborators are OPTIONAL and both defaults are inert where the existing
suite drives this: with ``interval=0.0`` the window has patience ``0.0`` and is
expired on its first ask, and with no ``heartbeat`` supplied there is no second
witness to consult.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import scheduling_lag

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# One stream: the watchdog is part of the proxy's story, so it writes to the
# proxy log ``configure_logging("proxy")`` already owns (same logger name as
# ``singleton``, from which this moved — a log line that changed streams is a
# log line nobody finds).
_logger = logging.getLogger("stealth.proxy")

#: How many completed strike runs a FRESH backend self-report may defer before
#: the client probes win anyway (F-889 review M1).
#:
#: The heartbeat proves the backend's event LOOP is turning. It does not prove
#: its HTTP LISTENER is reachable, and those can come apart — a closed socket, a
#: broken session manager — so an unconditional veto made a backend that stamps
#: and never answers undetectable, i.e. it traded the outage for a hang.
#:
#: A veto costs a whole strike run, and a strike run may not conclude until its
#: :class:`~scheduling_lag.FairWindow` has been SPENT (rule (a) above), so this
#: is a budget of FAIR-TIME rounds rather than of wall seconds: a genuinely
#: starved proxy spends it slowly, which is the behaviour the incident asks for,
#: and a fairly scheduled one spends it at the nominal rate. TEN, on
#: ``backend_liveness``'s "ten missed units" shape (``HEARTBEAT_STALE_SECONDS``
#: is ten missed intervals), so the two bounds in this subsystem read alike.
#:
#: What that costs, stated rather than implied. At the pinned defaults
#: (``interval`` 2.0, ``failures_before_teardown`` 3) one run is 3 ticks plus a
#: ``FairWindow(4.0)``: ~12 s on a fairly scheduled machine, and at most
#: ``4.0 * MAX_STRETCH`` = 16 s of window plus its probes when starved. So a
#: backend whose loop turns while its listener never answers reaches the
#: confirmation gate after 11 runs — **about 2 minutes idle, under ~4 under
#: maximal starvation** — and is then condemned by that gate on its own terms.
#: The budget REFILLS on any healthy tick or successful confirmation: it bounds
#: one continuous failure episode, not the session.
HEARTBEAT_VETOES = 10


def _defer(port: int, consecutive: int, already_said: bool) -> bool:
    """Say ONCE per strike run that this process is not being scheduled, so the
    verdict is being held. Always returns True — the run is now deferred.

    Once per RUN and not per tick (F-889 review L3): a starved proxy stays in
    this branch for as long as the starvation lasts, and a line per tick buries
    the strikes around it under its own repetition — on a machine that is
    already short of everything, including disk.
    """
    if not already_said:
        _logger.warning(
            "port %d: %d strikes, but this process is not being scheduled; "
            "deferring the verdict (F-889)",
            port,
            consecutive,
        )
    return True


def _vetoed(
    port: int,
    heartbeat: Callable[[], float | None] | None,
    vetoes: int,
    budget: int,
) -> bool:
    """Ask the SECOND WITNESS, and say whether it defers this verdict.

    ``heartbeat`` reports how long ago the backend said its own event loop
    turned, or ``None`` for "no evidence" — a stale stamp, a 2.1.9 backend that
    writes none, an entry that is gone. Tested ``is not None`` and never for
    truthiness, because a stamp written this instant is ``0.0``.

    Fresh AND within budget is "I am starved", never "it is dead": the one thing
    a starved prober cannot establish about itself. Fresh and OUT of budget is a
    loop that turns while the listener never answers — the heartbeat has bought
    this backend its whole allowance of fair-time runs and is now out of
    standing, so the patient gate decides on its own terms. Both are logged,
    because a verdict that was deferred and a verdict that stopped being
    deferred are the two things a post-mortem needs to tell apart.
    """
    age = heartbeat() if heartbeat is not None else None
    if age is None:
        return False
    if vetoes < budget:
        _logger.info(
            "backend on port %d reported its own loop turning %.1fs ago; "
            "the probe timeouts are ours, not its death (F-889) [%d/%d]",
            port,
            age,
            vetoes + 1,
            budget,
        )
        return True
    _logger.warning(
        "backend on port %d is still stamping (%.1fs ago) but has failed %d "
        "fair-time strike runs; asking the confirmation gate anyway "
        "(F-889 review M1)",
        port,
        age,
        vetoes + 1,
    )
    return False


async def watch_liveness(  # noqa: PLR0913  PERMANENT(function interface)
    port: int,
    *,
    is_healthy: Callable[[], object],
    confirm_probe: Callable[[], object],
    interval: float = 2.0,
    failures_before_teardown: int = 3,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    heartbeat: Callable[[], float | None] | None = None,
    fair_window: Callable[[float], object] | None = None,
    heartbeat_vetoes: int = HEARTBEAT_VETOES,
) -> None:
    """Return once the backend on ``port`` is CONFIRMED unusable.

    The caller tears the proxy's backend leg down then, converting a backend
    death mid-session into a heal (``proxy_selfheal``) instead of an unbounded
    hang on requests a dead backend can never answer. Armed only after the
    backend was confirmed up; one healthy check resets the failure run, so a
    transient blip never condemns a live backend.

    ``is_healthy`` is the fast per-tick check and ``confirm_probe`` the patient
    verdict; both may be sync or awaitable, and both are supplied by the caller
    so this module never has to know which probes are ours. ``sleep`` is the
    tick, injectable for tests.

    ``heartbeat`` is F-889's SECOND WITNESS — the backend's own report of how
    long ago its event loop last turned, or ``None`` for "no evidence". It is
    called INLINE and never through ``_ask``/a worker thread: it is a small
    local JSON read, and the exact-call-list pin in
    ``tests/test_watchdog_busy_vs_dead.py`` is what states that. Tested with
    ``is not None``, never for truthiness, because a stamp written this instant
    is ``0.0``.

    ``fair_window`` builds the patience window a strike run must spend before it
    may conclude; it defaults to ``scheduling_lag.FairWindow``, THE one home for
    "was this process scheduled fairly", which is consumed here and never
    modified.
    """
    import anyio

    async def _ask(probe: Callable[[], object]) -> object:
        res = probe()
        return await res if inspect.isawaitable(res) else res

    build_window = scheduling_lag.FairWindow if fair_window is None else fair_window
    # The window's patience is the nominal time the REMAINING ticks should take,
    # so a run that has genuinely had its full detection window still concludes
    # on the same strike it always did.
    patience = interval * (failures_before_teardown - 1)

    async def tick(window: object) -> None:
        """Pause between probes. Once a run is open the pause IS the
        measurement — ``FairWindow.nap`` waits and observes its own lag in one
        call, which is what keeps the two from drifting apart — so it runs on a
        worker thread: it sleeps, and run inline it would freeze the stdio pump
        (plan_M1 §2.2 rejected alternative #3). A caller that injects its own
        ``sleep`` OWNS the pause and therefore its measurement, and the window
        then charges wall seconds — which is what the pre-F-889 watchdog did and
        what the ``interval=0`` unit tests require.
        """
        if sleep is not None:
            await sleep(interval)
        elif window is None:
            await anyio.sleep(interval)
        else:
            await anyio.to_thread.run_sync(window.nap, interval)

    consecutive = 0
    window = None
    vetoes = 0
    deferred = False
    while True:
        await tick(window)
        if await _ask(is_healthy):
            consecutive, window, vetoes, deferred = 0, None, 0, False
            continue
        consecutive += 1
        _logger.warning(
            "probe failed %d/%d on port %d", consecutive, failures_before_teardown, port
        )
        if window is None:
            window = build_window(patience)
        # Asked EVERY tick once a run is open, so each tick is charged at the
        # lag that tick's own nap measured rather than at whatever the last one
        # happened to be.
        spent = window.expired()
        if consecutive < failures_before_teardown:
            continue
        if not spent:
            deferred = _defer(port, consecutive, deferred)
            continue
        if _vetoed(port, heartbeat, vetoes, heartbeat_vetoes):
            vetoes += 1
            consecutive, window, deferred = 0, None, False
            continue
        if not await _ask(confirm_probe):
            _logger.warning("backend on port %d confirmed unusable", port)
            return
        _logger.info("backend on port %d was busy, not dead", port)
        consecutive, window, vetoes, deferred = 0, None, 0, False
