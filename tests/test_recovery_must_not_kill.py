"""Pins for F-916 / F-917 / F-918 — one sentence, read three ways.

**An answer we could not establish must resolve toward NOT KILLING.** Startup
orphan recovery runs on every backend cold start and the thing it is deciding
about may be a human's logged-in Chrome, so each of the three places this lane
touches had the same shape: a witness that could not be read, and a fall-through
to the kill.

* **F-916** — the CLASSIFICATION. ``browser_reattach._adoptable_entry`` answered
  a bare ``None`` both for "this is not adoptable" and for "we could not
  establish whether it is", so a persistent entry we merely could not reach fell
  outside ``Classified.spare`` and was reaped.
* **F-917** — the KILL SET. The reap is matched by ``user_data_dir`` while the
  spare is matched by ``instance_id``, so one stale entry's reap killed a browser
  another entry had just protected.
* **F-918** — the ACT. ``_kill_process_by_pid`` logged "Could not verify process"
  from a blanket ``except`` and then terminated the pid anyway.
* **F-922** — the SCOPE, and the owner's ruling on F-917's residual. The kill set
  was built by scanning the ``user_data_dir`` whatever KIND of profile it was, so
  a Chrome the owner started by hand on one of their own named sessions — in no
  record entry at all, and therefore reachable by no spare — was killed by a
  stale entry's reap. The directory scan is for DISPOSABLE profiles only now.

Hermetic throughout: the record is a ``tmp_path`` file, both liveness witnesses
are injected, psutil is patched, and **nothing here may terminate a real
process** — every pin that reaches the kill path asserts on a recorded call, not
on a dead pid. ``_sweep_orphaned_temp_profiles`` is patched out wherever
``recover_orphans`` is driven: its glob reaches the real ``%TEMP%``, which on
this machine holds other agents' Chrome profiles.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import browser_cmdline, browser_reattach
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup

DEAD_OWNER = 9001
LIVE_CHROME = 7777
DEAD_CHROME = 7778
# A browser in NO record entry at all: the one the owner started by hand.
OWNERS_CHROME = 5555
# A browser an F-916 SPARE protects: recorded, but not in `Classified.adoptable`,
# so `browser_reattach.run`'s `candidate_pids` never names it (F-922 / B1).
SPARED_SIBLING = 6666
PORT = 51234


def _entry(
    *,
    pid=LIVE_CHROME,
    owner_pid=DEAD_OWNER,
    user_data_dir=r"C:\profiles\seller-central",
    auto_clone=False,
    uses_custom_data_dir=True,
    cdp_port=PORT,
    create_time=1700000000.0,
):
    """One recorded browser, in the shape ``normalize_entries`` yields.

    ``cdp_port=None`` is the 2.1.8/2.1.9 record shape — the population the audit
    names as carrying today's stranded logins.
    """
    return {
        "pid": pid,
        "create_time": create_time,
        "user_data_dir": user_data_dir,
        "uses_custom_data_dir": uses_custom_data_dir,
        "auto_clone": auto_clone,
        "cdp_port": cdp_port,
        "timestamp": 0,
        "owner_pid": owner_pid,
        "owner_create_time": 1699999000.0,
    }


def _owner_alive(_pid, _create_time):
    """No backend of ours is alive — every entry here is an orphan to recover."""
    return False


def _browser_alive(pid, _create_time):
    """Only LIVE_CHROME is still the Chrome its entry recorded."""
    return pid == LIVE_CHROME


def _recorded_browser_alive(_cleanup, pid, _create_time):
    """``browser_reattach.recorded_browser_alive``'s shape: cleanup comes first."""
    return pid == LIVE_CHROME


def _cleanup(tmp_path: Path) -> ProcessCleanup:
    """A ProcessCleanup whose record is in tmp_path, built without __init__."""
    pc = ProcessCleanup.__new__(ProcessCleanup)
    pc.pid_file = tmp_path / "browser_pids.json"
    pc.tracked_pids = set()
    pc.browser_processes = {}
    pc.orphan_profile_max_age_seconds = 0
    pc._init_time = 1700000100.0
    return pc


def _seed(path: Path, entries: dict) -> None:
    path.write_text(json.dumps({"browser_processes": entries, "timestamp": 0}))


def _read(path: Path) -> dict:
    return json.loads(path.read_text())["browser_processes"]


def _unreachable(monkeypatch) -> None:
    """No CDP endpoint is recoverable for any pid — the F-916 population.

    Both live rungs of the endpoint ladder are closed: the command line answers
    nothing (what a psutil ``AccessDenied`` on ``cmdline()`` leaves), and the
    ``DevToolsActivePort`` rung reads a directory with no such file in it.
    """
    monkeypatch.setattr(browser_cmdline, "debug_port", lambda _pid, _expect=None: None)


# ---------------------------------------------------------------------------
# F-916 — an entry we could not CLASSIFY is reaped
# ---------------------------------------------------------------------------


class TestUnclassifiableEntryIsSpared:
    """A persistent entry that is merely UNREACHABLE must land in ``.spare``.

    ``.spare`` is what startup recovery skips. The three conditions pinned here
    are the three ways ``_adoptable_entry`` can fail to ESTABLISH an answer for
    an entry it has already agreed is persistent; each used to answer the same
    bare ``None`` a disposable auto-clone answers, which is the one shape that
    means "reap me".
    """

    def test_no_recoverable_endpoint_is_spared(self, tmp_path, monkeypatch):
        """F-916's measured population: a 2.1.8/2.1.9 record with no ``cdp_port``.

        The browser is ALIVE and on a persistent profile — it is exactly the
        stranded login F-888 exists to hand over — and the only thing we could
        not do is find a door into it.
        """
        _unreachable(monkeypatch)
        entry = _entry(cdp_port=None, user_data_dir=str(tmp_path / "seller-central"))

        classified = browser_reattach.adoptable(
            {"i-1": entry}, owner_alive=_owner_alive, browser_alive=_browser_alive
        )

        assert "i-1" not in classified.adoptable, "unreachable is not adoptable"
        assert "i-1" in classified.spare, "but it must not be reaped either"

    def test_missing_create_time_is_spared(self, tmp_path, monkeypatch):
        """An entry too old to carry a ``create_time`` cannot prove its pid.

        Adoption refuses it deliberately (taking over a stranger's chrome.exe on
        a recycled pid is worse than not adopting), but "we cannot prove whose
        pid this is" is not a licence to END it.
        """
        _unreachable(monkeypatch)
        entry = _entry(create_time=None, user_data_dir=str(tmp_path / "seller-central"))

        classified = browser_reattach.adoptable(
            {"i-1": entry}, owner_alive=_owner_alive, browser_alive=_browser_alive
        )

        assert "i-1" not in classified.adoptable
        assert "i-1" in classified.spare

    @pytest.mark.parametrize(
        "profile_dir", [None, "", 17], ids=["absent", "empty", "not-a-str"]
    )
    def test_unreadable_entry_shape_is_spared(self, tmp_path, monkeypatch, profile_dir):
        """A persistent entry whose DIRECTORY cannot be read tells us nothing.

        The three shapes here are the ones the real reader can actually deliver
        to this guard: all three survive `normalize_entries` as
        `user_data_dir=None`. This pin used to set `pid = "not-a-pid"` instead,
        which reaches the same guard's other arm only when `adoptable` is called
        with a hand-built dict — `normalize_entries` DROPS an entry whose pid is
        not an int, so no record on disk can produce it. Pinning an arm the
        reader cannot deliver measures the test's own fixture.
        """
        _unreachable(monkeypatch)
        entry = _entry(user_data_dir=str(tmp_path / "seller-central"))
        entry["user_data_dir"] = profile_dir

        classified = browser_reattach.adoptable(
            {"i-1": entry}, owner_alive=_owner_alive, browser_alive=_browser_alive
        )

        assert "i-1" not in classified.adoptable
        assert "i-1" in classified.spare

    def test_a_disposable_auto_clone_is_still_reaped(self, tmp_path, monkeypatch):
        """The other direction, and it is what keeps the record from growing.

        "Not persistent" is an answer we DID establish, so it stays outside
        ``.spare``. Without this the fix would spare everything and startup
        recovery would never reap anything again.
        """
        _unreachable(monkeypatch)
        entry = _entry(
            auto_clone=True,
            uses_custom_data_dir=True,
            user_data_dir=str(tmp_path / "uc_throwaway"),
        )

        classified = browser_reattach.adoptable(
            {"i-1": entry}, owner_alive=_owner_alive, browser_alive=_browser_alive
        )

        assert "i-1" not in classified.spare

    def test_a_dead_browser_is_still_reaped(self, tmp_path, monkeypatch):
        """So is an entry whose Chrome is provably gone — also an ESTABLISHED
        answer, and the one that lets a dead entry leave the record at all."""
        _unreachable(monkeypatch)
        entry = _entry(pid=DEAD_CHROME, user_data_dir=str(tmp_path / "seller-central"))

        classified = browser_reattach.adoptable(
            {"i-1": entry}, owner_alive=_owner_alive, browser_alive=_browser_alive
        )

        assert "i-1" not in classified.spare

    def test_recovery_does_not_kill_the_unreachable_browser(
        self, tmp_path, monkeypatch
    ):
        """The harm itself, at the one call site: ``recover_orphans``.

        Not a classification assertion — the browser's pid must not reach the
        kill path on a plain backend cold start.
        """
        _unreachable(monkeypatch)
        profile = tmp_path / "seller-central"
        profile.mkdir()
        pc = _cleanup(tmp_path)
        _seed(pc.pid_file, {"i-1": _entry(cdp_port=None, user_data_dir=str(profile))})

        killed: list[int] = []
        with (
            patch.object(pc, "_owner_backend_alive", _owner_alive),
            patch.object(
                browser_reattach, "recorded_browser_alive", _recorded_browser_alive
            ),
            patch.object(pc, "_sweep_orphaned_temp_profiles"),
            patch.object(
                pc, "_get_browser_pids_for_profile", return_value={LIVE_CHROME}
            ),
            patch.object(
                pc, "_kill_process_by_pid", lambda pid, iid: killed.append(pid) or True
            ),
        ):
            pc.recover_orphans()

        assert killed == [], "a browser we could not reach must be left running"
        assert "i-1" in _read(pc.pid_file), "and left recorded, or nothing names it"


# ---------------------------------------------------------------------------
# F-917 — the reap is DIRECTORY-matched while the spare is INSTANCE-ID-matched
# ---------------------------------------------------------------------------


class TestReapDoesNotCrossTheSpare:
    """One stale entry's reap must not kill a browser another entry protected.

    **F-922 narrowed where this can happen at all.** The subtraction guards the
    DIRECTORY-derived kill set, and since the owner's ruling that set is only
    built for DISPOSABLE profiles — so the two unit pins below run on an
    auto-clone entry, where two entries sharing one directory is still
    reachable (a re-track, a record carried across versions). Run on a
    persistent entry they would pass VACUOUSLY: measured, the answer is the
    same with and without ``protected_pids``, because no directory scan ran.
    Each therefore pins BOTH directions.
    """

    def test_stale_entry_does_not_kill_a_spared_siblings_browser(self, tmp_path):
        """Two entries, ONE directory — the shared profile, i.e. the master.

        ``i-live`` is adoptable and therefore spared. ``i-stale`` names a Chrome
        that is gone, so it is reaped — and its kill set is built by scanning the
        DIRECTORY, which finds ``i-live``'s browser. Before F-917 the reap of the
        dead entry ended the live one.

        The recovery start-time fence is deliberately let THROUGH (the patched
        ``create_time`` predates ``_init_time``): that fence is what hides this
        on a machine where the pid happens to be absent, and a pin it satisfies
        would read green without the fix.

        Since F-922 this shared profile is PERSISTENT, so the scope rule stops
        it one layer earlier and this pin no longer isolates the subtraction —
        the two unit pins below do that on a disposable entry. It is kept
        because it is the harm as REPORTED, and because two guards on the
        owner's logged-in Chrome is the right number.
        """
        profile = tmp_path / "master"
        profile.mkdir()
        pc = _cleanup(tmp_path)
        _seed(
            pc.pid_file,
            {
                "i-live": _entry(pid=LIVE_CHROME, user_data_dir=str(profile)),
                "i-stale": _entry(pid=DEAD_CHROME, user_data_dir=str(profile)),
            },
        )
        before_init = MagicMock()
        before_init.create_time.return_value = pc._init_time - 50.0

        killed: list[int] = []
        with (
            patch.object(pc, "_owner_backend_alive", _owner_alive),
            patch.object(
                browser_reattach, "recorded_browser_alive", _recorded_browser_alive
            ),
            patch.object(pc, "_sweep_orphaned_temp_profiles"),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.Process",
                return_value=before_init,
            ),
            # The directory scan is what the defect is about: it answers with
            # every browser on that profile, the spared one included.
            patch.object(
                pc, "_get_browser_pids_for_profile", return_value={LIVE_CHROME}
            ),
            patch.object(
                pc, "_kill_process_by_pid", lambda pid, iid: killed.append(pid) or True
            ),
        ):
            pc.recover_orphans()

        assert LIVE_CHROME not in killed, (
            "the spared entry's browser was killed by its stale sibling's reap"
        )
        assert "i-live" in _read(pc.pid_file), "and the spared entry stays recorded"

    def test_protected_pids_are_filtered_from_every_kill_path(self, tmp_path):
        """The filter sits at the ONE place the kill set is finally spent.

        Both ways a pid can get into that set — the directory scan and the
        recorded fallback pid — pass through it, so neither can grow a second
        answer to "is this one protected".

        The counter-assertion is what keeps this honest: the SAME input with an
        empty ``protected_pids`` must kill, or the pin is measuring the absence
        of a directory scan rather than the subtraction.
        """
        pc = _cleanup(tmp_path)
        metadata = _entry(
            pid=LIVE_CHROME,
            auto_clone=True,
            user_data_dir=str(tmp_path / "uc_shared"),
        )

        def reap(protected):
            killed: list[int] = []
            with (
                patch.object(
                    pc, "_get_browser_pids_for_profile", return_value={LIVE_CHROME}
                ),
                patch.object(
                    pc,
                    "_kill_process_by_pid",
                    lambda pid, iid: killed.append(pid) or True,
                ),
            ):
                pc._kill_processes_for_metadata(
                    "i-stale", metadata, recovery=False, protected_pids=protected
                )
            return killed

        assert reap(frozenset({LIVE_CHROME})) == []
        assert reap(frozenset()) == [LIVE_CHROME], "otherwise the pin is vacuous"

    def test_an_unprotected_pid_on_the_directory_is_still_killed(self, tmp_path):
        """The filter is a subtraction, not a switch — everything else still goes."""
        pc = _cleanup(tmp_path)
        metadata = _entry(
            pid=DEAD_CHROME,
            auto_clone=True,
            user_data_dir=str(tmp_path / "uc_shared"),
        )

        killed: list[int] = []
        with (
            patch.object(
                pc,
                "_get_browser_pids_for_profile",
                return_value={LIVE_CHROME, DEAD_CHROME},
            ),
            patch.object(
                pc, "_kill_process_by_pid", lambda pid, iid: killed.append(pid) or True
            ),
        ):
            pc._kill_processes_for_metadata(
                "i-stale",
                metadata,
                recovery=False,
                protected_pids=frozenset({LIVE_CHROME}),
            )

        assert killed == [DEAD_CHROME]


# ---------------------------------------------------------------------------
# F-918 — an UNVERIFIABLE process is terminated
# ---------------------------------------------------------------------------


class TestUnverifiableProcessIsNotKilled:
    """``_kill_process_by_pid`` must refuse a pid it could not identify."""

    @staticmethod
    def _proc(*, name_raises=None, name="chrome.exe"):
        proc = MagicMock()
        if name_raises is not None:
            proc.name.side_effect = name_raises
        else:
            proc.name.return_value = name
        return proc

    @pytest.mark.parametrize(
        "unreadable",
        [
            psutil.AccessDenied(1234),
            OSError("handle is invalid"),
        ],
        ids=["access-denied", "oserror"],
    )
    def test_an_unreadable_name_refuses_the_kill(self, tmp_path, unreadable):
        """Windows answers ``AccessDenied`` on ``.name()`` for a process we may
        not open. Before F-918 that log line was followed by ``terminate()``."""
        pc = _cleanup(tmp_path)
        proc = self._proc(name_raises=unreadable)

        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.pid_exists",
                return_value=True,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.Process",
                return_value=proc,
            ),
        ):
            killed = pc._kill_process_by_pid(1234, "i-unknown")

        assert killed is False, "an unidentified pid must not count as reaped"
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_a_zombie_reads_as_gone_rather_than_as_unreadable(self, tmp_path):
        """A zombie is an EXITED process, not an unreadable one.

        ``psutil.ZombieProcess`` subclasses ``NoSuchProcess``, so it lands on
        the "gone" rung — which is the right answer and not an accident:
        ``process_exit`` carries the measured argument that such a process has
        already closed its files and committed its cookie store. Nothing to
        kill, and the reap counts as done.

        This one is UNCHANGED by F-918 and is pinned because it looks like it
        should have changed: measured on the pre-fix source, a zombie already
        reached ``except psutil.NoSuchProcess`` — which is listed BEFORE the
        blanket handler — and answered True without terminating. Only
        ``AccessDenied`` and a plain ``OSError`` fell through to the kill.
        """
        pc = _cleanup(tmp_path)
        proc = self._proc(name_raises=psutil.ZombieProcess(1234))

        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.pid_exists",
                return_value=True,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.Process",
                return_value=proc,
            ),
        ):
            killed = pc._kill_process_by_pid(1234, "i-zombie")

        assert killed is True
        proc.terminate.assert_not_called()

    def test_a_verified_browser_is_still_killed(self, tmp_path):
        """The refusal is about the UNREADABLE answer only."""
        pc = _cleanup(tmp_path)
        proc = self._proc(name="chrome.exe")

        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.pid_exists",
                return_value=True,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.Process",
                return_value=proc,
            ),
        ):
            killed = pc._kill_process_by_pid(1234, "i-known")

        assert killed is True
        proc.terminate.assert_called_once()

    def test_a_non_browser_name_is_still_refused(self, tmp_path):
        """And the guard that already worked keeps working."""
        pc = _cleanup(tmp_path)
        proc = self._proc(name="explorer.exe")

        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.pid_exists",
                return_value=True,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.Process",
                return_value=proc,
            ),
        ):
            killed = pc._kill_process_by_pid(1234, "i-stranger")

        assert killed is False
        proc.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# F-922 — a named profile is reaped by the RECORD, never by its DIRECTORY
# ---------------------------------------------------------------------------


def _reap(pc, metadata, on_directory, *, recovery=True):
    """Drive one reap; answer the pids the kill path actually received.

    The recovery start-time fence is deliberately let THROUGH — every patched
    ``create_time`` predates ``_init_time`` — so what these pins measure is the
    kill SET itself and not a fence that happens to narrow it on some machines.
    """
    before = MagicMock()
    before.create_time.return_value = pc._init_time - 50.0
    killed: list[int] = []
    with (
        patch.object(
            pc, "_get_browser_pids_for_profile", return_value=set(on_directory)
        ),
        patch(
            "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.Process",
            return_value=before,
        ),
        patch.object(
            pc, "_kill_process_by_pid", lambda pid, iid: killed.append(pid) or True
        ),
    ):
        pc._kill_processes_for_metadata("i-x", metadata, recovery=recovery)
    return sorted(killed)


class TestPersistentProfileIsReapedByRecordOnly:
    """The owner's ruling: the DIRECTORY scan is for disposable profiles only.

    An auto-clone directory is ours BY CONSTRUCTION — a human would never open
    one by hand — so anything running on it is ours to reap. A NAMED profile is
    exactly what a human does open by hand; that is what ``session=`` is for,
    and since F-888 a persistent-profile browser is meant to outlive its
    backend. The safe direction therefore differs by profile KIND, which is why
    one uniform rule was the wrong shape.

    The predicate is ``browser_pid_registry.on_persistent_profile`` read the
    other way round — the same one F-888 and the profile-deletion guard already
    ask. There is deliberately no second notion of "ours".
    """

    def test_a_hand_started_chrome_on_a_named_profile_is_not_killed(self, tmp_path):
        """The reported harm. The owner's Chrome is in NO record entry, so no
        spare can reach it (F-917 protects recorded pids); only narrowing the
        SCOPE can."""
        stale = _entry(
            pid=DEAD_CHROME,
            user_data_dir=str(tmp_path / "sessions" / "work"),
            auto_clone=False,
        )

        assert _reap(_cleanup(tmp_path), stale, [OWNERS_CHROME]) == []

    def test_the_close_path_is_narrowed_the_same_way(self, tmp_path):
        """``kill_browser_process`` reaches the same function with
        ``recovery=False``, and a named profile is a named profile whichever
        caller arrived — keying the rule on the CALLER would be a second answer
        to "may we kill by directory"."""
        stale = _entry(
            pid=DEAD_CHROME,
            user_data_dir=str(tmp_path / "sessions" / "work"),
            auto_clone=False,
        )

        assert _reap(_cleanup(tmp_path), stale, [OWNERS_CHROME], recovery=False) == []

    def test_a_named_profile_reap_no_longer_takes_a_bystander_with_it(self, tmp_path):
        """Even a JUSTIFIED reap over-reached: the entry's own browser is killed
        and the owner's is killed beside it, because both are on the
        directory."""
        pc = _cleanup(tmp_path)
        live = _entry(
            pid=LIVE_CHROME,
            create_time=pc._init_time - 50.0,
            user_data_dir=str(tmp_path / "sessions" / "work"),
            auto_clone=False,
        )

        assert _reap(pc, live, [LIVE_CHROME, OWNERS_CHROME]) == [LIVE_CHROME]

    def test_a_named_profiles_own_recorded_browser_is_still_reaped(self, tmp_path):
        """The control that keeps this a NARROWING and not a stand-down: what
        the record names is still ended, identity-checked as it always was."""
        pc = _cleanup(tmp_path)
        live = _entry(
            pid=LIVE_CHROME,
            create_time=pc._init_time - 50.0,
            user_data_dir=str(tmp_path / "sessions" / "work"),
            auto_clone=False,
        )

        assert _reap(pc, live, [LIVE_CHROME]) == [LIVE_CHROME]

    def test_a_disposable_auto_clone_still_reaps_its_whole_directory(self, tmp_path):
        """The other half of the ruling, and the reason it is not "record-only
        everywhere": an orphaned clone browser the record lost would otherwise
        accumulate forever on a directory nothing else will ever claim."""
        clone = _entry(
            pid=DEAD_CHROME,
            user_data_dir=str(tmp_path / "sessions" / "uc_throwaway"),
            auto_clone=True,
        )

        assert _reap(_cleanup(tmp_path), clone, [OWNERS_CHROME]) == [OWNERS_CHROME]

    def test_an_entry_missing_both_keys_reads_as_disposable(self, tmp_path):
        """Stated rather than left to be discovered. ``on_persistent_profile``
        answers False for an entry carrying NEITHER key, so a hand-edited or
        cross-version record still gets the directory scan. That is the audit's
        A1 shape and it is unchanged here — this finding narrowed the scope, it
        did not fix what decides the kind."""
        legacy = {
            "pid": DEAD_CHROME,
            "create_time": 1700000000.0,
            "user_data_dir": str(tmp_path / "sessions" / "work"),
            "timestamp": 0,
        }

        assert _reap(_cleanup(tmp_path), legacy, [OWNERS_CHROME]) == [OWNERS_CHROME]


# ---------------------------------------------------------------------------
# F-922 / B1 — the SECOND door: a failed adoption's own fallback reap
# ---------------------------------------------------------------------------


def _failed_adoption_metadata(pid, user_data_dir, *, auto_clone=False):
    """EXACTLY the synthetic entry ``browser_reattach.run`` hands its reap.

    Spelled out here rather than imported because that dict is built inline at
    the call site; a pin that assembled it some other way would stop describing
    the call it is about. Note it carries no ``create_time`` — the real one does
    not either, which is why these pins patch the shared identity predicate
    instead of pretending to a value the product never supplies.
    """
    return {
        "pid": pid,
        "user_data_dir": user_data_dir,
        "uses_custom_data_dir": True,
        "auto_clone": auto_clone,
    }


class TestFailedAdoptionReapDoesNotCrossTheSpare:
    """F-917's harm reached through ``browser_reattach.run``'s OWN reap.

    ``run`` protects the pids of its ADOPTABLE candidates (``candidate_pids``),
    and an entry F-916 spared is by construction NOT one of them — it is in
    ``Classified.spare``, which ``run`` never reads. So when an adoption fails
    and falls back to ``reap_recorded``, a spared sibling's pid is in neither
    set, and a directory scan over the profile the two share would end it: the
    same defect as F-917, through a door F-917's filter does not reach.

    **What closes it is F-922's scope rule, not a second subtraction.** ``run``
    hands that reap a metadata dict hard-coding ``uses_custom_data_dir: True``
    and ``auto_clone: False`` — it must, because the profile-delete guard reads
    those two keys and a persistent profile must not be deleted — so the entry
    is PERSISTENT by construction and no directory scan is ever built for it.

    Measured on both sides: at ``50c63fc`` this reap answered ``[6666, 7777]``
    and the spared browser died; here it answers ``[7777]``.
    """

    @staticmethod
    def _reap(pc, metadata, on_directory, protected=frozenset()):
        """Drive ``reap_recorded`` the way ``run`` does; answer the pids killed."""
        before = MagicMock()
        before.create_time.return_value = pc._init_time - 50.0
        killed: list[int] = []
        with (
            patch.object(
                pc, "_get_browser_pids_for_profile", return_value=set(on_directory)
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.Process",
                return_value=before,
            ),
            patch.object(
                pc, "_kill_process_by_pid", lambda pid, iid: killed.append(pid) or True
            ),
            # Not what these pins are about: the synthetic entry carries no
            # create_time, and whether a recorded pid is still its own process
            # is the shared predicate F-917 and F-922 both already lean on.
            patch.object(pc, "_fallback_pid_identity_ok", return_value=True),
            patch.object(pc, "_cleanup_profile_for_metadata"),
        ):
            browser_reattach.reap_recorded(pc, "i-candidate", metadata, protected)
        return sorted(killed)

    def test_a_spared_siblings_browser_survives_a_failed_adoption(self, tmp_path):
        """Both directions in one assertion: the spared sibling is NOT ended and
        the candidate's own recorded browser still IS, so the pin cannot pass by
        the reap having done nothing at all."""
        pc = _cleanup(tmp_path)
        shared = str(tmp_path / "sessions" / "master")

        killed = self._reap(
            pc,
            _failed_adoption_metadata(LIVE_CHROME, shared),
            # `run` passes `candidate_pids - {candidate.pid}`; a SPARED entry is
            # not in `classified.adoptable`, so its pid is in neither set.
            [LIVE_CHROME, SPARED_SIBLING],
            frozenset(),
        )

        assert killed == [LIVE_CHROME], (
            "a browser F-916 spared was killed by a sibling's failed adoption"
        )

    def test_the_scope_rule_is_what_closes_it(self, tmp_path):
        """The counter-direction, so this cannot go vacuous the way F-917's two
        unit pins did: flip the ONE key the scope rule reads and the directory
        scan runs again, reaching the very pid the pin above protects. That is
        what shows these pins measure the rule rather than an empty answer."""
        pc = _cleanup(tmp_path)
        shared = str(tmp_path / "sessions" / "master")

        killed = self._reap(
            pc,
            _failed_adoption_metadata(LIVE_CHROME, shared, auto_clone=True),
            [LIVE_CHROME, SPARED_SIBLING],
            frozenset(),
        )

        # `_reap` sorts, and SPARED_SIBLING (6666) is the lower pid.
        assert killed == [SPARED_SIBLING, LIVE_CHROME]


# ---------------------------------------------------------------------------
# B1 — `run`'s own protected set, and the entry whose persistence is UNKNOWN
# ---------------------------------------------------------------------------


def _shared_record(pc, profile):
    """One adoptable entry and one UNDECIDED entry on ONE directory."""
    _seed(
        pc.pid_file,
        {
            "i-adoptable": _entry(pid=LIVE_CHROME, user_data_dir=str(profile)),
            # No recorded port and no live witness for one -> UNDECIDED, so it
            # lands in `.spare` and NOT in `.adoptable`. That gap is the defect.
            "i-undecided": _entry(
                pid=SPARED_SIBLING, cdp_port=None, user_data_dir=str(profile)
            ),
        },
    )


class TestRunProtectsTheSparedNotTheAdoptable:
    """``browser_reattach.run``'s failed-adoption reap is the SECOND subtraction
    site, and it was handed the wrong set (B1).

    ``run`` protects pids when it falls back to ``reap_recorded``, and it built
    that set from ``classified.adoptable``. An entry F-916 spared is by
    construction NOT adoptable -- it is in ``classified.unclassifiable`` -- so
    every browser this lane exists to protect was absent from the set, while the
    reap's kill set is DIRECTORY-matched. Two subtraction sites disagreeing is
    exactly the shape F-917 fixed; ``process_cleanup`` asks
    ``reap_guard.spared_pids`` and this one did not.

    **These two nodes pin different things and both are needed**, because the
    harm and the defect are currently closed by two different rules:

    * the OUTCOME is F-922's -- a persistent entry gets no directory scan, so
      the reap can only reach the pid the record names. Measured on this tree,
      the sibling survives even with the protected set emptied entirely.
    * the SET is this fix's, and nothing about the outcome can witness it while
      F-922 holds. So the second node reads the set ``run`` actually hands the
      reap. That is what fails when the set is built from ``.adoptable``, and
      what fails if it is emptied -- neither of which any outcome assertion on
      this tree can see.
    """

    @staticmethod
    def _drive(pc, profile):
        """Run one pass whose single adoption FAILS; answer (killed, protected)."""
        manager = MagicMock()
        manager._lock = asyncio.Lock()
        manager._instances = {}
        killed: list[int] = []
        protected: list[frozenset] = []
        before = MagicMock()
        before.create_time.return_value = pc._init_time - 50.0
        real_reap = browser_reattach.reap_recorded

        def spy(cleanup, instance_id, metadata, protected_pids=frozenset()):
            protected.append(protected_pids)
            return real_reap(cleanup, instance_id, metadata, protected_pids)

        with (
            patch.object(pc, "_owner_backend_alive", _owner_alive),
            # BOTH browsers are alive: the sibling must be spared for being
            # UNREACHABLE (no recorded port, no live witness), not for being
            # dead -- a dead entry is an ESTABLISHED negative and gets reaped.
            patch.object(
                browser_reattach, "recorded_browser_alive", lambda _c, _p, _t: True
            ),
            patch.object(
                browser_cmdline, "debug_port", lambda _pid, _expect=None: None
            ),
            patch.object(
                browser_reattach, "_adopt_one", side_effect=RuntimeError("refused")
            ),
            patch.object(browser_reattach, "reap_recorded", spy),
            patch.object(
                pc,
                "_get_browser_pids_for_profile",
                return_value={LIVE_CHROME, SPARED_SIBLING},
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.psutil.Process",
                return_value=before,
            ),
            patch.object(
                pc, "_kill_process_by_pid", lambda pid, iid: killed.append(pid) or True
            ),
            patch.object(pc, "_cleanup_profile_for_metadata"),
        ):
            asyncio.run(browser_reattach.run(manager, pc))
        return sorted(killed), protected

    def test_a_failed_adoption_does_not_kill_the_spared_sibling(self, tmp_path):
        """The harm, end to end: the reap of the entry we could not attach to
        must not reach the browser the same pass just refused to reap."""
        profile = tmp_path / "master"
        profile.mkdir()
        pc = _cleanup(tmp_path)
        _shared_record(pc, profile)

        killed, _ = self._drive(pc, profile)

        assert SPARED_SIBLING not in killed, (
            "a browser F-916 spared was killed by a sibling's failed adoption"
        )
        assert killed == [LIVE_CHROME], "and the failed candidate's own is reaped"

    def test_run_hands_the_reap_the_spared_pids(self, tmp_path):
        """The set itself, because no outcome on this tree can witness it.

        Both directions, so it cannot go vacuous: the spared sibling's pid must
        be IN the set, and the candidate's own must NOT -- the reap is of that
        candidate, and protecting it from itself would leak the browser this
        pass has just failed to adopt.
        """
        profile = tmp_path / "master"
        profile.mkdir()
        pc = _cleanup(tmp_path)
        _shared_record(pc, profile)

        _, protected = self._drive(pc, profile)

        assert len(protected) == 1, "one failed adoption, one reap"
        assert SPARED_SIBLING in protected[0], (
            "the spared sibling's pid is missing from the protected set: "
            "it is built from `.adoptable`, which never contains a spared entry"
        )
        assert LIVE_CHROME not in protected[0]


class TestUnknownPersistenceIsSpared:
    """An entry that records NEITHER persistence key cannot be called disposable.

    ``on_persistent_profile`` answers False for it, but that False is the
    reader's default for a record shape that predates the keys -- not a finding
    about the profile. Measured through the real ``recover_orphans`` with the
    Chrome ALIVE on the shared profile: killed, and its entry dropped, on a
    plain backend cold start. It reaches the kill through the RECORDED-pid
    fallback, so F-922's directory-scan rule does not stop it, and F-918's guard
    answers "may this pid be ended" rather than "is this the right entry".
    """

    @staticmethod
    def _legacy(profile):
        """A 2.0.3-era entry: no `uses_custom_data_dir`, no `auto_clone`."""
        return {
            "pid": LIVE_CHROME,
            "create_time": 1700000000.0,
            "user_data_dir": str(profile),
            "cdp_port": PORT,
            "timestamp": 0,
            "owner_pid": DEAD_OWNER,
            "owner_create_time": 1699999000.0,
        }

    def test_a_live_browser_on_a_keyless_entry_is_not_killed(self, tmp_path):
        """The harm, at the one call site."""
        profile = tmp_path / "master"
        profile.mkdir()
        pc = _cleanup(tmp_path)
        _seed(pc.pid_file, {"i-legacy": self._legacy(profile)})

        killed: list[int] = []
        with (
            patch.object(pc, "_owner_backend_alive", _owner_alive),
            patch.object(
                browser_reattach, "recorded_browser_alive", _recorded_browser_alive
            ),
            patch.object(pc, "_sweep_orphaned_temp_profiles"),
            patch.object(
                pc, "_get_browser_pids_for_profile", return_value={LIVE_CHROME}
            ),
            patch.object(
                pc, "_kill_process_by_pid", lambda pid, iid: killed.append(pid) or True
            ),
        ):
            pc.recover_orphans()

        assert killed == [], "a live browser whose persistence is unknown was killed"
        assert "i-legacy" in _read(pc.pid_file), (
            "and its entry was dropped, so nothing names the browser any more"
        )

    def test_a_keyless_entry_whose_chrome_is_gone_is_still_reaped(self, tmp_path):
        """The counter-direction, and what keeps the record able to SHRINK.

        Sparing on unknown persistence ALONE would make every pre-2.0.4 entry
        permanent -- `browser_pids.json` has no age prune, so nothing would ever
        remove one. The spare is bought with a POSITIVE liveness witness (both
        halves of the pid's identity), so an entry whose Chrome is provably gone
        leaves the record exactly as it does today, and the permanent population
        this fix adds is bounded to browsers that are actually still running.
        """
        profile = tmp_path / "master"
        profile.mkdir()
        pc = _cleanup(tmp_path)
        entry = self._legacy(profile)
        entry["pid"] = DEAD_CHROME
        _seed(pc.pid_file, {"i-legacy": entry})

        killed: list[int] = []
        with (
            patch.object(pc, "_owner_backend_alive", _owner_alive),
            patch.object(
                browser_reattach, "recorded_browser_alive", _recorded_browser_alive
            ),
            patch.object(pc, "_sweep_orphaned_temp_profiles"),
            patch.object(pc, "_get_browser_pids_for_profile", return_value=set()),
            patch.object(
                pc, "_kill_process_by_pid", lambda pid, iid: killed.append(pid) or True
            ),
        ):
            pc.recover_orphans()

        assert "i-legacy" not in _read(pc.pid_file), (
            "a keyless entry whose Chrome is gone must still leave the record"
        )

    def test_an_established_disposable_is_still_reaped(self, tmp_path):
        """The counter-direction, and the distinction the fix rests on:
        ``uses_custom_data_dir: False`` is an ANSWER, not an absence."""
        profile = tmp_path / "uc_throwaway"
        profile.mkdir()
        entry = self._legacy(profile)
        entry["uses_custom_data_dir"] = False

        classified = browser_reattach.adoptable(
            {"i-1": entry}, owner_alive=_owner_alive, browser_alive=_browser_alive
        )

        assert "i-1" not in classified.spare

    def test_an_auto_clone_that_says_so_is_still_reaped(self, tmp_path):
        """The other established shape: both keys present, and they answer."""
        profile = tmp_path / "uc_throwaway"
        profile.mkdir()
        entry = self._legacy(profile)
        entry["uses_custom_data_dir"] = True
        entry["auto_clone"] = True

        classified = browser_reattach.adoptable(
            {"i-1": entry}, owner_alive=_owner_alive, browser_alive=_browser_alive
        )

        assert "i-1" not in classified.spare
