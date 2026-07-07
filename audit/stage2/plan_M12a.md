# plan_M12a — Reconcile the hook priority docs↔code lie (first-match-wins) + delete the dead ResponseStageProcessor duplicate

**Status: APPROVED by human 2026-07-03 (approve as-is; F-163 truth = honest-first-match; shadow surfacing = runtime WARNING).**
Pinned SHA `2267b83d3efda03f93936db2c34ded33aaa0d701` (HEAD == pinned SHA, verified) · branch `fix/singleton-version-aware-backend` · 2026-07-03 · plan **10 of 13** in Stage-3 serial order · base tree = pinned SHA + approved plans **M3(+A1/M10a) · M1 · M8(+A1) · M2 · M7 · M11a_M15 · M9 · M6**.
Closes **F-163** (hook chain/doc lie), **F-721** (dead duplicate dispatch class), **F-742** (misleading same-prefix filename).

---

## 0. Verification results at HEAD (every anchor re-opened before writing)

| Claim | Verified at HEAD |
|---|---|
| `_process_request_hooks` runs only the top match | `dynamic_hook_system.py:301` `hook = matching_hooks[0]`; sorts at `:280` `matching_hooks.sort(key=lambda h: h.priority)`; logs count at `:290` `Found {len(matching_hooks)} matching hooks`; executes `_execute_hook_action` exactly once at `:331`. |
| Docstring promises a chain | `dynamic_hook_system.py:270` `"""Process hooks for a request/response in real-time with priority chain processing."""` |
| AI-facing doc promises ordering | `hook_learning_system.py:465` `"Use priority (lower = higher priority) to control hook execution order"` — inside `HookLearningSystem.get_requirements_documentation()` `best_practices` list (the surface behind the `get_hook_requirements_documentation` MCP tool). |
| **Full doc-surface sweep** (grep `priorit\|chain\|execution order\|matching hook\|each hook\|hooks run\|first match` across `embedded/*.py` + `server.py`) | **Only two surfaces lie about multi-hook ordering: `dynamic_hook_system.py:270` and `hook_learning_system.py:465`.** All other `priority` mentions are accurate ("lower = higher priority", i.e. which one wins) and are KEPT: `dynamic_hook_system.py:61`, `models.py:159`, `dynamic_hook_ai_interface.py:33`, `server.py:3917` (create_dynamic_hook docstring). The `create_dynamic_hook` tool description (`server.py:3906-3929`) and all `get_hook_*` delegators (`server.py:4015-4055`) contain **no** chaining/ordering language. |
| `ResponseStageProcessor` is unimported dead code | grep `ResponseStageProcessor\|response_stage_hooks\|response_stage_processor` repo-wide (`*.py`, excl. `audit/`) → **only** `response_stage_hooks.py:16,148` (definition + module instance) and `tests/test_response_stage_hooks.py:1,11,35` (its dedicated test). **Zero production imports.** |
| Base64 rot (the concrete cost of the duplicate) | Live: `dynamic_hook_system.py:453-455,461` `import base64` → `body_base64 = base64.b64encode(body_bytes).decode('ascii')` → `body=body_base64`. Dead: `response_stage_hooks.py:46` `body=action.body or ""` (raw, no base64) — malformed body if ever wired in. |
| M10a rebase point | `dynamic_hook_system.py:318-319` bare `except Exception:` / `response_headers = {}` — plan_M3 §1.1 row 10 / §3 step 7b inserts a **WARNING** log here. **Below** every Part-1 edit site (see §1.3). |
| Test count contributed by the deleted test | `pytest --collect-only tests/test_response_stage_hooks.py` → **11 tests** (`TestResponseAction`: 4 param + 1 error; `TestRequestAction`: 5 param + 1 error). `asyncio_mode = "auto"` (pyproject.toml:63) collects them. |
| Coverage gate | `--cov-fail-under=39` lives **only** in `.github/workflows/test.yml:41` (CI CLI flag); **not** in `pyproject.toml` (`[tool.coverage.report]` has no `fail_under`) — local runs never enforce it (`audit/stage0/metrics.md:7`). Current: TOTAL 5996 stmts / 3525 miss = **41.21%**; `response_stage_hooks.py` 68 stmts / 6 miss (91%); `dynamic_hook_system.py` 299 / 154 (48%), lines **271-341 = the whole `_process_request_hooks` body = uncovered**. |

---

## 1. Scope

### 1.1 Files touched (source + tests)

| File | Change | Finding |
|---|---|---|
| `src/stealth_chrome_devtools_mcp/embedded/dynamic_hook_system.py` | **Edit** `:270` docstring (drop "priority chain processing" → first-match-by-priority); **insert** a shadow WARNING right after `:301` `hook = matching_hooks[0]` | F-163 |
| `src/stealth_chrome_devtools_mcp/embedded/hook_learning_system.py` | **Edit** `:465` `best_practices` item (execution-order → first-match-by-priority) | F-163 |
| `src/stealth_chrome_devtools_mcp/embedded/response_stage_hooks.py` | **DELETE** (148 lines, dead `ResponseStageProcessor` + module instance) | F-721, F-742 |
| `tests/test_response_stage_hooks.py` | **DELETE** (11 tests; hand-rolled `FakeTab`, dies with it) | F-721 |
| `tests/test_dynamic_hook_system.py` | **Append** one test class (~4 tests) pinning first-match-wins + shadow surfacing + the deletion assertion; reuse the file's existing `_req()` helper (`:26`) and `tests/fakes.py` `FakeTab` (M6 canon) | F-163, F-721 |

No other files change. `server.py` is **read-only** here (its hook-tool descriptions are already truthful — verified §0).

### 1.2 Confirmed anchors (re-opened at pinned SHA `2267b83d`)

- `dynamic_hook_system.py`: `async def _process_request_hooks(self, tab, request: RequestInfo, event=None):` `:269`; docstring `:270`; `matching_hooks.sort(key=lambda h: h.priority)` `:280`; `Found {len(matching_hooks)} matching hooks` log `:290`; `hook = matching_hooks[0]` `:301`; `debug_logger.log_warning(...)` already used in-file at `:127-130` (signature `(component, method, message)`).
- `hook_learning_system.py`: `get_requirements_documentation()` `best_practices` list, item at `:465`.
- `response_stage_hooks.py`: whole file; `class ResponseStageProcessor:` `:16`; `response_stage_processor = ResponseStageProcessor(None)` `:148`.
- `tests/test_dynamic_hook_system.py`: `from dynamic_hook_system import (...)` `:15`; `def _req(...)` `:26`; `def _hook(...)` `:34`; classes `TestDataclasses/TestMatches/TestProcess/TestRegistry` — **none touch `_process_request_hooks`** (the exact gap this plan fills).

### 1.3 Anchors that predecessors SHIFT (re-anchor by SYMBOL in Stage 3)

- **`dynamic_hook_system.py` — ONLY `plan_M3` (M10a-7b) edits this file** (verified: grep `dynamic_hook_system` `audit/stage2/plan_*.md` → M10a row only; M9 explicitly rules it OUT; M1/M2/M6/M7/M8/M11a/M15 do not touch it). M10a inserts a **WARNING** log into the `except Exception:` at `:318` (response-header parse fallback), shifting lines `≥318` down ≈ **+2**. **Every Part-1 edit site is ABOVE :307** (`:270` docstring; the insert immediately after `:301` `hook = matching_hooks[0]`). **No collision.** Re-anchor by symbol, not line: `async def _process_request_hooks(`, its docstring, `hook = matching_hooks[0]`. **Do NOT touch the `:318` header-parse `except` — that is M10a's; leave its WARNING intact.**
- **`hook_learning_system.py` — NO predecessor touches it** (verified: absent from all `plan_*.md`). Anchor by symbol: `get_requirements_documentation` → `best_practices` → the `"Use priority ... execution order"` string.
- **`response_stage_hooks.py` / `tests/test_response_stage_hooks.py` — NO predecessor touches them.** Deleted wholesale; line drift irrelevant.
- **`tests/test_dynamic_hook_system.py` — NO predecessor edits it.** Append-only.
- **`tests/fakes.py` + `tests/conftest.py` — CREATED/EXTENDED by `plan_M6` (predecessor, plan 8).** At M12a execution time they exist: `FakeTab` records `.send(cdp_obj)` and returns canned responses from a configurable map; conftest exposes the `fake_tab` fixture. **My Part-1 tests depend on M6 having landed** (it has, by serial order). Reuse `FakeTab`/`fake_tab` — hand-rolling a second tab fake is a dedup-lens defect.

### 1.4 Explicit out-of-scope

- **F-606** (`matches()` per-request `eval()` re-parse, `dynamic_hook_system.py:119-131`) — same file, **different concern (perf)**, unrouted, stays **OPEN**. Not folded in (state.json: "F-606 … M12a's file" but a distinct finding; plan_M9 §8 also parks it).
- **M12b** exec sandboxing / `create_python_binding` trust boundary (F-107/F-161/F-160/F-166, the other half of REPORT §M12) — **REJECTED at triage**; not routed to M12a. No sandbox work, no trust-boundary doc here.
- **No hook re-architecture / no chain implementation / no new hook features** beyond the doc reconcile + the read-only shadow WARNING.
- **No rename of `response_handler.py`** — deleting its confusing same-prefix sibling resolves F-742 with zero rename (fix_direction's cheaper branch). `response_handler.py` keeps M15's `STATE_DIR` edit; untouched here.
- **No `server.py` edits** — hook-tool descriptions are already truthful.

---

## 2. Approach + rejected alternatives

### The F-163 decision: **(b) honest-first-match** — reconcile every doc surface to first-match-by-priority, and surface shadowing as a runtime WARNING.

**Chosen truth:** the code keeps first-match-wins (highest priority = lowest number runs; lower-priority matches are shadowed and never fire). Both lying doc surfaces are rewritten to say exactly that, and `_process_request_hooks` gains one WARNING (only when `len(matching_hooks) > 1`) naming the winner and the shadowed hooks — closing the "trigger_count stuck at 0 with no indication why" debugging trap the finding centres on.

**Why (b) and not (a) chain-by-priority — under the maintainability lenses, 0 users:**

1. **The CDP platform already dictates first-match-wins.** `Fetch.RequestPaused` requires **exactly one** terminal disposition per paused request (`continue_request` / `continue_response` / `fulfill_request` / `fail_request`). Once any hook fulfills or fails, the request is resolved — there is nothing left for a second hook to act on. Every `HookAction` in this system is terminal (`block`→fail, `fulfill`→fulfill, `redirect`/`modify`→continue-with-mutation, `continue`→continue). First-match-wins is not a bug to fix; it is the semantics the domain enforces. Making the docs honest is the fix.
2. **Conventions lens — (a) introduces a second execution model.** A real chain needs a new split of `HookAction` into non-terminal "transform" actions (compose) vs terminal "stop" actions, accumulated-mutation threading, and cross-hook precedence rules ("hook 1 returns modify, hook 2 returns block" → which wins? both fulfill → ?). That is a brand-new mechanism layered on the existing one — the exact "a fix that introduces a second way of doing something is a defect" the addendum forbids. (b) introduces zero new mechanisms.
3. **Modularity lens — (a) adds cross-hook coupling.** Chaining makes each hook's output depend on the others' outputs and order; (b) keeps each hook independent and the module understandable in isolation.
4. **0 users / effort / risk.** No one depends on a chain that has never worked, so there is no migration cost to admitting first-match-wins. (a) is speculative feature work on the hot interception path with new failure modes (partial mutations, double-terminal-dispatch); the finding itself flags true chaining as "a larger design decision … scoped separately if actually wanted." For a maintainability audit, (b) is the correct-and-cheap truth.

### Rejected alternatives

1. **(a) Implement a real priority chain.** Rejected — see 1–4 above: it is a semantics redesign (new action taxonomy + compose rules) against a platform that permits one terminal disposition per request, on a 0-user local tool, explicitly out of the brief's scope ("no new hook features").
2. **Surface shadowing as a static `is_shadowed_by` field in `list_hooks`/`get_hook_details`** (the finding's floated example). Rejected — deciding "does hook A's pattern shadow hook B's?" statically requires **fnmatch-glob overlap analysis**, which is undecidable in general; a tractable heuristic (flag only identical `url_pattern` strings) gives **false negatives** for real overlaps like `*.example.com` shadowing `api.example.com`, i.e. it reports "not shadowed" when the hook silently never fires — a field that lies is worse than no field (clarity lens) and is a second, half-working mechanism (conventions lens). The chosen **runtime WARNING is precise** because it fires on the actual per-request match result (no overlap guessing), reuses the existing `debug_logger` convention, and covers **all** overlap shapes. *(This is the one genuine sub-decision — see §9. Recommended default: runtime WARNING. Fallback if the human wants absolute minimalism: docs-only, no WARNING.)*
3. **Docs-only, no shadow surfacing.** Rejected as the default — it fixes the "docs lie" half of F-163 but leaves the silent debugging trap (`why_it_matters`: "trigger_count … stays at 0 forever with no indication why") wide open. The WARNING is ~6 lines, cannot change request disposition, and only fires when 2+ hooks actually collide. Kept as the fallback if the human vetoes the WARNING.

### F-721 / F-742 approach

Delete `response_stage_hooks.py` and its test outright. `ResponseStageProcessor` re-implements the entire block/fulfill/redirect/modify CDP dispatch that the live `DynamicHookSystem._execute_hook_action` (`:435-519`) already owns for both stages, is imported nowhere in production (grep-proven), and has already rotted (raw vs base64 fulfill body). Deleting it leaves **one** canonical `HookAction → CDP Fetch` dispatch (dedup lens) and removes the `response_handler.py` vs `response_stage_hooks.py` same-prefix ambiguity with **no rename** (clarity lens, F-742). If response-stage-specific dispatch is ever genuinely wanted, it belongs as a branch on the one live method — not a parallel class.

---

## 3. Sequencing (independently verifiable; one checkpoint commit per step)

> Discipline: each step leaves the suite green; run `.venv\Scripts\python.exe -m pytest -m "not integration" -q` after every step (venv python directly — `uv run` is BROKEN on this `&`+space path). Deviation from a confirmed symbol → **STOP and report**.

**M12a-1 — Part-1 behaviour tests first (TDD).** Append to `tests/test_dynamic_hook_system.py` a `TestProcessRequestHooks` class (async; `asyncio_mode=auto`, no marker) using `tests/fakes.py` `FakeTab` and the existing `_req()` helper:
- `test_first_match_by_priority_wins_only_one_runs` — a fresh `DynamicHookSystem`; `create_hook(name="hi", requirements={"url_pattern":"*example.com*"}, function_code=CONTINUE_SRC, instance_ids=["i1"], priority=10)` and again `priority=20`; `await _process_request_hooks(FakeTab(), _req(stage="request"))`; assert the priority-10 hook `.trigger_count == 1` and the priority-20 hook `.trigger_count == 0`. **(Pins first-match-wins — already green; characterizes the truth we align docs to.)**
- `test_shadowed_match_emits_warning` — same 2-hook setup; `monkeypatch.setattr(dhs.debug_logger, "log_warning", <recorder>)`; assert the recorder fired once and the shadowed hook's `name` is in the message. **(RED until M12a-2 — drives the WARNING.)**
- `test_single_match_emits_no_shadow_warning` — one matching hook; assert no shadow WARNING and `.trigger_count == 1`. **(Pins no false-positive.)**
- *Verify:* run the file → the shadow test is RED, the rest GREEN.

**M12a-2 — Part-1 code (reconcile + surface).** Then:
- `dynamic_hook_system.py:270` → `"""Process hooks for a request/response in real-time. When several hooks match, the highest-priority match (lowest priority number) runs and lower-priority matches are shadowed — they do NOT run (first-match-by-priority, not a chain)."""`
- Insert immediately after `dynamic_hook_system.py:301` `hook = matching_hooks[0]`:
  ```python
              if len(matching_hooks) > 1:
                  debug_logger.log_warning(
                      "dynamic_hook_system", "_process_request_hooks",
                      f"{len(matching_hooks)} hooks matched {request.stage} {request.url}; "
                      f"only highest-priority '{hook.name}' (priority={hook.priority}) runs — "
                      f"shadowed (never fire): {[h.name for h in matching_hooks[1:]]}",
                  )
  ```
  (Placed after the assignment so `hook` is defined; above `:307`, so M10a's `:318` WARNING is untouched.)
- `hook_learning_system.py:465` → `"Set priority (lower = higher priority) to pick which single hook wins when several match the same request; the highest-priority match runs and lower-priority matches are shadowed (first-match-by-priority, not a chain)"`.
- *Verify:* full non-integration suite GREEN (shadow test now passes). **Commit:** `M12a-1: reconcile hook priority docs↔code to first-match-wins + surface shadowing (F-163)`.

**M12a-3 — Part-2 deletion (F-721/F-742).**
- `git rm src/stealth_chrome_devtools_mcp/embedded/response_stage_hooks.py tests/test_response_stage_hooks.py`.
- Append to `tests/test_dynamic_hook_system.py`: `test_response_stage_hooks_module_is_deleted` — `with pytest.raises(ModuleNotFoundError): import response_stage_hooks`, and assert `from dynamic_hook_system import HookAction, RequestInfo` still succeeds (the canonical home survives).
- *Verify:* full non-integration suite GREEN; test count = pre-M12a baseline − 11 (deleted) + 4 (new). **Commit:** `M12a-2: delete dead ResponseStageProcessor + its test (F-721, F-742)`.

**M12a-4 — Coverage gate check (verification only, no code).**
- Run the CI command locally: `.venv\Scripts\python.exe -m pytest -m "not integration" --cov=src/stealth_chrome_devtools_mcp --cov-report=term-missing --cov-fail-under=39 -q`.
- Confirm exit 0 (≥39%). **Do NOT ratchet the gate** (separate hygiene concern; consistent with M2/M6). If it somehow dipped, the remedy is MORE `_process_request_hooks` tests (never lowering the gate) — but the math (§5) says it rises.

---

## 4. Breaking changes (0 users → free; enumerated for the single local operator)

- **New WARNING log line** when ≥2 hooks match the same request (previously silent). Purely additive diagnostic; **does not change which hook runs** (still first-by-priority) or the request disposition.
- **Docstring + `best_practices` text change** — doc-only; the `get_hook_requirements_documentation` MCP tool now returns the corrected best-practice string (same list shape, `test_hook_learning_system.py:29` still green).
- **`response_stage_hooks.py` removed** — it was imported by nothing in production, so **zero runtime behavior change**. The MCP tool surface is unchanged (no tool added/removed/renamed).
- No config knob, no schema change, no signature change. Universal default (no per-user branching), consistent with the repo's "ship every-user defaults" stance.

---

## 5. Test strategy

- **Behaviour-pinning BEFORE the change** (M12a-1, TDD): first-match-wins (green, characterizes the kept truth), shadow-WARNING (red→green, drives the code), single-match-no-warning (no false-positive). All hermetic, in-process, reusing `tests/fakes.py` `FakeTab` (M6 canon) + the existing `_req()` helper — no second fake, no real browser.
- **Deletion assertion** (M12a-3): `import response_stage_hooks` must raise `ModuleNotFoundError`; `HookAction`/`RequestInfo` still import from `dynamic_hook_system` (canonical home intact).
- **The 11 tests in `test_response_stage_hooks.py` die with the file** — they only exercised the dead class; nothing else imports it (grep-proven). Its hand-rolled `FakeTab` is not migrated (dedup: the surviving path uses M6's canonical fake).
- **Test-count delta (M12a's own contribution):** **−11** (deleted file) **+4** (new) = **−7**. Measured directly on HEAD that is `402 − 7 = 395`; the true new baseline = `(pre-M12a count after predecessors) − 7`. The executor records the actual number.
- **Coverage sanity-check (gate `fail_under=39`, CI-only):**
  - *Worst case* — delete file+tests, add zero new tests: `(2471−62)/(5996−68) = 2409/5928 = 40.64%` ≥ 39 (**−0.57 pt**, still 1.64 pt of headroom).
  - *Actual* — Part-1 tests cover the currently-uncovered `_process_request_hooks` body (lines 271-341, all missed today), adding to the numerator without adding to the denominator → coverage returns to ~41%+. M6 (predecessor) further raises the pre-M12a baseline. **Gate safe with margin.**

---

## 6. Rollback + checkpoints

Three commits (M12a-1 code, M12a-2 deletion; M12a-1-tests fold into the first code commit or its own — executor's choice, kept atomic per step).
- **Revert M12a-2** (`git revert`) restores `response_stage_hooks.py` + its test verbatim and drops the deletion assertion — the tree returns to post-M12a-1.
- **Revert M12a-1** restores the old docstring/`best_practices` (re-introduces the lie) and removes the WARNING + Part-1 tests — revert both to fully undo.
- Each step is independently green, so a partial landing (e.g. Part-1 only, Part-2 deferred) is a valid stopping point.

---

## 7. Risk (blast radius · worst case · early warning)

- **Blast radius: minimal.** Part 1 = 3 doc/log lines across 2 files + 3 tests; the WARNING is on the hot interception path but is a pure log call (no control-flow change) that fires only when ≥2 hooks collide. Part 2 = deleting unreferenced dead code + its isolated test.
- **Worst cases & mitigations:** (a) WARNING references `hook.name` before assignment → mitigated by inserting **after** `hook = matching_hooks[0]`. (b) Deletion breaks an import → disproven by the repo-wide grep (only the deleted test imports it). (c) Coverage dips <39 → disproven by the 40.64% worst-case math + the newly-covered `_process_request_hooks` path. (d) A doc-text test breaks → disproven (`test_hook_learning_system.py:29` asserts only list-non-emptiness; no test pins the changed strings).
- **Early-warning signs:** full non-integration suite red after M12a-1 (doc/log) or M12a-2 (deletion); `--cov-fail-under=39` non-zero exit at M12a-4; any anchor that does not match its confirmed symbol (→ STOP and report — likely a predecessor rebase surprise).

---

## 8. Findings closed

- **F-163** (hook chain/doc lie) — **CLOSED.** Code keeps first-match-by-priority (the disposition CDP enforces); both lying surfaces (`dynamic_hook_system.py:270`, `hook_learning_system.py:465`) reworded to match; the silent-shadowing debugging trap closed by a runtime WARNING naming winner + shadowed hooks. The full doc-surface sweep (§0) confirms no other surface disagrees.
- **F-721** (dead duplicate CDP dispatch, latent base64 bug) — **CLOSED.** `response_stage_hooks.py` deleted; the one live `_execute_hook_action` remains the sole `HookAction → CDP Fetch` dispatch.
- **F-742** (misleading same-prefix `response_handler.py` vs `response_stage_hooks.py`) — **CLOSED by the same deletion**; `response_handler.py` is left unambiguous, no rename.

---

## Appendix — where the four lenses shaped a choice

- **Deduplication:** delete the second `HookAction → CDP Fetch` dispatch so it lives in exactly one place (`_execute_hook_action`); Part-1 tests reuse `tests/fakes.py` (M6) + the existing `_req()` helper, no second fake.
- **Clarity:** the docstring/`best_practices` stop lying; deleting the confusing sibling makes `response_handler.py`'s "response" unambiguous with no rename.
- **Conventions:** first-match-wins is the single execution model the CDP domain permits — chaining would add a second one (a defect); the shadow signal reuses the existing `debug_logger.log_warning` convention rather than inventing a static introspection field.
- **Modularity:** rejecting the chain keeps hooks independent (no cross-hook output coupling); the module stays understandable in isolation.
