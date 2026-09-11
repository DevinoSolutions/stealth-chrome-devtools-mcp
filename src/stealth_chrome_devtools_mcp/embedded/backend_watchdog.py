"""THE one home for the stdio proxy's mid-session liveness watchdog — the SLOW
witness that a backend has stopped being usable.

Extracted from ``singleton`` by F-856, which needed room in a file already at
its LOC budget. The gate forced the question; the answer was already right.
This is a self-contained algorithm (strikes, then a confirmation phase) that
takes every collaborator through an argument, so it belongs beside the module
that wires it, not inside it. It is a leaf: the two probes arrive as
parameters, so nothing here imports ``singleton``, and the dead-vs-busy policy
stays single-homed in ``singleton._same_identity_backend_ready``.

Its history, unchanged by the move:

* **F-501** — the fast check used to be a bare socket connect, which a wedged
  backend (dispatch loop dead, socket still open) always passes, so the sole
  auto-recovery watchdog never armed against the exact failure it exists for.
  It is now an app-level ``initialize`` probe, driven off-thread by the caller
  (a blocking httpx call run inline would freeze the stdio pump for up to
  ``LIVENESS_PROBE_TIMEOUT`` every ``interval``).
* **F-820** — those strikes no longer condemn on their own. Under fleet load a
  healthy shared backend answers slower than 2s for 20-40s at a time, and whole
  waves of proxies tore down in the same second while it went on serving. They
  now only open a CONFIRMATION phase whose verdict comes from the SAME gate the
  cold-start lock trusts, so there is no second busy-vs-dead policy here.
* **F-856** — that gate now spends its patience in fairly scheduled seconds
  (``scheduling_lag``), so a confirmation reached while this process itself was
  not being scheduled is no longer mistaken for evidence about the backend. The
  strikes are untouched: they never condemned on their own, so their timing was
  never the thing to make elastic.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# One stream: the watchdog is part of the proxy's story, so it writes to the
# proxy log ``configure_logging("proxy")`` already owns (same logger name as
# ``singleton``, from which this moved — a log line that changed streams is a
# log line nobody finds).
_logger = logging.getLogger("stealth.proxy")


async def watch_liveness(  # noqa: PLR0913  PERMANENT(function interface)
    port: int,
    *,
    is_healthy: Callable[[], object],
    confirm_probe: Callable[[], object],
    interval: float = 2.0,
    failures_before_teardown: int = 3,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Return once the backend on ``port`` is CONFIRMED unusable.

    The caller tears the proxy's backend leg down then, converting a backend
    death mid-session into a heal (``proxy_selfheal``) instead of an unbounded
    hang on requests a dead backend can never answer. Armed only after the
    backend was confirmed up; one healthy check resets the failure run, so a
    transient blip never condemns a live backend.

    ``is_healthy`` is the fast per-tick check and ``confirm_probe`` the patient
    verdict; both may be sync or awaitable, and both are supplied by the caller
    so this module never has to know which probes are ours. ``sleep`` is the
    tick, injectable for tests.
    """
    import anyio

    async def _ask(probe: Callable[[], object]) -> object:
        res = probe()
        return await res if inspect.isawaitable(res) else res

    nap = sleep or anyio.sleep
    consecutive = 0
    while True:
        await nap(interval)
        if await _ask(is_healthy):
            consecutive = 0
            continue
        consecutive += 1
        _logger.warning(
            "probe failed %d/%d on port %d", consecutive, failures_before_teardown, port
        )
        if consecutive < failures_before_teardown:
            continue
        if not await _ask(confirm_probe):
            _logger.warning("backend on port %d confirmed unusable", port)
            return
        _logger.info("backend on port %d was busy, not dead", port)
        consecutive = 0
