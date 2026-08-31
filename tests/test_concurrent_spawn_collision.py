"""F-834 pins: concurrent spawns must not funnel into one profile directory,
and one instance's cleanup must not delete another's live profile.

The incident (v2.0.7, backend-163320.log, corr b42ef5202147): three agents
spawned against one backend. `clone_storage._unique_clone_dir` named the
retry/fallback clone `{base}-{os.getpid()}-{suffix}` — the BACKEND's pid, which
is identical for every concurrent spawn in the process — and the only guard,
`_profile_has_running_browser`, is False for ALL of them during their pre-launch
window (it is a liveness check, not a reservation). Every loser of the master
race landed in the SAME `…-163320-retry` directory. Then a deferred profile
delete fired against that shared path and removed it out from under the one
attempt that had already been reported `ready`, which was dead ~2.5s later.

Three layers are pinned here, one per defect:

* **Per-attempt uniqueness** — the selection helpers must hand out a distinct
  directory per spawn ATTEMPT, not per process, and an in-flight spawn's
  reservation must make its directory unavailable to the next caller.
* **Cleanup ownership at FIRE time** — a deferred delete is decided at defer
  time and fired later; in between, another tracked instance can become the
  live owner of that path. `cleanup_deferred_profiles` must re-ask then, not
  trust the answer it cached at defer time. The regression guard below keeps
  that from degrading into "never delete anything".
* **Honest error text** — nodriver's "you need to pass no_sandbox=True /
  running as root" advice is a red herring for this failure mode; it cost two
  independent diagnosing agents real time. A failed spawn that raced siblings
  says so, following the F-811 `exhaustion_hint` append pattern.

Hermetic: no Chrome is launched, no real ~/.stealth-mcp is touched, and the
process table is never scanned for real (`os.getpid()` is the one live pid used,
because this test process is the only process guaranteed to be alive).
"""

import asyncio
import os
import time

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    spawn_contention,
    spawn_exhaustion,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import (
    ProcessCleanup,
    process_cleanup,
)

# ---------------------------------------------------------------------------
# Layer 1: per-attempt clone directories
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_reservations():
    clone_storage._clear_protected_clone_dirs()
    yield
    clone_storage._clear_protected_clone_dirs()


def test_retry_clone_dirs_are_distinct_per_attempt(tmp_path):
    """Two concurrent spawns resolving a RETRY clone must not collide.

    This is the exact incident shape: both callers are pre-launch, so neither
    directory has a running browser and the old `-{pid}-retry` name was the same
    string for both.
    """
    base = tmp_path / "sessions" / "sess-f876e3d7f2ec"
    first = clone_storage._unique_clone_dir(base, "retry")
    second = clone_storage._unique_clone_dir(base, "retry")

    assert first != second, (
        f"concurrent retries collided on {first} — the suffix is per-process, "
        "not per-attempt (F-834)"
    )
    assert "retry" in first.name and "retry" in second.name
    assert first.parent == second.parent == base.parent


def test_fallback_clone_dirs_are_distinct_per_attempt(tmp_path):
    """The second fallback rung (`clone_suffix="snapshot"`) has the same duty."""
    base = tmp_path / "sessions" / "sess-f876e3d7f2ec"
    dirs = {clone_storage._unique_clone_dir(base, "snapshot") for _ in range(8)}
    assert len(dirs) == 8, f"8 attempts produced {len(dirs)} distinct dirs"


def test_reserved_base_clone_is_not_handed_to_a_second_caller(tmp_path):
    """`_available_clone_dir` must honour an in-flight spawn's reservation.

    `_protect_clone_dir` is the existing in-process reservation set. Without it
    being consulted, two concurrent spawns both find the session's base clone
    "free" (no browser has launched yet) and both copy into it.
    """
    base = tmp_path / "sessions" / "sess-f876e3d7f2ec"
    first = clone_storage._available_clone_dir(base)
    assert first == base, "an unreserved, unused base clone is still reused"

    clone_storage._protect_clone_dir(first)
    second = clone_storage._available_clone_dir(base)
    assert second != first, (
        f"second concurrent spawn was handed the reserved dir {second} (F-834)"
    )


def test_two_reserved_losers_still_get_distinct_dirs(tmp_path):
    """Both losers of the base-clone race must land somewhere different."""
    base = tmp_path / "sessions" / "sess-f876e3d7f2ec"
    clone_storage._protect_clone_dir(base)
    loser_a = clone_storage._available_clone_dir(base)
    clone_storage._protect_clone_dir(loser_a)
    loser_b = clone_storage._available_clone_dir(base)

    assert base not in (loser_a, loser_b)
    assert loser_a != loser_b, "both losers were handed the same `-{pid}` dir (F-834)"


# ---------------------------------------------------------------------------
# Layer 2: cleanup ownership guard
# ---------------------------------------------------------------------------


def _entry(user_data_dir, pid):
    return {
        "pid": pid,
        "create_time": None,
        "user_data_dir": str(user_data_dir),
        "uses_custom_data_dir": False,
        "auto_clone": True,
        "timestamp": time.time(),
    }


def _seed_cleanup(tmp_path, entries):
    """A ProcessCleanup holding *entries*, blind to the real process table."""
    pc = ProcessCleanup.__new__(ProcessCleanup)
    pc.pid_file = tmp_path / "pids.json"
    pc.tracked_pids = set()
    pc.orphan_profile_max_age_seconds = 21600
    pc._init_time = time.time()
    pc.browser_processes = dict(entries)
    # Hermetic: the real machine's browsers are never consulted, so the ONLY
    # thing that can spare a directory here is the ownership guard under test.
    pc._get_active_browser_profile_dirs = lambda: set()
    pc._get_browser_pids_for_profile = lambda _dir: set()
    return pc


def _profile(tmp_path, name):
    d = tmp_path / "sessions" / name
    d.mkdir(parents=True)
    (d / "Cookies").write_bytes(b"sqlite-cookie-stub")
    return d


def test_deferred_delete_skips_a_profile_a_live_instance_owns(tmp_path):
    """The fatal step, pinned: the winner's directory survives the sweep.

    `dead-loser` deferred a delete of the shared path; by the time the sweep
    fires, `live-winner` owns that same path with a running browser. Deleting it
    is what turned a `ready` spawn into a corpse 2.5s later.
    """
    shared = _profile(tmp_path, "sess-shared-retry")
    pc = _seed_cleanup(
        tmp_path,
        {
            "dead-loser": _entry(shared, None),
            "live-winner": _entry(shared, os.getpid()),
        },
    )

    pc.cleanup_deferred_profiles()

    assert shared.exists(), (
        "deferred cleanup deleted a profile directory owned by a live tracked "
        "instance (F-834)"
    )
    assert "live-winner" in pc.browser_processes, "the live owner must stay tracked"


def test_deferred_delete_still_fires_for_a_dead_owner(tmp_path):
    """Regression guard: the guard must not become "never delete anything".

    Without this, layer 2 could ship as an unconditional skip and every leaked
    clone would live forever (the very leak `cleanup_deferred_profiles` exists
    to close).
    """
    orphan = _profile(tmp_path, "sess-orphan")
    pc = _seed_cleanup(tmp_path, {"dead-instance": _entry(orphan, None)})

    finalized = pc.cleanup_deferred_profiles()

    assert not orphan.exists(), "an unowned deferred profile must still be reclaimed"
    assert finalized == 1
    assert "dead-instance" not in pc.browser_processes


def test_deferred_delete_fires_when_the_other_claimant_is_also_dead(tmp_path):
    """Two dead entries on one path is a genuine leak, not shared ownership."""
    shared = _profile(tmp_path, "sess-both-dead")
    pc = _seed_cleanup(
        tmp_path,
        {"dead-a": _entry(shared, None), "dead-b": _entry(shared, None)},
    )

    pc.cleanup_deferred_profiles()

    assert not shared.exists(), "no live owner — the directory must be reclaimed"


def test_ownership_guard_does_not_block_an_instance_cleaning_its_own_dir(tmp_path):
    """The guard looks at OTHER instances only; self-cleanup is the normal path."""
    own = _profile(tmp_path, "sess-own")
    pc = _seed_cleanup(tmp_path, {"mine": _entry(own, None)})

    assert pc._cleanup_profile_dir(str(own), "mine") is True
    assert not own.exists()


# ---------------------------------------------------------------------------
# Layer 3: honest error text on the contention path
# ---------------------------------------------------------------------------

INNER_FAILURE = (
    "Failed to connect to browser -- Possibly because you are running as "
    "root? In that case you need to pass no_sandbox=True"
)


@pytest.fixture
def doomed_manager(monkeypatch, tmp_path):
    """A BrowserManager whose launch phase always fails, reaching spawn's except.

    Same seam as tests/test_spawn_exhaustion_hint.py: the WIRING is under test,
    never a real launch. The exhaustion hint is silenced so the assertions below
    read the contention paragraph alone.
    """

    async def failing_launch(self, options, browser_executable, launch_args):
        await asyncio.sleep(0.02)  # overlap the sibling spawn's in-flight window
        raise RuntimeError(INNER_FAILURE)

    monkeypatch.setattr(
        BrowserManager,
        "_resolve_launch_args",
        lambda self, options, proxy, platform_info: ([], "/fake/chrome", []),
    )
    monkeypatch.setattr(BrowserManager, "_launch_browser", failing_launch)
    monkeypatch.setattr(process_cleanup, "pid_file", tmp_path / "browser_pids.json")
    monkeypatch.setattr(spawn_exhaustion, "exhaustion_hint", lambda path: None)
    return BrowserManager()


async def test_contended_spawn_failure_names_contention_and_disowns_the_advice(
    doomed_manager,
):
    """Two overlapping spawns fail; BOTH errors carry the honest paragraph.

    "Both" is the load-bearing word: the losers of one race fail in sequence, so
    the live in-flight count is back to 1 by the last of them. Only a per-burst
    PEAK tells the last loser it was contended too — and the last loser is
    exactly the caller most likely to be the one reading the message.
    """
    results = await asyncio.gather(
        doomed_manager.spawn_browser(BrowserOptions()),
        doomed_manager.spawn_browser(BrowserOptions()),
        return_exceptions=True,
    )

    for error in results:
        message = str(error)
        assert INNER_FAILURE in message, "the underlying failure must survive"
        assert "F-834" in message, f"no contention hint in: {message}"
        assert "no_sandbox" in message.split("F-834")[1], (
            "the hint must name the misleading advice it is correcting"
        )
        assert "concurrent" in message.lower()


async def test_solo_spawn_failure_is_left_undecorated(doomed_manager):
    """One spawn in flight is not contention — the error stays byte-identical."""
    with pytest.raises(Exception) as err:
        await doomed_manager.spawn_browser(BrowserOptions())
    assert str(err.value) == INNER_FAILURE


def test_contention_hint_carries_its_own_separator_and_never_raises():
    """Same two contracts F-811's hint holds: self-separated, and inert below
    the threshold so the call site is a bare concatenation."""
    assert spawn_contention.contention_hint(1) is None
    assert spawn_contention.contention_hint(0) is None
    hint = spawn_contention.contention_hint(3)
    assert hint.startswith("\n\n")
    assert "3" in hint


def test_in_flight_counters_return_to_zero_after_a_burst(doomed_manager):
    """A leaked count or a leaked PEAK would decorate every later solo failure
    with a stale contention paragraph — pin the finally and the burst reset."""
    assert doomed_manager._spawns_in_flight == 0

    async def burst():
        await asyncio.gather(
            doomed_manager.spawn_browser(BrowserOptions()),
            doomed_manager.spawn_browser(BrowserOptions()),
            return_exceptions=True,
        )

    asyncio.run(burst())
    assert doomed_manager._spawns_in_flight == 0
    assert doomed_manager._spawn_peak_in_flight == 0, (
        "a stale peak would tell the next solo failure it was contended"
    )
