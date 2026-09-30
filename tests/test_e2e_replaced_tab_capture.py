"""F-935 E2E — a tab the manager starts driving is captured like the first one.

Network capture, ``extra_headers`` and ``timezone_id`` were applied to the tab
``spawn_browser`` launched with and to no other. Every later change of the
tracked tab — the stale-tab recovery in ``get_navigation_tab``, the navigation
recycle threshold, the retry after a failed navigation, ``switch_tab``, and the
re-point after ``close_tab`` — handed the tools a tab nobody had armed: the page
loaded, ``navigate`` answered success, and ``list_network_requests`` stayed empty
for good. On the Windows gate it surfaced as three different tests that each
found their first request "never captured" right after a readyState check passed.

Each case below drives the product's OWN path to a new tracked tab and then asks
the capture — and the server's view of the spawn header — about a request made
on it. Hermetic: the fixture app binds an ephemeral 127.0.0.1 port.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from e2e_helpers import (
    capture_miss_report,
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    runtime,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

HEADER_NAME = "X-F935-Spawn-Header"
HEADER_VALUE = "rearmed"
TIMEZONE = "Pacific/Auckland"


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _captured(iid: str, url_substr: str, timeout: float = 10.0):
    """Bounded poll for a captured row whose URL contains ``url_substr``; its
    ``get_request_details`` payload, or ``None`` at the deadline."""
    list_requests = get_fn("list_network_requests")
    details_fn = get_fn("get_request_details")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = await list_requests(instance_id=iid)
        if isinstance(rows, list):
            match = next((r for r in rows if url_substr in (r.get("url") or "")), None)
            if match:
                return await details_fn(request_id=match["request_id"])
        await asyncio.sleep(0.25)
    return None


async def _tracked_target_id(iid: str) -> str:
    tab = await runtime.browser_manager.get_tab(iid)
    return str(tab.target.target_id)


async def _force_recycle(iid: str, monkeypatch) -> None:
    """The NAVIGATION_RECYCLE_THRESHOLD path: the next navigate replaces."""
    monkeypatch.setattr(runtime.browser_manager, "NAVIGATION_RECYCLE_THRESHOLD", 1)


async def _force_failed_health_check(iid: str, monkeypatch) -> None:
    """The stale-tab recovery path: ``get_navigation_tab``'s one
    ``update_targets`` raises, so the next navigate replaces the tab without
    closing it — the shape a transient CDP hiccup takes on a loaded runner."""
    browser = await runtime.browser_manager.get_browser(iid)
    real = browser.update_targets
    calls = {"n": 0}

    async def flaky_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("F-935: simulated transient target-list failure")
        return await real()

    monkeypatch.setattr(browser, "update_targets", flaky_once)


async def _switch_to_new_tab(iid: str, monkeypatch) -> None:
    """``new_tab`` + ``switch_tab``: the caller moves the tools onto another tab."""
    opened = await get_fn("new_tab")(instance_id=iid, url="about:blank")
    assert await get_fn("switch_tab")(instance_id=iid, tab_id=opened["tab_id"])


@pytest.mark.parametrize(
    "move_to_new_tab",
    [_force_recycle, _force_failed_health_check, _switch_to_new_tab],
    ids=["recycle-threshold", "failed-health-check", "switch-tab"],
)
async def test_a_new_tracked_tab_is_captured_and_keeps_the_spawn_overrides(
    fixture_app_server, monkeypatch, move_to_new_tab
):
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")

    result = await spawn(
        headless=True,
        extra_headers={HEADER_NAME: HEADER_VALUE},
        timezone_id=TIMEZONE,
        **sandbox_kwargs(),
    )
    iid = result["instance_id"]
    try:
        # The spawn tab is armed: this half passed before the fix too, and is
        # what makes a miss below about the NEW tab rather than the fixture.
        await navigate_and_settle(iid, f"{fixture_app_server}/network.html?tab=first")
        assert await _captured(iid, "tab=first") is not None, (
            f"the spawn tab's request was never captured\n{await capture_miss_report(iid)}"
        )
        first_tab = await _tracked_target_id(iid)

        await move_to_new_tab(iid, monkeypatch)
        await navigate_and_settle(iid, f"{fixture_app_server}/network.html?tab=second")
        assert await _tracked_target_id(iid) != first_tab, (
            "the scenario never moved the tools onto a new tab, so it proves nothing"
        )

        details = await _captured(iid, "tab=second")
        assert details is not None, (
            "a request on the new tracked tab was never captured\n"
            f"{await capture_miss_report(iid)}"
        )
        sent = {k.lower(): v for k, v in (details.get("headers") or {}).items()}
        assert sent.get(HEADER_NAME.lower()) == HEADER_VALUE, (
            f"the spawn's extra header is missing on the new tab: {sorted(sent)}"
        )
        zone = await eval_js(iid, "Intl.DateTimeFormat().resolvedOptions().timeZone")
        assert zone == TIMEZONE, (
            f"the spawn's timezone is missing on the new tab: {zone}"
        )
    finally:
        await close(instance_id=iid)
