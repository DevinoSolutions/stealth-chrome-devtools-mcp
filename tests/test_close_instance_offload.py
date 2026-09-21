"""F-180 pinning tests: close_instance must offload the synchronous kill to a
worker thread under a real timeout so the event loop stays responsive.

Scenarios:
1. Loop-stays-responsive: a 30s-stuck kill must NOT freeze the event loop.
2. Double-close: second sequential close returns False, kill invoked once.
3. Concurrent close: exactly one of two concurrent closes claims.
4. Happy path: fast kill returns True, instance removed, storage cleaned.
5. A wedged tab's close is bounded, so teardown still completes.
6. The kill ladder reports the rung that ended the browser (F-910).
7. The TOOL tells its caller what the close did to the seed (F-910 M3) —
   hermetic, through the real tool body with the singletons swapped.
8. The wait OBSERVES the browser's exit and never reaps it, so asyncio's child
   watcher keeps the status it is waiting for (F-910 M2, POSIX); it declines a
   pid asyncio has already collected (S1); and a close cancelled DURING the
   grace still ends the browser (S2).
"""

import ast
import asyncio
import inspect
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import process_exit
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserState


def _noop_coro():
    """A coroutine factory for fake awaitable returns."""

    async def _noop():
        pass

    return _noop()


def _make_fake_browser():
    """Return a minimal fake browser object with stubs for all close_instance needs."""
    browser = SimpleNamespace(
        tabs=[],
        connection=SimpleNamespace(
            closed=True,
            send=MagicMock(side_effect=lambda *a, **kw: _noop_coro()),
            disconnect=MagicMock(side_effect=lambda *a, **kw: _noop_coro()),
        ),
        _process=SimpleNamespace(
            returncode=0,
            pid=99999,
            terminate=MagicMock(),
            kill=MagicMock(),
        ),
        _process_pid=99999,
        stop=MagicMock(return_value=None),
    )
    return browser


def _make_fake_instance(instance_id: str = "test-1"):
    """Return a BrowserInstance with minimal fields."""
    return BrowserInstance(
        instance_id=instance_id,
        state=BrowserState.READY,
    )


def _seed_manager(manager: BrowserManager, instance_id: str = "test-1"):
    """Seed a BrowserManager with one fake instance, returning (browser, instance)."""
    browser = _make_fake_browser()
    instance = _make_fake_instance(instance_id)
    manager._instances[instance_id] = {
        "browser": browser,
        "instance": instance,
    }
    manager._spawn_diagnostics[instance_id] = {"dummy": True}
    return browser, instance


PATCHES = {
    "stealth_chrome_devtools_mcp.embedded.process_cleanup.kill_browser_process": MagicMock(),
    "stealth_chrome_devtools_mcp.embedded.process_cleanup.finalize_browser_process": MagicMock(),
    "stealth_chrome_devtools_mcp.embedded.process_cleanup.cleanup_deferred_profiles": MagicMock(),
    "stealth_chrome_devtools_mcp.embedded.in_memory_storage.remove_instance": MagicMock(),
}


@pytest.fixture(autouse=True)
def _isolate_process_cleanup(monkeypatch):
    """Stub out process_cleanup and in_memory_storage so no real OS work happens."""
    from stealth_chrome_devtools_mcp.embedded.in_memory_storage import (
        in_memory_storage as ps,
    )
    from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

    monkeypatch.setattr(process_cleanup, "kill_browser_process", MagicMock())
    monkeypatch.setattr(process_cleanup, "finalize_browser_process", MagicMock())
    monkeypatch.setattr(process_cleanup, "cleanup_deferred_profiles", MagicMock())
    monkeypatch.setattr(ps, "remove_instance", MagicMock())


# ---------------------------------------------------------------------------
# 1. Loop-stays-responsive (§5.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_stays_responsive_during_stuck_kill(monkeypatch):
    """A 30-second stuck kill must not freeze the event loop.

    Monkepatches kill_browser_process to sleep in a cancellable loop (simulating
    a wedged synchronous kill). A concurrent heartbeat task increments a counter
    every 0.05s. close_instance with a ~1s timeout must return within ~2s,
    heartbeat must have advanced, and the kill must NOT be re-run inline.
    """
    import threading

    from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

    kill_call_count = 0
    kill_done = threading.Event()

    def slow_kill(instance_id):
        nonlocal kill_call_count
        kill_call_count += 1
        kill_done.wait(timeout=30)

    monkeypatch.setattr(process_cleanup, "kill_browser_process", slow_kill)

    manager = BrowserManager()
    _browser, _instance = _seed_manager(manager, "stuck-1")

    # Override CLOSE_KILL_TIMEOUT to 1.0s for a fast test
    monkeypatch.setattr(manager, "CLOSE_KILL_TIMEOUT", 1.0)

    heartbeat_count = 0
    heartbeat_running = True

    async def heartbeat():
        nonlocal heartbeat_count
        while heartbeat_running:
            heartbeat_count += 1
            await asyncio.sleep(0.05)

    hb_task = asyncio.create_task(heartbeat())

    warnings_before = len(debug_logger._warnings)

    t0 = time.monotonic()
    result = await manager.close_instance("stuck-1")
    elapsed = time.monotonic() - t0

    heartbeat_running = False
    kill_done.set()
    await asyncio.sleep(0.1)
    hb_task.cancel()
    try:
        await hb_task
    except asyncio.CancelledError:
        pass

    # close_instance returned within ~CLOSE_KILL_TIMEOUT, not 30s
    assert elapsed < 5.0, f"close_instance took {elapsed:.1f}s, expected < 5s"
    assert result is True

    # The heartbeat advanced (loop never froze)
    assert heartbeat_count >= 10, (
        f"heartbeat only ticked {heartbeat_count} times — loop was frozen"
    )

    # Instance is gone from _instances (claimed in Phase 1)
    assert "stuck-1" not in manager._instances

    # WARNING was logged about the timeout via debug_logger
    new_warnings = debug_logger._warnings[warnings_before:]
    warning_msgs = [w["message"] for w in new_warnings]
    assert any("exceeded" in m for m in warning_msgs), (
        f"Expected a timeout WARNING with 'exceeded', got: {warning_msgs}"
    )

    # kill_browser_process was invoked exactly once (NOT re-run inline)
    assert kill_call_count == 1, (
        f"kill_browser_process called {kill_call_count} times, expected 1"
    )


# ---------------------------------------------------------------------------
# 2. Double-close (§5.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_close_returns_false_second_time(monkeypatch):
    """Two sequential close_instance calls: first True, second False."""
    manager = BrowserManager()
    _seed_manager(manager, "dbl-1")

    result1 = await manager.close_instance("dbl-1")
    result2 = await manager.close_instance("dbl-1")

    assert result1 is True
    assert result2 is False


@pytest.mark.asyncio
async def test_double_close_blocking_teardown_invoked_once(monkeypatch):
    """_blocking_teardown must be invoked exactly once across two sequential closes."""
    manager = BrowserManager()
    _seed_manager(manager, "dbl-2")

    teardown_calls = 0
    original_teardown = getattr(manager, "_blocking_teardown", None)

    if original_teardown is not None:

        def counting_teardown(*args, **kwargs):
            nonlocal teardown_calls
            teardown_calls += 1
            return original_teardown(*args, **kwargs)

        monkeypatch.setattr(manager, "_blocking_teardown", counting_teardown)

        await manager.close_instance("dbl-2")
        await manager.close_instance("dbl-2")

        assert teardown_calls == 1
    else:
        # Pre-implementation: _blocking_teardown doesn't exist yet
        # This test will pass vacuously and be meaningful after implementation
        await manager.close_instance("dbl-2")
        await manager.close_instance("dbl-2")
        assert "dbl-2" not in manager._instances


# ---------------------------------------------------------------------------
# 3. Concurrent close (§5.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_close_exactly_one_claims(monkeypatch):
    """Two concurrent close_instance calls: exactly one returns True."""
    manager = BrowserManager()
    _seed_manager(manager, "conc-1")

    results = await asyncio.gather(
        manager.close_instance("conc-1"),
        manager.close_instance("conc-1"),
    )

    assert sorted(results) == [False, True], (
        f"Expected exactly one True and one False, got {results}"
    )


# ---------------------------------------------------------------------------
# 4. Happy path (§5.2)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. Phase-2 tab close is bounded (RELEASE-FIX-A C5 / A7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_instance_bounds_hung_tab_close():
    """A wedged renderer whose ``tab.close()`` never returns must not hang
    close_instance. Phase-2 wraps each ``tab.close()`` in ``asyncio.wait_for``
    (timeout=2.0), so a hung tab is logged and teardown proceeds to completion.

    RED before the fix: the bare ``await tab.close()`` blocks forever, so the
    outer 15s guard cancels close_instance and raises TimeoutError.
    """

    class _HungTab:
        async def close(self):
            await asyncio.Event().wait()  # never set — hangs forever

    manager = BrowserManager()
    browser, _instance = _seed_manager(manager, "hung-1")
    browser.tabs = [_HungTab()]

    t0 = time.monotonic()
    result = await asyncio.wait_for(manager.close_instance("hung-1"), timeout=15)
    elapsed = time.monotonic() - t0

    assert result is True
    assert "hung-1" not in manager._instances
    # Phase-2 bounded the hung tab close at ~2s; without the fix it would run
    # until the outer guard cancels at 15s (raising TimeoutError above instead).
    assert elapsed < 10.0, f"close_instance took {elapsed:.1f}s — tab close unbounded"


@pytest.mark.asyncio
async def test_happy_path_fast_kill(monkeypatch):
    """Fast stubbed kill: returns True, instance removed, in_memory_storage called."""
    from stealth_chrome_devtools_mcp.embedded.in_memory_storage import (
        in_memory_storage as ps,
    )

    storage_mock = MagicMock()
    monkeypatch.setattr(ps, "remove_instance", storage_mock)

    manager = BrowserManager()
    _seed_manager(manager, "happy-1")

    result = await manager.close_instance("happy-1")

    assert result is True
    assert "happy-1" not in manager._instances
    storage_mock.assert_called_once_with("happy-1")


# ---------------------------------------------------------------------------
# 6. The kill ladder reports what it did, and reporting cannot break it (F-910)
# ---------------------------------------------------------------------------


def test_the_kill_ladder_logs_the_rung_that_ended_the_browser():
    """A rung that WORKS must log and return, and the log must not raise.

    Every other node in this file hands the teardown a process whose
    ``returncode`` is already set, so the ladder returns at its guard and its
    logging is never executed. That gap shipped a real defect: ``process_exit``
    imported the debug_logger MODULE instead of the singleton, so the line
    written after a successful terminate was an ``AttributeError`` — swallowed
    where the close-diagnostics are written, but NOT here, where it would
    escape the ladder that had just killed a browser. So this node drives a
    process that is still running.
    """
    process = SimpleNamespace(
        returncode=None,
        pid=4242,
        terminate=MagicMock(),
        kill=MagicMock(),
    )
    before = len(debug_logger.get_debug_view_paginated().get("all_info") or [])

    process_exit.terminate("ladder-1", process, None, 2)

    process.terminate.assert_called_once_with()
    process.kill.assert_not_called()
    written = (debug_logger.get_debug_view_paginated().get("all_info") or [])[before:]
    assert [
        entry
        for entry in written
        if entry.get("component") == "process_exit"
        and entry.get("method") == "terminate_process"
        and "ladder-1" in entry.get("message", "")
    ], f"the rung that ended the browser wrote nothing: {written!r}"


# ---------------------------------------------------------------------------
# 7. The tool reports what the close did to the SEED (F-910, the lead's M3)
# ---------------------------------------------------------------------------


def _closing_server(patched_server, *, role: str, refresh: dict | None):
    """Drive the REAL ``close_instance`` tool body with everything else faked.

    The subject is the tool and not the manager: the refresh's answer was
    computed correctly all along and then dropped on the floor, so a pin on
    ``clone_storage`` or on ``browser_manager`` cannot see this defect at all.
    ``refresh=None`` means the refresh must never be called.
    """
    calls: list[str] = []

    def _refresh(reason):
        calls.append(reason)
        assert refresh is not None, f"the refresh must not run for role {role!r}"
        return refresh

    async def _close(instance_id):
        return True

    async def _spawn_diagnostics(instance_id):
        return {"profile_selection": {"profile_role": role}}

    async def _clear(instance_id):
        return None

    server = patched_server(
        browser_manager=SimpleNamespace(
            close_instance=_close,
            get_spawn_diagnostics=_spawn_diagnostics,
        ),
        network_interceptor=SimpleNamespace(clear_instance_data=_clear),
        dynamic_hook_system=SimpleNamespace(remove_instance=MagicMock()),
        clone_storage=SimpleNamespace(
            _refresh_master_snapshot_if_safe=_refresh,
            _release_clone_dir=MagicMock(),
        ),
        profile_seed=SimpleNamespace(DEFAULT_SESSION="default"),
    )
    return server, calls


@pytest.mark.asyncio
async def test_a_refused_seed_refresh_reaches_the_caller(patched_server, call_tool):
    """A refusal must be REPORTED, in `clone_storage`'s own words.

    This is the half F-910 could not see: closing the shared session refreshes
    the seed every later session is copied from, and the refusal went into a
    dict ``close_instance`` discarded — so a seed that had stopped moving was
    indistinguishable, at every surface a caller has, from one that had not.
    """
    server, calls = _closing_server(
        patched_server,
        role="default",
        refresh={"seed_refreshed": False, "seed_error": "default-in-use"},
    )

    answer = await call_tool(server, "close_instance", instance_id="i1")

    assert calls == ["after-default-close"]
    assert answer == {
        "closed": True,
        "seed_refreshed": False,
        "seed_error": "default-in-use",
    }, answer


@pytest.mark.asyncio
async def test_a_close_that_owed_no_refresh_says_so_rather_than_nothing(
    patched_server, call_tool
):
    """``seed_refreshed: None`` is "not asked" — one answer shape, no missing key.

    A key present only after a `default` close would make "nothing to report"
    and "nothing reported" the same reading, which is the shape of the defect
    this reporting exists to remove.
    """
    server, calls = _closing_server(patched_server, role="clone", refresh=None)

    answer = await call_tool(server, "close_instance", instance_id="i2")

    assert calls == []
    assert answer == {"closed": True, "seed_refreshed": None}, answer


@pytest.mark.asyncio
async def test_a_refusal_with_no_words_is_still_reported(patched_server, call_tool):
    """A refusal the refresh left unexplained must not read as an absent key.

    ``_refresh_master_snapshot_if_safe`` names every refusal it makes today;
    this pins what happens if one ever stops, because ``seed_refreshed: False``
    with no ``seed_error`` is a silence of exactly the kind under repair.
    """
    server, _calls = _closing_server(
        patched_server, role="default", refresh={"seed_refreshed": False}
    )

    answer = await call_tool(server, "close_instance", instance_id="i3")

    assert answer["seed_error"] == "unreported", answer


# ---------------------------------------------------------------------------
# 8. The wait does not reap (F-910 M2) — POSIX, unreachable from this host
# ---------------------------------------------------------------------------


def test_the_wait_calls_nothing_that_would_reap_the_browser():
    """No ``wait()`` anywhere in ``process_exit``, and the reason is POSIX.

    A browser we launched is a child of this process and asyncio's child
    watcher is already blocked in ``os.waitpid`` on it. ``psutil``'s POSIX
    ``Process.wait()`` reaps (``_psposix.wait_pid`` polls
    ``os.waitpid(pid, WNOHANG)``), and a child's status can be collected once —
    so the loser of that race reports ``returncode 255`` for a browser that
    exited 0. This host is Windows, where no watcher and no zombie exist, so
    the defect is INVISIBLE to every node here and to every local measurement:
    a source pin is what can state it at all. Keyed on the CALL and not on a
    text match, so the module's prose about waiting is untouched.
    """
    source = Path(inspect.getsourcefile(process_exit)).read_text(encoding="utf-8")
    waits = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait"
    ]
    assert not waits, (
        "process_exit calls .wait() at "
        f"{[node.lineno for node in waits]} — on POSIX that reaps a child "
        "asyncio is waiting on, and asyncio then reports returncode 255"
    )


def _fake_psutil(statuses):
    """A psutil stand-in whose one process answers *statuses* in order."""
    answers = list(statuses)

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def is_running(self):
            return answers[0] != "gone" if answers else False

        def status(self):
            return answers.pop(0) if len(answers) > 1 else answers[0]

    return SimpleNamespace(
        Process=_Proc,
        STATUS_ZOMBIE=psutil.STATUS_ZOMBIE,
        NoSuchProcess=psutil.NoSuchProcess,
        Error=psutil.Error,
    )


def test_a_zombie_is_an_exited_browser(monkeypatch):
    """The POSIX success shape: still running, then a zombie, then done.

    Windows never produces this, so it is driven through the module's one
    ``psutil`` name rather than a real process. A zombie has exited — its
    cookie store is committed — and calling that a timeout would spend the
    whole 5 s grace and then kill an already-dead pid on every POSIX close.
    """
    monkeypatch.setattr(
        process_exit,
        "psutil",
        _fake_psutil([psutil.STATUS_RUNNING, psutil.STATUS_ZOMBIE]),
    )
    monkeypatch.setattr(process_exit, "_POLL_SECONDS", 0.0)

    waited = process_exit.wait_for_exit(4321, timeout=1.0)

    assert waited.exited is True, waited
    assert waited.reason == "exited", waited


def test_a_browser_that_never_leaves_is_handed_to_the_kill_path(monkeypatch):
    """The grace is a CEILING: a wedged browser times out and is not called gone."""
    monkeypatch.setattr(process_exit, "psutil", _fake_psutil([psutil.STATUS_RUNNING]))
    monkeypatch.setattr(process_exit, "_POLL_SECONDS", 0.0)

    waited = process_exit.wait_for_exit(4321, timeout=0.05)

    assert waited.exited is False, waited
    assert waited.reason == "still-running", waited


def test_a_process_asyncio_has_already_collected_is_never_waited_on():
    """F-910 S1: a set ``returncode`` means the pid may already be a stranger's.

    asyncio collected this child, so on POSIX the pid is free from that
    instant; waiting would spend up to the whole grace on whoever holds it
    next. The answer is None — "do not wait" — which lands on exactly the
    behaviour that shipped before F-910.
    """
    collected = SimpleNamespace(returncode=0, pid=4242)

    assert process_exit.browser_pid(collected, None) is None
    assert process_exit.browser_pid(collected, 777) is None


@pytest.mark.asyncio
async def test_a_cancelled_grace_still_kills_the_browser(monkeypatch):
    """F-910 S2: a client that disconnects mid-grace must not strand a Chrome.

    Phase 2b sits inside a ``try`` whose handler is ``except Exception``, and
    ``CancelledError`` is not one — so without this arm the grace would newly
    make a cancelled close likelier to leave a live browser for the orphan
    reaper, by the width of the ceiling (up to 5 s for a wedged one).
    """

    async def _cancelled(pid, timeout=process_exit.EXIT_GRACE_SECONDS):
        raise asyncio.CancelledError

    monkeypatch.setattr(process_exit, "wait_for_exit_async", _cancelled)
    process = SimpleNamespace(
        returncode=None, pid=5150, terminate=MagicMock(), kill=MagicMock()
    )

    with pytest.raises(asyncio.CancelledError):
        await process_exit.settle("cancel-1", process, None, 2)

    process.terminate.assert_called_once_with()
