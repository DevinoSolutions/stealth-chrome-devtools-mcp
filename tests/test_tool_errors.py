"""C3a pins (plan_M4ph1 §2.D / §3-C3a / Amendment A1 §A1.2).

Pins the ONE error convention for the MCP tool surface (F-104/F-761/F-746):

* ``ToolError`` / ``InstanceNotFoundError`` are raised (FastMCP surfaces the
  raised error to the client) — replacing the former per-tool
  ``{"success": False, ...}`` dicts and ``json.dumps({"error": ...})`` returns;
* ``_require_tab`` / ``_require_browser`` are the single instance-not-found
  guard: raise ``InstanceNotFoundError("Instance not found: {id}")`` on a miss,
  return the handle on a hit;
* the C3a shape-groups A1 §A1.2 assigns to this checkpoint — **G1** (was
  ``raise Exception``), **G1'** (``@mcp.resource`` json), **G1''**
  (cdp-functions dict) — now all raise ``InstanceNotFoundError`` with the exact
  ``Instance not found: {id}`` message text (M6's adapter-shape pins hold).

The remaining operation-specific handlers (G2-G5) are C3b; the deliberate
KEEP contracts (G6/G7) are C3b guards. This file grows in C3b.
"""

import pytest

from fakes import FakeBrowserManager
from stealth_chrome_devtools_mcp.embedded import server
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    InstanceNotFoundError,
    ToolError,
    _require_browser,
    _require_tab,
)


class _NoInstances:
    """browser_manager stand-in whose every lookup misses (returns None)."""

    async def get_tab(self, instance_id, touch_activity=False):
        return None

    async def get_browser(self, instance_id, touch_activity=False):
        return None

    async def get_page_state(self, instance_id):
        return None


class _Present:
    """browser_manager stand-in that always resolves to one handle."""

    def __init__(self, handle):
        self._handle = handle

    async def get_tab(self, instance_id, touch_activity=False):
        return self._handle

    async def get_browser(self, instance_id, touch_activity=False):
        return self._handle


class TestErrorTypes:
    def test_hierarchy(self):
        assert issubclass(ToolError, Exception)
        assert issubclass(InstanceNotFoundError, ToolError)

    def test_message_is_preserved(self):
        assert str(ToolError("boom")) == "boom"
        assert str(InstanceNotFoundError("Instance not found: x")) == (
            "Instance not found: x"
        )


class TestRequireGuards:
    """The single instance-not-found guard, tested hermetically with a fake
    browser_manager passed in (no server import — the module stays a leaf)."""

    async def test_require_tab_raises_on_miss(self):
        with pytest.raises(InstanceNotFoundError, match=r"Instance not found: missing"):
            await _require_tab(_NoInstances(), "missing")

    async def test_require_tab_returns_handle_on_hit(self):
        sentinel = object()
        assert await _require_tab(_Present(sentinel), "abc") is sentinel

    async def test_require_browser_raises_on_miss(self):
        with pytest.raises(InstanceNotFoundError, match=r"Instance not found: missing"):
            await _require_browser(_NoInstances(), "missing")

    async def test_require_browser_returns_handle_on_hit(self):
        sentinel = object()
        assert await _require_browser(_Present(sentinel), "abc") is sentinel


class TestInstanceNotFoundCluster:
    """A1 §A1.2 shape-group pins for the C3a cluster: every instance-not-found
    path now RAISES ``InstanceNotFoundError`` (was: raise Exception / dict / json),
    message text unchanged."""

    async def test_g1_tool_raises_typed_not_found(self, call_tool, patched_server):
        # G1: was ``raise Exception("Instance not found: {id}")``.
        srv = patched_server(browser_manager=FakeBrowserManager())
        with pytest.raises(InstanceNotFoundError, match=r"Instance not found: missing"):
            await call_tool(srv, "reload_page", instance_id="missing")

    async def test_g1pp_cdp_tool_raises_instead_of_dict(
        self, call_tool, patched_server
    ):
        # G1'': was ``return {"success": False, "error": "Instance not found: {id}"}``.
        srv = patched_server(browser_manager=FakeBrowserManager())
        with pytest.raises(InstanceNotFoundError, match=r"Instance not found: missing"):
            await call_tool(
                srv,
                "call_javascript_function",
                instance_id="missing",
                function_path="x",
            )

    async def test_g1prime_resource_raises_instead_of_json(self, monkeypatch):
        # G1': was ``return json.dumps({"error": "Instance not found"})``.
        monkeypatch.setattr(server, "browser_manager", _NoInstances())
        with pytest.raises(InstanceNotFoundError, match=r"Instance not found: missing"):
            await server.get_browser_state_resource.fn("missing")
