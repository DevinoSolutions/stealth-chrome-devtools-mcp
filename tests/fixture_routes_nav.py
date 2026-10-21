"""Lifecycle, F-882 navigation-supersession and F-883 async-script pages and routes.

Split out of :mod:`fixture_routes` (T-5431); that module re-exports every name
here, so callers keep importing from ``fixture_routes``.
"""

from __future__ import annotations

import json
import time

from fixture_routes_base import (
    NAV_CHAIN_SENTINEL,
    NAV_CHAIN_TITLE,
    NAV_CHALLENGE_COOKIE,
    NAV_CHALLENGE_SENTINEL,
    NAV_DELAY_CEILING_SECONDS,
    NAV_DOWNLOAD_BODY,
    NAV_DOWNLOAD_NAME,
    NAV_HEAD_REPLACE_SENTINEL,
    NAV_IFRAME_HOST_SENTINEL,
    NAV_IFRAME_HOST_TITLE,
    NAV_LANDING_SENTINEL,
    NAV_LANDING_TITLE,
    NAV_META_REFRESH_SENTINEL,
    NAV_PIXEL_PNG,
    NAV_PREEMPT_SENTINEL,
    NAV_PREEMPT_TITLE,
    NAV_RELOADED_TITLE,
    NAV_SELF_RELOAD_SENTINEL,
    NAV_SLOW_DOC_SENTINEL,
    NAV_SLOW_DOC_TITLE,
    NAV_SOLUTION,
    NAV_SOLVED_SENTINEL,
    NAV_SOLVED_TITLE,
    _fill,
    _page,
    _record,
    _send,
    _send_html,
    _send_json,
)
from fixture_routes_handlers import _query_value

# ── Lifecycle-resilience page (tests/test_e2e_lifecycle_resilience.py) ───────
# ONE page that answers every tool that module's soak issues — navigate, scroll,
# type, screenshot — so a lifecycle node never has to reach for a page another
# plan owns and change its meaning. Deliberately static after load: no timer, no
# fetch, no worker. A lifecycle stress asks whether the SESSION survived, and a
# page that kept doing work of its own would put a second, noisier explanation
# under every failure.
LIFE_SENTINEL = "fixture-life-lifecycle-page"
LIFE_ROWS = 400  # ~12000px of document: taller than any CI viewport, so it scrolls


def life_lifecycle_page() -> str:
    rows = "".join(
        f"<div class='row' id='row-{i}'>row {i}</div>" for i in range(LIFE_ROWS)
    )
    return _page(
        "fixture-life-lifecycle",
        LIFE_SENTINEL,
        (
            "<input id='life-input' type='text' value=''>"
            "<div id='life-rows'>" + rows + "</div>"
        ),
        head="<style>.row{height:30px}</style>",
    )


def _r_life_lifecycle(handler, query: str) -> None:
    _send_html(handler, life_lifecycle_page())


# ── F-882 navigation-supersession pages and routes ──────────────────────────
def nav_document(
    sentinel: str, title: str | None, head: str = "", body: str = ""
) -> str:
    """One nav fixture document.

    ``title=None`` emits NO ``<title>`` element at all, which is the half of
    these shapes that makes a stale answer visible: ``document.title`` reads
    ``""`` for the document the tool's own loader committed, and only the
    document that REPLACES it has a title. ``_page`` cannot express that — it
    always writes a title — and widening it would put a nav-only concern in the
    builder every other shape shares.
    """
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"{f'<title>{title}</title>' if title else ''}{head}</head><body>"
        f"<p id='sentinel'>{sentinel}</p>{body}</body></html>"
    )


def nav_landing_page(origin: str) -> str:
    """Where every superseding navigation lands. ``origin`` names which shape
    sent us here, so a test reads the route taken rather than inferring it."""
    return nav_document(
        NAV_LANDING_SENTINEL,
        NAV_LANDING_TITLE,
        body=f"<p id='from'>{origin}</p>",
    )


def nav_head_replace_page(target: str) -> str:
    """(a) A head script that ``location.replace``s while the parser is still
    running: this document commits under the tool's loaderId and is destroyed
    before it can fire ``load``."""
    return nav_document(
        NAV_HEAD_REPLACE_SENTINEL,
        None,
        head=f"<script>location.replace({json.dumps(target)});</script>",
    )


def nav_meta_refresh_page(target: str) -> str:
    """(b) The same supersession with no script at all — Chrome's own
    ``meta refresh`` at delay 0."""
    return nav_document(
        NAV_META_REFRESH_SENTINEL,
        None,
        head=f"<meta http-equiv='refresh' content='0;url={target}'>",
    )


_NAV_SELF_RELOAD_JS = """
(function () {
  var key = 'nav-reload-' + __TOKEN__;
  if (sessionStorage.getItem(key)) { document.title = __TITLE__; return; }
  sessionStorage.setItem(key, '1');
  window.addEventListener('load', function () { location.reload(); });
})();
"""


def nav_self_reload_page(token: str) -> str:
    """(c) The Amazon shape: the FIRST document has no title and reloads itself
    once at its own ``load``; the second sets one. ``sessionStorage`` keyed on
    the caller's token is what makes "exactly once" a property of the page
    rather than of how often the suite has run."""
    return nav_document(
        NAV_SELF_RELOAD_SENTINEL,
        None,
        head="<script>"
        + _fill(
            _NAV_SELF_RELOAD_JS,
            token=json.dumps(token),
            title=json.dumps(NAV_RELOADED_TITLE),
        )
        + "</script>",
    )


_NAV_CHALLENGE_JS = """
(function () {
  document.cookie = __COOKIE__ + '=' + __SOLUTION__ + '; path=/';
  location.replace(__TARGET__);
})();
"""


def nav_js_challenge_page(token: str) -> str:
    """(d) Reddit's ``js_challenge``: an untitled interstitial that sets a
    cookie and re-navigates itself to the solved URL. The cookie is the
    server-side oracle — the landing route records it, so "the challenge really
    ran" is not the page's own word."""
    target = f"/nav/js-challenge?token={token}&solution={NAV_SOLUTION}&js_challenge=1"
    return nav_document(
        NAV_CHALLENGE_SENTINEL,
        None,
        head="<script>"
        + _fill(
            _NAV_CHALLENGE_JS,
            cookie=json.dumps(f"{NAV_CHALLENGE_COOKIE}_{token}"),
            solution=json.dumps(NAV_SOLUTION),
            target=json.dumps(target),
        )
        + "</script>",
    )


def nav_chain_final_page(delay_ms: int) -> str:
    """(e) The control: ONE loader (a 302 chain is one navigation) whose
    ``load`` is held open by a slow IMAGE. An image and not a script, so
    ``DOMContentLoaded`` and the ``<title>`` are early and only ``load`` is
    late — which is the whole thing a ``wait_until='load'`` claims to wait
    for."""
    return nav_document(
        NAV_CHAIN_SENTINEL,
        NAV_CHAIN_TITLE,
        body=f"<img id='slow' src='/nav/slow-asset?ms={delay_ms}' alt=''>",
    )


_NAV_PREEMPT_JS = """
window.addEventListener('load', function () {
  setTimeout(function () { location.replace(__TARGET__); }, __MS__);
});
"""


def nav_preempt_page(delay_ms: int, target: str) -> str:
    """(g) The pre-commit shape's first half: a loaded page that navigates
    itself away on a timer. A test navigates FROM here to a document the server
    answers slowly, so the page pre-empts a navigation that never committed."""
    return nav_document(
        NAV_PREEMPT_SENTINEL,
        NAV_PREEMPT_TITLE,
        head="<script>"
        + _fill(_NAV_PREEMPT_JS, target=json.dumps(target), ms=delay_ms)
        + "</script>",
    )


def nav_iframe_host_page() -> str:
    """A titled document with a SAME-ORIGIN iframe whose own document replaces
    itself: two loaders commit in the subframe while the main frame commits
    once. Every ``Page.lifecycleEvent`` carries the frame it belongs to, so the
    answer must stay the HOST's — a wait that counted subframe loaders would be
    waiting on a document the caller never asked for."""
    return nav_document(
        NAV_IFRAME_HOST_SENTINEL,
        NAV_IFRAME_HOST_TITLE,
        body="<iframe id='child' src='/nav/head-replace'></iframe>",
    )


def _nav_delay_seconds(query: str) -> float:
    """``?ms=`` as bounded seconds; 0 for anything that is not a plain count."""
    raw = _query_value(query, "ms")
    if not raw.isdigit():
        return 0.0
    return min(int(raw) / 1000.0, NAV_DELAY_CEILING_SECONDS)


def _nav_record(handler) -> None:
    _record(handler, "nav_paths", handler.path)


def _r_nav_landing(handler, query: str) -> None:
    _nav_record(handler)
    _send_html(handler, nav_landing_page(_query_value(query, "from")))


def _r_nav_head_replace(handler, query: str) -> None:
    _nav_record(handler)
    _send_html(handler, nav_head_replace_page("/nav/landing?from=head-replace"))


def _r_nav_meta_refresh(handler, query: str) -> None:
    _nav_record(handler)
    _send_html(handler, nav_meta_refresh_page("/nav/landing?from=meta-refresh"))


def _r_nav_self_reload(handler, query: str) -> None:
    _nav_record(handler)
    _send_html(handler, nav_self_reload_page(_query_value(query, "token")))


def _r_nav_js_challenge(handler, query: str) -> None:
    """The interstitial, or the solved page once the page has re-navigated.

    The Cookie header is recorded on the SOLVED request only: that is the one
    request whose presence proves the challenge document ran and re-navigated.
    """
    _nav_record(handler)
    if _query_value(query, "js_challenge") != "1":
        _send_html(handler, nav_js_challenge_page(_query_value(query, "token")))
        return
    _record(handler, "nav_paths", f"cookie={handler.headers.get('Cookie') or ''}")
    _send_html(handler, nav_document(NAV_SOLVED_SENTINEL, NAV_SOLVED_TITLE))


def _r_nav_chain_start(handler, query: str) -> None:
    _nav_record(handler)
    _send(handler, 302, [("Location", f"/nav/chain-mid?{query}")])


def _r_nav_chain_mid(handler, query: str) -> None:
    _nav_record(handler)
    _send(handler, 302, [("Location", f"/nav/chain-final?{query}")])


def _r_nav_chain_final(handler, query: str) -> None:
    _nav_record(handler)
    raw = _query_value(query, "ms")
    _send_html(handler, nav_chain_final_page(int(raw) if raw.isdigit() else 0))


def _r_nav_slow_asset(handler, query: str) -> None:
    """The subresource that holds ``load`` open. Bounded by the ceiling."""
    time.sleep(_nav_delay_seconds(query))
    _send(handler, 200, [("Content-Type", "image/png")], NAV_PIXEL_PNG)


def _r_nav_slow_doc(handler, query: str) -> None:
    """A DOCUMENT the server answers slowly — so a navigation to it is pending,
    not committed, for as long as the caller asked.

    Recorded BEFORE the delay, like every other ``/nav/*`` route: ``nav_paths``
    is what the browser FETCHED, and the fetch is the arrival. Recording after
    the delay instead attributes the request to whichever ledger is current
    when the delay ENDS — and this route exists to be pre-empted, so the client
    abandons the request while this thread sleeps on and the entry lands in the
    next test's freshly reset ledger. Measured: CI run 35150887345.

    ``nav_paths`` therefore has ONE meaning — arrival — for every ``/nav/*``
    route. A witness for the other end, "the answer reached the client", must
    be a SEPARATE signal under its own ledger key, recorded after a successful
    flush; it can never be this one. Recording here late does not make one
    either: this line runs before ``_send_html``, the only thing that writes a
    header, so it fired identically whether the answer went out or the peer had
    already gone.
    """
    _nav_record(handler)
    time.sleep(_nav_delay_seconds(query))
    _send_html(handler, nav_document(NAV_SLOW_DOC_SENTINEL, NAV_SLOW_DOC_TITLE))


def _r_nav_preempt(handler, query: str) -> None:
    _nav_record(handler)
    raw = _query_value(query, "ms")
    _send_html(
        handler,
        nav_preempt_page(int(raw) if raw.isdigit() else 0, "/nav/landing?from=preempt"),
    )


def _r_nav_iframe_host(handler, query: str) -> None:
    _nav_record(handler)
    _send_html(handler, nav_iframe_host_page())


def _r_nav_download(handler, query: str) -> None:
    """(f) A navigation that commits NOTHING: Chrome turns it into a download
    and answers ``Page.navigate`` with ``net::ERR_ABORTED``."""
    _nav_record(handler)
    _send(
        handler,
        200,
        [
            ("Content-Type", "application/octet-stream"),
            ("Content-Disposition", f'attachment; filename="{NAV_DOWNLOAD_NAME}"'),
        ],
        NAV_DOWNLOAD_BODY,
    )


# ── F-883 (``es_*``): the page ``execute_script``'s async contract is proved on ─
#
# Appended at the END, per the F-883 task's rule for new routes. The page is
# deliberately inert — no timers of its own, no fetches, nothing that races the
# script under test. What it provides is the two things a Promise assertion
# needs and a literal cannot fake: a delayed resolution the PAGE owns
# (``esDelayed``), so a value that arrives is proof the tool waited for it, and
# a rejection carrying a known reason (``esRejects``), so the error the tool
# raises can be matched against text nothing else on the page holds. ``esNever``
# is the un-settling Promise the ``timeout_ms`` bound is measured against, and
# ``esSettled`` records what ``esDelayed`` last resolved — the observable the
# late-settlement node (F-883 B1) polls instead of sleeping.
ES_SENTINEL = "fixture-es-async-page"
ES_REJECT_REASON = "es-rejected-on-purpose"
ES_VALUE_TOKEN = "es-fetched-value"
ES_JSON_PAYLOAD = {"k": ES_VALUE_TOKEN, "n": 9}


def es_async_page() -> str:
    """The exact page ``/es_async.html`` serves."""
    script = """
(function () {
  window.esSettled = null;
  window.esDelayed = function (ms, value) {
    return new Promise(function (ok) {
      setTimeout(function () { window.esSettled = value; ok(value); }, ms);
    });
  };
  window.esRejects = function (reason) {
    return new Promise(function (_ok, fail) {
      setTimeout(function () { fail(new Error(reason)); }, 10);
    });
  };
  window.esNever = function () { return new Promise(function () {}); };
  window.esNested = function () {
    return { user: { id: 7, tags: ['a', 'b'] },
             rows: [{ k: 1 }, { k: [3, { deep: true }] }],
             flags: { zero: 0, empty: '', no: false, nil: null } };
  };
})();
"""
    return _page(
        "es-async",
        ES_SENTINEL,
        f"<p id='es-reason'>{ES_REJECT_REASON}</p>",
        head=f"<script>{script}</script>",
    )


def _r_es_async(handler, query: str) -> None:
    _send_html(handler, es_async_page())


def _r_es_value(handler, query: str) -> None:
    _send_json(handler, ES_JSON_PAYLOAD)
