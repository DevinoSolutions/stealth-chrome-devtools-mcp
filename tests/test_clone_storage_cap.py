"""Auto-clone storage must be bounded — leaked clones can never grow without limit.

Background: a clone is only made when the master profile is busy (a second
concurrent browser). Those clones live under the clone root and are deleted on
close. But when an on-close delete fails (Windows holding Cache files) or the
tracking is lost (the singleton backend restarts), the clone is orphaned — and
nothing swept the clone root and no size cap existed, so leaks accumulated to
146 GB. The cache-exclusion fix shrank each clone ~60x but left growth unbounded.

These tests pin a size-capped sweep of the clone root that:
  - reclaims the OLDEST idle auto-clones until total storage is back under a cap,
  - NEVER deletes user-named/explicit profiles (they persist by design),
  - NEVER deletes a clone a live browser is currently using,
  - NEVER deletes unmarked directories it did not create.

Pure filesystem tests: no browser.
"""

import json
import os

import server
from server import _clone_is_auto, _enforce_clone_storage_cap_in


MARKER = ".stealth_chrome_devtools_mcp_clone.json"


def _make_clone(
    root, name, *, size_bytes, source_kind="master-snapshot", auto_clean=True
):
    # Defaults to a genuine auto-clone: production always writes an explicit
    # auto_clean flag, and that flag — not the source_kind — is what marks a clone
    # disposable. Named/explicit profiles pass auto_clean=False; a legacy no-flag
    # marker is exercised in test_clone_legacy_marker_classification.py.
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "data.bin").write_bytes(b"x" * size_bytes)
    marker = {
        "source": "master",
        "source_kind": source_kind,
        "created_at": "2026-01-01T00:00:00Z",
    }
    if auto_clean is not None:
        marker["auto_clean"] = auto_clean
    (d / MARKER).write_text(json.dumps(marker), encoding="utf-8")
    return d


def _set_mtime(path, when):
    os.utime(path, (when, when))


class TestClassification:
    def test_master_clone_is_auto(self, tmp_path):
        d = _make_clone(
            tmp_path, "sess-a", size_bytes=10, source_kind="master-snapshot"
        )
        assert _clone_is_auto(d) is True

    def test_live_master_fallback_clone_is_auto(self, tmp_path):
        d = _make_clone(
            tmp_path, "sess-b", size_bytes=10, source_kind="live-master-fallback"
        )
        assert _clone_is_auto(d) is True

    def test_explicit_named_profile_is_not_auto(self, tmp_path):
        d = _make_clone(
            tmp_path,
            "github",
            size_bytes=10,
            source_kind="explicit-master",
            auto_clean=False,
        )
        assert _clone_is_auto(d) is False

    def test_unmarked_dir_is_not_auto(self, tmp_path):
        d = tmp_path / "mystery"
        d.mkdir()
        assert _clone_is_auto(d) is False

    def test_explicit_auto_clean_flag_overrides_source_kind(self, tmp_path):
        # A future explicit marker pinned auto_clean=False must win even if the
        # source_kind heuristic would have called it an auto-clone.
        d = _make_clone(
            tmp_path,
            "weird",
            size_bytes=10,
            source_kind="master-snapshot",
            auto_clean=False,
        )
        assert _clone_is_auto(d) is False


class TestCapSweep:
    def test_under_cap_is_noop(self, tmp_path):
        _make_clone(tmp_path, "a", size_bytes=1000)
        removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=10_000)
        assert removed == 0
        assert (tmp_path / "a").exists()

    def test_cap_zero_disables_sweep(self, tmp_path):
        _make_clone(tmp_path, "a", size_bytes=10_000)
        removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=0)
        assert removed == 0
        assert (tmp_path / "a").exists()

    def test_oldest_idle_autoclones_removed_until_under_cap(self, tmp_path):
        old = _make_clone(tmp_path, "old", size_bytes=4096)
        mid = _make_clone(tmp_path, "mid", size_bytes=4096)
        new = _make_clone(tmp_path, "new", size_bytes=4096)
        _set_mtime(old, 1_000)
        _set_mtime(mid, 2_000)
        _set_mtime(new, 3_000)

        # ~12.3 KB total, cap 6 KB -> the two oldest must go, newest survives.
        removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=6000)

        assert removed == 2
        assert not old.exists()
        assert not mid.exists()
        assert new.exists()

    def test_named_profiles_never_deleted_even_when_oldest_and_huge(self, tmp_path):
        named = _make_clone(
            tmp_path,
            "github",
            size_bytes=20_000,
            source_kind="explicit-master",
            auto_clean=False,
        )
        _set_mtime(named, 1)
        removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=1000)
        assert removed == 0
        assert named.exists()

    def test_running_clone_never_deleted(self, tmp_path, monkeypatch):
        old_running = _make_clone(tmp_path, "old-running", size_bytes=8000)
        newer_idle = _make_clone(tmp_path, "newer-idle", size_bytes=8000)
        _set_mtime(old_running, 1_000)
        _set_mtime(newer_idle, 2_000)

        monkeypatch.setattr(
            server,
            "_profile_has_running_browser",
            lambda p: str(p).endswith("old-running"),
        )

        # ~16 KB total, cap 6 KB. Oldest is the live one (protected), so the
        # idle one is reclaimed and the sweep stops without killing the session.
        removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=6000)
        assert removed == 1
        assert old_running.exists()
        assert not newer_idle.exists()

    def test_unmarked_dirs_never_deleted(self, tmp_path):
        mystery = tmp_path / "not-ours"
        mystery.mkdir()
        (mystery / "big.bin").write_bytes(b"x" * 50_000)
        removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=1000)
        assert removed == 0
        assert mystery.exists()
