"""plan_RELEASE W5-prep — the real-transport ``get_cookies`` success path.

W5's **``get_cookies`` hard block** (plan_RELEASE §2.5): at W5 start, successful
real-browser cookie *retrieval* had never been proved, so the contract generator
refuses a 94-release-qualified-tool statement. The block clears only via option
(a) — "a real Chrome + real transport success test sets a cookie, retrieves it,
and asserts its value". A mock-only success, a missing-instance error, a schema
check, or a characterization explicitly **cannot** satisfy it.

This module is that test, and it is a **dedicated collected node** on purpose.
§2.5 rules that a *representative journey* cannot satisfy a per-tool success
claim, and §2.1 names ``test_real_stdio_release_gate_journey`` as exactly that —
so folding these assertions into the canonical journey would have produced
evidence W5 must reject. The machinery is still the one harness (absolute
installed launcher, isolated HOME/session root, fixture app, stdio
``tools/call``, bounded teardown); only the steps differ.

Covers three tools on the one real path — ``set_cookie``, ``get_cookies``,
``clear_cookies``.

Marked ``integration`` + ``transport``; skipped when Chrome / the server is
unavailable (same guard as the other e2e modules). macOS transport is excluded
under F-773, so this node's evidence is Linux/X64 + Windows/X64.
"""

from __future__ import annotations

import shutil

import pytest

from e2e_helpers import CAN_RUN
from release_gate_harness import (
    COOKIE_ROUND_TRIP,
    RESULT_SCHEMA_VERSION,
    gate_work_dir,
    resolve_launcher,
    run_release_gate_journey,
)

pytestmark = [pytest.mark.integration, pytest.mark.transport]

if not CAN_RUN:
    pytestmark.append(pytest.mark.skip("Chrome not available or server failed to load"))


async def test_real_transport_cookie_round_trip(tmp_path):
    """set_cookie → get_cookies → **assert the value** → clear_cookies, over real
    headless Chrome and real stdio JSON-RPC.

    Note the assertions read the harness record, which is built from
    ``result.structured_content`` — the raw wire shape a user's client receives.
    fastmcp's ``result.data`` reconstruction is deliberately NOT used: this tool
    is declared ``-> list[dict[str, Any]]`` but returns nodriver
    ``cdp.network.Cookie`` dataclasses, and while pydantic serializes those to
    correct JSON objects on the wire, ``.data`` rebuilds them as an opaque
    ``[Root()]``. Asserting through ``.data`` would look like a product failure
    when it is only an artifact of the reconstruction.
    """
    launcher = resolve_launcher()  # this env's absolute installed console launcher
    work_dir = gate_work_dir(tmp_path)  # RUNNER_TEMP on CI (see helper docstring)

    try:
        record = await run_release_gate_journey(
            launcher=launcher, work_dir=work_dir, stages=COOKIE_ROUND_TRIP
        )
    finally:
        if work_dir != tmp_path:  # pytest cleans its own; this one is ours
            shutil.rmtree(work_dir, ignore_errors=True)

    assert record["schema_version"] == RESULT_SCHEMA_VERSION
    assert record["transport"] == "stdio"
    assert record["stages"] == COOKIE_ROUND_TRIP
    assert record["navigation_verified"] is True  # a real page, not about:blank
    assert record["launcher"].endswith(
        ("stealth-chrome-devtools-mcp.exe", "stealth-chrome-devtools-mcp")
    )

    journey = record["journey"]
    assert journey["instance_id"]
    # A real http:// origin, not about:blank — cookies need one.
    assert journey["navigated_url"].startswith("http://127.0.0.1:")

    cookies = journey["cookies"]
    expected = cookies["value"]
    assert expected.startswith("w5-")  # unique per run; a stale cookie cannot forge it

    # THE assertion the hard block turns on: the value we set came back, from
    # both retrieval paths (Network.getCookies and Network.getAllCookies).
    assert cookies["scoped_value"] == expected
    assert cookies["all_value"] == expected

    # Retrieval returned real, populated cookie objects — not empty shells.
    assert cookies["scoped_count"] >= 1
    assert {"name", "value", "domain", "path"} <= set(cookies["field_names"])

    # The cookie really reached the browser; get_cookies did not merely echo us.
    assert cookies["document_cookie_confirms"] is True

    # clear_cookies removed it (proved by re-reading, not by its return value).
    assert cookies["cleared"] is True

    # Teardown left nothing running.
    assert record["backend_gone"] is True
    assert record["no_child_remaining"] is True
