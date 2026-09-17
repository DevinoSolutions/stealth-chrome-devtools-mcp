"""Lifecycle resilience over the REAL wire — "the MCP server disconnected and my
browsers vanished" as an executable invariant.

The two symptoms this module exists to make impossible are the operator's, not a
schema's: an MCP server that goes to ``CONNECTION_CLOSED`` mid-session, and
browsers that close under a session that never asked for it. Both have the same
shape in the code — *something decided the shared backend was no longer the
backend* — and the tree has five machines that can decide it: the proxy
watchdog's condemnation (F-820), ``proxy_selfheal``'s heal/teardown (F-838,
F-843), a source-fingerprint eviction (F-829), orphan reaping
(``process_cleanup`` + ``browser_pid_registry``'s owner stamps), and MCP session
hygiene (F-862). Every node below applies ONE stress to a real fleet and then
asks the same four questions.

**The four invariants.** After each stress, from the client's view AND from the
logs:

1. the backend pid recorded in the isolated ``server.json`` is UNCHANGED;
2. every browser spawned before the stress is still alive — its pid is running
   AND it answers a CDP round trip (``get_active_tab`` + ``execute_script``),
   which is what separates "the process is still there" from "the browser is
   still usable";
3. every ``tools/call`` issued on a surviving proxy — the ones in flight during
   the stress and the ones issued after it — got exactly one non-error response
   frame, and no proxy's stdout reached EOF (an EOF IS the client's
   ``CONNECTION_CLOSED``);
4. ZERO lifecycle incidents appear in the proxy and backend logs written during
   the stress.

**Why the logs are read for lines rather than for Sentry events.** F-827's
``observability.capture_lifecycle`` is the canonical report for the four
transitions, but the whole suite runs under ``STEALTH_MCP_NO_ERROR_REPORTING=1``
(``tests/conftest.py`` sets it, and an isolated backend inherits it), under which
``capture_lifecycle`` returns ``False`` before it builds anything. What survives
that switch is the half the docstring calls the piggyback: *"every caller keeps
its own log line, at its own level, with its own text"*. Those lines are the
oracle here, and :data:`LIFECYCLE_INCIDENTS` binds each one to the constant it
accompanies, so a message that drifts is caught by
``test_lifecycle_incident_patterns_match_the_product_strings`` rather than by a
node quietly never matching anything again.

A STRIKE is deliberately not an incident. ``backend_watchdog`` logs
``probe failed n/3`` on every missed probe, and F-820's whole point is that
strikes alone no longer condemn; a node that failed on one would be re-asserting
the defect. Strikes are COUNTED and reported instead, because how many a stress
produced is the measurement that says whether the stress bit at all.

**The fleet is real.** Every node drives the absolute installed console launcher
over stdio JSON-RPC (:class:`release_gate_harness.RawStdioWire`), inside ONE
throwaway HOME with its own ``--singleton-port``
(:func:`release_gate_harness.gate_workspace`), against real headless Chrome.
Nothing is mocked and nothing imports ``embedded/server.py``: a disconnect does
not exist at the in-process ``.fn`` seam, and neither does a second proxy.

Lane and budget. Marked ``integration`` + ``transport``, so it runs in both
gate cells on Linux/Windows and is deselected on macOS exactly as
``test_wire_semantics`` is (F-773). Measured wall times per node are recorded in
each node's docstring. The whole module is **408.5s** (local Windows, 2.1.8, 7
passed + 1 xfail), against a ``transport`` cell that took 4m43s at 2.1.8 inside
a 20-minute job budget and an ``integration`` cell that took 14m33s inside 40 —
so it fits both with room, and every node is inside the 300s per-test ceiling.

The idle node's window is the expensive one and it is EARNED rather than slept:
:data:`IDLE_WINDOW_SECONDS` is computed from the product's own constants (the
maximum periodic reaper in the tree is session hygiene's
``ABANDONED_AFTER_SECONDS`` + ``SWEEP_INTERVAL_SECONDS``), and the witness proxy
that must out-wait it is established in a module fixture and then left strictly
alone while every other node runs. Its idleness is real wall-clock idleness,
overlapped with work rather than added to it — which also makes it a truer model
of the operator's machine, where some sessions are busy while others sit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import psutil
import pytest
import pytest_asyncio

from e2e_helpers import CAN_RUN

# ``_backend_pid_from_state`` / ``_pid_running`` are imported rather than
# re-implemented: "which pid does this isolated server.json record" and "is that
# pid running" already have ONE home each in the harness, and a second copy here
# is exactly the drift this suite legislates against everywhere else.
from release_gate_harness import (
    RawStdioWire,
    _backend_pid_from_state,
    _backend_pids_from_state,
    _pid_running,
    gate_work_dir,
    gate_workspace,
    resolve_launcher,
    workspace_backend_logs,
    workspace_proxy_warnings,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.transport,
    # ONE event loop for the whole module, stated rather than inherited. The
    # idle witness is a LIVE proxy that must survive from module setup to the
    # last node, and an ``asyncio.subprocess`` transport belongs to the loop
    # that created it: on a per-function loop the witness's first write after
    # setup raised ``AttributeError: 'NoneType' object has no attribute 'send'``
    # from a proactor whose loop had closed — a harness artefact that would have
    # read as a product failure. Same reasoning, and the same one-line
    # statement, as ``test_e2e_animations_edge``'s module-scoped fixture.
    pytest.mark.asyncio(loop_scope="module"),
    # Both gate cells run this module at --timeout=300; every wait below is
    # bounded by its own deadline well inside that. Pinned for the same reason
    # test_wire_semantics pins it: the two cells must agree, and the stricter
    # one must not cut a node off before its own bound can say what went wrong.
    pytest.mark.timeout(300),
]

if not CAN_RUN:
    pytestmark.append(pytest.mark.skip("Chrome not available or server failed to load"))


# ── Bounds. Harness bounds, never product deadlines: if one of these fires the
# fleet did not answer, and the node fails by name instead of hanging. ────────
HANDSHAKE_BOUND = 130.0  # first backend-bound call — covers the backend cold start
SPAWN_BOUND = 120.0  # a real Chrome launch, cold
CALL_BOUND = 90.0
NAV_TIMEOUT_MS = 20_000

# ── Stress sizing. Measured, and justified where the number is not obvious. ──
CPU_SATURATION_SECONDS = 20.0  # house rule caps this at 25s; one window at a time
CPU_LOAD_MULTIPLE = 2  # os.cpu_count() * 2 busy loops, normal priority
SOAK_SECONDS = 60.0
SOAK_PROXIES = 3
PROBE_CHURN_SESSIONS = 30  # initialize + DELETE, the product's own liveness probe
PROBE_CHURN_PROXIES = 5  # clean connect/disconnect cycles beside the churn
MIXED_VERSION_SECONDS = 60.0
TICK_SECONDS = 1.0


# ── The lifecycle-incident vocabulary ────────────────────────────────────────
# One entry per transition that ends, or nearly ends, a user's session. The
# value is the set of substrings the product's own ``logging`` call renders into
# the proxy log, ALL of which must appear in one line; the comment names the
# function it comes from, so the pin below can check the pair rather than
# trusting either half alone.
#
# A tuple rather than one substring because a single short phrase is a false
# positive waiting to happen: ``times in a row`` on its own would fire on any
# future line anywhere that happened to contain it. Spanning the `%d` that sits
# in the middle of the real message is what makes the match specific.
LIFECYCLE_INCIDENTS: dict[str, tuple[str, ...]] = {
    # backend_watchdog.watch_liveness — F-820's strikes CONCLUDED
    "condemned:watchdog": ("backend on port", "confirmed unusable"),
    # proxy_selfheal._confirm_bridge_verdict — F-843's fast witness concluded
    "condemned:connection_lost": ("confirmed gone after a lost connection",),
    # proxy_selfheal.heal_backend — the session survived, onto a NEW backend
    "healed": ("backend healed: re-bridging",),
    # proxy_selfheal.heal_backend — every attempt failed (reason=unhealable)
    "teardown:unhealable": ("backend unhealable after",),
    # proxy_selfheal.drive — recoveries keep failing (reason=flapping)
    "teardown:flapping": ("backend lost", "times in a row", "giving up"),
    # singleton._start_backend_holding_lock — F-829's source-change eviction
    "eviction": ("backend stale (source changed), evicting",),
}

# NOT an incident: F-820 exists precisely so a missed probe is not a verdict.
# ``backend_watchdog.watch_liveness`` writes one of these per missed tick as
# ``probe failed %d/%d on port %d``, so the FULL run is readable from the text —
# which is what lets S1 assert the implication below rather than a count.
STRIKE_MARKER = "probe failed"
# NOT an incident either: F-886's bind-site line, written by the proxy that
# found another session's backend still serving browsers and spawned its own
# beside it instead of evicting it. S5 asserts its PRESENCE — it is the proof
# the fix path was taken, rather than the fleet converging by some accident.
STEP_ASIDE_MARKER = "spawning ours beside it"
# The OTHER non-incident, and the one that makes a full strike run legible:
# ``watch_liveness`` logs this (INFO) at the single point where the confirmation
# phase was entered AND answered "alive" — i.e. exactly when F-820 did its job.
# It is the positive half of the only branch that can also emit
# ``confirmed unusable`` (``condemned:watchdog`` above), so "a full strike run,
# neither line" is not a state the product has: a run that reaches N/N proves
# which way the verdict went. Same tuple-of-substrings shape as an incident, for
# the same reason — one short phrase is a false positive waiting to happen.
CONFIRMED_BUSY_PARTS = ("backend on port", "was busy, not dead")
# The product's own format string for a strike, restated so the reader below is
# derived from it rather than from a hand-typed shape; the vocabulary pin
# asserts it is still in ``backend_watchdog`` AND that the reader reads what it
# renders, so a reworded line cannot leave S1 silently matching nothing.
STRIKE_FORMAT = "probe failed %d/%d on port %d"
_STRIKE_RUN_RE = re.compile(r"probe failed (\d+)/(\d+) on port (\d+)")
# ``on port %d`` is the ONE way every watchdog line names its subject, which is
# what lets a verdict be matched to the strike run it concluded rather than to
# "some verdict, somewhere in the fleet".
_PORT_RE = re.compile(r"on port (\d+)")


async def test_lifecycle_incident_patterns_match_the_product_strings() -> None:
    """Every substring in :data:`LIFECYCLE_INCIDENTS` still occurs in the module
    that emits it — so a reworded log line turns THIS node red instead of
    silently making every stress node vacuous.

    It reads SOURCE BYTES and nothing else — no Chrome, no fleet, no logs. It is
    the reason the log-scan oracle can be trusted at all, so it is placed first:
    when a rename breaks the vocabulary, this is the node that names it, and
    every stress node below stops meaning anything until it is fixed.

    ``async def`` only because the module pins one event loop (see
    ``pytestmark``) and the autouse fixtures that establish the fleet are async
    on it; the body awaits nothing.

    Residual, named rather than hidden: it reads the REPO's ``src/`` while every
    proxy runs the INSTALLED launcher. Identical under this repo's editable
    install (a ``.pth`` that appends ``src`` to ``sys.path``), but a gate cell
    that installed a built wheel would have this node pinning bytes the fleet
    does not execute. The fleet-side half is covered by the stress nodes' own
    log scans, which read what the running proxies actually wrote.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "stealth_chrome_devtools_mcp"
    watchdog = (src / "embedded" / "backend_watchdog.py").read_text(encoding="utf-8")
    selfheal = (src / "embedded" / "proxy_selfheal.py").read_text(encoding="utf-8")
    singleton = (src / "embedded" / "singleton.py").read_text(encoding="utf-8")
    eviction = (src / "embedded" / "backend_eviction.py").read_text(encoding="utf-8")

    homes = {
        "condemned:watchdog": watchdog,
        "condemned:connection_lost": selfheal,
        "healed": selfheal,
        "teardown:unhealable": selfheal,
        "teardown:flapping": selfheal,
        "eviction": singleton,
    }
    assert set(homes) == set(LIFECYCLE_INCIDENTS)
    for kind, text in homes.items():
        for part in LIFECYCLE_INCIDENTS[kind]:
            assert part in text, (
                f"{kind}: {part!r}, part of the log line this module greps for, "
                f"is gone from its own module — re-derive it, do not delete the "
                f"check"
            )
    assert STRIKE_MARKER in watchdog
    assert STEP_ASIDE_MARKER in eviction
    # The three non-incident facts S1's implication oracle is built on, pinned
    # for the same reason the incidents are: reword any of them and the oracle
    # goes RED rather than quietly vacuous.
    for part in CONFIRMED_BUSY_PARTS:
        assert part in watchdog, (
            f"{part!r}, part of the 'busy, not dead' verdict S1's implication "
            f"oracle requires, is gone from backend_watchdog"
        )
    assert STRIKE_FORMAT in watchdog, (
        f"{STRIKE_FORMAT!r}, the format string S1 parses strike runs out of, is "
        f"gone from backend_watchdog — re-derive the reader from the new one"
    )
    rendered = STRIKE_FORMAT % (3, 3, 19222)
    assert _strike_run(rendered) == (3, 3, 19222), (
        f"the strike-run reader no longer reads the product's own line: "
        f"{rendered!r} -> {_strike_run(rendered)}"
    )
    assert _port_in(rendered) == 19222
    # And the verdict must still be logged at a level the workspace RECORDS.
    # Its strikes are WARNING and the verdict is INFO, so the two halves of the
    # implication do not travel together: `release_gate_harness._isolated_env`
    # pins STEALTH_MCP_LOG_LEVEL=INFO so an exported level cannot drop the
    # verdict (pinned there, where the env is built), and this asserts the other
    # direction — that the product has not moved the verdict BELOW what that
    # pin records. Either alone leaves a decided run reading as a silence.
    verdict_call = watchdog[: watchdog.index(CONFIRMED_BUSY_PARTS[1])]
    level = verdict_call.rsplit("_logger.", 1)[-1].split("(", 1)[0]
    assert level in {"info", "warning", "error", "critical"}, (
        f"the 'busy, not dead' verdict is logged at {level!r}, which the "
        f"gate workspace's pinned INFO level does not record — a decided "
        f"strike run would read as a silence"
    )


# ── The idle window, computed from the product's own constants ───────────────
def _idle_window_seconds() -> float:
    """The longest a session may sit untouched and still have to be alive.

    Derived, never typed: the maximum PERIODIC reaper in the tree is MCP session
    hygiene (F-862), which terminates a session with no standing GET stream that
    has made no request for ``ABANDONED_AFTER_SECONDS``, checked every
    ``SWEEP_INTERVAL_SECONDS``. Everything else is shorter or disabled —
    ``backend_watchdog`` ticks at 2s with 3 strikes, ``singleton``'s reuse
    patience is 60s, ``browser_idle_timeout`` defaults to 0 (reaping OFF, the
    correct default for a persistent server, asserted in the idle node), and
    ``process_cleanup``'s orphan reap is a one-shot at the serve boundary.

    Imported from the module that owns them so a change to either constant moves
    this window rather than leaving a stale number behind.
    """
    from stealth_chrome_devtools_mcp.embedded import session_hygiene

    return (
        session_hygiene.ABANDONED_AFTER_SECONDS
        + session_hygiene.SWEEP_INTERVAL_SECONDS
        + 5.0  # margin: the sweep can only fire AFTER the interval elapses
    )


IDLE_WINDOW_SECONDS = _idle_window_seconds()
# The most the idle node may SLEEP inside the module's 300s per-test ceiling and
# still have room for its own assertions (~10s measured). A full-module run
# reaches the node with 194-218s remaining; anything above this is a partial
# selection, which the node turns into a legible skip rather than a timeout.
#
# It is a budget, not a derivation, and it depends on exactly three things —
# change any of them and re-measure this number: (1) the ``timeout`` in this
# module's ``pytestmark`` (300s, the per-test ceiling both gate cells run at),
# (2) :data:`IDLE_WINDOW_SECONDS`, which is what the node must out-wait, and
# (3) the wall time the nodes BEFORE it spend, since the witness's idleness is
# overlapped with their work rather than added to it.
IDLE_SLEEP_BUDGET_SECONDS = 250.0


# ── Frame readers (same shapes test_wire_semantics uses) ─────────────────────
def _tool_result(frame: dict) -> dict:
    assert "error" not in frame, f"protocol error frame: {frame}"
    return frame["result"]


def _tool_payload(frame: dict):
    result = _tool_result(frame)
    if "structuredContent" in result:
        structured = result["structuredContent"]
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    content = result.get("content") or []
    return json.loads(content[0]["text"]) if content else None


def _response_frame_counts(wire: RawStdioWire) -> Counter:
    """How many RESPONSE frames arrived per request id — THE one home for the
    "exactly one outcome per call" rule this module documents.

    Counted off ``wire.frames``, the raw stdout the client actually read, not off
    the harness's ``_responses`` map, which keeps only the last frame for an id
    and so cannot see a duplicate at all. A frame carrying ``method`` is a
    server->client REQUEST (this server sends ``roots/list``) in the server's own
    id space, which can collide with ours — excluding it is what keeps the count
    honest, the same reason ``RawStdioWire.frames_for`` excludes it.
    """
    return Counter(
        frame["id"]
        for frame in wire.frames
        if frame.get("id") is not None
        and "method" not in frame
        and ("result" in frame or "error" in frame)
    )


async def _call(wire: RawStdioWire, name: str, args: dict, timeout: float = CALL_BOUND):
    request_id = await wire.call_tool(name, args)
    frame = await wire.response(request_id, timeout)
    seen = _response_frame_counts(wire)[request_id]
    assert seen == 1, (
        f"tool {name} (id {request_id}) got {seen} response frames, not exactly "
        f"one — a client cannot tell which outcome is its answer"
    )
    result = _tool_result(frame)
    assert result.get("isError") is not True, f"tool {name} failed: {result}"
    return frame


async def _handshake(wire: RawStdioWire) -> None:
    await wire.initialize()
    listed = await wire.request("tools/list")
    await wire.response(listed, HANDSHAKE_BOUND)


async def _spawn(wire: RawStdioWire, profile: str) -> str:
    frame = await _call(
        wire,
        "spawn_browser",
        {"headless": True, "sandbox": False, "user_data_dir": profile},
        timeout=SPAWN_BOUND,
    )
    return _tool_payload(frame)["instance_id"]


async def _cdp_round_trip(wire: RawStdioWire, instance_id: str) -> None:
    """Prove the browser is USABLE, not merely running.

    Two calls on purpose. ``get_active_tab`` goes through ``tab_identity``'s
    ``Target.getTargets`` round trip — the browser's own devtools socket must
    answer — and ``execute_script`` proves a live execution context in the page.
    A browser whose process survived a stress but whose websocket did not is
    exactly the failure an operator reports as "my browser closed".
    """
    await _call(wire, "get_active_tab", {"instance_id": instance_id})
    frame = await _call(
        wire, "execute_script", {"instance_id": instance_id, "script": "1 + 1"}
    )
    payload = _tool_payload(frame)
    assert payload["success"] is True, payload
    assert payload["result"] == 2, payload


# ── Log-scan oracle ──────────────────────────────────────────────────────────
def _log_files(space: dict) -> list[Path]:
    # ``*.log*``, not ``*.log``: ``configure_logging`` installs a
    # ``RotatingFileHandler`` (5 MiB x 3), so a rotation inside a stress window
    # moves lines — an incident line among them — into ``<name>.log.1``. Unlikely
    # at 5 MiB and free to cover; a silently-missed incident is the one failure
    # this oracle may not have.
    dirs = [space["log_dir"], space["home_dir"] / ".stealth-mcp" / "logs"]
    files: list[Path] = []
    for directory in dirs:
        if directory.is_dir():
            files.extend(sorted(directory.glob("*.log*")))
    return files


def _log_offsets(space: dict) -> dict[Path, int]:
    """Byte offset of every existing log file — the "before" of a stress window.

    Offsets rather than a line-set diff: a repeated identical warning is a real
    thing a fleet produces, and a set difference would swallow the second one.
    Files created DURING the stress simply have no offset and are read whole.
    """
    offsets: dict[Path, int] = {}
    for path in _log_files(space):
        with contextlib.suppress(OSError):
            offsets[path] = path.stat().st_size
    return offsets


def _lines_since(space: dict, offsets: dict[Path, int]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in _log_files(space):
        start = offsets.get(path, 0)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start)
                text = handle.read()
        except OSError:
            continue
        out.extend((path.name, line) for line in text.splitlines() if line.strip())
    return out


def _incidents(lines: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """``(kind, file, line)`` for every lifecycle incident in ``lines``.

    ALL of a kind's substrings must be in the same line — see
    :data:`LIFECYCLE_INCIDENTS` for why one short phrase is not enough.
    """
    found: list[tuple[str, str, str]] = []
    for name, line in lines:
        for kind, parts in LIFECYCLE_INCIDENTS.items():
            if all(part in line for part in parts):
                found.append((kind, name, line))
    return found


def _strikes(lines: list[tuple[str, str]]) -> int:
    return sum(1 for _, line in lines if STRIKE_MARKER in line)


def _strike_run(line: str) -> tuple[int, int, int] | None:
    """``(consecutive, limit, port)`` for a strike line, else ``None``.

    Derived from :data:`STRIKE_FORMAT`, which the vocabulary pin ties to the
    product's own source AND to this reader, so a reworded line cannot leave
    this matching nothing.
    """
    m = _STRIKE_RUN_RE.search(line)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _port_in(line: str) -> int | None:
    """The port a watchdog line is ABOUT — every one of them says ``on port N``."""
    m = _PORT_RE.search(line)
    return int(m[1]) if m else None


def _watchdog_event(name: str, line: str) -> tuple[str, int, tuple[int, int]] | None:
    """``(file, port, (consecutive, limit))`` for a strike line, ``(file, port,
    ())`` for a busy verdict, ``None`` for anything else.

    The FILE is part of the identity and not decoration: every proxy in this
    module talks to the one backend on ``space["port"]``, so the port alone
    cannot tell two proxies apart, and ``proxy-<pid>.log`` is the only thing
    that can.
    """
    port = _port_in(line)
    if port is None:
        return None
    if all(part in line for part in CONFIRMED_BUSY_PARTS):
        return (name, port, ())
    run = _strike_run(line)
    return (name, port, (run[0], run[1])) if run is not None else None


def assert_strikes_concluded_correctly(
    lines: list[tuple[str, str]], what: str
) -> tuple[int, int]:
    """The F-820 IMPLICATION: **whenever one proxy reaches a FULL strike run,
    that proxy's confirmation phase must have answered "busy, not dead"** — once
    the run is DECIDED, and never by a sibling's verdict.

    Returns ``(strike lines, longest run reached)``.

    An implication rather than a count, because the count is not stable: across
    seven runs of this module S1 logged 0, 0, 0, 0, 0, 6 and 0 strikes, so any
    threshold or floor over it would be a coin flip. The implication is exactly
    as strong as the product's own branch — ``watch_liveness`` reaches
    ``consecutive == failures_before_teardown`` and then logs precisely one of
    two verdicts — so it is VACUOUS on a run where the load did not bite and a
    real F-820 oracle on one where it did.

    TWO things make it an assertion about the right thing, and without each the
    oracle is wrong in a DIFFERENT direction:

    1. The key is ``(log file, port)``, never the port alone. The port is shared
       by construction here, so a port-keyed check lets proxy B's verdict close
       proxy A's open run — a real silence, passed. The file name is what
       identifies the proxy.
    2. A full run that is the LAST watchdog line its proxy wrote is PENDING, not
       silent. ``watch_liveness`` reaches the limit and then *awaits*
       ``confirm_probe`` — ``_same_identity_backend_ready`` with
       ``REUSE_PATIENCE_SECONDS`` (60 s) of patience and a 10 s per-attempt
       probe — logging nothing until it decides. A proxy that struck out in the
       last seconds of a window is still inside that when the logs are read, and
       demanding its verdict would fail a product doing exactly what F-820 asks.
       Any LATER watchdog line from the same proxy proves its loop moved on, so
       the verdict was due and its absence is the real defect.

    The other half of the implication — never ``confirmed unusable`` — is not
    checked here at all: it is one of :data:`LIFECYCLE_INCIDENTS`, so
    :func:`assert_no_lifecycle_incident` has already failed on it, with a better
    message, before this function runs. That is also why a condemnation cannot
    reach this scan as a "later watchdog line".
    """
    longest = 0
    open_runs: dict[tuple[str, int], tuple[int, str]] = {}  # key -> (index, line)
    last_event: dict[str, int] = {}  # file -> index of its last watchdog line
    for index, (name, line) in enumerate(lines):
        event = _watchdog_event(name, line)
        if event is None:
            continue
        _, port, run = event
        last_event[name] = index
        if not run:  # the busy verdict: this proxy's run on this port is closed
            open_runs.pop((name, port), None)
            continue
        consecutive, limit = run
        longest = max(longest, consecutive)
        if consecutive >= limit:
            open_runs[(name, port)] = (index, line)
        # A PARTIAL run deliberately does not close an open one. The counter is
        # only reset to 0 by a healthy tick or by a successful confirmation, and
        # the latter logs the verdict — so a `1/3` following a `3/3` with no
        # verdict between them IS the silence this function exists to catch.

    silent = {
        key: line
        for key, (index, line) in open_runs.items()
        if last_event[key[0]] > index
    }
    assert not silent, (
        f"{what}: {len(silent)} full strike run(s) were reached, the proxy that "
        f"reached each kept logging afterwards, and no "
        f"{CONFIRMED_BUSY_PARTS[1]!r} verdict was ever written for them:\n"
        + "\n".join(
            f"  {name} (port {port}): {line}" for (name, port), line in silent.items()
        )
        + "\nThe confirmation phase is what F-820 exists for; a decided run "
        "that never reports it means the watchdog did not confirm.\n"
        "All watchdog lines in the window:\n"
        + "\n".join(
            f"  {name}: {line}"
            for name, line in lines
            if _watchdog_event(name, line) is not None
        )
    )
    return _strikes(lines), longest


def assert_no_lifecycle_incident(
    space: dict, offsets: dict[Path, int], what: str
) -> tuple[int, int]:
    """The fourth invariant, plus the one thing a strike DOES have to imply.

    Returns ``(strike lines, longest run reached)`` — both measurements, never
    verdicts (F-820: strikes alone never condemn). The verdict half is
    :func:`assert_strikes_concluded_correctly`, applied here rather than in one
    node because "a full strike run must have concluded 'busy, not dead'" is
    true of EVERY node, not only the one that applies load: S0, which applies
    none, has logged 2 strikes.
    """
    lines = _lines_since(space, offsets)
    incidents = _incidents(lines)
    assert not incidents, (
        f"{what}: {len(incidents)} lifecycle incident(s) in the logs — this is "
        f"the disconnect the operator reports:\n"
        + "\n".join(f"  [{kind}] {name}: {line}" for kind, name, line in incidents)
        + f"\n--- proxy warnings ---\n{workspace_proxy_warnings(space)[-3000:]}"
    )
    return assert_strikes_concluded_correctly(lines, what)


def _log_line(pid: int, text: str) -> tuple[str, str]:
    """One synthetic ``(file, line)`` pair in the shape ``_lines_since`` yields:
    the file is ``proxy-<pid>.log``, which is how a proxy is identified."""
    return (f"proxy-{pid}.log", f"2026-09-16 18:10:58,550 WARNING {pid} [-] {text}")


STRUCK_OUT = STRIKE_FORMAT % (3, 3, 4087)
PARTIAL_STRIKE = STRIKE_FORMAT % (1, 3, 4087)
BUSY_VERDICT = "backend on port 4087 was busy, not dead"


async def test_the_strike_implication_is_per_proxy_and_waits_for_a_pending_verdict():
    """:func:`assert_strikes_concluded_correctly` on synthetic lines — the two
    ways an implication oracle can be wrong, made RED-provable without Chrome.

    It exists because the live oracle has never fired: across seven runs of this
    module the longest strike run never reached the limit, so neither its pass
    nor its fail path was ever exercised by a real fleet. A check whose first
    real firing is also its first execution is a coin flip; these five cases are
    how it is exercised instead.

    The first case is the reviewer's, verbatim, and it FAILED the port-keyed
    version of this function: every proxy here talks to the one backend on
    ``space["port"]``, so proxy B's verdict closed proxy A's open run and a real
    silence passed. The third is its mirror — ``watch_liveness`` awaits a
    confirmation that may legitimately take up to ``REUSE_PATIENCE_SECONDS``
    without logging, so a run still in flight at scan time must not be a
    failure. Both are the same fix: key on ``(file, port)`` and treat "no later
    watchdog line from this proxy" as pending.

    ``async def`` only because the module pins one event loop; the body awaits
    nothing.
    """
    # 1. Two proxies, ONE verdict — proxy 111 never concluded and kept logging.
    with pytest.raises(AssertionError, match=r"proxy-111\.log"):
        assert_strikes_concluded_correctly(
            [
                _log_line(111, STRUCK_OUT),
                _log_line(222, STRUCK_OUT),
                _log_line(222, BUSY_VERDICT),
                _log_line(111, PARTIAL_STRIKE),
            ],
            "sibling verdict",
        )

    # 2. The same proxy concludes its own run: the implication holds.
    assert assert_strikes_concluded_correctly(
        [_log_line(111, STRUCK_OUT), _log_line(111, BUSY_VERDICT)],
        "self verdict",
    ) == (1, 3)

    # 3. PENDING: the full run is the last watchdog line this proxy wrote, so
    #    its confirmation is still running. Not a defect, must not fail.
    assert assert_strikes_concluded_correctly(
        [_log_line(222, BUSY_VERDICT), _log_line(111, STRUCK_OUT)],
        "verdict in flight",
    ) == (1, 3)

    # 4. DECIDED AND SILENT: the same proxy kept logging after striking out and
    #    never reported a verdict. This is the defect the oracle is for.
    with pytest.raises(AssertionError, match="full strike run"):
        assert_strikes_concluded_correctly(
            [_log_line(111, STRUCK_OUT), _log_line(111, PARTIAL_STRIKE)],
            "decided and silent",
        )

    # 5. Below the limit: vacuous, and the longest run is still measured.
    assert assert_strikes_concluded_correctly(
        [_log_line(111, PARTIAL_STRIKE), _log_line(111, STRIKE_FORMAT % (2, 3, 4087))],
        "no full run",
    ) == (2, 2)


# ── Browser-liveness oracle ──────────────────────────────────────────────────
def _browser_pids(space: dict) -> dict[str, int]:
    """``{instance_id: pid}`` from the isolated ``browser_pids.json``.

    Read through ``browser_pid_registry.read_entries`` — the product's OWN
    reader — rather than by parsing the file here. The record has a wrapper
    (``{"browser_processes": {...}, "timestamp": ...}``) and a legacy shape, and
    a hand-rolled parse that guessed wrong would have read an EMPTY table as "no
    browsers", i.e. would have been unable to distinguish a healthy fleet from a
    massacred one. That is exactly what this module must never do, so the parse
    keeps its one home.
    """
    from stealth_chrome_devtools_mcp.embedded import browser_pid_registry

    path = space["home_dir"] / ".stealth-mcp" / "browser_pids.json"
    entries = browser_pid_registry.read_entries(path)
    return {
        instance_id: entry["pid"]
        for instance_id, entry in entries.items()
        if isinstance(entry.get("pid"), int)
    }


def assert_browser_processes_alive(
    space: dict, instance_ids: list[str], what: str
) -> None:
    tracked = _browser_pids(space)
    for instance_id in instance_ids:
        pid = tracked.get(instance_id)
        assert pid is not None, f"{what}: {instance_id} is no longer tracked: {tracked}"
        assert _pid_running(pid), f"{what}: browser pid {pid} for {instance_id} is gone"


def assert_wire_healthy(wire: RawStdioWire, what: str) -> None:
    """Invariant 3, asserted over the whole session's stdout.

    Three things: no EOF (an EOF IS the client's ``CONNECTION_CLOSED``), no
    non-frame bytes, and EXACTLY ONE response frame per request id. The last is
    the sweep half of the rule ``_call`` checks per call: a duplicate that
    arrives AFTER its call returned is invisible there and visible here.

    Issues no call of its own, so it is safe on the idle witness — reading
    ``wire.frames`` does not reset an idle clock.
    """
    assert not wire.stdout_eof, f"{what}: the proxy's stdout reached EOF"
    assert not wire.non_frame_stdout, (
        f"{what}: non-frame bytes on stdout: {wire.non_frame_stdout[:3]}"
    )
    duplicated = {rid: n for rid, n in _response_frame_counts(wire).items() if n != 1}
    assert not duplicated, (
        f"{what}: request id(s) with a response-frame count other than one: "
        f"{duplicated}"
    )


# ── Fixtures ─────────────────────────────────────────────────────────────
# Ports this module must never bind: the product's default singleton port and
# the two other live ports the brief names. The harness's ``_pick_free_port``
# already refuses the default port and every port the developer's REAL
# ``server.json`` records (which is where 52554 lives when it is live); this
# set is the brief's literal, asserted AFTER the pick as a belt-and-braces
# check, never a second picker.
RESERVED_PORTS = frozenset({19222, 52554, 7169})


@contextlib.contextmanager
def _isolated_workspace(work_dir, label: str):
    """``gate_workspace`` (whose port pick refuses the reserved set) plus a
    check of that pick against :data:`RESERVED_PORTS` and the leftover-children
    assertion, for BOTH workspaces this module creates.

    The assertion is inside the ``with`` rather than after it, and guarded by
    having actually got a workspace: written after the ``finally`` it raises
    ``NameError`` when ``gate_workspace`` fails BEFORE yielding, hiding the real
    cause behind an unbound name.
    """
    with gate_workspace(work_dir) as workspace:
        assert workspace["port"] not in RESERVED_PORTS, workspace["port"]
        yield workspace
    assert not workspace["leftover_children"], (
        f"{label} left child processes behind: {workspace['leftover_children']}"
    )


@pytest.fixture(scope="module")
def launcher():
    return resolve_launcher()


@pytest.fixture(scope="module")
def space(tmp_path_factory):
    """ONE isolated backend for the whole module: throwaway HOME, own state dir,
    own log dir, own ``--singleton-port``.

    Nothing here can touch the developer's live ``~/.stealth-mcp``, their
    browser-session root, or ports 19222 / 52554 / 7169 — the port is checked
    against :data:`RESERVED_PORTS` and the HOME redirect is what makes
    ``server.json`` and ``browser_pids.json`` private to this run. The block also
    owns teardown: the recorded backend is terminated and any child left behind
    is named.
    """
    fallback = tmp_path_factory.mktemp("life")
    work_dir = gate_work_dir(fallback)
    try:
        with _isolated_workspace(work_dir, "the lifecycle module") as workspace:
            yield workspace
    finally:
        if work_dir != fallback:
            shutil.rmtree(work_dir, ignore_errors=True)


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def primed(launcher, space):
    """Pay the backend cold start and create the master profile ONCE, then exit.

    Two things follow from doing it in a throwaway proxy that then goes away.
    The master profile exists, so every node's NAMED profile can be cloned from
    the snapshot. And the backend is no longer any later proxy's child, which is
    what makes the sibling-death node able to hard-kill a proxy without the kill
    being about the backend's parentage instead of about the policy under test.
    """
    wire = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    await wire.start()
    try:
        await _handshake(wire)
        frame = await _call(
            wire,
            "spawn_browser",
            {"headless": True, "sandbox": False},
            timeout=SPAWN_BOUND,
        )
        instance_id = _tool_payload(frame)["instance_id"]
        await _call(wire, "close_instance", {"instance_id": instance_id})
    finally:
        await wire.aclose()

    pid = _backend_pid_from_state(space["home_dir"])
    assert pid is not None and _pid_running(pid), "no isolated backend came up"
    return pid


@pytest.fixture()
def backend_pid(space, primed):
    """The recorded backend pid, re-read now and asserted unchanged since prime.

    A node that starts on a DIFFERENT backend than the module primed has already
    lost the invariant, and finding that out here names it as setup rather than
    as the node's own stress.
    """
    current = _backend_pid_from_state(space["home_dir"])
    assert current == primed, (
        f"the backend changed before this node even ran: {primed} -> {current}\n"
        f"{workspace_proxy_warnings(space)[-2000:]}"
    )
    return current


def assert_backend_unchanged(space: dict, expected: int, what: str) -> None:
    current = _backend_pid_from_state(space["home_dir"])
    assert current == expected, (
        f"{what}: the shared backend was replaced ({expected} -> {current}) — "
        f"every session on it saw a disconnect\n"
        f"--- proxy warnings ---\n{workspace_proxy_warnings(space)[-3000:]}\n"
        f"--- backend logs ---\n{workspace_backend_logs(space)[-2000:]}"
    )
    assert _pid_running(expected), f"{what}: backend pid {expected} is gone"


@contextlib.asynccontextmanager
async def _proxy(launcher, space, *, handshake: bool = True):
    wire = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    await wire.start()
    try:
        if handshake:
            await _handshake(wire)
        yield wire
    finally:
        await wire.aclose()


@contextlib.asynccontextmanager
async def _owned_instance(wire: RawStdioWire, profile: str):
    """One named-profile browser whose close is a FINALIZER, not trailing code.

    A node that fails mid-body used to leave its Chrome running until the module
    teardown terminated the backend — three of them, on a developer machine that
    may already be near Chrome's process ceiling (F-811). The close is
    best-effort and suppressed: a node's verdict is its assertions, never its
    cleanup, and a browser the stress genuinely killed must not turn a real
    failure into a confusing one.
    """
    instance_id = await _spawn(wire, profile)
    try:
        yield instance_id
    finally:
        with contextlib.suppress(Exception):
            await _call(wire, "close_instance", {"instance_id": instance_id})


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def idle_witness(launcher, space, primed):
    """A proxy + browser established once, then left STRICTLY alone.

    ``autouse`` for one reason and it is the whole design: the witness's clock
    must start at MODULE setup, not at the node that reads it. Requested only by
    the idle node, it would be created when that node runs and its idle window
    would then have to be slept in full.

    This is the idle node's subject and the reason its 335s window costs the
    module almost nothing: the witness's idleness runs concurrently with every
    other node's work, which is also the fleet shape the operator actually has —
    some sessions hammering the backend while others sit untouched for minutes.

    Nothing may touch the wire between setup and the idle node — a single call
    would reset the very clock that node measures — but it must still be
    WRITABLE there, which is what the module's ``loop_scope`` is for.
    """
    wire = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    await wire.start()
    await _handshake(wire)
    instance_id = await _spawn(wire, "life-idle-witness")
    await _cdp_round_trip(wire, instance_id)
    state = {
        "wire": wire,
        "instance_id": instance_id,
        "quiet_since": time.monotonic(),
    }
    yield state
    with contextlib.suppress(Exception):
        await wire.aclose()


# ── S0 — baseline soak ───────────────────────────────────────────────────────
async def test_s0_baseline_soak_three_proxies_sixty_seconds(
    launcher, space, backend_pid, fixture_app_server
):
    """S0: three proxies, three browsers, 60s of mixed real tool calls, nothing
    moves.

    The control. Every other node adds a stress on top of exactly this shape, so
    if S0 is red the rest of the module is uninterpretable. The operations are
    the ones a session actually issues — navigate to a local fixture page,
    scroll it, type into it, screenshot it, list instances — rather than a ping,
    because a fleet that only ever handshakes exercises none of the CDP work
    that makes a shared backend slow enough to be suspected of being dead.

    SIZING, MEASURED (local Windows, 2.1.8): the 60s window produced **756 tool
    calls** across the three proxies — one round of six operations costs ~1.4s
    per proxy, so each ran ~42 rounds. Node wall time 65.4s of soak inside 89.6s
    including the module's cold start, the idle witness and three Chrome
    launches. 60s was chosen as the shortest window that still puts several
    hundred real CDP round trips through one shared backend; it leaves the node
    at ~30% of the 300s per-test ceiling even on a runner three times slower.
    Strikes observed: 0.
    """
    page = f"{fixture_app_server}/life/lifecycle.html"
    offsets = _log_offsets(space)
    wires: list[RawStdioWire] = []
    instances: list[str] = []
    calls = {"issued": 0}

    async def one_proxy(index: int) -> None:
        async with (
            _proxy(launcher, space) as wire,
            _owned_instance(wire, f"life-soak-{index}") as instance_id,
        ):
            wires.append(wire)
            instances.append(instance_id)
            await _call(
                wire,
                "navigate",
                {"instance_id": instance_id, "url": page, "timeout": NAV_TIMEOUT_MS},
            )
            deadline = time.monotonic() + SOAK_SECONDS
            while time.monotonic() < deadline:
                await _call(
                    wire,
                    "scroll_page",
                    {
                        "instance_id": instance_id,
                        "direction": "down",
                        "amount": 400,
                        # Instant: a smooth scroll settles on the page's own
                        # scrollend and can cost seconds. The soak is measuring
                        # session survival, not scroll fidelity — that is
                        # test_e2e_scroll_page_verification's question.
                        "smooth": False,
                    },
                )
                await _call(
                    wire,
                    "type_text",
                    {
                        "instance_id": instance_id,
                        "selector": "#life-input",
                        "text": f"soak-{index}",
                        "clear_first": True,
                    },
                )
                await _call(wire, "take_screenshot", {"instance_id": instance_id})
                await _call(wire, "list_instances", {})
                await _cdp_round_trip(wire, instance_id)
                calls["issued"] += 6
            # Still healthy at the end, on the SAME wire that ran the whole soak,
            # and the browser still tracked, running and CDP-responsive —
            # invariant 2 asserted here, the last moment before
            # ``_owned_instance`` closes it.
            assert_wire_healthy(wire, "S0 soak")
            assert_browser_processes_alive(space, [instance_id], "S0 soak")
            await _cdp_round_trip(wire, instance_id)

    started = time.monotonic()
    await asyncio.gather(*(one_proxy(i) for i in range(SOAK_PROXIES)))
    elapsed = time.monotonic() - started

    # A THROUGHPUT floor as well as a survival one. Measured 624-768 calls
    # across three runs; 60 per proxy is ~4x below the slowest of those, so a
    # product that got several times slower fails here instead of passing with
    # 18 calls (the old floor, 35x below the measurement — a number that could
    # not have failed).
    assert calls["issued"] >= SOAK_PROXIES * 60, calls
    assert_backend_unchanged(space, backend_pid, "S0 soak")
    strikes, longest_run = assert_no_lifecycle_incident(space, offsets, "S0 soak")
    # The longest run is printed here too, not only in S1: S0 applies no load
    # and has still logged 2 strikes, and only the run length says whether that
    # was two proxies striking once or one proxy getting halfway to a verdict.
    print(
        f"\nS0: {elapsed:.1f}s, {calls['issued']} tool calls, {strikes} strikes, "
        f"longest consecutive run {longest_run}"
    )


# ── S1 — CPU saturation ──────────────────────────────────────────────────────
def _busy_loop_command(seconds: float) -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "import time\n"
            f"end = time.monotonic() + {seconds}\n"
            "x = 0\n"
            "while time.monotonic() < end:\n"
            "    x += 1\n"
        ),
    ]


@contextlib.contextmanager
def _saturated_cpu(seconds: float):
    """``os.cpu_count() * 2`` normal-priority busy loops, for ``seconds``.

    Normal priority is the point: the F-820 incident was a machine whose OWN
    agent fleet was saturating it, not a machine under a priority attack, and a
    low-priority load would never make the backend's probes miss. Every pid is
    recorded and killed in the ``finally`` — this module kills only processes it
    created, because the developer's other sessions are live on this machine.
    """
    count = (os.cpu_count() or 4) * CPU_LOAD_MULTIPLE
    procs: list[subprocess.Popen] = []
    try:
        for _ in range(count):
            # Appended one at a time, not built as a comprehension (PERF401):
            # the list is the KILL LIST, and a launch that fails halfway must
            # still leave every process already started in it.
            procs.append(  # noqa: PERF401  PERMANENT(partial-launch kill list)
                subprocess.Popen(  # noqa: S603  PERMANENT(fixed argv, our own interpreter)
                    _busy_loop_command(seconds),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        yield count
    finally:
        for proc in procs:
            with contextlib.suppress(OSError):
                proc.kill()
        for proc in procs:
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)


async def test_s1_cpu_saturation_does_not_condemn_a_live_backend(
    launcher, space, backend_pid, fixture_app_server
):
    """S1: the machine is saturated for 20s while two proxies keep calling —
    the watchdog must not condemn a backend that still answers.

    This is the F-820 / F-856 regression oracle at the fleet level. Under
    saturation the watchdog's 2s probes can miss, strikes accumulate, and the
    ONLY thing standing between that and a fleet-wide disconnect is the
    confirmation phase deciding busy-vs-dead through the same identity+readiness
    gate the cold-start lock trusts — spending its patience in fairly-scheduled
    seconds rather than wall seconds. A strike here is expected and COUNTED; a
    condemnation is the defect.

    Both proxies keep a call in flight roughly every second for the whole
    window, so "the backend still answers" is asserted continuously rather than
    inferred from a probe at the end. Every one of those calls must have exactly
    one non-error response.

    WHAT WAS MEASURED, stated honestly because it bounds what this node proves:
    on a 32-core Windows box, 64 normal-priority busy loops over a ~21s window
    produced 28-80 answered calls, and the strike count VARIED across runs —
    **0** in the first five (mine and an independent reviewer's) and **6** in the
    latest, which also answered only 28 calls rather than 76-80, i.e. the load
    bit that time. In neither case did anything condemn. S0, which applies no
    load at all, logged **2 strikes** in one of the reviewer's runs, so strikes
    are not a clean function of the stress on this box.

    WHAT IT ASSERTS ABOUT THOSE STRIKES, and the exact shape of the disclaimer.
    No count and no floor — 0,0,0,0,0,6,0 across seven runs cannot carry a
    threshold. What is asserted is the IMPLICATION
    (:func:`assert_strikes_concluded_correctly`, applied by every node's
    incident check): if a FULL run is ever reached on a port, the confirmation
    phase must have run for THAT port and must have answered ``was busy, not
    dead`` — never silence, never ``confirmed unusable``. So:

    * on a run whose longest run is BELOW the limit, this node is silent about
      the confirmation phase, because the product never entered it;
    * on a run where a proxy reaches the limit AND keeps logging afterwards,
      this node is an F-820 oracle end to end FOR THAT PROXY, over the real
      transport with the real gate — and stays silent about a sibling whose own
      run is still in flight, which is a property of the two proxies S1 runs,
      not of the check.

    It has NOT been exercised on any run yet: across seven runs the longest run
    never reached the limit, so neither path has fired against a real fleet. The
    hermetic node above is what exercises both, which is why it exists at all.
    The longest run is printed on every run so which of those two happened is
    readable from the output, and so a cell where the load bites harder stays
    visible: a 2-core CI runner at the same 2x oversubscription, with Chrome
    beside it, is the likelier place. The hermetic
    ``test_watchdog_busy_vs_dead`` and ``test_singleton_starvation_patience``
    remain the nodes that reach the confirmation phase DELIBERATELY rather than
    when the box happens to be slow. Not tuned upward to force strikes — the
    window is capped at 25s by house rule, the developer machine runs other
    agents, and a node that must starve a shared machine to mean anything is a
    node that will flake.
    """
    page = f"{fixture_app_server}/life/lifecycle.html"
    offsets = _log_offsets(space)

    async with (
        _proxy(launcher, space) as wire_a,
        _proxy(launcher, space) as wire_b,
        _owned_instance(wire_a, "life-cpu-0") as instance_a,
        _owned_instance(wire_b, "life-cpu-1") as instance_b,
    ):
        pairs = [(wire_a, instance_a), (wire_b, instance_b)]
        for wire, instance_id in pairs:
            await _call(
                wire,
                "navigate",
                {"instance_id": instance_id, "url": page, "timeout": NAV_TIMEOUT_MS},
            )

        answered = {"n": 0}

        async def keep_calling(wire: RawStdioWire, instance_id: str, deadline: float):
            while time.monotonic() < deadline:
                tick = time.monotonic()
                await _call(wire, "list_instances", {})
                await _call(
                    wire,
                    "execute_script",
                    {"instance_id": instance_id, "script": "Date.now()"},
                )
                answered["n"] += 2
                nap = TICK_SECONDS - (time.monotonic() - tick)
                if nap > 0:
                    await asyncio.sleep(nap)

        started = time.monotonic()
        with _saturated_cpu(CPU_SATURATION_SECONDS) as load:
            deadline = time.monotonic() + CPU_SATURATION_SECONDS
            await asyncio.gather(*(keep_calling(w, i, deadline) for w, i in pairs))
        elapsed = time.monotonic() - started

        # The client's view: nothing dropped, and both proxies still work AFTER
        # the load has gone — the condemnation the watchdog might have opened
        # during the window would conclude here, not during it.
        for wire, instance_id in pairs:
            assert_wire_healthy(wire, "S1 cpu saturation")
            await _cdp_round_trip(wire, instance_id)

        assert_backend_unchanged(space, backend_pid, "S1 cpu saturation")
        assert_browser_processes_alive(
            space, [i for _, i in pairs], "S1 cpu saturation"
        )
        strikes, longest_run = assert_no_lifecycle_incident(
            space, offsets, "S1 cpu saturation"
        )

    print(
        f"\nS1: {elapsed:.1f}s, {load} busy loops on {os.cpu_count()} cpus, "
        f"{answered['n']} calls answered, {strikes} watchdog strikes, "
        f"longest consecutive run {longest_run}"
    )


# ── S2 — sibling death ───────────────────────────────────────────────────────
async def test_s2_hard_killing_one_proxy_leaves_the_fleet_untouched(
    launcher, space, backend_pid, fixture_app_server
):
    """S2: proxies A and B share the backend and each owns a browser; A is
    hard-killed. B, B's browser, the backend — and A's browser — all survive.

    A's browser surviving is the DOCUMENTED policy, not a leniency. A browser is
    owned by the BACKEND, not by the proxy: ``browser_pid_registry`` stamps each
    entry with the ``owner_pid``/``owner_create_time`` of the process that wrote
    it, which is the backend, and orphan recovery reaps an entry only when no
    live owner still holds it. Nothing in the tree reaps on a proxy's death, and
    F-867 is what keeps the backend itself out of the client's Job Object so a
    session ending cannot take it down either. "My browsers vanished when
    another Claude session exited" is precisely the bug this node forbids.

    A is killed with ``Process.kill()`` — TerminateProcess on Windows, SIGKILL
    on POSIX — on A's OWN pid and never on its tree: the module's ``primed``
    fixture already paid the cold start, so the backend is nobody's child here,
    and a tree kill would be asking a question about parentage rather than about
    policy.

    Both browsers get a CDP round trip, A's through the SURVIVING proxy — see
    the comment at the assertion for why pid liveness alone cannot decide this.

    MEASURED (local Windows, 2.1.8): 19.7-25.7s for the node, 15.1s of it the
    settle window after the kill. Both browsers alive AND CDP-responsive, both
    listed, B still answering, backend pid unchanged, 0 strikes and 0 incidents.

    One residual, named: on Windows the kill lands on the console-script
    trampoline, which spawns the real ``python.exe`` proxy. An independent probe
    measured the propagation (the proxy child died; only the DETACHED backend
    survived, which is F-867 working), so the stress is real today — but that
    propagation is a uv/OS property this node observes rather than asserts.
    """
    page = f"{fixture_app_server}/life/lifecycle.html"
    offsets = _log_offsets(space)

    wire_a = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    await wire_a.start()
    try:
        await _handshake(wire_a)
        instance_a = await _spawn(wire_a, "life-sibling-a")
        await _call(
            wire_a,
            "navigate",
            {"instance_id": instance_a, "url": page, "timeout": NAV_TIMEOUT_MS},
        )
        proxy_a_pid = wire_a.proc.pid
        assert proxy_a_pid != backend_pid

        async with _proxy(launcher, space) as wire_b:
            instance_b = await _spawn(wire_b, "life-sibling-b")
            await _call(
                wire_b,
                "navigate",
                {"instance_id": instance_b, "url": page, "timeout": NAV_TIMEOUT_MS},
            )
            browsers_before = _browser_pids(space)
            assert instance_a in browsers_before and instance_b in browsers_before

            try:
                elapsed, strikes = await _s2_kill_and_assert(
                    space=space,
                    offsets=offsets,
                    backend_pid=backend_pid,
                    wire_a=wire_a,
                    wire_b=wire_b,
                    proxy_a_pid=proxy_a_pid,
                    instance_a=instance_a,
                    instance_b=instance_b,
                )
            finally:
                # A FINALIZER, not trailing code: an assertion that fires inside
                # the stress must not also leave two Chromes running for the
                # rest of the module.
                for instance_id in (instance_a, instance_b):
                    with contextlib.suppress(Exception):
                        await _call(
                            wire_b, "close_instance", {"instance_id": instance_id}
                        )
    finally:
        await wire_a.aclose()

    print(f"\nS2: {elapsed:.1f}s after the kill, {strikes} strikes")


async def _s2_kill_and_assert(  # noqa: PLR0913  PERMANENT(one call site, named args)
    *, space, offsets, backend_pid, wire_a, wire_b, proxy_a_pid, instance_a, instance_b
) -> tuple[float, int]:
    """S2's stress and its four invariants. Split out of the node ONLY so the
    node's cleanup can be a ``finally`` without burying the assertions three
    indents deep; it has exactly one caller and no policy of its own."""
    # The stress: A dies with no warning and no chance to clean up. The handle
    # is taken BEFORE the kill — on Windows the pid is gone from the table by
    # the time ``kill()`` returns, so re-constructing a ``psutil.Process`` on it
    # afterwards raises rather than reporting the death. Death is confirmed by
    # reaping the child we own.
    proxy_a = psutil.Process(proxy_a_pid)
    started = time.monotonic()
    proxy_a.kill()
    assert await wire_a.wait_exit(30.0) is not None, "proxy A did not die"

    # Give every reaper that COULD react a real chance to: the watchdog ticks at
    # 2s with 3 strikes, so a window several times that is the honest bound for
    # "nothing decided to act on this".
    await asyncio.sleep(15.0)

    assert_backend_unchanged(space, backend_pid, "S2 sibling death")
    assert_wire_healthy(wire_b, "S2 sibling death")
    assert_browser_processes_alive(space, [instance_a, instance_b], "S2 sibling death")

    # B's view of the fleet still names A's orphaned instance: the backend kept
    # it, which is what makes it recoverable rather than silently gone.
    listing = _tool_payload(await _call(wire_b, "list_instances", {}))
    listed = {entry["instance_id"] for entry in listing}
    assert {instance_a, instance_b} <= listed, listing

    # BOTH browsers get the CDP witness, and A's — the orphan, the whole subject
    # of this node — gets it through the SURVIVING proxy. Pid liveness alone
    # would not do: ``_pid_running`` is ``psutil.Process(pid).is_running()``,
    # which returns True for a ZOMBIE, and in this node the backend is
    # deliberately still alive, so a Chrome killed by a hypothetical
    # proxy-death reaper would sit unreaped on Linux/macOS and read as a pass.
    # A round trip cannot be answered by a zombie.
    for instance_id in (instance_b, instance_a):
        await _cdp_round_trip(wire_b, instance_id)

    strikes, _ = assert_no_lifecycle_incident(space, offsets, "S2 sibling death")
    return time.monotonic() - started, strikes


# ── S3 — liveness-probe churn ────────────────────────────────────────────────
async def test_s3_session_churn_keeps_the_backend_serving_old_and_new_sessions(
    launcher, space, backend_pid, idle_witness
):
    """S3: 30 rapid ``initialize``+DELETE sessions and 5 clean proxy
    connect/disconnect cycles — the backend keeps serving, its pid does not
    move, and a session that predates the churn by minutes is untouched.

    The churn is generated by the product's OWN probe,
    ``singleton._backend_http_ready``, rather than by a hand-rolled HTTP client:
    it is the exact shape every proxy's watchdog sends every ~2s, so 30 of them
    back to back is a compressed fleet rather than a synthetic one.

    WHAT THIS NODE DOES NOT CLAIM, stated because its first name claimed it.
    It cannot observe F-862's hygiene sweep reaping anything: the sweep sleeps
    ``SWEEP_INTERVAL_SECONDS`` (30s) between passes and reaps only sessions with
    no GET stream that have been silent for ``ABANDONED_AFTER_SECONDS`` (300s),
    so inside a ~10s node it cannot fire once, and the probe DELETEs its own
    session on every success, so the churn leaves no abandoned session for it to
    find. "A live proxy is never reaped" is S4's claim — the node that actually
    out-waits the window.

    What S3 DOES ask, and the reason the idle witness is asserted here, is the
    dangerous combination no other node covers: a burst of brand-new sessions
    arriving while an OLD session sits silent. The witness was opened at module
    setup, has been idle through S0-S2, and is checked after the churn WITHOUT
    issuing a call on it — ``assert_wire_healthy`` reads its stdout buffer and
    the pid check reads the registry — so its idle clock is not reset and S4 is
    unaffected.

    MEASURED (local Windows, 2.1.8): 5-14s for the node — 30/30 probe sessions
    answered, 5/5 transient proxies handshook and exited cleanly, both live
    proxies still served, the idle witness's stdout intact and its browser
    running, backend pid unchanged, 0 strikes.
    """
    from stealth_chrome_devtools_mcp.embedded import singleton

    offsets = _log_offsets(space)
    port = space["port"]

    async with (
        _proxy(launcher, space) as live_a,
        _proxy(launcher, space) as live_b,
    ):
        await _call(live_a, "list_instances", {})
        await _call(live_b, "list_instances", {})

        started = time.monotonic()
        ok = 0
        for _ in range(PROBE_CHURN_SESSIONS):
            if await asyncio.to_thread(singleton._backend_http_ready, port):
                ok += 1

        for index in range(PROBE_CHURN_PROXIES):
            async with _proxy(launcher, space) as transient:
                await _call(transient, "list_instances", {})
                assert_wire_healthy(transient, f"S3 transient proxy {index}")

        elapsed = time.monotonic() - started

        assert ok == PROBE_CHURN_SESSIONS, (
            f"only {ok}/{PROBE_CHURN_SESSIONS} liveness probes answered — the "
            f"backend stopped serving under session churn"
        )
        for name, wire in (("A", live_a), ("B", live_b)):
            assert_wire_healthy(wire, f"S3 live proxy {name}")
            await _call(wire, "list_instances", {})
        # The old session, untouched: no call on it (that would reset the idle
        # clock S4 measures), only reads of what it already has.
        assert_wire_healthy(idle_witness["wire"], "S3 idle witness after churn")
        assert_browser_processes_alive(
            space, [idle_witness["instance_id"]], "S3 idle witness after churn"
        )
        assert_backend_unchanged(space, backend_pid, "S3 session churn")
        strikes, _ = assert_no_lifecycle_incident(space, offsets, "S3 session churn")

    print(
        f"\nS3: {elapsed:.1f}s, {ok} probe sessions + {PROBE_CHURN_PROXIES} proxy "
        f"cycles, {strikes} strikes"
    )


# ── S4 — idle ────────────────────────────────────────────────────────────────
async def test_s4_a_session_idle_past_every_reaper_still_answers(
    launcher, space, backend_pid, idle_witness
):
    """S4: a proxy and its browser sit untouched for longer than every periodic
    reaper in the tree, then answer.

    The window is :data:`IDLE_WINDOW_SECONDS`, derived from session hygiene's own
    constants (300s abandoned + 30s sweep + margin) because that is the longest
    period in the tree; ``browser_idle_timeout`` is asserted to still default to
    0 here, since a non-zero default would put a SECOND reaper above that window
    and silently invalidate the derivation.

    The witness was opened in a module fixture and has been idle through every
    node above, so most of the window is already spent by the time this runs;
    only the remainder is slept. That is not a shortcut — it is a stricter test
    than a dedicated sleep would be, because the backend was busy serving other
    sessions throughout, which is when a per-session reaper is most likely to
    pick the wrong victim.

    MEASURED (local Windows, 2.1.8): window 335.0s, of which the nodes above had
    already spent 116.9s, so 218.4s was slept here. 0 strikes; the witness's
    session, its backend and its browser all survived. This node is the module's
    single largest cost and the overlap is what keeps it affordable — run in
    isolation it would cost the full 335s.
    """
    from stealth_chrome_devtools_mcp.settings import Settings

    assert Settings.model_fields["browser_idle_timeout"].default == 0, (
        "browser idle reaping is no longer off by default — IDLE_WINDOW_SECONDS "
        "must be re-derived before this node means anything"
    )

    offsets = _log_offsets(space)
    idle_for = time.monotonic() - idle_witness["quiet_since"]
    remaining = IDLE_WINDOW_SECONDS - idle_for
    if remaining > IDLE_SLEEP_BUDGET_SECONDS:
        # This node's window is PAID by the nodes above it. Selected on its own
        # (`-k s4`, `--lf` after a flake, a bisect) it would have to sleep the
        # whole 335s and would die at the 300s ceiling as `Timeout >300.0s`
        # with no diagnosis. A legible skip is the honest answer; a full-module
        # run never takes this branch (measured remaining: 194-218s).
        pytest.skip(
            f"the idle witness has been idle {idle_for:.0f}s of the "
            f"{IDLE_WINDOW_SECONDS:.0f}s window and {remaining:.0f}s remain, more "
            f"than this node may sleep inside the 300s ceiling "
            f"({IDLE_SLEEP_BUDGET_SECONDS:.0f}s); its window is spent by the nodes "
            f"above it — run the whole module"
        )
    if remaining > 0:
        await asyncio.sleep(remaining)
    total_idle = time.monotonic() - idle_witness["quiet_since"]
    assert total_idle >= IDLE_WINDOW_SECONDS

    wire: RawStdioWire = idle_witness["wire"]
    instance_id: str = idle_witness["instance_id"]

    try:
        assert_wire_healthy(wire, "S4 idle")
        await _call(wire, "list_instances", {})
        await _cdp_round_trip(wire, instance_id)
        assert_backend_unchanged(space, backend_pid, "S4 idle")
        assert_browser_processes_alive(space, [instance_id], "S4 idle")
        strikes, _ = assert_no_lifecycle_incident(space, offsets, "S4 idle")
    finally:
        # The witness's browser is closed here rather than in its fixture: the
        # fixture must never CALL the wire (that resets the clock), and this is
        # the one node allowed to. A finalizer so a failed assertion above does
        # not also leave it running.
        with contextlib.suppress(Exception):
            await _call(wire, "close_instance", {"instance_id": instance_id})

    print(
        f"\nS4: idle {total_idle:.1f}s (window {IDLE_WINDOW_SECONDS:.0f}s, "
        f"{remaining:.1f}s of it slept here), {strikes} strikes"
    )


# ── S5 — mixed source fingerprints ───────────────────────────────────────────
def _variant_package(work_dir: Path) -> Path:
    """A copy of the installed package tree differing by ONE byte, and the
    ``PYTHONPATH`` root that makes a proxy import it.

    One byte in a comment, in a module the fingerprint necessarily covers:
    ``singleton._source_fingerprint`` hashes every ``*.py`` under the package
    root, so a single appended character is a different digest and an identical
    program. The installed distribution is an editable ``.pth`` that merely
    appends ``src`` to ``sys.path``, and ``PYTHONPATH`` entries precede
    ``.pth``-added directories, so a child launched with this root on
    ``PYTHONPATH`` runs the copy — same console launcher, same version metadata,
    different fingerprint. That is exactly the operator's fleet: several
    long-lived sessions on packages that differ only in source.
    """
    source = (
        Path(__file__).resolve().parent.parent / "src" / "stealth_chrome_devtools_mcp"
    )
    root = work_dir / "variant"
    target = root / "stealth_chrome_devtools_mcp"
    shutil.copytree(
        source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    marker = target / "embedded" / "tool_runtime.py"
    with marker.open("a", encoding="utf-8") as handle:
        handle.write("#\n")  # the one byte (plus its newline) that moves the digest
    return root


def _fingerprint_of(package_dir: Path) -> str | None:
    """The product's OWN digest of ``package_dir`` — ``singleton._source_fingerprint``
    over a repointed ``SOURCE_ROOT`` — so "the two sides differ" is proven by the
    rule the eviction gate actually applies, not by the byte this module wrote.
    """
    from stealth_chrome_devtools_mcp.embedded import singleton

    original = singleton.SOURCE_ROOT
    singleton.SOURCE_ROOT = package_dir
    try:
        return singleton._source_fingerprint()
    finally:
        singleton.SOURCE_ROOT = original


def _process_alive(pid: int) -> bool:
    """Running AND not a zombie: an evicted backend on POSIX stays a zombie
    until the proxy that spawned it reaps it, and ``is_running`` says True of a
    zombie."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


async def _mixed_version_waves(launcher, space, variant_root: Path) -> dict:
    """Run one same-source proxy and one variant-source proxy against ONE state
    dir for :data:`MIXED_VERSION_SECONDS`, and count what happened.

    A "wave" is a change of the recorded backend pid: every wave is a backend
    that was terminated with its browsers still attached, which is what the
    operator sees as "all my browsers closed at once".
    """
    variant_env = dict(space["env"])
    existing = variant_env.get("PYTHONPATH")
    variant_env["PYTHONPATH"] = (
        f"{variant_root}{os.pathsep}{existing}" if existing else str(variant_root)
    )

    timeline: list[tuple[float, int]] = []  # (seconds since t0, recorded pid)
    instances: dict[str, str] = {}
    served: dict[str, str] = {}
    browser_pid_at_spawn: dict[str, int] = {}
    t0 = time.monotonic()

    def _record_pid() -> None:
        pid = _backend_pid_from_state(space["home_dir"])
        if pid is not None and (not timeline or timeline[-1][1] != pid):
            timeline.append((round(time.monotonic() - t0, 1), pid))

    def _remember_browser(name: str) -> None:
        pid = _browser_pids(space).get(instances.get(name, ""))
        if pid is not None:
            browser_pid_at_spawn[name] = pid

    wire_same = RawStdioWire(launcher=launcher, env=space["env"], port=space["port"])
    wire_variant = RawStdioWire(launcher=launcher, env=variant_env, port=space["port"])
    await wire_same.start()
    try:
        await _handshake(wire_same)
        instances["same"] = await _spawn(wire_same, "life-mixed-same")
        _remember_browser("same")
        _record_pid()

        await wire_variant.start()
        try:
            with contextlib.suppress(Exception):
                await _handshake(wire_variant)
            with contextlib.suppress(Exception):
                instances["variant"] = await _spawn(wire_variant, "life-mixed-variant")
            _remember_browser("variant")

            deadline = t0 + MIXED_VERSION_SECONDS
            while time.monotonic() < deadline:
                _record_pid()
                await asyncio.sleep(TICK_SECONDS)
            _record_pid()

            # The client's view at the end of the window, per side: did the
            # session survive at all, and does it still answer? A proxy whose
            # stdout reached EOF is the operator's CONNECTION_CLOSED.
            # Deliberately NOT through ``_call``: that helper ASSERTS, and an
            # assertion turns the very thing being measured into a lost frame.
            # Here the frame itself is the measurement.
            for name, wire in (("same", wire_same), ("variant", wire_variant)):
                if wire.stdout_eof:
                    served[name] = "eof"  # the operator's CONNECTION_CLOSED
                    continue
                try:
                    rid = await wire.call_tool("list_instances", {})
                    frame = await wire.response(rid, 30.0)
                except Exception as exc:  # noqa: BLE001  PERMANENT(measurement, not a verdict)
                    served[name] = f"no-answer:{type(exc).__name__}"
                    continue
                result = frame.get("result")
                if "error" in frame:
                    served[name] = f"protocol-error:{json.dumps(frame['error'])[:200]}"
                elif isinstance(result, dict) and result.get("isError") is True:
                    served[name] = f"tool-error:{json.dumps(result)[:200]}"
                else:
                    served[name] = "ok"
        finally:
            await wire_variant.aclose()
    finally:
        await wire_same.aclose()

    pids = [pid for _, pid in timeline]
    return {
        "backend_pid_timeline": timeline,
        "backend_pids": pids,
        "waves": max(len(pids) - 1, 0),
        # Which recorded backends are still processes at the end of the window,
        # read BEFORE the workspace tears its own backend down: convergence
        # means exactly one of them is.
        "backends_alive": {pid: _process_alive(pid) for pid in pids},
        # EVERY backend the record names at the end, not just the one the
        # timeline followed (F-886). The timeline reads the FIRST entry, which
        # is the incumbent; the arriving proxy's own backend is a SECOND entry
        # under the same display context, and "both are recorded and both are
        # running" is the post-fix invariant the timeline alone cannot see.
        "recorded_at_end": {
            pid: _process_alive(pid)
            for pid in _backend_pids_from_state(space["home_dir"])
        },
        "instances": instances,
        # Every browser this node spawned, by the pid it had AT SPAWN — so a
        # browser killed with its evicted backend is reported as dead rather
        # than merely absent from a registry the winner rewrote.
        # ``_process_alive``, never ``_pid_running``: the surviving side's
        # browser still has a live backend to reparent to, so a killed Chrome
        # can sit as a zombie and ``is_running`` would call it alive. S5b
        # asserts on this dict, so a zombie-blind read would let the eviction
        # fix pass over a dead browser — the one false pass this suite exists
        # to prevent.
        "browsers_alive": {
            name: _process_alive(pid) for name, pid in browser_pid_at_spawn.items()
        },
        "served_at_end": served,
    }


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def mixed_fleet(launcher, tmp_path_factory):
    """ONE mixed-fingerprint run, shared by the two S5 nodes.

    The run costs ~60s and answers two independent questions — did the fleet
    converge, and what became of the session that LOST — so it is paid once and
    read twice rather than run twice.
    """
    fallback = tmp_path_factory.mktemp("life-mixed")
    work_dir = gate_work_dir(fallback)
    try:
        with _isolated_workspace(
            work_dir, "the mixed-fingerprint fleet"
        ) as mixed_space:
            variant_root = _variant_package(Path(work_dir))
            # The premise, proven by the product's own rule before a second
            # of fleet time is spent: the two roots the two proxies will import
            # carry DIFFERENT fingerprints. Without this, a copy that failed
            # to move the digest would run a same-source fleet and report
            # "no eviction, browsers alive" — a pass about nothing.
            fingerprints = {
                "same": _fingerprint_of(
                    Path(__file__).resolve().parent.parent
                    / "src"
                    / "stealth_chrome_devtools_mcp"
                ),
                "variant": _fingerprint_of(
                    variant_root / "stealth_chrome_devtools_mcp"
                ),
            }
            assert None not in fingerprints.values(), fingerprints
            assert fingerprints["same"] != fingerprints["variant"], (
                f"the variant package did not move the source fingerprint: "
                f"{fingerprints}"
            )
            offsets = _log_offsets(mixed_space)
            started = time.monotonic()
            report = await _mixed_version_waves(launcher, mixed_space, variant_root)
            report["elapsed"] = time.monotonic() - started
            report["fingerprints"] = fingerprints
            lines = _lines_since(mixed_space, offsets)
            report["incidents"] = _incidents(lines)
            report["stepped_aside"] = sum(
                1 for _, line in lines if STEP_ASIDE_MARKER in line
            )
            report["proxy_warnings"] = workspace_proxy_warnings(mixed_space)[-4000:]
    finally:
        if work_dir != fallback:
            shutil.rmtree(work_dir, ignore_errors=True)

    print(
        f"\nS5: {report['elapsed']:.1f}s  waves={report['waves']}  "
        f"stepped_aside={report['stepped_aside']}  "
        f"timeline={report['backend_pid_timeline']}  "
        f"backends_alive={report['backends_alive']}  "
        f"recorded_at_end={report['recorded_at_end']}  "
        f"served_at_end={report['served_at_end']}  "
        f"browsers_alive={report['browsers_alive']}  "
        f"fingerprints={ {k: v[:12] for k, v in report['fingerprints'].items()} }"
    )
    for kind, name, line in report["incidents"]:
        print(f"S5 incident [{kind}] {name}: {line}")
    # Every proxy's WARNING-or-worse lines. Which SIDE lost its backend, and
    # whether it condemned, healed, re-bridged or said NOTHING AT ALL, is the
    # difference between the shapes these two nodes tell apart.
    print(f"S5 proxy warnings:\n{report['proxy_warnings']}")
    return report


async def test_s5_mixed_source_fingerprints_converge(mixed_fleet):
    """S5: two proxies whose SOURCE FINGERPRINT differs, one state dir, 60s.

    Since F-886 the invariant is stronger than convergence: NOBODY IS EVICTED.
    The second proxy finds a backend it would not adopt, sees that it still owns
    a live browser, and spawns its own on a fresh port beside it — the
    ``STEP_ASIDE_MARKER`` line — so the recorded backend never changes hands,
    both sessions keep answering, and the log carries no lifecycle incident at
    all. ``server.json`` (schema v3) holds both backends under the one display
    context, which is what lets a THIRD proxy of either identity adopt its own
    rather than spawn a fourth.

    Its own workspace, deliberately: this node churns identities by
    construction, and doing that on the module's shared backend would make every
    earlier node's teardown a consequence of this one.

    WHAT WAS MEASURED BEFORE THE FIX (local Windows, 2.1.8, seven runs — six at
    the finding, one instrumented at 0.25s resolution): ONE wave every time. The
    variant proxy's cold-start lock evicted the same-source backend
    (``backend stale (source changed), evicting``, the only incident line in
    the run); the evicted backend's browser outlived it by 4.43s and was then
    reaped by the replacement's orphan recovery (``process_cleanup.recovery:
    Killed 1 orphaned browser processes``); the loser's later calls answered
    ``Session terminated`` (5 of 6) with no condemnation, no heal and no
    teardown in its log, because both death witnesses are PORT-scoped and the
    replacement bound the same port. Timelines, as seconds since the first
    proxy started paired with the recorded backend pid: ``[(4.9, A), (11.0,
    B)]``, ``[(4.8, A), (10.5, B)]``, ``[(4.8, A), (10.5, B)]``, ``[(5.1, A),
    (10.7, B)]``, ``[(4.8, A), (10.6, B)]``, ``[(7.6, A), (17.1, B)]``,
    ``[(7.4, A), (16.3, B)]``.

    WHAT IS MEASURED NOW. Zero waves, every run: the arriving proxy finds a
    backend it would not adopt, sees that it still owns a live browser, and
    spawns its own on a fresh port beside it — the ``STEP_ASIDE_MARKER`` line —
    so the recorded incumbent never changes hands, both sessions keep answering,
    and the whole run writes no lifecycle incident at all. ``server.json``
    (schema v3) ends holding BOTH backends under the one display context, both
    running, which is what lets a third proxy of either identity adopt rather
    than spawn a fourth. Local Windows: ``S5`` 61.4s, both S5 nodes plus the
    vocabulary pin 99.3s; the whole module 420.0s, 8 passed, 0 xfail.

    WHAT THIS NODE ASSERTS, and why each one. The two sides' fingerprints really
    differ (proven by the product's own digest in the fixture — without it a
    copy that failed to move the digest would run a same-source fleet and report
    "no eviction, browsers alive", a pass about nothing). Zero waves. No
    incident. At least one step-aside line, so a run that converged by some
    OTHER means cannot read as proof of this fix. Both recorded backends alive
    at the end — read through the harness's ``_backend_pids_from_state``, not
    the timeline, because the timeline follows the FIRST entry and the arriving
    proxy's backend is the second. Both sides had a browser, and both sides are
    still served.

    The stronger property "a newer install must take effect over an older
    running backend" is deliberately NOT here: it is the identity gate's own
    contract (``test_singleton_version_aware``), and this fleet has no "newer"
    side — only a different one. The rule lives in
    ``embedded/backend_eviction.py``; the two sites that ask it are
    ``singleton._select_backend_port`` (bind) and
    ``singleton._clear_stale_backend`` (kill); the write-up is
    ``audit/stage2/finding_F886_eviction_kills_sibling_browsers.md``.
    """
    report = mixed_fleet
    assert report["fingerprints"]["same"] != report["fingerprints"]["variant"]
    assert report["waves"] == 0, (
        f"a backend was evicted: {report['waves']} wave(s) in "
        f"{MIXED_VERSION_SECONDS:.0f}s (timeline "
        f"{report['backend_pid_timeline']}); each wave killed a backend's "
        f"browsers, which is the operator's 'all my browsers closed' report"
    )
    assert not report["incidents"], (
        f"lifecycle incident(s) during a mixed-fingerprint fleet: "
        f"{[(k, line) for k, _, line in report['incidents']]}"
    )
    assert report["stepped_aside"] >= 1, (
        "the second proxy did not take the F-886 step-aside path — no "
        f"'{STEP_ASIDE_MARKER}' line in any proxy log; the fleet converged by "
        "some other means and this node is no longer proving the fix.\n"
        f"{report['proxy_warnings']}"
    )
    assert sorted(report["recorded_at_end"].values()) == [True, True], (
        f"the fleet must end on TWO live recorded backends, one per source "
        f"identity; recorded_at_end={report['recorded_at_end']} "
        f"timeline={report['backend_pid_timeline']}"
    )
    # The fleet shape itself, so a run where one side never got a browser up
    # turns THIS node red rather than quietly weakening its sibling.
    assert set(report["browsers_alive"]) == {"same", "variant"}, (
        f"both sides must have had a browser; got {report['browsers_alive']} "
        f"(instances {report['instances']})"
    )
    assert report["served_at_end"] == {"same": "ok", "variant": "ok"}, (
        f"a session stopped being served: {report['served_at_end']}"
    )


async def test_s5_an_eviction_must_not_close_another_sessions_browser(mixed_fleet):
    """S5b: no browser spawned before a mixed-fingerprint fleet forms may die.

    This is the user's actual requirement, stated as an invariant: a browser
    belongs to the session that asked for it, and no other session's cold start
    may take it away. It was a strict ``xfail`` at 2.1.8 — the evicted backend's
    browser was killed on every run — and F-886 flipped it: the arriving
    proxy now refuses to evict a backend that still owns live browsers and
    spawns beside it instead.

    Deliberately asserted on the browser pid captured AT SPAWN rather than on
    the registry, because a winning backend used to rewrite ``browser_pids.json``
    and an absent entry would otherwise read as "nothing to check".

    Shares :func:`mixed_fleet` with the convergence node, so this costs no
    extra run.
    """
    report = mixed_fleet
    assert all(report["browsers_alive"].values()), (
        f"an eviction closed another session's browser: "
        f"{report['browsers_alive']} (sessions at the end: "
        f"{report['served_at_end']}; backend timeline "
        f"{report['backend_pid_timeline']}). The incident kinds logged were "
        f"{sorted({k for k, _, _ in report['incidents']})} and the proxy "
        f"warnings were:\n{report['proxy_warnings']}"
    )
