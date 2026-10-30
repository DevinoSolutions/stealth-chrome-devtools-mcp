"""F-1008: a profile reads HELD for the whole of our own browser launch, and the
lock-read edges F-998's review named resolve toward HELD or parse as Chromium does.

Before a starting browser is visible to either of Chrome's witnesses -- the
process scan matches a command line, which on Linux is EMPTY while the launcher
``exec``s the real binary, and ``SingletonLock`` is written 0.1-0.6 s after
that -- ``profile_lock.profile_hold`` answered FREE. F-998's review measured 4
FREE polls inside one launch. A second launch, copy or trim deciding in that
window takes a profile a browser is starting on: the master-profile-loss class.

Hermetic: no Chrome starts. The launch is ``BrowserManager._launch_browser``
itself with nodriver's ``start`` (or the delegated launch) replaced by a fake
that polls ``profile_hold`` the way a concurrent spawn or sweep would, with the
process scan answering nothing and no ``SingletonLock`` written -- exactly the
state a real launch is in during the window.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path

import psutil
import pytest

from fakes import LaunchedBrowser, nodriver_registry, write_singleton
from stealth_chrome_devtools_mcp.embedded import (
    browser_manager,
    clone_storage,
    profile_copy,
    profile_lock,
    spawn_budget,
    spawn_leak,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup


def no_pids(_user_data_dir):
    """The process scan during the window: the browser is not visible yet."""
    return set()


def hold(profile: Path):
    return profile_lock.profile_hold(profile, no_pids)


def age_marker(profile: Path, marker: Path, since_failure: float):
    """``hold`` with a failed launch's ``marker`` dated as if it failed
    ``since_failure`` seconds ago. A failed launch dates its marker
    ``LAUNCH_BUDGET - FAILED_LAUNCH_GRACE`` back, and ``hold`` reads the wall
    clock, so the date is taken from the clock at the check, never from the
    marker's own mtime (a slow lane between the two aged it for nothing)."""
    dated = (
        time.time()
        - (
            spawn_budget.LAUNCH_BUDGET_SECONDS
            - profile_lock.FAILED_LAUNCH_GRACE_SECONDS
        )
        - since_failure
    )
    os.utime(marker, (dated, dated))
    return hold(profile)


def dead_pid() -> int:
    for pid in range(999_999, 900_000, -1):
        if not psutil.pid_exists(pid):
            return pid
    raise RuntimeError("no free pid in the probe range")


def leftovers(profile: Path) -> list[str]:
    return sorted(p.name for p in profile.iterdir() if "stealth-launch" in p.name)


@pytest.fixture
def launch_seams(monkeypatch):
    """Everything ``_launch_browser`` does around the start, made inert."""
    monkeypatch.setattr(browser_manager.browser_connect, "install", lambda: None)
    monkeypatch.setattr(
        browser_manager.login_persistence, "remove_saved_tabs", lambda _d: []
    )

    def delegate(flag: bool) -> None:
        monkeypatch.setattr(
            browser_manager.desktop_launch, "should_delegate", lambda _h: flag
        )

    return delegate


class TestOurLaunchHoldsTheProfile:
    async def test_the_profile_reads_held_while_nodriver_is_starting_chrome(
        self, tmp_path, monkeypatch, launch_seams
    ):
        launch_seams(False)
        seen = []
        started = object()

        async def start(config):
            seen.append(hold(tmp_path))
            return started

        monkeypatch.setattr(browser_manager.uc, "start", start)
        browser = await BrowserManager()._launch_browser(
            BrowserOptions(user_data_dir=str(tmp_path)), "exe", [], _Attempt()
        )

        assert browser is started
        assert len(seen) == 1
        assert seen[0] is not None, "a profile being launched on read as FREE"
        # The holder is the LAUNCHER, never a browser: no pid a consumer could
        # adopt or kill (browser_reattach.held_by reads a hold's pid as one).
        assert seen[0].pid is None
        assert "launch marker" in seen[0].reason
        # Once the launch returns, Chrome's own witnesses carry the answer.
        assert hold(tmp_path) is None
        assert leftovers(tmp_path) == []

    async def test_a_delegated_launch_holds_it_too(
        self, tmp_path, monkeypatch, launch_seams
    ):
        launch_seams(True)
        seen = []

        async def launch_and_attach(_exe, _args, _directory):
            seen.append(hold(tmp_path))
            return "browser", 4242

        monkeypatch.setattr(
            browser_manager.desktop_launch, "launch_and_attach", launch_and_attach
        )
        await BrowserManager()._launch_browser(
            BrowserOptions(user_data_dir=str(tmp_path)), "exe", [], _Attempt()
        )

        assert seen and seen[0] is not None
        assert hold(tmp_path) is None

    async def test_a_launch_that_fails_holds_it_while_it_starts(
        self, tmp_path, monkeypatch, launch_seams
    ):
        launch_seams(False)
        seen = []

        async def start(config):
            seen.append(hold(tmp_path))
            raise RuntimeError("Failed to connect to browser")

        monkeypatch.setattr(browser_manager.uc, "start", start)
        with pytest.raises(RuntimeError):
            await BrowserManager()._launch_browser(
                BrowserOptions(user_data_dir=str(tmp_path)), "exe", [], _Attempt()
            )

        assert seen and seen[0] is not None


class _Attempt:
    config = None


class _Reaper:
    """``process_cleanup`` as a failed spawn's reap sees it: *running* is what
    the scan reports on the profile, *kills* whether a kill succeeds."""

    def __init__(self, running, kills=True):
        self.running, self.kills, self.killed = set(running), kills, []

    def _get_browser_pids_for_profile(self, _user_data_dir):
        return set(self.running)

    def _kill_process_by_pid(self, pid, _instance_id="unknown"):
        self.killed.append(pid)
        return self.kills


async def _fail_launch(profile: Path, monkeypatch, error: BaseException):
    """``_launch_browser`` with nodriver's start raising *error* after Chrome
    "started": the state ``spawn_browser``'s teardown is entered in."""

    async def start(config):
        raise error

    monkeypatch.setattr(browser_manager.uc, "start", start)
    options = BrowserOptions(user_data_dir=str(profile))
    with pytest.raises(type(error)):
        await BrowserManager()._launch_browser(options, "exe", [], _Attempt())
    return options


class TestAFailedLaunchHoldsUntilItsChromeIsReaped:
    """The Chrome a failed start launched runs until the failed spawn's
    teardown reaps it, AFTER ``_launch_browser`` has raised: removing the
    marker in its ``finally`` showed that browser's profile FREE meanwhile."""

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(RuntimeError("Failed to connect to browser"), id="raised"),
            pytest.param(asyncio.CancelledError(), id="cancelled"),
        ],
    )
    async def test_held_until_the_teardown_has_reaped_it(
        self, tmp_path, monkeypatch, launch_seams, error
    ):
        launch_seams(False)
        options = await _fail_launch(tmp_path, monkeypatch, error)
        assert hold(tmp_path) is not None, "a failed launch's Chrome read as FREE"

        reaper = _Reaper(running={4242})
        monkeypatch.setattr(browser_manager, "process_cleanup", reaper)
        monkeypatch.setattr(spawn_leak, "launched_pid", lambda _attempt: 4242)
        await BrowserManager()._teardown_failed_spawn(
            "error", "i-1", None, None, options, _Attempt()
        )

        assert reaper.killed == [4242]
        assert hold(tmp_path) is None
        assert leftovers(tmp_path) == []

    async def test_a_reap_that_fails_leaves_it_held(
        self, tmp_path, monkeypatch, launch_seams
    ):
        launch_seams(False)
        options = await _fail_launch(tmp_path, monkeypatch, RuntimeError("boom"))
        monkeypatch.setattr(
            browser_manager, "process_cleanup", _Reaper(running={4242}, kills=False)
        )
        monkeypatch.setattr(spawn_leak, "launched_pid", lambda _attempt: 4242)
        await BrowserManager()._teardown_failed_spawn(
            "error", "i-1", None, None, options, _Attempt()
        )
        assert hold(tmp_path) is not None, "a Chrome the reap could not end read FREE"

    @pytest.mark.parametrize(
        ("launched", "held"),
        [
            pytest.param(os.getpid, True, id="the os still has it"),
            pytest.param(dead_pid, False, id="the os does not"),
        ],
    )
    async def test_a_scan_that_misses_the_pid_frees_it_only_if_the_pid_is_gone(
        self, tmp_path, monkeypatch, launch_seams, launched, held
    ):
        """In its exec window a live Chrome reads an EMPTY argv (F-998), so the
        profile scan misses it: that miss alone freed the profile under it."""
        launch_seams(False)
        options = await _fail_launch(tmp_path, monkeypatch, RuntimeError("boom"))
        pid = launched()
        reaper = _Reaper(running=set())
        monkeypatch.setattr(browser_manager, "process_cleanup", reaper)
        monkeypatch.setattr(spawn_leak, "launched_pid", lambda _attempt: pid)
        await BrowserManager()._teardown_failed_spawn(
            "error", "i-1", None, None, options, _Attempt()
        )
        assert reaper.killed == []
        assert (hold(tmp_path) is not None) is held

    async def test_a_start_that_registered_nothing_frees_it_for_the_retry(
        self, tmp_path, monkeypatch, launch_seams
    ):
        """nodriver raised before it registered a ``Browser`` (no executable
        there), so nothing was launched: the retry of the same ``default``
        directory must not read this attempt's own marker as a holder."""
        launch_seams(False)

        async def start(config):
            raise FileNotFoundError("Could not determine browser executable.")

        monkeypatch.setattr(browser_manager.uc, "start", start)
        options = BrowserOptions(user_data_dir=str(tmp_path))
        attempt = spawn_leak.Attempt()
        with pytest.raises(FileNotFoundError):
            await BrowserManager()._launch_browser(options, "exe", [], attempt)
        assert attempt.config is not None
        monkeypatch.setattr(browser_manager, "process_cleanup", _Reaper(set()))
        await BrowserManager()._teardown_failed_spawn(
            "error", "i-1", None, None, options, attempt
        )
        assert clone_storage._profile_hold(tmp_path) is None, "our own marker held it"

    async def test_a_start_whose_chrome_was_collected_frees_it(
        self, tmp_path, monkeypatch, launch_seams
    ):
        """Registered, and asyncio has the exit code: that Chrome is gone."""
        launch_seams(False)
        attempt = spawn_leak.Attempt()

        async def start(config):
            exited = LaunchedBrowser(config, dead_pid())
            exited._process.returncode = 0
            registry.add(exited)
            raise RuntimeError("Failed to connect to browser")

        monkeypatch.setattr(browser_manager.uc, "start", start)
        options = BrowserOptions(user_data_dir=str(tmp_path))
        with nodriver_registry() as registry:
            with pytest.raises(RuntimeError):
                await BrowserManager()._launch_browser(options, "exe", [], attempt)
            monkeypatch.setattr(browser_manager, "process_cleanup", _Reaper(set()))
            await BrowserManager()._teardown_failed_spawn(
                "error", "i-1", None, None, options, attempt
            )
        assert hold(tmp_path) is None

    async def test_a_registered_browser_with_no_process_keeps_it(
        self, tmp_path, monkeypatch, launch_seams
    ):
        """Registered with no ``_process`` (nodriver's connect-to-existing
        shape) proves nothing about what is on the profile: it keeps the grace."""
        launch_seams(False)
        attempt = spawn_leak.Attempt()

        async def start(config):
            attached = LaunchedBrowser(config, os.getpid())
            attached._process = attached._process_pid = None
            registry.add(attached)
            raise RuntimeError("Failed to connect to browser")

        monkeypatch.setattr(browser_manager.uc, "start", start)
        options = BrowserOptions(user_data_dir=str(tmp_path))
        with nodriver_registry() as registry:
            with pytest.raises(RuntimeError):
                await BrowserManager()._launch_browser(options, "exe", [], attempt)
            monkeypatch.setattr(browser_manager, "process_cleanup", _Reaper(set()))
            await BrowserManager()._teardown_failed_spawn(
                "error", "i-1", None, None, options, attempt
            )
        assert hold(tmp_path) is not None, "a Browser with no process read as gone"

    async def test_a_delegated_launch_that_failed_keeps_its_grace(
        self, tmp_path, monkeypatch, launch_seams
    ):
        """It leaves no config, and ``launch_and_attach`` kills only a Chrome
        whose pid and start time it learned: after its task ran, a Chrome can
        be live on the desktop with nothing here able to name it."""
        launch_seams(True)

        async def launch_and_attach(_exe, _args, _directory):
            raise RuntimeError("delegated launch timed out")

        monkeypatch.setattr(
            browser_manager.desktop_launch, "launch_and_attach", launch_and_attach
        )
        options = BrowserOptions(user_data_dir=str(tmp_path))
        attempt = spawn_leak.Attempt()
        with pytest.raises(RuntimeError):
            await BrowserManager()._launch_browser(options, "exe", [], attempt)
        assert attempt.config is None
        monkeypatch.setattr(browser_manager, "process_cleanup", _Reaper(set()))
        await BrowserManager()._teardown_failed_spawn(
            "error", "i-1", None, None, options, attempt
        )
        assert hold(tmp_path) is not None, "an unnamed delegated Chrome read FREE"

    async def test_one_nothing_can_reap_holds_for_the_grace_only(
        self, tmp_path, monkeypatch, launch_seams
    ):
        launch_seams(False)
        await _fail_launch(tmp_path, monkeypatch, RuntimeError("boom"))
        assert hold(tmp_path) is not None, "a failed launch's Chrome read as FREE"

        grace = profile_lock.FAILED_LAUNCH_GRACE_SECONDS
        # Longer than the reap it waits for; far shorter than a launch, or a
        # failed `default` launch would turn its own retry into a clone.
        rungs = sum(patience for _m, patience, _o in ProcessCleanup._KILL_RUNGS)
        assert rungs < grace < spawn_budget.LAUNCH_BUDGET_SECONDS / 4
        marker = tmp_path / profile_lock.LAUNCH_NAME

        assert age_marker(tmp_path, marker, grace - 2) is not None
        assert age_marker(tmp_path, marker, grace + 2) is None

    async def test_a_slow_lane_between_the_write_and_the_check_does_not_age_it(
        self, tmp_path, monkeypatch, launch_seams
    ):
        """``hold`` compares against the wall clock, so the ageing is dated
        from the clock at the check, not from the marker's own mtime: a starved
        lane that spends over 2 s between the two read ``grace - 2`` as expired."""
        launch_seams(False)
        await _fail_launch(tmp_path, monkeypatch, RuntimeError("boom"))
        marker = tmp_path / profile_lock.LAUNCH_NAME
        await asyncio.sleep(2.2)
        grace = profile_lock.FAILED_LAUNCH_GRACE_SECONDS
        assert age_marker(tmp_path, marker, grace - 2) is not None


class TestTheLaunchMarker:
    def test_two_launches_in_one_backend_hold_until_the_last_ends(self, tmp_path):
        with profile_lock.launching(tmp_path):
            with profile_lock.launching(str(tmp_path)):
                assert hold(tmp_path) is not None
            assert hold(tmp_path) is not None, "the first launch to end freed it"
        assert hold(tmp_path) is None

    def test_a_second_claim_re_dates_the_marker(self, tmp_path):
        """And the directory, which dates it when its own ``lstat`` fails."""
        marker = tmp_path / profile_lock.LAUNCH_NAME
        long_ago = time.time() - 60
        with profile_lock.launching(tmp_path):
            os.utime(marker, (long_ago, long_ago))
            os.utime(tmp_path, (long_ago, long_ago))
            with profile_lock.launching(tmp_path):
                assert marker.stat().st_mtime > long_ago + 30
                assert tmp_path.stat().st_mtime > long_ago + 30

    def test_a_later_launch_is_held_for_its_own_whole_budget(self, tmp_path):
        """The first launch started a whole budget ago; the second has just
        started and must not read FREE under it."""
        marker = tmp_path / profile_lock.LAUNCH_NAME
        then = time.time() - spawn_budget.LAUNCH_BUDGET_SECONDS - 5
        with profile_lock.launching(tmp_path):
            os.utime(marker, (then, then))
            with profile_lock.launching(tmp_path):
                assert hold(tmp_path) is not None, "the newer launch read FREE"

    def test_a_marker_whose_age_cannot_be_read_is_dated_by_its_directory(
        self, tmp_path, monkeypatch
    ):
        """It held the profile for ever: an unanswerable age read as 0."""
        (tmp_path / profile_lock.LAUNCH_NAME).write_text(
            f"{socket.gethostname()}-{os.getpid()}", encoding="utf-8"
        )
        real_lstat = Path.lstat

        def lstat(self, *args, **kwargs):
            if self.name == profile_lock.LAUNCH_NAME:
                raise PermissionError(13, "denied", str(self))
            return real_lstat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", lstat)
        fresh = hold(tmp_path)
        stale = time.time() - spawn_budget.LAUNCH_BUDGET_SECONDS - 5
        os.utime(tmp_path, (stale, stale))
        aged = hold(tmp_path)
        monkeypatch.undo()

        assert fresh is not None
        assert aged is None, "a marker no age could be read for held it for ever"

    def test_an_unlink_refused_while_it_is_read_is_retried(self, tmp_path, monkeypatch):
        """Windows: a ``profile_hold`` reading the marker has it open, and an
        open file cannot be deleted. One refusal left it holding the profile."""
        real_unlink = Path.unlink
        refused = []

        def unlink(self, *args, **kwargs):
            if self.name == profile_lock.LAUNCH_NAME and len(refused) < 2:
                refused.append(self.name)
                raise PermissionError(13, "in use", str(self))
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        with profile_lock.launching(tmp_path):
            pass
        monkeypatch.undo()

        assert len(refused) == 2
        assert leftovers(tmp_path) == []
        assert hold(tmp_path) is None

    async def test_an_unlink_refused_every_time_still_launches(
        self, tmp_path, monkeypatch, launch_seams
    ):
        """The launch succeeds; the marker left behind holds the profile until
        the age bound drops it, the survivable direction."""
        launch_seams(False)
        started = object()

        async def start(config):
            return started

        def refuse(self, *args, **kwargs):
            raise PermissionError(13, "in use", str(self))

        monkeypatch.setattr(browser_manager.uc, "start", start)
        monkeypatch.setattr(Path, "unlink", refuse)
        browser = await BrowserManager()._launch_browser(
            BrowserOptions(user_data_dir=str(tmp_path)), "exe", [], _Attempt()
        )
        monkeypatch.undo()

        assert browser is started
        left = hold(tmp_path)
        assert left is not None
        assert left.pid is None
        stale = time.time() - spawn_budget.LAUNCH_BUDGET_SECONDS - 5
        os.utime(tmp_path / profile_lock.LAUNCH_NAME, (stale, stale))
        assert hold(tmp_path) is None

    def test_a_marker_whose_launcher_died_is_orphaned(self, tmp_path):
        write_singleton(
            tmp_path, f"{socket.gethostname()}-{dead_pid()}", profile_lock.LAUNCH_NAME
        )
        assert hold(tmp_path) is None

    def test_a_marker_older_than_any_launch_is_dropped(self, tmp_path):
        """A launcher's pid recycled after it died mid-launch must not hold the
        profile for longer than a launch may last."""
        marker = tmp_path / profile_lock.LAUNCH_NAME
        marker.write_text(f"{socket.gethostname()}-{os.getpid()}", encoding="utf-8")
        assert hold(tmp_path) is not None
        stale = time.time() - spawn_budget.LAUNCH_BUDGET_SECONDS - 5
        os.utime(marker, (stale, stale))
        assert hold(tmp_path) is None

    def test_another_backends_marker_is_left_for_it(self, tmp_path):
        other = f"{socket.gethostname()}-{os.getppid()}"
        with profile_lock.launching(tmp_path):
            (tmp_path / profile_lock.LAUNCH_NAME).write_text(other, encoding="utf-8")
        assert (tmp_path / profile_lock.LAUNCH_NAME).read_text(encoding="utf-8") == (
            other
        )

    def test_a_directory_that_does_not_exist_yet_is_not_created(self, tmp_path):
        missing = tmp_path / "not-yet"
        with profile_lock.launching(missing), profile_lock.launching(None):
            pass
        assert not missing.exists()

    def test_a_marker_that_cannot_be_written_does_not_fail_the_launch(
        self, tmp_path, monkeypatch
    ):
        def refuse(self, *args, **kwargs):
            raise PermissionError(13, "denied")

        monkeypatch.setattr(Path, "write_text", refuse)
        with profile_lock.launching(tmp_path):
            pass
        monkeypatch.undo()
        assert leftovers(tmp_path) == []

    def test_no_profile_copy_carries_it(self):
        names = [profile_lock.LAUNCH_NAME, f"{profile_lock.LAUNCH_NAME}.4242.tmp"]
        assert profile_copy.ignore_names(names) == set(names)


class TestLockReadEdges:
    def test_a_lock_that_exists_but_cannot_be_read_is_a_hold(
        self, tmp_path, monkeypatch
    ):
        """It used to read as NO lock: whatever ``is_symlink`` did with the
        ``PermissionError``, the profile came out free."""
        write_singleton(tmp_path, f"{socket.gethostname()}-{os.getpid()}")
        real_lstat = Path.lstat

        def lstat(self, *args, **kwargs):
            if self.name == profile_lock.LOCK_NAME:
                raise PermissionError(13, "denied", str(self))
            return real_lstat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", lstat)
        held = hold(tmp_path)
        monkeypatch.undo()

        assert held is not None
        assert "could not be read" in held.reason

    @pytest.mark.parametrize(
        ("raised", "held"),
        [
            pytest.param(PermissionError(1, "EPERM"), True, id="hidden, not ours"),
            pytest.param(ProcessLookupError(3, "ESRCH"), False, id="really gone"),
        ],
    )
    def test_a_pid_proc_hides_is_asked_of_the_kernel(
        self, tmp_path, monkeypatch, raised, held
    ):
        """``hidepid=2``: ``psutil`` lists only our own processes, so another
        user's live browser read as gone and its lock as orphaned."""
        hidden = dead_pid()  # no process to hurt, were the alias ever missed
        write_singleton(tmp_path, f"{socket.gethostname()}-{hidden}")
        asked = []

        def kill(pid, signal):
            asked.append((pid, signal))
            raise raised

        monkeypatch.setattr(profile_lock.psutil, "pid_exists", lambda _pid: False)
        # raising=False: before F-1008 there was no such switch, and the pin
        # must reach the old answer (FREE) rather than fail on a missing name.
        monkeypatch.setattr(profile_lock, "_SIGNAL_ASKS", True, raising=False)
        # The module's own alias: patching `os.kill` would swap it process-wide.
        monkeypatch.setattr(profile_lock, "_signal", kill, raising=False)
        answer = hold(tmp_path)
        monkeypatch.undo()

        assert asked == [(hidden, 0)]
        assert (answer is not None) is held

    def test_windows_never_signals_a_pid(self, tmp_path, monkeypatch):
        """``os.kill`` TERMINATES on Windows: the question is never asked there."""
        write_singleton(tmp_path, f"{socket.gethostname()}-{dead_pid()}")
        monkeypatch.setattr(profile_lock.psutil, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(profile_lock, "_SIGNAL_ASKS", False, raising=False)
        monkeypatch.setattr(
            profile_lock,
            "_signal",
            lambda *_a: pytest.fail("signalled a pid"),
            raising=False,
        )
        assert hold(tmp_path) is None

    @pytest.mark.parametrize(
        "pid_text",
        ["99999999999999999999", str(2**31), "٤٢", "4_2"],
    )
    def test_a_pid_chromium_would_not_parse_is_no_pid_and_never_raises(
        self, tmp_path, pid_text
    ):
        """Chromium's ``StringToInt`` fails on each of these, so it calls the
        lock orphaned. A pid too large for an ``int`` reached ``psutil`` and
        raised ``OverflowError`` out of a function that never raises."""
        write_singleton(tmp_path, f"{socket.gethostname()}-{pid_text}")
        assert hold(tmp_path) is None

    def test_the_largest_pid_chromium_parses_is_still_asked_about(
        self, tmp_path, monkeypatch
    ):
        asked = []
        monkeypatch.setattr(
            profile_lock, "_pid_alive", lambda pid: asked.append(pid) or True
        )
        write_singleton(tmp_path, f"{socket.gethostname()}-{2**31 - 1}")
        assert hold(tmp_path) is not None
        assert asked == [2**31 - 1]
