"""F-939 E2E — a clone cannot rotate, the master can; a live jar crosses a debug port.

Against real Chromes on a throwaway session root (never the operator's), with no
Google account and no Google request:

* the rotation patterns are swapped for the fixture server's own
  ``/RotateCookies`` path, because the product's patterns name Google hosts. The
  oracle is the page's own ``fetch``: a blocked request REJECTS, a served one
  RESOLVES (the fixture server answers it, 404 or not);
* the master (first unnamed spawn) is served; the clone (second unnamed spawn,
  taken while the master is open) is blocked in its main frame, in a same-origin
  iframe, in a popup and in a tab opened afterwards;
* a cookie in a browser this backend holds no object for crosses to a spawned
  browser over that browser's debug port, and the server sees it.
"""

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
from stealth_chrome_devtools_mcp.embedded import (
    cookie_handoff,
    google_rotation_guard,
    tool_runtime,
)
from stealth_chrome_devtools_mcp.settings import get_settings

pytestmark = integration_pytestmark()

_ROTATE_JS = """
(async () => {
  try {
    const reply = await fetch('/RotateCookies?x=1');
    return 'served:' + reply.status;
  } catch (error) {
    return 'blocked';
  }
})()
"""


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(autouse=True)
def _isolated(tmp_empty_root, tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setattr(
        tool_runtime.process_cleanup, "pid_file", tmp_path / "browser_pids.json"
    )
    return tmp_empty_root


@pytest.fixture
def fixture_patterns(monkeypatch):
    """Point the guard at the fixture server's rotation path."""
    original = google_rotation_guard.arm

    async def arm(browser, patterns=()):
        return await original(browser, ["*://127.0.0.1:*/RotateCookies*"])

    monkeypatch.setattr(google_rotation_guard, "arm", arm)


async def test_a_clone_is_blocked_everywhere_and_the_master_is_not(
    fixture_app_server, fixture_patterns
):
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    new_tab = get_fn("new_tab")
    master = await spawn(headless=True, **sandbox_kwargs())
    clone = None
    directories = []
    try:
        selection = master["spawn_diagnostics"]["profile_selection"]
        assert selection["profile_role"] == "default", selection
        directories.append(selection["user_data_dir"])
        clone = await spawn(headless=True, **sandbox_kwargs())
        clone_selection = clone["spawn_diagnostics"]["profile_selection"]
        assert clone_selection["profile_role"] == "clone", clone_selection
        directories.append(clone_selection["user_data_dir"])
        iid = clone["instance_id"]

        page = f"{fixture_app_server}/index.html"
        await navigate_and_settle(master["instance_id"], page)
        assert (await eval_js(master["instance_id"], _ROTATE_JS)).startswith(
            "served"
        ), "the master must still rotate"

        await navigate_and_settle(iid, page)
        assert await eval_js(iid, _ROTATE_JS) == "blocked"

        await eval_js(
            iid,
            "document.body.insertAdjacentHTML('beforeend',"
            f" '<iframe id=f src=\"{page}\"></iframe>')",
        )
        frame = await eval_js(
            iid,
            "new Promise(r => setTimeout(async () => {"
            " try { await document.getElementById('f').contentWindow"
            ".fetch('/RotateCookies'); r('served'); } catch (e) { r('blocked'); }"
            " }, 1500))",
        )
        assert frame == "blocked", "a same-origin iframe must be blocked too"

        await new_tab(instance_id=iid, url=page)
        tabs = await get_fn("list_tabs")(instance_id=iid)
        for tab in tabs:
            await get_fn("switch_tab")(instance_id=iid, tab_id=tab["tab_id"])
            if tab["url"].startswith(fixture_app_server):
                assert await eval_js(iid, _ROTATE_JS) == "blocked", tab
    finally:
        for live in (clone and clone["instance_id"], master["instance_id"]):
            if live:
                with contextlib.suppress(Exception):
                    await close(instance_id=live)
        for directory in directories:
            await released(directory)


async def test_a_jar_crosses_a_debug_port_to_a_spawned_browser(
    fixture_app_server, tmp_empty_root
):
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    value = uuid.uuid4().hex[:12]
    source_instance = None
    source_dir = None
    target = None
    directory = None
    try:
        source_instance = await spawn(
            session=f"f939-src-{uuid.uuid4().hex[:8]}",
            headless=True,
            **sandbox_kwargs(),
        )
        source_dir = source_instance["spawn_diagnostics"]["profile_selection"][
            "user_data_dir"
        ]
        manager = tool_runtime.browser_manager
        source = await manager.get_browser(source_instance["instance_id"])
        tab = await source.get(f"{fixture_app_server}/index.html")
        await tab.evaluate(
            f"document.cookie = 'f939_live={value}; Path=/; Max-Age=3600'"
        )
        port = source.config.port

        target = await spawn(
            session=f"f939-{uuid.uuid4().hex[:8]}", headless=True, **sandbox_kwargs()
        )
        directory = target["spawn_diagnostics"]["profile_selection"]["user_data_dir"]
        browser = await manager.get_browser(target["instance_id"])
        handoff = await cookie_handoff.hand_off_from_port(port, browser)
        assert handoff.sent >= 1 and handoff.jar_after >= 1

        await navigate_and_settle(
            target["instance_id"], f"{fixture_app_server}/index.html"
        )
        header = str(
            await eval_js(
                target["instance_id"],
                "(async () => (await (await fetch('/api/echo', {method:'POST',"
                " body:'f939'})).json()).headers.cookie || '')()",
            )
        )
        assert f"f939_live={value}" in header, header
    finally:
        if source_instance is not None:
            with contextlib.suppress(Exception):
                await close(instance_id=source_instance["instance_id"])
        if source_dir is not None:
            await released(source_dir)
        if target is not None:
            with contextlib.suppress(Exception):
                await close(instance_id=target["instance_id"])
        if directory is not None:
            await released(directory)
