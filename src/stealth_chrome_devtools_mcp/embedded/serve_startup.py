"""THE one home for "startup work that must not delay the backend's first
serve" (F-856).

FastMCP runs ``app_lifespan`` on the first MCP session, not at process start,
so anything that lifespan does synchronously is time the very first
``initialize`` spends unanswered. ``process_cleanup.activate()`` did its orphan
reap there: killing the browsers a dead backend left behind, removing their
profiles, and paying a 5s force-kill wait for each stubborn one. On 2026-09-02
that cost ~90 seconds, and the backend that had been cold-started to REPLACE a
condemned one answered nothing for ~114 — while the proxy healing above it held
a 45s readiness budget. The heal could not have succeeded at any level of
patience, because the thing it was waiting for was not listening yet.

**The fix is an ordering, not a speed-up.** Bind, serve, and reap concurrently.
Nothing about the reap needs to precede the first tool call: ownership, not
timing, is what makes it safe to kill a browser (F-808 —
``browser_pid_registry.is_reapable`` spares every entry whose owner backend is
alive, and this process is alive, so a browser spawned during the reap is never
a candidate), the registry write that follows is a read-merge-write that drops
the reaped ids BY NAME, and the temp-profile sweep skips anything a live
browser holds or anything younger than the orphan age. What must stay
synchronous is the handler installation that precedes it — a SIGTERM handler
armed after the first browser exists is a handler that was not there when it
counted — and it does.

**Best-effort by contract.** A job that raises is logged and dropped: startup
work that fails must never take down the backend it exists to make usable, and
every job routed here must be idempotent AND safe to run beside itself — under
runpy the lifespan's guard is per module copy, so ``activate()`` can be reached
twice, which used to mean two sequential reaps and now means two concurrent
ones. The reap qualifies (an already-dead pid, an already-removed directory and
an already-dropped record are each a no-op), so this stays a contract on the
job rather than a dedupe here. The worker is a daemon for the same family of
reasons — interpreter exit must not block on a half-finished sweep.

**Not a general background-task runner.** ``clone_storage.spawn_background_sweep``
is deliberately NOT folded in: it is an asyncio task with an in-flight dedupe
and a trigger-time root capture, called from four sites of which only one is
startup. Different lifetime, different dedupe, different callers — folding it
here would be a widening, not a consolidation.

A leaf: stdlib plus the backend's own logger. It never imports ``process_cleanup``
(the caller passes its own job in), never ``singleton``, never ``server``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# One stream: this only ever runs in the backend process, so it writes to the
# log ``configure_logging("backend")`` already owns.
_logger = logging.getLogger("stealth.backend")


def after_serving(job: Callable[[], object], *, name: str) -> threading.Thread:
    """Run ``job`` off the caller's thread and return at once.

    The returned thread is handed back so a test can join it deterministically;
    production callers ignore it, which is the point — nothing waits on this.
    """
    thread = threading.Thread(
        target=_run, args=(job, name), name=f"serve-startup-{name}", daemon=True
    )
    thread.start()
    return thread


def _run(job: Callable[[], object], name: str) -> None:
    """Run one startup job, timed, and never let it escape."""
    started = time.monotonic()
    try:
        job()
    except Exception:  # noqa: BLE001  PERMANENT(startup work is best-effort)
        _logger.warning("startup job %r failed after serving began", name)
        _logger.debug("startup job detail", exc_info=True)
        return
    _logger.info(
        "startup job %r finished %.1fs after serving began",
        name,
        time.monotonic() - started,
    )
