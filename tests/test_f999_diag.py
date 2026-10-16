"""TEMPORARY F-999 diagnostic (removed before merge): repeat MQ-118's CORS step on
a fresh instance and report, per iteration, what the page and the server saw."""

from __future__ import annotations

import json

import pytest

from e2e_helpers import (
    eval_js,
    fixture_ledger,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    reset_fixture_ledger,
    sandbox_kwargs,
    wait_for_js,
    warmup_once,
)

pytestmark = integration_pytestmark()

ITERATIONS = 20
RUNS_PER_INSTANCE = 2


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def test_f999_cors_loop(fixture_origin_pair):
    origin_a, origin_b = fixture_origin_pair
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    report: list[str] = []
    bad = 0
    for i in range(ITERATIONS):
        iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
        try:
            await reset_fixture_ledger(origin_a)
            await navigate_and_settle(iid, f"{origin_a}/dynamic_probe.html")
            await eval_js(iid, "window.__auth.run()")
            await wait_for_js(iid, "window.__auth.done", True, timeout=15.0)
            for j in range(RUNS_PER_INSTANCE):
                await reset_fixture_ledger(origin_b)
                await eval_js(iid, "window.__cors.done = false")
                await eval_js(iid, "window.__cors.run()")
                done = await wait_for_js(iid, "window.__cors.done", True, timeout=15.0)
                cors = json.loads(await eval_js(iid, "JSON.stringify(window.__cors)"))
                led = await fixture_ledger(origin_b)
                ok = cors.get("echo_status") == 200 and led["cors_methods"] == [
                    "OPTIONS",
                    "POST",
                    "GET-blocked",
                ]
                if not ok:
                    bad += 1
                report.append(
                    f"iter={i} run={j} ok={ok} done={done} "
                    f"cors={json.dumps(cors, sort_keys=True)} "
                    f"cors_methods={led['cors_methods']}"
                )
        finally:
            await close(instance_id=iid)
    summary = "\n".join(report)
    print(summary)
    assert bad == 0, f"F999-DIAG {bad} bad of {len(report)}\n{summary}"
