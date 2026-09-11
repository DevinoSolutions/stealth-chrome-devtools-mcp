"""THE one home for "this spawn failed before nodriver handed a ``Browser`` back
— what did it leave running, and reap it" (F-860).

nodriver's ``Browser.start()`` spawns Chrome, polls ``/json/version`` and, on no
answer, raises without killing what it spawned; the websocket handshake that
follows can time out the same way. Either raise happens INSIDE
``BrowserManager._launch_browser``, so the orchestrator holds no ``Browser`` to
stop, and ``process_cleanup.kill_browser_process`` returns early because the
instance is tracked only in ``_apply_post_launch`` — after a successful launch.
The Chrome that nodriver started therefore outlived the failure, untracked and
invisible to ``list_instances``; on ``master`` it also made every later caller
clone, with nothing to explain why, until a backend restart reaped it.

**What identifies the leaked process is the attempt's profile directory**, not
a pid: the only thing the orchestrator still knows about a launch that raised
is the ``--user-data-dir`` it was launched on. ``process_cleanup`` already owns
the cmdline scan for that (``_get_browser_pids_for_profile``) and the escalating
kill (``_kill_process_by_pid``); this module composes them with the ONE extra
fact that makes the reap safe — **only a process that started at or after the
attempt began is ours to kill**. An explicit ``user_data_dir`` may name a
profile a REAL Chrome already holds (its singleton is then exactly why our
launch failed); that process predates the attempt and must survive. Passing
the ``ProcessCleanup`` in keeps this a leaf.

**It never raises**: it runs inside a failure handler, and a cleanup failure
that replaces the launch failure the caller needs to see is strictly worse
than a leak that is logged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psutil

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

# psutil's create_time and time.time() read the same wall clock, but the kernel
# rounds process start times (10 ms on Linux, ~16 ms on Windows). The same 1 s
# tolerance ``_fallback_pid_identity_ok`` grants a recycled pid, in the
# direction that never spares a process this attempt started.
_CLOCK_TOLERANCE_SECONDS = 1.0


def reap_launched_browsers(
    cleanup: ProcessCleanup,
    user_data_dir: str | None,
    since: float,
    instance_id: str,
) -> list[int]:
    """Kill every browser process on *user_data_dir* that started at or after
    *since* (a ``time.time()`` taken just before the launch), and return the
    pids that were reaped. ``None`` for the directory means nothing can be
    matched, so the process table is not walked at all.
    """
    if not user_data_dir:
        return []
    try:
        candidates = cleanup._get_browser_pids_for_profile(user_data_dir)
    except Exception as error:  # noqa: BLE001  PERMANENT(a reap inside a failure handler must not raise)
        debug_logger.log_warning(
            "spawn_leak",
            "reap",
            f"Could not scan for browsers left by failed spawn {instance_id}: {error}",
        )
        return []
    reaped: list[int] = []
    for pid in sorted(candidates):
        if not _started_after(pid, since - _CLOCK_TOLERANCE_SECONDS):
            continue
        debug_logger.log_warning(
            "spawn_leak",
            "reap",
            f"Failed spawn {instance_id} left browser pid {pid} running on "
            f"{user_data_dir}; killing it (F-860)",
        )
        try:
            if cleanup._kill_process_by_pid(pid, instance_id):
                reaped.append(pid)
        except Exception as error:  # noqa: BLE001  PERMANENT(a reap inside a failure handler must not raise)
            debug_logger.log_warning(
                "spawn_leak",
                "reap",
                f"Could not kill browser pid {pid} left by failed spawn "
                f"{instance_id}: {error}",
            )
    return reaped


def _started_after(pid: int, threshold: float) -> bool:
    """Whether *pid* started at or after *threshold*; a process that is gone
    or unreadable is not ours to judge, so it is never killed."""
    try:
        return psutil.Process(pid).create_time() >= threshold
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    except Exception as error:  # noqa: BLE001  PERMANENT(a reap inside a failure handler must not raise)
        debug_logger.log_warning(
            "spawn_leak",
            "reap",
            f"Could not read start time of browser pid {pid}: {error}",
        )
        return False
