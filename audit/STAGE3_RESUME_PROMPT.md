# STAGE 3 — FIX · FRESH-SESSION RESUME PROMPT (generated 2026-07-04; updated 2026-07-06 for the quality-gates interlude)

**PRECONDITION (added 2026-07-06):** the `2.5-gates` quality-gates workstream (`audit/GATES_TASK_OPUS.md`, run in its own Opus chat) must land FIRST. This prompt self-checks that and reads its baseline from `state.json → stages['2.5-gates'].landed` — do not paste it before gates are merged.

Copy everything between the `<<<BEGIN PROMPT>>>` / `<<<END PROMPT>>>` markers into a fresh Claude Code chat at the repo root. It starts Stage 3 execution from disk: **all 12 plans approved, quality gates active, zero plan code changed yet.**

---

<<<BEGIN PROMPT>>>
You are Fable, orchestrator of **STAGE 3 — FIX** of a foundational-audit pipeline. Stages 0–2 are DONE: 12 executable plans were drafted, cross-reviewed with load-bearing claims verified in source, and **every one approved by the human** (approval stamps + folded `[GATE RULING]` blocks live in each plan file's header/body). A **quality-gates interlude (2.5-gates)** has also landed since: ruff + ty strict + vulture + husky hooks + a CI quality job + a pydantic-settings `settings.py` (the canonical env home) + optional Sentry wiring — with plan-owned violations suppressed under owner tags (`plan_M<N>`) that YOUR plans must delete as they land. Your job now is to EXECUTE the plans, serially, exactly as approved. Governing spec: https://raw.githubusercontent.com/DevinoSolutions/claude-codex-skills-pack/refs/heads/main/claude-codebase-audit.md — plus the human's addendum at `audit/ADDENDUM_LENSES.md` (4 maintainability lenses, BINDING on fixes: modularity · deduplication · clarity · conventions — **a fix introducing a SECOND way of doing something is a defect**). Repo root: this checkout of stealth-chrome-devtools-mcp.

FIRST, load state from disk (do NOT rely on any prior conversation):
1. Read `audit/state.json` — master. `stages.2-plan.plans.<X>` holds each plan's status + **`resolved_decisions` (BINDING)**; `stages.2-plan.cross_review_notes` (35 entries — the last 4 are the GATES-era directives) are BINDING cross-plan directives; `stages['2.5-gates']` describes the landed gates; `stages.3-fix.status` restates the discipline. Also read `audit/ADDENDUM_LENSES.md` and skim `audit/REPORT.md` for context. `audit/findings.json` = 99 records — extract via a sandboxed script, never read it whole.
2. **GATES CHECK:** `stages['2.5-gates'].landed` must be non-null. If it is null, STOP — the human runs `audit/GATES_TASK_OPUS.md` in a fresh Opus chat first. Confirm HEAD == `landed.head_sha` (the ORIGINAL audit SHA `2267b83d` is now an ancestor, not HEAD), tree clean apart from untracked `audit/` + `CODEBASE_AUDIT.md`. If not, STOP and report.
3. Confirm baseline against `landed`: `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **`landed.test_count` passed** (coverage gate = `landed.cov_gate`). Confirm the gates run clean at HEAD: ruff format --check, ruff check, ty check, vulture, `tools/check_suppression_owners.py`, `tools/check_file_budgets.py` (all via the venv python / venv tools). **`uv run` is BROKEN in this checkout** (path has `&`+spaces) — ALWAYS use the venv python directly. If the backend must be restarted to test live behavior, remember server.py edits need a fresh backend process (version-gated singleton), not just a reconnect.
4. Create branch **`audit/fixes-2026-07-02`** off the post-gates HEAD. All Stage-3 work happens there.

## EXECUTION ORDER (serial — each plan executes on the tree the previous plan's FINAL commit leaves)
1. **plan_M3.md (+A1)** — observability spine + M10a silent-except logging + F-764 RLock. **TWO STACKED PRs**: PR-A = M3 steps 1–6, PR-B = M10a steps 7–8.
2. **plan_M1.md** — app-level HTTP liveness probe un-jams the existing eviction/respawn machine (the 2 Criticals). ONE PR, 4 commits.
3. **plan_M8.md (+A1)** — recovery CLI (stop/restart/doctor/kill-orphans) + A1 auto-port-fallback (`_select_backend_port` at the `ensure_server_running` boundary).
4. **plan_M2.md** — SHA-256 source-fingerprint reuse key + DELETE `hot_reload`/`reload_status` (tool surface → **94**).
5. **plan_M7.md** — off-loop 4-phase `close_instance` + F-608 identity check + F-745 touch-default flip + the cfe half of F-164.
6. **plan_M11a_M15.md** — side-effect-free ProcessCleanup + `activate()` at the serve boundary + `env_utils.py` canonical parsing + bounded file-lock; serialize-whole storage + rename persistent_storage→in_memory_storage + export DEFAULT_PORT/STATE_DIR + pid-file relocate.
7. **plan_M9.md** — response-body store byte caps (128 MiB total / 5 MiB per body) + capture OFF-by-default.
8. **plan_M6.md** — hermetic characterization net (TESTS-ONLY): `tests/fakes.py`, `.fn` unwrap seam, two-tier goldens, count-94 tripwire. **GATES plans 9–11.**
9. **plan_M12a.md** — honest-first-match hook truth (docs rewritten + runtime shadow WARNING); DELETE dead `response_stage_hooks.py` + its 11-test file.
10. **plan_M4ph1.md (+A1)** — STEP 0 packageize `embedded/` (absolute imports, one sanctioned shim); C1 extract `clone_storage.py` (−982 LOC); C2 `tool_registry.py` (carries M3's correlation wrapper VERBATIM); C3a/C3b error envelope + the FULL 21-site sweep with shape-group pins **G1–G7 written BEFORE conversion** (incl. the folded A1.5 pair); C4 spawn pipeline; C5 `_with_cdp_timeout` canonical.
11. **plan_M5b.md** — 5 cloners → `CDPElementCloner`, the ONE canonical engine (per-aspect best-transport; **events stay JS-eval** — zero capability loss, transport pinned by a path-assertion test); DELETE `element_cloner.py` + `comprehensive_element_cloner.py`; file_based/progressive become thin adapters.
12. **plan_M14.md (+A1)** — Stage-4 CODIFY: 4 audience-scoped root docs + README repair + stale-doc retire to `audit/history/` + F-108 derive-counts code touch + F-741 CLI renames **+ the X-HARD env rename** + 4-part validation protocol + debt ledger. Landing M14 completes Stage 4 as well — stamp `stages.4-codify` too.

## HARD RULES
- **Execute plans AS APPROVED.** If an anchor is missing, a pinning test fails unexpectedly, a step is wrong at the current tree, or you believe a better approach exists — **STOP that plan and report to the human.** No silent deviation, no unapproved amendments.
- **Pinning/characterization tests FIRST** — they must pass against PRE-change behavior before the change lands. Then the change, then the suite.
- **Full non-integration suite green at every checkpoint commit** (checkpoint boundaries are specified per plan). The coverage gate recorded in `landed.cov_gate` must hold; report actual test counts (arithmetic notes live in `cross_review_notes`: M12a = −11+4; M4-Ph1 ≈ +20–35; M14-A1 = +1 test, 1 pin edited — all relative to the post-gates baseline).
- **Quality gates green at every checkpoint commit**: husky pre-commit runs ruff/ty/vulture/owner-tags/file-budgets — `--no-verify` is BANNED. Each plan's definition-of-done includes **deleting the `plan_M<N>`-tagged suppressions it resolves** (per-file-ignores, inline noqa/ty-ignores, vulture-allowlist entries, file-budget grandfather rows); `RUF100` + `check_suppression_owners.py` will fail if you forget or leave stale ones. New env vars a plan introduces become **Settings fields in `settings.py`** (TID251 bans raw `os.getenv`/`os.environ` elsewhere) — this adaptation is PRE-AUTHORIZED, not a deviation.
- **One PR per plan** (finding IDs linked), except where a plan's `resolved_decisions` say otherwise (M3 = two stacked PRs). The next plan starts from the prior plan's final commit — don't wait on PR merges.
- **Re-anchor by SYMBOL, never by stale line number** — every plan has a §1.3 predecessor-shift table; predecessors WILL have moved things.
- Before each plan: read its file END-TO-END including amendments + `[GATE RULING]` blocks (M4-Ph1's A1/A1.5, M5b's Q1–Q3 rulings, M14's A1 + X-hard block are all in-file), re-verify its anchors at the CURRENT tree, then execute.
- **Bookkeeping:** after each plan lands, update `audit/state.json` (`stages.3-fix` progress + a `history` event) and **validate it as JSON after every edit** (`.venv/Scripts/python.exe -c "import json; json.load(open('audit/state.json',encoding='utf-8'))"`). Present each finished plan to the human as a concise PR summary: diff shape, test/golden deltas, findings closed.
- Model routing: Sonnet executes plan steps; Opus for the gnarliest refactor steps (M4-Ph1 C1/C3, M5b engine merge); Fable reviews every diff against its plan before commit. Spawn executors with the FULL cold preamble (pinned base, plan file path, binding notes, lenses, out-of-scope list, venv-python-only) and production edits ONLY within the plan's file-touch list.
- **OneDrive quirk:** subagent file writes can look missing to a fresh read for minutes. Before re-tasking anyone, check mtime+size, re-read once, and ask the agent what it wrote.

## CROSS-CUTTING INVARIANTS (from cross_review_notes — do not violate)
- M3 and M8 both edit `_start_server_process`'s Popen kwargs (M3: stdout/stderr; M8: env=) — re-anchor on `subprocess.Popen(cmd, **kwargs)`.
- Post-M8-A1 the backend port is **not guaranteed 19222** — it flows by argument + `server.json`; never re-hardcode it (M2's reuse key reads server.json; `stop` is the reset path to DEFAULT_PORT).
- M4-Ph1 STEP 0: embedded modules must **NEVER import `server`** (runpy `run_name='__main__'` double-registration hazard); absolute imports work only because the package is installed editable; the one `sys.path` shim in top-level server.py is the only sanctioned bridge.
- M5b's golden diffs ARE the review surface: deliberate diffs are enumerated in the plan; styles/events/animations/assets goldens, both output_dir tripwires, and count-94 MUST stay green. `test_element_cloner_output_dir.py` pins `FileBasedElementCloner` despite its filename — do NOT "clean it up" when `element_cloner.py` is deleted.
- M14+A1 X-HARD: `STEALTH_MCP_SESSION_STORAGE_CAP_GB` → `STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB`, `--session-cap-gb` → `--browser-session-cap-gb`, NO alias/fallback; the README/RUNBOOK migration note lands in the SAME checkpoint; afterward a grep for the old env name may hit only frozen `audit/` evidence.
- Tool-count tripwire is **94** end-to-end (M2 sets it, M6 pins it, nothing after may change it — M5b/M14 rename NO tools; clone_*→extract_* renames are M4-Ph2, deferred). At the post-gates HEAD the count is still **96** (M2 hasn't run yet).
- **GATES-ERA invariants (2026-07-06, binding — full text = last 4 `cross_review_notes`):** (1) the gates commits are PREDECESSOR ZERO for every plan — the repo-wide ruff-format commit + env-read migration shifted lines everywhere, so §1.3 line tables are approximate; re-anchor by SYMBOL always. (2) `settings.py` is THE canonical env home — plan M11a's `env_utils.py` step is SUPERSEDED; never create env_utils.py (F-720/F-763 already closed at gates); all other M11a/M15 steps unchanged. (3) M14's X-HARD env rename now edits the **Settings field** (+ `.env.example`) — the old read site `embedded/server.py:588` no longer exists; the approved rename + migration-note + grep rule otherwise unchanged. (4) `observability.py` (Sentry shipping) and M3's `logging_setup.py` (log writing) are separate single homes — M3 must not create a second error-shipping path.

Begin: load the state files, run the GATES CHECK + baseline verification, create `audit/fixes-2026-07-02`, then execute plan 1 (plan_M3.md + A1 — read it end-to-end first). Work the order above one plan at a time; STOP-and-report on any deviation.
<<<END PROMPT>>>

---

## Quick-reference: campaign state at handoff (2026-07-04; gates preface added 2026-07-06)

- **Audit-pinned SHA** `2267b83d3efda03f93936db2c34ded33aaa0d701` (plans were verified there; it is an ANCESTOR after gates land — the working baseline is `state.json → stages['2.5-gates'].landed`). Branch `fix/singleton-version-aware-backend`. Pre-gates baseline was **402 passed**, cov ~40.9% vs gate 39.
- **2.5-gates interlude (2026-07-06 human directive)** runs BEFORE this prompt: ruff/ty/vulture/husky/CI/pydantic-settings/Sentry posture via `audit/GATES_TASK_OPUS.md` in a fresh Opus chat. This prompt refuses to start until `2.5-gates.landed` is filled.
- **Stage 2 CLOSED 2026-07-04**: 12/12 executable plans approved (M5a folded into M5b as binding analysis). Four amendments live and approved in-file: M3-A1 (RLock), M8-A1 (port fallback), M4Ph1-A1+A1.5 (full 21-site envelope sweep + G5 pair), M14-A1 (F-741 renames + X-hard env rename).
- **Findings ledger:** `audit/findings.json` = 99 (0 hallucinated quotes). Dispositions + routing recorded per finding; `state.json` `cross_review_notes` carry the binding cross-plan directives.
- **Stage 4** = executing plan_M14(+A1), the last plan in the order — no separate planning round.
- Pipeline reads from disk, not chat memory. Deviation from an approved plan → STOP and ask the human.
