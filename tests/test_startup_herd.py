"""Startup herd — 40 concurrent Claude Code sessions must all be up in 30s.

The claim under test is the singleton architecture's founding promise
(``singleton.py``: "When multiple Claude Code sessions start simultaneously"):
every stdio proxy answers ``initialize`` locally and instantly, exactly one
backend cold-starts under the file lock, and everyone else converges on it.
Every existing transport node starts ONE client, so the promise has never been
measured at the scale it was written for. This module starts **40 launcher
processes at once against a cold workspace** — the "40+ Claude instances"
deployment shape — and requires the whole herd to finish ``initialize`` AND a
real ``tools/list`` (which, unlike the locally-answered handshake, genuinely
waits on the backend) within 30 seconds.

What a red here means, by symptom:

* the whole herd slow — proxy startup cost (imports, ``_source_fingerprint``)
  compounds under CPU contention; the per-process price is the suspect;
* a few stragglers — the lock race or the readiness-poll backoff left someone
  behind (the F-509 window where a half-born backend's port can be
  misclassified as foreign lives here);
* more than one backend counted — the exclusive lock failed at its one job.

No Chrome is spawned: ``tools/list`` needs the backend up, not a browser, so
the herd is cheap enough to run everywhere. The machinery is the ONE release
gate harness (:func:`release_gate_harness.gate_workspace`: isolated HOME +
``server.json``, distinct singleton port, owned teardown) — a source-built
herd can never evict a developer's real backend.

Marked ``integration`` + ``transport``; skipped when the server is unavailable
(same guard as the other transport nodes).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from typing import TYPE_CHECKING

import psutil
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from e2e_helpers import CAN_RUN
from release_gate_harness import (
    INIT_TIMEOUT,
    REGISTRY_TOOL_COUNT,
    gate_work_dir,
    gate_workspace,
    resolve_launcher,
    workspace_backend_logs,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.transport]

if not CAN_RUN:
    pytestmark.append(pytest.mark.skip("Chrome not available or server failed to load"))

# The deployment shape the user actually runs: a fleet of Claude Code sessions
# starting together, each spawning its own stdio proxy. The full 40 is a
# workstation-class claim; hosted CI cells have 3-4 cores and would spend the
# whole budget just starting interpreters, so they run a reduced fleet against
# the SAME invariants (cf. the Linux headed-sizing skip: a premise the lane's
# hardware cannot express belongs where the hardware exists).
HERD_SIZE = 12 if os.environ.get("CI") else 40
# The spec: the ENTIRE herd — cold backend start included — is usable within
# this. Chosen to match Claude Code's own 30s MCP connect timeout: if the herd
# fits, no individual session can have timed out.
HERD_DEADLINE_SECONDS = 30.0
# Joining an already-running backend is the every-later-session path; it must
# cost a process spawn plus probes, nowhere near the cold-start budget.
WARM_JOIN_DEADLINE_SECONDS = 10.0
# The wire-level backstop so a wedged herd fails by name instead of hanging
# the lane until pytest's outer timeout. Generous on purpose: the assertion
# that matters is the 30s one, measured below.
HERD_HARD_TIMEOUT_SECONDS = 240.0


def _summary(times: list[dict]) -> str:
    done = [t for t in times if t is not None]
    if not done:
        return "no session completed"
    inits = sorted(t["initialize_s"] for t in done)
    listed = sorted(t["tools_list_s"] for t in done)

    def pct(values: list[float], p: float) -> float:
        return values[min(len(values) - 1, int(p * len(values)))]

    return (
        f"{len(done)}/{len(times)} sessions | "
        f"initialize p50={pct(inits, 0.5):.2f}s p95={pct(inits, 0.95):.2f}s "
        f"max={inits[-1]:.2f}s | "
        f"tools/list p50={pct(listed, 0.5):.2f}s p95={pct(listed, 0.95):.2f}s "
        f"max={listed[-1]:.2f}s"
    )


def _our_backends_on_port(port: int) -> list[int]:
    """Pids of OUR *logical* backends for ``port`` — must be exactly one.

    Identified by cmdline (module + ``--transport http`` + the workspace's own
    port), the same identity ``singleton._is_our_backend`` uses, so a
    developer's real backend on another port is never counted. Counted by
    process-tree ROOT: on Windows a uv-managed venv's ``python.exe`` is a
    trampoline, so the spawned pid is a shim whose child — same command line —
    is the real interpreter. Two matching processes, one backend; a match
    whose parent also matches is therefore the same backend, not a second one.
    """
    matches: dict[int, psutil.Process] = {}
    for proc in psutil.process_iter(["cmdline"]):
        try:
            joined = " ".join(proc.info.get("cmdline") or [])
        except psutil.Error:
            continue
        if (
            "stealth_chrome_devtools_mcp" in joined
            and "--transport http" in joined
            and f"--port {port}" in joined
        ):
            matches[proc.pid] = proc
    roots: list[int] = []
    for pid, proc in matches.items():
        try:
            if proc.ppid() in matches:
                continue  # trampoline child of a counted shim
        except psutil.Error:
            pass
        roots.append(pid)
    return roots


async def _one_session(launcher: Path, space: dict, herd_t0: float, slot: list) -> None:
    """One simulated Claude Code session: spawn the launcher, complete the
    ``initialize`` handshake, then a real ``tools/list`` (the first request
    that genuinely waits on the backend). Timestamps are relative to the
    HERD's start, because "all instances up in 30s" is a fleet clock, not a
    per-process one.
    """
    transport = StdioTransport(
        command=str(launcher),
        args=["--singleton-port", str(space["port"])],
        env=space["env"],
        keep_alive=False,
    )
    async with Client(transport, init_timeout=INIT_TIMEOUT) as client:
        initialize_s = time.monotonic() - herd_t0
        tools = await client.list_tools()
        slot[0] = {
            "initialize_s": initialize_s,
            "tools_list_s": time.monotonic() - herd_t0,
            "tool_count": len(tools),
        }


async def test_forty_cold_sessions_are_all_usable_within_30s(tmp_path):
    """THE herd pin: 40 simultaneous cold starts, one backend, 30s to usable."""
    launcher = resolve_launcher()
    work_dir = gate_work_dir(tmp_path)
    try:
        with gate_workspace(work_dir) as space:
            slots: list[list] = [[None] for _ in range(HERD_SIZE)]
            herd_t0 = time.monotonic()
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        _one_session(launcher, space, herd_t0, slots[i])
                        for i in range(HERD_SIZE)
                    )
                ),
                timeout=HERD_HARD_TIMEOUT_SECONDS,
            )
            herd_seconds = time.monotonic() - herd_t0
            results = [slot[0] for slot in slots]
            backends = _our_backends_on_port(space["port"])

            # Exactly one backend survived the 40-way lock race. Counted while
            # the workspace is still alive; teardown below then owns its exit.
            assert len(backends) == 1, (
                f"expected exactly one backend for port {space['port']}, "
                f"found pids {backends}\n{workspace_backend_logs(space)}"
            )

            # Every session is genuinely usable — the full registry answered,
            # not just the proxy's local handshake.
            assert all(r is not None for r in results), _summary(results)
            assert {r["tool_count"] for r in results} == {REGISTRY_TOOL_COUNT}

            # The spec itself.
            assert herd_seconds <= HERD_DEADLINE_SECONDS, (
                f"herd took {herd_seconds:.1f}s (> {HERD_DEADLINE_SECONDS:.0f}s): "
                f"{_summary(results)}\n{workspace_backend_logs(space)}"
            )

            # A 41st session joining the warm backend pays only its own spawn.
            warm_t0 = time.monotonic()
            warm_slot: list = [None]
            await asyncio.wait_for(
                _one_session(launcher, space, warm_t0, warm_slot),
                timeout=HERD_HARD_TIMEOUT_SECONDS,
            )
            warm_seconds = time.monotonic() - warm_t0
            assert warm_seconds <= WARM_JOIN_DEADLINE_SECONDS, (
                f"warm join took {warm_seconds:.1f}s "
                f"(> {WARM_JOIN_DEADLINE_SECONDS:.0f}s)"
            )

            print(
                f"\nherd={herd_seconds:.1f}s warm_join={warm_seconds:.1f}s | "
                f"{_summary(results)}"
            )
    finally:
        if work_dir != tmp_path:  # pytest cleans its own; this one is ours
            shutil.rmtree(work_dir, ignore_errors=True)
