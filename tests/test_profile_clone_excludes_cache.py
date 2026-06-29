"""Profile clone must exclude regenerable cache dirs (huge, worthless to copy).

A real Chrome profile is ~98% cache (Cache, Code Cache, Service Worker,
optimization-guide ML model stores, Safe Browsing). Cloning those makes
spawn_browser take ~40s on a 5.4 GB profile. Only ~88 MB of session state
(cookies, logins, Web Data, Local Storage, Preferences) actually matters.

These tests pin the behavior: cache dirs are skipped, session state is kept.
No browser required.
"""

from server import _profile_ignore_names, _copy_profile_delta


CACHE_DIRS_THAT_MUST_BE_SKIPPED = [
    "Cache",
    "Code Cache",
    "Service Worker",
    "optimization_guide_model_store",
    "OptGuideOnDeviceModel",
    "OptGuideOnDeviceClassifierModel",
    "Safe Browsing",
]

SESSION_STATE_THAT_MUST_BE_KEPT = [
    "Default",
    "Cookies",
    "Login Data",
    "Web Data",
    "Local Storage",
    "Preferences",
    "Network",
]


class TestIgnoreNamesExcludesCache:
    def test_heavy_cache_dirs_are_ignored(self):
        ignored = _profile_ignore_names("/fake", CACHE_DIRS_THAT_MUST_BE_SKIPPED)
        for name in CACHE_DIRS_THAT_MUST_BE_SKIPPED:
            assert name in ignored, f"cache dir {name!r} should be excluded from clone"

    def test_session_state_names_are_kept(self):
        ignored = _profile_ignore_names("/fake", SESSION_STATE_THAT_MUST_BE_KEPT)
        assert ignored == set(), f"session state must never be excluded, got {ignored}"


class TestCloneSkipsCacheKeepsState:
    def test_copy_skips_cache_trees_but_keeps_session_state(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"

        # ── heavy, regenerable caches that must NOT be cloned ──
        for rel in (
            "Default/Cache/Cache_Data/data_0",
            "Default/Code Cache/js/blob",
            "Default/Service Worker/CacheStorage/big",
            "optimization_guide_model_store/model.bin",
            "Default/Safe Browsing/store",
        ):
            f = src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x" * 4096)

        # ── essential session state that MUST be cloned ──
        for rel in (
            "Default/Cookies",
            "Default/Network/Cookies",
            "Default/Login Data",
            "Default/Web Data",
            "Default/Preferences",
        ):
            f = src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"keep")

        dst.mkdir()
        _copy_profile_delta(src, dst)

        # caches excluded
        assert not (dst / "Default" / "Cache").exists()
        assert not (dst / "Default" / "Code Cache").exists()
        assert not (dst / "Default" / "Service Worker").exists()
        assert not (dst / "optimization_guide_model_store").exists()
        assert not (dst / "Default" / "Safe Browsing").exists()

        # session state preserved
        assert (dst / "Default" / "Cookies").read_bytes() == b"keep"
        assert (dst / "Default" / "Network" / "Cookies").read_bytes() == b"keep"
        assert (dst / "Default" / "Login Data").exists()
        assert (dst / "Default" / "Web Data").exists()
        assert (dst / "Default" / "Preferences").exists()
