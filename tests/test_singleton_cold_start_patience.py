"""F-807 — the cold-start lock must never kill a healthy backend it just missed.

Unit tier for the race the startup herd (``test_startup_herd.py``) surfaced by
construction. The race had two halves, and each gets its own pin here:

* the winner used to release the lock at *socket-bind* while the reuse gate
  demands *MCP-ready* — so a thread acquiring the lock inside that gap saw
  "not reusable", killed the newborn backend, and double-spawned;
* under a startup herd, a busy-but-healthy backend can miss a single
  ``LIVENESS_PROBE_TIMEOUT`` (2s) probe — same wrong verdict, same fratricide.

The fix is one identity-gated patience window
(:func:`singleton._same_identity_backend_ready`): a backend whose recorded
version AND source fingerprint match ours gets up to
``REUSE_PATIENCE_SECONDS`` to answer ``initialize`` before the lock-holder may
evict it; a version- or source-stale record gets NO patience (upgrades must
still take effect immediately, issue #14). Everything runs against fakes —
no processes, no sockets; the probe and spawn seams are recorded functions.

issue #56 is the other side of the same coin and lives here too: patience must
be spent on a backend that MIGHT still come up, never on one that is provably
gone. ``TestSpawnedBackendDeathEndsTheWait`` pins that a child which exited
nonzero ends ``_wait_for_server`` on its first pass carrying the boot log's
tail as the cause — while a clean exit (the uv trampoline shim) and a
slow-but-healthy boot both keep the full wait, which is the anti-F-807 half.
"""

from __future__ import annotations

import json
import time

import pytest

from stealth_chrome_devtools_mcp.embedded import singleton

PORT = 47123


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point every singleton state path into tmp_path (same shape as the other
    singleton unit modules) and neutralize real sleeping."""
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "LOCK_FILE", tmp_path / "singleton.lock")
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(singleton.time, "sleep", lambda seconds: None)
    return tmp_path


def _write_state(tmp_path, *, port=PORT, version="9.9.9", fingerprint="fp-same"):
    (tmp_path / "server.json").write_text(
        json.dumps(
            {
                "port": port,
                "version": version,
                "pid": 4242,
                "source_fingerprint": fingerprint,
            }
        )
    )


@pytest.fixture
def our_identity(monkeypatch):
    monkeypatch.setattr(singleton, "_server_version", lambda: "9.9.9")
    monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "fp-same")


class Recorder:
    """A stub that records calls and pops scripted results (last one sticks)."""

    def __init__(self, *results):
        self.calls = 0
        self._results = list(results)

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0] if self._results else None


class TestBusyBackendSurvivesTheLockHolder:
    def test_probe_miss_then_recovery_is_reuse_not_eviction(
        self, isolated_state, our_identity, monkeypatch
    ):
        """THE F-807 pin: one missed probe on a same-identity backend must not
        terminate it. `_find_running_server`'s single shot fails, the patience
        window retries, the backend answers — nobody dies, nobody spawns."""
        _write_state(isolated_state)
        probe = Recorder(False, False, True)
        terminate = Recorder(True)
        spawn = Recorder(None)
        # Busy, not dead: the socket stays open while probes miss.
        monkeypatch.setattr(singleton, "_server_is_healthy", Recorder(True))
        monkeypatch.setattr(singleton, "_backend_http_ready", probe)
        monkeypatch.setattr(singleton, "_terminate_backend", terminate)
        monkeypatch.setattr(singleton, "_start_server_process", spawn)

        singleton._start_backend_holding_lock(PORT)

        assert probe.calls == 3  # single-shot miss, then patient retries
        assert terminate.calls == 0
        assert spawn.calls == 0

    def test_never_ready_backend_is_still_evicted_after_patience(
        self, isolated_state, our_identity, monkeypatch
    ):
        """Patience is a grace, not a pardon: a same-identity backend that
        stays unready for the whole window is genuinely dead/wedged, and the
        existing eviction+respawn machine must still run."""
        _write_state(isolated_state)
        monkeypatch.setattr(singleton, "REUSE_PATIENCE_SECONDS", 0.0)
        monkeypatch.setattr(singleton, "_server_is_healthy", Recorder(True))
        probe = Recorder(False)
        terminate = Recorder(True)
        spawn = Recorder(None)
        monkeypatch.setattr(singleton, "_backend_http_ready", probe)
        monkeypatch.setattr(singleton, "_terminate_backend", terminate)
        monkeypatch.setattr(singleton, "_start_server_process", spawn)
        monkeypatch.setattr(singleton, "_wait_for_server", Recorder(True))

        singleton._start_backend_holding_lock(PORT)

        assert terminate.calls == 1
        assert spawn.calls == 1


class TestDeadBackendSkipsTheWait:
    def test_no_socket_and_no_live_process_fails_on_the_first_probe(
        self, isolated_state, our_identity, monkeypatch
    ):
        """Patience is for the busy, not the dead: a same-identity record whose
        socket is closed AND whose recorded pid is gone must fail immediately,
        so a normal cold start after a crash never waits out the window."""
        _write_state(isolated_state)
        probe = Recorder(False)
        monkeypatch.setattr(singleton, "_backend_http_ready", probe)
        monkeypatch.setattr(singleton, "_server_is_healthy", Recorder(False))
        monkeypatch.setattr(singleton, "_is_our_backend", Recorder(False))

        assert singleton._same_identity_backend_ready(PORT) is False
        assert probe.calls == 1  # exited on the dead check, not the deadline


class TestStaleRecordsGetNoPatience:
    @pytest.mark.parametrize("stale", [{"version": "0.0.1"}, {"fingerprint": "fp-OLD"}])
    def test_version_or_source_mismatch_evicts_without_a_single_probe(
        self, isolated_state, our_identity, monkeypatch, stale
    ):
        """An upgrade or source edit must take effect NOW (issue #14/F-206):
        the identity check fails before any probe, so a stale backend pays
        zero patience on its way out."""
        _write_state(isolated_state, **stale)
        probe = Recorder(True)
        terminate = Recorder(True)
        spawn = Recorder(None)
        monkeypatch.setattr(singleton, "_backend_http_ready", probe)
        monkeypatch.setattr(singleton, "_terminate_backend", terminate)
        monkeypatch.setattr(singleton, "_start_server_process", spawn)
        monkeypatch.setattr(singleton, "_wait_for_server", Recorder(True))

        singleton._start_backend_holding_lock(PORT)

        assert probe.calls == 0  # identity gate short-circuits the probe
        assert terminate.calls == 1
        assert spawn.calls == 1


class TestWinnerHoldsLockUntilReady:
    def test_lock_is_held_through_an_mcp_ready_probe_not_just_socket_bind(
        self, isolated_state, our_identity, monkeypatch
    ):
        """The winner's spawn sequence must end with the reuse gate's own
        probe, so the lock is released only into a state where the next
        acquirer sees a reusable backend — closing the bind→ready gap."""
        events: list[str] = []

        def spawn(port):
            events.append("spawn")
            _write_state(isolated_state, port=port)

        monkeypatch.setattr(singleton, "_start_server_process", spawn)
        monkeypatch.setattr(
            singleton,
            "_wait_for_server",
            lambda port, **kw: events.append("socket-bound") or True,
        )
        monkeypatch.setattr(
            singleton,
            "_backend_http_ready",
            lambda port, **kw: bool(events.append("ready-probe")) or True,
        )
        monkeypatch.setattr(singleton, "_terminate_backend", Recorder(True))

        singleton._start_backend_holding_lock(PORT)

        assert events == ["spawn", "socket-bound", "ready-probe"]


class FakeProc:
    """Stands in for the ``Popen`` ``_start_server_process`` now returns. Only
    ``poll()`` is consumed: None = still running, 0 = clean exit (which the uv
    trampoline shim can produce while the real backend lives on), nonzero =
    crashed."""

    def __init__(self, *codes):
        self._codes = list(codes)

    def poll(self):
        return self._codes.pop(0) if len(self._codes) > 1 else self._codes[0]


@pytest.fixture
def no_stale_spawn_failure():
    """`singleton._spawn_failure` is a module global by design (it crosses from
    the cold-start thread to the proxy's readiness wait). Clear it around every
    test so one test's recorded death can never short-circuit another's wait."""
    singleton._spawn_failure.clear()
    yield singleton._spawn_failure
    singleton._spawn_failure.clear()


class TestSpawnedBackendDeathEndsTheWait:
    """issue #56: waiting on a process that has already died is pure latency —
    the live report was a full 120s of silence for a backend that was gone in
    under a second."""

    def test_crashed_child_ends_the_wait_on_the_first_pass(
        self, isolated_state, no_stale_spawn_failure, monkeypatch
    ):
        health = Recorder(False)
        monkeypatch.setattr(singleton, "_server_is_healthy", health)
        monkeypatch.setattr(singleton, "_backend_http_ready", Recorder(False))
        monkeypatch.setattr(singleton, "_backend_failure_reason", lambda: "boom")

        started = time.monotonic()
        result = singleton._wait_for_server(PORT, timeout=30, proc=FakeProc(1))
        elapsed = time.monotonic() - started

        assert result is False
        # ONE socket check == it bailed on the first pass, not at the deadline.
        assert health.calls == 1
        assert elapsed < 2.0, f"burned {elapsed:.1f}s on an already-dead child"

    def test_the_cause_reported_is_the_backend_log_tail(
        self, isolated_state, no_stale_spawn_failure, monkeypatch, tmp_path
    ):
        """The whole point of failing fast is having something to SAY. The
        reason must be the child's own error text (issue #56's live case was a
        settings ValidationError), read from the boot log the Popen redirect
        writes — and bounded, because that log grows without limit."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "backend-boot.log").write_text(
            "".join(f"line-from-a-boot-long-ago-{i}\n" for i in range(500))
            + "pydantic_core.ValidationError: 2 validation errors for Settings\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(logs))
        monkeypatch.setattr(singleton, "_server_is_healthy", Recorder(False))
        monkeypatch.setattr(singleton, "_backend_http_ready", Recorder(False))

        assert singleton._wait_for_server(PORT, timeout=5, proc=FakeProc(1)) is False

        reason = singleton._spawn_failure["reason"]
        assert "pydantic_core.ValidationError: 2 validation errors" in reason
        assert "backend-boot.log" in reason
        assert "line-from-a-boot-long-ago-499" in reason  # the tail is there
        assert "line-from-a-boot-long-ago-100" not in reason  # but not the lot
        assert len(reason) < 10_000  # a multi-MB boot log is never slurped whole

    def test_a_slow_but_healthy_boot_is_still_awaited(
        self, isolated_state, no_stale_spawn_failure, monkeypatch
    ):
        """The anti-F-807 half: a live child that has not bound its socket yet
        must keep the full patience. ``poll()`` is None, so nothing here may
        conclude "dead" no matter how many probes miss."""
        health = Recorder(False, False, False, True)
        monkeypatch.setattr(singleton, "_server_is_healthy", health)
        monkeypatch.setattr(singleton, "_backend_http_ready", Recorder(False))

        assert singleton._wait_for_server(PORT, timeout=30, proc=FakeProc(None)) is True
        assert health.calls == 4
        assert singleton._spawn_failure == {}  # nothing was declared dead

    def test_a_clean_exit_is_not_death_the_trampoline_shim_exits_zero(
        self, isolated_state, no_stale_spawn_failure, monkeypatch
    ):
        """On Windows ``sys.executable`` is a uv trampoline: a shim whose
        identically-named child does the work, so the handle we hold is the
        shim's. Measured 2026-08-01 it blocks for the child's whole life and
        forwards its exit code, but this pins the conservative reading anyway —
        exit code 0 is NEVER death, so a launcher that returned early could not
        make us kill a backend that is still coming up (the F-807 class)."""
        health = Recorder(False, False, True)
        monkeypatch.setattr(singleton, "_server_is_healthy", health)
        monkeypatch.setattr(singleton, "_backend_http_ready", Recorder(False))

        assert singleton._wait_for_server(PORT, timeout=30, proc=FakeProc(0)) is True
        assert singleton._spawn_failure == {}

    def test_an_answering_backend_outvotes_a_nonzero_exit(
        self, isolated_state, no_stale_spawn_failure, monkeypatch
    ):
        """Third reading of the three: even a nonzero exit does not mean dead
        while the MCP probe still answers. Nothing may be declared dead that is
        demonstrably serving."""
        monkeypatch.setattr(singleton, "_server_is_healthy", Recorder(False, True))
        monkeypatch.setattr(singleton, "_backend_http_ready", Recorder(True))

        assert singleton._wait_for_server(PORT, timeout=30, proc=FakeProc(1)) is True
        assert singleton._spawn_failure == {}

    def test_no_proc_handle_keeps_the_old_contract(
        self, isolated_state, no_stale_spawn_failure, monkeypatch
    ):
        """Callers that pass no ``proc`` (and the pre-#56 shape) must behave
        exactly as before: poll to the deadline, then report False."""
        health = Recorder(False)
        monkeypatch.setattr(singleton, "_server_is_healthy", health)

        assert singleton._wait_for_server(PORT, timeout=0) is False
        assert health.calls == 0  # timeout=0 -> the loop never runs, as before


class TestProxyDoesNotWaitOutAProvenDeath:
    """The cold-start thread and the proxy's own 120s readiness wait live in
    the same process; without the shared record the proxy still burned the full
    window on a backend the thread had already buried (issue #56)."""

    def test_await_backend_http_returns_at_once_when_the_spawn_is_known_dead(
        self, no_stale_spawn_failure
    ):
        import anyio

        singleton._spawn_failure["reason"] = "backend-boot.log: ValidationError"

        started = time.monotonic()
        ready = anyio.run(singleton._await_backend_http, "http://127.0.0.1:1/mcp/")
        elapsed = time.monotonic() - started

        assert ready is False
        assert elapsed < 2.0, f"waited {elapsed:.1f}s on a backend known to be dead"
