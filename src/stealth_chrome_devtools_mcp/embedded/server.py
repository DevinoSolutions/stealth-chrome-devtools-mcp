"""Main MCP server for browser automation."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.logging_setup import (
    backend_uvicorn_config,
    bootstrap_backend_process_logging,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import InstanceNotFoundError
from stealth_chrome_devtools_mcp.embedded.tool_registry import (
    DISABLED_SECTIONS,
    SECTION_TOOLS,  # used by --list-sections + the derived tool-count (F-108); also re-exported as server.SECTION_TOOLS
    ToolRegistry,
)
from stealth_chrome_devtools_mcp.embedded.tool_runtime import (
    _with_cdp_timeout,  # noqa: F401  plan_SERVERSPLIT slice 11 — kept for tests/test_observability.py's four `w15_server._with_cdp_timeout` reads; re-pointed and deleted in slice 12
    browser_manager,
    clone_storage,  # noqa: F401  plan_SERVERSPLIT slice 10 — kept for tests/test_clone_storage.py's "server delegates via the imported module" pin; deleted in slice 12
    debug_logger,
    network_interceptor,
)
from stealth_chrome_devtools_mcp.embedded.tool_sections import SECTION_MODULES
from stealth_chrome_devtools_mcp.observability import sentry_init
from stealth_chrome_devtools_mcp.settings import get_settings

# plan_SERVERSPLIT slice 0: the knobs, the script/timeout guards and the four
# constructed singletons now live in ``tool_runtime`` — THE one patchable home
# (a module attribute resolved at call time, in a module that is imported once
# rather than re-executed by every runpy/spec load). The block above is a
# MIGRATION ALIAS block. An alias is pruned by the slice that takes its LAST
# consumer out of this file — prod or test — which is why it shrank with every
# slice. As of slice 11 this file holds NO tool bodies, so what is left is only
# what still has a named reader: ``browser_manager`` / ``network_interceptor`` /
# ``debug_logger`` for the four ``@mcp.resource`` handlers, ``app_lifespan`` and
# the ``__main__`` block, plus the two suppressed above that exist purely to keep
# a test's attribute read working until slice 12 re-points it. Note the aliases
# bind the OBJECT at import time; ``tests/conftest.py``'s ``patched_server``
# therefore still patches both homes, and
# ``tests/test_tool_sections_contract.py``'s alias-identity pin fails the moment
# the two could diverge. ``app_lifespan`` below deliberately does NOT use them —
# it reads ``rt.*`` so a patched singleton reaches the lifespan too.


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
