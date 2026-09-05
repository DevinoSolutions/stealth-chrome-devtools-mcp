"""The ``tabs`` tools. See ``tool_sections/__init__.py`` for the contract.

plan_SERVERSPLIT slice 2. This section is the first to use ``_require_browser``
and ``_require_landing_ok``, and the first to read a tuned knob DIRECTLY rather
than through ``_with_cdp_timeout``'s default — ``new_tab`` hands
``rt.CDP_OPERATION_TIMEOUT`` to ``_require_landing_ok``. That read is the point
of the slice: it is resolved against ``tool_runtime`` at CALL time, so
``tests/conftest.py``'s ``patched_server(CDP_OPERATION_TIMEOUT=…)`` still reaches
a body that no longer lives in ``server.py``.

Bodies moved verbatim: the only edits are the dropped registration decorator and
the rewrite of the singleton/knob reads to ``rt.<name>``. Docstrings and
signatures are byte-identical — FastMCP surfaces them and
``tests/goldens/tool_surface.json`` is a HARD golden for this migration.
"""

from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    ToolError,
    _require_browser,
    _require_landing_ok,
)

SECTION = "tabs"


async def list_tabs(instance_id: str) -> list[dict[str, str]]:
    """
    List all tabs for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        List[Dict[str, str]]: List of tabs with their details.
    """
    return await rt._with_cdp_timeout(
        rt.browser_manager.list_tabs(instance_id), instance_id=instance_id
    )


async def switch_tab(instance_id: str, tab_id: str) -> bool:
    """
    Switch to a specific tab by bringing it to front.

    Args:
        instance_id (str): Browser instance ID.
        tab_id (str): Target tab ID to switch to.

    Returns:
        bool: True if switched successfully.
    """
    return await rt._with_cdp_timeout(
        rt.browser_manager.switch_to_tab(instance_id, tab_id), instance_id=instance_id
    )


async def close_tab(instance_id: str, tab_id: str) -> bool:
    """
    Close a specific tab.

    Args:
        instance_id (str): Browser instance ID.
        tab_id (str): Tab ID to close.

    Returns:
        bool: True if closed successfully.
    """
    return await rt._with_cdp_timeout(
        rt.browser_manager.close_tab(instance_id, tab_id), instance_id=instance_id
    )


async def get_active_tab(instance_id: str) -> dict[str, Any]:
    """
    Get information about the currently active tab.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Dict[str, Any]: Active tab information.
    """
    tab = await rt._with_cdp_timeout(
        rt.browser_manager.get_active_tab(instance_id), instance_id=instance_id
    )
    if not tab:
        raise ToolError("No active tab found")
    await rt._with_cdp_timeout(tab, instance_id=instance_id)
    return {
        "tab_id": str(tab.target.target_id),
        "url": getattr(tab, "url", "") or "",
        "title": getattr(tab.target, "title", "") or "Untitled",
        "type": getattr(tab.target, "type_", "page"),
    }


async def new_tab(instance_id: str, url: str = "about:blank") -> dict[str, Any]:
    """
    Open a new tab in the browser instance.

    Args:
        instance_id (str): Browser instance ID.
        url (str): URL to open in the new tab.

    Returns:
        Dict[str, Any]: New tab information. A Chrome error page raises (F-833).
    """
    browser = await _require_browser(rt.browser_manager, instance_id)
    try:
        tab = await rt._with_cdp_timeout(
            browser.get(url, new_tab=True), instance_id=instance_id
        )
        await _require_landing_ok(
            tab, url, rt.CDP_OPERATION_TIMEOUT, close_on_error=True
        )
        return {
            "tab_id": str(tab.target.target_id),
            "url": getattr(tab, "url", "") or url,
            "title": getattr(tab.target, "title", "") or "New Tab",
            "type": getattr(tab.target, "type_", "page"),
        }
    except Exception as e:
        raise ToolError(f"Failed to create new tab: {e!s}")


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in ``SECTION_TOOLS["tabs"]``.
TOOLS = (list_tabs, switch_tab, close_tab, get_active_tab, new_tab)
