"""F-966 -- an unnamed spawn never opens ``default``, and ``close_instance`` will
not close ``default`` or ``fleet`` unless it is told which session it means.

The incident (owner, 2026-10-12 21:39Z, backend 2.1.27): a chat called
``spawn_browser()`` with NO session, twice, and both answers carried
``profile_role == "default"`` on ``<root>/master``. Nobody held master, so the
resolver's "master is free" branch OPENED it for a caller who asked for no profile
at all; the chat then ``close_instance``d its two instances, which closed the
shared browser, and every later clone lost its live seed.

Two rules, both the owner's, both with no setting:

1. **An unnamed spawn is always a disposable clone.** Whether master is free, at
   rest without a seed, held by a browser we drive (the existing hand-off) or
   held by one we do not. Only ``session="default"`` (or master's own path) opens
   master. A brand-new install, with neither master nor seed, gets a fresh empty
   clone and a ``seed_warning`` that names ``session="default"``.
2. **``close_instance`` refuses the ``default`` and ``fleet`` browsers unless the
   caller names the session**, with the exact call to make. Every other instance
   closes as before. The self-heal names the session it is healing, so it keeps
   working.

Hermetic: no Chrome. ``tmp_session_root`` / ``tmp_empty_root`` own the session
root, and the manager is ``FakeBrowserManager`` with real ``BrowserOptions``.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeBrowserManager
from stealth_chrome_devtools_mcp.embedded import (
    browser_reattach,
    clone_storage,
    profile_seed,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

SHARED = profile_seed.DEFAULT_SESSION


@pytest.fixture(autouse=True)
def _clean_reservations():
    clone_storage._clear_protected_clone_dirs()
    yield
    clone_storage._clear_protected_clone_dirs()


def _is_clone(selection: dict) -> bool:
    directory = Path(selection["user_data_dir"])
    return (
        selection["profile_role"] == "clone"
        and directory != clone_storage.master_profile_dir()
        and clone_storage._is_relative_to(directory, clone_storage.clone_root_dir())
    )


# ---------------------------------------------------------------------------
# 1. the resolver
# ---------------------------------------------------------------------------


class TestResolver:
    async def test_an_unnamed_selection_is_a_clone_while_master_is_free(
        self, tmp_session_root
    ):
        selection = await clone_storage.resolve_profile_selection(None)

        assert _is_clone(selection), selection
        assert selection["clone_source"] == "default-seed"

    async def test_with_no_seed_it_copies_master_at_rest_and_never_opens_it(
        self, tmp_session_root
    ):
        import shutil

        shutil.rmtree(tmp_session_root["snapshot"])

        selection = await clone_storage.resolve_profile_selection(None)

        assert _is_clone(selection), selection
        assert selection["clone_source"] == "live-default-fallback"

    @pytest.mark.parametrize("spelling", ["name", "path"])
    async def test_the_default_session_still_opens_master(
        self, tmp_session_root, spelling
    ):
        by_path = (str(tmp_session_root["master"]), None)
        landed = clone_storage.require_allowed_user_data_dir(
            *(by_path if spelling == "path" else (None, SHARED))
        )

        selection = await clone_storage.resolve_profile_selection(landed)

        assert selection["profile_role"] == SHARED
        assert Path(selection["user_data_dir"]) == clone_storage.master_profile_dir()

    async def test_concurrent_unnamed_selections_land_on_distinct_clones(
        self, tmp_session_root
    ):
        selections = await asyncio.gather(
            *(clone_storage.resolve_profile_selection(None) for _ in range(3))
        )

        assert all(_is_clone(s) for s in selections), selections
        assert len({s["user_data_dir"] for s in selections}) == 3

    async def test_a_first_install_gets_a_fresh_empty_clone_and_is_told_why(
        self, tmp_empty_root
    ):
        selection = await clone_storage.resolve_profile_selection(None)

        assert _is_clone(selection), selection
        clone = Path(selection["user_data_dir"])
        assert clone.is_dir()
        assert [p.name for p in clone.iterdir()] == [profile_seed.MARKER_NAME], (
            "a fresh clone holds nothing but its own marker"
        )
        assert profile_seed.is_auto(clone), "disposable, so the sweep can reclaim it"
        assert f'session="{SHARED}"' in selection["seed_warning"]
        assert not tmp_empty_root["master"].exists(), "master was created"
        assert not tmp_empty_root["snapshot"].exists(), "a seed was invented"

    async def test_a_second_first_install_spawn_does_not_inherit_the_first_ones_tree(
        self, tmp_empty_root
    ):
        first = await clone_storage.resolve_profile_selection(None)
        (Path(first["user_data_dir"]) / "stale-login").write_text("x")
        clone_storage._clear_protected_clone_dirs()

        second = await clone_storage.resolve_profile_selection(None)

        assert not (Path(second["user_data_dir"]) / "stale-login").exists()

    async def test_the_default_session_on_a_first_install_opens_master_not_a_clone(
        self, tmp_empty_root
    ):
        landed = clone_storage.require_allowed_user_data_dir(None, SHARED)

        selection = await clone_storage.resolve_profile_selection(landed)

        assert selection["profile_role"] == SHARED
        assert Path(selection["user_data_dir"]) == tmp_empty_root["master"]
        assert not tmp_empty_root["snapshot"].exists(), (
            "opening a master that does not exist yet published an EMPTY seed, "
            "which every later clone is then copied from"
        )


# ---------------------------------------------------------------------------
# 2. the spawn tool, end to end through the tool body
# ---------------------------------------------------------------------------


def _manager() -> FakeBrowserManager:
    return FakeBrowserManager(
        spawn_instance=SimpleNamespace(
            instance_id="i1",
            state="active",
            headless=True,
            viewport={"width": 600, "height": 900},
        )
    )


class TestSpawnTool:
    async def test_an_unnamed_spawn_drives_a_clone_the_owners_exact_call(
        self, tmp_session_root, call_tool, patched_server
    ):
        manager = _manager()
        srv = patched_server(browser_manager=manager)

        answer = await call_tool(
            srv,
            "spawn_browser",
            headless=True,
            sandbox=False,
            viewport_width=600,
            viewport_height=900,
        )

        selection = answer["spawn_diagnostics"]["profile_selection"]
        assert selection["profile_role"] == "clone", selection
        (options,) = manager.spawn_calls
        assert Path(options.user_data_dir) != tmp_session_root["master"]
        assert options.auto_clone is True

    async def test_an_unnamed_spawn_is_never_handed_a_browser_holding_master(
        self, tmp_session_root, call_tool, patched_server, monkeypatch
    ):
        """F-931 re-attached a STRANDED master browser for a spawn naming nothing,
        which is the same hand-over of the shared browser by another door."""
        asked: list[str] = []

        async def _spy(manager, cleanup, user_data_dir, **_kwargs):
            asked.append(user_data_dir)
            return browser_reattach.Held()

        monkeypatch.setattr(browser_reattach, "adopt_held_profile", _spy)
        srv = patched_server(browser_manager=_manager())

        answer = await call_tool(srv, "spawn_browser", headless=True, sandbox=False)

        assert asked == [], f"an unnamed spawn asked to adopt {asked}"
        assert answer["spawn_diagnostics"]["profile_selection"]["profile_role"] == (
            "clone"
        )

    async def test_session_default_still_drives_master_itself(
        self, tmp_session_root, call_tool, patched_server
    ):
        manager = _manager()
        srv = patched_server(browser_manager=manager)

        answer = await call_tool(
            srv, "spawn_browser", headless=True, sandbox=False, session=SHARED
        )

        selection = answer["spawn_diagnostics"]["profile_selection"]
        assert selection["profile_role"] == SHARED, selection
        (options,) = manager.spawn_calls
        assert Path(options.user_data_dir) == tmp_session_root["master"]
        assert options.auto_clone is False

    async def test_three_concurrent_unnamed_spawns_drive_three_distinct_clones(
        self, tmp_session_root, call_tool, patched_server
    ):
        manager = _manager()
        srv = patched_server(browser_manager=manager)

        await asyncio.gather(
            *(
                call_tool(srv, "spawn_browser", headless=True, sandbox=False)
                for _ in range(3)
            )
        )

        dirs = [options.user_data_dir for options in manager.spawn_calls]
        assert len(set(dirs)) == 3, dirs
        assert str(tmp_session_root["master"]) not in dirs


# ---------------------------------------------------------------------------
# 3. close_instance
# ---------------------------------------------------------------------------


class _Closing(FakeBrowserManager):
    """Instances whose spawn diagnostics carry the profile selection the spawn
    recorded (what a re-attach records too, F-888), and a close that records
    itself. ``get_instance`` is NOT answered: the guard must not need it, because
    it discards a dead browser."""

    def __init__(self) -> None:
        super().__init__()
        self.selections: dict[str, dict] = {}
        self.closed: list[str] = []

    def add(self, instance_id: str, directory: Path, *, role: str):
        self._instances.append(SimpleNamespace(instance_id=instance_id))
        self.selections[instance_id] = {
            "user_data_dir": str(directory),
            "profile_role": role,
        }
        return instance_id

    async def get_instance(self, instance_id):
        raise AssertionError("a close must not look the instance up (it discards)")

    async def get_spawn_diagnostics(self, instance_id):
        selection = self.selections.get(instance_id)
        return {"profile_selection": selection} if selection else {}

    async def close_instance(self, instance_id) -> bool:
        self.closed.append(instance_id)
        return instance_id in self.selections


@pytest.fixture()
def closing(tmp_session_root, patched_server):
    manager = _Closing()
    manager.add("i-default", tmp_session_root["master"], role=SHARED)
    manager.add("i-fleet", tmp_session_root["sessions"] / "fleet", role="explicit")
    manager.add("i-work", tmp_session_root["sessions"] / "work", role="explicit")
    manager.add("i-clone", tmp_session_root["sessions"] / "sess-abc123", role="clone")
    return SimpleNamespace(manager=manager, srv=patched_server(browser_manager=manager))


class TestCloseGuard:
    @pytest.mark.parametrize(
        ("instance_id", "session"),
        [("i-default", SHARED), ("i-fleet", "fleet")],
    )
    async def test_closing_a_shared_session_without_naming_it_is_refused(
        self, closing, call_tool, instance_id, session
    ):
        with pytest.raises(ToolError) as refused:
            await call_tool(closing.srv, "close_instance", instance_id=instance_id)

        text = str(refused.value)
        assert f"{session!r}" in text or f'"{session}"' in text
        assert "other chats" in text
        assert f'close_instance(instance_id="{instance_id}", session="{session}")' in (
            text
        )
        assert closing.manager.closed == [], "the browser was touched"

    @pytest.mark.parametrize(
        ("instance_id", "session"),
        [("i-default", SHARED), ("i-fleet", "fleet"), ("i-fleet", "Fleet")],
    )
    async def test_naming_the_session_closes_it(
        self, closing, call_tool, instance_id, session
    ):
        answer = await call_tool(
            closing.srv, "close_instance", instance_id=instance_id, session=session
        )

        assert answer["closed"] is True
        assert closing.manager.closed == [instance_id]

    @pytest.mark.parametrize(
        ("instance_id", "wrong"),
        [("i-default", "fleet"), ("i-fleet", SHARED), ("i-fleet", "work")],
    )
    async def test_naming_the_other_session_is_refused_too(
        self, closing, call_tool, instance_id, wrong
    ):
        with pytest.raises(ToolError):
            await call_tool(
                closing.srv, "close_instance", instance_id=instance_id, session=wrong
            )

        assert closing.manager.closed == []

    @pytest.mark.parametrize("instance_id", ["i-clone", "i-work"])
    async def test_every_other_instance_closes_exactly_as_before(
        self, closing, call_tool, instance_id
    ):
        answer = await call_tool(closing.srv, "close_instance", instance_id=instance_id)

        assert answer["closed"] is True
        assert closing.manager.closed == [instance_id]

    async def test_session_is_ignored_for_an_instance_that_is_not_shared(
        self, closing, call_tool
    ):
        answer = await call_tool(
            closing.srv, "close_instance", instance_id="i-clone", session="fleet"
        )

        assert answer["closed"] is True

    async def test_an_unknown_instance_is_still_answered_by_the_manager(
        self, closing, call_tool
    ):
        answer = await call_tool(closing.srv, "close_instance", instance_id="nope")

        assert answer["closed"] is False
        assert closing.manager.closed == ["nope"]

    async def test_a_clone_that_sits_on_the_fleet_directory_is_not_the_fleet(
        self, tmp_session_root, patched_server, call_tool
    ):
        """The classifier is F-964's: a clone is never one of the two."""
        manager = _Closing()
        manager.add("i-x", tmp_session_root["sessions"] / "fleet", role="clone")
        srv = patched_server(browser_manager=manager)

        answer = await call_tool(srv, "close_instance", instance_id="i-x")

        assert answer["closed"] is True
