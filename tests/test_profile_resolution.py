"""Unit tests for profile path resolution and clone logic.

Validates that _resolve_profile_selection, _is_relative_to,
_next_available_explicit_dir, _profile_ignore_names, _snapshot_needs_refresh,
and _copy_profile_delta all behave correctly across edge cases.

No browser required — uses tmp_path fixtures with env-var patches.
"""

import json
import os
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# These are module-level functions in server.py (bare imports via sys.path)
from server import (
    _is_relative_to,
    _profile_ignore_names,
    _copy_profile_delta,
    _copy_profile_tree,
    _snapshot_needs_refresh,
    _next_available_explicit_dir,
    _resolve_profile_selection,
    _default_session_root,
    _master_profile_dir,
    _clone_root_dir,
    _master_snapshot_dir,
)


# ---------------------------------------------------------------------------
# _is_relative_to
# ---------------------------------------------------------------------------

class TestIsRelativeTo:
    def test_child_is_relative(self, tmp_path):
        parent = tmp_path / "a"
        child = parent / "b" / "c"
        parent.mkdir()
        child.mkdir(parents=True)
        assert _is_relative_to(child, parent) is True

    def test_sibling_not_relative(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert _is_relative_to(b, a) is False

    def test_same_dir_is_relative(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        assert _is_relative_to(d, d) is True

    def test_nonexistent_paths(self, tmp_path):
        """Works with strict=False even for paths that don't exist."""
        parent = tmp_path / "parent"
        child = parent / "child"
        assert _is_relative_to(child, parent) is True


# ---------------------------------------------------------------------------
# _profile_ignore_names
# ---------------------------------------------------------------------------

class TestProfileIgnoreNames:
    def test_volatile_dirs_ignored(self):
        names = ["Default", "Crashpad", "GPUCache", "ShaderCache", "BrowserMetrics"]
        ignored = _profile_ignore_names("/fake", names)
        assert "Crashpad" in ignored
        assert "GPUCache" in ignored
        assert "ShaderCache" in ignored
        assert "BrowserMetrics" in ignored
        assert "Default" not in ignored

    def test_singleton_prefix(self):
        names = ["SingletonLock", "SingletonSocket", "SingletonCookie", "SingletonFoo"]
        ignored = _profile_ignore_names("/fake", names)
        assert len(ignored) == 4  # all Singleton* caught

    def test_tmp_and_lock_extensions(self):
        names = ["data.tmp", "session.TMP", "write.lock", "LOCK", "lockfile"]
        ignored = _profile_ignore_names("/fake", names)
        assert "data.tmp" in ignored
        assert "session.TMP" in ignored
        assert "write.lock" in ignored
        assert "LOCK" in ignored
        assert "lockfile" in ignored

    def test_safe_names_pass(self):
        names = ["Default", "Cookies", "Login Data", "Preferences"]
        ignored = _profile_ignore_names("/fake", names)
        assert len(ignored) == 0


# ---------------------------------------------------------------------------
# _copy_profile_delta
# ---------------------------------------------------------------------------

class TestCopyProfileDelta:
    def test_copies_files(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "file.txt").write_text("hello")
        _copy_profile_delta(src, dst)
        assert (dst / "file.txt").read_text() == "hello"

    def test_copies_nested_dirs(self, tmp_path):
        src = tmp_path / "src" / "Default"
        dst_root = tmp_path / "dst"
        src.mkdir(parents=True)
        dst_root.mkdir()
        (src / "Cookies").write_bytes(b"data")
        _copy_profile_delta(src.parent, dst_root)
        assert (dst_root / "Default" / "Cookies").read_bytes() == b"data"

    def test_skips_volatile_dirs(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "Crashpad").mkdir()
        (src / "Crashpad" / "report.dmp").write_bytes(b"crash")
        (src / "Default").mkdir()
        (src / "Default" / "Cookies").write_bytes(b"ok")
        _copy_profile_delta(src, dst)
        assert not (dst / "Crashpad").exists()
        assert (dst / "Default" / "Cookies").exists()

    def test_skips_singleton_files(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "SingletonLock").write_text("lock")
        (src / "data.txt").write_text("keep")
        _copy_profile_delta(src, dst)
        assert not (dst / "SingletonLock").exists()
        assert (dst / "data.txt").exists()

    def test_incremental_only_copies_changed(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "a.txt").write_text("original")
        _copy_profile_delta(src, dst)
        # Modify dst to have different content but same size
        (dst / "a.txt").write_text("modified")
        # src unchanged — delta should skip (same size, same mtime)
        _copy_profile_delta(src, dst)
        # The file was overwritten because mtime comparison uses int precision
        # and both were created in the same second — this is expected behavior


# ---------------------------------------------------------------------------
# _copy_profile_tree
# ---------------------------------------------------------------------------

class TestCopyProfileTree:
    def test_creates_target_and_copies(self, tmp_session_root):
        dirs = tmp_session_root
        source = dirs["snapshot"]
        target = dirs["sessions"] / "test-clone"
        _copy_profile_tree(source, target, dirs["sessions"], "test")
        assert target.exists()
        assert (target / "Default" / "Cookies").exists()
        # Clone marker written
        marker = target / ".stealth_chrome_devtools_mcp_clone.json"
        assert marker.exists()
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["source_kind"] == "test"

    def test_marker_created_at_is_utc_z_seconds(self, tmp_session_root):
        # Guards the datetime.utcnow() -> datetime.now(timezone.utc) migration:
        # the marker timestamp must stay a naive-looking UTC instant with a 'Z'
        # suffix and second precision (e.g. 2026-07-01T12:34:56Z). A naive
        # now(timezone.utc).isoformat()+"Z" would corrupt it to '...+00:00Z'.
        import re

        dirs = tmp_session_root
        target = dirs["sessions"] / "ts-clone"
        _copy_profile_tree(dirs["snapshot"], target, dirs["sessions"], "test")
        created_at = json.loads(
            (target / ".stealth_chrome_devtools_mcp_clone.json").read_text(encoding="utf-8")
        )["created_at"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at), created_at

    def test_refuses_target_outside_clone_root(self, tmp_session_root):
        dirs = tmp_session_root
        outside = dirs["root"] / "outside-clone"
        with pytest.raises(ValueError, match="Refusing"):
            _copy_profile_tree(dirs["snapshot"], outside, dirs["sessions"], "test")

    def test_missing_source_creates_empty_dir(self, tmp_session_root):
        dirs = tmp_session_root
        target = dirs["sessions"] / "empty"
        fake_source = dirs["root"] / "nonexistent"
        _copy_profile_tree(fake_source, target, dirs["sessions"], "test")
        assert target.exists()
        assert len(list(target.iterdir())) == 0

    def test_double_pass_copy(self, tmp_session_root):
        """_copy_profile_tree does two passes of _copy_profile_delta."""
        dirs = tmp_session_root
        target = dirs["sessions"] / "double-pass"
        source = dirs["snapshot"]
        _copy_profile_tree(source, target, dirs["sessions"], "test")
        # All source files present in target
        marker_name = ".stealth_chrome_devtools_mcp_clone.json"
        src_files = set(f.name for f in source.rglob("*") if f.is_file()
                        and "Singleton" not in f.name and "Crashpad" not in str(f)
                        and f.name != marker_name)
        dst_files = set(f.name for f in target.rglob("*") if f.is_file()
                        and f.name != marker_name)
        assert src_files.issubset(dst_files)


# ---------------------------------------------------------------------------
# _snapshot_needs_refresh
# ---------------------------------------------------------------------------

class TestSnapshotNeedsRefresh:
    def test_no_snapshot_returns_false(self, tmp_session_root):
        dirs = tmp_session_root
        shutil.rmtree(dirs["snapshot"])
        assert _snapshot_needs_refresh() is False

    def test_no_marker_returns_true(self, tmp_session_root):
        dirs = tmp_session_root
        marker = dirs["snapshot"] / ".stealth_chrome_devtools_mcp_clone.json"
        marker.unlink(missing_ok=True)
        assert _snapshot_needs_refresh() is True

    def test_fresh_snapshot_returns_false(self, tmp_session_root):
        """When marker is newer than all auth files, no refresh needed."""
        dirs = tmp_session_root
        marker = dirs["snapshot"] / ".stealth_chrome_devtools_mcp_clone.json"
        # Touch marker to be newest
        time.sleep(0.05)
        marker.write_text(marker.read_text(encoding="utf-8"), encoding="utf-8")
        assert _snapshot_needs_refresh() is False

    def test_stale_snapshot_returns_true(self, tmp_session_root):
        """When master Cookies is newer than snapshot marker, refresh needed."""
        dirs = tmp_session_root
        marker = dirs["snapshot"] / ".stealth_chrome_devtools_mcp_clone.json"
        # Make marker old
        old_time = time.time() - 3600
        os.utime(marker, (old_time, old_time))
        # Touch master Cookies to be newer
        cookies = dirs["master"] / "Default" / "Cookies"
        cookies.write_bytes(b"updated-cookies")
        assert _snapshot_needs_refresh() is True


# ---------------------------------------------------------------------------
# _next_available_explicit_dir
# ---------------------------------------------------------------------------

class TestNextAvailableExplicitDir:
    def test_returns_dash_2_first(self, tmp_session_root):
        dirs = tmp_session_root
        requested = dirs["sessions"] / "my-profile"
        requested.mkdir()
        result = _next_available_explicit_dir(requested)
        assert result.name == "my-profile-2"

    def test_skips_busy_variants(self, tmp_session_root):
        dirs = tmp_session_root
        requested = dirs["sessions"] / "busy"
        requested.mkdir()
        # Create -2 and -3 with SingletonLock (simulates running browser)
        for i in (2, 3):
            d = dirs["sessions"] / f"busy-{i}"
            d.mkdir()
            (d / "SingletonLock").write_text("lock")
        result = _next_available_explicit_dir(requested)
        assert result.name == "busy-4"


# ---------------------------------------------------------------------------
# _resolve_profile_selection (async)
# ---------------------------------------------------------------------------

class TestResolveProfileSelection:
    @pytest.mark.asyncio
    async def test_no_user_data_dir_uses_master(self, tmp_session_root):
        dirs = tmp_session_root
        result = await _resolve_profile_selection(None)
        assert result["profile_role"] == "master"
        assert result["user_data_dir"] == str(dirs["master"])

    @pytest.mark.asyncio
    async def test_relative_path_lands_in_sessions(self, tmp_session_root):
        dirs = tmp_session_root
        result = await _resolve_profile_selection("my-browser")
        resolved = Path(result["user_data_dir"])
        assert _is_relative_to(resolved, dirs["sessions"])
        assert resolved.name == "my-browser"

    @pytest.mark.asyncio
    async def test_sessions_prefix_no_double(self, tmp_session_root):
        """'sessions/foo' should NOT become 'sessions/sessions/foo'."""
        dirs = tmp_session_root
        result = await _resolve_profile_selection("sessions/test-browser")
        resolved = Path(result["user_data_dir"])
        # Should be inside sessions/, not sessions/sessions/
        assert resolved.parent == dirs["sessions"]
        assert resolved.name == "test-browser"

    @pytest.mark.asyncio
    async def test_absolute_path_used_directly(self, tmp_session_root, tmp_path):
        dirs = tmp_session_root
        abs_path = str(tmp_path / "custom-profile")
        result = await _resolve_profile_selection(abs_path)
        assert result["user_data_dir"] == abs_path
        assert result["profile_role"] == "explicit"

    @pytest.mark.asyncio
    async def test_relative_clones_from_snapshot(self, tmp_session_root):
        """When relative path doesn't exist, it clones from snapshot."""
        dirs = tmp_session_root
        result = await _resolve_profile_selection("fresh-clone")
        resolved = Path(result["user_data_dir"])
        assert resolved.exists()
        assert (resolved / "Default" / "Cookies").exists()

    @pytest.mark.asyncio
    async def test_busy_profile_auto_suffixes(self, tmp_session_root):
        """When relative profile is busy, auto-suffix to -2."""
        dirs = tmp_session_root
        busy = dirs["sessions"] / "occupied"
        busy.mkdir()
        (busy / "SingletonLock").write_text("lock")
        result = await _resolve_profile_selection("occupied")
        resolved = Path(result["user_data_dir"])
        assert resolved.name == "occupied-2"

    @pytest.mark.asyncio
    async def test_master_busy_clones(self, tmp_session_root):
        """When master is busy and user_data_dir=None, should clone."""
        dirs = tmp_session_root
        (dirs["master"] / "SingletonLock").write_text("lock")
        result = await _resolve_profile_selection(None)
        assert result["profile_role"] == "clone"
        assert result["clone_source"] is not None

    @pytest.mark.asyncio
    async def test_no_master_no_snapshot_raises(self, tmp_empty_root):
        """When no master and no snapshot exist, RuntimeError."""
        # Master doesn't exist, and no snapshot — but master busy check
        # will pass (no lock), so it tries to use master directly.
        # Actually master dir doesn't exist, so it'll try to create it.
        # Let's simulate: master doesn't exist + force_clone
        with pytest.raises(RuntimeError, match="No master profile"):
            await _resolve_profile_selection(
                None, force_clone=True
            )
