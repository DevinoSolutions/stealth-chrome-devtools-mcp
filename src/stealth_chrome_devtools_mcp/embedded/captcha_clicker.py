"""Automatic captcha checkbox clicker.

Detects Cloudflare Turnstile, Google reCAPTCHA v2, and hCaptcha iframes on
the page, and clicks their checkboxes via ``Input.dispatchMouseEvent`` --
a trusted coordinate click, the same as a human. Does NOT solve challenges
(image puzzles, sliders): the click initiates the check, and the captcha
provider decides whether to pass or escalate based on fingerprint and
reputation.

Like pydoll's ``expect_cloudflare_turnstile``, but running as a background
monitor across all tabs of a managed instance instead of wrapping one
navigation.

Only widgets in the TOP document are found: a widget inside a cross-origin
frame has no bounding rect in the top document's viewport, and a closed shadow
root hides its iframe from ``querySelector`` altogether.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from nodriver import Browser, Tab, cdp

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from collections.abc import Generator

    from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

SUPPORTED_TYPES = ("turnstile", "recaptcha-v2", "hcaptcha")

#: Where the checkbox sits relative to its iframe's top-left corner, in CSS px.
#: Turnstile's widget is 300x65, reCAPTCHA's anchor frame 302x78, hCaptcha's
#: checkbox frame is laid out like reCAPTCHA's.
CHECKBOX_OFFSETS: dict[str, tuple[float, float]] = {
    "turnstile": (30, 28),
    "recaptcha-v2": (27, 30),
    "hcaptcha": (27, 28),
}

#: Pixels of random jitter either way, so the click is not the same point twice.
JITTER_PX = 3

#: Bound on every CDP send, so a broken tab cannot hang the scan loop.
SEND_TIMEOUT = 5.0

_RNG = random.SystemRandom()

MIN_INTERVAL = 0.5
MAX_INTERVAL = 30.0
DEFAULT_INTERVAL = 2.0

#: Returns a JSON string (an array of hits) so an empty page and a thrown script
#: are told apart from a value by type, not by truthiness (``Tab.evaluate``
#: answers a falsy value with the RemoteObject itself).
DETECTION_JS = r"""
(() => {
  const CONTAINERS = [
    ['turnstile', '.cf-turnstile'],
    ['recaptcha-v2', '.g-recaptcha'],
    ['hcaptcha', '.h-captcha'],
  ];
  const classify = (frame) => {
    const src = frame.getAttribute('src') || frame.src || '';
    if (src.includes('challenges.cloudflare.com')) return 'turnstile';
    if (src.includes('google.com/recaptcha/api2/anchor')) return 'recaptcha-v2';
    if (src.includes('hcaptcha.com')) {
      return src.includes('frame=challenge') ? null : 'hcaptcha';
    }
    // Container fallback: only when the src belongs to a known provider domain
    // (no container-only match — a malicious page can wrap any iframe in
    // <div class="cf-turnstile"> and the monitor would click inside it).
    const domain = (() => {
      try { return new URL(src, location.href).hostname; } catch { return ''; }
    })();
    const PROVIDER_DOMAINS = [
      'cloudflare.com', 'recaptcha.net', 'google.com', 'gstatic.com',
      'hcaptcha.com',
    ];
    const ok = (d) => domain === d || domain.endsWith('.' + d);
    const onProvider = PROVIDER_DOMAINS.some(ok);
    if (onProvider) {
      for (const [type, selector] of CONTAINERS) {
        if (frame.closest(selector)) return type;
      }
    }
    return null;
  };
  const solved = (frame) => {
    const scope = frame.closest(
      '.cf-turnstile, .g-recaptcha, .h-captcha, [data-sitekey]'
    ) || frame.parentElement;
    if (!scope) return false;
    const fields = scope.querySelectorAll(
      'input[name*="response"], textarea[name*="response"]'
    );
    return Array.from(fields).some((field) => (field.value || '').length > 0);
  };
  const hits = [];
  for (const frame of document.querySelectorAll('iframe')) {
    const type = classify(frame);
    if (!type) continue;
    const rect = frame.getBoundingClientRect();
    hits.push({
      type,
      x: rect.left,
      y: rect.top,
      width: rect.width,
      height: rect.height,
      src: frame.getAttribute('src') || frame.src || '',
      already_checked: solved(frame),
      viewport_width: window.innerWidth,
      viewport_height: window.innerHeight,
    });
  }
  return JSON.stringify(hits);
})()
"""


@dataclass(frozen=True)
class CaptchaHit:
    type: str
    iframe_x: float
    iframe_y: float
    iframe_width: float
    iframe_height: float
    src: str
    already_checked: bool
    viewport_width: float | None = None
    viewport_height: float | None = None

    def checkbox_point(self) -> tuple[float, float]:
        offset_x, offset_y = CHECKBOX_OFFSETS[self.type]
        return self.iframe_x + offset_x, self.iframe_y + offset_y

    def on_screen(self) -> bool:
        """Whether the checkbox is inside the viewport: a mouse event outside it
        reaches nothing, so such a widget is left until the page scrolls to it."""
        x, y = self.checkbox_point()
        if x < 0 or y < 0:
            return False
        if self.viewport_width is not None and x > self.viewport_width:
            return False
        return self.viewport_height is None or y <= self.viewport_height


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(value)
    return float(value)


def _hit_from_json(raw: object) -> CaptchaHit | None:
    if not isinstance(raw, dict) or raw.get("type") not in CHECKBOX_OFFSETS:
        return None
    try:
        return CaptchaHit(
            type=str(raw["type"]),
            iframe_x=_number(raw["x"]),
            iframe_y=_number(raw["y"]),
            iframe_width=_number(raw["width"]),
            iframe_height=_number(raw["height"]),
            src=str(raw.get("src", "")),
            already_checked=bool(raw.get("already_checked", False)),
            viewport_width=_optional_number(raw.get("viewport_width")),
            viewport_height=_optional_number(raw.get("viewport_height")),
        )
    except (KeyError, TypeError):
        return None


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


async def scan(tab: Tab) -> list[CaptchaHit]:
    """Captcha checkboxes on ``tab`` that still wait for a click."""
    async with asyncio.timeout(SEND_TIMEOUT):
        answer = await tab.evaluate(DETECTION_JS, return_by_value=True)
    if not isinstance(answer, str):
        return []
    try:
        rows = json.loads(answer)
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    hits = (_hit_from_json(row) for row in rows)
    return [
        hit
        for hit in hits
        if hit is not None
        and not hit.already_checked
        and hit.iframe_width > 0
        and hit.iframe_height > 0
        and hit.on_screen()
    ]


async def _send(
    tab: Tab, command: Generator[dict[str, object], dict[str, object], None]
) -> None:
    async with asyncio.timeout(SEND_TIMEOUT):
        await tab.send(command)


async def click_captcha(tab: Tab, hit: CaptchaHit) -> dict[str, object]:
    """Click ``hit``'s checkbox with a trusted mouse move, press and release."""
    base_x, base_y = hit.checkbox_point()
    cx = round(base_x + _RNG.uniform(-JITTER_PX, JITTER_PX), 1)
    cy = round(base_y + _RNG.uniform(-JITTER_PX, JITTER_PX), 1)
    await _send(
        tab,
        cdp.input_.dispatch_mouse_event(
            "mouseMoved",
            x=cx,
            y=cy,
            button=cdp.input_.MouseButton("none"),
            buttons=0,
            click_count=0,
        ),
    )
    await asyncio.sleep(0.05)
    await _send(
        tab,
        cdp.input_.dispatch_mouse_event(
            "mousePressed",
            x=cx,
            y=cy,
            button=cdp.input_.MouseButton("left"),
            buttons=1,
            click_count=1,
        ),
    )
    await asyncio.sleep(0.02)
    await _send(
        tab,
        cdp.input_.dispatch_mouse_event(
            "mouseReleased",
            x=cx,
            y=cy,
            button=cdp.input_.MouseButton("left"),
            buttons=0,
            click_count=1,
        ),
    )
    return {"type": hit.type, "clicked_at": {"x": cx, "y": cy}, "src": hit.src}


def _tab_key(tab: Tab) -> str:
    target = getattr(tab, "target", None)
    return str(getattr(target, "target_id", None) or id(tab))


_MAX_CLICK_LOG = 100


class CaptchaMonitor:
    """Per-instance background scan loop that clicks the captchas it finds."""

    def __init__(self, interval: float = DEFAULT_INTERVAL) -> None:
        self._default_interval = interval
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._intervals: dict[str, float] = {}
        self._click_log: dict[str, list[dict[str, object]]] = {}
        self._dedup: dict[str, set[str]] = {}

    def start(
        self,
        instance_id: str,
        browser_manager: BrowserManager,
        interval: float | None = None,
    ) -> None:
        """Begin (or restart, with a new interval) scanning ``instance_id``."""
        self.stop(instance_id)
        self._intervals[instance_id] = (
            self._default_interval if interval is None else interval
        )
        self._monitors[instance_id] = asyncio.create_task(
            self._loop(instance_id, browser_manager)
        )

    def stop(self, instance_id: str) -> bool:
        """Stop scanning ``instance_id``; the click history stays readable."""
        task = self._monitors.pop(instance_id, None)
        self._intervals.pop(instance_id, None)
        if task is None:
            return False
        task.cancel()
        return True

    def status(self, instance_id: str) -> dict[str, object]:
        return {
            "active": instance_id in self._monitors,
            "clicks": list(self._click_log.get(instance_id, [])),
            "interval": self._intervals.get(instance_id),
        }

    def cleanup(self, instance_id: str) -> None:
        """Stop and forget everything about ``instance_id``."""
        self.stop(instance_id)
        self._dedup.pop(instance_id, None)
        self._click_log.pop(instance_id, None)

    async def _scan_tab(self, instance_id: str, tab: Tab, seen: set[str]) -> bool:
        """Click what ``tab`` shows; False when the tab could not be read or clicked."""
        dedup = self._dedup.setdefault(instance_id, set())
        try:
            hits = await scan(tab)
            for hit in hits:
                key = f"{_tab_key(tab)}:{hit.type}:{hit.src}"
                seen.add(key)
                if key in dedup:
                    continue
                result = await click_captcha(tab, hit)
                dedup.add(key)
                result["timestamp"] = datetime.now(UTC).isoformat()
                log = self._click_log.setdefault(instance_id, [])
                log.append(result)
                del log[: max(0, len(log) - _MAX_CLICK_LOG)]
                debug_logger.log_info(
                    "captcha_clicker", "click", f"clicked {hit.type} captcha"
                )
        except Exception as err:  # noqa: BLE001  PERMANENT(a tab mid-navigation or already closed must not end the monitor)
            debug_logger.log_debug(
                "captcha_clicker", "scan_tab", f"tab skipped: {type(err).__name__}"
            )
            return False
        return True

    async def _scan_tabs(self, instance_id: str, browser: Browser) -> None:
        seen: set[str] = set()
        results = [
            await self._scan_tab(instance_id, tab, seen)
            for tab in cast("list[Tab]", list(browser.tabs))
            if hasattr(tab, "send")
        ]
        if all(results):
            # A widget that is gone, or solved, may come back unsolved later and
            # then deserves a fresh click; one still waiting stays in ``seen``.
            self._dedup.setdefault(instance_id, set()).intersection_update(seen)

    async def _loop(self, instance_id: str, browser_manager: BrowserManager) -> None:
        while True:
            try:
                data = await browser_manager.get_instance(instance_id)
                if not data:
                    self._monitors.pop(instance_id, None)
                    self.cleanup(instance_id)
                    return
                if data.get("browser"):
                    await self._scan_tabs(instance_id, data["browser"])
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001  PERMANENT(a bad scan must not end the monitor)
                debug_logger.log_debug(
                    "captcha_clicker", "loop", f"scan failed: {type(err).__name__}"
                )
            await asyncio.sleep(self._intervals.get(instance_id, MIN_INTERVAL))


monitor = CaptchaMonitor()
