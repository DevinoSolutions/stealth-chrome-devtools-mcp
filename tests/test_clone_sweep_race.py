"""The storage-cap sweep must never evict a clone the server is actively using —
including one that is *in flight*: its on-disk marker is already written (so the
sweep classifies it as a reclaimable auto-clone) but its Chrome has not attached
yet, so the filesystem/PID liveness heuristic (`_profile_has_running_browser`)
still reports it as *not running*.

That window is real. `_copy_profile_tree` writes the ``auto_clean`` marker at the
END of the copy — before the browser is launched and before the process is
tracked. A concurrent (or startup) sweep firing in that window, under an
over-cap condition, deletes the live-but-not-yet-attached clone out from under
the spawning browser: a silent mid-session session loss with nothing logged.

The authoritative "this dir belongs to an instance we own" knowledge lives in
the server's memory, not on disk, so the sweep must consult a protected-dir
registry — not only the racy filesystem heuristic.

Pure filesystem tests: no browser.
"""

import json
import os

import pytest

from stealth_chrome_devtools_mcp.embedded import server
from stealth_chrome_devtools_mcp.embedded.server import _enforce_clone_storage_cap_in

MARKER = ".stealth_chrome_devtools_mcp_clone.json"


@pytest.fixture(autouse=True)
def _isolate_protected_registry():
    """The protected-dir registry is module-global; keep it hermetic per test."""
    server._clear_protected_clone_dirs()
    yield
    server._clear_protected_clone_dirs()


def _make_clone(root, name, *, size_bytes, source_kind="master-snapshot"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "data.bin").write_bytes(b"x" * size_bytes)
    (d / MARKER).write_text(
        json.dumps(
            {
                "source": "master",
                "source_kind": source_kind,
                # A genuine auto-clone as production writes it: the explicit
                # auto_clean flag is what makes it a sweep target at all.
                "auto_clean": True,
                "created_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return d


def _set_mtime(path, when):
    os.utime(path, (when, when))


class TestProtectedInFlightClone:
    def test_protected_clone_survives_even_when_oldest_and_not_running(
        self, tmp_path, monkeypatch
    ):
        # Oldest by mtime => the sweep's first eviction victim by ordering.
        protected = _make_clone(tmp_path, "in-flight", size_bytes=8000)
        idle = _make_clone(tmp_path, "idle", size_bytes=8000)
        _set_mtime(protected, 1_000)
        _set_mtime(idle, 2_000)

        # In-flight: marker on disk, but Chrome has not attached yet, so the
        # filesystem heuristic (correctly) reports "not running".
        monkeypatch.setattr(server, "_profile_has_running_browser", lambda p: False)

        server._protect_clone_dir(protected)
        try:
            # ~16 KB total, cap 6 KB. Oldest is the protected in-flight clone, so
            # the idle one must be reclaimed instead — the session survives.
            removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=6000)
        finally:
            server._release_clone_dir(protected)

        assert protected.exists(), "protected in-flight clone must not be evicted"
        assert not idle.exists(), "the idle clone should be reclaimed instead"
        assert removed == 1

    def test_released_clone_becomes_evictable_again(self, tmp_path, monkeypatch):
        d = _make_clone(tmp_path, "sess", size_bytes=8000)
        _set_mtime(d, 1_000)
        monkeypatch.setattr(server, "_profile_has_running_browser", lambda p: False)

        server._protect_clone_dir(d)
        server._release_clone_dir(d)

        # Once released (instance closed), the clone is a normal reclaim target.
        removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=1000)
        assert removed == 1
        assert not d.exists()

    def test_protection_is_path_normalized(self, tmp_path, monkeypatch):
        # Protecting via one spelling must shield the same dir addressed by an
        # equivalent spelling (trailing separator; case on Windows). The sweep
        # sees `clone_root.iterdir()` spellings, which may differ from the path
        # the spawn flow registered.
        d = _make_clone(tmp_path, "norm", size_bytes=8000)
        _set_mtime(d, 1_000)
        monkeypatch.setattr(server, "_profile_has_running_browser", lambda p: False)

        server._protect_clone_dir(str(d) + os.sep)  # trailing-separator variant
        try:
            removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=1000)
        finally:
            server._release_clone_dir(str(d) + os.sep)

        assert d.exists(), "normalized-path protection must shield the same dir"
        assert removed == 0


class TestSpawnFlowProtectsClone:
    """The production spawn path must register the clone as protected BEFORE its
    marker is written — otherwise the race window is still open regardless of the
    registry mechanics above."""

    @pytest.mark.asyncio
    async def test_resolve_profile_selection_protects_the_clone(self, tmp_session_root):
        # Force the clone branch by making the master profile look busy.
        (tmp_session_root["master"] / "SingletonLock").write_text("lock")

        result = await server._resolve_profile_selection(None)

        assert result["profile_role"] == "clone"
        assert server._clone_dir_is_protected(result["user_data_dir"]), (
            "spawn flow must protect the clone dir before its marker is written"
        )
