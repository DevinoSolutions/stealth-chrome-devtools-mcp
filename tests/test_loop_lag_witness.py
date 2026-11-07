"""T-15386 — a backend's stamp says how LATE its own loop runs, and a stamp that
says "seconds late" no longer excuses failed probes or blocks an eviction.

On 2026-11-07 three shared backends hung for 1.5-48 h. Their loops crawled rather
than stopped, so they went on stamping: the proxies read each fresh stamp as
"the probe timeouts are ours" 276 times, and the eviction read it as "slow, not
wedged" and refused 325 times ("backend unhealable"). Only a hand kill ended it.

Three halves, pinned separately: the MEASUREMENT (``LoopLag`` and the real
``beat`` against a loop something blocks), the two READERS (``self_report`` for
the F-889 veto, ``unusable_for`` for F-967's spare) and the WIRING (singleton's
kill site asks ``unusable_for``). Hermetic: ``tmp_path`` records and pids that
are never real processes.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_eviction,
    backend_liveness,
    backend_registry,
    browser_pid_registry,
    singleton,
)

PORT = 51997
PID = 4343
BROWSER_PID = 9002


@pytest.fixture()
def record(tmp_path):
    path = tmp_path / "server.json"
    backend_registry.record_backend(
        path,
        port=PORT,
        version="2.1.9",
        pid=PID,
        source_fingerprint="a" * 64,
        display_context="win-session-1",
    )
    return path


def _stamp(record, *, age=1.0, lag=None, lagging_for=None):
    now = time.time()
    backend_registry.stamp_heartbeat(
        record,
        port=PORT,
        pid=PID,
        at=now - age,
        loop_lag=lag,
        lagging_since=None if lagging_for is None else now - lagging_for,
    )


def _hold_the_loop(seconds):
    """Block the calling thread, i.e. the loop, as the GIL-holding psutil poll did."""
    time.sleep(seconds)


class TestTheMeasurement:
    def test_a_median_needs_five_beats(self):
        lag = backend_liveness.LoopLag(3.0)
        for _ in range(backend_liveness.LAG_MIN_SAMPLES - 1):
            assert lag.observe(5.0, now=100.0) is None
        assert lag.observe(5.0, now=100.0) == 5.0

    def test_one_spike_is_not_a_lagging_loop(self):
        """A ``CreateProcess`` on the loop thread costs up to 2 s; it is a spike."""
        lag = backend_liveness.LoopLag(3.0)
        for sample in (0.01, 0.01, 2.0, 0.01, 0.01, 0.01):
            lag.observe(sample, now=100.0)
        assert lag.median() == pytest.approx(0.01)
        assert lag.lagging_since is None

    def test_lagging_since_is_the_first_lagging_median_and_clears_on_recovery(self):
        lag = backend_liveness.LoopLag(3.0)
        for at in range(5):
            lag.observe(2.0, now=float(at))
        assert lag.lagging_since == 4.0
        lag.observe(2.0, now=99.0)
        assert lag.lagging_since == 4.0, "the run keeps its start"
        for _ in range(20):
            lag.observe(0.0, now=200.0)
        assert lag.lagging_since is None

    def test_the_window_is_a_minute_of_beats(self):
        lag = backend_liveness.LoopLag(3.0)
        for _ in range(20):
            lag.observe(5.0, now=1.0)
        for _ in range(11):
            lag.observe(0.0, now=2.0)
        assert lag.median() == 0.0, "older beats than the window are forgotten"

    def test_a_plain_stamp_keeps_the_two_field_shape(self, record):
        backend_liveness.stamp(record, PORT, PID)
        assert set(backend_registry.read_heartbeat(record, PORT)) == {
            backend_registry.HEARTBEAT_AT,
            backend_registry.HEARTBEAT_PID,
        }

    def test_a_blocked_loop_reports_itself_lagging(self, record, monkeypatch):
        """The real ``beat`` on a real loop, with a coroutine that holds the loop
        the way a GIL-holding call does. Thresholds scaled down; the shape is
        the incident's: the stamp keeps coming and says how late it is."""
        monkeypatch.setattr(backend_liveness, "UNUSABLE_LAG_SECONDS", 0.1)

        async def hog():
            while True:
                _hold_the_loop(0.25)
                await asyncio.sleep(0)

        async def drive():
            beat = asyncio.create_task(
                backend_liveness.beat(record, PORT, PID, interval=0.01)
            )
            blocker = asyncio.create_task(hog())
            deadline = time.monotonic() + 10
            stamp = {}
            while time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                stamp = backend_registry.read_heartbeat(record, PORT)
                if stamp.get(backend_registry.HEARTBEAT_LAGGING_SINCE) is not None:
                    break
            beat.cancel()
            blocker.cancel()
            return stamp

        stamp = asyncio.run(drive())
        assert stamp[backend_registry.HEARTBEAT_LOOP_LAG] >= 0.1
        assert stamp[backend_registry.HEARTBEAT_LAGGING_SINCE] <= time.time()

    def test_a_free_loop_reports_no_lag_worth_the_name(self, record):
        async def drive():
            beat = asyncio.create_task(
                backend_liveness.beat(record, PORT, PID, interval=0.01)
            )
            deadline = time.monotonic() + 10
            stamp = {}
            while time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                stamp = backend_registry.read_heartbeat(record, PORT)
                if backend_registry.HEARTBEAT_LOOP_LAG in stamp and (
                    stamp[backend_registry.HEARTBEAT_LOOP_LAG] is not None
                ):
                    break
            beat.cancel()
            return stamp

        stamp = asyncio.run(drive())
        assert stamp[backend_registry.HEARTBEAT_LOOP_LAG] < 0.5
        assert stamp[backend_registry.HEARTBEAT_LAGGING_SINCE] is None


class TestTheVetoWitness:
    def test_a_fresh_stamp_with_a_free_loop_still_vetoes(self, record):
        _stamp(record, age=1.0, lag=0.01)
        assert backend_liveness.self_report(record, PORT) == pytest.approx(1.0, abs=0.5)

    def test_an_older_backends_stamp_still_vetoes(self, record):
        _stamp(record, age=1.0)
        assert backend_liveness.self_report(record, PORT) is not None

    def test_a_fresh_stamp_from_a_lagging_loop_is_no_evidence(self, record, caplog):
        _stamp(record, age=1.0, lag=2.5, lagging_for=40.0)
        with caplog.at_level("WARNING", logger="stealth.proxy"):
            assert backend_liveness.self_report(record, PORT) is None
        assert "reports its own loop 2.50s late" in caplog.text

    def test_the_bound_is_inclusive(self, record):
        _stamp(record, age=1.0, lag=backend_liveness.UNUSABLE_LAG_SECONDS)
        assert backend_liveness.self_report(record, PORT) is None

    def test_a_stale_stamp_is_still_no_evidence(self, record):
        _stamp(record, age=backend_liveness.HEARTBEAT_STALE_SECONDS + 5, lag=0.01)
        assert backend_liveness.self_report(record, PORT) is None


class TestTheEvictionWitness:
    def test_a_free_loop_is_as_old_as_its_silence(self, record):
        _stamp(record, age=12.0, lag=0.01)
        assert backend_liveness.unusable_for(record, PORT) == pytest.approx(
            12.0, abs=0.5
        )

    def test_a_lagging_loop_is_as_old_as_its_lag(self, record):
        _stamp(record, age=1.0, lag=3.0, lagging_for=400.0)
        assert backend_liveness.unusable_for(record, PORT) == pytest.approx(
            400.0, abs=0.5
        )

    def test_a_long_silence_outweighs_a_short_lag(self, record):
        _stamp(record, age=500.0, lag=3.0, lagging_for=20.0)
        assert backend_liveness.unusable_for(record, PORT) == pytest.approx(
            500.0, abs=0.5
        )

    def test_a_predecessors_stamp_is_no_evidence(self, record):
        backend_registry.stamp_heartbeat(
            record,
            port=PORT,
            pid=PID + 1,
            at=time.time(),
            loop_lag=3.0,
            lagging_since=time.time() - 400,
        )
        assert backend_liveness.unusable_for(record, PORT) is None
        assert backend_liveness.reported_lag(record, PORT) is None


def _write_browsers(state_dir):
    entries = {
        "inst-1": browser_pid_registry.with_owner(
            browser_pid_registry.new_entry(
                BROWSER_PID,
                create_time=1.0,
                user_data_dir=None,
                uses_custom_data_dir=None,
                auto_clone=False,
            ),
            PID,
            1.0,
        )
    }
    path = state_dir / browser_pid_registry.RECORD_NAME
    path.write_text(json.dumps({"browser_processes": entries}), encoding="utf-8")


class TestTheKillSiteAsksIt:
    """``singleton._protecting_browsers(starved=True)`` — the KILL site — reads
    ``unusable_for``: the incident's backend is no longer spared forever."""

    @pytest.fixture()
    def wired(self, record, monkeypatch):
        monkeypatch.setattr(singleton, "SERVER_STATE_FILE", record)
        monkeypatch.setattr(singleton, "STATE_DIR", record.parent)
        monkeypatch.setattr(singleton, "_identity_matches", lambda entry: True)
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: pid == PID)
        monkeypatch.setattr(
            singleton.psutil, "pid_exists", lambda pid: pid == BROWSER_PID
        )
        _write_browsers(record.parent)
        return record

    def test_a_briefly_lagging_backend_is_still_spared(self, wired):
        _stamp(wired, age=1.0, lag=3.0, lagging_for=60.0)
        assert singleton._protecting_browsers(PORT, starved=True) == [BROWSER_PID]

    def test_a_backend_lagging_past_the_ceiling_is_evictable(self, wired):
        """The 2026-11-07 shape: stamping every few seconds, lagging for long."""
        lagging = backend_eviction.STARVED_CEILING_SECONDS + 1
        _stamp(wired, age=1.0, lag=3.0, lagging_for=lagging)
        assert singleton._protecting_browsers(PORT, starved=True) == []

    def test_a_free_loop_keeps_f967s_spare(self, wired):
        _stamp(wired, age=1.0, lag=0.01)
        assert singleton._protecting_browsers(PORT, starved=True) == [BROWSER_PID]
