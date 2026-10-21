"""Base route implementations and the W10 fault controllers.

Split out of :mod:`fixture_routes` (T-5431); that module re-exports every name
here, so callers keep importing from ``fixture_routes``.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import time
from typing import Any

from fixture_routes_base import (
    _WS_GUID,
    AUTH_CHALLENGE_BODY,
    AUTH_GRANTED_BODY,
    AUTH_HEADER_VALUE,
    AUTH_REALM,
    CSP_HEADER_NAME,
    CSP_HEADER_VALUE,
    CSP_PING_BODY,
    FAULT_CEILING_SECONDS,
    FAULT_PARTIAL_PREFIX,
    FAULT_PARTIAL_SENTINEL,
    FAULT_PARTIAL_SUFFIX,
    FAULT_RELEASED_BODY,
    FAULT_SLOW_BODY,
    FAULT_SLOW_SENTINEL,
    FEED_LAST_PAGE,
    PAYLOAD_CHUNKS,
    PAYLOAD_TEXT_BODY,
    SSE_EVENTS,
    STATUS_418_BODY,
    STATUS_503_BODY,
    WS_CLOSE_CODE,
    WS_MESSAGES,
    _new_ledger,
    _page,
    _record,
    _send,
    _send_html,
    _send_json,
    _send_raw,
    _send_text,
    feed_rows,
    payload_binary_body,
)
from fixture_routes_pages import (
    csp_strict_page,
    dynamic_probe_page,
    events_page,
    frames_inner_page,
    frames_middle_page,
    frames_outer_page,
    lazy_virtual_infinite_page,
    popup_components_page,
    popup_target_page,
    redirect_final_page,
    spa_history_page,
)


# ── Route implementations ───────────────────────────────────────────────────
def _peer(handler) -> str:
    return handler.origin_state["peer_url"]


def _role(handler) -> str:
    return handler.origin_state["role"]


def _r_reset(handler, query: str) -> None:
    state = handler.origin_state
    with state["lock"]:
        state["ledger"] = _new_ledger()
    _send_json(handler, {"reset": True, "role": state["role"]})


def _r_ledger(handler, query: str) -> None:
    state = handler.origin_state
    with state["lock"]:
        _send_json(handler, dict(state["ledger"]))


def _r_feed(handler, query: str) -> None:
    page = 0
    for part in query.split("&"):
        if part.startswith("page="):
            page = int(part[5:])
    _record(handler, "feed_pages", page)
    rows = feed_rows(page)
    payload: dict[str, Any] = {
        "page": page,
        "rows": rows,
        "has_more": page < FEED_LAST_PAGE,
    }
    if not rows:
        payload["terminal"] = True
    _send_json(handler, payload)


def _r_csp_strict(handler, query: str) -> None:
    _send_html(
        handler,
        csp_strict_page(_peer(handler)),
        extra_headers=[(CSP_HEADER_NAME, CSP_HEADER_VALUE)],
    )


def _r_csp_ping(handler, query: str) -> None:
    _send_text(handler, CSP_PING_BODY)


def _r_auth_basic(handler, query: str) -> None:
    """Grant on the exact credential; otherwise issue the Basic challenge.

    KNOWN LIMITATION (measured, not assumed): under headless Chrome a fetch that
    receives this 401 never settles — no resolve, no reject — because the
    ``WWW-Authenticate`` challenge has no prompt delegate. Reproduced with plain
    ``nodriver`` and through the product, and unchanged by
    ``credentials: 'omit'``; a 401 WITHOUT the challenge header settles normally
    in both. It is a browser-configuration property, not a product defect, so
    MQ-118 asserts the challenge over plain HTTP and the grant in the browser.
    """
    sent = handler.headers.get("Authorization")
    _record(handler, "auth_headers", sent)
    if sent == AUTH_HEADER_VALUE:
        _send_text(
            handler, AUTH_GRANTED_BODY, extra_headers=[("X-E2E-Auth", "granted")]
        )
        return
    _send_text(
        handler,
        AUTH_CHALLENGE_BODY,
        status=401,
        extra_headers=[("WWW-Authenticate", AUTH_REALM)],
    )


def _r_redirect_start(handler, query: str) -> None:
    _send(handler, 302, [("Location", "/redirect/final")])


def _r_redirect_to_b(handler, query: str) -> None:
    _send(handler, 302, [("Location", f"{_peer(handler)}/redirect/final")])


def _r_redirect_final(handler, query: str) -> None:
    role = _role(handler)
    _send_html(
        handler,
        redirect_final_page(role),
        extra_headers=[("X-E2E-Redirect-Final", role)],
    )


def _cors_allow_headers(handler) -> list[tuple[str, str]]:
    return [
        ("Access-Control-Allow-Origin", _peer(handler)),
        ("Access-Control-Allow-Methods", "POST, OPTIONS"),
        ("Access-Control-Allow-Headers", "content-type, x-e2e-probe"),
        ("Access-Control-Max-Age", "0"),
    ]


def _r_cors_echo_options(handler, query: str) -> None:
    _record(handler, "cors_methods", "OPTIONS")
    _send(handler, 204, _cors_allow_headers(handler))


def _r_cors_echo_post(handler, query: str) -> None:
    _record(handler, "cors_methods", "POST")
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length).decode("utf-8", "replace") if length else ""
    _send_json(
        handler,
        {"echo": raw, "origin": handler.headers.get("Origin")},
        extra_headers=_cors_allow_headers(handler)[:1],
    )


def _r_cors_blocked(handler, query: str) -> None:
    """Deliberately omits ``Access-Control-Allow-Origin``: the browser must be
    the thing that blocks it, so the body a same-origin client sees proves the
    route itself is healthy."""
    _record(handler, "cors_methods", "GET-blocked")
    _send_json(handler, {"blocked": "no-acao"})


def _r_payload_text(handler, query: str) -> None:
    _send_text(handler, PAYLOAD_TEXT_BODY)


def _r_payload_binary(handler, query: str) -> None:
    body = payload_binary_body()
    _send(handler, 200, [("Content-Type", "application/octet-stream")], body)


def _r_payload_chunked(handler, query: str) -> None:
    """Three chunks that complete normally (no truncation shape is claimed)."""
    frames = b"".join(
        f"{len(chunk):x}\r\n".encode("latin-1") + chunk.encode("utf-8") + b"\r\n"
        for chunk in PAYLOAD_CHUNKS
    )
    _send_raw(
        handler,
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n\r\n",
        frames + b"0\r\n\r\n",
    )


def _r_status_418(handler, query: str) -> None:
    _send_text(
        handler, STATUS_418_BODY, status=418, extra_headers=[("X-E2E-Status", "teapot")]
    )


def _r_status_503(handler, query: str) -> None:
    _send_text(
        handler,
        STATUS_503_BODY,
        status=503,
        extra_headers=[("X-E2E-Status", "unavailable")],
    )


def _r_sse(handler, query: str) -> None:
    """Emit ids 1..3 and end. No timer: the stream's end IS the third event."""
    _record(handler, "streams", "sse")
    body = "".join(
        f"id: {event_id}\ndata: {data}\n\n" for event_id, data in SSE_EVENTS
    ).encode("utf-8")
    _send_raw(
        handler,
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\n"
        "Connection: close\r\n\r\n",
        body,
    )


def _ws_frame(payload: bytes, opcode: int) -> bytes:
    """One unmasked server frame. Payloads here are far below the 126-byte
    boundary, so the short length form is always correct."""
    return bytes([0x80 | opcode, len(payload)]) + payload


def _r_ws(handler, query: str) -> None:
    """RFC 6455 upgrade, three text frames, then a 1000 close handshake.

    The closing handshake is completed rather than assumed: after sending the
    close frame we read until the peer's own close arrives (or the socket ends)
    so ``CloseEvent.wasClean`` is a fact about the exchange, not a hope. Bounded
    by a socket timeout because an unresponsive peer must not wedge the server
    thread — it is a failure bound, never a synchronization point.
    """
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        _send_text(handler, "missing Sec-WebSocket-Key", status=400)
        return
    _record(handler, "streams", "ws")
    accept = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()  # noqa: S324  RFC 6455 mandates SHA-1 here, PERMANENT(protocol)
    ).decode("ascii")
    frames = b"".join(_ws_frame(m.encode("utf-8"), 0x1) for m in WS_MESSAGES)
    frames += _ws_frame(WS_CLOSE_CODE.to_bytes(2, "big"), 0x8)
    handler.wfile.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("latin-1")
        + frames
    )
    handler.wfile.flush()
    try:
        handler.connection.settimeout(10.0)
        while handler.connection.recv(1024):
            break  # the peer's close frame; the handshake is complete
    except OSError:
        pass
    handler.close_connection = True


def _r_spa(handler, query: str) -> None:
    _send_html(handler, spa_history_page())


def _r_frames_outer(handler, query: str) -> None:
    _send_html(handler, frames_outer_page(_peer(handler)))


def _r_frames_middle(handler, query: str) -> None:
    _send_html(handler, frames_middle_page(_peer(handler)))


def _r_frames_inner(handler, query: str) -> None:
    _send_html(handler, frames_inner_page())


def _r_lazy(handler, query: str) -> None:
    _send_html(handler, lazy_virtual_infinite_page())


def _r_dynamic_probe(handler, query: str) -> None:
    _send_html(handler, dynamic_probe_page(_peer(handler)))


def _r_events_page(handler, query: str) -> None:
    _send_html(handler, events_page())


def _r_popup_components(handler, query: str) -> None:
    _send_html(handler, popup_components_page())


def _r_popup_target(handler, query: str) -> None:
    _send_html(handler, popup_target_page())


# ── W10 fault controllers: arm / observe / release, and the three phases ────
def fault_slow_page() -> str:
    """The exact page ``/fault/slow`` serves once its controller is released."""
    return _page(
        "fault-slow",
        FAULT_SLOW_SENTINEL,
        f"<p id='slow-body'>{FAULT_SLOW_BODY}</p>",
    )


def fault_partial_head() -> str:
    """The first chunk of the after-headers phases: a committed, parseable, but
    deliberately UNCLOSED document."""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>fault-after-headers</title></head><body>"
        f"<p id='sentinel'>{FAULT_PARTIAL_SENTINEL}</p>"
        f"<p id='partial'>{FAULT_PARTIAL_PREFIX}</p>"
    )


def fault_partial_tail() -> str:
    """The chunk that only a RELEASE produces — the completion oracle."""
    return f"<p id='complete'>{FAULT_PARTIAL_SUFFIX}</p></body></html>"


def _new_fault_controller(token: str) -> dict[str, Any]:
    return {
        "token": token,
        "phase": "",
        "entered": threading.Event(),  # the request really reached the server
        "release": threading.Event(),  # the ONLY thing that ends a fault wait
        "exited": threading.Event(),  # the handler thread left (no wedge)
        "disconnected": False,  # peer went away mid-fault; expected, recorded
        "ceiling_hit": False,  # the failure bound fired; never a pass signal
    }


def _fault_snapshot(controller: dict[str, Any]) -> dict[str, Any]:
    return {
        "token": controller["token"],
        "phase": controller["phase"],
        "entered": controller["entered"].is_set(),
        "released": controller["release"].is_set(),
        "exited": controller["exited"].is_set(),
        "disconnected": controller["disconnected"],
        "ceiling_hit": controller["ceiling_hit"],
    }


def _query_value(query: str, key: str) -> str:
    """The raw value of ``key`` in a ``a=1&b=2`` query string, or ``""``."""
    prefix = f"{key}="
    for part in query.split("&"):
        if part.startswith(prefix):
            return part[len(prefix) :]
    return ""


def _fault_lookup(handler, query: str) -> dict[str, Any] | None:
    """Return the armed controller for ``?token=``, or ``None`` having answered.

    A tokenless or unarmed request is answered IMMEDIATELY with a 4xx. That is
    what lets the hermetic route-enumeration backstop call every ``ROUTES`` key
    with no query string without ever parking a thread — and it makes "this
    fault is live" an explicit act of the test rather than a side effect of a
    URL being fetched.
    """
    token = _query_value(query, "token")
    state = handler.origin_state
    if not token:
        _send_text(handler, "fault: missing ?token=", status=400)
        return None
    with state["lock"]:
        controller = state["faults"].get(token)
    if controller is None:
        _send_text(handler, "fault: token is not armed", status=409)
        return None
    return controller


def _await_release(controller: dict[str, Any], phase: str) -> bool:
    """Publish the ``entered`` barrier, then block until the test releases.

    Returns ``True`` only for a real release. The wait is on an event, never on
    elapsed time; :data:`FAULT_CEILING_SECONDS` bounds it so an abandoned fault
    cannot outlive the session, and a ceiling hit is recorded as the failure it
    is rather than being reported as a release.
    """
    controller["phase"] = phase
    controller["entered"].set()
    released = controller["release"].wait(FAULT_CEILING_SECONDS)
    if not released:
        controller["ceiling_hit"] = True
    return released


def _write_partial_head(handler) -> None:
    """Commit a chunked 200 and flush the first chunk, framing it ourselves.

    ``BaseHTTPRequestHandler`` would append ``Content-Length`` and buffer, and
    a partially-transferred body must be genuinely partial on the wire.
    """
    head = fault_partial_head().encode("utf-8")
    handler.wfile.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
        + f"{len(head):x}\r\n".encode("latin-1")
        + head
        + b"\r\n"
    )
    handler.wfile.flush()


def _r_fault_arm(handler, query: str) -> None:
    """Mint (idempotently) the controller a later fault request will find."""
    token = _query_value(query, "token")
    if not token:
        _send_text(handler, "fault: missing ?token=", status=400)
        return
    state = handler.origin_state
    with state["lock"]:
        controller = state["faults"].setdefault(token, _new_fault_controller(token))
    _send_json(handler, _fault_snapshot(controller))


def _r_fault_status(handler, query: str) -> None:
    """The barrier a test polls: has the request ARRIVED, has it left, was the
    peer's disconnect seen? Served from a different thread than the parked
    handler, which is why the fault routes must never hold ``state["lock"]``
    while they wait."""
    controller = _fault_lookup(handler, query)
    if controller is None:
        return
    _send_json(handler, _fault_snapshot(controller))


def _r_fault_release(handler, query: str) -> None:
    controller = _fault_lookup(handler, query)
    if controller is None:
        return
    controller["release"].set()
    _send_json(handler, _fault_snapshot(controller))


def _r_fault_slow(handler, query: str) -> None:
    """Slow SUCCESS: withhold the entire response until released, then serve the
    exact page.

    This is the sensitivity control for every timeout node. Released inside the
    product deadline it must COMPLETE — which is what proves a timeout assertion
    elsewhere measured a real hang rather than a universally broken route.
    """
    controller = _fault_lookup(handler, query)
    if controller is None:
        return
    try:
        if not _await_release(controller, "slow"):
            return
        _send_html(handler, fault_slow_page())
    except OSError:  # peer gave up first: expected during a fault, recorded
        controller["disconnected"] = True
    finally:
        controller["exited"].set()


def _r_fault_hang_before_headers(handler, query: str) -> None:
    """Hang BEFORE headers: not one byte — no status line, no header — until
    released. The navigation has nothing to commit, so only the product's own
    deadline can end it."""
    controller = _fault_lookup(handler, query)
    if controller is None:
        return
    try:
        if not _await_release(controller, "before-headers"):
            return
        _send_text(handler, FAULT_RELEASED_BODY)
    except OSError:
        controller["disconnected"] = True
    finally:
        controller["exited"].set()


def _r_fault_hang_after_headers(handler, query: str) -> None:
    """Hang AFTER headers with a partial body: a complete 200 head plus one
    chunk, flushed, then nothing until released.

    The navigation COMMITS and the document is partially parseable while the
    transfer is demonstrably still in flight — the phase that separates
    "committed" from "complete". The tail chunk exists only after a release, so
    ``#complete`` is a fact about the transfer, not about timing.
    """
    controller = _fault_lookup(handler, query)
    if controller is None:
        return
    try:
        _write_partial_head(handler)
        if not _await_release(controller, "after-headers"):
            return
        tail = fault_partial_tail().encode("utf-8")
        handler.wfile.write(
            f"{len(tail):x}\r\n".encode("latin-1") + tail + b"\r\n0\r\n\r\n"
        )
        handler.wfile.flush()
    except OSError:
        controller["disconnected"] = True
    finally:
        handler.close_connection = True
        controller["exited"].set()


def _r_fault_drop(handler, query: str) -> None:
    """Mid-transfer DROP: the same committed 200 and partial body, then the
    connection is ABORTED instead of finished.

    ``SO_LINGER(on, 0)`` makes the eventual close emit an RST rather than a FIN,
    so the peer observes a dropped connection rather than a clean end of stream
    — a network failure that happens strictly after the operation started. The
    socket is not closed here: socketserver owns that, and a double close would
    only add noise.
    """
    controller = _fault_lookup(handler, query)
    if controller is None:
        return
    try:
        _write_partial_head(handler)
        if not _await_release(controller, "drop"):
            return
        handler.connection.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
    except OSError:
        controller["disconnected"] = True
    finally:
        handler.close_connection = True
        controller["exited"].set()


def release_all_faults(state: dict[str, Any], timeout: float = 10.0) -> list[str]:
    """Release every armed controller and wait for its handler thread to leave.

    Returns the tokens whose handler had ENTERED but did not exit inside
    *timeout*; an empty list is the "no wedged handler" proof fixture
    finalization asserts on. This must run BEFORE ``httpd.shutdown()``, which
    only stops the accept loop and never interrupts an in-flight request.
    """
    with state["lock"]:
        controllers = list(state["faults"].values())
    for controller in controllers:
        controller["release"].set()
    deadline = time.monotonic() + timeout
    stuck: list[str] = []
    for controller in controllers:
        if not controller["entered"].is_set():
            continue
        remaining = max(0.0, deadline - time.monotonic())
        if not controller["exited"].wait(remaining):
            stuck.append(controller["token"])
    return stuck
