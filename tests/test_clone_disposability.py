"""T-5968 review -- what a sweep may take is decided by the ROLE of a directory,
read from a marker written by the REAL marker writer, and every way to not know
reads as KEEP.

Seven things the independent review found (REQUEST_CHANGES on 1d38e143):

* B1: with ``STEALTH_MCP_SEED_SESSION=fleet`` an UNNAMED spawn's clone got an
  ``explicit-session`` marker with ``auto_clean: false``, so every guard that
  protects named sessions protected a clone nobody would ever delete (a leak,
  and a regression from main, which deleted it);
* M3: a browser-named process whose command line cannot be READ
  (``psutil.AccessDenied``) named no profile to any scan, so its profile read
  FREE to a delete;
* L5: a clone's age was its ``created_at`` alone, so a clone a caller re-opened
  by path was stale as soon as it was old;
* L6: the cap sweep and the reaper asked two different questions, and the cap
  sweep still fell back from a refused move to a delete;
* L7: the abandon test used a hand-made marker;
* the NITs: a failed ``utime`` shortened the retention, and the first sweep after
  an upgrade said nothing before it moved a hundred clones.

Hermetic: temp session roots, no Chrome. Every marker below is the one
``clone_storage`` writes; none is a dict built by hand.
"""

import asyncio
import os
import time
from pathlib import Path

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    clone_protection,
    clone_stale,
    clone_storage,
    clone_trash,
    fleet_session,
    profile_seed,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup
from stealth_chrome_devtools_mcp.settings import get_settings

LOGIN = b"a signed-in cookie jar"
COOKIES = "Default/Cookies"
LONG_AGO = 9 * 3600.0


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """A process table of ONE real process (this one), so another browser on the
    machine running the suite cannot change an answer, and a clean reservation
    set per test."""
    clone_storage._clear_protected_clone_dirs()
    monkeypatch.setattr(clone_storage, "_profile_hold", lambda directory: None)
    monkeypatch.setattr(
        psutil, "process_iter", lambda *_a, **_k: iter([psutil.Process(os.getpid())])
    )
    yield
    clone_storage._clear_protected_clone_dirs()
    get_settings.cache_clear()


async def _selection(*, session=None, seed_from=None) -> dict:
    """What a spawn resolves to, composed the way ``spawn_browser`` composes it."""
    landed = clone_storage.require_allowed_user_data_dir(None, session)
    seed = clone_storage.require_allowed_seed_from(seed_from, landed)
    selection = await clone_storage.resolve_profile_selection(landed, seed_from=seed)
    return clone_storage._public_profile_selection(selection)


def _age(directory: Path, seconds: float) -> None:
    """Make *directory* look unused for *seconds*: the marker's ``created_at`` AND
    every mtime the reaper reads, because a copy that has just been made has a
    fresh mtime on all of them."""
    then = time.time() - seconds
    marker = directory / profile_seed.MARKER_NAME
    text = marker.read_text(encoding="utf-8")
    created = str(profile_seed.read_marker(directory)["created_at"])
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(then))
    marker.write_text(text.replace(created, stamp), encoding="utf-8")
    for path in (*directory.rglob("*"), directory):
        os.utime(path, (then, then))


def _intact(directory: Path) -> bool:
    return (directory / COOKIES).exists()


def _put_login(directory: Path) -> None:
    (directory / COOKIES).parent.mkdir(parents=True, exist_ok=True)
    (directory / COOKIES).write_bytes(LOGIN)


@pytest.fixture
def fleet_seeded(tmp_session_root, monkeypatch):
    """``STEALTH_MCP_SEED_SESSION=fleet`` with a ``fleet`` session that exists."""
    monkeypatch.setenv(fleet_session.SEED_SETTING, fleet_session.FLEET_SESSION)
    get_settings.cache_clear()
    asyncio.run(_selection(session=fleet_session.FLEET_SESSION))
    _put_login(tmp_session_root["sessions"] / fleet_session.FLEET_SESSION)
    return tmp_session_root


class TestDisposabilityIsTheRoleNotTheSeed:
    """B1. One test per seed an unnamed spawn can be copied from."""

    async def test_a_clone_copied_from_the_snapshot(self, tmp_session_root):
        selection = await _selection()

        marker = profile_seed.read_marker(Path(selection["user_data_dir"]))

        assert marker["auto_clean"] is True
        assert profile_seed.is_auto(Path(selection["user_data_dir"]))

    async def test_a_clone_copied_from_the_configured_seed_session(self, fleet_seeded):
        selection = await _selection()
        clone = Path(selection["user_data_dir"])

        marker = profile_seed.read_marker(clone)

        assert selection["seeded_from"] == fleet_session.FLEET_SESSION
        assert marker["source_kind"].startswith("explicit"), "the seed is recorded"
        assert marker["auto_clean"] is True, "the REVIEW'S LEAK: a clone nobody keeps"
        assert profile_seed.is_auto(clone)
        assert not profile_seed.is_named(clone)

    async def test_a_clone_copied_from_the_live_default_when_there_is_no_snapshot(
        self, tmp_session_root
    ):
        import shutil

        shutil.rmtree(tmp_session_root["snapshot"])

        clone = Path((await _selection())["user_data_dir"])

        assert profile_seed.is_auto(clone)

    async def test_a_clone_copied_from_nothing_on_a_new_install(self, tmp_session_root):
        import shutil

        shutil.rmtree(tmp_session_root["snapshot"])
        shutil.rmtree(tmp_session_root["master"])

        clone = Path((await _selection())["user_data_dir"])

        assert profile_seed.is_auto(clone)

    async def test_a_clone_made_by_the_retry_path_after_a_failed_first_try(
        self, fleet_seeded
    ):
        first = Path((await _selection())["user_data_dir"])
        retry = Path(
            (
                await clone_storage._fallback_profile_selection(
                    {"profile_role": "clone", "user_data_dir": str(first)}, 1
                )
            )["user_data_dir"]
        )

        assert retry != first
        assert profile_seed.is_auto(retry)

    async def test_a_session_the_caller_named_is_never_disposable(self, fleet_seeded):
        named = Path((await _selection(session="work"))["user_data_dir"])
        from_it = Path(
            (await _selection(session="job", seed_from="work"))["user_data_dir"]
        )

        for session in (named, from_it):
            assert profile_seed.is_named(session), session
            assert not profile_seed.is_auto(session), session
            assert clone_trash.protected(session), session
            assert not clone_trash.disposable(session), session

    async def test_the_close_path_deletes_a_clone_copied_from_the_fleet(
        self, fleet_seeded
    ):
        """The reproduction: the leak was visible at the close path."""
        clone = Path((await _selection())["user_data_dir"])
        cleanup = ProcessCleanup.__new__(ProcessCleanup)
        cleanup.browser_processes = {}
        cleanup._get_active_browser_profile_dirs = lambda: set()
        metadata = {
            "user_data_dir": str(clone),
            "uses_custom_data_dir": True,
            "auto_clone": True,
        }

        assert cleanup._cleanup_profile_for_metadata("i1", metadata) is True

        assert not clone.exists()

    async def test_a_stale_clone_copied_from_the_fleet_is_reaped_and_the_fleet_is_not(
        self, fleet_seeded
    ):
        sessions = fleet_seeded["sessions"]
        clone = Path((await _selection())["user_data_dir"])
        fleet = sessions / fleet_session.FLEET_SESSION
        _age(clone, LONG_AGO)
        _age(fleet, LONG_AGO * 9)

        moved = clone_stale.reap(
            sessions, lambda _p: False, lambda _p: False, clone_storage._spared()
        )

        assert moved == 1
        assert not clone.exists()
        assert _intact(fleet), "the fleet session is a NAMED one and never reaped"


class TestOnePredicateForEverySweep:
    """L6."""

    def test_the_seeds_are_unreachable_when_the_clone_root_is_the_session_root(
        self, tmp_session_root
    ):
        """``STEALTH_MCP_BROWSER_PROFILE_CLONE_ROOT`` may be the session root, and
        then ``master`` and ``master-snapshot`` are children of the root a sweep
        walks. The snapshot's OWN marker says ``default-seed`` (disposable), so
        only its NAME keeps it."""
        root = tmp_session_root["root"]
        for seed in (tmp_session_root["snapshot"], tmp_session_root["master"]):
            profile_seed.write_marker(
                seed,
                source=root,
                source_kind="default-seed",
                seeded_from="x",
                disposable=True,
            )
            _put_login(seed)
            _age(seed, LONG_AGO * 9)

        moved = clone_stale.reap(
            root, lambda _p: False, lambda _p: False, clone_storage._spared()
        )
        victims = clone_storage._idle_autoclones_over_cap(root, 1)
        clone_storage.run_storage_sweep(root, 1, 1)

        assert moved == 0
        assert victims == []
        assert _intact(tmp_session_root["snapshot"])
        assert _intact(tmp_session_root["master"])

    def test_both_sweeps_leave_a_link_alone(self, tmp_session_root):
        root = tmp_session_root["sessions"]
        victim = tmp_session_root["root"] / "victim"
        _put_login(victim)
        profile_seed.write_marker(
            victim,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        link = root / "linked"
        try:
            link.symlink_to(victim, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this account cannot create symlinks")
        _age(victim, LONG_AGO)

        assert not clone_trash.disposable(link)
        assert clone_storage._idle_autoclones_over_cap(root, 1) == []
        assert clone_stale.reap(root, lambda _p: False, lambda _p: False) == 0
        assert _intact(victim)

    def test_the_cap_sweep_never_turns_a_refused_move_into_a_delete(
        self, tmp_session_root, monkeypatch
    ):
        root = tmp_session_root["sessions"]
        clone = root / "over-cap"
        _put_login(clone)
        profile_seed.write_marker(
            clone,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        monkeypatch.setattr(os, "replace", _refuse)

        clone_storage._enforce_clone_storage_cap_in(root, 1)

        assert _intact(clone)

    def test_a_configured_seed_directory_name_is_spared(
        self, tmp_session_root, monkeypatch
    ):
        """The docstring's claim, made true: a seed configured under another name
        is not taken either, whatever its marker says."""
        root = tmp_session_root["root"]
        seed = root / "my-seed"
        _put_login(seed)
        profile_seed.write_marker(
            seed,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        _age(seed, LONG_AGO * 9)
        monkeypatch.setattr(clone_storage, "master_snapshot_dir", lambda: seed)

        assert (
            clone_stale.reap(
                root, lambda _p: False, lambda _p: False, clone_storage._spared()
            )
            == 0
        )
        assert _intact(seed)


def _refuse(*_a, **_k):
    raise PermissionError("a file is open")


class _UnreadableChrome:
    """A browser-named process whose command line psutil is not allowed to read,
    raised by the REAL psutil: the test process's own ``psutil.Process`` answers
    to the name ``chrome.exe`` and refuses its command line with the real
    ``psutil.AccessDenied`` (what an elevated or other user's Chrome does)."""

    def __init__(self, monkeypatch):
        pid = os.getpid()
        real_name, real_cmdline = psutil.Process.name, psutil.Process.cmdline

        def name(process):
            return "chrome.exe" if process.pid == pid else real_name(process)

        def cmdline(process):
            if process.pid == pid:
                raise psutil.AccessDenied(pid, "chrome.exe")
            return real_cmdline(process)

        monkeypatch.setattr(psutil.Process, "name", name)
        monkeypatch.setattr(psutil.Process, "cmdline", cmdline)
        self.pid = pid


class TestUnknownMeansHeld:
    """M3."""

    def test_the_walk_names_the_unreadable_browser(self, monkeypatch):
        chrome = _UnreadableChrome(monkeypatch)

        found = browser_cmdline.unreadable_browsers(
            ProcessCleanup._is_browser_process_name
        )

        assert chrome.pid in found

    def test_a_readable_table_names_nobody(self):
        assert not list(
            browser_cmdline.unreadable_browsers(lambda name: name == "no-such-process")
        )

    def test_the_shared_predicate_reads_the_profile_as_held(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            clone_storage, "_profile_has_running_browser", lambda _p: False
        )
        _UnreadableChrome(monkeypatch)

        assert clone_storage._profile_may_be_held(tmp_path) is True

    def test_the_reaper_keeps_a_stale_clone_while_a_browser_is_unreadable(
        self, tmp_session_root, monkeypatch
    ):
        root = tmp_session_root["sessions"]
        clone = root / "dead-or-not"
        _put_login(clone)
        profile_seed.write_marker(
            clone,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        _age(clone, LONG_AGO)
        monkeypatch.setattr(
            clone_storage, "_profile_has_running_browser", lambda _p: False
        )
        _UnreadableChrome(monkeypatch)

        moved = clone_stale.reap(
            root, lambda _p: False, clone_storage._profile_may_be_held
        )

        assert moved == 0
        assert _intact(clone)

    def test_the_cap_sweep_keeps_it_too(self, tmp_session_root, monkeypatch):
        root = tmp_session_root["sessions"]
        clone = root / "over-cap"
        _put_login(clone)
        profile_seed.write_marker(
            clone,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        monkeypatch.setattr(
            clone_storage, "_profile_has_running_browser", lambda _p: False
        )
        _UnreadableChrome(monkeypatch)

        assert clone_storage._idle_autoclones_over_cap(root, 1) == []
        assert clone_storage._enforce_clone_storage_cap_in(root, 1) == 0
        assert _intact(clone)

    def test_the_close_path_keeps_it_too(self, tmp_session_root, monkeypatch):
        root = tmp_session_root["sessions"]
        clone = root / "closing"
        _put_login(clone)
        profile_seed.write_marker(
            clone,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        cleanup = ProcessCleanup.__new__(ProcessCleanup)
        cleanup.browser_processes = {}
        cleanup._get_active_browser_profile_dirs = lambda: set()
        _UnreadableChrome(monkeypatch)

        assert cleanup._cleanup_profile_dir(str(clone), "i1") is False

        assert _intact(clone)

    def test_the_abandon_path_keeps_it_too(self, tmp_session_root, monkeypatch):
        root = tmp_session_root["sessions"]
        clone = root / "failed"
        _put_login(clone)
        profile_seed.write_marker(
            clone,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        monkeypatch.setattr(
            clone_storage, "_profile_has_running_browser", lambda _p: False
        )
        _UnreadableChrome(monkeypatch)

        asyncio.run(
            clone_protection.abandon(clone, clone_storage._profile_may_be_held, root)
        )

        assert _intact(clone)


class TestLastUseEvidence:
    """L5."""

    @pytest.fixture
    def old_clone(self, tmp_session_root):
        root = tmp_session_root["sessions"]
        clone = root / "old"
        _put_login(clone)
        (clone / "Local State").write_text("{}", encoding="utf-8")
        (clone / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        profile_seed.write_marker(
            clone,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        _age(clone, LONG_AGO)
        return clone

    def test_created_long_ago_and_untouched_is_stale(self, old_clone):
        assert clone_stale.stale_clones(old_clone.parent, lambda _p: False) == [
            old_clone
        ]

    @pytest.mark.parametrize("witness", ["Local State", "Default/Preferences", COOKIES])
    def test_a_file_chrome_rewrote_recently_means_it_was_used(self, old_clone, witness):
        (old_clone / witness).write_bytes(b"rewritten by a browser that just ran")

        assert clone_stale.stale_clones(old_clone.parent, lambda _p: False) == []

    def test_a_marker_with_no_readable_created_at_is_kept(self, old_clone):
        marker = old_clone / profile_seed.MARKER_NAME
        marker.write_text(
            marker.read_text(encoding="utf-8").replace("created_at", "made"),
            encoding="utf-8",
        )

        assert clone_stale.last_used(old_clone) is None
        assert clone_stale.stale_clones(old_clone.parent, lambda _p: False) == []

    def test_the_clone_a_sibling_re_copied_is_not_taken_after_the_scan(self, old_clone):
        def scan_during_which_it_is_opened(_p):
            (old_clone / "Local State").write_text("{x}", encoding="utf-8")
            return False

        assert (
            clone_stale.reap(
                old_clone.parent, lambda _p: False, scan_during_which_it_is_opened
            )
            == 0
        )
        assert _intact(old_clone)


class TestARealFailedLaunchesCloneIsAbandoned:
    """L7: the clone is the one the resolver made, with the marker the resolver
    wrote, reserved the way ``spawn_browser`` reserves it."""

    @pytest.mark.parametrize("configured", [False, True])
    async def test_it_is_deleted_and_released(
        self, tmp_session_root, monkeypatch, configured
    ):
        if configured:
            monkeypatch.setenv(fleet_session.SEED_SETTING, fleet_session.FLEET_SESSION)
            get_settings.cache_clear()
            await _selection(session=fleet_session.FLEET_SESSION)
        clone = Path((await _selection())["user_data_dir"])
        assert clone_storage._clone_dir_is_protected(clone)

        await clone_protection.abandon(
            clone,
            clone_storage._profile_has_running_browser,
            clone_storage.clone_root_dir(),
        )

        assert not clone.exists()
        assert not clone_storage._clone_dir_is_protected(clone)


class TestRetentionAndFirstSweepSafety:
    def test_a_move_that_cannot_be_stamped_is_undone_not_left_with_an_old_mtime(
        self, tmp_session_root, monkeypatch
    ):
        root = tmp_session_root["sessions"]
        clone = root / "unstampable"
        _put_login(clone)
        profile_seed.write_marker(
            clone,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        monkeypatch.setattr(os, "utime", _refuse)

        assert clone_trash.trash(clone, root, lambda _p: False) is None

        assert _intact(clone), "put back where it was, never purged early"
        assert not list(clone_trash.trash_dir(root).iterdir())

    def test_a_discard_whose_expiry_stamp_fails_keeps_the_longer_retention(
        self, tmp_session_root, monkeypatch
    ):
        """``discard`` ages a husk to expired so the next purge takes it; if THAT
        stamp fails, the husk keeps the moment ``trash`` gave it (24 h from now)."""
        root = tmp_session_root["sessions"]
        clone = root / "husk"
        _put_login(clone)
        profile_seed.write_marker(
            clone,
            source=root,
            source_kind="default-seed",
            seeded_from="x",
            disposable=True,
        )
        from stealth_chrome_devtools_mcp.embedded import profile_copy

        def delete_part_of_it(path, *_a, **_k):
            (Path(path) / COOKIES).unlink()

        monkeypatch.setattr(profile_copy, "rmtree_robust", delete_part_of_it)
        real_utime = os.utime

        def utime(path, times=None, **kw):
            if times == (0, 0):
                raise PermissionError("stamp refused")
            return real_utime(path, times, **kw)

        monkeypatch.setattr(os, "utime", utime)

        clone_trash.discard(clone)

        (moved,) = clone_trash.trash_dir(root).iterdir()
        assert moved.stat().st_mtime > time.time() - 600, "retention was shortened"

    def test_the_first_sweep_says_what_it_will_take_before_it_takes_it(
        self, tmp_session_root, monkeypatch
    ):
        root = tmp_session_root["sessions"]
        names = []
        for index in range(3):
            clone = root / f"dead-{index}"
            _put_login(clone)
            profile_seed.write_marker(
                clone,
                source=root,
                source_kind="default-seed",
                seeded_from="x",
                disposable=True,
            )
            _age(clone, LONG_AGO)
            names.append(clone.name)
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(
            debug_logger,
            "log_warning",
            lambda _c, _f, message, *a, **k: seen.append(
                (message, [p.name for p in root.iterdir()])
            ),
        )

        moved = clone_stale.reap(root, lambda _p: False, lambda _p: False)

        assert moved == 3
        (message, present) = next(s for s in seen if "moving 3 clone" in s[0])
        assert all(name in message for name in names)
        assert "bytes" in message
        assert all(name in present for name in names), "said BEFORE anything moved"


def test_the_reaper_has_one_place_that_asks_whether_a_clone_is_disposable(
    tmp_session_root,
):
    """Mutation guard for the duplicated auto check: with ``is_auto`` always true
    a NAMED session must still not be selected, so the check lives in ONE place
    (``clone_trash.disposable``) and a stale named session is simply not a
    candidate."""
    root = tmp_session_root["sessions"]
    named = root / "acme"
    _put_login(named)
    profile_seed.write_marker(
        named,
        source=root,
        source_kind="explicit-session",
        seeded_from="x",
        disposable=False,
    )
    _age(named, LONG_AGO * 9)

    assert clone_stale.stale_clones(root, lambda _p: False) == []
    assert not clone_trash.disposable(named)
