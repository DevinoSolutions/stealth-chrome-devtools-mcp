# RESUME BRIEF — read this first after compaction

**What this is:** a foundational-audit pipeline (AUDIT → triage → PLAN → approve → **[quality-gates interlude]** → FIX → CODIFY) for `stealth-chrome-devtools-mcp`. Conversation memory is gone after compaction; **everything authoritative is on disk under `audit/`.** Read this file, then `audit/state.json`.

## Where we are (updated 2026-07-06, post-convergence)
- **Stages 0, 1, 1b, triage, 2 (PLAN), 2.5-gates, and branch CONVERGENCE are COMPLETE.**
- **2.5-gates LANDED** (14 commits): ruff 0 violations, ty 0 errors/39 warnings (src/ gated scope; 64 diagnostics repo-wide, no floor yet — open finding N-2), vulture 0 findings (WITH the allowlist arg), all 6 gates green, husky armed, CI quality job added. 415 tests, 42% coverage (gate 41). 56 inline noqa + 21 per-file-ignores, all owner-tagged.
- **CONVERGED on `main`** (e39be31 + bookkeeping commits): PR #20→#21 verified lossless (empty diff vs fix-branch tip); six stale branches deleted local+remote; tags `audit-baseline`@2267b83, `gates-final`@3edac20, `archive/perf-spawn-and-timeouts`@2bc7504; the whole `audit/` corpus is git-tracked; `CODEBASE_AUDIT.md` lives at `audit/prior/`. **perf/spawn-and-timeouts was SUPERSEDED-BY-REWORK — never merge it** (its content evolved on main; it overlaps M2/M7/M9 territory).
- Four approved amendments ride their plans in-file: M3-A1 (F-764 RLock), M8-A1 (F-509 auto-port-fallback), M4Ph1-A1+A1.5 (full 21-site error-envelope sweep with G1–G7 pins), M14-A1 (F-741 CLI renames + **X-HARD** env rename). `plan_M5a.md` is NOT executable — folded into M5b.

## NEXT ACTIONS (in order)
1. **Phase-2 targeted re-audit — DONE 2026-07-06** (stage `2.7-reaudit`; full detail in `audit/reaudit/REPORT.md`). 39 re-verdicted: 35 STILL_VALID (incl. both Criticals), 3 FIXED_BY_GATES (F-602/F-720/F-763), 1 PARTIALLY_FIXED (F-762); **no regressions from the gates**; 6 NEW findings G-1..G-6. Fable re-verified 5 load-bearing claims in source.
2. **Optional pre-Stage-3 cleanups** (recommended, small): fix **G-1** (owner-tag gate is inert — governance currently fake) + **G-2** (src-only noqa scan) + **G-4** (pin husky exact); enable **branch protection on `main`** (CI runs but nothing blocks a red merge).
3. **Stage 3 FIX**: fresh session, paste `audit/STAGE3_RESUME_PROMPT.md`. Baseline from `stages['2.5-gates'].landed` (`head_sha` e39be31 = ancestor-of-HEAD check, NOT equality). Branch `audit/fixes-2026-07-02` off main.

## PARKED ITEMS — deferred on purpose, NOT hidden (human ruling 2026-07-07: "leave them for later but don't hide them")
- **39 known dependency vulnerabilities** (re-scanned 2026-07-07; was 34 at stage-0 — 5 new advisories against the same pins; inventory: `audit/dep_audit_2026-07-07.md`) — owned by NO plan. fastmcp (6 IDs, fixes span 2.13→3.2) is the load-bearing bump; M3-5's tools/list schema-snapshot test now pins its blast radius. Needs pin bumps eventually.
- **G-3 (Low)**: stdio-proxy shim entrypoint inits no Sentry — client-side proxy failures unreported. Natural home: fold into M3-era observability or a follow-up.
- **G-5 (Low)**: venv-python resolver duplicated verbatim in `.husky/pre-commit` + `.husky/pre-push`.
- **G-6 (Low)**: settings.py loud-rejection guards only the `STEALTH_MCP_` prefix; typo'd legacy unprefixed env var is silently ignored (.env-file typos ARE caught).
- **ty warning-floor ratchet**: 39 in-scope warnings, no floor — warnings can grow silently until a shrink-only gate is added (same pattern as the coverage gate).
- **F-762 residue**: STATE_DIR duplication half of the finding (idiom half fixed by gates).

## HONEST STATUS — what is NOT done / NOT tested (do not let summaries imply otherwise)
- **Plan 1 of 12 (M3+A1+M10a) is MERGED to main** (2026-07-07): PR #22 @ 37bb34c + stacked PR #23 @ 3c7b60c (both merge commits); main @ 3c7b60c carries the full plan-1 payload (+2453/−94, 32 files). Post-merge validation: 490 unit tests + all 6 gates + **24/24 integration tests (first live-browser run of the campaign)** green on merged main. The other 11 plans exist only as plans. The two Critical findings (wedged-but-alive backend jams eviction forever, F-301/F-501) are **still live bugs** — M1 (in flight on `audit/fixes-2026-07-02-m1`) closes them.
- **The characterization net (M6) does not exist yet** — there are currently NO behavior pins over the 96-tool dispatch surface; the structural refactors (M4-Ph1, M5b) are unprotected until M6 lands (that ordering is designed-in: M6 is plan 8 of 12).
- Coverage ~43.4% (gate 41). **Integration tests ran for the first time 2026-07-07 on merged main: 24/24 green** — but stages 0–2 never ran them, so original audit findings remain quote-verified against source only; the 24 tests cover proxy/handshake/backend-death paths, not the full 96-tool surface (M6 characterization net still pending).
- Plan anchors were verified at pinned SHA `2267b83d` only; the gates workstream will shift lines repo-wide (format commit + env-read migration) — plans re-anchor by SYMBOL, §1.3 line tables become approximate.
- **34 known dependency vulnerabilities** (stage-0 tooling scan) are owned by NO plan and are OUT of the gates scope — an open item.
- The gates workstream is DONE. Pre-push timing: ~65-80s on the original machine (acceptable).
- Deferred by design (recorded, not forgotten): M10b/M11b/M4-Ph2 (DEFER), M12b (REJECT), plus the M14 debt ledger items (F-740, F-702, F-703, F-603 cross-module, F-606, F-765, M1 probe-body dedup, styles-twin merge).

## Stage 3 discipline (detail + per-plan rulings in STAGE3_RESUME_PROMPT.md and state.json)
- Branch `audit/fixes-2026-07-02` off the **post-gates HEAD** (recorded in `2.5-gates.landed`).
- **Serial order:** M3+A1(M10a) → M1 → M8+A1 → M2 → M7 → M11a+M15 → M9 → M6 → M12a → M4-Ph1+A1 → M5b → M14+A1. Each plan executes from the prior plan's final commit; landing M14 completes Stage 4 (CODIFY) too.
- One PR per plan (M3 = two stacked PRs). Pinning/characterization tests FIRST; full non-integration suite green at every checkpoint; **husky gates green at every checkpoint, `--no-verify` banned**; each plan deletes its `plan_M<N>`-tagged suppressions; re-anchor by SYMBOL.
- Gates-era adaptations are PRE-AUTHORIZED and recorded in `cross_review_notes` (last 4 entries): `env_utils.py` is SUPERSEDED by `settings.py` (never create it); new plan env vars become Settings fields; M14's X-HARD env-rename edit site moved into `settings.py`.
- The 4 lenses (`audit/ADDENDUM_LENSES.md`) bind fixes — a fix introducing a second way of doing something is a defect. **Deviation from an approved plan → STOP and ask the human.** Update + JSON-validate `audit/state.json` after each plan lands.

## Pinned facts (post-gates baseline — read from `stages['2.5-gates'].landed`)
- Branch `main` (the only branch — converged). **Gates ACTIVE** — husky pre-commit runs 6 checks, pre-push runs unit tests.
- Baseline: **415 passed**, 42% coverage, gate 41. **`uv run` is BROKEN in this checkout** (path has `&`+spaces) — always use `.venv\Scripts\python.exe` directly; `uv` works in CI.
- Context: LOCAL single-user tool, 0 external users — breaking changes are FREE. Priorities: (1) maintainability (2) operability (3) performance (order-of-magnitude only).
- Tool count: **96 at HEAD** (gates changed nothing); **94 post-M2**, pinned by M6, unchanged through M14.
- OneDrive checkout quirk: subagent writes can look missing to a fresh read for minutes — check mtime+size and ask the agent before re-tasking.

## Authoritative docs map
- `audit/state.json` — master state: per-plan status, **resolved_decisions (binding)**, **cross_review_notes (35, binding cross-plan directives)**, `2.5-gates` block, history.
- `audit/GATES_TASK_OPUS.md` — paste-ready quality-gates task for a fresh Opus chat (runs FIRST).
- `audit/STAGE3_RESUME_PROMPT.md` — paste-ready Stage-3 kickoff (runs AFTER gates; self-gating on `2.5-gates.landed`).
- `audit/stage2/plan_*.md` — the 12 approved plans (+ folded plan_M5a analysis). Each carries verified anchors, §1.3 shift tables, test strategy, rollback, and its approval stamp/gate rulings. **Anchors in plans supersede any older cheat-sheets; symbols supersede line numbers post-gates.**
- `audit/ADDENDUM_LENSES.md` — the 4 binding lenses. `audit/REPORT.md` — Stage-1 synthesis (15 root causes). `audit/findings.json` — 99-record ledger (extract via script, don't read whole). `audit/TRIAGE.md` — superseded by STAGE2_BRIEF/state.json where they differ.
