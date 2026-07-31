# F-807 — the cold-start lock could evict (kill) a healthy backend it merely failed to probe

**Status: RESOLVED** in the 40-session startup-herd work, 2026-07-31 (same
change that opened it — found by construction while building
`tests/test_startup_herd.py`, before any client-visible failure occurred).
**Severity: HIGH (latent)** — the failure mode is a lock-holder terminating
the singleton backend that every other Claude Code session is actively using,
then double-spawning. Never observed end-to-end (see the diagnosis note below
for what WAS observed and why it looked like this defect), but reachable by
construction on every multi-session cold start.

---

## The race, by construction

Two facts that were individually reasonable and jointly wrong:

1. **The winner released the lock at socket-bind.** `_start_backend_holding_lock`
   held the exclusive lock through `_wait_for_server(port)` — a bare TCP
   connect. But the reuse gate (`_find_running_server`) demands an answered
   MCP `initialize` (F-301: a bare socket cannot distinguish a wedged backend
   from a healthy one). Between bind and MCP-ready there is a window where the
   backend is real and healthy but the gate says "not reusable".
2. **The gate's verdict was a single 2s probe, and "not reusable" meant
   evict.** Any thread acquiring the freed lock inside that window — or while
   the backend is busy enough (40 sessions initializing at once) to miss one
   `LIVENESS_PROBE_TIMEOUT` attempt — ran `_clear_stale_backend`, which
   *terminates* the recorded backend, then spawned a replacement.

A 40-session herd makes both halves likely: 40 proxies race the lock, and the
backend spends its first seconds absorbing 40 `initialize` + `tools/list`
storms, exactly when probes are cheapest to miss.

## The fix (singleton.py)

One identity-gated patience window, `_same_identity_backend_ready(port,
patience)`, now the single home for the identity+readiness contract
(`_find_running_server` collapsed onto it with `patience=0`):

* **The winner holds the lock until MCP-ready**, not socket-bind — the lock is
  released only into a state where the reuse gate itself would pass.
* **A lock-holder gives a same-identity backend (version AND source
  fingerprint both match) up to `REUSE_PATIENCE_SECONDS` (60s)** of retried
  probes (each with the wider `REUSE_PROBE_TIMEOUT` = 10s, matching
  `_await_backend_http`'s own per-request budget) before it may evict.
  Waiting is free: proxies sit in their own 120s `_await_backend_http`
  window, and a genuinely wedged backend is still evicted well inside it.
* **No patience for the stale or the dead**: a version- or source-mismatched
  record evicts immediately (issue #14 — upgrades must take effect NOW), and
  a record whose socket is closed AND whose pid is no longer our live backend
  fails on the first probe, so a normal crash-recovery cold start stays fast.
* The watchdog's human-pinned `LIVENESS_PROBE_TIMEOUT` = 2.0 and its ~12s
  detection window are untouched; the single-shot discovery probe keeps 2s.

Pinned by `tests/test_singleton_cold_start_patience.py` (6 unit nodes: busy
survives, dead skips the wait, stale gets no patience, winner holds through a
ready probe) and exercised at process scale by `tests/test_startup_herd.py`.

## Diagnosis note — the trampoline that impersonated this defect

The herd test's first "exactly one backend" assertion counted **process
command lines**, and failed with two matches on every run. That was NOT this
race: on Windows, a uv-managed venv's `python.exe` is a **trampoline** — the
`Popen` pid is a shim whose child (identical command line) is the real
interpreter. One logical backend, two matching processes; `server.json`
records the shim's pid, and only the child writes `backend-{pid}.log`, which
is also why the "second backend" never seemed to log. The herd test now
counts process-tree roots. Corollary kept in mind for future work:
`_terminate_backend`'s recorded-pid fallback terminates the shim, not the
child (the port-resolution primary path already targets the real listener).

## Measured, after the fix (Windows workstation, 2026-07-31)

40 simultaneous cold sessions: all usable (initialize + `tools/list`) in
**7.9s** — initialize p95 4.55s, `tools/list` p95 7.56s, exactly one logical
backend. Warm 41st join: **1.0s**. Spec was 30s for the full herd.
