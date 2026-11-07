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
the cost of a backend no one could use.

A leaf, on ``backend_eviction``'s shape: the record, the heartbeat readers and
the act are imported, and every probe that has to know which backends are OURS
(``is_ours``, the health probe, the port check) arrives as an argument from
``cli_supervise``. State is one small JSON file beside the record; a pass that
cannot read it starts every clock afresh, which can only delay a kill.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, NamedTuple

import psutil

from stealth_chrome_devtools_mcp.embedded import (
    backend_eviction,
    backend_liveness,
    backend_registry,
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
#: How many kills the state file remembers, newest last.
KILLS_KEPT = 20

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
    whether this pass terminates it."""

    port: int
    pid: int
    state: str
    unusable_for: float
    kill: bool


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


def _write_state(path: Path, state: dict[str, object]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with contextlib.suppress(OSError):
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)


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
    for verdict, seen in zip(verdicts, observed, strict=True):
        if verdict.state == GREEN:
            continue
        if on_trouble is not None:
            on_trouble()
            on_trouble = None
        _logger.warning(
            "backend pid %d on port %d is %s for %.0fs (silence %s, lag %s)%s",
            seen.pid,
            seen.port,
            verdict.state,
            verdict.unusable_for,
            "none" if seen.silence is None else f"{seen.silence:.1f}s",
            "none" if seen.lag is None else f"{seen.lag:.2f}s",
            "; terminating it (T-15386)" if verdict.kill and not dry_run else "",
        )
        if verdict.kill and not dry_run:
            killed = kill(seen, is_ours=is_ours, is_healthy=is_healthy)
            history.append(
                {
                    "at": at,
                    "port": seen.port,
                    "pid": seen.pid,
                    "state": verdict.state,
                    "unusable_for": round(verdict.unusable_for, 1),
                    "killed": killed,
                }
            )
            clocks.pop(str(seen.port), None)
    _write_state(state_path, {**clocks, "kills": history[-KILLS_KEPT:]})
    return verdicts
