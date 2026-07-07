# Triage & Cost/Benefit — stealth-chrome-devtools-mcp

**Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · 2026-07-02
**Authority:** Human delegated the triage decision to the orchestrator ("look deeper, if it nets positive and effort justifies, do it — I'll take your recommendation"). This file IS the triage decision of record. Source findings: `audit/findings.json`; full report: `audit/REPORT.md`.
**Lens:** single-user, local dev tool, one maintainer. "Worth it" = (benefit to *this* context) × (does it unlock other work) − (effort) − (fix risk). Priorities: (1) maintainability (2) operability (3) performance.

## Decision table

| # | Finding (root cause) | Sev | Effort | Net | **Verdict** |
|---|---|---|---|---|---|
| M1 | Socket-only liveness masks a wedged backend | Crit | M | ++ unlocks recovery + diagnosis | **FIX (now)** |
| M2 | Live code edits don't take effect (version-key + broken hot_reload) | High | M | ++ maintainer's *daily* pain | **FIX (now)** |
| M3 | Backend unobservable (DEVNULL + off-by-default logger) | High | M | +++ unlocks verifying everything | **FIX (now, first)** |
| M7 | close_instance sync-kill freezes shared backend | High | S–M | + cheap, real freeze | **FIX (now)** |
| M8 | No recovery CLI (stop/restart/kill-orphans) | High | M | ++ daily loop + M2 escape hatch | **FIX (now)** |
| M9 | Unbounded response-body store (OOM) | High | S (cap) | + cheap cap kills the risk | **FIX (now, minimal: cap + capture opt-in/filtered)** |
| M15 | State dup; persistent_storage drops fields | Med | S | + cheap, prevents silent field-drop | **FIX (now, serialize the model)** |
| M11a | ProcessCleanup kills processes *at import* | Med | S | + real footgun, cheap guard | **FIX (now, lazy/guarded init)** |
| M10a | 22 truly-silent excepts swallow diagnoses | Med | S | + directly aids M3 | **FIX (now, make them log)** |
| M12a | Hook "priority chain" only runs matching_hooks[0] (doc/behavior lie) | Med | S | + silent-bug or doc fix | **FIX (now, reconcile code↔doc)** |
| M5a | Cloner hang-fix landed in 1 of 5 sibling methods (live latent bug) | High | S–M | + fixes a real latent hang | **FIX (now — the bug only, not the rewrite)** |
| M6 | 0 of 96 tool bodies unit-tested | High | M | ++ enabler; gates M4/M5 | **FIX (scoped: dispatch + cloners + bug-prone tools)** |
| M5b | Consolidate 5 cloners → 1 parameterized (−700–900 LOC) | High | L | ++ maintainability, but risky | **FIX (gated on M6)** |
| M4 | server.py 4,207-line god object | High | L | ++ priority #1, but big/risky | **FIX (staged: Ph1 now = extract 700L eviction + registry + error envelope; Ph2 = incremental section split as touched)** |
| M13 | spawn_browser 236-line god method | Med | M | + enables testing central op | **FIX (bundle into M4/M6)** |
| M14 | Docs don't let a new hire run/test/ship | Med | S–M | + = the Codify stage anyway | **FIX (as Stage 4 Codify)** |
| M10b | Full error-envelope unification | Med | M | ~ nice-to-have | **DEFER (do opportunistically inside M4)** |
| M11b | Full DI/reset seam for 15 singletons | Med | M | ~ big, low daily pain | **DEFER (couple with M4 Ph2)** |
| M4-Ph2 | Full 11-section split of server.py | High | L | ~ value tapers after Ph1 | **DEFER (only if it keeps hurting after Ph1)** |
| M12b | Sandbox the operator-authored exec() | Low | L | − security theater for a local self-inflicted path (adversarial downgraded to Low; no page→exec) | **REJECT** |

## Why the splits (deeper look)

- **M4 is not big-bang.** Priority #1 is maintainability, so the god file must shrink — but a 4,207→11-module split at 40% coverage is the single riskiest edit in the plan. The honest highest-leverage slice is Phase 1: lift the ~700 LOC of clone-eviction filesystem logic that *doesn't belong in a tool-registry file* (F-201), formalize the `section_tool` registry, and standardize the error envelope. Sections then split incrementally as they're next touched. Full split is DEFERred until Ph1 proves insufficient.
- **M5 splits into a bug and a refactor.** F-142 (hang-fix in only 1 of 5 sibling extraction methods) is a *live latent bug* — fix it now, standalone, small. The 700–900 LOC consolidation is a genuine maintainability win but must wait for M6 characterization tests or it's blind.
- **M9/M10/M11 keep only the cheap, high-value half.** The byte-cap, the "make silent excepts log," and the "don't kill processes at import" guards are each S-effort and kill the actual harm; the fuller refactors (envelope, DI seam) are DEFERred as opportunistic.
- **M12 splits fix vs reject.** The doc/behavior mismatch (F-163) is a real silent bug — fix it. Sandboxing CPython `exec()` of operator-authored code on a local single-user tool is high-effort with ~no real-world payoff (adversarial verified no untrusted→exec path) — REJECT.

## Execution waves (leverage + dependency order)

**Wave 1 — See & control the backend (unlocks all diagnosis; high daily value):**
1. M3 observability spine — file `RotatingFileHandler` under state dir, unconditional INFO+, background flush, per-request correlation id.
2. M1 app-level liveness — real MCP/`/healthz` probe feeding status/doctor/watchdog/eviction (verified via M3 logs).
3. M8 recovery CLI — `stop`/`restart`/`kill-orphans` + print PID (also a manual escape hatch for M2).

**Wave 2 — Dev-loop pain & footguns (cheap, maintainer-facing):**
4. M2 source-hash (or mtime) reuse key + fix-or-remove `hot_reload`.
5. M7 offload teardown kill to a thread executor under the timeout.
6. M11a lazy/guarded ProcessCleanup init (+ set opt-out in conftest).
7. M15 serialize `BrowserInstance` model in storage; rename the misnomer.
8. M9 byte-cap/ring-buffer the body store; capture opt-in/filtered by default.

**Wave 3 — Safety net, then the latent bug & cheap health:**
9. M6 characterization tests (dispatch + cloner outputs + bug-prone tools).
10. M5a propagate the CDP hang-fix to the other 4 cloner methods.
11. M10a make the 22 silent excepts log.
12. M12a reconcile hook-chain code↔doc.

**Wave 4 — Structural (after the net exists):**
13. M4 Ph1 extract eviction + registry + error envelope (bundle M13 spawn_browser extraction).
14. M5b consolidate cloners → 1 parameterized engine (−700–900 LOC).

**Wave 5 — Codify (pipeline Stage 4):**
15. M14 CLAUDE.md + DESIGN.md + RUNBOOK.md + CONTRIBUTING.md, validated by a cold-agent onboarding re-run.

## Proceeds to Stage 2 planning (one Opus plan per item, human approves each)
FIX set in wave order: **M3, M1, M8, M2, M7, M11a, M15, M9, M6, M5a, M10a, M12a, M4-Ph1(+M13), M5b, M14.**
Trivially-related items to batch into one plan: {M3, M10a} (logging touches the same excepts), {M11a, M15} (both small storage/singleton edits), {M4-Ph1, M13} (same file decomposition).
DEFERRED: M10b, M11b, M4-Ph2. REJECTED: M12b.
