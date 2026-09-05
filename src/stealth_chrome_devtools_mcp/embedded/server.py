"""Main MCP server for browser automation."""

import asyncio
import base64
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.logging_setup import (
    backend_uvicorn_config,
    bootstrap_backend_process_logging,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    InstanceNotFoundError,
    ToolError,
    _require_tab,
)
from stealth_chrome_devtools_mcp.embedded.tool_registry import (
    DISABLED_SECTIONS,
    SECTION_TOOLS,  # used by --list-sections + the derived tool-count (F-108); also re-exported as server.SECTION_TOOLS
    ToolRegistry,
)
from stealth_chrome_devtools_mcp.embedded.tool_runtime import (
    CDP_OPERATION_TIMEOUT,
    EXECUTE_SCRIPT_TIMEOUT,
    _clamp_timeout,
    _script_rejection_reason,
    _with_cdp_timeout,
    browser_manager,
    clone_storage,  # noqa: F401  plan_SERVERSPLIT slice 10 — kept for tests/test_clone_storage.py's "server delegates via the imported module" pin; deleted in slice 12
    debug_logger,
    dom_handler,
    network_interceptor,
    response_handler,
)
from stealth_chrome_devtools_mcp.embedded.tool_sections import SECTION_MODULES
from stealth_chrome_devtools_mcp.observability import sentry_init
from stealth_chrome_devtools_mcp.settings import get_settings

# plan_SERVERSPLIT slice 0: the knobs, the script/timeout guards and the four
# constructed singletons now live in ``tool_runtime`` — THE one patchable home
# (a module attribute resolved at call time, in a module that is imported once
# rather than re-executed by every runpy/spec load). The block above is a
# MIGRATION ALIAS: the tool bodies still in this file read these as bare names,
# and ``tests/conftest.py``'s ``patched_server`` patches both homes until slice
# 12 deletes the aliases. An alias is pruned by the slice that takes its LAST
# consumer out of this file — prod or test — which is why the block shrinks with
# every slice and why ``clone_storage`` carries an explicit keep-reason above.
# ``app_lifespan`` below deliberately does NOT use them — it reads ``rt.*`` so a
# patched singleton reaches the lifespan too.


def _install_asyncio_close_noise_filter() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    marker = "_stealth_chrome_devtools_close_noise_filter"
    if getattr(loop, marker, False):
        return

    previous_handler = loop.get_exception_handler()

    def exception_handler(
        loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        exception = context.get("exception")
        if (
            exception is not None
            and exception.__class__.__name__
            in ("ConnectionClosedOK", "ConnectionClosedError", "ConnectionClosed")
            and str(exception.__class__.__module__).startswith("websockets.")
        ):
            # Swallow both clean (OK) and abnormal (Error) websocket closes. When
            # Chrome crashes or is killed, nodriver's background listener task
            # raises ConnectionClosedError; without this it surfaces loudly and
            # can escalate. The instance is already unusable and will be respawned.
            return

        if previous_handler is not None:
            previous_handler(loop, context)
            return

        loop.default_exception_handler(context)

    loop.set_exception_handler(exception_handler)
    setattr(loop, marker, True)


def _install_nodriver_cookie_compat() -> None:
    try:
        import nodriver.cdp.network as cdp_network
    except Exception as e:
        debug_logger.log_warning("server", "_install_nodriver_cookie_compat", str(e))
        return

    marker = "_stealth_chrome_devtools_cookie_compat"
    if getattr(cdp_network.Cookie, marker, False):
        return

    original_from_json = cdp_network.Cookie.from_json

    def from_json(json_obj: dict[str, Any]):
        if isinstance(json_obj, dict) and "sameParty" not in json_obj:
            json_obj = dict(json_obj)
            json_obj["sameParty"] = False
        return original_from_json(json_obj)

    cdp_network.Cookie.from_json = staticmethod(from_json)
    setattr(cdp_network.Cookie, marker, True)


DEBUG_LOGGING_ENABLED = get_settings().stealth_browser_debug or get_settings().debug

# B1 (RELEASE-FIX-B): FastMCP runs the server ``lifespan`` once PER MCP SESSION
# over streamable HTTP, not once per process. Startup must therefore be guarded
# to the first entry per process, and the destructive teardown must be bound to
# *process* end (stdio standalone), never *session* end — otherwise every probe
# session's exit tears down all live browsers. ``_SERVE_TRANSPORT`` is stamped by
# the ``__main__`` entrypoint from the parsed ``--transport``; the default keeps
# the standalone-stdio contract. A boolean guard (not a refcount) is deliberate:
# an idle HTTP backend crossing back to zero sessions must NOT re-arm startup.
_LIFESPAN_STARTED = False
_SERVE_TRANSPORT = "stdio"


@asynccontextmanager
async def app_lifespan(server):
    """
    Manage application lifecycle with proper cleanup.

    Args:
        server (Any): The server instance for which the lifespan is being managed.
    """
    global _LIFESPAN_STARTED
    if not _LIFESPAN_STARTED:
        _LIFESPAN_STARTED = True
        _install_asyncio_close_noise_filter()
        _install_nodriver_cookie_compat()
        debug_logger.log_info(
            "server", "startup", "Starting Browser Automation MCP Server..."
        )
        rt.process_cleanup.activate()
        await rt.browser_manager.start_idle_reaper()
        # Reclaim leaked auto-clones and trim oversized idle named profiles left
        # by a previous run. Fire-and-forget so a large first sweep never delays
        # server readiness.
        rt.clone_storage.spawn_background_sweep("startup")
    try:
        yield
    finally:
        # HTTP session exit is a no-op: instances are shared across sessions and
        # process termination is already reaped by process_cleanup's atexit/signal
        # handlers. Only the standalone-stdio process (one session == process
        # lifetime) runs the destructive teardown, preserving the 1.x contract.
        # An ``if`` guard (not an early ``return``) is deliberate: a ``return`` in
        # a ``finally`` would suppress an exception propagating from the session.
        if _SERVE_TRANSPORT != "http":
            debug_logger.log_info(
                "server", "shutdown", "Shutting down Browser Automation MCP Server..."
            )
            try:
                await rt.browser_manager.stop_idle_reaper()
            except Exception as e:
                debug_logger.log_error("server", "cleanup", e)
            try:
                await rt.browser_manager.close_all()
                debug_logger.log_info(
                    "server", "cleanup", "All browser instances closed"
                )
            except Exception as e:
                debug_logger.log_error("server", "cleanup", e)

            try:
                rt.process_cleanup._cleanup_all_tracked()
                debug_logger.log_info("server", "cleanup", "Process cleanup complete")
            except Exception as e:
                debug_logger.log_error(
                    "server", "cleanup", f"Process cleanup failed: {e}"
                )
            try:
                persistent_instances = rt.in_memory_storage.list_instances()
                if persistent_instances.get("instances"):
                    debug_logger.log_info(
                        "server",
                        "storage_cleanup",
                        f"Clearing in-memory storage with {len(persistent_instances['instances'])} instances...",
                    )
                    rt.in_memory_storage.clear_all()
                    debug_logger.log_info(
                        "server", "storage_cleanup", "In-memory storage cleared"
                    )
            except Exception as e:
                debug_logger.log_error("server", "storage_cleanup", e)
            debug_logger.log_info(
                "server", "shutdown", "Browser Automation MCP Server shutdown complete"
            )


mcp = FastMCP(
    name="Browser Automation MCP",
    instructions="""
    This MCP server provides undetectable browser automation using nodriver (CDP-based).
    
    Key features:
    - Spawn and manage multiple browser instances
    - Navigate and interact with web pages
    - Query and manipulate DOM elements
    - Intercept and analyze network traffic
    - Execute JavaScript in page context
    - Manage cookies and storage
    
    All browser instances are undetectable by anti-bot systems.
    """,
    lifespan=app_lifespan,
)

registry = ToolRegistry(mcp)
section_tool = registry.section_tool
apply_disabled_sections = registry.apply_disabled_sections

# Registration is driven from HERE, once per execution of this module body, so the
# canonical import, the bare-name spec load and the runpy __main__ load each get a
# fully-populated `mcp`. A @section_tool decorator inside a section module would
# run on the FIRST import only and leave every later execution with a zero-tool
# app — the mirror image of the 282 == 3 x 94 accumulation this repo has already
# paid for, and invisible to the count tripwire. Binding into globals() is what
# keeps `getattr(server, tool_name)` — the mechanism tests/fakes.py's call_tool
# and tests/e2e_helpers.py's get_fn both use — working after a body moves out.
# Must stay AHEAD of the xpool-safe gate at the foot of this file, or the
# cdp-functions tools would register into a gate that had already closed.
for _section_module in SECTION_MODULES:
    for _tool in _section_module.TOOLS:
        globals()[_tool.__name__] = section_tool(_section_module.SECTION)(_tool)

if DEBUG_LOGGING_ENABLED:
    debug_logger.enable()


@section_tool("element-interaction")
async def query_elements(
    instance_id: str,
    selector: str,
    text_filter: str | None = None,
    visible_only: bool = True,
    limit: Any | None = None,
) -> list[dict[str, Any]]:
    """
    Query DOM elements.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath (starts with '//').
        text_filter (Optional[str]): Filter by text content.
        visible_only (bool): Only return visible elements.
        limit (Optional[Any]): Maximum number of elements to return.

    Returns:
        List[Dict[str, Any]]: List of matching elements with their properties.
    """
    tab = await _require_tab(browser_manager, instance_id)
    debug_logger.log_info(
        "Server",
        "query_elements",
        f"Received limit parameter: {limit} (type: {type(limit)})",
    )
    elements = await _with_cdp_timeout(
        dom_handler.query_elements(tab, selector, text_filter, visible_only, limit),
        instance_id=instance_id,
    )
    debug_logger.log_info(
        "Server", "query_elements", f"DOM handler returned {len(elements)} elements"
    )
    result = []
    for i, elem in enumerate(elements):
        try:
            if hasattr(elem, "model_dump"):
                elem_dict = elem.model_dump()
            else:
                elem_dict = elem.dict()
            result.append(elem_dict)
            debug_logger.log_info(
                "Server",
                "query_elements",
                f"Converted element {i + 1} to dict: {list(elem_dict.keys())}",
            )
        except Exception as e:
            debug_logger.log_error("Server", "query_elements", e, {"element_index": i})
    debug_logger.log_info(
        "Server", "query_elements", f"Returning {len(result)} results to MCP client"
    )
    return result or []


@section_tool("element-interaction")
async def click_element(
    instance_id: str,
    selector: str,
    text_match: str | None = None,
    timeout: int = 10000,
) -> bool:
    """
    Click an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        text_match (Optional[str]): Click element with matching text.
        timeout (int): Timeout in ms (default 10000, max 60000). Clicks rarely need more than 5s — only increase for dynamically loaded elements. Values above 60000 are capped.

    Returns:
        bool: True if clicked successfully.
    """
    timeout = _clamp_timeout(timeout, default=10_000)
    tab = await _require_tab(browser_manager, instance_id)
    return await _with_cdp_timeout(
        dom_handler.click_element(tab, selector, text_match, timeout),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def upload_file(
    instance_id: str,
    selector: str,
    file_paths: str | list[str],
    timeout: int = 10000,
) -> dict[str, Any]:
    """
    Upload local file(s) to a file input. USE THIS for file uploads.

    Sets the files directly on the <input type="file"> via CDP — reliable and
    non-blocking. Do NOT try to upload by fetching blobs / building base64 /
    DataTransfer inside execute_script: that hits mixed-content/CORS limits and
    can freeze the page. This tool is the supported path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath for the <input type="file"> element.
        file_paths (Union[str, List[str]]): Absolute path, or list of paths, to attach.
            For multiple files the input must have the `multiple` attribute.
        timeout (int): Element lookup timeout in ms (default 10000, max 60000).

    Returns:
        Dict[str, Any]: {"uploaded": [absolute paths], "count": int}.
    """
    timeout = _clamp_timeout(timeout, default=10_000)
    paths = [file_paths] if isinstance(file_paths, str) else list(file_paths)
    tab = await _require_tab(browser_manager, instance_id)
    return await _with_cdp_timeout(
        dom_handler.upload_file(tab, selector, paths, timeout),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def type_text(
    instance_id: str,
    selector: str,
    text: str,
    clear_first: bool = True,
    delay_ms: int = 50,
    parse_newlines: bool = False,
    shift_enter: bool = False,
) -> bool:
    """
    Type text into an input field.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        text (str): Text to type.
        clear_first (bool): Clear field before typing.
        delay_ms (int): Delay between keystrokes in milliseconds.
        parse_newlines (bool): If True, parse \n as Enter key presses.
        shift_enter (bool): If True, use Shift+Enter instead of Enter (for chat apps).

    Returns:
        bool: True if typed successfully.
    """
    if isinstance(delay_ms, str):
        delay_ms = int(delay_ms)
    tab = await _require_tab(browser_manager, instance_id)
    return await _with_cdp_timeout(
        dom_handler.type_text(
            tab, selector, text, clear_first, delay_ms, parse_newlines, shift_enter
        ),
        timeout=60,
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def paste_text(
    instance_id: str, selector: str, text: str, clear_first: bool = True
) -> bool:
    """
    Paste text instantly into an input field.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        text (str): Text to paste.
        clear_first (bool): Clear field before pasting.

    Returns:
        bool: True if pasted successfully.
    """
    tab = await _require_tab(browser_manager, instance_id)
    return await _with_cdp_timeout(
        dom_handler.paste_text(tab, selector, text, clear_first),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def select_option(
    instance_id: str,
    selector: str,
    value: str | None = None,
    text: str | None = None,
    index: Any | None = None,
) -> bool:
    """
    Select an option from a dropdown.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the select element.
        value (Optional[str]): Option value attribute.
        text (Optional[str]): Option text content.
        index (Optional[Any]): Option index (0-based). Can be string or int.

    Returns:
        bool: True if selected successfully.
    """
    tab = await _require_tab(browser_manager, instance_id)

    converted_index = None
    if index is not None:
        try:
            converted_index = int(index)
        except (ValueError, TypeError):
            raise ToolError(f"Invalid index value: {index}. Must be a number.")

    return await _with_cdp_timeout(
        dom_handler.select_option(tab, selector, value, text, converted_index),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def get_element_state(instance_id: str, selector: str) -> dict[str, Any]:
    """
    Get complete state of an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.

    Returns:
        Dict[str, Any]: Element state including attributes, style, position, etc.
    """
    tab = await _require_tab(browser_manager, instance_id)
    return await _with_cdp_timeout(
        dom_handler.get_element_state(tab, selector), instance_id=instance_id
    )


@section_tool("element-interaction")
async def wait_for_element(
    instance_id: str,
    selector: str,
    timeout: int = 30000,
    visible: bool = True,
    text_content: str | None = None,
) -> bool:
    """
    Wait for an element to appear.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        timeout (int): Timeout in ms (default 30000, max 60000). Most elements appear within 5-10s — only increase for very slow async content. Values above 60000 are capped.
        visible (bool): Wait for element to be visible.
        text_content (Optional[str]): Wait for specific text content.

    Returns:
        bool: True if element found.
    """
    timeout = _clamp_timeout(timeout, default=30_000)
    tab = await _require_tab(browser_manager, instance_id)
    return await _with_cdp_timeout(
        dom_handler.wait_for_element(tab, selector, timeout, visible, text_content),
        timeout=max(timeout / 1000 + 5, CDP_OPERATION_TIMEOUT),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def scroll_page(
    instance_id: str, direction: str = "down", amount: int = 500, smooth: bool = True
) -> bool:
    """
    Scroll the page.

    Args:
        instance_id (str): Browser instance ID.
        direction (str): 'down', 'up', 'left', 'right', 'top', or 'bottom'.
        amount (int): Pixels to scroll (ignored for 'top' and 'bottom').
        smooth (bool): Use smooth scrolling.

    Returns:
        bool: True if scrolled successfully.
    """
    if isinstance(amount, str):
        amount = int(amount)
    tab = await _require_tab(browser_manager, instance_id)
    return await _with_cdp_timeout(
        dom_handler.scroll_page(tab, direction, amount, smooth), instance_id=instance_id
    )


@section_tool("element-interaction")
async def execute_script(
    instance_id: str,
    script: str,
    args: list[Any] | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """
    Execute JavaScript source in the active page and return its value.

    The default exec-family tool; prefer a sibling when it fits — `inject_and_execute_script`
    (specific execution context), `call_javascript_function`/`execute_function_sequence`
    (invoke defined functions), `execute_python_in_browser` (Python), `execute_cdp_command` (raw CDP).

    ⚠️ Async, non-blocking code only. The script runs on the page's main thread,
    so anything that blocks it freezes the whole tab and makes every later call
    time out. Specifically:
      • NEVER use synchronous XHR — `xhr.open(url, false)`. Use `await fetch(url)`.
      • NEVER use infinite/blocking loops — `while(true)`, `for(;;)`, busy-waits.
      • NEVER call `alert()` / `confirm()` / `prompt()` — they block automation.
      • To UPLOAD FILES, use the `upload_file` tool — do NOT fetch blobs or build
        base64/DataTransfer here (mixed-content/CORS limits and can freeze the page).
      • Keep scripts small (< ~100KB); don't inline large payloads.

    Args:
        instance_id (str): Browser instance ID.
        script (str): JavaScript to execute; non-blocking. Top-level 'return' OK.
        args (Optional[List[Any]]): Arguments passed to the script body.
        timeout_ms (Optional[int]): Max run time in ms (default 10000, max 60000).
            A blocking script is killed at this limit instead of hanging the tab.

    Returns:
        Dict[str, Any]: {"success": bool, "result": Any, "error": Optional[str]}.
    """
    rejection = _script_rejection_reason(script)
    if rejection:
        return {"success": False, "result": None, "error": rejection}
    tab = await _require_tab(browser_manager, instance_id)
    if timeout_ms is not None:
        timeout_s = (
            _clamp_timeout(timeout_ms, default=int(EXECUTE_SCRIPT_TIMEOUT * 1000))
            / 1000
        )
    else:
        timeout_s = EXECUTE_SCRIPT_TIMEOUT
    try:
        result = await _with_cdp_timeout(
            dom_handler.execute_script(tab, script, args),
            timeout=timeout_s,
            instance_id=instance_id,
        )
        return {"success": True, "result": result, "error": None}
    except Exception as e:
        raise ToolError(str(e))


@section_tool("element-interaction")
async def get_page_content(
    instance_id: str, include_frames: bool = False
) -> dict[str, Any]:
    """
    Get page HTML and text content.

    Args:
        instance_id (str): Browser instance ID.
        include_frames (bool): Include iframe information.

    Returns:
        Dict[str, Any]: Page content including HTML, text, and metadata.
    """
    tab = await _require_tab(browser_manager, instance_id)
    content = await _with_cdp_timeout(
        dom_handler.get_page_content(tab, include_frames), instance_id=instance_id
    )

    return response_handler.handle_response(
        content,
        "page_content",
        {"instance_id": instance_id, "include_frames": include_frames},
    )


@section_tool("element-interaction")
async def take_screenshot(
    instance_id: str,
    full_page: bool = False,
    format: str = "png",
    file_path: str | None = None,
) -> str | dict[str, Any]:
    """
    Take a screenshot of the page.

    Args:
        instance_id (str): Browser instance ID.
        full_page (bool): Capture full page (not just viewport).
        format (str): Image format ('png' or 'jpeg').
        file_path (Optional[str]): Optional file path to save screenshot to.

    Returns:
        Union[str, Dict]: File path if file_path provided, otherwise optimized base64 data or file info dict.
    """
    import io

    from PIL import Image

    tab = await _require_tab(browser_manager, instance_id)

    if file_path:
        save_path = Path(file_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        await _with_cdp_timeout(tab.save_screenshot(save_path), instance_id=instance_id)
        return f"Screenshot saved. AI agents should use the Read tool to view this image: {save_path.absolute()!s}"

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        await _with_cdp_timeout(tab.save_screenshot(tmp_path), instance_id=instance_id)

        with Image.open(tmp_path) as img:
            if img.mode in ("RGBA", "LA", "P") and format.lower() == "jpeg":
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(
                    img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None
                )
                img = background

            output_buffer = io.BytesIO()

            if format.lower() == "jpeg":
                img.save(output_buffer, format="JPEG", quality=85, optimize=True)
            else:
                img.save(output_buffer, format="PNG", optimize=True)

            compressed_bytes = output_buffer.getvalue()

            base64_size = len(compressed_bytes) * 1.33
            estimated_tokens = int(base64_size / 4)

            if estimated_tokens > 20000:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_filename = (
                    f"screenshot_{timestamp}_{instance_id[:8]}.{format.lower()}"
                )
                screenshot_path = response_handler.clone_dir / screenshot_filename

                with open(screenshot_path, "wb") as f:
                    f.write(compressed_bytes)

                file_size_kb = len(compressed_bytes) / 1024
                return {
                    "file_path": str(screenshot_path),
                    "filename": screenshot_filename,
                    "file_size_kb": round(file_size_kb, 2),
                    "estimated_tokens": estimated_tokens,
                    "reason": "Screenshot too large, automatically saved to file",
                    "message": f"Screenshot saved. AI agents should use the Read tool to view this image: {screenshot_path!s}",
                }

            return base64.b64encode(compressed_bytes).decode("utf-8")

    finally:
        if tmp_path.exists():
            os.unlink(tmp_path)


@mcp.resource("browser://{instance_id}/state")
async def get_browser_state_resource(instance_id: str) -> str:
    """
    Get current state of a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of the browser state or error message.
    """
    state = await browser_manager.get_page_state(instance_id)
    if state:
        return json.dumps(state.dict(), indent=2)
    raise InstanceNotFoundError(f"Instance not found: {instance_id}")


@mcp.resource("browser://{instance_id}/cookies")
async def get_cookies_resource(instance_id: str) -> str:
    """
    Get cookies for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of cookies or error message.
    """
    tab = await browser_manager.get_tab(instance_id)
    if tab:
        cookies = await network_interceptor.get_cookies(tab)
        return json.dumps(cookies, indent=2)
    raise InstanceNotFoundError(f"Instance not found: {instance_id}")


@mcp.resource("browser://{instance_id}/network")
async def get_network_resource(instance_id: str) -> str:
    """
    Get network requests for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of network requests.
    """
    requests = await network_interceptor.list_requests(instance_id)
    return json.dumps([req.dict() for req in requests], indent=2)


@mcp.resource("browser://{instance_id}/console")
async def get_console_resource(instance_id: str) -> str:
    """
    Get console logs for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of console logs or error message.
    """
    state = await browser_manager.get_page_state(instance_id)
    if state:
        return json.dumps(state.console_logs, indent=2)
    raise InstanceNotFoundError(f"Instance not found: {instance_id}")


if get_settings().xpool_safe_mode:
    DISABLED_SECTIONS.add("cdp-functions")
    apply_disabled_sections()


def build_arg_parser():
    """Construct the CLI parser for the ``python -m ... server`` entrypoint.

    Extracted to module scope so argument defaults (notably the HTTP bind host)
    are unit-testable without executing the server.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Stealth Browser MCP Server with "
            f"{sum(len(v) for v in SECTION_TOOLS.values())} tools"
        )
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=get_settings().port,
        help="Port for HTTP transport",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for HTTP transport. Defaults to loopback because "
        "the backend is unauthenticated and drives logged-in "
        "browser profiles; pass 0.0.0.0 only to deliberately "
        "expose it.",
    )

    parser.add_argument(
        "--disable-browser-management",
        action="store_true",
        help="Disable browser management tools (spawn, navigate, close, etc.)",
    )
    parser.add_argument(
        "--disable-element-interaction",
        action="store_true",
        help="Disable element interaction tools (click, type, scroll, etc.)",
    )
    parser.add_argument(
        "--disable-element-extraction",
        action="store_true",
        help="Disable element extraction tools (styles, structure, events, etc.)",
    )
    parser.add_argument(
        "--disable-file-extraction",
        action="store_true",
        help="Disable file-based extraction tools",
    )
    parser.add_argument(
        "--disable-network-debugging",
        action="store_true",
        help="Disable network debugging and interception tools",
    )
    parser.add_argument(
        "--disable-cdp-functions",
        action="store_true",
        help="Disable CDP function execution tools",
    )
    parser.add_argument(
        "--disable-progressive-cloning",
        action="store_true",
        help="Disable progressive element cloning tools",
    )
    parser.add_argument(
        "--disable-cookies-storage",
        action="store_true",
        help="Disable cookie and storage management tools",
    )
    parser.add_argument(
        "--disable-tabs", action="store_true", help="Disable tab management tools"
    )
    parser.add_argument(
        "--disable-debugging",
        action="store_true",
        help="Disable debug and system tools",
    )
    parser.add_argument(
        "--disable-dynamic-hooks",
        action="store_true",
        help="Disable dynamic network hook system",
    )

    parser.add_argument(
        "--minimal",
        action="store_true",
        help="Enable only core browser management and element interaction (disable everything else)",
    )
    parser.add_argument(
        "--list-sections",
        action="store_true",
        help="List all available tool sections and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=DEBUG_LOGGING_ENABLED,
        help="Enable debug logging to stderr",
    )
    parser.add_argument(
        "--xpool-safe",
        action="store_true",
        default=get_settings().xpool_safe_mode,
        help="Enable xpool-safe surface (disables cdp-functions tools that trigger Runtime.enable)",
    )

    return parser


if __name__ == "__main__":
    bootstrap_backend_process_logging()
    args = build_arg_parser().parse_args()

    if args.debug and not debug_logger._enabled:
        debug_logger.enable()

    if args.list_sections:
        # F-108: per-section counts + total are DERIVED from the live
        # SECTION_TOOLS registry, never hand-typed, so they can't drift when a
        # tool is added or removed. The descriptions are cosmetic labels.
        section_descriptions = {
            "browser-management": "Core browser operations",
            "element-interaction": "Page interaction and element manipulation",
            "element-extraction": "Element cloning and extraction",
            "file-extraction": "File-based extraction tools",
            "network-debugging": "Network monitoring and interception",
            "cdp-functions": "Chrome DevTools Protocol function execution",
            "progressive-cloning": "Advanced element cloning system",
            "cookies-storage": "Cookie and storage management",
            "tabs": "Tab management",
            "debugging": "Debug and system tools",
            "dynamic-hooks": "AI-powered network hook system",
        }
        print("Available tool sections:")
        for section, tools in SECTION_TOOLS.items():
            label = section_descriptions.get(section, section)
            print(f"  {section}: {label} ({len(tools)} tools)")
        print(f"\nTotal: {sum(len(v) for v in SECTION_TOOLS.values())} tools")
        print("\nUse --disable-<section-name> to disable specific sections")
        print("Use --minimal to enable only core functionality")
        sys.exit(0)

    if args.minimal:
        DISABLED_SECTIONS.update(
            [
                "element-extraction",
                "file-extraction",
                "network-debugging",
                "cdp-functions",
                "progressive-cloning",
                "cookies-storage",
                "tabs",
                "debugging",
                "dynamic-hooks",
            ]
        )

    if args.disable_browser_management:
        DISABLED_SECTIONS.add("browser-management")
    if args.disable_element_interaction:
        DISABLED_SECTIONS.add("element-interaction")
    if args.disable_element_extraction:
        DISABLED_SECTIONS.add("element-extraction")
    if args.disable_file_extraction:
        DISABLED_SECTIONS.add("file-extraction")
    if args.disable_network_debugging:
        DISABLED_SECTIONS.add("network-debugging")
    if args.disable_cdp_functions:
        DISABLED_SECTIONS.add("cdp-functions")
    if args.disable_progressive_cloning:
        DISABLED_SECTIONS.add("progressive-cloning")
    if args.disable_cookies_storage:
        DISABLED_SECTIONS.add("cookies-storage")
    if args.disable_tabs:
        DISABLED_SECTIONS.add("tabs")
    if args.disable_debugging:
        DISABLED_SECTIONS.add("debugging")
    if args.disable_dynamic_hooks:
        DISABLED_SECTIONS.add("dynamic-hooks")

    if args.xpool_safe:
        DISABLED_SECTIONS.add("cdp-functions")

    apply_disabled_sections()

    if DISABLED_SECTIONS:
        debug_logger.log_info(
            "server",
            "startup",
            f"Disabled tool sections: {', '.join(sorted(DISABLED_SECTIONS))}",
        )

    # Ship errors to Sentry (on by default; opt out: STEALTH_MCP_NO_ERROR_REPORTING).
    sentry_init()

    # B1: bind app_lifespan's teardown policy to the serve transport. HTTP runs
    # the lifespan per MCP session, so session-exit teardown must be a no-op.
    _SERVE_TRANSPORT = args.transport

    if args.transport == "http":
        mcp.run(
            transport="http",
            host=args.host,
            port=args.port,
            uvicorn_config=backend_uvicorn_config(),
        )
    else:
        mcp.run(transport="stdio")
