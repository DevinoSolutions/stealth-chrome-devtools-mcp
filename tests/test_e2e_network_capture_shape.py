"""F-803 E2E — the SHAPE of what network capture returns (real headless Chrome).

Released 2.0.0 captured 30 rows for one page load: ``resource_type`` was ``None``
on every one of them, ``filter_type="document"`` therefore matched zero rows, and
24 of the 30 were ``chrome://new-tab-page/*`` noise from Chrome's own startup.
Each of those three is a live assertion here — a unit test can pin the handler,
but only a real browser proves the CDP event really carries what we now read and
that Chrome really emits the internal traffic we now drop.

Style follows the plan_E2E suite (``e2e_helpers`` mechanism, the session-scoped
fixture app, bounded polling, every spawn closed in ``finally``). Assertions are
about our own fixture URLs and about the ABSENCE of internal schemes — never
about a total request count, which Chrome makes non-deterministic.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

# Kept in step with network_interceptor.INTERNAL_URL_SCHEMES; spelled out here so
# the test states the user-visible contract rather than importing the answer.
INTERNAL_PREFIXES = (
    "chrome:",
    "chrome-error:",
    "chrome-extension:",
    "chrome-native:",
    "chrome-search:",
    "chrome-untrusted:",
    "devtools:",
    "about:",
)


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _poll_rows(iid: str, url_substr: str, timeout: float = 10.0):
    """Bounded-poll list_network_requests until a row's URL contains
    ``url_substr``; return the full row list (possibly without a match)."""
    list_requests = get_fn("list_network_requests")
    deadline = time.monotonic() + timeout
    rows: list = []
    while time.monotonic() < deadline:
        result = await list_requests(instance_id=iid)
        rows = result if isinstance(result, list) else []
        if any(url_substr in (r.get("url") or "") for r in rows):
            return rows
        await asyncio.sleep(0.25)
    return rows


async def test_capture_shape_resource_type_filter_and_no_internal_noise(
    fixture_app_server,
):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    list_requests = get_fn("list_network_requests")
    get_request_details = get_fn("get_request_details")
    get_filters = get_fn("get_network_capture_filters")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        # network.html pulls styles.css + app.js, so a single load yields several
        # distinct CDP resource types (Document / Stylesheet / Script).
        await navigate_and_settle(iid, f"{base}/network.html")
        rows = await _poll_rows(iid, "/network.html")
        assert rows, "no network requests captured at all"

        # (1) resource_type is populated. In 2.0.0 this was None on every row.
        typed = [r for r in rows if r.get("resource_type")]
        assert typed, f"every captured row still has a null resource_type: {rows}"
        assert any(r["resource_type"] == "Document" for r in typed), (
            f"no Document row among captured types: "
            f"{sorted({r['resource_type'] for r in typed})}"
        )

        # (2) filter_type is a working feature, and matches case-insensitively:
        # lowercase 'document' must find the CDP-cased 'Document' rows.
        docs = await list_requests(instance_id=iid, filter_type="document")
        assert isinstance(docs, list) and docs, (
            "filter_type='document' returned no rows while the unfiltered call "
            f"returned {len(rows)}"
        )
        assert any("/network.html" in (r.get("url") or "") for r in docs)
        assert all(
            (r.get("resource_type") or "").lower().find("document") >= 0 for r in docs
        )

        # (3) Chrome's own startup traffic never enters the capture.
        internal = [
            r
            for r in rows
            if (r.get("url") or "").lower().startswith(INTERNAL_PREFIXES)
        ]
        assert not internal, f"browser-internal URLs leaked into capture: {internal}"

        # get_request_details agrees with the list row (one home for the type).
        doc_row = next(r for r in docs if "/network.html" in r["url"])
        details = await get_request_details(request_id=doc_row["request_id"])
        assert details["resource_type"] == doc_row["resource_type"]

        # The exclusion is reported, not silent, so an operator can find the knob.
        filters = await get_filters(instance_id=iid)
        assert filters["capture_internal_urls"] is False
    finally:
        await close(instance_id=iid)
