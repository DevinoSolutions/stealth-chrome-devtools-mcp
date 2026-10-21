"""Fixed tokens and payloads, per-origin state and the low-level response helpers.

Split out of :mod:`fixture_routes` (T-5431); that module re-exports every name
here, so callers keep importing from ``fixture_routes``.
"""

from __future__ import annotations

import base64
import json
import threading
from typing import Any

# ── Fixed tokens and payloads (the exact oracles the W7 tests assert on) ─────
CSP_HEADER_NAME = "Content-Security-Policy"
CSP_HEADER_VALUE = (
    "default-src 'self'; script-src 'nonce-e2e9'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'"
)
CSP_NONCE = "e2e9"
CSP_PING_BODY = "csp-ping-ok"

AUTH_USER = "e2e-user"
AUTH_PASS = "e2e-pass"
AUTH_HEADER_VALUE = "Basic " + base64.b64encode(
    f"{AUTH_USER}:{AUTH_PASS}".encode()
).decode("ascii")
AUTH_REALM = 'Basic realm="e2e"'
AUTH_GRANTED_BODY = "auth-granted-token"
AUTH_CHALLENGE_BODY = "auth-required-token"

PAYLOAD_TEXT_BODY = "payload-text-body-e2e-119"
PAYLOAD_BINARY_LENGTH = 4096
PAYLOAD_CHUNKS = ("chunk-one-", "chunk-two-", "chunk-three")
PAYLOAD_CHUNKED_BODY = "".join(PAYLOAD_CHUNKS)
STATUS_418_BODY = "status-418-body"
STATUS_503_BODY = "status-503-body"

FEED_PAGE_SIZE = 25
FEED_LAST_PAGE = 3  # pages 0..3 carry rows; page 4+ is the terminal sentinel
FEED_TOTAL_ROWS = FEED_PAGE_SIZE * (FEED_LAST_PAGE + 1)

VIRTUAL_TOTAL_ROWS = 1000
VIRTUAL_POOL_SIZE = 20

SSE_EVENTS = (("1", "sse-alpha"), ("2", "sse-beta"), ("3", "sse-gamma"))
WS_MESSAGES = ("alpha", "beta", "gamma")
WS_CLOSE_CODE = 1000
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

POPUP_TARGET_TOKEN = "popup-target-token"

# ── W10 fault controllers (MQ-126…129) ──────────────────────────────────────
# The ceiling is a FAILURE bound, never a synchronization point: the only thing
# that ends a fault wait in a passing run is the test's own /fault/release. It
# exists so a test that dies before releasing cannot park a request thread past
# the end of the session. A ceiling hit is recorded and is never a pass signal.
FAULT_CEILING_SECONDS = 25.0
FAULT_SLOW_SENTINEL = "fixture-fault-slow-page"
FAULT_SLOW_BODY = "slow-success-body-e2e-w10"
FAULT_PARTIAL_SENTINEL = "fixture-fault-partial-page"
FAULT_PARTIAL_PREFIX = "partial-body-prefix-e2e-w10"
FAULT_PARTIAL_SUFFIX = "completed-body-suffix-e2e-w10"
FAULT_RELEASED_BODY = "fault-released-body-e2e-w10"

# ── W16 workers / PWA / state / i18n (MQ-155…162) ───────────────────────────
# Every W16 oracle is computed TWICE — once in the page or worker (JavaScript)
# and once here (Python) — and the test asserts the two agree. FNV-1a/32 is the
# shared transform because it reproduces exactly in both languages with no
# library, no float, and no platform-dependent digest: ``crypto.subtle`` is a
# promise API, and the product's ``execute_script`` evaluates a *synchronous*
# expression, so a page can never hand a test an awaited digest directly.
W16_WORKER_VERSION = "w16-dedicated-v1"
W16_WORKER_IDS = (1, 2, 3)
W16_WORKER_SALT = "w16-dedicated-salt"
W16_WORKER_CLOSED = "w16-dedicated-closed"
W16_WORKER_LATE_ID = 99  # posted AFTER self.close(); must never be answered

W16_SHARED_VERSION = "w16-shared-v1"
W16_SHARED_ZERO_CLIENTS = "w16-shared-zero-clients"

W16_SW_VERSION = "w16-sw-v1"
W16_SW_SCOPE = "/pwa/"
W16_SW_CACHE = "w16-pwa-cache-v1"
W16_SW_INSTALLED = "w16-sw-installed"
W16_SW_ACTIVATED = "w16-sw-activated"
W16_SW_ASSET_PATH = "/pwa/asset.txt"
W16_SW_ASSET_BODY = "w16-pwa-asset-body-0123456789"
W16_SW_OFFLINE_BODY = "w16-pwa-offline-deterministic-body"
W16_SW_UNCACHED_PATH = "/pwa/never-cached.txt"
W16_SW_UNCACHED_BODY = "w16-pwa-never-cached-online-body"

W16_STATE_CACHE = "w16-state-cache-v1"
W16_CACHE_ENTRIES = (
    ("/state/cache-a.txt", "w16-cache-entry-alpha"),
    ("/state/cache-b.txt", "w16-cache-entry-beta"),
    ("/state/cache-c.txt", "w16-cache-entry-gamma"),
)

W16_IDB_NAME = "w16-idb-v1"
W16_IDB_STORE = "records"
W16_IDB_INDEX = "by_group"
# (id, name, group, payload) — the index is on ``group``, so the oracle below
# can predict every index cursor result without asking the page.
W16_IDB_RECORDS = (
    (1, "alpha", "g1", "payload-alpha"),
    (2, "bravo", "g1", "payload-bravo"),
    (3, "charlie", "g2", "payload-charlie"),
    (4, "delta", "g2", "payload-delta"),
    (5, "echo", "g2", "payload-echo"),
    (6, "foxtrot", "g3", "payload-foxtrot"),
)
W16_IDB_ABORT_ID = 99  # written then aborted; must never be readable

W16_LOCAL_KEY = "w16-local-key"
W16_LOCAL_VALUE = "w16-local-value-persist"
W16_SESSION_KEY = "w16-session-key"
W16_SESSION_VALUE = "w16-session-value-ephemeral"
W16_COOKIE_PERSISTENT = "w16_persistent"
W16_COOKIE_PERSISTENT_VALUE = "w16-cookie-persistent-value"
W16_COOKIE_SESSION = "w16_session"
W16_COOKIE_SESSION_VALUE = "w16-cookie-session-value"
W16_COOKIE_MAX_AGE = 3600

# ── The internationalization oracle (MQ-161) ────────────────────────────────
# These constants are the only place the exact strings exist, and the whole MQ
# is an assertion about their CODE POINTS — so a tool, editor, or diff that
# re-encoded this file would corrupt the oracle and the fixture at once, and
# the test would still pass. ``test_fixture_dynamic_routes`` therefore pins
# every one of them to a literal code-point list; nothing here is trusted to
# have survived transport just because it renders.
I18N_NFC = "é"  # LATIN SMALL LETTER E WITH ACUTE (precomposed)
I18N_NFD = "é"  # e + COMBINING ACUTE — canonically equivalent, NOT equal
I18N_COMBINING = "ą́"  # stacked combining acute + ogonek
I18N_EMOJI_ZWJ = "\U0001f468‍\U0001f469‍\U0001f467"  # family, 5 cp
I18N_NON_BMP = "\U0001d11e\U00020bb7"  # G-CLEF + CJK ext-B (surrogate pairs)
I18N_RTL = "אבג العربية"
I18N_BIDI_ISOLATE = (  # LRI/RLI/FSI + PDI, escaped: PLE2502 bans the literals
    "\u2066LTR\u2069\u2067RTL\u2069\u2068auto\u2069"
)
I18N_MIXED_ATTR = "abc אבג 123 العرب"
# id -> string. The page renders each one as a text node AND as an attribute,
# and offers one input per string, so a single table drives all three round
# trips and no round trip can be silently dropped from the fixture.
I18N_STRINGS: dict[str, str] = {
    "nfc": I18N_NFC,
    "nfd": I18N_NFD,
    "combining": I18N_COMBINING,
    "emoji": I18N_EMOJI_ZWJ,
    "nonbmp": I18N_NON_BMP,
    "rtl": I18N_RTL,
    "isolate": I18N_BIDI_ISOLATE,
    "mixed": I18N_MIXED_ATTR,
}
I18N_TYPED_KEYS = ("nfc", "nfd", "combining", "rtl")  # keystroke-synthesizable
COMPOSITION_STEPS = ("か", "かん", "漢")  # ka -> kan -> kanji
COMPOSITION_FINAL = "漢字"  # the committed string the page must hold

# ── F-882 navigation supersession (the ``nav_*`` shapes) ────────────────────
# Every way the document Chrome committed for a navigation's OWN loaderId can
# be replaced before it reaches ``load`` — a head-script ``location.replace``,
# a ``meta refresh``, a JS challenge that re-navigates itself, a page that
# reloads once, a download that commits nothing, and a pending navigation the
# page itself pre-empts. The live sites that showed these (a signed-out Gmail,
# YouTube, Reddit's ``js_challenge``, Amazon's self-reload) are not fixtures:
# they change under us and a test may never reach the network, so each shape is
# reproduced here exactly and locally.
NAV_LANDING_SENTINEL = "fixture-nav-landing-page"
NAV_LANDING_TITLE = "Nav Landing"
NAV_HEAD_REPLACE_SENTINEL = "fixture-nav-head-replace-page"
NAV_META_REFRESH_SENTINEL = "fixture-nav-meta-refresh-page"
NAV_SELF_RELOAD_SENTINEL = "fixture-nav-self-reload-page"
NAV_RELOADED_TITLE = "Nav Reloaded"
NAV_CHALLENGE_SENTINEL = "fixture-nav-js-challenge-page"
NAV_SOLVED_SENTINEL = "fixture-nav-challenge-solved-page"
NAV_SOLVED_TITLE = "Nav Challenge Solved"
NAV_SOLUTION = "nav-solution-f882"
NAV_CHALLENGE_COOKIE = "nav_jsc"
NAV_CHAIN_SENTINEL = "fixture-nav-chain-final-page"
NAV_CHAIN_TITLE = "Nav Chain Final"
NAV_SLOW_DOC_SENTINEL = "fixture-nav-slow-doc-page"
NAV_SLOW_DOC_TITLE = "Nav Slow Doc"
NAV_PREEMPT_SENTINEL = "fixture-nav-preempt-page"
NAV_PREEMPT_TITLE = "Nav Preempt"
NAV_IFRAME_HOST_SENTINEL = "fixture-nav-iframe-host-page"
NAV_IFRAME_HOST_TITLE = "Nav Iframe Host"
NAV_DOWNLOAD_NAME = "nav-download.bin"
NAV_DOWNLOAD_BODY = b"nav-download-body-e2e-f882"
# The one thing a supersession fixture cannot express as an event: "this
# document had not reached ``load`` yet" IS a duration. Both delayed routes read
# it from ``?ms=``, defaulting to 0 so the hermetic enumeration backstop (which
# requests every route with NO query) never waits, and both clamp to this
# ceiling so a mistyped query cannot park a server thread.
NAV_DELAY_CEILING_SECONDS = 5.0
# A 1x1 transparent PNG — the smallest real subresource ``load`` must wait for.
NAV_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def w16_hash(text: str) -> str:
    """FNV-1a/32 over UTF-16 code units — the JS ``charCodeAt`` domain.

    The page-side twin XORs ``s.charCodeAt(i)``, which yields UTF-16 *code
    units* (so a non-BMP character contributes its two surrogates). Hashing
    Python's code points instead would silently disagree on exactly the
    non-BMP inputs W16 cares about, so the units are reconstructed explicitly.
    """
    data = text.encode("utf-16-le")
    h = 0x811C9DC5
    for index in range(0, len(data), 2):
        h ^= data[index] | (data[index + 1] << 8)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def w16_worker_replies() -> list[dict[str, Any]]:
    """The exact ordered replies ``/workers/dedicated.js`` must post for ids 1..3."""
    return [
        {
            "id": message_id,
            "echo": f"{W16_WORKER_VERSION}:{message_id}",
            "hash": w16_hash(f"{W16_WORKER_SALT}:{message_id}"),
        }
        for message_id in W16_WORKER_IDS
    ]


def w16_cache_oracle(name: str, entries) -> dict[str, Any]:
    """The independently computed count/keys/hashes for one seeded cache."""
    items = [
        {
            "path": path,
            "status": 200,
            "length": len(body),
            "hash": w16_hash(body),
        }
        for path, body in sorted(entries)
    ]
    return {"name": name, "count": len(items), "items": items}


def w16_idb_group(group: str) -> list[dict[str, Any]]:
    """Every record the ``by_group`` index must yield for *group*, in key order."""
    return [
        {"id": rid, "name": name, "group": grp, "payload": payload}
        for rid, name, grp, payload in W16_IDB_RECORDS
        if grp == group
    ]


def code_points(text: str) -> list[int]:
    """The exact code points of *text* — the one i18n comparison currency."""
    return [ord(character) for character in text]


def _ncr(text: str) -> str:
    """Numeric character references for *text* (ASCII-safe served markup)."""
    return "".join(f"&#x{ord(character):x};" for character in text)


def payload_binary_body() -> bytes:
    """The 4,096 seeded bytes ``/payload/binary`` returns.

    Deliberately NOT valid UTF-8 (``i == 254`` yields ``0xFF``, which no UTF-8
    sequence may contain), so ``get_response_content`` takes its declared
    base64 branch every time rather than only usually.
    """
    return bytes((i * 7 + 13) % 256 for i in range(PAYLOAD_BINARY_LENGTH))


def feed_rows(page: int) -> list[str]:
    """The exact ordered rows ``/api/feed?page=<page>`` serves."""
    if page > FEED_LAST_PAGE:
        return []
    start = page * FEED_PAGE_SIZE
    return [f"feed-row-{i}" for i in range(start, start + FEED_PAGE_SIZE)]


# ── Per-origin state ────────────────────────────────────────────────────────
def new_origin_state(role: str) -> dict[str, Any]:
    """A fresh origin descriptor: identity, peer link, and the server ledger.

    ``self_url``/``peer_url`` are filled in by the harness AFTER both sockets
    bind (each origin's port is only known then) and BEFORE either server
    starts accepting, so no request can ever observe a half-linked pair.
    """
    return {
        "role": role,
        "self_url": "",
        "peer_url": "",
        "lock": threading.Lock(),
        "ledger": _new_ledger(),
        # W10's token -> fault controller registry. A SIBLING of the ledger, not
        # a key inside it: ``/e2e/reset`` replaces the ledger wholesale, and a
        # reset must never orphan an in-flight waiter.
        "faults": {},
    }


def _new_ledger() -> dict[str, Any]:
    return {
        "feed_pages": [],  # every ?page= value actually requested
        "auth_headers": [],  # every Authorization value /auth/basic received
        "cors_methods": [],  # every method /cors/* received (preflight order)
        "streams": [],  # "sse" / "ws" as each stream is served
        # W16. These three are the reason the workers and the service worker
        # are believable at all: a worker's lifecycle is invisible from the
        # page it outlived, so each milestone is reported to the SERVER and
        # read back from here. ``w16_asset`` is the offline oracle — a cached
        # read must not add an entry.
        "w16_shared": [],  # the shared worker's zero-client teardown report
        "w16_sw": [],  # service-worker install/activate reports
        "w16_asset": [],  # every NETWORK hit on the service-worker-cached asset
        # F-882. Which documents the BROWSER actually fetched for one
        # navigation, in order — the independent oracle for "our loader was
        # superseded": a tool that answered without the second fetch having
        # happened cannot agree with this.
        "nav_paths": [],
    }


def _record(handler, key: str, value: Any) -> None:
    state = handler.origin_state
    with state["lock"]:
        state["ledger"][key].append(value)


# ── Low-level response helpers ──────────────────────────────────────────────
def _send(handler, status: int, headers, body: bytes = b"") -> None:
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def _send_html(handler, markup: str, extra_headers=()) -> None:
    _send(
        handler,
        200,
        [("Content-Type", "text/html; charset=utf-8"), *extra_headers],
        markup.encode("utf-8"),
    )


def _send_text(handler, text: str, status: int = 200, extra_headers=()) -> None:
    _send(
        handler,
        status,
        [("Content-Type", "text/plain; charset=utf-8"), *extra_headers],
        text.encode("utf-8"),
    )


def _send_json(handler, payload, status: int = 200, extra_headers=()) -> None:
    _send(
        handler,
        status,
        [("Content-Type", "application/json"), *extra_headers],
        json.dumps(payload).encode("utf-8"),
    )


def _send_raw(handler, head: str, body: bytes = b"") -> None:
    """Write a complete response ourselves and close.

    ``BaseHTTPRequestHandler`` speaks HTTP/1.0 and appends ``Content-Length``;
    a chunked, event-stream, or upgraded response needs neither and must
    control its own framing, so those routes bypass ``send_response`` entirely.
    """
    handler.wfile.write(head.encode("latin-1") + body)
    handler.wfile.flush()
    handler.close_connection = True


# ── Page builders ───────────────────────────────────────────────────────────
def _fill(template: str, **values: Any) -> str:
    """Substitute ``__NAME__`` placeholders in a JS/HTML template.

    Plain replacement rather than %-format, ``str.format``, or an f-string:
    these templates are mostly JavaScript, and every formatting mini-language
    would try to interpret the braces in it.
    """
    for name, value in values.items():
        template = template.replace(f"__{name.upper()}__", str(value))
    return template


def _page(title: str, sentinel: str, body: str, head: str = "") -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>{head}</head><body>"
        f"<p id='sentinel'>{sentinel}</p>{body}</body></html>"
    )
