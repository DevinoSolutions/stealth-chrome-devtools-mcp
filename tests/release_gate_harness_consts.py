"""The contract constants, bounds and diagnostic caps of the release-gate harness.

Split out of :mod:`release_gate_harness` (T-6148); that module re-exports every name
here, so callers keep importing from ``release_gate_harness``.
"""

from __future__ import annotations

from pathlib import Path

# ── Contract constants ──────────────────────────────────────────────────────
# Which declared segment of the ONE gate mechanism `run_release_gate_journey`
# runs. These are extents/segments, never duplicated machinery: every value
# below shares the same launcher resolution, isolated env, fixture app, stdio
# client, and teardown. See that function's docstring for what each claims.
FULL_JOURNEY = "full"
HANDSHAKE_ONLY = "handshake"
COOKIE_ROUND_TRIP = "cookies"
SOAK_STABILITY = "soak"

SERVER_NAME = "stealth-chrome-devtools-mcp"
REGISTRY_TOOL_COUNT = 97  # remediation baseline (CLAUDE.md: derived == 97)
RESULT_SCHEMA_VERSION = 1
FIXTURE_APP_DIR = Path(__file__).resolve().parent / "fixture_app"
#: The stand-in for a sign-in (F-958): ``GET /api/login`` sets this cookie
#: HttpOnly, as a real login's session cookie is, so page script cannot read it
#: back and only the server's own echo of the ``Cookie`` header can.
LOGIN_COOKIE = "fleet_login"
LOGIN_COOKIE_VALUE = "signed-in-f958"

# ── Bounds (every await is wrapped; the pytest --timeout is the outer net) ──
INIT_TIMEOUT = 60.0  # initialize handshake (answered locally by the proxy)
LIST_TIMEOUT = 130.0  # first backend-bound call — covers backend cold start
SPAWN_TIMEOUT = 120.0  # first real Chrome launch
WARMUP_TIMEOUT = 150.0  # cold Chrome + master-profile bootstrap (best-effort)
WARMUP_ATTEMPTS = 4  # the cold launch intermittently fails outright, not just slowly
WARMUP_BACKOFF_SECONDS = 3.0  # multiplied by the attempt number; see _cold_start_warmup
NAV_TIMEOUT = 60.0
CALL_TIMEOUT = 45.0
CLOSE_TIMEOUT = 45.0
SETTLE_TIMEOUT = 20.0
TERMINATE_TIMEOUT = 15.0
CHILD_SETTLE_TIMEOUT = 15.0

# ── Soak bounds (the stability segment; see _soak_journey) ──────────────────
# THE per-call deadline of the soak. Every measured soak operation is wrapped in
# it, so "nothing hangs" is an assertion about each individual call rather than
# about the journey's total runtime: one overdue reply fails the node by NAME.
# Deliberately far below NAV_TIMEOUT/CALL_TIMEOUT — after the cold path has been
# paid, a warm local page must answer in seconds, not tens of seconds.
SOAK_OP_TIMEOUT = 30.0
SOAK_CYCLES = 3  # mixed-operation rounds against the ONE instance (~17 ops each)
# A host that cannot resolve. `.invalid` is reserved by RFC 2606 precisely so it
# can never be registered, so this is a guaranteed DNS failure on any network —
# no third-party service is contacted and the shape does not depend on the run.
SOAK_BAD_HOST = "https://definitely-not-a-real-host.invalid/"
SOAK_PAGES = ("index.html", "interact.html", "extract.html", "hard_dom.html")
# What the soak ASKS `wait_for_element` to wait for a selector that never appears.
# The gap between this and what the call actually costs is F-805; the soak
# measures it (``journey["missing_selector"]``) rather than assuming either value.
SOAK_MISSING_SELECTOR_WAIT_MS = 2000

# ── Diagnostic caps ─────────────────────────────────────────────────────────
_STDERR_CAP_BYTES = 64 * 1024
_STDERR_CAP_LINES = 200
