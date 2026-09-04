"""THE one home for what a tool body reaches for beyond its own arguments.

Every tool body resolves its dependencies as ``rt.<name>`` against THIS module at
call time, from whichever ``tool_sections`` module it lives in. That is what gives
the whole 94-tool surface exactly one patchable home (``tests/conftest.py``'s
``patched_server``) instead of one per section module: a module attribute is looked
up at call time, so ``monkeypatch.setattr(tool_runtime, "browser_manager", fake)``
reaches every body no matter which file it lives in.

Three kinds of thing live here and nothing else:
  * the stateful singletons a tool drives,
  * the tuned knobs read at call time,
  * the guards that enforce those knobs.

It imports no ``mcp``, no ``registry``, no ``server`` — it is a leaf, so it is safe
to import from every section module and from ``server.py`` alike (CLAUDE.md
convention 1). ``_with_cdp_timeout`` reads ``CDP_OPERATION_TIMEOUT`` from its OWN
module globals, which is exactly why the knobs sit beside it: co-locating them
keeps that read patchable at the one home. ``tool_errors.py`` was the other
candidate and is ruled out by its own stated contract — it declares three times
over that it imports neither ``server`` nor ``settings`` (it is why
``_require_landing_ok`` takes ``timeout`` as a parameter).

``__all__`` is the patchable surface, stated once.
``tests/test_tool_sections_contract.py`` derives the migration alias pin from it and
``tests/conftest.py``'s ``patched_server`` patches against this module, so a
singleton added here cannot be forgotten by either.

plan_SERVERSPLIT slice 0: created by the mechanism slice, zero tools moved. The
tool bodies still live in ``server.py`` and reach these names through an alias
import block there; slices 1-11 move them into ``tool_sections/`` and the aliases
are deleted in slice 12.
"""

import asyncio
import re

from stealth_chrome_devtools_mcp.embedded import clone_storage, display_context
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.cdp_element_cloner import cdp_element_cloner
from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import (
    CDPFunctionExecutor,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_ai_interface import (
    dynamic_hook_ai,
)
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import dynamic_hook_system
from stealth_chrome_devtools_mcp.embedded.file_based_element_cloner import (
    file_based_element_cloner,
)
from stealth_chrome_devtools_mcp.embedded.in_memory_storage import in_memory_storage
from stealth_chrome_devtools_mcp.embedded.network_interceptor import NetworkInterceptor
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.progressive_element_cloner import (
    progressive_element_cloner,
)
from stealth_chrome_devtools_mcp.embedded.response_handler import response_handler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings

__all__ = [
    "CDP_OPERATION_TIMEOUT",
    "EXECUTE_SCRIPT_TIMEOUT",
    "MAX_TIMEOUT_MS",
    "MAX_USER_SCRIPT_BYTES",
    "_BLOCKING_SCRIPT_PATTERNS",
    "_clamp_timeout",
    "_script_rejection_reason",
    "_with_cdp_timeout",
    "browser_manager",
    "cdp_element_cloner",
    "cdp_function_executor",
    "clone_storage",
    "debug_logger",
    "display_context",
    "dom_handler",
    "dynamic_hook_ai",
    "dynamic_hook_system",
    "file_based_element_cloner",
    "in_memory_storage",
    "network_interceptor",
    "process_cleanup",
    "progressive_element_cloner",
    "response_handler",
]

CDP_OPERATION_TIMEOUT = get_settings().cdp_operation_timeout_seconds
MAX_TIMEOUT_MS = 60_000

# User-supplied JS (execute_script) gets a short, dedicated timeout so a blocking
# script fails fast instead of freezing the tab for the full CDP_OPERATION_TIMEOUT
# window and stalling every subsequent call.
EXECUTE_SCRIPT_TIMEOUT = get_settings().execute_script_timeout_seconds

# Reject user scripts larger than this. Huge inline payloads (e.g. base64-encoded
# files) overflow the transport and are almost always an upload hack — callers
# should use the upload_file tool instead.
MAX_USER_SCRIPT_BYTES = get_settings().max_user_script_bytes

# High-confidence denylist of patterns that block the renderer's main thread or
# overflow the page. This is NOT a JS sandbox — just a guard against the handful
# of foot-guns that wedge the browser for every later call.
_BLOCKING_SCRIPT_PATTERNS = [
    (
        re.compile(r"\.open\s*\([^)]*,\s*false\s*\)", re.IGNORECASE),
        "Synchronous XMLHttpRequest (xhr.open(url, false)) blocks the page's main "
        "thread and freezes every later call. Use 'await fetch(url)' instead.",
    ),
    (
        re.compile(r"while\s*\(\s*(?:true|1)\s*\)"),
        "Infinite 'while(true)' loop freezes the renderer. Use a bounded loop or an "
        "async delay: 'await new Promise(r => setTimeout(r, ms))'.",
    ),
    (
        re.compile(r"for\s*\(\s*;\s*;\s*\)"),
        "Infinite 'for(;;)' loop freezes the renderer. Use a bounded loop instead.",
    ),
    (
        re.compile(r"\b(?:alert|confirm|prompt)\s*\("),
        "Modal dialogs (alert/confirm/prompt) block the renderer and cannot be "
        "dismissed by automation. Remove them.",
    ),
]


def _script_rejection_reason(script: str) -> str | None:
    """Return a corrective message if a user script is unsafe to run, else None.

    Guards against the common foot-guns that freeze the tab or overflow the
    transport (sync XHR, infinite loops, blocking dialogs, oversized payloads).
    Intentionally small and high-confidence — not a JavaScript sandbox.
    """
    if not isinstance(script, str):
        return None
    size = len(script.encode("utf-8", errors="ignore"))
    if size > MAX_USER_SCRIPT_BYTES:
        return (
            f"Script too large ({size} bytes > {MAX_USER_SCRIPT_BYTES} limit). "
            "Inline payloads such as base64-encoded files overflow the transport — "
            "use the 'upload_file' tool for files, or a file-based approach."
        )
    for pattern, message in _BLOCKING_SCRIPT_PATTERNS:
        if pattern.search(script):
            return f"Rejected: {message}"
    return None


def _clamp_timeout(timeout_ms: int, default: int = 30_000) -> int:
    """Clamp a user-provided timeout (ms) to [1, MAX_TIMEOUT_MS]."""
    if isinstance(timeout_ms, str):
        timeout_ms = int(timeout_ms)
    return max(1, min(timeout_ms, MAX_TIMEOUT_MS))


async def _with_cdp_timeout(coro, timeout: float = 0, instance_id: str = ""):
    """Wrap a CDP coroutine with asyncio.wait_for to prevent infinite hangs.

    When a Chrome DevTools Protocol connection is stale or dead, awaiting a
    CDP operation blocks forever.  This wrapper raises a clear error after
    *timeout* seconds so the caller (and the MCP client) gets a response
    instead of hanging indefinitely.
    """
    t = timeout or CDP_OPERATION_TIMEOUT
    try:
        return await asyncio.wait_for(coro, timeout=t)
    except TimeoutError:
        tag = f" (instance {instance_id})" if instance_id else ""
        raise ToolError(
            f"CDP operation timed out after {t:.0f}s{tag}. "
            "The browser may have crashed or the connection dropped. "
            "Try closing the instance with close_instance and spawning a new one."
        )


# The four constructed singletons. They move here from ``server.py``'s module body
# so that the canonical import, the bare-name spec load and the runpy ``__main__``
# load share ONE of each instead of holding three (plan_SERVERSPLIT §7 R4);
# ``BrowserManager.start_idle_reaper``/``stop_idle_reaper`` are already idempotent,
# which is the axis that change is felt on.
browser_manager = BrowserManager()
network_interceptor = NetworkInterceptor()
dom_handler = DOMHandler()
cdp_function_executor = CDPFunctionExecutor()
