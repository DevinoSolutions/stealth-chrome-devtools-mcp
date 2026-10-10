"""THE one home for "which tab does THIS caller's page tool act on" (F-962).

Several Claude Code sessions share one browser (the ``fleet`` session), each in
its own tab. Every page tool used to act on ONE slot per instance — the stored
tab that ``switch_tab``, ``navigate`` and ``close_tab`` all rewrite — so one
chat's ``switch_tab`` moved every other chat's ``execute_script``,
``click_element`` and ``type_text`` onto ITS tab. Measured in the field: BioFlow
and uprank read another chat's GCP console and MinIO login pages.

A caller is now bound to a tab:

* :func:`claim` — ``spawn_browser`` gives each caller a tab of its own: the
  instance's tab when nobody else holds it, else a fresh one;
* :func:`adopt` — ``new_tab`` binds the caller to the tab it opened, and
  ``switch_tab`` to the tab it switched to;
* every page tool takes ``tab_id`` (added by :func:`scoped`, so no tool can be
  missed) and, without one, acts on the caller's bound tab — never on a tab
  another caller holds. A caller with no tab of its own is refused rather than
  pointed at someone else's.

**Who the caller is.** The stdio proxy sends ``backend_client.CALLER_HEADER``
on every request, one random value per proxy process, i.e. per Claude Code
session; a heal (F-959) keeps it. A client without it (an older proxy, a raw
HTTP client) is keyed by its ``mcp-session-id``. A call with neither — an
in-process call, which is what the hermetic test lanes make — is no caller at
all and keeps the pre-F-962 behaviour exactly: the instance's own tab.

In memory, like ``session_lease``: a backend restart forgets every binding, and
the first caller to touch a re-attached instance claims its tab.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import backend_client, tab_open
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    ToolError,
    instance_not_found,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FunctionType

    from nodriver import Browser, Tab

    from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

#: The argument every page tool takes, and the names whose use MAKES a tool a
#: page tool: a tool that resolves its tab through one of these gets the
#: argument from :func:`scoped`, so the set of page tools is derived from what
#: the tools do rather than typed into a list that a new tool could miss.
PARAMETER = "tab_id"
RESOLVERS = frozenset({"_require_tab", "tab_for_caller", "navigate_callers_tab"})

_PARAMETER_DOC = (
    "tab_id (Optional[str]): The tab to act on (an id from new_tab, "
    "spawn_browser or list_tabs). Default: this session's own tab — the last "
    "one it opened or switched to — never another session's."
)

_requested: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "stealth_requested_tab", default=None
)

#: (caller, instance id) -> target id of that caller's tab, oldest first. Kept
#: to the last ``_BOUND_KEPT`` so a long-lived backend does not grow without
#: bound; a caller whose binding aged out is refused or re-claims, never misled.
_bound: dict[tuple[str, str], str] = {}
_BOUND_KEPT = 4096


def _keep(key: tuple[str, str], target_id: str) -> None:
    _bound.pop(key, None)
    _bound[key] = target_id
    while len(_bound) > _BOUND_KEPT:
        del _bound[next(iter(_bound))]


def caller() -> str | None:
    """The calling session's key, or ``None`` for an in-process call."""
    from fastmcp.server.dependencies import get_http_headers

    headers = get_http_headers(include={"mcp-session-id"})
    return headers.get(backend_client.CALLER_HEADER) or headers.get("mcp-session-id")


def _id(tab: Tab) -> str:
    return str(tab.target.target_id)


def _holders(instance_id: str, target_id: str, besides: str | None) -> list[str]:
    return [
        who
        for (who, instance), bound in _bound.items()
        if instance == instance_id and bound == target_id and who != besides
    ]


async def _open(browser: Browser, target_id: str) -> Tab | None:
    """The tab *target_id* if it is open, asking Chrome once on a miss."""
    tab = tab_open.find(browser, target_id)
    if tab is None:
        await browser.update_targets()
        tab = tab_open.find(browser, target_id)
    return tab


async def tab_for_caller(
    browser_manager: BrowserManager, instance_id: str
) -> Tab | None:
    """THE tab a page tool acts on; ``None`` only when there is no such instance.

    The ``tab_id`` the call passed, else the caller's bound tab, else the
    instance's own tab — claimed for this caller when nobody holds it, and
    refused when another caller does. Raises when the tab named is not open.
    """
    requested = _requested.get()
    who = caller()
    wanted = requested or (_bound.get((who, instance_id)) if who else None)
    if wanted is None:
        tab = await browser_manager.get_tab(instance_id)
        if tab is not None and who is not None:
            if _holders(instance_id, _id(tab), who):
                raise ToolError(
                    f"Instance {instance_id} is shared and this session has no tab "
                    "of its own in it, so this call would act on another session's "
                    "tab. Call new_tab(instance_id) (or spawn_browser again) to get "
                    "one, or pass tab_id."
                )
            _keep((who, instance_id), _id(tab))
        return tab
    browser = await browser_manager.get_browser(instance_id)
    if browser is None:
        return None
    tab = await _open(browser, wanted)
    if tab is not None:
        return tab
    if requested:
        raise ToolError(
            f"Tab {wanted} is not open in instance {instance_id}; list_tabs shows "
            "the tabs that are."
        )
    _bound.pop((who, instance_id), None)
    raise ToolError(
        f"This session's tab ({wanted}) is no longer open in instance "
        f"{instance_id}. Call new_tab(instance_id) to open one, or switch_tab to "
        "one from list_tabs."
    )


async def adopt(browser_manager: BrowserManager, instance_id: str, tab: Tab) -> None:
    """Make *tab* the caller's own, armed like the spawn tab (F-935)."""
    await browser_manager._arm_tracked_tab(instance_id, tab)
    bind(instance_id, _id(tab))


def bind(instance_id: str, target_id: str) -> None:
    """Bind the caller to *target_id* (no-op for an in-process call)."""
    who = caller()
    if who is not None:
        _keep((who, instance_id), str(target_id))


def forget_tab(instance_id: str, target_id: str) -> None:
    """Drop every binding to a tab that was closed."""
    for key, bound in list(_bound.items()):
        if key[1] == instance_id and bound == str(target_id):
            del _bound[key]


async def claim(
    browser_manager: BrowserManager, instance_id: str
) -> dict[str, str | None]:
    """``spawn_browser``'s ``tab_id``: the caller's own tab in *instance_id*.

    Its bound tab if still open; else the instance's tab if no other caller
    holds it; else a new tab. Never raises: a tab that could not be opened is
    reported as ``tab_error``, and that caller's page tools then refuse rather
    than act on someone else's tab.
    """
    who = caller()
    main = await browser_manager.get_tab(instance_id)
    if who is None:
        return {"tab_id": _id(main)} if main is not None else {}
    try:
        browser = await browser_manager.get_browser(instance_id)
        mine = _bound.get((who, instance_id))
        if browser is not None and mine and await _open(browser, mine) is not None:
            return {"tab_id": mine}
        if main is not None and not _holders(instance_id, _id(main), who):
            bind(instance_id, _id(main))
            return {"tab_id": _id(main)}
        if browser is None:
            return {}
        tab = await tab_open.open_tab(browser, "about:blank")
        await adopt(browser_manager, instance_id, tab)
        return {"tab_id": _id(tab)}
    except Exception as error:  # noqa: BLE001  PERMANENT(F-962: a tab it could not open is the answer's tab_error, never a failed spawn)
        debug_logger.log_warning(
            "tab_binding", "claim", f"{instance_id}: {error!r}", error=error
        )
        return {"tab_id": None, "tab_error": f"{type(error).__name__}: {error}"}


async def navigate_callers_tab(  # noqa: PLR0913  PERMANENT(navigate's own five, F-962)
    browser_manager: BrowserManager,
    instance_id: str,
    url: str,
    wait_until: str,
    timeout: int,  # noqa: ASYNC109  plan_M7 (navigate's ms budget, as there)
    referrer: str | None,
) -> dict[str, object]:
    """``navigate`` on the caller's tab. The instance's own tab keeps its
    recycle and stale-tab recovery, and a caller bound to it follows it to the
    replacement; any other tab is navigated where it is. An in-process call
    naming no tab is exactly the pre-F-962 ``navigate``."""
    if _requested.get() is None and caller() is None:
        return await browser_manager.navigate(
            instance_id=instance_id,
            url=url,
            wait_until=wait_until,
            timeout=timeout,
            referrer=referrer,
        )
    tab = await tab_for_caller(browser_manager, instance_id)
    if tab is None:
        raise instance_not_found(instance_id)
    main = await browser_manager.get_tab(instance_id)
    if main is None or _id(main) != _id(tab):
        return await browser_manager.navigate(
            instance_id=instance_id,
            url=url,
            wait_until=wait_until,
            timeout=timeout,
            referrer=referrer,
            pinned=tab,
        )
    try:
        return await browser_manager.navigate(
            instance_id=instance_id,
            url=url,
            wait_until=wait_until,
            timeout=timeout,
            referrer=referrer,
        )
    finally:
        replaced = await browser_manager.get_tab(instance_id)
        if replaced is not None and _id(replaced) != _id(tab):
            for key, bound in list(_bound.items()):
                if key[1] == instance_id and bound == _id(tab):
                    _bound[key] = _id(replaced)


def scoped(func: FunctionType) -> Callable[..., object]:
    """Give a page tool its ``tab_id`` argument; any other tool is unchanged.

    The value travels in a context variable for the length of the call, so the
    tools' own bodies and the one resolver they share are all that read it.
    """
    names = set(func.__code__.co_names)
    signature = inspect.signature(func)
    if not names & RESOLVERS or PARAMETER in signature.parameters:
        return func
    if not inspect.iscoroutinefunction(func):
        raise TypeError(f"page tool {func.__name__} must be async to take {PARAMETER}")

    @functools.wraps(func)
    async def wrapper(
        *args: object, tab_id: str | None = None, **kwargs: object
    ) -> object:
        token = _requested.set(tab_id or None)
        try:
            return await func(*args, **kwargs)
        finally:
            _requested.reset(token)

    parameter = inspect.Parameter(
        PARAMETER,
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=str | None,
    )
    wrapper.__dict__["__signature__"] = signature.replace(
        parameters=[*signature.parameters.values(), parameter]
    )
    wrapper.__annotations__ = {**func.__annotations__, PARAMETER: str | None}
    wrapper.__doc__ = _documented(func.__doc__ or "")
    return wrapper


def _documented(doc: str) -> str:
    """*doc* with the ``tab_id`` line after the ``instance_id`` one."""
    lines = doc.split("\n")
    for index, line in enumerate(lines):
        if line.strip().startswith("instance_id ("):
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(index + 1, indent + _PARAMETER_DOC)
            return "\n".join(lines)
    body = [line for line in lines[1:] if line.strip()]
    indent = body[-1][: len(body[-1]) - len(body[-1].lstrip())] if body else ""
    return doc.rstrip() + "\n\n" + indent + _PARAMETER_DOC + "\n"
