"""Pins for F-888: a persistent named profile, and CDP re-attach after a restart.

Two halves, and the first is the reason the second is worth having.

**The persistence guarantees.** ``spawn_browser(user_data_dir=…)`` already
produced a directory nothing in the tree deletes; what it did not have was a
NAME for that property or a pin on it, so the guarantee was four separate
`if` statements that happened to agree. ``TestPersistenceGuarantees`` states the
four in one place — close, GC/quota, ``cleanup --apply``, ``kill-orphans`` — and
``TestOnePersistencePredicate`` pins that they are the SAME statement, because
three hand-written copies of one condition is how they come to disagree.

**The re-attach.** The incident: a backend went unresponsive, a replacement cold
started, and its startup orphan sweep killed every browser the dead one owned —
including a human's logged-in Seller Central session. The profile survived on
disk; the LOGIN did not, because a killed Chrome does not flush its session. So
the fix is not a better sweep, it is an adoption: a browser on a persistent
profile whose owner backend is gone is taken over, not reaped.

Everything here is hermetic. The record is a ``tmp_path`` file on every pin (the
only thing keeping a run off the developer's live ``~/.stealth-mcp``), both
liveness witnesses are injected, and the CDP door is patched — the real one is
pinned for SHAPE only, by reading the config nodriver would have been handed.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import browser_pid_registry as registry
from stealth_chrome_devtools_mcp.embedded import browser_reattach, desktop_launch
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

OUR_PID = 4242
DEAD_OWNER = 9001
LIVE_OWNER = 9002
CHROME_PID = 7777
PORT = 51234


def _entry(
    *,
    pid=CHROME_PID,
    owner_pid=DEAD_OWNER,
    user_data_dir=r"C:\profiles\seller-central",
    auto_clone=False,
    uses_custom_data_dir=True,
    cdp_port=PORT,
):
    """One recorded browser, in the shape ``normalize_entries`` yields."""
    return {
        "pid": pid,
        "create_time": 1700000000.0,
        "user_data_dir": user_data_dir,
        "uses_custom_data_dir": uses_custom_data_dir,
        "auto_clone": auto_clone,
        "cdp_port": cdp_port,
        "timestamp": 0,
        "owner_pid": owner_pid,
        "owner_create_time": 1699999000.0,
    }


def _owner_alive(pid, _create_time):
    """Only LIVE_OWNER is a backend of ours that is still running."""
    return pid == LIVE_OWNER


def _browser_alive(pid, _create_time):
    """Only CHROME_PID is still the Chrome we recorded."""
    return pid == CHROME_PID


def _cleanup(tmp_path: Path) -> ProcessCleanup:
    """A ProcessCleanup whose record is in tmp_path, built without __init__.

    The same helper shape ``test_browser_pid_registry.py`` uses, and for the same
    reason: everything here writes, so nothing may reach the real state dir.
    """
    pc = ProcessCleanup.__new__(ProcessCleanup)
    pc.pid_file = tmp_path / "browser_pids.json"
    pc.tracked_pids = set()
    pc.browser_processes = {}
    pc.orphan_profile_max_age_seconds = 0
    pc._init_time = 1700000100.0
    return pc


def _seed(path: Path, entries: dict) -> None:
    path.write_text(json.dumps({"browser_processes": entries, "timestamp": 0}))


def _read(path: Path) -> dict:
    return json.loads(path.read_text())["browser_processes"]


# ---------------------------------------------------------------------------
# What a named profile guarantees, stated once
# ---------------------------------------------------------------------------


class TestPersistenceGuarantees:
    """The four things ``user_data_dir=<name>`` promises, each pinned at the one
    function that could break it."""

    def test_a_close_does_not_delete_the_directory(self, tmp_path):
        """(a) Deletion on close: never, for a persistent profile.

        ``close_instance`` reaches the delete through
        ``_cleanup_profile_for_metadata``; a persistent entry is refused there
        before the path is even looked at.
        """
        profile = tmp_path / "seller-central"
        profile.mkdir()
        pc = _cleanup(tmp_path)
        assert (
            pc._cleanup_profile_for_metadata("i-1", _entry(user_data_dir=str(profile)))
            is False
        )
        assert profile.exists()

    def test_a_close_still_deletes_a_disposable_clone(self, tmp_path):
        """The other side of (a): the guarantee must not have widened into
        "nothing is ever deleted", which would leak every auto-clone."""
        profile = tmp_path / "auto-clone"
        profile.mkdir()
        pc = _cleanup(tmp_path)
        assert (
            pc._cleanup_profile_for_metadata(
                "i-1", _entry(user_data_dir=str(profile), auto_clone=True)
            )
            is True
        )
        assert not profile.exists()

    def test_b_the_storage_cap_never_selects_a_named_profile(self, tmp_path):
        """(b) GC/quota: the cap reclaims auto-clones only.

        Disposability is an explicit ``auto_clean`` flag in the clone marker, so
        a named profile — which never gets one — cannot be selected however large
        it is. Pinned through ``clone_is_auto``, the one predicate the sweep asks.
        """
        from stealth_chrome_devtools_mcp.embedded import clone_storage

        named = tmp_path / "github-session"
        named.mkdir()
        (named / ".stealth_chrome_devtools_mcp_clone.json").write_text(
            json.dumps({"source_kind": "explicit-master-snapshot"}), encoding="utf-8"
        )
        assert clone_storage.clone_is_auto(named) is False
        assert clone_storage.clone_is_named(named) is True

    def test_c_cleanup_apply_trims_caches_and_keeps_session_state(self, tmp_path):
        """(c) ``cleanup --apply``: a named profile is TRIMMED, never removed.

        The directory and every session-state file survive; only regenerable
        caches go, which Chrome rebuilds on the next launch.
        """
        from stealth_chrome_devtools_mcp.embedded import clone_storage

        named = tmp_path / "github-session"
        (named / "Default").mkdir(parents=True)
        (named / "Default" / "Cookies").write_bytes(b"login")
        (named / "Default" / "Cache").mkdir()
        (named / "Default" / "Cache" / "big").write_bytes(b"x" * 64)

        clone_storage._trim_profile_regenerable(named)

        assert named.exists()
        assert (named / "Default" / "Cookies").read_bytes() == b"login"
        assert not (named / "Default" / "Cache").exists()

    def test_d_kill_orphans_does_not_delete_the_directory(self, tmp_path):
        """(d) ``kill-orphans``: it kills the browser — that is the verb — but the
        profile stays, so the login is still there for the next spawn."""
        profile = tmp_path / "seller-central"
        profile.mkdir()
        pc = _cleanup(tmp_path)
        _seed(pc.pid_file, {"i-1": _entry(user_data_dir=str(profile))})

        with patch.object(pc, "_kill_processes_for_metadata", return_value=True):
            pc.recover_orphans(force=True)

        assert profile.exists()


class TestOnePersistencePredicate:
    """One condition, one home. Three hand-written copies is how the delete
    guard, the untrack decision and the shutdown spare come to disagree about
    one browser — and a directory spared whose entry is dropped is a live Chrome
    nothing on disk can find again."""

    def test_the_predicate_answers_both_ways(self):
        assert registry.on_persistent_profile(_entry()) is True
        assert registry.on_persistent_profile(_entry(auto_clone=True)) is False
        assert (
            registry.on_persistent_profile(_entry(uses_custom_data_dir=None)) is False
        )

    def test_a_legacy_entry_is_not_persistent(self):
        """An entry written before the flag existed reads as NOT persistent —
        2.0.3's behaviour, and the safe direction: the worst case is a temp
        profile reclaimed, where the other way round is a named profile kept
        alive forever by an adoption that should never have happened."""
        legacy = registry.normalize_entries({"i-1": 4242})
        assert registry.on_persistent_profile(legacy["i-1"]) is False

    def test_process_cleanup_spells_the_condition_nowhere(self):
        """The literal pair is gone from the module that had it three times."""
        source = Path(
            ProcessCleanup.__module__.replace(".", "/")
        )  # only for the message
        text = Path(
            __import__(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup",
                fromlist=["__file__"],
            ).__file__
        ).read_text(encoding="utf-8")
        assert 'uses_custom_data_dir") is True' not in text, (
            f"{source}: the persistence condition must be asked through "
            "browser_pid_registry.on_persistent_profile, never re-spelled"
        )


# ---------------------------------------------------------------------------
# The adoption rule
# ---------------------------------------------------------------------------


class TestAdoptionRule:
    """Four conditions, and each one alone is enough to refuse."""

    def _adoptable(self, entries):
        return browser_reattach.adoptable(
            entries, owner_alive=_owner_alive, browser_alive=_browser_alive
        )

    def test_a_dead_owners_persistent_running_browser_is_adoptable(self):
        found = self._adoptable({"i-1": _entry()})
        assert set(found) == {"i-1"}
        assert found["i-1"].pid == CHROME_PID
        assert found["i-1"].port == PORT

    def test_a_live_backend_of_ours_keeps_its_browser(self):
        """Two backends driving one Chrome is F-886's harm reached from the other
        side. Recovering a browser under a LIVE backend needs the operator to
        stop that backend first — it is not ours to take."""
        assert self._adoptable({"i-1": _entry(owner_pid=LIVE_OWNER)}) == {}

    def test_a_disposable_clone_is_never_adopted(self):
        """Its whole contract is that it dies with its browser; adopting one
        would keep a throwaway profile alive forever."""
        assert self._adoptable({"i-1": _entry(auto_clone=True)}) == {}

    def test_a_dead_chrome_is_not_adopted(self):
        assert self._adoptable({"i-1": _entry(pid=12345)}) == {}

    def test_no_endpoint_means_not_adoptable(self, tmp_path):
        """A candidate we cannot reach must be refused HERE, before the reaper is
        told to skip it — otherwise it can be neither adopted nor reaped and
        leaks forever."""
        entry = _entry(cdp_port=None, user_data_dir=str(tmp_path / "empty"))
        with patch.object(browser_reattach, "_port_from_cmdline", return_value=None):
            assert self._adoptable({"i-1": entry}) == {}

    def test_one_bad_entry_does_not_lose_the_others(self):
        """Classification never raises: a malformed entry is left out and every
        other candidate is still found."""
        found = self._adoptable({"bad": {"pid": "not-an-int"}, "i-1": _entry()})
        assert set(found) == {"i-1"}


class TestEndpointLadder:
    """Three witnesses, most trusted first. The last two exist for the 2.1.8 /
    2.1.9 records that carry today's stranded logins and no port at all."""

    def test_the_recorded_port_wins(self, tmp_path):
        profile = tmp_path / "p"
        profile.mkdir()
        (profile / browser_reattach.DEVTOOLS_PORT_FILE).write_text("9999\n/devtools/x")
        entry = _entry(user_data_dir=str(profile), cdp_port=PORT)
        assert browser_reattach.endpoint(entry) == PORT

    def test_a_legacy_entry_falls_back_to_chromes_own_file(self, tmp_path):
        """``DevToolsActivePort``: Chrome's own record of the port it bound,
        first line, inside the profile it was launched on."""
        profile = tmp_path / "p"
        profile.mkdir()
        (profile / browser_reattach.DEVTOOLS_PORT_FILE).write_text(
            "9999\n/devtools/browser/abc\n"
        )
        entry = _entry(user_data_dir=str(profile), cdp_port=None)
        assert browser_reattach.endpoint(entry) == 9999

    def test_then_the_command_line(self, tmp_path):
        entry = _entry(user_data_dir=str(tmp_path / "gone"), cdp_port=None)
        with patch.object(browser_reattach, "_port_from_cmdline", return_value=8123):
            assert browser_reattach.endpoint(entry) == 8123

    @pytest.mark.parametrize("cmdline_port", ["0", "70000", "nonsense", ""])
    def test_an_unusable_port_is_no_port(self, tmp_path, cmdline_port):
        """0 is "not bound yet", not a port: Chrome resolves an ``=0`` request to
        a real number before it writes the file."""
        profile = tmp_path / "p"
        profile.mkdir()
        (profile / browser_reattach.DEVTOOLS_PORT_FILE).write_text(cmdline_port)
        entry = _entry(user_data_dir=str(profile), cdp_port=None)
        with patch.object(browser_reattach, "_port_from_cmdline", return_value=None):
            assert browser_reattach.endpoint(entry) is None

    def test_a_hand_edited_boolean_is_not_port_one(self):
        """``bool`` is an ``int`` subclass; a recorded ``true`` must not read as
        port 1 and send an attach at whatever holds it."""
        assert registry.recorded_port({"cdp_port": True}) is None

    def test_the_cmdline_reader_takes_both_spellings(self):
        for cmdline, expected in (
            (["chrome", "--remote-debugging-port=51234"], 51234),
            (["chrome", "--remote-debugging-port", "51234"], 51234),
            (["chrome", "--headless"], None),
        ):
            with patch.object(
                browser_reattach.psutil,
                "Process",
                return_value=SimpleNamespace(cmdline=lambda c=cmdline: c),
            ):
                assert browser_reattach._port_from_cmdline(CHROME_PID) == expected


# ---------------------------------------------------------------------------
# The reaper, split
# ---------------------------------------------------------------------------


class TestRecoverySparesAdoptable:
    def _recover(self, pc, **kwargs):
        # The browser-side witness is patched at ITS home (a module function
        # taking the cleanup) rather than on the object, because that is where
        # `adoptable_for` reads it from — the same reason `singleton`'s thin
        # bindings are the suite's patch surface for the backend probes.
        with (
            patch.object(pc, "_owner_backend_alive", side_effect=_owner_alive),
            patch.object(
                browser_reattach,
                "recorded_browser_alive",
                side_effect=lambda _c, pid, ctime: _browser_alive(pid, ctime),
            ),
            patch.object(pc, "_kill_processes_for_metadata", return_value=True) as kill,
            patch.object(pc, "_cleanup_profile_for_metadata", return_value=True),
        ):
            pc._recover_orphaned_processes(**kwargs)
        return kill

    def test_an_adoptable_browser_is_neither_killed_nor_forgotten(self, tmp_path):
        """Left RUNNING and left TRACKED. Dropping the entry here would leave a
        live Chrome nothing on disk can find, which is the same loss by a quieter
        route."""
        pc = _cleanup(tmp_path)
        _seed(pc.pid_file, {"keep": _entry()})

        kill = self._recover(pc)

        assert kill.call_count == 0
        assert set(_read(pc.pid_file)) == {"keep"}

    def test_a_plain_orphan_is_still_reaped(self, tmp_path):
        """The sweep this finding narrows must still do its job: a disposable
        clone left by a dead backend is pure leak."""
        pc = _cleanup(tmp_path)
        _seed(pc.pid_file, {"drop": _entry(auto_clone=True)})

        kill = self._recover(pc)

        assert kill.call_count == 1
        assert _read(pc.pid_file) == {}

    def test_both_verdicts_in_one_pass(self, tmp_path):
        """The case a real machine hits, and the one a per-entry classification
        would get wrong in exactly one direction."""
        pc = _cleanup(tmp_path)
        _seed(
            pc.pid_file,
            {
                "keep": _entry(),
                "drop": _entry(auto_clone=True),
                "mine": _entry(owner_pid=LIVE_OWNER),
            },
        )

        kill = self._recover(pc)

        assert kill.call_count == 1
        assert set(_read(pc.pid_file)) == {"keep", "mine"}

    def test_force_takes_everything(self, tmp_path):
        """``kill-orphans --force`` has always meant "yes, including a live
        backend's". An operator asking IS the authority the adoption rule
        otherwise supplies."""
        pc = _cleanup(tmp_path)
        _seed(pc.pid_file, {"keep": _entry(), "mine": _entry(owner_pid=LIVE_OWNER)})

        kill = self._recover(pc, force=True)

        assert kill.call_count == 2
        assert _read(pc.pid_file) == {}


class TestShutdownHandsOver:
    """``stop`` and ``restart`` are where the incident's browsers died. A browser
    on a persistent profile is handed to the next backend instead."""

    def test_a_persistent_browser_survives_shutdown_with_its_entry(self, tmp_path):
        pc = _cleanup(tmp_path)
        pc.browser_processes = {"keep": _entry()}

        with patch.object(pc, "kill_browser_process") as kill:
            pc._cleanup_all_tracked()

        assert kill.call_count == 0
        assert set(_read(pc.pid_file)) == {"keep"}

    def test_a_clone_is_still_killed_at_shutdown(self, tmp_path):
        pc = _cleanup(tmp_path)
        pc.browser_processes = {"drop": _entry(auto_clone=True)}

        with patch.object(pc, "kill_browser_process", return_value=True) as kill:
            pc._cleanup_all_tracked()

        assert kill.call_count == 1


# ---------------------------------------------------------------------------
# The adoption itself
# ---------------------------------------------------------------------------


def _adoptable_record(profile="C:/profiles/seller-central"):
    return browser_reattach.Adoptable(
        instance_id="i-kept", pid=CHROME_PID, user_data_dir=profile, port=PORT
    )


class TestManagerAdoption:
    @pytest.fixture
    def manager(self):
        return BrowserManager()

    @pytest.fixture
    def cleanup(self):
        """The ProcessCleanup the pass takes as an ARGUMENT.

        A double rather than the real thing, because every call the pass makes on
        it writes: ``track_browser_process`` re-stamps the record and
        ``_drop_recorded`` rewrites it. Passing it in is the whole point of the
        seam — nothing here can reach the developer's live ``~/.stealth-mcp``.
        """
        return MagicMock()

    @pytest.mark.asyncio
    async def test_the_client_keeps_its_instance_id_and_gets_the_live_page(
        self, manager, cleanup
    ):
        """The point of the whole finding, from the client's side: the id it was
        holding before the restart still names its browser, and what that browser
        is SHOWING is read live (F-874), never the cached pair — which for an
        adopted instance is empty."""
        tab = FakeTab(url="https://sellercentral.amazon.com/home")
        tab.target.title = "Seller Central"
        # ``alive=True`` so ``get_instance``'s liveness gate keeps the adopted
        # record: this pin is about what the CLIENT sees afterwards, and a fake
        # whose pid psutil cannot find would be discarded before it could look.
        browser = FakeBrowser(alive=True, pid=CHROME_PID, main_tab=tab)
        candidate = _adoptable_record()

        with (
            patch.object(
                browser_reattach, "attach_config", return_value=SimpleNamespace()
            ),
            patch.object(browser_reattach, "attach", return_value=browser),
            patch.object(
                browser_reattach, "adoptable_for", return_value={"i-kept": candidate}
            ),
        ):
            adopted = await browser_reattach.run(manager, cleanup)

        assert adopted == ["i-kept"]
        listed = await manager.list_instances()
        assert [i.instance_id for i in listed] == ["i-kept"]
        instance = listed[0]
        assert instance.last_navigated_url == "https://sellercentral.amazon.com/home"
        assert instance.last_navigated_title == "Seller Central"
        assert await manager.get_tab("i-kept") is tab

    @pytest.mark.asyncio
    async def test_adoption_restamps_the_owner_through_the_one_write(
        self, manager, cleanup
    ):
        """Ownership moves to us by re-tracking, not by a second write protocol —
        which is also what makes the pass idempotent: the next session's
        classification no longer sees the entry as orphaned."""
        tab = FakeTab()
        browser = FakeBrowser(alive=None, pid=CHROME_PID, main_tab=tab)

        with (
            patch.object(
                browser_reattach, "attach_config", return_value=SimpleNamespace()
            ),
            patch.object(browser_reattach, "attach", return_value=browser),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value={"i-kept": _adoptable_record()},
            ),
        ):
            await browser_reattach.run(manager, cleanup)

        assert cleanup.track_browser_process.call_count == 1
        kwargs = cleanup.track_browser_process.call_args.kwargs
        assert kwargs["cdp_port"] == PORT
        assert kwargs["auto_clone"] is False
        assert kwargs["uses_custom_data_dir"] is True

    @pytest.mark.asyncio
    async def test_a_failed_attach_falls_back_to_the_reap(self, manager, cleanup):
        """Strictly 2.1.9's behaviour for that entry, not a new leak: an orphan we
        cannot adopt still reaches exactly one of the two ends the old sweep had.
        The reap is handed the two keys that spare the DIRECTORY, so the login is
        still on disk for the next spawn."""
        with (
            patch.object(
                browser_reattach, "attach_config", return_value=SimpleNamespace()
            ),
            patch.object(
                browser_reattach, "attach", side_effect=ConnectionRefusedError("no")
            ),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value={"i-kept": _adoptable_record()},
            ),
            patch.object(browser_reattach, "reap_recorded") as reap,
        ):
            adopted = await browser_reattach.run(manager, cleanup)

        assert adopted == []
        assert reap.call_count == 1
        reaped = reap.call_args.args[2]
        assert reaped["uses_custom_data_dir"] is True
        assert reaped["auto_clone"] is False
        cleanup._drop_recorded.assert_called_once_with({"i-kept"})

    @pytest.mark.asyncio
    async def test_an_attached_browser_with_no_tab_is_refused(self, manager, cleanup):
        """Half an instance is worse than none: a tool body must never find an
        instance whose tab is None."""
        browser = FakeBrowser(alive=None, pid=CHROME_PID, main_tab=None)

        with (
            patch.object(
                browser_reattach, "attach_config", return_value=SimpleNamespace()
            ),
            patch.object(browser_reattach, "attach", return_value=browser),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value={"i-kept": _adoptable_record()},
            ),
            patch.object(browser_reattach, "reap_recorded"),
        ):
            adopted = await browser_reattach.run(manager, cleanup)

        assert adopted == []
        assert "i-kept" not in manager._instances

    @pytest.mark.asyncio
    async def test_an_already_registered_instance_is_left_alone(self, manager, cleanup):
        """Idempotence at the second door: ``app_lifespan`` may drive this more
        than once, and re-attaching a browser we already hold would leak a
        connection and replace a live instance record."""
        manager._instances["i-kept"] = {"browser": object(), "tab": object()}

        with (
            patch.object(browser_reattach, "attach") as attach,
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value={"i-kept": _adoptable_record()},
            ),
        ):
            adopted = await browser_reattach.run(manager, cleanup)

        assert adopted == []
        assert attach.call_count == 0

    @pytest.mark.asyncio
    async def test_a_wedged_attach_is_bounded_and_reaped(self, manager, cleanup):
        """One browser that never answers costs its own budget and nothing else;
        it may not hang a backend's startup, which is the failure F-856 removed
        from this exact path."""

        async def _never(*_args, **_kwargs):
            await asyncio.Event().wait()

        with (
            patch.object(browser_reattach, "ATTACH_BUDGET_SECONDS", 0.05),
            patch.object(
                browser_reattach, "attach_config", return_value=SimpleNamespace()
            ),
            patch.object(browser_reattach, "attach", side_effect=_never),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value={"i-kept": _adoptable_record()},
            ),
            patch.object(browser_reattach, "reap_recorded") as reap,
        ):
            adopted = await browser_reattach.run(manager, cleanup)

        assert adopted == []
        assert reap.call_count == 1

    @pytest.mark.asyncio
    async def test_the_pass_is_driven_from_app_lifespan_with_both_collaborators(self):
        """The one wiring pin: ``server.py`` hands the pass BOTH objects, because
        neither is importable from ``browser_reattach`` — ``process_cleanup``
        imports it, so the seam only works in this direction."""
        source = Path(Path(browser_reattach.__file__).parent / "server.py").read_text(
            encoding="utf-8"
        )
        assert (
            "rt.browser_reattach.start(rt.browser_manager, rt.process_cleanup)"
            in source
        )


# ---------------------------------------------------------------------------
# One door
# ---------------------------------------------------------------------------


class TestOneDoor:
    def test_the_config_carries_host_and_port_and_the_profile(self):
        """Setting BOTH is nodriver's ``connect_existing`` gate; the profile dir
        rides along because ``browser.config.user_data_dir`` is what the spawn
        pipeline reads back to decide profile cleanup."""
        config = browser_reattach.attach_config(r"C:\profiles\p", PORT)
        assert config.host == browser_reattach.CDP_HOST
        assert config.port == PORT
        assert str(config.user_data_dir).endswith("p")
        assert config.uses_custom_data_dir is True

    def test_desktop_launch_uses_that_one_door(self):
        """F-810's delegated launch was standing in this door first; it is now
        this module's second consumer rather than a second door, so the two
        cannot drift on how a running browser is entered."""
        source = Path(desktop_launch.__file__).read_text(encoding="utf-8")
        assert "browser_reattach.attach_config(" in source
        assert "browser_reattach.attach(" in source
        assert "config.host =" not in source
        assert "uc.start(" not in source
