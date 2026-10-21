"""The base page builders (spa, frames, lazy, csp, dynamic probe, events, popups).

Split out of :mod:`fixture_routes` (T-5431); that module re-exports every name
here, so callers keep importing from ``fixture_routes``.
"""

from __future__ import annotations

from fixture_routes_base import (
    AUTH_HEADER_VALUE,
    CSP_NONCE,
    POPUP_TARGET_TOKEN,
    VIRTUAL_POOL_SIZE,
    VIRTUAL_TOTAL_ROWS,
    _fill,
    _page,
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
