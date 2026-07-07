# 2.5-GATES — QUALITY-GATES INTERLUDE · OPUS TASK PROMPT (written 2026-07-06)

Human directive 2026-07-06: land code-quality gates BEFORE Stage 3 FIX. Copy everything between the
`<<<BEGIN PROMPT>>>` / `<<<END PROMPT>>>` markers into a **fresh Opus chat** at the repo root.

---

<<<BEGIN PROMPT>>>
You are Opus, sole implementer of the **2.5-gates quality-gates workstream** for `stealth-chrome-devtools-mcp`.
Context: a foundational audit (99 quote-verified findings) is complete and 12 fix plans are human-approved but
**NOT executed** — you run BETWEEN planning and execution. Your mission: install ruff + ty (strict) + vulture +
husky + CI enforcement + a pydantic-settings `.env` schema + a Sentry-ready loud-error posture, and **fix the
code that violates these gates properly — no hacky tricks to get green** (no blanket ignores, no `--no-verify`,
no lowering strictness silently). Violations that an approved Stage-3 plan already owns get an **owner-tagged
suppression** instead of a premature fix (mechanism below).

## 0. Ground truth (verify before any change)
- HEAD must be `2267b83d3efda03f93936db2c34ded33aaa0d701` on branch `fix/singleton-version-aware-backend`;
  tree clean apart from untracked `audit/` + `CODEBASE_AUDIT.md`. If not → STOP and report.
- Baseline: `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed** (cov ~40.9%, CI gate 39).
- **`uv run` is BROKEN in this checkout** (path contains `&` + spaces) — ALWAYS use `.venv\Scripts\python.exe`
  locally. `uv` works fine in CI (clean workspace path). If you must regenerate `uv.lock` locally and `uv lock`
  hits the path bug, map the repo to a clean path first (`subst X: .`) and run it there; record what worked.
- Windows 11; git hooks run under Git's `sh` — quote every path (spaces AND `&`).
- OneDrive checkout: file writes can look missing to a fresh read for minutes — check mtime+size, re-read once.
- Tool surface at HEAD = **96 MCP tools** (94 only after Stage-3 plan M2 deletes 2). You add/remove/rename ZERO tools.
- Existing CI: `.github/workflows/test.yml` (unit matrix 3.11/3.12/3.13 + integration job) — **EXTEND it; do not
  create a second workflow** (one way per thing). `publish.yml` untouched.
- `pyproject.toml`: deps are `==`-pinned (`pydantic==2.11.7`, `python-dotenv==1.1.1` already present);
  `requires-python = ">=3.11"`; NO `[tool.ruff]`/`[tool.ty]`/`[tool.vulture]` exists yet. No package.json/husky.
- Read first: `audit/RESUME.md`, `audit/state.json` → `stages['2.5-gates']` + the 4 GATES-tagged
  `cross_review_notes` at the end of `stages['2-plan'].cross_review_notes`, and `audit/ADDENDUM_LENSES.md`
  (4 binding lenses — **a fix introducing a SECOND way of doing something is a defect**).
- `audit/stage2/plan_*.md` are **READ-ONLY** (approved). Skim plan_M3, plan_M11a_M15, plan_M4ph1 §C3, plan_M14 §A1.3
  so you know what NOT to pre-empt.
- Consult **current docs via context7/web** for ruff, ty, vulture, pydantic-settings, husky before configuring —
  rule names and config schemas move fast; pin the exact versions you verify.

## 1. The owner-tag suppression system (the bridge between gates and the approved plans)
Every suppression anywhere — `# noqa: <code>`, `# ty: ignore[<rule>]`, `[tool.ruff.lint.per-file-ignores]` entry,
vulture-allowlist entry, file-budget grandfather entry — MUST carry an owner tag in a trailing comment:
- `plan_M<id>` — an approved Stage-3 plan fixes this; that plan deletes the tag when it lands.
- `PERMANENT(<reason>)` — a conscious forever-exception (e.g., print-is-the-CLI's-UI).
- `FALSE-POSITIVE(<reason>)` — tool limitation (e.g., vulture can't see dynamic dispatch).
- `DEBT(F-<id>)` — known-debt item already in the audit ledger (M14 records it).
Write `tools/check_suppression_owners.py` to enforce the grammar repo-wide (scan noqa/ty-ignore comments,
per-file-ignores, the vulture allowlist, the file-budget grandfather list; untagged suppression → exit 1), with
its own tests. `PGH003`/`PGH004` (specific-code-only ignores) + `RUF100` (stale noqa fails lint) make the
inventory shrink automatically as Stage 3 lands. File-wide per-file-ignores only when a file has >3 sites for
that code; otherwise inline noqa at the site.

## 2. Ruff — curated ruleset (the audit's scars, made unwritable)
Config in `pyproject.toml`. `target-version = "py311"`. Adopt **`ruff format`** repo-wide as ONE isolated commit
(one style convention, zero debates; Stage-3 plans re-anchor by symbol so the line-shift is absorbed).
Run `ruff check --statistics` FIRST to size the work, then fix in themed commits.

`lint.select` (rationale — every group maps to a verified finding):
- `F,E,W,I,N,UP,C4,PIE,SIM,RET,RSE,A,PTH,PERF,FURB,RUF` — core correctness/modernization; `RUF006` (dangling
  asyncio task = exceptions vanish before Sentry), `RUF013`, `RUF100`.
- **Loud-error / Sentry-capturability chain** (an error reaches Sentry iff: not swallowed → logged with
  traceback or raised → cause-chain preserved → not lost in a dangling task):
  `E722` + `BLE001` (blind/bare except — the 57-except scar, ~21 truly silent, F-181),
  `S110`/`S112` (try-except-pass/continue),
  `B` incl. **`B904`** (`raise … from err` — preserves `__cause__` so Sentry shows the real cause),
  `TRY` **minus TRY003** (incl. `TRY002` no vanilla `raise Exception` — ruins Sentry grouping; **`TRY400`**
  `logging.exception` not `.error` in handlers — attaches the traceback),
  `LOG` + `G` (incl. `G201`: `.error(exc_info=True)` → `.exception`).
- `ASYNC` — blocking calls on the loop (the F-180 close_instance freeze family: sleep/subprocess/file-IO in async).
- `S` (bandit) — beyond S110/S112: hardcoded secrets, unsafe primitives.
- `T10`, `T20` — no debugger, no `print` in library code (CLI exempt: print IS its UI).
- `ERA` — commented-out code is dead code (dedup lens).
- `DTZ` — naive datetimes (timestamps feed logs/fingerprints).
- `ANN` — annotations everywhere ty needs them; **`ANN401` = no `typing.Any` in signatures** (the human's
  no-Any mandate; ty is the source of truth, this is the belt).
- `ARG`, `TC`, `INP` — unused args, type-checking imports, implicit namespace packages (`embedded/` has no
  `__init__.py` → INP001 fires → owner-tag `plan_M4ph1`, its STEP 0 packageizes).
- `PGH` — **the anti-hack law**: no bare `# noqa`, no un-coded `# type: ignore`.
- `PL` + `C90` — complexity budgets (`mccabe.max-complexity = 12`, pylint defaults). server.py's monsters get
  owner-tag `plan_M4ph1`.
- `TID` — see banned APIs below; also `flake8-tidy-imports.ban-relative-imports = "all"` (matches the approved
  M4-Ph1 STEP-0 absolute-imports design; nothing at HEAD violates it).

**`[tool.ruff.lint.flake8-tidy-imports.banned-api]`** — the custom, repo-specific bans (each `msg` must cite
the finding and the sanctioned alternative):
- `subprocess.DEVNULL` — F-303/F-503: DEVNULL spawn hid every backend crash. Allowed (owner-tagged `plan_M3`)
  only at the current spawn site until M3's logging spawn helper lands.
- `os.getenv` and `os.environ` — env access has ONE home: `settings.py` (F-720/F-763: divergent truthy-set
  parsing). per-file-ignore `TID251` for `settings.py` itself, tag PERMANENT(canonical env home).
- `typing.Any` — no Any, period. Use precise types / `object` / protocols.
- `asyncio.get_event_loop` — deprecated; use `get_running_loop()`.

**Deliberately NOT enabled** (record this list verbatim in pyproject comments — silence must be a decision):
`TRY003` + `EM101/102/103` — they fight the APPROVED M4-A1 error-envelope idiom `raise ToolError("<message>")`
(~55 call sites will land exactly that shape; linting against it would be a rule that lies).
`D` (docstrings) — M14 owns the doc surface; revisit after it lands. `FBT` — semantic-default bugs (F-745)
aren't lintable; churn without an owner. `COM`/`Q`/`ISC` — the formatter owns those. `FIX`/`TD` — debt lives
in the audit ledger (M14), not in TODO-comment policing.

Expected per-file-ignores you'll need (verify by running, tag every one):
`cli.py: T20` PERMANENT(print is the CLI UI) · `tests/**: S101, ANN, PLR2004, ARG` PERMANENT(pytest idioms) ·
`embedded/server.py: BLE001,S110,S112,TRY4xx,T20,C901,PLR09xx,ANN...` plan_M3/plan_M10a/plan_M4ph1 (enumerate
codes individually — no wildcard) · `embedded/dynamic_hook_system.py: S307` PERMANENT(Python-eval hook matching
is by design; sandboxing = M12b, REJECTED at triage; local single-user tool) · cloner modules plan_M5b ·
subprocess S603/S607 inline-noqa at the real spawn sites PERMANENT(spawning Chrome/backend is the product).

## 3. ty — strict, no Any / no Unknown
- Add `[tool.ty]` config (consult current ty docs FIRST — the rule set is young and moves). Target: **every
  diagnostic at error severity**, strictest available posture on implicit `Any`/unknown types, Python 3.11 floor.
  Add a `py.typed` marker to the package.
- Suppression policy: inline `# ty: ignore[<rule>]` with owner tag ONLY; no file-level/module-level unchecked
  escapes unless enumerated in pyproject with an owner tag. New modules you create (`settings.py`,
  `observability.py`, `tools/*.py`) = **zero suppressions**.
- Reality check, stated honestly: `embedded/server.py` (4,207 LOC) and the 5 cloners predate typing and are
  restructured by plans M4-Ph1/M5b. Annotate what is mechanical; owner-tag (`plan_M4ph1`/`plan_M5b`) what would
  require restructuring to type honestly. **Report the exact suppression count per file** — a big honest number
  beats a small fake one.
- If ty itself proves unusable on this codebase (pre-1.0 blocker bugs), STOP and report options — do NOT
  silently swap in another checker.

## 4. Vulture — dead code
- Config in `pyproject.toml`: `paths = ["src"]`, `min_confidence = 80` (tune, report the number), and
  `ignore_decorators` for the dynamic registration surface (verify actual decorator names in source — at least
  `@section_tool`, `@mcp.tool`, `@mcp.resource`; 96 tools are dispatched dynamically and MUST NOT read as dead).
- Triage EVERY hit into exactly one bucket:
  (a) **plan-owned dead code → allowlist with plan tag, do NOT delete** — known: `ResponseStageProcessor` in
  `embedded/response_stage_hooks.py` (plan_M12a deletes it WITH its test choreography), `hot_reload`/`reload_status`
  (plan_M2), dead cloner branches like the nested-schema fallback (plan_M5b).
  (b) **unowned dead code → DELETE NOW properly**: grep for dynamic references first, delete, suite green,
  list every deletion in your report.
  (c) **false positive → allowlist with FALSE-POSITIVE(reason)**.
- Allowlist file: `tools/vulture_allowlist.py`, every entry owner-tagged.

## 5. `.env` schema — pydantic-settings (the Python equivalent of zod)
The human asked for "zod schema validation for .env". zod is TypeScript; in this Python codebase the exact
equivalent — typed schema, coercion, strict unknown-key rejection, loud failures — is **pydantic-settings**
(pydantic 2.11.7 is already a pinned dep). State this translation in your report; do not silently substitute.
- New module `src/stealth_chrome_devtools_mcp/settings.py`: ONE `Settings(BaseSettings)` =
  **the canonical env home** for the whole repo, with
  `SettingsConfigDict(env_prefix="STEALTH_MCP_", env_file=".env", extra="forbid")` — `extra="forbid"` +
  the prefix means a typo'd `STEALTH_MCP_*` key fails LOUDLY at startup (zod `.strict()` semantics) while
  unrelated OS env vars stay ignored. Cached accessor `get_settings()`.
- Inventory EVERY env read first: grep `os.getenv`, `os.environ`, `parse_.*_env` across `src/`. Known family
  members include `STEALTH_MCP_BROWSER_SESSION_ROOT`, `STEALTH_MCP_SESSION_STORAGE_CAP_GB` (float, default 20.0,
  read at `embedded/server.py:588`), `STEALTH_MCP_NO_AUTO_RECOVERY`, debug/log flags — the grep is authoritative,
  not this list. One typed field per var, current names and defaults **preserved exactly**.
  **Do NOT rename `STEALTH_MCP_SESSION_STORAGE_CAP_GB`** — plan M14+A1 (X-HARD ruling) renames it later, inside
  your Settings model; you moved the edit site, the rename is still M14's.
- Swap every read site to `get_settings().<field>` **mechanically** — no restructuring, no timing changes
  (import-time reads stay import-time; plan M11a owns fixing import-time side effects). Delete the now-orphaned
  ad-hoc parse helpers (`parse_float_env` etc.) once nothing references them. This consciously closes findings
  F-720/F-763 early (one parse home; one truthy convention) — human-directed; plan M11a's `env_utils.py` step is
  SUPERSEDED (see the binding cross_review_note): **never create env_utils.py; a second env home is a defect**.
- Note: `settings.py` sits at package root; embedded modules import it absolutely
  (`from stealth_chrome_devtools_mcp.settings import get_settings`) — safe because the package is installed
  editable (verified F-120) and settings imports nothing back (embedded modules must NEVER import `server` —
  runpy double-registration hazard).
- Fail-fast: instantiate at process entrypoints (backend `__main__`, `cli.main`) inside try → `logging.exception`
  → re-raise. Invalid `.env` kills the process with the precise field name.
- Ship `.env.example` documenting every field + a test asserting the example stays complete (every Settings
  field appears in it). CLI flag precedence unchanged: explicit flags still override env (Settings supplies defaults).
- Tests: defaults instantiate; a bad value (e.g., cap `"abc"`) raises naming the field; an unknown
  `STEALTH_MCP_*` key in `.env` raises.

## 6. Sentry-ready loudness
- Optional dependency group `sentry = ["sentry-sdk==<verified>"]`. New `observability.py` with `sentry_init()`:
  no-op unless `settings.sentry_dsn` is set (field `SENTRY_DSN` via alias, default `None` — OFF by default;
  local tool, universal default), else init with `LoggingIntegration(event_level=ERROR)`, the asyncio
  integration, `release` from the package version. Called from backend `__main__` and `cli.main`.
- Do NOT build file logging or an excepthook framework — plan M3 owns the logging spine; `observability.py` is
  error-SHIPPING only, `logging_setup.py` (M3) will be log-WRITING. One home each; record that boundary in a
  module docstring so M3's executor sees it.
- The ruff ruleset above IS the Sentry guarantee (nothing swallowed, tracebacks attached, cause chains kept);
  the remaining swallow-stock inside `embedded/server.py` is owner-tagged plan_M3/M10a — the plans fix the
  stock, your gates make regressions impossible.
- Tests: no DSN → no init; DSN set → init called with expected kwargs (mock the SDK).

## 7. File-size budget (the god-file tripwire ruff doesn't have)
`tools/check_file_budgets.py`: fail if any `src/**/*.py` exceeds **1000 LOC**, with an explicit grandfather
list (measure at run; expect at least `embedded/server.py` → `plan_M4ph1`, cloners → `plan_M5b`,
`browser_manager.py` → `DEBT(F-702)` if over). Grandfathered files may never GROW: record current LOC in the
list and fail if exceeded. Tests included.

## 8. Enforcement wiring — husky + CI
- **husky** (the human's explicit choice; requires Node, which this machine has): minimal private
  `package.json` (`"prepare": "husky"`, devDependency `husky@^9`), `.husky/pre-commit` (POSIX sh) resolving the
  venv python cross-platform (`./.venv/bin/python` else `./.venv/Scripts/python.exe`, quoted) and running:
  ruff format --check → ruff check → ty check → vulture → `tools/check_suppression_owners.py` →
  `tools/check_file_budgets.py`. No pytest in pre-commit (too slow). `.husky/pre-push` runs
  `pytest -m "not integration" -q`; if it exceeds ~2 min on this machine, surface that with a timing and a
  recommendation instead of silently keeping it.
- **Activate husky in the FINAL commit** (after everything is green) so your own intermediate commits aren't
  blocked by half-installed gates. From then on `--no-verify` is banned for everyone including Stage 3.
- **CI**: add a `quality` job to `.github/workflows/test.yml` (uv-based like its siblings; new `dev` extra with
  exact `==` pins for ruff/ty/vulture) running the same six checks. Keep the unit matrix; re-measure coverage
  after your new tests land and **ratchet `--cov-fail-under` honestly** (floor(actual)−1, never down).
- README gets a short "Development setup" subsection (npm install once to arm hooks; the six gates; venv-python
  caveat). Keep it minimal — plan M14 owns the doc overhaul.

## 9. Fix-now vs plan-owned (the boundary that keeps you out of Stage 3's lane)
Fix NOW, properly: everything the gates flag that **no approved plan owns** — unused imports/vars, naive
datetimes, pathlib, import order, unowned dead code, missing annotations that are mechanical, prints in
library code nobody owns, blind excepts in files no plan touches. Behavior-preserving; if a gate exposes a REAL
bug, fix it WITH a test and list it separately in your report.
Owner-tag, do NOT fix: silent-excepts/DEVNULL/prints in the M3/M10a surface · hot_reload+reload_status (M2) ·
blocking-async in the close/teardown path (M7) · import-time side effects & the ProcessCleanup init (M11a) ·
`ResponseStageProcessor` (M12a) · embedded packageization, INP001, server.py complexity, tool-body error
envelopes (M4-Ph1) · cloner duplication/dead branches (M5b) · any rename (M14/M4-Ph2).

## 10. Hard rules
- Approved plans + all `audit/stage2/*` + frozen stage-1 evidence: READ-ONLY.
- Branch `quality/gates-2026-07-06` off `2267b83d`; conventional commits, themed; ONE PR into
  `fix/singleton-version-aware-backend`. The repo-wide format commit stands alone.
- Suite green (`.venv\Scripts\python.exe -m pytest -m "not integration" -q`) at every commit boundary; report
  the final count (baseline 402 + your new tests).
- Tool count: `96` at HEAD, unchanged by you.
- No hacks: PGH + check_suppression_owners are law; every suppression tagged; nothing blanket; strictness is
  never lowered without a written reason in the report.
- Ambiguity or a forced deviation (e.g., ty can't run, a plan-owned file blocks a gate entirely) → STOP and ask
  the human. Do not improvise policy.

## 11. End-of-session bookkeeping (required)
1. `audit/state.json`: fill `stages['2.5-gates'].landed` with
   `{date, head_sha, branch, test_count, coverage, cov_gate, suppression_counts: {ruff, ty, vulture, budget},
   deleted_dead_code: [...], real_bugs_fixed: [...], notes}` and set the stage status to DONE; append a
   `history` event. **Validate after every edit**:
   `.venv/Scripts/python.exe -c "import json; json.load(open('audit/state.json',encoding='utf-8'))"`.
2. Refresh `audit/RESUME.md` pinned facts (new HEAD, test count, coverage, "gates ACTIVE" line). Do NOT edit
   `audit/STAGE3_RESUME_PROMPT.md` — it already reads its baseline from `stages['2.5-gates'].landed`.
3. Final report to the human: what landed (per deliverable) · exact suppression counts with owner-tag breakdown ·
   dead code deleted · real bugs found+fixed · what is honestly NOT done (e.g., ty carve-out size, pre-push
   timing, the 34 known dep vulns which remain OUT of scope) · new baseline facts · confirmation that Stage 3
   resumes via `audit/STAGE3_RESUME_PROMPT.md`.

Begin: verify §0 ground truth, read the state files, size the violations (`ruff check --statistics` with a
scratch config), then propose your commit sequence and execute.
<<<END PROMPT>>>
