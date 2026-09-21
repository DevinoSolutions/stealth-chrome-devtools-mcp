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
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeBrowserManager
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    profile_lock,
    profile_seed,
    spawn_contention,
    spawn_exhaustion,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import (
    ProcessCleanup,
    process_cleanup,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: The instance id of the sibling spawn that WON a stage-1 race — see
#: :func:`_sibling_driving`.
SIBLING_ID = "sibling-on-master"


def _driving(directory: Path):
    """The F-898 witness as a plain predicate, answering True for exactly one
    directory. ``profile_seed.same_dir`` and not ``==`` because the product
    compares that way — a caller names a session, the resolver holds a path."""
    return lambda profile: profile_seed.same_dir(profile, directory)


def _sibling_driving(directory: Path) -> dict:
    """``FakeBrowserManager`` kwargs for "this backend ALREADY drives a browser
    on *directory*" — the snapshot ``spawn_browser`` takes before its loop.

    Needed since F-914: the winner of a stage-1 race is a spawn of THIS backend,
    and that is now what decides whether the loser gets a clone with the shared
    jar handed over or a refusal by name. The directory is seeded through
    ``profiles=`` because it lives on the instance ENTRY's ``options`` and
    nowhere else — ``BrowserInstance`` has no such field (F-898).
    """
    return {
        "instances": [SimpleNamespace(instance_id=SIBLING_ID)],
        "profiles": {SIBLING_ID: str(directory)},
    }


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


def test_the_hint_names_the_count_and_never_asserts_a_shared_directory():
    """The paragraph may claim only what this module can know: an integer.

    F-834's own layers hand concurrent spawns DISTINCT reserved directories —
    stage 2 per ATTEMPT, stage 1 for the loser of the master race — so a hint
    that states they "contend for the same Chrome profile" asserts a mechanism
    it never measured, and one that is frequently false. Measured false on the
    coverage gate's macOS/ARM64 cell (run 35150887345, attempt 2): a fleet that
    had already serialised its one master-taking lead spawn still lost a
    FOLLOWER to `ConnectionRefusedError` with every follower on its own
    directory, where the likelier cause was a two-core runner.
    """
    hint = spawn_contention.contention_hint(5)

    assert "5 spawn_browser calls were in flight" in hint
    assert "same Chrome profile" not in hint, "the hint asserted the mechanism"
    assert "measured" in hint, "the hint must say which part of it IS known"
    # Both causes offered, neither asserted as the answer.
    assert "user-data-dir" in hint and "CPU" in hint
    # The one remedy that serves both causes survives.
    assert "Serialize the spawns" in hint
    # F-834 stays a single occurrence: the no_sandbox disclaimer is pinned above
    # as the text AFTER it, and a second mention would split that assertion.
    assert hint.count("F-834") == 1


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


# ---------------------------------------------------------------------------
# Stage 1: the master race — the loser must land somewhere, not nowhere
# ---------------------------------------------------------------------------


async def test_concurrent_unnamed_selections_all_still_pick_master(tmp_session_root):
    """Characterization, NOT the defect: the master branch is a LIVENESS check.

    ``_dir_unavailable``'s own docstring says ``_profile_has_running_browser`` is
    a liveness check and never a reservation, and every concurrent spawn is
    pre-launch when it asks — so three-of-three ``master`` is the reading the
    code is designed to give. Reserving master is deliberately NOT the fix
    (F-834 "Not fixed here"): the reservation would need a matching release on
    the close path, and a leaked one would silently force every later spawn to
    clone. What has to change is what happens to the two callers that then lose
    Chrome's own profile singleton.
    """
    selections = await asyncio.gather(
        *(clone_storage.resolve_profile_selection(None) for _ in range(3))
    )
    assert [s["profile_role"] for s in selections] == ["default"] * 3
    assert {s["user_data_dir"] for s in selections} == {
        str(clone_storage.master_profile_dir())
    }


@pytest.fixture
def master_race(monkeypatch):
    """Chrome's process singleton, modelled: master is FREE until someone takes
    it, and held by a live process from then on.

    The two states have to be separable, because the race runs through both: at
    selection time master is free for every concurrent caller (that is stage 1),
    and it is only during the launch that the winner takes it. Patching this
    two-line adapter rather than writing a `SingletonLock` keeps the test true on
    Windows, where Chrome writes no `Singleton*` at all and `profile_lock` has
    only the process-table witness (F-871).
    """
    master = clone_storage.master_profile_dir()
    real = clone_storage._profile_hold
    taken: list[bool] = []

    def _hold(profile_dir):
        if taken and Path(profile_dir) == master:
            return profile_lock.Hold(os.getpid(), "test: the race winner holds it")
        return real(profile_dir)

    monkeypatch.setattr(clone_storage, "_profile_hold", _hold)
    return SimpleNamespace(dir=master, take=lambda: taken.append(True))


@pytest.fixture
def master_taken(master_race):
    """The race already lost: a sibling holds master before this caller asks."""
    master_race.take()
    return master_race.dir


def _master_selection():
    """A selection on the SHARED profile, in the role word the resolver issues
    — ``default`` since F-896, read rather than re-spelled."""
    return {
        "user_data_dir": str(clone_storage.master_profile_dir()),
        "profile_role": profile_seed.DEFAULT_SESSION,
    }


async def test_a_master_loser_retries_onto_a_reserved_clone(
    tmp_session_root, master_taken
):
    """THE stage-1 defect: the fallback answered ``None`` for every non-clone
    role, so the loser of the master race got no second attempt at all — the
    caller saw ``Failed to connect to browser`` plus nodriver's root/no_sandbox
    advice, which F-834's own hint already disclaims.

    A sibling HOLDS master here, which is what makes a clone the right answer:
    retrying a directory another Chrome owns would fail the same way again.

    SOFT GOLDEN UPDATED for F-914 — the clone and its reservation are unchanged
    and the ``driven`` witness is new. The sibling in a real stage-1 race is a
    spawn of THIS backend, so the clone it lands on gets the shared session's
    live jar handed over and is no longer the logged-out copy of a frozen seed
    F-914 is about. A loser whose holder is NOT ours is the node below.
    """
    fallback = await clone_storage._fallback_profile_selection(
        _master_selection(), 0, driven=_driving(master_taken)
    )

    assert fallback is not None, "a master-role loser got no retry at all (F-834)"
    assert fallback["profile_role"] == "clone"
    clone = Path(fallback["user_data_dir"])
    assert clone != master_taken
    assert clone_storage._is_relative_to(clone, clone_storage.clone_root_dir())
    assert clone_storage._clone_dir_is_protected(clone), (
        "the retry clone must be RESERVED — being reserved is the whole reason a "
        "clone is a safe place for a master loser to land"
    )
    assert fallback[clone_storage.LIVE_SEED_KEY] == str(master_taken)


async def test_a_master_loser_is_refused_when_the_holder_is_not_ours(
    tmp_session_root, master_taken
):
    """F-914 reached through F-834's door, which is the door it would otherwise
    have reappeared through.

    This function was widened to all three roles by F-834, so left alone it
    answers a held shared session with exactly the snapshot clone the resolver
    now refuses — one spawn failure later, and with no pin watching it. What
    that costs is named rather than hidden: a spawn that lost the profile
    singleton to a Chrome this backend does not drive now FAILS, where 2.1.12
    handed back a working browser on a seed frozen at the last clean close. The
    owner's ruling is that the second one is the harm.
    """
    with pytest.raises(ToolError, match=profile_seed.DEFAULT_SESSION):
        await clone_storage._fallback_profile_selection(_master_selection(), 0)

    assert not clone_storage._PROTECTED_CLONE_DIRS, "a refusal reserved a directory"


async def test_a_master_attempt_retries_master_when_no_sibling_took_it(
    tmp_session_root,
):
    """The other half of the master rule: nobody else holds it, so master is
    still the best profile here and a clone would be a needless copy of it.

    This is the shape the macOS/ARM64 cell measured (run 35150887345 attempt 2):
    Chrome had STARTED and was killed by the failed attempt's F-860 reap, so the
    directory is free again by the time this is asked.
    """
    fallback = await clone_storage._fallback_profile_selection(_master_selection(), 0)

    assert fallback == _master_selection(), (
        "a master nobody took must be retried, not cloned away from"
    )


async def test_the_master_hold_is_asked_about_the_directory_the_attempt_drove(
    tmp_session_root, monkeypatch
):
    """The hold is read off the SELECTION, never re-derived from config.

    The two agree for every selection the resolver issues, so this costs
    nothing; asking the selection is what keeps the answer about the directory
    this attempt actually drove rather than the one config says master is now.
    """
    asked: list[Path] = []
    monkeypatch.setattr(
        clone_storage, "_profile_hold", lambda d: asked.append(Path(d)) and None
    )
    drove = clone_storage.clone_root_dir() / "somewhere-else"

    await clone_storage._fallback_profile_selection(
        {"user_data_dir": str(drove), "profile_role": "default"}, 0
    )

    assert asked == [drove]


async def test_two_master_losers_land_in_distinct_dirs(tmp_session_root, master_taken):
    """Both losers of one master race retry at once; layer 1's per-attempt token
    has to hold for this new entry point too.

    ``driven`` for F-914's reason, stated on the node above: the holder of a
    stage-1 race is a sibling spawn of this backend, and a retry onto a held
    shared session we do NOT drive is a refusal now, not a clone.
    """
    driven = _driving(master_taken)
    first = await clone_storage._fallback_profile_selection(
        _master_selection(), 0, driven=driven
    )
    second = await clone_storage._fallback_profile_selection(
        _master_selection(), 0, driven=driven
    )

    assert first["user_data_dir"] != second["user_data_dir"]


async def test_a_named_profile_retries_itself_and_is_never_walked_or_cloned(
    tmp_session_root,
):
    """A NAMED profile retries the SAME directory, whatever holds it.

    The caller asked for THAT profile's cookies and logins. Swapping it for a
    clone is an identity change and so is walking it to `<name>-2`; the only
    place either may happen is `resolve_profile_selection`, where F-871 reports
    it. A role the resolver never issues still gets no retry.
    """
    named = {
        "user_data_dir": str(clone_storage.clone_root_dir() / "fleet-tabswitch"),
        "profile_role": "explicit",
        "clone_source": None,
    }
    for attempt in (0, 2):
        again = await clone_storage._fallback_profile_selection(named, attempt)
        assert again == named, f"attempt {attempt} moved a named profile"

    for role in ({}, {"profile_role": "nonsense"}):
        assert await clone_storage._fallback_profile_selection(role, 0) is None
        assert await clone_storage._fallback_profile_selection(role, 2) is None


class _MasterRefusingManager(FakeBrowserManager):
    """Chrome's profile singleton, modelled: a launch against the MASTER
    directory fails, every other directory works.

    That is the stage-1 race as the losing caller experiences it — a second
    Chrome against a user-data-dir another Chrome already holds hands its
    command line to the incumbent and exits, which nodriver reports as
    ``Failed to connect to browser``.
    """

    def __init__(self, race, spawn_instance):
        super().__init__(
            spawn_instance=spawn_instance,
            spawn_diagnostics={},
            **_sibling_driving(race.dir),
        )
        self._race = race
        self.dirs: list[str] = []

    async def spawn_browser(self, options):
        self.dirs.append(options.user_data_dir)
        if options.user_data_dir == str(self._race.dir):
            # The winner has it from this moment on — which is exactly what makes
            # a clone, not master again, the right retry for this caller.
            self._race.take()
            raise RuntimeError(INNER_FAILURE)
        return await super().spawn_browser(options)


async def test_a_spawn_deciding_its_retry_is_not_counted_in_flight(
    tmp_session_root, doomed_manager, patched_server, call_tool, monkeypatch
):
    """Why the retry does not wait for the sibling wave, measured not assumed.

    `_spawns_in_flight` is incremented inside `BrowserManager.spawn_browser` and
    decremented in its `finally`, so a spawn sitting in the tool body's except
    handler — exactly where a "wait until the others settle" gate would go — is
    NOT counted. Every member of a failing wave would therefore read a number
    that excludes every other waiter, reach the same verdict at the same moment,
    and be released together: the gate cannot see the herd it exists to break
    up. It would buy nothing and cost every failing spawn its own latency, which
    is why the fallback returns immediately and the retry budget is the bound.
    """
    seen: list[int] = []
    real = clone_storage._fallback_profile_selection

    async def _sampling(previous, attempt, **kwargs):
        # ``**kwargs`` rather than a named ``driven``: this double exists to
        # sample one counter and must not restate the signature it wraps, or it
        # goes stale the next time a witness is threaded through (F-914 added
        # one and this node failed on the arity, not on its subject).
        seen.append(doomed_manager._spawns_in_flight)
        return await real(previous, attempt, **kwargs)

    monkeypatch.setattr(clone_storage, "_fallback_profile_selection", _sampling)
    srv = patched_server(browser_manager=doomed_manager)

    with pytest.raises(Exception):
        await call_tool(srv, "spawn_browser", headless=True, sandbox=False)

    assert seen, "the spawn never reached a retry decision at all"
    assert set(seen) == {0}, (
        f"in-flight counts at retry-decision time were {seen}; a wait gated on "
        "this counter cannot see its own waiters"
    )


class _AlwaysFailsManager(FakeBrowserManager):
    """Every attempt fails, so the loop runs its full budget and then raises."""

    def __init__(self, race):
        super().__init__(
            spawn_instance=None, spawn_diagnostics={}, **_sibling_driving(race.dir)
        )
        self._race = race
        self.dirs: list[str] = []

    async def spawn_browser(self, options):
        self.dirs.append(options.user_data_dir)
        if options.user_data_dir == str(self._race.dir):
            self._race.take()
        raise RuntimeError(INNER_FAILURE)


async def test_a_spawn_that_fails_every_attempt_leaks_no_protected_clone(
    tmp_session_root, call_tool, patched_server, master_race
):
    """The LAST attempt must not ask for a fallback it can never use.

    The retry budget is exhausted by then and the loop's `else` raises, so that
    selection is never driven. Asking for it anyway costs a whole profile-tree
    copy and leaves the directory `_protect_clone_dir`-ed for the life of the
    process: the two release paths are this attempt's own failure handler, which
    has already run, and `close_instance`, which never will. Stage 1 is what
    routes a MASTER failure down this path at all — before it, a master-role
    spawn that failed made zero clones and leaked nothing.
    """
    manager = _AlwaysFailsManager(master_race)
    srv = patched_server(browser_manager=manager)
    clone_root = clone_storage.clone_root_dir()
    assert not clone_storage._PROTECTED_CLONE_DIRS, "the fixture starts clean"

    with pytest.raises(Exception):
        await call_tool(srv, "spawn_browser", headless=True, sandbox=False)

    assert len(manager.dirs) == 3, f"attempted dirs: {manager.dirs}"
    assert manager.dirs[0] == str(master_race.dir)
    assert not clone_storage._PROTECTED_CLONE_DIRS, (
        "a clone dir stayed protected for a launch that never happened — nothing "
        "will ever release it"
    )
    on_disk = [d for d in clone_root.iterdir() if d.is_dir() and d.name != ".trash"]
    assert len(on_disk) == 2, (
        f"{len(on_disk)} profile trees copied for 2 clone attempts: {on_disk}"
    )


class _FailsOnceManager(FakeBrowserManager):
    """nodriver's connect failure, once, whatever directory it is given.

    The macOS/ARM64 shape (run 35150887345 attempt 2): Chrome STARTED on the
    requested directory but had not opened its DevTools port inside nodriver
    0.47's fixed connect deadline — 0.25 s plus five 0.5 s naps, a constant that
    does not scale with load — so on a 3-vCPU runner under five concurrent
    launches the first attempt loses a race with a stopwatch, not with a sibling
    for a directory. The failed attempt's F-860 reap then kills that Chrome, so
    the very same directory is free for the retry.
    """

    def __init__(self, spawn_instance):
        super().__init__(spawn_instance=spawn_instance, spawn_diagnostics={})
        self.dirs: list[str] = []

    async def spawn_browser(self, options):
        self.dirs.append(options.user_data_dir)
        if len(self.dirs) == 1:
            raise RuntimeError(INNER_FAILURE)
        return await super().spawn_browser(options)


async def test_a_named_profile_spawn_retries_the_very_same_directory(
    tmp_session_root, call_tool, patched_server
):
    """The named half, end to end: the retry drives the SAME directory.

    Never `<name>-2` and never a clone — the caller named `fleet-tabswitch`
    because they want that profile's cookies, and a spawn that quietly hands
    back a different one has answered a different question.
    """
    manager = _FailsOnceManager(
        SimpleNamespace(
            instance_id="i1",
            state="active",
            headless=True,
            viewport={"width": 1920, "height": 1080},
        )
    )
    srv = patched_server(browser_manager=manager)

    result = await call_tool(
        srv,
        "spawn_browser",
        headless=True,
        sandbox=False,
        user_data_dir="fleet-tabswitch",
    )

    assert result["state"] == "active"
    assert len(manager.dirs) == 2, f"attempted dirs: {manager.dirs}"
    assert manager.dirs[0] == manager.dirs[1], "the retry moved to another directory"
    assert Path(manager.dirs[0]).name == "fleet-tabswitch"
    selection = result["spawn_diagnostics"]["profile_selection"]
    assert selection["profile_role"] == "explicit"
    assert "walked_to" not in selection, "a named profile must never be walked here"
    assert selection["spawn_retries"], "the swallowed first failure must be reported"


async def test_a_spawn_that_loses_master_succeeds_on_its_second_attempt(
    tmp_session_root, call_tool, patched_server, master_race
):
    """End to end through the tool body: the loser gets a SECOND attempt, on a
    distinct reserved clone, and the spawn succeeds instead of raising.

    Master is free when this caller selects it — that is stage 1 — and held by
    the time the fallback is asked, which is why the answer is a clone here and
    master itself in `test_a_master_attempt_retries_master_when_no_sibling_took_it`.
    """
    master = master_race.dir
    manager = _MasterRefusingManager(
        master_race,
        SimpleNamespace(
            instance_id="i1",
            state="active",
            headless=True,
            viewport={"width": 1920, "height": 1080},
        ),
    )
    srv = patched_server(browser_manager=manager)

    result = await call_tool(srv, "spawn_browser", headless=True, sandbox=False)

    assert result["state"] == "active"
    assert len(manager.dirs) == 2, f"attempted dirs: {manager.dirs}"
    assert manager.dirs[0] == str(master)
    clone = Path(manager.dirs[1])
    assert clone != master
    assert clone_storage._is_relative_to(clone, clone_storage.clone_root_dir())
    assert clone_storage._clone_dir_is_protected(clone)
    selection = result["spawn_diagnostics"]["profile_selection"]
    assert selection["profile_role"] == "clone"
    assert selection["user_data_dir"] == str(clone)
    assert selection["spawn_retries"], "the swallowed first failure must be reported"
