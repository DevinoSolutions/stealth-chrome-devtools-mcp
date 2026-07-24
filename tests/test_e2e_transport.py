"""plan_RELEASE W1 (G-A) — the real-stdio transport release gate.

Closes the transport gap: the existing E2E suite drives tools via the in-process
``.fn`` seam and an in-memory FastMCP client, so the actual wire path a user gets
— a client spawning the installed console launcher over **stdio JSON-RPC**,
``initialize`` → ``tools/list`` → ``tools/call`` against real headless Chrome — is
never exercised. This test resolves the absolute installed launcher and runs the
one canonical :mod:`release_gate_harness` journey through it, then asserts the
returned record. W3 imports the same harness unchanged for its install smoke.

Marked ``integration`` + ``transport``; skipped when Chrome / the server is
unavailable (same guard as the other e2e modules).

KNOWN RED (finding B1, fix owned by RELEASE-FIX-B): the journey currently dies
mid-flight because FastMCP runs ``app_lifespan`` PER MCP SESSION over streamable
HTTP, and the stdio proxy's liveness watchdog opens+closes a probe session
(``_backend_http_ready``: real ``initialize`` then DELETE) every 2s — each probe's
lifespan entry re-runs orphan recovery (killing freshly spawned Chrome) and each
exit runs the full server cleanup ("All browser instances closed"). Every browser
instance on the backend dies within ~2s of any probe. The in-process E2E seam
bypasses the proxy+HTTP entirely, which is why only this transport gate sees it.
With the watchdog quieted locally the full journey passes end-to-end, so the
xfail below pins exactly this defect and nothing else. ``strict=False`` because
the failure point depends on where the 2s tick lands in the journey (a lucky
run could sneak through). RELEASE-FIX-B must remove the marker.
"""

from __future__ import annotations

import pytest

from e2e_helpers import CAN_RUN
from release_gate_harness import (
    REGISTRY_TOOL_COUNT,
    RESULT_SCHEMA_VERSION,
    SERVER_NAME,
    resolve_launcher,
    run_release_gate_journey,
)

pytestmark = [pytest.mark.integration, pytest.mark.transport]

if not CAN_RUN:
    pytestmark.append(pytest.mark.skip("Chrome not available or server failed to load"))


@pytest.mark.xfail(
    reason=(
        "B1 (RELEASE-FIX-B): per-MCP-session app_lifespan + the proxy's 2s "
        "watchdog probe sessions close all browser instances over real stdio"
    ),
    strict=False,
)
async def test_real_stdio_release_gate_journey(tmp_path):
    """Foundation proof + handshake/schema + canonical journey over real stdio."""
    launcher = resolve_launcher()  # this env's absolute installed console launcher

    record = await run_release_gate_journey(launcher=launcher, work_dir=tmp_path)

    # Foundation proof: protocol, server identity, and the 94-tool registry.
    assert record["schema_version"] == RESULT_SCHEMA_VERSION
    assert record["transport"] == "stdio"
    assert record["stdout_framing_only"] is True
    assert record["server"]["name"] == SERVER_NAME
    assert record["server"]["version"]
    assert record["server"]["protocol_version"]
    assert record["tool_count"] == REGISTRY_TOOL_COUNT

    # The launcher we drove is the absolute installed entry point.
    assert record["launcher"].endswith(
        ("stealth-chrome-devtools-mcp.exe", "stealth-chrome-devtools-mcp")
    )

    # Representative tools/call result matches the in-process .fn seam.
    parity = record["representative_parity"]
    assert parity["seam_available"] is True
    assert parity["equal"] is True

    # Canonical journey ground truth (oracle + live DOM), all via tools/call.
    journey = record["journey"]
    assert journey["instance_id"]
    assert journey["tabs_count"] >= 1
    assert "click:btn-counter" in journey["actions"]
    assert "change:select-single:beta" in journey["actions"]
    assert journey["counter_value"] == "1"
    assert journey["text_input_value"] == "hello"
    assert journey["select_value"] == "beta"
    assert journey["title"] == "fixture-interact-page"
    assert journey["screenshot_format"] == "png"
    assert journey["screenshot_bytes"] > 0

    # Fixture was literal-IPv4 loopback; teardown left nothing running.
    assert record["fixture_base_url"].startswith("http://127.0.0.1:")
    assert record["backend_pid_recorded"] is True  # a real backend actually ran
    assert record["backend_gone"] is True
    assert record["no_child_remaining"] is True
