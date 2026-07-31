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
* :func:`_require_js_value` / :func:`_require_navigation_ok` — the two
  *truthfulness* guards (F-795, F-802). Both turn an operation that FAILED at
  the browser into a raised :class:`ToolError` instead of a payload whose
  ``success`` says it worked. They are duck-typed rather than typed against the
  nodriver CDP classes, which is what keeps this module a dependency-free leaf.
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


#: Every Chrome navigation-failure page (DNS miss, refused connection, TLS
#: failure, …) commits to a URL under this scheme. A page that merely answered
#: with an HTTP error status (404/500) does NOT — it keeps its own URL.
CHROME_ERROR_SCHEME = "chrome-error://"


def _require_js_value(value: object) -> object:
    """Return an evaluated script's value, or raise if the page threw (F-795).

    ``nodriver``'s ``Tab.evaluate`` does not raise when the evaluated script
    throws: it RETURNS the CDP ``Runtime.ExceptionDetails`` record *in the
    value's place*. A caller that trusts the value therefore reports a thrown
    script as a working one. This is the ONE place that converts that record
    into the error convention; every eval path that wants the truth calls it
    rather than growing its own ``hasattr`` check.

    Duck-typed on ``exception_id`` + ``text`` (the two fields every
    ``ExceptionDetails`` carries and no ordinary JS value does) so this module
    keeps importing nothing.
    """
    if not (hasattr(value, "exception_id") and hasattr(value, "text")):
        return value
    exception = getattr(value, "exception", None)
    detail = getattr(exception, "description", None) or getattr(value, "text", None)
    raise ToolError(f"Script raised an exception: {detail}")


def _require_navigation_ok(url: str, result: object) -> object:
    """Return a navigation result, or raise if Chrome landed on an error page
    (F-802).

    A navigation that Chrome could not perform — the host does not resolve, the
    connection is refused, the TLS handshake fails — still *completes*: Chrome
    commits an error page and the tool's own bookkeeping succeeds, so the
    payload used to say ``success: true`` with a ``chrome-error://`` URL.

    Only a Chrome-level navigation failure is one: a page answering 404/500, a
    redirect to a different final URL, ``about:blank`` and ``data:`` URLs all
    keep their own scheme and pass through untouched.
    """
    final_url = result.get("url") if isinstance(result, dict) else None
    if isinstance(final_url, str) and final_url.startswith(CHROME_ERROR_SCHEME):
        raise ToolError(
            f"Navigation to {url} failed: Chrome loaded an error page "
            f"({final_url}). The host may not resolve, the connection may have "
            "been refused, or the TLS handshake may have failed."
        )
    return result
