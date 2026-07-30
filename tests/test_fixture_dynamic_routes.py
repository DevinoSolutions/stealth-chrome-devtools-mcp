"""plan_RELEASE W7 — hermetic backstop for the dynamic fixture routes.

No Chrome and no ``integration`` marker: this runs in the unit lane and proves
the server half of every W7 shape *before* a browser is involved. When
``tests/test_e2e_dynamic_sites.py`` goes red, these tests are what separate "the
product mishandled the shape" from "the fixture never served it" — the two are
indistinguishable at the tool boundary otherwise.

It is also the **enumeration backstop** the plan requires: every page in
``fixture_routes.DYNAMIC_PAGES`` and every entry in ``fixture_routes.ROUTES``
must be reachable and asserted here, so a route cannot be added without proof
that it serves.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

import fixture_routes as fr
from release_gate_harness import serve_fixture_origin_pair

TIMEOUT = 10


@pytest.fixture(scope="module")
def origins():
    with serve_fixture_origin_pair() as (origin_a, origin_b):
        yield origin_a, origin_b


# ── Enumeration backstops ───────────────────────────────────────────────────
def test_every_declared_page_serves_with_its_sentinel(origins):
    origin_a, origin_b = origins
    for path, sentinel in fr.DYNAMIC_PAGES.items():
        expected = sentinel + "a" if path == "/redirect/final" else sentinel
        response = requests.get(f"{origin_a}{path}", timeout=TIMEOUT)
        assert response.status_code == 200, path
        assert expected in response.text, path
    # Role is what makes the symmetric routes distinguishable across origins.
    assert (
        "redirect-final-token-b"
        in requests.get(f"{origin_b}/redirect/final", timeout=TIMEOUT).text
    )


def test_every_route_is_reachable_and_none_falls_through(origins):
    """Every ``ROUTES`` key answers; nothing 404s into the static tree."""
    origin_a, _ = origins
    skip = {("GET", "/events/ws")}  # exercised by the raw-socket test below
    for method, path in fr.ROUTES:
        if (method, path) in skip:
            continue
        response = requests.request(
            method, f"{origin_a}{path}", timeout=TIMEOUT, allow_redirects=False
        )
        assert response.status_code != 404, (method, path)


def test_no_external_urls_in_the_dynamic_routes():
    """Same determinism rule the static fixture tree already lives under: the
    fixture may never reach the live web."""
    source = Path(fr.__file__).read_text(encoding="utf-8")
    external = re.findall(r"https?://(?!127\.0\.0\.1)[\w.-]+", source)
    assert external == [], external


# ── MQ-116: the feed protocol ───────────────────────────────────────────────
def test_feed_pages_are_exact_and_page_four_is_the_terminal_sentinel(origins):
    origin_a, _ = origins
    requests.get(f"{origin_a}/e2e/reset", timeout=TIMEOUT)
    seen: list[str] = []
    for page in range(fr.FEED_LAST_PAGE + 1):
        payload = requests.get(
            f"{origin_a}/api/feed?page={page}", timeout=TIMEOUT
        ).json()
        assert payload["page"] == page
        assert payload["rows"] == fr.feed_rows(page)
        assert len(payload["rows"]) == fr.FEED_PAGE_SIZE
        assert payload["has_more"] is (page < fr.FEED_LAST_PAGE)
        assert "terminal" not in payload
        seen.extend(payload["rows"])
    assert seen == [f"feed-row-{i}" for i in range(fr.FEED_TOTAL_ROWS)]

    terminal = requests.get(f"{origin_a}/api/feed?page=4", timeout=TIMEOUT).json()
    assert terminal == {"page": 4, "rows": [], "has_more": False, "terminal": True}

    ledger = requests.get(f"{origin_a}/e2e/ledger", timeout=TIMEOUT).json()
    assert ledger["feed_pages"] == [0, 1, 2, 3, 4]


# ── MQ-117: the CSP header bytes ────────────────────────────────────────────
def test_csp_header_is_byte_exact(origins):
    origin_a, origin_b = origins
    response = requests.get(f"{origin_a}/csp/strict", timeout=TIMEOUT)
    assert response.status_code == 200
    assert response.headers[fr.CSP_HEADER_NAME] == fr.CSP_HEADER_VALUE
    assert f"nonce='{fr.CSP_NONCE}'" in response.text
    # The page names the real peer origin, so the blocked fetch is cross-origin.
    assert f"{origin_b}/csp/ping" in response.text
    ping = requests.get(f"{origin_a}/csp/ping", timeout=TIMEOUT)
    assert ping.text == fr.CSP_PING_BODY


# ── MQ-118: auth, redirects, CORS ───────────────────────────────────────────
def test_basic_auth_challenge_grant_and_server_side_ledger(origins):
    origin_a, _ = origins
    requests.get(f"{origin_a}/e2e/reset", timeout=TIMEOUT)

    challenge = requests.get(f"{origin_a}/auth/basic", timeout=TIMEOUT)
    assert challenge.status_code == 401
    assert challenge.headers["WWW-Authenticate"] == fr.AUTH_REALM
    assert challenge.text == fr.AUTH_CHALLENGE_BODY

    granted = requests.get(
        f"{origin_a}/auth/basic",
        headers={"Authorization": fr.AUTH_HEADER_VALUE},
        timeout=TIMEOUT,
    )
    assert granted.status_code == 200
    assert granted.headers["X-E2E-Auth"] == "granted"
    assert granted.text == fr.AUTH_GRANTED_BODY

    ledger = requests.get(f"{origin_a}/e2e/ledger", timeout=TIMEOUT).json()
    assert ledger["auth_headers"] == [None, fr.AUTH_HEADER_VALUE]


def test_redirects_resolve_within_a_and_across_to_b(origins):
    origin_a, origin_b = origins
    hop = requests.get(
        f"{origin_a}/redirect/start", timeout=TIMEOUT, allow_redirects=False
    )
    assert hop.status_code == 302
    assert hop.headers["Location"] == "/redirect/final"

    final = requests.get(f"{origin_a}/redirect/start", timeout=TIMEOUT)
    assert final.status_code == 200
    assert final.url == f"{origin_a}/redirect/final"
    assert final.headers["X-E2E-Redirect-Final"] == "a"
    assert "redirect-final-token-a" in final.text

    cross = requests.get(f"{origin_a}/redirect/to-b", timeout=TIMEOUT)
    assert cross.status_code == 200
    assert cross.url == f"{origin_b}/redirect/final"
    assert cross.headers["X-E2E-Redirect-Final"] == "b"
    assert "redirect-final-token-b" in cross.text


def test_cors_echo_allows_a_and_blocked_route_omits_acao(origins):
    origin_a, origin_b = origins
    preflight = requests.options(f"{origin_b}/cors/echo", timeout=TIMEOUT)
    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == origin_a
    assert preflight.headers["Access-Control-Allow-Methods"] == "POST, OPTIONS"

    echo = requests.post(
        f"{origin_b}/cors/echo", data='{"ping":"cors"}', timeout=TIMEOUT
    )
    assert echo.status_code == 200
    assert echo.json()["echo"] == '{"ping":"cors"}'
    assert echo.headers["Access-Control-Allow-Origin"] == origin_a

    blocked = requests.get(f"{origin_b}/cors/blocked", timeout=TIMEOUT)
    assert blocked.status_code == 200
    assert blocked.json() == {"blocked": "no-acao"}
    assert "Access-Control-Allow-Origin" not in blocked.headers


# ── MQ-119: the payload shapes ──────────────────────────────────────────────
def test_text_binary_chunked_and_error_payloads_are_exact(origins):
    origin_a, _ = origins

    text = requests.get(f"{origin_a}/payload/text", timeout=TIMEOUT)
    assert text.text == fr.PAYLOAD_TEXT_BODY

    binary = requests.get(f"{origin_a}/payload/binary", timeout=TIMEOUT)
    expected = fr.payload_binary_body()
    assert binary.content == expected
    assert len(expected) == fr.PAYLOAD_BINARY_LENGTH
    assert binary.headers["Content-Type"] == "application/octet-stream"
    with pytest.raises(UnicodeDecodeError):
        expected.decode("utf-8")  # forces get_response_content's base64 branch

    chunked = requests.get(f"{origin_a}/payload/chunked", timeout=TIMEOUT)
    assert chunked.headers["Transfer-Encoding"] == "chunked"
    assert chunked.text == fr.PAYLOAD_CHUNKED_BODY

    teapot = requests.get(f"{origin_a}/status/418", timeout=TIMEOUT)
    assert teapot.status_code == 418
    assert teapot.headers["X-E2E-Status"] == "teapot"
    assert teapot.text == fr.STATUS_418_BODY

    unavailable = requests.get(f"{origin_a}/status/503", timeout=TIMEOUT)
    assert unavailable.status_code == 503
    assert unavailable.headers["X-E2E-Status"] == "unavailable"
    assert unavailable.text == fr.STATUS_503_BODY


# ── MQ-120: the two stream protocols ────────────────────────────────────────
def test_sse_emits_three_ids_then_ends(origins):
    origin_a, _ = origins
    response = requests.get(f"{origin_a}/events/sse", timeout=TIMEOUT)
    assert response.headers["Content-Type"] == "text/event-stream"
    assert response.text == "".join(
        f"id: {event_id}\ndata: {data}\n\n" for event_id, data in fr.SSE_EVENTS
    )


def test_websocket_handshake_frames_and_close(origins):
    """Drive the upgrade over a raw socket: the accept key, three text frames,
    and a 1000 close frame are protocol facts the browser test then relies on."""
    origin_a, _ = origins
    parsed = urlparse(origin_a)
    key = base64.b64encode(b"0123456789abcdef").decode("ascii")
    with socket.create_connection((parsed.hostname, parsed.port), timeout=TIMEOUT) as s:
        s.sendall(
            (
                "GET /events/ws HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{parsed.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        chunks = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
            if b"\x88" in data:  # our close frame ends the server's output
                break
        raw = b"".join(chunks)

    head, _, frames = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 101 Switching Protocols")
    expected_accept = base64.b64encode(
        hashlib.sha1((key + fr._WS_GUID).encode("ascii")).digest()  # noqa: S324  RFC 6455 mandates SHA-1, PERMANENT(protocol)
    ).decode("ascii")
    assert f"Sec-WebSocket-Accept: {expected_accept}".encode() in head

    payloads = []
    cursor = 0
    while cursor < len(frames):
        opcode = frames[cursor] & 0x0F
        length = frames[cursor + 1] & 0x7F
        body = frames[cursor + 2 : cursor + 2 + length]
        payloads.append((opcode, body))
        cursor += 2 + length
    assert [p.decode("utf-8") for op, p in payloads if op == 0x1] == list(
        fr.WS_MESSAGES
    )
    close = [p for op, p in payloads if op == 0x8]
    assert close and int.from_bytes(close[0], "big") == fr.WS_CLOSE_CODE


# ── The cross-origin wiring itself ──────────────────────────────────────────
def test_origins_are_independent_and_cross_linked(origins):
    origin_a, origin_b = origins
    assert origin_a != origin_b
    assert urlparse(origin_a).hostname == urlparse(origin_b).hostname == "127.0.0.1"
    assert urlparse(origin_a).port != urlparse(origin_b).port
    # A's outer frame points at B; B's middle frame points back at A.
    outer = requests.get(f"{origin_a}/frames/a_outer.html", timeout=TIMEOUT).text
    middle = requests.get(f"{origin_b}/frames/b_middle.html", timeout=TIMEOUT).text
    assert f"{origin_b}/frames/b_middle.html" in outer
    assert f"{origin_a}/frames/a_inner.html" in middle


def test_reset_clears_the_server_side_ledger(origins):
    origin_a, _ = origins
    requests.get(f"{origin_a}/api/feed?page=0", timeout=TIMEOUT)
    assert requests.get(f"{origin_a}/e2e/ledger", timeout=TIMEOUT).json()["feed_pages"]
    assert requests.get(f"{origin_a}/e2e/reset", timeout=TIMEOUT).json() == {
        "reset": True,
        "role": "a",
    }
    assert requests.get(f"{origin_a}/e2e/ledger", timeout=TIMEOUT).json() == json.loads(
        json.dumps(
            {"feed_pages": [], "auth_headers": [], "cors_methods": [], "streams": []}
        )
    )


# ── MQ-126…129: W10's fault controllers, proved without a browser ───────────
# The integration tier can only tell "the product mishandled the fault" from
# "the fixture never injected it" if the server half is proved here first.
FAULT_ROUTE_PATHS = (
    "/fault/arm",
    "/fault/status",
    "/fault/release",
    "/fault/slow",
    "/fault/hang-before-headers",
    "/fault/hang-after-headers",
    "/fault/drop",
)


def _await_entered(origin: str, token: str, timeout: float = TIMEOUT) -> dict:
    """Bounded poll on the fixture's own ``entered`` barrier (never a sleep)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = requests.get(
            f"{origin}/fault/status?token={token}", timeout=TIMEOUT
        ).json()
        if snapshot["entered"]:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"the fixture never entered fault {token!r}")


def _fault_request_head(origin: str, path: str, token: str) -> bytes:
    parsed = urlparse(origin)
    return (
        f"GET {path}?token={token} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")


def test_every_fault_route_answers_a_tokenless_request_immediately(origins):
    """``test_every_route_is_reachable_and_none_falls_through`` calls every
    ``ROUTES`` key with no query string. A fault route that parked on that would
    wedge the unit lane, so refusing a tokenless request is load-bearing."""
    origin_a, _ = origins
    for path in FAULT_ROUTE_PATHS:
        response = requests.get(f"{origin_a}{path}", timeout=TIMEOUT)
        assert response.status_code == 400, path
    assert set(FAULT_ROUTE_PATHS) == {
        path for _, path in fr.ROUTES if path.startswith("/fault/")
    }


def test_an_unarmed_token_is_refused_rather_than_waited_on(origins):
    origin_a, _ = origins
    for path in FAULT_ROUTE_PATHS:
        if path == "/fault/arm":
            continue
        response = requests.get(f"{origin_a}{path}?token=never-armed", timeout=TIMEOUT)
        assert response.status_code == 409, path


def test_the_slow_route_withholds_everything_until_it_is_released(origins):
    """(a) slow-success: nothing is served before the release, the exact body is
    served after it, and the handler thread leaves."""
    origin_a, _ = origins
    token = "w10-hermetic-slow"
    armed = requests.get(f"{origin_a}/fault/arm?token={token}", timeout=TIMEOUT).json()
    assert armed == {
        "token": token,
        "phase": "",
        "entered": False,
        "released": False,
        "exited": False,
        "disconnected": False,
        "ceiling_hit": False,
    }

    captured: list = []

    def _fetch():
        captured.append(
            requests.get(f"{origin_a}/fault/slow?token={token}", timeout=TIMEOUT)
        )

    worker = threading.Thread(target=_fetch, daemon=True)
    worker.start()
    assert _await_entered(origin_a, token)["phase"] == "slow"
    assert captured == [], "the slow route answered before it was released"

    released = requests.get(
        f"{origin_a}/fault/release?token={token}", timeout=TIMEOUT
    ).json()
    assert released["released"] is True
    worker.join(timeout=TIMEOUT)
    assert not worker.is_alive()
    assert captured[0].status_code == 200
    assert fr.FAULT_SLOW_BODY in captured[0].text
    final = requests.get(f"{origin_a}/fault/status?token={token}", timeout=TIMEOUT)
    assert final.json()["exited"] is True
    assert final.json()["ceiling_hit"] is False


def test_the_hang_before_headers_route_writes_no_byte_until_released(origins):
    """(b) hang-before-headers: not even a status line reaches the peer."""
    origin_a, _ = origins
    token = "w10-hermetic-before"
    requests.get(f"{origin_a}/fault/arm?token={token}", timeout=TIMEOUT)
    parsed = urlparse(origin_a)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=TIMEOUT) as s:
        s.sendall(_fault_request_head(origin_a, "/fault/hang-before-headers", token))
        assert _await_entered(origin_a, token)["phase"] == "before-headers"
        s.settimeout(0.5)
        with pytest.raises(TimeoutError):
            s.recv(4096)  # a committed response would have arrived by now
        requests.get(f"{origin_a}/fault/release?token={token}", timeout=TIMEOUT)
        s.settimeout(TIMEOUT)
        received = b""
        while fr.FAULT_RELEASED_BODY.encode() not in received:
            chunk = s.recv(4096)
            if not chunk:
                break
            received += chunk
    assert received.startswith(b"HTTP/1.")
    assert fr.FAULT_RELEASED_BODY.encode() in received


def test_the_after_headers_route_commits_then_completes_only_on_release(origins):
    """(c) hang-after-headers: the head and first chunk are on the wire while the
    body is demonstrably incomplete; the tail exists only after a release."""
    origin_a, _ = origins
    token = "w10-hermetic-after"
    requests.get(f"{origin_a}/fault/arm?token={token}", timeout=TIMEOUT)
    parsed = urlparse(origin_a)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=TIMEOUT) as s:
        s.sendall(_fault_request_head(origin_a, "/fault/hang-after-headers", token))
        committed = b""
        while fr.FAULT_PARTIAL_PREFIX.encode() not in committed:
            chunk = s.recv(4096)
            assert chunk, "the route closed before committing its head"
            committed += chunk
        assert b"Transfer-Encoding: chunked" in committed
        assert fr.FAULT_PARTIAL_SUFFIX.encode() not in committed
        assert b"0\r\n\r\n" not in committed
        assert _await_entered(origin_a, token)["phase"] == "after-headers"

        requests.get(f"{origin_a}/fault/release?token={token}", timeout=TIMEOUT)
        rest = b""
        while b"0\r\n\r\n" not in rest:
            chunk = s.recv(4096)
            if not chunk:
                break
            rest += chunk
    assert fr.FAULT_PARTIAL_SUFFIX.encode() in rest


def test_the_drop_route_aborts_the_transfer_after_committing(origins):
    """(d) mid-transfer drop: the peer never receives the terminating chunk."""
    origin_a, _ = origins
    token = "w10-hermetic-drop"
    requests.get(f"{origin_a}/fault/arm?token={token}", timeout=TIMEOUT)
    parsed = urlparse(origin_a)
    rest = b""
    with socket.create_connection((parsed.hostname, parsed.port), timeout=TIMEOUT) as s:
        s.sendall(_fault_request_head(origin_a, "/fault/drop", token))
        committed = b""
        while fr.FAULT_PARTIAL_PREFIX.encode() not in committed:
            chunk = s.recv(4096)
            assert chunk, "the route closed before committing its head"
            committed += chunk
        assert _await_entered(origin_a, token)["phase"] == "drop"
        requests.get(f"{origin_a}/fault/release?token={token}", timeout=TIMEOUT)
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                rest += chunk
        except OSError:
            pass  # RST: the abort we asked for, and the point of the route
    assert b"0\r\n\r\n" not in committed + rest
    assert fr.FAULT_PARTIAL_SUFFIX.encode() not in committed + rest


def test_release_all_faults_unblocks_every_handler_and_reports_none_stuck():
    """Fixture finalization's own proof, exercised without a server: releasing
    ends the wait, the ceiling never fires, and the handler is seen to leave."""
    state = fr.new_origin_state("a")
    controller = fr._new_fault_controller("unit-token")
    state["faults"]["unit-token"] = controller
    left = threading.Event()

    def _handler():
        fr._await_release(controller, "unit")
        controller["exited"].set()
        left.set()

    threading.Thread(target=_handler, daemon=True).start()
    assert controller["entered"].wait(TIMEOUT)
    assert fr.release_all_faults(state, timeout=TIMEOUT) == []
    assert left.wait(TIMEOUT)
    assert controller["ceiling_hit"] is False


def test_release_all_faults_names_a_handler_that_never_left():
    """The wedge detector has to be able to FAIL, or its empty list proves
    nothing. An ENTERED controller that never exits is reported by token; one
    that was armed but never entered is not a handler and is not reported."""
    state = fr.new_origin_state("a")
    wedged = fr._new_fault_controller("wedged-token")
    wedged["entered"].set()
    idle = fr._new_fault_controller("idle-token")
    state["faults"].update({"wedged-token": wedged, "idle-token": idle})
    assert fr.release_all_faults(state, timeout=0.2) == ["wedged-token"]
