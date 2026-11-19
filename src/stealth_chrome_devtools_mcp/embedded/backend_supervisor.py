"""THE one home for the OUT-OF-PROCESS backend supervisor (T-15386): one pass,
run by a scheduled task every minute, that terminates a backend which has been
unusable for minutes so the next proxy call cold-starts a fresh one.

**Why it has to live outside both processes.** Every other recovery in this tree
runs inside a process the hang can reach. The backend's own heartbeat runs on
the loop that is stuck; the proxies' watchdog asks that backend and then a
cold-start gate whose eviction rule defers to that backend's own stamp. On
2026-11-07 three backends hung for 1.5-48 h each: the loop crawled rather than
stopped, so it went on stamping, the proxies excused their failed probes 276
times, the eviction refused 325 times ("backend unhealable"), and every one of
the ~25 Claude Code sessions on the machine waited until a person killed it.
Each kill fixed it at once. This is that kill, with the rule written down.

**The rule.** A backend of ours is GREEN when ``/_stealth/health`` answers
within :data:`HEALTH_TIMEOUT_SECONDS`. Otherwise it is RED when its own stamp
also says it is in trouble — stale, absent, or reporting a median loop lag at or
past ``backend_liveness.UNUSABLE_LAG_SECONDS`` — and SUSPECT when its stamp says
its loop is fine. RED for :data:`RED_KILL_SECONDS` without one green pass in
between is terminated; SUSPECT gets :data:`SUSPECT_KILL_SECONDS`, because the
one case where the probe can be wrong is this process not being scheduled
(F-889's paged-out machine), and a backend whose own loop is turning normally is
the one witness that says so. A stuck listener behind a healthy loop (F-889
review M1's shape) is still caught, ten minutes later.

**What it never does.** It never kills a process it did not judge: the pid is
pinned to its create time from the pass that saw it red, and a backend that
restarted on the same port starts a fresh clock. It never kills a backend
younger than its own kill threshold, so a slow cold start is not a red one. It
takes no lock — the stuck cold-start gate is exactly what may be holding it —
and the kill is ``backend_eviction.terminate``, the one act every other
terminator uses, with its ``is_ours`` guard. Persistent-profile browsers outlive
it and are re-attached by F-888; clones are reaped by the next backend, which is
the cost of a backend no one could use, and which the kill therefore NAMES
(:func:`lost_browsers`, T-15466).

**It stands down** (T-15466): a backend that goes red again and again after each
fresh start is a fault a kill does not fix, and the supervisor would otherwise
destroy every clone on the machine every few minutes for as long as it lasted.
After :data:`STAND_DOWN_KILLS` kills inside :data:`STAND_DOWN_WINDOW_SECONDS` it
terminates nothing, logs one ERROR per pass saying why and when it resumes, and
keeps every clock, so it resumes by itself the moment the kill that ends it
leaves the window. The kills it counts are the ones in the state file that
terminated a backend (``killed``) with a finite stamp between 0 and the window
behind now: a refused or failed attempt, a malformed entry, and a stamp from the
future (a clock that ran ahead and was corrected) cost nothing, so no clock error
can hold it down. The ERROR is logged once when a stand-down begins
(``supervisor.json`` keeps ``stood_down_since``); later passes of the same one log
a WARNING. One budget covers every backend: a replacement comes up on another
port with another pid, so a per-backend one would never trip, at the price that one
flapping backend can shield another that is wedged for up to the window.

A leaf, on ``backend_eviction``'s shape: the record, the heartbeat readers and
the act are imported, and every probe that has to know which backends are OURS
(``is_ours``, the health probe, the port check) arrives as an argument from
``cli_supervise``. State is one small JSON file beside the record; a pass that
cannot read it starts every clock afresh, which can only delay a kill.
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import TYPE_CHECKING, NamedTuple

import psutil

from stealth_chrome_devtools_mcp.embedded import (
    atomic_file,
    backend_eviction,
    backend_liveness,
    backend_registry,
    browser_pid_registry,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_logger = logging.getLogger("stealth.supervisor")

#: The supervisor's own probe budget. Two and a half times the proxies' 2 s,
#: because this is the witness that ends a backend's life, not one that opens a
#: confirmation phase. Measured under a frozen-Chrome load on a healthy backend,
#: ``/_stealth/health`` p99 was 1.7 s and its max 2.6 s.
HEALTH_TIMEOUT_SECONDS = 5.0
#: How long a backend may stay RED, across consecutive passes, before it is
#: terminated: three missed minutes, i.e. four red passes at one a minute.
RED_KILL_SECONDS = 180.0
#: The same for SUSPECT: failed probes against a backend whose own loop says it
#: is fine. Ten minutes, past the proxies' own ~2-4 minute F-889 veto budget.
SUSPECT_KILL_SECONDS = 600.0
#: The state file, beside ``server.json``.
STATE_NAME = "supervisor.json"
#: How many kills the state file remembers, newest last. Kills that terminated a
#: backend and failed attempts are kept in SEPARATE rings of this size, so a
#: backend whose terminations keep failing cannot push real kills out of the
#: budget.
KILLS_KEPT = 20
#: The state file's marker that a stand-down is in progress: when its first pass
#: held a kill back. It is why the ERROR is logged once per stand-down and not
#: once per pass.
STOOD_DOWN_KEY = "stood_down_since"
#: The stand-down rule (T-15466): this many kills that terminated a backend...
STAND_DOWN_KILLS = 3
#: ...inside this long (thirty minutes) and the supervisor kills no more until
#: the oldest of them is older than this.
STAND_DOWN_WINDOW_SECONDS = 1800.0

GREEN = "green"
RED = "red"
SUSPECT = "suspect"


class Observation(NamedTuple):
    """One recorded backend of ours, as one pass saw it."""

    port: int
    pid: int
    started: float
    healthy: bool
    silence: float | None
    lag: float | None


class Verdict(NamedTuple):
    """One backend's verdict: its state, how long it has been out of GREEN, and
    whether this pass terminates it (or would have, but is standing down)."""

    port: int
    pid: int
    state: str
    unusable_for: float
    kill: bool
    #: Due for termination but held back by the stand-down rule (``kill`` is
    #: then False: this pass terminates nothing).
    stood_down: bool = False


def judge(seen: Observation) -> str:
    """GREEN, RED or SUSPECT for one observation (the module doc's rule)."""
    if seen.healthy:
        return GREEN
    stale = (
        seen.silence is None or seen.silence > backend_liveness.HEARTBEAT_STALE_SECONDS
    )
    lagging = seen.lag is not None and seen.lag >= backend_liveness.UNUSABLE_LAG_SECONDS
    return RED if stale or lagging else SUSPECT


def _clock(state: dict[str, object], seen: Observation) -> float | None:
    """When this pid's current non-green run began, per the last pass, or None."""
    entry = state.get(str(seen.port))
    if not isinstance(entry, dict) or entry.get("pid") != seen.pid:
        return None
    if entry.get("started") != seen.started:
        return None
    since = entry.get("since")
    return float(since) if isinstance(since, int | float) else None


def decide(
    observed: list[Observation], state: dict[str, object], now: float
) -> tuple[list[Verdict], dict[str, object]]:
    """THE one decision, pure: each backend's verdict and the clocks to keep.

    A run's clock starts at the first non-green pass, never earlier than the
    process's own start, so a backend is never older-in-trouble than it is old.
    One GREEN pass clears it. A port the record no longer lists is dropped.
    """
    verdicts: list[Verdict] = []
    clocks: dict[str, object] = {}
    for seen in observed:
        state_now = judge(seen)
        if state_now == GREEN:
            verdicts.append(Verdict(seen.port, seen.pid, GREEN, 0.0, kill=False))
            continue
        since = _clock(state, seen)
        since = max(now if since is None else since, seen.started)
        clocks[str(seen.port)] = {
            "pid": seen.pid,
            "started": seen.started,
            "since": since,
        }
        limit = RED_KILL_SECONDS if state_now == RED else SUSPECT_KILL_SECONDS
        verdicts.append(
            Verdict(
                seen.port, seen.pid, state_now, now - since, kill=now - since >= limit
            )
        )
    return verdicts, clocks


def observe(
    record: Path,
    entry: backend_registry.BackendEntry,
    *,
    is_ours: Callable[[object], bool],
    healthy: Callable[[int], bool],
) -> Observation | None:
    """One recorded entry as a live backend of OURS, or None — no usable port or
    pid, or a pid that is not our backend (a dead record is ``cleanup``'s, never
    this module's: it has nothing to kill)."""
    port = backend_registry.recorded_int(entry, "port")
    pid = backend_registry.recorded_int(entry, "pid")
    if port is None or pid is None or not is_ours(pid):
        return None
    try:
        started = psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None
    return Observation(
        port=port,
        pid=pid,
        started=started,
        healthy=healthy(port),
        silence=backend_liveness.silence(record, port),
        lag=backend_liveness.reported_lag(record, port),
    )


def _still(pid: int, started: float) -> bool:
    """True iff ``pid`` is still the process this pass judged."""
    try:
        return psutil.Process(pid).create_time() == started
    except (psutil.Error, OSError):
        return False


def kill(
    seen: Observation,
    *,
    is_ours: Callable[[object], bool],
    is_healthy: Callable[[int], bool],
) -> bool:
    """Terminate exactly the backend ``seen`` describes, through the one act.

    The pid is pinned: ``terminate`` resolves its target through the
    ``pid_on_port`` it is handed and trusts that answer, so this one answers only
    the judged pid, only while it is still the same process AND still ours —
    ``is_ours`` is asked here because ``terminate`` asks it of the recorded-pid
    fallback alone, which is off.
    """

    def pinned(_port: int) -> int | None:
        same = _still(seen.pid, seen.started) and is_ours(seen.pid)
        return seen.pid if same else None

    return backend_eviction.terminate(
        seen.port,
        pid_on_port=pinned,
        recorded_pid=None,
        is_ours=is_ours,
        is_healthy=is_healthy,
    )


def read_state(path: Path) -> dict[str, object]:
    """The state file, or ``{}`` when it is absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict[str, object]) -> bool:
    """Publish the state through ``atomic_file`` (a sharing refusal is retried);
    False, with a WARNING, when it still cannot be written.

    Never raised: a pass has observed and decided by now, and failing it would
    only hide its verdicts. But the kill history in this file is what the
    stand-down counts (T-15466) and the next pass reads it back, so a lost write
    leaves the next pass counting fewer kills than happened. :func:`run_pass`
    therefore writes the record of a kill BEFORE making it, and does not make a
    kill it could not record.
    """
    try:
        atomic_file.write_json(path, state, indent=2)
    except OSError as error:
        _logger.warning(
            "supervisor state not written to %s: %s; the clocks and kills of this "
            "pass are not recorded, and a kill is not made without its record",
            path,
            error,
        )
        return False
    return True


def _real_kill_time(entry: object) -> float | None:
    """When ``entry`` terminated a backend, or None for a failed attempt, a
    malformed entry, or a stamp that is not a finite number (``json.loads``
    accepts ``Infinity`` and ``1e999``)."""
    if not isinstance(entry, dict) or entry.get("killed") is not True:
        return None
    stamp = entry.get("at")
    if isinstance(stamp, bool) or not isinstance(stamp, int | float):
        return None
    return float(stamp) if math.isfinite(stamp) else None


def recent_kills(history: list[object], at: float) -> list[float]:
    """When each kill that terminated a backend inside the stand-down window
    happened, oldest first: ``0 <= at - stamp < STAND_DOWN_WINDOW_SECONDS``.

    Both bounds, because a stand-down must always end by itself. A malformed
    entry or a non-finite stamp counts as none. A stamp in the FUTURE (a clock
    that ran ahead and was corrected) counts as none too: it cannot say how long
    ago a kill was, and counting it would extend a stand-down by however far the
    clock was out. The cost is bounded: after a clock step the budget forgets
    kills it cannot date, so at most three more kills can follow.
    """
    return sorted(
        stamp
        for entry in history
        if (stamp := _real_kill_time(entry)) is not None
        and 0 <= at - stamp < STAND_DOWN_WINDOW_SECONDS
    )


def _trimmed(history: list[object]) -> list[object]:
    """``history`` limited to the last :data:`KILLS_KEPT` real kills and the last
    ``KILLS_KEPT`` of everything else, in their original order."""
    real = [i for i, entry in enumerate(history) if _real_kill_time(entry) is not None]
    others = set(range(len(history))) - set(real)
    keep = set(real[-KILLS_KEPT:]) | set(sorted(others)[-KILLS_KEPT:])
    return [entry for i, entry in enumerate(history) if i in keep]


def _since(state: dict[str, object]) -> float | None:
    """When the stand-down the state file records began, or None."""
    stamp = state.get(STOOD_DOWN_KEY)
    if isinstance(stamp, bool) or not isinstance(stamp, int | float):
        return None
    return float(stamp) if math.isfinite(stamp) else None


#: The longest a session name is reported at, so a hand-named profile directory
#: cannot widen a log line.
_NAME_LIMIT = 64


def _session_name(entry: browser_pid_registry.Entry) -> str | None:
    """The NAME of the session a persistent browser belongs to, or None.

    ``fleet_session.lock_key`` is the one mapping from a directory to a session:
    ``default`` for the shared profile, the casefolded name for a directory under
    the session root, and the whole normalised PATH for anything else (a profile
    opened by absolute path through ``user_data_dir=``). Only the first two are
    names; a path is not the log's to carry, nor is its last component, so that
    browser is reported by instance id alone. Imported here, on the one pass that
    kills, because it brings the session-root settings with it.
    """
    directory = entry.get("user_data_dir")
    if not isinstance(directory, str) or not directory:
        return None
    try:
        from stealth_chrome_devtools_mcp.embedded import fleet_session

        key = fleet_session.lock_key(directory)
    except Exception:  # a log line must never fail a pass
        _logger.debug("could not name a session for a lost browser", exc_info=True)
        return None
    if not key or any(mark in key for mark in ("/", "\\", ":")):
        return None
    return key[:_NAME_LIMIT]


def lost_browsers(record: Path, pid: int) -> str | None:
    """One line naming the browsers backend ``pid`` owns that its termination
    loses, or None when it owns none that are running (T-15466).

    From the browser-pid registry beside ``record`` (``backend_eviction.
    owned_entries``, the one reader of "whose browsers"). A persistent profile is
    named by SESSION and will be re-attached by the next backend (F-888); every
    other browser is a disposable clone that does not survive the backend. Only
    instance ids and session names are written: no profile path, no URL, no
    cookie (the record holds none, and the line reads three fields of it).
    """
    owned = backend_eviction.owned_entries(
        record.parent, pid, is_running=psutil.pid_exists
    )
    clones = sorted(
        instance_id
        for instance_id, entry in owned.items()
        if not browser_pid_registry.on_persistent_profile(entry)
    )
    sessions = sorted(
        f"{instance_id} (session {name})"
        if (name := _session_name(entry))
        else instance_id
        for instance_id, entry in owned.items()
        if browser_pid_registry.on_persistent_profile(entry)
    )
    if not (clones or sessions):
        return None
    return (
        f"it owned {len(clones)} disposable clone(s) now lost "
        f"[{', '.join(clones) or 'none'}] and {len(sessions)} named session(s) "
        f"the next backend re-attaches [{', '.join(sessions) or 'none'}]"
    )


def _action(verdict: Verdict, *, dry_run: bool, standing: bool) -> str:
    """The tail of a pass's per-backend WARNING: what this pass does about it."""
    if standing:
        return "; NOT terminating it: standing down (T-15466)"
    return "; terminating it (T-15386)" if verdict.kill and not dry_run else ""


def run_pass(  # noqa: PLR0913  PERMANENT(function interface)
    record: Path,
    *,
    is_ours: Callable[[object], bool],
    healthy: Callable[[int], bool],
    is_healthy: Callable[[int], bool],
    dry_run: bool = False,
    now: Callable[[], float] = time.time,
    on_trouble: Callable[[], object] | None = None,
) -> list[Verdict]:
    """One supervisor pass over every backend ``record`` lists: observe, decide,
    terminate what is due (never under ``dry_run``), keep the clocks. Never
    raises for a backend it cannot see; a pass with nothing recorded is a no-op.

    The clocks are kept under ``dry_run`` too: they are observations, and a
    later real pass is right to act on how long a backend has really been red.
    ``on_trouble`` is called once, before the first non-green line is logged,
    so the caller opens a log file only on a pass that has something to say —
    at one pass a minute, an always-open log would be 1,440 files a day.
    """
    state_path = record.with_name(STATE_NAME)
    state = read_state(state_path)
    observed = [
        seen
        for entry in backend_registry.read_backends(record)
        if (seen := observe(record, entry, is_ours=is_ours, healthy=healthy))
    ]
    at = now()
    verdicts, clocks = decide(observed, state, at)
    kills = state.get("kills")
    history = list(kills) if isinstance(kills, list) else []
    since = _since(state)
    held_back = False

    def persist() -> bool:
        """Write the state as it stands. The stand-down lasts while the budget is
        spent; a pass that finds it spent no longer drops the marker, so the next
        episode is announced at ERROR again. A dry run reads the marker and never
        writes it, so it cannot swallow the real pass's ERROR."""
        spent = len(recent_kills(history, at)) >= STAND_DOWN_KILLS
        marker = _since(state) if dry_run else since
        extra = {STOOD_DOWN_KEY: marker} if marker is not None and spent else {}
        return _write_state(state_path, {**clocks, **extra, "kills": _trimmed(history)})

    for index, (verdict, seen) in enumerate(zip(verdicts, observed, strict=True)):
        if verdict.state == GREEN:
            continue
        if on_trouble is not None:
            on_trouble()
            on_trouble = None
        recent = recent_kills(history, at)
        standing = verdict.kill and len(recent) >= STAND_DOWN_KILLS
        _logger.warning(
            "backend pid %d on port %d is %s for %.0fs (silence %s, lag %s)%s",
            seen.pid,
            seen.port,
            verdict.state,
            verdict.unusable_for,
            "none" if seen.silence is None else f"{seen.silence:.1f}s",
            "none" if seen.lag is None else f"{seen.lag:.2f}s",
            _action(verdict, dry_run=dry_run, standing=standing),
        )
        if standing:
            # Held back, clock kept: the first pass after the window is due at once.
            verdicts[index] = verdict._replace(kill=False, stood_down=True)
            if not held_back:
                held_back = True
                # The kill that ends it is the one whose leaving the window drops
                # the count below STAND_DOWN_KILLS: the oldest of the newest ones.
                resumes = (
                    recent[len(recent) - STAND_DOWN_KILLS]
                    + STAND_DOWN_WINDOW_SECONDS
                    - at
                )
                log = _logger.error if since is None else _logger.warning
                log(
                    "supervisor standing down (T-15466)%s: %d backend kills in the "
                    "last %.0f minutes (limit %d), so it terminates nothing; a "
                    "backend that goes red after every fresh start is a fault a "
                    "kill does not fix. It resumes by itself in %.0fs, when the "
                    "kill that ends it leaves the window",
                    "" if since is None else " (still)",
                    len(recent),
                    STAND_DOWN_WINDOW_SECONDS / 60,
                    STAND_DOWN_KILLS,
                    resumes,
                )
                if since is None:
                    since = at
            continue
        if verdict.kill and not dry_run:
            lost = lost_browsers(record, seen.pid)
            # Written as a kill BEFORE the attempt and corrected after it: the
            # count is what bounds the kills, and a pass that is killed, or whose
            # final write is lost, must not leave a kill uncounted. A record that
            # cannot be written means no kill; the next pass tries again.
            attempt: dict[str, object] = {
                "at": at,
                "port": seen.port,
                "pid": seen.pid,
                "state": verdict.state,
                "unusable_for": round(verdict.unusable_for, 1),
                "killed": True,
            }
            history.append(attempt)
            if not persist():
                history.pop()
                _logger.warning(
                    "backend pid %d on port %d is NOT terminated: the kill could "
                    "not be recorded, and the stand-down counts what is recorded",
                    seen.pid,
                    seen.port,
                )
                continue
            killed = kill(seen, is_ours=is_ours, is_healthy=is_healthy)
            attempt["killed"] = killed
            if killed and lost is not None:
                _logger.warning(
                    "backend pid %d on port %d terminated; %s",
                    seen.pid,
                    seen.port,
                    lost,
                )
            clocks.pop(str(seen.port), None)
    persist()
    return verdicts
