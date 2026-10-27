"""Hermetic smoke tests for the E2E fixture app + its HTTP server (plan_E2E STEP 1).

No Chrome, no integration marker: this runs in the unit job. It proves the
fixture server serves every page and every §2.2 API route exactly as the
integration suite depends on, and it enforces the determinism backstop — zero
external URLs anywhere under ``tests/fixture_app``.
"""

import re
from pathlib import Path

import requests

FIXTURE_APP_DIR = Path(__file__).resolve().parent / "fixture_app"

# Each page carries a unique sentinel in a <p id="sentinel"> and its <title>.
PAGES = {
    "index.html": "fixture-index-page",
    "interact.html": "fixture-interact-page",
    "extract.html": "fixture-extract-page",
    "network.html": "fixture-network-page",
    "cookies.html": "fixture-cookies-page",
    "hooks.html": "fixture-hooks-page",
    "hard_dom.html": "fixture-hard-dom-page",
    "interactions.html": "fixture-interactions-page",
}


def test_every_page_serves_200_with_sentinel(fixture_app_server):
    for page, sentinel in PAGES.items():
        r = requests.get(f"{fixture_app_server}/{page}", timeout=5)
        assert r.status_code == 200, page
        assert sentinel in r.text, page


def test_iframe_child_page_serves(fixture_app_server):
    """The hard_dom.html child-frame document (loaded via <iframe src>) serves
    with its own child-sentinel and the button the E2E oracle clicks. It carries
    a distinct <p id="child-sentinel"> (not the shared #sentinel), so it is not
    part of PAGES above."""
    r = requests.get(f"{fixture_app_server}/iframe_child.html", timeout=5)
    assert r.status_code == 200
    assert "CHILD-SENTINEL-TEXT" in r.text
    assert 'id="child-btn"' in r.text


def test_api_json_is_exact(fixture_app_server):
    r = requests.get(f"{fixture_app_server}/api/json", timeout=5)
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/json"
    assert r.json() == {"ok": True, "value": 42, "source": "fixture"}


def test_api_echo_reflects_body_and_lowercased_headers(fixture_app_server):
    r = requests.post(
        f"{fixture_app_server}/api/echo",
        data="fixture-payload",
        headers={"X-Fixture-Probe": "probe-value"},
        timeout=5,
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["body"] == "fixture-payload"
    # Header keys are lowercased; the probe header is reflected verbatim.
    assert payload["headers"].get("x-fixture-probe") == "probe-value"


def test_api_set_cookie(fixture_app_server):
    r = requests.get(
        f"{fixture_app_server}/api/set-cookie", timeout=5, allow_redirects=False
    )
    assert r.status_code == 200
    assert r.cookies.get("fixture_cookie") == "server-set"


def test_redirect_is_302_to_api_json(fixture_app_server):
    r = requests.get(f"{fixture_app_server}/redirect", timeout=5, allow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"] == "/api/json"


def test_no_external_urls_in_fixture_app():
    """Determinism backstop: no http:// or https:// anywhere in fixture_app."""
    offenders = []
    for path in sorted(FIXTURE_APP_DIR.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"https?://", text):
            snippet = text[max(0, match.start() - 20) : match.start() + 20]
            offenders.append(f"{path.name}: ...{snippet}...")
    assert offenders == [], f"external URLs found in fixture_app: {offenders}"


def test_action_log_helper_present_in_app_js():
    app_js = (FIXTURE_APP_DIR / "app.js").read_text(encoding="utf-8")
    assert "window.__actions" in app_js
    assert "function logAction" in app_js
    assert "action-log" in app_js


def test_fixture_files_are_ascii():
    """Fixture files must be plain ASCII (readable, no smart quotes / BOM)."""
    non_ascii = []
    for path in sorted(FIXTURE_APP_DIR.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if any(byte > 0x7F for byte in raw):
            non_ascii.append(path.name)
    assert non_ascii == [], f"non-ASCII bytes in fixture files: {non_ascii}"


def test_app_js_sets_fixture_ready_only_after_wire():
    """``window.__fixtureReady`` is the harness's "listeners are attached" signal,
    so it must be set AFTER ``wire()`` returns: set before, a settle helper would
    release a click into a page whose handlers do not exist yet (F-1006)."""
    source = (FIXTURE_APP_DIR / "app.js").read_text(encoding="utf-8")
    start = re.search(r"function start\(\)\s*\{(?P<body>.*?)\n  \}", source, re.DOTALL)
    assert start, "app.js lost its start() entry point"
    body = start.group("body")
    assert "wire();" in body
    assert "window.__fixtureReady = true;" in body
    assert body.index("wire();") < body.index("window.__fixtureReady = true;")
    # And nothing else may set it: a second writer would bypass the ordering.
    assert source.count("__fixtureReady = true") == 1


def test_fixture_state_ready_needs_complete_and_a_wired_script():
    from fixture_routes import fixture_state_ready

    base = {"state": "complete", "script": True, "fetched": True, "ready": True}
    assert fixture_state_ready(base)
    assert not fixture_state_ready({**base, "ready": False})
    assert not fixture_state_ready({**base, "state": "interactive"})
    # A document without app.js has nothing to wire, but must still be complete.
    assert fixture_state_ready({**base, "script": False, "ready": False})
    assert not fixture_state_ready(
        {**base, "script": False, "ready": False, "state": "loading"}
    )


def _fake_settle_clock(monkeypatch, fixture_routes):
    """Give ``wait_fixture_ready`` a clock only its own sleeps advance (T-9468).

    ``FIXTURE_READY_TIMEOUT`` is 0.3 s of fake time and each ``sleep(0.1)`` adds
    0.1 to it, so the probe count before the deadline (four: at 0, 0.1, 0.2 and
    0.30000000000000004) never depends on how fast the runner is."""
    from types import SimpleNamespace

    now = [0.0]

    async def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(fixture_routes, "FIXTURE_READY_TIMEOUT", 0.3)
    monkeypatch.setattr(
        fixture_routes, "time", SimpleNamespace(monotonic=lambda: now[0])
    )
    monkeypatch.setattr(fixture_routes, "asyncio", SimpleNamespace(sleep=sleep))


def test_wait_fixture_ready_names_the_state_it_last_saw(monkeypatch):
    import asyncio

    import fixture_routes

    _fake_settle_clock(monkeypatch, fixture_routes)
    seen = []

    async def probe(expression):
        seen.append(expression)
        if len(seen) == 1:
            return None  # a failed probe is retried, not fatal
        return (
            '{"href":"http://x/interact.html","state":"complete",'
            '"script":true,"fetched":false,"ready":false}'
        )

    try:
        asyncio.run(fixture_routes.wait_fixture_ready(probe))
    except AssertionError as exc:
        assert "never settled" in str(exc)
        assert "'fetched': False" in str(exc)
    else:
        raise AssertionError("an unwired page must not count as settled")
    assert len(seen) == 4


def test_expect_app_rejects_a_document_without_the_script():
    from fixture_routes import fixture_state_ready

    blank = {"state": "complete", "script": False, "fetched": False, "ready": False}
    assert fixture_state_ready(blank)
    assert not fixture_state_ready(blank, expect_app=True)
    assert fixture_state_ready({**blank, "script": True, "ready": True}, True)


def test_wait_fixture_ready_warns_when_navigate_returned_early():
    import asyncio

    import pytest

    import fixture_routes

    states = iter(
        [
            '{"href":"http://x/interact.html","state":"interactive",'
            '"script":true,"fetched":false,"ready":false}',
            '{"href":"http://x/interact.html","state":"complete",'
            '"script":true,"fetched":true,"ready":true}',
        ]
    )

    async def probe(expression):
        return next(states)

    with pytest.warns(UserWarning, match="navigate had already returned") as record:
        asyncio.run(fixture_routes.wait_fixture_ready(probe, expect_app=True))
    assert "http://x/interact.html" in str(record[0].message)


def test_wait_fixture_ready_is_silent_when_first_probe_settled():
    import asyncio
    import warnings

    import fixture_routes

    async def probe(expression):
        return (
            '{"href":"http://x/","state":"complete","script":true,'
            '"fetched":true,"ready":true}'
        )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        asyncio.run(fixture_routes.wait_fixture_ready(probe))


# ── T-4709: the fixture's own diagnostics must not lose what they exist to show ──


def _hit_ending(suffix_of: str, tail: str, timeout: float = 5.0):
    """The newest ``_FIXTURE_HITS`` entry for *suffix_of* once it ends in *tail*
    (``""`` = served). The entry is rewritten by the server thread AFTER the client
    has its answer, so it is polled for, bounded."""
    import time

    import release_gate_harness as harness

    deadline = time.monotonic() + timeout
    while True:
        mine = [h for h in list(harness._FIXTURE_HITS) if f"GET {suffix_of} " in h]
        if mine and (
            mine[-1].endswith(tail) if tail else mine[-1].endswith("HTTP/1.1")
        ):
            return mine[-1]
        if time.monotonic() >= deadline:
            return mine[-1] if mine else None
        time.sleep(0.02)


def _route(monkeypatch, path, action):
    import fixture_routes

    real = fixture_routes.dispatch

    def dispatch(handler, method):
        if handler.path == path:
            return action(handler)
        return real(handler, method)

    monkeypatch.setattr(fixture_routes, "dispatch", dispatch)


def test_a_served_request_is_recorded_plain(fixture_app_server):
    requests.get(f"{fixture_app_server}/index.html", timeout=5)
    hit = _hit_ending("/index.html", "")
    assert hit is not None and hit.endswith("GET /index.html HTTP/1.1"), hit


def test_a_request_the_client_cut_off_is_recorded_as_reset(
    fixture_app_server, monkeypatch
):
    """The recorder used to DROP a request whose handler raised a reset, so "no GET
    /app.js" could not tell "never sent" from "sent and cancelled" (T-4709)."""

    def reset(handler):
        raise ConnectionResetError(10054, "the client went away")

    _route(monkeypatch, "/t4709-reset", reset)
    try:
        requests.get(f"{fixture_app_server}/t4709-reset", timeout=5)
    except requests.exceptions.ConnectionError:
        pass  # the server hung up without an answer: that is the case
    hit = _hit_ending("/t4709-reset", " [reset]")
    assert hit is not None and hit.endswith("GET /t4709-reset HTTP/1.1 [reset]"), hit


def test_a_request_still_being_served_is_recorded_as_unfinished(
    fixture_app_server, monkeypatch
):
    import threading

    entered, release = threading.Event(), threading.Event()

    def hold(handler):
        entered.set()
        release.wait(10)
        handler.send_response(200)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True

    _route(monkeypatch, "/t4709-hold", hold)
    client = threading.Thread(
        target=lambda: requests.get(f"{fixture_app_server}/t4709-hold", timeout=15),
        daemon=True,
    )
    client.start()
    try:
        assert entered.wait(5), "the held route was never reached"
        hit = _hit_ending("/t4709-hold", " [unfinished]")
        assert hit is not None and hit.endswith("[unfinished]"), hit
    finally:
        release.set()
        client.join(10)
    assert _hit_ending("/t4709-hold", "").endswith("GET /t4709-hold HTTP/1.1")


def test_the_settle_probe_reads_the_timing_and_the_resources_a_failure_needs():
    import fixture_routes

    for needed in (
        "loadEnd",
        "getEntriesByType('resource')",
        "getEntriesByType('navigation')",
        "responseStatus",
        "document.scripts",
        "__fixtureReady",
    ):
        assert needed in fixture_routes.FIXTURE_STATE_JS, needed


def test_a_settle_that_never_happens_names_first_and_last_state_and_the_probes(
    monkeypatch,
):
    import asyncio

    import fixture_routes

    _fake_settle_clock(monkeypatch, fixture_routes)
    calls = []

    async def probe(expression):
        calls.append(expression)
        if len(calls) == 1:
            return None
        loaded = len(calls) > 3
        return (
            '{"href":"http://x/interact.html","state":"complete","script":true,'
            '"fetched":false,"ready":false,"scripts":["http://x/app.js"],'
            f'"loadEnd":{99 if loaded else 0},"resources":[{{"name":"/styles.css",'
            '"status":200,"size":1588}]}'
        )

    try:
        asyncio.run(fixture_routes.wait_fixture_ready(probe))
    except AssertionError as exc:
        message = str(exc)
    else:
        raise AssertionError("an unwired page must not count as settled")
    assert "first page state" in message and "'loadEnd': 0" in message
    assert "last page state" in message and "'loadEnd': 99" in message
    assert "'name': '/styles.css'" in message
    assert len(calls) == 4
    assert "4 probes, 1 unanswered" in message


def test_the_early_navigate_warning_carries_what_the_first_probe_saw():
    import asyncio

    import pytest

    import fixture_routes

    states = iter(
        [
            '{"href":"http://x/interact.html","state":"interactive","script":true,'
            '"fetched":false,"ready":false,"loadEnd":0}',
            '{"href":"http://x/interact.html","state":"complete","script":true,'
            '"fetched":true,"ready":true,"loadEnd":40}',
        ]
    )

    async def probe(expression):
        return next(states)

    with pytest.warns(UserWarning, match="navigate had already returned") as record:
        asyncio.run(fixture_routes.wait_fixture_ready(probe, expect_app=True))
    assert "the first probe saw" in str(record[0].message)
    assert "'loadEnd': 0" in str(record[0].message)
