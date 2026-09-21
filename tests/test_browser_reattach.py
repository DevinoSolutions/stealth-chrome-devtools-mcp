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
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import (
    browser_claim,
    browser_cmdline,
    browser_reattach,
    cdp_attach,
    desktop_launch,
)
from stealth_chrome_devtools_mcp.embedded import browser_pid_registry as registry
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
        from stealth_chrome_devtools_mcp.embedded import profile_copy

        named = tmp_path / "github-session"
        (named / "Default").mkdir(parents=True)
        (named / "Default" / "Cookies").write_bytes(b"login")
        (named / "Default" / "Cache").mkdir()
        (named / "Default" / "Cache" / "big").write_bytes(b"x" * 64)

        profile_copy.trim_regenerable(named)

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

    def _classified(self, entries):
        return browser_reattach.adoptable(
            entries, owner_alive=_owner_alive, browser_alive=_browser_alive
        )

    def _adoptable(self, entries):
        return self._classified(entries).adoptable

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
        with patch.object(browser_cmdline, "debug_port", return_value=None):
            assert self._adoptable({"i-1": entry}) == {}

    def test_one_bad_entry_does_not_lose_the_others(self):
        """Classification never raises: a malformed entry is left out and every
        other candidate is still found."""
        found = self._adoptable({"bad": {"pid": "not-an-int"}, "i-1": _entry()})
        assert set(found) == {"i-1"}

    def test_an_unclassifiable_entry_is_spared_and_never_adopted(self):
        """F-888 review M1. The two answers are separate on purpose: an entry the
        rule could not reason about must not be reaped (it may be a human's
        logged-in Chrome) AND must not be attached to (we understood nothing
        about it). It used to be a bare ``contextlib.suppress``, which dropped it
        out of the spared set entirely and let recovery kill it in silence.
        """
        boom = MagicMock()
        boom.get.side_effect = RuntimeError("unreadable")
        classified = self._classified({"boom": boom, "i-1": _entry()})
        assert classified.unclassifiable == {"boom"}
        assert set(classified.adoptable) == {"i-1"}
        assert classified.spare == {"boom", "i-1"}

    def test_a_recorded_browser_with_no_create_time_is_not_adopted(self):
        """F-888 review M4. Reaping tolerates a missing create_time — skipping a
        recycled pid only leaks — but ADOPTION would take over a stranger's
        chrome.exe holding that pid, stamp our ownership on it and kill it at
        close_instance. Both halves of the identity, or the holder path instead.
        """
        entry = _entry()
        entry["create_time"] = None
        assert self._adoptable({"i-1": entry}) == {}


class TestEndpointLadder:
    """Three witnesses, most trusted first. The last two exist for the 2.1.8 /
    2.1.9 records that carry today's stranded logins and no port at all."""

    def test_the_recorded_port_wins(self, tmp_path):
        profile = tmp_path / "p"
        profile.mkdir()
        (profile / browser_reattach.DEVTOOLS_PORT_FILE).write_text("9999\n/devtools/x")
        entry = _entry(user_data_dir=str(profile), cdp_port=PORT)
        assert browser_reattach.endpoint(entry) == PORT

    def test_then_the_command_line(self, tmp_path):
        """Second rung: definitionally the live process's own port."""
        entry = _entry(user_data_dir=str(tmp_path / "gone"), cdp_port=None)
        with patch.object(browser_cmdline, "debug_port", return_value=8123):
            assert browser_reattach.endpoint(entry) == 8123

    def test_the_command_line_outranks_chromes_file(self, tmp_path):
        """Ordering, stated as a pin rather than left to the reader. The file
        outlives the browser that wrote it; the command line cannot."""
        profile = tmp_path / "p"
        profile.mkdir()
        (profile / browser_reattach.DEVTOOLS_PORT_FILE).write_text("9999\n/devtools/x")
        entry = _entry(user_data_dir=str(profile), cdp_port=None)
        with patch.object(browser_cmdline, "debug_port", return_value=8123):
            assert browser_reattach.endpoint(entry) == 8123

    def test_a_legacy_entry_falls_back_to_chromes_own_file(self, tmp_path):
        """Last rung, and it still earns its place: a caller passing
        ``--remote-debugging-port=0`` has a command line that names no usable
        port, and the file is where Chrome wrote the one it resolved that to.

        ``browser_cmdline.debug_port`` is patched rather than left to the real process
        table — the recorded pid is a literal, and on a busy machine it may name
        a real process whose command line would decide this assertion.
        """
        profile = tmp_path / "p"
        profile.mkdir()
        (profile / browser_reattach.DEVTOOLS_PORT_FILE).write_text(
            "9999\n/devtools/browser/abc\n"
        )
        entry = _entry(user_data_dir=str(profile), cdp_port=None)
        with patch.object(browser_cmdline, "debug_port", return_value=None):
            assert browser_reattach.endpoint(entry) == 9999

    @pytest.mark.parametrize("cmdline_port", ["0", "70000", "nonsense", ""])
    def test_an_unusable_port_is_no_port(self, tmp_path, cmdline_port):
        """0 is "not bound yet", not a port: Chrome resolves an ``=0`` request to
        a real number before it writes the file."""
        profile = tmp_path / "p"
        profile.mkdir()
        (profile / browser_reattach.DEVTOOLS_PORT_FILE).write_text(cmdline_port)
        entry = _entry(user_data_dir=str(profile), cdp_port=None)
        with patch.object(browser_cmdline, "debug_port", return_value=None):
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
                browser_cmdline.psutil,
                "Process",
                return_value=SimpleNamespace(cmdline=lambda c=cmdline: c),
            ):
                assert browser_cmdline.debug_port(CHROME_PID) == expected


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


def _classified(**adoptable):
    """What ``adoptable_for`` answers: two sets, never a bare dict."""
    return browser_reattach.Classified(adoptable=adoptable, unclassifiable=set())


class TestManagerAdoption:
    @pytest.fixture
    def manager(self):
        return BrowserManager()

    @pytest.fixture
    def cleanup(self, tmp_path):
        """The ProcessCleanup the pass takes as an ARGUMENT.

        A double rather than the real thing, because every call the pass makes on
        it writes: ``track_browser_process`` re-stamps the record and
        ``_drop_recorded`` rewrites it. Passing it in is the whole point of the
        seam — nothing here can reach the developer's live ``~/.stealth-mcp``.

        ``pid_file`` is a REAL tmp_path file, because the claim taken before the
        door is a real locked read-merge-write and stubbing it out would leave
        the one thing that makes adoption safe between processes unexercised.
        """
        double = MagicMock()
        double.pid_file = tmp_path / "browser_pids.json"
        double._owner_backend_alive.return_value = False
        return double

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
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach", return_value=browser),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=_classified(**{"i-kept": candidate}),
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
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach", return_value=browser),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=_classified(**{"i-kept": _adoptable_record()}),
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
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(
                cdp_attach, "attach", side_effect=ConnectionRefusedError("no")
            ),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=_classified(**{"i-kept": _adoptable_record()}),
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
        instance with no usable tab.

        The refusal is on the shape nodriver ACTUALLY produces — ``main_tab`` is
        ``sorted(self.targets, …)[0]``, so an attached browser with no targets
        raises ``IndexError`` and never answers None (F-888 review L2, and the
        fake raises to match).
        """
        browser = FakeBrowser(alive=None, pid=CHROME_PID, main_tab=None)

        with (
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach", return_value=browser),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=_classified(**{"i-kept": _adoptable_record()}),
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
            patch.object(cdp_attach, "attach") as attach,
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=_classified(**{"i-kept": _adoptable_record()}),
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
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach", side_effect=_never),
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=_classified(**{"i-kept": _adoptable_record()}),
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
# The browser with NO record entry — the one the record-driven pass cannot see
# ---------------------------------------------------------------------------


class TestHeldProfileAdoption:
    """The measured shape of the real stranded login: Chrome alive on
    ``…\\amazon-buy-bot\\seller-central-profile`` with ``--remote-debugging-port=9223``,
    owner backend 173824 gone, **no entry in browser_pids.json at all** (the
    successor backend rewrote the record without it), and **no
    ``DevToolsActivePort`` file** in the profile.

    ``run`` walks entries, so it would walk past this browser forever. The only
    thing that still names it is the directory a caller passes to
    ``spawn_browser(user_data_dir=…)``.
    """

    HELD = r"C:\Users\x\AppData\Local\amazon-buy-bot\seller-central-profile"

    def _browser_argv(self):
        return [
            "chrome.exe",
            f"--user-data-dir={self.HELD}",
            "--remote-debugging-port=9223",
        ]

    def _child_argv(self, kind="renderer"):
        """A CHILD of that browser: same profile, a ``--type``, and — measured on
        Chrome 153 — the debugging port too for a renderer, which is why the
        port cannot be the thing that identifies the browser."""
        return [*self._browser_argv(), f"--type={kind}"]

    def _held(
        self, *, entries=None, hold_pid=CHROME_PID, cmdline_port=9223, table=None
    ):
        """`held_by` with every witness injected and the process table faked.

        *table* is the live process tree on that profile, pid -> argv. The
        default is one browser process, which is what the simple pins want; a
        pin about WHICH member gets adopted supplies its own.
        """
        table = table if table is not None else {CHROME_PID: self._browser_argv()}
        hold = (
            SimpleNamespace(pid=hold_pid, reason="process")
            if hold_pid is not None
            else None
        )
        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.profile_lock.profile_hold",
                return_value=hold,
            ),
            patch.object(
                browser_cmdline, "arguments", side_effect=lambda pid: table.get(pid, [])
            ),
            patch.object(browser_cmdline, "debug_port", return_value=cmdline_port),
            patch.object(browser_cmdline, "dead_local_proxy", return_value=None),
        ):
            return browser_reattach.held_by(
                self.HELD,
                read_entries=lambda: entries if entries is not None else {},
                owner_alive=_owner_alive,
                live_pids=lambda _dir: set(table),
                new_instance_id="i-new",
            )

    def test_a_holder_with_no_entry_at_all_is_adoptable(self):
        """The incident's exact shape. An empty record must not mean "nothing to
        adopt" — it is how the successor backend left the machine."""
        found = self._held(entries={})
        assert found is not None
        assert found.pid == CHROME_PID
        assert found.port == 9223
        assert found.user_data_dir == self.HELD
        # Nothing recorded an id for it, so the caller's minted one is used.
        assert found.instance_id == "i-new"

    def test_the_port_comes_from_the_holders_command_line(self):
        """No record and no ``DevToolsActivePort`` file — measured true of the
        stranded Chrome — leaves the process table as the only witness."""
        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.profile_lock.profile_hold",
                return_value=SimpleNamespace(pid=CHROME_PID, reason="process"),
            ),
            patch.object(
                browser_cmdline.psutil,
                "Process",
                return_value=SimpleNamespace(cmdline=self._browser_argv),
            ),
        ):
            found = browser_reattach.held_by(
                self.HELD,
                read_entries=dict,
                owner_alive=_owner_alive,
                live_pids=lambda _dir: {CHROME_PID},
                new_instance_id="i-new",
            )
        assert found is not None and found.port == 9223

    def test_the_browser_is_adopted_when_the_witness_names_a_child(self):
        """MEASURED, on a real spawn: eleven processes on one profile — the
        browser, six renderers, two utilities, a gpu-process and a
        crashpad-handler — and ``profile_hold``'s witness is a SET, so the pid it
        reports is whichever member iterated first.

        Adopting that member would be wrong in two ways at once: a ``utility``
        child carries no ``--remote-debugging-port`` at all (so the adoption
        declined, and did it silently), and a ``renderer`` DOES carry one (so it
        would have been adopted, stamped onto ``Browser._process_pid``, discarded
        by the manager the moment that renderer recycled, and killed instead of
        the browser by ``close_instance``). The browser is the process with no
        ``--type``, and nothing else.
        """
        renderer, utility = 41001, 41002
        found = self._held(
            hold_pid=utility,
            table={
                utility: self._child_argv("utility"),
                renderer: self._child_argv("renderer"),
                CHROME_PID: self._browser_argv(),
            },
        )
        assert found is not None
        assert found.pid == CHROME_PID, (
            "adoption must target the browser process, not whichever member of "
            "its process tree the holding witness happened to name"
        )

    def test_a_tree_with_no_browser_process_refuses_with_a_reason(self):
        """Every member has a ``--type``, so none of them is the browser. There
        is nothing safe to attach to and the caller must be TOLD — this is a live
        browser that was left alone, not an empty directory."""
        with pytest.raises(browser_reattach.Refused, match="the browser itself"):
            self._held(hold_pid=41002, table={41002: self._child_argv("gpu-process")})

    def test_a_dead_owners_entry_donates_its_instance_id(self):
        """When the record DOES still name it, the client's id is preserved
        rather than a new one minted — the same promise `run` makes."""
        found = self._held(entries={"i-was": _entry(owner_pid=DEAD_OWNER)})
        assert found is not None and found.instance_id == "i-was"

    def test_a_live_backends_browser_is_never_taken(self):
        """F-886 from the other side. The refusal is `is_reapable`, the same one
        `run` asks, so the two entry points cannot disagree.

        It RAISES rather than answering None, and the distinction is what the
        caller reports: every other None here means "an ordinary spawn, nothing
        to say", while this one means "your browser is alive, we did not touch
        it, and the remedy is to stop that backend".
        """
        with pytest.raises(browser_reattach.Refused, match="already owns the browser"):
            self._held(entries={"theirs": _entry(owner_pid=LIVE_OWNER)})

    def test_nothing_holding_the_directory_is_not_adoptable(self):
        """The ordinary case by far: the caller gets a normal spawn."""
        assert self._held(hold_pid=None) is None

    def test_a_holder_we_cannot_name_a_port_for_refuses_with_a_reason(self):
        """A browser IS there and we cannot get in. The spawn still goes ahead —
        this path never reaps — but "no endpoint" must not arrive at the caller
        as the same silence "nothing holds this directory" produces: the whole
        point of the decline is to tell an operator that the login they were
        reaching for is still running."""
        with pytest.raises(browser_reattach.Refused, match="no CDP endpoint"):
            self._held(cmdline_port=None)

    def test_a_witness_with_no_pid_at_all_is_not_adoptable(self):
        """Windows' bare ``lockfile`` names nothing, so there is no process to
        read and nothing to report beyond an ordinary spawn."""
        assert self._held(hold_pid=None, cmdline_port=9223) is None


def _held_candidate(instance_id="i-held", profile="C:/p"):
    return browser_reattach.Adoptable(
        instance_id=instance_id, pid=CHROME_PID, user_data_dir=profile, port=9223
    )


def _spawn_cleanup(tmp_path, name="browser_pids.json"):
    """A cleanup double whose record is real — the claim is not stubbed out."""
    double = MagicMock()
    double.pid_file = tmp_path / name
    double._owner_backend_alive.return_value = False
    return double


class TestWhatTheCommandLineSays:
    """The leaf that reads a live process's argv, which for a browser whose
    backend is gone is the only description of it left."""

    def _with_cmdline(self, *args):
        return patch.object(
            browser_cmdline.psutil,
            "Process",
            return_value=SimpleNamespace(cmdline=lambda: list(args)),
        )

    def test_headless_is_measured_not_assumed(self):
        """F-888 review M2: an adopted instance used to report the model's
        ``headless=False`` default about a browser this backend never launched."""
        with self._with_cmdline("chrome", "--headless=new"):
            assert browser_cmdline.is_headless(CHROME_PID) is True
        with self._with_cmdline("chrome", "--headless"):
            assert browser_cmdline.is_headless(CHROME_PID) is True
        with self._with_cmdline("chrome", "--remote-debugging-port=1"):
            assert browser_cmdline.is_headless(CHROME_PID) is False

    @pytest.mark.parametrize(
        ("launched", "ours"),
        [
            pytest.param(r"C:\other", r"C:\ours", id="windows-paths"),
            pytest.param("/var/other", "/var/ours", id="posix-paths"),
        ],
    )
    def test_a_stranger_directory_never_donates_its_port(self, launched, ours):
        """F-888 review M4's other half: "a Chrome holds this pid" and "this pid
        names a port" were independent, so a RECYCLED pid on a stranger's
        chrome.exe would have donated that stranger's debugging port.

        Both flavors run on every platform because the REFUSAL is flavor-free:
        two different directories stay different under anybody's normalisation.
        The other half of the join — two spellings of ONE directory — is not,
        and has its own pin below.
        """
        with self._with_cmdline(
            "chrome", f"--user-data-dir={launched}", "--remote-debugging-port=9223"
        ):
            assert browser_cmdline.debug_port(CHROME_PID, ours) is None

    def test_the_same_directory_spelled_differently_still_matches(self):
        """The other half of the join, in the RUNNING platform's own flavor.

        `browser_pid_registry.normalize_path` is `os.path` — deliberately the
        platform's, because both sides of every real comparison come from ONE
        machine: the record this backend wrote and the argv of a process running
        beside it. So what counts as "the same directory spelled differently" is
        a per-platform fact and has to be pinned as one. Windows folds case and
        eats a trailing separator; POSIX does neither and normalises a `.`
        component instead.

        This pin was `c:\\other\\\\` against `--user-data-dir=C:\\other` on every
        platform, which is a Windows normalisation asked of `posixpath` — green
        on Windows, red on all five POSIX cells (PR #135, run 35457340072).
        Making the comparator flavor-aware was considered and rejected: it is the
        function that normalises the record on the way IN, a backslash is a legal
        character in a POSIX filename and `C:foo` is a legal POSIX relative path,
        so shape-sniffing would corrupt stored entries to serve a case that
        cannot occur.
        """
        launched, recorded = (
            (r"C:\other", "c:\\other\\\\")
            if os.name == "nt"
            else ("/var/other", "/var/./other/")
        )
        with self._with_cmdline(
            "chrome", f"--user-data-dir={launched}", "--remote-debugging-port=9223"
        ):
            assert browser_cmdline.debug_port(CHROME_PID, recorded) == 9223

    def test_the_owner_witness_is_blind_to_which_build_the_owner_runs(self):
        """A live backend of ours owns its browsers whatever version it is.

        Two different questions now live one import apart and both are spelled
        "is this one of ours": `singleton._adoptable_identity` (F-889) asks
        whether a recorded BACKEND is one this client would REUSE, which is
        version- and fingerprint-gated on purpose, and
        `singleton._is_our_backend` asks whether a PID is our HTTP backend at
        all, which must not be. Only the second reaches
        `browser_pid_registry.is_reapable`, and if the two were ever folded
        together a backend running a NEWER build would read as no owner —
        so a cold start would adopt the browsers of a live sibling and F-886's
        two-drivers harm would be back, reached from this side.

        Pinned on the WITNESS rather than on a message, because that is the join
        that could be "unified" by someone reading two identically-worded
        docstrings: the cmdline of a backend says nothing about its build.
        """
        entry = {
            **_entry(),
            "owner_pid": LIVE_OWNER,
            # A build this client would never adopt — the exact input
            # `_adoptable_identity` answers False for.
            "version": "99.0.0",
            "source_fingerprint": "a-stranger-digest",
        }
        assert registry.is_reapable(entry, _owner_alive) is False, (
            "a live backend of ours is a live owner whatever build it runs"
        )

    def test_a_remote_proxy_is_never_judged(self):
        """It was not tied to the dead backend's lifetime, and connect-probing a
        stranger's host is not this tool's business."""
        with self._with_cmdline("chrome", "--proxy-server=http://proxy.example:8080"):
            assert browser_cmdline.dead_local_proxy(CHROME_PID) is None

    def test_a_loopback_proxy_nothing_answers_on_is_reported(self):
        """F-888 review M3. The authenticated forwarder lives INSIDE the backend,
        so it dies with it while the launch arg lives on — an adopted browser can
        come back with every navigation failing at a closed local port."""
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            dead_port = taken.getsockname()[1]
        with self._with_cmdline("chrome", f"--proxy-server=127.0.0.1:{dead_port}"):
            assert (
                browser_cmdline.dead_local_proxy(CHROME_PID) == f"127.0.0.1:{dead_port}"
            )

    def test_a_loopback_proxy_that_is_still_up_is_not_reported(self):
        """The test is a CONNECT, not the presence of the flag: a caller may run
        their own local proxy, and warning about a working one would be a lie."""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            live_port = listener.getsockname()[1]
            with self._with_cmdline("chrome", f"--proxy-server=127.0.0.1:{live_port}"):
                assert browser_cmdline.dead_local_proxy(CHROME_PID) is None

    def test_two_browser_roots_on_one_directory_answer_nothing(self):
        """F-888 re-review L-new-1. Chrome's singleton normally guarantees one
        browser per directory — but the states this feature operates in are
        exactly the ones where it does not: F-871's stale or absent
        ``SingletonLock``, and a hard-killed Chrome on Windows whose ``lockfile``
        names no pid. Taking ``[0]`` of two roots puts the set-ordering coin flip
        back SILENTLY, which is the property this function exists to remove, so
        an ambiguous answer is no answer and the caller reports it."""
        argv = ["chrome", r"--user-data-dir=C:\ours"]
        table = {41001: argv, 41002: argv}
        with patch.object(
            browser_cmdline, "arguments", side_effect=lambda pid: table.get(pid, [])
        ):
            assert browser_cmdline.browser_process({41001}, r"C:\ours") == 41001
            assert browser_cmdline.browser_process(set(table), r"C:\ours") is None


class TestIgnoredArgsNamesOnlyWhatTheCallerPassed:
    """F-888 re-review M-new-3. ``ignored_spawn_args`` exists to be BELIEVED —
    its own docstring says "the ones you passed" — so it may not name an argument
    the handler filled in on the caller's behalf."""

    def _args(self, **passed):
        from stealth_chrome_devtools_mcp.embedded.tool_sections import (
            browser_management,
        )

        return browser_management._launch_only_args(**passed)

    def test_an_unset_sandbox_is_not_reported(self):
        """`sandbox` is resolved from None to a real bool before the spawn runs,
        and passing the RESOLVED value named it on every single re-attach — which
        devalues the field for the arguments that actually matter."""
        assert self._args(sandbox=None) == []

    def test_a_sandbox_the_caller_really_passed_is_reported(self):
        assert self._args(sandbox=False) == ["sandbox"]

    def test_only_what_differs_from_the_default_is_named(self):
        assert self._args(headless=False, viewport_width=1920, proxy=None) == []
        assert self._args(headless=True, viewport_width=800) == [
            "headless",
            "viewport_width",
        ]


class TestAnAdoptedInstanceTellsTheTruth:
    """F-888 review M2. What ``_build_instance`` fills in from OPTIONS is a
    request nobody made here — the process that launched this browser is gone."""

    async def _adopt(self, tmp_path, tab, *, headless=False, dead_egress=None):
        manager = BrowserManager()
        browser = FakeBrowser(alive=None, pid=CHROME_PID, main_tab=tab)
        # `dead_egress` rides on the CANDIDATE, because that is where the real
        # `held_by` puts it and this pin patches `held_by` out.
        candidate = browser_reattach.Adoptable(
            instance_id="i-held",
            pid=CHROME_PID,
            user_data_dir="C:/p",
            port=9223,
            dead_egress=dead_egress,
        )
        with (
            patch.object(browser_reattach, "held_by", return_value=candidate),
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach", return_value=browser),
            patch.object(browser_cmdline, "is_headless", return_value=headless),
        ):
            held = await browser_reattach.adopt_held_profile(
                manager, _spawn_cleanup(tmp_path), "C:/p"
            )
        return manager, held

    @pytest.mark.asyncio
    async def test_headless_comes_off_the_holders_command_line(self, tmp_path):
        manager, held = await self._adopt(tmp_path, FakeTab(), headless=True)
        assert manager._instances[held.instance_id]["instance"].headless is True

    @pytest.mark.asyncio
    async def test_the_window_is_measured_and_never_resized(self, tmp_path):
        """``apply_and_measure`` would SET the size first, which for a browser we
        did not launch means resizing a human's open window to a default they
        never asked for."""
        tab = FakeTab()
        with patch.object(
            browser_reattach.window_sizing, "measure", return_value={"w": 1}
        ) as measure:
            manager, held = await self._adopt(tmp_path, tab)
        assert measure.call_count == 1
        assert manager._spawn_diagnostics[held.instance_id]["window_size"] == {
            "actual": {"w": 1},
            "measured": True,
        }

    @pytest.mark.asyncio
    async def test_an_unmeasurable_window_says_so_instead_of_claiming_1920(
        self, tmp_path
    ):
        tab = FakeTab()
        with patch.object(browser_reattach.window_sizing, "measure", return_value=None):
            manager, held = await self._adopt(tmp_path, tab)
        assert (
            manager._spawn_diagnostics[held.instance_id]["window_size"]["measured"]
            is False
        )

    @pytest.mark.asyncio
    async def test_what_cannot_be_restored_is_named(self, tmp_path):
        """Per-instance state that lived in the backend that died. Interception
        and dynamic hooks are deliberately NOT in this list — both are
        re-established on the adopted tab."""
        manager, held = await self._adopt(tmp_path, FakeTab())
        not_restored = manager._spawn_diagnostics[held.instance_id]["not_restored"]
        assert set(not_restored) == {
            "extra_headers",
            "timezone_id",
            "user_agent",
            "proxy",
        }
        assert "block_resources" not in not_restored

    @pytest.mark.asyncio
    async def test_the_adopted_tab_gets_this_backends_hooks(self, tmp_path):
        """Exactly what a spawn does: an adopted tab carries none of THIS
        backend's handlers, so a hook created against it would be registered and
        never fire."""
        tab = FakeTab()
        with patch.object(
            BrowserManager, "_setup_dynamic_hooks", return_value=True
        ) as hooks:
            _manager, held = await self._adopt(tmp_path, tab)
        assert hooks.call_count == 1
        assert hooks.call_args.args[0] is tab
        assert hooks.call_args.args[1] == held.instance_id

    @pytest.mark.asyncio
    async def test_a_dead_egress_proxy_is_adopted_but_stamped(self, tmp_path):
        """F-888 review M3, decided: ADOPT and say so. Refusing would convert a
        recoverable logged-in browser into a reap on the record path — this
        finding's own harm — while a browser whose page loads fail at a closed
        local port is at least still reachable, closable and re-spawnable."""
        manager, held = await self._adopt(
            tmp_path, FakeTab(), dead_egress="127.0.0.1:1"
        )
        assert held.instance_id is not None
        assert (
            manager._spawn_diagnostics[held.instance_id]["dead_egress_proxy"]
            == "127.0.0.1:1"
        )


class TestHeldAdoptionNeverReaps:
    """The one place this differs from `run`, and it is load-bearing: a CLIENT
    asked for this browser, so a failed attach must never kill it."""

    @pytest.mark.asyncio
    async def test_a_failed_attach_spawns_instead_and_kills_nothing(self, tmp_path):
        manager = BrowserManager()
        cleanup = _spawn_cleanup(tmp_path)
        with (
            patch.object(browser_reattach, "held_by", return_value=_held_candidate()),
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(
                cdp_attach, "attach", side_effect=ConnectionRefusedError("no")
            ),
            patch.object(browser_reattach, "reap_recorded") as reap,
        ):
            held = await browser_reattach.adopt_held_profile(manager, cleanup, "C:/p")

        assert held.instance_id is None, (
            "a failed adoption must fall through to the spawn"
        )
        assert reap.call_count == 0, (
            "the caller's own logged-in browser must never be reaped by the path "
            "that was trying to reach it"
        )
        assert cleanup._drop_recorded.call_count == 0

    @pytest.mark.asyncio
    async def test_a_failed_attach_says_why_rather_than_walking_silently(
        self, tmp_path
    ):
        """F-888 team-lead shape (d). The spawn proceeds onto a different
        directory, and the caller is owed the reason: the browser they were
        reaching for is STILL RUNNING and this path deliberately did not kill it.
        """
        with (
            patch.object(browser_reattach, "held_by", return_value=_held_candidate()),
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(
                cdp_attach, "attach", side_effect=ConnectionRefusedError("no")
            ),
        ):
            held = await browser_reattach.adopt_held_profile(
                BrowserManager(), _spawn_cleanup(tmp_path), "C:/p"
            )

        assert held.instance_id is None
        assert str(CHROME_PID) in held.declined
        assert "9223" in held.declined
        assert "left running and untouched" in held.declined

    @pytest.mark.asyncio
    async def test_an_ordinary_spawn_has_nothing_to_declare(self, tmp_path):
        """Nothing holds the directory: no reason, no diagnostics noise, and the
        F-871 walk keeps exactly the shape it has today."""
        with patch.object(browser_reattach, "held_by", return_value=None):
            held = await browser_reattach.adopt_held_profile(
                BrowserManager(), _spawn_cleanup(tmp_path), "C:/p"
            )
        assert held.instance_id is None
        assert held.declined is None

    @pytest.mark.asyncio
    async def test_a_successful_adoption_answers_with_the_instance_id(self, tmp_path):
        manager = BrowserManager()
        cleanup = _spawn_cleanup(tmp_path)
        tab = FakeTab(url="https://sellercentral.amazon.com/home")
        browser = FakeBrowser(alive=None, pid=CHROME_PID, main_tab=tab)
        with (
            patch.object(browser_reattach, "held_by", return_value=_held_candidate()),
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach", return_value=browser),
        ):
            held = await browser_reattach.adopt_held_profile(
                manager, cleanup, "C:/p", ignored_args=["headless", "proxy"]
            )

        assert held.instance_id == "i-held"
        diagnostics = manager._spawn_diagnostics["i-held"]
        assert diagnostics["reattached"] is True
        assert diagnostics["reattached_pid"] == CHROME_PID
        assert diagnostics["ignored_spawn_args"] == ["headless", "proxy"]


class TestALostClaimIsNeverAReap:
    """F-888 RE-review HIGH. The startup pass reaps on failure, and that is right
    for exactly one class of failure: evidence about the BROWSER.

    A claim another backend won, or a claim that could not be written at all, is
    evidence about US. Reaping on either was strictly worse than the race the
    claim was introduced to end: backends B and C both classify entry E adoptable
    from their own snapshot, C claims first and adopts, and B would then kill
    every browser on that profile predating its own `_init_time` — which C's
    adopted browser does — and drop the entry C has just re-stamped. The human's
    login dies, C holds a handle to a corpse, and nothing on disk names it.
    """

    @pytest.fixture
    def cleanup(self, tmp_path):
        double = _spawn_cleanup(tmp_path)
        double._drop_recorded = MagicMock()
        return double

    async def _pass_with(self, cleanup, claim_result):
        """One `run` over one adoptable entry, with the claim forced."""
        manager = MagicMock()
        manager._lock = asyncio.Lock()
        manager._instances = {}
        with (
            patch.object(
                browser_reattach,
                "adoptable_for",
                return_value=_classified(**{"i-kept": _adoptable_record()}),
            ),
            patch.object(browser_reattach, "claim", **claim_result),
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach") as attach,
            patch.object(browser_reattach, "reap_recorded") as reap,
        ):
            adopted = await browser_reattach.run(manager, cleanup)
        return adopted, reap, attach

    @pytest.mark.asyncio
    async def test_a_browser_a_sibling_claimed_first_is_spared_not_reaped(
        self, cleanup
    ):
        """The race the claim exists for, ending the way it must. `claim` answers
        None — a live backend of ours owns it — and this pass leaves BOTH the
        browser and the entry alone. The entry especially: it is the winner's
        stamp, and dropping it would leave a live adopted browser that nothing on
        disk names."""
        adopted, reap, attach = await self._pass_with(cleanup, {"return_value": None})

        assert adopted == []
        assert reap.call_count == 0, (
            "a browser a sibling backend legitimately claimed was reaped by this pass"
        )
        assert cleanup._drop_recorded.call_count == 0, (
            "the winner's entry was dropped, leaving its browser unnamed on disk"
        )
        assert attach.call_count == 0

    @pytest.mark.asyncio
    async def test_a_record_write_failure_is_spared_not_reaped(self, cleanup):
        """`update_entries` raises BY DESIGN on a lock timeout or an OSError
        writing the record. That says nothing whatsoever about the browser, so
        converting it into a kill put a human's logged-in Chrome inside the blast
        radius of a transient state-dir error."""
        adopted, reap, _ = await self._pass_with(
            cleanup, {"side_effect": OSError("record locked")}
        )

        assert adopted == []
        assert reap.call_count == 0
        assert cleanup._drop_recorded.call_count == 0

    @pytest.mark.asyncio
    async def test_a_browser_level_failure_still_reaps(self, cleanup):
        """The other side, and why the handler cannot simply stop reaping: an
        orphan we CLAIMED and then could not reach must still reach one of the
        two ends 2.1.9 had, or it leaks forever."""
        adopted, reap, _ = await self._pass_with(
            cleanup,
            {"return_value": registry.Claimed(instance_id="i-kept", previous=None)},
        )

        assert adopted == []
        assert reap.call_count == 1
        cleanup._drop_recorded.assert_called_once_with({"i-kept"})

    def test_the_two_spare_reasons_are_one_class(self):
        """`Undecided` is a SUBCLASS of `Refused` on purpose: every caller's
        handling is identical, so one `except Refused` has to cover both. Two
        sibling types is how one of them comes to be missed at a handler."""
        assert issubclass(browser_claim.Undecided, browser_reattach.Refused)


class TestTheCrossProcessClaim:
    """F-888 review HIGH. Adoption is safe between PROCESSES or it is not safe:
    an asyncio lock cannot help, because the racers are backends."""

    def _candidate(self):
        return _held_candidate(instance_id="i-fresh")

    def test_the_claim_stamps_us_as_the_owner_before_the_door(self, tmp_path):
        cleanup = _spawn_cleanup(tmp_path)
        claimed = browser_reattach.claim(cleanup, self._candidate())

        assert claimed is not None
        entry = _read(cleanup.pid_file)[claimed.instance_id]
        assert entry["owner_pid"] == registry.owner_identity()[0]
        assert entry["pid"] == CHROME_PID
        assert entry["cdp_port"] == 9223

    def test_a_live_owners_browser_is_refused_inside_the_lock(self, tmp_path):
        """The refusal is keyed on the PID, not the instance id: the id is not
        stable across the two entry points, so two backends racing for one Chrome
        would mint two different ids and both 'win'."""
        cleanup = _spawn_cleanup(tmp_path)
        cleanup._owner_backend_alive.return_value = True
        _seed(cleanup.pid_file, {"theirs": _entry(owner_pid=LIVE_OWNER)})

        assert browser_reattach.claim(cleanup, self._candidate()) is None
        assert _read(cleanup.pid_file)["theirs"]["owner_pid"] == LIVE_OWNER

    def test_a_dead_owners_entry_donates_its_instance_id(self, tmp_path):
        """Which is how a client holding an id from before the restart keeps
        addressing the same browser."""
        cleanup = _spawn_cleanup(tmp_path)
        _seed(cleanup.pid_file, {"i-was": _entry(owner_pid=DEAD_OWNER)})

        claimed = browser_reattach.claim(cleanup, self._candidate())

        assert claimed is not None
        assert claimed.instance_id == "i-was"
        assert set(_read(cleanup.pid_file)) == {"i-was"}

    def test_the_second_claimant_loses_because_the_first_is_now_the_owner(
        self, tmp_path
    ):
        """The two-concurrent-spawns case, and the reason the claim is what
        answers it: after the first claim lands, the record names a LIVE owner —
        us — so the second reads a refusal rather than opening a second
        connection and registering one Chrome as two instances."""
        first = _spawn_cleanup(tmp_path)
        assert browser_reattach.claim(first, self._candidate()) is not None

        second = _spawn_cleanup(tmp_path)
        second._owner_backend_alive.return_value = True  # the first backend lives
        assert browser_reattach.claim(second, self._candidate()) is None

    def test_a_failed_adoption_hands_the_claim_back(self, tmp_path):
        """Otherwise the record names us as the owner of a browser we do not
        hold, which is WORSE than what we found: the next backend reads a live
        owner and refuses to adopt a browser nobody is driving."""
        cleanup = _spawn_cleanup(tmp_path)
        _seed(cleanup.pid_file, {"i-was": _entry(owner_pid=DEAD_OWNER)})
        before = registry.read_entries(cleanup.pid_file)

        claimed = browser_reattach.claim(cleanup, self._candidate())
        assert _read(cleanup.pid_file)["i-was"]["owner_pid"] != DEAD_OWNER

        registry.release_claim(cleanup.pid_file, claimed)
        assert registry.read_entries(cleanup.pid_file) == before

    def test_a_claim_with_no_prior_entry_is_released_by_removal(self, tmp_path):
        cleanup = _spawn_cleanup(tmp_path)
        claimed = browser_reattach.claim(cleanup, self._candidate())
        registry.release_claim(cleanup.pid_file, claimed)
        assert _read(cleanup.pid_file) == {}

    def test_a_claim_never_flips_a_disposable_entry_to_persistent(self, tmp_path):
        """F-888 re-review L-new-3. The held path is deliberately not gated on
        ``on_persistent_profile`` — there is usually no record to read it from —
        but the claim writes both keys as "persistent". On an entry that ALREADY
        exists that would convert a disposable auto-clone into a profile nothing
        ever reclaims and a browser nothing ever reaps, from a caller merely
        naming that directory by hand. What the record says about disposability
        is a fact about how the profile was CREATED; a claim is a statement about
        ownership and has no standing to change it."""
        cleanup = _spawn_cleanup(tmp_path)
        _seed(
            cleanup.pid_file,
            {
                "i-clone": _entry(
                    owner_pid=DEAD_OWNER, auto_clone=True, uses_custom_data_dir=False
                )
            },
        )

        claimed = browser_reattach.claim(cleanup, self._candidate())

        assert claimed is not None
        entry = _read(cleanup.pid_file)["i-clone"]
        assert entry["auto_clone"] is True
        assert entry["uses_custom_data_dir"] is False
        assert registry.on_persistent_profile(entry) is False
        # The ownership half DID land — that is the part a claim is for.
        assert entry["owner_pid"] == registry.owner_identity()[0]

    @pytest.mark.asyncio
    async def test_the_attach_is_never_reached_when_the_claim_is_refused(
        self, tmp_path
    ):
        """The order is the whole point: the claim is taken BEFORE a single byte
        reaches Chrome, so a browser a sibling backend owns is never connected
        to at all."""
        cleanup = _spawn_cleanup(tmp_path)
        cleanup._owner_backend_alive.return_value = True
        _seed(cleanup.pid_file, {"theirs": _entry(owner_pid=LIVE_OWNER)})

        with (
            patch.object(browser_reattach, "held_by", return_value=self._candidate()),
            patch.object(cdp_attach, "attach") as attach,
        ):
            held = await browser_reattach.adopt_held_profile(
                BrowserManager(), cleanup, "C:/p"
            )

        assert attach.call_count == 0
        assert held.instance_id is None
        assert "already owns the browser" in held.declined

    @pytest.mark.asyncio
    async def test_a_failed_attach_releases_the_claim_on_the_record(self, tmp_path):
        cleanup = _spawn_cleanup(tmp_path)
        _seed(cleanup.pid_file, {"i-was": _entry(owner_pid=DEAD_OWNER)})
        before = registry.read_entries(cleanup.pid_file)

        with (
            patch.object(browser_reattach, "held_by", return_value=self._candidate()),
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(
                cdp_attach, "attach", side_effect=ConnectionRefusedError("no")
            ),
        ):
            await browser_reattach.adopt_held_profile(BrowserManager(), cleanup, "C:/p")

        assert registry.read_entries(cleanup.pid_file) == before

    @pytest.mark.asyncio
    async def test_the_budget_expiring_after_the_claim_still_releases_it(
        self, tmp_path
    ):
        """F-888 re-review M-new-2. The whole adoption runs under
        ``ATTACH_BUDGET_SECONDS``, so the cancellation can land anywhere after
        the claim — and the claim must be released on THAT path too.

        A claim taken outside the handler that releases it leaves the record
        naming this LIVE backend as the owner of a browser it does not hold.
        Nothing recovers that while we run: our own classification reads a live
        owner, recovery spares it, and every other backend is refused. It is the
        one failure mode that produces an *unreachable* browser, which is the
        state this whole finding exists to abolish.
        """
        cleanup = _spawn_cleanup(tmp_path)
        _seed(cleanup.pid_file, {"i-was": _entry(owner_pid=DEAD_OWNER)})
        before = registry.read_entries(cleanup.pid_file)

        async def _never_answers(*_args, **_kwargs):
            await asyncio.sleep(10)

        with (
            patch.object(browser_reattach, "ATTACH_BUDGET_SECONDS", 0.25),
            patch.object(browser_reattach, "held_by", return_value=self._candidate()),
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach", side_effect=_never_answers),
        ):
            held = await browser_reattach.adopt_held_profile(
                BrowserManager(), cleanup, "C:/p"
            )

        assert held.instance_id is None
        assert registry.read_entries(cleanup.pid_file) == before, (
            "a budget that expired after the claim left the record naming us as "
            "the owner of a browser we never attached to"
        )

    @pytest.mark.asyncio
    async def test_the_budget_expiring_while_the_claim_lands_still_releases_it(
        self, tmp_path
    ):
        """The narrowest window, and the only one that produces an UNREACHABLE
        browser. Cancelling an ``await asyncio.to_thread(...)`` does not stop the
        worker thread — it still takes the record lock and still writes — so the
        result is never handed back while the stamp lands anyway. Asking the TASK
        what it wrote is the only way to release it."""
        cleanup = _spawn_cleanup(tmp_path)
        _seed(cleanup.pid_file, {"i-was": _entry(owner_pid=DEAD_OWNER)})
        before = registry.read_entries(cleanup.pid_file)
        real_claim = browser_reattach.claim

        def _slow_claim(*args, **kwargs):
            time.sleep(0.4)  # the worker thread; the budget expires under it
            return real_claim(*args, **kwargs)

        with (
            patch.object(browser_reattach, "ATTACH_BUDGET_SECONDS", 0.05),
            patch.object(browser_reattach, "held_by", return_value=self._candidate()),
            patch.object(browser_reattach, "claim", side_effect=_slow_claim),
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach") as attach,
        ):
            held = await browser_reattach.adopt_held_profile(
                BrowserManager(), cleanup, "C:/p"
            )
            # The worker thread outlives the cancellation; let it land and let
            # the teardown's release follow it.
            await asyncio.sleep(1.0)

        assert held.instance_id is None
        assert attach.call_count == 0
        assert registry.read_entries(cleanup.pid_file) == before, (
            "the claim landed after the budget expired and nothing released it: "
            "the record now names this live backend as the owner of a browser "
            "it never attached to, which no other backend can ever adopt"
        )


class TestNamedSessionDirIsNeverReclaimed:
    """Fact 3 of the live report: the other stranded login's Chrome is gone but
    its directory under the session root must still be there for a re-spawn.

    Measured against the ONE selection gate the storage sweep uses, for both
    shapes a named session dir can have on disk."""

    def test_a_marker_less_directory_is_not_a_reclaim_target(self, tmp_path):
        """A directory the server did not create a marker for — which is what a
        hand-made or legacy session dir looks like — reads as NOT auto."""
        from stealth_chrome_devtools_mcp.embedded import clone_storage

        session = tmp_path / "MASTER_CHAT-299040179676"
        session.mkdir()
        assert clone_storage.clone_is_auto(session) is False

    def test_a_named_sessions_marker_is_not_a_reclaim_target(self, tmp_path):
        """And the shape the server DOES write for a named profile: an explicit
        ``auto_clean: false``."""
        from stealth_chrome_devtools_mcp.embedded import clone_storage

        session = tmp_path / "MASTER_CHAT-299040179676"
        session.mkdir()
        (session / ".stealth_chrome_devtools_mcp_clone.json").write_text(
            json.dumps({"source_kind": "explicit", "auto_clean": False}),
            encoding="utf-8",
        )
        assert clone_storage.clone_is_auto(session) is False
        assert clone_storage.clone_is_named(session) is True


# ---------------------------------------------------------------------------
# One door
# ---------------------------------------------------------------------------


class TestOneDoor:
    def test_the_config_carries_host_and_port_and_the_profile(self):
        """Setting BOTH is nodriver's ``connect_existing`` gate; the profile dir
        rides along because ``browser.config.user_data_dir`` is what the spawn
        pipeline reads back to decide profile cleanup."""
        config = cdp_attach.config_for(r"C:\profiles\p", PORT)
        assert config.host == cdp_attach.CDP_HOST
        assert config.port == PORT
        assert str(config.user_data_dir).endswith("p")
        assert config.uses_custom_data_dir is True

    def test_both_consumers_use_that_one_door(self):
        """F-810's delegated launch was standing in this door first and F-888's
        adoption arrived at it from the other side; neither may spell the gate
        itself, or the two drift on how a running browser is entered."""
        for module in (desktop_launch, browser_reattach):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "cdp_attach." in source, module.__name__
            assert "config.host =" not in source, module.__name__
            assert "uc.start(" not in source, module.__name__

    @pytest.mark.asyncio
    async def test_an_abandoned_attach_closes_the_connection_it_opened(self):
        """F-888 review L3. Cancelling an await never un-opens a websocket, so a
        plain ``wait_for`` around the door leaves a live connection and a live
        listener task on a ``Browser`` nobody references any more."""
        opened = asyncio.Event()
        browser = FakeBrowser(alive=None, pid=CHROME_PID, main_tab=FakeTab())

        async def _slow(*_args, **_kwargs):
            opened.set()
            await asyncio.sleep(0.05)
            return browser

        with patch.object(cdp_attach, "attach", side_effect=_slow):
            task = asyncio.ensure_future(
                cdp_attach.attach_reclaiming(SimpleNamespace(), CHROME_PID)
            )
            await opened.wait()
            # `wait_for` CANCELS what it bounds, which is the shape the caller
            # has: `_adopt_one` runs under one. A shield here would cancel the
            # test's own wrapper instead and prove nothing.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, timeout=0.01)
            # The attach the caller gave up on still completes, and what it
            # produced is closed rather than left holding a socket.
            await asyncio.sleep(0.2)

        assert browser.connection.disconnected is True, (
            "an abandoned attach must not leave its CDP connection open"
        )

    @pytest.mark.asyncio
    async def test_the_adoption_path_goes_through_the_reclaiming_attach(self, tmp_path):
        """And the caller actually USES it. The budget that bounds an adoption is
        the one that can fire mid-attach, so a plain `attach` there would put the
        leak back with every pin above still green.

        **Every edge here is an EVENT, and that is F-909.** The shipped version
        raced a 0.25 s budget against a real locked record write on a worker
        thread and a 0.6 s sleep in the door. On a loaded Windows runner the
        first of those won: the budget was spent BEFORE the door
        (``browser_claim.py``'s ``await asyncio.shield(claiming)`` took the
        cancellation), no browser was ever produced, and the node reported the
        connection it had never opened as "left open" — a true assertion about a
        thing that never happened, which is the worst shape a pin can fail in.
        Measured: ~3 ms to reach the door locally against a 250 ms budget, and
        8 s spent failing on CI.

        So the claim is stubbed to the one thing this node is about — it was
        taken, and it was handed back — with NO disk in the measured window, and
        the door PARKS on an event instead of sleeping, which makes the budget
        provably spent inside the attach rather than probably. Reaching the door
        is asserted on its own, so the pre-door stall can never again present as
        the post-door leak. The real claim keeps its own pins in
        ``TestTheCrossProcessClaim``; widening the budget would have hidden the
        race rather than removed it.
        """
        browser = FakeBrowser(alive=None, pid=CHROME_PID, main_tab=FakeTab())
        at_the_door = asyncio.Event()
        let_the_door_answer = asyncio.Event()
        claimed_pids: list[int] = []
        handed_back: list[str] = []

        async def _park_at_the_door(*_args, **_kwargs):
            at_the_door.set()
            await let_the_door_answer.wait()
            return browser

        def _claim_without_touching_disk(_cleanup, candidate):
            claimed_pids.append(candidate.pid)
            return registry.Claimed(instance_id=candidate.instance_id, previous=None)

        # The first `asyncio.to_thread` in a process pays for a thread-pool
        # worker; `browser_claim.held` runs the claim on one, and that hop is
        # the last thing between the call and the door. Paying it here keeps it
        # out of the budget.
        await asyncio.to_thread(int)

        with (
            patch.object(browser_reattach, "ATTACH_BUDGET_SECONDS", 0.25),
            patch.object(browser_reattach, "held_by", return_value=_held_candidate()),
            patch.object(browser_reattach, "claim", _claim_without_touching_disk),
            patch.object(
                registry,
                "release_claim",
                lambda _path, landed: handed_back.append(landed.instance_id),
            ),
            patch.object(cdp_attach, "config_for", return_value=SimpleNamespace()),
            patch.object(cdp_attach, "attach", side_effect=_park_at_the_door),
        ):
            adopting = asyncio.ensure_future(
                browser_reattach.adopt_held_profile(
                    BrowserManager(), _spawn_cleanup(tmp_path), "C:/p"
                )
            )
            # The budget is running from the line above, so the door has to be
            # reached before it expires or this node is about nothing. It is its
            # OWN assertion, because that is exactly what went wrong on CI.
            try:
                await asyncio.wait_for(at_the_door.wait(), timeout=10.0)
            except TimeoutError:
                adopting.cancel()
                pytest.fail(
                    "the attach door was never reached — the claim, not the "
                    "attach, spent the adoption budget (F-909)"
                )

            # The door never answers on its own, so the budget can only expire
            # HERE, inside the attach. That is the whole point of the node — and
            # also why this edge is bounded by the NODE: if the product's budget
            # ever stopped firing, an unbounded await would hang the job instead
            # of failing it (there is no global pytest timeout in this repo).
            try:
                held = await asyncio.wait_for(adopting, timeout=10.0)
            except TimeoutError:
                let_the_door_answer.set()
                adopting.cancel()
                pytest.fail("the adoption budget never fired inside the attach (F-909)")
            assert held.instance_id is None
            assert claimed_pids == [CHROME_PID], "the claim was not taken first"
            assert handed_back == ["i-held"], (
                "an adoption the budget cancelled must hand its claim back"
            )

            # The abandoned attach finishes after the caller gave up, and the
            # closer runs after IT. Wait for the fact, not for a guessed nap —
            # the thing under test is that it happens at all.
            let_the_door_answer.set()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while not browser.connection.disconnected and loop.time() < deadline:
                await asyncio.sleep(0.02)

        assert browser.connection.disconnected is True, (
            "the adoption timed out mid-attach and left its connection open"
        )
