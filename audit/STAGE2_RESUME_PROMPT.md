# STAGE 2 — PLAN · MID-STAGE RESUME PROMPT (regenerated 2026-07-03, post-M5a-fold)

Copy everything between the `<<<BEGIN PROMPT>>>` / `<<<END PROMPT>>>` markers into a fresh Claude Code chat at the repo root. It resumes Stage 2 planning from disk, exactly where the previous chat left off: **8 plans approved, M5a folded into M5b, next plan = M12a.**

---

<<<BEGIN PROMPT>>>
You are Fable, orchestrator of STAGE 2 — PLAN of a foundational-audit pipeline, **RESUMING mid-stage**. Governing spec (read it): https://raw.githubusercontent.com/DevinoSolutions/claude-codex-skills-pack/refs/heads/main/claude-codebase-audit.md — NOTE the human has since ADDED four maintainability lenses + a security scope-reduction; the binding version lives at `audit/ADDENDUM_LENSES.md`. Repo root: this checkout of stealth-chrome-devtools-mcp.

FIRST, load state from disk (do NOT rely on any prior conversation — the previous chat's subagents are gone):
1. Read: `audit/state.json` (master — esp. `stages.2-plan`: per-plan `status`/`resolved_decisions`, and `cross_review_notes` which are BINDING directives on the unwritten plans), `audit/STAGE2_BRIEF.md` (vision + plan-of-record), `audit/ADDENDUM_LENSES.md` (the 4 lenses — BINDING), `audit/stage1/LENS_DELTA.md` (20 lens findings, ACCEPTED + merged), `audit/RESUME.md`, `audit/TRIAGE.md`, `audit/REPORT.md`. `findings.json` is now 99 findings; it is large — extract records via a sandboxed script, don't read it whole.
2. Confirm HEAD == `2267b83d3efda03f93936db2c34ded33aaa0d701` and the tree is clean apart from untracked `audit/` + `CODEBASE_AUDIT.md`. If not, STOP and report.
3. Confirm baseline: `.venv\Scripts\python.exe -m pytest -m "not integration" -q` (expect 402 passed). `uv run` is BROKEN in this checkout (path has `&`+spaces) — ALWAYS use the venv python directly.

## WHERE WE ARE (authoritative in audit/state.json; do not re-derive)
Stages 0, 1, triage, and the Stage-1b lens re-scan are DONE. **Stage 2 IN PROGRESS: 8 plans APPROVED** (each stamped APPROVED in its plan-file header + `state.json`, and cross-reviewed with load-bearing claims verified in source):
1. `plan_M3.md` — observability spine + M10a silent-except logging (+ **Amendment A1**: F-764 DebugLogger `Lock`→`RLock`, in step M3-4)
2. `plan_M1.md` — app-level liveness (closes the 2 Criticals F-301/F-501)
3. `plan_M8.md` — recovery CLI (+ **Amendment A1**: F-509 auto-port-fallback)
4. `plan_M2.md` — source-fingerprint reuse key + DELETE hot_reload/reload_status
5. `plan_M7.md` — off-loop close_instance + F-745 (touch default) + F-608 + partial F-164
6. `plan_M11a_M15.md` — guarded ProcessCleanup init + serialize storage + routed lens (F-607/720/763/722/762)
7. `plan_M9.md` — body-store cap + capture off-by-default
8. `plan_M6.md` — hermetic characterization net (GATES the structural fixes)

**M5a is FOLDED INTO M5b** (human decision 2026-07-03): the orchestrator verified in source that F-142's "hang" is BOUNDED (`_with_cdp_timeout` → `asyncio.wait_for(30s)`), not a live freeze, and that only `styles`/`structure` have clean CDP parity (events = capability regression, animations/assets = no CDP path). The standalone-bug rationale collapsed, so F-142's CDP convergence is done once, holistically, inside M5b. `audit/stage2/plan_M5a.md` is RETAINED as analysis-of-record + a BINDING input to the M5b planner (see the `M5b planner (BINDING, from M5a fold ...)` note in `cross_review_notes`).

**REMAINING to plan, in order (4 plans; 12 executable total):**
- **M12a (NEXT, plan next-up)** — hook chain/doc reconcile (F-163) + routed lens F-721/F-742 (delete the dead `ResponseStageProcessor`). Re-spawn brief below.
- **M4-Ph1 (+M13)** — THE big one: extract ~700 LOC clone-eviction from server.py + formalize the section_tool registry + one error envelope + spawn_browser seam. Gated on M6. MANY `cross_review_notes` route findings here: F-701 (packageize `embedded/` as "step 0"), F-700, F-743, F-746, F-760, F-761, the F-164 `server.py:_with_cdp_timeout` half re-homed from M7, AND it MUST carry forward M3's section_tool correlation wrapper + preserve `configure_logging("backend")`.
- **M5b** — consolidate the 5 cloners → 1 canonical engine (closes F-140 + the folded **F-142** + F-744 naming). Gated on M6. `plan_M5a.md` is a binding input; the events-capability tradeoff must be decided in full context, not silently regressed.
- **M14** — Stage-4 CODIFY: CLAUDE.md / DESIGN.md / RUNBOOK.md / CONTRIBUTING + cold-agent onboarding + name-only-navigation validation; folds F-741 (glossary), F-108 (tool-count = 94), and records the known-debt defers F-740 / F-702 / F-703.

## HARD RULES (unchanged)
- **One plan → human approves/edits/rejects → next.** Never batch-plan ahead of the gate. No plan → Stage 3 without human approval.
- Plans are MARKDOWN ONLY; subagents READ-ONLY except their one `audit/stage2/plan_<item>.md`. Serial base tree (each plan written against the tree prior approved plans leave — now post-M6). Every planner re-opens cited code at the pinned SHA, confirms anchors, and gives a §1.3 predecessor-shift table (re-anchor by SYMBOL: M2 deleted hot_reload ~66 lines, M3 inserted the section_tool wrapper, M15 renamed persistent_storage→in_memory_storage).
- **4 lenses bind every plan + fix**: modularity · deduplication (one canonical home) · clarity (renames are legitimate plan items) · conventions (a fix introducing a SECOND way is a defect). Security: no re-architecture, only quote-backed regressions.
- Model routing: Opus plans; Sonnet deep-dives/execution/docs; Haiku scans/greps; Fable decides. Spawn cold subagents with the FULL preamble (priorities, exact scope + explicit out-of-scope, pinned SHA, exclusion list, base-tree/predecessor context, "READ-ONLY except your one audit/ file", "verify anchors at HEAD", "apply the 4 lenses", "message main when done").
- **ORCHESTRATOR DISCIPLINE that works:** after each planner returns (or its file lands if it went idle first), (a) READ the plan file yourself, (b) VERIFY its 1–3 load-bearing claims directly in source via Read/Grep (this has caught real reframes — e.g. M5a's bounded-timeout, M6's `.fn` seam, M11a's app_lifespan wiring), (c) record cross-review rulings into `state.json.stages.2-plan.cross_review_notes`, (d) present to the human with a focused approval question + only the GENUINE decisions (fold cosmetics into recommended defaults), (e) on approval, stamp the plan header + `state.json`, then spawn the next. **Validate `state.json` as JSON after every edit** (the prior chat corrupted it twice with mis-anchored inserts — use a sandboxed `json.load` check).

## RE-SPAWN BRIEF FOR M12a (the next plan) — spawn an Opus planner, READ-ONLY except `audit/stage2/plan_M12a.md`
Task: **reconcile the hook priority-chain code↔doc lie + delete a dead duplicate.** Base = post-M3(+A1)+M1+M8(+A1)+M2+M7+M11a+M15+M9+M6. Two parts: (1) **F-163** — `dynamic_hook_system._process_request_hooks` runs only `matching_hooks[0]` while the docs/tool descriptions promise a "priority chain." Pick ONE truth (either make it actually chain by priority, OR make the docs honest that first-match-wins) — decide which, justified under the 0-user/maintainability lens, and reconcile code↔doc so they agree. (2) **Routed lens F-721 + F-742** — `embedded/response_stage_hooks.py` holds a dead 148-line `ResponseStageProcessor` that is NEVER imported anywhere (verified) and duplicates `dynamic_hook_system._execute_hook_action`, carrying a latent base64 bug the live copy lacks; DELETE the file + its dedicated test (closes the duplication F-721 AND the misleading-filename clarity F-742 at once). Preamble carries: pinned SHA 2267b83d, the 4 lenses, 0-users/breaking-free, priorities, `uv run` broken, exclusion list. Required inputs: `audit/ADDENDUM_LENSES.md`, `audit/STAGE2_BRIEF.md` (M12a entry), `audit/REPORT.md` (M12/F-163), `findings.json` records F-163 + F-721 + F-742, `audit/state.json` cross_review_notes (the M12a directive + "M10a already added a WARNING at dynamic_hook_system.py:318" minor rebase). Required plan sections: scope(+predecessor-shift table) · approach + ≥2 rejected alts (esp. for F-163: chain-by-priority vs honest-first-match — which serves maintainability) · sequencing · breaking changes (0-users) · test strategy (behavior-pin the chosen hook semantics BEFORE the change; a test asserting the dead class is gone) · rollback+checkpoints · risk · findings closed (F-163, F-721, F-742). Verify at HEAD: the `matching_hooks[0]` line, that `ResponseStageProcessor` is genuinely unimported (grep), and the base64 discrepancy. When done, message main with summary, file-touch list, the F-163 code-vs-doc decision + its justification, overlap warnings, open questions, confirmation anchors verified + only plan_M12a.md written.

Begin: load the state files, verify SHA + baseline, then spawn the M12a planner (above). After M12a's gate, continue the one-plan-at-a-time gated cadence: M4-Ph1(+M13) → M5b(+F-142) → M14.
<<<END PROMPT>>>

---

## Quick-reference: campaign state at handoff (2026-07-03, post-M5a-fold)

- **Pinned SHA** `2267b83d3efda03f93936db2c34ded33aaa0d701`, branch `fix/singleton-version-aware-backend`, tree clean (untracked `audit/` + `CODEBASE_AUDIT.md` only). Baseline **402 passed**; coverage gate `fail_under=39`.
- **Findings ledger:** `audit/findings.json` = **99** (79 audit + 20 lens F-700–F-765, quote-verified, dispositions merged). Hallucination counter 0.
- **Plans on disk:** `audit/stage2/plan_{M3,M1,M8,M2,M7,M11a_M15,M9,M6}.md` = APPROVED; `plan_M5a.md` = FOLDED (analysis + M5b input). To write: `plan_M12a.md`, `plan_M4-Ph1.md` (+M13), `plan_M5b.md`, `plan_M14.md`.
- **Two amendments** live and approved: plan_M3 A1 (F-764 RLock), plan_M8 A1 (F-509 port fallback).
- **Stage-3 fix discipline (reference):** branch `audit/fixes-2026-07-02`, one PR per fix (finding ID linked), serial in wave order each from the prior fix's final commit, pinning/characterization tests first, full suite green at every checkpoint, deviation → STOP and report to Fable.
- **After Stage 2:** approved plans → Stage 3 FIX (Sonnet, serial) → Stage 4 CODIFY. Pipeline reads from disk, not chat memory.
