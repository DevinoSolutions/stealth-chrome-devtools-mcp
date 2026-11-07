"""THE one home for "the event loop stopped turning: leave the evidence behind".

On 2026-11-07 the shared backend stopped answering three times. Each time the
process stayed alive, its log simply went quiet, and killing it was the only fix
and the only diagnosis: nothing recorded WHERE the loop was. A frozen asyncio loop
cannot report itself, and a Python thread watching it cannot either when the GIL is
what it is waiting for (``zombie`` names the call that held it for seconds at a
time). ``faulthandler``'s watchdog is a C thread that needs neither: armed with a
deadline, it writes every thread's stack when the deadline passes unless it is
re-armed first.

So the heartbeat re-arms it. ``backend_liveness.beat`` calls :func:`arm` on every
beat, ON the event loop, before it hands the stamp to a thread; a loop that misses
:data:`STALL_DUMP_SECONDS` of beats has its stacks written to the backend's own
fault log (``backend-<pid>-fault.log``, the file ``faulthandler.enable`` already
writes a hard crash to), and the next :func:`arm` that runs says so in the
structured log with the length of the stall. Measured on an isolated backend under
load, the first such dump named the culprit of every >1 s stall: a Chrome launch's
``CreateProcess``, which asyncio's Proactor loop runs on the loop thread.

The dump is bounded, because a loop that crawls instead of stopping would
otherwise write one per beat for as long as it crawls: :data:`MAX_DUMPS` per
process, after which arming stops and says so once. Never raises: evidence must
not cost the backend its heartbeat.

A leaf: stdlib only, so ``logging_setup`` can hand it the fault log at boot.
"""

from __future__ import annotations

import faulthandler
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO

#: How long the loop may go without a beat before its stacks are written. Three
#: missed beats of ``backend_liveness.HEARTBEAT_INTERVAL_SECONDS`` (3 s) plus one:
#: past anything a healthy loop does, and well inside the 30 s after which the
#: proxies stop believing the heartbeat at all.
STALL_DUMP_SECONDS = 10.0
#: Dumps per process. Each is every thread's stack, a few KB, into a file that is
#: never rotated; the first few stalls are the evidence, the rest repeat it.
MAX_DUMPS = 10

_logger = logging.getLogger("stealth.backend")
_fault_log: TextIO | None = None
_armed_at: float | None = None
_dumps = 0


def fault_log_path(log_path: Path) -> Path:
    """THE name of the fault log beside ``backend-<pid>.log``."""
    return log_path.with_name(f"{log_path.stem}-fault.log")


def open_fault_log(log_path: Path) -> TextIO:
    """Open (append) the fault log beside *log_path* and keep it for :func:`arm`.
    Raises ``OSError`` as ``open`` does; the caller decides what that costs."""
    global _fault_log  # noqa: PLW0603  PERMANENT(one fault log per process, opened at boot)
    _fault_log = fault_log_path(log_path).open("a", encoding="utf-8")
    return _fault_log


def arm(*, now: float | None = None) -> bool:
    """(Re-)arm the stall dump from the event loop; True iff it is armed now.

    A no-op without a fault log (every process but a booted backend: the proxy,
    the CLI, the test suite). When the previous arming is older than
    :data:`STALL_DUMP_SECONDS` the dump has fired, so the stall is counted and
    logged before arming again.
    """
    global _armed_at, _dumps  # noqa: PLW0603  PERMANENT(process-wide watchdog state)
    if _fault_log is None or _dumps >= MAX_DUMPS:
        return False
    now = time.monotonic() if now is None else now
    try:
        if _armed_at is not None and now - _armed_at > STALL_DUMP_SECONDS:
            _dumps += 1
            _logger.warning(
                "the event loop did not turn for %.1fs; every thread's stack is in "
                "%s (stall dump %d of at most %d)",
                now - _armed_at,
                getattr(_fault_log, "name", "the fault log"),
                _dumps,
                MAX_DUMPS,
            )
            if _dumps >= MAX_DUMPS:
                faulthandler.cancel_dump_traceback_later()
                _logger.warning("stall dumps stop here: %d written", _dumps)
                return False
        faulthandler.dump_traceback_later(STALL_DUMP_SECONDS, file=_fault_log)
    except (OSError, ValueError, RuntimeError):
        _logger.debug("could not arm the stall dump", exc_info=True)
        return False
    _armed_at = now
    return True
