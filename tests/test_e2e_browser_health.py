"""Real-Chrome proof for F-963 and F-964.

F-963: a named-session browser is healed under the same id.

The hermetic pins in ``test_browser_health.py`` prove the loop's decisions against
doubles and the probe against a loopback websocket. This node runs the whole thing
once on a real headless Chrome and the node's own tmp session root: the REAL
``probe_browser`` passes on a real named session, ``BrowserHealth.heal`` (with the
real ``close_instance`` tool and the real ``setup_interception``) closes that
Chrome and starts another on the same directory under the same ``instance_id``, the
old process is gone, the new one answers the probe, and ``navigate`` works on the
instance afterwards.

F-964: the shared ``fleet`` session's Chrome is killed by ITS OWN pid, one
``check_once`` relaunches it under the same instance id with a new pid and a new
``chrome_log`` that exists on disk, and the log flags are not visible to a page.

Only the node's tmp root and a tmp browser record are touched.
"""

import asyncio
import contextlib
import re
import sys
import uuid
from pathlib import Path

import psutil
import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    released,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import (
    browser_health,
    chrome_log,
    tool_runtime,
)
from stealth_chrome_devtools_mcp.embedded.tool_sections import browser_management
from stealth_chrome_devtools_mcp.settings import get_settings

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(autouse=True)
def _isolated_root(tmp_empty_root):
    """Make ``tmp_empty_root`` win over the warmup's cached settings; see
    ``test_e2e_seed_from_running_source._isolated_root``."""
    get_settings.cache_clear()
    return tmp_empty_root


@pytest.fixture(autouse=True)
def _record_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tool_runtime.process_cleanup, "pid_file", tmp_path / "browser_pids.json"
    )


async def _browser_of(instance_id: str):
    browser = await tool_runtime.browser_manager.get_browser(instance_id)
    assert browser is not None, f"{instance_id} is not a live instance"
    return browser


async def _until_exited(pid: int, budget: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + budget
    while psutil.pid_exists(pid):
        assert asyncio.get_running_loop().time() < deadline, f"{pid} is still running"
        await asyncio.sleep(0.25)


async def test_a_named_session_is_healed_under_the_same_instance_id(
    fixture_app_server, tmp_empty_root
):
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    manager = tool_runtime.browser_manager
    name = f"f963-{uuid.uuid4().hex[:8]}"

    spawned = await spawn(session=name, headless=True, **sandbox_kwargs())
    instance_id = spawned["instance_id"]
    profile = Path(spawned["spawn_diagnostics"]["profile_selection"]["user_data_dir"])
    try:
        assert profile == tmp_empty_root["sessions"] / name, profile
        old_browser = await _browser_of(instance_id)
        old_pid = old_browser._process_pid
        assert isinstance(old_pid, int) and psutil.pid_exists(old_pid)

        # (b) the real probe passes on a healthy named session
        steps: list[str] = []
        await browser_health.probe_browser(old_browser.websocket_url, steps)
        assert steps == ["createTarget", "attachToTarget", "evaluate", "closeTarget"]

        # (c) the heal, over the real close path and the real interception
        health = browser_health.BrowserHealth(
            manager,
            close=browser_management.close_instance,
            arm_interception=tool_runtime.network_interceptor.setup_interception,
        )
        assert await health.heal(instance_id) is True

        live = [i.instance_id for i in await manager.list_instances()]
        assert live == [instance_id], "the same id, and no second instance"
        new_browser = await _browser_of(instance_id)
        new_pid = new_browser._process_pid
        assert isinstance(new_pid, int) and new_pid != old_pid
        await _until_exited(old_pid)
        assert psutil.pid_exists(new_pid)
        selection = (await manager.get_spawn_diagnostics(instance_id))[
            "profile_selection"
        ]
        assert Path(selection["user_data_dir"]) == profile

        await browser_health.probe_browser(new_browser.websocket_url, [])

        await navigate_and_settle(instance_id, f"{fixture_app_server}/index.html")
        assert (await navigate(instance_id=instance_id, url="about:blank"))["url"] == (
            "about:blank"
        )
        assert await eval_js(instance_id, "1+1") == 2
    finally:
        with contextlib.suppress(Exception):
            await get_fn("close_instance")(instance_id=instance_id)
        await released(profile)


_CHROME_LOG_NAME = re.compile(r"chrome-fleet-[0-9a-f]{8}-\d{8}T\d{6}Z\.log")


class _Recorder:
    """``debug_logger`` as ``browser_health`` sees it: passes every call through and
    keeps the warnings."""

    def __init__(self, real) -> None:
        self._real = real
        self.warnings: list[str] = []

    def log_warning(self, component, method, message, *args, **kwargs):
        self.warnings.append(str(message))
        return self._real.log_warning(component, method, message, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_a_shared_session_that_exits_is_relaunched_under_the_same_id(
    fixture_app_server, tmp_empty_root, monkeypatch
):
    recorder = _Recorder(browser_health.debug_logger)
    monkeypatch.setattr(browser_health, "debug_logger", recorder)
    spawn = get_fn("spawn_browser")
    manager = tool_runtime.browser_manager

    spawned = await spawn(session="fleet", headless=True, **sandbox_kwargs())
    instance_id = spawned["instance_id"]
    profile = Path(spawned["spawn_diagnostics"]["profile_selection"]["user_data_dir"])
    try:
        assert profile == tmp_empty_root["sessions"] / "fleet", profile
        first_log = Path(spawned["spawn_diagnostics"]["chrome_log"])
        assert _CHROME_LOG_NAME.fullmatch(first_log.name), first_log.name
        assert first_log.stat().st_size > 0, "Chrome wrote it, not just us"
        old_pid = (await _browser_of(instance_id))._process_pid
        assert isinstance(old_pid, int) and psutil.pid_exists(old_pid)

        # the browser dies on its own: nobody asked close_instance
        psutil.Process(old_pid).kill()
        await _until_exited(old_pid)

        health = browser_health.BrowserHealth(
            manager,
            close=browser_management.close_instance,
            arm_interception=tool_runtime.network_interceptor.setup_interception,
        )
        await health.check_once()

        # F-993: the exit code of the browser we just killed is in the relaunch line
        # (psutil's kill is TerminateProcess(pid, 15) on Windows, SIGKILL elsewhere)
        (line,) = [w for w in recorder.warnings if "exited unexpectedly" in w]
        assert re.search(r"exit_code=(15|-9)\b", line), line

        live = [i.instance_id for i in await manager.list_instances()]
        assert live == [instance_id], "the same id, and no second instance"
        new_browser = await _browser_of(instance_id)
        new_pid = new_browser._process_pid
        assert isinstance(new_pid, int) and new_pid != old_pid
        assert psutil.pid_exists(new_pid)
        diagnostics = await manager.get_spawn_diagnostics(instance_id)
        assert Path(diagnostics["profile_selection"]["user_data_dir"]) == profile
        second_log = Path(diagnostics["chrome_log"])
        assert second_log != first_log and second_log.stat().st_size > 0
        assert _CHROME_LOG_NAME.fullmatch(second_log.name), second_log.name
        await browser_health.probe_browser(new_browser.websocket_url, [])
        assert await eval_js(instance_id, "1+1") == 2
    finally:
        with contextlib.suppress(Exception):
            await get_fn("close_instance")(instance_id=instance_id, session="fleet")
        await released(profile)


async def test_the_first_failed_probe_captures_the_real_browser_before_any_heal(
    fixture_app_server, tmp_empty_root, monkeypatch
):
    """F-993 on a real Chrome: a probe that fails once (the freeze, simulated at the
    probe seam) makes the REAL capture sample the real process tree, ask a fresh
    connection, and (Windows) write a readable dump -- and nothing is closed."""
    recorder = _Recorder(browser_health.debug_logger)
    monkeypatch.setattr(browser_health, "debug_logger", recorder)
    spawn = get_fn("spawn_browser")
    manager = tool_runtime.browser_manager
    spawned = await spawn(session="fleet", headless=True, **sandbox_kwargs())
    instance_id = spawned["instance_id"]
    profile = Path(spawned["spawn_diagnostics"]["profile_selection"]["user_data_dir"])
    closes: list[str] = []

    async def close(iid: str, session: str | None = None) -> dict:
        closes.append(iid)
        return {}

    async def frozen(_url: str, steps: list[str]) -> None:
        steps.append("evaluate")
        raise TimeoutError

    try:
        health = browser_health.BrowserHealth(
            manager,
            close=close,
            arm_interception=tool_runtime.network_interceptor.setup_interception,
            probe=frozen,
        )
        await health.check_once()

        (line,) = [w for w in recorder.warnings if "probe 1 failed" in w]
        assert closes == [], "the first failure heals nothing"
        assert "evidence(cpu(3.0s: browser=" in line, line
        assert "process(status=" in line and "threads=" in line, line
        assert "fresh_cdp(Browser.getVersion: answered in " in line, line
        if sys.platform == "win32":
            dump = Path(re.search(r"minidump\((.+?\.dmp) \(", line).group(1))
            assert dump.parent.name == "freeze-dumps" and dump.stat().st_size > 0
            assert dump.read_bytes()[:4] == b"MDMP"
        else:
            assert "minidump(skipped: platform)" in line, line
        for forbidden in ("http", "about:blank", "cookie"):
            assert forbidden not in line.split("evidence(")[1].lower(), forbidden
    finally:
        with contextlib.suppress(Exception):
            await get_fn("close_instance")(instance_id=instance_id, session="fleet")
        await released(profile)


_FINGERPRINT = (
    "JSON.stringify([navigator.userAgent, navigator.webdriver, navigator.languages,"
    " navigator.plugins.length, typeof window.chrome, navigator.hardwareConcurrency,"
    " navigator.platform])"
)


async def test_the_log_flags_are_not_visible_to_a_page(tmp_empty_root, monkeypatch):
    """The same named session launched with and without ``--enable-logging``
    reads the same navigator to a page, and Chrome keeps the log file it was told
    to."""
    spawn = get_fn("spawn_browser")
    logged = await spawn(
        session=f"f964-{uuid.uuid4().hex[:8]}", headless=True, **sandbox_kwargs()
    )
    monkeypatch.setattr(chrome_log, "flags", lambda *_a, **_k: [])  # the control
    plain = await spawn(
        session=f"f964-{uuid.uuid4().hex[:8]}", headless=True, **sandbox_kwargs()
    )
    logged_id, plain_id = logged["instance_id"], plain["instance_id"]
    profiles = [
        Path(spawned["spawn_diagnostics"]["profile_selection"]["user_data_dir"])
        for spawned in (logged, plain)
    ]
    try:
        log_path = Path(logged["spawn_diagnostics"]["chrome_log"])
        assert "chrome_log" not in plain["spawn_diagnostics"]
        args = (await tool_runtime.browser_manager.get_spawn_diagnostics(logged_id))[
            "effective_browser_args"
        ]
        assert "--enable-logging" in args
        assert await eval_js(logged_id, _FINGERPRINT) == await eval_js(
            plain_id, _FINGERPRINT
        )
        assert "enable-logging" not in await eval_js(logged_id, "navigator.userAgent")
    finally:
        for instance_id in (logged_id, plain_id):
            with contextlib.suppress(Exception):
                await get_fn("close_instance")(instance_id=instance_id)
        for profile in profiles:
            await released(profile)
    assert log_path.stat().st_size > 0, "Chrome wrote the file it was told to"
