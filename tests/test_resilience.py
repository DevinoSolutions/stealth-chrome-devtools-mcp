"""plan_RELEASE W10 (MQ-126…129) — injected faults against real Chrome.

W8's negative space is static: a disabled button, a readonly field. The edge
cases users actually hit are **dynamic** — the browser dies, a tab vanishes
under a running tool, the transfer stops halfway, the connection drops. This
module injects those four faults and asks one question of each: does the tool
**fail in a typed, bounded, recoverable way**, or does it hang, surface a raw
CDP error, or quietly return success?

Three properties make a node here evidence rather than a smoke test:

*Every wait is bounded twice.* The product's own deadline is set strictly
inside :data:`OUTER_BOUND`, the harness bound. If the harness bound is what
fires, the product did not answer and the node fails naming what hung — a hang
can never be reported as a pass, and a failing node can never wedge the run.

*Every fault is controlled, not timed.* The hanging and slow routes are
``tests/fixture_routes.py`` controllers addressed by a token this module mints.
``entered`` is the barrier — the fixture says the request really arrived, so a
fault is injected into an operation that has demonstrably started — and
``/fault/release`` is the only thing that ends the wait. No sleep is ever a
synchronization point.

*Every timeout node has a sensitivity control.* The same slow route, released
inside the product deadline, must COMPLETE. Without it, "the tool timed out"
is equally consistent with "this route never works", and the assertion proves
nothing.

Recovery invariant. After every fault the server must still be usable: the
instance (or a fresh one) drives a normal navigation, ``close_instance``
succeeds, the instance leaves the tracked-process table, and no Chrome process
from its tree survives. A fault that leaves the server wedged is the finding.

Scope honesty. These nodes drive the in-process ``.fn`` seam against real
image-provided Chrome Stable, which is what puts them in the ``integration``
lane on all three W2 runners. The stdio wire path is W1's separate claim
(``tests/test_e2e_transport.py``); nothing here re-makes it. Because the seam
returns one awaitable per call, "exactly one terminal outcome" is asserted at
the call boundary, not over JSON-RPC request ids — MQ-127 says so in words.

What the faults actually found. Two of the four recover cleanly and two do
not, and W10's job is to say which is which rather than to pick assertions that
pass:

* **MQ-127** (a tab vanishes) and **MQ-129** (the connection drops mid-body)
  hold the full contract, including recovery, and are `satisfied`.
* **MQ-126** (the browser is killed) fails on one point: the very call the
  product's own error message tells you to make, ``close_instance``, returns
  ``False`` for a browser that is provably gone (F-789). Everything else — no
  orphan, removable profile, working fresh spawn — does hold.
* **MQ-128** (navigation deadlines) times out exactly as specified, on the
  product's own deadline, with the M6-pinned message. But a timed-out
  navigation leaves the instance's CDP connection permanently wedged (F-788),
  so the "then prove a normal navigation succeeds" half cannot be claimed.

Both are pinned as characterizations and routed, never fixed: `src/` edits are
a plan_RELEASE non-goal (§1.2), and a characterization can never satisfy an MQ
(§0.2). MQ-126 and MQ-128 are therefore `planned` in the parity manifest and
are NOT bound to any ``--mq`` id.

MQ binding. Four faults, eight nodes; only the two whose contract holds are
bound:

===========  ================================================================
MQ           node
===========  ================================================================
``MQ-127``   ``test_tab_closed_under_a_running_tool_has_one_terminal_outcome``
``MQ-129``   ``test_route_abort_mid_navigation_is_bounded_and_recoverable``
===========  ================================================================

The ids are bound to runtime evidence by the ``--mq`` flags on the
``integration`` cell's ``release_evidence.py emit`` step in
``.github/workflows/release-gate.yml``. That ledger, not this docstring, is
what W8 resolves against; this table exists so the two cannot silently
disagree.

The other six nodes are current support, not acceptance:
``test_slow_success_control_completes_when_released`` and the two
``..._times_out_with_the_pinned_message`` nodes are real assertions that MQ-128
will rest on once F-788 closes;
``test_crash_recovery_after_the_owned_chrome_is_killed`` (F-789),
``test_a_navigation_timeout_wedges_the_instance_connection`` (F-788) and
``test_networkidle_returns_before_the_transfer_completes`` (F-787) are
characterization pins. Each pin asserts in the direction that makes a FIX go
red, so closing any of these findings forces a deliberate test update.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import psutil
import pytest
import requests

import fixture_routes as fr
from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

pytestmark = integration_pytestmark()

HTTP_TIMEOUT = 10
# The harness bound. Never a product deadline: if THIS fires, the tool did not
# answer. Sized well above the worst legitimate path (navigate retries once, so
# a timing-out navigation costs ~2x its own deadline) and well under the gate's
# per-test ceiling, so a wedge is reported as a failure rather than a kill.
OUTER_BOUND = 90.0
# The product deadline, strictly inside OUTER_BOUND on both attempts.
NAV_TIMEOUT_MS = 4000
BARRIER_TIMEOUT = 20.0  # how long the fixture may take to say "request arrived"
REAP_TIMEOUT = 25.0  # how long the OS may take to reap a killed Chrome tree


# ── Fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


@pytest.fixture()
async def instance():
    """One headless instance per node. The node asserts its own clean close;
    this ``finally`` is only the net that stops a failed node leaking Chrome."""
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    result = await spawn(headless=True, **sandbox_kwargs())
    iid = result["instance_id"]
    try:
        yield iid
    finally:
        with contextlib.suppress(Exception):
            await close(instance_id=iid)


# ── Fault-controller client (the fixture's barrier, never a sleep) ──────────
def _token(name: str) -> str:
    """A token unique to this node AND run — the fixture server is session
    scoped, so two nodes must never address one controller."""
    return f"w10-{name}-{uuid.uuid4().hex[:8]}"


async def _http_get(url: str):
    """Plain HTTP straight from this process, off-thread so it never blocks the
    loop driving Chrome."""
    return await asyncio.to_thread(requests.get, url, timeout=HTTP_TIMEOUT)


async def _arm(base: str, token: str) -> None:
    armed = (await _http_get(f"{base}/fault/arm?token={token}")).json()
    assert armed["token"] == token and armed["entered"] is False, armed


async def _fault_status(base: str, token: str) -> dict:
    return (await _http_get(f"{base}/fault/status?token={token}")).json()


async def _await_entered(base: str, token: str) -> dict:
    """Bounded poll on the fixture's ``entered`` barrier.

    Returning means the SERVER has the request: whatever fault the caller
    injects next lands on an operation that has demonstrably started, rather
    than racing it.
    """
    deadline = time.monotonic() + BARRIER_TIMEOUT
    while time.monotonic() < deadline:
        snapshot = await _fault_status(base, token)
        if snapshot["entered"]:
            return snapshot
        await asyncio.sleep(0.05)
    raise AssertionError(f"the fixture never entered fault {token!r}")


async def _release(base: str, token: str) -> dict:
    released = (await _http_get(f"{base}/fault/release?token={token}")).json()
    assert released["released"] is True, released
    return released


# ── Outer bound + terminal-outcome capture ──────────────────────────────────
async def _terminal(coro, what: str) -> tuple[str, object, float]:
    """Await *coro* under :data:`OUTER_BOUND` and return its ONE terminal outcome.

    ``("returned", value, elapsed)`` or ``("raised", exception, elapsed)``. The
    harness bound firing is never one of them: that is an unbounded product
    path, and it fails the node by name instead of being swallowed as a result.
    """
    started = time.monotonic()
    try:
        value = await asyncio.wait_for(coro, OUTER_BOUND)
    except TimeoutError as exc:
        elapsed = time.monotonic() - started
        # asyncio.wait_for and the product both speak TimeoutError; only elapsed
        # distinguishes "we cut it off" from "it answered with one".
        if elapsed >= OUTER_BOUND - 1.0:
            raise AssertionError(
                f"{what} did not terminate inside the {OUTER_BOUND}s outer bound"
            ) from exc
        return ("raised", exc, elapsed)
    except Exception as exc:
        return ("raised", exc, time.monotonic() - started)
    return ("returned", value, time.monotonic() - started)


# ── Process-level oracles (independent of anything the tools report) ────────
def _tracked(iid: str) -> dict:
    return dict(process_cleanup.browser_processes.get(iid) or {})


def _tree_pids(pid: object) -> list[int]:
    """The live Chrome process tree rooted at *pid*, captured while it exists."""
    if not isinstance(pid, int) or not psutil.pid_exists(pid):
        return []
    try:
        return [pid, *[child.pid for child in psutil.Process(pid).children(True)]]
    except psutil.Error:
        return [pid]


def _kill_tree(pids: list[int]) -> None:
    for pid in pids:
        with contextlib.suppress(psutil.Error):
            psutil.Process(pid).kill()


async def _await_pids_gone(pids: list[int], timeout: float) -> list[int]:
    deadline = time.monotonic() + timeout
    survivors = [pid for pid in pids if psutil.pid_exists(pid)]
    while survivors and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        survivors = [pid for pid in pids if psutil.pid_exists(pid)]
    return survivors


# ── The recovery invariant, asserted after EVERY fault ──────────────────────
async def _assert_still_drivable(iid: str, base: str) -> None:
    """The same instance still does ordinary work through the public tools."""
    result = await navigate_and_settle(iid, f"{base}/index.html")
    assert result["success"] is True, result
    assert await eval_js(iid, "document.getElementById('sentinel').textContent") == (
        "fixture-index-page"
    )


async def _assert_close_leaves_nothing(iid: str) -> None:
    """``close_instance`` succeeds, untracks, and reaps the whole Chrome tree."""
    close = get_fn("close_instance")
    tree = _tree_pids(_tracked(iid).get("pid"))
    outcome, value, _ = await _terminal(close(instance_id=iid), f"close({iid})")
    assert outcome == "returned", value
    assert value is True, value
    assert iid not in process_cleanup.get_tracked_processes()
    assert await _await_pids_gone(tree, REAP_TIMEOUT) == [], (
        f"close left orphaned chrome process(es) from {tree}"
    )


async def _assert_fresh_spawn_works(base: str) -> None:
    """A brand-new instance spawns, navigates, and closes after the fault."""
    spawn = get_fn("spawn_browser")
    result = await spawn(headless=True, **sandbox_kwargs())
    fresh = result["instance_id"]
    try:
        await _assert_still_drivable(fresh, base)
    finally:
        await _assert_close_leaves_nothing(fresh)


async def _assert_recovered(iid: str, base: str) -> None:
    """The full invariant for a fault the instance was expected to survive."""
    await _assert_still_drivable(iid, base)
    await _assert_close_leaves_nothing(iid)
    await _assert_fresh_spawn_works(base)


async def _reap(iid: str) -> None:
    """Close *iid* best-effort, then make sure no Chrome from it survives.

    Used by the nodes that deliberately leave the instance wedged (F-788): the
    product cannot be relied on to tear it down, and a resilience suite that
    leaked a Chrome tree per node would be its own worst finding. The final
    assertion is unconditional, so a leak still fails the node.
    """
    close = get_fn("close_instance")
    tree = _tree_pids(_tracked(iid).get("pid"))
    with contextlib.suppress(Exception):
        await asyncio.wait_for(close(instance_id=iid), OUTER_BOUND)
    survivors = await _await_pids_gone(tree, 5.0)
    if survivors:
        _kill_tree(survivors)
    assert await _await_pids_gone(tree, REAP_TIMEOUT) == [], (
        f"chrome from the wedged instance survived even a direct kill: {tree}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# MQ-126 — crash recovery: the owned Chrome is killed mid-session
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.characterization
async def test_crash_recovery_after_the_owned_chrome_is_killed(
    instance, fixture_app_server
):
    """PINS CURRENT BEHAVIOR incl. known quirk F-789; update deliberately when
    it lands. Killing the owned Chrome tree mid-session, then calling the next
    tool, gives a bounded typed failure with an actionable message — and that
    message tells the caller to "close the instance with close_instance and
    spawn a new one". ``close_instance`` then returns **False**.

    Everything else the MQ-126 recovery contract asks for does hold, and is
    asserted here so a regression in any of it is caught: no process from the
    killed tree survives, the crashed instance's profile is removable, and a
    freshly spawned instance navigates and closes cleanly. Only the documented
    recovery call's own return value is wrong, which is why this is a pin and
    why MQ-126 is not satisfied at HEAD.

    The killed tree is enumerated from ``process_cleanup``'s OWN tracking table
    before the kill, so "the process we killed is the one the product thinks it
    owns" is a fact rather than a name match. Confirmed exit is awaited before
    the next call — otherwise the tool might merely be racing a dying browser.
    """
    base = fixture_app_server
    navigate = get_fn("navigate")
    close = get_fn("close_instance")

    await _assert_still_drivable(instance, base)

    metadata = _tracked(instance)
    root_pid = metadata.get("pid")
    if not isinstance(root_pid, int) or not psutil.pid_exists(root_pid):
        pytest.skip("no tracked root pid to crash")
    profile_dir = metadata.get("user_data_dir")
    tree = _tree_pids(root_pid)

    _kill_tree(tree)
    assert await _await_pids_gone(tree, REAP_TIMEOUT) == [], (
        "the owned Chrome tree did not exit; the fault was never injected"
    )

    outcome, value, elapsed = await _terminal(
        navigate(
            instance_id=instance,
            url=f"{base}/index.html",
            timeout=NAV_TIMEOUT_MS,
        ),
        "navigate after the browser was killed",
    )
    assert elapsed < OUTER_BOUND
    assert outcome == "raised", (
        f"navigate reported SUCCESS against a killed browser: {value!r}"
    )
    assert isinstance(value, Exception)
    assert str(value), "the failure carried no message for the caller to act on"

    # F-789: the documented recovery call reports failure for a browser that is
    # provably gone. Pinned, not fixed — src edits are a plan_RELEASE non-goal.
    closed_outcome, closed, _ = await _terminal(
        close(instance_id=instance), f"close({instance}) after the crash"
    )
    assert closed_outcome == "returned", closed
    assert closed is False, (
        "close_instance now reports success for a crashed browser — F-789 is "
        "fixed and MQ-126 can be promoted from planned to satisfied"
    )

    # The rest of the recovery contract DOES hold, and is pinned so it cannot
    # silently regress behind the one known defect.
    assert await _await_pids_gone(tree, REAP_TIMEOUT) == []
    await _assert_fresh_spawn_works(base)
    if profile_dir and _is_per_instance_clone(profile_dir):
        removable = await asyncio.to_thread(_remove_dir_if_present, profile_dir)
        assert removable, (
            f"the crashed instance's profile is not removable: {profile_dir}"
        )


def _is_per_instance_clone(path: str) -> bool:
    """Only a per-instance CLONE may be deleted by this suite (F-841).

    An uncontended spawn opens the shared MASTER profile directly
    (``profile_role: "master"``), and ``metadata["user_data_dir"]`` then names
    the operator's real master — which this suite once ``rmtree``'d on a quiet
    machine (2026-08-31; restored from ``master-snapshot``). The lane only ever
    looked safe because a busy machine's live Chrome held the master and forced
    every test spawn onto a clone. "The crashed instance's profile is
    removable" is a contract about the disposable clone the spawn created, so
    the check now runs only when the instance actually got one: clones live
    under the session root's ``sessions/`` directory; the master (and anything
    else) is never this test's to delete.
    """
    from pathlib import Path

    return "sessions" in Path(path).parts


def _remove_dir_if_present(path: str) -> bool:
    """True when *path* is absent, or was removed. Cross-platform on purpose:
    a Windows handle still held by a surviving Chrome is exactly the failure
    this asserts against."""
    import shutil
    from pathlib import Path

    target = Path(path)
    if not target.exists():
        return True
    shutil.rmtree(target, ignore_errors=True)
    return not target.exists()


# ═══════════════════════════════════════════════════════════════════════════
# MQ-127 — a tab is closed out of band under a running tool
# ═══════════════════════════════════════════════════════════════════════════
async def test_tab_closed_under_a_running_tool_has_one_terminal_outcome(
    instance, fixture_app_server
):
    """MQ-127: with the fixture's ``entered`` barrier proving the navigation has
    started, close its tab out of band; the call must produce exactly ONE
    terminal outcome that is not a silent success, and the instance must still
    serve another tab operation and close cleanly.

    Scope: "one terminal outcome" is asserted at the tool-call boundary (the
    seam returns a single awaitable). No claim is made about JSON-RPC response
    framing for one request id — that is W13's surface.
    """
    base = fixture_app_server
    navigate = get_fn("navigate")
    new_tab = get_fn("new_tab")
    switch_tab = get_fn("switch_tab")
    close_tab = get_fn("close_tab")
    token = _token("tabclose")

    await _assert_still_drivable(instance, base)
    victim = await new_tab(instance_id=instance, url=f"{base}/index.html")
    assert await switch_tab(instance_id=instance, tab_id=victim["tab_id"]) is True

    await _arm(base, token)
    running = asyncio.ensure_future(
        navigate(
            instance_id=instance,
            url=f"{base}/fault/slow?token={token}",
            timeout=NAV_TIMEOUT_MS,
        )
    )
    try:
        await _await_entered(base, token)
        # The fault: the tab the navigation is running on disappears.
        await _terminal(
            close_tab(instance_id=instance, tab_id=victim["tab_id"]),
            "close_tab under a running navigation",
        )
        await _release(base, token)
        outcome, value, elapsed = await _terminal(
            running, "navigate whose tab was closed under it"
        )
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await running

    assert elapsed < OUTER_BOUND
    if outcome == "returned":
        # A success is only honest if the navigation really landed on the page.
        assert isinstance(value, dict), value
        assert value.get("url", "").endswith(f"token={token}"), (
            f"the tool reported success for a tab that no longer exists: {value!r}"
        )
    else:
        assert isinstance(value, Exception)
        assert str(value), "the failure carried no message for the caller to act on"

    # Recovery: another tab operation works, and the instance closes cleanly.
    survivor = await new_tab(instance_id=instance, url=f"{base}/index.html")
    assert survivor["tab_id"] != victim["tab_id"]
    assert await switch_tab(instance_id=instance, tab_id=survivor["tab_id"]) is True
    await _assert_still_drivable(instance, base)
    await _assert_close_leaves_nothing(instance)
    await _assert_fresh_spawn_works(base)


# ═══════════════════════════════════════════════════════════════════════════
# MQ-128 — load / networkidle against the controlled hang phases
# ═══════════════════════════════════════════════════════════════════════════
async def test_slow_success_control_completes_when_released(
    instance, fixture_app_server
):
    """MQ-128 (control): the SAME route the timeout nodes hang on, released
    inside the product deadline, completes and serves its exact body.

    This is what makes the two timeout nodes a measurement. Without it, "the
    navigation timed out" is equally consistent with "this route never works".
    """
    base = fixture_app_server
    navigate = get_fn("navigate")
    token = _token("control")

    await _arm(base, token)
    running = asyncio.ensure_future(
        navigate(
            instance_id=instance,
            url=f"{base}/fault/slow?token={token}",
            timeout=NAV_TIMEOUT_MS,
        )
    )
    try:
        await _await_entered(base, token)
        await _release(base, token)
        outcome, value, elapsed = await _terminal(running, "released slow navigation")
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await running

    assert outcome == "returned", f"the released control failed: {value!r}"
    assert value["success"] is True, value
    assert elapsed < NAV_TIMEOUT_MS / 1000, (
        f"the control took {elapsed:.2f}s — it did not beat the product deadline"
    )
    assert (
        await eval_js(instance, "document.getElementById('slow-body').textContent")
        == fr.FAULT_SLOW_BODY
    )

    await _assert_recovered(instance, base)


async def _assert_hang_times_out(instance, base: str, wait_until: str) -> None:
    """Navigate at a route that never sends a byte and pin the exact failure.

    The message is asserted byte-for-byte, not matched loosely: it is the M6
    pin, and a looser assertion would let a reworded (or differently caused)
    failure keep the node green.
    """
    navigate = get_fn("navigate")
    token = _token(f"hang-{wait_until}")
    url = f"{base}/fault/hang-before-headers?token={token}"

    await _arm(base, token)
    running = asyncio.ensure_future(
        navigate(
            instance_id=instance,
            url=url,
            wait_until=wait_until,
            timeout=NAV_TIMEOUT_MS,
        )
    )
    try:
        await _await_entered(base, token)
        outcome, value, elapsed = await _terminal(
            running,
            f"navigate(wait_until={wait_until!r}) at a hang-before-headers route",
        )
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await running
        await _release(base, token)

    assert outcome == "raised", (
        f"a route that never answered produced a SUCCESS: {value!r}"
    )
    assert elapsed >= NAV_TIMEOUT_MS / 1000, (
        f"the tool gave up after {elapsed:.2f}s, before its own {NAV_TIMEOUT_MS}ms "
        "deadline — the deadline is not what ended it"
    )
    assert elapsed < OUTER_BOUND
    assert str(value) == f"Navigation to {url} timed out after {NAV_TIMEOUT_MS}ms", (
        f"M6-pinned navigation-timeout message changed: {str(value)!r}"
    )


async def test_load_wait_against_a_hang_times_out_with_the_pinned_message(
    instance, fixture_app_server
):
    """MQ-128 (load): ``wait_until='load'`` at a route that never sends a byte
    fails on the product's own deadline, inside the outer bound, with the
    M6-pinned message byte-for-byte.

    Recovery is NOT asserted here: a timed-out navigation wedges the instance's
    CDP connection (F-788), which
    ``test_a_navigation_timeout_wedges_the_instance_connection`` pins. That is
    why MQ-128 is `planned` rather than satisfied at HEAD.
    """
    base = fixture_app_server
    await _assert_hang_times_out(instance, base, "load")
    await _reap(instance)


async def test_networkidle_wait_against_a_hang_times_out_with_the_pinned_message(
    instance, fixture_app_server
):
    """MQ-128 (networkidle): the same, for the other advertised wait condition.

    Scope: this qualifies ``networkidle`` only as a wait CONDITION that honours
    the navigation deadline. It makes no claim that ``networkidle`` waits for
    network idleness — see ``test_networkidle_returns_before_the_transfer_
    completes``, which pins that it does not (F-787).
    """
    base = fixture_app_server
    await _assert_hang_times_out(instance, base, "networkidle")
    await _reap(instance)


@pytest.mark.characterization
async def test_a_navigation_timeout_wedges_the_instance_connection(
    instance, fixture_app_server
):
    """PINS CURRENT BEHAVIOR incl. known quirk F-788; update deliberately when
    it lands. After a navigation times out at a route that never answered, the
    instance is permanently unusable: the NEXT navigation does not succeed, it
    fails with the generic CDP-operation-timeout message after the full
    ``_with_cdp_timeout`` budget.

    Cause (recorded, not fixed): the navigation deadline cancels ``tab.get``
    mid-transaction; when Chrome later answers, nodriver's single connection
    listener dies resolving the cancelled transaction, after which no CDP
    future on that connection is ever resolved again. The product's own timeout
    wrapper is the only reason callers stay bounded instead of hanging.

    This is the fault that "leaves the server wedged", so it is the finding —
    and it is exactly why MQ-128's recovery half cannot be claimed at HEAD.
    """
    base = fixture_app_server
    navigate = get_fn("navigate")

    await _assert_hang_times_out(instance, base, "load")

    outcome, value, elapsed = await _terminal(
        navigate(instance_id=instance, url=f"{base}/index.html"),
        "the navigation AFTER a timed-out navigation",
    )
    assert outcome == "raised", (
        "a normal navigation succeeded after a timeout — F-788 is fixed and "
        "MQ-128 can be promoted from planned to satisfied"
    )
    assert str(value).startswith("CDP operation timed out after "), str(value)
    assert f"(instance {instance})" in str(value), str(value)
    assert elapsed < OUTER_BOUND

    await _reap(instance)


@pytest.mark.characterization
async def test_networkidle_returns_before_the_transfer_completes(
    instance, fixture_app_server
):
    """PINS CURRENT BEHAVIOR incl. known quirk F-787; update deliberately when
    it lands. ``wait_until='networkidle'`` is implemented as a fixed short
    sleep, not as a network-quiescence wait: against a route whose body is
    still mid-transfer it returns SUCCESS while the document is provably
    incomplete (the release-only ``#complete`` node is absent).

    Recorded here so W5 can narrow the claim, and so a real networkidle
    implementation surfaces as a deliberate test update. A characterization
    never satisfies MQ-128.
    """
    base = fixture_app_server
    navigate = get_fn("navigate")
    token = _token("idle-partial")
    url = f"{base}/fault/hang-after-headers?token={token}"

    await _arm(base, token)
    running = asyncio.ensure_future(
        navigate(
            instance_id=instance,
            url=url,
            wait_until="networkidle",
            timeout=NAV_TIMEOUT_MS,
        )
    )
    try:
        await _await_entered(base, token)
        outcome, value, elapsed = await _terminal(
            running, "navigate(wait_until='networkidle') at a mid-transfer route"
        )
        assert outcome == "returned", value
        assert value["success"] is True, value
        assert elapsed < NAV_TIMEOUT_MS / 1000
        # The transfer is still open: the tail chunk only exists after a release.
        assert (await _fault_status(base, token))["released"] is False
        partial = await eval_js(
            instance, "document.getElementById('partial').textContent"
        )
        assert partial == fr.FAULT_PARTIAL_PREFIX
        complete = await eval_js(instance, "!!document.getElementById('complete')")
        assert complete is False, (
            "the body completed — the characterization no longer reproduces"
        )
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await running
        await _release(base, token)

    await _assert_recovered(instance, base)


# ═══════════════════════════════════════════════════════════════════════════
# MQ-129 — connectivity is cut mid-operation
# ═══════════════════════════════════════════════════════════════════════════
async def test_route_abort_mid_navigation_is_bounded_and_recoverable(
    instance, fixture_app_server
):
    """MQ-129 (route abort): the fixture commits a response, then RSTs the
    connection mid-body once the navigation has demonstrably started. The call
    must reach one terminal outcome inside the outer bound, and any success it
    reports must not claim the body it never received.
    """
    base = fixture_app_server
    navigate = get_fn("navigate")
    token = _token("drop")
    url = f"{base}/fault/drop?token={token}"

    await _arm(base, token)
    running = asyncio.ensure_future(
        navigate(instance_id=instance, url=url, timeout=NAV_TIMEOUT_MS)
    )
    try:
        await _await_entered(base, token)
        await _release(base, token)  # the drop itself
        outcome, value, elapsed = await _terminal(
            running, "navigate whose connection was dropped mid-body"
        )
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await running

    assert elapsed < OUTER_BOUND
    if outcome == "returned":
        assert isinstance(value, dict), value
        completed = await eval_js(instance, "!!document.getElementById('complete')")
        assert completed is False, "a dropped transfer delivered a complete body"
    else:
        assert isinstance(value, Exception)
        assert str(value), "the failure carried no message for the caller to act on"

    await _assert_recovered(instance, base)


# The CDP `Network.emulateNetworkConditions(offline=True)` variant the plan
# offers as the ALTERNATIVE injection path is deliberately not a node here.
# Issued against a tab that is parked in an in-flight `Page.navigate`, it never
# returns: nodriver's single connection listener dies with `InvalidStateError`
# while resolving an earlier command's generator, after which no future on that
# connection is ever resolved again (F-788). That wedges the *injection*, so it
# cannot measure the product — and the harness bound correctly reported it
# rather than letting it hang. The route-abort node above is the plan's other
# named mechanism and is what MQ-129 rests on; the exclusion is stated in the
# MQ step so a reader cannot infer offline-emulation coverage that is absent.
