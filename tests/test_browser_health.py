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
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import serve

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import (
    browser_health,
    directory_gate,
    fleet_session,
    tab_binding,
)
from stealth_chrome_devtools_mcp.embedded import browser_pid_registry as registry
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserOptions

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
        self.launches: list[tuple[BrowserOptions, str | None]] = []
        self.armed: list[tuple[str, str, object]] = []
        self.gate_held_during: list[str] = []
        self.heals: list[str] = []
        self.launch_error: BaseException | None = None
        self.close_kills = True
        self.waits: list[tuple[int | None, float]] = []
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
        queue = self.probe_results.get(iid, [])
        steps.append("evaluate")
        if queue and not queue.pop(0):
            raise ConnectionError("no answer")

    async def close(self, instance_id: str) -> dict:
        self.closes.append(instance_id)
        if self._held():
            self.gate_held_during.append("close")
        data = self.manager._instances.pop(instance_id)
        self.manager._spawn_diagnostics.pop(instance_id, None)
        if self.close_kills:
            data["browser"]._process = SimpleNamespace(poll=lambda: 0, returncode=0)
        return {"closed": True}

    async def _spawn(self, options: BrowserOptions, *, instance_id: str | None = None):
        self.launches.append((options, instance_id))
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
        self.armed.append((tab.target.target_id, instance_id, block_resources))

    async def forensics(self, **facts) -> str:
        return f"uptime={facts['uptime_seconds']:.0f}s targets(page=1 other=0)"

    async def wait_exit(self, pid, timeout):
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
        (warning,) = log.at("warning")
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

        async def close_while_a_spawn_races(instance_id: str) -> object:
            # An EMPTY context, as another request's task has: the gate is
            # reentrant per context, and a copy of this one already holds it.
            racers.append(
                asyncio.create_task(a_concurrent_spawn(), context=contextvars.Context())
            )
            await asyncio.sleep(0)  # the racer runs to the gate and waits there
            return await close(instance_id)

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
