"""A clone whose marker predates the ``auto_clean`` flag must never be deleted by
the storage-cap sweep.

Real regression: user-named business profiles (``discord-lead-gen``,
``accounting-internal-devino``, ``superbooks``, ...) were created by an older
build whose clone marker recorded only ``source_kind`` — no ``auto_clean`` flag.
``_clone_is_auto`` then fell back to ``not source_kind.startswith("explicit")``,
and because those profiles were cloned from a plain ``master-snapshot`` (not an
``explicit-*`` source), the heuristic judged them *disposable*. Over the storage
cap, the sweep would then permanently delete a 5 GiB logged-in business session —
a silent, unrecoverable loss — the moment its browser wasn't running.

The fix is fail-safe classification: absent an explicit ``auto_clean`` flag, a
clone is NEVER treated as disposable. Wrongly keeping a legacy clone costs
bounded disk; wrongly deleting a legacy named profile is permanent data loss. The
two are not symmetric, so ambiguity must resolve to "keep".

Modern markers (which always carry ``auto_clean``) are unaffected — proven here
so the fix reclaims genuine auto-clones exactly as before.

Pure filesystem tests: no browser.
"""

import json
import os

import pytest

from stealth_chrome_devtools_mcp.embedded import server
from stealth_chrome_devtools_mcp.embedded.server import (
    _clone_is_auto,
    _enforce_clone_storage_cap_in,
)

MARKER = ".stealth_chrome_devtools_mcp_clone.json"


@pytest.fixture(autouse=True)
def _isolate_protected_registry():
    """The protected-dir registry is module-global; keep it hermetic per test."""
    server._clear_protected_clone_dirs()
    yield
    server._clear_protected_clone_dirs()


def _write_marker(clone_dir, data):
    clone_dir.mkdir(parents=True, exist_ok=True)
    (clone_dir / MARKER).write_text(json.dumps(data), encoding="utf-8")


def _make_legacy_clone(root, name, *, size_bytes, source_kind="master-snapshot"):
    """A clone as an OLD build wrote it: source_kind only, no ``auto_clean`` key."""
    d = root / name
    _write_marker(
        d,
        {
            "source": "master",
            "source_kind": source_kind,
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    (d / "data.bin").write_bytes(b"x" * size_bytes)
    return d


def _make_modern_clone(
    root, name, *, size_bytes, auto_clean, source_kind="master-snapshot"
):
    """A clone as the CURRENT build writes it: explicit ``auto_clean`` flag."""
    d = root / name
    _write_marker(
        d,
        {
            "source": "master",
            "source_kind": source_kind,
            "auto_clean": auto_clean,
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    (d / "data.bin").write_bytes(b"x" * size_bytes)
    return d


def _set_mtime(path, when):
    os.utime(path, (when, when))


class TestLegacyMarkerClassification:
    def test_legacy_master_snapshot_marker_is_not_auto(self, tmp_path):
        # The exact real-world shape: a named business profile cloned from a plain
        # master-snapshot by a build that never wrote auto_clean.
        d = _make_legacy_clone(
            tmp_path, "discord-lead-gen-f5d195b036f2", size_bytes=100
        )
        assert _clone_is_auto(d) is False, (
            "a legacy marker without an explicit auto_clean flag must never be "
            "classified as a disposable auto-clone"
        )

    def test_legacy_explicit_source_marker_is_not_auto(self, tmp_path):
        # Legacy explicit profiles were already safe; keep them safe.
        d = _make_legacy_clone(
            tmp_path,
            "github-session",
            size_bytes=100,
            source_kind="explicit-master-snapshot",
        )
        assert _clone_is_auto(d) is False

    def test_modern_auto_clean_true_is_auto(self, tmp_path):
        # The fix must NOT neuter cleanup of genuine auto-clones.
        d = _make_modern_clone(
            tmp_path, "stealth-auto", size_bytes=100, auto_clean=True
        )
        assert _clone_is_auto(d) is True

    def test_modern_auto_clean_false_is_not_auto(self, tmp_path):
        d = _make_modern_clone(tmp_path, "named", size_bytes=100, auto_clean=False)
        assert _clone_is_auto(d) is False


class TestSweepNeverDeletesLegacyNamedProfile:
    def test_legacy_named_profile_survives_sweep_even_when_oldest_and_over_cap(
        self, tmp_path, monkeypatch
    ):
        # A legacy business profile: oldest (first eviction candidate by ordering),
        # not running, not protected, and the clone root is well over cap.
        business = _make_legacy_clone(
            tmp_path, "superbooks-7cf1e3db6477", size_bytes=8000
        )
        auto = _make_modern_clone(
            tmp_path, "stealth-auto", size_bytes=8000, auto_clean=True
        )
        _set_mtime(business, 1_000)  # oldest -> would be evicted first if it were auto
        _set_mtime(auto, 2_000)

        # No running browser and nothing protected: only classification stands
        # between the business profile and deletion.
        monkeypatch.setattr(server, "_profile_has_running_browser", lambda p: False)

        removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=6000)

        assert business.exists(), (
            "a legacy named business profile must never be deleted by the cap sweep"
        )
        assert not auto.exists(), "the genuine auto-clone should be reclaimed instead"
        assert removed == 1

    def test_sweep_still_reclaims_modern_auto_clone_over_cap(
        self, tmp_path, monkeypatch
    ):
        # Guard: the fix leaves real auto-clone reclamation intact.
        auto = _make_modern_clone(
            tmp_path, "stealth-auto", size_bytes=8000, auto_clean=True
        )
        _set_mtime(auto, 1_000)
        monkeypatch.setattr(server, "_profile_has_running_browser", lambda p: False)

        removed = _enforce_clone_storage_cap_in(tmp_path, cap_bytes=1000)

        assert not auto.exists()
        assert removed == 1
