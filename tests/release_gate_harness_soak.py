"""The soak (stability) segment: one instance, bounded mixed operations.

Split out of :mod:`release_gate_harness` (T-6148); that module re-exports every name
here, so callers keep importing from ``release_gate_harness``.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING, Any

from release_gate_harness_consts import (
    CLOSE_TIMEOUT,
    SOAK_BAD_HOST,
    SOAK_CYCLES,
    SOAK_MISSING_SELECTOR_WAIT_MS,
    SOAK_OP_TIMEOUT,
    SOAK_PAGES,
    SPAWN_TIMEOUT,
)
from release_gate_harness_journey import (
    _call,
    _decode_screenshot,
    _headless_spawn_kwargs,
    _settle_dom,
    _wire_shape,
)

if TYPE_CHECKING:
    from fastmcp import Client


# ---------------------------------------------------------------------------
# Soak (stability) segment — one instance, ~50 bounded mixed operations.
# ---------------------------------------------------------------------------
def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of a small sample (no numpy in the test deps)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(pct / 100.0 * len(ordered)) - 1)
    return ordered[index]


def _reply_shape(value: Any) -> Any:
    """A compact, log-safe description of one TOLERANT reply (diagnostics only).

    Recorded, printed, and never asserted on: which shape these paths return is
    exactly what is being changed elsewhere, so the soak reports it instead of
    pinning it — while still making a silent change of shape visible in the log.
    """
    if isinstance(value, dict):
        shape: dict[str, Any] = {"keys": sorted(value)}
        if "success" in value:
            shape["success"] = value["success"]
        return shape
    return type(value).__name__


async def _soak_op(
    client: Client,
    ops: list[dict[str, Any]],
    label: str,
    tool: str,
    args: dict[str, Any],
    *,
    timeout: float = SOAK_OP_TIMEOUT,
    tolerant: bool = False,
) -> Any:
    """Run ONE soak operation under an explicit deadline; record its latency.

    Every soak call goes through here, which is what makes "nothing hangs" an
    assertion rather than a hope: the deadline belongs to the operation, so a
    single overdue reply raises an ``AssertionError`` **naming the call** instead
    of stalling until pytest's outer timeout kills the whole session with no clue
    which tool wedged.

    ``tolerant=True`` marks the operations whose *failure* is a legitimate
    outcome — a deliberately throwing script, a host that cannot resolve. There
    the claim is only that a **bounded reply arrived** and the instance survived,
    so either the current success-shape or a ``ToolError`` is accepted (the error
    convention for these is being tightened separately; this node must not pin
    either side of that). ``raise_on_error=False`` is what turns a tool error
    into a returned result rather than an exception.
    """
    started = time.monotonic()
    entry: dict[str, Any] = {"op": label, "tool": tool}
    ops.append(entry)
    try:
        result = await asyncio.wait_for(
            client.call_tool(tool, args, raise_on_error=not tolerant), timeout
        )
    except TimeoutError as exc:
        entry["seconds"] = round(time.monotonic() - started, 3)
        entry["outcome"] = "OVERDUE"
        raise AssertionError(
            f"soak operation {label!r} ({tool}) never replied within its "
            f"{timeout}s deadline — the call hung"
        ) from exc
    except BaseException as exc:  # noqa: BLE001  PERMANENT(record the latency+shape of the failing op, then re-raise unchanged)
        entry["seconds"] = round(time.monotonic() - started, 3)
        entry["outcome"] = f"raised:{type(exc).__name__}"
        raise
    entry["seconds"] = round(time.monotonic() - started, 3)
    entry["outcome"] = "error" if getattr(result, "is_error", False) else "ok"
    return _wire_shape(result)


async def _soak_cycle(
    client: Client,
    iid: str,
    base_url: str,
    ops: list[dict[str, Any]],
    cycle: int,
    journey: dict[str, Any],
) -> None:
    """One mixed-operation round against the ONE live instance.

    The order is deliberate: each hostile operation (a throwing script, an
    unresolvable host, a tab closed out from under the rediscovery path) is
    immediately followed by an ordinary one that can only succeed on a healthy
    instance. That is what turns "it replied" into "it did not wedge".
    """
    page = SOAK_PAGES[(cycle - 1) % len(SOAK_PAGES)]
    url = f"{base_url}/{page}"
    tag = f"c{cycle}"

    await _soak_op(
        client, ops, f"{tag}:navigate", "navigate", {"instance_id": iid, "url": url}
    )
    await _settle_dom(client, iid)
    body = await _soak_op(
        client,
        ops,
        f"{tag}:query_elements",
        "query_elements",
        {"instance_id": iid, "selector": "body"},
    )
    assert isinstance(body, list) and body, (
        f"{tag}: body not queryable after navigate: {body!r}"
    )

    ok = await _soak_op(
        client,
        ops,
        f"{tag}:execute_script",
        "execute_script",
        {"instance_id": iid, "script": "document.title"},
    )
    assert isinstance(ok, dict) and ok.get("success") is True, ok

    # THE hostile one: a script that throws must not wedge the tab.
    thrown = await _soak_op(
        client,
        ops,
        f"{tag}:execute_script_throwing",
        "execute_script",
        {
            "instance_id": iid,
            "script": "(function(){ throw new Error('soak-intentional'); })()",
        },
        tolerant=True,
    )
    journey.setdefault("throwing_script_shapes", []).append(_reply_shape(thrown))
    # …and the very next ordinary call proves it did not.
    after = await _soak_op(
        client,
        ops,
        f"{tag}:execute_script_after_throw",
        "execute_script",
        {"instance_id": iid, "script": "1 + 1"},
    )
    assert isinstance(after, dict) and after.get("success") is True, after

    state = await _soak_op(
        client,
        ops,
        f"{tag}:get_element_state",
        "get_element_state",
        {"instance_id": iid, "selector": "body"},
    )
    assert isinstance(state, dict), state

    # Two more shapes that can only be proved bounded by *doing* them: a wait for
    # an element that will never exist (the tool owns its own timeout, so this
    # measures whether the product's own bound actually fires inside ours) and an
    # interaction with a selector that resolves to nothing.
    #
    # Cycle 1 only, deliberately. See F-805, now HALF fixed: the wait honours
    # its own 2000ms budget since F-884 (~2.03s), while the click still costs
    # ~10.5s because THIS call passes no timeout and so spends click_element's
    # own 10000ms default (it does honour one when given: timeout=2000 -> 2.03s).
    # The branch that genuinely ignores a declared timeout is text_match, which
    # this journey does not exercise. Repeating them every cycle would triple the
    # soak's runtime to buy a repetition of a fact one measurement already
    # establishes, and would push the node past the integration lane's budget.
    if cycle == 1:
        await _soak_op(
            client,
            ops,
            f"{tag}:wait_for_missing_element",
            "wait_for_element",
            {
                "instance_id": iid,
                "selector": "#soak-never-exists",
                "timeout": SOAK_MISSING_SELECTOR_WAIT_MS,
            },
            tolerant=True,
        )
        await _soak_op(
            client,
            ops,
            f"{tag}:click_missing_element",
            "click_element",
            {"instance_id": iid, "selector": "#soak-never-exists"},
            tolerant=True,
        )
        alive = await _soak_op(
            client,
            ops,
            f"{tag}:query_elements_after_misses",
            "query_elements",
            {"instance_id": iid, "selector": "body"},
        )
        assert isinstance(alive, list) and alive, (
            f"{tag}: the instance did not survive the missing-selector calls: {alive!r}"
        )
        by_label = {o["op"]: o for o in ops}
        journey["missing_selector"] = {
            "requested_wait_seconds": SOAK_MISSING_SELECTOR_WAIT_MS / 1000,
            "wait_seconds": by_label[f"{tag}:wait_for_missing_element"]["seconds"],
            "click_seconds": by_label[f"{tag}:click_missing_element"]["seconds"],
        }
    await _soak_op(
        client,
        ops,
        f"{tag}:scroll_page",
        "scroll_page",
        {"instance_id": iid, "direction": "down", "amount": 300, "smooth": False},
    )
    shot = await _soak_op(
        client,
        ops,
        f"{tag}:take_screenshot",
        "take_screenshot",
        {"instance_id": iid, "format": "png"},
    )
    assert _decode_screenshot(shot)[:8] == b"\x89PNG\r\n\x1a\n", f"{tag}: not a PNG"
    cookies = await _soak_op(
        client, ops, f"{tag}:get_cookies", "get_cookies", {"instance_id": iid}
    )
    assert isinstance(cookies, list), cookies

    # ── tab churn ──────────────────────────────────────────────────────────
    before = await _soak_op(
        client, ops, f"{tag}:list_tabs", "list_tabs", {"instance_id": iid}
    )
    assert isinstance(before, list) and before, before
    opened = await _soak_op(
        client,
        ops,
        f"{tag}:new_tab",
        "new_tab",
        {"instance_id": iid, "url": "about:blank"},
    )
    assert isinstance(opened, dict) and opened.get("tab_id"), opened
    await _soak_op(
        client,
        ops,
        f"{tag}:switch_tab",
        "switch_tab",
        {"instance_id": iid, "tab_id": opened["tab_id"]},
    )
    await _soak_op(
        client,
        ops,
        f"{tag}:close_tab",
        "close_tab",
        {"instance_id": iid, "tab_id": opened["tab_id"]},
    )
    # The post-close_tab read is the point of the churn: nodriver's `browser.tabs`
    # yields raw Connection objects after ANY close, despite its List[Tab]
    # annotation, so this is where a Tab-only call on a Connection would surface.
    after_tabs = await _soak_op(
        client, ops, f"{tag}:list_tabs_after_close", "list_tabs", {"instance_id": iid}
    )
    assert isinstance(after_tabs, list) and len(after_tabs) == len(before), (
        f"{tag}: tab count did not return to {len(before)} after close_tab: {after_tabs}"
    )
    inst_state = await _soak_op(
        client,
        ops,
        f"{tag}:get_instance_state",
        "get_instance_state",
        {"instance_id": iid},
    )
    assert isinstance(inst_state, dict), inst_state

    # ── an unresolvable host must answer, bounded, and leave the tab usable ──
    if cycle >= 2:
        bad = await _soak_op(
            client,
            ops,
            f"{tag}:navigate_bad_host",
            "navigate",
            {"instance_id": iid, "url": SOAK_BAD_HOST},
            tolerant=True,
        )
        journey.setdefault("bad_host_shapes", []).append(_reply_shape(bad))
        await _soak_op(
            client,
            ops,
            f"{tag}:navigate_recover",
            "navigate",
            {"instance_id": iid, "url": url},
        )
        await _settle_dom(client, iid)
        recovered = await _soak_op(
            client,
            ops,
            f"{tag}:query_elements_recover",
            "query_elements",
            {"instance_id": iid, "selector": "body"},
        )
        assert isinstance(recovered, list) and recovered, (
            f"{tag}: the instance did not recover from {SOAK_BAD_HOST}: {recovered!r}"
        )

    # ── interleaved invariant: still exactly ONE instance, still answering ──
    instances = await _soak_op(
        client, ops, f"{tag}:list_instances", "list_instances", {}
    )
    assert isinstance(instances, list) and len(instances) == 1, (
        f"{tag}: expected exactly one live instance, got {instances!r}"
    )
    active = await _soak_op(
        client, ops, f"{tag}:get_active_tab", "get_active_tab", {"instance_id": iid}
    )
    assert isinstance(active, dict) and active.get("tab_id"), active


async def _soak_journey(client: Client, base_url: str, record: dict[str, Any]) -> None:
    """The stability segment: ONE instance survives ~50 bounded mixed operations.

    What this claims that the canonical journey does not: the browser stays up
    across a long, *hostile* sequence — throwing scripts, an unresolvable host,
    repeated tab open/close churn — with **every** call individually bounded, and
    it shuts down cleanly at the end leaving no owned Chrome process behind.

    Latency of every operation is recorded so the summary (max / p95 / the five
    slowest) lands in the CI log on PASS, making stability drift across releases
    visible instead of invisible-until-it-times-out.
    """
    spawn = await _call(
        client, "spawn_browser", _headless_spawn_kwargs(), SPAWN_TIMEOUT
    )
    assert isinstance(spawn, dict) and spawn.get("instance_id"), spawn
    iid = spawn["instance_id"]
    ops: list[dict[str, Any]] = []
    journey: dict[str, Any] = {"instance_id": iid, "cycles": SOAK_CYCLES, "ops": ops}
    record["journey"] = journey
    try:
        journey["navigated_url"] = f"{base_url}/{SOAK_PAGES[0]}"
        for cycle in range(1, SOAK_CYCLES + 1):
            await _soak_cycle(client, iid, base_url, ops, cycle, journey)

        # End state: close the instance and prove the registry is empty again.
        await _soak_op(
            client,
            ops,
            "close_instance",
            "close_instance",
            {"instance_id": iid},
            timeout=CLOSE_TIMEOUT,
        )
        remaining = await _soak_op(
            client, ops, "list_instances_final", "list_instances", {}
        )
        assert isinstance(remaining, list) and not remaining, (
            f"instances remained after close_instance: {remaining!r}"
        )
        journey["closed_cleanly"] = True
    finally:
        durations = [o["seconds"] for o in ops if "seconds" in o]
        journey["op_count"] = len(ops)
        journey["latency"] = {
            "max_seconds": round(max(durations), 3) if durations else 0.0,
            "p95_seconds": round(_percentile(durations, 95), 3),
            "p50_seconds": round(_percentile(durations, 50), 3),
            "total_seconds": round(sum(durations), 2),
            "deadline_seconds": SOAK_OP_TIMEOUT,
            "slowest": sorted(
                (o for o in ops if "seconds" in o),
                key=lambda o: o["seconds"],
                reverse=True,
            )[:5],
        }
        if not journey.get("closed_cleanly"):
            await _call(
                client,
                "close_instance",
                {"instance_id": iid},
                CLOSE_TIMEOUT,
                raise_on_error=False,
                allow_fail=True,
            )
