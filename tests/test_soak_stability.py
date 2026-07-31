"""2.0.1 stabilization — the real-Chrome, real-stdio SOAK stability gate.

The gap this closes: every existing transport node is a **single pass**. The
canonical journey (``test_e2e_transport.py``) spawns, does about a dozen things,
and closes; the cookie node does even less. Neither can answer the two questions
that actually decide whether this tool survives a long working session — *does
the browser stay up*, and *does anything hang* — because a wedge that needs a
throwing script, a dead host, a missing selector, or repeated tab churn to appear
has no chance to appear in twelve happy-path calls.

This module runs ~60 mixed operations against **one** spawned instance, with an
explicit per-call deadline on **every** one of them
(``release_gate_harness.SOAK_OP_TIMEOUT``). A single overdue reply fails by NAME
rather than hanging until pytest's outer timeout, so "nothing hangs" is a
measurement and not a hope. Each hostile operation (a script that throws, a host
that cannot resolve, a selector that does not exist, a tab closed out from under
nodriver's rediscovery path) is followed immediately by an ordinary call that
only a healthy instance can answer.

Deliberately tolerant where the product is mid-change: for the throwing script,
the unresolvable host, and the missing selector this module asserts only that a
**bounded reply arrived** and the instance survived — not which error shape came
back. The error convention for those paths is being tightened separately, and a
stability test must not pin either side of it. The observed shapes are printed
instead, so a silent change is still visible in the log.

Two collected nodes, ONE browser journey: the module-scoped ``soak_record``
fixture drives :func:`release_gate_harness.run_release_gate_journey` at its
``soak`` stage exactly once and both nodes assert on that record. They make
genuinely different claims (stability vs the F-805 characterization) and so may
not be one node — but neither is worth a second real-Chrome journey.

The machinery is the ONE release-gate harness (absolute installed launcher,
isolated HOME + session root, fixture app, stdio ``tools/call``, bounded teardown
behind the ``_pid_in_workspace`` ownership filter). There is no second journey
mechanism here.

Marked ``integration`` + ``transport``; skipped when Chrome / the server is
unavailable (same guard as the other e2e modules).
"""

from __future__ import annotations

import asyncio
import shutil
from collections import Counter

import pytest

from e2e_helpers import CAN_RUN
from release_gate_harness import (
    RESULT_SCHEMA_VERSION,
    SOAK_MISSING_SELECTOR_WAIT_MS,
    SOAK_OP_TIMEOUT,
    SOAK_STABILITY,
    gate_work_dir,
    resolve_launcher,
    run_release_gate_journey,
)

pytestmark = [pytest.mark.integration, pytest.mark.transport]

if not CAN_RUN:
    pytestmark.append(pytest.mark.skip("Chrome not available or server failed to load"))

# The soak's own floor on how much work it must actually have done. Not a
# stylistic minimum: if a future edit trims the journey down, the node would keep
# passing while proving progressively less, and nothing else in the suite would
# notice.
MIN_SOAK_OPS = 40

# F-805 — what a caller-honoured `wait_for_element(timeout=2000)` would cost.
# Halfway between the honest answer (~2s) and the observed one (~10.5s, nodriver's
# DEFAULT select timeout), so neither a fix nor the current bug lands near it.
F805_HONEST_WAIT_SECONDS = 6.0


@pytest.fixture(scope="module")
def soak_record(tmp_path_factory):
    """Drive the ONE soak journey once; both nodes below read its record.

    Synchronous on purpose: it owns its own loop for the single journey, so the
    two nodes share a real browser run without depending on an event-loop scope
    that the rest of the suite does not use.
    """
    launcher = resolve_launcher()  # this env's absolute installed console launcher
    tmp = tmp_path_factory.mktemp("soak")
    work_dir = gate_work_dir(tmp)  # RUNNER_TEMP on CI (see helper docstring)
    try:
        return asyncio.run(
            run_release_gate_journey(
                launcher=launcher, work_dir=work_dir, stages=SOAK_STABILITY
            )
        )
    finally:
        if work_dir != tmp:  # pytest cleans its own; this one is ours
            shutil.rmtree(work_dir, ignore_errors=True)


def _histogram(ops: list[dict]) -> str:
    """A fixed-bucket latency histogram of the soak's per-operation timings.

    Printed on PASS so successive CI logs carry a comparable stability profile: a
    release that starts spending seconds where it used to spend milliseconds is
    visible here long before it becomes a timeout.
    """
    buckets = [
        ("      < 0.1s", 0.0, 0.1),
        ("0.1s -  0.5s", 0.1, 0.5),
        ("0.5s -    1s", 0.5, 1.0),
        ("  1s -    3s", 1.0, 3.0),
        ("  3s -   10s", 3.0, 10.0),
        (" 10s -   30s", 10.0, SOAK_OP_TIMEOUT),
    ]
    seconds = [op["seconds"] for op in ops if "seconds" in op]
    lines = []
    for label, low, high in buckets:
        hits = [s for s in seconds if low <= s < high]
        lines.append(f"  {label} | {len(hits):3d} {'#' * min(len(hits), 60)}")
    return "\n".join(lines)


def test_real_transport_soak_one_instance_stays_up(soak_record, capsys):
    """~60 bounded mixed operations on ONE instance: it stays up, nothing hangs."""
    record = soak_record

    assert record["schema_version"] == RESULT_SCHEMA_VERSION
    assert record["transport"] == "stdio"
    assert record["stages"] == SOAK_STABILITY
    assert record["navigation_verified"] is True
    assert record["launcher"].endswith(
        ("stealth-chrome-devtools-mcp.exe", "stealth-chrome-devtools-mcp")
    )

    journey = record["journey"]
    ops = journey["ops"]
    latency = journey["latency"]

    # The soak really ran a soak's worth of work against ONE instance.
    assert journey["instance_id"]
    assert journey["op_count"] >= MIN_SOAK_OPS, (
        f"soak ran only {journey['op_count']} operations (floor {MIN_SOAK_OPS}) — "
        "the journey was trimmed and now proves less than it claims"
    )
    assert len(ops) == journey["op_count"]

    # THE hang assertion. The harness raises on the first operation that blows its
    # deadline, so reaching here already means none did; asserting it explicitly
    # keeps the claim readable and survives a change in how the harness reports.
    overdue = [op for op in ops if op.get("outcome") == "OVERDUE"]
    assert not overdue, (
        f"operations exceeded the {SOAK_OP_TIMEOUT}s deadline: {overdue}"
    )
    assert all("seconds" in op for op in ops), "an operation recorded no latency"
    assert latency["max_seconds"] < SOAK_OP_TIMEOUT, latency

    # Every hostile operation really ran (a silently skipped one would otherwise
    # leave this node passing while testing nothing) and produced a bounded reply.
    labels = {op["op"] for op in ops}
    assert {
        f"c{c}:execute_script_throwing" for c in range(1, journey["cycles"] + 1)
    } <= labels
    assert any(op.endswith(":navigate_bad_host") for op in labels)
    assert any(op.endswith(":list_tabs_after_close") for op in labels)
    assert any(op.endswith(":wait_for_missing_element") for op in labels)
    assert len(journey["throwing_script_shapes"]) == journey["cycles"]
    assert journey["bad_host_shapes"], "the unresolvable-host op never ran"

    # It shut down cleanly: registry empty, backend gone, no owned child left.
    assert journey["closed_cleanly"] is True
    assert record["backend_gone"] is True
    assert record["no_child_remaining"] is True

    missing = journey["missing_selector"]
    summary = (
        "\n[soak] one instance, "
        f"{journey['op_count']} bounded operations across {journey['cycles']} cycles\n"
        f"[soak] per-op deadline {SOAK_OP_TIMEOUT}s | "
        f"max {latency['max_seconds']}s | p95 {latency['p95_seconds']}s | "
        f"p50 {latency['p50_seconds']}s | tool time {latency['total_seconds']}s\n"
        f"{_histogram(ops)}\n"
        f"[soak] outcomes: {dict(Counter(op['outcome'] for op in ops))}\n"
        f"[soak] throwing-script replies: {journey['throwing_script_shapes']}\n"
        f"[soak] unresolvable-host replies: {journey['bad_host_shapes']}\n"
        f"[soak] missing selector (F-805): {missing}\n"
        "[soak] slowest operations:\n"
        + "\n".join(f"  {op['seconds']:7.3f}s  {op['op']}" for op in latency["slowest"])
    )
    with capsys.disabled():  # so the profile lands in the CI log on PASS too
        print(summary)


@pytest.mark.characterization
@pytest.mark.xfail(
    strict=True,
    reason=(
        "F-805: wait_for_element and click_element ignore the caller's timeout for "
        "a selector that never resolves — both spend nodriver's DEFAULT 10s "
        "tab.select wait, because element_resolution.resolve_element is called "
        "with timeout=None. Bounded, so not a hang, but a 2000ms request costs "
        "~10.5s. See audit/stage2/finding_F805_missing_selector_ignores_caller_"
        "timeout.md — this node turns RED (XPASS) the moment it is fixed."
    ),
)
def test_missing_selector_calls_honour_the_caller_timeout(soak_record):
    """The honest contract for a selector that will never exist.

    Deliberately NOT folded into the stability node above: that node's claim is
    "nothing hangs", which this behaviour does not violate — ~10.5s is slow, not
    unbounded. Pinning the honest bound separately keeps the stability claim
    green and truthful while the defect stays visible and dated.
    """
    missing = soak_record["journey"]["missing_selector"]
    assert missing["requested_wait_seconds"] == SOAK_MISSING_SELECTOR_WAIT_MS / 1000
    assert missing["wait_seconds"] <= F805_HONEST_WAIT_SECONDS, (
        f"wait_for_element(timeout={SOAK_MISSING_SELECTOR_WAIT_MS}ms) took "
        f"{missing['wait_seconds']}s"
    )
    assert missing["click_seconds"] <= F805_HONEST_WAIT_SECONDS, (
        f"click_element on a missing selector took {missing['click_seconds']}s"
    )
