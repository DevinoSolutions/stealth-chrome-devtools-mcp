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
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import fail_a_rename, held_profile
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    profile_copy,
    profile_seed,
)

MARKER = profile_seed.MARKER_NAME

#: Chrome >= 96's cookie jar — the file that IS the login, per
#: ``profile_seed.LOGIN_WITNESSES``. The product's own spelling of it is pinned
#: to a single home by ``test_profile_seed_truth``; this is a fixture path.
#: What a refusal may say about a browser: an observation made BEFORE the copy,
#: never "no browser holds it" (``profile_lock`` cannot see every starting Chrome).
SAW_NO_BROWSER = "profile_lock saw no browser holding it before the copy started"

COOKIES = Path("Default") / "Network" / "Cookies"

SEED_LOGIN = b"seed-login-jar-the-old-generation"
MASTER_LOGIN = b"master-login-jar-the-new-generation"
STALE_LOGIN = b"stale-login-jar-a-leftover-generation"


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


def _fail_the_publish_once(monkeypatch) -> list[str]:
    """Make ONLY the staging -> target rename raise, and only once.

    ``_displace``'s rename has the TARGET as its source and the rollback's
    has the PREVIOUS generation, and neither of those names carries
    :data:`profile_copy.STAGING_SUFFIX` (it is not a substring of
    ``PREVIOUS_SUFFIX``), so both keep working for real. The pin therefore
    drives the production path and stubs no verdict.
    """
    raised: list[str] = []
    real_replace = Path.replace

    def _publish_fails(self, dest):
        if profile_copy.STAGING_SUFFIX in self.name and not raised:
            raised.append(self.name)
            raise OSError(5, "Access is denied")
        return real_replace(self, dest)

    monkeypatch.setattr(Path, "replace", _publish_fails)
    return raised


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
from pathlib import Path
from types import SimpleNamespace
from stealth_chrome_devtools_mcp.embedded import clone_storage, profile_copy

# This child runs WITHOUT conftest, so `tests/operator_fence.py` is not
# installed and nothing else would stop it writing into the operator's real
# browser-session root if the env below failed to apply. It fences itself:
# every directory it is about to touch must resolve under the tmp root the
# parent passed, and it refuses to run at all otherwise. `staging.mkdir`
# happens before the copy is patched out, so "it would have written nothing"
# is not true and this guard is not decorative.
allowed = Path(os.environ["F925_TMP_ROOT"]).resolve()
for resolved in (clone_storage.master_profile_dir(),
                 clone_storage.master_snapshot_dir(),
                 clone_storage.clone_root_dir()):
    if allowed not in resolved.resolve().parents:
        sys.exit(f"refusing to run outside {allowed}: {resolved}")

def die(source, target):
    Path(os.environ["F925_TOMBSTONE"]).write_bytes(b"reached")
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
            "F925_TMP_ROOT": str(seed_layout["root"]),
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
        """``replace_tree`` answers ``TARGET_LOCKED`` (T-10522; it was reported
        as ``TARGET_IN_USE``, which says a browser holds the seed) when the
        tree already in place cannot be moved out of the way. The alternative is a HALF-SWAP: a new copy
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
        assert result["seed_error"].startswith(
            (profile_copy.TARGET_LOCKED, profile_copy.TARGET_NOT_REPLACED)
        )
        assert _login(seed_layout["snapshot"]) == SEED_LOGIN, "the seed moved anyway"


class TestAFailedPublishPutsTheOldTreeBack:
    """The gap between the two renames NOT closing (N1).

    ``_displace`` has succeeded, so the target is absent and the only complete
    old tree is the displaced one. If the publishing rename then RAISES — a
    Windows handle on the staging tree, which an AV scanner or an indexer can
    take at any moment — the exception propagates and ``replace_tree``'s
    ``finally`` removes the complete, stamped new tree. What is left is: target
    ABSENT, old tree only at ``*.stealth-previous``, new tree gone.

    That is not the microsecond gap F-926 is about; it is that gap never
    closing. Every later spawn falls through ``resolve_profile_selection``'s
    ``elif master.exists()`` branch and copies the LIVE shared profile, which
    carries zero cookies — permanently, not for an instant. So it is closed by
    putting the displaced tree back, not by making the window shorter.
    """

    def test_a_publish_that_raises_restores_the_displaced_tree(
        self, seed_layout, monkeypatch
    ):
        raised = _fail_the_publish_once(monkeypatch)

        result = clone_storage._refresh_master_snapshot_if_safe("test")

        assert raised, "the publishing rename was never reached"
        assert _login(seed_layout["snapshot"]) == SEED_LOGIN, (
            "the seed is absent, or holds a tree that was never published"
        )
        marker = json.loads((seed_layout["snapshot"] / MARKER).read_text())
        assert marker["source_kind"] == "test-fixture", (
            "the tree at the target is not the one that was displaced"
        )
        assert result["seed_refreshed"] is False
        assert result["seed_error"].startswith(profile_copy.PUBLISH_FAILED)
        assert _scratch_in(seed_layout["root"]) == [], (
            "the rollback left the displaced generation or the staging copy behind"
        )

    def test_the_rollback_restores_the_displaced_tree_not_a_leftover(
        self, seed_layout, monkeypatch
    ):
        """``_displace`` removes a stale generation and puts the LIVE tree on
        that name, so what a rollback restores is the tree THIS refresh
        displaced — never the one that was sitting there first."""
        _profile(
            seed_layout["snapshot"].with_name(
                seed_layout["snapshot"].name + profile_copy.PREVIOUS_SUFFIX
            ),
            STALE_LOGIN,
        )
        raised = _fail_the_publish_once(monkeypatch)

        result = clone_storage._refresh_master_snapshot_if_safe("test")

        assert raised, "the publishing rename was never reached"
        assert _login(seed_layout["snapshot"]) == SEED_LOGIN, (
            "the seed is not the tree this refresh displaced"
        )
        assert result["seed_refreshed"] is False

    def test_a_leftover_previous_generation_is_never_published(
        self, seed_layout, monkeypatch
    ):
        """The rollback is keyed on whether THIS refresh displaced something,
        never on ``previous.exists()``.

        A previous generation can be a LEFTOVER from an earlier refresh taken
        when the target did not exist, and restoring that one publishes a
        STALER tree as the seed — this finding's own harm by a new route,
        committed by the fix for it. So: no target, a stale previous beside it,
        a publish that raises. Nothing was displaced, so nothing may be put
        back.

        Deliberately does NOT assert the scratch is gone: a refresh that
        displaced nothing leaves that leftover exactly where it found it.
        """
        _profile(
            seed_layout["snapshot"].with_name(
                seed_layout["snapshot"].name + profile_copy.PREVIOUS_SUFFIX
            ),
            STALE_LOGIN,
        )
        shutil.rmtree(seed_layout["snapshot"])
        raised = _fail_the_publish_once(monkeypatch)

        result = clone_storage._refresh_master_snapshot_if_safe("test")

        assert raised, "the publishing rename was never reached"
        assert _login(seed_layout["snapshot"]) != STALE_LOGIN, (
            "a leftover previous generation was published as the seed"
        )
        assert result["seed_refreshed"] is False


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
        # SOFT GOLDEN UPDATED for F-966: the empty target used to carry nothing at
        # all. It is the first-install clone now, so it carries its marker (what
        # makes it a clone the quota and GC own, seeded from nothing) and no profile.
        assert target.exists()
        assert [p.name for p in target.iterdir()] == [MARKER]
        assert profile_seed.read_marker(target)["seeded_from"] == profile_seed.NO_SEED

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

        assert refused.code == clone_storage.TARGET_IN_USE
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


# ---------------------------------------------------------------------------
# T-10522 — a lock something else holds is not a browser holding the profile
# ---------------------------------------------------------------------------


@pytest.fixture()
def idle_clone(seed_layout, monkeypatch):
    """A clone directory that EXISTS, holds a login and is held by nothing —
    the state an unnamed spawn finds when its previous browser has just gone."""
    monkeypatch.setattr(profile_copy, "RENAME_BACKOFF_SECONDS", 0)
    clone = _profile(seed_layout["sessions"] / "previous-clone", STALE_LOGIN)
    profile_seed.write_marker(
        clone, source=clone, source_kind="test", seeded_from="default"
    )
    return clone


def _copy_onto(layout: dict, clone: Path):
    return clone_storage._copy_profile_tree(
        layout["master"], clone, layout["sessions"], "test"
    )


class TestEachRefusalNamesItsOwnCause:
    """The copier is the one place that knows WHY a copy did not run. Three
    causes shared ``TARGET_IN_USE``, so an unnamed spawn that lost a race with
    OneDrive or an antivirus scan was told a browser held a profile that no
    browser held (T-10522, observed on the live MCP 2026-10-29)."""

    def test_a_target_that_cannot_be_moved_aside_is_target_locked(
        self, seed_layout, idle_clone, monkeypatch
    ):
        attempts = fail_a_rename(
            monkeypatch, "displace", PermissionError(13, "Access is denied")
        )

        refused = _copy_onto(seed_layout, idle_clone)

        assert attempts, "the displacing rename was never reached"
        assert refused.code == profile_copy.TARGET_LOCKED
        assert refused.code != clone_storage.TARGET_IN_USE
        assert "PermissionError" in refused.detail
        assert str(idle_clone) in refused.detail
        assert SAW_NO_BROWSER in refused.detail
        assert "No browser holds it" not in refused.detail
        assert "can be retried" in refused.detail
        assert "target-in-use" not in str(refused)
        assert _login(idle_clone) == STALE_LOGIN, "the tree moved anyway"
        assert _scratch_in(seed_layout["sessions"]) == []

    def test_a_publish_that_raises_is_publish_failed(
        self, seed_layout, idle_clone, monkeypatch
    ):
        attempts = fail_a_rename(
            monkeypatch, "publish", PermissionError(13, "Access is denied")
        )

        refused = _copy_onto(seed_layout, idle_clone)

        assert attempts, "the publishing rename was never reached"
        assert refused.code == profile_copy.PUBLISH_FAILED
        assert "PermissionError" in refused.detail
        assert SAW_NO_BROWSER in refused.detail
        assert "No browser holds it" not in refused.detail
        assert "can be retried" in refused.detail
        assert "target-in-use" not in str(refused)
        assert _login(idle_clone) == STALE_LOGIN, "the old tree was not put back"
        assert _scratch_in(seed_layout["sessions"]) == []

    def test_a_live_browser_is_still_target_in_use(self, seed_layout, idle_clone):
        held_profile(idle_clone)

        refused = _copy_onto(seed_layout, idle_clone)

        assert refused.code == clone_storage.TARGET_IN_USE
        assert "a browser holds it" in refused.detail
        assert "pid" in refused.detail
        assert SAW_NO_BROWSER not in refused.detail
        assert "retried" not in refused.detail
        assert _login(idle_clone) == STALE_LOGIN

    def test_the_four_codes_are_four_codes(self):
        codes = {
            clone_storage.TARGET_IN_USE,
            profile_copy.TARGET_LOCKED,
            profile_copy.TARGET_NOT_REPLACED,
            profile_copy.PUBLISH_FAILED,
        }
        assert len(codes) == 4

    def test_an_error_that_is_not_a_lock_claims_neither_a_lock_nor_a_retry(
        self, seed_layout, idle_clone, monkeypatch
    ):
        """An I/O error is not evidence of a lock, so the move-aside code is
        the neutral one and the sentence says nothing about retrying."""
        attempts = fail_a_rename(monkeypatch, "displace", OSError(5, "I/O error"))

        refused = _copy_onto(seed_layout, idle_clone)

        assert len(attempts) == 1
        assert refused.code == profile_copy.TARGET_NOT_REPLACED
        assert "locked" not in str(refused).lower()
        assert "retried" not in refused.detail
        assert "OSError" in refused.detail
        assert str(idle_clone) in refused.detail
        assert SAW_NO_BROWSER in refused.detail
        assert "Nothing was changed" in refused.detail
        assert _login(idle_clone) == STALE_LOGIN

    def test_the_refresh_reports_a_lock_in_its_own_words(
        self, seed_layout, monkeypatch
    ):
        """``seed_error`` is what ``stealthy`` prints and ``close_instance``
        returns: only a held seed may say ``seed-in-use``."""
        monkeypatch.setattr(profile_copy, "RENAME_BACKOFF_SECONDS", 0)
        fail_a_rename(monkeypatch, "displace", PermissionError(13, "denied"))

        locked = clone_storage._refresh_master_snapshot_if_safe("test")

        assert locked["seed_refreshed"] is False
        assert locked["seed_error"].startswith(profile_copy.TARGET_LOCKED)
        assert locked["seed_error"] != clone_storage.SEED_IN_USE

    def test_a_held_seed_still_says_seed_in_use(self, seed_layout):
        held_profile(seed_layout["snapshot"])

        held = clone_storage._refresh_master_snapshot_if_safe("test")

        assert held["seed_error"] == clone_storage.SEED_IN_USE

    def test_require_copied_puts_the_cause_and_the_sentence_in_the_error(
        self, tmp_path
    ):
        refusal = profile_copy.Refusal(profile_copy.TARGET_LOCKED, "the sentence")
        with pytest.raises(clone_storage.ToolError) as excinfo:
            clone_storage._require_copied(refusal, tmp_path)
        assert str(tmp_path) in str(excinfo.value)
        assert "target-locked" in str(excinfo.value)
        assert "the sentence" in str(excinfo.value)


class TestATransientLockIsRetriedABoundedNumberOfTimes:
    """The retry exists because the lock is usually gone a moment later (the
    observed spawn succeeded on an immediate retry), and is bounded because a
    lock that stays is a fact the caller must be told, not waited out."""

    @pytest.mark.parametrize("step", ["displace", "publish"])
    def test_a_lock_that_clears_is_not_reported(
        self, step, seed_layout, idle_clone, monkeypatch
    ):
        warnings: list[str] = []
        monkeypatch.setattr(
            profile_copy.debug_logger,
            "log_warning",
            lambda *args: warnings.append(args[-1]),
        )
        attempts = fail_a_rename(
            monkeypatch,
            step,
            PermissionError(13, "Access is denied"),
            times=profile_copy.RENAME_ATTEMPTS - 1,
        )

        refused = _copy_onto(seed_layout, idle_clone)

        assert refused is None
        assert len(attempts) == profile_copy.RENAME_ATTEMPTS - 1
        assert _login(idle_clone) == MASTER_LOGIN
        assert len(warnings) == 1, "one WARNING per copy, with the cause"
        assert "PermissionError" in warnings[0]
        assert str(idle_clone) in warnings[0]

    @pytest.mark.parametrize("step", ["displace", "publish"])
    def test_a_lock_that_stays_is_tried_exactly_the_bound_and_then_reported(
        self, step, seed_layout, idle_clone, monkeypatch
    ):
        attempts = fail_a_rename(
            monkeypatch, step, PermissionError(13, "Access is denied")
        )

        refused = _copy_onto(seed_layout, idle_clone)

        assert len(attempts) == profile_copy.RENAME_ATTEMPTS == 3
        assert refused.code == (
            profile_copy.TARGET_LOCKED
            if step == "displace"
            else profile_copy.PUBLISH_FAILED
        )
        assert _login(idle_clone) == STALE_LOGIN

    def test_the_bound_costs_well_under_a_second_per_rename(self):
        waits = [
            profile_copy.RENAME_BACKOFF_SECONDS * n
            for n in range(1, profile_copy.RENAME_ATTEMPTS)
        ]
        assert sum(waits) <= 1.0

    def test_an_error_a_wait_cannot_clear_is_not_retried(
        self, seed_layout, idle_clone, monkeypatch
    ):
        attempts = fail_a_rename(monkeypatch, "publish", OSError(5, "I/O error"))

        refused = _copy_onto(seed_layout, idle_clone)

        assert len(attempts) == 1
        assert refused.code == profile_copy.PUBLISH_FAILED
        assert "locked" not in str(refused).lower()
        assert "retried" not in refused.detail
        assert SAW_NO_BROWSER in refused.detail

    def test_a_live_browser_is_refused_before_any_rename(
        self, seed_layout, idle_clone, monkeypatch
    ):
        held_profile(idle_clone)
        attempts = fail_a_rename(
            monkeypatch, "displace", PermissionError(13, "Access is denied")
        )

        refused = _copy_onto(seed_layout, idle_clone)

        assert refused.code == clone_storage.TARGET_IN_USE
        assert attempts == [], "a held profile must never reach a rename"


def _tree(root: Path) -> dict[str, bytes] | None:
    """Every file under *root* with its bytes, or None when *root* is absent —
    what a concurrent copy that chose it as its SOURCE would read."""
    if not root.exists():
        return None
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _read_the_target_at_every_sleep(
    monkeypatch, target: Path, slept: list[float] | None = None
) -> list:
    """Give ``profile_copy`` a clock whose ``sleep`` READS *target* and returns
    what it found, so a test can assert on the tree a concurrent reader would
    have seen at each pause. The requested durations go to *slept* when given;
    nothing really sleeps."""
    seen: list = []

    def _sleep(seconds: float) -> None:
        seen.append(_tree(target))
        if slept is not None:
            slept.append(seconds)

    monkeypatch.setattr(
        profile_copy, "time", SimpleNamespace(sleep=_sleep, time=time.time)
    )
    return seen


class TestTheTargetIsNeverAbsentWhileAnythingSleeps:
    """Codex round 2, HIGH. The publish backoff slept between the two renames,
    so ``master-snapshot`` was MISSING for the pause, and a concurrent copy that
    had already chosen it as its source ran both passes inside the gap:
    ``copy_delta`` accepts a missing source and the result was an empty clone
    published as a success. The wait now comes AFTER the old tree is back."""

    @pytest.mark.parametrize("times", [None, profile_copy.RENAME_ATTEMPTS - 1])
    @pytest.mark.parametrize("step", ["displace", "publish"])
    def test_every_pause_sees_the_complete_old_tree(
        self, step, times, seed_layout, idle_clone, monkeypatch
    ):
        old = _tree(idle_clone)
        assert old, "the fixture has to hold a tree to lose"
        seen = _read_the_target_at_every_sleep(monkeypatch, idle_clone)
        fail_a_rename(
            monkeypatch, step, PermissionError(13, "Access is denied"), times=times
        )

        _copy_onto(seed_layout, idle_clone)

        assert len(seen) >= profile_copy.RENAME_ATTEMPTS - 1, "no pause was reached"
        assert all(tree == old for tree in seen), (
            "a pause saw the target absent or incomplete: "
            f"{[None if t is None else sorted(t) for t in seen if t != old]}"
        )

    def test_a_publish_that_keeps_failing_pauses_between_whole_steps(
        self, seed_layout, idle_clone, monkeypatch
    ):
        seen = _read_the_target_at_every_sleep(monkeypatch, idle_clone)
        attempts = fail_a_rename(
            monkeypatch, "publish", PermissionError(13, "Access is denied")
        )

        refused = _copy_onto(seed_layout, idle_clone)

        assert len(attempts) == profile_copy.RENAME_ATTEMPTS
        # the settle between the two copy passes, then one pause after each
        # failed publish but the last
        assert len(seen) == 1 + profile_copy.RENAME_ATTEMPTS - 1
        assert refused.code == profile_copy.PUBLISH_FAILED
        assert _scratch_in(seed_layout["sessions"]) == []

    def test_a_restore_that_fails_still_raises(
        self, seed_layout, idle_clone, monkeypatch
    ):
        """2.1.53's behaviour, kept: the target really is absent then, and the
        caller has to hear about it rather than be told nothing changed."""
        monkeypatch.setattr(profile_copy, "_undisplace", lambda _target: False)
        fail_a_rename(monkeypatch, "publish", PermissionError(13, "Access is denied"))

        with pytest.raises(PermissionError):
            _copy_onto(seed_layout, idle_clone)


class TestOneRetryBudgetPerCall:
    """Codex round 3, MED. The move-aside had its own three attempts inside each
    of the publish's three: a move-aside failing twice then passing, with every
    publish failing, was 9 move-asides, 3 publishes and 1.2 s of pauses. The
    bound is ONE budget for the call: ``RENAME_ATTEMPTS`` transient failures in
    all, whichever step fails, and the pauses 0.1 s + 0.2 s in total."""

    @pytest.mark.parametrize("displace_failures", [0, 1, 2, 3])
    def test_the_budget_is_shared_by_both_steps(
        self, displace_failures, seed_layout, idle_clone, monkeypatch
    ):
        monkeypatch.setattr(profile_copy, "RENAME_BACKOFF_SECONDS", 0.1)
        slept: list[float] = []
        seen = _read_the_target_at_every_sleep(monkeypatch, idle_clone, slept)
        old = _tree(idle_clone)
        displaced = fail_a_rename(
            monkeypatch,
            "displace",
            PermissionError(13, "Access is denied"),
            times=displace_failures,
        )
        published = fail_a_rename(
            monkeypatch, "publish", PermissionError(13, "Access is denied")
        )

        refused = _copy_onto(seed_layout, idle_clone)

        assert refused is not None
        assert len(displaced) + len(published) == profile_copy.RENAME_ATTEMPTS == 3
        # the settle between the two copy passes, then exactly two pauses
        assert slept == [profile_copy.RETRY_SETTLE_SECONDS, 0.1, 0.2]
        assert sum(slept[1:]) == pytest.approx(0.3)
        assert all(tree == old for tree in seen)
        assert _login(idle_clone) == STALE_LOGIN
        assert _scratch_in(seed_layout["sessions"]) == []

    def test_a_move_aside_that_recovers_still_leaves_the_publish_its_share(
        self, seed_layout, idle_clone, monkeypatch
    ):
        """Two move-aside failures spend two of the three; the publish that
        follows may fail once more and is then reported, not retried."""
        displaced = fail_a_rename(
            monkeypatch, "displace", PermissionError(13, "denied"), times=2
        )
        published = fail_a_rename(monkeypatch, "publish", PermissionError(13, "denied"))

        refused = _copy_onto(seed_layout, idle_clone)

        assert (len(displaced), len(published)) == (2, 1)
        assert refused.code == profile_copy.PUBLISH_FAILED


class TestARefusalSaysOnlyWhatWasObserved:
    """Codex round 3, MED. ``profile_lock saw no browser holding it`` was said
    even when no check had run: an initial seed refresh has no snapshot, so the
    holder check is skipped, and the sentence then described an observation
    nobody made."""

    SAW = "profile_lock saw no browser holding it"

    def _spy_on_the_holder_check(self, monkeypatch) -> list[Path]:
        asked: list[Path] = []
        real = clone_storage._profile_hold

        def _spy(profile_dir):
            asked.append(profile_dir)
            return real(profile_dir)

        monkeypatch.setattr(clone_storage, "_profile_hold", _spy)
        return asked

    def test_an_ordinary_copy_says_the_check_found_nobody(
        self, seed_layout, idle_clone, monkeypatch
    ):
        asked = self._spy_on_the_holder_check(monkeypatch)
        fail_a_rename(monkeypatch, "publish", PermissionError(13, "denied"))

        refused = _copy_onto(seed_layout, idle_clone)

        assert idle_clone in asked, "the check never ran, so the claim is unearned"
        assert self.SAW in refused.detail

    def test_an_initial_seed_refresh_says_nothing_about_holders(
        self, seed_layout, monkeypatch
    ):
        monkeypatch.setattr(profile_copy, "RENAME_BACKOFF_SECONDS", 0)
        shutil.rmtree(seed_layout["snapshot"])
        asked = self._spy_on_the_holder_check(monkeypatch)
        fail_a_rename(monkeypatch, "publish", PermissionError(13, "denied"))

        result = clone_storage._refresh_master_snapshot_if_safe("test")

        assert seed_layout["snapshot"] not in asked, "an absent path was asked about"
        assert result["seed_refreshed"] is False
        assert result["seed_error"].startswith(profile_copy.PUBLISH_FAILED)
        assert "profile_lock" not in result["seed_error"]
        assert "holding" not in result["seed_error"]
        assert "The same call can be retried" in result["seed_error"]

    def test_a_target_that_does_not_exist_never_claims_a_check(
        self, seed_layout, monkeypatch
    ):
        monkeypatch.setattr(profile_copy, "RENAME_BACKOFF_SECONDS", 0)
        fail_a_rename(monkeypatch, "publish", OSError(5, "I/O error"))

        refused = _copy_onto(seed_layout, seed_layout["sessions"] / "brand-new")

        assert refused.code == profile_copy.PUBLISH_FAILED
        assert "profile_lock" not in refused.detail
