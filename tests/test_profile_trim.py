"""Idle named profiles must be trimmable down to their real session state.

Evidence from a real "persistent" profile: 5.3 GB total, of which a single
on-device AI model (OptGuideOnDeviceModel) was 4 GB and Cache/Code Cache/Service
Worker most of the rest — while the logins that make it worth keeping (Cookies,
Login Data, Web Data, Preferences, Local Storage) were ~40 MB. So when sessions/
storage exceeds the cap, the largest idle NAMED profiles are trimmed of their
regenerable dirs — using the SAME name set the clone path excludes — and every
login is preserved. Auto-clones, unmarked dirs, and in-use profiles are untouched.

Pure filesystem tests: no browser.
"""

import json

from stealth_chrome_devtools_mcp.embedded import clone_storage
from stealth_chrome_devtools_mcp.embedded.clone_storage import (
    _enforce_named_profile_trim_in,
    _trim_profile_regenerable,
)
from stealth_chrome_devtools_mcp.embedded.clone_storage import (
    clone_is_named as _clone_is_named,
)

MARKER = ".stealth_chrome_devtools_mcp_clone.json"


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _named_profile(root, name, *, model_mb=0):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / MARKER).write_text(
        json.dumps({"source_kind": "explicit-master", "auto_clean": False}),
        encoding="utf-8",
    )
    # regenerable — profile-root model store + Default/ caches
    if model_mb:
        _write(
            d / "OptGuideOnDeviceModel" / "model.bin", b"x" * (model_mb * 1024 * 1024)
        )
    _write(d / "Default" / "Cache" / "data_0", b"x" * 4096)
    _write(d / "Default" / "Code Cache" / "js" / "blob", b"x" * 4096)
    _write(d / "Default" / "Service Worker" / "CacheStorage" / "big", b"x" * 4096)
    # session state — MUST survive a trim
    _write(d / "Default" / "Cookies", b"COOKIES")
    _write(d / "Default" / "Login Data", b"LOGINS")
    _write(d / "Default" / "Local Storage" / "leveldb" / "000003.log", b"LOCALSTATE")
    _write(d / "Default" / "Preferences", b"PREFS")
    return d


def _auto_clone(root, name, *, mb=1):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / MARKER).write_text(
        json.dumps({"source_kind": "master-snapshot", "auto_clean": True}),
        encoding="utf-8",
    )
    _write(d / "Default" / "Cache" / "data", b"x" * (mb * 1024 * 1024))
    return d


class TestClassifyNamed:
    def test_named_marker_is_named(self, tmp_path):
        assert _clone_is_named(_named_profile(tmp_path, "github")) is True

    def test_auto_clone_is_not_named(self, tmp_path):
        assert _clone_is_named(_auto_clone(tmp_path, "sess")) is False

    def test_unmarked_is_not_named(self, tmp_path):
        d = tmp_path / "mystery"
        d.mkdir()
        assert _clone_is_named(d) is False


class TestTrimProfile:
    def test_trims_regenerable_keeps_all_session_state(self, tmp_path):
        d = _named_profile(tmp_path, "github", model_mb=8)

        freed = _trim_profile_regenerable(d)

        # regenerable gone (root model store + Default caches)
        assert not (d / "OptGuideOnDeviceModel").exists()
        assert not (d / "Default" / "Cache").exists()
        assert not (d / "Default" / "Code Cache").exists()
        assert not (d / "Default" / "Service Worker").exists()
        # session state preserved exactly
        assert (d / "Default" / "Cookies").read_bytes() == b"COOKIES"
        assert (d / "Default" / "Login Data").read_bytes() == b"LOGINS"
        assert (
            d / "Default" / "Local Storage" / "leveldb" / "000003.log"
        ).read_bytes() == b"LOCALSTATE"
        assert (d / "Default" / "Preferences").read_bytes() == b"PREFS"
        assert freed > 8 * 1024 * 1024  # at least the model


class TestNamedProfileTrimCap:
    def test_under_cap_is_noop(self, tmp_path):
        _named_profile(tmp_path, "a", model_mb=1)
        assert _enforce_named_profile_trim_in(tmp_path, cap_bytes=10**9) == 0
        assert (tmp_path / "a" / "OptGuideOnDeviceModel").exists()

    def test_cap_zero_disables(self, tmp_path):
        _named_profile(tmp_path, "a", model_mb=20)
        assert _enforce_named_profile_trim_in(tmp_path, cap_bytes=0) == 0
        assert (tmp_path / "a" / "OptGuideOnDeviceModel").exists()

    def test_trims_largest_until_under_cap(self, tmp_path):
        big = _named_profile(tmp_path, "big", model_mb=20)
        small = _named_profile(tmp_path, "small", model_mb=2)

        # ~22 MB total, cap 12 MB → trimming the largest (big) drops under it;
        # the small one is left fully intact (model still present).
        freed = _enforce_named_profile_trim_in(tmp_path, cap_bytes=12 * 1024 * 1024)

        assert freed > 0
        assert not (big / "OptGuideOnDeviceModel").exists()
        assert (big / "Default" / "Cookies").read_bytes() == b"COOKIES"  # logins kept
        assert (small / "OptGuideOnDeviceModel").exists()  # untouched

    def test_never_trims_auto_clones(self, tmp_path):
        a = _auto_clone(tmp_path, "sess-auto", mb=50)
        assert _enforce_named_profile_trim_in(tmp_path, cap_bytes=1024 * 1024) == 0
        assert (a / "Default" / "Cache").exists()  # left for the clone-cap sweep

    def test_never_trims_unmarked(self, tmp_path):
        d = tmp_path / "not-ours"
        _write(d / "OptGuideOnDeviceModel" / "model.bin", b"x" * (50 * 1024 * 1024))
        assert _enforce_named_profile_trim_in(tmp_path, cap_bytes=1024 * 1024) == 0
        assert (d / "OptGuideOnDeviceModel").exists()

    def test_never_trims_running_profile(self, tmp_path, monkeypatch):
        d = _named_profile(tmp_path, "github", model_mb=50)
        monkeypatch.setattr(
            clone_storage, "_profile_has_running_browser", lambda p: True
        )
        assert _enforce_named_profile_trim_in(tmp_path, cap_bytes=1024) == 0
        assert (d / "OptGuideOnDeviceModel").exists()
