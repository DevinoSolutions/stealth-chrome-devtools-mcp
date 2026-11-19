"""THE one home for "is this pid still there, and is it the same process".

Three questions, three names, and a caller picks one on purpose (T-19642: there
were eleven private readers, and the fixes F-998 and F-1008 each reached only some
of them):

* :func:`alive` ``(pid, *, unknown)``: is *pid* a RUNNING process. Not listed is
  dead (after the kernel's own word, F-1008, asked only of an
  ``unknown=True`` caller: ``kill(tid, 0)`` cannot tell a thread id from a
  pid), a zombie is dead, a Linux process
  whose address space is already gone is dead (F-998), and an empty argv alone
  is NEVER dead: a live Chrome reads one while its ``sh`` launcher ``exec``s.
  ``unknown`` is the answer for a pid that cannot be asked about, and it has no
  default because the two directions are both right somewhere: ``True`` where
  "dead" would free a profile or kill a process (``profile_lock``), ``False``
  where "alive" would keep a dead instance (``process_exit``, ``devtools_port``).
* :func:`listed` ``(pid)``: is the NUMBER in the process table. A zombie and a
  process mid-teardown are listed, which is what a deferred delete, a reap and
  the eviction guard want: they wait one more pass rather than act on a
  process that has not finished leaving.
* :func:`same_process` ``(pid, started, *, tolerance, unknown)``: is *pid* still
  the process that started at *started*. A pid alone is recycled, so anything
  that acts on a REMEMBERED pid asks this, not :func:`alive`. :func:`create_time`
  is the read underneath it, for a caller that has to remember the start, and
  :func:`running` is the same question for a ``psutil.Process`` the caller
  already holds (whose identity psutil keeps itself). :func:`identified`
  returns the very ``Process`` the identity was read from, for a caller
  about to kill it.

A pid that names no process (zero, negative, ``None``, a bool) is dead to
:func:`alive` and unknown to nothing: ``psutil.Process(None)`` is the CALLING
process and ``os.kill(-1, 0)`` signals everyone, so neither is ever asked.

A leaf: ``psutil`` and the debug logger. It states facts and decides nothing.

**The zombie.** A zombie is a POSIX state: a child that has exited and whose
parent has not collected its status yet. Four places ask it (``process_exit``'s
``_poll_for_exit`` and ``browser_is_alive``, ``devtools_port``, ``profile_lock``)
and they used to ask it the same way everywhere, ``process.status() ==
psutil.STATUS_ZOMBIE``. On Windows that question has one answer, no, and asking it
was the most expensive call in the backend.

**Why asking it on Windows hangs the backend.** psutil's Windows
``Process.status()`` can only say ``running`` or ``stopped`` (``psutil/_pswindows.py``,
``status``), and to say even that it calls ``cext.proc_is_suspended``, which takes
one snapshot of EVERY process and thread on the machine
(``NtQuerySystemInformation(SystemProcessInformation)``) and walks it WITHOUT
releasing the GIL. Measured 2026-11-07 on the fleet PC at ~1,550 processes, psutil
7.2.2: about 46 ms per call, all of it with the GIL held. An asyncio loop that sleeps
10 ms beside ONE thread asking it in a loop woke p50 38 ms / p99 619 ms / max 810 ms
late (5.7 / 16 / 20 ms beside an idle thread). A worker thread is no shelter: what
starves the loop is the GIL, not the thread the call runs on.

``process_exit._poll_for_exit`` asked it every 20 ms for up to 5 s of every close,
and every heal, failed spawn and kill ladder closes something. On a backend shared
by ~25 sessions those closes overlap, the loop that serves every session gets the
GIL a few milliseconds in every hundred, and each await costs seconds. That is
the shape of the 2026-11-07 hangs: probe steps that take milliseconds took
2.5-3.0 s each, the heartbeat still stamped (so the proxies' watchdog excused every
failed probe as its own starvation), and no tool call came back for half an hour.
Production ``close_instance`` ran p50 6.9 s that week, so most closes polled the
whole grace. Against an isolated backend with suspended Chromes being closed, not
asking cut ``/_stealth/health``'s p99 from 3.0 s to 1.7 s and its max from 5.9 s to
2.6 s. At the ~3,400 processes the fleet PC reaches, every call roughly doubles.

So the question is asked only where its answer can be yes. Everything else about
the four callers stays theirs: ``is_running()`` is still each one's pid-reuse
guard, and on POSIX the status is read exactly as before.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TypeGuard

import psutil

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

#: Whether this platform has zombie processes at all. Windows has none: a process
#: that has exited is gone, and its exit code lives on in whatever HANDLE is open
#: on it. A module attribute so a test can ask the other platform's question.
ZOMBIES_EXIST = sys.platform != "win32"


def is_zombie(process: psutil.Process) -> bool:
    """True when *process* has exited and waits only for its parent to reap it.

    Free where :data:`ZOMBIES_EXIST` is False (never a psutil call); elsewhere one
    ``status()`` read, raising what it raises (``NoSuchProcess`` for a pid already
    reaped, which every caller already reads as "gone").
    """
    return ZOMBIES_EXIST and process.status() == psutil.STATUS_ZOMBIE


def running(process: psutil.Process) -> bool:
    """Whether a ``Process`` the caller HOLDS is still the process it was built
    for and has not exited.

    ``is_running()`` is the pid-REUSE guard: psutil compares the ``(pid,
    create_time)`` pair through ``Process.__eq__``, so a pid recycled after the
    object was built reads as not running, and a zombie is the one state it
    still calls running (so :func:`is_zombie` is asked as well). Raises what
    psutil raises; a caller that waits (``process_exit``) reads those itself.
    """
    return process.is_running() and not is_zombie(process)


def listed(pid: int) -> bool:
    """Whether *pid* is in the process table at all.

    A zombie is listed, and so is a process that is tearing down: that is the
    direction a deferred profile delete, a reap and the eviction guard are
    written for (see the module docstring), and the reason this is not
    :func:`alive`. Raises what ``psutil.pid_exists`` raises.
    """
    return psutil.pid_exists(pid)


def _names_a_process(pid: object) -> TypeGuard[int]:
    """Whether *pid* could name a process: a positive int, never a bool.

    Not asking anything else of a pid that fails this is the point:
    ``Process(None)`` reads the calling process, ``os.kill(-1, 0)`` is answered
    for EVERY process the user may signal, and ``True`` is the pid 1.
    """
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


# ---------------------------------------------------------------------------
# alive
# ---------------------------------------------------------------------------

#: Whether an EMPTY command line can be told from a live browser, which is a
#: Linux-only measurement (F-998): see :func:`_gone`. False elsewhere, and there
#: only a zombie counts as gone.
_EXE_WITNESS = sys.platform.startswith("linux")

#: Whether ``os.kill(pid, 0)`` is a QUESTION: on Windows any signal but the
#: console ones terminates the process.
_SIGNAL_ASKS = os.name != "nt"
#: This module's own alias: patching ``os.kill`` would swap it process-wide.
_signal = os.kill


def _exe_is_gone(pid: int) -> bool:
    """Whether ``/proc/<pid>/exe`` is definitely gone: ``FileNotFoundError`` (or
    its ``ESRCH`` twin) and nothing else. A readable link, ``PermissionError`` or
    any other error is "not shown gone", which the caller reads as HELD."""
    try:
        Path(f"{getattr(psutil, 'PROCFS_PATH', '/proc')}/{pid}/exe").readlink()
    except (FileNotFoundError, ProcessLookupError):
        return True
    except OSError:
        return False
    return False


def _kernel_has(pid: int) -> bool:
    """Whether the kernel still has *pid* though ``psutil`` did not list it.

    ``psutil`` answers from ``/proc``, and ``/proc`` mounted ``hidepid=2`` lists
    only the caller's own processes, so another user's live browser read as
    gone and its lock as orphaned (F-1008). ``kill(pid, 0)`` delivers nothing
    and asks the kernel instead: ``EPERM`` is a process that exists and is not
    ours. POSIX only — on Windows ``os.kill`` terminates, and Chrome writes no
    lock there to ask about.
    """
    if not _SIGNAL_ASKS:
        return False
    try:
        _signal(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone(pid: int, *, unreadable: bool) -> bool:
    """Whether a LISTED *pid* can no longer be a running process (F-998). A
    process that has been killed spends a moment in one of two states, and
    ``psutil.pid_exists`` is True for both:

    * a ZOMBIE, which has exited and waits for its parent's ``wait()``. Its
      ``status`` says so, on every platform that has them;
    * TEARING DOWN, which has already released its address space — the first
      thing a dying process does — and so reads an EMPTY command line, with no
      error, while the kernel finishes with its threads and files. **Linux
      only, and only with a second fact:** an empty command line is NOT enough,
      because a LIVE browser has one too. ``/usr/bin/chromium`` and Google's
      launcher are ``#!/bin/sh`` scripts that ``exec`` the real binary under the
      same pid, and across that ``execve`` the pid reads an empty argv while
      running, before ``SingletonLock`` exists (measured on Chromium 155, 40
      launches under CPU contention: the browser pid read empty while alive in
      11 of them, and the production ``profile_hold`` showed the profile free
      in 2 of 11,194 live polls with the first version of this function,
      against 0 of 11,033 before F-998). What separates the two is the
      executable link: ``/proc/<pid>/exe`` stayed readable (and ``maps``
      non-empty) on 81 of 81 live mid-exec samples, and was unreadable on every
      sample of a dying browser, because the dying process has no address space
      left to name an executable from. So an empty command line is gone only
      when ``/proc/<pid>/exe`` is also ``FileNotFoundError``;
      ``PermissionError``, a readable link or any other outcome is HELD.

    Off Linux only the zombie half applies. The exec-window measurement is
    Linux-only (Chrome on macOS is a direct binary, which was not measured, and
    Windows has no zombies and writes no ``SingletonLock``, so nothing licenses
    reading an empty argv as dead there).

    Known limit, not fixable here: on Linux the leader of a still-running
    multithreaded process reads ``Z`` once its own thread has exited (a main
    thread that calls ``pthread_exit``), which this reads as gone. Chrome does
    not do that. Holding a zombie that has more than one thread was tried and
    fails: a SIGKILLed Chrome keeps ``num_threads`` above 1 while its other
    threads finish, and 12 of 20 runs of the fleet journey walked to
    ``fleet-2`` again.

    A pid that vanished between the listing and either read is gone. One that
    cannot be opened or whose status cannot be read (``AccessDenied``) is
    *unreadable*, which is the caller's call. The command-line read is a second
    witness that can only ever convict: failing to read it is no evidence, so it
    is never *unreadable*, only "not shown gone".
    """
    try:
        process = psutil.Process(pid)
        if is_zombie(process):
            return True
    except psutil.NoSuchProcess:  # incl. ZombieProcess: reaped or reaping
        return True
    except (psutil.Error, OSError, ValueError):
        return unreadable
    try:
        return _EXE_WITNESS and not process.cmdline() and _exe_is_gone(pid)
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError, ValueError):
        return False


def alive(pid: object, *, unknown: bool) -> bool:
    """Whether *pid* is a RUNNING process. Never raises.

    *unknown* is what a pid that cannot be asked about counts as, and it is
    required because the right direction depends on what the answer is spent on:
    ``True`` is "not shown dead", for a caller whose wrong "dead" opens one
    profile in two browsers or ends what it should not (``profile_lock``);
    ``False`` is "not shown running", for one whose wrong "alive" keeps a dead
    instance (``process_exit``, ``devtools_port``). Both are written down at the
    call, which is the whole of F-998's and F-1008's lesson: a reader that picks
    its direction by accident drifts.

    A pid the process table does not list is asked of the kernel (F-1008,
    :func:`_kernel_has`) ONLY by a caller that passed ``unknown=True``. The probe
    cannot tell a pid from a thread id (``kill(tid, 0)`` succeeds; psutil's own
    Linux ``pid_exists`` reads ``/proc/<pid>/status`` for ``Tgid`` precisely to
    refuse one, and under ``hidepid=2`` that read is the very thing denied), so
    its word is only good for a caller whose wrong "alive" costs one more look
    (a held profile), never for one whose wrong "alive" keeps a dead instance
    (``unknown=False``: it owns a browser its own user can always see). A listed
    pid is dead when it is a zombie or is tearing down (:func:`_gone`).
    Anything that raises, ``OverflowError`` for a pid too
    large for a C long included, is *unknown*, and is logged: nothing else in
    here is.
    """
    if not _names_a_process(pid):
        return False
    try:
        if not listed(pid):
            return unknown and _kernel_has(pid)
    except (psutil.Error, OSError, OverflowError) as error:
        debug_logger.log_warning(
            "pid_alive",
            "alive",
            f"Could not tell whether pid {pid} is running: {error}",
        )
        return unknown
    return not _gone(pid, unreadable=not unknown)


# ---------------------------------------------------------------------------
# the same process
# ---------------------------------------------------------------------------


def create_time(pid: object) -> float | None:
    """When *pid* started, or None when that cannot be said: no such process,
    one this account may not open, a failed system call, or a pid that names no
    process. The second half of a pid's identity (:func:`same_process`) and the
    one read of it every caller that REMEMBERS a pid takes."""
    if not _names_a_process(pid):
        return None
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def _identify(
    pid: object, started: float | None, tolerance: float, unknown: bool
) -> psutil.Process | bool:
    """The ONE identity check: the ``Process`` it was made on when *pid* is shown
    to be the process that started at *started*, else the bool answer."""
    if not _names_a_process(pid):
        return False
    try:
        process = psutil.Process(pid)
        actual = process.create_time()
    except psutil.NoSuchProcess:  # incl. ZombieProcess
        return False
    except (psutil.Error, OSError):
        return unknown
    if started is None or abs(actual - started) <= tolerance:
        return process
    return False


def identified(
    pid: object, started: float | None, *, tolerance: float = 0.0
) -> psutil.Process | None:
    """The ``Process`` for *pid*, only when it is shown to be the one that started
    at *started*; None otherwise, an unaskable pid included. For a caller about to
    ACT on it: act on THIS object, never on a second ``Process(pid)`` built after
    the check, which is the recycle the check exists to refuse. Never raises."""
    found = _identify(pid, started, tolerance, False)
    return None if isinstance(found, bool) else found


def same_process(
    pid: object,
    started: float | None,
    *,
    tolerance: float = 0.0,
    unknown: bool,
) -> bool:
    """Whether *pid* is still the process that started at *started*. Never raises
    for a pid (any object); *started* must be a number or None.

    The pair, never the pid: a number is recycled, and a caller acting on a
    remembered one would end (or spare) a stranger. A process that is gone, a
    zombie that cannot be opened and a pid that names none are not the same
    process whatever *unknown* says; *unknown* is only the answer for one that
    CANNOT BE ASKED (``AccessDenied``, a failed system call): ``False`` for a
    caller about to act on the process, ``True`` where a false "gone" is the
    expensive error (``client_presence``).

    *tolerance* is how far apart two readings of one start time may be and still
    be one process, inclusive. Zero is exact equality and is right wherever both
    readings came out of psutil in one run; a value read back from a file a
    previous version wrote is why a caller passes more. ``started=None`` is a
    record that carries no start time: only that the pid is there and readable
    is asked (best effort, as it always was).
    """
    return _identify(pid, started, tolerance, unknown) is not False
