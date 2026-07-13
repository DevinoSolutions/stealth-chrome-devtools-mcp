"""The one error convention for the MCP tool surface (F-104/F-761/F-746).

Tools report failure by RAISING a typed error, not by hand-rolling a
``{"success": False, "error": ...}`` dict or a ``json.dumps({"error": ...})``
string per tool. FastMCP surfaces a raised exception to the client as the tool's
error, so raising is the majority convention already in the tree (~40 sites) —
this module makes it the ONLY one (ADDENDUM conventions lens: one way, no second
way). The former dict/json instance-not-found shapes are converted to raises.

* :class:`ToolError` — base for any tool failure surfaced to the client.
* :class:`InstanceNotFoundError` — the single instance-not-found shape
  (message ``Instance not found: {instance_id}``).
* :func:`_require_tab` / :func:`_require_browser` — the single guard replacing
  the ~40 hand-rolled ``if not tab: raise`` / dict / json instance-not-found
  sites. They take ``browser_manager`` as an argument rather than importing it:
  the singleton lives in ``server.py`` and NO embedded module may import
  ``server`` (that would re-arm the runpy double-registration hazard the
  package is built to avoid — see ``embedded/__init__.py``). This keeps
  ``tool_errors`` a dependency-free leaf and the guards trivially hermetic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nodriver import Browser, Tab

    from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager


class ToolError(Exception):
    """A tool failure surfaced to the MCP client as a raised error — the one
    error-report convention, replacing the former per-tool result/error dicts."""


class InstanceNotFoundError(ToolError):
    """Raised when an ``instance_id`` names no live browser instance. The single
    instance-not-found shape (``Instance not found: {instance_id}``)."""


async def _require_tab(browser_manager: BrowserManager, instance_id: str) -> Tab:
    """Return the instance's main tab, or raise on a miss.

    The one guard collapsing the ~40 ``tab = await browser_manager.get_tab(id);
    if not tab: raise/return`` sites into a single call and a single shape.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise InstanceNotFoundError(f"Instance not found: {instance_id}")
    return tab


async def _require_browser(
    browser_manager: BrowserManager, instance_id: str
) -> Browser:
    """Return the instance's browser object, or raise ``InstanceNotFoundError``
    on a miss — the ``_require_tab`` counterpart for browser-level tools."""
    browser = await browser_manager.get_browser(instance_id)
    if not browser:
        raise InstanceNotFoundError(f"Instance not found: {instance_id}")
    return browser
