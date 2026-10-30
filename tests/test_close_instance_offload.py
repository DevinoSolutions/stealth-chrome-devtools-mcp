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
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded import (
    clone_protection,
    clone_storage,
    login_persistence,
    process_exit,
    tab_open,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserState
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


def _noop_coro():
    """A coroutine factory for fake awaitable returns."""

    async def _noop():
        pass

    return _noop()


def _make_fake_browser():
    """Return a minimal fake browser object with stubs for all close_instance needs."""
    browser = SimpleNamespace(
        tabs=[],
        send=MagicMock(side_effect=lambda *a, **kw: _noop_coro()),
        aclose=MagicMock(side_effect=lambda *a, **kw: _noop_coro()),
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


def _tab_close_warnings(since: int, instance_id: str) -> list[str]:
    return [
        w["message"]
        for w in debug_logger._warnings[since:]
        if f"Failed to close tab for {instance_id}" in w["message"]
    ]


async def _close_with_tab_send_failing(instance_id: str, error: Exception):
    """close_instance on an instance whose one tab's CDP close raises ``error``."""
    manager = BrowserManager()
    browser, _instance = _seed_manager(manager, instance_id)
    browser.tabs = [SimpleNamespace(target=SimpleNamespace(target_id="T-1"))]

    async def _send(*_a, **_kw):
        raise error

    browser.send = _send
    before = len(debug_logger._warnings)
    assert await manager.close_instance(instance_id) is True
    return _tab_close_warnings(before, instance_id)


@pytest.mark.asyncio
async def test_a_tab_chrome_already_dropped_is_closed_not_a_warning():
    """F-996: ``Target.closeTarget`` on a tab Chrome already dropped answers
    ``No target with given id found [code: -32602]``. That is the state the
    close wanted, so it is not a WARNING (CI run 38919791742 counted it as a
    fault and failed the six-browser fleet test). The exception is built by
    nodriver's own constructor from the CDP error object, as the listener does.
    """
    gone = ProtocolException(
        {"code": -32602, "message": "No target with given id found"}
    )
    assert str(gone) == "No target with given id found [code: -32602]"

    assert await _close_with_tab_send_failing("gone-tab-1", gone) == []


@pytest.mark.asyncio
async def test_the_gone_tab_answer_is_recognised_without_its_code():
    """F-996: the same CDP failure also reaches us with no code at all (F-828
    saw that for a stale node), so the wording is what is matched."""
    gone = ProtocolException({"message": "No target with given id found"})

    assert await _close_with_tab_send_failing("gone-tab-2", gone) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        # Same code, a different refusal: still a fault worth a warning.
        ProtocolException({"code": -32602, "message": "Invalid parameters"}),
        ProtocolException({"code": -32000, "message": "Target closed"}),
        RuntimeError("connection reset"),
        # The wording from something that is not a CDP reply is not Chrome saying so.
        RuntimeError("No target with given id found"),
    ],
    ids=[
        "same-code-other-message",
        "other-protocol-failure",
        "not-protocol",
        "gone-wording-not-protocol",
    ],
)
async def test_any_other_tab_close_failure_still_warns(error):
    """F-996 control: only the gone-tab answer is absorbed; every other failure
    in the tab-close loop still logs the WARNING it logged before."""
    warnings = await _close_with_tab_send_failing("other-tab-1", error)

    assert len(warnings) == 1, warnings
    assert str(error) in warnings[0]


@pytest.mark.asyncio
async def test_a_tab_close_that_times_out_names_its_type():
    """T-5900: ``asyncio.wait_for`` raises ``TimeoutError()`` with NO text, so a
    tab close that outran its 2 s bound logged ``Failed to close tab for <id>: ``
    and a CI failure (run 39564101655) said nothing about what had failed. The
    type is part of the line now; a failure with text keeps it after the type."""
    (silent,) = await _close_with_tab_send_failing("timeout-tab-1", TimeoutError())
    assert silent.endswith("timeout-tab-1: TimeoutError"), silent

    (spoken,) = await _close_with_tab_send_failing(
        "timeout-tab-2", RuntimeError("connection reset")
    )
    assert spoken.endswith("timeout-tab-2: RuntimeError: connection reset"), spoken


@pytest.mark.asyncio
async def test_closing_a_browser_that_crashed_answers_closed_either_way_round():
    """F-1022 (T-5956, F-789): the recovery a failed tool call names is
    ``close_instance``, and its answer for a browser that DIED must not depend on
    which side of that call the crash was noticed. The fake browser's process has
    exited, so ``get_instance`` discards it as the next tool call does.

    * noticed AFTER (``close_instance`` first): it claims the instance — True, as
      it always did;
    * noticed BEFORE (a tool call or ``list_instances`` discarded it first): the
      instance is gone from the manager, and this used to answer False — "no such
      instance" — for the same browser, which flipped
      ``test_crash_recovery_after_the_owned_chrome_is_killed`` on macOS.

    One close claims a crashed browser; the next, and an id never held, still miss.
    """
    manager = BrowserManager()
    _seed_manager(manager, "after-1")
    assert await manager.close_instance("after-1") is True
    assert await manager.close_instance("after-1") is False

    _seed_manager(manager, "before-1")
    assert await manager.get_instance("before-1") is None  # the crash is noticed
    assert "before-1" not in manager._instances
    assert await manager.close_instance("before-1") is True
    assert await manager.close_instance("before-1") is False

    _seed_manager(manager, "before-2")
    assert await manager.list_instances() == []  # noticed by the other route
    assert await manager.close_instance("before-2") is True

    assert await manager.close_instance("never-held") is False


@pytest.mark.asyncio
async def test_a_close_never_claims_a_browser_that_was_closed_not_crashed():
    """The claim exists only for a browser that exited ON ITS OWN: one that
    ``close_instance`` closed is not claimable a second time."""
    manager = BrowserManager()
    _seed_manager(manager, "closed-1")
    assert await manager.close_instance("closed-1") is True
    assert await manager.get_instance("closed-1") is None
    assert await manager.close_instance("closed-1") is False


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


def _ladder_lines(pid_gone: bool, monkeypatch) -> list[str]:
    """The ``process_exit`` lines one ``terminate`` writes for a process whose pid
    is, or is not, still on the machine once ``terminate()`` has returned."""
    monkeypatch.setattr(process_exit.psutil, "pid_exists", lambda _pid: not pid_gone)
    process = SimpleNamespace(
        returncode=None, pid=4242, terminate=MagicMock(), kill=MagicMock()
    )
    before = len(debug_logger.get_debug_view_paginated().get("all_info") or [])

    process_exit.terminate("wording-1", process, None, 1)

    written = (debug_logger.get_debug_view_paginated().get("all_info") or [])[before:]
    return [
        entry["message"]
        for entry in written
        if entry.get("component") == "process_exit"
        and entry.get("method") == "terminate_process"
        and "wording-1" in entry.get("message", "")
    ]


def test_the_ladder_says_ended_only_for_a_process_it_saw_go(monkeypatch):
    """T-9335: a ``terminate()`` that merely RETURNED was logged "ended ...
    successfully" while Chrome was still tearing down for another ~15 s."""
    (line,) = _ladder_lines(True, monkeypatch)

    assert "ended browser with pid 4242" in line


def test_the_ladder_says_what_it_did_for_a_process_still_up(monkeypatch):
    (line,) = _ladder_lines(False, monkeypatch)

    assert "ended" not in line and "successfully" not in line
    assert "sent terminate_process" in line and "4242" in line
    assert "not yet seen to exit" in line


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


def _crashed_browser_server(patched_server, selection: dict, refreshes: list):
    """The REAL tool body over a REAL manager holding a browser whose process has
    exited, with the spawn's recorded *selection* — the thing a crash discard pops."""
    manager = BrowserManager()
    _seed_manager(manager, "crash-1")
    manager._spawn_diagnostics["crash-1"] = {"profile_selection": selection}

    def _refresh(reason):
        refreshes.append(reason)
        return {"seed_refreshed": True}

    async def _clear(instance_id):
        return None

    server = patched_server(
        browser_manager=manager,
        network_interceptor=SimpleNamespace(clear_instance_data=_clear),
        dynamic_hook_system=SimpleNamespace(remove_instance=MagicMock()),
        clone_storage=SimpleNamespace(_refresh_master_snapshot_if_safe=_refresh),
    )
    return server, manager


@pytest.mark.parametrize(
    "noticed_first", [True, False], ids=["discard-first", "close-first"]
)
@pytest.mark.asyncio
async def test_a_crashed_clone_is_released_to_the_sweep_whichever_call_noticed(
    patched_server, call_tool, tmp_path, noticed_first
):
    """F-1022 review: the discard popped the spawn diagnostics, so the tool saw no
    clone and left its dir in ``clone_protection.PROTECTED`` (sweep-exempt) until a
    backend restart. The discard itself now releases it - the browser is gone
    whether or not anyone ever calls ``close_instance`` - and the close that follows
    answers the same as when it noticed the crash itself."""
    clone = tmp_path / "clone-1"
    clone.mkdir()
    clone_protection.protect(clone)
    try:
        server, manager = _crashed_browser_server(
            patched_server,
            {"profile_role": "clone", "user_data_dir": str(clone)},
            [],
        )
        assert clone_protection.is_protected(clone), "the premise: a live clone"
        if noticed_first:
            assert await manager.get_instance("crash-1") is None
            assert not clone_protection.is_protected(clone), (
                "a crash-discarded clone stays sweep-exempt until restart"
            )

        answer = await call_tool(server, "close_instance", instance_id="crash-1")

        assert answer == {"closed": True, "seed_refreshed": None}, answer
        assert not clone_protection.is_protected(clone), answer
    finally:
        clone_protection.release(clone)


@pytest.mark.asyncio
async def test_closing_a_crashed_id_never_unprotects_a_respawn_in_its_dir(
    patched_server, call_tool, tmp_path
):
    """F-1022 review 2: clone dirs are deterministic per client session, so once the
    discard frees one, a respawn reserves it again. The later ``close_instance`` of
    the OLD crashed id must not release it a second time: that is the NEW spawn's
    in-flight protection (F-834)."""
    clone = tmp_path / "clone-1"
    clone.mkdir()
    clone_protection.protect(clone)
    try:
        server, manager = _crashed_browser_server(
            patched_server,
            {"profile_role": "clone", "user_data_dir": str(clone)},
            [],
        )
        assert await manager.get_instance("crash-1") is None  # discard releases
        clone_protection.protect(clone)  # a respawn reserves the same dir

        answer = await call_tool(server, "close_instance", instance_id="crash-1")

        assert answer["closed"] is True, answer
        assert clone_protection.is_protected(clone), (
            "closing the old crashed id unprotected the NEW spawn's clone dir"
        )
    finally:
        clone_protection.release(clone)


@pytest.mark.parametrize(
    "noticed_first", [True, False], ids=["discard-first", "close-first"]
)
@pytest.mark.parametrize("role", ["default", "fleet", "named"])
@pytest.mark.asyncio
async def test_a_crashed_non_clone_dir_is_never_unprotected(
    patched_server, call_tool, tmp_path, role, noticed_first
):
    """F-1022 review 2: only a disposable clone's dir is ever released. A crashed
    named, ``fleet`` or ``default`` session's dir is left exactly as it was by the
    discard and by the close, in both orderings."""
    directory = tmp_path / role
    directory.mkdir()
    clone_protection.protect(directory)
    try:
        server, manager = _crashed_browser_server(
            patched_server, {"profile_role": role, "user_data_dir": str(directory)}, []
        )
        if noticed_first:
            assert await manager.get_instance("crash-1") is None
            assert clone_protection.is_protected(directory), "the discard released it"

        await call_tool(server, "close_instance", instance_id="crash-1", session=role)

        assert clone_protection.is_protected(directory), role
    finally:
        clone_protection.release(directory)


@pytest.mark.parametrize(
    "noticed_first", [True, False], ids=["discard-first", "close-first"]
)
@pytest.mark.asyncio
async def test_a_crashed_default_browser_never_refreshes_the_seed(
    patched_server, call_tool, noticed_first
):
    """F-1022 review 2: master-profile loss is top severity, so the good seed is not
    overwritten from a profile whose browser crashed. Both orderings answer alike:
    ``seed_refreshed: False`` with ``seed_error: "default-crashed"``, and the refresh
    never runs. (A live ``default`` close still refreshes: the tests above.)"""
    refreshes: list[str] = []
    server, manager = _crashed_browser_server(
        patched_server,
        {
            "profile_role": "default",
            "user_data_dir": str(clone_storage.master_profile_dir()),
        },
        refreshes,
    )
    if noticed_first:
        assert await manager.get_instance("crash-1") is None

    answer = await call_tool(
        server, "close_instance", instance_id="crash-1", session="default"
    )

    assert refreshes == [], refreshes
    assert answer == {
        "closed": True,
        "seed_refreshed": False,
        "seed_error": "default-crashed",
    }, answer


@pytest.mark.parametrize(
    "noticed_first", [True, False], ids=["discard-first", "close-first"]
)
@pytest.mark.asyncio
async def test_an_unnamed_close_of_a_crashed_fleet_browser_is_refused_either_way_round(
    patched_server, call_tool, noticed_first
):
    """F-1022 review: the refusal keeps its one rule - it names which profile the id
    was, so the answer does not depend on which call noticed the crash. Refused,
    the claim is NOT consumed: closing it with its session name still answers True."""
    server, manager = _crashed_browser_server(
        patched_server,
        {
            "profile_role": "fleet",
            "user_data_dir": str(clone_storage.clone_root_dir() / "fleet"),
        },
        [],
    )
    if noticed_first:
        assert await manager.get_instance("crash-1") is None

    with pytest.raises(ToolError, match="'fleet' session's browser"):
        await call_tool(server, "close_instance", instance_id="crash-1")

    answer = await call_tool(
        server, "close_instance", instance_id="crash-1", session="fleet"
    )
    assert answer == {"closed": True, "seed_refreshed": None}, answer


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


@pytest.mark.asyncio
async def test_the_browser_connection_is_marked_closed_before_it_is_ended():
    """T-8501: after ``aclose`` the socket is ``None``, which nodriver 0.50 reopens
    for the next ``send``; the browser is marked closed FIRST so a task still in
    flight (a popup arming, a binding watcher) is refused instead."""
    manager = BrowserManager()
    browser, _ = _seed_manager(manager)
    seen = []

    def ending(*_a, **_kw):
        seen.append(tab_open.is_closed(browser))
        return _noop_coro()

    browser.aclose = MagicMock(side_effect=ending)
    browser.update_targets = MagicMock(side_effect=_noop_coro)

    assert await manager.close_instance("test-1") is True

    assert seen == [True]
    with pytest.raises(ConnectionError):
        await tab_open.update_targets(browser)


# ---------------------------------------------------------------------------
# T-9551: the proxy forwarder Phase 1 popped is closed on EVERY way out
# ---------------------------------------------------------------------------


class _Forwarder:
    """An authenticated proxy forwarder that counts the closes it receives."""

    def __init__(self, fail: bool = False) -> None:
        self.closes = 0
        self._fail = fail

    async def close(self) -> None:
        self.closes += 1
        if self._fail:
            raise OSError("socket already gone")


def _seed_with_forwarder(manager, forwarder, instance_id: str = "test-1"):
    browser, _ = _seed_manager(manager, instance_id)
    manager._proxy_forwarders[instance_id] = forwarder
    return browser


@pytest.mark.asyncio
async def test_a_close_that_ends_normally_closes_the_forwarder_once():
    manager = BrowserManager()
    forwarder = _Forwarder()
    _seed_with_forwarder(manager, forwarder)

    assert await manager.close_instance("test-1") is True

    assert forwarder.closes == 1
    assert "test-1" not in manager._proxy_forwarders


@pytest.mark.asyncio
async def test_a_forwarder_that_fails_to_close_does_not_fail_the_close():
    manager = BrowserManager()
    forwarder = _Forwarder(fail=True)
    _seed_with_forwarder(manager, forwarder)

    assert await manager.close_instance("test-1") is True

    assert forwarder.closes == 1


@pytest.mark.asyncio
async def test_a_close_cancelled_while_settling_still_closes_the_forwarder(
    monkeypatch,
):
    """T-9551: ``settle_session_restore`` is the first await after Phase 1 popped
    the forwarder, and sat outside the try whose body closed it -- a cancelled
    close left a listening socket nothing could reach any more."""
    manager = BrowserManager()
    forwarder = _Forwarder()
    _seed_with_forwarder(manager, forwarder)

    async def _cancelled(_browser):
        raise asyncio.CancelledError

    monkeypatch.setattr(login_persistence, "settle_session_restore", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await manager.close_instance("test-1")

    assert forwarder.closes == 1


@pytest.mark.asyncio
async def test_a_close_cancelled_in_phase_two_still_closes_the_forwarder():
    """T-9551: ``CancelledError`` is not an ``Exception``, so the graceful CDP
    teardown's handlers let it through before the forwarder's close was reached."""
    manager = BrowserManager()
    forwarder = _Forwarder()
    browser = _seed_with_forwarder(manager, forwarder)
    browser.aclose = MagicMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await manager.close_instance("test-1")

    assert forwarder.closes == 1


# T-9551: the forwarder's close is bounded, so a close that is cancelled before
# Chrome's CONNECT tunnels are gone still finishes.


def _forwarder():
    from stealth_chrome_devtools_mcp.embedded.proxy_forwarder import (
        AuthenticatedProxyForwarder,
    )

    return AuthenticatedProxyForwarder("http://user:pass@upstream.invalid:8080")


@pytest.mark.asyncio
async def test_a_forwarder_with_an_open_client_connection_closes():
    """A client (Chrome's CONNECT tunnel) still connected when ``close`` runs: on
    Python 3.12.1+ ``Server.wait_closed`` waits for it, so an unbounded close never
    returns while Chrome is up. Closed clients first, so nothing is waited for."""
    forwarder = _forwarder()
    await forwarder.start()
    _reader, writer = await asyncio.open_connection(forwarder.host, forwarder.port)
    await asyncio.sleep(0.2)  # let the server accept it
    try:
        # Idle, it holds ``wait_closed`` for the 5 s request timeout (measured
        # 4.8 s); an established CONNECT tunnel holds it for as long as it lives.
        async with asyncio.timeout(1.5):
            await forwarder.close()
    finally:
        writer.close()

    assert forwarder.server is None


@pytest.mark.asyncio
async def test_a_forwarder_whose_server_never_finishes_closing_is_given_up_on(
    monkeypatch,
):
    from stealth_chrome_devtools_mcp.embedded import proxy_forwarder

    monkeypatch.setattr(proxy_forwarder, "CLOSE_BUDGET_SECONDS", 0.2)
    forwarder = _forwarder()

    async def _never():
        await asyncio.Event().wait()

    forwarder.server = SimpleNamespace(close=MagicMock(), wait_closed=_never)

    async with asyncio.timeout(5):
        await forwarder.close()

    assert forwarder.server is None


@pytest.mark.asyncio
async def test_a_cancel_is_not_swallowed_by_the_forwarders_bound():
    forwarder = _forwarder()

    async def _never():
        await asyncio.Event().wait()

    forwarder.server = SimpleNamespace(close=MagicMock(), wait_closed=_never)
    task = asyncio.ensure_future(forwarder.close())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
