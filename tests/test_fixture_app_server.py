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


def test_wait_fixture_ready_names_the_state_it_last_saw(monkeypatch):
    import asyncio

    import fixture_routes

    monkeypatch.setattr(fixture_routes, "FIXTURE_READY_TIMEOUT", 0.3)
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
    assert len(seen) > 2
