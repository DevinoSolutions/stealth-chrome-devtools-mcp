"""Behavioral tests for the dynamic hook system (no browser).

Covers the parts that run entirely in-process: matching a request against a
hook's requirements, compiling + executing an AI-generated hook function
(including every failure fallback), and the hook/instance registry. The CDP
dispatch methods (_execute_hook_action, setup_interception) need a real tab and
are covered by the integration suite.

Safety focus: a malformed or malicious hook function must NEVER crash request
processing — every bad path must degrade to HookAction("continue").
"""

import pytest

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded import dynamic_hook_system as dhs
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import (
    DynamicHook,
    DynamicHookSystem,
    HookAction,
    RequestInfo,
)

CONTINUE = "def process_request(request):\n    return HookAction(action='continue')\n"


def _req(
    url="https://example.com/api/data",
    method="GET",
    *,
    stage="request",
    resource_type=None,
    headers=None,
):
    return RequestInfo(
        request_id="req-1",
        instance_id="inst-1",
        url=url,
        method=method,
        headers=headers or {},
        resource_type=resource_type,
        stage=stage,
    )


def _hook(requirements, code=CONTINUE, name="h", priority=100):
    return DynamicHook("hid", name, requirements, code, priority)


class TestDataclasses:
    def test_request_info_to_dict_roundtrips_fields(self):
        info = _req(headers={"A": "b"}, method="POST")
        d = info.to_dict()
        assert d["url"] == "https://example.com/api/data"
        assert d["method"] == "POST"
        assert d["headers"] == {"A": "b"}
        assert d["stage"] == "request"

    def test_hook_action_defaults(self):
        a = HookAction(action="block")
        assert a.url is None and a.headers is None and a.status_code is None


class TestMatches:
    def test_no_requirements_matches_everything(self):
        assert _hook({}).matches(_req()) is True

    def test_url_pattern_hit_and_miss(self):
        assert _hook({"url_pattern": "*example.com*"}).matches(_req()) is True
        assert _hook({"url_pattern": "*other.com*"}).matches(_req()) is False

    def test_method_is_case_insensitive(self):
        assert _hook({"method": "get"}).matches(_req(method="GET")) is True
        assert _hook({"method": "POST"}).matches(_req(method="GET")) is False

    def test_resource_type_filter(self):
        assert (
            _hook({"resource_type": "XHR"}).matches(_req(resource_type="XHR")) is True
        )
        assert (
            _hook({"resource_type": "XHR"}).matches(_req(resource_type="Document"))
            is False
        )

    def test_stage_filter(self):
        assert _hook({"stage": "response"}).matches(_req(stage="response")) is True
        assert _hook({"stage": "response"}).matches(_req(stage="request")) is False

    def test_custom_condition_true_and_false(self):
        assert (
            _hook({"custom_condition": "len(request.url) > 5"}).matches(_req()) is True
        )
        assert (
            _hook({"custom_condition": "request.method == 'POST'"}).matches(_req())
            is False
        )

    def test_custom_condition_error_fails_closed(self):
        # A broken condition must not match (and must not raise).
        assert _hook({"custom_condition": "request.nope.nope"}).matches(_req()) is False

    def test_multiple_requirements_all_must_hold(self):
        h = _hook({"url_pattern": "*example.com*", "method": "GET"})
        assert h.matches(_req(method="GET")) is True
        assert h.matches(_req(method="POST")) is False


class TestProcess:
    def test_hookaction_return_is_passed_through(self):
        h = _hook(
            {},
            code="def process_request(request):\n    return HookAction(action='block')\n",
        )
        result = h.process(_req())
        assert result.action == "block"
        assert h.trigger_count == 1
        assert h.last_triggered is not None

    def test_dict_return_is_coerced(self):
        h = _hook(
            {},
            code="def process_request(request):\n    return {'action': 'redirect', 'url': 'https://x'}\n",
        )
        result = h.process(_req())
        assert result.action == "redirect" and result.url == "https://x"

    def test_invalid_return_type_becomes_continue(self):
        h = _hook({}, code="def process_request(request):\n    return 42\n")
        assert h.process(_req()).action == "continue"

    def test_function_exception_becomes_continue(self):
        h = _hook(
            {}, code="def process_request(request):\n    raise RuntimeError('boom')\n"
        )
        assert h.process(_req()).action == "continue"

    def test_uncompilable_code_falls_back_to_continue(self):
        h = _hook({}, code="this is not valid python !!!")
        assert h.process(_req()).action == "continue"

    def test_missing_entrypoint_falls_back_to_continue(self):
        h = _hook({}, code="x = 1\n")  # valid python, no process_request
        assert h.process(_req()).action == "continue"

    def test_function_receives_dict_not_object(self):
        # process() passes request.to_dict(); a hook using dict access must work.
        h = _hook(
            {},
            code=(
                "def process_request(request):\n"
                "    return HookAction(action='block' if request['method'] == 'GET' else 'continue')\n"
            ),
        )
        assert h.process(_req(method="GET")).action == "block"


class TestRegistry:
    async def test_create_hook_scoped_to_instances(self):
        sys = DynamicHookSystem()
        hid = await sys.create_hook(
            "h", {"url_pattern": "*"}, CONTINUE, instance_ids=["inst-1"]
        )
        assert hid in sys.hooks
        assert sys.instance_hooks["inst-1"] == [hid]

    async def test_create_hook_without_instances_applies_to_existing(self):
        sys = DynamicHookSystem()
        sys.add_instance("inst-1")
        hid = await sys.create_hook("h", {"url_pattern": "*"}, CONTINUE)
        assert hid in sys.instance_hooks["inst-1"]

    async def test_remove_hook_drops_hook_and_associations(self):
        sys = DynamicHookSystem()
        hid = await sys.create_hook(
            "h", {"url_pattern": "*"}, CONTINUE, instance_ids=["inst-1"]
        )
        assert await sys.remove_hook(hid) is True
        assert hid not in sys.hooks
        assert hid not in sys.instance_hooks["inst-1"]
        assert await sys.remove_hook(hid) is False  # already gone

    async def test_list_and_details(self):
        sys = DynamicHookSystem()
        hid = await sys.create_hook(
            "named", {"url_pattern": "*"}, CONTINUE, instance_ids=["inst-1"]
        )
        listing = sys.list_hooks()
        assert (
            listing and listing[0]["hook_id"] == hid and listing[0]["name"] == "named"
        )
        details = sys.get_hook_details(hid)
        assert details["function_code"] == CONTINUE
        assert sys.get_hook_details("nope") is None

    def test_add_and_remove_instance(self):
        sys = DynamicHookSystem()
        sys.add_instance("inst-1")
        assert sys.instance_hooks["inst-1"] == []
        sys.remove_instance("inst-1")
        assert "inst-1" not in sys.instance_hooks


class TestProcessRequestHooks:
    """Dispatch is first-match-by-priority (F-163): when several hooks match one
    request, only the highest-priority match (lowest priority number) runs; the
    lower-priority matches are shadowed and never fire. A WARNING names the winner
    and the shadowed hooks so the silent 'trigger_count stuck at 0' trap is
    visible. Reuses FakeTab (M6 canon) + the _req() helper -- no browser.
    """

    async def test_first_match_by_priority_wins_only_one_runs(self):
        sys = DynamicHookSystem()
        hi = await sys.create_hook(
            name="win-hi",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=10,
        )
        lo = await sys.create_hook(
            name="shadow-lo",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=20,
        )
        await sys._process_request_hooks(FakeTab(), _req(stage="request"))
        assert sys.hooks[hi].trigger_count == 1
        assert sys.hooks[lo].trigger_count == 0

    async def test_shadowed_match_emits_warning(self, monkeypatch):
        sys = DynamicHookSystem()
        await sys.create_hook(
            name="win-hi",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=10,
        )
        await sys.create_hook(
            name="shadow-lo",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=20,
        )
        messages = []
        monkeypatch.setattr(
            dhs.debug_logger,
            "log_warning",
            lambda component, method, message: messages.append(message),
        )
        await sys._process_request_hooks(FakeTab(), _req(stage="request"))
        shadow = [m for m in messages if "shadowed" in m]
        assert len(shadow) == 1
        assert "shadow-lo" in shadow[0]
        assert "win-hi" in shadow[0]

    async def test_single_match_emits_no_shadow_warning(self, monkeypatch):
        sys = DynamicHookSystem()
        only = await sys.create_hook(
            name="solo",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=10,
        )
        messages = []
        monkeypatch.setattr(
            dhs.debug_logger,
            "log_warning",
            lambda component, method, message: messages.append(message),
        )
        await sys._process_request_hooks(FakeTab(), _req(stage="request"))
        assert [m for m in messages if "shadowed" in m] == []
        assert sys.hooks[only].trigger_count == 1


class TestResponseStageHooksRemoved:
    """F-721/F-742: the dead ResponseStageProcessor duplicate is deleted, so the
    HookAction -> CDP dispatch lives in exactly one place (dynamic_hook_system)."""

    def test_response_stage_hooks_module_is_deleted(self):
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("response_stage_hooks")

        # The canonical home for the dispatch types survives the deletion.
        from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import (
            HookAction,
            RequestInfo,
        )

        assert HookAction is not None and RequestInfo is not None
