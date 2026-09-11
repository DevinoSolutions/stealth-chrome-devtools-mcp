"""The ``cookies-storage`` tools. See ``tool_sections/__init__.py`` for the contract.

plan_SERVERSPLIT slice 1 — the first section to leave ``server.py``. It is the
smallest one that still exercises the whole shared-dependency set a tool body
reaches for (``rt.browser_manager``, ``rt.network_interceptor``,
``rt._with_cdp_timeout``, ``_require_tab`` and ``ToolError``), so the mechanism
is proven on three bodies before it is trusted with four hundred lines.

Bodies moved verbatim from ``server.py``: the only edits are the dropped
registration decorator (contract rule 2 — registration is driven from
``server.py``'s binding loop, once per execution of that module body) and the
rewrite of the singleton/helper reads to ``rt.<name>``, resolved at CALL time
against the one patchable home (contract rule 3). Docstrings and signatures are
byte-identical: FastMCP surfaces them, and ``tests/goldens/tool_surface.json``
is a HARD golden for this migration.
"""

from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError, _require_tab

SECTION = "cookies-storage"


async def get_cookies(
    instance_id: str, urls: list[str] | None = None
) -> list[dict[str, Any]]:
    """
    Get cookies for current page or specific URLs.

    Args:
        instance_id (str): Browser instance ID.
        urls (Optional[List[str]]): Optional list of URLs to get cookies for.

    Returns:
        List[Dict[str, Any]]: List of cookies.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.network_interceptor.get_cookies(tab, urls), instance_id=instance_id
    )


async def set_cookie(
    instance_id: str,
    name: str,
    value: str,
    url: str | None = None,
    domain: str | None = None,
    path: str = "/",
    secure: bool = False,
    http_only: bool = False,
    same_site: str | None = None,
) -> bool:
    """
    Set a cookie.

    Args:
        instance_id (str): Browser instance ID.
        name (str): Cookie name.
        value (str): Cookie value.
        url (Optional[str]): The request-URI to associate with the cookie.
        domain (Optional[str]): Cookie domain.
        path (str): Cookie path.
        secure (bool): Secure flag.
        http_only (bool): HttpOnly flag.
        same_site (Optional[str]): SameSite — 'Strict', 'Lax' or 'None' (any case).

    Returns:
        bool: True if set successfully.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)

    if not url and not domain:
        current_url = tab.url if hasattr(tab, "url") else None
        if current_url:
            url = current_url
        else:
            raise ToolError("At least one of 'url' or 'domain' must be specified")

    cookie = {
        "name": name,
        "value": value,
        "path": path,
        "secure": secure,
        "http_only": http_only,
    }
    if url:
        cookie["url"] = url
    if domain:
        cookie["domain"] = domain
    if same_site:
        cookie["same_site"] = same_site
    return await rt._with_cdp_timeout(
        rt.network_interceptor.set_cookie(tab, cookie), instance_id=instance_id
    )


async def clear_cookies(instance_id: str, url: str | None = None) -> bool:
    """
    Clear cookies.

    Args:
        instance_id (str): Browser instance ID.
        url (Optional[str]): Optional URL to clear cookies for (clears all if not specified).

    Returns:
        bool: True if cleared successfully.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.network_interceptor.clear_cookies(tab, url), instance_id=instance_id
    )


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in ``SECTION_TOOLS["cookies-storage"]``.
TOOLS = (get_cookies, set_cookie, clear_cookies)
