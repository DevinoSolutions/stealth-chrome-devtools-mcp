"""plan_E2E STEP 3 — data-tool E2E against the fixture app (real headless Chrome).

Covers network-debugging (10), element-extraction (9), progressive-cloning (10),
and file-extraction (9). Extraction/clone output is Chrome-version-volatile, so
tests assert invariant keys + fixture-pinned exact values (a substring of the
JSON blob), never live-Chrome goldens. Network assertions filter by our URL
substrings only — never total request counts (Chrome emits background noise).

Determinism per plan §2.6: bounded polling only, the fixture animation is paused,
every URL is base_url-relative, and every spawn closes in ``finally``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

CLONE_OUTPUT_DIR = Path(os.environ["STEALTH_MCP_CLONE_OUTPUT_DIR"]).resolve()


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _find_request(iid: str, url_substr: str, timeout: float = 10.0):
    """Bounded-poll list_network_requests for a request whose URL contains
    ``url_substr``. Returns the request dict or None on timeout."""
    list_requests = get_fn("list_network_requests")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reqs = await list_requests(instance_id=iid)
        if isinstance(reqs, list):
            for r in reqs:
                if url_substr in (r.get("url") or ""):
                    return r
        await asyncio.sleep(0.25)
    return None


async def _poll_response_details(rid: str, timeout: float = 10.0):
    """Bounded-poll get_response_details until it returns a non-None dict.

    Eventual-consistency contract: with body capture on, ``_on_response`` awaits
    the CDP get_response_body fetch BEFORE storing the response record, so a
    request can surface via ``_find_request`` a short window before its response
    metadata exists (reported as a finding). Same deadline/interval style as
    ``_find_request``."""
    get_response_details = get_fn("get_response_details")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        details = await get_response_details(request_id=rid)
        if isinstance(details, dict):
            return details
        await asyncio.sleep(0.25)
    return None


def _first_existing_path(result) -> Path | None:
    """Pull the first string value that names an existing file out of a tool
    result dict (the to-file tools report their path under file_path/filepath/
    path; this stays robust to which key)."""
    if not isinstance(result, dict):
        return None
    for value in result.values():
        if isinstance(value, str) and value.endswith(".json") and Path(value).exists():
            return Path(value)
    return None


# ---------------------------------------------------------------------------
# network-debugging (10): capture opt-in, request/response, export/import, headers
# ---------------------------------------------------------------------------


async def test_network_debugging_flow(fixture_app_server, tmp_path):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    click = get_fn("click_element")
    set_filters = get_fn("set_network_capture_filters")
    get_filters = get_fn("get_network_capture_filters")
    get_request_details = get_fn("get_request_details")
    get_response_content = get_fn("get_response_content")
    search = get_fn("search_network_requests")
    export = get_fn("export_network_data")
    import_data = get_fn("import_network_data")
    modify_headers = get_fn("modify_headers")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/network.html")

        # M9 opt-in: body capture is OFF by default; enable it for this instance.
        assert await set_filters(instance_id=iid, capture_bodies=True) is True
        filters = await get_filters(instance_id=iid)
        assert filters.get("capture_bodies") is True

        # Trigger a same-origin fetch to /api/json and wait for it to be captured.
        assert await click(instance_id=iid, selector="#fetch-json-btn")
        req = await _find_request(iid, "/api/json")
        assert req is not None, "fetch to /api/json was not captured"
        rid = req["request_id"]

        assert isinstance(await get_request_details(request_id=rid), dict)
        # Response metadata lands behind the body CDP fetch when capture is on, so
        # poll for the response record before asserting it (eventual consistency).
        assert isinstance(await _poll_response_details(rid), dict), (
            "response details never became available"
        )

        # Response body parses to the fixture ground truth (value == 42).
        body = await get_response_content(instance_id=iid, request_id=rid)
        assert body is not None
        assert json.loads(body)["value"] == 42

        # search filters by our URL substring only (never a total count).
        hits = await search(instance_id=iid, url_pattern="/api/json")
        assert "/api/json" in json.dumps(hits, default=str)

        # export to tmp_path, then import the same file back.
        exported = await export(instance_id=iid, filepath=str(tmp_path / "net.json"))
        assert exported.get("success") is True
        assert (tmp_path / "net.json").exists()
        assert await import_data(instance_id=iid, filepath=str(tmp_path / "net.json"))

        # modify_headers -> trigger a POST to /api/echo, which reflects request
        # headers (lowercased). Proves whether the injected header propagates.
        assert (
            await modify_headers(instance_id=iid, headers={"X-Fixture-Injected": "yes"})
            is True
        )
        assert await click(instance_id=iid, selector="#post-echo-btn")
        echo = await _find_request(iid, "/api/echo")
        assert echo is not None, "POST to /api/echo was not captured"
        # Same eventual-consistency window: wait for the response record before
        # reading its content.
        assert isinstance(await _poll_response_details(echo["request_id"]), dict)
        echo_body = await get_response_content(
            instance_id=iid, request_id=echo["request_id"]
        )
        reflected = json.loads(echo_body)
        assert reflected["body"] == "fixture-payload"
        assert reflected["headers"].get("x-fixture-injected") == "yes"
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# element-extraction (9): ground-truth styles/structure/assets/animations
# ---------------------------------------------------------------------------


async def test_element_extraction_ground_truth(fixture_app_server):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    extract_styles = get_fn("extract_element_styles")
    extract_structure = get_fn("extract_element_structure")
    extract_events = get_fn("extract_element_events")
    extract_animations = get_fn("extract_element_animations")
    extract_assets = get_fn("extract_element_assets")
    extract_styles_cdp = get_fn("extract_element_styles_cdp")
    extract_related = get_fn("extract_related_files")
    extract_complete_cdp = get_fn("extract_complete_element_cdp")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/extract.html")

        styles = await extract_styles(instance_id=iid, selector="#styled-card")
        styles_blob = json.dumps(styles, default=str)
        assert "rgb(17, 34, 51)" in styles_blob  # color ground truth
        assert "12px" in styles_blob  # padding ground truth

        structure = await extract_structure(
            instance_id=iid, selector="#styled-card", include_children=True
        )
        structure_blob = json.dumps(structure, default=str)
        assert "styled-card" in structure_blob
        # Nested >=3 levels deep is represented somewhere in the tree.
        assert "level-3" in structure_blob or "deep-text" in structure_blob

        events = await extract_events(instance_id=iid, selector="#styled-card")
        assert isinstance(events, dict)

        animations = await extract_animations(instance_id=iid, selector="#styled-card")
        assert "fixture-pulse" in json.dumps(animations, default=str)

        assets = await extract_assets(instance_id=iid, selector="#styled-card")
        assert "data:image/png" in json.dumps(assets, default=str)

        styles_cdp = await extract_styles_cdp(instance_id=iid, selector="#styled-card")
        assert "rgb(17, 34, 51)" in json.dumps(styles_cdp, default=str)

        related = await extract_related(instance_id=iid)
        assert "styles.css" in json.dumps(related, default=str)

        complete_cdp = await extract_complete_cdp(
            instance_id=iid, selector="#styled-card"
        )
        assert isinstance(complete_cdp, dict)
    finally:
        await close(instance_id=iid)


@pytest.mark.characterization
async def test_clone_element_complete_current_shape(fixture_app_server):
    """PINS clone_element_complete current output (known M5b selector-forwarding
    gap: the tool does not forward ``selector`` to its 4 JS sub-extractors). We
    pin the observable shape so the M5b fix surfaces as a deliberate test update.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    clone_complete = get_fn("clone_element_complete")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/extract.html")
        complete = await clone_complete(instance_id=iid, selector="#styled-card")
        # Current behavior: returns a dict (never raises for a present element).
        assert isinstance(complete, dict)
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# progressive-cloning (10): one clone, every expand_*, then list + clear
# ---------------------------------------------------------------------------


async def test_progressive_cloning_walk(fixture_app_server):
    """The progressive-clone tier over real Chrome, post-M5b.

    Pre-M5b every expand_* came back EMPTY: ``comprehensive_element_cloner``'s
    live output shape matched neither shape the readers tried. M5b routed the
    tier onto the canonical engine (``cdp_element_cloner.extract_complete_element``
    — styles via CDP, the rest JS-eval, selector forwarded), so the readers now
    read the shape the engine actually produces.

    #styled-card verifiably has rgb styles, a paused named animation (fixture-
    pulse), a ::before, 2 listeners, and a 3-level child tree. Styles + animations
    are asserted against fixture ground truth (deterministic); events / css_rules
    / pseudo are shape-checked, matching the sibling ``extract_element_events``
    E2E which also only shape-checks those aspects' live capture. The list/clear
    lifecycle tools are asserted for real.
    """
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    clone_progressive = get_fn("clone_element_progressive")
    expand_styles = get_fn("expand_styles")
    expand_events = get_fn("expand_events")
    expand_children = get_fn("expand_children")
    expand_css_rules = get_fn("expand_css_rules")
    expand_pseudo = get_fn("expand_pseudo_elements")
    expand_animations = get_fn("expand_animations")
    list_stored = get_fn("list_stored_elements")
    clear_stored = get_fn("clear_stored_element")
    clear_all = get_fn("clear_all_elements")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/extract.html")

        base_clone = await clone_progressive(instance_id=iid, selector="#styled-card")
        assert "element_id" in base_clone
        assert base_clone["selector"] == "#styled-card"
        element_id = base_clone["element_id"]
        # M5b: the base summary now surfaces real styles (canonical engine forwards
        # selector + produces styles.computed_styles) — was 0 pre-M5b.
        assert base_clone["base"]["summary"]["styles_count"] > 0

        # Styles expansion returns the real computed styles (ground-truth color).
        styles = await expand_styles(element_id=element_id)
        assert styles["total_available"] > 0
        assert "rgb(17, 34, 51)" in json.dumps(styles["styles"], default=str)

        # Events / css_rules / pseudo readers return well-shaped containers (live
        # capture of these aspects varies — the sibling extract_* E2E shape-checks
        # them too).
        assert isinstance(
            (await expand_events(element_id=element_id))["event_listeners"], list
        )
        assert isinstance(
            (await expand_css_rules(element_id=element_id))["css_rules"], list
        )
        assert isinstance(
            (await expand_pseudo(element_id=element_id))["pseudo_elements"], dict
        )

        # Animations expansion surfaces the named fixture animation.
        animations = await expand_animations(element_id=element_id)
        assert "fixture-pulse" in json.dumps(animations, default=str)

        # Children now read structure.children (a real list), not dict keys.
        children = await expand_children(element_id=element_id)
        assert isinstance(children["children"], list)
        assert children["total_available"] == children["returned_count"]
        assert len(children["children"]) > 0

        # list + clear lifecycle works correctly (asserted for real, not pinned).
        stored = await list_stored()
        assert element_id in json.dumps(stored, default=str)

        assert (await clear_stored(element_id=element_id)).get("success") is True
        assert (await clear_all()).get("success") is True
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# file-extraction (9): every to-file tool lands under the redirected dir, then
# list + cleanup
# ---------------------------------------------------------------------------


async def test_file_extraction_walk(fixture_app_server):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    clone_to_file = get_fn("clone_element_to_file")
    complete_to_file = get_fn("extract_complete_element_to_file")
    styles_to_file = get_fn("extract_element_styles_to_file")
    structure_to_file = get_fn("extract_element_structure_to_file")
    events_to_file = get_fn("extract_element_events_to_file")
    animations_to_file = get_fn("extract_element_animations_to_file")
    assets_to_file = get_fn("extract_element_assets_to_file")
    list_clone_files = get_fn("list_clone_files")
    cleanup_clone_files = get_fn("cleanup_clone_files")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/extract.html")

        results = [
            await clone_to_file(instance_id=iid, selector="#styled-card"),
            await complete_to_file(instance_id=iid, selector="#styled-card"),
            await styles_to_file(instance_id=iid, selector="#styled-card"),
            await structure_to_file(instance_id=iid, selector="#styled-card"),
            await events_to_file(instance_id=iid, selector="#styled-card"),
            await animations_to_file(instance_id=iid, selector="#styled-card"),
            await assets_to_file(instance_id=iid, selector="#styled-card"),
        ]

        created = []
        for res in results:
            path = _first_existing_path(res)
            assert path is not None, f"no output file in result: {res}"
            # Landed under the conftest-redirected clone-output dir (pathlib, not
            # a string-prefix compare — Windows/Linux safe).
            assert CLONE_OUTPUT_DIR in path.resolve().parents
            created.append(path)

        listed = await list_clone_files()
        assert isinstance(listed, list)
        assert len(listed) >= len(created)

        # max_age_hours=0 -> every file (ctime in the past) is eligible.
        cleaned = await cleanup_clone_files(max_age_hours=0)
        assert cleaned["deleted_count"] >= 1
        for path in created:
            assert not path.exists()
    finally:
        await close(instance_id=iid)
