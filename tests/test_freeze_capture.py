"""F-993 -- what is captured about a browser the moment it stops answering.

F-963 heals a persistent-profile browser that froze, and the counts its sampler
keeps (threads, handles, RSS) did not say WHY it froze: the decisive evidence (is the
browser spinning or blocked, what is its UI thread waiting on) only exists at the
moment of the freeze and is gone once the heal kills the process. ``freeze_capture``
takes it at the first failed probe: a counts-only record (CPU-time delta, status,
threads, priority, whether a FRESH connection still answers) and, on Windows, a
bounded user-mode minidump.

Hermetic: the process under test is this very test process (or a throwaway child
python), the dump writer is a keyword seam, and the one real ``MiniDumpWriteDump``
node runs only on Windows against a throwaway child that is killed by exact pid.
"""

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import freeze_capture

WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# 1. the counts-only record
# ---------------------------------------------------------------------------


class TestCpuDelta:
    def test_a_spinning_browser_shows_cpu_and_a_blocked_one_shows_none(self):
        before = {10: ("browser", 5.0), 11: ("renderer", 2.0), 12: ("renderer", 1.0)}
        spinning = {10: ("browser", 7.5), 11: ("renderer", 2.0), 12: ("renderer", 1.4)}
        blocked = dict(before)

        busy = freeze_capture.describe_cpu_delta(before, spinning, 3.0)
        idle = freeze_capture.describe_cpu_delta(before, blocked, 3.0)

        assert "browser=2.50s" in busy
        assert "renderers=0.40s (2 total, 1 busy)" in busy
        assert "browser=0.00s" in idle
        assert "renderers=0.00s (2 total, 0 busy)" in idle
        assert busy.startswith("3.0s:") and idle.startswith("3.0s:")

    def test_a_process_that_appeared_or_left_in_the_window_is_not_a_delta(self):
        before = {10: ("browser", 5.0), 11: ("renderer", 2.0)}
        after = {10: ("browser", 5.0), 12: ("renderer", 9.0)}

        line = freeze_capture.describe_cpu_delta(before, after, 3.0)

        assert "renderers=0.00s (0 total, 0 busy)" in line

    def test_a_browser_that_is_gone_at_the_second_sample_says_so(self):
        line = freeze_capture.describe_cpu_delta({10: ("browser", 5.0)}, {}, 3.0)

        assert "browser=gone" in line

    def test_everything_else_in_the_tree_is_counted_apart(self):
        before = {10: ("browser", 0.0), 13: ("other", 1.0)}
        after = {10: ("browser", 0.0), 13: ("other", 1.75)}

        line = freeze_capture.describe_cpu_delta(before, after, 3.0)

        assert "other=0.75s" in line


class TestSnapshotAndFacts:
    def test_the_snapshot_of_this_process_is_a_browser_with_a_cpu_time(self):
        snapshot = freeze_capture.cpu_snapshot(os.getpid())

        kind, seconds = snapshot[os.getpid()]
        assert kind == "browser"  # this process carries no --type=
        assert seconds >= 0.0

    def test_the_facts_name_status_threads_and_priority(self):
        line = freeze_capture.process_facts(os.getpid())

        assert "status=" in line and "threads=" in line and "priority=" in line
        assert int(line.split("threads=")[1].split()[0]) >= 1

    def test_a_pid_that_is_gone_is_unavailable_not_an_exception(self):
        line = freeze_capture.process_facts(2**22 + 7)

        assert "unavailable" in line


class TestTheFreshConnection:
    async def test_an_answer_is_timed(self):
        async def answers():
            return None

        line = await freeze_capture.fresh_answer(answers, 1.0)

        assert line.startswith("answered in ") and line.endswith("ms")

    async def test_silence_is_a_timeout_named_with_its_budget(self):
        async def never():
            await asyncio.sleep(60)

        line = await freeze_capture.fresh_answer(never, 0.3)

        assert line == "no answer within 0.3s"

    async def test_a_refusal_names_the_exception_type_only(self):
        async def refuses():
            raise ConnectionRefusedError("ws://127.0.0.1:9/devtools/browser/SECRET")

        line = await freeze_capture.fresh_answer(refuses, 1.0)

        assert line == "refused (ConnectionRefusedError)"
        assert "SECRET" not in line


# ---------------------------------------------------------------------------
# 2. the dump directory: at most two, oldest first out
# ---------------------------------------------------------------------------


def _dump(directory: Path, name: str, age: float, *, healed: bool = False) -> Path:
    path = directory / f"{name}{'-healed' if healed else ''}.dmp"
    path.write_bytes(b"MDMP")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


class TestRotation:
    def test_it_keeps_the_newest_two_healed_and_deletes_the_rest(self, tmp_path):
        oldest = _dump(tmp_path, "a", 400, healed=True)
        older = _dump(tmp_path, "b", 300, healed=True)
        newer = _dump(tmp_path, "c", 200, healed=True)
        newest = _dump(tmp_path, "d", 100, healed=True)

        freeze_capture.rotate(tmp_path)

        assert not oldest.exists() and not older.exists()
        assert newer.exists() and newest.exists()

    def test_an_unhealed_dump_is_evicted_before_a_healed_one(self, tmp_path):
        """The dump of an incident that REACHED THE HEAL is the one that matters; a
        later benign episode's dump, however much newer, must not push it out."""
        healed_old = _dump(tmp_path, "heal-1", 900, healed=True)
        healed_new = _dump(tmp_path, "heal-2", 800, healed=True)
        benign_old = _dump(tmp_path, "benign-1", 20)
        benign_new = _dump(tmp_path, "benign-2", 10)

        freeze_capture.rotate(tmp_path)

        assert healed_old.exists() and healed_new.exists()
        assert not benign_old.exists()
        assert benign_new.exists()  # one unhealed is kept, the newest

    def test_unhealed_dumps_are_capped_at_one_whatever_the_healed_count(self, tmp_path):
        for index, age in enumerate((300, 200, 100)):
            _dump(tmp_path, f"b{index}", age)

        freeze_capture.rotate(tmp_path)

        assert [p.name for p in tmp_path.iterdir()] == ["b2.dmp"]

    def test_the_defaults_are_two_healed_and_one_unhealed(self):
        assert freeze_capture.MAX_DUMPS == 2
        assert freeze_capture.MAX_UNHEALED == 1

    def test_it_leaves_other_files_alone(self, tmp_path):
        keep = tmp_path / "notes.txt"
        keep.write_text("x", encoding="utf-8")
        for index, age in enumerate((300, 200, 100)):
            _dump(tmp_path, f"d{index}", age)

        freeze_capture.rotate(tmp_path)

        assert keep.exists()

    def test_it_sweeps_a_stale_partial_but_not_the_one_in_progress(self, tmp_path):
        stale = tmp_path / "old.dmp.partial"
        live = tmp_path / "live.dmp.partial"
        stale.write_bytes(b"x")
        live.write_bytes(b"x")

        freeze_capture.rotate(tmp_path, keep_partial=live.name)

        assert not stale.exists() and live.exists()

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        freeze_capture.rotate(tmp_path / "nope")


class TestMarkHealed:
    def test_the_newest_unhealed_dump_of_the_label_is_renamed(self, tmp_path):
        older = _dump(tmp_path, "freeze-fleet-abc-1", 300)
        newest = _dump(tmp_path, "freeze-fleet-abc-2", 100)

        kept = freeze_capture.mark_healed("freeze-fleet-abc", tmp_path)

        assert kept == str(tmp_path / "freeze-fleet-abc-2-healed.dmp")
        assert (tmp_path / "freeze-fleet-abc-2-healed.dmp").exists()
        assert not newest.exists()
        assert older.exists()  # the one unhealed dump a benign episode may keep

    def test_another_browsers_dump_is_not_touched(self, tmp_path):
        mine = _dump(tmp_path, "freeze-fleet-abc-1", 100)
        theirs = _dump(tmp_path, "freeze-default-xyz-1", 50)

        freeze_capture.mark_healed("freeze-fleet-abc", tmp_path)

        assert theirs.exists() and not mine.exists()
        assert (tmp_path / "freeze-fleet-abc-1-healed.dmp").exists()

    def test_no_dump_is_none_not_an_error(self, tmp_path):
        assert freeze_capture.mark_healed("freeze-x", tmp_path) is None
        assert freeze_capture.mark_healed("freeze-x", tmp_path / "nope") is None

    def test_an_already_healed_dump_is_not_renamed_again(self, tmp_path):
        _dump(tmp_path, "freeze-x-1", 100, healed=True)

        assert freeze_capture.mark_healed("freeze-x", tmp_path) is None
        assert (tmp_path / "freeze-x-1-healed.dmp").exists()


class TestTheDumpLivesUnderTheLogDir:
    def test_it_is_a_subdirectory_of_resolve_log_dir(self, monkeypatch, tmp_path):
        from stealth_chrome_devtools_mcp.embedded import logging_setup

        monkeypatch.setattr(logging_setup, "resolve_log_dir", lambda: tmp_path)

        assert freeze_capture.dump_directory() == tmp_path / "freeze-dumps"

    def test_the_backend_log_sweep_does_not_count_or_delete_it(self, tmp_path):
        """``prune_old_logs`` globs ``*.log*`` FILES in the log dir; a subdirectory
        of dumps is neither, so the 50-file sweep leaves it be."""
        from stealth_chrome_devtools_mcp.embedded import logging_setup

        directory = tmp_path / freeze_capture.DUMP_DIR_NAME
        directory.mkdir()
        dump = _dump(directory, "freeze-x", 10 * 86400)

        logging_setup.prune_old_logs(tmp_path, keep_days=1, keep_files=0)

        assert dump.exists()


# ---------------------------------------------------------------------------
# 3. the minidump: attempted on Windows, bounded, never fatal
# ---------------------------------------------------------------------------


def _writer(payload: bytes = b"MDMP-body", calls: list | None = None):
    def write(pid: int, path: Path) -> None:
        if calls is not None:
            calls.append((pid, path))
        path.write_bytes(payload)

    return write


class TestMinidump:
    async def test_it_is_attempted_on_windows_and_lands_as_a_dmp(self, tmp_path):
        calls: list = []

        line = await freeze_capture.write_minidump(
            4242,
            tmp_path,
            "freeze-fleet-abc",
            platform="win32",
            write=_writer(calls=calls),
        )

        ((pid, partial),) = calls
        assert pid == 4242 and partial.name == "freeze-fleet-abc.dmp.partial"
        assert (tmp_path / "freeze-fleet-abc.dmp").read_bytes() == b"MDMP-body"
        assert not partial.exists()
        assert str(tmp_path / "freeze-fleet-abc.dmp") in line
        assert f"type=0x{freeze_capture.MINIDUMP_TYPE:X}" in line

    async def test_another_platform_is_skipped_with_the_reason_and_writes_nothing(
        self, tmp_path
    ):
        calls: list = []

        line = await freeze_capture.write_minidump(
            4242, tmp_path, "x", platform="linux", write=_writer(calls=calls)
        )

        assert line == "skipped: platform"
        assert calls == [] and list(tmp_path.iterdir()) == []

    async def test_a_failure_is_recorded_with_its_reason_and_leaves_no_partial(
        self, tmp_path
    ):
        def fails(_pid: int, path: Path) -> None:
            path.write_bytes(b"half")
            raise OSError(0, "Only part of a ReadProcessMemory request was completed")

        line = await freeze_capture.write_minidump(
            4242, tmp_path, "x", platform="win32", write=fails
        )

        assert line.startswith("failed: OSError")
        assert "ReadProcessMemory" in line
        assert list(tmp_path.iterdir()) == []

    async def test_a_dump_that_outlives_its_budget_does_not_block_the_loop(
        self, tmp_path
    ):
        release = threading.Event()

        def stuck(_pid: int, path: Path) -> None:
            release.wait(30)
            path.write_bytes(b"late")

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        watcher = asyncio.create_task(ticker())
        started = time.monotonic()
        try:
            line = await freeze_capture.write_minidump(
                4242, tmp_path, "x", budget=0.2, platform="win32", write=stuck
            )
            elapsed = time.monotonic() - started
        finally:
            watcher.cancel()
            release.set()
            await asyncio.sleep(0.2)  # let the stuck writer finish and clean up

        assert elapsed < 2.0
        assert ticks >= 5  # the loop kept turning while the write was stuck
        assert line.startswith("not finished within 0.2s")
        assert str(tmp_path / "x.dmp") in line

    async def test_a_second_dump_does_not_start_while_the_first_is_stuck(
        self, tmp_path
    ):
        release = threading.Event()
        calls: list = []

        def stuck(pid: int, path: Path) -> None:
            calls.append(pid)
            release.wait(30)
            path.write_bytes(b"late")

        # A loaded machine can still be finishing the PREVIOUS test's dump thread (the
        # lock is process-wide) or be slow to start this one's: wait for each, so the
        # 0.05 s budgets below measure the queue and not the scheduler.
        for _ in range(200):
            if not freeze_capture._dump_running.locked():
                break
            await asyncio.sleep(0.05)
        try:
            await freeze_capture.write_minidump(
                1, tmp_path, "a", budget=0.05, platform="win32", write=stuck
            )
            for _ in range(200):
                if calls:
                    break
                await asyncio.sleep(0.05)
            second = await freeze_capture.write_minidump(
                2, tmp_path, "b", budget=0.05, platform="win32", write=stuck
            )
        finally:
            release.set()
            await asyncio.sleep(0.2)

        assert second.startswith("skipped: an earlier dump was still being written")
        assert "0.1s" in second  # the wait it gave, so the record says WHY
        assert calls == [1]

    async def test_two_dumps_asked_together_are_written_one_behind_the_other(
        self, tmp_path
    ):
        """``default`` and ``fleet`` freeze together: BOTH get a dump, serially (one
        dbghelp writer at a time), and the loop is never blocked."""
        order: list = []

        def slow(pid: int, path: Path) -> None:
            order.append(("start", pid))
            time.sleep(0.15)
            path.write_bytes(b"MDMP")
            order.append(("end", pid))

        first, second = await asyncio.gather(
            freeze_capture.write_minidump(
                1, tmp_path, "a", budget=5.0, platform="win32", write=slow
            ),
            freeze_capture.write_minidump(
                2, tmp_path, "b", budget=5.0, platform="win32", write=slow
            ),
        )

        # both were WRITTEN; rotation then keeps the one unhealed dump, the newer
        assert [p.name for p in tmp_path.iterdir()] == ["b.dmp"]
        assert str(tmp_path / "a.dmp") in first and str(tmp_path / "b.dmp") in second
        assert [e[0] for e in order] == ["start", "end", "start", "end"]

    async def test_a_nearly_full_disk_skips_the_dump(self, tmp_path, monkeypatch):
        monkeypatch.setattr(freeze_capture, "MIN_FREE_BYTES", 2**62)
        calls: list = []

        line = await freeze_capture.write_minidump(
            4242, tmp_path, "x", platform="win32", write=_writer(calls=calls)
        )

        assert line.startswith("skipped: only ") and "free" in line
        assert calls == []

    async def test_four_unhealed_dumps_leave_one(self, tmp_path):
        for index in range(4):
            await freeze_capture.write_minidump(
                1, tmp_path, f"freeze-{index}", platform="win32", write=_writer()
            )
            stamp = time.time() - (10 - index)
            os.utime(tmp_path / f"freeze-{index}.dmp", (stamp, stamp))

        assert sorted(p.name for p in tmp_path.iterdir()) == ["freeze-3.dmp"]

    async def test_dump_now_names_the_file_by_label_and_utc_stamp(self, tmp_path):
        line = await freeze_capture.dump_now(
            7, "freeze-fleet-abc", directory=tmp_path, platform="win32", write=_writer()
        )

        (written,) = tmp_path.iterdir()
        assert written.name.startswith("freeze-fleet-abc-")
        assert written.name.endswith("Z.dmp")
        assert str(written) in line

    def test_the_type_has_thread_stacks_handle_data_and_no_heap(self):
        """Pins the choice (see the module docstring): Normal already carries every
        thread's stack; HandleData is what a blocked thread waits ON; ThreadInfo
        and ProcessThreadData give times, priority and the TEB/PEB. FullMemory
        (0x2) and IndirectlyReferencedMemory (0x40) are what make a dump huge and
        carry page content, so they are NOT in it."""
        chosen = freeze_capture.MINIDUMP_TYPE

        assert chosen & 0x4  # MiniDumpWithHandleData
        assert chosen & 0x1000  # MiniDumpWithThreadInfo
        assert chosen & 0x100  # MiniDumpWithProcessThreadData
        assert chosen & 0x20  # MiniDumpWithUnloadedModules
        assert not chosen & 0x2  # MiniDumpWithFullMemory
        assert not chosen & 0x40  # MiniDumpWithIndirectlyReferencedMemory
        assert not chosen & 0x1  # MiniDumpWithDataSegs (module globals)


def _streams(raw: bytes) -> dict[int, bytes]:
    """The stream directory of a minidump: ``{stream type: stream bytes}``."""
    import struct

    _sig, _version, count, directory = struct.unpack_from("<4sIII", raw, 0)
    found = {}
    for index in range(count):
        kind, size, rva = struct.unpack_from("<III", raw, directory + 12 * index)
        found[kind] = raw[rva : rva + size]
    return found


@pytest.mark.skipif(not WINDOWS, reason="MiniDumpWriteDump is Windows-only")
class TestTheRealDbghelpWriter:
    """Against a throwaway child python, killed by its exact pid."""

    async def test_it_writes_a_readable_mdmp(self, tmp_path):
        child = subprocess.Popen(  # noqa: S603  PERMANENT(F-993: a throwaway child python to dump)
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            await asyncio.sleep(0.5)
            line = await freeze_capture.write_minidump(
                child.pid, tmp_path, "freeze-real", budget=30.0
            )
            dump = tmp_path / "freeze-real.dmp"
            header = dump.read_bytes()[:4]
        finally:
            child.kill()
            child.wait(10)

        assert header == b"MDMP", line
        assert dump.stat().st_size > 0
        assert "failed" not in line and "skipped" not in line

    async def test_it_carries_the_streams_the_type_promises(self, tmp_path):
        """The header alone proves nothing: without ``PROCESS_DUP_HANDLE`` on the
        process handle, dbghelp quietly leaves the handle-data stream out."""
        child = subprocess.Popen(  # noqa: S603  PERMANENT(F-993: a throwaway child python to dump)
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            await asyncio.sleep(0.5)
            await freeze_capture.write_minidump(
                child.pid, tmp_path, "freeze-streams", budget=30.0
            )
            streams = _streams((tmp_path / "freeze-streams.dmp").read_bytes())
        finally:
            child.kill()
            child.wait(10)

        assert streams.keys() >= {
            3,  # ThreadListStream: every thread's context and stack
            4,  # ModuleListStream
            12,  # HandleDataStream: what a blocked thread waits ON
            16,  # MemoryInfoListStream
            17,  # ThreadInfoListStream
        }
        assert 9 not in streams  # Memory64ListStream: the full-memory form
        threads = int.from_bytes(streams[3][:4], "little")
        handles = int.from_bytes(streams[12][8:12], "little")
        assert threads >= 1 and handles >= 1

    async def test_an_unopenable_pid_is_a_recorded_failure(self, tmp_path):
        line = await freeze_capture.write_minidump(
            2**22 + 7, tmp_path, "freeze-gone", budget=10.0
        )

        assert line.startswith("failed: OSError")
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 3b. the probe's step timings
# ---------------------------------------------------------------------------


class TestTimedSteps:
    def test_each_step_is_timed_from_its_start_to_the_next_one(self):
        ticks = iter([100.0, 100.05, 100.35, 114.8])
        steps = freeze_capture.TimedSteps(clock=lambda: next(ticks))

        steps.append("createTarget")
        steps.append("evaluate")
        steps.finish()

        assert freeze_capture.describe_steps(steps) == (
            "connect=50ms createTarget=300ms evaluate=14450ms"
        )

    def test_a_probe_that_never_got_past_connecting_says_so(self):
        ticks = iter([5.0, 20.0])
        steps = freeze_capture.TimedSteps(clock=lambda: next(ticks))

        steps.finish()

        assert freeze_capture.describe_steps(steps) == "connect=15000ms (no step began)"

    def test_it_is_still_a_list_of_step_names_for_the_probe(self):
        steps = freeze_capture.TimedSteps()
        steps.append("evaluate")

        assert steps == ["evaluate"] and steps[-1] == "evaluate"


# ---------------------------------------------------------------------------
# 4. the whole capture
# ---------------------------------------------------------------------------


class TestCapture:
    async def test_the_record_carries_every_fact_and_is_one_line(self, tmp_path):
        async def answers():
            return None

        record = await freeze_capture.capture(
            pid=os.getpid(),
            label="freeze-fleet-abcd1234",
            answers=answers,
            directory=tmp_path,
            sample_seconds=0.05,
            platform="win32",
            write=_writer(),
        )

        assert "\n" not in record
        assert "cpu(0.1s:" in record or "cpu(0.0s:" in record
        assert "process(status=" in record and "threads=" in record
        assert "fresh_cdp(Browser.getVersion: answered in " in record
        assert "minidump(" in record and ".dmp" in record

    async def test_a_pid_less_browser_still_asks_the_fresh_connection(self, tmp_path):
        async def answers():
            return None

        record = await freeze_capture.capture(
            pid=None, label="x", answers=answers, directory=tmp_path, platform="win32"
        )

        assert "cpu(unavailable (no pid))" in record
        assert "fresh_cdp(Browser.getVersion: answered in " in record
        assert "minidump(skipped: no pid)" in record

    async def test_it_samples_cpu_across_the_window(self, tmp_path):
        seen: list[float] = []
        clock = iter([1.0, 3.5])

        def snapshot(pid: int) -> dict[int, tuple[str, float]]:
            seen.append(next(clock))
            return {pid: ("browser", seen[-1])}

        async def answers():
            return None

        record = await freeze_capture.capture(
            pid=7,
            label="x",
            answers=answers,
            directory=tmp_path,
            sample_seconds=0.01,
            platform="linux",
            snapshot=snapshot,
        )

        assert "browser=2.50s" in record

    async def test_the_platform_skip_lands_in_the_record(self, tmp_path):
        async def answers():
            return None

        record = await freeze_capture.capture(
            pid=os.getpid(),
            label="x",
            answers=answers,
            directory=tmp_path,
            sample_seconds=0.0,
            platform="linux",
        )

        assert "minidump(skipped: platform)" in record

    async def test_a_dead_fact_does_not_take_the_others_with_it(self, tmp_path):
        async def answers():
            raise ConnectionRefusedError

        record = await freeze_capture.capture(
            pid=2**22 + 7,
            label="x",
            answers=answers,
            directory=tmp_path,
            sample_seconds=0.0,
            platform="linux",
        )

        assert "cpu(unavailable (" in record
        assert (
            "fresh_cdp(Browser.getVersion: refused (ConnectionRefusedError))" in record
        )
        assert "minidump(skipped: platform)" in record

    async def test_it_holds_no_page_content(self, tmp_path):
        """Counts, a path and a pid -- never a URL, a title or a cookie."""

        async def answers():
            return None

        record = await freeze_capture.capture(
            pid=os.getpid(),
            label="x",
            answers=answers,
            directory=tmp_path,
            sample_seconds=0.0,
            platform="win32",
            write=_writer(),
        )

        for forbidden in ("http", "title", "cookie", "ws://"):
            assert forbidden not in record.lower()


async def _finished(incidents: freeze_capture.Incidents) -> None:
    """Wait until every capture the incidents started has run to its end, whatever
    the interpreter's number of loop iterations per await (``wait_for`` took one more
    on 3.11): a fixed ``sleep`` raced it."""
    await asyncio.gather(*incidents._running, return_exceptions=True)


class TestIncidents:
    def _incidents(self, now: list[float], records: list[str], log: list[str]):
        async def capture(**facts) -> str:
            records.append(facts["label"] + ":" + str(facts["skip_dump"]))
            return "record"

        async def dump_now(pid, label, **_k) -> str:
            return f"late({pid},{label})"

        return freeze_capture.Incidents(
            capture=capture,
            dump_now=dump_now,
            mark_healed=lambda label: f"{label}-healed.dmp",
            clock=lambda: now[0],
            log=log.append,
        )

    async def test_begin_returns_at_once_and_the_task_logs_its_record(self):
        records: list[str] = []
        log: list[str] = []
        incidents = self._incidents([0.0], records, log)

        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        assert incidents.has("i") and records == []  # not run yet: begin did not await
        await _finished(incidents)

        assert records == ["l:None"]
        assert log == ["i: evidence@probe1(record)"]

    async def test_a_second_begin_in_the_same_incident_does_nothing(self):
        records: list[str] = []
        incidents = self._incidents([0.0], records, [])

        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        await _finished(incidents)

        assert len(records) == 1

    async def test_a_pid_that_may_not_be_touched_is_recorded_not_captured(self):
        records: list[str] = []
        log: list[str] = []
        incidents = self._incidents([0.0], records, log)

        incidents.begin("i", pid=1, label="l", answers=lambda: None, problem="reused")
        await _finished(incidents)

        assert records == []
        assert log == ["i: evidence@probe1(capture skipped: reused)"]

    async def test_the_dump_is_withheld_inside_the_interval_and_taken_at_the_heal(self):
        now = [0.0]
        records: list[str] = []
        incidents = self._incidents(now, records, [])
        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        await _finished(incidents)
        incidents.end("i")
        now[0] = 60.0
        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        await _finished(incidents)

        said = await incidents.before_heal("i", pid=1, label="l")

        assert records[0] == "l:None" and records[1].startswith("l:the last unhealed")
        assert said == (
            "evidence@probe1(record); minidump_at_heal(late(1,l)); dump_kept(l-healed.dmp)"
        )

    async def test_a_withheld_dump_is_not_taken_for_a_pid_that_may_not_be_opened(self):
        now = [0.0]
        incidents = self._incidents(now, [], [])
        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        await _finished(incidents)
        incidents.end("i")
        now[0] = 60.0
        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        await _finished(incidents)

        said = await incidents.before_heal("i", pid=1, label="l", problem="reused")

        assert "minidump_at_heal(skipped: reused)" in said
        assert "late(" not in said

    async def test_a_withheld_dump_that_never_returns_is_cut_and_the_heal_goes_on(
        self, monkeypatch
    ):
        monkeypatch.setattr(freeze_capture, "HEAL_WAITS_FOR_CAPTURE_SECONDS", 0.05)
        monkeypatch.setattr(freeze_capture, "_DUMP_OUTER_GRACE_SECONDS", 0.05)
        now = [0.0]

        async def hangs(*_a, **_k) -> str:
            await asyncio.Event().wait()
            return ""

        incidents = freeze_capture.Incidents(
            capture=lambda **_k: asyncio.sleep(0, "r"),
            dump_now=hangs,
            mark_healed=lambda label: f"{label}-healed.dmp",
            clock=lambda: now[0],
            log=lambda _line: None,
        )
        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        await _finished(incidents)
        incidents.end("i")
        now[0] = 60.0
        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        await _finished(incidents)

        said = await asyncio.wait_for(
            incidents.before_heal("i", pid=1, label="l"), timeout=5
        )

        assert "minidump_at_heal(failed: TimeoutError)" in said
        assert "dump_kept(l-healed.dmp)" in said

    async def test_a_mark_that_raises_is_said_so_and_the_heal_goes_on(self):
        def broken(_label: str) -> str | None:
            raise OSError("locked")

        incidents = freeze_capture.Incidents(
            capture=lambda **_k: asyncio.sleep(0, "r"),
            dump_now=lambda *_a, **_k: asyncio.sleep(0, "x"),
            mark_healed=broken,
            clock=lambda: 0.0,
            log=lambda _line: None,
        )

        said = await incidents.before_heal("i", pid=1, label="l")

        assert said == "evidence@probe1(none); dump_not_marked_healed(OSError)"

    async def test_no_incident_is_said_so_and_still_marks_what_there_is(self):
        incidents = self._incidents([0.0], [], [])

        said = await incidents.before_heal("i", pid=1, label="l")

        assert said == "evidence@probe1(none); dump_kept(l-healed.dmp)"

    async def test_forget_drops_the_spacing_too_and_end_keeps_it(self):
        incidents = self._incidents([0.0], [], [])
        incidents.begin("i", pid=1, label="l", answers=lambda: None)
        await _finished(incidents)

        incidents.end("i")
        assert "i" in incidents.tracked()
        incidents.forget("i")
        assert "i" not in incidents.tracked()


def test_the_module_is_a_leaf_that_imports_no_server_and_no_browser_health():
    """``browser_health`` imports it, so it cannot import that back; and no module
    under ``embedded/`` imports ``server`` (CLAUDE.md convention 1)."""
    lines = Path(freeze_capture.__file__).read_text(encoding="utf-8").splitlines()
    imports = [ln for ln in lines if ln.startswith(("import ", "from ", "    from "))]

    assert not [ln for ln in imports if "browser_health" in ln]
    assert not [
        ln for ln in imports if ln.strip().endswith(("import server", ".server"))
    ]
    assert psutil.pid_exists(os.getpid())
