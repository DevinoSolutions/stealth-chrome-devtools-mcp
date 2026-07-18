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

from fakes import FakeBrowserManager, FakeTab
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

    async def get_active_tab(self, instance_id):
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


class _BrowserRaisingOnGet:
    """browser stand-in whose tab creation fails (drives new_tab's G2 wrap)."""

    async def get(self, url, new_tab=False):
        raise RuntimeError("cdp boom")


class _DomHandler:
    """dom_handler stand-in: returns a result, or raises to drive the error path."""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def execute_script(self, tab, script, args):
        if self._error is not None:
            raise self._error
        return self._result


class _DebugBoom:
    """debug_logger stand-in whose lock probe raises (drives get_debug_lock_status)."""

    def get_lock_status(self):
        raise RuntimeError("lock probe failed")


class TestG2GenericRewrap:
    """G2 (A1.3/A1.5): terminal ``raise Exception("Failed to ...")`` re-wraps now
    ``raise ToolError`` (typed, message preserved). spawn_browser shares this
    shape; its full spawn pipeline isn't feasibly unit-triggered, so new_tab is
    the group's hermetic pin (spawn_browser:437 is converted identically)."""

    async def test_new_tab_wraps_failure_as_tool_error(self, call_tool, patched_server):
        srv = patched_server(
            browser_manager=FakeBrowserManager(browsers={"i1": _BrowserRaisingOnGet()})
        )
        with pytest.raises(ToolError, match=r"Failed to create new tab"):
            await call_tool(srv, "new_tab", instance_id="i1")


class TestG3BareErrorDict:
    """G3: get_debug_lock_status' bare ``{"error": str(e)}`` now raises ToolError."""

    async def test_raises_tool_error_on_probe_failure(self, call_tool, patched_server):
        srv = patched_server(debug_logger=_DebugBoom())
        with pytest.raises(ToolError, match=r"lock probe failed"):
            await call_tool(srv, "get_debug_lock_status")


class TestG4InputValidationRaise:
    """G4: raised input-validation errors now ``raise ToolError``, message kept."""

    async def test_select_option_invalid_index_raises(self, call_tool, patched_server):
        srv = patched_server(browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}))
        with pytest.raises(ToolError, match=r"Invalid index value: abc"):
            await call_tool(
                srv, "select_option", instance_id="i1", selector="#s", index="abc"
            )

    async def test_clone_element_complete_invalid_json_raises(
        self, call_tool, patched_server
    ):
        srv = patched_server(browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}))
        with pytest.raises(ToolError, match=r"Invalid JSON in extraction_options"):
            await call_tool(
                srv,
                "clone_element_complete",
                instance_id="i1",
                selector="#s",
                extraction_options="{not json",
            )


class TestG5ResultEnvelope:
    """G5 (A1.5): execute_script / create_python_binding ERROR paths raise
    ToolError (message preserved); the SUCCESS dict is a deliberate value
    contract, pinned VERBATIM (NOT converted)."""

    async def test_execute_script_operational_error_raises(
        self, call_tool, patched_server
    ):
        srv = patched_server(
            browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}),
            dom_handler=_DomHandler(error=RuntimeError("js exploded")),
        )
        with pytest.raises(ToolError, match=r"js exploded"):
            await call_tool(srv, "execute_script", instance_id="i1", script="1 + 1")

    async def test_execute_script_success_dict_is_verbatim(
        self, call_tool, patched_server
    ):
        srv = patched_server(
            browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}),
            dom_handler=_DomHandler(result="R"),
        )
        res = await call_tool(srv, "execute_script", instance_id="i1", script="1 + 1")
        assert res == {"success": True, "result": "R", "error": None}

    async def test_create_python_binding_no_function_raises(
        self, call_tool, patched_server
    ):
        srv = patched_server(browser_manager=FakeBrowserManager(tabs={"i1": FakeTab()}))
        with pytest.raises(ToolError, match=r"No function found in Python code"):
            await call_tool(
                srv,
                "create_python_binding",
                instance_id="i1",
                binding_name="b",
                python_code="x = 1",
            )


class TestKeepContractsUnchanged:
    """G6/G7 KEEP: value/dict/fallback contracts that legitimately RETURN values
    stay UNCHANGED (converting would delete a deliberate contract). Other KEEPs
    are pinned in dedicated tests: execute_script's rejection (G7 input-
    validation) in test_execute_script_guard.py; get_instance_state's partial
    (G7) in test_tool_dispatch.py's characterization pin."""

    async def test_expand_children_invalid_arg_returns_error_dict(
        self, call_tool, patched_server
    ):
        # G7 input-validation VALUE-return: describes a bad argument, not an
        # operational failure -- stays a dict (NOT a raise).
        srv = patched_server()
        res = await call_tool(srv, "expand_children", element_id="e", max_count="abc")
        assert res == {"error": "Invalid max_count value: abc"}


class TestC3bAddendum:
    """C3b addendum (human ruling): the two residual operation-specific error
    shapes join the ONE convention, message text byte-preserved. Neither is an
    instance-not-found miss, so both stay OUT of ``_require_tab``:

    * ``get_active_tab`` returned a bare ``{"error": "No active tab found"}``
      value-dict when its OWN ``get_active_tab`` lookup (not ``get_tab``) came back
      empty -> now ``raise ToolError("No active tab found")``;
    * ``set_cookie`` raised a bare ``Exception`` for the url/domain argument-
      validation guard (after the tab already resolved) -> now ``raise ToolError``.
    """

    async def test_get_active_tab_no_tab_raises(self, call_tool, patched_server):
        # Was ``return {"error": "No active tab found"}`` (a value-return dict).
        srv = patched_server(browser_manager=_NoInstances())
        with pytest.raises(ToolError, match=r"No active tab found"):
            await call_tool(srv, "get_active_tab", instance_id="missing")

    async def test_set_cookie_no_url_or_domain_raises(self, call_tool, patched_server):
        # Was ``raise Exception("At least one of 'url' or 'domain' ...")``. The tab
        # resolves (FakeTab) but url/domain are absent AND ``tab.url`` is falsy
        # (seed ``url=""``), so the argument-validation guard fires.
        srv = patched_server(
            browser_manager=FakeBrowserManager(tabs={"i1": FakeTab(url="")})
        )
        with pytest.raises(ToolError, match=r"At least one of"):
            await call_tool(srv, "set_cookie", instance_id="i1", name="n", value="v")
