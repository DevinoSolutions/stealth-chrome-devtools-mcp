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
from types import SimpleNamespace

import psutil
import pytest

from fakes import FakeBrowserManager, held_profile, write_singleton
from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    clone_storage,
    profile_lock,
    profile_seed,
)
from stealth_chrome_devtools_mcp.embedded.clone_storage import (
    resolve_profile_selection,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


def _driving(directory: Path):
    """The F-898 witness, answering True for exactly one directory — through
    ``profile_seed.same_dir``, the tree's one path comparison, because the
    resolver asks about an anchored absolute path."""
    return lambda profile: profile_seed.same_dir(profile, directory)


def dead_pid() -> int:
    for pid in range(999_999, 900_000, -1):
        if not psutil.pid_exists(pid):
            return pid
    raise RuntimeError("no free pid in the probe range")


def no_pids(_user_data_dir):
    return set()


#: The profile every F-931 tree fixture below is about, spelled once.
TREE_DIR = r"C:\sessions\held"


def browser_argv(directory: str = TREE_DIR) -> list[str]:
    """A Chrome BROWSER process's argv: a ``--user-data-dir`` and no ``--type``."""
    return [
        "chrome.exe",
        f"--user-data-dir={directory}",
        "--remote-debugging-port=9222",
    ]


def child_argv(kind: str = "renderer", directory: str = TREE_DIR) -> list[str]:
    """A CHILD of that browser — same profile, same port for a renderer
    (measured on Chrome 153), and the one structural difference: ``--type``."""
    return [*browser_argv(directory), f"--type={kind}"]


def fake_process_table(monkeypatch, table: dict[int, list[str] | None]) -> None:
    """Make ``browser_cmdline`` read *table* instead of the real process table.

    ``None`` is a pid that IS there and whose argv could not be read (a Windows
    ``AccessDenied`` on an elevated Chrome); a pid absent from the table has
    EXITED. The two are different answers and this fixture keeps them apart,
    because the whole of F-931's direction rule turns on it.
    """
    monkeypatch.setattr(
        browser_cmdline, "arguments", lambda pid: list(table.get(pid) or [])
    )
    monkeypatch.setattr(browser_cmdline.psutil, "pid_exists", lambda pid: pid in table)


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

    def test_live_browser_process_is_a_hold(self, tmp_path, monkeypatch):
        """SOFT GOLDEN UPDATED for F-931. The shipped node handed the scan a
        bare pid nobody had established anything about and asserted a hold; the
        scan's answer is a process TREE and only its browser member holds the
        profile, so the fixture now says which member 4242 is. Written with a
        bare pid it was also latently flaky — ``4242`` may be a real live
        process on a busy machine, and the answer then depended on whose.
        """
        fake_process_table(monkeypatch, {4242: browser_argv(str(tmp_path))})

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

    def test_a_pid_scan_that_fails_resolves_toward_held_where_it_is_the_only_witness(
        self, tmp_path
    ):
        """Never raises, and the direction is uniform with ``_pid_alive``: an
        unanswerable question resolves toward HELD. On POSIX the lock is a
        second witness and answers "free"; on Windows there is none, so the
        profile is not SHOWN free."""

        def _raise(_user_data_dir):
            raise OSError("simulated PID-lookup failure")

        hold = profile_lock.profile_hold(tmp_path, _raise)
        if profile_lock._LOCK_IS_A_WITNESS:
            assert hold is None
        else:
            assert hold is not None
            assert hold.pid is None
            assert "could not be read" in hold.reason

    def test_a_scan_that_rejects_both_str_and_path_counts_as_unasked(self, tmp_path):
        """A TypeError is the str/Path probe, not an answer — a scan that takes
        neither was never really asked, and must not read as "no browsers"."""

        def _wrong_arity(_a, _b):
            raise AssertionError("must never be reached")

        assert profile_lock._browser_pids(tmp_path, _wrong_arity) is None

    def test_a_scan_that_answers_nothing_is_not_the_same_as_unasked(self, tmp_path):
        assert profile_lock._browser_pids(tmp_path, no_pids) == ()

    def test_a_missing_pid_scan_does_not_raise(self, tmp_path):
        """An absent collaborator is a replaced seam, not a runtime failure."""
        assert profile_lock.profile_hold(tmp_path, None) is None


# ---------------------------------------------------------------------------
# F-931 — a profile is held by its BROWSER, not by whatever of its tree is left
# ---------------------------------------------------------------------------


class TestWhichMemberOfTheTreeHolds:
    """The process scan answers about a TREE, and only one member of it is the
    browser.

    ``process_cleanup._get_browser_pids_for_profile`` matches every
    Chromium-family process carrying ``--user-data-dir=<dir>`` and applies no
    ``--type`` filter, and ``profile_hold`` reported ``min(pids)`` — so the pid
    it named was whichever member sorted first, which for a real Chrome is a
    renderer or a utility five times out of six. ``browser_reattach.held_by``
    has asked ``browser_cmdline.browser_process`` since F-888 for exactly that
    reason; this is the same rule reached from the other side, and until F-931
    the two witnesses disagreed.

    The consequence is not cosmetic. ``close_instance`` waits for the BROWSER's
    own exit (``process_exit.browser_pid`` answers ``None`` for any ``--type=``
    child, deliberately) and kills only the browser, so for a window after a
    close a surviving child made the profile read HELD — and since F-914 a held
    profile we do not drive is REFUSED. Measured on the release gate
    (``integration (Windows/X64)``, run 35689647688): an unnamed spawn refused
    with "the 'default' session is open in a browser this backend does not
    drive (pid 8084)" immediately after the previous test closed its own
    browser on that profile.
    """

    def test_a_tree_with_no_browser_member_left_does_not_hold(
        self, tmp_path, monkeypatch
    ):
        """Every survivor carries a ``--type=``, so the browser has gone and
        what is left holds no profile. This is the gate's own shape."""
        fake_process_table(
            monkeypatch,
            {
                8084: child_argv("renderer", str(tmp_path)),
                8085: child_argv("gpu-process", str(tmp_path)),
                8086: child_argv("crashpad-handler", str(tmp_path)),
            },
        )

        assert (
            profile_lock.profile_hold(tmp_path, lambda _d: {8084, 8085, 8086}) is None
        )

    def test_the_hold_names_the_browser_and_not_the_lowest_pid(
        self, tmp_path, monkeypatch
    ):
        """``min(pids)`` is set ordering wearing an answer's clothes: the
        refusal a caller reads, and ``walk_reason``, named a renderer."""
        fake_process_table(
            monkeypatch,
            {
                1001: child_argv("renderer", str(tmp_path)),
                1002: child_argv("utility", str(tmp_path)),
                2002: browser_argv(str(tmp_path)),
            },
        )

        hold = profile_lock.profile_hold(tmp_path, lambda _d: {1001, 1002, 2002})

        assert hold is not None
        assert hold.pid == 2002
        assert "2002" in hold.reason

    def test_a_member_we_could_not_read_still_reads_as_held(
        self, tmp_path, monkeypatch
    ):
        """The direction, uniform with ``_pid_alive`` and ``_browser_pids``: a
        witness we could not READ is not an established negative. The reason
        says so rather than claiming a browser we never saw — that sentence is
        quoted verbatim to the caller by F-914's refusal.
        """
        fake_process_table(monkeypatch, {7007: None})

        hold = profile_lock.profile_hold(tmp_path, lambda _d: {7007})

        assert hold is not None
        assert hold.pid == 7007
        assert "could not be read" in hold.reason

    def test_a_pid_that_has_since_exited_is_not_a_hold(self, tmp_path, monkeypatch):
        """The scan and this read are two moments. A pid gone between them is
        an ESTABLISHED negative and must not be confused with one we could not
        read — the distinction ``reap_guard.UNDECIDED`` exists for, here."""
        fake_process_table(monkeypatch, {})

        assert profile_lock.profile_hold(tmp_path, lambda _d: {9009}) is None

    def test_a_browser_on_another_profile_does_not_hold_this_one(
        self, tmp_path, monkeypatch
    ):
        """A scan replaced by a looser one must not make a stranger's Chrome
        this directory's holder; the argv is re-read against the directory for
        the same reason ``browser_cmdline.debug_port`` does it (F-888 M4)."""
        fake_process_table(monkeypatch, {3003: browser_argv(r"C:\somewhere\else")})

        assert profile_lock.profile_hold(tmp_path, lambda _d: {3003}) is None

    def test_two_browsers_on_one_directory_still_hold_it(self, tmp_path, monkeypatch):
        """``browser_cmdline.browser_process`` answers None for an AMBIGUOUS
        tree because adopting one of two is a coin flip. "Which one may we
        attach to" and "is anything there" are different questions, and reusing
        that None here would call a directory with two live browsers free."""
        fake_process_table(
            monkeypatch,
            {
                5005: browser_argv(str(tmp_path)),
                6006: browser_argv(str(tmp_path)),
            },
        )

        hold = profile_lock.profile_hold(tmp_path, lambda _d: {5005, 6006})

        assert hold is not None
        assert hold.pid in (5005, 6006)


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
        """A walk is an identity change; the answer has to name it.

        SOFT GOLDEN UPDATED for F-915 — the three fields are unchanged and the
        ``driven`` witness is new. F-871 made this walk REPORTED and left it
        unconditional, and what it reported was a directory copied from the
        SHARED seed: a different identity with none of ``occupied``'s logins,
        seventeen of them on the owner's machine. Since F-915 a walk happens
        only where the holder is a browser THIS backend drives, so the copy
        comes from the holder and its jar is handed over — which is also why
        ``occupied-2`` is still the right answer here rather than a refusal.
        """
        occupied = tmp_session_root["sessions"] / "occupied"
        occupied.mkdir()
        held_profile(occupied)

        result = await resolve_profile_selection("occupied", driven=_driving(occupied))

        assert Path(result["user_data_dir"]).name == "occupied-2"
        assert result["requested_user_data_dir"] == str(occupied)
        assert result["walked_to"] == result["user_data_dir"]
        assert str(os.getpid()) in result["walk_reason"]
        assert result[clone_storage.LIVE_SEED_KEY] == str(occupied)

    @pytest.mark.asyncio
    async def test_a_live_lock_we_do_not_drive_walks_nowhere(self, tmp_session_root):
        """F-915's other half: the same live lock, no browser of ours on it.

        There is no jar to hand over, so the walk would produce exactly what
        F-871 measured and reported — and the owner's ruling is that reporting
        a substitution is not the same as being given the session you asked for.
        """
        occupied = tmp_session_root["sessions"] / "occupied"
        occupied.mkdir()
        held_profile(occupied)

        with pytest.raises(ToolError, match="occupied"):
            await resolve_profile_selection("occupied")

        assert not (tmp_session_root["sessions"] / "occupied-2").exists()


# ---------------------------------------------------------------------------
# What spawn_browser actually tells the caller
# ---------------------------------------------------------------------------


class TestSpawnBrowserAnnouncesTheSubstitution:
    """`walk_reason` on its own is a quiet field beside a loud one: the named
    profile `warning` is what a model reads, so a substitution has to lead it."""

    async def test_the_walk_reason_leads_the_warning(
        self, call_tool, patched_server, monkeypatch
    ):
        fbm = FakeBrowserManager(
            spawn_instance=SimpleNamespace(
                instance_id="i1",
                state="active",
                headless=True,
                viewport={"width": 800, "height": 600},
            ),
            spawn_diagnostics={},
        )

        async def fake_resolve(user_data_dir, **kwargs):
            return {
                "user_data_dir": "/sessions/github-2",
                "profile_role": "explicit",
                "clone_source": None,
                "requested_user_data_dir": "/sessions/github",
                "walked_to": "/sessions/github-2",
                "walk_reason": "Chrome's SingletonLock is held by live pid 4242",
            }

        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        srv = patched_server(browser_manager=fbm)

        result = await call_tool(
            srv, "spawn_browser", headless=True, user_data_dir="github", sandbox=False
        )

        warning = result["spawn_diagnostics"]["profile_selection"]["warning"]
        assert warning.startswith("NOT the directory you asked for")
        assert "Chrome's SingletonLock is held by live pid 4242" in warning
        assert "/sessions/github-2" in warning
        # SOFT GOLDEN UPDATED for F-915. The shipped sentence said the walked
        # directory held "none of the cookies or logins the requested one
        # holds", which was true of F-871's unconditional walk and is false of
        # the only walk that survives: one taken because we drive the holder,
        # copied FROM that holder, with its jar handed over. Pinning the claim
        # that is still true -- a separate directory from here on, and what a
        # cookie jar does not carry -- rather than a sentence about a copy of
        # the seed that no longer happens.
        assert "with its cookies handed over" in warning
        assert "outside its cookie jar did not come with them" in warning
        assert "none of the cookies" not in warning
        assert "freshly cloned" not in warning
        # The standing named-profile advice is kept, not replaced.
        assert "NOT auto-cleaned" in warning

    async def test_no_walk_leaves_the_warning_alone(
        self, call_tool, patched_server, monkeypatch
    ):
        fbm = FakeBrowserManager(
            spawn_instance=SimpleNamespace(
                instance_id="i1",
                state="active",
                headless=True,
                viewport={"width": 800, "height": 600},
            ),
            spawn_diagnostics={},
        )

        async def fake_resolve(user_data_dir, **kwargs):
            return {
                "user_data_dir": "/sessions/github",
                "profile_role": "explicit",
                "clone_source": None,
            }

        monkeypatch.setattr(clone_storage, "resolve_profile_selection", fake_resolve)
        srv = patched_server(browser_manager=fbm)

        result = await call_tool(
            srv, "spawn_browser", headless=True, user_data_dir="github", sandbox=False
        )

        warning = result["spawn_diagnostics"]["profile_selection"]["warning"]
        assert warning.startswith("Named session created")
