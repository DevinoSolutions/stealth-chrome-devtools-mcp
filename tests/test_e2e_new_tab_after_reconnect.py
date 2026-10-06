"""F-940 E2E — a tab can still be opened after the browser's own websocket
reconnected.

nodriver learns about a new tab from ``Target.targetCreated``, an event Chrome
only sends on a session that asked for it with ``Target.setDiscoverTargets``.
nodriver asks once, in ``Browser.start``. When the BROWSER-level websocket drops,
the next ``send`` reconnects it silently — and ``Connection._register_handlers``
skips the Target domain as "enabled by default", so the new session never asks
again. ``Browser.get(new_tab=True)`` then creates the target, looks it up in a
``browser.targets`` that no event will ever fill, and dies on a bare
``next(filter(...))``: ``RuntimeError: coroutine raised StopIteration``.

Measured on Chrome 154 before the fix: 0 of 40 opens failed on a fresh browser,
3 of 3 failed after one forced drop, and re-sending ``setDiscoverTargets`` made
the next one work. In the field (Sentry STEALTH-CHROME-DEVTOOLS-MCP-B5 / -B4 /
-B0, two backends on 2.1.1 and 2.1.18) it failed ``new_tab`` and every ``navigate`` that had to
replace its tab — and the replacement happens on every 25th navigation, so an
affected instance could not navigate again at all.

The pin drives both tools through a real drop: the recycle threshold is lowered
to 1 so the second ``navigate`` takes the tab-replacement path B4 died in.
"""

from __future__ import annotations

import asyncio

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    runtime,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

# How long nodriver's listener gets to notice the close before the next send.
# It polls the socket every 50 ms; a second is generous on a loaded runner.
DROP_NOTICED_S = 1.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _drop_browser_socket(iid: str) -> None:
    """Close the browser-level websocket the way a dropped connection does,
    leaving Chrome and every tab running."""
    browser = await runtime.browser_manager.get_browser(iid)
    await browser.connection.websocket.close()
    await asyncio.sleep(DROP_NOTICED_S)


async def test_new_tab_and_tab_replacement_survive_a_browser_socket_reconnect(
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
        await _drop_browser_socket(iid)

        # B4: the threshold is reached, so this navigate replaces its tab first.
        await navigate(instance_id=iid, url="data:text/html,<title>two</title>")
        assert await eval_js(iid, "document.title") == "two"

        # B5: the new_tab tool itself.
        opened = await new_tab(instance_id=iid, url="about:blank")
        assert opened["tab_id"], opened
        assert opened["url"] == "about:blank", opened
    finally:
        await close(instance_id=iid)
