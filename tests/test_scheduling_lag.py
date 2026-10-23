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

import asyncio
import math
import os
import sys
import time

import pytest

from stealth_chrome_devtools_mcp.embedded import backend_eviction, scheduling_lag

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


class TestIsStarved:
    def test_starved_means_at_or_above_the_report_factor(self):
        at = scheduling_lag.REPORT_FACTOR
        assert scheduling_lag.is_starved(at)
        assert scheduling_lag.is_starved(scheduling_lag.MAX_STRETCH)
        assert not scheduling_lag.is_starved(math.nextafter(at, 0.0))
        assert not scheduling_lag.is_starved(1.0)

    def test_no_reading_is_never_starved(self):
        assert not scheduling_lag.is_starved(None)


class FakeCanaryProcess:
    """What ``asyncio.create_subprocess_exec`` hands back, minus the process."""

    pid = 4321

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.returncode: int | None = None
        self.kills = 0
        self.announce = True  # the child's first line, sent at spawn

    def kill(self) -> None:
        self.kills += 1
        self.returncode = -9
        self.stdout.feed_eof()

    async def wait(self) -> int | None:
        return self.returncode


class TestIdleCanary:
    """F-1007: an IDLE-priority child woken every CANARY_NAP_SECONDS DURING a probe.
    The backend's own sleeps wake on time on a host where an IDLE process gets 4% of
    its rate (measured), so this is what measures a renderer starved by priority."""

    @pytest.fixture
    async def canary(self, scheduler, monkeypatch):
        sched = scheduler(1.0)
        proc = FakeCanaryProcess()  # built inside the running loop its reader uses

        async def spawn():
            if proc.announce:
                proc.stdout.feed_data(b"ready\n")
            return proc

        monkeypatch.setattr(scheduling_lag, "_spawn_canary", spawn)
        return sched, proc

    async def window(self, sched, proc, seconds, wakes):
        """Start a canary (which waits for the child to announce itself), spend
        *seconds*, deliver *wakes*."""
        c = scheduling_lag.IdleCanary()
        await c.start()
        sched.now += seconds
        proc.stdout.feed_data(b".\n" * wakes)
        for _ in range(wakes + 10):  # let the reader task drain the burst
            if c._wakes >= wakes:
                break
            await asyncio.sleep(0)
        return c

    async def test_a_canary_that_kept_up_reads_one(self, canary):
        sched, proc = canary
        window = 15.0
        c = await self.window(
            sched, proc, window, int(window / scheduling_lag.CANARY_NAP_SECONDS)
        )

        factor = await c.factor()

        assert factor == pytest.approx(1.0)
        assert not scheduling_lag.is_starved(factor)

    async def test_a_canary_that_woke_early_still_reads_one_not_less(self, canary):
        sched, proc = canary
        window = 10.0
        c = await self.window(
            sched, proc, window, int(window / scheduling_lag.CANARY_NAP_SECONDS) + 5
        )

        assert await c.factor() == 1.0  # more wakes than expected is not "fast"

    async def test_a_starved_canary_reads_clamped_and_starved(self, canary):
        sched, proc = canary
        c = await self.window(sched, proc, 15.0, 3)

        factor = await c.factor()

        assert factor == scheduling_lag.MAX_STRETCH
        assert scheduling_lag.is_starved(factor)

    async def test_a_canary_that_never_woke_is_starved_not_unknown(self, canary):
        sched, proc = canary
        c = await self.window(sched, proc, 15.0, 0)

        assert await c.factor() == scheduling_lag.MAX_STRETCH

    async def test_half_the_wakes_is_exactly_the_threshold(self, canary):
        sched, proc = canary
        window = 10.0
        kept = int(window / scheduling_lag.CANARY_NAP_SECONDS / 2)  # 50: factor 2.0
        c = await self.window(sched, proc, window, kept)
        assert scheduling_lag.is_starved(await c.factor())

    async def test_one_wake_more_than_half_is_not(self, canary):
        sched, proc = canary
        window = 10.0
        kept = int(window / scheduling_lag.CANARY_NAP_SECONDS / 2) + 1  # 51
        c = await self.window(sched, proc, window, kept)
        assert not scheduling_lag.is_starved(await c.factor())

    async def test_a_window_under_the_minimum_says_nothing(self, canary):
        sched, proc = canary
        floor = scheduling_lag.CANARY_MIN_WINDOW_SECONDS
        short = await self.window(sched, proc, floor - 0.01, 0)
        assert await short.factor() is None

    async def test_a_window_at_the_minimum_speaks(self, canary):
        sched, proc = canary
        floor = scheduling_lag.CANARY_MIN_WINDOW_SECONDS
        at = await self.window(sched, proc, floor, 0)
        assert await at.factor() == scheduling_lag.MAX_STRETCH

    async def test_start_returns_only_once_the_child_has_announced_itself(
        self, scheduler, monkeypatch
    ):
        # The window a probe is read over must cover the WHOLE probe, so the clock
        # starts when the child speaks and ``start`` does not return before that.
        sched = scheduler(1.0)
        proc = FakeCanaryProcess()

        async def spawn():
            asyncio.get_running_loop().call_later(
                0.05, proc.stdout.feed_data, b"ready\n"
            )
            return proc

        monkeypatch.setattr(scheduling_lag, "_spawn_canary", spawn)
        c = scheduling_lag.IdleCanary()

        await c.start()

        assert c._ready_at == sched.now and proc.kills == 0

    async def test_a_canary_that_never_announced_itself_is_stopped_and_says_nothing(
        self, canary, monkeypatch
    ):
        sched, proc = canary
        proc.announce = False
        monkeypatch.setattr(scheduling_lag, "CANARY_READY_SECONDS", 0.05)
        c = scheduling_lag.IdleCanary()

        await c.start()
        sched.now += 15.0

        assert proc.kills == 1
        assert await c.factor() is None  # not "starved": it may never have started

    async def test_a_pipe_that_ends_without_a_word_does_not_wait_out_the_bound(
        self, canary
    ):
        _sched, proc = canary
        proc.announce = False
        proc.stdout.feed_eof()
        c = scheduling_lag.IdleCanary()

        started = time.monotonic()
        await c.start()

        assert time.monotonic() - started < scheduling_lag.CANARY_READY_SECONDS / 2
        assert proc.kills == 1 and await c.factor() is None

    async def test_a_cancel_while_waiting_for_the_child_still_reaps_it(self, canary):
        _sched, proc = canary
        proc.announce = False  # it never speaks, so start() is parked on the wait

        async def use() -> None:
            async with scheduling_lag.idle_canary():
                raise AssertionError("never entered")

        task = asyncio.ensure_future(use())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert proc.kills == 1

    async def test_a_canary_that_exited_says_nothing(self, canary):
        sched, proc = canary
        c = await self.window(sched, proc, 15.0, 0)
        proc.returncode = 1  # e.g. it could not lower its own priority

        assert await c.factor() is None

    async def test_a_spawn_that_fails_says_nothing_and_does_not_raise(
        self, scheduler, monkeypatch
    ):
        scheduler(1.0)

        async def broken():
            raise OSError("no process")

        monkeypatch.setattr(scheduling_lag, "_spawn_canary", broken)
        c = scheduling_lag.IdleCanary()

        await c.start()

        assert await c.factor() is None
        await c.stop()

    async def test_leaving_the_context_kills_the_child_even_on_an_error(self, canary):
        _sched, proc = canary

        with pytest.raises(RuntimeError):
            async with scheduling_lag.idle_canary():
                raise RuntimeError("the probe blew up")

        assert proc.kills == 1

    async def test_stop_is_idempotent(self, canary):
        _sched, proc = canary
        c = scheduling_lag.IdleCanary()
        await c.start()

        await c.stop()
        await c.stop()

        assert proc.kills == 1

    async def test_the_real_child_wakes_and_is_gone_after_stop(self, monkeypatch):
        """No fake: a real IDLE child, on whatever machine runs this. Only that it
        starts, is counted and is reaped; its factor depends on the host."""
        monkeypatch.setattr(scheduling_lag, "CANARY_MIN_WINDOW_SECONDS", 0.5)
        async with scheduling_lag.idle_canary() as c:
            deadline = time.monotonic() + 30
            while c._ready_at is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert c._ready_at is not None
            await asyncio.sleep(0.8)
            factor = await c.factor()
            proc = c._proc
        assert factor is not None and 1.0 <= factor <= scheduling_lag.MAX_STRETCH
        assert c._wakes > 0
        assert proc is not None
        # stop() only waits CANARY_REAP_SECONDS for the reap; an IDLE child on a
        # starved host can take longer, so this pin waits for the exit itself.
        if proc.returncode is None:
            await asyncio.wait_for(proc.wait(), 30)
        assert proc.returncode is not None

    def test_the_canary_is_as_low_as_chromium_makes_a_background_renderer(self):
        # kBackgroundPriority = 5 (desktop Linux, base/process/process_linux.cc);
        # IDLE_PRIORITY_CLASS on Windows (process_win.cc), 0x40 below. An absolute
        # setpriority, never os.nice(): that adds to whatever the backend already has.
        assert scheduling_lag.CANARY_NICE == 5
        assert f"os.setpriority(os.PRIO_PROCESS, 0, {scheduling_lag.CANARY_NICE})" in (
            scheduling_lag._CANARY_SOURCE
        )
        assert "SetPriorityClass(kernel.GetCurrentProcess(), 0x40)" in (
            scheduling_lag._CANARY_SOURCE
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX nice values")
    async def test_the_real_child_runs_at_the_chromium_nice_value(self):
        async with scheduling_lag.idle_canary() as c:
            assert c._proc is not None
            deadline = time.monotonic() + 10
            nice = os.getpriority(os.PRIO_PROCESS, c._proc.pid)
            while nice != scheduling_lag.CANARY_NICE and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                nice = os.getpriority(os.PRIO_PROCESS, c._proc.pid)
        assert nice == scheduling_lag.CANARY_NICE


class TestExcuseBudget:
    """How long "starved, not dead" is believed in a row (F-1007)."""

    CEILING = backend_eviction.STARVED_CEILING_SECONDS

    @pytest.fixture
    def budget(self):
        clock = [1000.0]
        return clock, scheduling_lag.ExcuseBudget(lambda: clock[0])

    def test_the_ceiling_is_the_one_eviction_uses(self):
        assert self.CEILING == 300.0

    def test_a_round_at_the_ceiling_is_excused_one_past_it_is_not(self, budget):
        clock, b = budget
        assert b.grant("a") == 0.0
        clock[0] += self.CEILING
        assert b.grant("a") == self.CEILING
        clock[0] += 0.001
        assert b.grant("a") is None
        assert b.grant("a") is None  # and it stays refused until reset

    def test_reset_starts_a_new_run(self, budget):
        clock, b = budget
        b.grant("a")
        clock[0] += self.CEILING + 1
        assert b.grant("a") is None
        b.reset("a")
        assert b.grant("a") == 0.0

    def test_subjects_do_not_share_a_run(self, budget):
        clock, b = budget
        b.grant("a")
        clock[0] += self.CEILING + 1
        assert b.grant("a") is None and b.grant("b") == 0.0
        assert b.tracked() == {"a", "b"}
        b.reset("a")
        b.reset("never-seen")  # no error
        assert b.tracked() == {"b"}
