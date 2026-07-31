"""The one home for a spawn's requested browser window size (F-804).

``viewport_width`` / ``viewport_height`` on a spawn are a **window** size
request. Two transports carry that one fact, deliberately (DESIGN §5's
"one engine, deliberate per-aspect transport" shape applied to sizing):

* ``--window-size=W,H`` at launch, so the window is *born* the right size —
  before any CDP round-trip, and before the first frame a headed user sees;
* CDP ``Browser.setWindowBounds`` right after launch, which is what corrects a
  window whose bounds Chrome restored from the profile (a restored bound beats
  the launch arg).

Neither transport can *promise* the result. Headed Chrome clamps a window to the
desktop work area, so a 1920x1080 request on a 1024x768 desktop lands at about
1044x788 — while headless, having no window manager, honours the request
exactly. That asymmetry is F-804: before this module the spawn result echoed the
*request* back as though it had been applied, so a clamped headed window
reported a size it never had.

So this module also **measures**, and the measurement — never the request — is
what spawn reports. :func:`apply_and_measure` returns both, plus a ``clamped``
flag, so a caller can see at a glance that the OS, not the tool, chose the size.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nodriver import Tab
    from nodriver.cdp.browser import Bounds

    from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions

WINDOW_SIZE_ARG_PREFIX = "--window-size="

_INNER_VIEWPORT_PROBE = (
    "JSON.stringify({width: window.innerWidth, height: window.innerHeight})"
)


class WindowSizeMetrics(TypedDict):
    """What was asked for, what Chrome gave, and whether those differ."""

    requested: dict[str, int]
    actual: dict[str, int] | None
    inner_viewport: dict[str, int] | None
    measured: bool
    clamped: bool | None


def append_size_arg(args: list[str], options: BrowserOptions) -> list[str]:
    """Append ``--window-size=W,H`` for *options* unless the caller chose one.

    An explicit caller ``--window-size=...`` in ``browser_args`` wins: the
    presence check is what makes it win, mirroring how an explicit
    ``--user-agent=`` beats the masked default in ``platform_utils``.
    """
    if any(arg.lower().startswith(WINDOW_SIZE_ARG_PREFIX) for arg in args):
        return args
    size = f"{options.viewport_width},{options.viewport_height}"
    return [*args, f"{WINDOW_SIZE_ARG_PREFIX}{size}"]


def _bounds_size(bounds: Bounds) -> dict[str, int] | None:
    """Read ``width``/``height`` off a CDP ``Browser.Bounds``, or ``None``.

    Chrome omits the size fields for a minimized window, so a partially
    populated Bounds must read as "not measured" rather than as ``0x0``.
    """
    width = getattr(bounds, "width", None)
    height = getattr(bounds, "height", None)
    if width is None or height is None:
        return None
    return {"width": int(width), "height": int(height)}


def _parse_inner_viewport(raw: object) -> dict[str, int] | None:
    """Parse the JSON the inner-viewport probe returns, tolerating anything else."""
    if not isinstance(raw, str):
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return None
    return {"width": int(parsed["width"]), "height": int(parsed["height"])}


async def _measure(tab: Tab) -> tuple[dict[str, int] | None, dict[str, int] | None]:
    """Return ``(outer_window_size, inner_viewport_size)``; either may be None."""
    _window_id, bounds = await tab.get_window()
    return _bounds_size(bounds), _parse_inner_viewport(
        await tab.evaluate(_INNER_VIEWPORT_PROBE)
    )


async def apply_and_measure(tab: Tab, options: BrowserOptions) -> WindowSizeMetrics:
    """Apply the requested window size, then report what Chrome actually did.

    Applying is *not* guarded — a window that cannot be sized is a failed spawn,
    as it has always been. Measuring is: a spawn must not fail because a
    diagnostic probe did, so a measurement failure degrades to
    ``measured: False`` (leaving the caller reporting the request, then the only
    size known) rather than taking the browser down with it.
    """
    await tab.set_window_size(
        left=0,
        top=0,
        width=options.viewport_width,
        height=options.viewport_height,
    )
    requested = {"width": options.viewport_width, "height": options.viewport_height}
    metrics: WindowSizeMetrics = {
        "requested": requested,
        "actual": None,
        "inner_viewport": None,
        "measured": False,
        "clamped": None,
    }
    try:
        actual, inner = await _measure(tab)
    except Exception as error:  # noqa: BLE001  RELEASE-FIX-D (F-804)
        debug_logger.log_warning(
            "window_sizing",
            "apply_and_measure",
            f"window size measurement failed ({type(error).__name__}: {error}); "
            f"reporting the requested {requested['width']}x{requested['height']} "
            "as unverified",
        )
        return metrics

    metrics["actual"] = actual
    metrics["inner_viewport"] = inner
    metrics["measured"] = actual is not None
    if actual is not None:
        metrics["clamped"] = actual != requested
    debug_logger.log_info(
        "window_sizing",
        "apply_and_measure",
        f"window size requested {requested['width']}x{requested['height']}, "
        f"actual {actual}",
    )
    return metrics
