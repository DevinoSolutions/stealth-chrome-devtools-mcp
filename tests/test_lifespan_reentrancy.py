"""RELEASE-FIX-B C1 (B1) — app_lifespan must be session-reentrant.

FastMCP serves streamable HTTP by running the low-level MCP ``Server.run()`` —
and thus the server ``lifespan`` — **once per MCP session**, not once per
process. ``app_lifespan`` was written for once-per-process semantics, so over
HTTP every probe session's lifespan EXIT ran the full destructive teardown
(``browser_manager.close_all`` / ``process_cleanup._cleanup_all_tracked`` /
``in_memory_storage.clear_all``) and every ENTRY re-armed orphan recovery
(``process_cleanup.activate``) — killing live browsers within ~2s (finding B1).

These hermetic tests drive ``app_lifespan`` directly (no Chrome, no backend) and
pin the fix: startup runs once per process, destructive teardown is bound to
process end (stdio standalone), and http-mode session exit is a no-op. The
process-end path is already covered by ``process_cleanup.activate``'s
atexit/signal reaping, so http-mode has nothing to clean at session exit.
"""

from __future__ import annotations

from typing import Any

import pytest

from stealth_chrome_devtools_mcp.embedded import server


class _SpyBrowserManager:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.close_all_calls = 0

    async def start_idle_reaper(self) -> None:
        self.start_calls += 1

    async def stop_idle_reaper(self) -> None:
        self.stop_calls += 1

    async def close_all(self) -> None:
        self.close_all_calls += 1


class _SpyProcessCleanup:
    def __init__(self) -> None:
        self.activate_calls = 0
        self.cleanup_all_calls = 0

    def activate(self) -> None:
        self.activate_calls += 1

    def _cleanup_all_tracked(self) -> None:
        self.cleanup_all_calls += 1


class _SpyStorage:
    def __init__(self, instances: list[Any] | None = None) -> None:
        self.clear_all_calls = 0
        self._instances = instances if instances is not None else ["seeded"]

    def list_instances(self) -> dict[str, Any]:
        return {"instances": self._instances}

    def clear_all(self) -> None:
        self.clear_all_calls += 1


class _SpyCloneStorage:
    def __init__(self) -> None:
        self.sweep_calls = 0

    def spawn_background_sweep(self, reason: str) -> None:
        self.sweep_calls += 1


@pytest.fixture()
def spies(monkeypatch):
    """Swap the four teardown/startup singletons for spies and reset the
    once-per-process startup guard so each test drives a fresh lifespan."""
    bm = _SpyBrowserManager()
    pc = _SpyProcessCleanup()
    ims = _SpyStorage()
    cs = _SpyCloneStorage()
    monkeypatch.setattr(server, "browser_manager", bm)
    monkeypatch.setattr(server, "process_cleanup", pc)
    monkeypatch.setattr(server, "in_memory_storage", ims)
    monkeypatch.setattr(server, "clone_storage", cs)
    # raising=False so the RED run (before the guard exists) fails on the
    # behavioral assertions below, not on a missing-attribute setup error.
    monkeypatch.setattr(server, "_LIFESPAN_STARTED", False, raising=False)
    return {"bm": bm, "pc": pc, "ims": ims, "cs": cs}


async def test_second_lifespan_cycle_does_not_run_teardown_in_http_mode(
    monkeypatch, spies
):
    """In http mode, a probe session's enter+exit must NOT run any destructive
    teardown while an earlier session (lifespan A) is still open."""
    monkeypatch.setattr(server, "_SERVE_TRANSPORT", "http", raising=False)
    bm, pc, ims = spies["bm"], spies["pc"], spies["ims"]

    async with server.app_lifespan(None):  # lifespan A — stays open
        async with server.app_lifespan(None):  # lifespan B — probe shape
            pass
        # B has fully exited; A is still open. No teardown may have fired.
        assert bm.close_all_calls == 0
        assert pc.cleanup_all_calls == 0
        assert ims.clear_all_calls == 0

    # Even A's exit is a no-op in http mode (process end reaps via atexit).
    assert bm.close_all_calls == 0
    assert pc.cleanup_all_calls == 0
    assert ims.clear_all_calls == 0


async def test_lifespan_reentry_does_not_rearm_orphan_recovery(monkeypatch, spies):
    """``process_cleanup.activate`` (orphan recovery) must run exactly once across
    two sequential lifespan cycles — a re-entry must not re-arm it."""
    monkeypatch.setattr(server, "_SERVE_TRANSPORT", "http", raising=False)
    pc, cs, bm = spies["pc"], spies["cs"], spies["bm"]

    async with server.app_lifespan(None):
        pass
    async with server.app_lifespan(None):
        pass

    assert pc.activate_calls == 1
    # The rest of the startup block is likewise once-per-process.
    assert cs.sweep_calls == 1
    assert bm.start_calls == 1


async def test_stdio_mode_exit_still_runs_full_teardown(monkeypatch, spies):
    """The 1.x standalone-stdio contract: a single enter+exit cycle runs the full
    destructive teardown (close_all / _cleanup_all_tracked / clear_all)."""
    monkeypatch.setattr(server, "_SERVE_TRANSPORT", "stdio", raising=False)
    bm, pc, ims = spies["bm"], spies["pc"], spies["ims"]

    async with server.app_lifespan(None):
        pass

    assert bm.stop_calls == 1
    assert bm.close_all_calls == 1
    assert pc.cleanup_all_calls == 1
    assert ims.clear_all_calls == 1
