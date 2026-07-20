"""Dispatch characterization net (M6-2) + harness smoke (M6-1).

Pins the CURRENT contract of the ``section_tool`` → ``SECTION_TOOLS`` →
``mcp.tool`` dispatch mechanism: the registry/section/``.fn`` invariants, the
true tool count (F-108 == 94), ``apply_disabled_sections``, the
``get_tab → raise "Instance not found" → delegate`` adapter shape for a
representative tool per section, and the F-202 sync-return runtime archetype (the
runtime complement to the ``handle_response`` AST guard in
``test_server_call_conventions.py``).

Hermetic: no Chrome, no backend, no network. Tool bodies run in-process via the
FastMCP ``.fn`` seam with the module-global singletons swapped for fakes.
"""

import inspect
import json

import pytest

from fakes import (
    FakeBrowserManager,
    FakeStorage,
    FakeTab,
    fake_instance,
)
from stealth_chrome_devtools_mcp.embedded import server, tool_registry

# The true post-M2 tool count (F-108 tripwire). If M4-Ph1 changes the tool set,
# it updates this one number with intent.
EXPECTED_TOOL_COUNT = 94

# The 5 tools registered as plain ``def`` (all hook documentation/validation);
# every other tool is ``async def``. ``with_correlation_id`` preserves this
# async/sync split through ``functools.wraps``.
SYNC_TOOL_NAMES = {
    "get_hook_documentation",
    "get_hook_examples",
    "get_hook_requirements_documentation",
    "get_hook_common_patterns",
    "validate_hook_function",
}


async def _live_tools():
    """The live FastMCP registry: {name: FunctionTool}."""
    return await server.mcp.get_tools()


# ===========================================================================
# M6-1 — harness smoke: the .fn seam works end-to-end, hermetically.
# ===========================================================================


async def test_smoke_list_instances_via_seam(call_tool, patched_server):
    """A seeded FakeBrowserManager drives the real ``list_instances`` body
    through ``call_tool`` and yields the expected merged-list shape — proving the
    ``.fn`` unwrap + module-global patch seam end-to-end with zero Chrome."""
    fbm = FakeBrowserManager(
        instances=[fake_instance("i1", "active", "https://example.test", "Example")]
    )
    srv = patched_server(browser_manager=fbm, in_memory_storage=FakeStorage())

    result = await call_tool(srv, "list_instances")

    assert result == [
        {
            "instance_id": "i1",
            "state": "active",
            "current_url": "https://example.test",
            "title": "Example",
            "source": "active",
        }
    ]


def test_smoke_import_server_registered_without_side_effects():
    """``import server`` (at module load) populated the registry and exposed the
    ``.fn`` seam without spawning Chrome or a backend process (conftest sets
    ``STEALTH_MCP_NO_AUTO_RECOVERY``; M11a made ``__init__`` side-effect-free)."""
    assert server.list_instances.fn.__name__ == "list_instances"
    assert inspect.iscoroutinefunction(server.list_instances.fn)
    # A sync tool is registered and NOT a coroutine (the 89/5 split).
    assert server.get_hook_documentation.fn.__name__ == "get_hook_documentation"
    assert not inspect.iscoroutinefunction(server.get_hook_documentation.fn)


# ===========================================================================
# M6-2 — dispatch net: the section_tool -> SECTION_TOOLS -> mcp registry
# contract, the F-108 count, apply_disabled_sections, the adapter shape, and
# the F-202 runtime archetype.
# ===========================================================================


class TestRegistryContract:
    """The registry invariants: name <-> section <-> ``.fn`` <-> live ``mcp``."""

    async def test_live_tool_count_is_94(self):
        """F-108 tripwire: the live FastMCP tool count is exactly 94 (post-M2)."""
        tools = await _live_tools()
        assert len(tools) == EXPECTED_TOOL_COUNT

    async def test_section_sum_equals_flattened_equals_live_count(self):
        flattened = [n for names in server.SECTION_TOOLS.values() for n in names]
        section_sum = sum(len(v) for v in server.SECTION_TOOLS.values())
        tools = await _live_tools()
        assert len(flattened) == section_sum == len(tools) == EXPECTED_TOOL_COUNT

    def test_every_tool_name_belongs_to_exactly_one_section(self):
        flattened = [n for names in server.SECTION_TOOLS.values() for n in names]
        # No name appears in two sections (set size == list size).
        assert len(flattened) == len(set(flattened))

    async def test_registry_names_match_section_names(self):
        tools = await _live_tools()
        flattened = {n for names in server.SECTION_TOOLS.values() for n in names}
        assert set(tools.keys()) == flattened

    async def test_fn_name_equals_registry_name(self):
        """The ``with_correlation_id`` wrapper preserves ``__name__`` (functools.
        wraps), so every registered object's ``.fn.__name__`` equals its key."""
        tools = await _live_tools()
        mismatches = {n: t.fn.__name__ for n, t in tools.items() if t.fn.__name__ != n}
        assert mismatches == {}

    async def test_async_sync_split_is_89_and_5(self):
        """89 tools expose a coroutine ``.fn``; exactly the 5 hook-doc tools are
        plain functions. Pins the split the correlation wrapper branches on."""
        tools = await _live_tools()
        coroutine_tools = {
            n for n, t in tools.items() if inspect.iscoroutinefunction(t.fn)
        }
        plain_tools = set(tools) - coroutine_tools
        assert plain_tools == SYNC_TOOL_NAMES
        assert len(coroutine_tools) == EXPECTED_TOOL_COUNT - len(SYNC_TOOL_NAMES)


class TestSectionGating:
    """``apply_disabled_sections`` unregisters exactly the named section's tools.

    Mutates the live module-singleton ``mcp`` registry, so the test snapshots the
    tool objects up front and re-registers any it removed in a ``finally`` — test
    order stays independent (registry-mutation safety)."""

    async def test_apply_disabled_sections_removes_only_named_section(
        self, monkeypatch
    ):
        before = dict(await server.mcp.get_tools())
        tabs_tools = set(server.SECTION_TOOLS["tabs"])
        keep_section = set(server.SECTION_TOOLS["cookies-storage"])
        try:
            # apply_disabled_sections reads tool_registry's module-global set
            # (server rebinds no longer reach it), so gate the DEFINING module.
            monkeypatch.setattr(tool_registry, "DISABLED_SECTIONS", {"tabs"})
            server.apply_disabled_sections()

            after = await _live_tools()
            # Every tabs tool is gone; every other section is untouched.
            assert tabs_tools.isdisjoint(after.keys())
            assert keep_section.issubset(after.keys())
            assert len(after) == EXPECTED_TOOL_COUNT - len(tabs_tools)
        finally:
            current = await _live_tools()
            for name, tool in before.items():
                if name not in current:
                    server.mcp.add_tool(tool)

    async def test_registry_restored_to_94_after_gating(self):
        """Guards the restore itself: whatever order tests run, the live count is
        back to 94 (this passing after the gating test proves clean teardown)."""
        tools = await _live_tools()
        assert len(tools) == EXPECTED_TOOL_COUNT


# One verified raise-style representative per tab-resolving section. Each does
# ``tab = await browser_manager.get_tab(id); if not tab: raise Exception(
# "Instance not found: id")`` before delegating.
ADAPTER_REPRESENTATIVES = [
    ("browser-management", "reload_page", {"instance_id": "missing"}),
    ("tabs", "new_tab", {"instance_id": "missing", "url": "https://x.test"}),
    ("cdp-functions", "execute_script", {"instance_id": "missing", "script": "1"}),
    (
        "element-interaction",
        "query_elements",
        {"instance_id": "missing", "selector": ".x"},
    ),
    (
        "element-extraction",
        "extract_element_assets",
        {"instance_id": "missing", "selector": ".x"},
    ),
    ("cookies-storage", "get_cookies", {"instance_id": "missing"}),
    (
        "progressive-cloning",
        "clone_element_progressive",
        {"instance_id": "missing", "selector": ".x"},
    ),
    (
        "file-extraction",
        "clone_element_to_file",
        {"instance_id": "missing", "selector": ".x"},
    ),
]


class TestAdapterContract:
    """The ``get_tab -> raise 'Instance not found' -> delegate`` adapter shape."""

    @pytest.mark.parametrize(
        ("section", "tool", "kwargs"),
        ADAPTER_REPRESENTATIVES,
        ids=[f"{s}:{t}" for s, t, _ in ADAPTER_REPRESENTATIVES],
    )
    async def test_missing_instance_raises_not_found(
        self, section, tool, kwargs, call_tool, patched_server
    ):
        # get_tab returns None -> the adapter must raise before any delegation.
        srv = patched_server(browser_manager=FakeBrowserManager())
        with pytest.raises(Exception, match=r"[Nn]ot found"):
            await call_tool(srv, tool, **kwargs)

    @pytest.mark.characterization
    async def test_adapter_not_found_shape_is_non_uniform(
        self, call_tool, patched_server
    ):
        """PINS CURRENT BEHAVIOR incl. known quirk F-105/adapter-non-uniformity;
        M4 will intentionally change this — update the assertion when that fix
        lands. Not every tool uses the raise-style adapter: some delegate to a
        BrowserManager method, and ``get_instance_state`` degrades to a partial
        dict instead of raising. The adapter contract is *not* uniform across the
        94 tools; that inconsistency is the finding M4 formalizes."""
        srv = patched_server(browser_manager=FakeBrowserManager())
        # Graceful-degradation variant: returns a dict, does NOT raise.
        state = await call_tool(srv, "get_instance_state", instance_id="missing")
        assert isinstance(state, (dict, str))
        text = state if isinstance(state, str) else json.dumps(state)
        assert "missing" in text


class _FakeResponseCloner:
    """A cloner whose extract methods return a plain dict — enough to exercise
    the synchronous ``response_handler.handle_response`` return path."""

    async def extract_element_assets(self, tab, **kwargs):
        return {"images": ["a.png"], "fonts": []}

    async def extract_related_files(self, tab, **kwargs):
        return {"stylesheets": ["main.css"], "scripts": []}

    async def extract_complete_element(self, tab, **kwargs):
        return {"html": {"tagName": "DIV"}, "styles": {}}


class TestF202RuntimeArchetype:
    """Runtime complement to the ``handle_response`` AST guard
    (``test_server_call_conventions.py``): invoking the three handle_response
    tools returns a plain ``dict`` — never a coroutine, never a ``TypeError`` from
    awaiting the synchronous ``handle_response`` return."""

    @pytest.mark.parametrize(
        ("tool", "kwargs", "expected"),
        [
            (
                "extract_element_assets",
                {"instance_id": "i1", "selector": ".x"},
                {"images": ["a.png"], "fonts": []},
            ),
            (
                "extract_related_files",
                {"instance_id": "i1"},
                {"stylesheets": ["main.css"], "scripts": []},
            ),
            (
                "clone_element_complete",
                {"instance_id": "i1", "selector": ".x"},
                {"html": {"tagName": "DIV"}, "styles": {}},
            ),
        ],
    )
    async def test_handle_response_tool_returns_dict(
        self, tool, kwargs, expected, call_tool, patched_server
    ):
        fake_cloner = _FakeResponseCloner()
        srv = patched_server(
            browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}),
            # extract_element_assets + clone_element_complete now route through the
            # canonical engine (M5b-2/3a) -> extract_complete_element; only
            # extract_related_files still goes via element_cloner (until M5b-5).
            # comprehensive_element_cloner is no longer a server global.
            cdp_element_cloner=fake_cloner,
            element_cloner=fake_cloner,
        )
        result = await call_tool(srv, tool, **kwargs)
        # The real (synchronous) response_handler passes a small dict through
        # unchanged: a dict comes back, not a coroutine, and no TypeError raised.
        assert isinstance(result, dict)
        assert not inspect.iscoroutine(result)
        assert result == expected
