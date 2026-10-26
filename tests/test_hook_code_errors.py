"""T-9135 (EA): an error in the CALLER's hook code is theirs, not our defect.

``hasattr`` is missing from the hook builtins, so a hook that uses it raises
``NameError`` on every paused request. That was logged as our ERROR (and reached
Sentry) while the caller never learned of it. Now it is a WARNING, the request is
still continued, and the hook's own record (``error_count`` / ``last_error`` /
``last_error_at``) carries it to the caller through ``get_dynamic_hook_details`` and
``list_dynamic_hooks``. Our own failures stay ERROR.

The paused request is Chrome's own ``Fetch.requestPaused`` from nodriver's
``from_json``, through the real ``_on_request_paused``. Every wait is bounded by
``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio
import logging

import nodriver as uc
import pytest

from fakes import FakeTab, call_tool
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import DynamicHookSystem
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

INSTANCE_ID = "i1"
BOUND = 2.0


def _paused(**response):
    return uc.cdp.fetch.RequestPaused.from_json(
        {
            "requestId": "r1",
            "request": {
                "url": "https://example.invalid/data",
                "method": "GET",
                "headers": {},
                "initialPriority": "High",
                "referrerPolicy": "strict-origin-when-cross-origin",
            },
            "frameId": "F1",
            "resourceType": "Fetch",
            **response,
        }
    )


REQUEST_STAGE = _paused()
RESPONSE_STAGE = _paused(
    responseStatusCode=200,
    responseHeaders=[{"name": "content-type", "value": "text/plain"}],
)


def _code(body: str) -> str:
    return f"def process_request(request):\n    {body}\n"


NAME_ERROR = _code(
    "return HookAction(action='continue') if hasattr(request, 'x') else None"
)
ZERO_DIVISION = _code("return 1 / 0")
RETURNS_NOTHING = _code("pass")
BAD_DICT = _code("return {'no_such_field': 1}")


@pytest.fixture
def backend_log(caplog):
    caplog.set_level(logging.DEBUG, logger="stealth.backend")
    return caplog


def _levels(caplog, level):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "stealth.backend" and r.levelno == level
    ]


async def _run(code, *, requirements=None, event=REQUEST_STAGE, system=None):
    system = system or DynamicHookSystem()
    system.add_instance(INSTANCE_ID)
    hook_id = await system.create_hook(
        "caller-hook", requirements or {}, code, [INSTANCE_ID]
    )
    tab = FakeTab(
        target_id="T1", cdp_responses={"get_response_body": lambda _n: ("", False)}
    )
    await asyncio.wait_for(system._on_request_paused(tab, event, INSTANCE_ID), BOUND)
    return system, system.hooks[hook_id], tab


class TestACallersErrorIsAWarningTheyCanRead:
    @pytest.mark.parametrize(
        ("code", "kind"),
        [
            (NAME_ERROR, "NameError"),
            (ZERO_DIVISION, "ZeroDivisionError"),
            (RETURNS_NOTHING, "TypeError"),
            (BAD_DICT, "TypeError"),
        ],
    )
    async def test_it_is_a_warning_and_the_request_is_continued(
        self, backend_log, code, kind
    ):
        _, hook, tab = await _run(code)

        assert _levels(backend_log, logging.ERROR) == []
        warnings = _levels(backend_log, logging.WARNING)
        assert any("caller-hook" in m and kind in m for m in warnings)
        assert tab.send_calls == ["continue_request"]
        assert hook.error_count == 1
        assert kind in hook.last_error

    async def test_a_response_stage_request_is_continued_as_a_response(
        self, backend_log
    ):
        _, hook, tab = await _run(
            ZERO_DIVISION, requirements={"stage": "response"}, event=RESPONSE_STAGE
        )

        assert _levels(backend_log, logging.ERROR) == []
        assert tab.send_calls[-1] == "continue_response"
        assert hook.error_count == 1

    async def test_a_failing_custom_condition_is_recorded_too(self, backend_log):
        _, hook, tab = await _run(
            _code("return HookAction(action='block')"),
            requirements={"custom_condition": "hasattr(request, 'url')"},
        )

        assert _levels(backend_log, logging.ERROR) == []
        assert any("hasattr" in m for m in _levels(backend_log, logging.WARNING))
        assert tab.send_calls == ["continue_request"]  # no hook matched
        assert hook.error_count == 1 and "NameError" in hook.last_error

    async def test_the_count_and_the_latest_error_accumulate(self, backend_log):
        system, hook, _ = await _run(ZERO_DIVISION)
        tab = FakeTab(target_id="T1")
        await asyncio.wait_for(
            system._on_request_paused(tab, REQUEST_STAGE, INSTANCE_ID), BOUND
        )

        assert hook.error_count == 2
        assert hook.last_error_at is not None
        assert hook.trigger_count == 0  # the function never produced an action

    async def test_a_hook_that_works_has_no_error_record(self, backend_log):
        _, hook, tab = await _run(_code("return HookAction(action='continue')"))

        assert hook.error_count == 0
        assert hook.last_error is None and hook.last_error_at is None
        assert hook.trigger_count == 1


class TestOurOwnFailuresStayErrors:
    async def test_a_failure_executing_a_good_hook_action_is_an_error(
        self, backend_log, monkeypatch
    ):
        system = DynamicHookSystem()

        async def broken(*_a, **_k):
            raise RuntimeError("our bug")

        monkeypatch.setattr(system, "_execute_hook_action", broken)
        _, hook, _ = await _run(
            _code("return HookAction(action='continue')"), system=system
        )

        assert any("our bug" in m for m in _levels(backend_log, logging.ERROR))
        assert hook.error_count == 0  # not the caller's code


class TestTheCallerReadsItBack:
    async def test_the_details_and_list_tools_carry_the_error(
        self, backend_log, patched_server, monkeypatch
    ):
        system, hook, _ = await _run(NAME_ERROR)
        monkeypatch.setattr(rt.dynamic_hook_ai, "hook_system", system)
        server = patched_server()

        details = await asyncio.wait_for(
            call_tool(server, "get_dynamic_hook_details", hook_id=hook.hook_id), BOUND
        )
        listed = await asyncio.wait_for(call_tool(server, "list_dynamic_hooks"), BOUND)

        record = details["hook"]
        assert record["error_count"] == 1
        assert "hasattr" in record["last_error"]
        assert record["last_error_at"]
        assert listed["hooks"][0]["error_count"] == 1
        assert "hasattr" in listed["hooks"][0]["last_error"]


class TestTheWarningTextIsCapped:
    async def test_the_function_error_is_capped_like_last_error(self, backend_log):
        _, hook, _ = await _run(_code("return {}['x' * 5000]"))

        (message,) = [
            m for m in _levels(backend_log, logging.WARNING) if "caller-hook" in m
        ]
        assert len(message) < 700 and "x" * 600 not in message
        assert len(hook.last_error) == 500

    async def test_the_custom_condition_error_is_capped_too(self, backend_log):
        long_key = "k" * 5000
        _, hook, _ = await _run(
            _code("return HookAction(action='block')"),
            requirements={"custom_condition": "{}['" + long_key + "']"},
        )

        (message,) = [
            m for m in _levels(backend_log, logging.WARNING) if "caller-hook" in m
        ]
        assert len(message) < 700 and "k" * 600 not in message
        assert len(hook.last_error) == 500


class TestTheToolRefusesCodeThatDoesNotLoad:
    async def test_create_dynamic_hook_raises_and_keeps_nothing(
        self, backend_log, patched_server, monkeypatch
    ):
        system = DynamicHookSystem()
        monkeypatch.setattr(rt.dynamic_hook_ai, "hook_system", system)
        server = patched_server()

        with pytest.raises(ToolError, match="NameError"):
            await asyncio.wait_for(
                call_tool(
                    server,
                    "create_dynamic_hook",
                    name="h",
                    requirements={},
                    # valid for the validator, but the module body raises on load
                    function_code="y = hasattr\n" + _code("pass"),
                ),
                BOUND,
            )

        assert system.hooks == {}
        assert _levels(backend_log, logging.ERROR) == []
