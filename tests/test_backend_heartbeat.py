"""F-889 (b) — the backend is a witness to its own liveness.

Every other liveness answer in this tree is a claim by the PROBER, and a timeout
is a fact about the backend only while the prober was awake to hear an answer.
On 2026-09-18 the machine had 2.4 GB free of 125.7 and 114 stdio proxies paged
out to ~0 MB; their 2 s probes timed out, the watchdog condemned, and the backend
they condemned answered an MCP ``initialize`` in 227 ms throughout, with zero
errors in its own log for the whole window.

So the backend stamps a wall timestamp and its pid from its EVENT LOOP, and a
proxy reads it with no HTTP, no socket and no thread. A fresh stamp plus a failed
client probe is "I am starved", not "it is dead".

**It stamps a SIDECAR, never ``server.json``** (F-889 review H3). The first pass
put two optional fields on the backend's own entry and wrote them back through a
whole-record read-modify-write, unlocked, every three seconds — which is a lost
update by construction: a sibling's ``record_backend`` landing between the read
and the write was erased, and a stamp interleaved with ``forget_entries``
RESURRECTED the entry that had just been dropped (both measured). Taking the
cold-start lock instead would serialise every backend on the machine against
every proxy start at 3 s intervals, so the record is not the right home for a
high-frequency per-backend fact at all. ``heartbeat-<port>.json`` has exactly ONE
writer — the backend on that port — so there is no merge to get wrong, and the
record it publishes is entirely its own. ``backend_registry`` still owns the
file: its name, its shape, its atomic commit and its deletion.

The two halves are pinned separately because they are different questions: the
WRITE (may it touch ``server.json`` at all? what cleans it up?) and the READ
(``backend_liveness.self_report`` — what counts as evidence?). Hermetic:
``tmp_path`` records only, and ``time.time`` steered through the reader's own
clock.
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
    def test_it_stamps_a_sidecar_beside_the_record(self, record):
        assert (
            backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=1000.0)
            is True
        )

        sidecar = backend_registry.heartbeat_path(record, PORT)
        assert sidecar.name == f"heartbeat-{PORT}.json"
        assert sidecar.parent == record.parent
        assert backend_registry.read_heartbeat(record, PORT) == {
            backend_registry.HEARTBEAT_AT: 1000.0,
            backend_registry.HEARTBEAT_PID: PID,
        }

    def test_it_never_writes_server_json_at_all(self, record):
        """**H3, stated as a single fact.** A whole-record read-modify-write from
        an unlocked writer running every three seconds is a lost update waiting
        for a sibling; it cannot be made safe by being careful, only by not
        touching the record. The byte comparison is the pin."""
        before = record.read_bytes()

        backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=1000.0)

        assert record.read_bytes() == before

    def test_a_stamp_holds_no_snapshot_of_the_record_to_lose(self, record, monkeypatch):
        """**Both measured failures at their shared root.**

        The shipped stamp did ``read_backends`` → mutate → ``_write``, unlocked,
        every three seconds. Two different processes landing in that window
        produced two different harms, and neither is fixable by being careful:
        a sibling's ``record_backend`` was written back out of existence, and a
        ``forget_entries`` was undone — the dropped entry came back from the
        pre-forget snapshot. Reproduced against the shipped code in
        ``$TEMP/verify_review.py``.

        The fix is not a better merge, it is holding no snapshot at all. Pinned
        by making every door onto ``server.json`` explode for the duration: a
        stamp that opens none cannot lose or resurrect anything in it.
        """

        def forbidden(*_args, **_kwargs):
            raise AssertionError("a heartbeat must not read or write server.json")

        monkeypatch.setattr(backend_registry, "read_backends", forbidden)
        monkeypatch.setattr(backend_registry, "read_record", forbidden)
        monkeypatch.setattr(backend_registry, "_write", forbidden)

        assert (
            backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=1000.0)
            is True
        )

    def test_a_forgotten_entry_is_never_resurrected(self, record):
        """The second measured failure. The shipped stamp rewrote the whole
        record from a pre-``forget_entries`` read, so the entry a proxy had just
        cleaned up came back. A stamp cannot put an entry anywhere now — and the
        READ still refuses, because it requires the entry to be there."""
        entry = backend_registry.backend_on_port(
            backend_registry.read_record(record), PORT
        )
        backend_registry.forget_entries(record, [entry])

        backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=time.time())

        assert backend_registry.read_backends(record) == []
        assert backend_liveness.self_report(record, PORT) is None

    def test_forgetting_an_entry_deletes_its_sidecar(self, record):
        """Owned by the record, so it leaves with the record. Without this the
        state dir accumulates one small file per port ever used, and a recycled
        port could be read against a predecessor's stamp."""
        backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=1000.0)
        assert backend_registry.heartbeat_path(record, PORT).exists()

        entry = backend_registry.backend_on_port(
            backend_registry.read_record(record), PORT
        )
        backend_registry.forget_entries(record, [entry])

        assert not backend_registry.heartbeat_path(record, PORT).exists()

    def test_it_leaves_no_temp_residue(self, record):
        """``os.replace``d through the module's one atomic commit, like every
        other write here: a reader concurrent with a stamp sees the whole old
        file or the whole new one, never a truncated one."""
        backend_registry.stamp_heartbeat(record, port=PORT, pid=PID, at=1000.0)

        assert [p.name for p in record.parent.glob("*.tmp")] == []

    def test_a_stamp_for_an_unrecorded_port_is_no_evidence(self, record):
        """It costs one small file and says nothing: :func:`self_report` starts
        from the ENTRY, so a sidecar with no entry behind it is not a claim
        anybody can read."""
        backend_registry.stamp_heartbeat(
            record, port=OTHER_PORT, pid=PID, at=time.time()
        )

        assert backend_liveness.self_report(record, OTHER_PORT) is None

    def test_the_schema_version_does_not_move(self, record):
        """The record's shape is untouched — there is no new field on it at all
        now. A 2.1.9 reader and a 2.1.9 backend are both unaffected; bumping the
        schema would make an upgrade evict rather than adopt."""
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
        """Every field read re-checks its own type. ``True`` is in the table
        because ``isinstance(True, int)`` is True in Python and a bool is not a
        time."""
        import json

        backend_registry.heartbeat_path(record, PORT).write_text(
            json.dumps(
                {
                    backend_registry.HEARTBEAT_AT: hand_edit,
                    backend_registry.HEARTBEAT_PID: PID,
                }
            )
        )

        assert backend_liveness.self_report(record, PORT) is None

    @pytest.mark.parametrize("junk", ["not json at all", "[]", '"a string"', "null"])
    def test_an_unreadable_sidecar_is_not_evidence(self, record, junk):
        """Never raises, and never guesses. An unreadable sidecar falls back to
        today's behaviour — the confirmation phase — exactly as an absent one."""
        backend_registry.heartbeat_path(record, PORT).write_text(junk)

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
                    at = backend_registry.read_heartbeat(record, PORT).get(
                        backend_registry.HEARTBEAT_AT
                    )
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
