"""Real-Chrome proof for F-966: an unnamed spawn is a clone, and ``default`` closes
only when it is named.

The hermetic pins in ``test_unnamed_spawn_never_opens_default.py`` prove the
decisions against doubles. This node reproduces the owner's incident against the
real thing, on the node's own tmp session root (a seeded ``master``, nobody
holding it, which is the state in which 2.1.27 handed the shared profile to a
caller that asked for none):

1. ``spawn_browser()`` with no session answers ``profile_role == "clone"`` on a
   directory under the clone root, never master;
2. ``spawn_browser(session="default")`` is the one door onto master, and asking
   ``close_instance`` to close it WITHOUT naming the session is refused: the
   browser is still listed and still answers a script afterwards;
3. naming the session closes it.

Every spawn is closed in ``finally``. Only the node's tmp root and a tmp browser
record are touched.
"""

import contextlib
from pathlib import Path

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    released,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import clone_storage, tool_runtime
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(autouse=True)
def _isolated_root(tmp_session_root):
    """Make ``tmp_session_root`` win over the warmup's cached settings; see
    ``test_e2e_seed_from_running_source._isolated_root``."""
    get_settings.cache_clear()
    return tmp_session_root


@pytest.fixture(autouse=True)
def _record_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tool_runtime.process_cleanup, "pid_file", tmp_path / "browser_pids.json"
    )


def _selection(spawn: dict) -> dict:
    return spawn["spawn_diagnostics"]["profile_selection"]


async def test_unnamed_spawn_is_a_clone_and_default_closes_only_by_name(
    tmp_session_root,
):
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    list_instances = get_fn("list_instances")
    master = tmp_session_root["master"]
    clone_root = tmp_session_root["sessions"]
    ids: dict[str, str] = {}
    try:
        # 1. The incident's call: nothing named, nobody holds master.
        unnamed = await spawn(headless=True, **sandbox_kwargs())
        ids["unnamed"] = unnamed["instance_id"]
        selection = _selection(unnamed)
        assert selection["profile_role"] == "clone", selection
        landed = Path(selection["user_data_dir"])
        assert landed != master
        assert clone_storage._is_relative_to(landed, clone_root), landed

        # 2. The shared session is still reachable, by name.
        default = await spawn(session="default", headless=True, **sandbox_kwargs())
        ids["default"] = default["instance_id"]
        selection = _selection(default)
        assert selection["profile_role"] == "default", selection
        assert Path(selection["user_data_dir"]) == master

        # ...and closing it without saying so is refused, the browser untouched.
        with pytest.raises(ToolError, match='session="default"'):
            await close(instance_id=ids["default"])
        listed = {row["instance_id"] for row in await list_instances()}
        assert ids["default"] in listed, "the refused close closed it anyway"
        assert await eval_js(ids["default"], "1 + 1") == 2

        # 3. Naming the session closes it.
        answer = await close(instance_id=ids.pop("default"), session="default")
        assert answer["closed"] is True

        # The clone closes as before, with no session at all.
        answer = await close(instance_id=ids.pop("unnamed"))
        assert answer["closed"] is True
    finally:
        for instance_id in ids.values():
            with contextlib.suppress(Exception):
                await close(instance_id=instance_id, session="default")
        await released(master)
