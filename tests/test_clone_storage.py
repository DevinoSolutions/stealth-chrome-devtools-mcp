"""C1 extraction pins (plan_M4ph1 §2.B / §3-C1).

Pins the F-201 move of the profile / clone-storage subsystem out of ``server.py``
into ``clone_storage.py``:

* the module exists and exposes its public API (the subsystem's API surface drops
  the leading underscore; internal helpers keep theirs);
* ``server.py`` keeps NO re-export or alias of the moved names — one home only
  (the second-way lens); a caller reaches the helpers via ``clone_storage``;
* ``spawn_browser`` resolves its profile selection through
  ``clone_storage.resolve_profile_selection`` (the delegation seam the move
  creates);
* a light delegate-identity pin that the extracted helpers still run from their
  new home. The deep per-branch behavior pins live in ``test_profile_resolution``
  and ``test_clone_storage_cap`` (both repointed to ``clone_storage``; semantics
  unchanged by the move).
"""

from pathlib import Path
from types import SimpleNamespace

from fakes import FakeBrowserManager
from stealth_chrome_devtools_mcp.embedded import clone_storage, server

# The public API surface (plan §2.B): these 12 lose the leading underscore.
PUBLIC_API = (
    "resolve_profile_selection",
    "run_storage_sweep",
    "spawn_background_sweep",
    "enforce_session_storage",
    "clone_is_auto",
    "clone_is_named",
    "default_session_root",
    "master_profile_dir",
    "clone_root_dir",
    "master_snapshot_dir",
    "clone_storage_cap_bytes",
    "session_storage_cap_bytes",
)


class TestModuleSurface:
    def test_public_api_present_and_callable(self):
        for name in PUBLIC_API:
            assert callable(getattr(clone_storage, name)), name

    def test_internal_helpers_keep_their_underscore(self):
        # Representative sample: helpers that are NOT part of the module's API
        # surface keep the leading underscore (including the two profile-selection
        # collaborators that stay private).
        for name in (
            "_fallback_profile_selection",
            "_public_profile_selection",
            "_release_clone_dir",
            "_refresh_master_snapshot_if_safe",
            "_enforce_clone_storage_cap_in",
            "_enforce_named_profile_trim_in",
            "_idle_autoclones_over_cap",
            "_named_profiles_over_session_cap",
            "_trash_clone",
            "_clone_trash_dir",
            "_profile_has_running_browser",
            "_copy_profile_delta",
        ):
            assert callable(getattr(clone_storage, name)), name

    def test_server_keeps_no_reexport_or_alias(self):
        # F-201 + second-way lens: the moved names live ONLY in clone_storage.
        # server delegates via the imported module, never a re-export.
        for name in (
            *PUBLIC_API,
            "_resolve_profile_selection",
            "_clone_is_auto",
            "_clone_is_named",
            "_spawn_background_sweep",
            "_release_clone_dir",
            "_default_session_root",
        ):
            assert not hasattr(server, name), f"server still exposes {name}"
        # The delegation handle IS present.
        assert server.clone_storage is clone_storage


class TestSpawnBrowserDelegates:
    """The one consumer pin the plan calls out: spawn_browser's profile selection
    routes through ``clone_storage.resolve_profile_selection`` (patching it there
    — not on ``server`` — is what lands)."""

    async def test_profile_selection_routes_through_clone_storage(
        self, call_tool, patched_server, monkeypatch
    ):
        seen = {}

        async def fake_resolve(user_data_dir, **kwargs):
            seen["called"] = True
            return {"user_data_dir": "/fake/dir", "profile_role": "clone"}

        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        fake_instance = SimpleNamespace(
            instance_id="i1",
            state="active",
            headless=False,
            viewport={"width": 1920, "height": 1080},
        )
        fbm = FakeBrowserManager(spawn_instance=fake_instance, spawn_diagnostics={})
        srv = patched_server(browser_manager=fbm)
        await call_tool(srv, "spawn_browser", sandbox=False)
        assert seen.get("called") is True


class TestDelegateIdentity:
    """Light pin that the extracted helpers execute from the new home; the deep
    per-branch coverage stays in the repointed predecessor suites."""

    def test_path_helpers_return_paths(self, tmp_session_root):
        assert isinstance(clone_storage.default_session_root(), Path)
        assert isinstance(clone_storage.master_profile_dir(), Path)
        assert isinstance(clone_storage.clone_root_dir(), Path)
        assert isinstance(clone_storage.master_snapshot_dir(), Path)

    def test_caps_are_positive_ints(self, tmp_session_root):
        assert isinstance(clone_storage.clone_storage_cap_bytes(), int)
        assert clone_storage.clone_storage_cap_bytes() > 0
        assert isinstance(clone_storage.session_storage_cap_bytes(), int)
        assert clone_storage.session_storage_cap_bytes() > 0
