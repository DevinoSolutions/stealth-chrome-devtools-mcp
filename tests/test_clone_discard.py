"""T-5968 -- a disposable clone whose browser is gone is DELETED, wherever the
ending came from, and nothing that is not a disposable clone is ever touched.

The leak (measured 2026-10-21, ~158 dead profiles / ~10 GB under ``sessions/``):
the storage-cap sweep was the only reaper, and it reclaims nothing until the clone
root is over its cap (10 GiB), so every clone that was NOT deleted by its own
close stayed until then. Three ways to not be deleted, one test class each:

* a delete stopped by ONE locked file (``shutil.rmtree`` aborts at the first
  error, leaving a partial tree and, if the marker went first, an unmarked
  husk no scan recognises);
* a launch that failed after the copy, whose clone nothing ever deleted;
* a backend killed hard (Windows ``stop``/``restart`` is a TerminateProcess that
  runs no shutdown hook) whose record recovery dropped after one failed delete.

Hermetic: temp session roots, no Chrome.
"""

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from fakes import FakeBrowserManager
from stealth_chrome_devtools_mcp.embedded import (
    clone_protection,
    clone_stale,
    clone_storage,
    clone_trash,
    profile_copy,
    profile_seed,
)
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

MARKER = profile_seed.MARKER_NAME


@pytest.fixture(autouse=True)
def _no_reservations_left_by_another_test():
    """``clone_protection.PROTECTED`` is process-global: start and end clean."""
    clone_storage._clear_protected_clone_dirs()
    yield
    clone_storage._clear_protected_clone_dirs()


def _clone(root: Path, name: str, *, auto: bool = True, age: float = 0.0) -> Path:
    """A marked profile directory with a few files, ``age`` seconds old."""
    directory = root / name
    (directory / "Default").mkdir(parents=True)
    (directory / "Default" / "Cookies").write_bytes(b"c" * 64)
    (directory / "Local State").write_text("{}", encoding="utf-8")
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))
    kind = "default-seed" if auto else "explicit-seed"
    flag = "true" if auto else "false"
    (directory / MARKER).write_text(
        f'{{"source": "s", "source_kind": "{kind}", "auto_clean": {flag}, '
        f'"created_at": "{created}"}}',
        encoding="utf-8",
    )
    # Chrome's own files age with the clone, or `clone_stale` (which counts the
    # newest of them as the last use) would read every fixture as used just now.
    then = time.time() - age
    for path in (*directory.rglob("*"), directory):
        os.utime(path, (then, then))
    return directory


def _link(link: Path, target: Path, kind: str) -> None:
    """A symlink, or an NTFS junction (the link a delete is most likely to
    follow, because Python reports it as neither a symlink nor a file)."""
    if kind == "junction":
        if sys.platform != "win32":
            pytest.skip("junctions are Windows-only")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")


LINKS = pytest.mark.parametrize("kind", ["symlink", "junction"])


def _intact(directory: Path) -> bool:
    return (directory / "Default" / "Cookies").read_bytes() == b"c" * 64


def _cleanup() -> ProcessCleanup:
    cleanup = ProcessCleanup.__new__(ProcessCleanup)
    cleanup.browser_processes = {}
    cleanup._get_active_browser_profile_dirs = lambda: set()
    return cleanup


class TestNothingElseIsEverTouched:
    """The data-loss direction, pinned before the deleting one."""

    @pytest.mark.parametrize("name", ["master", "master-snapshot", "default"])
    def test_the_shared_profile_its_seed_and_default_are_refused_by_name(
        self, tmp_path, name
    ):
        directory = _clone(tmp_path, name)

        assert clone_trash.protected(directory)
        assert clone_trash.discard(directory) is False
        assert _intact(directory)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows folds the dot")
    def test_a_spelling_the_filesystem_folds_onto_master_is_refused_too(self, tmp_path):
        directory = _clone(tmp_path, "master")

        assert clone_trash.protected(tmp_path / "master.")
        assert clone_trash.discard(tmp_path / "master.") is False
        assert _intact(directory)

    @pytest.mark.parametrize("name", ["fleet", "my-named-session"])
    def test_a_named_session_and_fleet_are_refused_by_their_marker(
        self, tmp_path, name
    ):
        directory = _clone(tmp_path, name, auto=False)

        assert clone_trash.discard(directory) is False
        assert _intact(directory)
        assert not clone_trash.trash_dir(tmp_path).exists()

    def test_the_close_path_leaves_a_named_profile_alone_and_stops_tracking_it(
        self, tmp_path
    ):
        directory = _clone(tmp_path, "keep-me", auto=False)

        assert _cleanup()._cleanup_profile_dir(str(directory), "i1") is True
        assert _intact(directory)

    def test_the_close_path_leaves_master_alone_even_if_a_record_names_it(
        self, tmp_path
    ):
        master = _clone(tmp_path, "master")

        assert _cleanup()._cleanup_profile_dir(str(master), "i1") is True
        assert _intact(master)

    @LINKS
    def test_a_link_inside_a_clone_is_unlinked_not_followed(self, tmp_path, kind):
        master = _clone(tmp_path, "master")
        clone = _clone(tmp_path, "clone-a")
        _link(clone / "Default" / "escape", master, kind)

        assert clone_trash.discard(clone) is True

        assert not clone.exists()
        assert _intact(master), "the delete walked through a link into master"
        assert (master / MARKER).exists()

    @LINKS
    def test_a_clone_that_is_itself_a_link_to_master_only_loses_the_link(
        self, tmp_path, kind, monkeypatch
    ):
        master = _clone(tmp_path, "master")
        clone = tmp_path / "clone-b"
        _link(clone, master, kind)

        def walked(*_args, **_kwargs):
            raise AssertionError("the delete walked a link")

        # Python 3.13's own rmtree happens to unlink a top-level junction and
        # 3.11's refuses it, so the outcome alone cannot pin this: pin that the
        # walking delete is never asked.
        monkeypatch.setattr(profile_copy, "rmtree_robust", walked)

        assert clone_trash.discard(clone) is True

        assert not os.path.lexists(clone)
        assert _intact(master)

    @LINKS
    def test_the_robust_delete_alone_never_follows_a_link_either(self, tmp_path, kind):
        """The primitive underneath: pinned so nobody 'optimises' it."""
        master = _clone(tmp_path, "master")
        clone = _clone(tmp_path, "clone-c")
        _link(clone / "escape", master, kind)

        profile_copy.rmtree_robust(clone)

        assert not clone.exists()
        assert _intact(master)


class TestALockedFileDoesNotStrandTheProfile:
    @pytest.mark.skipif(sys.platform != "win32", reason="needs a real Windows lock")
    def test_a_file_chrome_still_holds_keeps_a_marked_husk_for_the_next_try(
        self, tmp_path
    ):
        directory = _clone(tmp_path, "locked")
        with (directory / "Default" / "Cookies").open("rb"):  # held: no share-delete
            assert clone_trash.discard(directory) is False
            assert profile_seed.is_auto(directory), (
                "the husk lost its marker, so no later sweep can tell it is a clone"
            )
            assert directory in clone_stale.stale_clones(
                tmp_path, lambda _p: False, older_than=0
            )

        assert clone_trash.discard(directory) is True
        assert not directory.exists()

    def test_a_delete_that_removes_nothing_moves_the_clone_aside_expired(
        self, tmp_path, monkeypatch
    ):
        directory = _clone(tmp_path, "stuck")
        monkeypatch.setattr(profile_copy, "rmtree_robust", lambda _p, **_k: None)

        assert clone_trash.discard(directory) is True

        assert not directory.exists()
        (moved,) = clone_trash.trash_dir(tmp_path).iterdir()
        assert (moved / "Default" / "Cookies").exists()
        assert moved.stat().st_mtime < 1, "stamped as already expired"

    def test_the_next_purge_takes_it_whatever_the_retention_window_is(
        self, tmp_path, monkeypatch
    ):
        directory = _clone(tmp_path, "stuck-2")
        monkeypatch.setattr(profile_copy, "rmtree_robust", lambda _p, **_k: None)
        clone_trash.discard(directory)
        monkeypatch.undo()

        clone_storage._enforce_clone_storage_cap_in(tmp_path, cap_bytes=0)

        assert list(clone_trash.trash_dir(tmp_path).iterdir()) == []

    def test_an_unmarked_directory_is_not_moved_into_a_trash_it_has_no_root_for(
        self, tmp_path, monkeypatch
    ):
        temp_profile = tmp_path / "uc_temp"
        temp_profile.mkdir()
        (temp_profile / "x").write_text("x")
        monkeypatch.setattr(profile_copy, "rmtree_robust", lambda _p, **_k: None)

        assert clone_trash.discard(temp_profile) is False
        assert temp_profile.exists()
        assert not clone_trash.trash_dir(tmp_path).exists()

    def test_the_close_path_deletes_a_clone_through_discard(self, tmp_path):
        directory = _clone(tmp_path, "closed")

        assert _cleanup()._cleanup_profile_dir(str(directory), "i1") is True
        assert not directory.exists()


def _failing_manager(seen: list[Path]) -> FakeBrowserManager:
    class Failing(FakeBrowserManager):
        async def spawn_browser(self, options):
            directory = Path(options.user_data_dir)
            assert directory.is_dir(), "the copy must exist while the launch runs"
            seen.append(directory)
            raise RuntimeError("Failed to connect to browser")

    return Failing(
        spawn_instance=SimpleNamespace(
            instance_id="i1", state="active", headless=True, viewport={}
        )
    )


class TestAFailedLaunchTakesItsCloneWithIt:
    async def test_every_attempt_clone_is_deleted_and_master_is_untouched(
        self, tmp_session_root, call_tool, patched_server
    ):
        seen: list[Path] = []
        srv = patched_server(browser_manager=_failing_manager(seen))

        with pytest.raises(Exception, match="Failed to connect"):
            await call_tool(srv, "spawn_browser", headless=True, sandbox=False)

        assert len(seen) == 3, seen
        assert len(set(seen)) == 3, "each attempt is its own directory"
        for directory in seen:
            assert not directory.exists(), f"left behind: {directory}"
        assert list(tmp_session_root["sessions"].iterdir()) == []
        assert not clone_storage._PROTECTED_CLONE_DIRS
        assert (tmp_session_root["master"] / "Default" / "Preferences").exists()
        assert (tmp_session_root["snapshot"] / "Default" / "Preferences").exists()

    def test_a_directory_a_browser_still_holds_is_kept_but_released(self, tmp_path):
        directory = _clone(tmp_path, "held")
        clone_storage._protect_clone_dir(directory)

        asyncio.run(clone_protection.abandon(directory, lambda _p: True, tmp_path))

        assert _intact(directory)
        assert not clone_storage._clone_dir_is_protected(directory)

    async def test_a_named_session_that_fails_to_launch_keeps_its_profile(
        self, tmp_session_root, call_tool, patched_server
    ):
        seen: list[Path] = []
        srv = patched_server(browser_manager=_failing_manager(seen))

        with pytest.raises(Exception, match="Failed to connect"):
            await call_tool(
                srv, "spawn_browser", headless=True, sandbox=False, session="mine"
            )

        assert seen, "the launch was never attempted"
        assert all(d.is_dir() for d in seen), "a NAMED profile was deleted"


class TestADeadBackendsClonesAreReaped:
    def test_a_stale_unheld_marked_clone_is_moved_aside(self, tmp_path):
        old = _clone(tmp_path, "dead", age=3 * 3600)

        assert clone_stale.reap(tmp_path, lambda _p: False, lambda _p: False) == 1

        assert not old.exists()
        (moved,) = clone_trash.trash_dir(tmp_path).iterdir()
        assert (moved / "Default" / "Cookies").exists(), "recoverable, not deleted"

    def test_nothing_that_might_be_alive_or_wanted_is_taken(self, tmp_path):
        fresh = _clone(tmp_path, "fresh", age=60)
        held = _clone(tmp_path, "held", age=9 * 3600)
        reserved = _clone(tmp_path, "reserved", age=9 * 3600)
        named = _clone(tmp_path, "named", auto=False, age=9 * 3600)
        master = _clone(tmp_path, "master", age=9 * 3600)
        unmarked = tmp_path / "unmarked"
        unmarked.mkdir()
        (unmarked / "x").write_text("x")
        legacy = _clone(tmp_path, "legacy", age=9 * 3600)
        (legacy / MARKER).write_text(
            '{"source_kind": "default-seed", "created_at": "2020-01-01T00:00:00Z"}',
            encoding="utf-8",
        )

        # The seed's own marker says AUTO: `_refresh_master_snapshot_if_safe` writes
        # it with a `default-seed-*` source kind, so only its NAME keeps it.
        snapshot = _clone(tmp_path, "master-snapshot", age=9 * 3600)
        default = _clone(tmp_path, "default", age=9 * 3600)
        # A seed configured under another name inside the clone root, and a fleet
        # directory whose marker (wrongly) says auto: spared by the caller's names.
        custom_seed = _clone(tmp_path, "my-seed", age=9 * 3600)
        fleet = _clone(tmp_path, "fleet", age=9 * 3600)
        broken = _clone(tmp_path, "broken-marker", age=9 * 3600)
        (broken / MARKER).write_text("{not json", encoding="utf-8")

        moved = clone_stale.reap(
            tmp_path,
            lambda p: p.name == "reserved",
            lambda p: p.name == "held",
            spared=("fleet", "my-seed"),
        )

        assert moved == 0
        for kept in (
            *(fresh, held, reserved, named, master, unmarked, legacy),
            *(snapshot, default, custom_seed, fleet, broken),
        ):
            assert kept.exists(), kept
        assert not clone_trash.trash_dir(tmp_path).exists()

    def test_a_scratch_directory_and_the_trash_are_never_selected(self, tmp_path):
        staging = _clone(tmp_path, f"x{profile_copy.STAGING_SUFFIX}1-1", age=9 * 3600)
        previous = _clone(tmp_path, f"y{profile_copy.PREVIOUS_SUFFIX}", age=9 * 3600)

        assert clone_stale.stale_clones(tmp_path, lambda _p: False) == []
        assert staging.exists() and previous.exists()

    def test_the_sweep_spares_fleet_and_the_seeds_by_name_whatever_their_marker(
        self, tmp_path
    ):
        """The seed's marker says auto (`default-seed-*`), and `fleet`'s would if
        it ever lost its explicit one: the NAME is what the sweep goes by."""
        fleet = _clone(tmp_path, "fleet", age=9 * 3600)
        snapshot = _clone(tmp_path, "master-snapshot", age=9 * 3600)
        dead = _clone(tmp_path, "dead", age=9 * 3600)

        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert _intact(fleet) and _intact(snapshot)
        assert not dead.exists()

    def test_the_storage_sweep_runs_it_without_a_cap_being_exceeded(self, tmp_path):
        old = _clone(tmp_path, "dead", age=3 * 3600)
        young = _clone(tmp_path, "young", age=120)

        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert not old.exists()
        assert young.exists()


@contextlib.contextmanager
def _a_live_chrome_on(profile: Path, tmp_path: Path):
    """A REAL process the production process-table scan reads as a Chrome BROWSER
    on *profile*: a copy of the interpreter named ``chrome`` (the scan matches on
    the process name) whose argv carries ``--user-data-dir`` and no ``--type``.
    Nothing records it: no instance, no registry entry, no lock file -- the
    browser of a backend that is dead or is another chat's."""
    exe = tmp_path / ("chrome.exe" if sys.platform == "win32" else "chrome")
    base = Path(getattr(sys, "_base_executable", sys.executable))
    shutil.copy(base, exe)
    # A copied interpreter finds neither its DLLs nor its stdlib beside the copy
    # (a uv-managed Python on Windows): bring the DLLs and say where the stdlib is.
    for dll in base.parent.glob("*.dll"):
        shutil.copy(dll, tmp_path)
    child = subprocess.Popen(
        [str(exe), "-c", "import time; time.sleep(120)", f"--user-data-dir={profile}"],
        env={**os.environ, "PYTHONHOME": sys.base_prefix},
    )
    try:
        time.sleep(0.5)
        if child.poll() is not None:
            pytest.skip("this interpreter cannot run from a copy of itself")
        deadline = time.monotonic() + 15
        while not clone_storage._profile_has_running_browser(profile):
            assert time.monotonic() < deadline, "the scan never saw the process"
            time.sleep(0.1)
        yield child.pid
    finally:
        child.kill()
        child.wait()
        assert not psutil.pid_exists(child.pid) or child.poll() is not None


class TestALiveChromeIsNeverReaped:
    """The stale sweep's data-loss direction (owner rule): a window parked on a
    captcha, password or 2FA page that its owner will act on later is a clone
    older than 2 h with NO instance, holder or reservation naming it -- and it
    must survive. 'Held' is the live process table plus Chrome's own lock
    (``profile_lock``), never this backend's memory."""

    def test_a_3h_old_clone_with_a_live_process_and_no_record_is_kept(self, tmp_path):
        clone = _clone(tmp_path / "sessions", "parked", age=3 * 3600)
        with _a_live_chrome_on(clone, tmp_path):
            clone_storage.run_storage_sweep(clone.parent, 0, 0)

            assert clone.exists(), "a clone with a live Chrome on it was evicted"
            assert _intact(clone)
            assert not clone_trash.trash_dir(clone.parent).exists()

    def test_the_same_clone_with_no_process_is_reaped(self, tmp_path):
        clone = _clone(tmp_path / "sessions", "gone", age=3 * 3600)

        clone_storage.run_storage_sweep(clone.parent, 0, 0)

        assert not clone.exists()
        (moved,) = clone_trash.trash_dir(clone.parent).iterdir()
        assert (moved / "Default" / "Cookies").exists()

    def test_a_process_with_an_unreadable_argv_counts_as_holding_it(
        self, tmp_path, monkeypatch
    ):
        """The exec-window trap: an argv that cannot be read is not 'gone'."""
        clone = _clone(tmp_path / "sessions", "opaque", age=3 * 3600)
        monkeypatch.setattr(
            clone_storage.process_cleanup,
            "_get_browser_pids_for_profile",
            lambda _dir: {os.getpid()},
        )
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.browser_cmdline.arguments",
            lambda _pid: [],
        )

        clone_storage.run_storage_sweep(clone.parent, 0, 0)

        assert clone.exists()


def _boom(*_args, **_kwargs):
    raise PermissionError("a file in it is open")


def _stamp_now(directory: Path) -> None:
    """Re-copy the way a sibling backend's spawn does: a marker made just now."""
    marker = directory / MARKER
    marker.write_text(
        marker.read_text(encoding="utf-8").replace(
            str(profile_seed.read_marker(directory)["created_at"]),
            profile_seed.stamp_now(),
        ),
        encoding="utf-8",
    )


class TestTheReaperDecidesFromWhatItCanEstablish:
    """Every way the answer could not be had reads as KEEP."""

    def test_a_held_answer_that_raises_is_held(self, tmp_path):
        clone = _clone(tmp_path, "unknowable", age=9 * 3600)

        def cannot_say(_profile):
            raise RuntimeError("the process table could not be read")

        assert clone_stale.reap(tmp_path, lambda _p: False, cannot_say) == 0
        assert _intact(clone)

    def test_a_refused_move_is_never_turned_into_a_delete(self, tmp_path, monkeypatch):
        """A Windows rename that fails is more often a process with files open than
        a bad path, and the cap sweep's delete-instead fallback is NOT inherited."""
        clone = _clone(tmp_path, "open-handle", age=9 * 3600)
        monkeypatch.setattr(os, "replace", _boom)

        assert clone_stale.reap(tmp_path, lambda _p: False, lambda _p: False) == 0
        assert _intact(clone)
        assert profile_seed.is_auto(clone), "the marker went with the delete"

    def test_the_cap_sweep_asks_the_same_question_and_does_not_delete_either(
        self, tmp_path, monkeypatch
    ):
        """ONE predicate (T-5968 review L6): the cap sweep used to fall back to
        ``rmtree`` when the move was refused, which the reaper never did. A clone
        still over the cap waits for the next sweep."""
        clone = _clone(tmp_path, "over-cap")
        monkeypatch.setattr(os, "replace", _boom)

        assert clone_trash.trash(clone, tmp_path, lambda _p: False) is None
        assert _intact(clone)

    def test_a_clone_a_sibling_backend_publishes_during_the_scan_is_not_taken(
        self, tmp_path
    ):
        """The process-table scan is slow enough for a sibling backend's spawn to
        publish a fresh copy under this very name between the listing and the
        move; the question is asked again AFTER the scan, not before it."""
        clone = _clone(tmp_path, "recopied", age=9 * 3600)

        def scan_during_which_a_sibling_publishes(_profile):
            _stamp_now(clone)
            return False  # nothing was running when the scan looked

        assert (
            clone_stale.reap(
                tmp_path, lambda _p: False, scan_during_which_a_sibling_publishes
            )
            == 0
        )
        assert _intact(clone)
        assert not clone_trash.trash_dir(tmp_path).exists()

    def test_a_stamp_from_the_future_is_not_stale(self, tmp_path):
        clone = _clone(tmp_path, "skewed", age=-9 * 3600)

        assert clone_stale.reap(tmp_path, lambda _p: False, lambda _p: False) == 0
        assert _intact(clone)

    def test_only_a_direct_child_of_the_clone_root_is_ever_looked_at(self, tmp_path):
        nested = _clone(tmp_path / "group", "deep", age=9 * 3600)

        assert clone_stale.reap(tmp_path, lambda _p: False, lambda _p: False) == 0
        assert _intact(nested)

    @LINKS
    def test_a_link_in_the_clone_root_is_neither_followed_nor_moved(
        self, tmp_path, kind
    ):
        target = _clone(tmp_path / "elsewhere", "profile", age=9 * 3600)
        (tmp_path / "root").mkdir()
        link = tmp_path / "root" / "stale-looking"
        _link(link, target, kind)
        root = tmp_path / "root"

        assert clone_stale.stale_clones(root, lambda _p: False) == []
        assert clone_stale.reap(root, lambda _p: False, lambda _p: False) == 0
        assert os.path.lexists(link)
        assert _intact(target)


class TestAbandonOnlyDeletesWhatItMayAndNeverFailsTheSpawn:
    def test_an_idle_clone_directly_under_the_root_is_deleted_and_released(
        self, tmp_path
    ):
        directory = _clone(tmp_path, "failed-attempt")
        clone_storage._protect_clone_dir(directory)

        asyncio.run(clone_protection.abandon(directory, lambda _p: False, tmp_path))

        assert not directory.exists()
        assert not clone_storage._clone_dir_is_protected(directory)

    def test_a_directory_outside_the_clone_root_is_released_and_kept(self, tmp_path):
        root = tmp_path / "sessions"
        root.mkdir()
        outside = _clone(tmp_path / "elsewhere", "profile")
        deeper = _clone(root / "group", "profile")

        for directory in (outside, deeper):
            asyncio.run(clone_protection.abandon(directory, lambda _p: False, root))

            assert _intact(directory), directory

    def test_a_held_answer_that_raises_keeps_the_clone(self, tmp_path):
        directory = _clone(tmp_path, "unknowable")

        def cannot_say(_profile):
            raise RuntimeError("the process table could not be read")

        asyncio.run(clone_protection.abandon(directory, cannot_say, tmp_path))

        assert _intact(directory)

    def test_the_delete_runs_off_the_event_loop_thread(self, tmp_path, monkeypatch):
        directory = _clone(tmp_path, "offloaded")
        seen = []
        monkeypatch.setattr(
            clone_trash, "discard", lambda _p: seen.append(threading.get_ident())
        )

        asyncio.run(clone_protection.abandon(directory, lambda _p: False, tmp_path))

        assert seen
        assert seen[0] != threading.get_ident(), "the delete ran on the loop"

    def test_a_delete_that_raises_is_logged_and_the_clone_is_still_released(
        self, tmp_path, monkeypatch
    ):
        directory = _clone(tmp_path, "unlucky")
        clone_storage._protect_clone_dir(directory)

        def explode(_path):
            raise OSError("the volume went away")

        monkeypatch.setattr(clone_trash, "discard", explode)

        asyncio.run(clone_protection.abandon(directory, lambda _p: False, tmp_path))

        assert not clone_storage._clone_dir_is_protected(directory)

    async def test_a_failing_delete_does_not_replace_the_spawns_own_error(
        self, tmp_session_root, call_tool, patched_server, monkeypatch
    ):
        monkeypatch.setattr(clone_trash, "discard", _boom)
        seen: list[Path] = []
        srv = patched_server(browser_manager=_failing_manager(seen))

        with pytest.raises(Exception, match="Failed to connect"):
            await call_tool(srv, "spawn_browser", headless=True, sandbox=False)

        assert seen
        assert not clone_storage._PROTECTED_CLONE_DIRS
