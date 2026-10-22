"""plan_RELEASE W7/W10/W16 — the DYNAMIC half of the one fixture app.

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

Composition: a route is one entry in :data:`ROUTES` keyed by ``(method, path)``,
whose value takes the live ``BaseHTTPRequestHandler`` and writes the response.
Adding a shape means adding entries and page builders — never a second handler,
server, or dispatch. W16's dedicated/shared/service workers, PWA cache, state
stores and internationalized pages all landed under exactly that rule.

W16's two structural additions to the pattern. First, a **worker outlives the
page that can report on it**, so every worker milestone that matters (the
shared worker's zero-client teardown, the service worker's install/activate) is
reported to the SERVER and read back from the ledger — a page-side assertion
could not observe it at all. Second, every W16 oracle is computed twice: the
page or worker computes it in JavaScript, this module computes it in Python
(:func:`w16_hash` and friends), and the test asserts they agree. A fixture that
graded its own homework would pass whatever it produced.

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

The ``cov_*`` block at the end of this file is the SECOND deliberate exception,
and unlike W10's it really is elapsed time: ``/cov/slow_asset``,
``/cov/slow_page.html`` and the delay ``/cov/slow_load.html`` threads into the
first of those each ``time.sleep`` for a ``?ms=`` the caller names. They exist
because their consumers measure *when* an answer arrived — whether a navigation
waited for its page's own ``load`` (F-881) and whether three requests in flight
overlap — and neither question can be asked of an event a test releases, since
the release itself would be the thing being timed. What keeps them safe is the
rule they DO share with the fault routes: the delay is never a synchronization
point (nothing waits for one to finish before doing something else), it is
capped at :data:`COV_MAX_DELAY_MS` whatever the URL asks for, a request with no
``?ms=`` answers immediately so the hermetic route enumeration cannot park a
thread, and a peer that gave up first is recorded rather than raised.
"""

from __future__ import annotations

import asyncio
import json
import time
import warnings
from collections.abc import Callable
from typing import Any

from fixture_routes_base import (
    _WS_GUID,
    AUTH_CHALLENGE_BODY,
    AUTH_GRANTED_BODY,
    AUTH_HEADER_VALUE,
    AUTH_PASS,
    AUTH_REALM,
    AUTH_USER,
    COMPOSITION_FINAL,
    COMPOSITION_STEPS,
    CSP_HEADER_NAME,
    CSP_HEADER_VALUE,
    CSP_NONCE,
    CSP_PING_BODY,
    FAULT_CEILING_SECONDS,
    FAULT_PARTIAL_PREFIX,
    FAULT_PARTIAL_SENTINEL,
    FAULT_PARTIAL_SUFFIX,
    FAULT_RELEASED_BODY,
    FAULT_SLOW_BODY,
    FAULT_SLOW_SENTINEL,
    FEED_LAST_PAGE,
    FEED_PAGE_SIZE,
    FEED_TOTAL_ROWS,
    I18N_BIDI_ISOLATE,
    I18N_COMBINING,
    I18N_EMOJI_ZWJ,
    I18N_MIXED_ATTR,
    I18N_NFC,
    I18N_NFD,
    I18N_NON_BMP,
    I18N_RTL,
    I18N_STRINGS,
    I18N_TYPED_KEYS,
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
    PAYLOAD_BINARY_LENGTH,
    PAYLOAD_CHUNKED_BODY,
    PAYLOAD_CHUNKS,
    PAYLOAD_TEXT_BODY,
    POPUP_TARGET_TOKEN,
    SSE_EVENTS,
    STATUS_418_BODY,
    STATUS_503_BODY,
    VIRTUAL_POOL_SIZE,
    VIRTUAL_TOTAL_ROWS,
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
    W16_SW_UNCACHED_PATH,
    W16_SW_VERSION,
    W16_WORKER_CLOSED,
    W16_WORKER_IDS,
    W16_WORKER_LATE_ID,
    W16_WORKER_SALT,
    W16_WORKER_VERSION,
    WS_CLOSE_CODE,
    WS_MESSAGES,
    _fill,
    _ncr,
    _new_ledger,
    _page,
    _record,
    _send,
    _send_html,
    _send_json,
    _send_raw,
    _send_text,
    code_points,
    feed_rows,
    new_origin_state,
    payload_binary_body,
    w16_cache_oracle,
    w16_hash,
    w16_idb_group,
    w16_worker_replies,
)
from fixture_routes_cov import (
    _COV_PIXEL,
    COV_CLICKED_TITLE,
    COV_FORM_SENTINEL,
    COV_FORM_TITLE,
    COV_GATED_LOAD_PATH,
    COV_MAX_DELAY_MS,
    COV_PLAIN_SENTINEL,
    COV_PLAIN_TITLE,
    COV_SHELL_SENTINEL,
    COV_SHELL_TITLE,
    COV_SLOW_LOAD_SENTINEL,
    COV_SLOW_PAGE_SENTINEL,
    COV_TITLE_AFTER_LOAD,
    COV_TITLE_BEFORE_LOAD,
    _cov_bounded_ms,
    _r_cov_app_shell,
    _r_cov_form,
    _r_cov_gated_load,
    _r_cov_plain,
    _r_cov_slow_asset,
    _r_cov_slow_load,
    _r_cov_slow_page,
    cov_app_shell_page,
    cov_form_page,
    cov_plain_page,
    cov_slow_load_page,
)
from fixture_routes_handlers import (
    _await_release,
    _cors_allow_headers,
    _fault_lookup,
    _fault_snapshot,
    _new_fault_controller,
    _peer,
    _query_value,
    _r_auth_basic,
    _r_cors_blocked,
    _r_cors_echo_options,
    _r_cors_echo_post,
    _r_csp_ping,
    _r_csp_strict,
    _r_dynamic_probe,
    _r_events_page,
    _r_fault_arm,
    _r_fault_drop,
    _r_fault_hang_after_headers,
    _r_fault_hang_before_headers,
    _r_fault_release,
    _r_fault_slow,
    _r_fault_status,
    _r_feed,
    _r_frames_inner,
    _r_frames_middle,
    _r_frames_outer,
    _r_lazy,
    _r_ledger,
    _r_payload_binary,
    _r_payload_chunked,
    _r_payload_text,
    _r_popup_components,
    _r_popup_target,
    _r_redirect_final,
    _r_redirect_start,
    _r_redirect_to_b,
    _r_reset,
    _r_spa,
    _r_sse,
    _r_status_418,
    _r_status_503,
    _r_ws,
    _role,
    _write_partial_head,
    _ws_frame,
    fault_partial_head,
    fault_partial_tail,
    fault_slow_page,
    release_all_faults,
)
from fixture_routes_nav import (
    _NAV_CHALLENGE_JS,
    _NAV_PREEMPT_JS,
    _NAV_SELF_RELOAD_JS,
    ES_JSON_PAYLOAD,
    ES_REJECT_REASON,
    ES_SENTINEL,
    ES_VALUE_TOKEN,
    LIFE_ROWS,
    LIFE_SENTINEL,
    _nav_delay_seconds,
    _nav_record,
    _r_es_async,
    _r_es_value,
    _r_life_lifecycle,
    _r_nav_chain_final,
    _r_nav_chain_mid,
    _r_nav_chain_start,
    _r_nav_download,
    _r_nav_head_replace,
    _r_nav_iframe_host,
    _r_nav_js_challenge,
    _r_nav_landing,
    _r_nav_meta_refresh,
    _r_nav_preempt,
    _r_nav_self_reload,
    _r_nav_slow_asset,
    _r_nav_slow_doc,
    es_async_page,
    life_lifecycle_page,
    nav_chain_final_page,
    nav_document,
    nav_head_replace_page,
    nav_iframe_host_page,
    nav_js_challenge_page,
    nav_landing_page,
    nav_meta_refresh_page,
    nav_preempt_page,
    nav_self_reload_page,
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
from fixture_routes_w16 import (
    _DEDICATED_HOST_JS,
    _DEDICATED_WORKER_JS,
    _I18N_COMPOSITION_JS,
    _I18N_TEXT_JS,
    _PWA_APP_JS,
    _SERVICE_WORKER_JS,
    _SHARED_HOST_JS,
    _SHARED_WORKER_JS,
    _STATE_STORE_JS,
    _W16_HASH_JS,
    _W16_POINTS_JS,
    _r_dedicated_host,
    _r_dedicated_worker,
    _r_i18n_composition,
    _r_i18n_text,
    _r_pwa_app,
    _r_pwa_asset,
    _r_pwa_report,
    _r_pwa_uncached,
    _r_shared_host,
    _r_shared_report,
    _r_shared_worker,
    _r_state_store,
    _r_sw_script,
    _send_js,
    dedicated_host_page,
    dedicated_worker_script,
    i18n_composition_page,
    i18n_text_page,
    pwa_app_page,
    service_worker_script,
    shared_host_page,
    shared_worker_script,
    state_store_page,
)

#: Every name that moved to a sibling module stays importable from here (T-5431).
__all__ = [
    "AUTH_CHALLENGE_BODY",
    "AUTH_GRANTED_BODY",
    "AUTH_HEADER_VALUE",
    "AUTH_PASS",
    "AUTH_REALM",
    "AUTH_USER",
    "COMPOSITION_FINAL",
    "COMPOSITION_STEPS",
    "COV_CLICKED_TITLE",
    "COV_FORM_SENTINEL",
    "COV_FORM_TITLE",
    "COV_GATED_LOAD_PATH",
    "COV_MAX_DELAY_MS",
    "COV_PLAIN_SENTINEL",
    "COV_PLAIN_TITLE",
    "COV_SHELL_SENTINEL",
    "COV_SHELL_TITLE",
    "COV_SLOW_LOAD_SENTINEL",
    "COV_SLOW_PAGE_SENTINEL",
    "COV_TITLE_AFTER_LOAD",
    "COV_TITLE_BEFORE_LOAD",
    "CSP_HEADER_NAME",
    "CSP_HEADER_VALUE",
    "CSP_NONCE",
    "CSP_PING_BODY",
    "ES_JSON_PAYLOAD",
    "ES_REJECT_REASON",
    "ES_SENTINEL",
    "ES_VALUE_TOKEN",
    "FAULT_CEILING_SECONDS",
    "FAULT_PARTIAL_PREFIX",
    "FAULT_PARTIAL_SENTINEL",
    "FAULT_PARTIAL_SUFFIX",
    "FAULT_RELEASED_BODY",
    "FAULT_SLOW_BODY",
    "FAULT_SLOW_SENTINEL",
    "FEED_LAST_PAGE",
    "FEED_PAGE_SIZE",
    "FEED_TOTAL_ROWS",
    "I18N_BIDI_ISOLATE",
    "I18N_COMBINING",
    "I18N_EMOJI_ZWJ",
    "I18N_MIXED_ATTR",
    "I18N_NFC",
    "I18N_NFD",
    "I18N_NON_BMP",
    "I18N_RTL",
    "I18N_STRINGS",
    "I18N_TYPED_KEYS",
    "LIFE_ROWS",
    "LIFE_SENTINEL",
    "NAV_CHAIN_SENTINEL",
    "NAV_CHAIN_TITLE",
    "NAV_CHALLENGE_COOKIE",
    "NAV_CHALLENGE_SENTINEL",
    "NAV_DELAY_CEILING_SECONDS",
    "NAV_DOWNLOAD_BODY",
    "NAV_DOWNLOAD_NAME",
    "NAV_HEAD_REPLACE_SENTINEL",
    "NAV_IFRAME_HOST_SENTINEL",
    "NAV_IFRAME_HOST_TITLE",
    "NAV_LANDING_SENTINEL",
    "NAV_LANDING_TITLE",
    "NAV_META_REFRESH_SENTINEL",
    "NAV_PIXEL_PNG",
    "NAV_PREEMPT_SENTINEL",
    "NAV_PREEMPT_TITLE",
    "NAV_RELOADED_TITLE",
    "NAV_SELF_RELOAD_SENTINEL",
    "NAV_SLOW_DOC_SENTINEL",
    "NAV_SLOW_DOC_TITLE",
    "NAV_SOLUTION",
    "NAV_SOLVED_SENTINEL",
    "NAV_SOLVED_TITLE",
    "PAYLOAD_BINARY_LENGTH",
    "PAYLOAD_CHUNKED_BODY",
    "PAYLOAD_CHUNKS",
    "PAYLOAD_TEXT_BODY",
    "POPUP_TARGET_TOKEN",
    "SSE_EVENTS",
    "STATUS_418_BODY",
    "STATUS_503_BODY",
    "VIRTUAL_POOL_SIZE",
    "VIRTUAL_TOTAL_ROWS",
    "W16_CACHE_ENTRIES",
    "W16_COOKIE_MAX_AGE",
    "W16_COOKIE_PERSISTENT",
    "W16_COOKIE_PERSISTENT_VALUE",
    "W16_COOKIE_SESSION",
    "W16_COOKIE_SESSION_VALUE",
    "W16_IDB_ABORT_ID",
    "W16_IDB_INDEX",
    "W16_IDB_NAME",
    "W16_IDB_RECORDS",
    "W16_IDB_STORE",
    "W16_LOCAL_KEY",
    "W16_LOCAL_VALUE",
    "W16_SESSION_KEY",
    "W16_SESSION_VALUE",
    "W16_SHARED_VERSION",
    "W16_SHARED_ZERO_CLIENTS",
    "W16_STATE_CACHE",
    "W16_SW_ACTIVATED",
    "W16_SW_ASSET_BODY",
    "W16_SW_ASSET_PATH",
    "W16_SW_CACHE",
    "W16_SW_INSTALLED",
    "W16_SW_OFFLINE_BODY",
    "W16_SW_SCOPE",
    "W16_SW_UNCACHED_BODY",
    "W16_SW_UNCACHED_PATH",
    "W16_SW_VERSION",
    "W16_WORKER_CLOSED",
    "W16_WORKER_IDS",
    "W16_WORKER_LATE_ID",
    "W16_WORKER_SALT",
    "W16_WORKER_VERSION",
    "WS_CLOSE_CODE",
    "WS_MESSAGES",
    "_COV_PIXEL",
    "_DEDICATED_HOST_JS",
    "_DEDICATED_WORKER_JS",
    "_I18N_COMPOSITION_JS",
    "_I18N_TEXT_JS",
    "_NAV_CHALLENGE_JS",
    "_NAV_PREEMPT_JS",
    "_NAV_SELF_RELOAD_JS",
    "_PWA_APP_JS",
    "_SERVICE_WORKER_JS",
    "_SHARED_HOST_JS",
    "_SHARED_WORKER_JS",
    "_STATE_STORE_JS",
    "_W16_HASH_JS",
    "_W16_POINTS_JS",
    "_WS_GUID",
    "_await_release",
    "_cors_allow_headers",
    "_cov_bounded_ms",
    "_fault_lookup",
    "_fault_snapshot",
    "_fill",
    "_nav_delay_seconds",
    "_nav_record",
    "_ncr",
    "_new_fault_controller",
    "_new_ledger",
    "_page",
    "_peer",
    "_query_value",
    "_r_auth_basic",
    "_r_cors_blocked",
    "_r_cors_echo_options",
    "_r_cors_echo_post",
    "_r_cov_app_shell",
    "_r_cov_form",
    "_r_cov_gated_load",
    "_r_cov_plain",
    "_r_cov_slow_asset",
    "_r_cov_slow_load",
    "_r_cov_slow_page",
    "_r_csp_ping",
    "_r_csp_strict",
    "_r_dedicated_host",
    "_r_dedicated_worker",
    "_r_dynamic_probe",
    "_r_es_async",
    "_r_es_value",
    "_r_events_page",
    "_r_fault_arm",
    "_r_fault_drop",
    "_r_fault_hang_after_headers",
    "_r_fault_hang_before_headers",
    "_r_fault_release",
    "_r_fault_slow",
    "_r_fault_status",
    "_r_feed",
    "_r_frames_inner",
    "_r_frames_middle",
    "_r_frames_outer",
    "_r_i18n_composition",
    "_r_i18n_text",
    "_r_lazy",
    "_r_ledger",
    "_r_life_lifecycle",
    "_r_nav_chain_final",
    "_r_nav_chain_mid",
    "_r_nav_chain_start",
    "_r_nav_download",
    "_r_nav_head_replace",
    "_r_nav_iframe_host",
    "_r_nav_js_challenge",
    "_r_nav_landing",
    "_r_nav_meta_refresh",
    "_r_nav_preempt",
    "_r_nav_self_reload",
    "_r_nav_slow_asset",
    "_r_nav_slow_doc",
    "_r_payload_binary",
    "_r_payload_chunked",
    "_r_payload_text",
    "_r_popup_components",
    "_r_popup_target",
    "_r_pwa_app",
    "_r_pwa_asset",
    "_r_pwa_report",
    "_r_pwa_uncached",
    "_r_redirect_final",
    "_r_redirect_start",
    "_r_redirect_to_b",
    "_r_reset",
    "_r_shared_host",
    "_r_shared_report",
    "_r_shared_worker",
    "_r_spa",
    "_r_sse",
    "_r_state_store",
    "_r_status_418",
    "_r_status_503",
    "_r_sw_script",
    "_r_ws",
    "_record",
    "_role",
    "_send",
    "_send_html",
    "_send_js",
    "_send_json",
    "_send_raw",
    "_send_text",
    "_write_partial_head",
    "_ws_frame",
    "code_points",
    "cov_app_shell_page",
    "cov_form_page",
    "cov_plain_page",
    "cov_slow_load_page",
    "csp_strict_page",
    "dedicated_host_page",
    "dedicated_worker_script",
    "dynamic_probe_page",
    "es_async_page",
    "events_page",
    "fault_partial_head",
    "fault_partial_tail",
    "fault_slow_page",
    "feed_rows",
    "frames_inner_page",
    "frames_middle_page",
    "frames_outer_page",
    "i18n_composition_page",
    "i18n_text_page",
    "lazy_virtual_infinite_page",
    "life_lifecycle_page",
    "nav_chain_final_page",
    "nav_document",
    "nav_head_replace_page",
    "nav_iframe_host_page",
    "nav_js_challenge_page",
    "nav_landing_page",
    "nav_meta_refresh_page",
    "nav_preempt_page",
    "nav_self_reload_page",
    "new_origin_state",
    "payload_binary_body",
    "popup_components_page",
    "popup_target_page",
    "pwa_app_page",
    "redirect_final_page",
    "release_all_faults",
    "service_worker_script",
    "shared_host_page",
    "shared_worker_script",
    "spa_history_page",
    "state_store_page",
    "w16_cache_oracle",
    "w16_hash",
    "w16_idb_group",
    "w16_worker_replies",
]


# ── Fixture-ready wait: ONE definition for both settle helpers (the harness's
# ``_settle_dom`` and ``e2e_helpers.navigate_and_settle``). A document is settled
# when it is ``complete`` and, if it carries ``app.js``, that script has wired its
# listeners and set ``window.__fixtureReady``. The probe returns a JSON string
# (what ``execute_script`` carries unchanged) so a failure can name WHAT it saw:
# beyond the three settle facts it carries the document's own timing (``loadEnd``
# is 0 until its ``load`` event ends, so a ``navigate`` that returned early reads
# ``loadEnd: 0``), every ``script`` tag's src and the resource timing of what the
# page fetched (a request that ended without a response reads ``status`` 0), so the
# next "navigate returned before the page was wired" explains itself (T-4709). ──
FIXTURE_READY_TIMEOUT = 15.0
FIXTURE_STATE_JS = (
    "JSON.stringify((function(){"
    "var s=document.querySelector('script[src$=\"app.js\"]');"
    "var r=performance.getEntriesByType('resource');"
    "var n=performance.getEntriesByType('navigation')[0];"
    "return {href:location.href,state:document.readyState,script:s!==null,"
    "fetched:r.some(function(e){return e.name.indexOf('/app.js')>=0;}),"
    "ready:window.__fixtureReady===true,"
    "scripts:Array.prototype.map.call(document.scripts,function(e){return e.src;}),"
    "dcl:n?Math.round(n.domContentLoadedEventEnd):null,"
    "loadEnd:n?Math.round(n.loadEventEnd):null,now:Math.round(performance.now()),"
    "resources:r.slice(0,12).map(function(e){return {name:new URL(e.name).pathname,"
    "status:e.responseStatus,size:e.transferSize,start:Math.round(e.startTime),"
    "ms:Math.round(e.duration)};})};})())"
)


def fixture_state_ready(state: dict[str, Any], expect_app: bool = False) -> bool:
    """``expect_app``: the caller navigated to a page that loads ``app.js``, so a
    document without the script tag (``about:blank``, the previous page) is not it."""
    if state.get("state") != "complete":
        return False
    if not state.get("script"):
        return not expect_app
    return state.get("ready") is True


async def wait_fixture_ready(
    probe: Callable[[str], Any], expect_app: bool = False
) -> None:
    """Poll ``probe(FIXTURE_STATE_JS)`` until the document is settled, or fail.

    ``probe`` is the caller's transport (async, returns the JS result or ``None``
    on a failed call). A failed probe — a context destroyed by a redirect or
    meta-refresh — is retried until the deadline, never fatal on its own. The
    deadline failure names the first and the last page state it saw: ``fetched``
    false means the page recorded no resource timing for the script, true with
    ``ready`` false means it was fetched but did not finish wiring, and
    ``resources``, ``scripts`` and ``loadEnd`` say what each request and the
    document's own ``load`` did. A wait that recovers within the deadline
    still warns: ``navigate`` had already returned, so the page was not ready at
    the moment the tool said it was (T-4709)."""
    started = time.monotonic()
    deadline = started + FIXTURE_READY_TIMEOUT
    last: Any = None
    first: Any = None
    probes = 0
    unanswered = 0
    while True:
        probes += 1
        raw = await probe(FIXTURE_STATE_JS)
        state = None
        if isinstance(raw, str):
            try:
                state = json.loads(raw)
            except ValueError:
                state = None
        if isinstance(state, dict):
            last = state
            if first is None:
                first = state
            if fixture_state_ready(state, expect_app):
                if probes > 1:
                    warnings.warn(
                        f"navigate had already returned but {state.get('href')} was "
                        f"not settled: needed {probes} probes and "
                        f"{time.monotonic() - started:.2f}s before the document was "
                        "complete with app.js wired; the first probe saw: "
                        f"{first}",
                        stacklevel=2,
                    )
                return
        else:
            unanswered += 1
        assert time.monotonic() < deadline, (
            "fixture page never settled (app.js did not wire window.__fixtureReady "
            f"within {FIXTURE_READY_TIMEOUT:g}s; {probes} probes, {unanswered} "
            f"unanswered); first page state: {first}; last page state: {last}"
        )
        await asyncio.sleep(0.1)


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
    # MQ-155
    ("GET", "/workers/dedicated_host.html"): _r_dedicated_host,
    ("GET", "/workers/dedicated.js"): _r_dedicated_worker,
    # MQ-156
    ("GET", "/workers/shared_host.html"): _r_shared_host,
    ("GET", "/workers/shared.js"): _r_shared_worker,
    ("GET", "/workers/shared-report"): _r_shared_report,
    # MQ-157…158
    ("GET", "/pwa/app.html"): _r_pwa_app,
    ("GET", "/pwa/sw.js"): _r_sw_script,
    ("GET", "/pwa/report"): _r_pwa_report,
    ("GET", W16_SW_ASSET_PATH): _r_pwa_asset,
    ("GET", W16_SW_UNCACHED_PATH): _r_pwa_uncached,
    # MQ-158…160
    ("GET", "/state/store.html"): _r_state_store,
    # MQ-161…162
    ("GET", "/i18n/text.html"): _r_i18n_text,
    ("GET", "/i18n/composition.html"): _r_i18n_composition,
    # F-882 — the seven navigation-supersession shapes (a)…(g).
    ("GET", "/nav/landing"): _r_nav_landing,
    ("GET", "/nav/head-replace"): _r_nav_head_replace,
    ("GET", "/nav/meta-refresh"): _r_nav_meta_refresh,
    ("GET", "/nav/self-reload"): _r_nav_self_reload,
    ("GET", "/nav/js-challenge"): _r_nav_js_challenge,
    ("GET", "/nav/chain-start"): _r_nav_chain_start,
    ("GET", "/nav/chain-mid"): _r_nav_chain_mid,
    ("GET", "/nav/chain-final"): _r_nav_chain_final,
    ("GET", "/nav/slow-asset"): _r_nav_slow_asset,
    ("GET", "/nav/slow-doc"): _r_nav_slow_doc,
    ("GET", "/nav/preempt"): _r_nav_preempt,
    ("GET", "/nav/download"): _r_nav_download,
    ("GET", "/nav/iframe-host"): _r_nav_iframe_host,
    # Shared driver page + the server-side oracle ledger.
    ("GET", "/dynamic_probe.html"): _r_dynamic_probe,
    ("GET", "/e2e/reset"): _r_reset,
    ("GET", "/e2e/ledger"): _r_ledger,
    # Lifecycle resilience (test_e2e_lifecycle_resilience.py)
    ("GET", "/life/lifecycle.html"): _r_life_lifecycle,
    # F-883 — appended at the END (``es_*``).
    ("GET", "/es_async.html"): _r_es_async,
    ("GET", "/es_value"): _r_es_value,
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
    "/workers/dedicated_host.html": "fixture-w16-dedicated-host",
    "/workers/shared_host.html": "fixture-w16-shared-host",
    "/pwa/app.html": "fixture-w16-pwa-app",
    "/state/store.html": "fixture-w16-state-store",
    "/i18n/text.html": "fixture-w16-i18n-text",
    "/i18n/composition.html": "fixture-w16-i18n-composition",
    "/life/lifecycle.html": LIFE_SENTINEL,
    "/nav/landing": NAV_LANDING_SENTINEL,
    "/nav/head-replace": NAV_HEAD_REPLACE_SENTINEL,
    "/nav/meta-refresh": NAV_META_REFRESH_SENTINEL,
    "/nav/self-reload": NAV_SELF_RELOAD_SENTINEL,
    "/nav/js-challenge": NAV_CHALLENGE_SENTINEL,
    "/nav/chain-final": NAV_CHAIN_SENTINEL,
    "/nav/slow-doc": NAV_SLOW_DOC_SENTINEL,
    "/nav/preempt": NAV_PREEMPT_SENTINEL,
    "/nav/iframe-host": NAV_IFRAME_HOST_SENTINEL,
    # F-883 — appended at the END (``es_*``).
    "/es_async.html": ES_SENTINEL,
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


ROUTES.update(
    {
        ("GET", "/cov/plain.html"): _r_cov_plain,
        ("GET", "/cov/slow_asset"): _r_cov_slow_asset,
        ("GET", "/cov/slow_load.html"): _r_cov_slow_load,
        ("GET", "/cov/slow_page.html"): _r_cov_slow_page,
        ("GET", "/cov/app_shell.html"): _r_cov_app_shell,
        ("GET", "/cov/form.html"): _r_cov_form,
        ("GET", COV_GATED_LOAD_PATH): _r_cov_gated_load,
    }
)

DYNAMIC_PAGES.update(
    {
        "/cov/plain.html": COV_PLAIN_SENTINEL,
        "/cov/slow_load.html": COV_SLOW_LOAD_SENTINEL,
        "/cov/slow_page.html": COV_SLOW_PAGE_SENTINEL,
        "/cov/app_shell.html": COV_SHELL_SENTINEL,
        "/cov/form.html": COV_FORM_SENTINEL,
        COV_GATED_LOAD_PATH: COV_SLOW_LOAD_SENTINEL,
    }
)
