"""plan_E2E STEP 4 — cdp-functions, dynamic-hooks, debugging + coverage manifest.

The three browser-driven walks (cdp-functions 13, dynamic-hooks 5 async, debug 5)
are ``@integration_test`` — integration-marked and skipped when Chrome is absent.
The sync hook-doc tools (5) and the coverage manifest are hermetic and run in the
unit job: the manifest is a tripwire that breaks if a 95th tool is added without
deciding its E2E story (same philosophy as the F-108 count pin).
"""

from __future__ import annotations

import json

import pytest

from e2e_helpers import (
    CAN_RUN,
    get_fn,
    navigate_and_settle,
    sandbox_kwargs,
    server_mod,
    warmup_once,
)


def integration_test(fn):
    """Mark a test integration + skip it when Chrome / the server is unavailable
    (a per-test guard, since this module also holds hermetic tests)."""
    fn = pytest.mark.integration(fn)
    if not CAN_RUN:
        fn = pytest.mark.skip("Chrome not available or server failed to load")(fn)
    return fn


def _find_hook_id(listing, name):
    """Recursively find the hook_id of the hook named ``name`` in a list_hooks
    result (robust to whether hooks sit under 'hooks'/'dynamic_hooks'/etc.)."""

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("name") == name and "hook_id" in obj:
                return obj["hook_id"]
            for value in obj.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(listing)


# ---------------------------------------------------------------------------
# cdp-functions: 13 tools driven against hooks.html
# ---------------------------------------------------------------------------


@integration_test
async def test_cdp_functions_walk(fixture_app_server):
    await warmup_once()
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/hooks.html")

        commands = await get_fn("list_cdp_commands")()
        assert isinstance(commands, list) and len(commands) > 0

        assert isinstance(await get_fn("get_execution_contexts")(instance_id=iid), list)

        # execute_cdp_command is Runtime-only: it resolves the command via
        # getattr(uc.cdp.runtime, command), so the domain-qualified
        # "Runtime.evaluate" never resolves — use the bare snake_case attr
        # "evaluate" (advertised-vs-executable mismatch pinned separately below).
        cdp = await get_fn("execute_cdp_command")(
            instance_id=iid,
            command="evaluate",
            params={"expression": "6 * 7", "return_by_value": True},
        )
        assert cdp["success"] is True
        assert "42" in json.dumps(cdp, default=str)

        assert isinstance(
            await get_fn("discover_global_functions")(instance_id=iid), list
        )
        assert isinstance(
            await get_fn("discover_object_methods")(
                instance_id=iid, object_path="window.appAPI"
            ),
            list,
        )

        # calcTotal(2, 3) == 5: the retrieved value is our fixture ground truth.
        call = await get_fn("call_javascript_function")(
            instance_id=iid, function_path="window.calcTotal", args=[2, 3]
        )
        assert isinstance(call, dict)
        assert "5" in json.dumps(call, default=str)

        assert isinstance(
            await get_fn("inspect_function_signature")(
                instance_id=iid, function_path="window.calcTotal"
            ),
            dict,
        )

        injected = await get_fn("inject_and_execute_script")(
            instance_id=iid, script_code="return 6 * 7;"
        )
        assert isinstance(injected, dict)

        assert isinstance(
            await get_fn("create_persistent_function")(
                instance_id=iid, function_name="fixtureNoop", function_code="return 1;"
            ),
            dict,
        )
        seq = await get_fn("execute_function_sequence")(
            instance_id=iid,
            function_calls=[{"function_path": "window.calcTotal", "args": [1, 2]}],
        )
        # Returns a LIST of per-call records (not a dict). calcTotal(1, 2) == 3.
        assert isinstance(seq, list) and len(seq) == 1
        assert seq[0]["result"]["result"] == 3
        assert isinstance(
            await get_fn("create_python_binding")(
                instance_id=iid,
                binding_name="fixtureBinding",
                python_code="def handler(x):\n    return x",
            ),
            dict,
        )

        # execute_python_in_browser may lack the optional py2js extra — assert
        # the graceful dict shape either way (never a crash).
        assert isinstance(
            await get_fn("execute_python_in_browser")(
                instance_id=iid, python_code="result = 1 + 1"
            ),
            dict,
        )
        assert isinstance(
            await get_fn("get_function_executor_info")(instance_id=iid), dict
        )
    finally:
        await close(instance_id=iid)


@integration_test
async def test_python_binding_is_callable_from_javascript(fixture_app_server):
    """RELEASE-FIX-A C6 (A4): the genuine JS→Python round-trip, end-to-end.

    create_python_binding wires Runtime.bindingCalled → the Python function;
    calling ``window[name](...)`` in the page must resolve to the value the
    Python function returned. Before the fix the binding was never wired (the
    wrapper called ``chrome.runtime.sendMessage`` and no handler existed), so the
    page promise never resolved. This asserts the JS-visible return value equals
    Python's — the real-coverage proof a characterization pin cannot give.
    """
    await warmup_once()
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/hooks.html")

        binding = await get_fn("create_python_binding")(
            instance_id=iid,
            binding_name="pyAdd",
            python_code="def add(a, b):\n    return a + b",
        )
        assert isinstance(binding, dict) and binding.get("success") is True

        # Call the binding from JS and await the promise it returns; the resolved
        # value must equal what Python returned (40 + 2 == 42).
        cdp = await get_fn("execute_cdp_command")(
            instance_id=iid,
            command="evaluate",
            params={
                "expression": "window.pyAdd(40, 2)",
                "await_promise": True,
                "return_by_value": True,
            },
        )
        assert cdp["success"] is True, cdp
        assert "42" in json.dumps(cdp, default=str), cdp
    finally:
        await close(instance_id=iid)


@integration_test
@pytest.mark.characterization
async def test_execute_cdp_command_rejects_domain_qualified_name(fixture_app_server):
    """PINS execute_cdp_command's advertised-vs-executable mismatch.

    Finding — execute_cdp_command is Runtime-only and rejects domain-qualified
    names: it resolves the command via ``getattr(uc.cdp.runtime, command)`` (see
    cdp_function_executor.py:169-171), so ``list_cdp_commands`` advertises
    camelCase names like 'evaluate'/'callFunctionOn' but a domain-qualified
    'Runtime.evaluate' (or any non-Runtime domain) can never resolve. The working
    path uses ``command="evaluate"`` (test_cdp_functions_walk). Route M4-Ph1/M14.
    """
    await warmup_once()
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/hooks.html")
        cdp = await get_fn("execute_cdp_command")(
            instance_id=iid,
            command="Runtime.evaluate",
            params={"expression": "6 * 7", "return_by_value": True},
        )
        assert cdp["success"] is False
        assert "Unknown CDP command" in json.dumps(cdp, default=str)
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# dynamic-hooks (5 async): create (simple + full) -> list -> details -> remove
# ---------------------------------------------------------------------------


@integration_test
async def test_dynamic_hooks_lifecycle(fixture_app_server):
    await warmup_once()
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    create_simple = get_fn("create_simple_dynamic_hook")
    create_full = get_fn("create_dynamic_hook")
    list_hooks = get_fn("list_dynamic_hooks")
    get_details = get_fn("get_dynamic_hook_details")
    remove_hook = get_fn("remove_dynamic_hook")
    close = get_fn("close_instance")

    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/hooks.html")

        created = await create_simple(
            name="fixture-hook",
            url_pattern="/api/json",
            action="block",
            instance_ids=[iid],
        )
        assert isinstance(created, dict)

        listing = await list_hooks()
        assert isinstance(listing, dict)
        hook_id = _find_hook_id(listing, "fixture-hook")
        assert hook_id, f"created hook not found in listing: {listing}"

        details = await get_details(hook_id=hook_id)
        assert isinstance(details, dict)
        assert "fixture-hook" in json.dumps(details, default=str)

        removed = await remove_hook(hook_id=hook_id)
        assert isinstance(removed, dict)
        assert _find_hook_id(await list_hooks(), "fixture-hook") is None

        # Full AI-function variant: assert the graceful dict shape, then clean up
        # any hook it created (its function contract is exercised more deeply in
        # the hermetic test_dynamic_hook_system.py).
        full = await create_full(
            name="fixture-full-hook",
            requirements={"url_pattern": "/api/json"},
            function_code="def hook(request):\n    return request",
            instance_ids=[iid],
        )
        assert isinstance(full, dict)
        full_id = _find_hook_id(await list_hooks(), "fixture-full-hook")
        if full_id:
            await remove_hook(hook_id=full_id)
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# debugging (5): all global / env-level, no browser needed
# ---------------------------------------------------------------------------


@integration_test
async def test_debugging_tools(tmp_path):
    assert isinstance(await get_fn("get_debug_view")(), dict)
    assert isinstance(await get_fn("get_debug_lock_status")(), dict)
    exported = await get_fn("export_debug_logs")(filename=str(tmp_path / "dbg.json"))
    assert isinstance(exported, str)
    assert await get_fn("clear_debug_view")() is True
    assert isinstance(await get_fn("validate_browser_environment_tool")(), dict)


# ---------------------------------------------------------------------------
# dynamic-hooks (5 sync doc/validate tools): hermetic, no browser
# ---------------------------------------------------------------------------


def test_hook_doc_tools():
    for name in (
        "get_hook_documentation",
        "get_hook_examples",
        "get_hook_requirements_documentation",
        "get_hook_common_patterns",
    ):
        payload = get_fn(name)()
        assert isinstance(payload, dict) and payload  # non-empty documentation

    validated = get_fn("validate_hook_function")(
        function_code="def hook(request):\n    return request"
    )
    assert isinstance(validated, dict)


# ---------------------------------------------------------------------------
# §2.4 coverage manifest — every SECTION_TOOLS name is E2E-covered or exempt
# ---------------------------------------------------------------------------

# Explicit reasoned literals (NOT derived from the registry) so that adding a
# 95th tool without deciding its E2E story breaks this test. E2E_COVERED lists
# every tool a test in this suite drives against the fixture app; E2E_EXEMPT
# names any tool intentionally left to another tier, each with a reason.
E2E_COVERED = {
    # browser-management (8) — test_e2e_interaction.py
    "spawn_browser",
    "list_instances",
    "close_instance",
    "get_instance_state",
    "navigate",
    "go_back",
    "go_forward",
    "reload_page",
    # element-interaction (12) — test_e2e_interaction.py
    "query_elements",
    "click_element",
    "upload_file",
    "type_text",
    "paste_text",
    "select_option",
    "get_element_state",
    "wait_for_element",
    "scroll_page",
    "execute_script",
    "get_page_content",
    "take_screenshot",
    # cookies-storage (2 of 3; get_cookies is exempt below) — test_e2e_interaction
    "set_cookie",
    "clear_cookies",
    # tabs (5) — test_e2e_interaction.py
    "list_tabs",
    "switch_tab",
    "close_tab",
    "get_active_tab",
    "new_tab",
    # network-debugging (10) — test_e2e_data_tools.py
    "list_network_requests",
    "get_request_details",
    "get_response_details",
    "get_response_content",
    "search_network_requests",
    "export_network_data",
    "import_network_data",
    "set_network_capture_filters",
    "get_network_capture_filters",
    "modify_headers",
    # element-extraction (9) — test_e2e_data_tools.py
    "extract_element_styles",
    "extract_element_structure",
    "extract_element_events",
    "extract_element_animations",
    "extract_element_assets",
    "extract_element_styles_cdp",
    "extract_related_files",
    "clone_element_complete",
    "extract_complete_element_cdp",
    # progressive-cloning (10) — test_e2e_data_tools.py
    "clone_element_progressive",
    "expand_styles",
    "expand_events",
    "expand_children",
    "expand_css_rules",
    "expand_pseudo_elements",
    "expand_animations",
    "list_stored_elements",
    "clear_stored_element",
    "clear_all_elements",
    # file-extraction (9) — test_e2e_data_tools.py
    "clone_element_to_file",
    "extract_complete_element_to_file",
    "extract_element_styles_to_file",
    "extract_element_structure_to_file",
    "extract_element_events_to_file",
    "extract_element_animations_to_file",
    "extract_element_assets_to_file",
    "list_clone_files",
    "cleanup_clone_files",
    # cdp-functions (13) — test_e2e_functions_hooks.py
    "list_cdp_commands",
    "execute_cdp_command",
    "get_execution_contexts",
    "discover_global_functions",
    "discover_object_methods",
    "call_javascript_function",
    "inspect_function_signature",
    "inject_and_execute_script",
    "create_persistent_function",
    "execute_function_sequence",
    "create_python_binding",
    "execute_python_in_browser",
    "get_function_executor_info",
    # dynamic-hooks (10) — test_e2e_functions_hooks.py
    "create_dynamic_hook",
    "create_simple_dynamic_hook",
    "list_dynamic_hooks",
    "get_dynamic_hook_details",
    "remove_dynamic_hook",
    "get_hook_documentation",
    "get_hook_examples",
    "get_hook_requirements_documentation",
    "get_hook_common_patterns",
    "validate_hook_function",
    # debugging (5) — test_e2e_functions_hooks.py
    "get_debug_view",
    "clear_debug_view",
    "export_debug_logs",
    "get_debug_lock_status",
    "validate_browser_environment_tool",
}

# Tools intentionally left to another tier, each with a reason.
E2E_EXEMPT: dict[str, str] = {
    "get_cookies": (
        "hangs against real Chrome — the CDP Network.getCookies/getAllCookies "
        "command never returns in the installed nodriver (get_all_cookies is "
        "deprecated since 1.3) and poisons the tab's CDP connection so every "
        "later call times out. Exercising it in E2E would risk leaking a Chrome "
        "tree; the finding is reported for routing. set_cookie and clear_cookies "
        "ARE E2E-covered (asserted via document.cookie)."
    ),
}


def test_e2e_coverage_manifest():
    """The E2E suite covers every registered section tool (partition tripwire)."""
    assert server_mod is not None, "server module failed to load"
    all_names = {name for names in server_mod.SECTION_TOOLS.values() for name in names}
    assert len(all_names) == 94

    covered = set(E2E_COVERED)
    exempt = set(E2E_EXEMPT)
    assert covered.isdisjoint(exempt), (
        f"name is both covered and exempt: {covered & exempt}"
    )
    assert covered | exempt == all_names, {
        "missing_from_manifest": all_names - covered - exempt,
        "unknown_names_in_manifest": (covered | exempt) - all_names,
    }
