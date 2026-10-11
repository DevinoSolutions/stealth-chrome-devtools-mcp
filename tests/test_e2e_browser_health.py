"""Real-Chrome proof for F-963: a named-session browser is healed under the same id.

The hermetic pins in ``test_browser_health.py`` prove the loop's decisions against
doubles and the probe against a loopback websocket. This node runs the whole thing
once on a real headless Chrome and the node's own tmp session root: the REAL
``probe_browser`` passes on a real named session, ``BrowserHealth.heal`` (with the
real ``close_instance`` tool and the real ``setup_interception``) closes that
Chrome and starts another on the same directory under the same ``instance_id``, the
old process is gone, the new one answers the probe, and ``navigate`` works on the
instance afterwards.

Only the node's tmp root and a tmp browser record are touched.
"""

import asyncio
import contextlib
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
from stealth_chrome_devtools_mcp.embedded import browser_health, tool_runtime
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
