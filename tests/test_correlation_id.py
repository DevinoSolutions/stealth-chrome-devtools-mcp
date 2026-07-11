"""Pinning tests for M3-5: correlation id via the section_tool chokepoint
(F-308), including the MANDATORY tools/list schema-snapshot pin.

This is the plan's highest-uncertainty step (risk #5): ``section_tool``'s
wrapper must set/reset ``correlation_id_var`` around every one of the 96
registered tool calls without FastMCP losing any tool's JSON schema. 91 of
the 96 registered functions are ``async def`` and 5 are plain ``def``
(``get_hook_documentation`` et al.), so the wrapper must preserve both.

``TestToolsListSchemaSnapshot`` pins the exact ``inputSchema``/``name`` FastMCP
produces for one representative tool per section, captured from the REAL
pre-change tree (``git log`` show this commit's parent for the capture
script) before ``section_tool`` was touched. If this test goes red, the
fallback in plan_M3 risk #5 is ``wrapper.__signature__ =
inspect.signature(func)``.
"""

import asyncio
import json
import logging

import pytest

from stealth_chrome_devtools_mcp.embedded.logging_setup import (
    CorrelationIdFilter,
    correlation_id_var,
    with_correlation_id,
)

# Captured from the pre-change tree: one representative tool's real FastMCP
# name + inputSchema per section (11 sections). See M3-5's commit message for
# the capture method. A structural change here means section_tool's wrapper
# altered what a real MCP client sees in tools/list.
_GOLDEN_SCHEMA_JSON = r"""
{
  "browser-management": {
    "name": "spawn_browser",
    "inputSchema": {
      "properties": {
        "block_resources": {"default": null, "items": {"type": "string"}, "title": "Block Resources", "type": "array"},
        "browser_args": {"default": null, "items": {"type": "string"}, "title": "Browser Args", "type": "array"},
        "extra_headers": {"additionalProperties": {"type": "string"}, "default": null, "title": "Extra Headers", "type": "object"},
        "headless": {"default": false, "title": "Headless", "type": "boolean"},
        "idle_timeout_seconds": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null, "title": "Idle Timeout Seconds"},
        "proxy": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Proxy"},
        "sandbox": {"anyOf": [{}, {"type": "null"}], "default": null, "title": "Sandbox"},
        "timezone_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Timezone Id"},
        "user_agent": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "User Agent"},
        "user_data_dir": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "User Data Dir"},
        "viewport_height": {"default": 1080, "title": "Viewport Height", "type": "integer"},
        "viewport_width": {"default": 1920, "title": "Viewport Width", "type": "integer"}
      },
      "type": "object"
    }
  },
  "cdp-functions": {
    "name": "list_cdp_commands",
    "inputSchema": {"properties": {}, "type": "object"}
  },
  "cookies-storage": {
    "name": "get_cookies",
    "inputSchema": {
      "properties": {
        "instance_id": {"title": "Instance Id", "type": "string"},
        "urls": {"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": null, "title": "Urls"}
      },
      "required": ["instance_id"],
      "type": "object"
    }
  },
  "debugging": {
    "name": "get_debug_view",
    "inputSchema": {
      "properties": {
        "include_all": {"default": false, "title": "Include All", "type": "boolean"},
        "max_errors": {"default": 50, "title": "Max Errors", "type": "integer"},
        "max_info": {"default": 50, "title": "Max Info", "type": "integer"},
        "max_warnings": {"default": 50, "title": "Max Warnings", "type": "integer"}
      },
      "type": "object"
    }
  },
  "dynamic-hooks": {
    "name": "create_dynamic_hook",
    "inputSchema": {
      "properties": {
        "function_code": {"title": "Function Code", "type": "string"},
        "instance_ids": {"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": null, "title": "Instance Ids"},
        "name": {"title": "Name", "type": "string"},
        "priority": {"default": 100, "title": "Priority", "type": "integer"},
        "requirements": {"additionalProperties": true, "title": "Requirements", "type": "object"}
      },
      "required": ["name", "requirements", "function_code"],
      "type": "object"
    }
  },
  "element-extraction": {
    "name": "extract_element_styles",
    "inputSchema": {
      "properties": {
        "include_computed": {"default": true, "title": "Include Computed", "type": "boolean"},
        "include_css_rules": {"default": true, "title": "Include Css Rules", "type": "boolean"},
        "include_inheritance": {"default": false, "title": "Include Inheritance", "type": "boolean"},
        "include_pseudo": {"default": true, "title": "Include Pseudo", "type": "boolean"},
        "instance_id": {"title": "Instance Id", "type": "string"},
        "selector": {"title": "Selector", "type": "string"}
      },
      "required": ["instance_id", "selector"],
      "type": "object"
    }
  },
  "element-interaction": {
    "name": "query_elements",
    "inputSchema": {
      "properties": {
        "instance_id": {"title": "Instance Id", "type": "string"},
        "limit": {"anyOf": [{}, {"type": "null"}], "default": null, "title": "Limit"},
        "selector": {"title": "Selector", "type": "string"},
        "text_filter": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Text Filter"},
        "visible_only": {"default": true, "title": "Visible Only", "type": "boolean"}
      },
      "required": ["instance_id", "selector"],
      "type": "object"
    }
  },
  "file-extraction": {
    "name": "clone_element_to_file",
    "inputSchema": {
      "properties": {
        "extraction_options": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Extraction Options"},
        "instance_id": {"title": "Instance Id", "type": "string"},
        "selector": {"title": "Selector", "type": "string"}
      },
      "required": ["instance_id", "selector"],
      "type": "object"
    }
  },
  "network-debugging": {
    "name": "list_network_requests",
    "inputSchema": {
      "properties": {
        "filter_type": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Filter Type"},
        "instance_id": {"title": "Instance Id", "type": "string"}
      },
      "required": ["instance_id"],
      "type": "object"
    }
  },
  "progressive-cloning": {
    "name": "clone_element_progressive",
    "inputSchema": {
      "properties": {
        "include_children": {"default": true, "title": "Include Children", "type": "boolean"},
        "instance_id": {"title": "Instance Id", "type": "string"},
        "selector": {"title": "Selector", "type": "string"}
      },
      "required": ["instance_id", "selector"],
      "type": "object"
    }
  },
  "tabs": {
    "name": "list_tabs",
    "inputSchema": {
      "properties": {"instance_id": {"title": "Instance Id", "type": "string"}},
      "required": ["instance_id"],
      "type": "object"
    }
  }
}
"""


@pytest.fixture()
def captured_backend_records():
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("stealth.backend")
    handler = _ListHandler()
    handler.addFilter(CorrelationIdFilter())
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


class TestWithCorrelationIdUnit:
    """Direct unit tests of the wrapper itself, independent of FastMCP."""

    async def test_async_function_gets_id_and_resets_after(self):
        assert correlation_id_var.get() == "-"

        @with_correlation_id
        async def _tool():
            return correlation_id_var.get()

        result = await _tool()
        assert result != "-"
        assert correlation_id_var.get() == "-"

    def test_sync_function_gets_id_and_resets_after(self):
        assert correlation_id_var.get() == "-"

        @with_correlation_id
        def _tool():
            return correlation_id_var.get()

        result = _tool()
        assert result != "-"
        assert correlation_id_var.get() == "-"

    async def test_concurrent_async_calls_get_distinct_ids(self):
        # ContextVar isolation across asyncio tasks (plan_M3 risk #6).
        @with_correlation_id
        async def _tool():
            await asyncio.sleep(0.02)
            return correlation_id_var.get()

        id_a, id_b = await asyncio.gather(_tool(), _tool())
        assert id_a != "-"
        assert id_b != "-"
        assert id_a != id_b

    def test_wraps_preserves_name_doc_and_signature(self):
        import inspect

        def _original(a: int, b: str = "x") -> str:
            """Original docstring."""
            return b * a

        wrapped = with_correlation_id(_original)
        assert wrapped.__name__ == "_original"
        assert wrapped.__doc__ == "Original docstring."
        assert inspect.signature(wrapped) == inspect.signature(_original)

    async def test_async_wraps_preserves_name_doc_and_signature(self):
        import inspect

        async def _original(a: int, b: str = "x") -> str:
            """Original async docstring."""
            return b * a

        wrapped = with_correlation_id(_original)
        assert wrapped.__name__ == "_original"
        assert wrapped.__doc__ == "Original async docstring."
        assert inspect.signature(wrapped) == inspect.signature(_original)


class TestSectionToolIntegration:
    """A real registered tool, called end to end through FastMCP's own Tool
    object — not just the bare wrapper in isolation."""

    async def test_real_tool_call_stamps_correlation_id_on_log_lines(
        self, captured_backend_records
    ):
        from stealth_chrome_devtools_mcp.embedded import server

        tools = await server.mcp.get_tools()
        # Sync, static-documentation tool - no browser instance needed.
        tool = tools["get_hook_documentation"]

        assert correlation_id_var.get() == "-"
        await tool.run({})
        assert correlation_id_var.get() == "-"  # reset after the call

        ids = {
            record.correlation_id
            for record in captured_backend_records
            if getattr(record, "correlation_id", "-") != "-"
        }
        assert ids, (
            "expected at least one stealth.backend record stamped with a "
            "real (non-default) correlation id during the tool call"
        )


class TestToolsListSchemaSnapshot:
    """MANDATORY per plan risk #5: section_tool's wrapper must not change
    what a real MCP client sees in tools/list for any of the 96 tools."""

    async def test_representative_tool_schema_unchanged_per_section(self):
        golden = json.loads(_GOLDEN_SCHEMA_JSON)

        from stealth_chrome_devtools_mcp.embedded import server

        tools = await server.mcp.get_tools()

        for section, expected in sorted(golden.items()):
            name = expected["name"]
            assert name in tools, f"{section}: tool {name!r} no longer registered"
            mcp_tool = tools[name].to_mcp_tool(name=name)
            assert mcp_tool.name == expected["name"], section
            assert mcp_tool.inputSchema == expected["inputSchema"], (
                f"{section}: inputSchema for {name!r} changed - the "
                f"section_tool wrapper must preserve FastMCP's schema exactly"
            )
