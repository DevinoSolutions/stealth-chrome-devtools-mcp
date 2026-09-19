"""F-889 (a) + (b) — a starved proxy must not condemn a healthy backend.

Measured 2026-09-18, 13:30-14:00 UTC on 2.1.8. The machine had **2.4 GB free of
125.7** and 114 stdio proxies whose working sets had been paged out to ~0 MB. The
backend — pid 173824 on port 52554 — was healthy throughout: an MCP
``initialize`` against it answered in **227 ms** and its log carries no error for
the whole window. The proxies' logs carry the mechanism verbatim::

    13:32  WARNING stealth.proxy: probe failed 1/3 on port 52554
    13:32  WARNING stealth.proxy: probe failed 2/3 on port 52554
           ... condemnation, heals that could not complete, and the proxy EXITING

The asymmetry IS the finding: the only process that reported a problem was the
one that had no CPU, and the one it reported the problem about was answering in a
fifth of a second. 829 condemnations in seven days (Sentry).

Two rules are pinned here, and they are independent on purpose — either one alone
would have prevented the outage, and a mixed fleet gets (a) without (b):

* **(a)** a strike run may not conclude until its ``FairWindow`` has been SPENT,
  so a run that reached three strikes while unscheduled DEFERS. The window
  delays a verdict; it must never suppress one.
* **(b)** a condemnation needs a second witness. A FRESH backend self-report
  against a failed client probe means "I am starved" and resets the run; a stale
  or absent one falls through to the confirmation phase exactly as today.

Everything is injected — no sockets, no HTTP, no state file. ``sleep`` doubles as
the loop bound (``test_watchdog_busy_vs_dead``'s device): a watchdog that
correctly declines to condemn never returns, so the injected nap raises once the
loop has run long enough for the assertion.
"""

from __future__ import annotations

import logging

import anyio
import anyio.lowlevel
import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_watchdog,
    scheduling_lag,
    singleton,
)

PORT = 47889


class _StopWatchingError(Exception):
    """Raised by the injected nap to end an endless (== not condemned) watch."""


def _bounded_nap(limit: int):
    async def nap(_seconds):
        nap.calls += 1
        if nap.calls > limit:
            raise _StopWatchingError
        await anyio.lowlevel.checkpoint()

    nap.calls = 0
    return nap


class _Window:
    """A ``FairWindow`` double whose verdict the test dictates."""

    def __init__(self, spent: bool) -> None:
        self.spent = spent
        self.asks = 0
        self.naps: list[float] = []

    def expired(self) -> bool:
        self.asks += 1
        return self.spent

    def nap(self, seconds: float) -> None:
        self.naps.append(seconds)


@pytest.fixture()
def proxy_records():
    """Direct handler attachment — configure_logging sets propagate=False."""
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("stealth.proxy")
    handler = _ListHandler()
    logger.addHandler(handler)
    prior = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior)


def _strikes(records) -> list[int]:
    return [
        r.args[0]
        for r in records
        if r.levelno == logging.WARNING and "probe failed" in r.msg
    ]


class TestAStrikeMustBeEarnedInFairlyScheduledSeconds:
    async def test_an_unspent_window_defers_the_verdict(self, proxy_records):
        """THE (a) pin. Three strikes, a window that is NOT spent because this
        process is not being scheduled: no condemnation, and the patient
        confirmation gate is not even consulted — its probe would be spent the
        same starved way."""
        window = _Window(spent=False)
        confirm_calls = []

        with anyio.fail_after(5), pytest.raises(_StopWatchingError):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=2.0,
                failures_before_teardown=3,
                is_healthy=lambda: False,
                confirm_probe=lambda: confirm_calls.append(1) or False,
                sleep=_bounded_nap(6),
                fair_window=lambda _patience: window,
            )

        assert confirm_calls == [], "a starved prober must not reach the verdict"
        assert any(
            r.levelno == logging.WARNING and "not being scheduled" in r.msg
            for r in proxy_records
        ), "the deferral must be visible, not silent"

    async def test_a_spent_window_still_condemns(self):
        """The window DELAYS a verdict and must never suppress one — otherwise
        (a) would be a way to make a genuinely dead backend undetectable."""
        window = _Window(spent=True)

        with anyio.fail_after(5):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=2.0,
                failures_before_teardown=3,
                is_healthy=lambda: False,
                confirm_probe=lambda: False,
                sleep=_bounded_nap(50),
                fair_window=lambda _patience: window,
            )

    async def test_the_window_is_built_with_the_remaining_ticks_patience(self):
        """One policy, stated once: the patience is the nominal time the
        REMAINING ticks should take, so a run that has genuinely had its full
        detection window concludes on the strike it always did."""
        built = []

        with anyio.fail_after(5):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=2.0,
                failures_before_teardown=3,
                is_healthy=lambda: False,
                confirm_probe=lambda: False,
                sleep=_bounded_nap(50),
                fair_window=lambda patience: built.append(patience) or _Window(True),
            )

        assert built == [2.0 * (3 - 1)]

    async def test_a_healthy_tick_discards_the_window(self):
        """A new strike run gets a NEW window. Reusing a spent one would let a
        single earlier starved run condemn on the first strike of the next."""
        built = []
        # Two misses (a run opens, a window is built), then a healthy tick, then
        # a full run that condemns. Anything that reached three misses first
        # would condemn and return before the reset could be observed.
        healthy = iter([False, False, True, False, False, False])

        with anyio.fail_after(5):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=2.0,
                failures_before_teardown=3,
                is_healthy=lambda: next(healthy, False),
                confirm_probe=lambda: False,
                sleep=_bounded_nap(50),
                fair_window=lambda patience: built.append(patience) or _Window(True),
            )

        assert len(built) == 2, "the second strike run must open its own window"

    async def test_the_default_window_is_the_one_home_for_the_question(self):
        """``scheduling_lag.FairWindow`` is consumed, never re-spelled and never
        modified (``MAX_STRETCH`` included)."""
        assert backend_watchdog.scheduling_lag is scheduling_lag

        built = []
        real = scheduling_lag.FairWindow

        with anyio.fail_after(5):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=0.0,
                failures_before_teardown=1,
                is_healthy=lambda: False,
                confirm_probe=lambda: False,
                sleep=_bounded_nap(50),
                fair_window=lambda p: built.append(real(p)) or built[-1],
            )

        assert len(built) == 1
        assert isinstance(built[0], scheduling_lag.FairWindow)

    async def test_three_real_misses_on_an_idle_machine_still_condemn(self):
        """The idle-machine arithmetic, against the REAL FairWindow: the per-tick
        charge is ``interval * (1 + probe / nap_actual)``, which is ``>=
        interval`` always, so ``failures_before_teardown - 1`` ticks always spend
        the window and the human-pinned ~12 s detection is preserved in shape."""
        naps = []

        async def nap(seconds):
            # The caller owns the pause, so the window charges wall seconds --
            # exactly what the pre-F-889 watchdog did.
            naps.append(seconds)
            await anyio.sleep(seconds)

        with anyio.fail_after(10):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=0.05,
                failures_before_teardown=3,
                is_healthy=lambda: False,
                confirm_probe=lambda: False,
                sleep=nap,
            )

        assert len(naps) == 3, "teardown must still land on the third strike"


class TestTheBackendIsTheSecondWitness:
    async def test_a_fresh_self_report_resets_the_run_and_never_condemns(
        self, proxy_records
    ):
        """THE (b) pin, and the exact incident: our probe times out while the
        backend reports its own loop turning a fraction of a second ago. That is
        'I am starved', not 'it is dead'."""
        confirm_calls = []

        with anyio.fail_after(5), pytest.raises(_StopWatchingError):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=lambda: False,
                confirm_probe=lambda: confirm_calls.append(1) or False,
                sleep=_bounded_nap(6),
                heartbeat=lambda: 0.4,
            )

        assert confirm_calls == [], "a fresh self-report must not pay for a probe"
        assert _strikes(proxy_records) == [1, 2, 3, 1, 2, 3], (
            "a live self-report must RESET the strike run, not merely pause it"
        )
        assert any(
            r.levelno == logging.INFO and "its death" in r.msg for r in proxy_records
        ), "the veto must be reported at INFO, not silently"

    async def test_a_zero_age_report_is_evidence(self):
        """``0.0`` is falsy: a stamp written this instant is the FRESHEST
        possible evidence and must not read as 'no evidence'."""
        confirm_calls = []

        with anyio.fail_after(5), pytest.raises(_StopWatchingError):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=0.0,
                failures_before_teardown=1,
                is_healthy=lambda: False,
                confirm_probe=lambda: confirm_calls.append(1) or False,
                sleep=_bounded_nap(3),
                heartbeat=lambda: 0.0,
            )

        assert confirm_calls == []

    @pytest.mark.parametrize("report", [None])
    async def test_a_stale_or_absent_report_falls_through_and_condemns(self, report):
        """A stale stamp, and a 2.1.9 backend that writes none at all, both read
        as NO EVIDENCE — so the confirmation phase runs exactly as it does
        today. That is what makes a mixed fleet safe."""
        confirm_calls = []

        with anyio.fail_after(5):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=lambda: False,
                confirm_probe=lambda: confirm_calls.append(1) or False,
                sleep=_bounded_nap(50),
                heartbeat=lambda: report,
            )

        assert confirm_calls == [1], "no evidence must still reach the one gate"

    async def test_no_heartbeat_supplied_behaves_exactly_as_before(self):
        """The collaborator is OPTIONAL, so every existing caller and every
        existing pin keeps its behaviour."""
        confirm_calls = []

        with anyio.fail_after(5):
            await backend_watchdog.watch_liveness(
                PORT,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=lambda: False,
                confirm_probe=lambda: confirm_calls.append(1) or False,
                sleep=_bounded_nap(50),
            )

        assert confirm_calls == [1]


class TestTheProductionWiring:
    async def test_the_default_heartbeat_reads_our_record_for_this_port(
        self, monkeypatch, tmp_path
    ):
        """``singleton`` binds the witness the way it binds every other one: a
        lambda resolving this module's globals at CALL time, so a
        ``monkeypatch.setattr(singleton, "SERVER_STATE_FILE", ...)`` still
        redirects it."""
        asked = []
        monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
        monkeypatch.setattr(
            singleton.backend_liveness,
            "self_report",
            lambda path, port: asked.append((path, port)) or None,
        )

        with anyio.fail_after(5):
            await singleton._watch_backend_liveness(
                port=PORT,
                interval=0.0,
                failures_before_teardown=1,
                is_healthy=lambda: False,
                confirm_probe=lambda: False,
                sleep=_bounded_nap(50),
            )

        assert asked == [(tmp_path / "server.json", PORT)]

    async def test_the_heartbeat_is_read_inline_and_never_off_thread(
        self, monkeypatch, tmp_path
    ):
        """It is a small local JSON read. Paying a worker-thread hand-off for it
        on every strike run would be the cost the finding is about, backwards —
        and ``test_watchdog_busy_vs_dead``'s exact-call-list assertion is the
        other half of this pin."""
        run_sync_calls = []

        async def fake_run_sync(fn, *args, **kwargs):
            run_sync_calls.append(fn)
            return False

        monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
        monkeypatch.setattr(anyio.to_thread, "run_sync", fake_run_sync)

        with anyio.fail_after(5):
            await singleton._watch_backend_liveness(
                port=PORT,
                interval=0.0,
                failures_before_teardown=1,
                sleep=_bounded_nap(50),
            )

        assert singleton.backend_liveness.self_report not in run_sync_calls
        assert run_sync_calls == [
            singleton._backend_http_ready,
            singleton._same_identity_backend_ready,
        ]
