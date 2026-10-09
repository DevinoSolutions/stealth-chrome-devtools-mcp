"""Real-Chrome proof for F-952: the ``fleet`` session is shared, seeds clones and survives close.

The hermetic pins in ``test_fleet_session.py`` and ``test_session_lease.py``
prove the decisions against doubles. This node uses one real headless Chrome as
the ``fleet`` session on the node's own tmp session root and checks the four
things a caller relies on, with the fixture server's echo of the ``Cookie``
header as the oracle (as in ``test_e2e_seed_from_running_source``):

1. a second ``spawn_browser(session="fleet")`` gets the SAME instance back with
   ``already_running``, not a walked ``fleet-2``;
2. a session seeded from it with ``seed_from="fleet"`` sends the cookie set on
   the fleet's page;
3. the lock tools take turns, refuse with the holder, and release;
4. ``close_instance`` leaves the profile directory in place.

Only the node's tmp root and a tmp browser record are touched.
"""

import contextlib
import uuid
from pathlib import Path

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
    cookie_handoff,
    session_lease,
    tool_runtime,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings

pytestmark = integration_pytestmark()

_COOKIE_HEADER_JS = """
(async () => {
  const reply = await fetch('/api/echo', {method: 'POST', body: 'f952'});
  const answer = await reply.json();
  return answer.headers.cookie || '';
})()
"""


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


@pytest.fixture(autouse=True)
def _fresh_leases():
    session_lease.reset()
    yield
    session_lease.reset()


async def _cookie_header(instance_id: str) -> str:
    return str(await eval_js(instance_id, _COOKIE_HEADER_JS))


async def test_the_fleet_is_shared_seeds_a_clone_and_survives_close(
    fixture_app_server, tmp_empty_root
):
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    acquire = get_fn("acquire_session_lock")
    release = get_fn("release_session_lock")
    status = get_fn("get_session_lock_status")
    sessions = tmp_empty_root["sessions"]
    value = uuid.uuid4().hex[:12]
    clone_name = f"f952-job-{uuid.uuid4().hex[:8]}"

    fleet = await spawn(session="fleet", headless=True, **sandbox_kwargs())
    fleet_id = fleet["instance_id"]
    fleet_dir = Path(fleet["spawn_diagnostics"]["profile_selection"]["user_data_dir"])
    clone_id = None
    clone_dir = None
    try:
        assert fleet_dir == sessions / "fleet", fleet_dir
        assert "already_running" not in fleet
        await navigate_and_settle(fleet_id, f"{fixture_app_server}/index.html")
        await eval_js(
            fleet_id,
            f"document.cookie = 'f952_login={value}; Path=/; Max-Age=3600'; "
            "document.cookie.length",
        )
        assert value in await _cookie_header(fleet_id), "the control"

        # 1. Asked for again while it runs: the same browser, marked.
        again = await spawn(session="fleet", headless=True, **sandbox_kwargs())
        assert again["instance_id"] == fleet_id
        assert again["already_running"] is True
        assert not (sessions / "fleet-2").exists()

        # 2. A session seeded from it carries the login the server can see.
        result = await spawn(
            session=clone_name,
            seed_from="fleet",
            headless=True,
            **sandbox_kwargs(),
        )
        clone_id = result["instance_id"]
        selection = result["spawn_diagnostics"]["profile_selection"]
        clone_dir = selection["user_data_dir"]
        assert str(clone_dir).startswith(str(sessions)), clone_dir
        assert selection["seeded_via"] == cookie_handoff.VIA_CDP, selection
        assert selection["seeded_from"] == "fleet", selection
        assert "seed_warning" not in result, result
        await navigate_and_settle(clone_id, f"{fixture_app_server}/index.html")
        assert f"f952_login={value}" in await _cookie_header(clone_id)

        # 3. The lock takes turns.
        held = await acquire(owner="agent-a", lease_seconds=60)
        assert held["acquired"] is True and held["holder"] == "agent-a"
        with pytest.raises(ToolError, match="agent-a"):
            await acquire(owner="agent-b")
        assert (await status())["holder"] == "agent-a"
        with pytest.raises(ToolError):
            await release(owner="agent-b")
        await release(owner="agent-a")
        assert (await status())["locked"] is False

        # 4. Closing the browser keeps the profile.
        await close(instance_id=clone_id)
        clone_id = None
        await close(instance_id=fleet_id)
        fleet_id = None
        await released(fleet_dir)
        assert fleet_dir.is_dir(), "close_instance deleted the fleet session"
        assert any(fleet_dir.iterdir()), "the fleet profile was emptied"
    finally:
        for iid in (clone_id, fleet_id):
            if iid is not None:
                with contextlib.suppress(Exception):
                    await close(instance_id=iid)
        for directory in (clone_dir, fleet_dir):
            if directory is not None:
                await released(directory)
