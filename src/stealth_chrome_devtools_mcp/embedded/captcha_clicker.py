"""Automatic captcha checkbox clicker.

Detects Cloudflare Turnstile, Google reCAPTCHA v2, and hCaptcha checkbox frames
on the page, and clicks their checkboxes via ``Input.dispatchMouseEvent`` -- a
trusted coordinate click, the same as a human. Does NOT solve challenges (image
puzzles, sliders): the click initiates the check, and the captcha provider
decides whether to pass or escalate based on fingerprint and reputation.

**How a widget is found.** Through the browser, not the page. A provider widget
is a cross-origin iframe, so Chrome lists it as an ``iframe`` target with its
parent frame (``Target.getTargets``; an in-process frame comes from
``Page.getFrameTree`` instead). Its real URL says which provider it is, and
``DOM.getFrameOwner`` + ``DOM.getBoxModel`` say where its ``<iframe>`` element
sits in the viewport. That works where a page script cannot: real Turnstile
puts its iframe in a CLOSED shadow root, which ``querySelectorAll`` never sees
(2.1.60 detected only through the page and so never clicked a real Turnstile).
Neither call resets the session's node ids, unlike ``DOM.getDocument`` (F-884).

Only widgets whose iframe is a direct child of the tab's top document are
clicked: a box model of an element inside another frame is in that frame's
coordinates, so a click computed from it would land somewhere else.

**How fast.** A scan is a handful of CDP calls of 1-30 ms in all (measured), so
the default interval is ``DEFAULT_INTERVAL`` (0.25 s), and a widget that is not
due yet does not wait for the next scan: the loop sleeps only until the earliest
one is due. ``tests/test_captcha_integration.py`` measures, against each
provider's real widget, how long after the checkbox became clickable the click
that counted came, and fails above its ``CLICK_BUDGET_S``.

**When it clicks.** Nothing outside a widget's frame says when its checkbox
starts taking clicks: an iframe target's title stays its URL, and a
cross-origin frame's ``load`` is not the parent's event. Measured instead
(Windows, Chrome 157, the providers' test keys): the monitor first sees a
reCAPTCHA frame up to 274 ms before its ``load``; ONE click 47-194 ms after
``load``, or 158 ms before it, passed every time, while one 341 ms before it
did not. So a reCAPTCHA frame is first clicked once it is ``FIRST_CLICK_AGE``
old. hCaptcha drops a click that comes too early as well (measured: 18 ms
before its frame's ``load`` gave no token, 19 ms after it passed), so its first
click waits for a 0.15 s-old frame; Turnstile takes the first click at once.

reCAPTCHA then checks with Google for 1.9-4.1 s (measured) before its token
arrives, so a widget is clicked AGAIN only when it is still unsolved
``RETRY_AFTER`` seconds after the last click, longer than that check, and at
most ``MAX_ATTEMPTS`` times. Turnstile retries sooner: a real one can show
"Verifying..." before its checkbox, and a click during that does nothing.
Nothing is clicked on a tab while a reCAPTCHA or hCaptcha puzzle is showing:
the click worked, and a second one could land on the puzzle.

**Where it clicks.** A trusted click lands on whatever is on top at that
point, so before the press ``DOM.getNodeForLocation`` must answer the widget's
own ``<iframe>`` (or, for an in-process frame, a node inside it). It takes
DOCUMENT coordinates, the viewport point plus the scroll (measured: at the
viewport point of a scrolled page it found nothing). A widget under a sticky
header, a cookie banner or a modal is left alone until it is clear. The
checkbox offset scales with a CSS ``transform: scale()`` on the widget, read
off its box's quad against its layout width.

Tabs are scanned concurrently, each in a task of its own, and a sweep waits
for them at most ``SWEEP_WAIT`` seconds: a tab whose renderer is stuck is not
scanned again until its last scan ends, and holds up no other tab.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar, cast
from urllib.parse import parse_qs, urlsplit

from nodriver import Browser, Tab, cdp
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from collections.abc import Generator

    from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

SUPPORTED_TYPES = ("turnstile", "recaptcha-v2", "hcaptcha")

#: A provider frame that is a puzzle shown AFTER a checkbox click, not a checkbox.
CHALLENGE = "challenge"

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

#: The most one sweep waits for its tabs' scans; a scan still running then
#: finishes in the background, and its tab is skipped until it does.
SWEEP_WAIT = 1.0

#: How old a widget's frame must be before its FIRST click (module docstring).
FIRST_CLICK_AGE: dict[str, float] = {
    "turnstile": 0.0,
    "recaptcha-v2": 0.3,
    "hcaptcha": 0.15,
}

#: A widget still unsolved this long after a click is clicked again...
RETRY_AFTER: dict[str, float] = {
    "turnstile": 1.5,
    "recaptcha-v2": 6.0,
    "hcaptcha": 6.0,
}
#: ...but never more than this many times in all.
MAX_ATTEMPTS = 5

_RNG = random.SystemRandom()

_T = TypeVar("_T")

MIN_INTERVAL = 0.1
MAX_INTERVAL = 30.0
DEFAULT_INTERVAL = 0.25

_RECAPTCHA_HOSTS = frozenset(
    {"www.google.com", "google.com", "www.recaptcha.net", "recaptcha.net"}
)
_RECAPTCHA_PATHS = {
    "/recaptcha/api2/anchor": "recaptcha-v2",
    "/recaptcha/enterprise/anchor": "recaptcha-v2",
    "/recaptcha/api2/bframe": CHALLENGE,
    "/recaptcha/enterprise/bframe": CHALLENGE,
}
_HCAPTCHA_FRAMES = {"checkbox": "hcaptcha", "challenge": CHALLENGE}

#: The object group every remote object a scan creates goes into, released at
#: the end of each scan so the page's heap does not grow with the monitor.
_OBJECT_GROUP = "stealth-captcha-scan"

#: Called ON the widget's ``<iframe>`` element: does its widget hold a response
#: token yet? Climbs out of shadow roots (Turnstile's is closed, but the element
#: handed in by ``DOM.resolveNode`` is already inside it) and then up a few
#: ancestors to the first one holding a response field, which every provider
#: renders next to its iframe.
SOLVED_JS = r"""
function () {
  let node = this;
  for (let root = node.getRootNode(); root && root.host; root = node.getRootNode()) {
    node = root.host;
  }
  const selector = 'input[name*="response"], textarea[name*="response"]';
  for (let scope = node.parentElement, depth = 0; scope && depth < 5;
       scope = scope.parentElement, depth += 1) {
    const fields = scope.querySelectorAll(selector);
    if (fields.length) {
      return Array.from(fields).some((f) => (f.value || '').length > 0);
    }
  }
  return false;
}
"""


def classify(url: str) -> str | None:
    """What a frame loaded from ``url`` is: a ``SUPPORTED_TYPES`` checkbox,
    ``CHALLENGE`` (a puzzle on screen after a click), or ``None``.

    Decided by the frame's REAL URL, which the page cannot fake: an attacker's
    iframe in a ``<div class="cf-turnstile">`` is ``None`` here.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        return None
    host = (parts.hostname or "").lower()
    if host == "challenges.cloudflare.com":
        return "turnstile" if "/turnstile/" in parts.path else None
    if host in _RECAPTCHA_HOSTS:
        kind = _RECAPTCHA_PATHS.get(parts.path)
        if kind == "recaptcha-v2" and parse_qs(parts.query).get("size") == [
            "invisible"
        ]:
            return None  # the badge of an invisible reCAPTCHA has no checkbox
        return kind
    if host == "hcaptcha.com" or host.endswith(".hcaptcha.com"):
        (frame, *_) = parse_qs(parts.fragment).get("frame", [""])
        return _HCAPTCHA_FRAMES.get(frame)
    return None


@dataclass(frozen=True)
class CaptchaHit:
    type: str
    frame_id: str
    iframe_x: float
    iframe_y: float
    iframe_width: float
    iframe_height: float
    src: str
    already_checked: bool
    viewport_width: float | None = None
    viewport_height: float | None = None
    #: The ``<iframe>`` element, which a hit test at the checkbox must answer.
    owner_node_id: int | None = None
    #: The page's scroll (``cssVisualViewport.pageX/Y``): viewport + scroll is
    #: the document point ``DOM.getNodeForLocation`` takes.
    scroll_x: float = 0.0
    scroll_y: float = 0.0
    #: A CSS ``transform: scale()`` on the widget: its box on screen over its
    #: layout width. The checkbox offsets are in the widget's own pixels.
    scale: float = 1.0

    def checkbox_point(self) -> tuple[float, float]:
        offset_x, offset_y = CHECKBOX_OFFSETS[self.type]
        return (
            self.iframe_x + offset_x * self.scale,
            self.iframe_y + offset_y * self.scale,
        )

    def on_screen(self) -> bool:
        """Whether the checkbox is inside the viewport: a mouse event outside it
        reaches nothing, so such a widget is left until the page scrolls to it."""
        if self.iframe_width <= 0 or self.iframe_height <= 0:
            return False
        x, y = self.checkbox_point()
        if x < 0 or y < 0:
            return False
        if self.viewport_width is not None and x > self.viewport_width:
            return False
        return self.viewport_height is None or y <= self.viewport_height

    def visible(self) -> bool:
        """Whether any of the frame is on screen (how a shown puzzle is told
        from the parked one each provider keeps at ``top: -10000px``)."""
        if self.iframe_width <= 1 or self.iframe_height <= 1:
            return False
        if (
            self.iframe_x + self.iframe_width <= 0
            or self.iframe_y + self.iframe_height <= 0
        ):
            return False
        if self.viewport_width is not None and self.iframe_x >= self.viewport_width:
            return False
        return self.viewport_height is None or self.iframe_y < self.viewport_height


@dataclass
class ScanResult:
    """What one scan of a tab saw: the checkboxes, and whether a puzzle is up."""

    hits: list[CaptchaHit] = field(default_factory=list)
    challenge_showing: bool = False
    #: Provider frames listed but not measurable this time (hidden, detached):
    #: still there, so the monitor keeps what it knows of them.
    unmeasured: list[str] = field(default_factory=list)

    def clickable(self) -> list[CaptchaHit]:
        return [hit for hit in self.hits if not hit.already_checked and hit.on_screen()]


async def _send(
    tab: Tab, command: Generator[dict[str, object], dict[str, object], _T]
) -> _T:
    async with asyncio.timeout(SEND_TIMEOUT):
        return await tab.send(command)


async def _provider_frames(tab: Tab) -> list[tuple[str, str, str]]:
    """``(kind, frame_id, url)`` of every provider frame that is a direct child
    of ``tab``'s top document."""
    tree = await _send(tab, cdp.page.get_frame_tree())
    main = str(tree.frame.id_)
    urls: dict[str, str] = {}
    for child in tree.child_frames or []:
        urls[str(child.frame.id_)] = child.frame.url + (child.frame.url_fragment or "")
    for info in await _send(tab, cdp.target.get_targets()):
        if info.type_ == "iframe" and str(info.parent_frame_id) == main:
            urls[str(info.target_id)] = info.url
    return [
        (kind, frame_id, url)
        for frame_id, url in urls.items()
        if (kind := classify(url)) is not None
    ]


async def _solved(tab: Tab, backend_node_id: cdp.dom.BackendNodeId) -> bool:
    remote = await _send(
        tab,
        cdp.dom.resolve_node(
            backend_node_id=backend_node_id, object_group=_OBJECT_GROUP
        ),
    )
    if remote.object_id is None:
        return False
    answer, thrown = await _send(
        tab,
        cdp.runtime.call_function_on(
            SOLVED_JS,
            object_id=remote.object_id,
            return_by_value=True,
            silent=True,
        ),
    )
    return thrown is None and answer.value is True


async def scan(tab: Tab) -> ScanResult:
    """Every provider frame on ``tab``: its checkboxes and whether a puzzle shows."""
    frames = await _provider_frames(tab)
    result = ScanResult()
    if not frames:
        return result
    metrics = await _send(tab, cdp.page.get_layout_metrics())
    viewport = metrics[4]  # cssVisualViewport: CSS px, as the box model is
    try:
        for kind, frame_id, url in frames:
            try:
                backend_node_id, _ = await _send(
                    tab, cdp.dom.get_frame_owner(cdp.page.FrameId(frame_id))
                )
                box = await _send(
                    tab, cdp.dom.get_box_model(backend_node_id=backend_node_id)
                )
            except ProtocolException:
                # Detached since the listing, or not laid out yet (Chrome:
                # "Could not compute box model"): this frame only, this scan only.
                if kind != CHALLENGE:
                    result.unmeasured.append(frame_id)
                continue
            xs, ys = box.content[0::2], box.content[1::2]
            width, height = max(xs) - min(xs), max(ys) - min(ys)
            hit = CaptchaHit(
                type=kind,
                frame_id=frame_id,
                iframe_x=min(xs),
                iframe_y=min(ys),
                iframe_width=width,
                iframe_height=height,
                src=url,
                already_checked=False,
                viewport_width=viewport.client_width,
                viewport_height=viewport.client_height,
                owner_node_id=int(backend_node_id),
                scroll_x=viewport.page_x,
                scroll_y=viewport.page_y,
                scale=width / box.width if box.width > 0 and width > 0 else 1.0,
            )
            if kind == CHALLENGE:
                result.challenge_showing = result.challenge_showing or hit.visible()
                continue
            if hit.on_screen() and await _solved(tab, backend_node_id):
                hit = replace(hit, already_checked=True)
            result.hits.append(hit)
    finally:
        await _send(tab, cdp.runtime.release_object_group(_OBJECT_GROUP))
    return result


async def aim(tab: Tab, hit: CaptchaHit) -> tuple[float, float] | None:
    """Where to click ``hit``'s checkbox (with jitter), or None when something
    else is on top there and a click would land on it instead."""
    base_x, base_y = hit.checkbox_point()
    cx = round(base_x + _RNG.uniform(-JITTER_PX, JITTER_PX), 1)
    cy = round(base_y + _RNG.uniform(-JITTER_PX, JITTER_PX), 1)
    try:
        node, frame_id, _ = await _send(
            tab,
            cdp.dom.get_node_for_location(
                x=round(cx + hit.scroll_x), y=round(cy + hit.scroll_y)
            ),
        )
    except ProtocolException:
        return None  # "No node found at given location"
    if int(node) == hit.owner_node_id or str(frame_id) == hit.frame_id:
        return cx, cy
    return None


async def press(tab: Tab, cx: float, cy: float) -> None:
    """A trusted mouse move, press and release at ``(cx, cy)``. The release is
    sent even when the press fails or is cancelled, so the tab is never left
    with its left button held down."""
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
    try:
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
    finally:
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


def _entry(hit: CaptchaHit, cx: float, cy: float) -> dict[str, object]:
    return {"type": hit.type, "clicked_at": {"x": cx, "y": cy}, "src": hit.src}


async def click_captcha(tab: Tab, hit: CaptchaHit) -> dict[str, object] | None:
    """Click ``hit``'s checkbox, or return None (and send nothing but the hit
    test) when something else covers it."""
    point = await aim(tab, hit)
    if point is None:
        return None
    await press(tab, *point)
    return _entry(hit, *point)


def _tab_key(tab: Tab) -> str:
    target = getattr(tab, "target", None)
    return str(getattr(target, "target_id", None) or id(tab))


_MAX_CLICK_LOG = 100

#: Chrome's answer to a command for a target that no longer exists ("Session
#: with given id not found"), measured on a tab nodriver kept after its close.
_SESSION_GONE = -32001


@dataclass
class _Widget:
    """What the monitor knows of one widget (one frame of one tab)."""

    first_seen: float  # time.monotonic()
    clicks: int = 0
    last_click: float = 0.0  # time.monotonic() of the latest

    def due_at(self, kind: str) -> float | None:
        """When (``time.monotonic()``) this widget may next be clicked; None
        once it has had ``MAX_ATTEMPTS``."""
        if self.clicks == 0:
            return self.first_seen + FIRST_CLICK_AGE[kind]
        if self.clicks >= MAX_ATTEMPTS:
            return None
        return self.last_click + RETRY_AFTER[kind]


class CaptchaMonitor:
    """Per-instance background scan loop that clicks the captchas it finds."""

    def __init__(self, interval: float = DEFAULT_INTERVAL) -> None:
        self._default_interval = interval
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._intervals: dict[str, float] = {}
        self._click_log: dict[str, list[dict[str, object]]] = {}
        #: Per instance, per ``(tab key, frame id)``.
        self._widgets: dict[str, dict[tuple[str, str], _Widget]] = {}
        #: Per instance: the earliest ``due_at`` among widgets the last sweep
        #: left unclicked, so the loop wakes for it instead of a whole interval.
        self._next_due: dict[str, float] = {}
        #: Per instance, per tab key: the scan of that tab still running.
        self._scans: dict[str, dict[str, asyncio.Task[set[str] | None]]] = {}

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
        for running in self._scans.pop(instance_id, {}).values():
            running.cancel()
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
        self._widgets.pop(instance_id, None)
        self._next_due.pop(instance_id, None)
        self._click_log.pop(instance_id, None)

    async def _scan_tab(self, instance_id: str, tab: Tab) -> set[str] | None:
        """Click what ``tab`` shows. Answers the frame ids of the widgets it
        saw, or None when the tab could not be read or clicked."""
        tab_key = _tab_key(tab)
        widgets = self._widgets.setdefault(instance_id, {})
        try:
            result = await scan(tab)
            now = time.monotonic()
            seen = set(result.unmeasured)
            for hit in result.hits:
                seen.add(hit.frame_id)
                widgets.setdefault((tab_key, hit.frame_id), _Widget(first_seen=now))
            if result.challenge_showing:
                return seen  # the click worked; a second one could hit the puzzle
            for hit in result.clickable():
                widget = widgets[(tab_key, hit.frame_id)]
                due = widget.due_at(hit.type)
                if due is None:
                    continue
                if due > time.monotonic():
                    self._next_due[instance_id] = min(
                        due, self._next_due.get(instance_id, due)
                    )
                    continue
                point = await aim(tab, hit)
                if point is None:
                    continue  # covered: looked at again next sweep, unclicked
                # Counted BEFORE the press: one that fails halfway used an attempt.
                widget.clicks += 1
                widget.last_click = time.monotonic()
                await press(tab, *point)
                entry = _entry(hit, *point)
                entry["attempt"] = widget.clicks
                entry["timestamp"] = datetime.now(UTC).isoformat()
                log = self._click_log.setdefault(instance_id, [])
                log.append(entry)
                del log[: max(0, len(log) - _MAX_CLICK_LOG)]
                debug_logger.log_info(
                    "captcha_clicker",
                    "click",
                    f"clicked {hit.type} captcha (attempt {widget.clicks})",
                )
        except ProtocolException as err:
            if err.code == _SESSION_GONE:
                # nodriver still lists a tab whose target is closed: it holds
                # no widget, which is an answer, not a failure.
                return set()
            debug_logger.log_debug(
                "captcha_clicker", "scan_tab", f"tab skipped: {err.code}"
            )
            return None
        except Exception as err:  # noqa: BLE001  PERMANENT(a tab mid-navigation or already closed must not end the monitor)
            debug_logger.log_debug(
                "captcha_clicker", "scan_tab", f"tab skipped: {type(err).__name__}"
            )
            return None
        return seen

    async def _scan_tabs(self, instance_id: str, browser: Browser) -> None:
        running = self._scans.setdefault(instance_id, {})
        tabs = {
            _tab_key(tab): tab
            for tab in cast("list[Tab]", list(browser.tabs))
            if hasattr(tab, "send")
        }
        for gone in set(running) - set(tabs):
            running.pop(gone).cancel()
        started = []
        for key, tab in tabs.items():
            if key in running:
                continue  # its last scan has not ended yet
            running[key] = asyncio.create_task(self._scan_tab(instance_id, tab))
            started.append(running[key])
        if started:
            await asyncio.wait(started, timeout=SWEEP_WAIT)
        answered: dict[str, set[str]] = {}
        for key, task in list(running.items()):
            if task.done():
                del running[key]
                if not task.cancelled() and (seen := task.result()) is not None:
                    answered[key] = seen
        # A widget that is gone may come back (a reset re-renders it in a NEW
        # frame, so it is a new key anyway). Forget it once its tab answered
        # without it, or its tab is gone; a tab that failed forgets nothing.
        widgets = self._widgets.setdefault(instance_id, {})
        for tab_key, frame_id in list(widgets):
            if tab_key not in tabs or (
                tab_key in answered and frame_id not in answered[tab_key]
            ):
                del widgets[(tab_key, frame_id)]

    async def _loop(self, instance_id: str, browser_manager: BrowserManager) -> None:
        while True:
            try:
                data = await browser_manager.get_instance(instance_id)
                if not data:
                    self._monitors.pop(instance_id, None)
                    self.cleanup(instance_id)
                    return
                self._next_due.pop(instance_id, None)
                if data.get("browser"):
                    await self._scan_tabs(instance_id, data["browser"])
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001  PERMANENT(a bad scan must not end the monitor)
                debug_logger.log_debug(
                    "captcha_clicker", "loop", f"scan failed: {type(err).__name__}"
                )
            await asyncio.sleep(self._pause(instance_id))

    def _pause(self, instance_id: str) -> float:
        """Until the next sweep: the interval, or less when a widget is due sooner."""
        interval = self._intervals.get(instance_id, MIN_INTERVAL)
        due = self._next_due.get(instance_id)
        if due is None:
            return interval
        return min(interval, max(0.0, due - time.monotonic()))


monitor = CaptchaMonitor()
