"""RUF006 fix: BrowserManager background close tasks must be retained (so the
event loop cannot garbage-collect them mid-close) and their exceptions surfaced,
instead of fire-and-forget.

Before the fix, two ``asyncio.create_task(proxy_forwarder.close())`` sites
(``_discard_instance_unlocked`` and the forced-close path) dropped the task
reference, risking mid-close GC and silently swallowing any close() exception.
"""

import asyncio

from browser_manager import BrowserManager
from debug_logger import debug_logger


async def test_run_in_background_retains_task_and_surfaces_exception(monkeypatch):
    manager = BrowserManager()
    logged = []
    monkeypatch.setattr(
        debug_logger, "log_error", lambda comp, op, exc: logged.append((op, exc))
    )

    async def boom():
        raise RuntimeError("close failed")

    task = manager._run_in_background(boom(), "unit_probe")
    # Retained while pending -> not eligible for garbage collection.
    assert task in manager._background_tasks

    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # let the done-callback run

    # Discarded once complete, and the exception was surfaced (not swallowed).
    assert task not in manager._background_tasks
    assert any(
        op == "unit_probe" and isinstance(exc, RuntimeError) for op, exc in logged
    )
