"""Integration tests that spawn real browsers.

These require Chrome/Chromium installed and take ~30s to run.
Mark: pytest -m integration

Skip in CI without Chrome: pytest -m "not integration"
"""

import asyncio
import os
import sys
import time
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
_needs_no_sandbox = False
try:
    from platform_utils import check_browser_executable, is_running_as_root, is_running_in_container
    _can_run = _server_mod is not None and check_browser_executable() is not None
    _needs_no_sandbox = (
        is_running_as_root()
        or is_running_in_container()
        or os.environ.get("CI") == "true"  # GitHub Actions, GitLab CI, etc.
    )
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


def _sandbox_kwargs() -> dict:
    """Return sandbox=False when running as root/container (CI), else empty."""
    return {"sandbox": False} if _needs_no_sandbox else {}


# ---------------------------------------------------------------------------
# Warmup — first Chrome launch on CI is slow / flaky
# ---------------------------------------------------------------------------

_warmed_up = False


@pytest.fixture(autouse=True)
async def _warmup_chrome():
    """Launch and close a throwaway browser before the first real test."""
    global _warmed_up
    if _warmed_up or not _can_run:
        yield
        return
    _warmed_up = True
    spawn = _get_fn("spawn_browser")
    close = _get_fn("close_instance")
    try:
        result = await spawn(headless=True, user_data_dir="ci-warmup", **_sandbox_kwargs())
        await close(instance_id=result["instance_id"])
    except Exception:
        pass  # warmup failure is non-fatal
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBrowserSpawnAndClose:
    """Basic spawn → verify → close lifecycle."""

    @pytest.mark.asyncio
    async def test_spawn_and_close(self):
        """Spawn with a named session, verify ready, close."""
        spawn = _get_fn("spawn_browser")
        close = _get_fn("close_instance")

        result = await spawn(
            headless=True,
            user_data_dir="ci-basic-test",
            **_sandbox_kwargs(),
        )
        assert result["state"] == "ready"
        assert result["instance_id"]
        iid = result["instance_id"]

        closed = await close(instance_id=iid)
        assert closed is True or (isinstance(closed, dict) and closed.get("result") is True)

    @pytest.mark.asyncio
    async def test_spawn_with_relative_user_data_dir(self, tmp_path):
        """Relative user_data_dir should resolve to sessions/."""
        spawn = _get_fn("spawn_browser")
        close = _get_fn("close_instance")

        result = await spawn(headless=True, user_data_dir="integration-test-profile", **_sandbox_kwargs())
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
            **_sandbox_kwargs(),
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


class TestClosePerformance:
    """close_instance must be fast.

    Regression guard for the teardown-ordering bug where ``close_instance``
    disconnected the CDP websocket *before* sending the graceful
    ``Browser.close`` command. nodriver's ``send()`` then silently reconnected
    and awaited a response that the dying browser never returns — hanging for
    the full 5s ``wait_for`` budget and pushing every close to 6-8s.

    These are "time-tested" bounds: measured on the fixed path, with margin.
    """

    DATA_URL = "data:text/html,<h1 id='t'>close-perf</h1>"

    @pytest.mark.asyncio
    async def test_close_is_fast(self):
        spawn = _get_fn("spawn_browser")
        navigate = _get_fn("navigate")
        close = _get_fn("close_instance")

        result = await spawn(headless=True, **_sandbox_kwargs())
        iid = result["instance_id"]
        await navigate(instance_id=iid, url=self.DATA_URL)

        t0 = time.monotonic()
        await close(instance_id=iid)
        elapsed = time.monotonic() - t0

        # The old hang made this 6-8s (it blocked the entire 5s wait_for plus
        # forced cleanup). A correct close sends Browser.close on the live
        # connection (~1ms) and never reconnects.
        assert elapsed < 3.0, f"close took {elapsed:.2f}s — teardown hang regressed"


class TestCloseKillsProcessTree:
    """close_instance must kill the WHOLE Chrome process tree, not just root.

    The shipped bug: Chrome's renderer/GPU/utility/crashpad children do not
    carry ``--user-data-dir`` on Windows, so the profile-cmdline match that
    close relied on never enumerated them. Only the root ``chrome.exe`` was
    killed; the ~15 children orphaned — one leaked tree per spawn. The old
    "fast teardown" test passed precisely *because* close didn't wait for or
    ensure the children's death.

    This captures the live descendant PIDs before close and asserts every one
    is gone after. On Linux the children die with the parent regardless (which
    is why CI never caught it); on Windows this fails on the old root-only kill.
    """

    DATA_URL = "data:text/html,<h1 id='t'>tree</h1>"

    @pytest.mark.asyncio
    async def test_close_kills_entire_chrome_tree(self):
        import psutil
        import process_cleanup as pc_mod

        spawn = _get_fn("spawn_browser")
        navigate = _get_fn("navigate")
        close = _get_fn("close_instance")

        result = await spawn(headless=True, user_data_dir="tree-kill-test", **_sandbox_kwargs())
        iid = result["instance_id"]
        # Navigate so a renderer child definitely exists.
        await navigate(instance_id=iid, url=self.DATA_URL)

        metadata = pc_mod.process_cleanup.browser_processes.get(iid) or {}
        root_pid = metadata.get("pid")
        if not isinstance(root_pid, int) or not psutil.pid_exists(root_pid):
            pytest.skip("no tracked root pid to inspect")

        try:
            descendants = psutil.Process(root_pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        tree = [root_pid, *[p.pid for p in descendants]]

        try:
            await close(instance_id=iid)

            # Give the OS a beat to reap the tree.
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and any(psutil.pid_exists(p) for p in tree):
                await asyncio.sleep(0.2)

            survivors = [p for p in tree if psutil.pid_exists(p)]
            assert survivors == [], (
                f"close leaked {len(survivors)}/{len(tree)} chrome process(es): "
                f"{survivors} (root={root_pid}, {len(descendants)} children captured)"
            )
        finally:
            # Safety net: never let a failed assertion leak a real Chrome tree.
            for p in tree:
                try:
                    psutil.Process(p).kill()
                except psutil.Error:
                    pass


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

        result = await spawn(headless=True, **_sandbox_kwargs())
        iid = result["instance_id"]

        try:
            nav = await navigate(instance_id=iid, url=self.DATA_URL)
            assert nav.get("success") or nav.get("url")

            content = await get_content(instance_id=iid)
            body = content.get("content", "") or content.get("html", "")
            assert "Integration Test" in body
        finally:
            await close(instance_id=iid)

    @pytest.mark.asyncio
    async def test_execute_script(self):
        spawn = _get_fn("spawn_browser")
        navigate = _get_fn("navigate")
        execute = _get_fn("execute_script")
        close = _get_fn("close_instance")

        result = await spawn(headless=True, **_sandbox_kwargs())
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

        a = await spawn(headless=True, **_sandbox_kwargs())
        b = await spawn(headless=True, **_sandbox_kwargs())

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

        result = await spawn(headless=True, **_sandbox_kwargs())
        iid = result["instance_id"]
        assert result["spawn_diagnostics"]["idle_timeout_seconds"] == 0

        # Wait briefly then confirm still alive
        await asyncio.sleep(2)
        instances = await list_fn()
        active_ids = [i["instance_id"] for i in instances]
        assert iid in active_ids

        await close(instance_id=iid)


class TestFileUpload:
    """upload_file attaches local files to a file input via CDP (non-blocking)."""

    UPLOAD_URL = (
        "data:text/html,<html><body>"
        "<input id='single' type='file'>"
        "<input id='multi' type='file' multiple>"
        "</body></html>"
    )

    @pytest.mark.asyncio
    async def test_upload_single_file(self, tmp_path):
        spawn = _get_fn("spawn_browser")
        navigate = _get_fn("navigate")
        upload = _get_fn("upload_file")
        execute = _get_fn("execute_script")
        close = _get_fn("close_instance")

        f = tmp_path / "alpha.txt"
        f.write_text("alpha", encoding="utf-8")

        result = await spawn(headless=True, **_sandbox_kwargs())
        iid = result["instance_id"]
        try:
            await navigate(instance_id=iid, url=self.UPLOAD_URL)

            t0 = time.monotonic()
            res = await upload(instance_id=iid, selector="#single", file_paths=str(f))
            elapsed = time.monotonic() - t0

            assert res["count"] == 1
            # Proves it does not block the renderer (the sync-XHR bug froze ~30s).
            assert elapsed < 5.0, f"upload should be near-instant, took {elapsed:.1f}s"

            name = await execute(
                instance_id=iid,
                script="document.getElementById('single').files[0].name",
            )
            assert "alpha.txt" in str(name.get("result", name))
        finally:
            await close(instance_id=iid)

    @pytest.mark.asyncio
    async def test_upload_multiple_files(self, tmp_path):
        spawn = _get_fn("spawn_browser")
        navigate = _get_fn("navigate")
        upload = _get_fn("upload_file")
        execute = _get_fn("execute_script")
        close = _get_fn("close_instance")

        a = tmp_path / "a.txt"
        a.write_text("a", encoding="utf-8")
        b = tmp_path / "b.txt"
        b.write_text("b", encoding="utf-8")

        result = await spawn(headless=True, **_sandbox_kwargs())
        iid = result["instance_id"]
        try:
            await navigate(instance_id=iid, url=self.UPLOAD_URL)
            res = await upload(
                instance_id=iid, selector="#multi", file_paths=[str(a), str(b)]
            )
            assert res["count"] == 2
            count = await execute(
                instance_id=iid,
                script="document.getElementById('multi').files.length",
            )
            assert int(count.get("result")) == 2
        finally:
            await close(instance_id=iid)

    @pytest.mark.asyncio
    async def test_upload_missing_file_raises(self, tmp_path):
        spawn = _get_fn("spawn_browser")
        navigate = _get_fn("navigate")
        upload = _get_fn("upload_file")
        close = _get_fn("close_instance")

        result = await spawn(headless=True, **_sandbox_kwargs())
        iid = result["instance_id"]
        try:
            await navigate(instance_id=iid, url=self.UPLOAD_URL)
            with pytest.raises(Exception, match="File not found"):
                await upload(
                    instance_id=iid,
                    selector="#single",
                    file_paths=str(tmp_path / "nope.txt"),
                )
        finally:
            await close(instance_id=iid)

    @pytest.mark.asyncio
    async def test_upload_rejects_non_file_input(self, tmp_path):
        spawn = _get_fn("spawn_browser")
        navigate = _get_fn("navigate")
        upload = _get_fn("upload_file")
        close = _get_fn("close_instance")

        f = tmp_path / "x.txt"
        f.write_text("x", encoding="utf-8")

        result = await spawn(headless=True, **_sandbox_kwargs())
        iid = result["instance_id"]
        try:
            await navigate(instance_id=iid, url=self.UPLOAD_URL)
            with pytest.raises(Exception, match="file input"):
                await upload(instance_id=iid, selector="body", file_paths=str(f))
        finally:
            await close(instance_id=iid)


class TestAutoCloneDeletion:
    """End-to-end: disposable auto-clones are deleted on close, while the
    master profile and explicit named profiles are always preserved.
    """

    @pytest.mark.asyncio
    async def test_auto_clone_deleted_master_survives(self, tmp_session_root):
        from process_cleanup import process_cleanup as _pc

        spawn = _get_fn("spawn_browser")
        close = _get_fn("close_instance")
        dirs = tmp_session_root

        # First spawn with no user_data_dir takes the master profile directly.
        master_inst = await spawn(headless=True, **_sandbox_kwargs())
        master_iid = master_inst["instance_id"]
        try:
            master_sel = master_inst["spawn_diagnostics"]["profile_selection"]
            assert master_sel["profile_role"] == "master", master_sel

            # Master is busy now → a second spawn must clone from the snapshot.
            clone_inst = await spawn(headless=True, **_sandbox_kwargs())
            clone_iid = clone_inst["instance_id"]
            sel = clone_inst["spawn_diagnostics"]["profile_selection"]
            assert sel["profile_role"] == "clone", sel
            clone_dir = Path(sel["user_data_dir"])
            assert clone_dir.exists()
            assert str(dirs["sessions"]) in str(clone_dir)

            # Closing the clone must delete its directory. Force deferred
            # finalization to absorb any brief Windows file-lock after exit.
            await close(instance_id=clone_iid)
            for _ in range(40):
                if not clone_dir.exists():
                    break
                _pc.cleanup_deferred_profiles()
                await asyncio.sleep(0.25)
            assert not clone_dir.exists(), f"auto-clone not cleaned: {clone_dir}"

            # Master directory is never auto-deleted, even while in use.
            assert dirs["master"].exists()
        finally:
            await close(instance_id=master_iid)

        # Master survives its own close too (only the snapshot is refreshed).
        assert dirs["master"].exists()

    @pytest.mark.asyncio
    async def test_named_profile_survives_close(self, tmp_session_root):
        spawn = _get_fn("spawn_browser")
        close = _get_fn("close_instance")

        inst = await spawn(
            headless=True, user_data_dir="keep-me-session", **_sandbox_kwargs()
        )
        iid = inst["instance_id"]
        sel = inst["spawn_diagnostics"]["profile_selection"]
        named_dir = Path(sel["user_data_dir"])
        assert sel["profile_role"] == "explicit", sel
        assert named_dir.exists()
        # The AI-facing nudge must be surfaced when a named profile is created.
        assert "warning" in sel

        await close(instance_id=iid)
        await asyncio.sleep(1.0)
        assert named_dir.exists(), f"named profile wrongly deleted: {named_dir}"
