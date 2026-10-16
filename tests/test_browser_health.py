"""F-963 -- a persistent-profile browser that stopped answering is healed.

Two long-lived named-session browsers (``default`` and ``fleet``) froze at ~26.5 h:
Chrome's HTTP endpoints answered, ``Runtime.evaluate`` never returned in any tab,
``Browser.close`` replied ok and the process stayed up. ``browser_health`` probes
each persistent-profile browser over a FRESH websocket (never nodriver's own
connection), and after ``FAILURES_BEFORE_HEAL`` consecutive failures closes it
through the existing close path and relaunches it under the SAME instance id.

Hermetic: a real ``BrowserManager`` over ``FakeBrowser``s; the probe, the close
path, the launch and the forensics are the keyword seams the module offers. The
wire protocol of the real probe is pinned against an in-process loopback
websocket server that speaks the four CDP commands, so no Chrome is involved.
"""

import asyncio
import contextvars
import json
import os
import re
import time
from types import SimpleNamespace

import psutil
import pytest
from websockets.asyncio.server import serve

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import (
    browser_health,
    directory_gate,
    fleet_session,
    freeze_capture,
    tab_binding,
)
from stealth_chrome_devtools_mcp.embedded import browser_pid_registry as registry
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

DIRECTORY = "C:/sessions/fleet"
INSTANCE_ID = "i-fleet"
OLD_PID = 4242
SELECTION = {"user_data_dir": DIRECTORY, "profile_role": "explicit"}


class _Log:
    """``debug_logger`` as the module sees it: every line, by level."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []

    def log_info(self, _component, method, message, *_a, **_k):
        self.lines.append(("info", method, str(message)))

    def log_warning(self, _component, method, message, *_a, **_k):
        self.lines.append(("warning", method, str(message)))

    def log_error(self, _component, method, error, *_a, **_k):
        self.lines.append(("error", method, f"{type(error).__name__}: {error}"))

    def at(self, level: str) -> list[str]:
        return [message for lvl, _m, message in self.lines if lvl == level]


@pytest.fixture()
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(browser_health, "debug_logger", recorder)
    return recorder


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(tab_binding, "_bound", {})
    monkeypatch.setattr(directory_gate, "_gates", {})


def _browser(instance_id: str) -> FakeBrowser:
    browser = FakeBrowser(alive=True)
    browser.websocket_url = f"ws://127.0.0.1:1/devtools/browser/{instance_id}"
    return browser


def _install(manager: BrowserManager, instance_id: str = INSTANCE_ID, **options):
    manager._instances[instance_id] = {
        "browser": _browser(instance_id),
        "tab": FakeTab(target_id=f"T-{instance_id}"),
        "instance": BrowserInstance(
            instance_id=instance_id,
            headless=True,
            viewport={"width": 1280, "height": 720},
        ),
        "options": BrowserOptions(
            user_data_dir=options.pop("user_data_dir", DIRECTORY), **options
        ),
        "navigation_count": 0,
    }
    manager._spawn_diagnostics[instance_id] = {"profile_selection": dict(SELECTION)}


class _Stage:
    """The collaborators of one health loop, recording what the heal does."""

    def __init__(self, manager: BrowserManager, monkeypatch, *, ids=(INSTANCE_ID,)):
        self.manager = manager
        self.persistent = set(ids)
        self.now = 1000.0
        self.probe_results: dict[str, list[bool]] = {}
        self.probes: list[str] = []
        self.closes: list[str] = []
        self.close_sessions: list[str | None] = []
        self.launches: list[tuple[BrowserOptions, str | None]] = []
        self.armed: list[tuple[str, str, object]] = []
        self.gate_held_during: list[str] = []
        self.heals: list[str] = []
        self.launch_error: BaseException | None = None
        self.close_kills = True
        self.waits: list[tuple[int | None, float]] = []
        # F-993: every seam the heal touches, in the order it touched them.
        self.events: list[str] = []
        self.captures: list[dict] = []
        self.capture_record = "cpu(3.0s: browser=0.00s) minidump(skipped: platform)"
        self.capture_error: BaseException | None = None
        self.capture_seconds = 0.0
        self.late_dumps: list[tuple[int, str]] = []
        self.healed_labels: list[str] = []
        monkeypatch.setattr(manager, "spawn_browser", self._spawn)

    def _held(self) -> bool:
        return directory_gate.gate_for(DIRECTORY)._lock.locked()

    async def probe(self, url: str, steps: list[str]) -> None:
        # The browser under probe is named by the instance whose endpoint this is.
        iid = next(
            i
            for i, d in self.manager._instances.items()
            if d["browser"].websocket_url == url
        )
        self.probes.append(iid)
        self.events.append("probe")
        queue = self.probe_results.get(iid, [])
        steps.append("evaluate")
        if queue and not queue.pop(0):
            raise ConnectionError("no answer")

    async def close(self, instance_id: str, session: str | None = None) -> dict:
        self.closes.append(instance_id)
        self.events.append("close")
        self.close_sessions.append(session)
        if self._held():
            self.gate_held_during.append("close")
        data = self.manager._instances.pop(instance_id)
        self.manager._spawn_diagnostics.pop(instance_id, None)
        if self.close_kills:
            data["browser"]._process = SimpleNamespace(poll=lambda: 0, returncode=0)
        return {"closed": True}

    async def _spawn(self, options: BrowserOptions, *, instance_id: str | None = None):
        self.launches.append((options, instance_id))
        self.events.append("spawn")
        if self._held():
            self.gate_held_during.append("spawn")
        if self.launch_error is not None:
            raise self.launch_error
        _install(
            self.manager,
            instance_id or "i-new",
            user_data_dir=options.user_data_dir,
            block_resources=options.block_resources,
            guard_rotation=options.guard_rotation,
        )
        self.manager._instances[instance_id]["instance"].headless = options.headless
        self.manager._spawn_diagnostics[instance_id] = {}
        self.manager._instances[instance_id][
            "browser"
        ].websocket_url = f"ws://127.0.0.1:1/devtools/browser/{instance_id}-relaunch-{len(self.launches)}"
        return self.manager._instances[instance_id]["instance"]

    async def arm(self, tab, instance_id, block_resources):
        self.events.append("arm")
        self.armed.append((tab.target.target_id, instance_id, block_resources))

    async def capture(self, **facts) -> str:
        self.events.append("capture")
        self.captures.append(facts)
        if self.capture_seconds:
            await asyncio.sleep(self.capture_seconds)
        if self.capture_error is not None:
            raise self.capture_error
        self.events.append("capture-done")
        return self.capture_record

    async def dump_now(self, pid: int, label: str, **_facts) -> str:
        self.events.append("late-dump")
        self.late_dumps.append((pid, label))
        return "C:/dumps/late.dmp (1.4 MB in 0.1s, type=0x1924)"

    def mark_healed(self, label: str) -> str | None:
        self.events.append("mark-healed")
        self.healed_labels.append(label)
        return f"C:/dumps/{label}-20261016T000000Z-healed.dmp"

    async def forensics(self, **facts) -> str:
        return f"uptime={facts['uptime_seconds']:.0f}s targets(page=1 other=0)"

    async def wait_exit(self, pid, timeout):
        self.events.append("wait")
        self.waits.append((pid, timeout))
        return SimpleNamespace(exited=self.close_kills)

    async def own_ok(self, _browser) -> None:
        return None

    def health(self, **overrides) -> browser_health.BrowserHealth:
        kwargs = {
            "close": self.close,
            "arm_interception": self.arm,
            "probe": self.probe,
            "own_check": self.own_ok,
            "persistent": lambda iid: iid in self.persistent,
            "forensics": self.forensics,
            "capture": self.capture,
            "dump_now": self.dump_now,
            "mark_healed": self.mark_healed,
            "browser_pid": lambda _process, _pid: OLD_PID,
            "wait_exit": self.wait_exit,
            "clock": lambda: self.now,
        }
        kwargs.update(overrides)
        return browser_health.BrowserHealth(self.manager, **kwargs)


@pytest.fixture()
def stage(monkeypatch, log):
    manager = BrowserManager()
    _install(manager)
    return _Stage(manager, monkeypatch)


# ---------------------------------------------------------------------------
# 1. when it heals, and when it does not
# ---------------------------------------------------------------------------


class TestWhenItHeals:
    async def test_one_failed_probe_warns_and_does_not_heal(self, stage, log):
        stage.probe_results[INSTANCE_ID] = [False]
        health = stage.health()

        await health.check_once()

        assert stage.closes == [] and stage.launches == []
        (warning,) = [w for w in log.at("warning") if "probe 1 failed" in w]
        assert "probe 1 failed" in warning and "uptime=" in warning
        assert log.at("error") == []

    async def test_two_consecutive_failures_heal(self, stage, log):
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert stage.closes == [INSTANCE_ID]
        assert [iid for _o, iid in stage.launches] == [INSTANCE_ID]
        (error,) = log.at("error")
        assert error.startswith("HungBrowserError") and "healing" in error
        assert "targets(page=1 other=0)" in error
        assert any("healed" in line for line in log.at("info"))

    async def test_a_pass_between_two_failures_resets_the_count(self, stage):
        stage.probe_results[INSTANCE_ID] = [False, True, False]
        health = stage.health()

        for _ in range(3):
            await health.check_once()

        assert stage.closes == []

    async def test_the_threshold_is_two(self):
        assert browser_health.FAILURES_BEFORE_HEAL == 2
        assert browser_health.PROBE_INTERVAL_SECONDS == 120
        assert browser_health.PROBE_TIMEOUT_SECONDS == 15
        assert browser_health.HEAL_COOLDOWN_SECONDS == 900


class TestCooldown:
    async def test_a_second_heal_waits_out_the_cooldown(self, stage, log):
        stage.probe_results[INSTANCE_ID] = [False, False, False, False, False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()  # heals
        assert len(stage.closes) == 1

        stage.now += browser_health.HEAL_COOLDOWN_SECONDS - 1
        await health.check_once()
        await health.check_once()  # two more failures, inside the cooldown
        assert len(stage.closes) == 1
        assert any("not again within" in line for line in log.at("warning"))

        stage.now += 2
        await health.check_once()
        await health.check_once()
        assert len(stage.closes) == 2


class TestScope:
    async def test_clones_are_skipped(self, stage):
        stage.persistent.clear()
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert stage.probes == [] and stage.closes == []

    def test_the_default_predicate_is_on_persistent_profile(self, monkeypatch):
        from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

        named = registry.new_entry(
            1,
            create_time=None,
            user_data_dir=DIRECTORY,
            uses_custom_data_dir=True,
            auto_clone=False,
        )
        clone = registry.new_entry(
            2,
            create_time=None,
            user_data_dir="C:/c",
            uses_custom_data_dir=True,
            auto_clone=True,
        )
        monkeypatch.setattr(
            process_cleanup, "browser_processes", {"named": named, "clone": clone}
        )

        assert browser_health._is_persistent("named") is True
        assert browser_health._is_persistent("clone") is False
        assert browser_health._is_persistent("untracked") is False


# ---------------------------------------------------------------------------
# 2. the heal itself
# ---------------------------------------------------------------------------


class TestTheHealNamesTheSharedSession:
    """F-966: ``close_instance`` refuses the ``default`` and ``fleet`` browsers
    unless told which one, and the heal closes THROUGH it, so it must say."""

    @pytest.mark.parametrize(
        ("where", "session"),
        [("master", "default"), ("fleet", "fleet"), ("work", None)],
    )
    async def test_the_close_is_told_which_session_and_the_relaunch_keeps_it(
        self, stage, tmp_session_root, where, session
    ):
        directory = (
            tmp_session_root["master"]
            if where == "master"
            else tmp_session_root["sessions"] / where
        )
        _install(stage.manager, "i-x", user_data_dir=str(directory))
        stage.persistent.add("i-x")
        stage.probe_results["i-x"] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert stage.closes == ["i-x"]
        assert stage.close_sessions == [session]
        ((options, instance_id),) = stage.launches
        assert instance_id == "i-x"
        # The relaunch NAMES its directory, so it never goes through the resolver,
        # where a spawn naming nothing is a clone (F-966): master stays master.
        assert options.user_data_dir == str(directory)
        assert options.auto_clone is False


class TestTheHeal:
    async def test_it_keeps_the_instance_id_and_the_launch_shape(self, stage):
        _install(stage.manager, block_resources=["*.png"])
        stage.manager._instances[INSTANCE_ID]["instance"].viewport = {
            "width": 1111,
            "height": 777,
        }
        tab_binding._bound[("claude-session", INSTANCE_ID)] = "T-old"
        tab_binding._bound[("claude-session", "someone-else")] = "T-keep"
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        ((options, instance_id),) = stage.launches
        assert instance_id == INSTANCE_ID
        assert (options.user_data_dir, options.headless) == (DIRECTORY, True)
        assert (options.viewport_width, options.viewport_height) == (1111, 777)
        live = [i.instance_id for i in await stage.manager.list_instances()]
        assert live == [INSTANCE_ID], "the same id, and no second instance"
        assert stage.armed == [(f"T-{INSTANCE_ID}", INSTANCE_ID, ["*.png"])]
        assert ("claude-session", INSTANCE_ID) not in tab_binding._bound
        assert ("claude-session", "someone-else") in tab_binding._bound
        # close_instance reads the profile role from here for the `default` seed
        diagnostics = await stage.manager.get_spawn_diagnostics(INSTANCE_ID)
        assert diagnostics["profile_selection"] == SELECTION

    async def test_it_holds_the_directory_gate_for_the_whole_heal(self, stage):
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()
        await health.check_once()

        spawn_waited = []

        async def a_concurrent_spawn():
            gate = await directory_gate.hold(DIRECTORY)
            spawn_waited.append(len(stage.launches))
            gate.release()

        # The racer starts INSIDE the heal, once the gate is held: started before
        # `check_once`, when it reaches the gate depends on how long the
        # forensics' worker threads take, and on Linux CI it won the gate first.
        racers: list[asyncio.Task[None]] = []
        close = health._close

        async def close_while_a_spawn_races(
            instance_id: str, session: str | None = None
        ) -> object:
            # An EMPTY context, as another request's task has: the gate is
            # reentrant per context, and a copy of this one already holds it.
            racers.append(
                asyncio.create_task(a_concurrent_spawn(), context=contextvars.Context())
            )
            await asyncio.sleep(0)  # the racer runs to the gate and waits there
            return await close(instance_id, session)

        health._close = close_while_a_spawn_races
        await health.check_once()  # the second failure: heals
        await asyncio.gather(*racers)

        assert len(racers) == 1, "the heal closed once"
        assert stage.gate_held_during == ["close", "spawn"]
        assert spawn_waited == [1], "the racer got the gate only after the relaunch"
        assert not directory_gate.gate_for(DIRECTORY)._lock.locked()

    async def test_it_waits_for_the_pid_it_took_before_the_close(self, stage):
        """``close_instance`` offloads its kill and nodriver drops its process
        handle, so only the pid read BEFORE the close can witness the exit."""
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert stage.waits == [(OLD_PID, browser_health.EXIT_WAIT_SECONDS)]

    async def test_the_gate_is_released_when_the_relaunch_fails(self, stage, log):
        stage.launch_error = RuntimeError("no chrome")
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert not directory_gate.gate_for(DIRECTORY)._lock.locked()
        assert INSTANCE_ID not in stage.manager._instances, "left closed"
        assert any("RuntimeError: no chrome" in line for line in log.at("error"))

    async def test_a_browser_the_close_path_did_not_end_is_not_relaunched_over(
        self, stage, log
    ):
        stage.close_kills = False
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert stage.launches == []
        assert any("left the browser running" in line for line in log.at("error"))

    async def test_a_relaunch_that_still_fails_its_probe_is_an_error(self, stage, log):
        # failure 1, failure 2 (heals), then the re-probe of the new browser
        stage.probe_results[INSTANCE_ID] = [False, False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert any("relaunched but the probe still fails" in e for e in log.at("error"))
        assert not any("healed" in line for line in log.at("info"))

    @pytest.mark.parametrize("decided", [True, False])
    async def test_the_rotation_guard_decision_is_the_spawn_paths(
        self, stage, monkeypatch, decided
    ):
        seen = []

        def guards_rotation(selection):
            seen.append(selection)
            return decided

        monkeypatch.setattr(fleet_session, "guards_rotation", guards_rotation)
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        ((options, _id),) = stage.launches
        assert options.guard_rotation is decided
        assert seen == [{"profile_role": "explicit", "user_data_dir": DIRECTORY}]

    async def test_a_guard_the_spawn_set_is_kept(self, monkeypatch, log):
        manager = BrowserManager()
        _install(manager, guard_rotation=True)
        stage = _Stage(manager, monkeypatch)
        monkeypatch.setattr(fleet_session, "guards_rotation", lambda _s: False)
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert stage.launches[0][0].guard_rotation is True


# ---------------------------------------------------------------------------
# 3. the probe is bounded; the loop survives
# ---------------------------------------------------------------------------


class TestBounded:
    async def test_a_probe_that_hangs_is_bounded(self, stage, monkeypatch, log):
        monkeypatch.setattr(browser_health, "PROBE_TIMEOUT_SECONDS", 0.05)

        async def hangs(_url, steps):
            steps.append("evaluate")
            await asyncio.Event().wait()

        health = stage.health(probe=hangs)

        await asyncio.wait_for(health.check_once(), timeout=5)

        # the capture starts after the probe's line is logged and awaits nothing in
        # this round, so its own record line cannot fall inside it
        (warning,) = log.at("warning")
        assert "TimeoutError at evaluate" in warning

    async def test_a_forensics_gatherer_that_hangs_does_not_block_the_heal(
        self, stage, monkeypatch
    ):
        monkeypatch.setattr(browser_health, "FORENSICS_TIMEOUT_SECONDS", 0.05)

        async def hangs(**_facts):
            await asyncio.Event().wait()

        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health(forensics=hangs)

        await asyncio.wait_for(health.check_once(), timeout=5)
        await asyncio.wait_for(health.check_once(), timeout=5)

        assert stage.closes == [INSTANCE_ID]


class TestTheLoopSurvives:
    async def test_an_exception_in_one_instances_round_costs_only_that_instance(
        self, monkeypatch, log
    ):
        manager = BrowserManager()
        _install(manager, "i-a")
        _install(manager, "i-b", user_data_dir="C:/sessions/other")
        stage = _Stage(manager, monkeypatch, ids=("i-a", "i-b"))
        stage.probe_results["i-a"] = [False, False]

        async def broken_heal(_instance_id):
            raise RuntimeError("heal blew up")

        health = stage.health(heal=broken_heal)
        await health.check_once()
        await health.check_once()

        assert "i-b" in stage.probes, "the other instance was still probed"
        assert any("heal blew up" in line for line in log.at("error"))

    async def test_run_keeps_going_after_a_round_raises(self, stage, monkeypatch, log):
        monkeypatch.setattr(browser_health, "PROBE_INTERVAL_SECONDS", 0.01)
        rounds = []

        async def list_instances():
            rounds.append(1)
            if len(rounds) == 1:
                raise RuntimeError("manager hiccup")
            return []

        monkeypatch.setattr(stage.manager, "list_instances", list_instances)
        task = asyncio.create_task(stage.health().run())
        try:
            await asyncio.wait_for(_until(lambda: len(rounds) >= 3), timeout=5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert any("manager hiccup" in line for line in log.at("error"))

    async def test_start_is_idempotent_and_stop_ends_the_loop(self, stage):
        browser_health.start(
            stage.manager, close=stage.close, arm_interception=stage.arm
        )
        first = browser_health._task
        browser_health.start(
            stage.manager, close=stage.close, arm_interception=stage.arm
        )

        assert browser_health._task is first and not first.done()
        await browser_health.stop()
        assert first.cancelled() and browser_health._task is None
        await browser_health.stop()  # and again, harmlessly


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# 4. the backend's own connection is its own finding
# ---------------------------------------------------------------------------


class TestOwnConnection:
    async def test_a_wedged_own_connection_is_a_distinct_warning_and_no_heal(
        self, stage, log
    ):
        async def wedged(_browser):
            raise TimeoutError

        health = stage.health(own_check=wedged)

        for _ in range(3):
            await health.check_once()

        assert stage.closes == [] and stage.launches == []
        assert all("backend connection wedged" in w for w in log.at("warning"))
        assert len(log.at("warning")) == 3
        assert log.at("error") == []

    async def test_the_own_check_is_skipped_when_the_direct_probe_fails(self, stage):
        called = []

        async def own(_browser):
            called.append(1)

        stage.probe_results[INSTANCE_ID] = [False]
        await stage.health(own_check=own).check_once()

        assert called == []


# ---------------------------------------------------------------------------
# 5. forensics: counts and shapes, never a URL
# ---------------------------------------------------------------------------

SECRET_INFOS = [
    {
        "type": "page",
        "url": "https://bank.example/account?token=SECRET",
        "title": "Bank",
    },
    {"type": "page", "url": "https://mail.example/inbox", "title": "Inbox (3)"},
    {"type": "service_worker", "url": "https://mail.example/sw.js", "title": "sw"},
]


class TestForensics:
    def test_targets_are_counted_not_named(self):
        text = browser_health.describe_targets(SECRET_INFOS)

        assert text == "page=2 other=1"

    async def test_the_whole_line_carries_no_url_title_or_cookie(self, monkeypatch):
        async def fetch(_url):
            return SECRET_INFOS

        monkeypatch.setattr(
            browser_health,
            "machine_facts",
            lambda: "chromium_processes=61 free_ram=3.10GB",
        )

        text = await browser_health.gather_forensics(
            uptime_seconds=95400,
            websocket_url="ws://127.0.0.1:9222/devtools/browser/guid",
            pid=os.getpid(),
            fetch_targets=fetch,
        )

        assert text.startswith("uptime=95400s targets(page=2 other=1) tree(")
        assert "chromium_processes=61 free_ram=3.10GB" in text
        for leaked in ("http", "://", "SECRET", "Bank", "Inbox", "token", "ws:"):
            assert leaked not in text

    async def test_a_part_that_fails_is_named_by_type_only(self, monkeypatch):
        async def fetch(_url):
            raise ConnectionRefusedError("ws://127.0.0.1:9222 secret-guid")

        monkeypatch.setattr(browser_health, "machine_facts", lambda: "m")

        text = await browser_health.gather_forensics(
            uptime_seconds=1, websocket_url="ws://x", pid=None, fetch_targets=fetch
        )

        assert "targets=unavailable (ConnectionRefusedError)" in text
        assert "secret-guid" not in text and "ws://" not in text
        assert "tree(unavailable (no pid))" in text

    def test_the_process_tree_groups_by_type(self, monkeypatch):
        procs = {
            10: ["chrome.exe", "--user-data-dir=C:/p"],
            11: ["chrome.exe", "--type=renderer"],
            12: ["chrome.exe", "--type=renderer"],
            13: ["chrome.exe", "--type=gpu-process"],
            14: ["chrome.exe", "--type=https://evil.example/leak"],
        }

        class Proc:
            def __init__(self, pid):
                self.pid = pid

            def children(self, recursive=False):
                return [Proc(p) for p in procs if p != self.pid]

            def memory_info(self):
                return SimpleNamespace(rss=2**30)

        monkeypatch.setattr(browser_health.psutil, "Process", Proc)
        monkeypatch.setattr(
            browser_health.browser_cmdline, "arguments", lambda pid: procs[pid]
        )

        text = browser_health.process_tree(10)

        assert text == "browser=1 gpu-process=1 other=1 renderer=2 rss=5.00GB"


# ---------------------------------------------------------------------------
# 6. the real probe, over a loopback websocket that speaks the four commands
# ---------------------------------------------------------------------------


class _FakeChrome:
    """A browser-level CDP endpoint on loopback. ``evaluate`` picks the answer."""

    def __init__(self, evaluate="ok") -> None:
        self.evaluate = evaluate
        self.requests: list[dict] = []

    async def handler(self, ws) -> None:
        async for raw in ws:
            request = json.loads(raw)
            self.requests.append(request)
            method = request["method"]
            reply: dict = {"id": request["id"], "result": {}}
            if method == "Target.createTarget":
                reply["result"] = {"targetId": "T-probe"}
            elif method == "Target.attachToTarget":
                reply["result"] = {"sessionId": "S-probe"}
            elif method == "Runtime.evaluate":
                if self.evaluate == "hang":
                    continue
                value = 2 if self.evaluate == "ok" else 3
                reply["result"] = {"result": {"type": "number", "value": value}}
            elif method == "Target.getTargets":
                reply["result"] = {"targetInfos": SECRET_INFOS}
            await ws.send(json.dumps({"method": "Target.targetCreated", "params": {}}))
            await ws.send(json.dumps(reply))

    @property
    def methods(self) -> list[str]:
        return [r["method"] for r in self.requests]


@pytest.fixture()
async def chrome():
    fake = _FakeChrome()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        fake.url = f"ws://127.0.0.1:{port}/devtools/browser/x"
        yield fake


class TestTheRealProbe:
    async def test_it_opens_evaluates_and_closes_a_background_blank_target(
        self, chrome
    ):
        steps: list[str] = []

        await browser_health.probe_browser(chrome.url, steps)

        assert chrome.methods == [
            "Target.createTarget",
            "Target.attachToTarget",
            "Runtime.evaluate",
            "Target.closeTarget",
        ]
        create, attach, evaluate, close = chrome.requests
        assert create["params"] == {"url": "about:blank", "background": True}
        assert attach["params"] == {"targetId": "T-probe", "flatten": True}
        assert evaluate["sessionId"] == "S-probe"
        assert evaluate["params"] == {"expression": "1+1", "returnByValue": True}
        assert close["params"] == {"targetId": "T-probe"}
        assert steps == ["createTarget", "attachToTarget", "evaluate", "closeTarget"]

    async def test_a_wrong_answer_fails_and_still_closes_the_target(self, chrome):
        chrome.evaluate = "wrong"

        with pytest.raises(browser_health.ProbeError):
            await browser_health.probe_browser(chrome.url, [])

        assert chrome.methods[-1] == "Target.closeTarget"

    async def test_a_hung_evaluate_is_cut_off_and_the_close_is_still_tried(
        self, chrome
    ):
        chrome.evaluate = "hang"
        steps: list[str] = []

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(browser_health.probe_browser(chrome.url, steps), 0.3)

        assert steps[-2:] == ["evaluate", "closeTarget"]
        assert chrome.methods[-1] == "Target.closeTarget"

    async def test_nothing_listening_is_a_failure_not_a_hang(self):
        with pytest.raises(OSError):
            await browser_health.probe_browser(
                "ws://127.0.0.1:1/devtools/browser/x", []
            )

    async def test_the_connection_ignores_a_system_proxy(self, monkeypatch, chrome):
        seen = {}
        real = browser_health.websockets.asyncio.client.connect

        def spy(url, **options):
            seen.update(options)
            return real(url, **options)

        monkeypatch.setattr(browser_health.websockets.asyncio.client, "connect", spy)

        await browser_health.probe_browser(chrome.url, [])

        assert seen["proxy"] is None
        assert seen["max_size"] is None and seen["ping_interval"] is None

    async def test_get_targets_counts_what_chrome_lists(self, chrome):
        infos = await browser_health.fetch_target_infos(chrome.url)

        assert browser_health.describe_targets(infos) == "page=2 other=1"


# ---------------------------------------------------------------------------
# 7. spawn_browser(instance_id=...) is the seam the heal relaunches through
# ---------------------------------------------------------------------------


class TestSpawnUnderAGivenId:
    @pytest.fixture()
    def spawns(self, monkeypatch, tmp_path):
        """The REAL ``spawn_browser`` pipeline up to a launch that fails, with
        ``_build_instance`` spying on the id it is handed."""
        from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

        built: list[str] = []
        real_build = BrowserManager._build_instance

        def build(self, instance_id, options):
            built.append(instance_id)
            return real_build(self, instance_id, options)

        async def failing_launch(self, options, browser_executable, args, attempt):
            raise RuntimeError("launch refused")

        monkeypatch.setattr(BrowserManager, "_build_instance", build)
        monkeypatch.setattr(BrowserManager, "_launch_browser", failing_launch)
        monkeypatch.setattr(
            BrowserManager,
            "_resolve_launch_args",
            lambda self, options, proxy, platform_info: ([], "/fake/chrome", []),
        )
        monkeypatch.setattr(process_cleanup, "pid_file", tmp_path / "browser_pids.json")
        return built

    async def test_a_given_id_is_the_instance_id(self, spawns):
        with pytest.raises(Exception, match="launch refused"):
            await BrowserManager().spawn_browser(
                BrowserOptions(user_data_dir=DIRECTORY), instance_id="i-kept"
            )

        assert spawns == ["i-kept"]

    async def test_without_one_it_is_a_fresh_uuid(self, spawns):
        for _ in range(2):
            with pytest.raises(Exception, match="launch refused"):
                await BrowserManager().spawn_browser(BrowserOptions())

        assert len(set(spawns)) == 2 and all(len(i) == 36 for i in spawns)

    def test_the_parameter_is_keyword_only(self):
        import inspect

        parameter = inspect.signature(BrowserManager.spawn_browser).parameters[
            "instance_id"
        ]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# 7. F-993: the frozen state is captured at the first failed probe, before any heal
# ---------------------------------------------------------------------------


async def _settled() -> None:
    """Let a capture task that is not waiting on anything run to its end. How many loop
    iterations that takes differs by interpreter (3.11's ``wait_for`` spends one more),
    so the yields are generous: a task with nothing to wait on ends in the first few
    and the rest cost nothing."""
    for _ in range(50):
        await asyncio.sleep(0)


class TestTheFrozenStateIsCaptured:
    async def test_it_is_taken_once_per_incident_and_before_the_heal(self, stage, log):
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await _settled()
        assert stage.events == ["probe", "capture", "capture-done"]  # nothing closed

        await health.check_once()
        # the heal's own order, with no second capture in it, and the dump marked
        # healed before the browser is closed
        assert stage.events == [
            "probe",
            "capture",
            "capture-done",
            "probe",
            "mark-healed",
            "close",
            "wait",
            "spawn",
            "arm",
            "probe",
        ]

    async def test_the_first_failure_warning_says_capturing_and_nothing_more(
        self, stage, log
    ):
        stage.probe_results[INSTANCE_ID] = [False]

        await stage.health().check_once()

        (warning,) = [w for w in log.at("warning") if "probe 1 failed" in w]
        assert warning.endswith("evidence@probe1(capturing)")
        assert stage.capture_record not in warning  # the round did not wait for it

    async def test_the_record_is_logged_the_moment_the_capture_finishes(
        self, stage, log
    ):
        stage.probe_results[INSTANCE_ID] = [False]

        await stage.health().check_once()
        await _settled()

        assert f"{INSTANCE_ID}: evidence@probe1({stage.capture_record})" in log.at(
            "warning"
        )

    async def test_one_incident_writes_two_warning_lines_in_a_fixed_order(
        self, stage, log
    ):
        """BY DESIGN the record has its own line (logged when the capture task ends);
        the first-failure line only says it is being captured. The order is the
        design's, not the interpreter's: a capture that finishes instantly (stage's
        does) still logs after the line that announces it, because the task starts
        right after that line with nothing awaited between. The forensics gatherer
        here SUSPENDS (the real one does: a thread hop and a websocket), which is what
        lets an incident started BEFORE it finish AHEAD of the line that announces it,
        so a capture moved back before the facts goes red on every interpreter."""
        stage.probe_results[INSTANCE_ID] = [False]

        async def suspends(**facts) -> str:
            for _ in range(30):
                await asyncio.sleep(0)
            return await stage.forensics(**facts)

        await stage.health(forensics=suspends).check_once()
        await _settled()

        warnings = log.at("warning")
        assert len(warnings) == 2, warnings
        assert "probe 1 failed" in warnings[0]
        assert warnings[0].endswith("evidence@probe1(capturing)")
        assert warnings[1] == f"{INSTANCE_ID}: evidence@probe1({stage.capture_record})"

    async def test_the_heal_error_line_carries_it_labelled_with_its_age(
        self, stage, log
    ):
        """The record was taken at the FIRST failed probe, about two minutes before
        the heal; the line that reaches Sentry must not read as if it were current."""
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        (error,) = log.at("error")
        assert "healing it" in error
        assert f"evidence@probe1({stage.capture_record})" in error
        assert "evidence(" not in error.replace("evidence@probe1(", "")

    async def test_the_heal_error_line_names_the_kept_dump(self, stage, log):
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        (error,) = log.at("error")
        assert "dump_kept(C:/dumps/freeze-" in error and "-healed.dmp)" in error

    async def test_it_is_handed_the_browser_pid_a_label_and_a_fresh_connection_check(
        self, stage
    ):
        stage.probe_results[INSTANCE_ID] = [False]

        await stage.health().check_once()
        await _settled()

        (facts,) = stage.captures
        assert facts["pid"] == OLD_PID
        assert facts["label"].startswith("freeze-")
        assert facts["label"].endswith(f"-{INSTANCE_ID[:8]}")
        assert callable(facts["answers"])

    async def test_a_healthy_browser_costs_no_capture(self, stage):
        health = stage.health()

        for _ in range(3):
            await health.check_once()

        assert stage.captures == []

    async def test_a_pass_between_failures_ends_the_incident(self, stage):
        stage.probe_results[INSTANCE_ID] = [False, True, False]
        health = stage.health()

        for _ in range(3):
            await health.check_once()
            await _settled()

        assert len(stage.captures) == 2
        assert stage.closes == []

    async def test_a_failure_after_a_heal_is_a_new_incident_with_its_own_capture(
        self, stage
    ):
        stage.probe_results[INSTANCE_ID] = [False] * 6
        health = stage.health()

        await health.check_once()  # incident 1: capture
        await health.check_once()  # heals
        stage.probe_results[INSTANCE_ID] = [False] * 3
        await health.check_once()  # the relaunched browser fails at once: incident 2
        await health.check_once()  # inside the cooldown: warned, not healed again
        await health.check_once()
        await _settled()

        assert len(stage.captures) == 2
        assert len(stage.closes) == 1

    async def test_a_capture_that_raises_costs_the_heal_nothing(self, stage, log):
        stage.capture_error = RuntimeError("dump blew up")
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert (
            f"{INSTANCE_ID}: evidence@probe1(capture unavailable (RuntimeError))"
            in log.at("warning")
        )
        assert stage.closes == [INSTANCE_ID]
        assert [iid for _o, iid in stage.launches] == [INSTANCE_ID]

    async def test_the_thresholds_and_the_cooldown_are_unchanged(self):
        assert browser_health.FAILURES_BEFORE_HEAL == 2
        assert browser_health.PROBE_INTERVAL_SECONDS == 120
        assert browser_health.PROBE_TIMEOUT_SECONDS == 15
        assert browser_health.HEAL_COOLDOWN_SECONDS == 900
        assert browser_health.EXIT_WAIT_SECONDS == 15

    async def test_a_clone_is_not_captured(self, stage):
        stage.persistent.clear()
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()

        assert stage.captures == []


class TestTheCaptureRunsBesideTheHealTiming:
    """Review of #228: the capture used to be awaited inside the round, so the round
    slipped by up to a minute and two browsers frozen together serialised."""

    async def test_the_heal_gives_the_capture_only_a_small_fixed_bound(self):
        """The default itself, which the tests below shorten: the heal may be late by
        this much and no more, and far less than the probe interval."""
        assert 0 < freeze_capture.HEAL_WAITS_FOR_CAPTURE_SECONDS <= 10
        assert (
            freeze_capture.HEAL_WAITS_FOR_CAPTURE_SECONDS
            < browser_health.PROBE_TIMEOUT_SECONDS
        )

    async def test_the_round_does_not_wait_for_a_slow_capture(self, stage):
        stage.capture_seconds = 1.0
        stage.probe_results[INSTANCE_ID] = [False]
        health = stage.health()

        started = time.monotonic()
        await health.check_once()
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"the round waited {elapsed:.2f}s for the capture"
        await health._incidents.before_heal(INSTANCE_ID, pid=OLD_PID, label="x")

    async def test_the_heal_happens_in_the_same_round_as_without_a_capture(
        self, stage, monkeypatch
    ):
        """Same two rounds, same close, whatever the capture costs: a capture that
        takes N s delays neither the second probe nor the heal beyond the small
        bound the heal gives it."""
        monkeypatch.setattr(freeze_capture, "HEAL_WAITS_FOR_CAPTURE_SECONDS", 0.3)

        async def rounds(seconds: float) -> tuple[list[str], float]:
            stage.capture_seconds = seconds
            stage.events.clear()
            stage.closes.clear()
            stage.manager._instances.clear()
            _install(stage.manager)
            stage.probe_results[INSTANCE_ID] = [False, False]
            health = stage.health()
            started = time.monotonic()
            await health.check_once()
            await health.check_once()
            return list(stage.closes), time.monotonic() - started

        fast_closes, fast = await rounds(0.0)
        slow_closes, slow = await rounds(30.0)  # would be 30 s if it were awaited

        assert fast_closes == slow_closes == [INSTANCE_ID]
        assert slow < fast + 1.5, f"{slow:.2f}s against {fast:.2f}s without capture"

    async def test_the_capture_still_completes_before_the_kill_when_it_can(self, stage):
        stage.capture_seconds = 0.2  # still running when the second round begins
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        assert "capture-done" not in stage.events  # round 1 did not wait for it
        await health.check_once()

        assert stage.events.index("capture-done") < stage.events.index("close")

    async def test_a_hung_capture_is_waited_for_a_bounded_time_and_the_heal_goes_on(
        self, stage, monkeypatch, log
    ):
        monkeypatch.setattr(freeze_capture, "HEAL_WAITS_FOR_CAPTURE_SECONDS", 0.1)
        monkeypatch.setattr(freeze_capture, "CAPTURE_TIMEOUT_SECONDS", 30)

        async def hangs(**_facts):
            await asyncio.Event().wait()

        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health(capture=hangs)

        await asyncio.wait_for(health.check_once(), timeout=5)
        await asyncio.wait_for(health.check_once(), timeout=5)

        (error,) = log.at("error")
        assert "still capturing; the heal waits 0s and no longer" in error
        assert stage.closes == [INSTANCE_ID]

    async def test_a_capture_that_outlives_its_own_bound_is_named_unavailable(
        self, stage, monkeypatch, log
    ):
        monkeypatch.setattr(freeze_capture, "CAPTURE_TIMEOUT_SECONDS", 0.05)

        async def hangs(**_facts):
            await asyncio.Event().wait()

        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health(capture=hangs)

        await asyncio.wait_for(health.check_once(), timeout=5)
        await asyncio.wait_for(health.check_once(), timeout=5)

        assert (
            f"{INSTANCE_ID}: evidence@probe1(capture unavailable (TimeoutError))"
            in log.at("warning")
        )
        assert stage.closes == [INSTANCE_ID]

    async def test_a_pass_cancels_the_capture_that_is_still_running(self, stage):
        stage.capture_seconds = 30.0
        stage.probe_results[INSTANCE_ID] = [False, True]
        health = stage.health()

        await health.check_once()
        await _settled()
        (task,) = health._incidents._tasks.values()
        await health.check_once()  # the browser answered: the incident is over
        await _settled()

        assert task.cancelled()
        assert not health._incidents.has(INSTANCE_ID)

    async def test_a_browser_that_is_gone_cancels_its_capture(self, stage):
        stage.capture_seconds = 30.0
        stage.probe_results[INSTANCE_ID] = [False]
        health = stage.health()

        await health.check_once()
        await _settled()
        (task,) = health._incidents._tasks.values()
        stage.manager._instances.pop(INSTANCE_ID)
        await health.check_once()
        await _settled()

        assert task.cancelled()
        assert INSTANCE_ID not in health._incidents.tracked()

    async def test_stop_cancels_every_capture(self, stage):
        stage.capture_seconds = 30.0
        stage.probe_results[INSTANCE_ID] = [False]
        health = stage.health()

        await health.check_once()
        await _settled()
        (task,) = health._incidents._tasks.values()
        health.cancel_captures()
        await _settled()

        assert task.cancelled()

    async def test_two_browsers_frozen_together_both_get_a_dump(self, tmp_path):
        """Both captures run at once; the dumps queue one behind the other inside
        the budget instead of the second being skipped."""
        manager = BrowserManager()
        _install(manager, "i-default", user_data_dir="C:/sessions/default")
        _install(manager, "i-fleet", user_data_dir="C:/sessions/fleet")
        order: list[str] = []

        def slow_write(pid: int, path) -> None:
            order.append(f"start-{pid}")
            time.sleep(0.15)
            path.write_bytes(b"MDMP")
            order.append(f"end-{pid}")

        async def capture(**facts) -> str:
            return await freeze_capture.write_minidump(
                facts["pid"],
                tmp_path,
                facts["label"],
                budget=5.0,
                platform="win32",
                write=slow_write,
            )

        pids = {"i-default": 1001, "i-fleet": 1002}
        bh = browser_health.BrowserHealth(
            manager,
            close=lambda *_a, **_k: None,
            arm_interception=lambda *_a, **_k: None,
            probe=_always_fails,
            own_check=lambda _b: _none(),
            persistent=lambda _iid: True,
            forensics=lambda **_f: _text("x"),
            capture=capture,
            browser_pid=lambda _p, pid: (
                pids[next(i for i in pids if pids[i] == pid)] if pid else None
            ),
            clock=lambda: 1000.0,
        )
        for iid, pid in pids.items():
            manager._instances[iid]["browser"]._process_pid = pid

        await bh.check_once()
        records = await asyncio.gather(*bh._incidents._tasks.values())

        assert all("skipped" not in record for record in records), records
        assert sorted(order) == ["end-1001", "end-1002", "start-1001", "start-1002"]
        assert order[1].startswith("end") and order[3].startswith("end")  # serial


async def _always_fails(_url: str, steps: list[str]) -> None:
    steps.append("evaluate")
    raise ConnectionError("no answer")


async def _none() -> None:
    return None


async def _text(value: str) -> str:
    return value


class TestTheHealedDumpIsProtected:
    async def test_the_dump_of_an_incident_that_heals_is_marked_healed(
        self, stage, log
    ):
        stage.probe_results[INSTANCE_ID] = [False, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        (label,) = stage.healed_labels
        assert label.startswith("freeze-") and label.endswith(INSTANCE_ID[:8])
        assert stage.events.index("mark-healed") < stage.events.index("close")

    async def test_a_pass_leaves_the_dump_unhealed(self, stage):
        stage.probe_results[INSTANCE_ID] = [False, True]
        health = stage.health()

        await health.check_once()
        await health.check_once()

        assert stage.healed_labels == []

    async def test_a_second_unhealed_incident_inside_the_interval_takes_no_dump(
        self, stage
    ):
        stage.probe_results[INSTANCE_ID] = [False, True, False, True]
        health = stage.health()

        for _ in range(4):
            await health.check_once()
            await _settled()

        first, second = stage.captures
        assert first["skip_dump"] is None
        assert "minimum 1800s" in second["skip_dump"]

    async def test_the_interval_passes_and_the_next_incident_dumps_again(self, stage):
        stage.probe_results[INSTANCE_ID] = [False, True, False]
        health = stage.health()

        await health.check_once()
        await _settled()
        await health.check_once()
        stage.now += freeze_capture.DUMP_MIN_INTERVAL_SECONDS + 1
        await health.check_once()
        await _settled()

        assert [c["skip_dump"] for c in stage.captures] == [
            None,
            None,
        ]

    async def test_the_interval_is_thirty_minutes(self):
        assert freeze_capture.DUMP_MIN_INTERVAL_SECONDS == 1800.0

    async def test_a_withheld_dump_is_taken_when_the_incident_reaches_the_heal(
        self, stage, log
    ):
        """The rate limit is for BENIGN episodes: an incident that heals gets its
        dump at the heal, whatever the spacing said at the first probe."""
        stage.probe_results[INSTANCE_ID] = [False, True, False, False]
        health = stage.health()

        for _ in range(4):
            await health.check_once()
            await _settled()

        assert stage.late_dumps == [(OLD_PID, stage.healed_labels[0])]
        (error,) = log.at("error")
        assert "minidump_at_heal(C:/dumps/late.dmp" in error

    async def test_a_heal_resets_the_spacing(self, stage):
        # the fourth answer is the relaunched browser's own probe, inside the heal
        stage.probe_results[INSTANCE_ID] = [False, False, True, False]
        health = stage.health()

        await health.check_once()
        await health.check_once()  # heals
        stage.now += browser_health.HEAL_COOLDOWN_SECONDS + 1
        await health.check_once()  # a new incident, soon after: not rate limited
        await _settled()

        assert [c["skip_dump"] for c in stage.captures] == [None, None]


class TestAPidIsOpenedOnlyAfterItsIdentityIsVerified:
    def _entry(self, monkeypatch, *, pid: int, create_time: float) -> None:
        monkeypatch.setattr(
            process_cleanup,
            "browser_processes",
            {INSTANCE_ID: {"pid": pid, "create_time": create_time}},
        )

    async def test_a_pid_that_is_not_the_recorded_start_time_is_skipped_with_a_reason(
        self, stage, monkeypatch, log
    ):
        mine = psutil.Process()
        self._entry(monkeypatch, pid=mine.pid, create_time=mine.create_time() + 600)
        stage.probe_results[INSTANCE_ID] = [False]
        health = stage.health(browser_pid=lambda _p, _pid: mine.pid)

        # the real capture, so the real dump path would be reached
        health = stage.health(
            browser_pid=lambda _p, _pid: mine.pid, capture=freeze_capture.capture
        )
        await health.check_once()
        for _ in range(3):
            await asyncio.sleep(0)
        await asyncio.wait_for(
            asyncio.gather(*health._incidents._tasks.values()), timeout=20
        )

        (line,) = [w for w in log.at("warning") if "evidence@probe1(capture" in w]
        assert "capture skipped" in line and "start time" in line

    async def test_the_recorded_identity_is_let_through(self, stage, monkeypatch, log):
        mine = psutil.Process()
        self._entry(monkeypatch, pid=mine.pid, create_time=mine.create_time())
        stage.probe_results[INSTANCE_ID] = [False]

        await stage.health(browser_pid=lambda _p, _pid: mine.pid).check_once()
        await _settled()

        assert [c["pid"] for c in stage.captures] == [mine.pid]

    async def test_a_pid_reused_before_the_heal_is_not_dumped_at_the_heal(
        self, stage, monkeypatch, log
    ):
        """The withheld dump is taken minutes after the identity was first checked:
        it is checked again, and a pid that is no longer the recorded browser is named
        in the record instead of being opened."""
        mine = psutil.Process()
        self._entry(monkeypatch, pid=mine.pid, create_time=mine.create_time())
        stage.probe_results[INSTANCE_ID] = [False, True, False, False]
        health = stage.health(browser_pid=lambda _p, _pid: mine.pid)

        for round_ in range(4):
            if round_ == 3:  # the pid changes hands before the heal
                self._entry(
                    monkeypatch, pid=mine.pid, create_time=mine.create_time() + 600
                )
            await health.check_once()
            await _settled()

        assert stage.late_dumps == []
        (error,) = log.at("error")
        assert "minidump_at_heal(skipped: " in error and "start time" in error

    async def test_the_recorded_pid_is_not_the_one_found_is_skipped(
        self, stage, monkeypatch, log
    ):
        self._entry(monkeypatch, pid=OLD_PID + 1, create_time=1.0)
        stage.probe_results[INSTANCE_ID] = [False]

        await stage.health().check_once()
        await _settled()

        (line,) = [w for w in log.at("warning") if "capture skipped" in w]
        assert "not the recorded browser pid" in line

    async def test_the_exit_handle_is_not_held_for_a_reused_pid(
        self, stage, monkeypatch, log
    ):
        mine = psutil.Process()
        self._entry(monkeypatch, pid=mine.pid, create_time=mine.create_time() + 600)
        held: list[int] = []
        health = stage.health(
            browser_pid=lambda _p, _pid: mine.pid,
            hold_exit=lambda pid: held.append(pid) or 99,
            shared=lambda _o: True,
        )
        data = stage.manager._instances[INSTANCE_ID]
        data["browser"]._process = None
        data["browser"]._process_pid = mine.pid

        health._watch_exit(INSTANCE_ID, data)

        assert held == []
        assert any("not held" in w and "start time" in w for w in log.at("warning"))


class TestAStaleExitHandleIsReleasedFirst:
    async def test_a_launched_browser_under_an_id_that_held_a_handle_releases_it(
        self, stage
    ):
        """``_watch_exit`` returned early for a browser that has a process object
        BEFORE releasing the handle held for the pid that used to be under the id."""
        released: list[int] = []
        health = stage.health(
            hold_exit=lambda _pid: 77,
            release_exit=released.append,
            shared=lambda _o: True,
        )
        data = stage.manager._instances[INSTANCE_ID]
        data["browser"]._process = None
        data["browser"]._process_pid = OLD_PID
        health._watch_exit(INSTANCE_ID, data)
        assert health._exit_handles == {INSTANCE_ID: (OLD_PID, 77)}

        data["browser"]._process = SimpleNamespace(returncode=None)  # now launched
        health._watch_exit(INSTANCE_ID, data)

        assert released == [77]
        assert health._exit_handles == {}

    async def test_a_browser_that_is_no_longer_shared_releases_it_too(self, stage):
        released: list[int] = []
        shared = {"value": True}
        health = stage.health(
            hold_exit=lambda _pid: 78,
            release_exit=released.append,
            shared=lambda _o: shared["value"],
        )
        data = stage.manager._instances[INSTANCE_ID]
        data["browser"]._process = None
        data["browser"]._process_pid = OLD_PID
        health._watch_exit(INSTANCE_ID, data)

        shared["value"] = False
        health._watch_exit(INSTANCE_ID, data)

        assert released == [78] and health._exit_handles == {}


class TestTheProbeStepTimings:
    async def test_the_first_failure_line_carries_the_timings(self, stage, log):
        async def slow_probe(_url: str, steps: list[str]) -> None:
            steps.append("createTarget")
            await asyncio.sleep(0.02)
            steps.append("evaluate")
            raise ConnectionError("no answer")

        await stage.health(probe=slow_probe).check_once()

        (warning,) = [w for w in log.at("warning") if "probe 1 failed" in w]
        assert re.search(
            r"steps\(connect=\d+ms createTarget=\d+ms evaluate=\d+ms\)", warning
        ), warning

    async def test_the_failure_wording_the_heal_decides_on_is_unchanged(
        self, stage, log
    ):
        stage.probe_results[INSTANCE_ID] = [False]

        await stage.health().check_once()

        (warning,) = [w for w in log.at("warning") if "probe 1 failed" in w]
        assert "probe 1 failed (ConnectionError at evaluate)" in warning


class TestTheFreshConnectionCheck:
    async def test_it_asks_browser_get_version_over_a_new_connection(self, chrome):
        await browser_health.fresh_browser_version(chrome.url)

        assert chrome.methods == ["Browser.getVersion"]

    async def test_an_unreachable_endpoint_raises_for_the_capture_to_name(self):
        with pytest.raises(OSError):
            await browser_health.fresh_browser_version("ws://127.0.0.1:1/devtools/x")
