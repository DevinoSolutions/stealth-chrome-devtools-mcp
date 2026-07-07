# Lens Re-scan Delta (Stage 1b) — dispositions

**STATUS: ✅ ACCEPTED IN FULL by the human, 2026-07-02** (incl. the plan_M3 Amendment A1 for F-764). Directives below are BINDING on the named planners; the 20 records are merged into `audit/findings.json` with these dispositions.

**Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · 2026-07-02
**Source:** human ADDENDUM (`audit/ADDENDUM_LENSES.md`) — 4 lenses re-scanned over already-audited code; nothing redone.
**Ledgers:** `audit/stage1/findings_lens_{modularity,duplication,clarity,conventions}.json` — **20 findings (F-700–F-765), all 20 quote-verified programmatically by the orchestrator (exact-substring or line-level presence, CRLF-normalized). Hallucination counter stays 0.**
**Rule honored:** the plan-of-record wave order is UNCHANGED. Every disposition below routes a finding into an existing queued plan (as a binding planner directive), amends one approved plan (with human sign-off), or defers as known debt. Master `findings.json` merge happens after human acceptance.

## ⚡ Verified-critical item (orchestrator re-verified in source, confidence 1.0)

- **F-764 (High, conventions/lock-discipline)** — `debug_logger.py`: `export_to_file_paginated` acquires the **non-reentrant** `threading.Lock` with `acquire(timeout=5.0)` (`:363`, succeeds) then calls `get_debug_view_paginated` (`:373`) which re-enters `with self._lock:` (`:183`) → **unconditional self-deadlock**; the thread blocks forever HOLDING the logger lock; `finally`/`except` never fire. Today the blast radius is the export/view/clear paths (log_* early-return before the lock when disabled). **After approved M3-4 (unconditional recording) every `log_*` call would block too.** Upgrades the original audit's unverified F-307 (conf 0.55) into a concrete structural defect.
  **Disposition → AMEND approved `plan_M3` step M3-4** (one line: `threading.Lock()` → `threading.RLock()`, plus a re-entrancy pinning test that calls `export_to_file_paginated` on a populated logger and asserts it returns). Same file, same step, same PR-A; no scope creep beyond the lock type + test.

## Dispositions by target (bind the named planner; human approves each plan at its own gate as usual)

**→ M7 (browser_manager):** F-745 (Medium, clarity) `get_tab`/`get_browser` default `touch_activity=True` silently resets idle-eviction on every read — flip default or rename to signal the write. *(Joins F-608 already routed to M7.)*

**→ M11a (process_cleanup init/guard):** F-720 (Low, duplication) `_parse_nonnegative_int_env` defined twice + parse-family scattered → one `env_utils.py` home; F-763 (Medium, conventions, extends F-720) process_cleanup's inline bool-parse accepts a **narrower truthy set** than canonical `parse_bool_env` — "enabled" silently fails for `STEALTH_MCP_NO_AUTO_RECOVERY`. *(Joins F-607 already routed to M11a.)*

**→ M15 (storage/state model):** F-722 (Low, duplication) `DEFAULT_PORT`/`STATE_DIR` never imported — port re-typed in server.py:23 + cli.py:258; state-root re-typed/divergent (`~/.stealth-mcp-browser-sessions`, `C:\stealth-mcp-browser-sessions`, `~/.stealth_browser_pids.json`) → export + import the constants; F-762 (Low, conventions, extends F-722) one state-root convention nested under `STATE_DIR`.

**→ M12a (hook system):** F-721 (Medium, duplication) `response_stage_hooks.py` `ResponseStageProcessor` (148 lines) is a **dead, never-imported duplicate** of `dynamic_hook_system._execute_hook_action` — and carries a latent bug (missing base64-encode) → DELETE the file + its dedicated test; F-742 (Medium, clarity) its misleading "response" naming — resolved by the same deletion.

**→ M2 (singleton reuse-key):** F-765 (Medium→Low here, conventions) two bounded-wait idioms; if M2 touches the `singleton.py` deadline-poll loops anyway, extract one `poll_until()`; else leave as debt (do NOT do it as a drive-by).

**→ M5b (cloner consolidation):** F-744 (Medium, clarity) `clone_*` vs `extract_*` used interchangeably across the 4 whole-element tools → standardize on `extract_*` during the consolidation (which renames/merges these tools anyway).

**→ M4-Ph1 (registry/envelope/decomposition) — batch grows:**
- **F-701 (High, modularity — the big one):** `embedded/` is not a real package (empty `__init__`, zero relative imports, 3 sys.path mutation sites, latent dual-module-identity hazard) → make it a real package as **M4-Ph1 step 0** (mechanical import rewrite, ~20 files, after the singleton-churn plans land).
- F-700 (Medium, modularity, extends F-109): cli.py consumes 17 private `_symbols` across 25 sites → define the public surface when the registry is formalized.
- F-743 (Medium, clarity): six overlapping "run code in the page" tool names → rename/caveat at the registry pass.
- F-746 (Medium, clarity): `get_instance_state` promises "complete" but ships undocumented partial shapes → fix contract with the error-envelope work.
- F-760 (Medium, conventions): `get_`/`list_`/`new_`/`_tool`-suffix verb drift → one naming rule at the registry pass.
- F-761 (Medium, conventions, extends M10/F-104): "tab not found" handled 4 ways incl. one silent `[]` → the `_require_tab()` helper is exactly the error-envelope work already in M4-Ph1.

**→ M14 (Codify):** F-741 (Medium, clarity) "session" means 3 unrelated things and "proxy" 2 across singleton/cli/process_cleanup/proxy_* → glossary section in DESIGN.md pins one meaning per term; renames happen opportunistically in the M4-era passes, not as a standalone task. *(Orchestrator note: this line was inadvertently omitted from the doc as first presented; restored per the recommendation the human accepted.)*

**→ Defer as known debt (record in DESIGN.md at M14):**
- F-740 (High, clarity): `singleton.py` is 4 modules in a trench coat (proxy bridge + lifecycle + watchdog + state file) → split is SOUND but M3/M1/M8/M2 all edit this file serially; splitting now invalidates four plans' anchors. Revisit at wave 4 (post-M2), alongside M4-Ph1.
- F-702 (Medium, modularity): `BrowserManager` mixes 6 concerns → M4-Ph2-era work; M7 only touches `close_instance`.
- F-703 (Medium, modularity): `DebugLogger` bolts a 4-format serialization engine onto a logger → wave-4 cleanup; approved M3 deliberately left export tools as-is.

## Net effect
Wave order unchanged · no new standalone plans · M4-Ph1 batch grows by 6 findings (incl. step-0 packageization) · 1 amendment to approved plan_M3 (RLock, F-764) · 3 known-debt defers. After human acceptance: merge the 20 records + dispositions into `audit/findings.json` and update the cross_review_notes directives (done by orchestrator).
