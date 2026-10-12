"""Sentry STEALTH-…-1P E2E — the two header call sites against real Chrome.

Both crashed in production with ``AttributeError: 'dict' object has no attribute
'to_json'``: ``uc.cdp.network.set_extra_http_headers`` builds its command frame by
calling ``headers.to_json()``, so a raw ``dict`` fails the moment nodriver's
connection advances the command generator — i.e. inside ``tab.send``, on a real
browser, and nowhere else. The hermetic tier
(``tests/test_extra_headers_cdp.py``) pins the serialization; only this file
proves the command Chrome actually receives is accepted and applied.

Style follows the plan_E2E suite (``e2e_helpers`` mechanism, the session-scoped
fixture app, bounded polling, every spawn closed in ``finally``). Hermetic: the
fixture app binds an ephemeral 127.0.0.1 port, so no external network is touched.
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
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

EXTRA_HEADER_NAME = "X-Stealth-E2E-Extra"
EXTRA_HEADER_VALUE = "extra-headers-e2e"


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _request_details_for(iid: str, url_substr: str, timeout: float = 10.0):
    """Bounded-poll the capture for a row whose URL contains ``url_substr`` and
    return its ``get_request_details`` payload (or ``None`` at the deadline)."""
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


async def test_spawn_with_extra_headers_launches_and_sends_them(fixture_app_server):
    """A spawn carrying extra_headers must reach a usable page — and the header
    must reach the server, not merely fail to crash on the way out."""
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")

    # Before the fix this raised AttributeError HERE, during spawn: the CDP
    # command is sent while applying post-launch overrides.
    result = await spawn(
        headless=True,
        extra_headers={EXTRA_HEADER_NAME: EXTRA_HEADER_VALUE},
        **sandbox_kwargs(),
    )
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{fixture_app_server}/network.html")
        assert await eval_js(iid, "document.readyState") in ("interactive", "complete")

        details = await _request_details_for(iid, "/network.html")
        assert details is not None, (
            "the navigation request was never captured\n"
            f"{await capture_miss_report(iid)}"
        )
        sent = {k.lower(): v for k, v in (details.get("headers") or {}).items()}
        assert sent.get(EXTRA_HEADER_NAME.lower()) == EXTRA_HEADER_VALUE, (
            f"extra header absent from the outgoing request: {sorted(sent)}"
        )
    finally:
        await close(instance_id=iid)


async def test_navigate_with_a_referrer_applies_it(fixture_app_server):
    """``navigate(referrer=...)`` sends the same CDP command from a second site.

    ``document.referrer`` is read back from the page because it is Chrome's own
    view of the header it received — an assertion the tool cannot fake.
    """
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    close = get_fn("close_instance")
    referrer = f"{fixture_app_server}/referrer-origin.html"

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        # Before the fix this raised AttributeError inside navigate's retry loop.
        nav = await navigate(
            instance_id=iid,
            url=f"{fixture_app_server}/network.html",
            referrer=referrer,
        )
        assert nav["success"] is True
        assert await eval_js(iid, "document.referrer") == referrer
    finally:
        await close(instance_id=iid)


# A referrer no server answers for: Chrome's default policy would send a
# cross-origin target the ORIGIN only, so a full URL header was cancelled
# (F-965). It is exactly what a caller walking from one site to another passes.
CROSS_ORIGIN_REFERRER = "https://referrer.invalid/some/path?q=1"


async def test_navigate_with_a_cross_origin_referrer_loads_and_delivers_it(
    fixture_app_server,
):
    """F-965 (a): RED before the fix — ``navigate`` set ``Referer`` as an extra
    header, and Chrome cancelled the request (``net::ERR_BLOCKED_BY_CLIENT``)
    because the default policy would not send that full URL cross-origin."""
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        nav = await navigate(
            instance_id=iid,
            url=f"{fixture_app_server}/network.html",
            referrer=CROSS_ORIGIN_REFERRER,
        )
        assert nav["success"] is True
        assert await eval_js(iid, "document.referrer") == CROSS_ORIGIN_REFERRER
    finally:
        await close(instance_id=iid)


async def test_a_referrer_does_not_stick_to_the_tabs_later_navigations(
    fixture_app_server,
):
    """F-965 (b): RED before the fix — the header outlived its navigation, so the
    next plain ``navigate`` in the tab was blocked too (even example.com)."""
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate(
            instance_id=iid,
            url=f"{fixture_app_server}/network.html",
            referrer=CROSS_ORIGIN_REFERRER,
        )
        nav = await navigate(instance_id=iid, url=f"{fixture_app_server}/cookies.html")
        assert nav["success"] is True
        assert await eval_js(iid, "location.pathname") == "/cookies.html"
        # Nothing was named for this one, so nothing is delivered.
        assert await eval_js(iid, "document.referrer") == ""
    finally:
        await close(instance_id=iid)


async def test_a_referrer_leaves_the_spawn_time_extra_headers_in_place(
    fixture_app_server,
):
    """F-965 (c): RED before the fix — ``setExtraHTTPHeaders`` REPLACES the whole
    set, so a referrer navigation silently dropped what ``extra_headers`` had
    installed."""
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    close = get_fn("close_instance")

    result = await spawn(
        headless=True,
        extra_headers={EXTRA_HEADER_NAME: EXTRA_HEADER_VALUE},
        **sandbox_kwargs(),
    )
    iid = result["instance_id"]
    try:
        nav = await navigate(
            instance_id=iid,
            url=f"{fixture_app_server}/network.html",
            referrer=CROSS_ORIGIN_REFERRER,
        )
        assert nav["success"] is True

        details = await _request_details_for(iid, "/network.html")
        assert details is not None, (
            "the navigation request was never captured\n"
            f"{await capture_miss_report(iid)}"
        )
        sent = {k.lower(): v for k, v in (details.get("headers") or {}).items()}
        assert sent.get(EXTRA_HEADER_NAME.lower()) == EXTRA_HEADER_VALUE, (
            f"extra header dropped by the referrer navigation: {sorted(sent)}"
        )
    finally:
        await close(instance_id=iid)
