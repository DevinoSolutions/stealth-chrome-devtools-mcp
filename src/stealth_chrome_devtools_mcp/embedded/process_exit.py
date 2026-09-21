"""THE one home for ending a browser's OS process: wait for its own exit, then
escalate (F-910).

Chrome commits its cookie store lazily — a batch timer, plus a flush on clean
shutdown — so ``Browser.close`` is not the end of the story, it is the START of
one. The shutdown that follows writes the cookie jar that
``profile_seed.LOGIN_WITNESSES`` leads with (named THERE and nowhere else, this
docstring included — F-892's one-home pin), and it takes time: **measured
0.100-0.169 s on this machine across 20 unaided exits**, with
the whole of ``close_instance`` returning in 0.128-0.319 s. Before F-910 the
kill path ran the instant the graceful close had been SENT, so it landed on a
browser that was still shutting down **20 times out of 20 measured** — and when
the terminate beat the commit, the login the user had just made was gone. One
Windows CI runner lost it twice in two runs on two unrelated branches; locally
it was one close in thirty.

So the kill is no longer the first thing that happens to a browser we asked to
leave. ``wait_for_exit`` gives it ``EXIT_GRACE_SECONDS`` to go on its own, and
``terminate`` — the escalation that was already here — runs afterwards, on a
process that has usually already gone. Nothing is skipped: a browser that does
NOT exit inside the grace is terminated exactly as it was before this module
existed.

**Which pid.** ``browser_pid`` answers the BROWSER, never one of its children:
a Chrome profile is held by a whole tree (measured on one real spawn: eleven
processes — the browser, six renderers, two utilities, a gpu-process and a
crashpad-handler) and only the member with no ``--type`` is the browser
(``browser_cmdline.browser_process``' rule, reached here from the other side).
Waiting on a renderer would answer "exited" while the browser was still
flushing, which is this finding's own defect wearing a fix's clothes. A pid
carrying ``--type`` therefore answers None — do not wait, kill as before —
rather than being silently accepted.

**The wait must not REAP, and that is a POSIX fact, not a preference.** A
browser we launched is a CHILD of this process, and on POSIX asyncio is
already waiting on it: ``ThreadedChildWatcher.add_child_handler`` starts a
daemon thread per child that sits in a blocking ``os.waitpid(pid, 0)``
(CPython 3.13.11, ``asyncio/unix_events.py``:1421-1443), and on a Linux new
enough for ``pidfd_open`` the default is ``PidfdChildWatcher``, whose
``_do_wait`` reaps with the same call at :990. A child's exit status can be
collected exactly ONCE, so a second waiter is a race for it — and both
watchers answer losing it the same way: ``ChildProcessError`` ->
``returncode = 255`` plus a WARNING ("Unknown child process pid %d, will
report returncode 255" at :1449-1451, "exit status already read" at :995-998).
``psutil.Process.wait()`` is a second waiter: on POSIX it is
``_psposix.wait_pid``, which polls ``os.waitpid(pid, os.WNOHANG)``
(``psutil/_psposix.py``:62-155) and reaps whatever it finds. So the first
draft of this module could have made ``Browser._process.returncode`` read 255
for a browser that exited cleanly with 0 — a value ``terminate``'s own guard
reads — and put a spurious WARNING in every close's log.

The wait therefore OBSERVES instead: ``_has_exited`` polls
``Process.is_running()`` and treats ``STATUS_ZOMBIE`` as exited, so the status
is never collected and asyncio keeps its reap. A zombie IS an exited process —
its cookie store is committed and its files are closed — and it is the state
the browser is genuinely in for the moment between its exit and asyncio's
``waitpid``, which is why that status is a success and not a timeout. On
Windows there are no zombies and no child watcher (the Proactor loop waits on
the process HANDLE), so the poll answers on ``is_running()`` alone and nothing
about the behaviour there changes. **Linux and macOS have never executed this
module**: it was written and measured on Windows, so the gate's POSIX cells are
its first execution, and this paragraph is what they are checking.

**"THE one home" is about a CLOSE, and two other files still end browser
processes without coming through here — deliberately.** ``spawn_leak`` reaps
what a FAILED spawn left running and ``process_cleanup`` reaps orphans at
startup and shutdown; neither browser was ever asked to leave, so there is no
shutdown in flight to wait for and no cookie commit a kill could truncate. A
grace there would buy a wedged orphan up to 5 s of a startup or a shutdown for
nothing. What this module owns is the one path where a CALLER said "close
this" — and on that path the kill has exactly one home, which is what F-910
was about.

**What the grace costs at SHUTDOWN, measured rather than assumed.**
``app_lifespan`` calls ``close_all``, which closes instances SEQUENTIALLY, so
N wedged browsers add up to N x (``EXIT_GRACE_SECONDS`` + the thread slack) to
a backend's exit. That does NOT collide with uvicorn's 2.0 s
``timeout_graceful_shutdown`` (``logging_setup._GRACEFUL_SHUTDOWN_SECONDS``,
F-809): in uvicorn 0.x's ``Server.shutdown`` that budget wraps
``_wait_tasks_to_complete()`` ALONE (``uvicorn/server.py``:279-282) and
``await self.lifespan.shutdown()`` runs after it, outside the ``wait_for``,
with no deadline (:291-293) — so the lifespan shutdown was never bounded by
it, before this change or after. The closes are left serial on purpose:
parallelising them would run N ``_blocking_teardown`` calls at once, and each
mutates the one ``ProcessCleanup.browser_processes`` dict that
``_save_tracked_pids`` iterates, so the trade would be a faster exit against a
corrupted ``browser_pids.json`` — a worse failure than a slow one. A browser
that is merely SLOW costs nothing extra: the grace spends the time
``kill_browser_process``'s own ``terminate`` + ``wait(3)`` would have spent
anyway, which is why the measured close is indistinguishable either way
(0.128-0.319 s plain, 0.129-0.165 s waited).

A leaf: ``psutil`` + stdlib. Every input is a primitive or an
``asyncio.subprocess.Process``; it knows nothing about instances, records or
profiles. **Neither wait here raises** — an unreadable process is a process we
did not wait for, which resolves onto the behaviour that shipped before F-910 —
and ``report`` does not either, because a close must not fail over a log line.
``terminate`` deliberately keeps no such blanket: each rung catches what the
KILL can do and nothing else, so a bug in this module is visible rather than
silently making a browser survive its own teardown. That is not a free choice,
it is the one the first draft got wrong in the other direction: it imported the
debug_logger MODULE rather than the singleton, so every line it wrote was an
``AttributeError`` — swallowed inside ``report``, where it meant the close
diagnostics did not exist at all and nothing said so. Both halves are pinned
now, in ``tests/test_close_instance_offload.py`` and
``tests/test_close_waits_for_chrome.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psutil

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:  # the ladder's rungs are callables, typed but never imported
    from collections.abc import Callable

# How long a browser we have asked to close may take to go on its own before we
# terminate it. 5.0 s is ~30x the slowest unaided exit measured here (0.169 s),
# which is headroom for a loaded CI runner rather than a guess: the two Windows
# gate failures this fixes happened on a runner where the whole node ran 3x
# slower than locally. It is a CEILING and not a wait — a browser that has
# already gone costs one psutil read — and what it costs in the bad case is
# stated rather than hidden: a WEDGED Chrome makes one `close_instance` up to
# 5 s slower, and is then killed exactly as it was before F-910.
EXIT_GRACE_SECONDS = 5.0

# What the OUTER bound adds to the inner one. A worker thread cannot be
# cancelled, so `wait_for_exit_async` needs its own deadline to be sure a close
# reaches the kill path; one second is the hand-off, not a second grace.
_THREAD_SLACK_SECONDS = 1.0

# A child process of the browser (renderer, gpu-process, utility, crashpad).
# Chrome spells it `--type=renderer`; the bare flag never appears alone.
_CHILD_MARKER = "--type="

# How often the wait asks whether the browser has gone. It POLLS rather than
# blocking inside `waitpid`, because this wait must not REAP — see the module
# docstring's POSIX paragraph. What the interval costs is the granularity it
# adds to a close: 0.02 s against a measured 0.100-0.169 s exit, i.e. at most
# one extra poll's worth of wall clock on the common path.
_POLL_SECONDS = 0.02


@dataclass(frozen=True)
class Exited:
    """What waiting for one process to leave on its own actually observed."""

    pid: int | None
    exited: bool
    seconds: float
    reason: str


def browser_pid(process: object | None, fallback_pid: int | None) -> int | None:
    """The pid of the BROWSER itself, or None when we cannot say it is one.

    *process* is nodriver's ``Browser._process`` (an ``asyncio`` Process for a
    browser we launched, ``None`` for one we attached to); *fallback_pid* is
    ``Browser._process_pid``, which the F-888 adoption path stamps with the
    browser it entered. None means "do not wait", never "wait on this anyway",
    and it has three causes: no usable pid, a pid that carries ``--type=``
    (a child, below), and a process whose ``returncode`` is already set.

    That last one is not an optimisation, it is the other half of the pid-reuse
    guard: a returncode means asyncio has already COLLECTED this child, so on
    POSIX the pid is free from that instant and may belong to a stranger by the
    time we look. Waiting would then be up to ``EXIT_GRACE_SECONDS`` spent on
    somebody else's process. ``terminate`` reads the same field for the same
    reason, one function below.
    """
    if process is not None and getattr(process, "returncode", None) is not None:
        return None
    pid = getattr(process, "pid", None) or fallback_pid
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        # Cannot be read: it may already be gone, which the wait handles, or it
        # may be a process we have no business inspecting. Either way the pid
        # is the one the kill path would use, so hand it back unjudged.
        return pid
    if any(arg.startswith(_CHILD_MARKER) for arg in cmdline):
        return None
    return pid


def _has_exited(proc: psutil.Process) -> bool:
    """True when THIS process has finished — asked without reaping it.

    Two questions, because neither alone is the answer. ``is_running()`` is
    also the pid-REUSE guard: psutil compares the ``(pid, create_time)`` pair
    through ``Process.__eq__`` (``psutil/__init__.py``, ``is_running`` at
    616-641), so a pid recycled inside the grace reads as gone instead of
    restarting the wait against a stranger — which is why the ``Process``
    object is built ONCE, before the loop, and the identity it captured is
    what every poll is about.

    **What that leaves open is a recycle BEFORE the build, and on the ADOPTED
    path nothing closes it** (S4). The identity this compares against is
    whatever the pid named at construction time, so a pid already recycled by
    then makes every poll agree with the stranger and the wait runs to the
    ceiling — a ≤ ``EXIT_GRACE_SECONDS`` stall, never data loss, since the
    browser we meant is gone either way and Phase 3 still follows. For a
    browser we LAUNCHED the window is shut one function up: ``_process`` has a
    ``returncode``, and a set one declines to wait at all. For one we ADOPTED
    (F-888: ``_process`` is ``None``, so the pid is ``Browser._process_pid``
    alone) there is no ``returncode`` to read and nothing here joins the pid
    to the profile the way ``browser_cmdline.debug_port`` does on the adoption
    side — so this is narrower than round-1's S1 but not empty, and it is
    stated rather than implied. And it answers True for a ZOMBIE (documented,
    and line 635-638 says so), which on POSIX is exactly the state a browser
    we waited for is in: it has exited and is waiting only for its parent's
    ``waitpid``, and that parent is asyncio, not us.
    """
    if not proc.is_running():
        return True
    return proc.status() == psutil.STATUS_ZOMBIE


def wait_for_exit(pid: int | None, timeout: float = EXIT_GRACE_SECONDS) -> Exited:
    """Wait until *pid* exits on its own, or until *timeout*. Never raises.

    It POLLS and never calls ``wait()``, because on POSIX that would REAP a
    child asyncio is already waiting on — the module docstring carries the
    measurement-grade account and the two CPython line numbers.
    """
    started = time.monotonic()
    if pid is None:
        return Exited(None, False, 0.0, "no-browser-pid")
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return Exited(pid, True, time.monotonic() - started, "already-gone")
    except (psutil.Error, OSError) as err:
        return Exited(pid, False, time.monotonic() - started, f"unreadable: {err}")
    return _poll_for_exit(proc, pid, started, started + timeout)


def _poll_for_exit(
    proc: psutil.Process,
    pid: int,
    started: float,
    deadline: float,
) -> Exited:
    """Ask ``_has_exited`` until it says yes or the deadline passes."""
    while True:
        try:
            if _has_exited(proc):
                return Exited(pid, True, time.monotonic() - started, "exited")
        except psutil.NoSuchProcess:
            # Includes `ZombieProcess`, which subclasses it: both mean the
            # browser is done. Reaped between two polls, or a status this
            # platform answers by raising.
            return Exited(pid, True, time.monotonic() - started, "exited")
        except (psutil.Error, OSError) as err:
            return Exited(pid, False, time.monotonic() - started, f"unreadable: {err}")
        if time.monotonic() >= deadline:
            return Exited(pid, False, time.monotonic() - started, "still-running")
        time.sleep(_POLL_SECONDS)


async def wait_for_exit_async(
    pid: int | None,
    timeout: float = EXIT_GRACE_SECONDS,  # noqa: ASYNC109  PERMANENT(a thread, not an await)
) -> Exited:
    """``wait_for_exit`` off the event loop, bounded twice. Never raises.

    The inner wait is a blocking ``psutil`` call, so it runs on a worker
    thread — the loop must stay responsive, and the whole point of this wait is
    that a browser is still using it. The OUTER bound exists because a worker
    thread cannot be cancelled: if it somehow outlives its own timeout, the
    close proceeds to the kill path rather than waiting on it.
    """
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(wait_for_exit, pid, timeout),
            timeout=timeout + _THREAD_SLACK_SECONDS,
        )
    except TimeoutError:
        return Exited(pid, False, time.monotonic() - started, "wait-abandoned")
    except Exception as err:  # noqa: BLE001  PERMANENT(never blocks a close)
        return Exited(pid, False, time.monotonic() - started, f"wait-failed: {err}")


async def settle(
    instance_id: str,
    process: object | None,
    fallback_pid: int | None,
    retries: int,
) -> Exited:
    """The whole of a close's grace: wait, report, and never leave it alive.

    One call rather than four lines at the call site, and in THIS home rather
    than in ``browser_manager``, for the reason this module exists: the wait
    and the kill it gates belong together.

    **The ``CancelledError`` arm is what the grace costs and how it is paid.**
    A close's caller can give up while we are waiting — a client disconnect
    cancels the whole tool call — and Phase 2b sits inside a ``try`` whose
    handler is ``except Exception``, which a ``BaseException`` walks straight
    past. Normally that window is the 0.13 s the browser takes to leave; for a
    WEDGED browser it is the full ceiling, so the grace would newly make a
    cancelled close likelier to leave a live Chrome for the orphan reaper. So
    the kill runs HERE, synchronously, before the cancellation is re-raised:
    the browser this close claimed is dealt with on that path too, and the
    cancellation still propagates, so nothing about the caller's answer
    changes. F-899's move of the state pop to Phase 1 is what makes this the
    only remaining harm worth closing.
    """
    try:
        waited = await wait_for_exit_async(browser_pid(process, fallback_pid))
    except asyncio.CancelledError:
        terminate(instance_id, process, fallback_pid, retries)
        raise
    report(instance_id, waited)
    return waited


def terminate(
    instance_id: str,
    process: object | None,
    fallback_pid: int | None,
    retries: int,
) -> None:
    """End the browser process, escalating terminate -> kill -> SIGTERM.

    Unchanged in behaviour from the ladder that lived in ``browser_manager``;
    it moved here so the wait above and the kill it gates sit in one home. A
    process that has already exited (``returncode`` set) is left alone.
    """
    if process is None or getattr(process, "returncode", None) is not None:
        return
    for attempt in range(retries):
        rungs = (
            ("terminate_process", process.terminate, getattr(process, "pid", None)),
            ("kill_process", process.kill, getattr(process, "pid", None)),
        )
        if fallback_pid:
            rungs = (
                *rungs,
                ("kill_process", lambda: os.kill(fallback_pid, 15), fallback_pid),
            )
        for rung in rungs:
            if _rung(instance_id, rung, attempt, last=attempt == retries - 1):
                return


def _rung(
    instance_id: str,
    rung: tuple[str, Callable[[], object], int | None],
    attempt: int,
    *,
    last: bool,
) -> bool:
    """Try one rung of the escalation. True when the browser is dealt with."""
    action, call, pid = rung
    try:
        call()
    except (PermissionError, ProcessLookupError) as err:
        debug_logger.log_info(
            "process_exit",
            action,
            f"{instance_id}: browser already stopped or no permission to kill: {err}",
        )
        return True
    except Exception as err:  # noqa: BLE001  PERMANENT(drop to the next rung)
        if last:
            debug_logger.log_error("process_exit", action, err)
        return False
    debug_logger.log_info(
        "process_exit",
        action,
        f"{instance_id}: ended browser with pid {pid} successfully on "
        f"attempt {attempt + 1} ({action})",
    )
    return True


def report(instance_id: str, waited: Exited) -> None:
    """Write the one close-diagnostics line a post-mortem reads."""
    with contextlib.suppress(Exception):
        debug_logger.log_info(
            "process_exit",
            "close_exit_wait",
            f"{instance_id}: browser pid {waited.pid} "
            f"exited_unaided={waited.exited} after {waited.seconds:.3f}s "
            f"({waited.reason})",
        )
