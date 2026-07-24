# plan_RELEASE_FIX_B — B1: per-session app_lifespan destroys all browser state over the real wire path

**Status: EXECUTED** — human go 2026-07-24 ("I trust your execution"); C1 = f81ff8f
(session-reentrant lifespan; if-guard in `finally` instead of the planned early
return, deliberately — a `return` inside `finally` would suppress a propagating
session exception), C2 = 585ebf2 (xfail flipped; transport journey 3/3 green over
real stdio + independent orchestrator re-run). Merge gate: human, PR pending.
**Found by:** W1's real-stdio transport gate (`tests/test_e2e_transport.py`), first run.
**Severity:** Tier-A-equivalent, release-blocking. Invisible to the entire in-process
test suite; reproduced 3/3 over the real user path.

---

## 1. The finding (B1)

Over the transport a real user gets (stdio proxy → HTTP backend), **every browser
instance on the backend dies within ~2 seconds**.

Mechanism (all confirmed empirically from isolated-backend logs, run of 2026-07-24):

1. FastMCP serves streamable HTTP by running the low-level MCP `Server.run()` **per
   MCP session**, and `Server.run()` enters the server's `lifespan` context each
   time. Our `app_lifespan` (embedded/server.py:216) was written assuming
   once-per-process semantics; under HTTP it runs **once per MCP session**.
2. The stdio proxy's liveness watchdog (`singleton._watch_backend_liveness`,
   `interval=2.0`, default check `_backend_http_ready` since F-501/plan_M1) opens a
   **real `initialize`** and then DELETEs the session — every 2 seconds.
3. Each probe session therefore runs, on the shared backend:
   - **entry:** `process_cleanup.activate()` → orphan *recovery* re-runs with a fresh
     "server init time", so Chrome processes started *before this probe session*
     (e.g. the master-profile bootstrap, or a browser mid-spawn) look orphaned and
     get killed — observed: `process_cleanup.recovery: Killed 1 orphaned browser
     processes` while `spawn_browser` was in flight;
   - **exit:** the full destructive teardown — `browser_manager.close_all()`
     ("All browser instances closed"), `process_cleanup._cleanup_all_tracked()`,
     `in_memory_storage.clear_all()`.

Observed failure modes in the W1 journey (varies with where the 2s tick lands):
`spawn_browser` → nodriver "Failed to connect to browser" (Chrome killed mid-launch);
or spawn succeeds then `click_element` → `Instance not found: <iid>`.

**Control experiment:** with the watchdog interval locally set to 3600s (uncommitted
debug patch, reverted), the complete W1 journey passes end-to-end over real stdio —
foundation proof, 94-tool registry, parity, spawn/navigate/interact/oracle/
extraction/PNG screenshot, clean teardown. B1 is the only defect between the current
tree and a green transport gate.

**Blast radius beyond the watchdog:** the watchdog merely makes it constant. ANY
MCP session ending (a second Claude Code session disconnecting, a CLI `status`
probe, any client reconnect) runs the same destructive teardown on the shared
backend. This affects released 1.2.0 too (per-session lifespan has always been the
HTTP semantics); F-501's app-level watchdog probe (post-1.2.0) turned "instances
die when some session closes" into "instances die every 2 seconds".

## 2. Why no existing test saw it

The E2E suite drives tools through the in-process `.fn` seam (`tests/e2e_helpers.py`)
— no proxy, no HTTP, no sessions, and the single in-process lifespan matches the
faulty once-per-process assumption. Exactly the transport gap W1 (plan_RELEASE §2.1)
exists to close. This finding is the empirical proof of E2E-9's premise.

## 3. The fix (one chunk + the E2E flip)

**Principle: `app_lifespan` must be idempotent across sessions; destructive teardown
must be bound to *process* end, never *session* end.** (Same class as the
runpy double-registration lesson: module-global mutations at serve boundaries must
be idempotent.)

### C1 — make `app_lifespan` session-reentrant (src: embedded/server.py only)

RED first (hermetic, tests/test_lifespan_reentrancy.py, NEW):
- `test_second_lifespan_cycle_does_not_close_instances`: enter lifespan A (long-
  lived), register a fake instance in `browser_manager`; enter+exit lifespan B (the
  probe-session shape); the instance must still exist and `close_all` must NOT have
  run (spy/monkeypatch).
- `test_lifespan_reentry_does_not_rearm_orphan_recovery`: second entry must not
  re-run `process_cleanup.activate()` recovery (spy: activate called once).
- `test_last_exit_still_cleans_up_in_stdio_mode`: the standalone-stdio single-cycle
  path (enter once, exit once with the stdio flag) still runs the full teardown —
  the 1.x single-process contract is preserved verbatim (M6 error/message bytes
  untouched).

GREEN (design):
- Module-global session counter + first-entry guard in `app_lifespan`:
  - startup block (`activate()`, `start_idle_reaper()`, `spawn_background_sweep`)
    runs on FIRST entry only;
  - the `finally` destructive teardown runs ONLY when this process serves stdio
    (standalone mode: one session == process lifetime). In HTTP mode session exit
    is a no-op; actual process termination is already covered by
    `process_cleanup.activate()`'s atexit/signal reaping.
  - the serve mode is stamped by `main()` (which already parses `--transport`) into
    a module-level flag; default preserves current stdio behaviour.
- LOC: ~+12 net in server.py (3330/3389 — fits; cap never padded).
- Convention notes: no new error shapes, no tool-surface change, no new deps;
  `debug_logger` startup/shutdown message bytes unchanged (they just fire once).

### C2 — flip the W1 pin (tests only)

Remove `@pytest.mark.xfail(... B1 ...)` from
`tests/test_e2e_transport.py::test_real_stdio_release_gate_journey` and delete the
KNOWN-RED paragraph from its docstring. The transport gate must pass 3/3 locally
(Windows) and on CI (ubuntu, Chrome+Xvfb). This is the E2E proof of the fix — the
same journey that caught B1.

## 4. Out of scope (deliberately)

- Reducing watchdog probe cost (e.g. cheaper liveness endpoint): the probe is
  correct per F-501; it only *exposed* B1.
- `_probe_backend_status`/CLI probe session churn: harmless once lifespan is
  session-reentrant.
- Multi-session instance *ownership* semantics: instances remain globally shared
  by design (single-user tool).

## 5. Gates

Same as RELEASE-FIX-A: ruff format+check, ty `--exit-zero-on-warning src/...`
(76-diagnostic baseline), vulture, budgets, suppression owners, unit suite green,
full integration + transport locally; PR with true merge commit; human merge gate.
