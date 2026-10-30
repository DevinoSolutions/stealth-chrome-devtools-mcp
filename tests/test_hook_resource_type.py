"""T-9478: a bad ``resource_type`` in a dynamic hook is the caller's input, refused up front.

``setup_interception`` looks the name up with ``getattr(ResourceType, name.upper())``,
which raised ``AttributeError`` for an unknown one and was swallowed there: with a tab
armed the caller read the misleading "could not be armed" ``ToolError``; with none armed
the hook was stored and silently never matched. ``create_hook`` now refuses it with a
``CallerError`` that lists the valid names (read off the CDP enum, not typed) BEFORE the
hook is stored. And a bug in ``create_hook`` is reported once (FastMCP's), not twice.
"""

from __future__ import annotations

import asyncio
import logging

import nodriver as uc
import pytest

from fakes import call_tool
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import DynamicHookSystem
from stealth_chrome_devtools_mcp.embedded.tool_errors import CallerError

BOUND = 2.0
GOOD = "def process_request(request):\n    return HookAction(action='continue')\n"
VALID_NAMES = [member.value for member in uc.cdp.network.ResourceType]


@pytest.fixture
def system(monkeypatch):
    system = DynamicHookSystem()
    monkeypatch.setattr(rt.dynamic_hook_ai, "hook_system", system)
    return system


async def _create(server, resource_type, **extra):
    return await asyncio.wait_for(
        call_tool(
            server,
            "create_dynamic_hook",
            name="h",
            requirements={"url_pattern": "*", "resource_type": resource_type},
            function_code=GOOD,
            **extra,
        ),
        BOUND,
    )


class _Tab:
    target = type("T", (), {"target_id": "tab-1"})()

    async def send(self, *_a, **_k):
        return None

    def add_handler(self, *_a, **_k):
        return None


@pytest.mark.parametrize("bad", ["scripts", "", "img", 5, ["script"]])
async def test_an_unknown_resource_type_raises_and_stores_nothing(
    patched_server, system, bad
):
    with pytest.raises(CallerError, match="resource_type") as caught:
        await _create(patched_server(), bad)

    assert system.hooks == {}
    assert all(name in str(caught.value) for name in VALID_NAMES)


async def test_with_a_tab_armed_it_is_still_the_callers_error(patched_server, system):
    system.instance_hooks["inst"] = []
    system._armed_tabs["inst"] = {"tab-1": _Tab()}

    with pytest.raises(CallerError, match="resource_type") as caught:
        await _create(patched_server(), "scripts", instance_ids=["inst"])

    assert "could not be armed" not in str(caught.value)
    assert system.hooks == {}
    assert system.instance_hooks["inst"] == []


@pytest.mark.parametrize("good", ["script", "SCRIPT", "Xhr", "web_socket"])
async def test_a_valid_resource_type_is_still_stored(patched_server, system, good):
    created = await _create(patched_server(), good)

    assert created["hook_id"] in system.hooks


async def test_no_resource_type_is_still_stored(patched_server, system):
    created = await asyncio.wait_for(
        call_tool(
            patched_server(),
            "create_dynamic_hook",
            name="h",
            requirements={"url_pattern": "*"},
            function_code=GOOD,
        ),
        BOUND,
    )

    assert created["hook_id"] in system.hooks


async def test_a_bug_in_create_hook_is_logged_once_not_twice(system, monkeypatch):
    """Our own log_error and FastMCP's logger.exception both reported the same bug."""
    import stealth_chrome_devtools_mcp.embedded.dynamic_hook_system as module

    def boom(*_a, **_k):
        raise RuntimeError("bug of ours")

    monkeypatch.setattr(module, "DynamicHook", boom)
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("stealth.backend")
    handler = _Collect(level=logging.ERROR)
    logger.addHandler(handler)
    try:
        with pytest.raises(RuntimeError, match="bug of ours"):
            await system.create_hook("h", {}, GOOD)
    finally:
        logger.removeHandler(handler)

    assert [r for r in records if r.levelno >= logging.ERROR] == []


async def test_a_paused_request_carries_the_cdp_value_and_the_requirement_matches_it(
    system,
):
    """The matcher compared ``str(ResourceType.FETCH)`` ('ResourceType.FETCH') with the
    requirement, so no resource_type requirement ever matched a real request."""
    from stealth_chrome_devtools_mcp.embedded.dynamic_hook_model import (
        DynamicHook,
        RequestInfo,
    )

    request = RequestInfo(
        request_id="r",
        instance_id="i",
        url="http://x/api",
        method="GET",
        headers={},
        post_data=None,
        resource_type=uc.cdp.network.ResourceType.FETCH.value,
        stage="request",
    )

    for written in ("fetch", "Fetch", "FETCH"):
        hook = DynamicHook("h", "n", {"resource_type": written}, GOOD, 100)
        assert hook.matches(request) is True
    other = DynamicHook("h", "n", {"resource_type": "script"}, GOOD, 100)
    assert other.matches(request) is False


async def test_a_real_paused_event_reaches_a_hook_that_names_its_type():
    """Through ``_on_request_paused``, as Chrome delivers it: the event's
    ``resource_type`` is the enum, and a hook naming ``xhr`` must run for it."""
    from types import SimpleNamespace

    from fakes import FakeTab

    system = DynamicHookSystem()
    system.add_instance("inst")
    block = "def process_request(request):\n    return HookAction(action='block')\n"
    await system.create_hook(
        "h", {"resource_type": "xhr"}, block, instance_ids=["inst"]
    )
    tab = FakeTab()
    event = SimpleNamespace(
        request_id=uc.cdp.fetch.RequestId("req-1"),
        response_status_code=None,
        response_error_reason=None,
        resource_type=uc.cdp.network.ResourceType.XHR,
        request=SimpleNamespace(url="http://x/api", method="GET", headers={}),
    )

    await system._on_request_paused(tab, event, "inst")

    assert "continue_request" not in tab.send_calls
    assert tab.send_calls, "the hook's block action was never sent"


def _request(resource_type):
    from stealth_chrome_devtools_mcp.embedded.dynamic_hook_model import RequestInfo

    return RequestInfo(
        request_id="r",
        instance_id="i",
        url="http://x/api",
        method="GET",
        headers={},
        post_data=None,
        resource_type=resource_type,
        stage="request",
    )


TWO_WORD = [m for m in uc.cdp.network.ResourceType if m.name.lower() != m.value.lower()]


def test_the_two_word_types_differ_in_name_and_value():
    assert len(TWO_WORD) == 6


@pytest.mark.parametrize("member", TWO_WORD, ids=lambda m: m.name)
@pytest.mark.parametrize("spelling", ["name", "value", "name-lower", "value-lower"])
async def test_a_two_word_type_validates_and_matches_in_either_spelling(
    system, member, spelling
):
    """``event_source`` / ``EventSource`` name one type; the CDP form is the value."""
    written = {
        "name": member.name,
        "value": member.value,
        "name-lower": member.name.lower(),
        "value-lower": member.value.lower(),
    }[spelling]
    hook_id = await system.create_hook(
        "h", {"resource_type": written}, GOOD, instance_ids=None
    )

    assert system.hooks[hook_id].matches(_request(member.value)) is True
    assert system.hooks[hook_id].matches(_request("Document")) is False


async def test_the_refusal_lists_the_cdp_values_not_the_member_names(
    patched_server, system
):
    with pytest.raises(CallerError) as caught:
        await _create(patched_server(), "nope")

    text = str(caught.value)
    assert (
        "WebSocket" in text and "EventSource" in text and "CSPViolationReport" in text
    )
    assert "WEB_SOCKET" not in text


def _documented_resource_types(node):
    if isinstance(node, dict):
        if "resource_type" in node and "examples" in node["resource_type"]:
            yield from node["resource_type"]["examples"]
        for value in node.values():
            yield from _documented_resource_types(value)
    elif isinstance(node, list):
        for value in node:
            yield from _documented_resource_types(value)


async def test_every_documented_resource_type_example_is_accepted(
    patched_server, system
):
    from stealth_chrome_devtools_mcp.embedded.hook_learning_system import (
        hook_learning_system,
    )

    examples = set(
        _documented_resource_types(
            hook_learning_system.get_requirements_documentation()
        )
    )
    assert {"Document", "WebSocket"} <= examples
    for example in examples:
        created = await _create(patched_server(), example)
        assert created["hook_id"] in system.hooks


async def test_an_explicit_none_is_no_filter_in_the_matcher_and_when_stored(system):
    from stealth_chrome_devtools_mcp.embedded.dynamic_hook_model import DynamicHook

    direct = DynamicHook("h", "n", {"resource_type": None}, GOOD, 100)
    assert direct.matches(_request("Fetch")) is True

    hook_id = await system.create_hook("h", {"resource_type": None}, GOOD)
    assert system.hooks[hook_id].matches(_request("XHR")) is True


async def test_a_paused_event_whose_type_is_a_plain_string_still_runs_the_hook():
    from types import SimpleNamespace

    from fakes import FakeTab

    system = DynamicHookSystem()
    system.add_instance("inst")
    block = "def process_request(request):\n    return HookAction(action='block')\n"
    await system.create_hook(
        "h", {"resource_type": "xhr"}, block, instance_ids=["inst"]
    )
    tab = FakeTab()
    event = SimpleNamespace(
        request_id=uc.cdp.fetch.RequestId("req-1"),
        response_status_code=None,
        response_error_reason=None,
        resource_type="XHR",
        request=SimpleNamespace(url="http://x/api", method="GET", headers={}),
    )

    await system._on_request_paused(tab, event, "inst")

    assert "continue_request" not in tab.send_calls and tab.send_calls
