"""F-889 (b) — the backend is a witness to its own liveness.

Every other liveness answer in this tree is a claim by the PROBER, and a timeout
is a fact about the backend only while the prober was awake to hear an answer.
On 2026-09-18 the machine had 2.4 GB free of 125.7 and 114 stdio proxies paged
out to ~0 MB; their 2 s probes timed out, the watchdog condemned, and the backend
they condemned answered an MCP ``initialize`` in 227 ms throughout, with zero
errors in its own log for the whole window.

So the backend stamps a wall timestamp and its pid into its own ``server.json``
entry from its EVENT LOOP, and a proxy reads it with no HTTP, no socket and no
thread. A fresh stamp plus a failed client probe is "I am starved", not "it is
dead".

The two halves are pinned separately because they are different questions: the
WRITE (``backend_registry.stamp_heartbeat`` — may it touch any other entry? may
it create one?) and the READ (``backend_liveness.self_report`` — what counts as
evidence?). Hermetic: ``tmp_path`` records only, and ``time.time`` steered
through the reader's own clock.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from stealth_chrome_devtools_mcp.embedded import backend_liveness, backend_registry

PORT = 51999
OTHER_PORT = 51998
PID = 4242


def _record(path, *entries):
    for port, pid, context in entries:
        backend_registry.record_backend(
            path,
            port=port,
            version="2.1.9",
            pid=pid,
            source_fingerprint="a" * 64,
            display_context=context,
        )
    return path


@pytest.fixture()
def record(tmp_path):
    return _record(tmp_path / "server.json", (PORT, PID, "win-session-1"))


class TestTheWrite:
    def test_it_stamps_the_entry_on_that_port(self, record):
        assert (
            backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=1000.0)
            is True
        )

        entry = backend_registry.backend_on_port(
            backend_registry.read_record(record), PORT
        )
        assert entry[backend_registry.HEARTBEAT_AT] == 1000.0
        assert entry[backend_registry.HEARTBEAT_PID] == PID

    def test_it_leaves_every_other_entry_byte_identical(self, tmp_path):
        path = _record(
            tmp_path / "server.json",
            (PORT, PID, "win-session-1"),
            (OTHER_PORT, 999, "win-session-2"),
        )
        before = backend_registry.backend_on_port(
            backend_registry.read_record(path), OTHER_PORT
        )

        backend_registry.stamp_heartbeat(path, port=PORT, pid=PID, at=1000.0)

        after = backend_registry.backend_on_port(
            backend_registry.read_record(path), OTHER_PORT
        )
        assert after == before, "a sibling's entry must not move"

    def test_it_writes_nothing_when_no_entry_claims_the_port(self, record):
        """THE contract that makes a lock-free writer safe: a stamp must never
        resurrect an entry ``forget_entries`` has just dropped, or a proxy
        cleaning up a dead record would find it back a moment later."""
        before = record.read_text()

        assert (
            backend_registry.stamp_heartbeat(
                record, port=OTHER_PORT, pid=PID, at=1000.0
            )
            is False
        )

        assert record.read_text() == before
        assert backend_registry.read_backends(record) == backend_registry.backends_in(
            backend_registry.read_record(record)
        )

    def test_a_forgotten_entry_stays_forgotten(self, record):
        entry = backend_registry.backend_on_port(
            backend_registry.read_record(record), PORT
        )
        backend_registry.forget_entries(record, [entry])

        assert (
            backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=1000.0)
            is False
        )
        assert backend_registry.read_backends(record) == []

    def test_a_stamp_survives_a_concurrent_record_of_a_different_port(self, record):
        backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=1000.0)
        backend_registry.record_backend(
            record,
            port=OTHER_PORT,
            version="2.1.9",
            pid=999,
            source_fingerprint="a" * 64,
            display_context="win-session-2",
        )

        entry = backend_registry.backend_on_port(
            backend_registry.read_record(record), PORT
        )
        assert entry[backend_registry.HEARTBEAT_AT] == 1000.0

    def test_the_schema_version_does_not_move(self, record):
        """Two OPTIONAL fields on an existing entry are not a shape change. A
        2.1.9 reader copies entries whole and ignores them; a 2.1.9 backend
        writes none, so a newer reader sees 'absent'. Bumping the schema would
        make an upgrade evict rather than adopt."""
        backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=1000.0)

        assert backend_registry.read_record(record)["schema"] == 3
        assert backend_registry.SCHEMA_VERSION == 3


class TestTheRead:
    def test_a_fresh_stamp_is_evidence(self, record):
        backend_registry.stamp_heartbeat(
            record, port=PORT, pid=PID, at=time.time() - 1.0
        )

        age = backend_liveness.self_report(record, PORT)
        assert age is not None
        assert 0.0 <= age < 5.0

    def test_a_stamp_written_this_instant_reads_as_evidence_not_as_false(self, record):
        """``0.0`` is falsy. Callers must test ``is not None``, and this node is
        what says so out loud."""
        backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=time.time())

        assert backend_liveness.self_report(record, PORT) is not None

    def test_a_stale_stamp_is_not_evidence(self, record):
        backend_registry.stamp_heartbeat(
            record,
            port=PORT,
            pid=PID,
            at=time.time() - backend_liveness.HEARTBEAT_STALE_SECONDS - 1.0,
        )

        assert backend_liveness.self_report(record, PORT) is None

    def test_an_absent_stamp_is_not_evidence(self, record):
        """A 2.1.9 backend writes none. A mixed fleet must degrade to today's
        behaviour, never read silence as a claim."""
        assert backend_liveness.self_report(record, PORT) is None

    def test_an_absent_entry_is_not_evidence(self, record):
        assert backend_liveness.self_report(record, OTHER_PORT) is None

    def test_a_missing_record_is_not_evidence(self, tmp_path):
        assert backend_liveness.self_report(tmp_path / "absent.json", PORT) is None

    def test_a_stamp_from_the_future_is_not_evidence(self, record):
        """A clock that jumped forward is not a backend that is alive. The bound
        is symmetric for exactly that reason."""
        backend_registry.stamp_heartbeat(
            record,
            port=PORT,
            pid=PID,
            at=time.time() + backend_liveness.HEARTBEAT_STALE_SECONDS + 1.0,
        )

        assert backend_liveness.self_report(record, PORT) is None

    def test_a_pid_that_disagrees_with_the_entry_is_not_evidence(self, record):
        """The stamp is then a leftover from a predecessor on that port, not a
        claim by the process the record names."""
        backend_registry.stamp_heartbeat(record, port=PORT, pid=PID + 1, at=time.time())

        assert backend_liveness.self_report(record, PORT) is None

    @pytest.mark.parametrize("hand_edit", ["soon", None, True, [1], {"a": 1}])
    def test_a_hand_edited_stamp_is_not_evidence(self, record, hand_edit):
        """This record tolerates hand-editing and two prior schemas, so every
        field read re-checks its own type. ``True`` is in the table because
        ``isinstance(True, int)`` is True in Python and a bool is not a time."""
        import json

        raw = json.loads(record.read_text())
        raw["backends"][0][backend_registry.HEARTBEAT_AT] = hand_edit
        raw["backends"][0][backend_registry.HEARTBEAT_PID] = PID
        record.write_text(json.dumps(raw))

        assert backend_liveness.self_report(record, PORT) is None


class TestTheBackendSideTask:
    def test_beat_stamps_repeatedly_on_the_loop_it_is_started_on(self, record):
        async def drive():
            async with asyncio.timeout(5):
                task = asyncio.create_task(
                    backend_liveness.beat(record, PORT, PID, interval=0.01)
                )
                seen = set()
                while len(seen) < 2:
                    await asyncio.sleep(0.01)
                    entry = backend_registry.backend_on_port(
                        backend_registry.read_record(record), PORT
                    )
                    at = (entry or {}).get(backend_registry.HEARTBEAT_AT)
                    if at is not None:
                        seen.add(at)
                task.cancel()
                return seen

        stamps = asyncio.run(drive())
        assert len(stamps) >= 2, "the heartbeat must keep beating, not stamp once"

    def test_start_beating_is_idempotent_per_process(self, record, monkeypatch):
        """``server.py`` is executed three times under runpy
        (``session_hygiene.install()``'s precedent and the same reason)."""
        monkeypatch.setattr(backend_liveness, "_BEATING", False)
        started = []

        async def drive():
            started.append(backend_liveness.start_beating(record, PORT, PID))
            started.append(backend_liveness.start_beating(record, PORT, PID))
            await asyncio.sleep(0)

        asyncio.run(drive())
        assert started == [True, False]

    def test_start_beating_outside_a_loop_is_a_no_op(self, record, monkeypatch):
        """A backend that cannot stamp degrades to 2.1.9's behaviour, which is
        strictly better than a backend that will not serve."""
        monkeypatch.setattr(backend_liveness, "_BEATING", False)

        assert backend_liveness.start_beating(record, PORT, PID) is False

    def test_a_stamp_that_cannot_be_written_never_raises(self, tmp_path, monkeypatch):
        def boom(*_a, **_kw):
            raise OSError("the record is on a synced folder mid-lock")

        monkeypatch.setattr(backend_registry, "stamp_heartbeat", boom)

        assert backend_liveness.stamp(tmp_path / "server.json", PORT, PID) is False


class TestTheTwoNumbersStateTheirRelation:
    def test_stale_is_ten_missed_intervals(self):
        """Sized against the incident, not against taste: a backend answering
        ``initialize`` in 227 ms is stamping, and one starved badly enough to
        miss TEN consecutive loop iterations while its socket still answers is a
        state nobody has observed. Pinned so neither number can move alone."""
        assert backend_liveness.HEARTBEAT_INTERVAL_SECONDS == 3.0
        assert backend_liveness.HEARTBEAT_STALE_SECONDS == 30.0
        assert (
            backend_liveness.HEARTBEAT_STALE_SECONDS
            / backend_liveness.HEARTBEAT_INTERVAL_SECONDS
            == 10
        )
