"""THE one home for what is captured about a persistent-profile browser the moment it
stops answering, BEFORE the heal closes it (F-993).

**Why it exists.** F-963 heals a frozen long-lived browser and logs counts about it,
and after six hard freezes on 2026-10-11..13 (``/json`` answering, ``Runtime.evaluate``
dead in every tab, a force kill that did not end the process for seconds) the cause is
still unknown, because everything the sampler keeps -- threads, handles, GDI, RSS --
stayed flat across the onset. What decides it exists only AT the freeze and dies with
the process: is the browser spinning or blocked, and what is its UI thread waiting on.
So the first failed probe takes three things, once per incident, and healing is not
touched by any of them.

**1. A counts-only record** (:func:`capture`): the browser's and its renderers'
CPU-time delta over :data:`CPU_SAMPLE_SECONDS` (spinning against blocked), psutil's
status and thread count, the priority class, and whether ``Browser.getVersion`` over a
FRESH connection answers inside :data:`FRESH_ANSWER_TIMEOUT_SECONDS`. Never a URL, a
title, a cookie or a command line.

**2. A user-mode minidump, Windows only** (:func:`write_minidump`), through
``MiniDumpWriteDump`` from ``dbghelp.dll`` over ctypes.

*Why that API and not ``rundll32 comsvcs.dll, MiniDump``.* ``comsvcs`` always writes
``MiniDumpWithFullMemory`` (the whole address space of a Chrome browser process:
~600 MB measured on a fresh one, 0.5-1.5 GB estimated for a long-lived one, and it
carries every page of content the browser held),
offers no say over the type, and is the textbook credential-dumping command line that
Defender and EDR products block or quarantine -- on a host where Defender is resident
a dump that silently never appears is worse than none. ``dbghelp`` lets us choose the
type and reports a real error code.

*The type* (:data:`MINIDUMP_TYPE`) is ``MiniDumpNormal`` (every thread's stack and
the module list: with Chrome's public symbols this is the UI thread's call stack) plus
``MiniDumpWithHandleData`` (the events, mutexes and files a blocked thread is waiting
ON), ``WithThreadInfo`` (per-thread times, priority, suspend count),
``WithProcessThreadData`` (the TEB and PEB), ``WithUnloadedModules`` and
``WithFullMemoryInfo`` (the address-space map): 1.4-1.6 MB measured on a fresh
Chrome (59 threads, ~1,650 handles). The process handle needs ``PROCESS_DUP_HANDLE``
too, or dbghelp silently omits the handle stream. It leaves out ``FullMemory``,
``IndirectlyReferencedMemory`` and ``DataSegs``: those are what make a dump large and
what put heap, module globals and page content into it. The dump is therefore local
evidence of the same kind as Chrome's own log (:mod:`chrome_log`): stack memory can
still hold a fragment of whatever a thread was handling, so it is never uploaded and
the record carries its PATH only.

*Bounded.* Off the event loop, on its own daemon thread (a pool thread would block
interpreter exit if the write never returned); a time budget
(:data:`DUMP_BUDGET_SECONDS`) covering the wait for its turn AND the write, after which
the loop moves on and the record says why; and one dump at a time -- two browsers that
freeze together are dumped one behind the other inside their budgets, and a write stuck
in the kernel makes the next give up when its own budget is spent instead of piling up.
It is written to ``<name>.dmp.partial`` and renamed on success, so a half-written file
is never mistaken for evidence. No dump at all below :data:`MIN_FREE_BYTES` free.
They live in ``<log dir>/freeze-dumps/`` (``STEALTH_MCP_LOG_DIR`` relocates them), a
subdirectory ``logging_setup.prune_old_logs`` neither counts nor sweeps.

*Retention protects the one that mattered.* A dump of an incident that reaches the
heal is renamed ``...-healed.dmp`` (:func:`mark_healed`); a slow-but-alive episode never
is. Rotation keeps the newest :data:`MAX_DUMPS` healed ones and only the newest
:data:`MAX_UNHEALED` unhealed one, deleting the unhealed first, so a run of benign
episodes cannot push a freeze's dump out. :class:`Incidents` also spaces a browser's
unhealed dumps :data:`DUMP_MIN_INTERVAL_SECONDS` apart; a heal that finds its dump was
withheld takes one at that moment, because a heal is the freeze.

*Other platforms* record ``skipped: platform``.

**3. The exit code** of a browser that exited is :mod:`process_exit`'s
(:func:`process_exit.exit_status`), not this module's.

**Running beside the probe loop, not inside it.** :class:`Incidents` runs the capture
as a task started at the first failed probe, so the loop keeps its two-minute spacing
and two browsers are captured concurrently; the heal waits for that task at most
:data:`HEAL_WAITS_FOR_CAPTURE_SECONDS`, which is how the record still lands before the
kill whenever it can (it started two minutes earlier, and a dump is seconds).

A leaf: ``psutil``, ``ctypes`` and stdlib, ``browser_cmdline`` (is a pid a renderer),
``process_exit`` (the kernel32 bindings) and ``logging_setup`` (the log dir). It
imports neither ``browser_health`` (which imports it) nor ``server``; the
fresh-connection check, the pid-identity verdict and the log writer arrive as
arguments.
Every fact is gathered independently, so one that cannot be read does not cost the
others, and nothing here raises into the caller.
"""

import asyncio
import contextlib
import shutil
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

import psutil

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    logging_setup,
    process_exit,
)
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

#: How long the CPU-time delta is taken over. Long enough to tell a spinning
#: process from a blocked one on a 15.6 ms Windows timer, short enough to leave the
#: heal's two-minute spacing alone.
CPU_SAMPLE_SECONDS = 3.0

#: How long a FRESH connection gets to answer ``Browser.getVersion``.
FRESH_ANSWER_TIMEOUT_SECONDS = 5.0

#: How long the loop waits for the dump to be written before it moves on.
DUMP_BUDGET_SECONDS = 45.0

#: How long the heal waits for the capture task it is about to outrun: it started a
#: probe interval earlier, so this is the tail of a slow dump, never the whole of one.
HEAL_WAITS_FOR_CAPTURE_SECONDS = 10.0

#: How long the heal waits for the rename of the healed dump (one thread hop and a
#: file rename). Its own budget, not the capture's: a test shortens the capture's wait
#: to make a hung dump quick, and a loaded host takes longer than that to run a thread.
HEAL_WAITS_FOR_MARK_SECONDS = 10.0

#: Slack on top of the dump's own budget for the heal's outer ``asyncio.timeout``,
#: so a dump that keeps its budget says why itself and only one that does not
#: return is cut.
_DUMP_OUTER_GRACE_SECONDS = 2.0

#: The bound on one whole capture (the CPU window and the fresh connection run
#: together, then the dump has its own budget). Under the probe interval.
CAPTURE_TIMEOUT_SECONDS = 60

#: Healed dumps kept (the oldest deleted); an unhealed one is kept only until a
#: newer one lands.
MAX_DUMPS = 2
MAX_UNHEALED = 1

#: The least time between two UNHEALED dumps of one browser.
DUMP_MIN_INTERVAL_SECONDS = 1800.0

#: No dump is started with less than this free on the volume.
MIN_FREE_BYTES = 2 * 2**30

DUMP_DIR_NAME = "freeze-dumps"
_DUMP_SUFFIX = ".dmp"
_HEALED_SUFFIX = "-healed.dmp"
_PARTIAL_SUFFIX = ".dmp.partial"

# MINIDUMP_TYPE bits (minidumpapiset.h): Win32 API values that cannot change.
_WITH_HANDLE_DATA = 0x00000004
_WITH_UNLOADED_MODULES = 0x00000020
_WITH_PROCESS_THREAD_DATA = 0x00000100
_WITH_FULL_MEMORY_INFO = 0x00000800
_WITH_THREAD_INFO = 0x00001000
MINIDUMP_TYPE = (
    _WITH_HANDLE_DATA
    | _WITH_UNLOADED_MODULES
    | _WITH_PROCESS_THREAD_DATA
    | _WITH_FULL_MEMORY_INFO
    | _WITH_THREAD_INFO
)

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
# Without it MiniDumpWriteDump silently omits the handle-data stream (measured).
_PROCESS_DUP_HANDLE = 0x0040

_RENDERER = "renderer"
_BROWSER = "browser"
_OTHER = "other"
_BUSY_SECONDS = 0.01  # under a timer tick: not "busy"
_QUEUE_POLL_SECONDS = 0.05

#: Held by the dump's thread for as long as it runs, so a stuck write is never
#: joined by a second one.
_dump_running = threading.Lock()

Snapshot = dict[int, tuple[str, float]]
Writer = Callable[[int, Path], None]


class _DumpSkippedError(Exception):
    """The dump was not started, and why."""


# ---------------------------------------------------------------------------
# The counts-only record
# ---------------------------------------------------------------------------


def cpu_snapshot(pid: int) -> Snapshot:
    """``{pid: (kind, cpu seconds)}`` for the browser and every process under it.
    ``kind`` is ``browser`` (no ``--type``), ``renderer`` or ``other``."""
    root = psutil.Process(pid)
    taken: Snapshot = {}
    for proc in [root, *root.children(recursive=True)]:
        try:
            times = proc.cpu_times()
            declared = browser_cmdline.flag_value(
                browser_cmdline.arguments(proc.pid), browser_cmdline.TYPE_FLAG
            )
        except (psutil.Error, OSError):
            continue
        if declared is None:
            kind = _BROWSER
        elif declared == _RENDERER:
            kind = _RENDERER
        else:
            kind = _OTHER
        taken[proc.pid] = (kind, times.user + times.system)
    return taken


def describe_cpu_delta(before: Snapshot, after: Snapshot, seconds: float) -> str:
    """``3.0s: browser=2.50s renderers=0.40s (2 total, 1 busy) other=0.00s``: CPU
    seconds each group used in the window, over the processes present at both ends."""
    spent: dict[str, float] = {_BROWSER: 0.0, _RENDERER: 0.0, _OTHER: 0.0}
    renderers = busy = 0
    browser_seen = False
    for pid, (kind, start) in before.items():
        if pid not in after:
            continue
        used = max(after[pid][1] - start, 0.0)
        spent[kind] += used
        browser_seen = browser_seen or kind == _BROWSER
        if kind == _RENDERER:
            renderers += 1
            busy += used >= _BUSY_SECONDS
    browser = f"{spent[_BROWSER]:.2f}s" if browser_seen else "gone"
    return (
        f"{seconds:.1f}s: browser={browser} "
        f"renderers={spent[_RENDERER]:.2f}s ({renderers} total, {busy} busy) "
        f"other={spent[_OTHER]:.2f}s"
    )


_PRIORITY_CLASSES = (
    "IDLE",
    "BELOW_NORMAL",
    "NORMAL",
    "ABOVE_NORMAL",
    "HIGH",
    "REALTIME",
)


def _priority(proc: psutil.Process) -> str:
    """The priority class by name on Windows (where it is what Chrome's background
    throttling sets on a renderer), ``nice=N`` elsewhere."""
    value = proc.nice()
    if sys.platform == "win32":
        for name in _PRIORITY_CLASSES:
            if value == getattr(psutil, f"{name}_PRIORITY_CLASS", None):
                return name.lower()
    return f"nice={value}"


def process_facts(pid: int) -> str:
    """``status=running threads=52 priority=normal``; a fact that cannot be read is
    ``unavailable (<exception type>)`` and the others still are."""

    def read(name: str, ask: Callable[[psutil.Process], object]) -> str:
        try:
            return f"{name}={ask(psutil.Process(pid))}"
        except (psutil.Error, OSError) as error:
            return f"{name}=unavailable ({type(error).__name__})"

    return " ".join(
        (
            read("status", lambda proc: proc.status()),
            read("threads", lambda proc: proc.num_threads()),
            read("priority", _priority),
        )
    )


async def fresh_answer(answers: Callable[[], Awaitable[None]], seconds: float) -> str:
    """Did a NEW connection get its answer, and how fast. Names the exception type
    on a refusal and never its text (an endpoint URL carries a browser id)."""
    started = time.monotonic()
    try:
        async with asyncio.timeout(seconds):
            await answers()
    except TimeoutError:
        return f"no answer within {seconds:.1f}s"
    except Exception as error:  # noqa: BLE001  PERMANENT(F-993: every way of not answering is the finding)
        return f"refused ({type(error).__name__})"
    return f"answered in {(time.monotonic() - started) * 1000:.0f}ms"


# ---------------------------------------------------------------------------
# The minidump
# ---------------------------------------------------------------------------


def dump_directory() -> Path:
    """``<log dir>/freeze-dumps``. Pure: it creates nothing."""
    return logging_setup.resolve_log_dir() / DUMP_DIR_NAME


def rotate(
    directory: Path,
    keep: int = MAX_DUMPS,
    *,
    keep_unhealed: int = MAX_UNHEALED,
    keep_partial: str = "",
) -> None:
    """Keep the newest *keep* healed dumps and the newest *keep_unhealed* unhealed
    ones, delete the rest, and sweep any partial file other than *keep_partial* (the
    one being written). Never raises."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    healed: list[tuple[float, str, Path]] = []
    unhealed: list[tuple[float, str, Path]] = []
    for path in entries:
        if path.name.endswith(_PARTIAL_SUFFIX):
            if path.name != keep_partial:
                with contextlib.suppress(OSError):
                    path.unlink()
        elif path.name.endswith(_DUMP_SUFFIX):
            group = healed if path.name.endswith(_HEALED_SUFFIX) else unhealed
            with contextlib.suppress(OSError):
                group.append((path.stat().st_mtime, path.name, path))
    for group, limit in ((healed, keep), (unhealed, keep_unhealed)):
        for _mtime, _name, stale in sorted(group, reverse=True)[limit:]:
            with contextlib.suppress(OSError):
                stale.unlink()


def mark_healed(label: str, directory: Path | None = None) -> str | None:
    """Rename the newest unhealed dump of *label* to ``...-healed.dmp``, so rotation
    keeps it ahead of every benign episode's. The new path, or ``None`` when there is
    none. Never raises."""
    folder = directory or dump_directory()
    try:
        mine = [
            (path.stat().st_mtime, path.name, path)
            for path in folder.iterdir()
            if path.name.startswith(f"{label}-")
            and path.name.endswith(_DUMP_SUFFIX)
            and not path.name.endswith(_HEALED_SUFFIX)
        ]
        if not mine:
            return None
        newest = max(mine)[2]
        target = newest.with_name(
            newest.name.removesuffix(_DUMP_SUFFIX) + _HEALED_SUFFIX
        )
        newest.replace(target)
        rotate(folder)
    except OSError:
        return None
    return str(target)


def _write_with_dbghelp(pid: int, path: Path) -> None:
    """``MiniDumpWriteDump`` of *pid* into *path*. Raises ``OSError`` carrying the
    Win32 error on any refusal."""
    if sys.platform != "win32":
        raise OSError("MiniDumpWriteDump is Windows-only")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = process_exit.kernel32()  # OpenProcess / CloseHandle, typed once there
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    system = ctypes.create_unicode_buffer(260)
    if not kernel.GetSystemDirectoryW(system, len(system)):
        raise ctypes.WinError(ctypes.get_last_error())
    # The system copy by full path: nothing in the working directory may stand in.
    dbghelp = ctypes.WinDLL(
        str(Path(system.value) / "dbghelp.dll"), use_last_error=True
    )
    dbghelp.MiniDumpWriteDump.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    dbghelp.MiniDumpWriteDump.restype = wintypes.BOOL
    process = kernel.OpenProcess(
        _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ | _PROCESS_DUP_HANDLE, False, pid
    )
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        with path.open("wb") as out:
            written = dbghelp.MiniDumpWriteDump(
                process,
                pid,
                msvcrt.get_osfhandle(out.fileno()),
                MINIDUMP_TYPE,
                None,
                None,
                None,
            )
            if not written:
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(process)


def _dump_job(pid: int, directory: Path, name: str, write: Writer) -> tuple[Path, int]:
    """The whole dump on the worker thread: room check, write, rename, rotate."""
    directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(directory).free
    if free < MIN_FREE_BYTES:
        raise _DumpSkippedError(f"only {free / 2**30:.1f} GB free")
    partial = directory / f"{name}{_PARTIAL_SUFFIX}"
    final = directory / f"{name}{_DUMP_SUFFIX}"
    try:
        write(pid, partial)
        size = partial.stat().st_size
        partial.replace(final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    rotate(directory, keep_partial=partial.name)
    return final, size


def _settle(
    done: asyncio.Future[tuple[Path, int]], outcome: tuple[Path, int] | BaseException
) -> None:
    """Hand the worker's outcome to the awaiting side, unless it gave up already."""
    if done.done():
        return
    if isinstance(outcome, BaseException):
        done.set_exception(outcome)
    else:
        done.set_result(outcome)


def _explain(error: Exception, budget: float, final: Path) -> str:
    """The record's words for a dump that did not land."""
    if isinstance(error, TimeoutError):
        return (
            f"not finished within {budget:.1f}s (still running; the file lands at "
            f"{final} if it completes)"
        )
    if isinstance(error, _DumpSkippedError):
        return f"skipped: {error}"
    return f"failed: {type(error).__name__}: {error}"


async def write_minidump(  # noqa: PLR0913  PERMANENT(F-993: the budget, platform and writer are keyword seams so the dump runs hermetically)
    pid: int,
    directory: Path,
    name: str,
    *,
    budget: float = DUMP_BUDGET_SECONDS,
    platform: str = sys.platform,
    write: Writer | None = None,
) -> str:
    """Dump *pid* into ``<directory>/<name>.dmp`` and say what happened, in words:
    the path and size, ``skipped: <why>``, ``failed: <reason>`` or that the write
    had not finished within *budget*. Never raises; never blocks the loop."""
    if platform != "win32":
        return "skipped: platform"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while not _dump_running.acquire(blocking=False):  # another browser's dump: queue
        if loop.time() >= deadline:
            return (
                "skipped: an earlier dump was still being written after waiting "
                f"{budget:.1f}s"
            )
        await asyncio.sleep(_QUEUE_POLL_SECONDS)
    done: asyncio.Future[tuple[Path, int]] = loop.create_future()
    started = time.monotonic()
    final = directory / f"{name}{_DUMP_SUFFIX}"
    writer = write or _write_with_dbghelp

    def work() -> None:
        outcome: tuple[Path, int] | BaseException
        try:
            outcome = _dump_job(pid, directory, name, writer)
        except BaseException as error:  # noqa: BLE001  PERMANENT(F-993: any failure of the write is handed to the awaiting side, which records it)
            outcome = error
        finally:
            _dump_running.release()
        with contextlib.suppress(RuntimeError):  # the loop may be gone
            loop.call_soon_threadsafe(_settle, done, outcome)

    try:
        threading.Thread(target=work, name="freeze-minidump", daemon=True).start()
    except RuntimeError as error:
        _dump_running.release()
        return f"failed: {type(error).__name__}: {error}"
    try:
        async with asyncio.timeout(max(deadline - loop.time(), 0.05)):
            path, size = await done
    except Exception as error:  # noqa: BLE001  PERMANENT(F-993: a failed, skipped or late dump is recorded, never raised into the health loop)
        return _explain(error, budget, final)
    return (
        f"{path} ({size / 2**20:.1f} MB in {time.monotonic() - started:.1f}s, "
        f"type=0x{MINIDUMP_TYPE:X})"
    )


# ---------------------------------------------------------------------------
# Probe step timings
# ---------------------------------------------------------------------------


def identity_problem(entry: Mapping[str, object], pid: int | None) -> str | None:
    """Why *pid* may not be opened for the browser recorded in *entry*, or ``None``
    (F-993): it is not the recorded pid, or its create time is not the recorded one,
    i.e. the pid was reused. The rule is ``process_cleanup``'s own (one home); an
    entry with no pid or no create time recorded has nothing to contradict."""
    recorded = entry.get("pid")
    if pid is None or recorded is None:
        return None
    if recorded != pid:
        return f"pid {pid} is not the recorded browser pid {recorded}"
    stored = entry.get("create_time")
    if not process_cleanup._fallback_pid_identity_ok(
        pid, stored if isinstance(stored, float) else None
    ):
        return f"pid {pid} is gone or not the process recorded (start time differs)"
    return None


class TimedSteps(list[str]):
    """The probe's *steps* list, which also notes WHEN each step began (F-993), so a
    failed probe says where its 15 s went and not only where it was standing.

    Still a plain ``list[str]`` to the probe, which only ever ``append``s a name."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._clock = clock
        self.began = clock()
        self.starts: list[float] = []
        self.ended: float | None = None

    def append(self, step: str) -> None:
        super().append(step)
        self.starts.append(self._clock())

    def finish(self) -> None:
        """Mark the probe over (answered, refused or timed out)."""
        self.ended = self._clock()


def describe_steps(steps: TimedSteps) -> str:
    """``connect=50ms createTarget=300ms evaluate=14450ms``: the time before the
    first step (the websocket open), then each step to the start of the next, the
    last to the end of the probe."""
    ended = steps.ended if steps.ended is not None else steps.began
    if not steps.starts:
        return f"connect={(ended - steps.began) * 1000:.0f}ms (no step began)"
    edges = [steps.began, *steps.starts, ended]
    shown = [
        f"{name}={(edges[i + 1] - edges[i]) * 1000:.0f}ms"
        for i, name in enumerate(["connect", *steps])
    ]
    return " ".join(shown)


# ---------------------------------------------------------------------------
# The whole capture
# ---------------------------------------------------------------------------


async def _unavailable(reason: str) -> str:
    return f"unavailable ({reason})"


async def _cpu_window(
    pid: int, seconds: float, snapshot: Callable[[int], Snapshot]
) -> str:
    before = await asyncio.to_thread(snapshot, pid)
    await asyncio.sleep(seconds)
    after = await asyncio.to_thread(snapshot, pid)
    return describe_cpu_delta(before, after, seconds)


async def capture(  # noqa: PLR0913  PERMANENT(F-993: one keyword seam per external effect, so the capture runs hermetically)
    *,
    pid: int | None,
    label: str,
    answers: Callable[[], Awaitable[None]],
    directory: Path | None = None,
    sample_seconds: float = CPU_SAMPLE_SECONDS,
    dump_budget: float = DUMP_BUDGET_SECONDS,
    platform: str = sys.platform,
    write: Writer | None = None,
    snapshot: Callable[[int], Snapshot] = cpu_snapshot,
    skip_dump: str | None = None,
) -> str:
    """One line of counts about a browser that stopped answering, then (Windows) a
    minidump of it. *label* names the dump file; *answers* is the fresh-connection
    ``Browser.getVersion`` check, which this module cannot import; *skip_dump* is the
    reason the caller withholds the dump, recorded in its place."""

    if pid is None:
        window: Awaitable[str] = _unavailable("no pid")
        facts: Awaitable[str] = _unavailable("no pid")
    else:
        window = _cpu_window(pid, sample_seconds, snapshot)
        facts = asyncio.to_thread(process_facts, pid)
    parts = await asyncio.gather(
        window,
        facts,
        fresh_answer(answers, FRESH_ANSWER_TIMEOUT_SECONDS),
        return_exceptions=True,
    )
    named = ("cpu", "process", "fresh_cdp")
    shown = []
    for name, part in zip(named, parts, strict=True):
        text = (
            f"unavailable ({type(part).__name__})"
            if isinstance(part, BaseException)
            else str(part)
        )
        shown.append(
            f"fresh_cdp(Browser.getVersion: {text})"
            if name == "fresh_cdp"
            else f"{name}({text})"
        )
    if skip_dump is not None:
        dump = f"skipped: {skip_dump}"
    elif pid is None:
        dump = "skipped: no pid"
    else:
        dump = await dump_now(
            pid,
            label,
            directory=directory,
            budget=dump_budget,
            platform=platform,
            write=write,
        )
    return " ".join([*shown, f"minidump({dump})"])


async def dump_now(  # noqa: PLR0913  PERMANENT(F-993: the same keyword seams as write_minidump)
    pid: int,
    label: str,
    *,
    directory: Path | None = None,
    budget: float = DUMP_BUDGET_SECONDS,
    platform: str = sys.platform,
    write: Writer | None = None,
) -> str:
    """:func:`write_minidump` into the dump directory under ``<label>-<UTC stamp>``."""
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return await write_minidump(
        pid,
        directory or dump_directory(),
        f"{label}-{stamp}",
        budget=budget,
        platform=platform,
        write=write,
    )


# ---------------------------------------------------------------------------
# One incident at a time, per browser
# ---------------------------------------------------------------------------


class Incidents:
    """The capture of each browser's CURRENT incident, run beside the probe loop.

    ``begin`` (the first failed probe) starts the capture as a task and returns at
    once; ``end`` (a probe passed, or the browser is gone) cancels it;
    ``before_heal`` waits for it for at most :data:`HEAL_WAITS_FOR_CAPTURE_SECONDS`,
    takes the dump a rate limit withheld, and marks the dump healed. Nothing here
    raises into the caller, and the record is logged by ``log`` the moment the task
    finishes."""

    def __init__(
        self,
        *,
        capture: Callable[..., Awaitable[str]],
        dump_now: Callable[..., Awaitable[str]],
        mark_healed: Callable[[str], str | None],
        clock: Callable[[], float],
        log: Callable[[str], None],
    ) -> None:
        self._capture = capture
        self._dump_now = dump_now
        self._mark_healed = mark_healed
        self._clock = clock
        self._log = log
        self._tasks: dict[str, asyncio.Task[str]] = {}
        self._running: set[asyncio.Task[str]] = set()  # strong refs, until done
        self._last_dump: dict[str, float] = {}
        self._withheld: set[str] = set()

    def has(self, instance_id: str) -> bool:
        """Is this browser's current incident already being captured?"""
        return instance_id in self._tasks

    def tracked(self) -> set[str]:
        return set(self._tasks) | set(self._last_dump)

    def begin(
        self,
        instance_id: str,
        *,
        pid: int | None,
        label: str,
        answers: Callable[[], Awaitable[None]],
        problem: str | None = None,
    ) -> None:
        """Start the capture of a new incident. *problem* is why this pid may not be
        touched at all (it is not the browser we recorded); it is recorded instead."""
        if instance_id in self._tasks:
            return
        if problem is not None:
            job = _skipped(instance_id, problem, self._log)
        else:
            now = self._clock()
            last = self._last_dump.get(instance_id)
            skip = None
            if last is not None and now - last < DUMP_MIN_INTERVAL_SECONDS:
                skip = (
                    f"the last unhealed dump of this browser was {now - last:.0f}s "
                    f"ago (minimum {DUMP_MIN_INTERVAL_SECONDS:.0f}s)"
                )
                self._withheld.add(instance_id)
            else:
                self._last_dump[instance_id] = now
            job = self._run(instance_id, pid, label, answers, skip)
        task = asyncio.create_task(job)
        self._tasks[instance_id] = task
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _run(
        self,
        instance_id: str,
        pid: int | None,
        label: str,
        answers: Callable[[], Awaitable[None]],
        skip: str | None,
    ) -> str:
        try:
            async with asyncio.timeout(CAPTURE_TIMEOUT_SECONDS):
                record = await self._capture(
                    pid=pid, label=label, answers=answers, skip_dump=skip
                )
        except Exception as error:  # noqa: BLE001  PERMANENT(F-993: evidence is best effort and must not stand between a hung browser and its heal)
            record = f"capture unavailable ({type(error).__name__})"
        self._log(f"{instance_id}: evidence@probe1({record})")
        return record

    def end(self, instance_id: str) -> None:
        """The incident is over without a heal (a probe passed) or the browser is
        gone: stop the capture. Its dump, if written, stays unhealed. The spacing
        between unhealed dumps is kept for a browser that is still there."""
        task = self._tasks.pop(instance_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._withheld.discard(instance_id)

    def forget(self, instance_id: str) -> None:
        """A browser that is gone: no incident and no spacing to keep."""
        self.end(instance_id)
        self._last_dump.pop(instance_id, None)

    def cancel_all(self) -> None:
        """Shutdown: stop every capture still running."""
        for task in list(self._running):
            task.cancel()

    async def before_heal(
        self,
        instance_id: str,
        *,
        pid: int | None,
        label: str,
        problem: str | None = None,
    ) -> str:
        """What the heal's ERROR line says about the incident, taken now: the
        capture's record (waited for at most :data:`HEAL_WAITS_FOR_CAPTURE_SECONDS`),
        the dump a rate limit withheld, and where the healed dump is kept. *problem*
        is why *pid* may not be opened now (minutes have passed since the first
        failure; it may no longer be the browser recorded): that dump is not taken."""
        task = self._tasks.pop(instance_id, None)
        parts = []
        if task is None:
            parts.append("evidence@probe1(none)")
        else:
            try:
                async with asyncio.timeout(HEAL_WAITS_FOR_CAPTURE_SECONDS):
                    record = await asyncio.shield(task)
            except TimeoutError:
                waited = f"{HEAL_WAITS_FOR_CAPTURE_SECONDS:.0f}s"
                record = f"still capturing; the heal waits {waited} and no longer"
            except Exception as error:  # noqa: BLE001  PERMANENT(F-993: nothing about the evidence may stop the heal)
                record = f"capture unavailable ({type(error).__name__})"
            parts.append(f"evidence@probe1({record})")
        if instance_id in self._withheld and pid is not None:
            self._withheld.discard(instance_id)
            if problem is not None:
                late = f"skipped: {problem}"
            else:
                try:
                    async with asyncio.timeout(
                        HEAL_WAITS_FOR_CAPTURE_SECONDS + _DUMP_OUTER_GRACE_SECONDS
                    ):
                        late = await self._dump_now(
                            pid, label, budget=HEAL_WAITS_FOR_CAPTURE_SECONDS
                        )
                except Exception as error:  # noqa: BLE001  PERMANENT(F-993: nothing about the evidence may stop the heal)
                    late = f"failed: {type(error).__name__}"
            parts.append(f"minidump_at_heal({late})")
        self._last_dump.pop(instance_id, None)  # this dump is healed: spacing resets
        try:
            async with asyncio.timeout(HEAL_WAITS_FOR_MARK_SECONDS):
                kept = await asyncio.to_thread(self._mark_healed, label)
        except Exception as error:  # noqa: BLE001  PERMANENT(F-993: nothing about the evidence may stop the heal)
            parts.append(f"dump_not_marked_healed({type(error).__name__})")
            kept = None
        if kept:
            parts.append(f"dump_kept({kept})")
        return "; ".join(parts)


async def _skipped(instance_id: str, problem: str, log: Callable[[str], None]) -> str:
    log(f"{instance_id}: evidence@probe1(capture skipped: {problem})")
    return f"capture skipped: {problem}"
