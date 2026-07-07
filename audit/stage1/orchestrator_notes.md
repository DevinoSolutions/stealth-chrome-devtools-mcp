# Orchestrator Synthesis Notes (running) — Stage 1

Pinned SHA 2267b83 confirmed clean at collection time (HEAD==pinned, server.py clean; dd2's "uncommitted edits" was a misread of untracked audit/).

## Cross-agent convergence map (root cause -> corroborating finding IDs)
Findings from independent agents pointing at the SAME root cause = strong synthesis candidates.

1. **Socket-only liveness masks a wedged backend** (central operability failure)
   - F-501 (premortem, Critical) + F-301 (incident, Critical) — BOTH cite singleton.py:83 TCP-connect, derived independently. STRONGEST convergence.
   - Related: F-611 (prior P-017) list_instances no CDP round-trip; F-164 (dd4) hung script wedges tab.

2. **Backend unobservable — DEVNULL stdout/stderr + in-memory logger off-by-default**
   - F-303 (incident, Critical) singleton.py:228 DEVNULL, grep-verified zero FileHandler.
   - F-503 (premortem, High) detached spawn no supervisor/logs. F-306 (incident) no runbook.

3. **Stale backend serves pre-edit code (version-string reuse key)**
   - F-120 (dd2, High, root) + F-206 (change, High) + F-504 (premortem, High) + user memory [[applying-backend-edits-live]]. QUADRUPLE-confirmed.
   - hot_reload is a false fix: F-121 (dd2) + F-610 (prior P-015) + F-506 (premortem) — skips server.py, orphans live instances, untested.

4. **server.py god object (4207 LOC, ~96 tools + ~700L unrelated eviction logic)**
   - F-505 (premortem, High) + F-201 (change, High) + F-612 (prior P-024, High) + both hotspots. DD-1 pending.

5. **5-variant element cloner duplication (2223 LOC, ~35-40% removable)**
   - F-140/F-142 (dd3) + F-203 (change) + F-508 (premortem) + F-601 (prior P-002) + both hotspots.
   - F-142 latent bug: hang-avoidance CDP rewrite in 1/5 sibling methods only. F-145 zero extraction tests.

6. **No recovery tooling / ops surface**
   - F-302 (incident) CLI no stop/restart/kill; F-305 (incident) PID recorded but not surfaced; F-509 (premortem) fixed port no fallback.

7. **Error-handling / observability voids (broad except, no correlation id)**
   - F-204 (change, High) DebugLogger dedup key has no instance_id -> cross-session suppression; F-308 (incident) no request-id; F-307 (incident, low-conf) logger lock. DD-5 pending (consolidated broad-except finding).

8. **CI gates exclude the real-browser surface**
   - F-507 (premortem, High) publish/coverage gate is `-m "not integration"`; corroborated by Stage 0 test_baseline (24 deselected).

9. **Onboarding/docs gaps**
   - F-403 (onboard) documented `uv run` test cmd fails on this checkout (also hit in Stage 0) — RIGHT-SIZE to High not Critical (tests pass once uv bypassed; trigger partly local path & + spaces).
   - F-407 (onboard, High) no CONTRIBUTING/branch/PR/release docs. F-401/402/404/405/406.

10. **Import-time side effects (testability + safety)**
   - F-124 (dd2, High) ProcessCleanup() kills real orphans AT IMPORT; conftest never opts out.
   - F-125 (dd2) all 15 singletons import-time, no DI/reset seam.

11. **Unbounded response-body store (memory)**
   - F-605 (prior P-007, High) network_interceptor unbounded body dicts, no cap/eviction until instance close.

## OPEN CONFLICTS to resolve (adversarial review / dd5)
- **C1: CDP hang.** P-016 refuted "hangs forever" (timeout at 59 sites, F-603 preamble) VS F-164 (dd4) hung JS eval wedges tab because terminateExecution never called. Resolution: per-CDP-call timeout EXISTS; distinct residual hang vector (script termination) survives. Keep F-164, mark P-016 already_fixed-as-stated. Backend-liveness hang (F-301) is a THIRD, separate layer. Synthesis must separate these 3 hang layers cleanly.
- **C2: kill safety.** emp-incident: process_cleanup Chrome-orphan kill is SAFE (user-data-dir+create_time). F-608 (prior P-010): a NON-recovery kill path trusts stored PID with no identity check. F-126 (dd2): eviction terminate() may bypass graceful shutdown on Windows. Likely 3 different kill paths. dd5-lifecycle must disambiguate before synthesis.

## Severity recalibrations flagged
- F-403 Critical -> High (see above).
- F-161 (dd4) create_python_binding zero-sandbox: High-as-written but LOCAL self-inflicted trust boundary (no page->exec path per dd4). Weigh at adversarial.

## NON-findings deliberately recorded (guard against false positives)
- dd4: NO page-controlled->exec path (self-inflicted only); NO shared-state race (asyncio cooperative).
- emp-incident: process_cleanup orphan-Chrome kill is well-engineered — PRESERVE.
- prior-verify refuted: P-005, P-012, P-013, P-018, P-026 (+2 unverifiable).

## Still pending at time of note: dd1-server (F-101), dd5-lifecycle (F-180).
