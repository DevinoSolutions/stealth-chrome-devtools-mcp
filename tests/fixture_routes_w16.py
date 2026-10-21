"""W16 workers, PWA, state stores and i18n pages with their routes.

Split out of :mod:`fixture_routes` (T-5431); that module re-exports every name
here, so callers keep importing from ``fixture_routes``.
"""

from __future__ import annotations

import json

from fixture_routes_base import (
    I18N_STRINGS,
    W16_CACHE_ENTRIES,
    W16_COOKIE_MAX_AGE,
    W16_COOKIE_PERSISTENT,
    W16_COOKIE_PERSISTENT_VALUE,
    W16_COOKIE_SESSION,
    W16_COOKIE_SESSION_VALUE,
    W16_IDB_ABORT_ID,
    W16_IDB_INDEX,
    W16_IDB_NAME,
    W16_IDB_RECORDS,
    W16_IDB_STORE,
    W16_LOCAL_KEY,
    W16_LOCAL_VALUE,
    W16_SESSION_KEY,
    W16_SESSION_VALUE,
    W16_SHARED_VERSION,
    W16_SHARED_ZERO_CLIENTS,
    W16_STATE_CACHE,
    W16_SW_ACTIVATED,
    W16_SW_ASSET_BODY,
    W16_SW_ASSET_PATH,
    W16_SW_CACHE,
    W16_SW_INSTALLED,
    W16_SW_OFFLINE_BODY,
    W16_SW_SCOPE,
    W16_SW_UNCACHED_BODY,
    W16_SW_VERSION,
    W16_WORKER_CLOSED,
    W16_WORKER_LATE_ID,
    W16_WORKER_SALT,
    W16_WORKER_VERSION,
    _fill,
    _ncr,
    _page,
    _record,
    _send,
    _send_html,
    _send_json,
    _send_text,
)
from fixture_routes_handlers import _query_value

# ── W16 pages, workers, and the service worker (MQ-155…162) ─────────────────
# Shared by every page and worker below so the Python oracle and the JS twin
# can never drift apart in more than one place.
_W16_HASH_JS = (
    "function w16h(s){let x=0x811c9dc5>>>0;"
    "for(let i=0;i<s.length;i+=1){x^=s.charCodeAt(i);"
    "x=Math.imul(x,0x01000193)>>>0;}"
    "return x.toString(16).padStart(8,'0');}"
)

_DEDICATED_WORKER_JS = """
__HASH__
const CFG = __CFG__;
self.onmessage = (event) => {
  const message = event.data || {};
  if (message.op === 'close') {
    self.postMessage({sentinel: CFG.closed, version: CFG.version});
    self.close();
    return;
  }
  if (message.op === 'echo') {
    self.postMessage({
      id: message.id,
      echo: CFG.version + ':' + message.id,
      hash: w16h(CFG.salt + ':' + message.id)
    });
  }
};
"""

_DEDICATED_HOST_JS = """
const CFG = __CFG__;
window.__w16d = {log: [], errors: [], terminated: false};
const worker = new Worker(CFG.script);
worker.onmessage = (event) => { window.__w16d.log.push(event.data); };
worker.onerror = (event) => {
  window.__w16d.errors.push(String(event.message || event));
};
window.w16Send = (ids) => {
  for (const id of ids) { worker.postMessage({op: 'echo', id: id}); }
  return ids.length;
};
window.w16Close = () => { worker.postMessage({op: 'close'}); return true; };
window.w16Late = () => {
  worker.postMessage({op: 'echo', id: CFG.lateId});
  return true;
};
window.w16Terminate = () => {
  worker.terminate();
  window.__w16d.terminated = true;
  return true;
};
window.w16Log = () => JSON.stringify(window.__w16d);
"""

_SHARED_WORKER_JS = """
const CFG = __CFG__;
let counter = 0;
let clients = 0;
let nextPortId = 1;
self.onconnect = (event) => {
  const port = event.ports[0];
  const portId = nextPortId;
  nextPortId += 1;
  clients += 1;
  port.onmessage = (message) => {
    const data = message.data || {};
    if (data.op === 'tick') {
      counter += 1;
      port.postMessage({portId: portId, counter: counter});
      return;
    }
    if (data.op === 'bye') {
      clients -= 1;
      port.postMessage({portId: portId, bye: true, clients: clients});
      if (clients === 0) {
        fetch(CFG.report + '?sentinel=' + encodeURIComponent(CFG.zero) +
              '&counter=' + counter +
              '&token=' + encodeURIComponent(data.token || ''));
      }
    }
  };
  port.start();
  port.postMessage({hello: true, portId: portId, version: CFG.version});
};
"""

_SHARED_HOST_JS = """
const CFG = __CFG__;
window.__w16s = {log: [], portId: null, version: null};
const shared = new SharedWorker(CFG.script);
shared.port.onmessage = (event) => {
  const data = event.data;
  window.__w16s.log.push(data);
  if (data.hello) {
    window.__w16s.portId = data.portId;
    window.__w16s.version = data.version;
  }
};
shared.port.start();
window.w16Tick = () => { shared.port.postMessage({op: 'tick'}); return true; };
window.w16Bye = (token) => {
  shared.port.postMessage({op: 'bye', token: token});
  return true;
};
window.w16Log = () => JSON.stringify(window.__w16s);
"""

_SERVICE_WORKER_JS = """
const CFG = __CFG__;
self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CFG.cache);
    await cache.addAll([CFG.asset]);
    await fetch(CFG.report + '?phase=install&sentinel=' +
                encodeURIComponent(CFG.installed) + '&version=' + CFG.version);
    await self.skipWaiting();
  })());
});
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    await fetch(CFG.report + '?phase=activate&sentinel=' +
                encodeURIComponent(CFG.activated) + '&version=' + CFG.version);
    await self.clients.claim();
  })());
});
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const mine = url.origin === self.location.origin &&
               url.pathname.startsWith(CFG.scope) &&
               url.pathname !== CFG.report;
  if (!mine) { return; }
  event.respondWith((async () => {
    const cached = await caches.match(event.request);
    if (cached) { return cached; }
    try {
      return await fetch(event.request);
    } catch (error) {
      return new Response(CFG.offline, {
        status: 200,
        headers: {'Content-Type': 'text/plain; charset=utf-8'}
      });
    }
  })());
});
"""

_PWA_APP_JS = """
__HASH__
const CFG = __CFG__;
window.__w16p = {
  state: 'init',
  error: null,
  scope: null,
  controllerAtLoad: navigator.serviceWorker.controller ? 'controlled'
                                                       : 'uncontrolled',
  fetches: {},
  cacheReport: null,
  cleanup: null
};
window.w16Register = () => {
  window.__w16p.state = 'registering';
  navigator.serviceWorker.register(CFG.script, {scope: CFG.scope})
    .then(async (registration) => {
      window.__w16p.scope = registration.scope;
      await navigator.serviceWorker.ready;
      window.__w16p.state = 'ready';
      // F-909. `ready` resolves on an ACTIVE registration and says nothing
      // about THIS document: `clients.claim()` reaches a page as its own task,
      // and this worker awaits a network round trip to the ledger before it
      // calls claim at all. So `ready` is the wrong promise to read
      // `controller` after -- measured, by putting 1500 ms in front of the
      // claim: state `ready`, controller `uncontrolled`, which is exactly the
      // shape a slow CI runner produced. `controllerchange` is the event that
      // says the claim has landed here, and `controlled` is the state that
      // reports it. A worker that never claims leaves the state at `ready`,
      // which the poll's own failure then names.
      if (!navigator.serviceWorker.controller) {
        await new Promise((resolve) => {
          navigator.serviceWorker.addEventListener(
            'controllerchange', resolve, {once: true});
          // Armed AFTER the check above, so check again: a claim landing
          // between the two would otherwise leave this waiting forever for an
          // event that has already fired.
          if (navigator.serviceWorker.controller) { resolve(); }
        });
      }
      window.__w16p.state = 'controlled';
    })
    .catch((error) => {
      window.__w16p.state = 'error';
      window.__w16p.error = String(error);
    });
  return true;
};
window.w16Controller = () =>
  navigator.serviceWorker.controller ? 'controlled' : 'uncontrolled';
window.w16Fetch = (path) => {
  window.__w16p.fetches[path] = null;
  fetch(path)
    .then((response) => response.text().then((body) => {
      window.__w16p.fetches[path] = {status: response.status, body: body};
    }))
    .catch((error) => {
      window.__w16p.fetches[path] = {status: -1, body: String(error)};
    });
  return true;
};
window.w16Fetched = (path) => JSON.stringify(window.__w16p.fetches[path]);
window.w16CacheReport = () => {
  window.__w16p.cacheReport = null;
  (async () => {
    const names = (await caches.keys()).sort();
    const report = {names: names, caches: {}};
    for (const name of names) {
      const cache = await caches.open(name);
      const items = [];
      for (const request of await cache.keys()) {
        const response = await cache.match(request);
        const body = await response.text();
        items.push({
          path: new URL(request.url).pathname,
          status: response.status,
          length: body.length,
          hash: w16h(body)
        });
      }
      items.sort((a, b) => (a.path < b.path ? -1 : 1));
      report.caches[name] = {count: items.length, items: items};
    }
    window.__w16p.cacheReport = report;
  })();
  return true;
};
window.w16Cleanup = () => {
  window.__w16p.cleanup = null;
  (async () => {
    const registrations = await navigator.serviceWorker.getRegistrations();
    for (const registration of registrations) { await registration.unregister(); }
    for (const name of await caches.keys()) { await caches.delete(name); }
    window.__w16p.cleanup = {
      registrations: (await navigator.serviceWorker.getRegistrations()).length,
      caches: (await caches.keys()).length
    };
  })();
  return true;
};
window.w16CleanupReport = () => JSON.stringify(window.__w16p.cleanup);
window.w16Report = () => JSON.stringify({
  state: window.__w16p.state,
  error: window.__w16p.error,
  scope: window.__w16p.scope,
  controllerAtLoad: window.__w16p.controllerAtLoad,
  controller: window.w16Controller(),
  cacheReport: window.__w16p.cacheReport
});
"""

_STATE_STORE_JS = """
__HASH__
const CFG = __CFG__;
window.__w16t = {cache: null, idb: null, storage: null};
window.w16SeedCache = () => {
  window.__w16t.cache = null;
  (async () => {
    const cache = await caches.open(CFG.cacheName);
    for (const entry of CFG.cacheEntries) {
      await cache.put(entry[0], new Response(entry[1], {
        status: 200,
        headers: {'Content-Type': 'text/plain; charset=utf-8'}
      }));
    }
    window.__w16t.cache = {seeded: CFG.cacheEntries.length};
  })();
  return true;
};
window.w16ReadCache = () => {
  window.__w16t.cache = null;
  (async () => {
    const cache = await caches.open(CFG.cacheName);
    const items = [];
    for (const request of await cache.keys()) {
      const response = await cache.match(request);
      const body = await response.text();
      items.push({
        path: new URL(request.url).pathname,
        status: response.status,
        length: body.length,
        hash: w16h(body)
      });
    }
    items.sort((a, b) => (a.path < b.path ? -1 : 1));
    window.__w16t.cache = {
      name: CFG.cacheName,
      count: items.length,
      items: items
    };
  })();
  return true;
};
function w16OpenDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(CFG.dbName, 1);
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore(CFG.storeName,
                                                     {keyPath: 'id'});
      store.createIndex(CFG.indexName, 'group', {unique: false});
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
window.w16SeedIdb = () => {
  window.__w16t.idb = null;
  (async () => {
    const db = await w16OpenDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(CFG.storeName, 'readwrite');
      const store = tx.objectStore(CFG.storeName);
      for (const record of CFG.records) { store.put(record); }
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    let aborted = 'no';
    await new Promise((resolve) => {
      const tx = db.transaction(CFG.storeName, 'readwrite');
      tx.objectStore(CFG.storeName).put({
        id: CFG.abortId, name: 'rolled-back', group: 'g9', payload: 'discarded'
      });
      tx.onabort = () => { aborted = 'yes'; resolve(); };
      tx.oncomplete = () => { aborted = 'committed'; resolve(); };
      tx.abort();
    });
    db.close();
    window.__w16t.idb = {seeded: CFG.records.length, aborted: aborted};
  })();
  return true;
};
window.w16QueryIdb = () => {
  window.__w16t.idb = null;
  (async () => {
    const db = await w16OpenDb();
    const tx = db.transaction(CFG.storeName, 'readonly');
    const store = tx.objectStore(CFG.storeName);
    const index = store.index(CFG.indexName);
    const groups = {};
    for (const group of CFG.groups) {
      groups[group] = await new Promise((resolve, reject) => {
        const request = index.getAll(group);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    }
    const all = await new Promise((resolve, reject) => {
      const request = store.getAll();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const rolledBack = await new Promise((resolve, reject) => {
      const request = store.get(CFG.abortId);
      request.onsuccess = () => resolve(request.result === undefined);
      request.onerror = () => reject(request.error);
    });
    db.close();
    window.__w16t.idb = {
      groups: groups,
      keys: all.map((row) => row.id),
      payloadHash: w16h(all.map((row) => row.payload).join('|')),
      abortedRecordAbsent: rolledBack
    };
  })();
  return true;
};
window.w16SeedStorage = () => {
  localStorage.setItem(CFG.localKey, CFG.localValue);
  sessionStorage.setItem(CFG.sessionKey, CFG.sessionValue);
  document.cookie = CFG.persistentCookie + '=' + CFG.persistentValue +
                    '; path=/; max-age=' + CFG.cookieMaxAge + '; SameSite=Lax';
  document.cookie = CFG.sessionCookie + '=' + CFG.sessionCookieValue +
                    '; path=/; SameSite=Lax';
  return true;
};
window.w16ReadStorage = () => JSON.stringify({
  local: localStorage.getItem(CFG.localKey),
  session: sessionStorage.getItem(CFG.sessionKey),
  cookie: document.cookie,
  localCount: localStorage.length,
  dbs: null
});
window.w16ClearStorage = () => {
  localStorage.clear();
  sessionStorage.clear();
  document.cookie = CFG.persistentCookie + '=; path=/; max-age=0';
  document.cookie = CFG.sessionCookie + '=; path=/; max-age=0';
  return true;
};
window.w16ClearState = () => {
  window.__w16t.cleared = null;
  (async () => {
    for (const name of await caches.keys()) { await caches.delete(name); }
    await new Promise((resolve) => {
      const request = indexedDB.deleteDatabase(CFG.dbName);
      request.onsuccess = resolve;
      request.onerror = resolve;
      request.onblocked = resolve;
    });
    window.__w16t.cleared = {
      caches: (await caches.keys()).length,
      cookie: document.cookie,
      local: localStorage.length,
      session: sessionStorage.length
    };
  })();
  return true;
};
window.w16State = (key) => JSON.stringify(window.__w16t[key]);
"""

# The one code-point reader. Both i18n pages expose it, because comparing
# rendered strings is exactly the mistake MQ-161 exists to avoid and a page
# that lacked the reader would tempt a node into comparing them.
#
# Both i18n page scripts below are deliberately pure ASCII (no em dashes in the
# comments). The served page must carry its strings as numeric character
# references and nothing else, so the browser is what reconstitutes the code
# points; a stray non-ASCII byte anywhere in the markup would make the page
# depend on wire encoding, and the hermetic backstop asserts ``isascii()``.
_W16_POINTS_JS = (
    "window.w16Points = (text) => "
    "JSON.stringify(Array.from(text).map((c) => c.codePointAt(0)));"
)

_I18N_TEXT_JS = """
__POINTS__
window.__w16i = {actions: []};
// Delegated, and bound on DOMContentLoaded: this script is in <head>, so the
// inputs do not exist yet and a querySelectorAll here would silently bind to
// nothing, leaving an action log that is empty for the honest reason and the
// dishonest one alike.
document.addEventListener('DOMContentLoaded', () => {
  document.body.addEventListener('input', (event) => {
    window.__w16i.actions.push(event.target.id + '=' + event.target.value);
  });
});
window.w16Text = (id) => document.getElementById(id).textContent;
window.w16Value = (id) => document.getElementById(id).value;
window.w16Attr = (id) => document.getElementById(id).getAttribute('data-label');
window.w16Dir = (id) => document.getElementById(id).getAttribute('dir');
window.w16Actions = () => JSON.stringify(window.__w16i.actions);
"""

_I18N_COMPOSITION_JS = """
__POINTS__
window.__w16c = {events: []};
const w16Composer = () => document.getElementById('composer');
const kinds = ['keydown', 'keyup', 'compositionstart', 'compositionupdate',
               'compositionend', 'beforeinput', 'input'];
// Bound on DOMContentLoaded - this script is in <head> and #composer does not
// exist yet. Resolving it eagerly would throw and take every w16Comp* helper
// down with it.
document.addEventListener('DOMContentLoaded', () => {
  for (const kind of kinds) {
    w16Composer().addEventListener(kind, (event) => {
      window.__w16c.events.push({
        type: kind,
        data: (event.data === undefined || event.data === null) ? '' : event.data,
        inputType: event.inputType || '',
        isComposing: event.isComposing === true,
        value: w16Composer().value
      });
    });
  }
});
window.w16CompReset = () => {
  window.__w16c.events = [];
  w16Composer().value = '';
  return true;
};
window.w16CompEvents = () =>
  JSON.stringify(window.__w16c.events.map((e) => e.type + ':' + e.data));
window.w16CompDetail = () => JSON.stringify(window.__w16c.events);
window.w16CompValue = () => w16Composer().value;
"""


def dedicated_worker_script() -> str:
    """MQ-155: the versioned dedicated worker. Answers ids in order, then closes."""
    config = {
        "version": W16_WORKER_VERSION,
        "salt": W16_WORKER_SALT,
        "closed": W16_WORKER_CLOSED,
    }
    return _fill(_DEDICATED_WORKER_JS, hash=_W16_HASH_JS, cfg=json.dumps(config))


def dedicated_host_page() -> str:
    config = {"script": "/workers/dedicated.js", "lateId": W16_WORKER_LATE_ID}
    return _page(
        "w16 dedicated worker",
        "fixture-w16-dedicated-host",
        "",
        head="<script>"
        + _fill(_DEDICATED_HOST_JS, cfg=json.dumps(config))
        + "</script>",
    )


def shared_worker_script() -> str:
    """MQ-156: the versioned shared worker.

    The zero-client teardown sentinel is reported to the SERVER, not to a port:
    by the time the last client leaves there is no page left to tell, so a
    page-side assertion would be structurally unable to observe it.
    """
    config = {
        "version": W16_SHARED_VERSION,
        "zero": W16_SHARED_ZERO_CLIENTS,
        "report": "/workers/shared-report",
    }
    return _fill(_SHARED_WORKER_JS, cfg=json.dumps(config))


def shared_host_page() -> str:
    config = {"script": "/workers/shared.js"}
    return _page(
        "w16 shared worker",
        "fixture-w16-shared-host",
        "",
        head="<script>" + _fill(_SHARED_HOST_JS, cfg=json.dumps(config)) + "</script>",
    )


def service_worker_script() -> str:
    """MQ-157/158: the PWA service worker. Cache-first inside its own scope."""
    config = {
        "version": W16_SW_VERSION,
        "scope": W16_SW_SCOPE,
        "cache": W16_SW_CACHE,
        "asset": W16_SW_ASSET_PATH,
        "installed": W16_SW_INSTALLED,
        "activated": W16_SW_ACTIVATED,
        "offline": W16_SW_OFFLINE_BODY,
        "report": "/pwa/report",
    }
    return _fill(_SERVICE_WORKER_JS, cfg=json.dumps(config))


def pwa_app_page() -> str:
    config = {"script": "/pwa/sw.js", "scope": W16_SW_SCOPE}
    return _page(
        "w16 pwa",
        "fixture-w16-pwa-app",
        "",
        head="<script>"
        + _fill(_PWA_APP_JS, hash=_W16_HASH_JS, cfg=json.dumps(config))
        + "</script>",
    )


def state_store_page() -> str:
    """MQ-158/159/160: separately seeded CacheStorage, IndexedDB, and storage."""
    config = {
        "cacheName": W16_STATE_CACHE,
        "cacheEntries": [list(entry) for entry in W16_CACHE_ENTRIES],
        "dbName": W16_IDB_NAME,
        "storeName": W16_IDB_STORE,
        "indexName": W16_IDB_INDEX,
        "records": [
            {"id": rid, "name": name, "group": group, "payload": payload}
            for rid, name, group, payload in W16_IDB_RECORDS
        ],
        "groups": sorted({record[2] for record in W16_IDB_RECORDS}),
        "abortId": W16_IDB_ABORT_ID,
        "localKey": W16_LOCAL_KEY,
        "localValue": W16_LOCAL_VALUE,
        "sessionKey": W16_SESSION_KEY,
        "sessionValue": W16_SESSION_VALUE,
        "persistentCookie": W16_COOKIE_PERSISTENT,
        "persistentValue": W16_COOKIE_PERSISTENT_VALUE,
        "sessionCookie": W16_COOKIE_SESSION,
        "sessionCookieValue": W16_COOKIE_SESSION_VALUE,
        "cookieMaxAge": W16_COOKIE_MAX_AGE,
    }
    return _page(
        "w16 state",
        "fixture-w16-state-store",
        "",
        head="<script>"
        + _fill(_STATE_STORE_JS, hash=_W16_HASH_JS, cfg=json.dumps(config))
        + "</script>",
    )


def i18n_text_page() -> str:
    """MQ-161: every fixed string as a text node, an attribute, and an input."""
    rows = []
    for key, value in I18N_STRINGS.items():
        reference = _ncr(value)
        rows.append(
            f"<p id='text-{key}' dir='auto' data-label='{reference}'>"
            f"{reference}</p>"
            f"<input id='input-{key}' dir='auto' value=''>"
        )
    return _page(
        "w16 i18n",
        "fixture-w16-i18n-text",
        "".join(rows),
        head="<script>" + _fill(_I18N_TEXT_JS, points=_W16_POINTS_JS) + "</script>",
    )


def i18n_composition_page() -> str:
    """MQ-162: records the DOM composition/input sequence, synthesizes nothing."""
    return _page(
        "w16 composition",
        "fixture-w16-i18n-composition",
        "<input id='composer' value=''>",
        head="<script>"
        + _fill(_I18N_COMPOSITION_JS, points=_W16_POINTS_JS)
        + "</script>",
    )


# ── W16 route implementations ───────────────────────────────────────────────
def _send_js(handler, source: str) -> None:
    """Serve worker source. ``Service-Worker-Allowed`` widens the SW scope so a
    script under ``/pwa/`` could claim ``/`` — declared once here rather than
    left to a default a browser upgrade could change."""
    _send(
        handler,
        200,
        [
            ("Content-Type", "text/javascript; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Service-Worker-Allowed", "/"),
        ],
        source.encode("utf-8"),
    )


def _r_dedicated_worker(handler, query: str) -> None:
    _send_js(handler, dedicated_worker_script())


def _r_dedicated_host(handler, query: str) -> None:
    _send_html(handler, dedicated_host_page())


def _r_shared_worker(handler, query: str) -> None:
    _send_js(handler, shared_worker_script())


def _r_shared_host(handler, query: str) -> None:
    _send_html(handler, shared_host_page())


def _r_shared_report(handler, query: str) -> None:
    """The shared worker's zero-client teardown report.

    A tokenless request records nothing and still answers 200 — the hermetic
    enumeration backstop walks every route with no query, and a route that
    404s or writes a phantom entry there would corrupt the oracle it exists
    to protect.
    """
    sentinel = _query_value(query, "sentinel")
    if sentinel:
        _record(
            handler,
            "w16_shared",
            {
                "sentinel": sentinel,
                "counter": int(_query_value(query, "counter") or 0),
                "token": _query_value(query, "token"),
            },
        )
    _send_json(handler, {"recorded": bool(sentinel)})


def _r_sw_script(handler, query: str) -> None:
    _send_js(handler, service_worker_script())


def _r_pwa_app(handler, query: str) -> None:
    _send_html(handler, pwa_app_page())


def _r_pwa_report(handler, query: str) -> None:
    phase = _query_value(query, "phase")
    if phase:
        _record(
            handler,
            "w16_sw",
            {
                "phase": phase,
                "sentinel": _query_value(query, "sentinel"),
                "version": _query_value(query, "version"),
            },
        )
    _send_json(handler, {"recorded": bool(phase)})


def _r_pwa_asset(handler, query: str) -> None:
    """The one asset the service worker caches. Every NETWORK read lands here,
    so a cached read is proved by this ledger entry NOT appearing."""
    _record(handler, "w16_asset", W16_SW_ASSET_PATH)
    _send_text(handler, W16_SW_ASSET_BODY)


def _r_pwa_uncached(handler, query: str) -> None:
    _send_text(handler, W16_SW_UNCACHED_BODY)


def _r_i18n_text(handler, query: str) -> None:
    _send_html(handler, i18n_text_page())


def _r_i18n_composition(handler, query: str) -> None:
    _send_html(handler, i18n_composition_page())


def _r_state_store(handler, query: str) -> None:
    _send_html(handler, state_store_page())
