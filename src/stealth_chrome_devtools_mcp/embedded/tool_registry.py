"""Tool registry: the one registration + section-gating mechanism for the MCP
tool surface (F-101/F-505/F-612).

``server.py`` builds a :class:`ToolRegistry` around the FastMCP app and exposes
its ``section_tool`` decorator; every tool registers through that single path,
which stamps M3's per-call correlation id (``with_correlation_id``, F-308) and
records the tool's section in ``SECTION_TOOLS``. ``apply_disabled_sections``
unregisters whole sections at startup per the ``--disable-sections`` / env
policy.

Verb taxonomy (F-760) — the ONE tool-naming rule, documented here and handed to
M14. Tools are NOT renamed in Ph1; renames happen once, at the Ph2 per-section
move, from this taxonomy:

* ``list_*``                              — enumerate many
* ``get_*``                               — fetch one / detail
* ``create_*`` / ``spawn_*``              — make
* ``execute_*`` / ``call_*``              — run code
* ``extract_*`` / ``clone_*``             — element capture
* ``set_*`` / ``modify_*`` / ``clear_*``  — mutate
* ``discover_*`` / ``inspect_*``          — introspect

Two surfaces, one backend (F-700): the MCP tool surface (this registry) drives
the browser for an AI client; the ops ``cli`` inspects and operates the same
backend for a human and never reimplements browser logic. They are deliberately
separate surfaces over one backend — not merged.
"""

from collections import defaultdict

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.logging_setup import with_correlation_id

DISABLED_SECTIONS: set[str] = set()
SECTION_TOOLS: dict[str, list[str]] = defaultdict(list)


def is_section_enabled(section: str) -> bool:
    """Check if a tool section is enabled."""
    return section not in DISABLED_SECTIONS


class ToolRegistry:
    """Owns the ``section_tool`` decorator + section gating over a FastMCP app.

    Constructed with the ``mcp`` instance (passed in, not imported — this keeps
    the module free of a ``server.py`` import cycle). The decorator registers
    each tool through ``mcp.tool`` after wrapping it with M3's correlation-id
    stamp; ``apply_disabled_sections`` removes whole sections via
    ``mcp.remove_tool``.
    """

    def __init__(self, mcp) -> None:
        self._mcp = mcp

    def section_tool(self, section: str):
        """Decorator that registers tools and tracks section membership."""

        def decorator(func):
            # Idempotent by design: embedded/server.py's module body runs more
            # than once per process (canonical import, plus the entry point's
            # runpy.run_path of server.py; see embedded/__init__.py). Since
            # SECTION_TOOLS now lives in this one shared module rather than per
            # server-module, a re-execution must not double-count a tool, so
            # record each name at most once per section.
            names = SECTION_TOOLS[section]
            if func.__name__ not in names:
                names.append(func.__name__)
            return self._mcp.tool(with_correlation_id(func))

        return decorator

    def apply_disabled_sections(self) -> None:
        """Apply section disable rules by unregistering tools from FastMCP."""
        for section in sorted(DISABLED_SECTIONS):
            for tool_name in SECTION_TOOLS.get(section, []):
                try:
                    self._mcp.remove_tool(tool_name)
                except Exception as e:
                    # Tool may already be removed by another section policy.
                    debug_logger.log_debug("server", "apply_disabled_sections", str(e))
                    continue
