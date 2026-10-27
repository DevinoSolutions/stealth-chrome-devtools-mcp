"""T-9184: ``create_dynamic_hook`` RAISES when the validator rejects the code.

It used to answer ``{"success": False, "error": "Invalid function code", "issues":
[...], "warnings": [...]}`` -- a second error convention (DESIGN section 9). The tool
now raises ``ToolError`` whose text carries the issues (and any warnings), and
registers nothing. ``create_simple_dynamic_hook`` builds its code from the caller's
``target_url``, so a bad one reaches the same validator and must raise too, not be
flattened into a dict by its own catch-all.
"""

from __future__ import annotations

import asyncio

import pytest

from fakes import call_tool
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import DynamicHookSystem
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

BOUND = 2.0
UNCLOSED = "def process_request(request):\n  return ("
NO_ENTRY_POINT = "def other(request):\n    return None\n"


@pytest.fixture
def system(monkeypatch):
    system = DynamicHookSystem()
    monkeypatch.setattr(rt.dynamic_hook_ai, "hook_system", system)
    return system


async def _create(server, code):
    return await asyncio.wait_for(
        call_tool(
            server,
            "create_dynamic_hook",
            name="h",
            requirements={},
            function_code=code,
        ),
        BOUND,
    )


class TestTheValidatorRaises:
    async def test_a_syntax_error_raises_with_the_issue_text(
        self, patched_server, system
    ):
        with pytest.raises(ToolError, match=r"Syntax error.*never closed"):
            await _create(patched_server(), UNCLOSED)

    async def test_a_missing_entry_point_raises_with_the_issue_text(
        self, patched_server, system
    ):
        with pytest.raises(ToolError, match="process_request"):
            await _create(patched_server(), NO_ENTRY_POINT)

    async def test_the_warnings_travel_with_the_issues(
        self, patched_server, system, monkeypatch
    ):
        monkeypatch.setattr(
            rt.dynamic_hook_ai.learning_system,
            "validate_hook_function",
            lambda _code: {
                "valid": False,
                "issues": ["issue-one"],
                "warnings": ["warning-one"],
            },
        )

        with pytest.raises(ToolError) as caught:
            await _create(patched_server(), "ignored")

        assert "issue-one" in str(caught.value)
        assert "warning-one" in str(caught.value)

    async def test_nothing_is_registered_afterwards(self, patched_server, system):
        server = patched_server()
        with pytest.raises(ToolError):
            await _create(server, UNCLOSED)

        listed = await asyncio.wait_for(call_tool(server, "list_dynamic_hooks"), BOUND)

        assert system.hooks == {}
        assert listed["count"] == 0 and listed["hooks"] == []


class TestTheSimpleToolRaisesToo:
    async def test_a_target_url_that_breaks_the_template_raises(
        self, patched_server, system
    ):
        with pytest.raises(ToolError, match="Syntax error"):
            await asyncio.wait_for(
                call_tool(
                    patched_server(),
                    "create_simple_dynamic_hook",
                    name="h",
                    url_pattern="*",
                    action="redirect",
                    target_url='https://x/"',
                ),
                BOUND,
            )

        assert system.hooks == {}
