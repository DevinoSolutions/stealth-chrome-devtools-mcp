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
"""

from __future__ import annotations

import json

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
            lambda port, timeout=30: events.append("socket-bound") or True,
        )
        monkeypatch.setattr(
            singleton,
            "_backend_http_ready",
            lambda port, **kw: bool(events.append("ready-probe")) or True,
        )
        monkeypatch.setattr(singleton, "_terminate_backend", Recorder(True))

        singleton._start_backend_holding_lock(PORT)

        assert events == ["spawn", "socket-bound", "ready-probe"]
