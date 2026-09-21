"""F-898's gating MEASUREMENT: what a seed from a RUNNING source actually carries.

The design study (`design_session_ux.md` §3.C) offers two mechanisms for seeding
a new browser session from a source whose browser is still up, and gates the
sentence "this carries your login" on a matrix rather than on reasoning:

* **C1** — a file copy of the live profile directory. This is not a proposal: it
  is what `clone_storage._copy_profile_tree` already does on every clone, with a
  double delta pass 0.2 s apart and locked files skipped with a WARNING.
* **C2** — a CDP hand-off, `Storage.getCookies` on the running source into
  `Storage.setCookies` on the target. Browser-wide, plaintext, and cookies only.

Three cells and a control, over five storage mechanisms, on two cross-site
loopback origins. Every origin, cookie and token here is SYNTHETIC; nothing in
this file reaches the network, and no real credential exists to leak.

**Why the cookie oracle is the SERVER and not `document.cookie`.** Three of the
four cookies are `HttpOnly`, which is the shape a real session cookie has — the
page cannot see them, so a JS-side check would report "not carried" for the one
class that matters most. The fixture server records the `Cookie` header of every
request it serves into a ledger, so what is asserted is what Chrome actually
SENT. `document.cookie` is still read, for the contrast.

**Why two IP literals and not `localhost`.** CHIPS (`Partitioned`) is only
observable when the cookie is set in a cross-SITE embed, and a site is
scheme+host, not port — so two ports on `127.0.0.1` are one site. `127.0.0.1`
and `[::1]` are different sites, are both "potentially trustworthy" (so `Secure`
and service workers are permitted without TLS), and neither costs a name
resolution: measured, `http://localhost:<port>` against a v4-only listener spent
~2 s per request on this machine walking `::1` first.

What each cell measures is recorded in
`audit/stage2/finding_F898_seed_from_running_source.md`. The assertions here are
the STABLE half of what was measured over three consecutive local runs; anything
that varied is reported in the finding and deliberately not asserted.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.server
import json
import socket
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import clone_storage, tool_runtime

pytestmark = integration_pytestmark()

# ── The synthetic state. Names are asserted on; the values never leave this
# process and are constants, not secrets — but they are still never logged or
# printed, on the same discipline the finding's PII rule states. ──
TOKEN = "f898-synthetic-token"

COOKIE_SESSION = "f898_session"  # no Expires/Max-Age — dies with the browser
COOKIE_PERSIST = "f898_persist"  # Max-Age, so it is written to the jar on disk
COOKIE_HTTPONLY = "f898_httponly"  # HttpOnly; Secure; SameSite=None
COOKIE_CHIPS = "f898_chips"  # the same, plus Partitioned — set on site B
COOKIE_B_PLAIN = "f898_b_plain"  # site B, SameSite=None; Secure, NOT partitioned

SITE_A_COOKIES = (COOKIE_SESSION, COOKIE_PERSIST, COOKIE_HTTPONLY)
SITE_B_COOKIES = (COOKIE_CHIPS, COOKIE_B_PLAIN)
#: The site-A cookies a FILE copy can carry at all: a session cookie has no
#: expiry, so Chrome never writes it to the jar on disk and no copy of that
#: file can contain it. MEASURED, and the reason `COOKIE_SESSION` is in the
#: matrix at all — it is the one mechanism where C1-stopped and C2 disagree.
PERSISTENT_SITE_A_COOKIES = (COOKIE_PERSIST, COOKIE_HTTPONLY)

# A profile write lands in LevelDB/SQLite buffers before it lands on disk. C1
# copies bytes, so it can only carry what has been flushed — this is the grace
# the measurement GIVES the file-copy mechanism, so a miss is a real miss and
# not a race the test created. One second is ~5x the product's own 0.2 s
# inter-pass gap.
FLUSH_SETTLE_SECONDS = 1.0

# A whole cell (two Chromes, a profile copy, four navigations) on a loaded
# machine. The nodes are bounded so a wedged browser names itself rather than
# running into the suite timeout.
CELL_DEADLINE = 300.0


# ---------------------------------------------------------------------------
# The fixture origins
# ---------------------------------------------------------------------------


@dataclass
class _Origin:
    """One loopback site: its base URL and the ledger of what it was sent."""

    label: str
    base_url: str
    hits: list[dict]

    @property
    def site(self) -> str:
        """The schemeful SITE, which is the base URL without its port.

        MEASURED — this is not a definition copied from a spec: Chrome reports
        the partition key of a cookie set here as `http://127.0.0.1`, with no
        port, which is the direct evidence that two ephemeral ports on one
        loopback host would have been ONE site and could not have partitioned
        anything. It is why this file binds two IP literals.
        """
        return self.base_url.rpartition(":")[0]


_SW_JS = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {});
"""

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>f898</title><body>f898 {label}</body>"
)

_TOP = (
    "<!doctype html><meta charset=utf-8><title>f898 top</title>"
    '<body><iframe id=f src="{peer}/embed"></iframe></body>'
)


class _Handler(http.server.BaseHTTPRequestHandler):
    """The one handler both origins run. Every route is a measurement surface.

    ``/seed`` and ``/embed`` are the only routes that SET anything; everything
    else exists so the test can read back what Chrome sent, which is the oracle
    three of the four cookies have no other way to be observed through.
    """

    protocol_version = "HTTP/1.1"

    def __init__(self, *args, origin: _Origin, peer, **kwargs):
        # ``peer`` is a zero-argument callable and not a string: neither origin
        # knows the other's URL until both sockets are bound, and the handler
        # class is built before either is.
        self.origin = origin
        self._peer = peer
        super().__init__(*args, **kwargs)

    def log_message(self, *args, **kwargs):
        """Silence per-request stderr logging."""

    # -- helpers --------------------------------------------------------
    def _cookie_names(self) -> list[str]:
        raw = self.headers.get("Cookie") or ""
        names = [
            part.split("=", 1)[0].strip() for part in raw.split(";") if part.strip()
        ]
        return sorted(name for name in names if name)

    def _record(self) -> list[str]:
        names = self._cookie_names()
        self.origin.hits.append({"path": self.path, "cookies": names})
        return names

    def _send(self, body: bytes, content_type: str, cookies: tuple[str, ...] = ()):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, cookies: tuple[str, ...] = ()):
        self._send(text.encode("utf-8"), "text/html; charset=utf-8", cookies)

    def _json(self, payload):
        self._send(json.dumps(payload).encode("utf-8"), "application/json")

    # -- routes ---------------------------------------------------------
    def do_GET(self):  # noqa: N802  stdlib override, PERMANENT(interface)
        self._record()
        path = self.path.split("?", 1)[0]

        if path == "/ledger":
            self._json(self.origin.hits)
            return
        if path == "/reset":
            self.origin.hits.clear()
            self._json({"reset": True})
            return
        if path == "/sw.js":
            self._send(_SW_JS.encode("utf-8"), "text/javascript; charset=utf-8")
            return
        if path == "/seed":
            # Three shapes on one page: a session cookie (no expiry at all), a
            # persistent one, and the HttpOnly+Secure+SameSite=None shape a real
            # SSO session cookie has.
            self._html(
                _PAGE.format(label=self.origin.label),
                cookies=(
                    f"{COOKIE_SESSION}={TOKEN}; Path=/",
                    f"{COOKIE_PERSIST}={TOKEN}; Path=/; Max-Age=86400",
                    f"{COOKIE_HTTPONLY}={TOKEN}; Path=/; Secure; HttpOnly; "
                    f"SameSite=None; Max-Age=86400",
                ),
            )
            return
        if path == "/embed":
            # Site B, inside a cross-site iframe: the only context in which a
            # `Partitioned` cookie is partitioned at all.
            self._html(
                _PAGE.format(label=self.origin.label),
                cookies=(
                    f"{COOKIE_CHIPS}={TOKEN}; Path=/; Secure; HttpOnly; "
                    f"SameSite=None; Max-Age=86400; Partitioned",
                    f"{COOKIE_B_PLAIN}={TOKEN}; Path=/; Secure; HttpOnly; "
                    f"SameSite=None; Max-Age=86400",
                ),
            )
            return
        if path == "/top":
            self._html(_TOP.format(peer=self._peer()))
            return
        self._html(_PAGE.format(label=self.origin.label))


class _V6Server(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True
    allow_reuse_address = True


class _V4Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextlib.contextmanager
def _serve_cross_site_pair():
    """Two CROSS-SITE loopback origins: ``127.0.0.1`` and ``[::1]``.

    Both bind before either serves, because ``/top`` on A has to name B's URL
    and neither knows its own port until its socket exists — the same ordering
    ``release_gate_harness.serve_fixture_origin_pair`` uses, for the same
    reason. That harness is deliberately NOT reused: its pair is two PORTS on
    one host, which is one site, and a same-site pair cannot partition a
    cookie.
    """
    origin_a = _Origin("a", "", [])
    origin_b = _Origin("b", "", [])
    urls: dict[str, str] = {}

    server_a = _V4Server(
        ("127.0.0.1", 0),
        functools.partial(_Handler, origin=origin_a, peer=lambda: urls["b"]),
    )
    server_b = _V6Server(
        ("::1", 0),
        functools.partial(_Handler, origin=origin_b, peer=lambda: urls["a"]),
    )
    urls["a"] = origin_a.base_url = f"http://127.0.0.1:{server_a.server_address[1]}"
    urls["b"] = origin_b.base_url = f"http://[::1]:{server_b.server_address[1]}"

    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (server_a, server_b)
    ]
    for thread in threads:
        thread.start()
    try:
        yield origin_a, origin_b
    finally:
        for server in (server_a, server_b):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


@pytest.fixture()
def origins():
    with _serve_cross_site_pair() as pair:
        yield pair


async def _ledger(origin: _Origin) -> list[dict]:
    """The server's own record, fetched out-of-band so the browser is not asked.

    Off-thread: a blocking urlopen on the loop driving Chrome would stall every
    CDP reply behind it.
    """

    def _fetch():
        with urllib.request.urlopen(  # noqa: S310  FALSE-POSITIVE(a literal http:// loopback fixture URL this file bound itself)
            f"{origin.base_url}/ledger", timeout=10
        ) as response:
            return json.loads(response.read())

    return await asyncio.to_thread(_fetch)


def _cookies_seen(ledger: list[dict], path: str) -> set[str]:
    """Every cookie name Chrome sent to *path*, over all of this run's hits."""
    return {
        name
        for hit in ledger
        if hit["path"].startswith(path)
        for name in hit["cookies"]
    }


# ---------------------------------------------------------------------------
# Browser-side mechanism
# ---------------------------------------------------------------------------

#: The token is substituted rather than interpolated: every one of these
#: scripts is a JS object literal farm, so a format string would have to escape
#: its own braces and a reader could no longer tell the JS from the escaping.
_TOKEN_MARK = "__F898_TOKEN__"

_WRITE_JS = """
(async () => {
  const T = '__F898_TOKEN__';
  localStorage.setItem('f898_ls', T);
  sessionStorage.setItem('f898_ss', T);
  await new Promise((res, rej) => {
    const rq = indexedDB.open('f898db', 1);
    rq.onupgradeneeded = () => rq.result.createObjectStore('kv');
    rq.onerror = () => rej(new Error('idb open'));
    rq.onsuccess = () => {
      const db = rq.result;
      const tx = db.transaction('kv', 'readwrite');
      tx.objectStore('kv').put(T, 't');
      tx.oncomplete = () => { db.close(); res(); };
      tx.onerror = () => { db.close(); rej(new Error('idb put')); };
    };
  });
  const cache = await caches.open('f898');
  await cache.put('/cached-token', new Response(T));
  const reg = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
  await Promise.race([
    navigator.serviceWorker.ready,
    new Promise(res => setTimeout(res, 5000)),
  ]);
  return JSON.stringify({sw_scope: reg.scope});
})()
""".replace(_TOKEN_MARK, TOKEN)

_READ_JS = """
(async () => {
  const out = {};
  out.local = localStorage.getItem('f898_ls');
  out.session = sessionStorage.getItem('f898_ss');
  out.idb = await new Promise(res => {
    const rq = indexedDB.open('f898db');
    rq.onupgradeneeded = () => { try { rq.result.createObjectStore('kv'); } catch (e) {} };
    rq.onerror = () => res(null);
    rq.onsuccess = () => {
      const db = rq.result;
      if (!db.objectStoreNames.contains('kv')) { db.close(); res(null); return; }
      const g = db.transaction('kv', 'readonly').objectStore('kv').get('t');
      g.onsuccess = () => { db.close(); res(g.result === undefined ? null : g.result); };
      g.onerror = () => { db.close(); res(null); };
    };
  });
  try {
    out.cache = (await caches.has('f898'))
      ? await (await (await caches.open('f898')).match('/cached-token'))?.text() ?? null
      : null;
  } catch (e) { out.cache = null; }
  try {
    out.service_workers = (await navigator.serviceWorker.getRegistrations()).length;
  } catch (e) { out.service_workers = -1; }
  out.document_cookies = document.cookie
    ? document.cookie.split('; ').map(s => s.split('=')[0]).sort()
    : [];
  return JSON.stringify(out);
})()
"""


async def _read_storage(instance_id: str) -> dict:
    return json.loads(await eval_js(instance_id, _READ_JS))


async def _spawn(profile: Path) -> str:
    """Spawn headless on *profile*, through the product's own spawn path."""
    result = await get_fn("spawn_browser")(
        headless=True, user_data_dir=str(profile), **sandbox_kwargs()
    )
    return result["instance_id"]


async def _close(instance_id: str | None) -> None:
    if instance_id is None:
        return
    with contextlib.suppress(Exception):
        await get_fn("close_instance")(instance_id=instance_id)


def _browsers_on(profile: Path) -> bool:
    target = str(profile).lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        with contextlib.suppress(Exception):
            if "chrome" not in (proc.info["name"] or "").lower():
                continue
            if any(target in (arg or "").lower() for arg in proc.info["cmdline"] or ()):
                return True
    return False


async def _released(profile: Path, *, budget: float = 30.0) -> None:
    """Wait for a closed instance's Chrome to actually EXIT.

    ``close_instance`` offloads its teardown, so it returns while the process
    tree is still dying — and the stopped-source control copies the profile the
    instant it returns. A copy taken while Chrome is still flushing measures
    neither cell. Bounded and silent on expiry, exactly as
    ``test_e2e_persistent_profile_reattach._released`` is.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline and _browsers_on(profile):
        await asyncio.sleep(0.2)


async def _seed_source(instance_id: str, origin_a: _Origin, origin_b: _Origin) -> dict:
    """Write every mechanism into the running source, and prove each landed.

    The returned row is the SOURCE's own line of the matrix, and every cell
    asserts it before it seeds anything: a cell that copied an EMPTY source
    would pass every "not carried" assertion for the wrong reason, which is the
    one way this measurement could be confidently wrong.
    """
    await navigate_and_settle(instance_id, f"{origin_a.base_url}/seed")
    written = json.loads(await eval_js(instance_id, _WRITE_JS))
    assert written["sw_scope"].startswith(origin_a.base_url)

    # The cross-site embed: site B's Partitioned cookie is only partitioned
    # under a top-level site that is not B.
    await navigate_and_settle(instance_id, f"{origin_a.base_url}/top")

    source_row = await _visit_and_read(instance_id, origin_a, origin_b)
    storage = source_row["storage"]
    assert storage["local"] == TOKEN
    assert storage["idb"] == TOKEN
    assert storage["cache"] == TOKEN
    assert storage["service_workers"] == 1
    assert set(SITE_A_COOKIES) <= source_row["site_a_cookies"], source_row
    assert set(SITE_B_COOKIES) <= source_row["site_b_cookies"], source_row

    await asyncio.sleep(FLUSH_SETTLE_SECONDS)
    return source_row


async def _visit_and_read(
    instance_id: str, origin_a: _Origin, origin_b: _Origin
) -> dict:
    """Load both origins in the seeded browser and report everything visible."""

    def _reset(origin):
        with urllib.request.urlopen(  # noqa: S310  FALSE-POSITIVE(a literal http:// loopback fixture URL this file bound itself)
            f"{origin.base_url}/reset", timeout=10
        ) as response:
            response.read()

    for origin in (origin_a, origin_b):
        await asyncio.to_thread(_reset, origin)

    # Top level on A (so A's own cookies ride), then A embedding B (so B's
    # cookies ride in the partition they were set in).
    await navigate_and_settle(instance_id, f"{origin_a.base_url}/blank")
    storage = await _read_storage(instance_id)
    await navigate_and_settle(instance_id, f"{origin_a.base_url}/top")
    await asyncio.sleep(0.5)  # the iframe is fetched after the top document lands

    ledger_a = await _ledger(origin_a)
    ledger_b = await _ledger(origin_b)
    return {
        "storage": storage,
        "site_a_cookies": _cookies_seen(ledger_a, "/blank")
        | _cookies_seen(ledger_a, "/top"),
        "site_b_cookies": _cookies_seen(ledger_b, "/embed"),
    }


# ---------------------------------------------------------------------------
# C1 — the file copy the product already performs
# ---------------------------------------------------------------------------


def _copy_profile(
    source: Path, target: Path, root: Path
) -> tuple[str | None, list[str]]:
    """Run THE product's copy and capture what it could not read.

    The skipped-file WARNING is the only thing `_copy_profile_file` says about a
    share-mode refusal, and it goes to the debug logger and nowhere else — so
    the spy is how this measurement answers "which files were locked". Nothing
    is patched into the product: the real function runs, and the real warning is
    observed on its way out.
    """
    skipped: list[str] = []
    real = clone_storage.debug_logger.log_warning

    def _spy(component, action, message, *args, **kwargs):
        if action == "copy_skip":
            skipped.append(message)
        return real(component, action, message, *args, **kwargs)

    with patch.object(clone_storage.debug_logger, "log_warning", _spy):
        refusal = clone_storage._copy_profile_tree(source, target, root, "f898-measure")
    return refusal, skipped


#: Chrome >= 96's cookie jar, profile-relative. The same file
#: `profile_seed.LOGIN_WITNESSES` leads with, named here as the BASENAME only —
#: the witness list has exactly one home (F-892) and a second spelling of the
#: whole path is what that pin exists to prevent.
COOKIE_JAR = "Cookies"


def _locked_names(skipped: list[str]) -> set[str]:
    """The file NAMES a copy could not read, out of its warning text.

    Names and not paths: the warning carries an absolute path, which on a real
    profile names the operating user, and the matrix only ever needs to say
    WHICH file was locked.
    """
    names = set()
    for message in skipped:
        # `Skipping locked profile file <path>: <OSError>`. Split on the LAST
        # ": " and not the first — a Windows path begins with one.
        path = message.partition(" file ")[2].rpartition(": ")[0]
        if path:
            names.add(Path(path).name)
    return names


# ---------------------------------------------------------------------------
# C2 — the CDP cookie hand-off
# ---------------------------------------------------------------------------


class _RawCdp:
    """A second, RAW websocket to a running browser's DevTools endpoint.

    Deliberately not `nodriver`'s connection, and the reason is a live defect
    rather than a preference. `nodriver` 0.47's generated
    `network.Cookie.from_json` reads `json["sameParty"]` unconditionally, and
    **Chrome 153.0.8010.50 no longer sends that field** — so every CDP result
    carrying a cookie raises `KeyError` inside `Transaction.__call__`, which
    `Connection._listener` does NOT guard on the result path (it guards the
    EVENT path, two lines down). The listener task ends, and every later call on
    that connection hangs forever. MEASURED end to end through this product's
    own tools; written up as its own finding, because it is not F-898's to fix
    and it reaches far past cookies — see
    `finding_F902_cookie_result_kills_the_cdp_listener.md`.

    A measurement may not be conducted through a door it has just proved
    broken, so C2 is driven over a socket this test owns: Chrome's own JSON in,
    Chrome's own JSON out, no generated parser in between. That also makes the
    fidelity table stronger — every field below is the wire's, not a
    dataclass's idea of the wire.
    """

    def __init__(self, websocket):
        self._websocket = websocket
        self._next_id = 0

    async def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message_id = self._next_id
        await self._websocket.send(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(await self._websocket.recv())
            if message.get("id") != message_id:
                continue  # an event, or another client's reply
            if "error" in message:
                raise AssertionError(f"{method} -> {message['error']}")
            return message.get("result", {})


@contextlib.asynccontextmanager
async def _raw_cdp(instance_id: str):
    """Open a raw browser-level CDP socket beside the product's own."""
    import websockets

    browser = await tool_runtime.browser_manager.get_browser(instance_id)
    assert browser is not None, f"no browser for {instance_id}"
    url = browser.info.webSocketDebuggerUrl
    async with websockets.connect(url, max_size=None) as websocket:
        yield _RawCdp(websocket)


# Every `Network.Cookie` field that is also a `Network.CookieParam` field, so
# the hand-off is a rename-free pass-through and nothing is typed by hand.
_CARRIED_FIELDS = (
    "name",
    "value",
    "domain",
    "path",
    "secure",
    "httpOnly",
    "sameSite",
    "priority",
    "sourceScheme",
    "sourcePort",
    "partitionKey",
)


def _as_param(cookie: dict) -> dict:
    """One `Network.Cookie` as the `Network.CookieParam` C2 would send back.

    Verbatim over `_CARRIED_FIELDS`, plus `expires` ONLY when it is a real time:
    CDP reports a session cookie's expiry as `-1`, which is a marker and not a
    date, and forwarding it is a claim about 1969. Omitting it is what makes the
    target treat the cookie as a session cookie too. `size`, `session` and
    `partitionKeyOpaque` are read-only report fields with no `CookieParam`
    counterpart and are dropped.
    """
    param = {
        field: cookie[field]
        for field in _CARRIED_FIELDS
        if cookie.get(field) is not None
    }
    if cookie.get("expires") is not None and cookie["expires"] > 0:
        param["expires"] = cookie["expires"]
    return param


async def _browser_cookies(instance_id: str) -> list[dict]:
    """Every cookie in the browser, the way C2 would read them."""
    async with _raw_cdp(instance_id) as cdp:
        return (await cdp.call("Storage.getCookies"))["cookies"]


async def _hand_off_cookies(
    source_id: str, target_id: str
) -> tuple[list[dict], list[dict], list[str]]:
    """C2 itself: read the running source's jar, write it into the target.

    Each cookie is also offered ALONE after the batch, so a cookie the batch
    dropped silently is named rather than merely missing — `Storage.setCookies`
    answers with no per-cookie verdict at all.
    """
    source_cookies = await _browser_cookies(source_id)
    params = [_as_param(cookie) for cookie in source_cookies]
    refused: list[str] = []
    async with _raw_cdp(target_id) as cdp:
        await cdp.call("Storage.setCookies", {"cookies": params})
        landed = {
            row["name"] for row in (await cdp.call("Storage.getCookies"))["cookies"]
        }
        for param in params:
            if param["name"] in landed:
                continue
            try:
                await cdp.call("Storage.setCookies", {"cookies": [param]})
            except AssertionError as exc:
                refused.append(f"{param['name']}: {exc}")
                continue
            after = {
                row["name"] for row in (await cdp.call("Storage.getCookies"))["cookies"]
            }
            refused.append(
                f"{param['name']}: "
                + ("accepted alone" if param["name"] in after else "dropped, no error")
            )
    return source_cookies, await _browser_cookies(target_id), refused


def _by_name(cookies: list[dict]) -> dict[str, dict]:
    return {cookie["name"]: cookie for cookie in cookies}


def _reported_names(names) -> list[str]:
    """This test's OWN cookie names, plus a COUNT of everything else.

    Not tidiness — evidence. `Storage.getCookies` is browser-wide, and MEASURED
    on run 1 of 3 a freshly created profile had already acquired a Google `NID`
    cookie from Chrome's own startup fetches, which C2 then carried into the
    target along with the five synthetic ones. On a real profile that class is
    every site the human is logged into, so a report which printed it verbatim
    would be doing exactly what the finding's PII rule forbids the product from
    doing. Names beginning `f898_` are this file's and are safe to print
    because this file wrote them.
    """
    mine = sorted(name for name in names if name.startswith("f898_"))
    others = sum(1 for name in names if not name.startswith("f898_"))
    return [*mine, f"<{others} non-synthetic>"] if others else mine


def _fidelity(before: list[dict], after: list[dict]) -> dict[str, dict]:
    """Per cookie, the carried fields that did NOT survive the hand-off.

    An empty dict per name is a clean round trip. A cookie's VALUE is never
    compared into the report — only field names and the flags/ports/scheme,
    none of which is the secret. `session` is included because it is the one
    derived field a caller would notice: a persistent cookie arriving as a
    session cookie is a login that ends when the window does.
    """
    arrived = _by_name(after)
    drift: dict[str, dict] = {}
    for name, cookie in _by_name(before).items():
        target = arrived.get(name)
        if target is None:
            drift[name] = {"": "MISSING"}
            continue
        drift[name] = {
            field: [cookie.get(field), target.get(field)]
            for field in (*_CARRIED_FIELDS, "session")
            if field != "value" and cookie.get(field) != target.get(field)
        }
    return drift


# ---------------------------------------------------------------------------
# The cells
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(autouse=True)
def _record_in_tmp(tmp_path):
    """No node here may write the developer's live ``browser_pids.json``.

    Every spawn below records a PERSISTENT entry (an explicit ``user_data_dir``
    is `uses_custom_data_dir=True, auto_clone=False`), which since F-888 is
    precisely what the next real backend on this machine adopts and spares from
    its orphan sweep. Autouse, on ``test_e2e_persistent_profile_reattach``'s
    precedent and for its reason.
    """
    with patch.object(
        tool_runtime.process_cleanup, "pid_file", tmp_path / "browser_pids.json"
    ):
        yield


def _report(cell: str, payload: dict) -> None:
    """One line per cell, so a CI log can be diffed against the finding.

    Never a cookie VALUE, and never a cookie NAME this file did not itself
    write: every key whose name mentions cookies goes through
    `_reported_names`, so the line obeys the same rule the finding asks the
    PRODUCT to obey. That discipline is `page_storage`'s, `text_entry`'s and
    `control_state`'s, applied to the one report that would otherwise be the
    exception.
    """
    redacted = {
        key: (_reported_names(value) if "cookie" in key else value)
        for key, value in payload.items()
    }
    if isinstance(redacted.get("storage"), dict):
        storage = dict(redacted["storage"])
        storage["document_cookies"] = _reported_names(storage["document_cookies"])
        # The token is this file's own constant, but printing it teaches the
        # habit of printing a stored value. Shape only.
        redacted["storage"] = {
            name: (f"<{len(value)} chars>" if isinstance(value, str) else value)
            for name, value in storage.items()
        }
    print(f"F898 {cell} {json.dumps(redacted, default=sorted, sort_keys=True)}")


async def test_c1_file_copy_of_a_running_source(origins, tmp_empty_root, tmp_path):
    """C1-live: copy the live profile directory; see what arrives.

    What is asserted here is the measured, three-run-stable answer: the file
    copy carries **nothing a login is made of** while the source is up, because
    the two stores that matter are open files Windows will not let it read.
    """
    origin_a, origin_b = origins
    source = tmp_path / "source"
    target = tmp_path / "c1-live"
    source_id = target_id = None
    try:
        source_id = await _spawn(source)
        source_row = await asyncio.wait_for(
            _seed_source(source_id, origin_a, origin_b), timeout=CELL_DEADLINE
        )
        _report("source", source_row)

        refusal, skipped = await asyncio.to_thread(
            _copy_profile, source, target, tmp_path
        )
        assert refusal is None, f"the copy refused: {refusal}"

        target_id = await _spawn(target)
        seen = await asyncio.wait_for(
            _visit_and_read(target_id, origin_a, origin_b), timeout=CELL_DEADLINE
        )
        _report("C1-live", seen | {"locked_files": _locked_names(skipped)})

        # THE finding: not one cookie crosses. `Default/Network/Cookies` is the
        # SQLite jar and Chrome holds it open, so `_copy_profile_file` skips it
        # (WinError 32) on every one of its three attempts, twice over.
        assert seen["site_a_cookies"] == set()
        assert seen["site_b_cookies"] == set()
        assert COOKIE_JAR in _locked_names(skipped), skipped

        # The positive control: `IndexedDB` IS carried, 7 runs of 7. Without it
        # this node would pass identically if the copy had silently done
        # nothing at all, and "C1 carries no login" would be a claim about the
        # test rather than about C1. It crosses because an IndexedDB
        # transaction commits its LevelDB log at `oncomplete`, which the seed
        # awaits.
        assert seen["storage"]["idb"] == TOKEN

        # `localStorage` is DELIBERATELY NOT ASSERTED. It is the one unstable
        # cell in the whole matrix: MEASURED absent in 6 of 7 local runs and
        # present in 1. REASONED cause — Blink commits `localStorage` to its
        # LevelDB on a delay rather than at the `setItem`, so whether the bytes
        # are on disk when the copy walks past depends on how long the rest of
        # the seed took. A test that asserted either answer would be asserting
        # a race. What the FINDING takes from it is the only safe reading: C1
        # from a live source may or may not carry `localStorage`, and it cannot
        # say which — which is itself disqualifying for a seed a user is told
        # carries their login.
        assert seen["storage"]["session"] is None

        # Cache Storage and the service worker registration are not a race at
        # all — `Service Worker/` is in `_REGENERABLE_PROFILE_NAMES`, so the
        # copier is instructed never to carry them.
        assert seen["storage"]["cache"] is None
        assert seen["storage"]["service_workers"] == 0
    finally:
        await _close(target_id)
        await _close(source_id)
        await _released(source)
        await _released(target)


async def test_c2_cdp_cookie_handoff_from_a_running_source(
    origins, tmp_empty_root, tmp_path
):
    """C2-live: `Storage.getCookies` on the live source into a fresh profile.

    Both halves of F-898's question are here: WHAT arrives (the matrix row) and
    HOW FAITHFULLY (the field-by-field comparison), because "the cookie is
    there" and "the cookie is the same cookie" are different claims and only the
    second one licenses the sentence "this carries your login".
    """
    origin_a, origin_b = origins
    source = tmp_path / "source"
    target = tmp_path / "c2-live"
    source_id = target_id = None
    try:
        source_id = await _spawn(source)
        source_row = await asyncio.wait_for(
            _seed_source(source_id, origin_a, origin_b), timeout=CELL_DEADLINE
        )
        _report("source", source_row)

        target_id = await _spawn(target)
        before, after, refused = await asyncio.wait_for(
            _hand_off_cookies(source_id, target_id), timeout=CELL_DEADLINE
        )
        seen = await asyncio.wait_for(
            _visit_and_read(target_id, origin_a, origin_b), timeout=CELL_DEADLINE
        )
        _report(
            "C2-live",
            seen
            | {
                "read_from_source": _reported_names(_by_name(before)),
                "in_target_jar": _reported_names(_by_name(after)),
                "refused": refused,
                "fidelity": {
                    name: drift
                    for name, drift in _fidelity(before, after).items()
                    if name.startswith("f898_") or drift
                },
            },
        )

        # C2 carries COOKIES — including the session cookie, the HttpOnly one
        # and, cross-site, the partitioned one.
        assert set(SITE_A_COOKIES) <= seen["site_a_cookies"], seen
        assert set(SITE_B_COOKIES) <= seen["site_b_cookies"], seen
        assert refused == [], refused

        # CHIPS, positively. The brief's open question was whether a
        # `Partitioned` cookie is partitioned AT ALL on a loopback origin with
        # no TLS — so this asserts the source really produced a partition key
        # (naming site A, the TOP-level site, not B which set the cookie), and
        # that the key is what arrived. Without this the `fidelity` row for
        # `f898_chips` would be an empty dict either way: two cookies with no
        # partition key at all also "match".
        partitioned = _by_name(before)[COOKIE_CHIPS]["partitionKey"]
        assert partitioned["topLevelSite"] == origin_a.site, partitioned
        assert partitioned["hasCrossSiteAncestor"] is True, partitioned
        assert _by_name(after)[COOKIE_CHIPS]["partitionKey"] == partitioned
        # …and its unpartitioned twin, set by the same response, carries none —
        # which is what proves the key above is CHIPS and not just "site B".
        assert _by_name(before)[COOKIE_B_PLAIN].get("partitionKey") is None

        # …and NOTHING else. Every one of these is a real login shape for some
        # site, and the tool docstring must say so.
        assert seen["storage"]["local"] is None
        assert seen["storage"]["session"] is None
        assert seen["storage"]["idb"] is None
        assert seen["storage"]["cache"] is None
        assert seen["storage"]["service_workers"] == 0

        # Fidelity: every cookie arrives, and every carried field round-trips.
        assert set(_by_name(before)) == set(_by_name(after)), _fidelity(before, after)
        for name, drifted in _fidelity(before, after).items():
            assert drifted == {}, (name, drifted)
    finally:
        await _close(target_id)
        await _close(source_id)
        await _released(source)
        await _released(target)


async def test_c1_file_copy_of_a_stopped_source(origins, tmp_empty_root, tmp_path):
    """The control: the same copy, with the source cleanly closed first.

    This is what separates "C1 cannot carry this" from "C1 cannot carry this
    WHILE THE SOURCE IS UP". Three of the five mechanisms change answer here,
    and two do not — and the two that do not are the two the copier is told to
    skip, which is a decision and not a race.
    """
    origin_a, origin_b = origins
    source = tmp_path / "source"
    target = tmp_path / "c1-stopped"
    source_id = target_id = None
    try:
        source_id = await _spawn(source)
        source_row = await asyncio.wait_for(
            _seed_source(source_id, origin_a, origin_b), timeout=CELL_DEADLINE
        )
        _report("source", source_row)

        await _close(source_id)
        source_id = None
        await _released(source)

        refusal, skipped = await asyncio.to_thread(
            _copy_profile, source, target, tmp_path
        )
        assert refusal is None, f"the copy refused: {refusal}"

        target_id = await _spawn(target)
        seen = await asyncio.wait_for(
            _visit_and_read(target_id, origin_a, origin_b), timeout=CELL_DEADLINE
        )
        _report("C1-stopped", seen | {"locked_files": _locked_names(skipped)})

        # With the jar closed, every PERSISTENT cookie crosses — both sites,
        # `HttpOnly` and `Partitioned` included.
        assert set(PERSISTENT_SITE_A_COOKIES) <= seen["site_a_cookies"], seen
        assert set(SITE_B_COOKIES) <= seen["site_b_cookies"], seen
        assert COOKIE_JAR not in _locked_names(skipped), skipped

        # The session cookie does NOT, and that is the whole reason it is in
        # this matrix: it never reached the file, so no copy of the file can
        # carry it, and this is the one mechanism where C2 beats a clean C1.
        assert COOKIE_SESSION not in seen["site_a_cookies"], seen

        # And so does the origin's own storage.
        assert seen["storage"]["local"] == TOKEN
        assert seen["storage"]["idb"] == TOKEN

        # sessionStorage never crosses anything: it is per-tab and in memory,
        # and is here so the matrix says that rather than leaving it implied.
        assert seen["storage"]["session"] is None

        # The two the copier is INSTRUCTED to drop stay dropped — closing the
        # source changes nothing about `_REGENERABLE_PROFILE_NAMES`.
        assert seen["storage"]["cache"] is None
        assert seen["storage"]["service_workers"] == 0
    finally:
        await _close(target_id)
        await _close(source_id)
        await _released(source)
        await _released(target)
