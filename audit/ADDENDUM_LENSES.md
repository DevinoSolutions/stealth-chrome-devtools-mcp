# ADDENDUM — Four maintainability lenses + security scope reduction

**Received from the human 2026-07-02, mid-Stage-2 (after plan_M3 and plan_M1 were approved).**
**Authority: supplements STAGE2_BRIEF.md and the governing pipeline spec. Applies to all remaining pipeline work.**

## The directive (verbatim)

> Add four maintainability lenses (they weight within priority 1):
>
> 1. **Modularity** — high cohesion, low coupling (coupling only where the domain truly demands it). This matters more than usual: agents work with limited context windows, so every module must be understandable in isolation. Flag anything that can't be.
> 2. **Deduplication** — each concept/behavior should live in exactly one place. Duplicated logic across N sites = one finding naming the canonical home.
> 3. **Clarity** — file, schema, and method names must be self-describing enough that even a lighter AI model can navigate and place changes correctly WITHOUT reading callers or bodies. Names that lie or under-explain are findings; renames are legitimate fixes.
> 4. **Conventions** — one way to do each thing, followed everywhere. Drift is a finding, and any fix that introduces a second way of doing something is a defect.
>
> Add categories `modularity|duplication|clarity` to the finding schema. Same rules as before: verbatim quotes required, symptoms collapsed to root causes.
>
> Scope reduction: the general security architecture has already been reviewed and is solid. Do NOT re-audit it or propose security re-architecture — report only concrete, quote-backed regressions (exposed secrets, injection, dangerous defaults). Reallocate that effort to the four lenses above.
>
> (Also: re-scan already-covered areas only through these new lenses, don't redo completed work.)

## Orchestrator interpretation at the current pipeline position (Stage 2, plans 1–2 approved)

1. **Finding schema** gains categories `modularity|duplication|clarity` (plus a `lens` tag). Applies to the lens re-scan below and to every new finding filed from Stage 2/3 onward (incl. mid-fix discoveries).
2. **Lens re-scan (supplementary Stage-1b):** 4 agents, one per lens, sweep `src/` at the pinned SHA `2267b83d` through their lens ONLY. They must NOT re-file root causes the audit already holds; where a lens confirms an existing finding they may add corroborating instances marked `extends`. Output: `audit/stage1/findings_lens_<lens>.json`, ID ranges F-700–719 (modularity), F-720–739 (duplication), F-740–759 (clarity), F-760–779 (conventions). Verbatim-quote rule + root-cause collapse unchanged. Results are quote-verified, merged, and presented to the human as a **proposed delta** at the next plan gate — the plan-of-record is not re-litigated without human approval.
3. **Planner preambles** (M8 onward) carry the four lenses verbatim + the rule "a fix that introduces a second way of doing something is a defect" + the security scope reduction.
4. **Already-approved plans re-checked against the lenses (2026-07-02):**
   - plan_M3: passes — one canonical logging module, reuses the existing state-dir convention, self-describing names (`logging_setup`, `configure_logging`, `resolve_log_dir`).
   - plan_M1: passes with one recorded tension — `_backend_http_ready` deliberately COPIES ~10 lines of `_await_backend_http`'s initialize body (to stay disjoint from M3-owned code during serial execution). Under the deduplication lens this is a tracked debt: already logged in state.json cross_review_notes as a future finding naming the canonical home (one shared probe body in `singleton.py`). Consolidate post-M1 landing; do not reopen the approved plan.
5. **Security:** consistent with triage — M12b (exec sandboxing) stays REJECTED; no security re-architecture will be planned. Only concrete quote-backed regressions (exposed secrets, injection, dangerous defaults) may be filed, under existing rules.

## Already-covered root causes (lens agents: do NOT re-file; extend only)

- Modularity: **M4** (server.py 4,207-line god object; F-101/F-505/F-612/F-201), **M13** (spawn_browser 236-line god method; F-208, F-106), **M11** (import-time singletons, no DI seam; F-124/F-125/F-103).
- Deduplication: **M5** (five overlapping cloners; F-140/F-203/F-601/F-142), **F-141** (to-file boilerplate ×8), **F-143** (dead dual-schema fallback), **F-144** (per-call vs singleton cloner), **F-165** (dup header loop), **M15** (state duplicated 4–5 ways; F-207).
- Clarity: **M15/F-122** (`persistent_storage`/`InMemoryStorage` misnomer), **F-108** (tool count disagrees 90/96/99).
- Conventions: **M10** (4+ error shapes; F-104/F-181), **F-602** (ad-hoc getenv), **F-603** (timeout preamble ×49), **F-604** (sys.path fragility), **F-613** (import strategy inconsistency).
