"""F-937 E2E — a login survives a close and a relaunch of a named session.

Against a real Chrome, on a throwaway session root (never the operator's):

* the launched command line carries ONE merged ``--disable-features`` naming
  every Device Bound Session Credentials feature plus nodriver's own (the switch
  reaches ``chrome://version``);
* a SESSION cookie (no expiry) is still sent by the browser after the session is
  closed and spawned again — the default drops it, ``session.restore_on_startup=1``
  keeps it, and the saved tabs are deleted before the relaunch;
* the relaunch opens on exactly one ``about:blank`` (F-936) and not on the tabs
  the first run left open.

No account is signed in and no credential is typed.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

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
from stealth_chrome_devtools_mcp.embedded import login_persistence, tool_runtime
from stealth_chrome_devtools_mcp.settings import get_settings

pytestmark = integration_pytestmark()

_COOKIE_HEADER_JS = """
(async () => {
  const reply = await fetch('/api/echo', {method: 'POST', body: 'f937'});
  return (await reply.json()).headers.cookie || '';
})()
"""


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(autouse=True)
def _isolated_root(tmp_empty_root, tmp_path, monkeypatch):
    """The throwaway root must win (see ``test_e2e_seed_from_running_source``),
    and the browser record goes to tmp so no live backend reads it."""
    get_settings.cache_clear()
    monkeypatch.setattr(
        tool_runtime.process_cleanup, "pid_file", tmp_path / "browser_pids.json"
    )
    return tmp_empty_root


async def test_session_cookie_and_dbsc_switch_survive_a_relaunch(
    fixture_app_server, tmp_empty_root
):
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    list_tabs = get_fn("list_tabs")
    new_tab = get_fn("new_tab")
    execute_cdp_command = get_fn("execute_cdp_command")
    sessions = tmp_empty_root["sessions"]
    name = f"f937-{uuid.uuid4().hex[:8]}"
    value = uuid.uuid4().hex[:12]

    first = await spawn(session=name, headless=True, **sandbox_kwargs())
    directory = first["spawn_diagnostics"]["profile_selection"]["user_data_dir"]
    assert str(directory).startswith(str(sessions)), directory
    iid = first["instance_id"]
    second_iid = None
    try:
        await navigate_and_settle(iid, "chrome://version")
        command_line = await eval_js(iid, "document.body.innerText")
        switches = [
            part
            for part in command_line.split(" --")
            if part.lstrip("-").startswith("disable-features=")
        ]
        merged = switches[-1].split("=", 1)[1].split()[0].split(",")
        assert set(login_persistence.DBSC_FEATURES) <= set(merged), switches
        assert set(login_persistence.NODRIVER_DISABLED_FEATURES) <= set(merged)

        await navigate_and_settle(iid, f"{fixture_app_server}/index.html")
        await eval_js(iid, f"document.cookie = 'f937_session={value}; Path=/'")
        # Tabs for the relaunch NOT to bring back.
        for _ in range(2):
            await new_tab(instance_id=iid, url=f"{fixture_app_server}/index.html")
        # Quit the way a user does, tabs still open: ``close_instance`` closes
        # them first, which leaves Chrome nothing to restore and hides the
        # saved-tabs half of this node.
        with contextlib.suppress(Exception):
            await execute_cdp_command(instance_id=iid, command="Browser.close")
        await asyncio.sleep(1.0)
        await close(instance_id=iid)
        iid = None
        await released(directory)

        second = await spawn(session=name, headless=True, **sandbox_kwargs())
        second_iid = second["instance_id"]
        tabs = await list_tabs(instance_id=second_iid)
        assert [t.get("url") for t in tabs] == ["about:blank"], tabs

        await navigate_and_settle(second_iid, f"{fixture_app_server}/index.html")
        header = str(await eval_js(second_iid, _COOKIE_HEADER_JS))
        assert f"f937_session={value}" in header, (
            f"the session cookie did not survive the close: {header!r}"
        )
    finally:
        for live in (iid, second_iid):
            if live is not None:
                with contextlib.suppress(Exception):
                    await close(instance_id=live)
        await released(directory)


_SIGNIN_INTERNALS_JS = (
    "(async () => { for (let i = 0; i < 40; i++) {"
    " if (document.body.innerText.includes('Account Consistency')) break;"
    " await new Promise(r => setTimeout(r, 250)); }"
    " return document.body.innerText; })()"
)


async def _signin_internals(iid: str) -> str:
    await navigate_and_settle(iid, "chrome://signin-internals")
    return str(await eval_js(iid, _SIGNIN_INTERNALS_JS))


@pytest.mark.parametrize("opt_out", [False, True], ids=["default-none", "opt-out-dice"])
async def test_browser_signin_is_off_by_default_and_dice_with_the_opt_out(
    tmp_empty_root, monkeypatch, opt_out
):
    """F-938: ``--allow-browser-signin=false`` gives Account Consistency ``None``
    and an inactive reconcilor; the opt-out gives stock ``DICE``. Read from
    ``chrome://signin-internals`` of a throwaway root; nothing is signed in."""
    if opt_out:
        monkeypatch.setenv("STEALTH_MCP_ALLOW_BROWSER_SIGNIN", "true")
    get_settings.cache_clear()
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    name = f"f938-{uuid.uuid4().hex[:8]}"
    result = await spawn(session=name, headless=True, **sandbox_kwargs())
    directory = result["spawn_diagnostics"]["profile_selection"]["user_data_dir"]
    iid = result["instance_id"]
    try:
        text = await _signin_internals(iid)
        line = next((ln for ln in text.splitlines() if "Account Consistency" in ln), "")
        assert line, text[:400]
        if opt_out:
            assert "DICE" in text and "None" not in line, line
        else:
            assert "None" in line and "DICE" not in line, line
            reconcilor = next(
                (ln for ln in text.splitlines() if "Reconcilor State" in ln), ""
            )
            assert not reconcilor or "Inactive" in reconcilor, reconcilor
    finally:
        with contextlib.suppress(Exception):
            await close(instance_id=iid)
        await released(directory)
        get_settings.cache_clear()
