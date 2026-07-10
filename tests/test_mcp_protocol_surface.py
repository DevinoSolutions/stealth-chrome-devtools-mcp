"""plan_E2E §2.5 — MCP protocol-surface tests (hermetic, no Chrome).

Drives tools through the FastMCP layer (schema validation + serialization) using
the in-memory ``fastmcp.Client(server.mcp)`` transport, with the M6 fakes patched
in so the tier stays hermetic. This is the seam nothing else covers: everywhere
else calls the raw ``.fn`` coroutine directly, bypassing the protocol boundary
that validates arguments and serializes results. Verified against the installed
fastmcp 2.11.2 (``CallToolResult.data`` holds the deserialized return value;
missing/mistyped params raise a validation ``ToolError``).

NOT integration-marked: runs in the unit job.
"""

from __future__ import annotations

import sys
from pathlib import Path

import fastmcp
import pytest

# Make embedded/ importable the same way conftest / the entrypoint does.
EMBEDDED_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
)
if str(EMBEDDED_DIR) not in sys.path:
    sys.path.insert(0, str(EMBEDDED_DIR))

import server

from fakes import FakeBrowserManager, FakeStorage, call_tool, fake_instance


class _ProtocolBrowserManager(FakeBrowserManager):
    """``FakeBrowserManager`` plus the no-op lifecycle hooks the FastMCP
    in-memory Client's server-lifespan invokes on startup/shutdown
    (``start_idle_reaper`` / ``stop_idle_reaper`` / ``close_all``). Defined here
    so the fakes-dependent test can boot the *real* transport against a seeded
    fake without modifying the shared ``tests/fakes.py`` harness.
    """

    async def start_idle_reaper(self) -> None:
        """No-op: the idle reaper never runs against a fake."""

    async def stop_idle_reaper(self) -> None:
        """No-op: paired with start_idle_reaper."""

    async def close_all(self) -> None:
        """No-op: nothing to close on a fake manager."""


async def test_list_instances_via_protocol_matches_seam(patched_server):
    """Happy path through the protocol layer equals the known seam result.

    The registered tool resolves ``browser_manager`` from the server namespace at
    call time, so the M6 ``patched_server`` fake is used even when the call comes
    through FastMCP's in-memory transport — proving the hermetic seam holds one
    layer up from the raw ``.fn`` calls.
    """
    patched_server(
        browser_manager=_ProtocolBrowserManager(
            instances=[fake_instance("i1", "active", "https://example.test", "Example")]
        ),
        in_memory_storage=FakeStorage(),
    )
    # The raw .fn seam result is the reference the protocol result must match.
    seam_result = await call_tool(server, "list_instances")
    async with fastmcp.Client(server.mcp) as client:
        protocol_result = await client.call_tool("list_instances", {})

    # A list return serializes to structured_content under the MCP "result" key
    # (an object wrapper, since MCP structured content must be an object). That
    # plain payload — not the opaque ``.data`` models fastmcp builds for a
    # ``dict[str, Any]`` item type — is what we compare to the seam.
    assert protocol_result.structured_content == {"result": seam_result}
    assert seam_result == [
        {
            "instance_id": "i1",
            "state": "active",
            "current_url": "https://example.test",
            "title": "Example",
            "source": "active",
        }
    ]


async def test_sync_hook_doc_tool_via_protocol():
    """A sync (plain ``def``) tool still round-trips through the async protocol
    layer and serializes to structured data."""
    async with fastmcp.Client(server.mcp) as client:
        result = await client.call_tool("get_hook_documentation", {})

    assert result.data is not None
    assert result.structured_content  # non-empty structured payload


async def test_list_returning_tool_serializes_cleanly():
    """A list-returning tool needing no browser serializes to a list of strings —
    exercises the protocol result-serialization path end to end."""
    async with fastmcp.Client(server.mcp) as client:
        result = await client.call_tool("list_cdp_commands", {})

    assert isinstance(result.data, list)
    assert len(result.data) > 0
    assert all(isinstance(item, str) for item in result.data)


async def test_missing_required_param_is_validation_error():
    """Omitting a required param fails schema validation (not a body crash)."""
    async with fastmcp.Client(server.mcp) as client:
        with pytest.raises(Exception, match="valid"):
            await client.call_tool("navigate", {})


async def test_wrong_type_param_is_validation_error():
    """A wrong JSON type for a typed param fails schema validation."""
    async with fastmcp.Client(server.mcp) as client:
        with pytest.raises(Exception, match="valid"):
            await client.call_tool(
                "navigate", {"instance_id": ["not", "a", "string"], "url": "x"}
            )


async def test_unknown_tool_raises():
    """Calling a tool that is not registered raises (never silently succeeds)."""
    async with fastmcp.Client(server.mcp) as client:
        with pytest.raises(Exception):
            await client.call_tool("does_not_exist_tool", {})
