# STAGE 2 GO-FORWARD BRIEF — the goal from here

**This is the authoritative direction doc for the fix campaign. It does not get re-litigated between sessions.**
Pinned SHA `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend` · 2026-07-02.
Pipeline spec (governing): https://raw.githubusercontent.com/DevinoSolutions/claude-codex-skills-pack/refs/heads/main/claude-codebase-audit.md
Detail lives in: `audit/REPORT.md` (findings + scores), `audit/findings.json` (machine ledger), `audit/TRIAGE.md` (first-pass triage), `audit/RESUME.md` (cold-start cheat-sheet). This brief supersedes TRIAGE.md where they differ.

> **⚠️ ADDENDUM 2026-07-02 (`audit/ADDENDUM_LENSES.md`):** four maintainability lenses now weight within priority 1 — modularity (modules understandable in isolation), deduplication (one canonical home per concept), clarity (self-describing names; renames are legitimate fixes), conventions (one way per thing; a fix introducing a second way is a defect). Finding schema gains `modularity|duplication|clarity`. Security is scope-reduced to concrete quote-backed regressions only. Binds every plan below.

## Operating principles (the lens for every decision)
- **Vision:** an *undetectable* browser-automation MCP server — a broad AI-driven tool surface (96 tools) over a stealth nodriver/CDP browser, whose headline capabilities are **element extraction/cloning**, **network interception**, and **dynamic hooks**. The product IS the tool surface + those crown-jewel features, all routed through one per-user backend.
- **0 users to maintain. Breaking changes are free.** Optimize purely for the *maintainer's* ability to (1) change the code safely and (2) operate it under failure. Do not preserve any behavior, signature, tool name, or config for compatibility. If ripping something out is simpler than fixing it, rip it out.
- **Priority order:** maintainability > operability > performance (order-of-magnitude only).
- **The reframe that matters:** the only thing that made the two big structural fixes (god-file split, cloner consolidation) "risky" was compatibility. With 0 users that risk is gone; the *only* residual risk is internal correctness, which a test net (M6) covers. So both are now **FIX**, gated on tests — not deferred.

## Deep value assessment (worth × leverage − effort − risk), tiered

**Tier A — the product is DOWN or un-iterable without these. Do first.**
- **M3 Observability spine** — the backend's stdout/stderr are `DEVNULL` and the logger is off-by-default in-memory; the product is a black box and *every other fix is unverifiable without it*. Highest enabling value. **FIX (first).**
- **M1 App-level liveness** — a wedged backend reads "healthy" forever = silent total outage of the whole product; the sole auto-recovery watchdog polls the same blind signal. Single highest defect. **FIX.**
- **M2 Live edits take effect** — reuse keyed on version *string* + a `hot_reload` that silently no-ops means the maintainer cannot reliably iterate on the server. For a 0-user tool whose only stakeholder is the developer, this is the #1 velocity tax. Simplest correct fix: **dev = always-fresh backend, and delete `hot_reload`** rather than repair it. **FIX.**
- **M7 Teardown freeze** — sync kill under a fictional `asyncio.wait_for` means one bad `close_instance` freezes every session on the shared backend. Cheap, high. **FIX.**
- **M8 Recovery CLI** — turns a dead product into a one-command restart (`stop`/`restart`/`kill-orphans`, print PID); also the manual escape hatch for M2. **FIX.**

**Tier B — protects a crown-jewel feature or removes a footgun. Cheap, high worth.**
- **M5a Cloner latent bug** — the CDP hang-fix landed in 1 of 5 sibling extraction methods; the other 4 (structure/events/animations/assets) still use the hang-prone JS path → a headline feature can hang. Fix the bug now, standalone. **FIX (now).**
- **M9 Body-store OOM** — interception is default-on and retains full bodies unbounded → long sessions on a headline feature OOM. With 0 users: **flip capture to opt-in/off-by-default + byte-cap.** **FIX (now).**
- **M11a Import-time process kill** — importing the package reaps real processes; `conftest` never opts out → a genuine footgun + blocks clean tests. **FIX (now, lazy/guarded init).**
- **M15 Silent field-drop** — storage hand-builds a 6-field subset, silently dropping every other option → directly taxes the core "add a tool/option" loop. **FIX (now, serialize the model).**

**Tier C — makes the product's core safe to evolve. High worth, gated on the test net.**
- **M6 Characterization tests** — 96 tools with ~0 body coverage; you cannot safely evolve the surface (the whole product) without this. Scoped first (dispatch + cloners + bug-prone tools), then widen. **FIX (enabler; gates C-rest).**
- **M4 Decompose `server.py`** — the tool surface breadth is the product; making it safe to add/modify tools is directly vision-serving (priority #1). **FIX: Ph1 now** (extract the ~700 LOC eviction logic that doesn't belong + formalize the `section_tool` registry + one error envelope); **Ph2 full per-section split gated on M6** (upgraded from "defer" — with 0 users the split is free of external risk).
- **M5b Cloner consolidation** — halve a crown-jewel feature's code (−700–900 LOC), kill the drift that caused M5a. **FIX (gated on M6).**
- **M13 `spawn_browser` god method** — the most central op is untestable (236 lines, no seam). **FIX (bundle into M4-Ph1 + M6).**

**Tier D — cheap correctness/clarity.**
- **M10a Silent excepts → log** — the 22 truly-silent handlers swallow the exact diagnoses M3 wants to capture. **FIX (now).** (Full error-envelope = absorbed into M4, not a standalone task.)
- **M12a Hook chain/doc lie** — `_process_request_hooks` runs only `matching_hooks[0]` while docs promise a "priority chain." Reconcile code↔doc. **FIX (now).**

**Tier E — not worth it.**
- **M12b Sandbox the `exec()`** — operator-authored, local, no untrusted→exec path (adversarially verified); with 0 users there's even less reason. Sandboxing CPython exec is high-effort theater. **REJECT.**
- **M10b full error envelope, M11b full DI/reset seam** — real but low daily pain; **DEFER by absorbing into M4** opportunistically, never as standalone tasks.

## Net change from first-pass triage
Under the 0-user/breaking-free lens, **M4 full split and M5b consolidation move from "defer unless it keeps hurting" to "FIX, gated on M6."** Everything else holds. One hard REJECT (M12b). The DEFERs are now "absorb into M4," not "schedule later."

## Plan-of-record (wave order = plan order for Stage 2)
1. **M3** observability spine
2. **M1** app-level liveness  *(verified via M3)*
3. **M8** recovery CLI
4. **M2** always-fresh dev backend + delete hot_reload
5. **M7** offload teardown to executor
6. **M11a** guarded ProcessCleanup init  *(batch with M15)*
7. **M15** serialize BrowserInstance in storage
8. **M9** body-store cap + capture opt-in
9. **M6** characterization tests  *(gate for 10–14)*
10. **M5a** propagate cloner hang-fix
11. **M10a** silent-except logging  *(batch with M3's logging work)*
12. **M12a** hook chain/doc reconcile
13. **M4-Ph1 (+M13)** extract eviction + registry + error envelope + spawn_browser seam
14. **M5b** cloner consolidation
15. **M4-Ph2** full per-section split  *(only after M6 coverage of touched sections)*
16. **M14** CODIFY (Stage 4): CLAUDE.md + DESIGN.md + RUNBOOK.md + CONTRIBUTING, cold-agent onboarding re-run.
DEFER→absorb: M10b, M11b. REJECT: M12b.

## Stage-handoff protocol (why we start a new chat per stage)
Each stage ends by writing its artifacts to `audit/` and updating `audit/state.json`, then a **fresh chat** is opened for the next stage carrying only the paste prompt below. Rationale (from the pipeline spec): *stages read from disk, not conversation memory* — this keeps context small and makes crashed/compacted runs resumable. Sequence: **AUDIT ✅ → triage ✅ → PLAN (next) → approve each plan → FIX → CODIFY.**

## Fix-branch discipline (Stage 3, for reference)
Work on `audit/fixes-2026-07-02`; one PR per fix (finding ID + plan linked); serial execution in wave order, each fix from the previous fix's final commit; full suite green at every checkpoint; pinning tests written before the change. Deviation → stop, report to Fable.

---

## ▶ PASTE-READY PROMPT FOR THE NEXT CHAT (Stage 2 — Plan)
Copy everything between the lines into a new Claude Code chat at the repo root.

<<<BEGIN PROMPT>>>
You are Fable, orchestrator of STAGE 2 — PLAN of a foundational-audit pipeline. Governing spec: https://raw.githubusercontent.com/DevinoSolutions/claude-codex-skills-pack/refs/heads/main/claude-codebase-audit.md (read it). Repo root: this checkout of stealth-chrome-devtools-mcp.

FIRST, load state from disk (do NOT rely on any prior conversation):
1. Read audit/RESUME.md, audit/STAGE2_BRIEF.md, audit/TRIAGE.md, audit/REPORT.md, audit/findings.json, audit/state.json.
2. Confirm HEAD == 2267b83d3efda03f93936db2c34ded33aaa0d701 and the tree is clean (`git rev-parse HEAD`, `git status`). If not, STOP and report.
3. Confirm the test baseline with `.venv\Scripts\python.exe -m pytest -m "not integration" -q` (expect 402 passed). NOTE: `uv run` is BROKEN in this checkout (path has `&`+spaces) — always use the venv python directly.

CONTEXT (do not re-derive): This is a LOCAL single-user tool, 0 users, breaking changes are free; optimize for maintainability then operability. The audit is done (79 findings, adversarially reviewed) and triage is done — the FIX set and wave order are fixed in audit/STAGE2_BRIEF.md ("Plan-of-record"). Do not re-triage.

MODEL ROUTING: Opus plans/pre-mortems/adversarial; Sonnet deep dives/execution/doc drafts; Haiku scans/greps/quote-checks; you (Fable) synthesize and decide. Spawn cold subagents with full preamble (priorities, exact scope, the pinned SHA, the exclusion list, "READ-ONLY except your audit/ output", and "message main when done").

YOUR TASK — produce Stage 2 plans, one per FIX item, in the wave order from STAGE2_BRIEF.md, batching the trivially-related pairs {M3,M10a}, {M11a,M15}, {M4-Ph1,M13}. For each, spawn an Opus planner to write audit/stage2/plan_<item>.md containing EXACTLY: scope (exact files + explicit out-of-scope) · approach + rejected alternatives · sequencing (independently verifiable steps) · breaking changes (state "0 users, N/A" where true) · test strategy (behavior-pinning tests written BEFORE the change; characterization tests for risky/undertested areas) · rollback + checkpoint commit boundaries · risk (blast radius, worst case, early-warning signs). Every plan must cite the finding IDs it closes and re-open the cited code to confirm line anchors at the pinned SHA.

RULES: NO code, NO edits to source/tests/config during planning — plans are markdown only. After each planner returns, you cross-review for file overlaps (merge or sequence). Update audit/state.json after each plan.

⛔ HARD GATE: Start with the M3 (+M10a) plan ONLY. Write it, then STOP and present it to the human for approval before planning the next item. Do not batch-plan everything. One plan → human approves/edits/rejects → next plan. No plan proceeds to Stage 3 (fixes) until the human approves it.

Begin: load the state files, verify SHA + baseline, then produce audit/stage2/plan_M3.md and stop for approval.
<<<END PROMPT>>>
