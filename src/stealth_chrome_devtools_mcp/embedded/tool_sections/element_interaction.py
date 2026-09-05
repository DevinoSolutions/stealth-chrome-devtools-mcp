"""The ``element-interaction`` tools. See ``tool_sections/__init__.py`` for the contract.

plan_SERVERSPLIT slice 11 — the LAST section to leave ``server.py`` and the
largest: twelve bodies covering every way a caller touches the page (query,
click, type, paste, select, upload, scroll, wait, read and screenshot) plus
``execute_script``, the default exec-family tool.

``execute_script`` is why this section went last. It is the only body that reads
THREE runtime knobs at once — ``rt._script_rejection_reason`` (and through it
``MAX_USER_SCRIPT_BYTES``), ``rt.EXECUTE_SCRIPT_TIMEOUT`` and
``rt._clamp_timeout`` — and every one of them is resolved against
``tool_runtime`` at CALL time, which is what keeps
``tests/conftest.py``'s ``patched_server`` reaching a guard whose body no longer
lives in ``server.py``. The guard itself never moved: it has lived in
``tool_runtime`` since slice 0, so what this slice proves is the LAST caller
reaching it from a section module.

With this slice ``server.py`` holds no tool bodies at all — only ``mcp``, the
registry, ``app_lifespan``, the four ``@mcp.resource`` handlers, the binding loop
that registers all 94 of these functions once per execution of its module body,
the xpool-safe gate, ``build_arg_parser`` and the ``__main__`` block, plus the
migration alias block slice 12 deletes.

Bodies moved verbatim: the only edits are the dropped registration decorator
(contract rule 2) and the rewrite of the singleton/knob reads to ``rt.<name>``
(contract rule 3). Docstrings and signatures are byte-identical — FastMCP
surfaces them and ``tests/goldens/tool_surface.json`` is a HARD golden for this
migration — and so are ``take_screenshot``'s two function-local imports (``io``
and ``PIL.Image``), carried as they stood rather than hoisted, because hoisting
them would put Pillow on this module's import graph at binding time.
"""

import base64
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError, _require_tab

SECTION = "element-interaction"


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
    tab = await _require_tab(rt.browser_manager, instance_id)
    rt.debug_logger.log_info(
        "Server",
        "query_elements",
        f"Received limit parameter: {limit} (type: {type(limit)})",
    )
    elements = await rt._with_cdp_timeout(
        rt.dom_handler.query_elements(tab, selector, text_filter, visible_only, limit),
        instance_id=instance_id,
    )
    rt.debug_logger.log_info(
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
            rt.debug_logger.log_info(
                "Server",
                "query_elements",
                f"Converted element {i + 1} to dict: {list(elem_dict.keys())}",
            )
        except Exception as e:
            rt.debug_logger.log_error(
                "Server", "query_elements", e, {"element_index": i}
            )
    rt.debug_logger.log_info(
        "Server", "query_elements", f"Returning {len(result)} results to MCP client"
    )
    return result or []


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
    timeout = rt._clamp_timeout(timeout, default=10_000)
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.dom_handler.click_element(tab, selector, text_match, timeout),
        instance_id=instance_id,
    )


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
    timeout = rt._clamp_timeout(timeout, default=10_000)
    paths = [file_paths] if isinstance(file_paths, str) else list(file_paths)
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.dom_handler.upload_file(tab, selector, paths, timeout),
        instance_id=instance_id,
    )


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
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.dom_handler.type_text(
            tab, selector, text, clear_first, delay_ms, parse_newlines, shift_enter
        ),
        timeout=60,
        instance_id=instance_id,
    )


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
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.dom_handler.paste_text(tab, selector, text, clear_first),
        instance_id=instance_id,
    )


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
    tab = await _require_tab(rt.browser_manager, instance_id)

    converted_index = None
    if index is not None:
        try:
            converted_index = int(index)
        except (ValueError, TypeError):
            raise ToolError(f"Invalid index value: {index}. Must be a number.")

    return await rt._with_cdp_timeout(
        rt.dom_handler.select_option(tab, selector, value, text, converted_index),
        instance_id=instance_id,
    )


async def get_element_state(instance_id: str, selector: str) -> dict[str, Any]:
    """
    Get complete state of an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.

    Returns:
        Dict[str, Any]: Element state including attributes, style, position, etc.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.dom_handler.get_element_state(tab, selector), instance_id=instance_id
    )


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
    timeout = rt._clamp_timeout(timeout, default=30_000)
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.dom_handler.wait_for_element(tab, selector, timeout, visible, text_content),
        timeout=max(timeout / 1000 + 5, rt.CDP_OPERATION_TIMEOUT),
        instance_id=instance_id,
    )


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
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.dom_handler.scroll_page(tab, direction, amount, smooth),
        instance_id=instance_id,
    )


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
    rejection = rt._script_rejection_reason(script)
    if rejection:
        return {"success": False, "result": None, "error": rejection}
    tab = await _require_tab(rt.browser_manager, instance_id)
    if timeout_ms is not None:
        timeout_s = (
            rt._clamp_timeout(timeout_ms, default=int(rt.EXECUTE_SCRIPT_TIMEOUT * 1000))
            / 1000
        )
    else:
        timeout_s = rt.EXECUTE_SCRIPT_TIMEOUT
    try:
        result = await rt._with_cdp_timeout(
            rt.dom_handler.execute_script(tab, script, args),
            timeout=timeout_s,
            instance_id=instance_id,
        )
        return {"success": True, "result": result, "error": None}
    except Exception as e:
        raise ToolError(str(e))


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
    tab = await _require_tab(rt.browser_manager, instance_id)
    content = await rt._with_cdp_timeout(
        rt.dom_handler.get_page_content(tab, include_frames), instance_id=instance_id
    )

    return rt.response_handler.handle_response(
        content,
        "page_content",
        {"instance_id": instance_id, "include_frames": include_frames},
    )


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

    tab = await _require_tab(rt.browser_manager, instance_id)

    if file_path:
        save_path = Path(file_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        await rt._with_cdp_timeout(
            tab.save_screenshot(save_path), instance_id=instance_id
        )
        return f"Screenshot saved. AI agents should use the Read tool to view this image: {save_path.absolute()!s}"

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        await rt._with_cdp_timeout(
            tab.save_screenshot(tmp_path), instance_id=instance_id
        )

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
                screenshot_path = rt.response_handler.clone_dir / screenshot_filename

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


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in
#: ``SECTION_TOOLS["element-interaction"]``.
TOOLS = (
    query_elements,
    click_element,
    upload_file,
    type_text,
    paste_text,
    select_option,
    get_element_state,
    wait_for_element,
    scroll_page,
    execute_script,
    get_page_content,
    take_screenshot,
)
