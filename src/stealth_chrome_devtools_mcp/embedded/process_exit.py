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
    browser it entered. None means "do not wait", never "wait on this anyway".
    """
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


def wait_for_exit(pid: int | None, timeout: float = EXIT_GRACE_SECONDS) -> Exited:
    """Block until *pid* exits on its own, or until *timeout*. Never raises."""
    started = time.monotonic()
    if pid is None:
        return Exited(None, False, 0.0, "no-browser-pid")
    try:
        psutil.Process(pid).wait(timeout=timeout)
    except psutil.NoSuchProcess:
        return Exited(pid, True, time.monotonic() - started, "already-gone")
    except psutil.TimeoutExpired:
        return Exited(pid, False, time.monotonic() - started, "still-running")
    except (psutil.Error, OSError) as err:
        return Exited(pid, False, time.monotonic() - started, f"unreadable: {err}")
    return Exited(pid, True, time.monotonic() - started, "exited")


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
