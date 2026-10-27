"""T-9464: the dynamic-hook tool layer lets a bug of ours propagate.

``dynamic_hook_ai_interface`` had ten ``except Exception`` blocks that each answered
``{"success": False, "error": str(e)}``: a second error shape (DESIGN section 9) that
also turned a bug of ours inside the hook system into a "failure" the caller reads as
its own. Since T-9184 a refusal of the caller's input raises ``CallerError`` (dropped
from Sentry on purpose), so a bug has to surface as itself, never as a dict and never
as a ``ToolError``. The caller's refusals stay ``CallerError``; ``ToolError`` from
below (an unarmable hook) passes through untouched.
"""

from __future__ import annotations

import asyncio

import pytest

from fakes import call_tool
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import DynamicHookSystem
from stealth_chrome_devtools_mcp.embedded.tool_errors import CallerError, ToolError

BOUND = 2.0
GOOD = "def process_request(request):\n    return HookAction(action='continue')\n"
BUG = "bug-of-ours-9464"

# (public tool, its arguments, the object that backs it, the method that fails)
SYSTEM = "hook_system"
LEARNING = "learning_system"
CASES = [
    (
        "create_dynamic_hook",
        {"name": "h", "requirements": {}, "function_code": GOOD},
        SYSTEM,
        "create_hook",
    ),
    (
        "create_simple_dynamic_hook",
        {"name": "h", "url_pattern": "*", "action": "block"},
        SYSTEM,
        "create_hook",
    ),
    ("list_dynamic_hooks", {}, SYSTEM, "list_hooks"),
    ("get_dynamic_hook_details", {"hook_id": "x"}, SYSTEM, "get_hook_details"),
    ("remove_dynamic_hook", {"hook_id": "x"}, SYSTEM, "remove_hook"),
    ("get_hook_documentation", {}, LEARNING, "get_request_object_documentation"),
    ("get_hook_examples", {}, LEARNING, "get_hook_examples"),
    (
        "get_hook_requirements_documentation",
        {},
        LEARNING,
        "get_requirements_documentation",
    ),
    ("get_hook_common_patterns", {}, LEARNING, "get_common_patterns"),
    (
        "validate_hook_function",
        {"function_code": GOOD},
        LEARNING,
        "validate_hook_function",
    ),
]
IDS = [case[0] for case in CASES]


@pytest.fixture
def system(monkeypatch):
    system = DynamicHookSystem()
    monkeypatch.setattr(rt.dynamic_hook_ai, "hook_system", system)
    return system


async def _call(server, tool, /, **kwargs):
    return await asyncio.wait_for(call_tool(server, tool, **kwargs), BOUND)


def _break(monkeypatch, owner, method, error):
    target = getattr(rt.dynamic_hook_ai, owner)
    if method in ("create_hook", "remove_hook"):

        async def fail(*_args, **_kwargs):
            raise error

    else:

        def fail(*_args, **_kwargs):
            raise error

    monkeypatch.setattr(target, method, fail)


class TestABugOfOursPropagates:
    @pytest.mark.parametrize(("tool", "kwargs", "owner", "method"), CASES, ids=IDS)
    async def test_it_surfaces_as_itself_not_as_a_dict(
        self, patched_server, system, monkeypatch, tool, kwargs, owner, method
    ):
        _break(monkeypatch, owner, method, RuntimeError(BUG))

        with pytest.raises(RuntimeError, match=BUG) as caught:
            await _call(patched_server(), tool, **kwargs)

        assert not isinstance(caught.value, ToolError)

    @pytest.mark.parametrize(("tool", "kwargs", "owner", "method"), CASES, ids=IDS)
    async def test_it_is_never_dressed_up_as_the_callers_refusal(
        self, patched_server, system, monkeypatch, tool, kwargs, owner, method
    ):
        _break(monkeypatch, owner, method, KeyError(BUG))

        with pytest.raises(KeyError):
            await _call(patched_server(), tool, **kwargs)

    async def test_a_real_client_reads_it_as_an_error_result(
        self, patched_server, system, monkeypatch
    ):
        from fastmcp import Client

        _break(monkeypatch, SYSTEM, "list_hooks", RuntimeError(BUG))
        server = patched_server()
        async with Client(server.mcp) as client:
            result = await asyncio.wait_for(
                client.call_tool("list_dynamic_hooks", {}, raise_on_error=False),
                BOUND,
            )

        assert result.is_error is True


class TestARefusalBelowPassesThroughUntouched:
    @pytest.mark.parametrize("error_type", [ToolError, CallerError])
    @pytest.mark.parametrize(
        ("tool", "kwargs"),
        [
            ("create_dynamic_hook", CASES[0][1]),
            ("create_simple_dynamic_hook", CASES[1][1]),
        ],
    )
    async def test_the_same_exception_object_reaches_the_caller(
        self, patched_server, system, monkeypatch, tool, kwargs, error_type
    ):
        refusal = error_type("refused by create_hook")
        _break(monkeypatch, SYSTEM, "create_hook", refusal)

        with pytest.raises(error_type) as caught:
            await _call(patched_server(), tool, **kwargs)

        assert caught.value is refusal


class TestTheSuccessPathIsUnchanged:
    async def test_the_documentation_tools_answer_success_with_their_payload(
        self, patched_server, system
    ):
        server = patched_server()

        docs = await _call(server, "get_hook_documentation")
        examples = await _call(server, "get_hook_examples")
        reqs = await _call(server, "get_hook_requirements_documentation")
        patterns = await _call(server, "get_hook_common_patterns")

        assert docs["success"] is True and docs["documentation"]
        assert examples["success"] is True and examples["examples"]
        assert reqs["success"] is True and reqs["documentation"]
        assert patterns["success"] is True and patterns["patterns"]

    async def test_validate_hook_function_answers_its_verdict_as_data(
        self, patched_server, system
    ):
        server = patched_server()

        good = await _call(server, "validate_hook_function", function_code=GOOD)
        bad = await _call(
            server, "validate_hook_function", function_code="def process_request(r:"
        )

        assert good["success"] is True and good["validation"]["valid"] is True
        assert bad["success"] is True and bad["validation"]["valid"] is False

    async def test_create_list_details_remove_still_round_trip(
        self, patched_server, system
    ):
        server = patched_server()

        made = await _call(
            server,
            "create_dynamic_hook",
            name="h",
            requirements={},
            function_code=GOOD,
        )
        listed = await _call(server, "list_dynamic_hooks")
        details = await _call(
            server, "get_dynamic_hook_details", hook_id=made["hook_id"]
        )
        removed = await _call(server, "remove_dynamic_hook", hook_id=made["hook_id"])

        assert made["success"] is True and listed["count"] == 1
        assert details["hook"]["hook_id"] == made["hook_id"]
        assert removed["success"] is True and system.hooks == {}

    async def test_list_filters_by_instance_without_raising(
        self, patched_server, system
    ):
        server = patched_server()
        system.add_instance("inst-a")
        await _call(
            server,
            "create_dynamic_hook",
            name="h",
            requirements={},
            function_code=GOOD,
            instance_ids=["inst-a"],
        )

        mine = await _call(server, "list_dynamic_hooks", instance_id="inst-a")
        other = await _call(server, "list_dynamic_hooks", instance_id="inst-b")

        assert mine["count"] == 1
        assert other == {"success": True, "hooks": [], "count": 0}
