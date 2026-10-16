"""F-964 -- the two shared sessions are relaunched when their browser EXITS.

On 2026-10-11 ~16:27Z ``default`` (master), ``fleet`` and one clone, all driven by one
backend, exited within seconds of each other. The backend log said only "Removed
stale browser instance ...: browser process is not running", and nobody relaunched
them for five minutes. ``BrowserManager`` now records each browser that died ON ITS
OWN (``take_exited``) and ``browser_health`` relaunches the ones whose directory is
``default``'s or ``fleet``'s, under the same instance id, through the tail the F-963
heal uses.

Hermetic: a real ``BrowserManager`` over ``FakeBrowser``s and the node's own tmp
session root, so the scope rule runs against real ``fleet_session.lock_key``
answers; the launch, the close path, the forensics and the "who holds this
directory" witness are the keyword seams the module offers.
"""

import asyncio
import contextvars
import os
from types import SimpleNamespace

import pytest

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import (
    browser_health,
    directory_gate,
    tab_binding,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserOptions

FLEET_ID = "i-fleet"
MASTER_ID = "i-master"
SELECTION = {"profile_role": "default"}
CHROME_LOG = "C:/logs/chrome-fleet-i-fleet-20261011T162700Z.log"


class _Log:
    """``debug_logger`` as ``browser_health`` sees it: every line, by level."""

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


def _alive_browser(instance_id: str) -> FakeBrowser:
    browser = FakeBrowser(alive=True)
    browser.websocket_url = f"ws://127.0.0.1:1/devtools/browser/{instance_id}"
    return browser


def _install(manager: BrowserManager, instance_id: str, directory, **options):
    manager._instances[instance_id] = {
        "browser": _alive_browser(instance_id),
        "tab": FakeTab(target_id=f"T-{instance_id}"),
        "instance": BrowserInstance(
            instance_id=instance_id,
            headless=options.pop("headless", True),
            viewport={"width": 1280, "height": 720},
        ),
        "options": BrowserOptions(user_data_dir=str(directory), **options),
        "navigation_count": 0,
    }
    diagnostics = {"profile_selection": dict(SELECTION), "chrome_log": CHROME_LOG}
    manager._spawn_diagnostics[instance_id] = diagnostics
    manager._instances[instance_id]["spawn_diagnostics"] = diagnostics


def _die(manager: BrowserManager, instance_id: str, returncode: int = 1) -> None:
    """The browser process is gone and nobody asked: what a crash looks like to
    ``browser_is_alive``."""
    browser = manager._instances[instance_id]["browser"]
    browser._process = SimpleNamespace(poll=lambda: returncode, returncode=returncode)


class _Stage:
    """A manager with a shared-session browser and the collaborators of one health
    loop, recording what the relaunch does."""

    def __init__(self, manager: BrowserManager, monkeypatch, roots) -> None:
        self.manager = manager
        self.roots = roots
        self.now = 1000.0
        self.launches: list[tuple[BrowserOptions, str | None]] = []
        self.closes: list[str] = []
        self.armed: list[tuple[str, str, object]] = []
        self.forensics_calls: list[dict] = []
        self.holder: str | None = None
        self.launch_error: BaseException | None = None
        self.gate_held_during: list[str] = []
        self.probe_ok = True
        self.persistent: set[str] = set()
        monkeypatch.setattr(manager, "spawn_browser", self._spawn)

    @property
    def fleet_dir(self):
        return self.roots["sessions"] / "fleet"

    async def _spawn(self, options: BrowserOptions, *, instance_id: str | None = None):
        self.launches.append((options, instance_id))
        if directory_gate.gate_for(options.user_data_dir)._lock.locked():
            self.gate_held_during.append("spawn")
        if self.launch_error is not None:
            raise self.launch_error
        _install(
            self.manager,
            instance_id,
            options.user_data_dir,
            block_resources=options.block_resources,
            headless=options.headless,
        )
        self.persistent.add(instance_id)
        return self.manager._instances[instance_id]["instance"]

    async def close(self, instance_id: str) -> dict:
        self.closes.append(instance_id)
        self.manager._instances.pop(instance_id, None)
        return {"closed": True}

    async def arm(self, tab, instance_id, block_resources):
        self.armed.append((tab.target.target_id, instance_id, block_resources))

    async def probe(self, _url: str, steps: list[str]) -> None:
        steps.append("evaluate")
        if not self.probe_ok:
            raise ConnectionError("no answer")

    async def own_ok(self, _browser) -> None:
        return None

    async def capture(self, **facts) -> str:
        return "cpu(3.0s: browser=0.00s)"

    async def forensics(self, **facts) -> str:
        self.forensics_calls.append(facts)
        return f"uptime={facts['uptime_seconds']:.0f}s machine(chromium_processes=3)"

    def health(self, **overrides) -> browser_health.BrowserHealth:
        kwargs = {
            "close": self.close,
            "arm_interception": self.arm,
            "probe": self.probe,
            "own_check": self.own_ok,
            "persistent": lambda iid: iid in self.persistent,
            "forensics": self.forensics,
            "capture": self.capture,
            "holder": lambda _dir: self.holder,
            "clock": lambda: self.now,
        }
        kwargs.update(overrides)
        return browser_health.BrowserHealth(self.manager, **kwargs)


@pytest.fixture()
def stage(monkeypatch, log, tmp_session_root):
    manager = BrowserManager()
    stage = _Stage(manager, monkeypatch, tmp_session_root)
    _install(manager, FLEET_ID, stage.fleet_dir)
    stage.persistent.add(FLEET_ID)
    return stage


# ---------------------------------------------------------------------------
# 1. what counts as an exit: only a browser that died on its own
# ---------------------------------------------------------------------------


class TestWhatCountsAsAnExit:
    async def test_a_listing_that_finds_a_dead_browser_records_it(self, stage):
        _die(stage.manager, FLEET_ID)

        assert await stage.manager.list_instances() == []

        ((instance_id, data),) = stage.manager.take_exited()
        assert instance_id == FLEET_ID
        assert data["options"].user_data_dir == str(stage.fleet_dir)
        assert data["spawn_diagnostics"]["profile_selection"] == SELECTION

    async def test_a_lookup_that_finds_a_dead_browser_records_it_too(self, stage):
        _die(stage.manager, FLEET_ID)

        assert await stage.manager.get_instance(FLEET_ID) is None

        assert [iid for iid, _data in stage.manager.take_exited()] == [FLEET_ID]

    async def test_take_exited_drains(self, stage):
        _die(stage.manager, FLEET_ID)
        await stage.manager.list_instances()

        assert len(stage.manager.take_exited()) == 1
        assert stage.manager.take_exited() == []

    async def test_a_close_instance_is_not_an_exit(self, stage, monkeypatch):
        browser = stage.manager._instances[FLEET_ID]["browser"]
        browser._process = SimpleNamespace(poll=lambda: 1, returncode=1)
        await stage.manager.close_instance(FLEET_ID)

        assert stage.manager.take_exited() == []

    async def test_close_all_is_not_an_exit(self, stage):
        _install(stage.manager, "i-other", stage.roots["sessions"] / "other")
        await stage.manager.close_all()

        assert stage.manager.take_exited() == []

    async def test_an_idle_reap_is_not_an_exit(self, stage):
        entry = stage.manager._instances[FLEET_ID]
        entry["idle_timeout_seconds"] = 1
        entry["instance"].last_activity = entry["instance"].last_activity.replace(
            year=2000
        )

        assert await stage.manager.cleanup_inactive() == 1

        assert stage.manager.take_exited() == []

    async def test_the_record_is_bounded(self, stage):
        for n in range(40):
            _install(stage.manager, f"i-{n}", stage.roots["sessions"] / f"s{n}")
            _die(stage.manager, f"i-{n}")
        await stage.manager.list_instances()

        assert len(stage.manager.take_exited()) <= 16


# ---------------------------------------------------------------------------
# 2. the scope: default and fleet, nothing else
# ---------------------------------------------------------------------------


class TestScope:
    async def test_an_exited_fleet_browser_is_relaunched_under_the_same_id(
        self, stage, log
    ):
        entry = stage.manager._instances[FLEET_ID]
        entry["instance"].viewport = {"width": 1111, "height": 777}
        entry["options"] = entry["options"].model_copy(
            update={"block_resources": ["*.png"]}
        )
        tab_binding._bound[("claude-session", FLEET_ID)] = "T-old"
        _die(stage.manager, FLEET_ID)

        await stage.health().check_once()

        ((options, instance_id),) = stage.launches
        assert instance_id == FLEET_ID
        assert (options.user_data_dir, options.headless) == (
            str(stage.fleet_dir),
            True,
        )
        assert (options.viewport_width, options.viewport_height) == (1111, 777)
        live = [i.instance_id for i in await stage.manager.list_instances()]
        assert live == [FLEET_ID], "the same id, and no second instance"
        assert stage.armed == [(f"T-{FLEET_ID}", FLEET_ID, ["*.png"])]
        assert ("claude-session", FLEET_ID) not in tab_binding._bound
        assert any("relaunched after an unexpected exit" in m for m in log.at("info"))

    async def test_an_exited_default_browser_is_relaunched_headed_as_it_was(
        self, stage, log
    ):
        _install(stage.manager, MASTER_ID, stage.roots["master"], headless=False)
        _die(stage.manager, MASTER_ID)

        await stage.health().check_once()

        ((options, instance_id),) = stage.launches
        assert instance_id == MASTER_ID
        assert options.user_data_dir == str(stage.roots["master"])
        assert options.headless is False

    async def test_the_default_relaunch_names_master_and_never_asks_the_resolver(
        self, stage, monkeypatch
    ):
        """F-966: a spawn naming NOTHING is a clone, so a relaunch that carried
        ``user_data_dir=None`` would turn the shared browser into a disposable
        copy. It carries master explicitly and never reaches the resolver."""
        from stealth_chrome_devtools_mcp.embedded import clone_storage

        async def _never(*_a, **_k):
            raise AssertionError("a relaunch went through the profile resolver")

        monkeypatch.setattr(clone_storage, "resolve_profile_selection", _never)
        _install(stage.manager, MASTER_ID, stage.roots["master"], headless=False)
        _die(stage.manager, MASTER_ID)

        await stage.health().check_once()

        ((options, instance_id),) = stage.launches
        assert instance_id == MASTER_ID
        assert options.user_data_dir == str(stage.roots["master"])
        assert options.auto_clone is False

    async def test_another_named_session_is_left_alone(self, stage):
        _install(stage.manager, "i-work", stage.roots["sessions"] / "work")
        _die(stage.manager, "i-work")

        await stage.health().check_once()

        assert stage.launches == []

    async def test_a_session_that_only_ends_in_fleet_is_not_the_fleet(self, stage):
        _install(stage.manager, "i-x", stage.roots["root"] / "elsewhere" / "fleet")
        _die(stage.manager, "i-x")

        await stage.health().check_once()

        assert stage.launches == []

    async def test_a_clone_is_left_alone(self, stage):
        _install(
            stage.manager,
            "i-clone",
            stage.fleet_dir,  # even on a directory that would key as the fleet
            auto_clone=True,
        )
        _die(stage.manager, "i-clone")

        await stage.health().check_once()

        assert stage.launches == []

    @pytest.mark.parametrize(
        ("where", "expected"),
        [
            ("master", True),
            ("fleet", True),
            ("FLEET", True),
            ("work", False),
            ("fleet-2", False),
        ],
    )
    def test_the_scope_rule_is_the_one_session_key(
        self, tmp_session_root, where, expected
    ):
        directory = (
            tmp_session_root["master"]
            if where == "master"
            else tmp_session_root["sessions"] / where
        )

        assert (
            browser_health.is_shared_session(
                BrowserOptions(user_data_dir=str(directory))
            )
            is expected
        )

    def test_no_directory_is_no_session(self):
        assert browser_health.is_shared_session(BrowserOptions()) is False


# ---------------------------------------------------------------------------
# 3. when it does not relaunch, and what it says
# ---------------------------------------------------------------------------


class TestWhenItDoesNot:
    async def test_a_directory_held_by_a_live_browser_is_not_relaunched_over(
        self, stage, log
    ):
        stage.holder = "pid 777"
        _die(stage.manager, FLEET_ID)

        await stage.health().check_once()

        assert stage.launches == []
        (warning,) = log.at("warning")
        assert "pid 777" in warning and "not relaunching" in warning
        assert not directory_gate.gate_for(stage.fleet_dir)._lock.locked()

    async def test_a_stopping_backend_relaunches_nothing(self, stage, log):
        _die(stage.manager, FLEET_ID)
        health = stage.health()
        health.stopping = True

        await health.check_once()

        assert stage.launches == []

    async def test_a_stopping_backend_probes_and_heals_nothing_either(self, stage):
        stage.probe_ok = False
        health = stage.health()
        health.stopping = True

        await health.check_once()
        await health.check_once()

        assert stage.closes == [] and stage.launches == []

    async def test_a_stop_that_lands_mid_round_stops_the_relaunch(self, stage, log):
        """``stop()`` between the round's listing and the relaunch: the gate is
        taken, the flag is read AFTER it, and nothing launches."""
        _die(stage.manager, FLEET_ID)
        health = stage.health()
        entry = stage.manager._instances[FLEET_ID]
        options, instance = entry["options"], entry["instance"]
        health.stopping = True

        assert await health.relaunch_exited(FLEET_ID, options, instance, {}) is False

        assert stage.launches == []
        assert any("the backend is stopping" in w for w in log.at("warning"))

    async def test_stop_marks_the_running_loop_as_stopping(self, stage):
        browser_health.start(
            stage.manager, close=stage.close, arm_interception=stage.arm
        )
        try:
            running = browser_health._health
            assert running is not None and running.stopping is False
            await browser_health.stop()
            assert running.stopping is True
            _die(stage.manager, FLEET_ID)
            await running.check_once()
            assert stage.launches == []
        finally:
            await browser_health.stop()

    async def test_a_failed_relaunch_is_an_error_and_the_end_of_it(self, stage, log):
        stage.launch_error = RuntimeError("no chrome")
        _die(stage.manager, FLEET_ID)
        health = stage.health()

        await health.check_once()
        await health.check_once()
        stage.now += browser_health.HEAL_COOLDOWN_SECONDS + 1
        await health.check_once()

        assert len(stage.launches) == 1, "nothing retries a failed relaunch"
        (error,) = log.at("error")
        assert "relaunch failed" in error and "RuntimeError: no chrome" in error
        assert FLEET_ID not in stage.manager._instances, "left closed"
        assert not directory_gate.gate_for(stage.fleet_dir)._lock.locked()

    async def test_one_instances_failure_does_not_end_the_round(self, stage, log):
        _install(stage.manager, MASTER_ID, stage.roots["master"])
        stage.persistent.add(MASTER_ID)
        _die(stage.manager, FLEET_ID)
        _die(stage.manager, MASTER_ID)
        health = stage.health()
        real = health.relaunch_exited

        async def first_blows_up(instance_id, *rest):
            if instance_id == FLEET_ID:
                raise RuntimeError("relaunch blew up")
            return await real(instance_id, *rest)

        health.relaunch_exited = first_blows_up

        await health.check_once()

        assert [iid for _o, iid in stage.launches] == [MASTER_ID]
        assert any("relaunch blew up" in line for line in log.at("error"))


# ---------------------------------------------------------------------------
# 4. an exit seen by a listing before the loop did still relaunches
# ---------------------------------------------------------------------------


class TestAnExitSeenElsewhere:
    async def test_an_instance_discarded_by_a_listing_before_the_loop_ran_relaunches(
        self, stage
    ):
        _die(stage.manager, FLEET_ID)
        # a tool call's `list_instances` finds it first: the entry is GONE from the
        # manager by the time the loop looks
        await stage.manager.list_instances()
        assert FLEET_ID not in stage.manager._instances

        await stage.health().check_once()

        assert [iid for _o, iid in stage.launches] == [FLEET_ID]

    async def test_the_relaunched_browser_is_probed_and_scoped_in_the_same_round(
        self, stage
    ):
        _die(stage.manager, FLEET_ID)
        probed: list[str] = []

        async def probe(url: str, steps: list[str]) -> None:
            probed.append(url)
            steps.append("evaluate")

        await stage.health(probe=probe).check_once()

        assert probed, "the relaunch's own probe ran (the shared tail)"


# ---------------------------------------------------------------------------
# 5. one cooldown, shared with the heal
# ---------------------------------------------------------------------------


class TestSharedCooldown:
    async def test_an_exit_right_after_a_relaunch_is_an_error_not_a_loop(
        self, stage, log
    ):
        _die(stage.manager, FLEET_ID)
        health = stage.health()
        await health.check_once()  # relaunches
        assert len(stage.launches) == 1

        stage.now += browser_health.HEAL_COOLDOWN_SECONDS - 1
        _die(stage.manager, FLEET_ID)
        await health.check_once()

        assert len(stage.launches) == 1
        assert any("not relaunching again within" in e for e in log.at("error"))

        stage.now += 2
        _install(stage.manager, FLEET_ID, stage.fleet_dir)
        _die(stage.manager, FLEET_ID)
        await health.check_once()
        assert len(stage.launches) == 2, "past the cooldown it relaunches again"

    async def test_an_exit_after_a_heal_waits_out_the_heal_cooldown(self, stage, log):
        health = stage.health()
        health._last_heal[FLEET_ID] = stage.now  # a heal just ran
        _die(stage.manager, FLEET_ID)

        await health.check_once()

        assert stage.launches == []
        assert any("not relaunching again within" in e for e in log.at("error"))

    async def test_a_hang_after_a_relaunch_waits_out_the_cooldown_too(self, stage, log):
        _die(stage.manager, FLEET_ID)
        health = stage.health()
        await health.check_once()  # relaunches; probes pass
        stage.probe_ok = False

        await health.check_once()
        await health.check_once()  # two failures: would heal

        assert stage.closes == []
        assert any("not again within" in line for line in log.at("warning"))


# ---------------------------------------------------------------------------
# 6. the log line: counts only, no dead tree, the chrome log's PATH
# ---------------------------------------------------------------------------


class TestTheLogLine:
    async def test_it_warns_with_the_forensics_minus_the_dead_tree(self, stage, log):
        _die(stage.manager, FLEET_ID)

        await stage.health().check_once()

        (call,) = stage.forensics_calls
        assert call["browser_alive"] is False
        warnings = [w for w in log.at("warning") if "exited unexpectedly" in w]
        (line,) = warnings
        assert line.startswith(f"{FLEET_ID}: exited unexpectedly")
        assert line.endswith("; relaunching")
        assert "uptime=" in line and "machine(" in line
        assert "tree(" not in line and "targets(" not in line

    async def test_it_carries_the_chrome_log_path_and_nothing_of_its_content(
        self, stage, log
    ):
        _die(stage.manager, FLEET_ID)

        await stage.health().check_once()

        (line,) = [w for w in log.at("warning") if "exited unexpectedly" in w]
        assert f"chrome_log={CHROME_LOG}" in line

    async def test_the_cooldown_error_names_the_path_too(self, stage, log):
        health = stage.health()
        health._last_heal[FLEET_ID] = stage.now
        _die(stage.manager, FLEET_ID)

        await health.check_once()

        assert any(f"chrome_log={CHROME_LOG}" in e for e in log.at("error"))

    async def test_a_hang_warning_carries_the_chrome_log_path(self, stage, log):
        stage.probe_ok = False

        await stage.health().check_once()

        (warning,) = log.at("warning")
        assert "probe 1 failed" in warning and f"chrome_log={CHROME_LOG}" in warning


class TestForensicsForADeadBrowser:
    async def test_it_asks_neither_the_targets_nor_the_tree(self):
        asked: list[str] = []

        async def fetch(_url):
            asked.append("targets")
            return []

        line = await browser_health.gather_forensics(
            uptime_seconds=61.0,
            websocket_url="ws://x",
            pid=1,
            fetch_targets=fetch,
            browser_alive=False,
        )

        assert asked == []
        assert line.startswith("uptime=61s machine(")
        assert "tree" not in line and "targets" not in line

    async def test_the_default_still_asks_all_three(self):
        async def fetch(_url):
            return [{"type": "page"}]

        line = await browser_health.gather_forensics(
            uptime_seconds=1.0, websocket_url="ws://x", pid=None, fetch_targets=fetch
        )

        assert "targets(page=1 other=0)" in line
        assert "tree(" in line and "machine(" in line


class TestTheGateIsHeld:
    async def test_the_relaunch_runs_under_the_directory_gate(self, stage):
        _die(stage.manager, FLEET_ID)

        await stage.health().check_once()

        assert stage.gate_held_during == ["spawn"]
        assert not directory_gate.gate_for(stage.fleet_dir)._lock.locked()

    async def test_a_spawn_that_raced_in_first_is_not_doubled(self, stage):
        """A concurrent ``spawn_browser(session='fleet')`` that took the gate and
        launched while the loop waited leaves a live browser on the directory: the
        holder witness sees it and the relaunch stands down."""
        _die(stage.manager, FLEET_ID)
        gate = await directory_gate.hold(str(stage.fleet_dir))
        health = stage.health()
        # An EMPTY context, as another request's task has: the gate is reentrant
        # per context, and a copy of this one already holds it.
        racing = asyncio.create_task(health.check_once(), context=contextvars.Context())
        await asyncio.sleep(0)
        stage.holder = "pid 4321"  # the racer's launch
        gate.release()
        await racing

        assert stage.launches == []


# ---------------------------------------------------------------------------
# F-993: the EXIT CODE of an unexpected exit, to tell an external kill from a crash
# ---------------------------------------------------------------------------

A_PID_THAT_IS_THERE = os.getpid()
A_PID_THAT_IS_NOT = 2**22 + 7


def _adopt(manager: BrowserManager, instance_id: str) -> FakeBrowser:
    """A browser this backend did not launch (F-888 re-attach): no process object,
    only a pid."""
    browser = manager._instances[instance_id]["browser"]
    browser._process = None
    browser._process_pid = A_PID_THAT_IS_THERE
    return browser


class TestTheExitCodeIsInTheRelaunchLine:
    @pytest.mark.parametrize(
        ("code", "shown"),
        [
            (3221225794, "exit_code=3221225794 (0xC0000142)"),
            (1, "exit_code=1"),
            (0, "exit_code=0"),
            (-9, "exit_code=-9 (signal 9)"),
        ],
    )
    async def test_a_launched_browser_reports_its_process_return_code(
        self, stage, log, code, shown
    ):
        _die(stage.manager, FLEET_ID, returncode=code)

        await stage.health().check_once()

        (line,) = [w for w in log.at("warning") if "exited unexpectedly" in w]
        assert shown in line
        assert line.endswith("; relaunching")

    async def test_the_code_is_inside_the_parenthesis_with_the_other_facts(
        self, stage, log
    ):
        _die(stage.manager, FLEET_ID, returncode=7)

        await stage.health().check_once()

        (line,) = [w for w in log.at("warning") if "exited unexpectedly" in w]
        assert "uptime=" in line and "; exit_code=7)" in line

    async def test_the_cooldown_refusal_carries_it_too(self, stage, log):
        health = stage.health()
        health._last_heal[FLEET_ID] = stage.now
        _die(stage.manager, FLEET_ID, returncode=5)

        await health.check_once()

        assert any("exit_code=5" in e for e in log.at("error"))

    async def test_an_exit_with_no_code_to_read_says_so(self, stage, log):
        browser = stage.manager._instances[FLEET_ID]["browser"]
        browser._process = None
        browser._process_pid = A_PID_THAT_IS_NOT

        await stage.health().check_once()

        (line,) = [w for w in log.at("warning") if "exited unexpectedly" in w]
        assert "exit_code=unavailable (" in line


class TestAnAdoptedBrowserIsWatchedFromTheFirstRound:
    """A browser re-attached after a backend restart (F-888) has no process object,
    and the default and fleet sessions are usually THAT kind. A handle held from the
    first probe round is what keeps its exit code readable after it dies."""

    @pytest.fixture()
    def handles(self):
        class Handles:
            def __init__(self) -> None:
                self.opened: list[int] = []
                self.released: list[int] = []
                self.read: list[tuple[object, int | None]] = []
                self.next = 100

            def hold(self, pid: int) -> int:
                self.opened.append(pid)
                self.next += 1
                return self.next

            def release(self, handle: int) -> None:
                self.released.append(handle)

            def status(self, browser: object, handle: int | None) -> str:
                self.read.append((browser, handle))
                return f"exit_code=handle-{handle}"

        return Handles()

    def _health(self, stage, handles, **overrides):
        return stage.health(
            hold_exit=handles.hold,
            release_exit=handles.release,
            exit_status=handles.status,
            **overrides,
        )

    async def test_the_handle_is_taken_at_a_round_and_read_at_the_exit(
        self, stage, handles, log
    ):
        browser = _adopt(stage.manager, FLEET_ID)
        health = self._health(stage, handles)

        await health.check_once()  # the browser is alive: a handle is taken
        assert handles.opened == [A_PID_THAT_IS_THERE]

        browser._process_pid = A_PID_THAT_IS_NOT  # ...and the browser dies
        await health.check_once()

        ((read_browser, read_handle),) = handles.read
        assert read_browser is browser and read_handle == 101
        (line,) = [w for w in log.at("warning") if "exited unexpectedly" in w]
        assert "exit_code=handle-101" in line

    async def test_the_handle_is_released_once_the_code_is_read(self, stage, handles):
        browser = _adopt(stage.manager, FLEET_ID)
        health = self._health(stage, handles)
        await health.check_once()

        browser._process_pid = A_PID_THAT_IS_NOT
        await health.check_once()

        assert handles.released == [101]

    async def test_a_round_does_not_take_a_second_handle_for_the_same_browser(
        self, stage, handles
    ):
        _adopt(stage.manager, FLEET_ID)
        health = self._health(stage, handles)

        for _ in range(3):
            await health.check_once()

        assert handles.opened == [A_PID_THAT_IS_THERE] and handles.released == []

    async def test_a_new_browser_under_the_same_id_replaces_the_old_handle(
        self, stage, handles
    ):
        browser = _adopt(stage.manager, FLEET_ID)
        health = self._health(stage, handles)
        await health.check_once()

        browser._process_pid = os.getppid()  # another live process, same instance id
        await health.check_once()

        assert handles.opened == [A_PID_THAT_IS_THERE, os.getppid()]
        assert handles.released == [101]

    async def test_a_browser_that_is_closed_on_purpose_gives_its_handle_back(
        self, stage, handles
    ):
        _adopt(stage.manager, FLEET_ID)
        health = self._health(stage, handles)
        await health.check_once()

        stage.manager._instances.pop(FLEET_ID)  # close_instance popped it
        stage.persistent.discard(FLEET_ID)
        await health.check_once()

        assert handles.released == [101]
        assert handles.read == []  # nobody asked for a code: it was not an exit

    async def test_release_all_gives_every_handle_back_at_shutdown(
        self, stage, handles
    ):
        _adopt(stage.manager, FLEET_ID)
        health = self._health(stage, handles)
        await health.check_once()

        health.release_exit_handles()
        health.release_exit_handles()  # idempotent

        assert handles.released == [101]

    async def test_a_launched_browser_needs_no_handle(self, stage, handles):
        await self._health(stage, handles).check_once()  # a FakeBrowser with _process

        assert handles.opened == []

    async def test_a_persistent_browser_that_is_not_a_shared_session_is_not_watched(
        self, stage, handles, tmp_session_root
    ):
        work = tmp_session_root["sessions"] / "work"
        _install(stage.manager, "i-work", work)
        stage.persistent.add("i-work")
        stage.manager._instances.pop(FLEET_ID)
        stage.persistent.discard(FLEET_ID)
        _adopt(stage.manager, "i-work")

        await self._health(stage, handles).check_once()

        assert handles.opened == []

    async def test_a_handle_that_could_not_be_opened_is_not_fatal(self, stage, handles):
        browser = _adopt(stage.manager, FLEET_ID)
        health = stage.health(
            hold_exit=lambda _pid: None,
            release_exit=handles.release,
            exit_status=handles.status,
        )

        await health.check_once()
        browser._process_pid = A_PID_THAT_IS_NOT
        await health.check_once()

        assert handles.read == [(browser, None)]
        assert [iid for _o, iid in stage.launches] == [FLEET_ID]
