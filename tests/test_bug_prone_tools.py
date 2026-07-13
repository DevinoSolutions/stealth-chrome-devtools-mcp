"""Bug-prone tool characterization (M6-4).

Three targets the upcoming M4/M13 refactors will churn:

* ``spawn_browser`` — pin the param-forwarding contract (tool kwargs → the
  ``BrowserOptions`` handed to ``BrowserManager.spawn_browser``) and the result
  shape, WITHOUT exercising the ~230-line internals. Reaching the seam needs
  patching *two* collaborators (``_resolve_profile_selection`` and
  ``browser_manager``) because the tool has no single injection point — that
  multi-collaborator coupling is the F-208 seam gap M4/M13 close.
* ``_fallback_profile_selection`` — gap-fill only: ``test_profile_resolution.py``
  pins ``_resolve_profile_selection`` thoroughly but never the fallback's retry
  protocol, so pin its guard clause (non-clone selections never retry).
* ``list_instances`` liveness (F-611) — prune-by-OS-process-only, with NO CDP
  round-trip, so a process-alive-but-CDP-dead instance still reads active.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fakes import FakeBrowser, FakeBrowserManager, FakeStorage
from stealth_chrome_devtools_mcp.embedded import browser_manager as _bm
from stealth_chrome_devtools_mcp.embedded import clone_storage
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

# ===========================================================================
# spawn_browser — param forwarding through the (multi-collaborator) seam.
# ===========================================================================


class TestSpawnBrowserSeam:
    """Pins spawn_browser's forwarding + result shape. NOTE (seam gap, F-208 →
    M4/M13): the tool cannot be driven by patching a single seam — it resolves a
    profile AND delegates to the manager AND wires interception inline, so the
    test must patch ``_resolve_profile_selection`` and ``browser_manager``
    together. The 230-line internals stay unexercised by design."""

    async def test_forwards_kwargs_into_browser_options(
        self, call_tool, patched_server, monkeypatch
    ):
        fake_instance = SimpleNamespace(
            instance_id="i1",
            state="active",
            headless=True,
            viewport={"width": 800, "height": 600},
        )
        fbm = FakeBrowserManager(spawn_instance=fake_instance, spawn_diagnostics={})

        async def fake_resolve(user_data_dir, **kwargs):
            return {"user_data_dir": "/fake/dir", "profile_role": "clone"}

        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        srv = patched_server(browser_manager=fbm)
        # get_tab returns None (default) → network interception is skipped, so the
        # network_interceptor collaborator need not be patched.
        result = await call_tool(
            srv,
            "spawn_browser",
            headless=True,
            viewport_width=800,
            viewport_height=600,
            proxy="http://p:1",
            user_agent="UA",
            timezone_id="UTC",
            sandbox=False,
        )

        assert len(fbm.spawn_calls) == 1
        options = fbm.spawn_calls[0]
        assert options.headless is True
        assert options.viewport_width == 800
        assert options.viewport_height == 600
        assert options.proxy == "http://p:1"
        assert options.user_agent == "UA"
        assert options.timezone_id == "UTC"
        assert options.sandbox is False
        assert options.user_data_dir == "/fake/dir"
        # profile_role == "clone" flows through to auto_clone.
        assert options.auto_clone is True

    async def test_result_shape(self, call_tool, patched_server, monkeypatch):
        fake_instance = SimpleNamespace(
            instance_id="i1",
            state="active",
            headless=False,
            viewport={"width": 1920, "height": 1080},
        )
        fbm = FakeBrowserManager(
            spawn_instance=fake_instance, spawn_diagnostics={"ok": True}
        )

        async def fake_resolve(user_data_dir, **kwargs):
            return {"user_data_dir": "/fake/dir", "profile_role": "clone"}

        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        srv = patched_server(browser_manager=fbm)
        result = await call_tool(srv, "spawn_browser", sandbox=False)

        assert set(result) == {
            "instance_id",
            "state",
            "headless",
            "viewport",
            "spawn_diagnostics",
        }
        assert result["instance_id"] == "i1"
        # The tool decorates the manager's diagnostics with the public selection.
        assert result["spawn_diagnostics"]["ok"] is True
        assert "profile_selection" in result["spawn_diagnostics"]


# ===========================================================================
# _fallback_profile_selection — gap-fill (retry protocol unpinned elsewhere).
# ===========================================================================


class TestFallbackProfileSelection:
    """``test_profile_resolution.py`` pins ``_resolve_profile_selection`` but not
    the fallback's retry guard — pin it here (no overlap)."""

    async def test_non_clone_selection_never_retries(self):
        # profile_role != "clone" short-circuits to None before any dir I/O (pure).
        assert (
            await clone_storage._fallback_profile_selection(
                {"profile_role": "explicit"}, 0
            )
            is None
        )
        assert await clone_storage._fallback_profile_selection({}, 0) is None
        assert (
            await clone_storage._fallback_profile_selection(
                {"profile_role": "explicit"}, 5
            )
            is None
        )

    async def test_clone_with_no_snapshot_returns_none(self, tmp_empty_root):
        # A clone selection retries from the master snapshot; with no snapshot on
        # disk (tmp_empty_root), both the first and final attempts yield None.
        assert (
            await clone_storage._fallback_profile_selection(
                {"profile_role": "clone"}, 0
            )
            is None
        )
        assert (
            await clone_storage._fallback_profile_selection(
                {"profile_role": "clone"}, 1
            )
            is None
        )


# ===========================================================================
# list_instances liveness (F-611) — OS-process pruning, no CDP round-trip.
# ===========================================================================


class TestListInstancesLiveness:
    def test_process_alive_is_alive(self):
        assert BrowserManager._browser_process_is_alive(FakeBrowser(alive=True)) is True

    def test_process_exited_is_dead(self):
        assert (
            BrowserManager._browser_process_is_alive(FakeBrowser(alive=False)) is False
        )

    @pytest.mark.characterization
    def test_no_process_no_pid_defaults_to_alive(self):
        """PINS CURRENT BEHAVIOR incl. known quirk F-611 (no CDP probe); a
        standalone follow-up will add a CDP round-trip — update when it lands. A
        browser with neither a ``_process`` handle nor a ``_process_pid`` is
        assumed ALIVE (the liveness check has no CDP fallback to prove otherwise)."""
        assert (
            BrowserManager._browser_process_is_alive(FakeBrowser(alive=None, pid=None))
            is True
        )

    @pytest.fixture()
    def isolated_manager(self, monkeypatch):
        """A fresh BrowserManager with the discard-path collaborators stubbed, so
        pruning a dead instance touches no real process/storage/hook state."""
        monkeypatch.setattr(_bm, "process_cleanup", MagicMock())
        monkeypatch.setattr(_bm, "in_memory_storage", FakeStorage())
        monkeypatch.setattr(_bm, "dynamic_hook_system", MagicMock())
        return BrowserManager()

    async def test_dead_pruned_alive_kept(self, isolated_manager):
        isolated_manager._instances = {
            "alive": {
                "browser": FakeBrowser(alive=True),
                "instance": SimpleNamespace(instance_id="alive", state="active"),
            },
            "dead": {
                "browser": FakeBrowser(alive=False),
                "instance": SimpleNamespace(instance_id="dead", state="active"),
            },
        }
        survivors = await isolated_manager.list_instances()
        ids = {inst.instance_id for inst in survivors}
        assert ids == {"alive"}

    @pytest.mark.characterization
    async def test_alive_process_stays_without_cdp_probe(self, isolated_manager):
        """PINS CURRENT BEHAVIOR incl. known quirk F-611 (no CDP round-trip); the
        standalone follow-up will change this — update when it lands. An instance
        whose OS process is alive is retained as active EVEN IF its CDP channel is
        dead: ``list_instances`` inspects only the process, never the tab, so a
        wedged-but-running browser is indistinguishable from a healthy one here."""
        # FakeBrowser is process-alive; its tab/CDP is never consulted by the prune.
        isolated_manager._instances = {
            "wedged": {
                "browser": FakeBrowser(alive=True),
                "instance": SimpleNamespace(instance_id="wedged", state="active"),
            }
        }
        survivors = await isolated_manager.list_instances()
        assert [inst.instance_id for inst in survivors] == ["wedged"]
