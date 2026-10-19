"""F-969: a spawn whose Chrome stalls AFTER it has launched must fail inside a
named budget, through the teardown every other spawn failure already takes.

The incident (v2.1.33's publish gate, run 38554874913, macOS/ARM64): the
``instance`` fixture's ``spawn_browser`` never returned and pytest-timeout ended
the node at 300 s. The thread dump showed the event loop idle in ``select``, both
default-executor threads parked and one live child process — a spawn awaiting a
reply from a running Chrome, which nodriver gives no deadline. ``spawn_browser``
awaited ``Browser.start`` and then ``_apply_post_launch`` / ``_setup_dynamic_hooks``
with nothing around them.

What is pinned, with a double that NEVER answers rather than one that raises
(a raising double is the failure the product already handled):

* each of the three phases is bounded, by a named module constant, and the
  ``ToolError`` it raises names the stage and the budget;
* the failure reaches ``spawn_browser``'s own handler and nothing else: the Chrome
  nodriver launched is reaped by the pid it launched with (``spawn_leak``), a
  held ``Browser`` is stopped, the tracked record and the clone's directory go
  with ``kill_browser_process`` — there is no second teardown path;
* the tool body's ``_SPAWN_ATTEMPTS`` loop retries a timed-out attempt onto a
  fresh clone and leaves no leaked process, record or reservation behind;
* ``e2e_helpers.bounded_spawn`` — the integration fixtures' wrapper — names the
  await a stalled spawn is parked on.

Every await on a stalled path is wrapped in ``asyncio.wait_for`` with a named
budget: a regression here must FAIL, never hang the suite (no global pytest
timeout exists). Hermetic: no Chrome, no real ``~/.stealth-mcp``; the process
table is ``fakes.ProcessTable`` and ``pid_file`` is under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from types import SimpleNamespace

import nodriver
import pytest

import e2e_helpers
from fakes import FakeChrome, LaunchedBrowser, ProcessTable, nodriver_registry
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    google_rotation_guard,
    spawn_budget,
    spawn_contention,
    spawn_exhaustion,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import (
    dynamic_hook_system,
)
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: The shipped ceilings, read before the autouse fixture lowers them.
SHIPPED_LAUNCH_BUDGET = spawn_budget.LAUNCH_BUDGET_SECONDS

#: The budget of the ONE phase a test stalls on purpose. A stall never answers, so
#: all this has to do is elapse: a loaded runner can only make it elapse later,
#: never produce a different outcome. It costs the suite a fraction of a second.
SHORT_BUDGET = 0.3

#: The budget of every phase a test does NOT stall (F-997). A phase that is meant
#: to answer must never be the one a slow runner cuts off, or the stage the error
#: names is the runner's choice and not the test's (the Windows py3.11 failures:
#: "stalled in the post-launch setup stage" on a healthy spawn, and a regex that
#: wanted "post-launch setup" and read "launch"). Under WAIT_BUDGET so a phase
#: wired to the wrong constant fails fast, in the wrong stage's words.
AMPLE_BUDGET = 20.0

#: How long a healthy double takes to answer, per phase. Small, because it is
#: slept (a slow runner only lengthens it), and nonzero, so a phase has to be held
#: open across the loop for a budget to be able to cut it off.
HEALTHY_LATENCY = 0.02

#: How long a phase that swallows its cancels has, after its deadline, to end.
#: The real phase re-cancels every ``CANCEL_GRACE_SECONDS`` (patched to 0.05 s
#: there), so it ends in a fraction of a second; this is the bound a regression
#: hits instead of a hang.
PHASE_END_BUDGET = 10.0

#: Hard stop for any await on a stalled path: a hang is a failure, not a timeout.
WAIT_BUDGET = 30.0


@pytest.fixture
def registry():
    with nodriver_registry() as live:
        yield live


@pytest.fixture
def table(monkeypatch, registry):
    return ProcessTable(monkeypatch, registry)


@pytest.fixture
def pid_file(tmp_path, monkeypatch):
    path = tmp_path / "browser_pids.json"
    monkeypatch.setattr(process_cleanup, "pid_file", path)
    monkeypatch.setattr(process_cleanup, "browser_processes", {})
    return path


@pytest.fixture(autouse=True)
def _short_budgets(monkeypatch):
    monkeypatch.setattr(spawn_budget, "LAUNCH_BUDGET_SECONDS", AMPLE_BUDGET)
    monkeypatch.setattr(spawn_budget, "POST_LAUNCH_BUDGET_SECONDS", AMPLE_BUDGET)
    monkeypatch.setattr(spawn_exhaustion, "exhaustion_hint", lambda path: None)
    monkeypatch.setattr(spawn_contention, "contention_hint", lambda n: None)
    monkeypatch.setattr(google_rotation_guard, "arm", _no_arm)
    monkeypatch.setattr(
        BrowserManager,
        "_resolve_launch_args",
        lambda self, options, proxy, platform_info: ([], "/fake/chrome", []),
    )
    clone_storage._clear_protected_clone_dirs()
    yield
    clone_storage._clear_protected_clone_dirs()


async def _no_arm(browser) -> None:
    return None


def stall_budget(monkeypatch, name: str) -> None:
    """Make *name* the one short budget: the phase a test stalls on purpose."""
    monkeypatch.setattr(spawn_budget, name, SHORT_BUDGET)


def tighten_after_post_launch(monkeypatch) -> None:
    """``_apply_post_launch`` and ``_setup_dynamic_hooks`` share one budget, so a
    test that stalls the second must not leave the first on a short one: wrap the
    first so the short budget starts only once it has answered. The real, bounded
    phase still runs inside the wrapper, under the ample budget."""
    real = BrowserManager._apply_post_launch

    async def then_tighten(self, *args, **kwargs):
        result = await real(self, *args, **kwargs)
        stall_budget(monkeypatch, "POST_LAUNCH_BUDGET_SECONDS")
        return result

    monkeypatch.setattr(BrowserManager, "_apply_post_launch", then_tighten)


async def _never() -> None:
    await asyncio.Event().wait()


class StalledTab:
    """A tab whose DevTools never answers a window-size command."""

    url = "about:blank"
    type_ = "page"

    async def send(self, *_args, **_kwargs):
        raise RuntimeError("a version probe that fails is swallowed (F-806)")

    async def evaluate(self, *_args, **_kwargs):
        raise RuntimeError("the measurement is guarded and degrades")

    async def set_window_size(self, **_kwargs) -> None:
        await _never()


class LiveTab(StalledTab):
    async def set_window_size(self, **_kwargs) -> None:
        return None


class AnsweringTab(StalledTab):
    """A healthy Chrome's tab: it answers, after a real turn of the loop. A double
    that answers without ever awaiting finishes inside the first step of its phase
    and could not tell a budget that cuts a phase off from one that does not."""

    async def set_window_size(self, **_kwargs) -> None:
        await asyncio.sleep(HEALTHY_LATENCY)


class LaunchedChrome:
    """A launched nodriver ``Browser`` as ``spawn_browser`` holds it: the live
    process, the caller's ``config`` verbatim and a tab. ``stop`` is counted."""

    def __init__(self, config, pid, tab) -> None:
        self.config = config
        self._process = SimpleNamespace(pid=pid, returncode=None)
        self._process_pid = pid
        self.main_tab = tab
        # A launched Chrome with a tab has opened a page (F-1003 reads targets).
        self.targets = [] if tab is None else [tab]
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


def stalling_start(
    monkeypatch, table, registry, pids, *, tab=None, stall=True, latency=0.0
):
    """Replace ``nodriver.start`` with a Chrome that LAUNCHES (a live process in
    the table, registered with nodriver the way ``Browser.start`` registers it
    before it polls) and then either never answers (``stall``) or hands back a
    browser with *tab*. Returns the browsers it registered."""
    seen: list[object] = []
    next_pid = iter(pids)

    async def start(config=None, **_kwargs):
        pid = next(next_pid)
        table.procs[pid] = FakeChrome(pid, config.user_data_dir, time.time())
        browser = LaunchedChrome(config, pid, tab)
        seen.append(browser)
        registry.add(browser if tab is not None else LaunchedBrowser(config, pid))
        if stall:
            await _never()
        if latency:
            await asyncio.sleep(latency)
        return browser

    monkeypatch.setattr(nodriver, "start", start)
    return seen


def clone_options(tmp_path, name: str = "clone-a") -> BrowserOptions:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "Preferences").write_text("{}", encoding="utf-8")
    return BrowserOptions(
        headless=True, user_data_dir=str(directory), auto_clone=True, sandbox=False
    )


async def spawn(manager: BrowserManager, options: BrowserOptions):
    return await asyncio.wait_for(manager.spawn_browser(options), WAIT_BUDGET)


# ---------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------


async def test_a_phase_that_never_returns_names_its_stage_and_budget(monkeypatch):
    monkeypatch.setattr(spawn_budget, "LAUNCH_BUDGET_SECONDS", 0.05)

    @spawn_budget.phase("launch", "LAUNCH_BUDGET_SECONDS")
    async def phase() -> None:
        await _never()

    with pytest.raises(ToolError) as error:
        await asyncio.wait_for(phase(), WAIT_BUDGET)

    assert "launch stage" in str(error.value)
    assert "0.05 s" in str(error.value)


async def test_a_phase_that_answers_is_untouched_and_its_own_timeout_passes_through():
    @spawn_budget.phase("launch", "LAUNCH_BUDGET_SECONDS")
    async def answers(value: int) -> int:
        return value

    @spawn_budget.phase("launch", "LAUNCH_BUDGET_SECONDS")
    async def has_its_own_bound() -> None:
        raise TimeoutError("its own, not ours")

    assert await answers(7) == 7
    with pytest.raises(TimeoutError, match="its own, not ours"):
        await has_its_own_bound()


def test_the_three_spawn_phases_carry_the_bound():
    """A refactor that drops the decorator from a phase puts the hang back; each
    wrapper keeps the original reachable as ``__wrapped__``."""
    for name in ("_launch_browser", "_apply_post_launch", "_setup_dynamic_hooks"):
        assert hasattr(getattr(BrowserManager, name), "__wrapped__"), name


# ---------------------------------------------------------------------------
# One spawn through the real orchestrator
# ---------------------------------------------------------------------------


async def test_a_launch_that_never_answers_fails_in_a_named_stage_and_reaps_its_chrome(
    monkeypatch, table, registry, pid_file, tmp_path
):
    options = clone_options(tmp_path)
    stall_budget(monkeypatch, "LAUNCH_BUDGET_SECONDS")
    stalling_start(monkeypatch, table, registry, [6001])
    manager = BrowserManager()

    with pytest.raises(
        Exception, match=rf"launch stage.*{re.escape(f'{SHORT_BUDGET:g}')} s"
    ):
        await spawn(manager, options)

    # THE one reap (spawn_leak): the Chrome nodriver launched, by its own pid.
    assert table.alive_on(options.user_data_dir) == []
    assert table.procs[6001].terminate_calls == 1
    assert not pid_file.exists(), "a launch that never answered was recorded"
    assert manager._spawns_in_flight == 0


async def test_a_post_launch_exchange_that_never_answers_stops_and_forgets_the_browser(
    monkeypatch, table, registry, pid_file, tmp_path
):
    options = clone_options(tmp_path)
    stall_budget(monkeypatch, "POST_LAUNCH_BUDGET_SECONDS")
    browsers = stalling_start(
        monkeypatch, table, registry, [6002], tab=StalledTab(), stall=False
    )
    manager = BrowserManager()

    with pytest.raises(
        Exception, match=rf"post-launch setup stage.*{re.escape(f'{SHORT_BUDGET:g}')} s"
    ):
        await spawn(manager, options)

    assert [held.stops for held in browsers] == [1], "the held Browser was not stopped"
    assert table.alive_on(options.user_data_dir) == []
    # It WAS tracked (the first thing the phase does), so the record and the
    # disposable clone go through kill_browser_process, the one path.
    assert process_cleanup.browser_processes == {}
    assert not Path(options.user_data_dir).exists(), "the clone's directory leaked"
    assert manager._instances == {}


async def test_a_dynamic_hook_setup_that_never_answers_is_bounded_too(
    monkeypatch, table, registry, pid_file, tmp_path
):
    options = clone_options(tmp_path)
    tighten_after_post_launch(monkeypatch)
    stalling_start(monkeypatch, table, registry, [6003], tab=LiveTab(), stall=False)
    monkeypatch.setattr(dynamic_hook_system, "setup_interception", _never_with_args)
    manager = BrowserManager()

    with pytest.raises(
        Exception,
        match=rf"dynamic-hook setup stage.*{re.escape(f'{SHORT_BUDGET:g}')} s",
    ):
        await spawn(manager, options)

    assert table.alive_on(options.user_data_dir) == []
    assert process_cleanup.browser_processes == {}


async def _never_with_args(*_args, **_kwargs) -> None:
    await _never()


async def test_a_healthy_spawn_is_not_cut_off(
    monkeypatch, table, registry, pid_file, tmp_path
):
    """The budgets are ceilings: a Chrome that answers promptly costs what it costs.

    Every budget is the ample one (the autouse fixture): what is pinned is not
    "this spawn took under N ms", which a loaded runner decides, but that a spawn
    which answers is never turned into the stall error, however the budget is read."""
    options = clone_options(tmp_path)
    stalling_start(
        monkeypatch,
        table,
        registry,
        [6004],
        tab=AnsweringTab(),
        stall=False,
        latency=HEALTHY_LATENCY,
    )
    monkeypatch.setattr(dynamic_hook_system, "setup_interception", _answers_with_args)
    manager = BrowserManager()

    instance = await spawn(manager, options)

    assert instance.instance_id in manager._instances
    assert table.alive_on(options.user_data_dir) == [6004]


async def _answers_with_args(*_args, **_kwargs) -> None:
    await asyncio.sleep(HEALTHY_LATENCY)


# ---------------------------------------------------------------------------
# The tool body's retry loop
# ---------------------------------------------------------------------------


async def test_a_timed_out_attempt_is_retried_and_leaves_nothing_behind(
    monkeypatch, table, registry, pid_file, tmp_session_root, patched_server, call_tool
):
    """Attempt 1 stalls in its launch; attempts 2 and 3 are fresh clones that fail
    in the ordinary way. All three run (the stall is an ordinary failure to
    ``_SPAWN_ATTEMPTS``), and when the loop is spent no Chrome, no record and no
    reservation is left over from ANY of them."""
    monkeypatch.setattr(clone_storage, "_profile_hold", lambda directory: None)
    monkeypatch.setattr(
        clone_storage, "_profile_has_running_browser", lambda directory: False
    )
    launched: list[str] = []
    pids = iter((7001, 7002, 7003))
    stall_budget(monkeypatch, "LAUNCH_BUDGET_SECONDS")

    async def start(config=None, **_kwargs):
        pid = next(pids)
        launched.append(config.user_data_dir)
        table.procs[pid] = FakeChrome(pid, config.user_data_dir, time.time())
        registry.add(LaunchedBrowser(config, pid))
        if pid == 7001:
            # Attempt 1 is the stall and its (short) budget is already running;
            # the retries are meant to fail in the ordinary way, so they get the
            # ample one and a slow runner cannot turn them into a second stall.
            monkeypatch.setattr(spawn_budget, "LAUNCH_BUDGET_SECONDS", AMPLE_BUDGET)
            await _never()
        raise RuntimeError("an ordinary launch failure")

    monkeypatch.setattr(nodriver, "start", start)
    srv = patched_server(browser_manager=BrowserManager())

    with pytest.raises(Exception) as error:
        await asyncio.wait_for(
            call_tool(srv, "spawn_browser", headless=True, sandbox=False), WAIT_BUDGET
        )

    message = str(error.value)
    assert "launch stage" in message and "an ordinary launch failure" in message
    assert len(launched) == 3 and len(set(launched)) == 3, launched
    for directory in launched:
        assert table.alive_on(directory) == [], directory
    assert [proc.terminate_calls for proc in table.procs.values()] == [1, 1, 1]
    assert not pid_file.exists()
    assert not clone_storage._PROTECTED_CLONE_DIRS, "a failed attempt kept its clone"


# ---------------------------------------------------------------------------
# The integration fixtures' wrapper
# ---------------------------------------------------------------------------


async def test_bounded_spawn_names_the_await_a_stalled_spawn_is_parked_on():
    async def spawn_that_never_answers(**_kwargs) -> None:
        await _park_here()

    async def _park_here() -> None:
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError) as error:
        await asyncio.wait_for(
            e2e_helpers.bounded_spawn(
                spawn_that_never_answers, budget=0.2, headless=True
            ),
            WAIT_BUDGET,
        )

    message = str(error.value)
    assert "did not answer within 0.2s" in message
    assert "spawn_that_never_answers" in message and "_park_here" in message


async def test_bounded_spawn_returns_what_a_prompt_spawn_returns():
    async def prompt(**kwargs):
        return {"instance_id": "i-1", **kwargs}

    assert await e2e_helpers.bounded_spawn(prompt, headless=True) == {
        "instance_id": "i-1",
        "headless": True,
    }


def test_the_fixture_bound_sits_between_the_products_launch_budget_and_pytest_timeout():
    """240 s: past one stalled attempt and a retry (120 s + a spawn), under the
    300 s the release gate kills a node at, so the report is ours."""
    assert SHIPPED_LAUNCH_BUDGET < e2e_helpers.SPAWN_BOUND < 300


# ---------------------------------------------------------------------------
# F-969b: the deadline must hold when a cancel is swallowed
# ---------------------------------------------------------------------------


async def _body_whose_inner_wait_for_completes_as_the_deadline_fires() -> None:
    """One post-launch step shaped like ``reconcile_launched_browser_version``: an
    ``asyncio.wait_for`` around work that FINISHES in the same loop iteration the
    phase's deadline fires in. The inner work blocks the loop for longer than the
    budget and then returns, so its done-callback and the deadline's ``cancel()``
    are both ready together, in that order. On Python 3.11 ``wait_for`` then
    swallows the cancellation (gh-86296, fixed in 3.12), the phase carries on, and
    the NEXT step (``_never``) hangs with nothing left to cut it short."""

    async def inner() -> None:
        time.sleep(SHORT_BUDGET * 2)  # noqa: ASYNC251  PERMANENT(the pin needs the loop blocked past the deadline, like a starved one)

    await asyncio.wait_for(inner(), WAIT_BUDGET)
    await _never()


async def test_the_deadline_holds_when_an_inner_wait_for_swallows_the_cancel(
    monkeypatch,
):
    """THE F-969b pin: red on 3.11 against the ``asyncio.timeout``-only phase (the
    CI cell that failed: Windows py3.11), where the phase never raises and the
    next step hangs until ``WAIT_BUDGET`` cancels the whole thing."""

    stall_budget(monkeypatch, "POST_LAUNCH_BUDGET_SECONDS")

    @spawn_budget.phase("post-launch setup", "POST_LAUNCH_BUDGET_SECONDS")
    async def phase() -> None:
        await _body_whose_inner_wait_for_completes_as_the_deadline_fires()

    started = time.monotonic()
    with pytest.raises(ToolError, match="post-launch setup stage"):
        await asyncio.wait_for(phase(), WAIT_BUDGET)
    assert time.monotonic() - started < WAIT_BUDGET / 2


async def test_an_outer_cancellation_cancels_the_phase_and_waits_it_out():
    reached = asyncio.Event()
    finished = asyncio.Event()

    @spawn_budget.phase("launch", "LAUNCH_BUDGET_SECONDS")
    async def phase() -> None:
        reached.set()
        try:
            await _never()
        finally:
            finished.set()

    caller = asyncio.ensure_future(phase())
    await asyncio.wait_for(reached.wait(), WAIT_BUDGET)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, WAIT_BUDGET)

    assert finished.is_set(), (
        "the phase's own cleanup did not run before the caller left"
    )


async def test_a_phase_that_swallows_every_cancel_still_ends_in_a_named_error(
    monkeypatch,
):
    """A phase that ignores cancellation twice before it gives in is re-cancelled
    until it ends, and the caller still gets the named error."""
    seen = {"cancels": 0}
    released = False
    stall_budget(monkeypatch, "LAUNCH_BUDGET_SECONDS")

    @spawn_budget.phase("launch", "LAUNCH_BUDGET_SECONDS")
    async def stubborn() -> None:
        while True:
            try:
                await _never()
            except asyncio.CancelledError:
                seen["cancels"] += 1
                if seen["cancels"] >= 3 or released:
                    raise

    monkeypatch.setattr(spawn_budget, "CANCEL_GRACE_SECONDS", 0.05)
    # `asyncio.wait`, not `wait_for`: wait_for would add a cancel of its own and
    # then wait out a phase that swallows it, which is the hang under test. Against
    # a phase that cancels once (asyncio.timeout alone) the third cancel never
    # comes, so the bound has to be ours.
    caller = asyncio.ensure_future(stubborn())
    done, _ = await asyncio.wait({caller}, timeout=PHASE_END_BUDGET)
    if not done:
        released = True  # let the stubborn body give in so nothing leaks
        caller.cancel()
        await asyncio.wait({caller}, timeout=WAIT_BUDGET)
        pytest.fail(
            f"the phase was still running {PHASE_END_BUDGET:g}s after its deadline "
            f"({seen['cancels']} cancels delivered): it is not re-cancelled until it ends"
        )
    with pytest.raises(ToolError, match="launch stage"):
        caller.result()
    assert seen["cancels"] == 3
