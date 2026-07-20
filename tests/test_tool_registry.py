"""C2 registry pins (plan_M4ph1 §2.C / §3-C2).

Pins the F-101/F-505/F-612 extraction of the ``section_tool`` dispatch mechanism
into ``tool_registry.py`` as a ``ToolRegistry`` constructed around the FastMCP
app:

* the ``section_tool`` decorator still stamps M3's per-call correlation id
  (F-308) — the registered function is wrapped with ``with_correlation_id``
  (pinned through a fake FastMCP app, independent of the live registry);
* section membership is recorded in ``SECTION_TOOLS``;
* ``apply_disabled_sections`` unregisters exactly the disabled sections' tools
  via ``mcp.remove_tool`` (and swallows an already-removed tool);
* ``is_section_enabled`` reflects ``DISABLED_SECTIONS``;
* a count-94 tripwire mirroring M6's ``test_tool_dispatch`` (the live registry
  and the section map agree at 94).

The end-to-end correlation-id + ``tools/list`` schema-snapshot pins live in
``test_correlation_id.py`` (unchanged by the move — they drive ``server.mcp``).
"""

from collections import defaultdict

from stealth_chrome_devtools_mcp.embedded import server, tool_registry
from stealth_chrome_devtools_mcp.embedded.logging_setup import correlation_id_var
from stealth_chrome_devtools_mcp.embedded.tool_registry import (
    ToolRegistry,
    is_section_enabled,
)


class _FakeMcp:
    """Drives the registry without a real FastMCP app: the decorator calls
    ``tool``; gating calls ``remove_tool``."""

    def __init__(self):
        self.registered = {}
        self.removed = []

    def tool(self, func):
        self.registered[func.__name__] = func
        return func

    def remove_tool(self, name):
        self.removed.append(name)


class TestSectionToolDecorator:
    async def test_registers_and_stamps_correlation_id(self, monkeypatch):
        # Isolate the module-global map so the test never pollutes the live
        # 94-tool registry that the count tripwire (and M6) reads.
        monkeypatch.setattr(tool_registry, "SECTION_TOOLS", defaultdict(list))
        fake = _FakeMcp()
        registry = ToolRegistry(fake)

        @registry.section_tool("browser-management")
        async def sample_tool():
            return correlation_id_var.get()

        # Section membership recorded, and the wrapped func reached mcp.tool.
        assert tool_registry.SECTION_TOOLS["browser-management"] == ["sample_tool"]
        assert "sample_tool" in fake.registered
        # M3 stamp preserved: a real (non-default) id inside the call, reset after.
        assert correlation_id_var.get() == "-"
        result = await sample_tool()
        assert result != "-"
        assert correlation_id_var.get() == "-"

    def test_preserves_name_via_functools_wraps(self, monkeypatch):
        monkeypatch.setattr(tool_registry, "SECTION_TOOLS", defaultdict(list))
        fake = _FakeMcp()
        registry = ToolRegistry(fake)

        @registry.section_tool("tabs")
        def plain_tool():
            return None

        assert fake.registered["plain_tool"].__name__ == "plain_tool"

    def test_reregistration_is_idempotent(self, monkeypatch):
        # embedded/server.py's body runs more than once per process (canonical
        # import + the entry point's runpy.run_path); a re-registration of the
        # same tool must not double-count it in the shared section map.
        monkeypatch.setattr(tool_registry, "SECTION_TOOLS", defaultdict(list))
        fake = _FakeMcp()
        registry = ToolRegistry(fake)

        @registry.section_tool("tabs")
        def dup_tool():
            return None

        # A second module-body execution re-registers the same name.
        registry.section_tool("tabs")(dup_tool)

        assert tool_registry.SECTION_TOOLS["tabs"] == ["dup_tool"]


class TestApplyDisabledSections:
    def test_removes_only_disabled_sections_tools(self, monkeypatch):
        monkeypatch.setattr(
            tool_registry,
            "SECTION_TOOLS",
            defaultdict(
                list,
                {"tabs": ["list_tabs", "new_tab"], "cookies-storage": ["get_cookies"]},
            ),
        )
        monkeypatch.setattr(tool_registry, "DISABLED_SECTIONS", {"tabs"})
        fake = _FakeMcp()

        ToolRegistry(fake).apply_disabled_sections()

        assert set(fake.removed) == {"list_tabs", "new_tab"}

    def test_swallows_already_removed_tool(self, monkeypatch):
        monkeypatch.setattr(
            tool_registry, "SECTION_TOOLS", defaultdict(list, {"tabs": ["gone"]})
        )
        monkeypatch.setattr(tool_registry, "DISABLED_SECTIONS", {"tabs"})

        class _RaisingMcp(_FakeMcp):
            def remove_tool(self, name):
                raise KeyError(name)

        # A tool already removed by another section policy must not propagate.
        ToolRegistry(_RaisingMcp()).apply_disabled_sections()


class TestIsSectionEnabled:
    def test_reflects_disabled_sections(self, monkeypatch):
        monkeypatch.setattr(tool_registry, "DISABLED_SECTIONS", {"tabs"})
        assert is_section_enabled("tabs") is False
        assert is_section_enabled("cookies-storage") is True


class TestCountTripwire:
    """Mirror of M6's F-108 tripwire from the registry's vantage: the live
    FastMCP registry and the section map agree at 94."""

    EXPECTED_TOOL_COUNT = 94

    async def test_live_count_and_section_sum_are_94(self):
        tools = await server.mcp.get_tools()
        section_sum = sum(len(v) for v in server.SECTION_TOOLS.values())
        assert len(tools) == section_sum == self.EXPECTED_TOOL_COUNT

    def test_cli_description_count_is_registry_derived(self):
        """F-108: the ArgumentParser description count is DERIVED from
        SECTION_TOOLS, never hand-typed — so it can't drift from the surface."""
        section_sum = sum(len(v) for v in server.SECTION_TOOLS.values())
        assert section_sum == self.EXPECTED_TOOL_COUNT
        assert f"with {section_sum} tools" in server.build_arg_parser().description

    def test_list_sections_printed_total_matches_registry(self):
        """F-108: `--list-sections` prints the registry-derived total (and
        per-section counts), so a documented count can't silently diverge from
        the real tool surface."""
        import subprocess
        import sys as _sys

        section_sum = sum(len(v) for v in server.SECTION_TOOLS.values())
        proc = subprocess.run(
            [
                _sys.executable,
                "-m",
                "stealth_chrome_devtools_mcp",
                "--transport",
                "http",
                "--list-sections",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert proc.returncode == 0, proc.stderr
        assert f"Total: {section_sum} tools" in proc.stdout
        assert f"Total: {self.EXPECTED_TOOL_COUNT} tools" in proc.stdout
