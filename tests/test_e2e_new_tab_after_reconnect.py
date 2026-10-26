"""F-940 E2E -- a tab can still be opened on a session that never asked Chrome for
``Target.targetCreated``.

On nodriver 0.47 a new tab was found through ``Target.targetCreated``, an event
Chrome only sends on a session that asked for it with ``Target.setDiscoverTargets``.
nodriver asked once, in ``Browser.start``; when the BROWSER-level websocket dropped,
the next ``send`` reconnected it silently and the new session never asked again.
``Browser.get(new_tab=True)`` then created the target, looked it up in a
``browser.targets`` that no event would ever fill, and died on a bare
``next(filter(...))``: ``RuntimeError: coroutine raised StopIteration``.

Measured on Chrome 154 before the fix: 0 of 40 opens failed on a fresh browser,
3 of 3 failed after one forced drop, and re-sending ``setDiscoverTargets`` made
the next one work. In the field (Sentry STEALTH-CHROME-DEVTOOLS-MCP-B5 / -B4 /
-B0, two backends on 2.1.1 and 2.1.18) it failed ``new_tab`` and every ``navigate``
that had to replace its tab -- and the replacement happens on every 25th
navigation, so an affected instance could not navigate again at all.

nodriver 0.50 (T-8501) changed both halves. ``Browser.update_targets`` asks
``Target.getTargets`` and registers a real ``Tab`` for every target, so opening a
tab no longer depends on an event (``tab_open.open_tab``). But a dropped websocket
is no longer reconnected AT ALL: ``Connection.send`` re-attaches only when the
socket is ``None`` (``connection.py``:396), and a dropped one is not, so every
browser-level command raised ``ConnectionClosed`` for the life of the instance.
``tab_open.send`` / ``update_targets`` re-attach once and retry once.

Two pins. The first closes the browser websocket FOR REAL and drives ``navigate``'s
tab replacement (the recycle threshold is lowered to 1 so the second ``navigate``
takes the path B4 died in), ``new_tab``, and a page-opened popup, which must still
be armed on the new connection. The second keeps the precondition 0.47 died in: a
session that is NOT discovering targets.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from nodriver import cdp
from websockets.exceptions import ConnectionClosed

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    runtime,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _drop_browser_socket(iid: str) -> None:
    """Close the browser-level websocket for real; a later command must fail on it
    unless ``tab_open`` repairs it."""
    browser = await runtime.browser_manager.get_browser(iid)
    await browser.socket.close()
    await browser.socket.wait_closed()
    assert browser.socket.close_code is not None
    with pytest.raises(ConnectionClosed):  # 0.50 never repairs this by itself
        await browser.send(cdp.browser.get_version())


async def _stop_discovering(iid: str) -> None:
    """Take the browser connection back to the state F-940 died in: Chrome sends
    it no ``Target.targetCreated`` (``arm_popups`` switched discovery on)."""
    browser = await runtime.browser_manager.get_browser(iid)
    await browser.send(cdp.target.set_discover_targets(False))


async def test_new_tab_and_tab_replacement_without_target_discovery(
    tmp_empty_root, monkeypatch
):
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    new_tab = get_fn("new_tab")
    close = get_fn("close_instance")
    monkeypatch.setattr(
        type(runtime.browser_manager), "NAVIGATION_RECYCLE_THRESHOLD", 1
    )

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate(instance_id=iid, url="data:text/html,<title>one</title>")
        await _stop_discovering(iid)

        # B4: the threshold is reached, so this navigate replaces its tab first.
        await navigate(instance_id=iid, url="data:text/html,<title>two</title>")
        assert await eval_js(iid, "document.title") == "two"

        # B5: the new_tab tool itself.
        opened = await new_tab(instance_id=iid, url="about:blank")
        assert opened["tab_id"], opened
        assert opened["url"] == "about:blank", opened
    finally:
        await close(instance_id=iid)


async def test_a_dropped_browser_socket_is_recovered(tmp_empty_root, monkeypatch):
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    new_tab = get_fn("new_tab")
    close = get_fn("close_instance")
    execute = get_fn("execute_script")
    list_tabs = get_fn("list_tabs")
    manager = type(runtime.browser_manager)
    monkeypatch.setattr(manager, "NAVIGATION_RECYCLE_THRESHOLD", 1)
    armed: list[str] = []
    arm = manager._arm_tracked_tab

    async def spy(self, instance_id, tab):
        armed.append(str(tab.target.target_id))
        return await arm(self, instance_id, tab)

    monkeypatch.setattr(manager, "_arm_tracked_tab", spy)

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate(instance_id=iid, url="data:text/html,<title>one</title>")
        await _drop_browser_socket(iid)

        # The 25th-navigation recycle (threshold 1): browser-level createTarget.
        await navigate(instance_id=iid, url="data:text/html,<title>two</title>")
        assert await eval_js(iid, "document.title") == "two"

        opener = (await new_tab(instance_id=iid, url="about:blank"))["tab_id"]
        known = {t["tab_id"] for t in await list_tabs(instance_id=iid)}
        armed.clear()
        await execute(
            instance_id=iid,
            script="window.open('about:blank#popup', '_blank'); return true;",
            tab_id=opener,
        )
        deadline = time.monotonic() + 15.0
        popup = None
        while popup is None and time.monotonic() < deadline:
            fresh = [
                t["tab_id"]
                for t in await list_tabs(instance_id=iid)
                if t["tab_id"] not in known
            ]
            popup = fresh[0] if fresh and fresh[0] in armed else None
            await asyncio.sleep(0.25)
        assert popup is not None, ("the popup was not armed after recovery", armed)
    finally:
        await close(instance_id=iid)
