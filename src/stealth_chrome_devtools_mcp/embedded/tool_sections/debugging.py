"""The ``debugging`` tools. See ``tool_sections/__init__.py`` for the contract.

plan_SERVERSPLIT slice 3, and the first slice that RELOCATES a body rather than
only moving it: ``validate_browser_environment_tool`` was physically filed among
the element-extraction bodies in ``server.py`` while being registered into
``debugging``. It joins its own section here, which is what makes module ↔
section a clean 1:1 and lets ``SECTION`` be a per-module constant. Pure
relocation — no rename, no behaviour change, and the served surface is unmoved
(``tests/goldens/tool_surface.json``).

This is also the section the export-timeout message pin follows: the
``MSG_EXPORT_TIMEOUT`` string lives in ``export_debug_logs`` below, and
``tests/test_observability.py`` scans it through ``tests/source_scan.py``'s
derived file set rather than one hard-wired module — so the guard travels with
the code instead of passing vacuously over the file it left.

The two ``asyncio.wait_for`` calls keep their ``# F-164 non-CDP`` markers
verbatim; ``tests/test_cdp_timeout.py`` scans the same derived set for them.
"""

import asyncio
from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.platform_utils import (
    get_platform_info,
    validate_browser_environment,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

SECTION = "debugging"


async def get_debug_view(
    max_errors: int = 50,
    max_warnings: int = 50,
    max_info: int = 50,
    include_all: bool = False,
) -> dict[str, Any]:
    """
    Get comprehensive debug view with all logged errors and statistics.

    Args:
        max_errors (int): Maximum number of errors to include (default: 50).
        max_warnings (int): Maximum number of warnings to include (default: 50).
        max_info (int): Maximum number of info logs to include (default: 50).
        include_all (bool): Include all logs regardless of limits (default: False).

    Returns:
        Dict[str, Any]: Debug information including errors, warnings, and statistics.
    """
    debug_data = rt.debug_logger.get_debug_view_paginated(
        max_errors=max_errors if not include_all else None,
        max_warnings=max_warnings if not include_all else None,
        max_info=max_info if not include_all else None,
    )
    return debug_data


async def clear_debug_view() -> bool:
    """
    Clear all debug logs and statistics with timeout protection.

    Returns:
        bool: True if cleared successfully.
    """
    try:
        # F-164 non-CDP: guards a thread-offloaded debug-logger clear (no CDP);
        # a timeout is this tool's documented ``-> bool`` False path, not a CDP
        # hang, so it is deliberately not routed through _with_cdp_timeout.
        await asyncio.wait_for(
            asyncio.to_thread(rt.debug_logger.clear_debug_view_safe), timeout=10.0
        )
        return True
    except TimeoutError:
        return False


async def export_debug_logs(
    filename: str = "debug_log.json",
    max_errors: int = 100,
    max_warnings: int = 100,
    max_info: int = 100,
    include_all: bool = False,
    format: str = "auto",
) -> str:
    """
    Export debug logs to a file using the fastest available method with timeout protection.

    Args:
        filename (str): Name of the file to export to.
        max_errors (int): Maximum number of errors to export (default: 100).
        max_warnings (int): Maximum number of warnings to export (default: 100).
        max_info (int): Maximum number of info logs to export (default: 100).
        include_all (bool): Include all logs regardless of limits (default: False).
        format (str): Export format: 'json', 'pickle', 'gzip-pickle', 'auto' (default: 'auto').
                     'auto' chooses fastest format based on data size:
                     - Small data (<100 items): JSON (human readable)
                     - Medium data (100-1000 items): Pickle (fast binary)
                     - Large data (>1000 items): Gzip-Pickle (fastest, compressed)

    Returns:
        str: Path to the exported file.
    """
    try:
        # F-164 non-CDP: guards a thread-offloaded file export (no CDP); a timeout
        # returns this tool's ``-> str`` guidance string, not a CDP-hang error, so
        # it is deliberately not routed through _with_cdp_timeout.
        filepath = await asyncio.wait_for(
            asyncio.to_thread(
                rt.debug_logger.export_to_file_paginated,
                filename,
                max_errors if not include_all else None,
                max_warnings if not include_all else None,
                max_info if not include_all else None,
                format,
            ),
            timeout=30.0,
        )
        return filepath
    except TimeoutError:
        return "Export timeout - file too large. Try with smaller limits or 'gzip-pickle' format."


async def get_debug_lock_status() -> dict[str, Any]:
    """
    Get current debug logger lock status for debugging hanging exports.

    Returns:
        Dict[str, Any]: Lock status information.
    """
    try:
        return rt.debug_logger.get_lock_status()
    except Exception as e:
        raise ToolError(str(e))


async def validate_browser_environment_tool() -> dict[str, Any]:
    """
    Validate browser environment and diagnose potential issues.

    Returns:
        Dict[str, Any]: Environment validation results with platform info and recommendations
    """
    try:
        return validate_browser_environment()
    except Exception as e:
        return {
            "error": str(e),
            "platform_info": get_platform_info(),
            "is_ready": False,
            "issues": [f"Validation failed: {e!s}"],
            "warnings": [],
        }


#: Surface order — the order ``SECTION_TOOLS["debugging"]`` already holds, which
#: is registration order in ``server.py`` and NOT this file's former physical
#: order: ``validate_browser_environment_tool`` was decorated last, three hundred
#: lines away among the element-extraction bodies.
TOOLS = (
    get_debug_view,
    clear_debug_view,
    export_debug_logs,
    get_debug_lock_status,
    validate_browser_environment_tool,
)
