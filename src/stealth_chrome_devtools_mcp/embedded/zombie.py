"""THE one home for "has this process exited and is it only waiting to be reaped".

A zombie is a POSIX state: a child that has exited and whose parent has not
collected its status yet. Four places ask it (``process_exit._has_exited`` and
``browser_is_alive``, ``devtools_port._running``, ``browser_cmdline``'s gone-pid
witness) and they used to ask it the same way everywhere, ``process.status() ==
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

import sys

import psutil

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
