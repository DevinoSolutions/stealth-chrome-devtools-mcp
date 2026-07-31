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

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fakes import FakeBrowserManager
from stealth_chrome_devtools_mcp.embedded import clone_storage, server
from stealth_chrome_devtools_mcp.settings import Settings, get_settings

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
    "browser_session_storage_cap_bytes",
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


class TestClientRootsRoundTripIsBounded:
    """F-790: ``_client_session_seed()`` asks the CLIENT for its roots, and MCP
    ``roots`` is an OPTIONAL client capability — a conforming client may simply
    never answer. Before the fix that await had no deadline, so the default
    (auto-clone) ``spawn_browser`` path parked forever.

    These are the unit half of the pin; the real-transport half lives in
    ``test_wire_semantics.py::test_a_second_unnamed_spawn_is_bounded_when_the_
    client_never_answers_roots_list``, which drives the installed launcher over
    stdio and never answers the frame at all.
    """

    @staticmethod
    def _no_local_seed(monkeypatch):
        """Silence the configured-key fast path so execution reaches the round
        trip, and pin the fallback chain to a value the assertions can name."""
        for field in (
            "stealth_chrome_profile_key",
            "browser_profile_key",
            "codex_workspace",
            "claude_project_dir",
            "pwd",
        ):
            monkeypatch.setattr(get_settings(), field, None, raising=False)

    @staticmethod
    def _context(list_roots):
        return SimpleNamespace(list_roots=list_roots)

    async def test_a_client_that_never_answers_falls_back_within_the_bound(
        self, monkeypatch
    ):
        # The oracle for the hang itself: a client that goes silent must not
        # extend the seed's runtime past the configured deadline.
        self._no_local_seed(monkeypatch)
        monkeypatch.setattr(
            get_settings(), "client_roots_timeout_seconds", 0.25, raising=False
        )

        async def _never_answers():
            await asyncio.Event().wait()  # exactly what a silent client looks like

        with patch(
            "fastmcp.server.dependencies.get_context",
            return_value=self._context(_never_answers),
        ):
            started = time.monotonic()
            seed = await asyncio.wait_for(clone_storage._client_session_seed(), 10.0)
            elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"the bounded await took {elapsed:.1f}s"
        # Fell back to the same local chain an unsupported client already takes.
        assert seed == os.getcwd()

    async def test_a_client_that_answers_still_seeds_from_its_roots(self, monkeypatch):
        # The preserved-behavior half: bounding the await must not change what a
        # client that DOES answer produces.
        self._no_local_seed(monkeypatch)

        async def _answers():
            return [
                SimpleNamespace(uri="file:///b/second"),
                SimpleNamespace(uri="file:///a/first"),
            ]

        with patch(
            "fastmcp.server.dependencies.get_context",
            return_value=self._context(_answers),
        ):
            seed = await clone_storage._client_session_seed()

        # Sorted and "|"-joined, with file:// decoded per PurePath flavor
        # (_root_to_path strips the leading slash only on nt).
        lead = "" if os.name == "nt" else "/"
        assert seed == f"{lead}a/first|{lead}b/second"

    async def test_a_zero_bound_never_waits_on_the_client(self, monkeypatch):
        # 0 is the documented "never ask" setting; it must degrade to the
        # fallback rather than raise out of the seed.
        self._no_local_seed(monkeypatch)
        monkeypatch.setattr(
            get_settings(), "client_roots_timeout_seconds", 0.0, raising=False
        )

        async def _never_answers():
            await asyncio.Event().wait()

        with patch(
            "fastmcp.server.dependencies.get_context",
            return_value=self._context(_never_answers),
        ):
            assert (
                await asyncio.wait_for(clone_storage._client_session_seed(), 10.0)
                == os.getcwd()
            )

    def test_the_bound_is_a_typed_setting_in_the_one_env_home(self):
        # settings.py is THE env home (CLAUDE.md); the knob may not regress into
        # a bare literal or an os.environ read.
        assert Settings.model_fields["client_roots_timeout_seconds"].default == 5.0
        assert get_settings().client_roots_timeout_seconds >= 0

    @pytest.mark.parametrize("value", ["0.5", "12"])
    def test_the_bound_is_operator_configurable(self, monkeypatch, value):
        monkeypatch.setenv("STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS", value)
        get_settings.cache_clear()
        assert get_settings().client_roots_timeout_seconds == float(value)


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
        assert isinstance(clone_storage.browser_session_storage_cap_bytes(), int)
        assert clone_storage.browser_session_storage_cap_bytes() > 0

    def test_default_session_root_uses_os_specific_default(self, monkeypatch):
        # plan_RELEASE W2: with NO configured root, default_session_root() falls to
        # an OS-specific default (the os.name branch in clone_storage). Every
        # existing caller overrides the root via env, so this branch is asserted
        # nowhere. The three-OS gate runs this on each OS, proving both the Windows
        # (C:\stealth-mcp-browser-sessions) and POSIX (~/.stealth-mcp-browser-
        # sessions) defaults on the platform that produces them.
        import os

        from stealth_chrome_devtools_mcp.settings import get_settings

        monkeypatch.delenv("STEALTH_MCP_BROWSER_SESSION_ROOT", raising=False)
        get_settings.cache_clear()

        root = clone_storage.default_session_root()
        if os.name == "nt":
            assert root == Path(r"C:\stealth-mcp-browser-sessions")
        else:
            assert root == Path.home() / ".stealth-mcp-browser-sessions"
