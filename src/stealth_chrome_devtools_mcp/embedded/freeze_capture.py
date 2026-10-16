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

*Bounded three ways.* Off the event loop, on its own daemon thread (a pool thread
would block interpreter exit if the write never returned); a time budget
(:data:`DUMP_BUDGET_SECONDS`), after which the loop moves on and the record says the
dump had not finished; and at most one dump at a time -- a write that is stuck in the
kernel must not pile up behind itself. It is written to ``<name>.dmp.partial`` and
renamed on success, so a half-written file is never mistaken for evidence. Disk is
bounded too: at most :data:`MAX_DUMPS` kept (the oldest deleted), and no dump at all
below :data:`MIN_FREE_BYTES` free. They live in ``<log dir>/freeze-dumps/``, a
subdirectory ``logging_setup.prune_old_logs`` neither counts nor sweeps.

*Other platforms* record ``skipped: platform``.

**3. The exit code** of a browser that exited is :mod:`process_exit`'s
(:func:`process_exit.exit_status`), not this module's.

A leaf: ``psutil``, ``ctypes`` and stdlib, ``browser_cmdline`` (is a pid a renderer)
and ``logging_setup`` (the log dir). It imports neither ``browser_health`` (which
imports it) nor ``server``; the fresh-connection check arrives as an argument.
Every fact is gathered independently, so one that cannot be read does not cost the
others, and nothing here raises into the caller.
"""

import asyncio
import contextlib
import shutil
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

import psutil

from stealth_chrome_devtools_mcp.embedded import browser_cmdline, logging_setup

#: How long the CPU-time delta is taken over. Long enough to tell a spinning
#: process from a blocked one on a 15.6 ms Windows timer, short enough to leave the
#: heal's two-minute spacing alone.
CPU_SAMPLE_SECONDS = 3.0

#: How long a FRESH connection gets to answer ``Browser.getVersion``.
FRESH_ANSWER_TIMEOUT_SECONDS = 5.0

#: How long the loop waits for the dump to be written before it moves on.
DUMP_BUDGET_SECONDS = 45.0

#: The most dumps kept; the oldest is deleted when a newer one lands.
MAX_DUMPS = 2

#: No dump is started with less than this free on the volume.
MIN_FREE_BYTES = 2 * 2**30

DUMP_DIR_NAME = "freeze-dumps"
_DUMP_SUFFIX = ".dmp"
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
        await asyncio.wait_for(answers(), seconds)
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


def rotate(directory: Path, keep: int = MAX_DUMPS, *, keep_partial: str = "") -> None:
    """Keep the newest *keep* dumps and delete the rest, and sweep any partial file
    other than *keep_partial* (the one being written). Never raises."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    dumps: list[tuple[float, str, Path]] = []
    for path in entries:
        if path.name.endswith(_PARTIAL_SUFFIX):
            if path.name != keep_partial:
                with contextlib.suppress(OSError):
                    path.unlink()
        elif path.name.endswith(_DUMP_SUFFIX):
            with contextlib.suppress(OSError):
                dumps.append((path.stat().st_mtime, path.name, path))
    for _mtime, _name, stale in sorted(dumps, reverse=True)[keep:]:
        with contextlib.suppress(OSError):
            stale.unlink()


def _write_with_dbghelp(pid: int, path: Path) -> None:
    """``MiniDumpWriteDump`` of *pid* into *path*. Raises ``OSError`` carrying the
    Win32 error on any refusal."""
    if sys.platform != "win32":
        raise OSError("MiniDumpWriteDump is Windows-only")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
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
    if not _dump_running.acquire(blocking=False):
        return "skipped: an earlier dump is still being written"
    loop = asyncio.get_running_loop()
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
        path, size = await asyncio.wait_for(done, budget)
    except Exception as error:  # noqa: BLE001  PERMANENT(F-993: a failed, skipped or late dump is recorded, never raised into the health loop)
        return _explain(error, budget, final)
    return (
        f"{path} ({size / 2**20:.1f} MB in {time.monotonic() - started:.1f}s, "
        f"type=0x{MINIDUMP_TYPE:X})"
    )


# ---------------------------------------------------------------------------
# Probe step timings
# ---------------------------------------------------------------------------


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
) -> str:
    """One line of counts about a browser that stopped answering, then (Windows) a
    minidump of it. *label* names the dump file; *answers* is the fresh-connection
    ``Browser.getVersion`` check, which this module cannot import."""

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
    if pid is None:
        dump = "skipped: no pid"
    else:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dump = await write_minidump(
            pid,
            directory or dump_directory(),
            f"{label}-{stamp}",
            budget=dump_budget,
            platform=platform,
            write=write,
        )
    return " ".join([*shown, f"minidump({dump})"])
