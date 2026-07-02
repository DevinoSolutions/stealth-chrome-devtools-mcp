"""Auto-clone eviction must be RECOVERABLE, not immediately destructive.

Background: the worst incident this project has had was the storage-cap sweep
*irreversibly* deleting a logged-in profile. Every selection guard added since
lowers the odds of a wrong eviction, but cannot make one recoverable. This suite
pins the defense-in-depth behavior: an evicted auto-clone is *renamed* into a
``.trash`` directory inside the clone root (instant, same volume) instead of
being ``rmtree``'d, and trash is purged only after a retention window. A wrong
eviction therefore becomes a recoverable event within that window instead of
data loss.

Invariants pinned here:
  - eviction moves the clone into ``.trash`` with its contents intact,
  - ``.trash`` is invisible to every clone-root scan (never re-counted as an
    auto-clone, never inflates the named-profile session-cap total),
  - trash older than the retention window is purged; fresher trash survives,
  - the sweep purges expired trash *before* evicting more (pressure ordering),
  - a running profile is never moved to trash.

Pure filesystem tests: no browser.
"""

import json
import os
import time

import server


MARKER = ".stealth_chrome_devtools_mcp_clone.json"


def _make_clone(root, name, *, size_bytes, source_kind="master-snapshot", auto_clean=True):
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


class TestEvictionIsRecoverable:
    def test_evicted_autoclone_is_moved_to_trash_not_deleted(self, tmp_path):
        old = _make_clone(tmp_path, "old", size_bytes=4096)
        new = _make_clone(tmp_path, "new", size_bytes=4096)
        _set_mtime(old, 1_000)
        _set_mtime(new, 2_000)

        # ~8 KB total, cap 6 KB -> the oldest is evicted.
        removed = server._enforce_clone_storage_cap_in(tmp_path, cap_bytes=6000)

        assert removed == 1
        assert not old.exists(), "evicted clone must leave its original path"
        assert new.exists()

        trashed = server._clone_trash_dir(tmp_path) / "old"
        assert trashed.exists(), "evicted clone must be recoverable from .trash"
        assert (trashed / "data.bin").read_bytes() == b"x" * 4096, "contents intact"

    def test_trash_clone_returns_new_location(self, tmp_path):
        clone = _make_clone(tmp_path, "sess", size_bytes=100)
        dest = server._trash_clone(clone, tmp_path)
        assert dest is not None
        assert dest.exists()
        assert not clone.exists()
        assert server._clone_trash_dir(tmp_path) in dest.parents

    def test_trash_clone_refuses_running_profile(self, tmp_path, monkeypatch):
        clone = _make_clone(tmp_path, "live", size_bytes=100)
        monkeypatch.setattr(server, "_profile_has_running_browser", lambda p: True)

        dest = server._trash_clone(clone, tmp_path)

        assert dest is None, "a running profile must never be moved to trash"
        assert clone.exists(), "running profile must stay exactly where it is"


class TestTrashIsInvisibleToScans:
    def test_trash_not_selected_as_autoclone(self, tmp_path):
        # A trashed auto-clone (marker still says auto) lives one level under
        # .trash; the selector must not descend into it or treat .trash as a clone.
        trash = server._clone_trash_dir(tmp_path)
        _make_clone(trash, "old-evicted", size_bytes=50_000)

        victims = server._idle_autoclones_over_cap(tmp_path, cap_bytes=1000)

        assert victims == [], ".trash contents must never be re-selected for eviction"

    def test_trash_size_excluded_from_named_profile_cap(self, tmp_path):
        # Named profile (4 KB) is UNDER the 6 KB session cap on its own; only the
        # 4 KB of trash pushes the raw total over. Trash must not count, so the
        # named profile must not be selected for trimming.
        named = _make_clone(tmp_path, "github", size_bytes=4000,
                            source_kind="explicit-master", auto_clean=False)
        _make_clone(server._clone_trash_dir(tmp_path), "old-evicted", size_bytes=4000)

        victims = server._named_profiles_over_session_cap(tmp_path, cap_bytes=6000)

        assert named not in victims
        assert victims == []


class TestPurge:
    def test_purge_removes_expired_keeps_fresh(self, tmp_path):
        trash = server._clone_trash_dir(tmp_path)
        old = _make_clone(trash, "ancient", size_bytes=100)
        fresh = _make_clone(trash, "recent", size_bytes=100)
        _set_mtime(old, 1_000)                 # epoch-ancient
        _set_mtime(fresh, time.time())         # just now

        purged = server._purge_expired_trash(tmp_path, max_age_seconds=3600)

        assert purged == 1
        assert not old.exists(), "trash older than retention must be purged"
        assert fresh.exists(), "trash within retention must survive"

    def test_purge_noop_when_no_trash(self, tmp_path):
        assert server._purge_expired_trash(tmp_path, max_age_seconds=3600) == 0


class TestSweepWiring:
    def test_sweep_purges_expired_trash_then_evicts(self, tmp_path, monkeypatch):
        # cleanup_deferred_profiles touches real global state; neutralize it so
        # this stays a hermetic filesystem test.
        monkeypatch.setattr(
            server.process_cleanup, "cleanup_deferred_profiles", lambda: None
        )

        ancient = _make_clone(server._clone_trash_dir(tmp_path), "ancient", size_bytes=100)
        _set_mtime(ancient, 1_000)  # far older than the default 24h retention

        old = _make_clone(tmp_path, "old", size_bytes=4096)
        new = _make_clone(tmp_path, "new", size_bytes=4096)
        _set_mtime(old, 1_000)
        _set_mtime(new, 2_000)

        server._run_storage_sweep(
            tmp_path, clone_cap=6000, session_cap=0, reason="test"
        )

        assert not ancient.exists(), "sweep must purge expired trash"
        assert not old.exists(), "sweep must evict the oldest over-cap auto-clone"
        assert (server._clone_trash_dir(tmp_path) / "old" / "data.bin").exists(), \
            "evicted clone must be recoverable from trash after the sweep"
        assert new.exists()
