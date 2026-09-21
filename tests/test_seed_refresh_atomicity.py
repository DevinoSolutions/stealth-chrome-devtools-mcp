"""F-925 — refreshing the seed must never be able to destroy it.

The seed (``<session root>/master-snapshot``) is the directory EVERY new
session is copied from. ``clone_storage._copy_profile_tree`` used to refresh it
by ``rmtree``-ing it and rebuilding in place::

    if target.exists():
        ...
        profile_copy.rmtree_robust(target)   # the seed is DELETED
    target.mkdir(parents=True, exist_ok=True)
    profile_copy.copy_delta(source, target)  # ~101 MB, seconds
    time.sleep(0.2)
    profile_copy.copy_delta(source, target)
    profile_seed.write_marker(...)           # marker written LAST

Destroy-then-rebuild, non-atomically. For the whole of that copy the seed is
absent or half-written, and a process death anywhere inside it — a crash,
``stealthy stop``, an F-886 eviction, a reboot — leaves it that way. The window
is entered on every open AND every close of the ``default`` session, so it is
entered constantly.

What turns a window into permanent loss is that it does not self-heal: the
repair refresh is refused while the shared browser is open
(``_refresh_master_snapshot_if_safe`` -> ``default-in-use``), and every consumer
tests ``snapshot.exists()``, which an EMPTY DIRECTORY passes. So every session
created afterwards is copied from the gutted seed and comes up logged out.

The pins here are about the one invariant that closes it: **at no instant during
a refresh is the seed absent, empty or half-written.** The interruption seam is
``profile_copy.copy_delta`` — real production code, patched at the module
attribute, which is the same object whether it is called from ``clone_storage``
(as it was) or from inside ``profile_copy.replace_tree`` (as it is now). It is
therefore not a mock of the fix: the probe records what a process that vanished
mid-copy would have left on disk, and it is the pre-fix code that fails it.

Pure filesystem tests: no browser, no Chrome, no sockets.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fakes import held_profile
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    profile_copy,
    profile_seed,
)

MARKER = profile_seed.MARKER_NAME

#: Chrome >= 96's cookie jar — the file that IS the login, per
#: ``profile_seed.LOGIN_WITNESSES``. The product's own spelling of it is pinned
#: to a single home by ``test_profile_seed_truth``; this is a fixture path.
COOKIES = Path("Default") / "Network" / "Cookies"

SEED_LOGIN = b"seed-login-jar-the-old-generation"
MASTER_LOGIN = b"master-login-jar-the-new-generation"


def _profile(root: Path, jar: bytes) -> Path:
    """A directory shaped like a Chrome profile, with a recognisable jar."""
    (root / COOKIES).parent.mkdir(parents=True, exist_ok=True)
    (root / COOKIES).write_bytes(jar)
    (root / "Local State").write_bytes(b"{}")
    (root / "Default" / "Preferences").write_bytes(b"{}")
    return root


@pytest.fixture()
def seed_layout(tmp_path, monkeypatch):
    """A session root whose master and seed hold DIFFERENT cookie jars.

    Different on purpose: built from one template, "the seed still has a jar"
    would be indistinguishable from "the seed was rebuilt from the master", and
    every assertion below would pass for the wrong reason.
    """
    master = _profile(tmp_path / "master", MASTER_LOGIN)
    snapshot = _profile(tmp_path / "master-snapshot", SEED_LOGIN)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    profile_seed.write_marker(
        snapshot, source=master, source_kind="test-fixture", seeded_from="default"
    )
    for key, value in {
        "STEALTH_MCP_BROWSER_SESSION_ROOT": str(tmp_path),
        "BROWSER_MASTER_USER_DATA_DIR": str(master),
        "BROWSER_MASTER_SNAPSHOT_DIR": str(snapshot),
        "BROWSER_PROFILE_CLONE_ROOT": str(sessions),
    }.items():
        monkeypatch.setenv(key, value)
    return {
        "root": tmp_path,
        "master": master,
        "snapshot": snapshot,
        "sessions": sessions,
    }


def _login(profile: Path) -> bytes | None:
    """The cookie jar in *profile*, or None when there is not one — which is
    exactly what a session copied from a gutted seed would find."""
    try:
        return (profile / COOKIES).read_bytes()
    except OSError:
        return None


def _dies_mid_copy(*_args, **_kwargs):
    raise RuntimeError("the process died mid-copy")


def _scratch_in(root: Path) -> list[str]:
    return sorted(
        entry.name for entry in root.iterdir() if profile_copy.is_scratch(entry.name)
    )


class TestADeathMidRefreshLeavesTheSeedUsable:
    """The finding itself: what is on disk if the process vanishes mid-copy."""

    def test_the_seed_still_holds_its_logins_at_the_instant_of_death(
        self, seed_layout, monkeypatch
    ):
        """Interrupt the FIRST copy pass — the deepest point of the old damage,
        where the ``rmtree`` has run and not one byte has been copied back."""
        seen: dict[str, object] = {}

        def dying_copy_delta(source, target):
            # What a process that died right here would leave behind.
            seen["exists"] = seed_layout["snapshot"].exists()
            seen["login"] = _login(seed_layout["snapshot"])
            raise RuntimeError("the process died mid-copy")

        monkeypatch.setattr(profile_copy, "copy_delta", dying_copy_delta)

        result = clone_storage._refresh_master_snapshot_if_safe("test")

        assert result["seed_refreshed"] is False, "an interrupted copy is no refresh"
        assert seen["exists"] is True, "the seed vanished mid-refresh"
        assert seen["login"] == SEED_LOGIN, (
            "the seed was already gutted when the process died: every session "
            "created after this point is copied from an empty directory"
        )

    def test_the_seed_is_intact_after_the_failed_refresh(
        self, seed_layout, monkeypatch
    ):
        """The state that PERSISTS — the half that does not self-heal, because
        the repair refresh is refused while the shared browser is open and
        ``snapshot.exists()`` is True for an empty directory."""
        monkeypatch.setattr(profile_copy, "copy_delta", _dies_mid_copy)

        clone_storage._refresh_master_snapshot_if_safe("test")

        assert _login(seed_layout["snapshot"]) == SEED_LOGIN
        assert (seed_layout["snapshot"] / MARKER).exists(), "the seed lost provenance"

    def test_a_death_between_the_copy_and_the_marker_publishes_nothing(
        self, seed_layout, monkeypatch
    ):
        """The marker is written INTO the staged copy, so nothing is published
        until the tree is complete AND stamped. Stamping after the swap would
        put a complete profile with no marker on disk if the process died
        between the two — and an unmarked seed reads as provenance ``unknown``
        forever, which is this finding's own shape one step smaller."""
        monkeypatch.setattr(profile_seed, "write_marker", _dies_mid_copy)

        result = clone_storage._refresh_master_snapshot_if_safe("test")

        assert result["seed_refreshed"] is False
        assert _login(seed_layout["snapshot"]) == SEED_LOGIN, "an unstamped copy landed"
        assert (seed_layout["snapshot"] / MARKER).exists()

    def test_a_later_session_still_gets_the_logins(self, seed_layout, monkeypatch):
        """The harm as the owner meets it: not "the seed is empty" but "the
        session I just spawned is logged out"."""
        monkeypatch.setattr(profile_copy, "copy_delta", _dies_mid_copy)
        clone_storage._refresh_master_snapshot_if_safe("test")
        monkeypatch.undo()

        session = seed_layout["sessions"] / "work"
        refused = clone_storage._copy_profile_tree(
            seed_layout["snapshot"], session, seed_layout["sessions"], "test"
        )

        assert refused is None
        assert _login(session) == SEED_LOGIN, (
            "a session created after the interrupted refresh came up logged out"
        )


#: Run a seed refresh in a CHILD interpreter and kill it, un-unwound, at the
#: first copy pass. ``os._exit`` runs no ``finally``, no ``atexit`` and no
#: handler — which is the difference between this and the in-process pins
#: above, where the interruption is an exception and cleanup still happens.
_SUICIDE_CHILD = """
import os, sys
from stealth_chrome_devtools_mcp.embedded import clone_storage, profile_copy

def die(source, target):
    open(os.environ["F925_TOMBSTONE"], "wb").write(b"reached")
    os._exit(9)

profile_copy.copy_delta = die
clone_storage._refresh_master_snapshot_if_safe("suicide")
sys.exit("the child was supposed to die inside the copy")
"""


class TestARealProcessDeath:
    """The pins above interrupt with an exception, so every ``finally`` still
    runs. This one kills the interpreter outright at the same point, which is
    what a crash, ``stealthy stop``, an eviction or a power cut actually do."""

    def test_the_seed_survives_a_killed_interpreter(self, seed_layout):
        tombstone = seed_layout["root"] / "tombstone"
        env = {
            **os.environ,
            "STEALTH_MCP_BROWSER_SESSION_ROOT": str(seed_layout["root"]),
            "BROWSER_MASTER_USER_DATA_DIR": str(seed_layout["master"]),
            "BROWSER_MASTER_SNAPSHOT_DIR": str(seed_layout["snapshot"]),
            "BROWSER_PROFILE_CLONE_ROOT": str(seed_layout["sessions"]),
            "F925_TOMBSTONE": str(tombstone),
        }
        done = subprocess.run(
            [sys.executable, "-c", _SUICIDE_CHILD],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert tombstone.exists(), f"the child never reached the copy: {done.stderr}"
        assert done.returncode == 9, f"the child did not die un-unwound: {done!r}"
        assert _login(seed_layout["snapshot"]) == SEED_LOGIN, (
            "a killed interpreter left the seed gutted"
        )
        assert (seed_layout["snapshot"] / MARKER).exists()
        assert _scratch_in(seed_layout["root"]), (
            "no staging copy was left behind, so the process did not die where "
            "this pin claims it did — a `finally` must have run"
        )


class TestTheSeedIsNeverAbsentDuringASuccessfulRefresh:
    def test_every_copy_pass_sees_a_complete_seed(self, seed_layout, monkeypatch):
        """The invariant stated positively, on the path that SUCCEEDS: while the
        new copy is being built, the seed still serves its old contents."""
        observed: list[bytes | None] = []
        real = profile_copy.copy_delta

        def watching_copy_delta(source, target):
            observed.append(_login(seed_layout["snapshot"]))
            return real(source, target)

        monkeypatch.setattr(profile_copy, "copy_delta", watching_copy_delta)

        result = clone_storage._refresh_master_snapshot_if_safe("test")

        assert result["seed_refreshed"] is True
        assert observed and all(seen == SEED_LOGIN for seen in observed), observed
        assert _login(seed_layout["snapshot"]) == MASTER_LOGIN, "the refresh must land"

    def test_the_refreshed_seed_carries_its_marker(self, seed_layout):
        clone_storage._refresh_master_snapshot_if_safe("test")

        marker = json.loads(
            (seed_layout["snapshot"] / MARKER).read_text(encoding="utf-8")
        )
        assert marker["source_kind"] == "default-seed-test"
        assert marker["seeded_from"] == profile_seed.DEFAULT_SESSION


class TestTheDisplacedGenerationIsKept:
    def test_the_old_seed_survives_the_swap(self, seed_layout):
        """One previous generation, not a retention window: what it insures
        against is a copy that silently skipped a locked file, and the newest
        previous generation covers that. The cost stays flat at one extra
        profile — the argument is on ``profile_copy._displace``."""
        clone_storage._refresh_master_snapshot_if_safe("test")

        previous = seed_layout["snapshot"].with_name(
            seed_layout["snapshot"].name + profile_copy.PREVIOUS_SUFFIX
        )
        assert _login(previous) == SEED_LOGIN

    def test_a_second_refresh_replaces_rather_than_accumulates(self, seed_layout):
        clone_storage._refresh_master_snapshot_if_safe("one")
        clone_storage._refresh_master_snapshot_if_safe("two")

        assert _scratch_in(seed_layout["root"]) == [
            seed_layout["snapshot"].name + profile_copy.PREVIOUS_SUFFIX
        ]


class TestAFailedDisplaceChangesNothing:
    def test_a_target_that_cannot_be_moved_aside_is_refused(
        self, seed_layout, monkeypatch
    ):
        """``replace_tree`` answers False — and ``_copy_profile_tree`` turns
        that into ``TARGET_IN_USE`` — when the tree already in place cannot be
        moved out of the way. The alternative is a HALF-SWAP: a new copy
        published over a target we failed to displace, which is this finding
        again by another route.

        Driven through the real path rather than by stubbing the verdict: a
        previous generation that is still there and cannot be removed — the
        Windows lock ``rmtree_robust`` exists to tolerate — makes the rename
        onto it fail, which is exactly how this branch is reached in
        production.
        """
        previous = seed_layout["snapshot"].with_name(
            seed_layout["snapshot"].name + profile_copy.PREVIOUS_SUFFIX
        )
        (previous / "Default").mkdir(parents=True)
        (previous / "Default" / "stuck").write_bytes(b"locked")
        monkeypatch.setattr(profile_copy, "rmtree_robust", lambda *a, **k: None)

        result = clone_storage._refresh_master_snapshot_if_safe("test")

        assert result["seed_refreshed"] is False
        assert result["seed_error"] == clone_storage.SEED_IN_USE
        assert _login(seed_layout["snapshot"]) == SEED_LOGIN, "the seed moved anyway"


class TestScratchIsInvisibleToEveryCloneRootScan:
    """A staged copy carries a marker, so without this a storage sweep can
    select one as an eviction victim mid-build, and a displaced generation
    inflates the session-cap total."""

    def test_staging_is_never_an_eviction_victim(self, tmp_path):
        staged = tmp_path / f"sess{profile_copy.STAGING_SUFFIX}123-1"
        staged.mkdir()
        (staged / "data.bin").write_bytes(b"x" * 50_000)
        profile_seed.write_marker(
            staged, source=tmp_path, source_kind="test", seeded_from="default"
        )

        assert clone_storage._idle_autoclones_over_cap(tmp_path, cap_bytes=1000) == []

    def test_a_displaced_generation_is_never_trimmed(self, tmp_path):
        previous = tmp_path / f"sess{profile_copy.PREVIOUS_SUFFIX}"
        previous.mkdir()
        (previous / "data.bin").write_bytes(b"x" * 50_000)
        profile_seed.write_marker(
            previous, source=tmp_path, source_kind="explicit-test", seeded_from="x"
        )

        assert (
            clone_storage._named_profiles_over_session_cap(tmp_path, cap_bytes=1000)
            == []
        )

    def test_a_real_session_is_still_selected(self, tmp_path):
        """The skip is narrow: it must not shield an ordinary session, or the
        storage cap stops being enforced at all."""
        clone = tmp_path / "sess"
        clone.mkdir()
        (clone / "data.bin").write_bytes(b"x" * 50_000)
        profile_seed.write_marker(
            clone, source=tmp_path, source_kind="test", seeded_from="default"
        )

        assert clone_storage._idle_autoclones_over_cap(tmp_path, cap_bytes=1000) == [
            clone
        ]


class TestTheRefusalsAreUnchanged:
    """F-893's contract: a refusal is not a success, and it moves nothing."""

    def test_a_held_target_is_still_refused_and_left_alone(self, seed_layout):
        held_profile(seed_layout["snapshot"])

        result = clone_storage._refresh_master_snapshot_if_safe("test")

        assert result["seed_refreshed"] is False
        assert result["seed_error"] == clone_storage.SEED_IN_USE
        assert _login(seed_layout["snapshot"]) == SEED_LOGIN

    def test_a_refused_copy_leaves_no_scratch_behind(self, seed_layout):
        held_profile(seed_layout["snapshot"])

        clone_storage._refresh_master_snapshot_if_safe("test")

        assert _scratch_in(seed_layout["root"]) == [], (
            "a refusal must not pay for a copy it did not make"
        )

    def test_a_failed_copy_leaves_no_scratch_behind(self, seed_layout, monkeypatch):
        monkeypatch.setattr(profile_copy, "copy_delta", _dies_mid_copy)

        clone_storage._refresh_master_snapshot_if_safe("test")

        assert _scratch_in(seed_layout["root"]) == [], (
            "the half-built copy must not be left on disk"
        )


class TestStaleStagingIsReclaimed:
    def test_a_staging_copy_a_dead_process_left_is_removed(self, seed_layout):
        """The one leftover a ``finally`` cannot clean up, because the process
        that owned it is gone. Reclaimed by the next refresh of the same target
        rather than by a new sweep call site."""
        stale = seed_layout["snapshot"].with_name(
            f"{seed_layout['snapshot'].name}{profile_copy.STAGING_SUFFIX}999-1"
        )
        stale.mkdir()
        (stale / "half.bin").write_bytes(b"x" * 100)
        ancient = time.time() - profile_copy.STALE_STAGING_SECONDS - 60
        os.utime(stale, (ancient, ancient))

        clone_storage._refresh_master_snapshot_if_safe("test")

        assert not stale.exists(), "a dead process's staging copy leaked"

    def test_a_fresh_staging_copy_is_left_alone(self, seed_layout):
        """A concurrent refresh's in-flight build must survive: two refreshes
        can overlap (a close runs one on a worker thread while a spawn runs one
        on the loop), and reclaiming by age is only safe because the window is
        ~1000x the time a 101 MB copy takes."""
        live = seed_layout["snapshot"].with_name(
            f"{seed_layout['snapshot'].name}{profile_copy.STAGING_SUFFIX}998-1"
        )
        live.mkdir()

        clone_storage._refresh_master_snapshot_if_safe("test")

        assert live.exists(), "a sibling's in-flight copy was destroyed"


class TestTheCopyContractIsUnchanged:
    """The three things every caller of ``_copy_profile_tree`` relies on
    (``test_profile_resolution``'s pins), re-asserted here because the fix
    rewrites the function they describe."""

    def test_a_missing_source_still_creates_an_empty_target(self, seed_layout):
        target = seed_layout["sessions"] / "empty"

        refused = clone_storage._copy_profile_tree(
            seed_layout["root"] / "nonexistent", target, seed_layout["sessions"], "test"
        )

        assert refused is None
        assert target.exists() and list(target.iterdir()) == []

    def test_a_target_outside_the_clone_root_is_still_refused(self, seed_layout):
        with pytest.raises(ValueError, match="Refusing"):
            clone_storage._copy_profile_tree(
                seed_layout["snapshot"],
                seed_layout["root"] / "outside",
                seed_layout["sessions"],
                "test",
            )

    def test_a_held_target_still_reports_the_refusal_without_a_marker(
        self, seed_layout
    ):
        target = seed_layout["sessions"] / "held-clone"
        target.mkdir()
        held_profile(target)

        refused = clone_storage._copy_profile_tree(
            seed_layout["master"], target, seed_layout["sessions"], "test"
        )

        assert refused == clone_storage.TARGET_IN_USE
        assert not (target / MARKER).exists()

    def test_a_fresh_target_gets_every_source_file(self, seed_layout):
        """The two-pass copy's contract: everything in the source lands."""
        target = seed_layout["sessions"] / "fresh"

        clone_storage._copy_profile_tree(
            seed_layout["master"], target, seed_layout["sessions"], "test"
        )

        assert _login(target) == MASTER_LOGIN
        assert (target / "Local State").exists()
        assert (target / MARKER).exists()
