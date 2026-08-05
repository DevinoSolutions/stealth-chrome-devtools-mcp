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
        assert details is not None, "the navigation request was never captured"
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
