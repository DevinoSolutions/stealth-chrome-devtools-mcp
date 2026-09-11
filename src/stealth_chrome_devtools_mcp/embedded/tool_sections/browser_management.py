"""The ``browser-management`` tools. See ``tool_sections/__init__.py`` for the contract.

plan_SERVERSPLIT slice 10 — the second-largest section and the one that owns the
BROWSER's own lifecycle: ``spawn_browser`` (the plan's largest single tool),
``close_instance``, the three history verbs and ``navigate``.

Two things make this section different from the nine before it, and both are
reasons the mechanism had to be proven elsewhere first:

* ``spawn_browser`` carries the F-808/F-810 headed-visibility guard, which runs
  BEFORE the ``try`` and outside it, so a spawn nobody could ever see refuses
  without first cloning a profile dir onto disk. It reads
  ``rt.display_context.display_context()`` for the refusal message and calls
  ``desktop_launch.can_deliver_headed_window()`` through a function-local import
  — both carried verbatim, including the local import, which is what keeps
  ``desktop_launch`` off this module's import graph.
* ``spawn_browser`` and ``close_instance`` are the two ends of the on-disk
  profile/clone lifecycle, so six of this module's calls land in
  ``rt.clone_storage`` — the disk subsystem, resolved at call time like every
  other singleton, which is what keeps
  ``tests/test_clone_storage.py``'s "patch it THERE, not on server" pin true of a
  body that no longer lives in ``server.py``.

Bodies moved verbatim: the only edits are the dropped registration decorator
(contract rule 2 — registration is driven from ``server.py``'s binding loop, once
per execution of that module body) and the rewrite of the singleton/knob reads to
``rt.<name>``, resolved at CALL time against the one patchable home (contract
rule 3). Docstrings and signatures are byte-identical — FastMCP surfaces them and
``tests/goldens/tool_surface.json`` is a HARD golden for this migration — and so
is ``get_instance_state``'s ``# F-164 non-CDP`` marker comment, which
``tests/test_cdp_timeout.py`` follows into this file through
``tests/source_scan.py``.
"""

import asyncio
from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.models import (
    BrowserOptions,
)
from stealth_chrome_devtools_mcp.embedded.platform_utils import (
    is_running_as_root,
    is_running_in_container,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    ToolError,
    _require_landing_ok,
    _require_tab,
)
from stealth_chrome_devtools_mcp.settings import get_settings

SECTION = "browser-management"


async def spawn_browser(
    headless: bool = False,
    user_agent: str | None = None,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    proxy: str | None = None,
    browser_args: list[str] = None,
    timezone_id: str | None = None,
    idle_timeout_seconds: int | None = None,
    block_resources: list[str] = None,
    extra_headers: dict[str, str] = None,
    user_data_dir: str | None = None,
    sandbox: Any | None = None,
) -> dict[str, Any]:
    """
    Spawn a new browser instance.

    Args:
        headless (bool): Run in headless mode.
        user_agent (Optional[str]): Custom user agent string.
        viewport_width (int): Requested browser WINDOW width in pixels (outer, not
            the CSS viewport). Best-effort: a headed window is clamped to the work
            area of the LAUNCHING context's desktop — the user's monitor only when
            the backend runs on it (F-808), not the caller's screen — so a request
            larger than that desktop lands smaller.
        viewport_height (int): Requested browser WINDOW height in pixels, same
            best-effort clamping as viewport_width.
        proxy (Optional[str]): Proxy server URL.
        browser_args (List[str]): Additional browser launch args.
        timezone_id (Optional[str]): IANA timezone ID applied via CDP timezone override.
        idle_timeout_seconds (Optional[int]): Idle timeout override in seconds for automatic instance cleanup.
        block_resources (List[str]): List of resource types to block (e.g., ['image', 'font', 'stylesheet']).
        extra_headers (Dict[str, str]): Additional HTTP headers.
        user_data_dir (Optional[str]): Leave UNSET for normal use. When unset, the server
            automatically clones a disposable session from the master profile and deletes it
            as soon as the browser closes — you never need to manage or clean up sessions.
            Only set this when the user has EXPLICITLY asked for a persistent/named profile:
            a named profile is NOT auto-cleaned and persists on disk indefinitely, so treat
            creating one as a deliberate, space-consuming action. Do not invent names.
        sandbox (Optional[Any]): Enable browser sandbox. Accepts bool, string ('true'/'false'), int (1/0), or None for auto-detect.

    Network interception captures request/response metadata by default, but
    response *bodies* are NOT stored unless capture is enabled — via
    set_network_capture_filters(capture_bodies=True) or
    STEALTH_MCP_NETWORK_CAPTURE_BODIES=1 (F-605, off by default). When on, the
    body store is byte-bounded (STEALTH_MCP_NETWORK_BODY_MAX_BYTES per body,
    STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES total). get_response_content
    live-refetches a body on demand regardless of this setting.

    Returns:
        Dict[str, Any]: Instance information including instance_id. ``viewport`` is
        the window size Chrome ACTUALLY produced (measured post-launch, F-804), not
        an echo of the request; ``spawn_diagnostics["window_size"]`` carries
        ``requested``/``actual``/``inner_viewport``/``clamped`` so a size the OS
        overrode is visible rather than silent.
    """
    # BEFORE any other work, and outside the try so it is not re-wrapped (F-808):
    # a spawn nobody could ever see must not first clone a profile dir onto disk.
    # F-810 demoted it to a FALLBACK: it fires only when delegation is impossible.
    from stealth_chrome_devtools_mcp.embedded import desktop_launch

    if not headless and not desktop_launch.can_deliver_headed_window():
        raise ToolError(
            f"This backend runs in a context that cannot display a window "
            f"({rt.display_context.display_context()}), so a headed browser would launch "
            "invisibly (F-808), and no user is logged on at the desktop for the OS to "
            "launch it there instead (F-810). Start the backend from a desktop session "
            "or pass headless=True; `stealth-chrome-devtools doctor` lists the contexts."
        )
    try:
        if sandbox is None:
            sandbox = not (is_running_as_root() or is_running_in_container())
        elif isinstance(sandbox, str):
            sandbox = sandbox.lower() in ("true", "1", "yes", "on", "enabled")
        elif isinstance(sandbox, int) or not isinstance(sandbox, bool):
            sandbox = bool(sandbox)

        profile_selection = await rt.clone_storage.resolve_profile_selection(
            user_data_dir
        )
        spawn_errors = []

        for spawn_attempt in range(3):
            selected_user_data_dir = profile_selection["user_data_dir"]
            options = BrowserOptions(
                headless=headless,
                user_agent=user_agent,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                proxy=proxy,
                browser_args=browser_args or [],
                timezone_id=timezone_id,
                idle_timeout_seconds=idle_timeout_seconds,
                block_resources=block_resources or [],
                extra_headers=extra_headers or {},
                user_data_dir=selected_user_data_dir,
                sandbox=sandbox,
                auto_clone=(profile_selection.get("profile_role") == "clone"),
            )
            try:
                instance = await rt.browser_manager.spawn_browser(options)
                user_data_dir = selected_user_data_dir
                break
            except Exception as spawn_error:
                spawn_errors.append(f"{type(spawn_error).__name__}: {spawn_error}")
                # This attempt's clone never became a live instance — drop its
                # sweep protection so a failed clone can't stay protected (and thus
                # unreclaimable) for the rest of the process.
                if profile_selection.get("profile_role") == "clone":
                    rt.clone_storage._release_clone_dir(selected_user_data_dir)
                fallback_selection = await rt.clone_storage._fallback_profile_selection(
                    profile_selection, spawn_attempt
                )
                if fallback_selection is None:
                    raise
                profile_selection = fallback_selection
        else:
            raise Exception("; ".join(spawn_errors))

        tab = await rt.browser_manager.get_tab(instance.instance_id)
        if tab:
            await rt.network_interceptor.setup_interception(
                tab, instance.instance_id, block_resources
            )
        spawn_diagnostics = await rt.browser_manager.get_spawn_diagnostics(
            instance.instance_id
        )
        if isinstance(spawn_diagnostics, dict):
            spawn_diagnostics["profile_selection"] = (
                rt.clone_storage._public_profile_selection(profile_selection)
            )
            if spawn_errors:
                spawn_diagnostics["profile_selection"]["spawn_retries"] = spawn_errors
            if profile_selection.get("profile_role") == "explicit":
                spawn_diagnostics["profile_selection"]["warning"] = (
                    "Named profile created — it is NOT auto-cleaned and persists on disk. "
                    "Only pass user_data_dir when the user explicitly asks for a persistent "
                    "profile; otherwise omit it so the session is auto-cloned and auto-deleted."
                )
        return {
            "instance_id": instance.instance_id,
            "state": instance.state,
            "headless": instance.headless,
            "viewport": instance.viewport,
            "spawn_diagnostics": spawn_diagnostics or {},
        }
    except Exception as e:
        raise ToolError(f"Failed to spawn browser: {e!s}")


async def list_instances() -> list[dict[str, Any]]:
    """
    List all active browser instances.

    Returns:
        List[Dict[str, Any]]: List of browser instances with their current state.
    """
    memory_instances = await rt.browser_manager.list_instances()
    storage_instances = rt.in_memory_storage.list_instances()
    result = []
    for inst in memory_instances:
        result.append(
            {
                "instance_id": inst.instance_id,
                "state": inst.state,
                "current_url": inst.current_url,
                "title": inst.title,
                "source": "active",
            }
        )
    memory_ids = {inst.instance_id for inst in memory_instances}
    for instance_id, inst_data in storage_instances.get("instances", {}).items():
        if instance_id not in memory_ids:
            result.append(
                {
                    "instance_id": inst_data["instance_id"],
                    "state": inst_data["state"] + " (stored)",
                    "current_url": inst_data["current_url"],
                    "title": inst_data["title"],
                    "source": "stored",
                }
            )
    return result


async def close_instance(instance_id: str) -> bool:
    """
    Close a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True if closed successfully.
    """
    spawn_diagnostics = await rt.browser_manager.get_spawn_diagnostics(instance_id)
    profile_selection = {}
    if isinstance(spawn_diagnostics, dict):
        profile_selection = spawn_diagnostics.get("profile_selection") or {}
    should_refresh_snapshot = profile_selection.get("profile_role") == "master"

    success = await rt.browser_manager.close_instance(instance_id)
    if success:
        await rt.network_interceptor.clear_instance_data(instance_id)
        rt.dynamic_hook_system.remove_instance(instance_id)
        # Instance is gone — lift sweep protection for its disposable clone so the
        # storage cap can reclaim it later if the on-close delete was deferred.
        if profile_selection.get("profile_role") == "clone" and profile_selection.get(
            "user_data_dir"
        ):
            rt.clone_storage._release_clone_dir(profile_selection["user_data_dir"])
        if should_refresh_snapshot:
            await asyncio.to_thread(
                rt.clone_storage._refresh_master_snapshot_if_safe, "after-master-close"
            )
    return success


async def get_instance_state(instance_id: str) -> dict[str, Any] | None:
    """
    Get detailed state of a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Optional[Dict[str, Any]]: Full page state, or a partial record
        (``partial: True``) with ``detail_error`` if collection times out or fails.
    """
    timeout_seconds = get_settings().browser_state_timeout_seconds
    try:
        # F-164 non-CDP: bounds a multi-step page-state aggregation with its own
        # browser_state_timeout_seconds budget; the except paths below return an
        # honest partial record (F-746), not the generic CDP-timeout error that
        # _with_cdp_timeout raises — so this is deliberately not that wrapper.
        state = await asyncio.wait_for(
            rt.browser_manager.get_page_state(instance_id),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        for instance in await rt.browser_manager.list_instances():
            if instance.instance_id == instance_id:
                return {
                    "instance_id": instance.instance_id,
                    "state": instance.state,
                    "current_url": instance.current_url,
                    "title": instance.title,
                    "source": "active",
                    "partial": True,
                    "detail_error": f"Timed out after {timeout_seconds:g}s while collecting full page state.",
                }
        return {
            "instance_id": instance_id,
            "state": "unknown",
            "partial": True,
            "detail_error": f"Timed out after {timeout_seconds:g}s while collecting full page state.",
        }
    except Exception as exc:
        for instance in await rt.browser_manager.list_instances():
            if instance.instance_id == instance_id:
                return {
                    "instance_id": instance.instance_id,
                    "state": instance.state,
                    "current_url": instance.current_url,
                    "title": instance.title,
                    "source": "active",
                    "partial": True,
                    "detail_error": f"Failed to collect full page state: {type(exc).__name__}: {exc}",
                }
        return {
            "instance_id": instance_id,
            "state": "unknown",
            "partial": True,
            "detail_error": f"Failed to collect full page state: {type(exc).__name__}: {exc}",
        }
    if state:
        result = state.dict()
        result["partial"] = False
        return result
    return None


async def navigate(
    instance_id: str,
    url: str,
    wait_until: str = "load",
    timeout: int = 30000,
    referrer: str | None = None,
) -> dict[str, Any]:
    """
    Navigate to a URL.

    Args:
        instance_id (str): Browser instance ID.
        url (str): URL to navigate to.
        wait_until (str): Wait condition - 'load', 'domcontentloaded', or 'networkidle'.
        timeout (int): Navigation timeout in ms (default 30000, max 60000). Most pages load in under 10s — only increase if you have evidence the page is slow. Values above 60000 are capped.
        referrer (Optional[str]): Referrer URL.

    Returns:
        Dict[str, Any]: Navigation result with final URL and title.

    Raises:
        ToolError: the navigation failed at the browser — Chrome committed an
            error page (unresolvable host, refused connection, TLS failure).
            An HTTP error status (404/500) is a loaded page, not a failure.
    """
    timeout = rt._clamp_timeout(timeout, default=30_000)
    outer_timeout = max(timeout / 1000 + 5, rt.CDP_OPERATION_TIMEOUT)
    result = await rt._with_cdp_timeout(
        rt.browser_manager.navigate(
            instance_id=instance_id,
            url=url,
            wait_until=wait_until,
            timeout=timeout,
            referrer=referrer,
        ),
        timeout=outer_timeout,
        instance_id=instance_id,
    )
    # Bookkeeping completed above, so raising here cannot leave the tab or the
    # state table behind (F-802).
    return await _require_landing_ok(result, url, rt.CDP_OPERATION_TIMEOUT)


async def go_back(instance_id: str) -> bool:
    """
    Navigate back in history.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True. Landing on a Chrome error page raises instead (F-833).
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    await rt._with_cdp_timeout(tab.back(), instance_id=instance_id)
    return await _require_landing_ok(tab, "the previous page", rt.CDP_OPERATION_TIMEOUT)


async def go_forward(instance_id: str) -> bool:
    """
    Navigate forward in history.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True. Landing on a Chrome error page raises instead (F-833).
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    await rt._with_cdp_timeout(tab.forward(), instance_id=instance_id)
    return await _require_landing_ok(tab, "the next page", rt.CDP_OPERATION_TIMEOUT)


async def reload_page(instance_id: str, ignore_cache: bool = False) -> bool:
    """
    Reload the current page.

    Args:
        instance_id (str): Browser instance ID.
        ignore_cache (bool): Whether to ignore cache when reloading.

    Returns:
        bool: True. Landing on a Chrome error page raises instead (F-833).
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    await rt._with_cdp_timeout(tab.reload(), instance_id=instance_id)
    return await _require_landing_ok(tab, "the reloaded page", rt.CDP_OPERATION_TIMEOUT)


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in ``SECTION_TOOLS["browser-management"]``.
TOOLS = (
    spawn_browser,
    list_instances,
    close_instance,
    get_instance_state,
    navigate,
    go_back,
    go_forward,
    reload_page,
)
