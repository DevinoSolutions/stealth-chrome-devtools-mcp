"""Vault mode in a real Chrome (T-19366, fifth review).

While a vault file exists every raw or unbounded capability is refused from one home
(``vault_mode``). The unit tests pin each rule; this file proves the three that a
mock cannot: that the tools refuse in the real registry, that a browser started
BEFORE the vault is pinned to a fixed download folder afterwards, and that a
``file:`` page is barred and blanked wherever it came from.

The attacker's route is the one the audit found: a browser whose download folder was
set to a trap (``Browser.setDownloadBehavior`` over a raw connection, which is what
``execute_cdp_command`` did on main), then a blob download of ``evil.pth``.

Real Chrome; the raw connection in the tests is the test's own, never a tool.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from nodriver import cdp

import test_login_flow as flow
from e2e_helpers import eval_js, get_fn, sandbox_kwargs
from stealth_chrome_devtools_mcp.embedded import (
    debug_port_guard,
    login_persistence,
    login_seal,
    tab_open,
    vault_mode,
    vault_store,
)
from stealth_chrome_devtools_mcp.embedded import tool_runtime as runtime
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings
from vault_helpers import make_vault, spawn_before_the_vault

pytestmark = flow.pytestmark
site, vault, _warmup = flow.site, flow.vault, flow._warmup

#: The guard's pass interval for these tests: a vault made after the browser started
#: is pinned within this long.
FAST = 0.4

DOWNLOAD = (
    "(() => { const a = document.createElement('a');"
    " a.href = URL.createObjectURL(new Blob(['payload'], {type: 'text/plain'}));"
    " a.download = 'evil.pth'; document.body.appendChild(a); a.click(); return 'clicked'; })()"
)


async def _browser_of(iid):
    return (await runtime.browser_manager.get_instance(iid))["browser"]


def _make_vault_file():
    make_vault({})
    assert vault_store.VAULT_PATH.exists()


async def _wait_for(check, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        await asyncio.sleep(0.1)
    return check()


def _files(folder):
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@pytest.fixture
def fast_guard(monkeypatch):
    monkeypatch.setattr(debug_port_guard, "KEEP_SECONDS", FAST)


async def _targets(browser):
    infos = await tab_open.send(browser, cdp.target.get_targets)
    return [(str(i.target_id), str(i.type_), str(i.url)) for i in infos]


async def test_the_tools_that_forward_raw_power_are_refused_while_a_vault_exists(
    site, tmp_path, fast_guard
):
    async with flow._browser(tmp_path) as iid:
        # no vault: everything is as on main
        version = await get_fn("execute_cdp_command")(
            instance_id=iid, command="Browser.getVersion", params={}
        )
        assert version, version
        _make_vault_file()
        refusals = []
        for call in (
            lambda: get_fn("execute_cdp_command")(
                instance_id=iid, command="Browser.getVersion", params={}
            ),
            lambda: get_fn("spawn_browser")(
                headless=True, browser_args=["--log-file=" + str(tmp_path / "x.log")]
            ),
            lambda: get_fn("spawn_browser")(
                headless=True, user_data_dir=str(tmp_path / "another-profile")
            ),
            lambda: get_fn("navigate")(
                instance_id=iid, url=(tmp_path / "a.html").as_uri()
            ),
        ):
            with pytest.raises(ToolError) as caught:
                await call()
            refusals.append(str(caught.value))
        for text in refusals:
            assert "stealthy login destroy" in text, text
        assert "execute_cdp_command" in refusals[0]
        assert "log-file" in refusals[1]
        # what a login needs still works: a plain tab on the site
        await get_fn("navigate")(instance_id=iid, url=site.url("/login"))
        assert "password" in await eval_js(iid, "return document.body.innerHTML;")


async def test_a_browser_started_before_the_vault_is_pinned_to_the_fixed_folder(
    site, tmp_path, monkeypatch, fast_guard
):
    pinned = tmp_path / "pinned"
    trap = tmp_path / "trap"
    trap.mkdir()
    monkeypatch.setattr(vault_mode, "download_dir", lambda: pinned)
    vault_mode._reset_for_tests()
    async with flow._browser(tmp_path) as iid:
        browser = await _browser_of(iid)
        await get_fn("navigate")(instance_id=iid, url=site.url("/login"))
        # the audit's setup: a trap folder, set over a raw connection, no vault yet
        await tab_open.send(
            browser,
            lambda: cdp.browser.set_download_behavior("allow", download_path=str(trap)),
        )
        assert await eval_js(iid, f"return {DOWNLOAD};") == "clicked"
        assert await _wait_for(lambda: "evil.pth" in _files(trap)), _files(trap)
        assert _files(pinned) == []  # without a vault nothing is pinned

        # the vault appears; the browser was started without it
        (trap / "evil.pth").unlink()
        _make_vault_file()
        assert await _wait_for(lambda: pinned.exists()), "the pin was never made"
        await asyncio.sleep(FAST * 3)
        # the trap is set again from outside, the pass puts the pin back
        await tab_open.send(
            browser,
            lambda: cdp.browser.set_download_behavior("allow", download_path=str(trap)),
        )
        # The pass puts the pin back within KEEP_SECONDS (plus the time the pass takes
        # on a loaded machine): a download made in that window may still land in the
        # trap, so try until one lands in the pinned folder, then require that the
        # NEXT one does too and that the trap got nothing after the pin came back.
        landed: list[str] = []
        for _ in range(12):
            await asyncio.sleep(FAST * 2)
            assert await eval_js(iid, f"return {DOWNLOAD};") == "clicked"
            landed = await _wait_for(lambda: _files(pinned), seconds=3.0)
            if landed:
                break
        assert landed, "the pin was never put back"
        assert not any("evil" in name for name in landed), (
            landed
        )  # named by a GUID, never by the page
        for leftover in _files(trap):
            (trap / leftover).unlink()
        count = len(_files(pinned))
        assert await eval_js(iid, f"return {DOWNLOAD};") == "clicked"
        assert await _wait_for(lambda: len(_files(pinned)) > count), _files(pinned)
        assert _files(trap) == [], _files(trap)


async def test_a_tab_level_download_folder_set_before_the_vault_is_taken_back(
    site, tmp_path, monkeypatch, fast_guard
):
    """Audit 6, N3: ``Page.setDownloadBehavior`` is stored per tab, and raw CDP could
    set it before the vault existed. Measured here, on the Chrome this runs against,
    not assumed: a tab-level folder set before the vault does NOT win over the
    browser-level pin (the first download after the pin lands in the pinned folder),
    so the keeper needs no per-page reset. If a Chrome release changes that, this
    test goes red and the keeper must reset the page targets too."""
    pinned = tmp_path / "pinned-tab"
    trap = tmp_path / "trap-tab"
    trap.mkdir()
    monkeypatch.setattr(vault_mode, "download_dir", lambda: pinned)
    vault_mode._reset_for_tests()
    async with flow._browser(tmp_path) as iid:
        await get_fn("navigate")(instance_id=iid, url=site.url("/login"))
        tab = await runtime.browser_manager.get_tab(iid)
        await tab.send(cdp.page.set_download_behavior("allow", download_path=str(trap)))
        # control: with no vault the tab's own setting is what decides (so the claim
        # below is about the pin, not about a setting that never took effect)
        assert await eval_js(iid, f"return {DOWNLOAD};") == "clicked"
        assert await _wait_for(lambda: "evil.pth" in _files(trap)), _files(trap)
        (trap / "evil.pth").unlink()
        _make_vault_file()
        assert await _wait_for(lambda: pinned.exists()), "the pin was never made"
        landed: list[str] = []
        for _ in range(12):
            await asyncio.sleep(FAST * 3)
            assert await eval_js(iid, f"return {DOWNLOAD};") == "clicked"
            landed = await _wait_for(lambda: _files(pinned), seconds=3.0)
            if landed:
                break
        assert landed, "the tab's own folder won over the pin"
        for leftover in _files(trap):
            (trap / leftover).unlink()
        count = len(_files(pinned))
        assert await eval_js(iid, f"return {DOWNLOAD};") == "clicked"
        assert await _wait_for(lambda: len(_files(pinned)) > count), _files(pinned)
        assert _files(trap) == [], _files(trap)


async def test_a_file_page_is_barred_and_blanked_wherever_it_came_from(
    site, tmp_path, fast_guard
):
    page = tmp_path / "secret.html"
    page.write_text("<html><body>local file</body></html>", encoding="utf-8")
    url = page.as_uri()
    async with flow._browser(tmp_path) as iid:
        browser = await _browser_of(iid)
        # no vault: a file: page is left alone (this is the product as it was)
        before = await tab_open.send(browser, cdp.target.create_target, url)
        await asyncio.sleep(FAST * 4)
        now = {t: u for t, _k, u in await _targets(browser)}
        assert now.get(str(before), "").startswith("file:"), now

        # the vault appears: the page that was already open is blanked
        _make_vault_file()

        async def blanked():
            return {t: u for t, _k, u in await _targets(browser)}.get(str(before))

        deadline = time.monotonic() + 15
        seen = await blanked()
        while time.monotonic() < deadline and (seen or "").startswith("file:"):
            await asyncio.sleep(0.2)
            seen = await blanked()
        assert not (seen or "").startswith("file:"), seen

        # and one opened over a raw connection afterwards is blanked too
        after = await tab_open.send(browser, cdp.target.create_target, url)
        deadline = time.monotonic() + 15
        seen = url
        while time.monotonic() < deadline and seen.startswith("file:"):
            await asyncio.sleep(0.2)
            seen = {t: u for t, _k, u in await _targets(browser)}.get(str(after), "")
        assert not seen.startswith("file:"), seen


async def test_a_page_tool_with_no_tab_id_refuses_a_chrome_page_left_current(
    site, tmp_path, fast_guard
):
    """Audit 7 F1: the instance's current tab was left on ``chrome://version`` BEFORE
    the vault; a call that names no tab must not act on it, and a tab of its own
    still works."""
    async with flow._browser(tmp_path) as iid:
        # no vault yet: the page loads and is the current tab
        await get_fn("navigate")(instance_id=iid, url="chrome://version")
        assert "version" in (await get_fn("get_page_content")(instance_id=iid))["url"]
        _make_vault_file()
        assert vault_mode.active()
        # the keeper has judged the target: its first pass resolves the path rules,
        # which takes seconds on a cold machine, so wait for the mark, not a time
        assert await _wait_for(login_seal.any_sealing, seconds=30.0), "never marked"
        for call in (
            lambda: get_fn("execute_script")(instance_id=iid, script="1 + 1"),
            lambda: get_fn("get_page_content")(instance_id=iid),
            lambda: get_fn("take_screenshot")(instance_id=iid),
            lambda: get_fn("go_back")(instance_id=iid),
        ):
            with pytest.raises(ToolError) as caught:
                await call()
            text = str(caught.value)
            assert "stealthy login destroy" in text, text
            assert "new_tab" in text, text
        # a tab of its own is not touched by the rule
        opened = await get_fn("new_tab")(instance_id=iid, url="about:blank")
        own = opened["tab_id"]
        answer = await get_fn("execute_script")(
            instance_id=iid, script="1 + 1", tab_id=own
        )
        assert "2" in str(answer), answer
        assert await get_fn("get_page_content")(instance_id=iid, tab_id=own)


async def test_a_browser_spawned_while_the_vault_exists_is_pinned_from_its_first_moment(
    site, tmp_path, monkeypatch
):
    """The spawn path: ``protect`` pins before the browser is handed over, so the
    first download already lands in the fixed folder, with no wait for a pass."""
    pinned = tmp_path / "pinned-at-spawn"
    monkeypatch.setattr(vault_mode, "download_dir", lambda: pinned)
    vault_mode._reset_for_tests()
    _make_vault_file()
    # a spawn that is allowed in the mode: no path, no extra switches
    spawned = await get_fn("spawn_browser")(headless=True, **sandbox_kwargs())
    iid = spawned["instance_id"]
    try:
        await get_fn("navigate")(instance_id=iid, url=site.url("/login"))
        assert await eval_js(iid, f"return {DOWNLOAD};") == "clicked"
        landed = await _wait_for(lambda: _files(pinned))
        assert landed and not any("evil" in name for name in landed), landed
    finally:
        await get_fn("close_instance")(instance_id=iid)


async def test_a_named_session_still_keeps_its_logins_while_the_vault_exists(
    tmp_empty_root, tmp_path, monkeypatch
):
    """Audit 6, N6: ``chrome://`` pages are out of reach of the tools in vault mode,
    but the product's own session restore (a persistent profile, F-937) opens
    ``chrome://settings`` to switch ``restore_on_startup`` on. Blanking that page
    would stop logins from being kept across a close, so WebUI pages are hidden, not
    blanked: the pref still gets set with a vault present."""
    get_settings.cache_clear()
    monkeypatch.setattr(
        runtime.process_cleanup, "pid_file", tmp_path / "browser_pids.json"
    )
    _make_vault_file()
    name = f"vm-restore-{uuid.uuid4().hex[:8]}"
    spawned = await get_fn("spawn_browser")(
        session=name, headless=True, **sandbox_kwargs()
    )
    iid = spawned["instance_id"]
    try:
        directory = spawned["spawn_diagnostics"]["profile_selection"]["user_data_dir"]
        assert await _wait_for(
            lambda: login_persistence._pref_is_continue(directory), seconds=60.0
        ), "session.restore_on_startup was never set while a vault existed"
    finally:
        await get_fn("close_instance")(instance_id=iid)


def test_the_helper_that_sets_a_vault_aside_puts_it_back(tmp_path):
    _make_vault_file()
    with spawn_before_the_vault():
        assert not vault_store.VAULT_PATH.exists()
    assert vault_store.VAULT_PATH.exists()
