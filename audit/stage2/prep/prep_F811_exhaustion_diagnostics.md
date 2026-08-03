# prep: F-811 — spawn failures under process exhaustion give no actionable signal

Diagnosis 2026-08-03 (read-only agent, Sentry issue STEALTH-CHROME-DEVTOOLS-MCP-K,
82 events over 7 days; 68% from the maintainer's machines, not CI). Candidate
finding for 2.0.5.

## Evidence

- The "amin" events are **burst-distributed**: 3 tight clusters (28-min / 8-min /
  1-min windows) separated by multi-hour gaps. Within a single burst the events
  span **multiple distinct backend ports** (e.g. 55296/27632/6370 in one 28-min
  window) — several backends cold-starting concurrently and each failing
  spawn_browser, not one backend retrying.
- Local logs corroborate: 45 `backend-*.log` files created on Aug 2 alone; a
  sub-cluster of 9 backend PIDs within 26 minutes whose logs contain ONLY the
  "backend process starting" line (150-153 bytes) — died immediately at boot.
  Churn/crash-restart under cold-start contention.
- The mechanism was observed **live during the diagnosis**: 204 chrome.exe,
  203 python.exe, 33 stealth-chrome-devtools-mcp.exe; 192/204 chrome started
  within the preceding 30 minutes (parallel agent fleet active). Matches the
  known pattern in [[agent-fleets-exhaust-windows-chrome]].

## The defect (product, not just hygiene)

When Chrome fails to launch under exhaustion, the tool surfaces nodriver's raw
`ToolError: Failed to spawn browser: --- Failed to connect to browser ---` with
zero indication that the machine is drowning in orphaned processes — even though
the CLI already ships the remedies (`kill-orphans`, `cleanup`) and
`browser_pid_registry.py` already knows which pids are OURS vs OS-visible.

## Proposed fix shape (for the spec pass)

On spawn failure (the nodriver connect-failure path in `browser_manager.py`'s
spawn pipeline, or via `process_cleanup.recover_orphans()`'s existing seam):
sample the local chrome/python process counts (or tracked-vs-visible delta from
the pid registry) and, above a threshold, wrap the error with a distinct
diagnostic naming the counts and pointing at `stealth-chrome-devtools
kill-orphans` / `cleanup`. Turns an opaque error into an actionable one. Budget
note: browser_manager.py is at its exact 1532 cap — the diagnostic helper must
live in a leaf (candidate: alongside the pid registry) with a net-zero call-site
payment.

## Status

Scoping only — no implementation dispatched. Needs: a real spec pass (sites,
threshold choice, tests, budget payment), then the usual pipeline.
