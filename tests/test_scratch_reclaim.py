"""T-5968 -- a spawn's copy leaves no scratch behind on ANY ending, and the
scratch a dead process left is reclaimed by the storage sweep.

``replace_tree`` builds a clone beside its target (``<name>.stealth-staging-<pid>-<n>``)
and publishes it with one rename. Every in-process exit removes its own staging
in a ``finally``, so the four endings a spawn can have are pinned here for a
CLONE spawn (the seed's are ``test_seed_refresh_atomicity``'s): an exception, a
cancellation, a timeout, a held-profile refusal.

What a ``finally`` cannot do is run in a process that is gone, or finish a delete
Windows refused. Those leftovers were reclaimed only by the NEXT copy of the SAME
target name (``reclaim_stale_scratch``'s base-limited form), and a clone's name is unique per attempt
(``<label>-<pid>-<seq>``), so they were never reclaimed at all: every sweep skips
scratch by name. 2026-10-21 measured the same blind spot for whole clones (T-5968).

Hermetic: temp session roots, no Chrome.
"""

import asyncio
import importlib
import os
import threading
import time
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    clone_storage,
    profile_copy,
    profile_lock,
)

STALE = profile_copy.STALE_STAGING_SECONDS


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No real process table, and a clean reservation set per test."""
    # `client_roots.session_seed` imports fastmcp at its first call, which on a
    # cold interpreter takes seconds (see test_clone_spawn_offload).
    importlib.import_module("fastmcp.server.dependencies")
    clone_storage._clear_protected_clone_dirs()
    monkeypatch.setattr(clone_storage, "_profile_hold", lambda directory: None)
    # The other half of every DELETING path's question (``_profile_may_be_held``):
    # a browser-named process whose command line cannot be read (an elevated or
    # another user's Chrome) leaves the holder unruled out and the delete is
    # refused. That is the HOST's answer, and a busy host gives it (a lane run
    # kept two staging copies on 2026-11-19); nothing here may depend on it.
    monkeypatch.setattr(browser_cmdline, "unreadable_browsers", lambda _is_name: ())
    yield
    clone_storage._clear_protected_clone_dirs()


def _scratch(root: Path) -> list[str]:
    return sorted(e.name for e in root.iterdir() if profile_copy.is_scratch(e.name))


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _staging(root: Path, name: str, *, age: float, owned: bool = True) -> Path:
    """What a dead ``replace_tree`` leaves: a half copy and its OWNER proof."""
    directory = root / f"{name}{profile_copy.STAGING_SUFFIX}999-1"
    directory.mkdir()
    (directory / "half.bin").write_bytes(b"x" * 64)
    if owned:
        (directory / profile_copy.STAGING_OWNER).write_text("999")
    _age(directory, age)
    return directory


class TestEveryEndingOfACloneCopyRemovesItsStaging:
    async def test_an_exception_mid_copy(self, tmp_session_root, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(profile_copy, "copy_delta", boom)

        with pytest.raises(RuntimeError, match="disk full"):
            await clone_storage.resolve_profile_selection(None)

        sessions = tmp_session_root["sessions"]
        assert list(sessions.iterdir()) == []
        assert not clone_storage._PROTECTED_CLONE_DIRS

    async def test_a_cancellation_while_the_copy_runs(
        self, tmp_session_root, monkeypatch
    ):
        started, release = threading.Event(), threading.Event()
        real = profile_copy.copy_delta

        def slow(source, target):
            started.set()
            release.wait(10)
            return real(source, target)

        monkeypatch.setattr(profile_copy, "copy_delta", slow)
        task = asyncio.ensure_future(clone_storage.resolve_profile_selection(None))
        while not started.is_set():
            await asyncio.sleep(0.01)

        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert list(tmp_session_root["sessions"].iterdir()) == []
        assert not clone_storage._PROTECTED_CLONE_DIRS

    async def test_a_timeout_around_the_copy(self, tmp_session_root, monkeypatch):
        started, release = threading.Event(), threading.Event()
        real = profile_copy.copy_delta

        def slow(source, target):
            started.set()
            release.wait(10)
            return real(source, target)

        monkeypatch.setattr(profile_copy, "copy_delta", slow)
        waiter = asyncio.ensure_future(
            asyncio.wait_for(clone_storage.resolve_profile_selection(None), 0.3)
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        # The worker is still blocked when the 0.3 s timeout fires: the call is
        # cancelled and waits the thread out (`profile_offload`) until this.
        asyncio.get_running_loop().call_later(0.6, release.set)

        with pytest.raises(TimeoutError):
            await waiter

        assert list(tmp_session_root["sessions"].iterdir()) == []
        assert not clone_storage._PROTECTED_CLONE_DIRS

    def test_a_held_target_is_refused_before_any_staging_exists(
        self, tmp_session_root, monkeypatch
    ):
        sessions = tmp_session_root["sessions"]
        held = sessions / "held-clone"
        held.mkdir()
        monkeypatch.setattr(
            clone_storage,
            "_profile_hold",
            lambda _p: profile_lock.Hold(4242, "a browser has it"),
        )

        refusal = clone_storage._copy_profile_tree(
            tmp_session_root["snapshot"], held, sessions
        )

        assert refusal is not None
        assert refusal.code == clone_storage.TARGET_IN_USE
        assert _scratch(sessions) == []

    def test_a_refused_publish_removes_its_staging_and_restores_the_target(
        self, tmp_session_root, monkeypatch
    ):
        sessions = tmp_session_root["sessions"]
        target = sessions / "existing"
        target.mkdir()
        (target / "keep").write_text("old")
        real = Path.replace

        def refuse_publish(self, dest):
            if profile_copy.STAGING_SUFFIX in self.name:
                raise PermissionError("held")
            return real(self, dest)

        monkeypatch.setattr(Path, "replace", refuse_publish)

        refusal = clone_storage._copy_profile_tree(
            tmp_session_root["snapshot"], target, sessions
        )

        assert refusal is not None
        assert refusal.code == profile_copy.PUBLISH_FAILED
        assert (target / "keep").read_text() == "old"
        assert _scratch(sessions) == []


class TestScratchADeadProcessLeftIsReclaimedBySweep:
    def test_a_staging_copy_with_a_unique_clone_name_is_removed(self, tmp_path):
        stale = _staging(tmp_path, "proj-1a2b3c-4242-7", age=STALE + 60)

        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert not stale.exists(), "nothing ever reclaimed a unique name's staging"

    def test_a_staging_copy_a_sibling_is_still_building_is_left_alone(self, tmp_path):
        live = _staging(tmp_path, "proj-1a2b3c-4242-8", age=30)

        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert live.exists(), "a concurrent spawn's in-flight copy was destroyed"

    def test_the_seeds_scratch_is_never_touched_even_when_ancient(self, tmp_path):
        seed = _staging(tmp_path, "master-snapshot", age=STALE * 9)
        master = _staging(tmp_path, "master", age=STALE * 9)

        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert seed.exists() and master.exists()

    def test_a_displaced_previous_generation_is_never_reclaimed(self, tmp_path):
        """It is the one copy of what a refresh replaced; unattended code leaves it."""
        previous = tmp_path / f"proj-1a2b3c{profile_copy.PREVIOUS_SUFFIX}"
        previous.mkdir()
        (previous / "f").write_text("the only copy")
        _age(previous, STALE * 9)

        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert (previous / "f").read_text() == "the only copy"

    def test_staging_a_failed_delete_left_is_reclaimed_once_it_is_stale(
        self, tmp_session_root, monkeypatch
    ):
        """Windows refused the ``finally``'s delete (a scanner holding a new file):
        the staging stays, and used to stay forever."""
        sessions = tmp_session_root["sessions"]
        target = sessions / "proj-1a2b3c-4242-10"
        monkeypatch.setattr(profile_copy, "copy_delta", lambda *_a, **_k: 1 / 0)
        with monkeypatch.context() as stuck:
            stuck.setattr(profile_copy, "rmtree_robust", lambda *_a, **_k: None)
            with pytest.raises(ZeroDivisionError):
                profile_copy.replace_tree(
                    tmp_session_root["snapshot"], target, stamp=lambda _p: None
                )
        (leftover,) = sessions.iterdir()
        assert profile_copy.is_scratch(leftover.name)
        _age(leftover, STALE + 60)

        clone_storage.run_storage_sweep(sessions, 0, 0)

        assert not leftover.exists()

    def test_the_next_copy_of_the_same_target_still_reclaims_its_own(self, tmp_path):
        """The behaviour this generalises, unchanged."""
        target = tmp_path / "proj"
        stale = _staging(tmp_path, "proj", age=STALE + 60)
        other = _staging(tmp_path, "other", age=STALE + 60)

        assert profile_copy.reclaim_stale_scratch(tmp_path, "proj") == 1

        assert not stale.exists()
        assert other.exists(), "a copy of one target reclaimed another's scratch"
        assert not target.exists()

    def test_a_link_named_like_staging_is_not_followed_or_removed(self, tmp_path):
        """Not a copy this module made, whatever its name and age."""
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep").write_text("x")
        link = tmp_path / f"proj{profile_copy.STAGING_SUFFIX}999-1"
        try:
            link.symlink_to(victim, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                pytest.skip("this account cannot create symlinks")
            import _winapi

            _winapi.CreateJunction(str(victim), str(link))
        _age(link, STALE + 60)
        _age(victim, STALE + 60)

        assert profile_copy.stale_scratch(tmp_path) == []
        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert os.path.lexists(link)
        assert (victim / "keep").read_text() == "x"


class TestOnlyProvenScratchIsReclaimed:
    """T-5968 review B2: the root-wide reclaim took ANY directory whose name
    contained ``.stealth-staging-`` once its mtime was over an hour old, so a
    NAMED session called ``acme.stealth-staging-eu`` (the gate let it through)
    was deleted by one sweep. A name is not proof; the owner file is."""

    def test_a_directory_with_a_staging_name_and_no_owner_proof_survives(
        self, tmp_path
    ):
        lookalike = _staging(tmp_path, "acme", age=STALE * 9, owned=False)

        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert (lookalike / "half.bin").exists()

    def test_a_named_session_that_got_the_staging_name_is_never_swept(self, tmp_path):
        """Built with the real marker writer, as an older build would have made it."""
        from stealth_chrome_devtools_mcp.embedded import profile_seed

        session = tmp_path / f"acme{profile_copy.STAGING_SUFFIX}eu"
        session.mkdir()
        (session / "Cookies").write_bytes(b"login")
        profile_seed.write_marker(
            session, source=tmp_path, source_kind="explicit-session", seeded_from="x"
        )
        _age(session, STALE * 9)

        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert (session / "Cookies").read_bytes() == b"login"

    def test_even_with_the_owner_file_a_marked_directory_goes_through_the_guard(
        self, tmp_path
    ):
        """The delete is ``clone_trash.discard``, whose protection reads the
        marker: a NAMED session carrying a stray owner file is still refused."""
        from stealth_chrome_devtools_mcp.embedded import profile_seed

        session = _staging(tmp_path, "acme", age=STALE * 9)
        profile_seed.write_marker(
            session, source=tmp_path, source_kind="explicit-session", seeded_from="x"
        )
        _age(session, STALE * 9)  # writing the marker made the directory fresh
        assert profile_copy.stale_scratch(tmp_path) == [session], (
            "selected, so the guard is what saves it"
        )

        clone_storage.run_storage_sweep(tmp_path, 0, 0)

        assert (session / "half.bin").exists()

    def test_a_finished_copy_does_not_keep_its_owner_file(self, tmp_session_root):
        sessions = tmp_session_root["sessions"]
        target = sessions / "proj-1a2b3c-4242-11"

        profile_copy.replace_tree(
            tmp_session_root["snapshot"], target, stamp=lambda _p: None
        )

        assert target.is_dir()
        assert not (target / profile_copy.STAGING_OWNER).exists()

    def test_the_owner_file_is_never_copied_into_another_profile(self):
        """It rides the ``.lock`` suffix every profile copy already skips."""
        names = [profile_copy.STAGING_OWNER]
        assert profile_copy.ignore_names(names) == set(names)

    @pytest.mark.parametrize(
        "name",
        [
            "acme.stealth-staging-eu",
            "acme.stealth-staging-",
            "acme.stealth-previous",
            ".trash",
            ".TRASH",
            ".trash.",
        ],
    )
    def test_the_name_gate_refuses_every_name_the_sweep_reads_as_scratch(
        self, tmp_session_root, name
    ):
        from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

        with pytest.raises(ToolError, match="storage sweep owns"):
            clone_storage.require_allowed_user_data_dir(None, name)
