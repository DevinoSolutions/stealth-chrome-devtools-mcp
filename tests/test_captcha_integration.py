"""Captcha auto-click against the providers' own test widgets (real Chrome, real network).

Each node serves a page with ONE widget from a local HTTP server, turns the
monitor on at its DEFAULT interval, navigates, and then holds the monitor to two
things: it clicked the widget's checkbox, and it did so FAST.

The site keys are the providers' published test keys:

* Turnstile ``3x00000000000000000000FF`` *forces an interactive challenge*: the
  widget does not pass until its checkbox is clicked. (2.1.60's test used
  ``1x00000000000000000000AA``, which passes with no click at all, so the node
  was green while the monitor could not see a real Turnstile.) Its iframe lives
  in a CLOSED shadow root, exactly as on a real site.
* reCAPTCHA ``6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI`` and hCaptcha
  ``10000000-ffff-ffff-ffff-000000000001`` always pass once clicked.

**Speed.** The page stamps ``window.__ready`` (``Date.now()``) when the checkbox
becomes clickable: Turnstile's ``before-interactive-callback``, or the
checkbox iframe's ``load`` for the other two (a ``MutationObserver`` attaches
the listener as the provider inserts the frame). The click log's timestamp is on
the same machine's clock, so ``click - ready`` is the delay the monitor adds,
measured independently of the monitor. What the budget holds is the delay the
MONITOR adds: time the PAGE held the monitor's own CDP calls (a macOS publish
runner whose page was busy running the provider's script answered each in ~90 ms
instead of 1-30 ms, which put one click at 1590 ms) is measured call by call
(``tests/captcha_timeline.py``), recorded as ``captcha_<kind>_held_ms``, warned
about and not charged. Everything else is charged: a longer interval or
first-click age, a sleep, more work per scan. The charged delay must stay under
``CLICK_BUDGET_S``; the numbers go into the JUnit report as properties
(``captcha_<kind>_click_ms``, ``_held_ms``, ``_token_ms``) so CI keeps a record.

These nodes need the open internet and a browser, so they are ``integration``:
the release gate's integration lane runs them on Linux, Windows and macOS. A
provider that cannot be reached skips its node rather than failing it.
``tests/test_captcha_clicker.py`` pins the detection and the click without either.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import threading
import time
import urllib.request
import warnings
from datetime import datetime

import pytest

from captcha_timeline import STALL_WARN_MS, Timeline, charged_ms, now_ms
from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded import captcha_clicker, page_natives

pytestmark = integration_pytestmark()

#: Stamps ``window.__ready`` once, when the checkbox can first take a click.
#: ``FRAME_MATCH`` is filled per provider: which inserted iframe is the checkbox.
READY_PROBE = """
<script>
  window.__ready = 0;
  function markReady() { if (!window.__ready) window.__ready = Date.now(); }
  new MutationObserver(() => {
    for (const frame of document.querySelectorAll('iframe')) {
      if (!frame.__watched && FRAME_MATCH.test(frame.src || '')) {
        frame.__watched = true;
        frame.addEventListener('load', markReady);
      }
    }
  }).observe(document, {childList: true, subtree: true});
</script>
"""

PAGE = """<!DOCTYPE html>
<html>
<head>
  <script>const FRAME_MATCH = {match};</script>
  {probe}
  <script src="{script}" async defer></script>
</head>
<body>
  <form>
    <div class="{container}" data-sitekey="{key}" data-callback="onSuccess"
         data-before-interactive-callback="markReady"></div>
  </form>
  <div id="result"></div>
  <script>
    function onSuccess(token) {
      window.__passed = Date.now();
      document.getElementById('result').textContent = 'CAPTCHA_PASSED';
    }
  </script>
</body>
</html>
"""

#: kind -> (script, container class, site key, checkbox-frame regex, response field)
WIDGETS = {
    "turnstile": (
        "https://challenges.cloudflare.com/turnstile/v0/api.js",
        "cf-turnstile",
        "3x00000000000000000000FF",
        "/(?!)/",  # closed shadow root: the before-interactive callback marks it
        'input[name="cf-turnstile-response"]',
    ),
    "recaptcha-v2": (
        "https://www.google.com/recaptcha/api.js",
        "g-recaptcha",
        "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI",
        r"/\/recaptcha\/api2\/anchor/",
        'textarea[name="g-recaptcha-response"]',
    ),
    "hcaptcha": (
        "https://js.hcaptcha.com/1/api.js",
        "h-captcha",
        "10000000-ffff-ffff-ffff-000000000001",
        "/frame=checkbox/",
        'textarea[name="h-captcha-response"]',
    ),
}


def _page_html(kind: str) -> bytes:
    script, container, key, match, _ = WIDGETS[kind]
    return (
        PAGE.replace("{match}", match)
        .replace("{probe}", READY_PROBE)
        .replace("{script}", script)
        .replace("{container}", container)
        .replace("{key}", key)
        .encode()
    )


#: How long the monitor gets to find the widget, click it and see the provider
#: answer. The widget script is fetched and rendered first, so this is not short.
WAIT_SECONDS = 30.0

#: The most the monitor may take, from the checkbox becoming clickable to the
#: click. At the default 0.25 s interval, six local Windows runs measured
#: Turnstile 103-313 ms, reCAPTCHA 389-465 ms and hCaptcha 142-359 ms (their
#: first clicks wait for a 0.3 s / 0.15 s-old frame), each on its first
#: click; 2.1.60's 2 s interval alone could add up to 2 s. The margin is for a loaded CI
#: runner, not for a slower monitor.
CLICK_BUDGET_S = 1.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture(scope="module")
def pages():
    """A local server answering ``/<kind>`` with that widget's page."""
    routes = {f"/{kind}": _page_html(kind) for kind in WIDGETS}

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
async def _browser(tmp_path, kind: str):
    if not _reachable(WIDGETS[kind][0]):
        pytest.skip(f"{WIDGETS[kind][0]} is not reachable from here")
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    spawned = await spawn(
        headless=True,
        user_data_dir=str(tmp_path / f"captcha-{kind}"),
        **sandbox_kwargs(),
    )
    iid = spawned["instance_id"]
    try:
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
        await asyncio.sleep(0.25)
        token = await _token(iid, field)
    return token


async def _wait_for_click(iid: str, kind: str) -> list[dict]:
    """Poll the monitor status until at least one click of *kind* appears."""
    status = get_fn("get_captcha_status")
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        after = await status(instance_id=iid)
        clicks = [c for c in after.get("clicks", []) if c["type"] == kind]
        if clicks:
            return clicks
        await asyncio.sleep(0.25)
    return []


def _epoch_ms(click: dict) -> float:
    return datetime.fromisoformat(click["timestamp"]).timestamp() * 1000


@pytest.fixture
def timeline(monkeypatch):
    """What the monitor did, sweep by sweep, and how long the page held its CDP
    calls (``tests/captcha_timeline.py``): a speed failure prints it, so a slow
    click on a CI runner says where the time went (the frame not listed yet, a
    read that held it, a scan that stalled) instead of leaving one number.
    2.1.62's gate: one macOS reCAPTCHA click 2421 ms after ready, which ten
    local runs (380-520 ms) never showed."""
    line = Timeline()
    scan, state, press = (
        captcha_clicker.scan,
        captcha_clicker.CaptchaMonitor._state,
        captcha_clicker.press,
    )
    send, frame_world = captcha_clicker._send, page_natives.frame_world

    async def timed_scan(tab):
        start = now_ms()
        try:
            got = await scan(tab)
        except BaseException as err:
            line.note(
                f"scan raised {type(err).__name__} after {now_ms() - start:.0f} ms"
            )
            raise
        found = ",".join(hit.type for hit in got.hits) or "-"
        line.note(
            f"scan {now_ms() - start:.0f} ms: hits {found}, "
            f"unmeasured {len(got.unmeasured)}, puzzle {got.challenge_showing}"
        )
        if got.hits and line.first_seen_ms is None:
            line.first_seen_ms = now_ms()
        return got

    async def timed_state(tab, hit, widget):
        got = await state(tab, hit, widget)
        line.note(f"state {got} (clicks {widget.clicks})")
        return got

    async def timed_press(tab, x, y):
        line.note("press")
        return await press(tab, x, y)

    async def timed_send(tab, command, session_id=None):
        start = now_ms()
        try:
            return await send(tab, command, session_id)
        finally:
            line.call(start, now_ms())

    async def timed_frame_world(tab, frame_id, session_id=None):
        start = now_ms()
        try:
            return await frame_world(tab, frame_id, session_id)
        finally:
            line.call(start, now_ms())

    monkeypatch.setattr(captcha_clicker, "scan", timed_scan)
    monkeypatch.setattr(
        captcha_clicker.CaptchaMonitor, "_state", staticmethod(timed_state)
    )
    monkeypatch.setattr(captcha_clicker, "press", timed_press)
    monkeypatch.setattr(captcha_clicker, "_send", timed_send)
    monkeypatch.setattr(page_natives, "frame_world", timed_frame_world)
    return line


@pytest.mark.parametrize("kind", list(WIDGETS))
async def test_the_monitor_clicks_the_widget_fast(
    tmp_path, pages, kind, record_property, timeline
):
    field = WIDGETS[kind][4]
    enable, status = get_fn("enable_captcha_auto_click"), get_fn("get_captcha_status")
    async with _browser(tmp_path, kind) as iid:
        assert (await status(instance_id=iid))["active"] is False
        # The DEFAULT interval is the one under test: no interval_seconds.
        assert (await enable(instance_id=iid))["enabled"] is True
        await get_fn("navigate")(instance_id=iid, url=f"{pages}/{kind}")

        clicks = await _wait_for_click(iid, kind)
        assert clicks, f"{kind}: no click within {WAIT_SECONDS:g} s"
        # All three test keys pass once clicked, with no puzzle: a missing token
        # is a click the widget dropped (measured: an hCaptcha click 18 ms
        # before its frame loaded), which a click-only assert would hide.
        assert await _wait_for_token(iid, field), (
            f"{kind}: no token in {field} within {WAIT_SECONDS:g} s: "
            f"{await status(instance_id=iid)}"
        )
        ready = await eval_js(iid, "return window.__ready;")
        assert ready, f"{kind}: the page never marked its checkbox ready"
        passed = await eval_js(iid, "return window.__passed || 0;")
        clicks = [
            c for c in (await status(instance_id=iid))["clicks"] if c["type"] == kind
        ]
        record_property(f"captcha_{kind}_attempts", len(clicks))
        record_property(
            f"captcha_{kind}_clicks_rel_ms",
            ",".join(str(round(_epoch_ms(c) - ready)) for c in clicks),
        )
        # The click that COUNTED: the last one before the token (the first one
        # when the callback stamp is missing). Its delay
        # is what the budget holds, so a first click that did nothing and a
        # retry seconds later fails here however fast the first one was.
        before_pass = [c for c in clicks if not passed or _epoch_ms(c) <= passed]
        counted = before_pass[-1] if passed and before_pass else clicks[0]
        # Clamped at 0: a click may land just before the page's own mark (a
        # Turnstile click during "Verifying...", or a frame's ``load`` event
        # dispatched after the click it took).
        click_ms = max(0.0, _epoch_ms(counted) - ready)
        record_property(f"captcha_{kind}_click_ms", round(click_ms))
        if passed:
            record_property(f"captcha_{kind}_token_ms", round(passed - ready))
        # The page's share of that delay: its hold on the monitor's CDP calls.
        held_ms = timeline.held_ms(_epoch_ms(counted))
        record_property(f"captcha_{kind}_held_ms", round(held_ms))
        if held_ms >= STALL_WARN_MS:
            warnings.warn(
                f"{kind}: the page held the monitor's CDP calls for {held_ms:.0f} ms "
                f"of the {click_ms:.0f} ms before the click (a busy runner, not the "
                f"monitor); monitor, ms after ready: {timeline.since(ready)}",
                stacklevel=1,
            )
        assert charged_ms(click_ms, held_ms) <= CLICK_BUDGET_S * 1000, (
            f"{kind}: the click that counted came {click_ms:.0f} ms after the "
            f"checkbox was ready, of which the page held the monitor's CDP calls "
            f"for {held_ms:.0f} ms, over the {CLICK_BUDGET_S:g} s budget: {clicks}; "
            f"monitor, ms after ready: {timeline.since(ready)}"
        )
        for click in clicks:
            assert click["type"] == kind


async def test_a_solved_widget_is_not_clicked_again(tmp_path, pages):
    kind = "recaptcha-v2"
    field = WIDGETS[kind][4]
    enable, status = get_fn("enable_captcha_auto_click"), get_fn("get_captcha_status")
    async with _browser(tmp_path, kind) as iid:
        await get_fn("navigate")(instance_id=iid, url=f"{pages}/{kind}")
        await enable(instance_id=iid)
        assert await _wait_for_token(iid, field)
        settled = len((await status(instance_id=iid))["clicks"])

        await asyncio.sleep(3.0)
        assert len((await status(instance_id=iid))["clicks"]) == settled


@pytest.mark.parametrize("kind", ["turnstile", "recaptcha-v2"])
async def test_nothing_is_clicked_once_disabled(tmp_path, pages, kind):
    """No click, no token: which is also what makes the fast node's token mean
    the monitor's click passed the widget (the Turnstile key forces it)."""
    field = WIDGETS[kind][4]
    enable = get_fn("enable_captcha_auto_click")
    disable = get_fn("disable_captcha_auto_click")
    async with _browser(tmp_path, kind) as iid:
        await enable(instance_id=iid)
        await disable(instance_id=iid)
        await get_fn("navigate")(instance_id=iid, url=f"{pages}/{kind}")

        await asyncio.sleep(8.0)
        assert not await _token(iid, field)
        assert (await get_fn("get_captcha_status")(instance_id=iid))["clicks"] == []


@pytest.mark.parametrize("kind", ["recaptcha-v2", "hcaptcha"])
async def test_a_dropped_click_is_clicked_again_at_once(
    tmp_path, pages, kind, monkeypatch, record_property
):
    """A first click the widget never took (the 2.1.61 publish gate, macOS:
    hCaptcha's, 70 ms after its frame loaded) is retried within
    ``DROPPED_AFTER`` of it, not ``RETRY_AFTER`` (6 s) later: the checkbox read
    inside the frame stayed idle. The drop is made here by sending nothing."""
    real_press = captcha_clicker.press
    pressed = []

    async def drop_the_first(tab, cx, cy):
        pressed.append(time.monotonic())
        if len(pressed) > 1:
            await real_press(tab, cx, cy)

    monkeypatch.setattr(captcha_clicker, "press", drop_the_first)
    field = WIDGETS[kind][4]
    async with _browser(tmp_path, kind) as iid:
        await get_fn("enable_captcha_auto_click")(instance_id=iid)
        await get_fn("navigate")(instance_id=iid, url=f"{pages}/{kind}")
        assert await _wait_for_token(iid, field)
        gap = pressed[1] - pressed[0]
        record_property(f"captcha_{kind}_dropped_retry_ms", round(gap * 1000))
        assert len(pressed) == 2, pressed
        assert gap < 1.5, gap  # DROPPED_AFTER is 0.5 s, RETRY_AFTER 6 s
