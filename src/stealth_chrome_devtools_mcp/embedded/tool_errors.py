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
* :func:`_require_landing_ok` — the ONE entry point the five tab-moving tools
  call (F-833). It absorbs the only thing those five differ in — whether the
  landed URL is already in hand or still has to be read off the tab — so the
  *decision* stays in :func:`_require_navigation_ok` and no tool grows a second
  error-page detector.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, cast

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


def _require_navigation_ok(target: str, result: object) -> object:
    """Return a navigation result, or raise if Chrome landed on an error page
    (F-802).

    A navigation that Chrome could not perform — the host does not resolve, the
    connection is refused, the TLS handshake fails — still *completes*: Chrome
    commits an error page and the tool's own bookkeeping succeeds, so the
    payload used to say ``success: true`` with a ``chrome-error://`` URL.

    THE one error-page detector. *result* is either a navigation payload
    (``{"url": ...}``) or the landed URL itself, because a history move and a
    reload have a URL but no payload (F-833); *target* names what was being
    navigated to — a URL for ``navigate``/``new_tab``, a phrase naming the move
    ("the previous page") for the tools whose destination is only knowable
    afterwards.

    Only a Chrome-level navigation failure is one: a page answering 404/500, a
    redirect to a different final URL, ``about:blank`` and ``data:`` URLs all
    keep their own scheme and pass through untouched.
    """
    final_url = result.get("url") if isinstance(result, dict) else result
    if isinstance(final_url, str) and final_url.startswith(CHROME_ERROR_SCHEME):
        raise ToolError(
            f"Navigation to {target} failed: Chrome loaded an error page "
            f"({final_url}). The host may not resolve, the connection may have "
            "been refused, or the TLS handshake may have failed."
        )
    return result


async def _require_landing_ok(
    landed: object,
    target: str,
    timeout: float,  # noqa: ASYNC109  PERMANENT(caller-owned deadline)
    close_on_error: bool = False,
) -> object:
    """Return the value the calling tool reports, or raise if it landed on a
    Chrome error page (F-833) — the ONE entry point for the five tab-moving
    tools.

    ``navigate`` has been truthful since F-802, but ``go_back``, ``go_forward``,
    ``reload_page`` and ``new_tab`` move a tab the same way and never got the
    guard: a dead history entry, an offline reload and a new tab whose initial
    URL will not load each *complete*, so every Python-side step succeeded and
    the tool answered ``True`` over a ``chrome-error://`` page. Same defect
    class, four more surfaces.

    The five differ in exactly one thing — where the landed URL comes from —
    and that is all this absorbs. Pass the navigation payload when the caller
    already holds it (``navigate``: ``BrowserManager`` built it), or the tab
    itself when the destination is only knowable after the move. In the tab
    case the landing is read the way ``BrowserManager.navigate`` reads its final
    URL, ``window.location.href``: a tab's cached ``target.url`` is refreshed by
    ``update_targets()``, not by a history move, so it would answer for the page
    the tab used to be on. ``await tab`` first is nodriver's ``Tab.wait()`` — it
    returns on the navigation event, or after 0.5s — because ``Tab.back()`` is a
    bare ``window.history.back()`` that returns before Chrome has committed
    anything. A move slower than *timeout* raises rather than hanging the tool.
    That deadline is a parameter, not an ``asyncio.timeout`` block ASYNC109 would
    prefer, because its value is ``server``'s ``CDP_OPERATION_TIMEOUT`` and this
    module imports neither ``server`` nor ``settings`` — the same leaf rule that
    makes ``_require_tab`` take ``browser_manager`` as an argument.

    Returns what the caller should report: the payload it handed in, or ``True``
    for the bool-returning tools whose landing was read here. ``close_on_error``
    closes the tab BEFORE the raise, which is what keeps ``new_tab``'s failure
    path from leaking the half-open tab it just created.
    """
    if isinstance(landed, dict):
        return _require_navigation_ok(target, landed)

    # A string forward-ref, so the nodriver import stays TYPE_CHECKING-only and
    # this module stays the dependency-free leaf its guards are built on.
    tab = cast("Tab", landed)

    async def _settled_url() -> object:
        await tab  # nodriver Tab.wait(): the navigation event, or 0.5s
        return await tab.evaluate("window.location.href")

    try:
        final_url = await asyncio.wait_for(_settled_url(), timeout)
    except TimeoutError as exc:
        raise ToolError(
            f"Could not tell where {target} landed: the tab did not answer "
            f"within {timeout:.0f}s, so this may or may not have worked."
        ) from exc

    try:
        _require_navigation_ok(target, final_url)
    except ToolError:
        if close_on_error:
            # The landing failure is what the caller needs; a tab that also
            # refuses to close must not replace it.
            with contextlib.suppress(Exception):
                await tab.close()
        raise
    return True
