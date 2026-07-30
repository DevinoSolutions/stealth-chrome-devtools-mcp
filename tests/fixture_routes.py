"""plan_RELEASE W7/W10 — the DYNAMIC half of the one fixture app.

``tests/fixture_app/`` holds the static pages the plan_E2E suite navigates to.
This module holds the pages and API routes W7's eight adversarial shapes need
that a static file *cannot* express: an exact response header, a redirect, a
401 challenge, a chunked or ``text/event-stream`` body, a WebSocket upgrade, or
any page that must name its **peer origin** (whose port is ephemeral and so can
never be written into a file on disk).

There is still ONE serving mechanism: ``release_gate_harness._FixtureHandler``
dispatches here before falling through to static files, and
``release_gate_harness.serve_fixture_origin_pair`` starts the two independent
``127.0.0.1:0`` servers W7 needs. This module owns *what* the routes are; the
harness owns *how* they are served.

Composition (W16 adds PWA/worker fixtures here): a route is one entry in
:data:`ROUTES` keyed by ``(method, path)``, whose value takes the live
``BaseHTTPRequestHandler`` and writes the response. Adding a shape means adding
entries and page builders — never a second handler, server, or dispatch.

Per-origin state
----------------
Each origin gets a :func:`new_origin_state` dict carrying its own base URL, its
peer's base URL, a role (``"a"``/``"b"``) so symmetric routes can return
distinguishable tokens, and a small server-side **ledger**. The ledger is the
independent acceptance oracle for several MQs: it records what the browser
actually sent (the ``Authorization`` header, which feed pages were requested,
which streams were served), so a test never has to take the page's own word for
it. ``GET /e2e/reset`` clears it.

Determinism: no route sleeps, blocks on a timer, or synchronizes on elapsed
time. Streams emit their full declared content and end; every controller a page
exposes is driven by an event (a click, a fetch resolution, a stream message).

W10's fault routes are the one deliberate exception to "a route never blocks",
and they keep the rule that matters: a blocked route is waiting on an **event**
the test sets (``/fault/release``), never on elapsed time. Each is addressed by
a ``?token=`` the test mints, exposes an ``entered`` barrier so a test can know
the request really arrived before injecting its fault, and is bounded by
:data:`FAULT_CEILING_SECONDS` — a *failure* bound so a test that dies mid-fault
cannot wedge a server thread, never a synchronization point. Disconnects are
expected during a fault and are recorded rather than raised, and
:func:`release_all_faults` lets fixture finalization prove every handler left
before the server is shut down.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from collections.abc import Callable
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


def spa_history_page() -> str:
    """MQ-114: every transition REPLACES ``#route-root`` and bumps a generation.

    ``#route-root`` is never in the served markup — ``render`` creates it — so
    "the element the test just queried is the one this transition made" is a
    structural property of the page, not a promise.
    """
    script = """
(function () {
  var state = { log: [], gen: 0, route: null };
  window.__spa = state;
  var app = document.getElementById('app');
  function render(route) {
    state.gen += 1;
    var gen = state.gen;
    state.route = route;
    var old = document.getElementById('route-root');
    if (old) { old.remove(); }
    var root = document.createElement('div');
    root.id = 'route-root';
    root.setAttribute('data-route', route);
    root.setAttribute('data-gen', String(gen));
    var label = document.createElement('p');
    label.id = 'route-label';
    label.textContent = 'route-' + route + '-' + gen;
    var btn = document.createElement('button');
    btn.id = 'route-action';
    btn.type = 'button';
    btn.textContent = 'act-' + route;
    btn.addEventListener('click', function () {
      state.log.push('action:' + route + ':' + gen);
    });
    root.appendChild(label);
    root.appendChild(btn);
    app.appendChild(root);
    state.log.push('route:' + route + ':' + gen);
  }
  window.addEventListener('popstate', function (ev) {
    render((ev.state && ev.state.route) || 'home');
  });
  document.getElementById('nav-push').addEventListener('click', function () {
    history.pushState({ route: 'alpha' }, '', '?route=alpha');
    render('alpha');
  });
  document.getElementById('nav-replace').addEventListener('click', function () {
    history.replaceState({ route: 'beta' }, '', '?route=beta');
    render('beta');
  });
  history.replaceState({ route: 'home' }, '', location.pathname);
  render('home');
})();
"""
    body = (
        "<button id='nav-push' type='button'>push alpha</button>"
        "<button id='nav-replace' type='button'>replace beta</button>"
        "<div id='app'></div>"
        f"<script>{script}</script>"
    )
    return _page("fixture-spa-history-page", "fixture-spa-history-page", body)


def frames_outer_page(peer: str) -> str:
    """MQ-115 origin A: the outer document, whose only child frame is on B."""
    body = (
        f"<iframe id='frame-b-middle' name='e2e-frame-b' "
        f"src='{peer}/frames/b_middle.html'></iframe>"
    )
    return _page("fixture-frames-outer", "fixture-frames-a-outer", body)


def frames_middle_page(peer: str) -> str:
    """MQ-115 origin B: the middle document, whose child frame is back on A."""
    body = (
        f"<iframe id='frame-a-inner' name='e2e-frame-a-inner' "
        f"src='{peer}/frames/a_inner.html'></iframe>"
    )
    return _page("fixture-frames-middle", "fixture-frames-b-middle", body)


def frames_inner_page() -> str:
    """MQ-115 origin A again: the innermost document (A→B→A)."""
    return _page("fixture-frames-inner", "fixture-frames-a-inner", "")


def lazy_virtual_infinite_page() -> str:
    """MQ-116: one IntersectionObserver target, a recycled 20-node pool over
    1,000 logical rows, and a finite "infinite" feed.

    The pool nodes are created ONCE and stamped with a JS-only property
    (``__nodeStamp``, invisible to the DOM), so a test can prove the nodes were
    recycled rather than rebuilt without trusting the selector tool to tell it.
    """
    style = (
        "<style>#lazy-spacer{height:4000px}"
        ".v-row,.feed-row{height:14px;font:12px monospace}</style>"
    )
    script = """
(function () {
  window.__lazy = { observed: false };
  var target = document.getElementById('lazy-sentinel');
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting || window.__lazy.observed) { return; }
      window.__lazy.observed = true;
      var token = document.createElement('p');
      token.id = 'lazy-token';
      token.textContent = 'lazy-observed';
      target.appendChild(token);
      io.disconnect();
    });
  });
  io.observe(target);

  var POOL = __POOL__;
  var TOTAL = __TOTAL__;
  var host = document.getElementById('virtual-list');
  var nodes = [];
  for (var i = 0; i < POOL; i++) {
    var node = document.createElement('div');
    node.className = 'v-row';
    node.setAttribute('data-node-index', String(i));
    node.__nodeStamp = 'n' + i;
    host.appendChild(node);
    nodes.push(node);
  }
  window.__virtual = { total: TOTAL, pool: POOL, start: -1 };
  window.__virtual.stamps = function () {
    return Array.prototype.map.call(
      document.querySelectorAll('.v-row'),
      function (n) { return n.__nodeStamp === undefined ? 'rebuilt' : n.__nodeStamp; }
    );
  };
  function paint(start) {
    for (var i = 0; i < POOL; i++) {
      var logical = start + i;
      nodes[i].setAttribute('data-row-id', 'row-' + logical);
      nodes[i].textContent = 'virtual-row-' + logical;
    }
    window.__virtual.start = start;
  }
  document.getElementById('virtual-advance').addEventListener('click', function () {
    var next = window.__virtual.start + POOL;
    paint(next >= TOTAL ? 0 : next);
  });
  paint(0);

  window.__feed = { rows: [], requests: [], done: false, page: 0 };
  function loadNext() {
    var url = '/api/feed?page=' + window.__feed.page;
    window.__feed.requests.push(url);
    return fetch(url).then(function (r) { return r.json(); }).then(function (j) {
      var list = document.getElementById('feed-list');
      j.rows.forEach(function (row) {
        window.__feed.rows.push(row);
        var li = document.createElement('li');
        li.className = 'feed-row';
        li.textContent = row;
        list.appendChild(li);
      });
      if (j.has_more) {
        window.__feed.page += 1;
        return loadNext();
      }
      window.__feed.done = true;
      return null;
    });
  }
  document.getElementById('feed-load').addEventListener('click', function () {
    if (window.__feed.requests.length === 0) { loadNext(); }
  });
})();
"""
    script = _fill(script, pool=VIRTUAL_POOL_SIZE, total=VIRTUAL_TOTAL_ROWS)
    body = (
        "<button id='virtual-advance' type='button'>advance</button>"
        "<button id='feed-load' type='button'>load feed</button>"
        "<div id='virtual-list'></div>"
        "<ul id='feed-list'></ul>"
        "<div id='lazy-spacer'></div>"
        "<div id='lazy-sentinel'></div>"
        f"<script>{script}</script>"
    )
    return _page(
        "fixture-lazy-virtual-page", "fixture-lazy-virtual-page", body, head=style
    )


def csp_strict_page(peer: str) -> str:
    """MQ-117: nonce script, inline script, eval, self fetch, and a peer fetch.

    The violation listener is registered by the FIRST nonce script, before the
    parser reaches the inline script it must observe — so the recorded order is
    a property of document order, not of timing.
    """
    listener = """
window.__csp = { violations: [], nonce_ran: false, inline_ran: false,
                 eval_ran: false, self_fetch: null, peer_fetch: null, done: false };
document.addEventListener('securitypolicyviolation', function (e) {
  window.__csp.violations.push({
    directive: e.effectiveDirective,
    blocked: e.blockedURI
  });
});
window.__csp.nonce_ran = true;
"""
    attempts = """
(function () {
  try { (0, eval)('window.__csp.eval_ran = true;'); }
  catch (err) { window.__csp.eval_error = err.name; }
  fetch('/csp/ping').then(function (r) { return r.text(); }).then(function (t) {
    window.__csp.self_fetch = t;
  }).catch(function (err) {
    window.__csp.self_fetch = 'error:' + err.name;
  }).then(function () {
    return fetch('__PEER__/csp/ping').then(function (r) { return r.text(); })
      .then(function (t) { window.__csp.peer_fetch = t; })
      .catch(function (err) { window.__csp.peer_fetch = 'error:' + err.name; });
  }).then(function () { window.__csp.done = true; });
})();
"""
    attempts = _fill(attempts, peer=peer)
    body = (
        f"<script nonce='{CSP_NONCE}'>{listener}</script>"
        "<script>window.__csp.inline_ran = true;</script>"
        f"<script nonce='{CSP_NONCE}'>{attempts}</script>"
    )
    return _page("fixture-csp-strict-page", "fixture-csp-strict-page", body)


def redirect_final_page(role: str) -> str:
    """MQ-118: the terminal document of a redirect chain, tokenized by origin."""
    return _page(f"fixture-redirect-final-{role}", f"redirect-final-token-{role}", "")


def dynamic_probe_page(peer: str) -> str:
    """The origin-A driver page for MQ-118's auth/CORS and MQ-119's payloads.

    It exposes controllers only; nothing runs on load, so each test starts its
    own shape from a known-empty sentinel.
    """
    script = """
(function () {
  var PEER = '__PEER__';
  // Only the CREDENTIALED request runs in-page. A 401 carrying
  // `WWW-Authenticate: Basic` never settles a fetch under headless Chrome —
  // measured identically with plain nodriver and with the product, and with
  // `credentials:'omit'` — because the challenge has no prompt delegate to
  // answer it. The challenge branch is therefore asserted from the test
  // process over plain HTTP, where it is exact; see the MQ-118 node.
  window.__auth = { done: false };
  window.__auth.run = function () {
    return fetch('/auth/basic', {
      headers: { 'Authorization': '__AUTH__' }
    }).then(function (r) {
      window.__auth.status = r.status;
      window.__auth.header = r.headers.get('X-E2E-Auth');
      return r.text();
    }).then(function (t) {
      window.__auth.body = t;
      window.__auth.done = true;
    });
  };

  window.__cors = { done: false };
  window.__cors.run = function () {
    return fetch(PEER + '/cors/echo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-E2E-Probe': 'probe' },
      body: JSON.stringify({ ping: 'cors' })
    }).then(function (r) {
      window.__cors.echo_status = r.status;
      return r.json();
    }).then(function (j) {
      window.__cors.echo = j.echo;
    }).catch(function (err) {
      window.__cors.echo = 'error:' + err.name;
    }).then(function () {
      return fetch(PEER + '/cors/blocked').then(function (r) { return r.text(); })
        .then(function (t) { window.__cors.blocked = t; })
        .catch(function (err) { window.__cors.blocked = 'error:' + err.name; });
    }).then(function () { window.__cors.done = true; });
  };

  window.__payload = { done: false, seen: {} };
  window.__payload.run = function () {
    var text = ['/payload/text', '/payload/chunked', '/status/418', '/status/503'];
    var chain = Promise.resolve();
    text.forEach(function (path) {
      chain = chain.then(function () {
        return fetch(path).then(function (r) {
          window.__payload.seen[path] = { status: r.status };
          return r.text();
        }).then(function (t) {
          window.__payload.seen[path].body = t;
        });
      });
    });
    return chain.then(function () {
      return fetch('/payload/binary').then(function (r) {
        window.__payload.seen['/payload/binary'] = { status: r.status };
        return r.arrayBuffer();
      }).then(function (buf) {
        window.__payload.seen['/payload/binary'].length = buf.byteLength;
      });
    }).then(function () { window.__payload.done = true; });
  };
})();
"""
    script = _fill(script, peer=peer, auth=AUTH_HEADER_VALUE)
    return _page(
        "fixture-dynamic-probe-page",
        "fixture-dynamic-probe-page",
        f"<script>{script}</script>",
    )


def events_page() -> str:
    """MQ-120: a bounded sentinel recording both streams to completion."""
    script = """
(function () {
  window.__streams = {
    sse: { ids: [], data: [], closed: false, error: false },
    ws: { messages: [], code: null, clean: null, closed: false },
    done: false
  };
  function maybeDone() {
    if (window.__streams.sse.closed && window.__streams.ws.closed) {
      window.__streams.done = true;
    }
  }
  var es = new EventSource('/events/sse');
  es.onmessage = function (ev) {
    var s = window.__streams.sse;
    if (s.closed || s.ids.length >= 3) { return; }
    s.ids.push(ev.lastEventId);
    s.data.push(ev.data);
    if (s.ids.length === 3) {
      es.close();
      s.closed = true;
      maybeDone();
    }
  };
  es.onerror = function () {
    var s = window.__streams.sse;
    if (s.closed) { return; }
    s.error = true;
    es.close();
    s.closed = true;
    maybeDone();
  };
  var ws = new WebSocket(location.origin.replace('http', 'ws') + '/events/ws');
  ws.onmessage = function (ev) {
    var w = window.__streams.ws;
    if (w.messages.length < 8) { w.messages.push(ev.data); }
  };
  ws.onclose = function (ev) {
    var w = window.__streams.ws;
    w.code = ev.code;
    w.clean = ev.wasClean;
    w.closed = true;
    maybeDone();
  };
})();
"""
    return _page(
        "fixture-events-page", "fixture-events-page", f"<script>{script}</script>"
    )


def popup_components_page() -> str:
    """MQ-121: a tokenized ``target=_blank`` link plus the component surface.

    Lifecycle order is a language guarantee, not a race: ``customElements
    .define`` runs after the element is already parsed, so the upgrade fires
    ``attributeChangedCallback`` and then ``connectedCallback`` synchronously.
    """
    script = """
(function () {
  window.__components = { lifecycle: [], slots: [] };
  class FixtureCard extends HTMLElement {
    static get observedAttributes() { return ['label']; }
    attributeChangedCallback(name, oldValue, newValue) {
      window.__components.lifecycle.push(
        'attr:' + name + ':' + String(oldValue) + '->' + String(newValue));
    }
    connectedCallback() {
      window.__components.lifecycle.push('connected:' + this.id);
      var frag = document.getElementById('card-template').content.cloneNode(true);
      var holder = document.createElement('div');
      holder.className = 'card-template-holder';
      holder.appendChild(frag);
      this.appendChild(holder);
      window.__components.lifecycle.push('template-cloned:' + this.id);
      var slots = Array.prototype.map.call(
        this.querySelectorAll('[slot]'),
        function (n) { return n.getAttribute('slot'); });
      window.__components.slots = slots;
      window.__components.lifecycle.push('slots:' + slots.join(','));
    }
  }
  customElements.define('fixture-card', FixtureCard);
  var host = document.getElementById('shadow-host');
  var root = host.attachShadow({ mode: 'open' });
  var p = document.createElement('p');
  p.id = 'shadow-sentinel';
  p.textContent = 'open-shadow-sentinel-token';
  root.appendChild(p);
})();
"""
    body = (
        f"<a id='popup-link' target='_blank' "
        f"href='/popup_target.html?token={POPUP_TARGET_TOKEN}'>open popup</a>"
        "<template id='card-template'>"
        "<p class='card-template-line'>template-line-token</p></template>"
        "<fixture-card id='card-one' label='alpha'>"
        "<span slot='title'>slot-title-alpha</span>"
        "<div slot='body'><span class='nested-slot-leaf'>slot-body-leaf-alpha"
        "</span></div>"
        "</fixture-card>"
        "<div id='shadow-host'></div>"
        f"<script>{script}</script>"
    )
    return _page("fixture-popup-components-page", "fixture-popup-components-page", body)


def popup_target_page() -> str:
    return _page("fixture-popup-target-page", "fixture-popup-target-page", "")


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


# ── The route table (W16 extends THIS; it does not add a second one) ────────
Route = Callable[[Any, str], None]

ROUTES: dict[tuple[str, str], Route] = {
    # MQ-114
    ("GET", "/spa_history.html"): _r_spa,
    # MQ-115
    ("GET", "/frames/a_outer.html"): _r_frames_outer,
    ("GET", "/frames/b_middle.html"): _r_frames_middle,
    ("GET", "/frames/a_inner.html"): _r_frames_inner,
    # MQ-116
    ("GET", "/lazy_virtual_infinite.html"): _r_lazy,
    ("GET", "/api/feed"): _r_feed,
    # MQ-117
    ("GET", "/csp/strict"): _r_csp_strict,
    ("GET", "/csp/ping"): _r_csp_ping,
    # MQ-118
    ("GET", "/auth/basic"): _r_auth_basic,
    ("GET", "/redirect/start"): _r_redirect_start,
    ("GET", "/redirect/to-b"): _r_redirect_to_b,
    ("GET", "/redirect/final"): _r_redirect_final,
    ("OPTIONS", "/cors/echo"): _r_cors_echo_options,
    ("POST", "/cors/echo"): _r_cors_echo_post,
    ("GET", "/cors/blocked"): _r_cors_blocked,
    # MQ-119
    ("GET", "/payload/text"): _r_payload_text,
    ("GET", "/payload/binary"): _r_payload_binary,
    ("GET", "/payload/chunked"): _r_payload_chunked,
    ("GET", "/status/418"): _r_status_418,
    ("GET", "/status/503"): _r_status_503,
    # MQ-120
    ("GET", "/events/sse"): _r_sse,
    ("GET", "/events/ws"): _r_ws,
    ("GET", "/events_page.html"): _r_events_page,
    # MQ-121
    ("GET", "/popup_components.html"): _r_popup_components,
    ("GET", "/popup_target.html"): _r_popup_target,
    # MQ-126…129 — the W10 fault controllers. Every one answers a tokenless
    # request immediately, so enumerating ROUTES can never park a thread.
    ("GET", "/fault/arm"): _r_fault_arm,
    ("GET", "/fault/status"): _r_fault_status,
    ("GET", "/fault/release"): _r_fault_release,
    ("GET", "/fault/slow"): _r_fault_slow,
    ("GET", "/fault/hang-before-headers"): _r_fault_hang_before_headers,
    ("GET", "/fault/hang-after-headers"): _r_fault_hang_after_headers,
    ("GET", "/fault/drop"): _r_fault_drop,
    # Shared driver page + the server-side oracle ledger.
    ("GET", "/dynamic_probe.html"): _r_dynamic_probe,
    ("GET", "/e2e/reset"): _r_reset,
    ("GET", "/e2e/ledger"): _r_ledger,
}

# Every page this module serves, and the sentinel each carries. The hermetic
# enumeration backstop in tests/test_fixture_dynamic_routes.py walks this so a
# page cannot be added without a served-and-sentinel-checked proof.
DYNAMIC_PAGES: dict[str, str] = {
    "/spa_history.html": "fixture-spa-history-page",
    "/frames/a_outer.html": "fixture-frames-a-outer",
    "/frames/b_middle.html": "fixture-frames-b-middle",
    "/frames/a_inner.html": "fixture-frames-a-inner",
    "/lazy_virtual_infinite.html": "fixture-lazy-virtual-page",
    "/csp/strict": "fixture-csp-strict-page",
    "/dynamic_probe.html": "fixture-dynamic-probe-page",
    "/redirect/final": "redirect-final-token-",
    "/events_page.html": "fixture-events-page",
    "/popup_components.html": "fixture-popup-components-page",
    "/popup_target.html": "fixture-popup-target-page",
}


def dispatch(handler, method: str) -> bool:
    """Serve ``handler``'s request if it names a dynamic route.

    Returns ``True`` when the response has been written (the caller must not
    fall through to static file serving), ``False`` when the path belongs to
    ``tests/fixture_app`` or to the older plan_E2E API routes.
    """
    path, _, query = handler.path.partition("?")
    route = ROUTES.get((method, path))
    if route is None:
        return False
    route(handler, query)
    return True
