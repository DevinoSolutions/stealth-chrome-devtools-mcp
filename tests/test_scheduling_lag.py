"""F-856 — a patience window is spent in FAIRLY SCHEDULED seconds.

The mechanism, in isolation. Starvation itself is not reproducible honestly in
a unit test (synthetic load proves nothing about a scheduler), so what is
tested is the thing that READS starvation: hand ``FairWindow`` a clock and a
sleep whose relationship is under the test's control, and pin what the window
does with each answer.

``Scheduler`` is that pair. A sleep of ``d`` advances its clock by ``d * lag``,
which is exactly the observation a starved process makes — it asked for ``d``
and woke up later. ``work(d)`` advances the clock without sleeping: the
blocking probe the real loop spends most of its time inside.
"""

from __future__ import annotations

import pytest

from stealth_chrome_devtools_mcp.embedded import scheduling_lag

PATIENCE = 60.0
PROBE_SECONDS = 10.0  # REUSE_PROBE_TIMEOUT: what one missed probe costs
NAP_SECONDS = 0.25  # what the reuse gate asks for between probes


class Scheduler:
    """A fake clock/sleep pair whose sleeps overshoot by ``lag``."""

    def __init__(self, lag: float = 1.0) -> None:
        self.lag = lag
        self.now = 1000.0
        self.naps = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.naps += 1
        self.now += seconds * self.lag

    def work(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def scheduler(monkeypatch):
    """Install a ``Scheduler`` as the module's one timing seam."""

    def _install(lag: float = 1.0) -> Scheduler:
        sched = Scheduler(lag)
        monkeypatch.setattr(scheduling_lag, "_now", sched.clock)
        monkeypatch.setattr(scheduling_lag, "_wait", sched.sleep)
        return sched

    return _install


def _spend(sched: Scheduler, patience: float = PATIENCE) -> float:
    """Run the reuse gate's loop shape until the window expires; return the
    WALL seconds it lasted. Probe, ask, nap — the order in
    ``singleton._same_identity_backend_ready``."""
    window = scheduling_lag.FairWindow(patience)
    started = sched.now
    while True:
        sched.work(PROBE_SECONDS)
        if window.expired():
            return sched.now - started
        window.nap(NAP_SECONDS)


class TestAHealthyMachineIsUnchanged:
    def test_no_lag_spends_the_window_in_wall_seconds(self, scheduler):
        """The whole claim that this cannot misfire on an idle machine: with
        sleeps that land on time, the window is the wall-clock deadline it
        replaced, to within the granularity of one probe."""
        sched = scheduler(lag=1.0)

        elapsed = _spend(sched)

        assert PATIENCE <= elapsed <= PATIENCE + PROBE_SECONDS + NAP_SECONDS

    def test_zero_patience_never_sleeps(self, scheduler):
        """The discovery path (``patience=0.0``) probes once and gives up — no
        nap, so no lag sample, so nothing to stretch."""
        sched = scheduler(lag=100.0)

        window = scheduling_lag.FairWindow(0.0)

        assert window.expired() is True
        assert sched.naps == 0


class TestAStarvedMachineBuysTime:
    def test_a_lagging_scheduler_outlives_the_wall_patience(self, scheduler):
        """THE F-856 pin: when this process's own sleeps overshoot 4x, its
        timeouts stop being evidence about the backend, so the window must
        outlast the wall-clock deadline instead of condemning on it."""
        sched = scheduler(lag=4.0)

        elapsed = _spend(sched)

        assert elapsed > PATIENCE * 2

    def test_the_stretch_is_bounded(self, scheduler):
        """Patience is elastic, not infinite: an absurdly starved machine still
        reaches a verdict, inside ``patience * MAX_STRETCH`` plus the
        granularity of the iteration that crosses it."""
        sched = scheduler(lag=100.0)

        elapsed = _spend(sched)

        ceiling = PATIENCE * scheduling_lag.MAX_STRETCH
        assert elapsed <= ceiling + PROBE_SECONDS + NAP_SECONDS * 100.0


class TestTheStretchIsReported:
    def test_a_material_stretch_ships_one_lifecycle_report(
        self, scheduler, monkeypatch
    ):
        """F-827: declining to believe a timeout is a DECISION, not a crash, and
        without it the CONDEMNED/TEARDOWN series has no denominator. Once per
        window, never per iteration."""
        reports: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            scheduling_lag,
            "capture_lifecycle",
            lambda message, **fields: reports.append((message, fields)),
        )
        sched = scheduler(lag=4.0)

        _spend(sched)

        assert len(reports) == 1
        message, fields = reports[0]
        assert "patience" in message and "starv" in message
        assert fields["lag_factor"] >= scheduling_lag.REPORT_FACTOR
        assert fields["patience"] == PATIENCE

    def test_a_healthy_window_ships_nothing(self, scheduler, monkeypatch):
        """No report on an idle machine — the signal is zero there by
        construction, and a warning that fires normally is not a warning."""
        reports: list[str] = []
        monkeypatch.setattr(
            scheduling_lag,
            "capture_lifecycle",
            lambda message, **fields: reports.append(message),
        )
        sched = scheduler(lag=1.0)

        _spend(sched)

        assert reports == []
