"""F-871: a Chrome profile is held by a LIVE process, not by a leftover file.

Chrome's process singleton on POSIX is a symlink ``SingletonLock`` whose target
is the string ``<hostname>-<pid>`` (Chromium ``chrome/browser/
process_singleton_posix.cc::ProcessSingleton::Create``), plus ``SingletonSocket``
(a symlink into a per-launch ``/tmp`` directory) and ``SingletonCookie``.  Chrome
itself treats a lock whose pid is gone as ORPHANED: it unlinks it and starts
(``NotifyOtherProcessWithTimeout`` -> ``ORPHANED_LOCK_FILE`` -> ``PROCESS_NONE``).

The old busy check asked ``Path.exists()`` of the three names, which
* cannot see ``SingletonLock`` at all (a dangling symlink is ``exists() ==
  False`` -- pinned below, because the whole finding rests on it), and
* answers True for a ``SingletonSocket`` whose ``/tmp`` target outlived the
  browser -- exactly what a killed Chrome leaves behind.

So a named profile whose browser had been reaped (F-860) read as busy and the
caller was silently walked to ``<name>-2``: an identity change for a profile
that exists precisely to keep its cookies and logins.

Hermetic: no Chrome is spawned and no live process is inspected beyond this
test's own pid.  Locks are written as real symlinks where the platform allows
one and as a plain file otherwise (Windows symlink creation is privileged), and
production reads both -- the bytes are the same either way.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import psutil
import pytest

from fakes import held_profile, write_singleton
from stealth_chrome_devtools_mcp.embedded import profile_lock
from stealth_chrome_devtools_mcp.embedded.clone_storage import (
    resolve_profile_selection,
)


def dead_pid() -> int:
    for pid in range(999_999, 900_000, -1):
        if not psutil.pid_exists(pid):
            return pid
    raise RuntimeError("no free pid in the probe range")


def no_pids(_user_data_dir):
    return set()


# ---------------------------------------------------------------------------
# The premise the finding rests on
# ---------------------------------------------------------------------------


def test_a_real_singleton_lock_is_invisible_to_path_exists(tmp_path):
    """Chrome's lock points at ``<hostname>-<pid>``, which is not a path: a
    busy check written as ``(dir / "SingletonLock").exists()`` never sees it."""
    lock = held_profile(tmp_path)
    if not lock.is_symlink():
        pytest.skip("platform refused a symlink; the claim is about symlinks")
    assert lock.exists() is False
    assert os.path.lexists(lock) is True


# ---------------------------------------------------------------------------
# profile_lock.profile_hold
# ---------------------------------------------------------------------------


class TestProfileHold:
    def test_untouched_profile_is_not_held(self, tmp_path):
        assert profile_lock.profile_hold(tmp_path, no_pids) is None

    def test_live_browser_process_is_a_hold(self, tmp_path):
        hold = profile_lock.profile_hold(tmp_path, lambda _d: {4242})
        assert hold is not None
        assert hold.pid == 4242
        assert "4242" in hold.reason

    def test_lock_naming_a_dead_pid_is_not_a_hold(self, tmp_path):
        write_singleton(tmp_path, f"{socket.gethostname()}-{dead_pid()}")
        assert profile_lock.profile_hold(tmp_path, no_pids) is None

    def test_lock_naming_a_live_pid_is_a_hold(self, tmp_path):
        held_profile(tmp_path)
        hold = profile_lock.profile_hold(tmp_path, no_pids)
        assert hold is not None
        assert hold.pid == os.getpid()
        assert str(os.getpid()) in hold.reason

    def test_lock_from_another_host_is_a_hold(self, tmp_path):
        write_singleton(tmp_path, f"not-this-machine-{os.getpid()}")
        hold = profile_lock.profile_hold(tmp_path, no_pids)
        assert hold is not None
        assert "not-this-machine" in hold.reason

    def test_unparseable_lock_is_not_a_hold(self, tmp_path):
        """Chromium calls a lock with no ``<host>-<pid>`` an invalid lockfile,
        unlinks it and starts; nothing is proven held by it."""
        write_singleton(tmp_path, "lock")
        assert profile_lock.profile_hold(tmp_path, no_pids) is None

    def test_residual_socket_without_a_lock_is_not_a_hold(self, tmp_path):
        """The CI mechanism: a killed Chrome leaves ``SingletonSocket`` pointing
        at a ``/tmp`` target that still exists, but no lock owns the profile."""
        target = tmp_path / "socket-target"
        target.write_text("", encoding="utf-8")
        write_singleton(tmp_path, str(target), name="SingletonSocket")
        write_singleton(tmp_path, "0123456789", name="SingletonCookie")
        assert profile_lock.profile_hold(tmp_path, no_pids) is None

    def test_a_pid_scan_that_fails_does_not_raise(self, tmp_path):
        def _raise(_user_data_dir):
            raise OSError("simulated PID-lookup failure")

        assert profile_lock.profile_hold(tmp_path, _raise) is None

    def test_a_missing_pid_scan_does_not_raise(self, tmp_path):
        assert profile_lock.profile_hold(tmp_path, None) is None


# ---------------------------------------------------------------------------
# The named profile the caller asked for
# ---------------------------------------------------------------------------


class TestNamedProfileSelection:
    @pytest.mark.asyncio
    async def test_stale_lock_reuses_the_named_profile(self, tmp_session_root):
        """The defect: after F-860 reaped the browser, the next spawn on the
        same name must land on the SAME profile, not on ``-2``."""
        occupied = tmp_session_root["sessions"] / "occupied"
        occupied.mkdir()
        write_singleton(occupied, f"{socket.gethostname()}-{dead_pid()}")

        result = await resolve_profile_selection("occupied")

        assert Path(result["user_data_dir"]).name == "occupied"
        assert "walk_reason" not in result

    @pytest.mark.asyncio
    async def test_residual_socket_reuses_the_named_profile(self, tmp_session_root):
        """The shape CI hit: the reaped Chrome's ``SingletonSocket`` still
        resolves, so the old check called the profile busy and walked."""
        occupied = tmp_session_root["sessions"] / "occupied"
        occupied.mkdir()
        target = tmp_session_root["root"] / "socket-target"
        target.write_text("", encoding="utf-8")
        write_singleton(occupied, str(target), name="SingletonSocket")

        result = await resolve_profile_selection("occupied")

        assert Path(result["user_data_dir"]).name == "occupied"
        assert "walk_reason" not in result

    @pytest.mark.asyncio
    async def test_a_live_lock_walks_and_the_answer_says_why(self, tmp_session_root):
        """A walk is an identity change; the answer has to name it."""
        occupied = tmp_session_root["sessions"] / "occupied"
        occupied.mkdir()
        held_profile(occupied)

        result = await resolve_profile_selection("occupied")

        assert Path(result["user_data_dir"]).name == "occupied-2"
        assert result["requested_user_data_dir"] == str(occupied)
        assert result["walked_to"] == result["user_data_dir"]
        assert str(os.getpid()) in result["walk_reason"]
