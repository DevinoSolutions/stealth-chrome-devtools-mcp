"""F-936 E2E — a spawned browser opens on ``about:blank``, so nothing races the
caller's first navigation.

Chrome launched without a URL opens its own start page, ``chrome://newtab/``
(committed as ``chrome://new-tab-page/``), and loads it while the spawn is
already answering. A ``navigate`` issued in that window could lose to the start
page's own navigation: the tab stayed on the start page, the capture held only
the start page's ``data:image/png`` requests, and the gate reported the caller's
request as "never captured" (F-936, runs 36776048519 and PR #177).

So the pin is on the start page itself, which is deterministic where the race
is not: straight after spawn the tracked tab is on ``about:blank``, it is still
there after the window in which a late start-page commit would land, and the
browser has exactly that one page. Style follows the plan_E2E suite.
"""

from __future__ import annotations

import asyncio

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

# Long enough for the start page to commit on a loaded runner: the probe that
# found F-936 saw it committed on every spawn well inside a second.
LATE_START_PAGE_WINDOW_S = 2.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def test_spawned_tab_starts_on_about_blank_and_stays_there(tmp_empty_root):
    spawn = get_fn("spawn_browser")
    list_tabs = get_fn("list_tabs")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        first = await eval_js(iid, "location.href")
        await asyncio.sleep(LATE_START_PAGE_WINDOW_S)
        later = await eval_js(iid, "location.href")
        tabs = await list_tabs(instance_id=iid)

        assert (first, later) == ("about:blank", "about:blank"), (
            f"the spawned tab was on {first!r}, then {later!r}; Chrome's own "
            "start page races the caller's first navigation (F-936)"
        )
        assert [t.get("url") for t in tabs] == ["about:blank"], tabs
    finally:
        await close(instance_id=iid)
