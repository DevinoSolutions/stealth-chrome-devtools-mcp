"""The ``captcha`` tools. See ``tool_sections/__init__.py`` for the contract.

Switches the background captcha-checkbox clicker on and off for one browser
instance. The detection and the click live in ``embedded/captcha_clicker.py``;
this module is only the tool surface.
"""

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError, _require_browser

SECTION = "captcha"


async def enable_captcha_auto_click(
    instance_id: str,
    interval_seconds: float = rt.captcha_clicker.DEFAULT_INTERVAL,
) -> dict[str, object]:
    """
    Enable automatic captcha clicking for a browser instance.

    Starts a background monitor that scans all tabs every `interval_seconds`
    for Cloudflare Turnstile, Google reCAPTCHA v2, and hCaptcha checkboxes,
    and clicks them automatically via a trusted coordinate click. It only
    clicks the checkbox: it does not solve image puzzles or sliders, and the
    captcha provider decides whether the click passes. Enabling an instance
    that is already monitored restarts the monitor with the new interval.

    Args:
        instance_id (str): Browser instance ID.
        interval_seconds (float): Seconds between scans (default 2.0, min 0.5, max 30).

    Returns:
        Dict[str, Any]: enabled (True), instance_id and interval_seconds.
    """
    low, high = rt.captcha_clicker.MIN_INTERVAL, rt.captcha_clicker.MAX_INTERVAL
    if not low <= interval_seconds <= high:
        raise ToolError(
            f"interval_seconds must be between {low} and {high}, got {interval_seconds}"
        )
    await _require_browser(rt.browser_manager, instance_id)
    rt.captcha_clicker.monitor.start(instance_id, rt.browser_manager, interval_seconds)
    return {
        "enabled": True,
        "instance_id": instance_id,
        "interval_seconds": interval_seconds,
    }


async def disable_captcha_auto_click(instance_id: str) -> dict[str, object]:
    """
    Disable automatic captcha clicking for a browser instance.

    The record of clicks already made stays readable through get_captcha_status.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Dict[str, Any]: enabled (False) and instance_id.
    """
    await _require_browser(rt.browser_manager, instance_id)
    rt.captcha_clicker.monitor.stop(instance_id)
    return {"enabled": False, "instance_id": instance_id}


async def get_captcha_status(instance_id: str) -> dict[str, object]:
    """
    Get captcha auto-click status and recent click history for an instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Dict[str, Any]: active, interval_seconds (None when not active), clicks
        (a list of type, clicked_at {x, y}, src and timestamp) and
        supported_types.
    """
    await _require_browser(rt.browser_manager, instance_id)
    state = rt.captcha_clicker.monitor.status(instance_id)
    return {
        "active": state["active"],
        "interval_seconds": state["interval"],
        "clicks": state["clicks"],
        "supported_types": list(rt.captcha_clicker.SUPPORTED_TYPES),
    }


TOOLS = (enable_captcha_auto_click, disable_captcha_auto_click, get_captcha_status)
