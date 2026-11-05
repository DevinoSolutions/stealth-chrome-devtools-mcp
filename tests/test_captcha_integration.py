"""Captcha auto-click against the providers' own always-pass test widgets (real Chrome, real network).

Cloudflare publishes ``1x00000000000000000000AA`` and Google
``6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI`` as site keys whose widgets always
pass, so a click is all they ask for. Each node serves a page with ONE such
widget from a local HTTP server, turns the monitor on and waits for the page's
response field to carry a token.

These nodes need the open internet (the widget scripts are the providers') and a
browser, so they are ``integration`` and outside both the pre-push lane and the
release gate. A provider that cannot be reached skips its node rather than failing
it. ``tests/test_captcha_clicker.py`` pins the detection and the click without
either.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import threading
import time
import urllib.request

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

TURNSTILE_PAGE = """<!DOCTYPE html>
<html>
<head>
  <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
</head>
<body>
  <form>
    <div class="cf-turnstile" data-sitekey="1x00000000000000000000AA"
         data-callback="onSuccess"></div>
  </form>
  <div id="result"></div>
  <script>
    function onSuccess(token) {
      document.getElementById('result').textContent = 'CAPTCHA_PASSED';
    }
  </script>
</body>
</html>
"""

RECAPTCHA_PAGE = """<!DOCTYPE html>
<html>
<head>
  <script src="https://www.google.com/recaptcha/api.js" async defer></script>
</head>
<body>
  <form>
    <div class="g-recaptcha" data-sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"
         data-callback="onSuccess"></div>
  </form>
  <div id="result"></div>
  <script>
    function onSuccess(token) {
      document.getElementById('result').textContent = 'CAPTCHA_PASSED';
    }
  </script>
</body>
</html>
"""

#: (page, the response field the provider fills in, where its script comes from)
WIDGETS = {
    "turnstile": (
        TURNSTILE_PAGE,
        'input[name="cf-turnstile-response"]',
        "https://challenges.cloudflare.com/turnstile/v0/api.js",
    ),
    "recaptcha-v2": (
        RECAPTCHA_PAGE,
        'textarea[name="g-recaptcha-response"]',
        "https://www.google.com/recaptcha/api.js",
    ),
}

#: How long the monitor gets to find the widget, click it and see the provider
#: answer. The widget script is fetched and rendered first, so this is not short.
WAIT_SECONDS = 30.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(scope="module")
def pages():
    """A local server answering ``/<kind>`` with that widget's page."""
    routes = {f"/{kind}": page.encode() for kind, (page, _, _) in WIDGETS.items()}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = routes.get(self.path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _reachable(script_url: str) -> bool:
    try:
        with urllib.request.urlopen(script_url, timeout=10) as reply:  # noqa: S310  PERMANENT(a fixed https URL of the provider under test)
            return reply.status == 200
    except OSError:
        return False


@contextlib.asynccontextmanager
async def _page(tmp_path, base: str, kind: str):
    _, _, script_url = WIDGETS[kind]
    if not _reachable(script_url):
        pytest.skip(f"{script_url} is not reachable from here")
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    spawned = await spawn(
        headless=True,
        user_data_dir=str(tmp_path / f"captcha-{kind}"),
        **sandbox_kwargs(),
    )
    iid = spawned["instance_id"]
    try:
        await get_fn("navigate")(instance_id=iid, url=f"{base}/{kind}")
        yield iid
    finally:
        await close(instance_id=iid)


async def _token(iid: str, field: str) -> str:
    return await eval_js(
        iid,
        f"const f = document.querySelector('{field}'); return f ? f.value : '';",
    )


async def _wait_for_token(iid: str, field: str) -> str:
    deadline = time.monotonic() + WAIT_SECONDS
    token = await _token(iid, field)
    while not token and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        token = await _token(iid, field)
    return token


@pytest.mark.parametrize("kind", list(WIDGETS))
async def test_the_monitor_gets_the_widget_to_pass(tmp_path, pages, kind):
    field = WIDGETS[kind][1]
    enable = get_fn("enable_captcha_auto_click")
    status = get_fn("get_captcha_status")
    async with _page(tmp_path, pages, kind) as iid:
        assert (await status(instance_id=iid))["active"] is False

        enabled = await enable(instance_id=iid, interval_seconds=0.5)
        assert enabled["enabled"] is True
        token = await _wait_for_token(iid, field)

        after = await status(instance_id=iid)
        assert after["active"] is True
        assert token, f"{kind}: no token in {field} within {WAIT_SECONDS:g} s: {after}"
        assert (
            await eval_js(iid, "return document.getElementById('result').textContent;")
            == "CAPTCHA_PASSED"
        )
        if kind == "recaptcha-v2":
            # Google asks for the click; Cloudflare's test widget may pass by itself.
            assert after["clicks"], after
        for click in after["clicks"]:
            assert click["type"] == kind


async def test_a_solved_widget_is_not_clicked_again(tmp_path, pages):
    kind = "recaptcha-v2"
    field = WIDGETS[kind][1]
    enable, status = get_fn("enable_captcha_auto_click"), get_fn("get_captcha_status")
    async with _page(tmp_path, pages, kind) as iid:
        await enable(instance_id=iid, interval_seconds=0.5)
        assert await _wait_for_token(iid, field)
        settled = len((await status(instance_id=iid))["clicks"])

        await asyncio.sleep(3.0)
        assert len((await status(instance_id=iid))["clicks"]) == settled


async def test_nothing_is_clicked_once_disabled(tmp_path, pages):
    kind = "recaptcha-v2"
    field = WIDGETS[kind][1]
    enable = get_fn("enable_captcha_auto_click")
    disable = get_fn("disable_captcha_auto_click")
    async with _page(tmp_path, pages, kind) as iid:
        await enable(instance_id=iid, interval_seconds=0.5)
        await disable(instance_id=iid)

        await asyncio.sleep(8.0)
        assert not await _token(iid, field)
        assert (await get_fn("get_captcha_status")(instance_id=iid))["clicks"] == []
