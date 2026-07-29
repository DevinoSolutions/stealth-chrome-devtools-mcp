"""plan_RELEASE W7 (MQ-114…121) — adversarial breadth against real Chrome.

"Any site" is an unbounded quantifier over an adversarial, changing domain and
no finite suite can prove it. This module therefore owns **eight exact
deterministic shapes** instead: a SPA history swap, cross-origin nested frames,
lazy/virtualized/finite-infinite lists, a strict nonce CSP, basic auth with
redirect chains and a CORS preflight, text/binary/chunked/error payloads, SSE
and WebSocket streams, and a popup with custom elements and slots.

What makes each node evidence rather than a smoke test is the **independent
oracle**: no shape is verified using only the output of the tool under test.
The page keeps its own sentinel, the fixture server keeps its own ledger of what
the browser actually asked for, and the test process talks to that server
directly over HTTP — so a tool that silently did nothing cannot agree with
itself into a green result.

Determinism (plan §2.7): origin A and origin B are independent ``127.0.0.1:0``
servers; every controller is event-backed (a click, a fetch resolution, an
observer entry, a stream message) and released before teardown; every wait is a
bounded poll on an observable value, never a sleep. Every page, route, protocol
behavior, oracle, and test node in this wave lands together.

Scope honesty. These nodes drive the in-process ``.fn`` seam against real
image-provided Chrome Stable, which is what puts them in the ``integration``
lane on all three W2 runners. The stdio wire path is W1's separate claim
(``tests/test_e2e_transport.py``); nothing here re-makes it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
import requests

import fixture_routes as fr
from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    wait_for_js,
    warmup_once,
)

pytestmark = integration_pytestmark()

HTTP_TIMEOUT = 10
STREAM_TIMEOUT = 20.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture()
async def instance():
    """One headless instance per node, always closed (no fleet, no leak)."""
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        yield iid
    finally:
        await close(instance_id=iid)


# ── Small shared readers (mechanism only — no test logic lives here) ────────
async def _json_state(iid: str, expression: str) -> Any:
    """Read a page-side sentinel object as Python via one JSON round trip."""
    raw = await eval_js(iid, f"JSON.stringify({expression})")
    return json.loads(raw) if raw else None


async def _http_get(url: str):
    """Plain HTTP straight from this process — the oracle that does NOT go
    through the browser. Off-thread so it never blocks the loop driving Chrome.
    """
    return await asyncio.to_thread(requests.get, url, timeout=HTTP_TIMEOUT)


async def _reset_ledger(origin: str) -> None:
    await _http_get(f"{origin}/e2e/reset")


async def _ledger(origin: str) -> dict:
    return (await _http_get(f"{origin}/e2e/ledger")).json()


def _unwrap(payload: Any) -> Any:
    """Return a tool result, reading it back from disk if it spilled to a file.

    ``response_handler`` may hand back ``{"file_path": ...}`` for a large
    result; every W7 page is small, so this is a guard that keeps a surprise
    spill from reading as a content mismatch.
    """
    if isinstance(payload, dict) and payload.get("file_path"):
        return json.loads(Path(payload["file_path"]).read_text(encoding="utf-8"))
    return payload


async def _find_request(iid: str, url_substr: str, timeout: float = 15.0):
    """Bounded-poll ``list_network_requests`` for a captured request by URL."""
    list_requests = get_fn("list_network_requests")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _unwrap(await list_requests(instance_id=iid))
        if isinstance(found, list):
            for request in found:
                if url_substr in (request.get("url") or ""):
                    return request
        await asyncio.sleep(0.25)
    return None


async def _response_details(request_id: str, timeout: float = 15.0):
    """Bounded-poll ``get_response_details`` until the record is stored.

    Eventual-consistency: with body capture on, the response record is stored
    only after its CDP body fetch resolves, so the first read can legitimately
    miss.
    """
    get_details = get_fn("get_response_details")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        details = await get_details(request_id=request_id)
        if isinstance(details, dict):
            return details
        await asyncio.sleep(0.25)
    return None


def _lower_headers(details: dict) -> dict[str, str]:
    return {str(k).lower(): v for k, v in (details.get("headers") or {}).items()}


# ═══════════════════════════════════════════════════════════════════════════
# MQ-114 — SPA History API: pushState → replaceState → back/popstate
# ═══════════════════════════════════════════════════════════════════════════
async def test_spa_history_route_swap_and_requery(instance, fixture_origin_pair):
    """MQ-114: every transition replaces ``#route-root`` and bumps a generation;
    a FRESH public selector query + action must land on the new node each time.

    The test never retains an element handle across a transition — it re-queries
    through ``query_elements`` and re-acts through ``click_element`` after every
    swap. The independent oracle is the page's own ``window.__spa.log``, which
    records which generation actually received the action: if a tool had acted
    on a replaced node, the logged generation would not match the queried one.
    """
    origin_a, _ = fixture_origin_pair
    query = get_fn("query_elements")
    click = get_fn("click_element")
    back = get_fn("go_back")

    await navigate_and_settle(instance, f"{origin_a}/spa_history.html")

    async def requery_and_act(route: str, generation: int) -> None:
        roots = _unwrap(await query(instance_id=instance, selector="#route-root"))
        assert isinstance(roots, list) and len(roots) == 1, roots
        assert f"route-{route}-{generation}" in json.dumps(roots), roots
        assert await eval_js(
            instance, "document.getElementById('route-root').dataset.gen"
        ) == str(generation)
        assert await click(instance_id=instance, selector="#route-action") is True

    await requery_and_act("home", 1)
    assert await click(instance_id=instance, selector="#nav-push") is True
    await requery_and_act("alpha", 2)
    assert await click(instance_id=instance, selector="#nav-replace") is True
    await requery_and_act("beta", 3)

    assert await back(instance_id=instance) is True
    assert await wait_for_js(instance, "window.__spa.gen", 4) == 4
    await requery_and_act("home", 4)

    assert await _json_state(instance, "window.__spa.log") == [
        "route:home:1",
        "action:home:1",
        "route:alpha:2",
        "action:alpha:2",
        "route:beta:3",
        "action:beta:3",
        "route:home:4",
        "action:home:4",
    ]


# ═══════════════════════════════════════════════════════════════════════════
# MQ-115 — cross-origin nested frames A → B → A
# ═══════════════════════════════════════════════════════════════════════════
async def test_cross_origin_a_b_a_direct_metadata_and_limit(
    instance, fixture_origin_pair
):
    """MQ-115: ``get_page_content(include_frames=True)`` returns the top-level
    page plus metadata for its DIRECT B child, and the A-B-A page neither hangs
    nor crashes.

    This node also pins the limitation deliberately: there is no public frame
    targeting and no recursive child-frame content, so the innermost A document
    must NOT appear in the result. Asserting its absence is what stops a future
    reader from mistaking direct-child metadata for nested-frame support.
    """
    origin_a, origin_b = fixture_origin_pair
    get_page_content = get_fn("get_page_content")

    await navigate_and_settle(instance, f"{origin_a}/frames/a_outer.html")
    # The inner documents must have loaded, or "no recursive content" would be
    # trivially true for the wrong reason.
    assert (
        await wait_for_js(
            instance,
            "document.getElementById('frame-b-middle').contentWindow === null",
            False,
        )
        is False
    )

    content = _unwrap(await get_page_content(instance_id=instance, include_frames=True))
    assert isinstance(content, dict), content
    assert content["title"] == "fixture-frames-outer"
    assert "fixture-frames-a-outer" in content["text"]
    assert content["frames"] == [
        {
            "index": 0,
            "src": f"{origin_b}/frames/b_middle.html",
            "id": "frame-b-middle",
            "name": "e2e-frame-b",
        }
    ]

    # Documented limitation, asserted rather than assumed.
    blob = json.dumps(content)
    assert "fixture-frames-a-inner" not in blob
    assert "fixture-frames-b-middle" not in content["text"]

    # No hang/crash: the instance is still driveable afterwards.
    assert await eval_js(instance, "document.title") == "fixture-frames-outer"


# ═══════════════════════════════════════════════════════════════════════════
# MQ-116 — IntersectionObserver lazy load
# ═══════════════════════════════════════════════════════════════════════════
async def test_intersection_observer_lazy_load(instance, fixture_origin_pair):
    """MQ-116 (lazy): the token appears only after a CONTROLLED intersection.

    Absence first, then one public scroll, then the token — an observer that
    fired on load, or a token baked into the markup, both fail the first assert.
    """
    origin_a, _ = fixture_origin_pair
    scroll = get_fn("scroll_page")
    query = get_fn("query_elements")
    wait_for_element = get_fn("wait_for_element")

    await navigate_and_settle(instance, f"{origin_a}/lazy_virtual_infinite.html")

    assert await eval_js(instance, "window.__lazy.observed") is False
    assert _unwrap(await query(instance_id=instance, selector="#lazy-token")) == []

    assert await scroll(instance_id=instance, direction="bottom", smooth=False) is True

    assert (
        await wait_for_element(
            instance_id=instance, selector="#lazy-token", timeout=15000
        )
        is True
    )
    assert await wait_for_js(instance, "window.__lazy.observed", True) is True
    tokens = _unwrap(await query(instance_id=instance, selector="#lazy-token"))
    assert isinstance(tokens, list) and len(tokens) == 1, tokens
    assert "lazy-observed" in json.dumps(tokens), tokens


# ═══════════════════════════════════════════════════════════════════════════
# MQ-116 — virtualized pool + finite "infinite" list
# ═══════════════════════════════════════════════════════════════════════════
async def test_virtualized_and_finite_infinite_lists(instance, fixture_origin_pair):
    """MQ-116 (virtual + feed): node identities recycle while logical ids stay
    exact, and the finite-infinite load ends at 100 ordered rows in four
    requests with nothing from page 4.

    Two independent oracles carry this node. Recycling is proven by a JS-only
    property stamped on each pool node at creation (``__nodeStamp``) — invisible
    to the DOM, so if the page had rebuilt its rows the stamps would read
    ``rebuilt`` even though the selector tool returned twenty perfectly good
    elements. Request count is proven by the fixture SERVER's own ledger, read
    over plain HTTP from this process, so the page cannot vouch for itself.
    """
    origin_a, _ = fixture_origin_pair
    query = get_fn("query_elements")
    click = get_fn("click_element")

    await _reset_ledger(origin_a)
    await navigate_and_settle(instance, f"{origin_a}/lazy_virtual_infinite.html")

    async def row_texts() -> list[str]:
        rows = _unwrap(
            await query(instance_id=instance, selector=".v-row", visible_only=False)
        )
        assert isinstance(rows, list) and len(rows) == fr.VIRTUAL_POOL_SIZE, rows
        return await _json_state(
            instance,
            "Array.prototype.map.call(document.querySelectorAll('.v-row'),"
            "function(n){return n.textContent;})",
        )

    expected_stamps = [f"n{i}" for i in range(fr.VIRTUAL_POOL_SIZE)]

    assert await row_texts() == [f"virtual-row-{i}" for i in range(20)]
    assert await _json_state(instance, "window.__virtual.stamps()") == expected_stamps

    assert await click(instance_id=instance, selector="#virtual-advance") is True
    assert await wait_for_js(instance, "window.__virtual.start", 20) == 20

    assert await row_texts() == [f"virtual-row-{i}" for i in range(20, 40)]
    # Same twenty nodes, repainted — the pool never grew and never rebuilt.
    assert await _json_state(instance, "window.__virtual.stamps()") == expected_stamps
    assert await eval_js(instance, "window.__virtual.total") == fr.VIRTUAL_TOTAL_ROWS

    # Finite "infinite" list.
    assert await click(instance_id=instance, selector="#feed-load") is True
    assert await wait_for_js(instance, "window.__feed.done", True, timeout=20.0) is True

    feed = await _json_state(instance, "window.__feed")
    assert feed["requests"] == [f"/api/feed?page={page}" for page in range(4)]
    assert feed["rows"] == [f"feed-row-{i}" for i in range(fr.FEED_TOTAL_ROWS)]
    assert len(set(feed["rows"])) == fr.FEED_TOTAL_ROWS == 100
    assert (
        await eval_js(instance, "document.querySelectorAll('.feed-row').length") == 100
    )

    ledger = await _ledger(origin_a)
    assert ledger["feed_pages"] == [0, 1, 2, 3], "page 4 must never be requested"


# ═══════════════════════════════════════════════════════════════════════════
# MQ-117 — strict CSP with a nonce
# ═══════════════════════════════════════════════════════════════════════════
async def test_strict_csp_surface(instance, fixture_origin_pair):
    """MQ-117: exact header bytes; nonce script and self fetch run; inline
    script, eval, and the cross-origin fetch are blocked, with ordered
    violations naming the expected directives and blocked origins.

    The header is checked from THIS process over plain HTTP (byte-exact,
    independent of anything the browser reports), and the browser's own
    ``securitypolicyviolation`` record proves Chrome actually enforced that
    header rather than merely receiving it.
    """
    origin_a, origin_b = fixture_origin_pair

    served = await _http_get(f"{origin_a}/csp/strict")
    assert served.headers[fr.CSP_HEADER_NAME] == fr.CSP_HEADER_VALUE

    await navigate_and_settle(instance, f"{origin_a}/csp/strict")
    assert await wait_for_js(instance, "window.__csp.done", True, timeout=15.0) is True

    csp = await _json_state(instance, "window.__csp")
    assert csp["nonce_ran"] is True, "the nonce'd script must run"
    assert csp["inline_ran"] is False, "the un-nonced inline script must be blocked"
    assert csp["eval_ran"] is False, "eval must be blocked"
    assert csp["self_fetch"] == fr.CSP_PING_BODY
    assert csp["peer_fetch"] == "error:TypeError", csp["peer_fetch"]

    observed = await _json_state(
        instance,
        "window.__csp.violations.map(function(v){"
        "  var origin = v.blocked;"
        "  try { origin = new URL(v.blocked).origin; } catch (e) {}"
        "  return [v.directive, origin];"
        "})",
    )
    # ``script-src-elem`` is the effective directive Chrome reports for a
    # blocked script ELEMENT (CSP3 splits element and eval sources); the eval
    # violation still reports the ``script-src`` the header actually declared.
    assert observed == [
        ["script-src-elem", "inline"],
        ["script-src", "eval"],
        ["connect-src", origin_b],
    ], observed


# ═══════════════════════════════════════════════════════════════════════════
# MQ-118 — basic auth, redirect chains, CORS preflight
# ═══════════════════════════════════════════════════════════════════════════
async def test_auth_redirect_cors_preflight(instance, fixture_origin_pair):
    """MQ-118: the sent auth header, the final loaded page token, the final
    response status/headers, and the allowed/blocked CORS outcomes.

    Only FINAL outcomes are asserted. Redirect hop ids, ordering, counts, and
    loop diagnosis are excluded on purpose: redirect interception overwrites
    request ids and exposes no chain field, so there is no public contract to
    assert against and a test that invented one would be claiming coverage the
    product does not have.

    The auth oracle is the fixture server's ledger of the ``Authorization``
    header it actually received — the page's own report of its fetch would not
    prove the browser put the credential on the wire.

    KNOWN LIMITATION, measured rather than assumed: a fetch that receives the
    401 ``WWW-Authenticate: Basic`` challenge never settles under headless
    Chrome (no resolve, no reject), because the challenge has no prompt
    delegate. It reproduces identically with plain ``nodriver`` and through the
    product, is unaffected by ``credentials: 'omit'``, and disappears when the
    same 401 omits the challenge header — so it is a browser-configuration
    property, not a product defect. The challenge branch is therefore asserted
    over plain HTTP, where it is exact, and the browser asserts the grant. No
    interactive-authentication claim is made for the browser path.
    """
    origin_a, origin_b = fixture_origin_pair
    query = get_fn("query_elements")

    await _reset_ledger(origin_b)

    # ── The challenge and the grant, asserted from this process (exact bytes).
    challenge = await _http_get(f"{origin_a}/auth/basic")
    assert challenge.status_code == 401
    assert challenge.headers["WWW-Authenticate"] == fr.AUTH_REALM
    assert challenge.text == fr.AUTH_CHALLENGE_BODY

    # ── The browser sends the exact credential and sees the fixed final set.
    await _reset_ledger(origin_a)
    await navigate_and_settle(instance, f"{origin_a}/dynamic_probe.html")
    await eval_js(instance, "window.__auth.run()")
    assert await wait_for_js(instance, "window.__auth.done", True, timeout=15.0) is True
    auth = await _json_state(instance, "window.__auth")
    assert auth["status"] == 200
    assert auth["header"] == "granted"
    assert auth["body"] == fr.AUTH_GRANTED_BODY

    # The independent oracle: what the SERVER saw the browser send.
    ledger = await _ledger(origin_a)
    assert ledger["auth_headers"] == [fr.AUTH_HEADER_VALUE]

    # ── CORS: an allowed preflighted POST and a blocked simple GET.
    await eval_js(instance, "window.__cors.run()")
    assert await wait_for_js(instance, "window.__cors.done", True, timeout=15.0) is True
    cors = await _json_state(instance, "window.__cors")
    assert cors["echo_status"] == 200
    assert cors["echo"] == '{"ping":"cors"}'
    assert cors["blocked"] == "error:TypeError", cors["blocked"]
    peer_ledger = await _ledger(origin_b)
    assert peer_ledger["cors_methods"] == ["OPTIONS", "POST", "GET-blocked"]

    # ── Redirect that stays on A. Final status/headers come from an
    # independent HTTP client; the final PAGE token comes from the browser.
    same_origin = await _http_get(f"{origin_a}/redirect/start")
    assert same_origin.status_code == 200
    assert same_origin.url == f"{origin_a}/redirect/final"
    assert same_origin.headers["X-E2E-Redirect-Final"] == "a"

    await navigate_and_settle(instance, f"{origin_a}/redirect/start")
    assert await eval_js(instance, "location.href") == f"{origin_a}/redirect/final"
    assert await eval_js(instance, "document.title") == "fixture-redirect-final-a"
    assert "redirect-final-token-a" in json.dumps(
        _unwrap(await query(instance_id=instance, selector="#sentinel"))
    )

    # ── Redirect that leaves A for B.
    cross = await _http_get(f"{origin_a}/redirect/to-b")
    assert cross.status_code == 200
    assert cross.url == f"{origin_b}/redirect/final"
    assert cross.headers["X-E2E-Redirect-Final"] == "b"

    await navigate_and_settle(instance, f"{origin_a}/redirect/to-b")
    assert await eval_js(instance, "location.href") == f"{origin_b}/redirect/final"
    assert await eval_js(instance, "document.title") == "fixture-redirect-final-b"
    assert "redirect-final-token-b" in json.dumps(
        _unwrap(await query(instance_id=instance, selector="#sentinel"))
    )


# ═══════════════════════════════════════════════════════════════════════════
# MQ-119 — text / binary / chunked payloads and HTTP errors
# ═══════════════════════════════════════════════════════════════════════════
async def test_completed_text_base64_binary_chunked_and_http_errors(
    instance, fixture_origin_pair
):
    """MQ-119: ordinary request metadata, exact final status/headers, the
    completed text body, the completed binary body in the declared base64 form,
    the fully assembled chunked body, and preserved 4xx/5xx bodies.

    Only COMPLETED responses are claimed. No loading-failure, truncated-stream,
    or download behavior is asserted — there is no typed public contract for
    any of those, and there is no MCP download tool at all.

    The oracle for every body is the bytes the fixture server is defined to
    serve (recomputed here, and hashed for the binary case), never the network
    tool's own second opinion.
    """
    origin_a, _ = fixture_origin_pair
    set_filters = get_fn("set_network_capture_filters")
    get_content = get_fn("get_response_content")

    await navigate_and_settle(instance, f"{origin_a}/dynamic_probe.html")
    assert await set_filters(instance_id=instance, capture_bodies=True) is True

    await eval_js(instance, "window.__payload.run()")
    assert (
        await wait_for_js(instance, "window.__payload.done", True, timeout=20.0) is True
    )

    seen = await _json_state(instance, "window.__payload.seen")
    assert seen["/payload/text"] == {"status": 200, "body": fr.PAYLOAD_TEXT_BODY}
    assert seen["/payload/chunked"] == {
        "status": 200,
        "body": fr.PAYLOAD_CHUNKED_BODY,
    }
    assert seen["/status/418"] == {"status": 418, "body": fr.STATUS_418_BODY}
    assert seen["/status/503"] == {"status": 503, "body": fr.STATUS_503_BODY}
    assert seen["/payload/binary"] == {
        "status": 200,
        "length": fr.PAYLOAD_BINARY_LENGTH,
    }

    expected_binary = fr.payload_binary_body()
    cases = [
        ("/payload/text", 200, "text/plain; charset=utf-8", fr.PAYLOAD_TEXT_BODY),
        ("/payload/chunked", 200, "text/plain; charset=utf-8", fr.PAYLOAD_CHUNKED_BODY),
        ("/status/418", 418, "text/plain; charset=utf-8", fr.STATUS_418_BODY),
        ("/status/503", 503, "text/plain; charset=utf-8", fr.STATUS_503_BODY),
        ("/payload/binary", 200, "application/octet-stream", None),
    ]
    for path, status, content_type, body_text in cases:
        request = await _find_request(instance, path)
        assert request is not None, f"{path} was never captured"
        # Ordinary request metadata, exact.
        assert request["url"] == f"{origin_a}{path}", request
        assert request["method"] == "GET", request

        details = await _response_details(request["request_id"])
        assert details is not None, f"no response record for {path}"
        assert details["status"] == status, (path, details["status"])
        headers = _lower_headers(details)
        assert headers.get("content-type") == content_type, (path, headers)

        content = await get_content(
            instance_id=instance, request_id=request["request_id"]
        )
        if body_text is not None:
            assert content == body_text, (path, content)
        else:
            decoded = base64.b64decode(content)
            assert decoded == expected_binary
            assert (
                hashlib.sha256(decoded).hexdigest()
                == hashlib.sha256(expected_binary).hexdigest()
            )

    # The 418/503 bodies survived the error status rather than being dropped.
    assert (
        await get_content(
            instance_id=instance,
            request_id=(await _find_request(instance, "/status/503"))["request_id"],
        )
        == fr.STATUS_503_BODY
    )


# ═══════════════════════════════════════════════════════════════════════════
# MQ-120 — SSE and WebSocket lifecycle
# ═══════════════════════════════════════════════════════════════════════════
async def test_sse_and_websocket_lifecycle(instance, fixture_origin_pair):
    """MQ-120: both streams connect, deliver their declared data in order, and
    reach a finite close state — asserted through the page sentinel.

    MCP network debugging does not qualify SSE events or WebSocket
    handshakes/frames/messages, so this is deliberately a page-runtime claim
    and nothing more. The fixture server's stream ledger is the independent
    check that both streams were genuinely served rather than the sentinel
    having been satisfied some other way.
    """
    origin_a, _ = fixture_origin_pair

    await _reset_ledger(origin_a)
    await navigate_and_settle(instance, f"{origin_a}/events_page.html")
    assert (
        await wait_for_js(
            instance, "window.__streams.done", True, timeout=STREAM_TIMEOUT
        )
        is True
    )

    streams = await _json_state(instance, "window.__streams")
    sse = streams["sse"]
    assert sse["error"] is False, "the SSE stream must end by its own close, not error"
    assert sse["ids"] == [event_id for event_id, _ in fr.SSE_EVENTS]
    assert sse["data"] == [data for _, data in fr.SSE_EVENTS]
    assert sse["closed"] is True

    websocket = streams["ws"]
    assert websocket["messages"] == list(fr.WS_MESSAGES)
    assert websocket["code"] == fr.WS_CLOSE_CODE
    assert websocket["clean"] is True
    assert websocket["closed"] is True

    ledger = await _ledger(origin_a)
    assert sorted(ledger["streams"]) == ["sse", "ws"]


# ═══════════════════════════════════════════════════════════════════════════
# MQ-121 — custom elements, slots, and the popup tab lifecycle
# ═══════════════════════════════════════════════════════════════════════════
async def test_custom_elements_slots_and_popup_lifecycle(instance, fixture_origin_pair):
    """MQ-121: generic selectors reach custom-element light DOM, cloned template
    content, and nested slots; open shadow is reachable ONLY through an explicit
    ``execute_script`` escape hatch; and the popup flow is exactly
    ``list_tabs`` → ``switch_tab`` → assert URL/content → ``close_tab``.

    The shadow assertions are written to pin the limit, not to claim support:
    the generic selector tool must NOT see inside the open shadow root, and
    closed shadow is not exercised at all. There is no popup-control targeting
    claim either — the popup is opened by clicking an ordinary link.
    """
    origin_a, _ = fixture_origin_pair
    query = get_fn("query_elements")
    click = get_fn("click_element")
    list_tabs = get_fn("list_tabs")
    switch_tab = get_fn("switch_tab")
    close_tab = get_fn("close_tab")
    get_active_tab = get_fn("get_active_tab")

    await navigate_and_settle(instance, f"{origin_a}/popup_components.html")

    # ── Custom element: lifecycle order is a language guarantee, so it is exact.
    assert await _json_state(instance, "window.__components.lifecycle") == [
        "attr:label:null->alpha",
        "connected:card-one",
        "template-cloned:card-one",
        "slots:title,body",
    ]
    assert await _json_state(instance, "window.__components.slots") == ["title", "body"]

    # ── Light DOM is generic: ordinary selectors reach all of it.
    card = _unwrap(await query(instance_id=instance, selector="fixture-card#card-one"))
    assert isinstance(card, list) and len(card) == 1, card
    cloned = _unwrap(await query(instance_id=instance, selector=".card-template-line"))
    assert "template-line-token" in json.dumps(cloned), cloned
    slot_title = _unwrap(await query(instance_id=instance, selector="[slot='title']"))
    assert "slot-title-alpha" in json.dumps(slot_title), slot_title
    nested = _unwrap(await query(instance_id=instance, selector=".nested-slot-leaf"))
    assert "slot-body-leaf-alpha" in json.dumps(nested), nested

    # ── Open shadow: escape hatch only. The generic selector must not pierce it.
    assert _unwrap(await query(instance_id=instance, selector="#shadow-sentinel")) == []
    assert (
        await eval_js(
            instance,
            "document.getElementById('shadow-host').shadowRoot"
            ".getElementById('shadow-sentinel').textContent",
        )
        == "open-shadow-sentinel-token"
    )

    # ── Popup flow: list_tabs → switch_tab → assert URL/content → close_tab.
    before = await list_tabs(instance_id=instance)
    assert isinstance(before, list) and before, before
    origin_tab = (await get_active_tab(instance_id=instance))["tab_id"]

    assert await click(instance_id=instance, selector="#popup-link") is True

    deadline = time.monotonic() + 15.0
    popup = None
    while popup is None and time.monotonic() < deadline:
        for tab in await list_tabs(instance_id=instance):
            if "popup_target.html" in (tab.get("url") or ""):
                popup = tab
                break
        if popup is None:
            await asyncio.sleep(0.25)
    assert popup is not None, "the target=_blank popup never appeared in list_tabs"
    assert fr.POPUP_TARGET_TOKEN in popup["url"], popup

    assert await switch_tab(instance_id=instance, tab_id=popup["tab_id"]) is True
    assert fr.POPUP_TARGET_TOKEN in await eval_js(instance, "location.href")
    assert await eval_js(instance, "document.title") == "fixture-popup-target-page"
    assert "fixture-popup-target-page" in json.dumps(
        _unwrap(await query(instance_id=instance, selector="#sentinel"))
    )

    assert await close_tab(instance_id=instance, tab_id=popup["tab_id"]) is True

    # The popup is gone, and list_tabs still works after a close (F-771).
    deadline = time.monotonic() + 15.0
    remaining = await list_tabs(instance_id=instance)
    while (
        any(t["tab_id"] == popup["tab_id"] for t in remaining)
        and time.monotonic() < deadline
    ):
        await asyncio.sleep(0.25)
        remaining = await list_tabs(instance_id=instance)
    assert all(t["tab_id"] != popup["tab_id"] for t in remaining), remaining

    assert await switch_tab(instance_id=instance, tab_id=origin_tab) is True
    assert await eval_js(instance, "document.title") == "fixture-popup-components-page"
