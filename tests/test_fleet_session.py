"""F-952 -- the shared ``fleet`` session.

The claims, each pinned because each is a place a shared browser's login could
be lost or a caller could be told something false:

1. **It is persistent**, as a named session already is: never deleted by
   close, never selected by the storage sweep, never reaped by the F-888
   pass. Pinned at the functions that could break it, not by a role word.
2. **It is restored FIRST** and a failed attach to it does NOT reap it,
   while the same failure on any other profile still does (F-888 unchanged).
3. **Asking for it while it runs HERE returns that instance** with
   ``already_running``, never a walked ``fleet-2`` and never a clone; and an
   unnamed spawn is still never handed it.
4. **A clone or a new session seeded from it** takes its LIVE cookies when this
   backend drives it, is refused by name when something else does, and reports
   ``seeded_from`` truthfully. ``STEALTH_MCP_SEED_SESSION`` makes it the default
   source and is off unless set.
   F-939 (only the source rotates Google cookies) needs no code here: the
   guard follows ``auto_clone`` (``profile_role == "clone"``, `spawn_browser`),
   and ``test_google_rotation_guard.TestTheSpawnArmsClonesOnly`` pins the
   arming. These tests pin the two role words it reads: the fleet resolves to
   ``explicit`` (a source, free to rotate) and a clone seeded from it to
   ``clone`` (fenced).
5. **A running browser can become the fleet session**: ``session="fleet",
   seed_from=<name>`` -- including a walked auto-clone name.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fakes import FakeBrowser, FakeTab, held_profile
from stealth_chrome_devtools_mcp.embedded import (
    browser_reattach,
    cdp_attach,
    clone_storage,
    fleet_session,
    profile_seed,
    profile_source,
    session_lease,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.embedded.tool_sections import browser_management
from stealth_chrome_devtools_mcp.settings import get_settings

LOGIN = b"fleet-login-cookie-jar"
COOKIE_JAR = "Default/Network/Cookies"
CHROME_PID = 7777
PORT = 51234


def _driving(*profiles: Path):
    def driven(profile: Path) -> bool:
        return any(profile_seed.same_dir(profile, known) for known in profiles)

    return driven


async def _selection(
    *, session=None, seed_from=None, driven=profile_source.NOTHING_DRIVEN
) -> dict:
    """What a spawn resolves to, composed as ``spawn_browser`` does."""
    landed = clone_storage.require_allowed_user_data_dir(None, session)
    seed = clone_storage.require_allowed_seed_from(seed_from, landed, driven=driven)
    selection = await clone_storage.resolve_profile_selection(
        landed, seed_from=seed, driven=driven
    )
    live = {k: v for k, v in selection.items() if k == clone_storage.LIVE_SEED_KEY}
    return {**clone_storage._public_profile_selection(selection), **live}


async def _fleet(dirs: dict) -> Path:
    """Create the fleet session and put a login in it."""
    await _selection(session=fleet_session.FLEET_SESSION)
    directory = dirs["sessions"] / fleet_session.FLEET_SESSION
    jar = directory / COOKIE_JAR
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_bytes(LOGIN)
    return directory


def _record(profile: Path, instance_id="i-fleet") -> browser_reattach.Adoptable:
    return browser_reattach.Adoptable(
        instance_id=instance_id, pid=CHROME_PID, user_data_dir=str(profile), port=PORT
    )


class TestItIsAPersistentNamedSession:
    async def test_it_is_created_as_a_named_persistent_profile(self, tmp_session_root):
        selection = await _selection(session="fleet")

        directory = Path(selection["user_data_dir"])
        assert directory == tmp_session_root["sessions"] / "fleet"
        assert profile_seed.is_named(directory)
        assert not profile_seed.is_auto(directory)
        assert selection["profile_role"] == "explicit"
        assert selection["seeded_from"] == profile_seed.DEFAULT_SESSION

    async def test_close_does_not_delete_it(self, tmp_session_root, tmp_path):
        fleet = await _fleet(tmp_session_root)
        pc = ProcessCleanup.__new__(ProcessCleanup)
        pc.pid_file = tmp_path / "browser_pids.json"
        pc.tracked_pids = set()
        pc.browser_processes = {}
        entry = {
            "pid": CHROME_PID,
            "user_data_dir": str(fleet),
            "uses_custom_data_dir": True,
            "auto_clone": False,
        }

        assert pc._cleanup_profile_for_metadata("i-fleet", entry) is False
        assert (fleet / COOKIE_JAR).read_bytes() == LOGIN

    async def test_the_storage_sweep_never_deletes_it(self, tmp_session_root):
        fleet = await _fleet(tmp_session_root)

        clone_storage._enforce_clone_storage_cap_in(
            tmp_session_root["sessions"], 1, "test"
        )

        assert (fleet / COOKIE_JAR).read_bytes() == LOGIN

    async def test_a_recorded_fleet_browser_is_adoptable(self, tmp_session_root):
        fleet = await _fleet(tmp_session_root)
        entry = {
            "pid": CHROME_PID,
            "create_time": 1.0,
            "user_data_dir": str(fleet),
            "uses_custom_data_dir": True,
            "auto_clone": False,
            "cdp_port": PORT,
            "timestamp": 0,
            "owner_pid": 9001,
            "owner_create_time": 1.0,
        }

        found = browser_reattach.adoptable(
            {"i-fleet": entry},
            owner_alive=lambda *_: False,
            browser_alive=lambda *_: True,
        ).adoptable

        assert set(found) == {"i-fleet"}


class TestRestoredFirstAndNeverReaped:
    @pytest.fixture
    def manager(self):
        return BrowserManager()

    @pytest.fixture
    def cleanup(self, tmp_path):
        double = MagicMock()
        double.pid_file = tmp_path / "browser_pids.json"
        double._owner_backend_alive.return_value = False
        return double

    async def test_the_fleet_entry_is_attached_before_the_others(
        self, tmp_session_root, manager, cleanup
    ):
        fleet = await _fleet(tmp_session_root)
        other = tmp_session_root["sessions"] / "other"
        attached: list[str] = []

        async def fake_adopt_one(_manager, _cleanup, candidate):
            attached.append(candidate.instance_id)
            return candidate.instance_id

        # `other` is recorded BEFORE the fleet entry, so recorded order and
        # restore order disagree.
        adoptable = {
            "i-other": _record(other, "i-other"),
            "i-fleet": _record(fleet, "i-fleet"),
        }
        with (
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=browser_reattach.Classified(
                    adoptable=adoptable, unclassifiable=set()
                ),
            ),
            patch.object(browser_reattach, "_adopt_one", fake_adopt_one),
        ):
            adopted = await browser_reattach.run(manager, cleanup)

        assert attached == ["i-fleet", "i-other"]
        assert adopted == ["i-fleet", "i-other"]

    async def test_a_failed_attach_to_the_fleet_does_not_reap_it(
        self, tmp_session_root, manager, cleanup
    ):
        fleet = await _fleet(tmp_session_root)
        with (
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(
                cdp_attach, "attach", side_effect=ConnectionRefusedError("no")
            ),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=browser_reattach.Classified(
                    adoptable={"i-fleet": _record(fleet)}, unclassifiable=set()
                ),
            ),
            patch.object(browser_reattach, "reap_recorded") as reap,
        ):
            adopted = await browser_reattach.run(manager, cleanup)

        assert adopted == []
        assert reap.call_count == 0, "the fleet browser was killed for one bad attach"
        cleanup._drop_recorded.assert_not_called()

    async def test_any_other_profile_still_takes_the_reap(
        self, tmp_session_root, manager, cleanup
    ):
        """The contrast that keeps the exemption from being a regression: F-888's
        remedy for a browser it cannot attach to is unchanged for everyone else."""
        other = tmp_session_root["sessions"] / "other"
        with (
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(
                cdp_attach, "attach", side_effect=ConnectionRefusedError("no")
            ),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=browser_reattach.Classified(
                    adoptable={"i-other": _record(other, "i-other")},
                    unclassifiable=set(),
                ),
            ),
            patch.object(browser_reattach, "reap_recorded") as reap,
        ):
            await browser_reattach.run(manager, cleanup)

        assert reap.call_count == 1
        cleanup._drop_recorded.assert_called_once_with({"i-other"})

    async def test_the_fleet_comes_back_with_its_recorded_instance_id(
        self, tmp_session_root, manager, cleanup
    ):
        fleet = await _fleet(tmp_session_root)
        browser = FakeBrowser(alive=True, pid=CHROME_PID, main_tab=FakeTab())
        with (
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach", return_value=browser),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=browser_reattach.Classified(
                    adoptable={"i-fleet": _record(fleet)}, unclassifiable=set()
                ),
            ),
        ):
            await browser_reattach.run(manager, cleanup)

        listed = await manager.list_instances()
        assert [i.instance_id for i in listed] == ["i-fleet"]


class TestAskingForItWhileItRunsHere:
    def _manager(self, profile: Path) -> BrowserManager:
        manager = BrowserManager()
        manager._instances["i-fleet"] = {
            "browser": FakeBrowser(alive=True),
            "instance": BrowserInstance(instance_id="i-fleet"),
            "options": SimpleNamespace(user_data_dir=str(profile)),
        }
        return manager

    async def _spawn(
        self,
        call_tool,
        patched_server,
        monkeypatch,
        manager,
        fleet,
        headless=True,
        **kwargs,
    ):
        resolved: list = []

        def fake_held_by(user_data_dir, **_k):
            # Only the fleet's directory has a browser on it.
            if profile_seed.same_dir(Path(user_data_dir), fleet):
                return _record(fleet)
            return None

        async def fake_resolve(user_data_dir, **_):
            resolved.append(user_data_dir)
            return {
                "user_data_dir": "/a-fresh-copy",
                "profile_role": "clone",
                "clone_source": None,
            }

        async def fake_launch(_options):
            return SimpleNamespace(
                instance_id="i-new", state="active", headless=True, viewport={}
            )

        async def no_tab(_instance_id):
            return None

        async def diagnostics(_instance_id):
            return {}

        async def fake_adopted(instance_id, _block_resources):
            return {"instance_id": instance_id, "reattached": True, "headless": True}

        monkeypatch.setattr(browser_reattach, "held_by", fake_held_by)
        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        monkeypatch.setattr(
            browser_management, "_adopted_instance_record", fake_adopted
        )
        monkeypatch.setattr(manager, "spawn_browser", fake_launch)
        monkeypatch.setattr(manager, "get_tab", no_tab)
        monkeypatch.setattr(manager, "get_spawn_diagnostics", diagnostics)
        srv = patched_server(browser_manager=manager)
        answer = await call_tool(
            srv, "spawn_browser", headless=headless, sandbox=False, **kwargs
        )
        return answer, resolved

    async def test_it_returns_the_running_instance_with_a_marker(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)
        session_lease.reset()

        answer, resolved = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(fleet),
            fleet,
            session="fleet",
        )

        assert answer["instance_id"] == "i-fleet"
        assert answer["already_running"] is True
        assert answer["session_lock"] == {"session": "fleet", "locked": False}
        assert resolved == [], "the resolver walked or cloned instead of reusing"
        assert not (tmp_session_root["sessions"] / "fleet-2").exists()

    async def test_the_marker_carries_the_lock_holder(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)
        session_lease.reset()
        await session_lease.acquire("fleet", "agent-a", 60, 0)

        answer, _ = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(fleet),
            fleet,
            session="fleet",
        )
        session_lease.reset()

        assert answer["session_lock"]["holder"] == "agent-a"

    async def test_an_unnamed_spawn_is_never_handed_it(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)

        answer, resolved = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(fleet),
            fleet,
        )

        assert answer["instance_id"] == "i-new"
        assert "already_running" not in answer
        assert resolved == [None]

    async def test_a_headed_ask_that_got_a_headless_browser_is_told_so(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        """`headless=False` is the tool default, so a bare call must report it
        too: the running browser is invisible and the caller wanted a window."""
        fleet = await _fleet(tmp_session_root)
        session_lease.reset()

        answer, _ = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(fleet),
            fleet,
            headless=False,
            session="fleet",
        )

        mismatch = answer["headless_mismatch"]
        assert mismatch["requested_headless"] is False
        assert mismatch["actual_headless"] is True
        assert "headed" in mismatch["warning"] and "headless" in mismatch["warning"]

    async def test_a_matching_headless_state_adds_nothing(
        self, call_tool, patched_server, monkeypatch, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)
        session_lease.reset()

        answer, _ = await self._spawn(
            call_tool,
            patched_server,
            monkeypatch,
            self._manager(fleet),
            fleet,
            session="fleet",
        )

        assert "headless_mismatch" not in answer

    def test_a_session_that_was_not_running_has_no_marker(self):
        assert fleet_session.reuse_answer(False, "/anywhere/fleet") == {}


class TestSeedingFromIt:
    async def test_a_new_session_takes_the_live_jar_when_we_drive_it(
        self, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)
        held_profile(fleet)

        selection = await _selection(
            session="job-a", seed_from="fleet", driven=_driving(fleet)
        )

        assert selection[clone_storage.LIVE_SEED_KEY] == str(fleet)
        assert selection["handed_over_from"] == "fleet"
        assert selection["seeded_from"] == "fleet"
        assert (Path(selection["user_data_dir"]) / COOKIE_JAR).read_bytes() == LOGIN

    async def test_a_new_session_is_a_persistent_one_not_a_disposable_clone(
        self, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)
        held_profile(fleet)

        selection = await _selection(
            session="job-a", seed_from="fleet", driven=_driving(fleet)
        )

        created = Path(selection["user_data_dir"])
        assert profile_seed.is_named(created)
        marker = json.loads(
            (created / ".stealth_chrome_devtools_mcp_clone.json").read_text("utf-8")
        )
        assert marker["seeded_from"] == "fleet"

    async def test_a_fleet_held_by_a_browser_we_do_not_drive_is_refused_by_name(
        self, tmp_session_root
    ):
        fleet = await _fleet(tmp_session_root)
        held_profile(fleet)

        with pytest.raises(ToolError, match="fleet"):
            await _selection(session="job-a", seed_from="fleet")

        assert not (tmp_session_root["sessions"] / "job-a").exists()


class TestAdoptingARunningBrowserAsTheFleet:
    async def test_a_running_named_session_becomes_the_fleet(self, tmp_session_root):
        work = tmp_session_root["sessions"] / "work"
        await _selection(session="work")
        (work / COOKIE_JAR).parent.mkdir(parents=True, exist_ok=True)
        (work / COOKIE_JAR).write_bytes(LOGIN)
        held_profile(work)

        selection = await _selection(
            session="fleet", seed_from="work", driven=_driving(work)
        )

        assert selection["seeded_from"] == "work"
        assert selection[clone_storage.LIVE_SEED_KEY] == str(work)
        assert (Path(selection["user_data_dir"]) / COOKIE_JAR).read_bytes() == LOGIN
        assert profile_seed.is_named(Path(selection["user_data_dir"]))

    async def test_a_walked_clone_name_is_an_accepted_source(self, tmp_session_root):
        """``upup-b9be57135713-84488-27`` is what an agent's running browser is
        called after a walk: a directory under the sessions root, so a NAME
        ``seed_from`` accepts. Pinned because the alternative -- refusing it --
        would leave the one browser a human has logged in to unadoptable."""
        master = tmp_session_root["master"]
        held_profile(master)
        walked = await _selection(driven=_driving(master))
        clone = Path(walked["user_data_dir"])
        assert clone.parent == tmp_session_root["sessions"]
        assert profile_seed.is_auto(clone)
        (clone / COOKIE_JAR).parent.mkdir(parents=True, exist_ok=True)
        (clone / COOKIE_JAR).write_bytes(LOGIN)
        held_profile(clone)

        selection = await _selection(
            session="fleet", seed_from=clone.name, driven=_driving(clone)
        )

        fleet = Path(selection["user_data_dir"])
        assert selection["seeded_from"] == clone.name
        assert (fleet / COOKIE_JAR).read_bytes() == LOGIN
        assert profile_seed.is_named(fleet)
        assert not profile_seed.is_auto(fleet)

    async def test_a_clone_driven_by_another_backend_is_refused_by_name(
        self, tmp_session_root
    ):
        master = tmp_session_root["master"]
        held_profile(master)
        walked = await _selection(driven=_driving(master))
        clone = Path(walked["user_data_dir"])
        held_profile(clone)

        with pytest.raises(ToolError, match=clone.name):
            await _selection(session="fleet", seed_from=clone.name)

        assert not (tmp_session_root["sessions"] / "fleet").exists()


class TestTheDefaultSeedSetting:
    @pytest.fixture
    def seeded_by_fleet(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_SEED_SESSION", "fleet")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    async def test_it_is_off_by_default(self, tmp_session_root):
        assert get_settings().seed_session == ""
        selection = await _selection(session="plain")
        assert selection["seeded_from"] == profile_seed.DEFAULT_SESSION

    async def test_a_new_named_session_is_copied_from_it(
        self, tmp_session_root, seeded_by_fleet
    ):
        fleet = tmp_session_root["sessions"] / "fleet"
        # Created while the setting is on: it is the configured session itself,
        # so it bootstraps from the snapshot instead of from itself.
        await _selection(session="fleet")
        (fleet / COOKIE_JAR).parent.mkdir(parents=True, exist_ok=True)
        (fleet / COOKIE_JAR).write_bytes(LOGIN)

        selection = await _selection(session="job-b")

        assert selection["seeded_from"] == "fleet"
        assert (Path(selection["user_data_dir"]) / COOKIE_JAR).read_bytes() == LOGIN

    async def test_the_configured_session_bootstraps_from_the_snapshot(
        self, tmp_session_root, seeded_by_fleet
    ):
        selection = await _selection(session="fleet")

        assert selection["seeded_from"] == profile_seed.DEFAULT_SESSION

    async def test_a_caller_seed_from_wins(self, tmp_session_root, seeded_by_fleet):
        await _selection(session="fleet")
        work = await _selection(session="work")
        assert work["seeded_from"] == "fleet"

        selection = await _selection(session="job-c", seed_from="work")

        assert selection["seeded_from"] == "work"

    async def test_a_missing_source_is_refused_naming_the_setting(
        self, tmp_session_root, seeded_by_fleet
    ):
        with pytest.raises(ToolError, match="STEALTH_MCP_SEED_SESSION"):
            await _selection(session="job-d")

        assert not (tmp_session_root["sessions"] / "job-d").exists()

    async def test_an_unnamed_spawn_on_a_free_shared_profile_is_unaffected(
        self, tmp_session_root, seeded_by_fleet
    ):
        """The shared profile is opened itself, not copied, so a setting that
        names a session that does not exist cannot break the common spawn."""
        selection = await _selection()

        assert selection["profile_role"] == profile_seed.DEFAULT_SESSION

    async def test_a_new_clone_is_copied_from_it_with_its_live_jar(
        self, tmp_session_root, seeded_by_fleet
    ):
        fleet = tmp_session_root["sessions"] / "fleet"
        await _selection(session="fleet")
        (fleet / COOKIE_JAR).parent.mkdir(parents=True, exist_ok=True)
        (fleet / COOKIE_JAR).write_bytes(LOGIN)
        master = tmp_session_root["master"]
        held_profile(master)
        held_profile(fleet)

        selection = await _selection(driven=_driving(master, fleet))

        clone = Path(selection["user_data_dir"])
        assert selection["profile_role"] == "clone"
        assert (clone / COOKIE_JAR).read_bytes() == LOGIN
        assert selection[clone_storage.LIVE_SEED_KEY] == str(fleet), (
            "the jar must come from the fleet, not from the master that was "
            "held when the clone was made"
        )

    @pytest.mark.parametrize("attempt", [0, 1], ids=["retry", "final"])
    async def test_a_retry_clone_is_seeded_from_it_not_from_the_snapshot(
        self, tmp_session_root, seeded_by_fleet, attempt
    ):
        """A spawn that failed once re-clones through the fallback. Without the
        setting honoured there it would quietly copy master-snapshot: a second
        way to seed, and a clone that has none of the fleet's login."""
        fleet = tmp_session_root["sessions"] / "fleet"
        await _selection(session="fleet")
        (fleet / COOKIE_JAR).parent.mkdir(parents=True, exist_ok=True)
        (fleet / COOKIE_JAR).write_bytes(LOGIN)
        previous = await clone_storage.resolve_profile_selection(None, force_clone=True)

        retry = await clone_storage._fallback_profile_selection(previous, attempt)

        assert retry is not None
        clone = Path(retry["user_data_dir"])
        assert clone != Path(previous["user_data_dir"])
        assert (clone / COOKIE_JAR).read_bytes() == LOGIN
        assert profile_seed.provenance(clone)["seeded_from"] == "fleet"

    def test_the_setting_is_a_name_not_a_path(self, monkeypatch, tmp_session_root):
        monkeypatch.setenv("STEALTH_MCP_SEED_SESSION", os.fspath(Path("/x/y")))
        get_settings.cache_clear()
        try:
            with pytest.raises(ToolError, match="NAME"):
                fleet_session.default_seed(None, None)
        finally:
            get_settings.cache_clear()
