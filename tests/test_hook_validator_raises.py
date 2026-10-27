"""T-9184: the dynamic-hook tools RAISE ``CallerError`` for the caller's own input.

``create_dynamic_hook`` used to answer ``{"success": False, "error": "Invalid
function code", "issues": [...], "warnings": [...]}`` for code the validator
refused, and ``get_dynamic_hook_details`` / ``remove_dynamic_hook`` /
``create_simple_dynamic_hook`` answered a ``success: False`` dict for an unknown
hook id, a missing ``target_url`` or ``custom_headers``, or an unknown action --
a second error convention (DESIGN section 9). Each now raises ``CallerError`` (the
caller's input; a ``ToolError``, so the client reads the same text as ``isError``)
and changes nothing.
"""

from __future__ import annotations

import asyncio

import pytest

from fakes import call_tool
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import DynamicHookSystem
from stealth_chrome_devtools_mcp.embedded.tool_errors import CallerError, ToolError

BOUND = 2.0
UNCLOSED = "def process_request(request):\n  return ("
NO_ENTRY_POINT = "def other(request):\n    return None\n"
GOOD = "def process_request(request):\n    return HookAction(action='continue')\n"


@pytest.fixture
def system(monkeypatch):
    system = DynamicHookSystem()
    monkeypatch.setattr(rt.dynamic_hook_ai, "hook_system", system)
    return system


async def _call(server, tool, /, **kwargs):
    return await asyncio.wait_for(call_tool(server, tool, **kwargs), BOUND)


async def _create(server, code):
    return await _call(
        server, "create_dynamic_hook", name="h", requirements={}, function_code=code
    )


class TestTheValidatorRaises:
    async def test_a_syntax_error_raises_with_the_issue_text(
        self, patched_server, system
    ):
        with pytest.raises(CallerError, match=r"Syntax error.*never closed"):
            await _create(patched_server(), UNCLOSED)

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param("x = '" + chr(0xD800) + "'", id="lone-surrogate"),
            pytest.param("-" * 100_000 + "1", id="unary-minus-chain"),
            pytest.param("+".join(["1"] * 100_000), id="long-binop-chain"),
        ],
    )
    async def test_text_the_parser_cannot_take_raises_a_caller_error(
        self, patched_server, system, code
    ):
        # T-9431: the validator reports it as an issue instead of crashing the tool.
        with pytest.raises(CallerError, match="Syntax error"):
            await _create(patched_server(), code)

        assert system.hooks == {}

    async def test_a_missing_entry_point_raises_with_the_issue_text(
        self, patched_server, system
    ):
        with pytest.raises(CallerError, match="process_request"):
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

        with pytest.raises(CallerError) as caught:
            await _create(patched_server(), "ignored")

        assert "issue-one" in str(caught.value)
        assert "warning-one" in str(caught.value)

    async def test_nothing_is_registered_afterwards(self, patched_server, system):
        server = patched_server()
        with pytest.raises(CallerError):
            await _create(server, UNCLOSED)

        listed = await _call(server, "list_dynamic_hooks")

        assert system.hooks == {}
        assert listed["count"] == 0 and listed["hooks"] == []


class TestTheSimpleToolRaises:
    async def test_a_target_url_that_breaks_the_template_raises(
        self, patched_server, system
    ):
        with pytest.raises(CallerError, match="Syntax error"):
            await _call(
                patched_server(),
                "create_simple_dynamic_hook",
                name="h",
                url_pattern="*",
                action="redirect",
                target_url='https://x/"',
            )

        assert system.hooks == {}

    @pytest.mark.parametrize(
        ("kwargs", "text"),
        [
            ({"action": "redirect"}, "target_url required for redirect action"),
            ({"action": "add_headers"}, "custom_headers required for add_headers"),
            ({"action": "explode"}, "Unknown action: explode"),
        ],
    )
    async def test_input_it_cannot_use_raises_and_registers_nothing(
        self, patched_server, system, kwargs, text
    ):
        with pytest.raises(CallerError, match=text):
            await _call(
                patched_server(),
                "create_simple_dynamic_hook",
                name="h",
                url_pattern="*",
                **kwargs,
            )

        assert system.hooks == {}

    async def test_good_input_still_creates_the_hook(self, patched_server, system):
        result = await _call(
            patched_server(),
            "create_simple_dynamic_hook",
            name="h",
            url_pattern="*",
            action="block",
        )

        assert result["success"] is True
        assert list(system.hooks) == [result["hook_id"]]


class TestAnUnknownHookIdRaises:
    async def test_details_of_an_unknown_hook_raise(self, patched_server, system):
        with pytest.raises(CallerError, match="Hook nope not found"):
            await _call(patched_server(), "get_dynamic_hook_details", hook_id="nope")

    async def test_removing_an_unknown_hook_raises_and_keeps_the_others(
        self, patched_server, system
    ):
        server = patched_server()
        made = await _create(server, GOOD)

        with pytest.raises(CallerError, match="Hook nope not found"):
            await _call(server, "remove_dynamic_hook", hook_id="nope")

        assert list(system.hooks) == [made["hook_id"]]

    async def test_a_known_hook_still_reads_and_removes(self, patched_server, system):
        server = patched_server()
        made = await _create(server, GOOD)

        details = await _call(
            server, "get_dynamic_hook_details", hook_id=made["hook_id"]
        )
        removed = await _call(server, "remove_dynamic_hook", hook_id=made["hook_id"])

        assert details["success"] is True and removed["success"] is True
        assert system.hooks == {}

    def test_the_client_reads_a_caller_error_as_a_tool_error(self):
        assert issubclass(CallerError, ToolError)


class TestTheClientReadsIsError:
    """The ``.fn`` seam raises; a real in-memory MCP client must see ``isError``."""

    async def test_both_refusals_arrive_as_error_results_with_their_text(
        self, patched_server, system
    ):
        from fastmcp import Client

        server = patched_server()
        async with Client(server.mcp) as client:
            invalid = await asyncio.wait_for(
                client.call_tool(
                    "create_dynamic_hook",
                    {"name": "h", "requirements": {}, "function_code": UNCLOSED},
                    raise_on_error=False,
                ),
                BOUND,
            )
            unknown = await asyncio.wait_for(
                client.call_tool(
                    "get_dynamic_hook_details",
                    {"hook_id": "nope"},
                    raise_on_error=False,
                ),
                BOUND,
            )

        assert invalid.is_error is True
        assert "Invalid function code" in invalid.content[0].text
        assert "never closed" in invalid.content[0].text
        assert unknown.is_error is True
        assert "Hook nope not found" in unknown.content[0].text
        assert system.hooks == {}
