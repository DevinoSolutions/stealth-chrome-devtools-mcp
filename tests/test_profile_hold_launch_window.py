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

import os
import socket
import time
from pathlib import Path

import psutil
import pytest

from fakes import write_singleton
from stealth_chrome_devtools_mcp.embedded import (
    browser_manager,
    profile_copy,
    profile_lock,
    spawn_budget,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions


def no_pids(_user_data_dir):
    """The process scan during the window: the browser is not visible yet."""
    return set()


def hold(profile: Path):
    return profile_lock.profile_hold(profile, no_pids)


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

    async def test_a_launch_that_fails_leaves_nothing_holding_it(
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
        assert hold(tmp_path) is None
        assert leftovers(tmp_path) == []


class _Attempt:
    config = None


class TestTheLaunchMarker:
    def test_two_launches_in_one_backend_hold_until_the_last_ends(self, tmp_path):
        with profile_lock.launching(tmp_path):
            with profile_lock.launching(str(tmp_path)):
                assert hold(tmp_path) is not None
            assert hold(tmp_path) is not None, "the first launch to end freed it"
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
        hidden = 4242
        write_singleton(tmp_path, f"{socket.gethostname()}-{hidden}")
        asked = []

        def kill(pid, signal):
            asked.append((pid, signal))
            raise raised

        monkeypatch.setattr(profile_lock.psutil, "pid_exists", lambda _pid: False)
        # raising=False: before F-1008 there was no such switch, and the pin
        # must reach the old answer (FREE) rather than fail on a missing name.
        monkeypatch.setattr(profile_lock, "_SIGNAL_ASKS", True, raising=False)
        monkeypatch.setattr(profile_lock.os, "kill", kill)
        answer = hold(tmp_path)
        monkeypatch.undo()

        assert asked == [(hidden, 0)]
        assert (answer is not None) is held

    def test_windows_never_signals_a_pid(self, tmp_path, monkeypatch):
        """``os.kill`` TERMINATES on Windows: the question is never asked there."""
        write_singleton(tmp_path, f"{socket.gethostname()}-4242")
        monkeypatch.setattr(profile_lock.psutil, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(profile_lock, "_SIGNAL_ASKS", False, raising=False)
        monkeypatch.setattr(
            profile_lock.os, "kill", lambda *_a: pytest.fail("signalled a pid")
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
