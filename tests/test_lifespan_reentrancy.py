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

from stealth_chrome_devtools_mcp.embedded import desktop_launch, server


class _SpyBrowserManager:
    def __init__(self, order: list[str]) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.close_all_calls = 0
        self._order = order

    async def start_idle_reaper(self) -> None:
        self.start_calls += 1
        self._order.append("start_idle_reaper")

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
def spies(monkeypatch, patched_server):
    """Swap the four teardown/startup singletons for spies and reset the
    once-per-process startup guard so each test drives a fresh lifespan.

    Routed through ``patched_server`` (plan_SERVERSPLIT §3.4): ``app_lifespan``
    reads these four as ``rt.*`` against ``tool_runtime``, THE one patchable
    home, so a direct ``setattr(server, …)`` here would substitute the tool
    bodies' singletons while the lifespan kept driving the real ones.
    ``_LIFESPAN_STARTED`` stays a direct ``server`` write — it is per-execution
    process state, not tool state, and does not live in ``tool_runtime``.
    """
    order: list[str] = []
    bm = _SpyBrowserManager(order)
    pc = _SpyProcessCleanup()
    ims = _SpyStorage()
    cs = _SpyCloneStorage()
    patched_server(
        browser_manager=bm, process_cleanup=pc, in_memory_storage=ims, clone_storage=cs
    )
    # raising=False so the RED run (before the guard exists) fails on the
    # behavioral assertions below, not on a missing-attribute setup error.
    monkeypatch.setattr(server, "_LIFESPAN_STARTED", False, raising=False)
    # F-1004: the lifespan raises this process's own priority; a spy keeps the
    # suite from writing to the pytest process and records when it ran.
    monkeypatch.setattr(
        desktop_launch,
        "raise_inherited_priority",
        lambda pid=None, *, tree=False: order.append("raise_priority") or {},
    )
    return {"bm": bm, "pc": pc, "ims": ims, "cs": cs, "order": order}


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


async def test_the_priority_is_raised_once_and_before_any_browser_work(
    monkeypatch, spies
):
    """F-1004: every Chrome inherits the backend's I/O and memory priority, so
    the raise runs once per process and before the idle reaper (the first
    startup step that reaches a browser), however many sessions enter."""
    monkeypatch.setattr(server, "_SERVE_TRANSPORT", "http", raising=False)

    async with server.app_lifespan(None):
        pass
    async with server.app_lifespan(None):
        pass

    order = spies["order"]
    assert order.count("raise_priority") == 1
    assert order.index("raise_priority") < order.index("start_idle_reaper")


class _SpyHygiene:
    """Records the one call the lifespan makes to ``session_hygiene`` (F-1023)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.reattaching: list[Any] = []

    async def exit_if_unadopted(self, has_browsers, stop, reattaching=None) -> bool:
        self.calls.append((has_browsers, stop))
        self.reattaching.append(reattaching)
        return False


async def test_a_cold_started_backend_arms_the_unadopted_exit(
    monkeypatch, spies, patched_server
):
    """F-1023: ``--exit-if-unadopted`` + http starts the watch, once, with a browser
    question that asks the live manager and a stop that raises SIGTERM."""
    hygiene = _SpyHygiene()
    patched_server(session_hygiene=hygiene)
    monkeypatch.setattr(server, "_SERVE_TRANSPORT", "http", raising=False)
    monkeypatch.setattr(server, "_EXIT_IF_UNADOPTED", True, raising=False)
    raised: list[int] = []
    monkeypatch.setattr(server.signal, "raise_signal", raised.append)
    spies["bm"].list_instances = _list_instances  # type: ignore[method-assign]

    async with server.app_lifespan(None):
        await server._UNADOPTED_WATCH
    async with server.app_lifespan(None):
        pass

    assert len(hygiene.calls) == 1
    has_browsers, stop = hygiene.calls[0]
    assert await has_browsers() is False
    stop()
    assert raised == [server.signal.SIGTERM]
    # A re-attach pass still running keeps the backend: the watch is told how to ask.
    assert hygiene.reattaching == [server.rt.browser_reattach.in_progress]


async def test_an_operator_started_backend_never_arms_it(
    monkeypatch, spies, patched_server
):
    hygiene = _SpyHygiene()
    patched_server(session_hygiene=hygiene)
    monkeypatch.setattr(server, "_SERVE_TRANSPORT", "http", raising=False)
    monkeypatch.setattr(server, "_EXIT_IF_UNADOPTED", False, raising=False)

    async with server.app_lifespan(None):
        pass

    assert hygiene.calls == []


async def _list_instances() -> list[Any]:
    return []
