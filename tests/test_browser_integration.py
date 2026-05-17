"""Integration tests that spawn real browsers.

These require Chrome/Chromium installed and take ~30s to run.
Mark: pytest -m integration

Skip in CI without Chrome: pytest -m "not integration"
"""

import asyncio
import sys
from pathlib import Path

import pytest

# Embedded modules via conftest sys.path setup
import importlib.util

# We need to import server.py as a module (it uses bare imports internally)
_spec = importlib.util.spec_from_file_location(
    "server",
    Path(__file__).resolve().parent.parent
    / "src" / "stealth_chrome_devtools_mcp" / "embedded" / "server.py",
)
_server_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("server", _server_mod)
try:
    _spec.loader.exec_module(_server_mod)
except Exception:
    _server_mod = None


def _unwrap(fn):
    """FunctionTool wraps the original coroutine."""
    return getattr(fn, "fn", fn)


# Guard: skip all tests if server module failed to load or Chrome not found
pytestmark = pytest.mark.integration

_can_run = False
try:
    from platform_utils import check_browser_executable
    _can_run = _server_mod is not None and check_browser_executable() is not None
except Exception:
    pass

if not _can_run:
    pytestmark = [pytest.mark.integration, pytest.mark.skip("Chrome not available or server failed to load")]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_fn(name):
    """Get an unwrapped server function by name."""
    fn = getattr(_server_mod, name, None)
    if fn is None:
        pytest.skip(f"server.{name} not found")
    return _unwrap(fn)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBrowserSpawnAndClose:
    """Basic spawn → verify → close lifecycle."""

    @pytest.mark.asyncio
    async def test_spawn_no_args(self):
        """Spawn with defaults, verify ready, close."""
        spawn = _get_fn("spawn_browser")
        close = _get_fn("close_instance")

        result = await spawn(headless=True)
        assert result["state"] == "ready"
        assert result["instance_id"]
        iid = result["instance_id"]

        closed = await close(instance_id=iid)
        assert closed["result"] is True

    @pytest.mark.asyncio
    async def test_spawn_with_relative_user_data_dir(self, tmp_path):
        """Relative user_data_dir should resolve to sessions/."""
        spawn = _get_fn("spawn_browser")
        close = _get_fn("close_instance")

        result = await spawn(headless=True, user_data_dir="integration-test-profile")
        iid = result["instance_id"]
        udd = result["spawn_diagnostics"]["user_data_dir"]

        try:
            assert Path(udd).is_absolute()
            assert "sessions" in udd or "integration-test-profile" in udd
            assert result["state"] == "ready"
        finally:
            await close(instance_id=iid)

    @pytest.mark.asyncio
    async def test_spawn_detectable_args_stripped(self):
        """Detectable args should be silently stripped."""
        spawn = _get_fn("spawn_browser")
        close = _get_fn("close_instance")

        result = await spawn(
            headless=True,
            browser_args=["--enable-automation", "--no-sandbox", "--lang=en-US"],
        )
        iid = result["instance_id"]
        diag = result["spawn_diagnostics"]

        try:
            assert result["state"] == "ready"
            # Stealth warnings should be present
            stripped = diag.get("stealth_args_stripped", [])
            assert len(stripped) >= 2  # --enable-automation and --no-sandbox
            # --lang=en-US should survive
            assert any("lang" in arg for arg in diag.get("effective_browser_args", []))
        finally:
            await close(instance_id=iid)


class TestNavigateAndScreenshot:
    """Navigate to a data: URL and take a screenshot."""

    DATA_URL = (
        "data:text/html,<html><body>"
        "<h1 id='title'>Integration Test</h1>"
        "<input id='box' placeholder='type here'>"
        "</body></html>"
    )

    @pytest.mark.asyncio
    async def test_navigate_and_content(self):
        spawn = _get_fn("spawn_browser")
        navigate = _get_fn("navigate")
        get_content = _get_fn("get_page_content")
        close = _get_fn("close_instance")

        result = await spawn(headless=True)
        iid = result["instance_id"]

        try:
            nav = await navigate(instance_id=iid, url=self.DATA_URL)
            assert nav.get("success") or nav.get("url")

            content = await get_content(instance_id=iid)
            assert "Integration Test" in content.get("content", "")
        finally:
            await close(instance_id=iid)

    @pytest.mark.asyncio
    async def test_execute_script(self):
        spawn = _get_fn("spawn_browser")
        navigate = _get_fn("navigate")
        execute = _get_fn("execute_script")
        close = _get_fn("close_instance")

        result = await spawn(headless=True)
        iid = result["instance_id"]

        try:
            await navigate(instance_id=iid, url=self.DATA_URL)
            r = await execute(
                instance_id=iid,
                script="document.getElementById('title').textContent",
            )
            assert "Integration Test" in str(r.get("result", r))
        finally:
            await close(instance_id=iid)


class TestSelectiveClose:
    """Close one browser without affecting others."""

    @pytest.mark.asyncio
    async def test_close_one_keeps_others(self):
        spawn = _get_fn("spawn_browser")
        close = _get_fn("close_instance")
        list_fn = _get_fn("list_instances")

        a = await spawn(headless=True)
        b = await spawn(headless=True)

        # Close A
        await close(instance_id=a["instance_id"])

        # B should still be alive
        instances = await list_fn()
        active_ids = [i["instance_id"] for i in instances]
        assert b["instance_id"] in active_ids
        assert a["instance_id"] not in active_ids

        # Cleanup
        await close(instance_id=b["instance_id"])


class TestIdleTimeout:
    """Verify idle_timeout_seconds=0 means no auto-close."""

    @pytest.mark.asyncio
    async def test_zero_timeout_no_autoclose(self):
        spawn = _get_fn("spawn_browser")
        close = _get_fn("close_instance")
        list_fn = _get_fn("list_instances")

        result = await spawn(headless=True)
        iid = result["instance_id"]
        assert result["spawn_diagnostics"]["idle_timeout_seconds"] == 0

        # Wait briefly then confirm still alive
        await asyncio.sleep(2)
        instances = await list_fn()
        active_ids = [i["instance_id"] for i in instances]
        assert iid in active_ids

        await close(instance_id=iid)
