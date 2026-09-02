"""F-856 — the reuse gate must not condemn a backend on a prober's own lateness.

The incident (2026-09-02, 3,445 processes, CPU at 100%): F-820's confirmation
gate held six times across eleven minutes — ``backend on port 52554 was busy,
not dead`` — and then, on the seventh strike run, spent its whole
``REUSE_PATIENCE_SECONDS`` without an answer and returned ``confirmed
unusable``. Nothing about the backend had changed; what had changed is that the
prober's own 60 wall-clock seconds no longer contained 60 seconds of the
attention the window was sized for.

Everything here drives ``singleton._same_identity_backend_ready``, THE one gate
three callers share (the F-820 watchdog's confirmation, F-843's bridge verdict,
F-807's cold-start grace), through a fake scheduler. The probe never answers;
only the SCHEDULING changes between the two cases, which is the whole claim.
"""

from __future__ import annotations

import json

import pytest

from stealth_chrome_devtools_mcp.embedded import scheduling_lag, singleton

PORT = 47251
PROBE_SECONDS = 10.0  # singleton.REUSE_PROBE_TIMEOUT
# Enough missed probes that a 60s wall-clock deadline is long gone, but few
# enough that a 4x-stretched window is still open when the backend answers.
ANSWERS_ON_PROBE = 12


class Scheduler:
    """A fake clock/sleep pair whose sleeps overshoot by ``lag`` (see
    ``test_scheduling_lag`` for the same shape used against the window itself).
    """

    def __init__(self, lag: float) -> None:
        self.lag = lag
        self.now = 5000.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds * self.lag

    def work(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """A same-identity backend recorded on ``PORT``, with the socket open (busy,
    not dead) and every timing seam under the test's control."""
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(singleton, "_server_version", lambda: "9.9.9")
    monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "fp-same")
    (tmp_path / "server.json").write_text(
        json.dumps(
            {
                "port": PORT,
                "version": "9.9.9",
                "pid": 4242,
                "source_fingerprint": "fp-same",
            }
        )
    )
    monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: True)

    def _run(lag: float, answers_on: int | None) -> tuple[bool, int]:
        """Return ``(verdict, probe attempts)`` for one run of the gate."""
        sched = Scheduler(lag)
        attempts = {"n": 0}

        def probe(port, **kwargs):
            attempts["n"] += 1
            sched.work(PROBE_SECONDS)
            return answers_on is not None and attempts["n"] >= answers_on

        monkeypatch.setattr(singleton, "_backend_http_ready", probe)
        # Both worlds, one fake clock: the pre-fix gate reads ``time`` directly,
        # the post-fix one reads it through ``scheduling_lag``'s single seam.
        monkeypatch.setattr(singleton.time, "monotonic", sched.clock)
        monkeypatch.setattr(singleton.time, "sleep", sched.sleep)
        monkeypatch.setattr(scheduling_lag, "_now", sched.clock)
        monkeypatch.setattr(scheduling_lag, "_wait", sched.sleep)
        return singleton._same_identity_backend_ready(PORT), attempts["n"]

    return _run


class TestStarvationDoesNotCondemn:
    def test_a_starved_prober_waits_for_the_backend_it_cannot_hear(self, gate):
        """THE F-856 pin. Same backend, same silent probes; the only difference
        from the case below is that this process's own sleeps overshoot 4x. A
        timeout observed while we are not being scheduled is not evidence, so
        the gate must still be waiting when the backend finally answers."""
        verdict, _ = gate(lag=4.0, answers_on=ANSWERS_ON_PROBE)

        assert verdict is True

    def test_an_unstarved_prober_still_condemns_on_schedule(self, gate):
        """The guard against over-fixing: with sleeps landing on time, the same
        run of misses spends the same 60 seconds and ends in the same verdict
        F-807 and F-820 already depend on."""
        verdict, _ = gate(lag=1.0, answers_on=ANSWERS_ON_PROBE)

        assert verdict is False

    def test_a_dead_backend_still_fails_on_the_first_probe(self, gate, monkeypatch):
        """Starvation buys patience for the BUSY only. No socket and no process
        of ours is dead however late we were scheduled, and a cold start after
        a crash must never pay the stretched window for it."""
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: False)
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: False)

        verdict, attempts = gate(lag=4.0, answers_on=None)

        assert verdict is False
        assert attempts == 1
