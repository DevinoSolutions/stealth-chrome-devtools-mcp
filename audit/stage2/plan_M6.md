# Stage 2 Plan — M6 Characterization tests (dispatch + cloners + bug-prone tools)

- **Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend`
- **Date:** 2026-07-03
- **Batch:** **{M6}** — single item; executes **9th**, serially (`fix_order[8]`, after M9, before M5a).
- **Base tree:** post-`plan_M3`(+A1) + `plan_M1` + `plan_M8`(+A1) + `plan_M2` + `plan_M7` + `plan_M11a`+`plan_M15` + `plan_M9`. Runs from M9's final commit on branch `audit/fixes-2026-07-02`.
- **Status:** **APPROVED** (human, 2026-07-03) — cleared for Stage 3. Decisions: **two-tier cloner schema freeze** (hard invariant-key assertions + soft per-engine JSON goldens an M5b PR updates deliberately); **first-net scope as planned** (dispatch + cloners + spawn_browser/_resolve_profile/list_instances + F-202 archetype; exec surface netted later with M4-Ph2). Orchestrator verified the `.fn` unwrap seam against the repo's own `test_server_direct.py:27-29`. F-611 = characterize-current + standalone follow-up (consistent with the M1/M7 rulings).
- **Context (pinned, not re-derived):** LOCAL single-user tool, **0 users, breaking changes are FREE.** Priorities: (1) maintainability (2) operability (3) performance. Baseline: `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed**, coverage gate **39** (HEAD ~40.9%). `uv run` is BROKEN — never used. Integration tests (real Chrome, marker `integration`) are NOT in the 402 — **M6's net must be HERMETIC** (no Chrome, no backend, no network) to run in the default suite.
- **Lenses (ADDENDUM_LENSES.md, binding — applied to the TEST code too):** modularity · deduplication (**one canonical fixture/harness home, not N copies of the same mock**) · clarity (self-describing fixture/test names) · conventions (**one way to invoke a tool in-process; a second way is a defect**).

> **What makes M6 different:** M6 writes **tests only** and changes **no production behavior**. Its tests are **characterization tests** — they pin the *current observable behavior* of the tool surface (quirks and known bugs included) so that when M5a (10th), M4-Ph1 (13th), M5b (14th), and M4-Ph2 (15th) touch this surface, any behavior change surfaces as a failing test. M6 is the **safety net that GATES** those structural fixes. It is distinct from the behavior-pinning tests each fix-plan writes for its own change: M6 builds the net *under* the untested crown-jewel surface **before** the risky refactors arrive.

---

## 1. Scope

### 1.1 New test files (exact) + the canonical harness home

| New file | Purpose | Primary anchors it pins |
|---|---|---|
| `tests/fakes.py` | **THE canonical hermetic harness home** (one home — dedup/conventions lens). Holds the fake DOM/tab/browser/BrowserManager classes and the single in-process tool invoker. No test logic. | the `.fn` unwrap seam; the fake-CDP/fake-JS surfaces the cloners run against |
| `tests/conftest.py` (EXTEND, not replace) | Expose the `tests/fakes.py` objects as fixtures (`fake_tab`, `fake_browser`, `fake_browser_manager`, `call_tool`, `patched_server`). Keeps pytest's canonical fixture home authoritative. | fixture wiring only |
| `tests/test_tool_dispatch.py` | **Dispatch characterization** — the `section_tool`→`SECTION_TOOLS`→`mcp.tool` registry contract, the `.fn` unwrap seam, the true tool count (F-108), `apply_disabled_sections`, and the `get_tab → raise-if-missing → delegate` adapter contract on a representative tool per section; plus the **F-202 sync/async runtime archetype**. | `server.py:51,1208-1228,1279-1300` + representative tools |
| `tests/test_cloner_schemas.py` | **Cloner output characterization** — golden schemas for the 3 disagreeing "complete element" engines (F-140) + the 5 `ElementCloner.extract_*` methods + `ProgressiveElementCloner.expand_*` + the `FileBasedElementCloner` to-file summary shape, driven against fake tab/CDP. **This is the gate the BRIEF names for M5b/M5a.** | the 5 cloner modules |
| `tests/test_bug_prone_tools.py` | **Bug-prone tools** — `spawn_browser` param handling through the seam; `_resolve_profile_selection` (reference `test_profile_resolution.py`, gap-fill only); `list_instances` liveness (F-611). | `server.py:1305,1421` + `browser_manager.py:311,588,141` |

Total: **1 harness module + 1 conftest extension + 3 test modules.** No production code touched.

### 1.2 Confirmed anchors (re-opened at pinned SHA `2267b83d`; corrected where the brief's guess drifted)

**Dispatch mechanism (`embedded/server.py`):**

| Anchor @ HEAD | What it is |
|---|---|
| `SECTION_TOOLS: Dict[str, List[str]] = defaultdict(list)` `:51` | the registry — section → list of tool `func.__name__` |
| `is_section_enabled(section)` `:1208-1210` / `apply_disabled_sections()` `:1220-1228` | section gating (unregisters via `mcp.remove_tool`) |
| `def section_tool(section)` `:1212-1217` | the decorator: appends `func.__name__` to `SECTION_TOOLS[section]`, returns `mcp.tool(func)`. **M3-5 wraps this to stamp a correlation id** (see §1.3) |
| `mcp = FastMCP(...)` `:1279-1295`; `browser_manager/network_interceptor/dom_handler/cdp_function_executor = ...()` `:1297-1300` | the FastMCP app + the module-global singletons tools resolve by name at call time |
| `.fn` unwrap seam | **verified in the repo's own scratch harness** `test_server_direct.py:27-29`: `getattr(fn, "fn", fn)` — FastMCP's `mcp.tool()` returns a `FunctionTool` whose original coroutine is `.fn`. This is the in-process invocation seam. |
| `_with_cdp_timeout(coro, timeout=0, ...)` `:139-156` | `t = timeout or CDP_OPERATION_TIMEOUT`; `await asyncio.wait_for(coro, t)`. Fakes return instantly → no timeout interaction |

**Bug-prone dispatch-adjacent targets:**

| Anchor @ HEAD | What it is |
|---|---|
| `@section_tool("browser-management") async def spawn_browser(...)` `server.py:1305-1421` | the tool; delegates to `BrowserManager.spawn_browser` |
| `BrowserManager.spawn_browser` `browser_manager.py:311` (~236 lines, F-208) | the god method M13 extracts — no seam; only real coverage is integration |
| `_resolve_profile_selection` `server.py:1080-1176` (F-106, D-grade) + `_fallback_profile_selection` `:1177` | **already unit-tested** hermetically by `tests/test_profile_resolution.py` (tmp_path + env patches) |
| `list_instances` tool `server.py:1421-1450` | merges `browser_manager.list_instances()` + `persistent_storage.list_instances()` (→ `in_memory_storage` post-M15) |
| `BrowserManager.list_instances` `browser_manager.py:588-603` + `_browser_process_is_alive` `:141-156` (F-611) | prunes by **OS-process** liveness only — **no CDP round-trip** |

**The 5 cloners + the crown-jewel tool wiring** (confirmed via AST + `server.py` bodies):

| Module (LOC @ HEAD) | Class / singleton | "complete element" engine (F-140 schema) | Wired to tool(s) |
|---|---|---|---|
| `element_cloner.py` (648) | `ElementCloner` / `element_cloner` | `clone_element_complete:464` → **flat multi-key** (asyncio.gather of the 5 `extract_*`) | `clone_element_complete_to_file` (via file-based) |
| `comprehensive_element_cloner.py` (344) | `ComprehensiveElementCloner` / `comprehensive_element_cloner` | `extract_complete_element:39` → **flat**, keys incl. `selector,url,timestamp` | `clone_element_complete` tool `server.py:2920`; `clone_element_progressive` (delegates) |
| `cdp_element_cloner.py` (321) | `CDPElementCloner` (**no singleton — F-144**) | `extract_complete_element_cdp:33` → **nested snake_case under `element`** | `extract_complete_element_cdp` tool `server.py:~3308` (fresh `CDPElementCloner()` per call) |
| `progressive_element_cloner.py` (265) | `ProgressiveElementCloner` / `progressive_element_cloner` | `clone_element_progressive:30` (calls comprehensive) + `expand_*`, `list_stored_elements` | progressive-cloning tools `server.py:3061-3234` |
| `file_based_element_cloner.py` (648) | `FileBasedElementCloner` / `file_based_element_cloner` | 8 `*_to_file` methods → `{file_path, extraction_type, selector, summary}` (F-141) | file-extraction tools `server.py:3245-3413` |

The 5 `ElementCloner.extract_*` methods (`extract_element_styles:28` [CDP-native, F-142], `extract_element_structure:116`, `extract_element_events:171`, `extract_element_animations:226`, `extract_element_assets:281` — the last 4 still on the JS-eval path) back the `extract_element_*` tools `server.py:2666-2917`. The **F-202 handle_response sites** are `extract_element_assets:2845`, `extract_related_files:2917`, `clone_element_complete:2963` (synchronous calls, guarded).

### 1.3 Predecessor-shift note (M6 pins the tree AS IT IS when M6 runs)

M6 executes 9th; its base already includes every predecessor's change. The dispatch and cloner anchors above are HEAD line numbers — **re-anchor by symbol in Stage 3** because predecessors churn `server.py`:

- **M3 (correlation wrapper) — the load-bearing one for M6's dispatch net.** M3-5 rewrites `section_tool` (`:1212-1217`) to stamp a per-request correlation id, and is *explicitly designed to leave every tool's FastMCP schema byte-identical* (plan_M3 §4, pinned by M3-5's test). **Consequence for M6:** M6 pins the POST-M3 `section_tool` — the wrapper must preserve (a) the `.fn` unwrap seam, (b) `func.__name__` as recorded in `SECTION_TOOLS`, (c) the coroutine's signature/return. **M3 already adds a representative-tool `tools/list` schema snapshot "for a representative tool from each section" (plan_M3 §5.2, "ties into M6's intent").** M6 **extends, does not duplicate**: M6 pins the *registry + unwrap + count + adapter* contract; M3 pins *schema-identity of the wrapper*. (state.json L220: "M3 owns the correlation wrapper, M4 owns formalization.")
- **M2 (deletions):** `hot_reload`/`reload_status` are **DELETED** (`server.py ~:2974-3038`) — **do NOT characterize them** (state.json L219). Everything below ~:2974 (progressive-cloning `:3061`, `extract_complete_element_cdp` `:3308`, file-extraction `:3245-3413`) shifts **UP ≈ −66**. Surface is **94 tools** post-M2 (the F-108 count M6 pins).
- **M3 inserts above the tool region** (correlation wrapper + M10a logs) → tool anchors shift **DOWN**. Net of M2/M3: re-anchor by `@section_tool(...)` + `async def <name>(`.
- **M15 (rename):** `persistent_storage` → `in_memory_storage` (module + singleton). `list_instances` (`:1430`) and `progressive_element_cloner.py:14` now reference `in_memory_storage` — M6's `list_instances`/progressive characterization patches **`in_memory_storage`**, not `persistent_storage`. M15 also stores the **full** `model_dump` (more keys survive) — `list_instances` still reads only `instance_id/state/current_url/title`, unaffected.
- **M11a (import safety):** `ProcessCleanup.__init__` is side-effect-free + `conftest.py` sets `STEALTH_MCP_NO_AUTO_RECOVERY` → **`import server` is safe** in M6's base tree (no process kill on import). `env_utils.py` exists.
- **M7 (fakes precedent):** M7 uses **ad-hoc per-test** fake BrowserManager/fake tab (plan_M7 §5.1) — it did **not** create a shared harness. M6 creates the canonical home; retrofitting M7's fakes onto it is a *widen-later* note, **not** an M6 edit (M7's tests are out of scope).
- **M9 (network tools already netted):** `test_server_network_tools.py` characterizes the network **tool bodies** (`set/get_network_capture_filters`, `search_network_requests`, `export_network_data`). **M6 excludes network tools** (state.json: "fold in M9's tests, don't duplicate").

### 1.4 Explicit out-of-scope (stated)

- **All 94 tools.** M6 is a **scoped** net (dispatch mechanism + cloners + the named bug-prone tools), not per-tool coverage. Widening happens as M4/M5 touch each section (§5.5).
- **`hot_reload`/`reload_status`** — deleted by M2; never characterized.
- **Network tool bodies** — M9's `test_server_network_tools.py`. Do not duplicate.
- **Areas already netted by predecessors** — M3 (logging/correlation/silent-except AST guard), M1 (liveness), M8 (CLI), M2 (hot_reload-removed, fingerprint), M7 (close_instance/touch), M11a/M15 (env_utils, import-guard, field-round-trip), M9 (network). **Reference, never duplicate.**
- **Any production code change.** M6 adds no seam. If a tool is untestable without a seam (e.g. `spawn_browser`'s inner steps, F-208), **NOTE it as a finding for M4/M13 — do not add the seam here.** Characterize what the existing `.fn`/singleton seam allows.
- **Ratcheting `fail_under`.** M6 raises coverage as a side effect; note the delta (§5.4), do **not** move the gate (separate hygiene step).
- **Value-level extraction correctness** (does the CSS actually match the page). That needs real Chrome → integration. M6 pins **schema/shape**, not semantic fidelity.
- No drive-by refactors; any discovery → a new finding.

---

## 2. Approach + rejected alternatives

### 2.1 Chosen design

**In-process tool invocation via the `.fn` seam, with the module-global singletons monkeypatched to fakes.** A tool body is `server.<name>.fn`; it resolves `browser_manager`, `element_cloner`, `response_handler`, … as **names in `server`'s module namespace at call time**, so `monkeypatch.setattr(server, "browser_manager", fake)` (etc.) is a clean, hermetic seam that needs no production change. Invocation follows the suite's established convention: `pytest-asyncio` `async def test_` awaiting `.fn(**kwargs)` directly (168 async-test usages already in the suite; e.g. `test_cdp_timeout.py`, `test_network_interceptor.py`).

**One canonical harness (`tests/fakes.py`) exposed through conftest fixtures (dedup lens).** It provides exactly one of each:
- `FakeTab` — records `.evaluate(js)` and `.send(cdp_obj)` calls; returns canned responses from a configurable map; carries `.url`, `.target`. Covers **both** cloner paths (JS-eval and CDP).
- `FakeBrowser` — `.get(url, new_tab=…)`, `.target`, `_process` (a `poll()` stub), `_process_pid` (for the F-611 liveness path).
- `FakeBrowserManager` — seedable `get_tab`/`get_browser`/`list_instances`; returns fakes or `None`.
- `call_tool(server_mod, name, **kwargs)` — the **one** in-process invoker (unwrap `.fn`, await). Conventions lens: exactly one way to drive a tool in a test.

**Two characterization targets, two rigidities:**
1. **Dispatch mechanism** (`test_tool_dispatch.py`) — pin the *contract*, asserted structurally (not golden): every `@section_tool` name is in exactly one `SECTION_TOOLS` section; the registered object exposes `.fn` as a coroutine whose `__name__` equals the registry name; the flattened count equals the section-sum equals the live `mcp` tool count (F-108 tripwire, **94**); `apply_disabled_sections()` removes exactly the named section's tools; the `get_tab → raise "Instance not found" → _with_cdp_timeout(delegate)` adapter shape holds for a representative tool per section.
2. **Cloner outputs** (`test_cloner_schemas.py`) — **two-tier goldens** (the "pin without over-freezing" answer, §2.2): (a) **hard structural assertions** on the invariant contract — top-level key set, error shape `{"error": …}`, and the *nesting difference* that distinguishes the 3 engines (flat vs flat+camelCase vs nested-under-`element`); (b) **one soft golden JSON per engine** in `tests/goldens/`, captured from the current tree, that an M5b/M5a PR **diffs and updates deliberately**. A changed golden is a *review prompt*, not an automatic failure of intent.

**Quirk marking (so a characterization test never blocks a fix it's meant to enable).** Tests that pin *known-buggy* current behavior — the 3 disagreeing schemas (M5b fixes), the JS-eval path in 4 of 5 `extract_*` (M5a fixes, F-142), `list_instances` doing no CDP probe (F-611) — carry a `@pytest.mark.characterization` marker and a docstring: `PINS CURRENT BEHAVIOR incl. known quirk <F-id>; <M5a/M5b/M4> will intentionally change this — update the golden/assertion when that fix lands.` The fixer expects to touch them; a green M6 suite otherwise means *no unintended* change.

### 2.2 Rejected alternatives (≥3)

1. **Subprocess-MCP / JSON-RPC round-trip** (drive tools like `smoke_mcp.py` does over stdio). *Rejected:* it needs `uv run` (BROKEN here) or a live backend, is slow, is flaky (process/timing), and tests the transport, not the tool bodies. The `.fn` seam exercises the exact coroutine with zero process boundary. (`smoke_mcp.py` stays a scratch script, not a model.)
2. **Pure unit tests on extracted tool-body functions** (F-105's fix direction — lift request/response logic into plain functions). *Rejected for M6:* that is a **production refactor** (M4-Ph1's job) and M6 is tests-only. M6 must net the surface **as it is now**, before the refactor, so the refactor is verifiable. (M6 is the gate *for* that refactor, not the refactor.)
3. **Golden/full-snapshot everything** (freeze each cloner's entire output). *Rejected:* M5b will *legitimately* consolidate the 3 engines into 1 schema — a hard full-snapshot would fail on every legitimate line of that change, making the net cry wolf and get deleted. The **two-tier** design (hard invariant-key assertions + soft per-engine goldens the fixer updates) keeps the net honest: it catches an *accidental* shape change while *inviting* the deliberate one. Characterization ≠ over-specification.
4. **Assert-on-keys only (no golden files).** *Rejected as the whole answer:* key-only assertions miss value-shape drift (a list becoming a dict, a nested block flattening) that a consolidation could introduce silently. The soft golden captures the full current shape as a reviewable reference. (Kept as tier (a); the golden is tier (b).)
5. **Per-test hand-rolled mocks** (each test builds its own fake tab). *Rejected — dedup/conventions lens:* N copies of the same mock is exactly the defect the lenses forbid in production, applied to tests. One `tests/fakes.py` home; every test imports it. (This is also why M6, not M7, owns the canonical fakes — M7 predates the need and used ad-hoc ones.)
6. **Characterize via the public MCP `tools/list` only** (schema-level, no body execution). *Rejected as insufficient:* that is essentially what M3 already adds for a representative tool. The F-105/F-145 gap is the **bodies** (the delegation + the extraction), which only body execution against fakes reaches.

---

## 3. Sequencing (independently verifiable; one checkpoint commit per step)

> Discipline (every approved plan): each step is self-contained and leaves the suite green; **run the full hermetic suite after every step**; deviation from a confirmed symbol → **STOP and report**. Baseline before starting (post-M9 tree): `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → 402+predecessor tests green, coverage ≥ 39.

**M6-1 — The canonical harness (`tests/fakes.py` + conftest fixtures).**
- Add `tests/fakes.py` (FakeTab/FakeBrowser/FakeBrowserManager/`call_tool`) and the conftest fixtures wrapping them.
- A single smoke test proves the seam end-to-end hermetically: `await call_tool(server, "list_instances")` with a seeded `FakeBrowserManager` returns the expected list shape; `import server` triggers no Chrome/process side effect.
- *Verify:* `.venv\Scripts\python.exe -m pytest tests/test_tool_dispatch.py -q` (harness smoke) then `-m "not integration" -q` (all green, cov ≥ 39).

**M6-2 — Dispatch net (`tests/test_tool_dispatch.py`).**
- Registry invariants (name↔section↔`.fn`↔`mcp` count = 94, F-108); `apply_disabled_sections`; the `get_tab→raise→delegate` adapter contract for one representative tool per section; the **F-202 runtime archetype** (invoke the 3 `handle_response` tools with a fake cloner returning a dict; assert each returns a `dict`, not a coroutine, and raises no `TypeError` — the runtime complement to the existing AST guard `test_server_call_conventions.py`).
- *Verify:* `… pytest tests/test_tool_dispatch.py -q` then full hermetic suite.

**M6-3 — Cloner net (`tests/test_cloner_schemas.py` + `tests/goldens/`).**
- Drive each engine against `fake_tab`: the 3 complete-element engines (F-140 nesting differences), the 5 `ElementCloner.extract_*`, `ProgressiveElementCloner.expand_*` + `list_stored_elements`, the `FileBasedElementCloner` to-file `summary` shape. Tier-(a) structural assertions + tier-(b) soft goldens; quirk-mark the schema-divergence and JS-path tests.
- *Verify:* `… pytest tests/test_cloner_schemas.py -q` then full hermetic suite.

**M6-4 — Bug-prone net (`tests/test_bug_prone_tools.py`).**
- `spawn_browser` param handling through the seam (patch `browser_manager.spawn_browser` to a fake; assert the tool forwards its params and shapes the result — do **not** try to exercise the 236-line internals; note the seam gap for M4/M13); `_resolve_profile_selection` (reference `test_profile_resolution.py`; add only a *gap* case if the `_fallback_profile_selection` retry protocol is unpinned); `list_instances`/F-611 (fake browsers: process-alive stays, process-dead pruned, **process-alive-but-CDP-dead still reads active** — quirk-marked).
- *Verify:* `… pytest tests/test_bug_prone_tools.py -q` then full hermetic suite (final green + coverage delta recorded).

---

## 4. Breaking changes

**None — M6 is tests-only.** No production file, tool name, signature, schema, or config changes. `0 users` is moot here; there is nothing to break.

The *point* of M6 is that it **adds tests M5a/M4/M5b must keep green** — that is the gate, not a regression. Two nuances the downstream fixers must know (carried into their plans):
- A small, **explicitly marked** subset of goldens/assertions is **designed to change** under M5a (F-142 propagation) and M5b (schema consolidation). Those PRs **update** the marked goldens deliberately (a reviewed diff), and must leave the rest of the M6 net green.
- The F-108 count assertion pins **94**. If M4-Ph1 changes the tool set, it updates this one number with intent.

---

## 5. Test strategy (this plan *is* the test strategy)

### 5.1 What "characterization" means here
Pin the **current observable contract** of the dispatch mechanism and the cloner outputs — *including quirks and known bugs* — captured from the current tree, so a later refactor's behavior change becomes a failing test. This differs from a fix-plan's behavior-pinning test (which pins the *intended new* behavior of one change): M6 pins the *status quo of a surface* before multiple future changes touch it.

### 5.2 How goldens are captured and later used
Captured **from the current (post-M9) tree** by running each cloner against `fake_tab` and serializing the result into `tests/goldens/<engine>.json` (committed). A later refactor (M5b) that changes a schema produces a **golden diff** in its PR — the reviewer sees exactly what changed and updates the golden as part of that PR. Tier-(a) structural assertions run every suite; tier-(b) goldens are the human-reviewed reference. This is how the net "pins without freezing so hard legitimate M5b improvements can't land."

### 5.3 Hermetic + non-flaky (the audit's 0-flaky asset is sacred)
Every M6 test is **fully deterministic**: no real Chrome, no backend, no network, no `sleep`, no wall-clock/timing assertions, no ordering dependence on a real event loop beyond `await`. Fakes return canned data instantly (so `_with_cdp_timeout`'s `wait_for` never fires). The suite stays inside `-m "not integration"`. Any flake is treated as a stop-the-line defect, not a retry.

### 5.4 Coverage delta
M6 exercises tool bodies and cloner methods that today have **~0 body coverage** (F-105/F-145), so `server.py` + the 5 cloner modules gain meaningful line coverage — a clear net increase over the ~40.9% baseline. **The delta is recorded but `fail_under` is NOT ratcheted** (out of scope; a later hygiene step raises the gate once the net is broad).

### 5.5 How M6 composes with the fix-specific tests + the widen-later boundary
- **In the first net:** the dispatch *mechanism*; the cloner *output schemas*; `spawn_browser`/`_resolve_profile_selection`/`list_instances`; the F-202 archetype.
- **Widen-later (as M4/M5 touch each section):** the other ~80 tool bodies — element-interaction, cookies-storage, tabs, debugging, dynamic-hooks, cdp, python-in-browser — netted incrementally when M4-Ph2 splits each section (each section split ships with its slice of the net).
- **Never M6:** network tools (M9), deleted tools (M2), predecessor-owned areas (reference), value-level extraction fidelity (integration).
M6 **references, does not duplicate** the predecessor fix-tests enumerated in §1.3/§1.4.

---

## 6. Rollback + checkpoint commits

- **Branch:** `audit/fixes-2026-07-02`, serial **after M9's final commit**. Stage-3 discipline: full hermetic suite green at every checkpoint; **any deviation from a confirmed symbol → STOP and report** to the orchestrator.
- **One commit per step (4):** `M6-1 hermetic tool-invocation harness (tests/fakes.py + conftest)` · `M6-2 dispatch characterization net` · `M6-3 cloner output goldens` · `M6-4 bug-prone tool characterization`.
- **Rollback is trivial (tests-only):** any step reverts with a single `git revert`/reset; earlier checkpoints stay green and useful (the harness + dispatch net stand alone even if the cloner net is reverted). No production state to unwind.
- **PR:** one `{M6}` PR (four commits), stacked after M9 — matches the "one PR per fix" convention.

---

## 7. Risk (blast radius · worst case · early warning)

- **Over-specified goldens block a legitimate M5b diff** (the primary risk). *Blast radius:* M5b/M5a churn dozens of green assertions, tempting a fixer to gut the net. *Mitigate:* the two-tier design — hard assertions only on *invariant* contract; the full shape lives in soft, deliberately-updatable goldens; quirk-marked tests announce "you are expected to change me." *Worst case:* a fixer updates a golden — the intended workflow. *Early warning:* a golden diff appears in an M5b PR (the net *working*).
- **Flaky hermetic mock** (would corrode the audit's 0-flaky asset). *Mitigate:* zero timing/network/Chrome; canned instantaneous returns; deterministic fakes in one reviewed home. *Worst case:* a nondeterministic fake — caught immediately because any M6 flake is stop-the-line. *Early warning:* an intermittent failure on repeat runs of the same commit.
- **A characterization test pins a BUG so M5a/M4 can't fix it.** *Mitigate:* every known-quirk test is `@pytest.mark.characterization` + a docstring naming the F-id and the fix that will change it, so the fixer updates rather than fights it. *Worst case:* an unmarked quirk surprises a fixer — mitigated by cross-referencing F-140/F-142/F-611 in the test docstrings up front. *Early warning:* an M5a/M5b step fails an *unmarked* M6 test (signals either a real regression or a missing marker — investigate, don't blindly update).
- **False confidence from shallow tests** (adapter stubs that assert tautologies). *Mitigate:* the cloner net drives **real extraction logic** against fake CDP/JS (not a stubbed return), and the dispatch net asserts the *actual* delegation + the F-202 runtime path, not "a dict is a dict." *Worst case:* a body change slips through a gap in the scoped net — bounded by design (the net is explicitly scoped; widening is tracked). *Early warning:* a bug reaches a fixer's own behavior test that M6 should have caught → widen the net there.
- **Seam assumption breaks** (`.fn` unwrap or module-global patching stops working after M3's wrapper). *Mitigate:* M6-2 pins the seam itself (unwrap → coroutine with correct `__name__`); if M3's correlation wrapper ever changed `.fn`, M6-1's smoke test fails first, loudly, before any downstream fix relies on it.

---

## 8. Findings closed

- **F-105 (High — 0 of 94 tool bodies unit-tested) — SCOPED-CLOSED (net established; full closure = widen-as-touched).** How: the `.fn`-seam harness + the dispatch net put the **first** direct-execution coverage under the tool surface (registry contract, adapter shape for a representative tool per section, the F-202 runtime archetype). M6 does **not** unit-test all 94 bodies (that is the widen-later boundary, driven by M4-Ph2); it closes the *"there is no way to exercise a tool body hermetically"* half and leaves the *"every body is covered"* half to incremental widening. Cited per brief.
- **F-145 (High — cloner extraction untested) — SCOPED-CLOSED (the M5b/M5a gate).** How: `test_cloner_schemas.py` pins the current output schema of all 5 cloner modules against fake tab/CDP, so M5b's consolidation and M5a's hang-fix propagation produce a **visible, reviewable diff** rather than a blind change. This is precisely the "add extraction-correctness tests … so a consolidation PR can diff old-vs-new output" the finding asks for. Cited per brief.
- **F-108 (Low — tool count 90/96/99 disagree) — PINNED, not fixed.** M6 asserts the **true** count (`len(flattened SECTION_TOOLS)` == section-sum == live `mcp` tool count == **94** post-M2) as a dispatch tripwire. The *fix* (derive the 3 printed numbers from `SECTION_TOOLS`) stays M4/M14 — M6 only makes a future disagreement fail a test.
- **F-106 (Medium — `_resolve_profile_selection` D-grade) — DISPOSITION: reference, do not re-net.** It is **already** unit-tested hermetically (`tests/test_profile_resolution.py`); M6 references that suite and adds a case **only** if the `_fallback_profile_selection` retry protocol is found unpinned. The D-grade **split** (pure decision vs I/O) is M4/M13's refactor, not M6's — M6 does not add the seam.
- **F-611 (Medium, unverified — `list_instances` no CDP round-trip) — RULING: IN as scoped characterization; the fix stays a standalone follow-up.** Per state.json L207/L134 ("re-homed to M6 + standalone follow-up; unverified, feature-add not freeze-fix"), M6 **characterizes the current contract**: `list_instances` prunes by OS-process liveness only, so a **process-alive-but-CDP-dead** instance still reads `active` (quirk-marked). M6 does **not** add a CDP probe (a feature-add, out of tests-only scope); the probe is filed/kept as the standalone follow-up finding, and M6's pinned test is the tripwire that will flag that future change as intentional.
- **Related duplication findings pinned as a side effect (not closed — that's M5b):** F-140 (3 schemas), F-142 (JS-path in 4/5), F-143 (dead dual-schema fallback), F-141 (to-file boilerplate ×8), F-144 (per-call CDP cloner), F-203/F-601 (5-module duplication) — M6's cloner goldens make each of these a *diffable* surface for the consolidation that closes them.

---

### Header recap for the gate
Pinned SHA `2267b83d3efda03f93936db2c34ded33aaa0d701` · 2026-07-03 · batch **{M6}** · base = post-M3(+A1)+M1+M8(+A1)+M2+M7+M11a+M15+M9 · **status DRAFT awaiting human approval.** Four lenses applied to the test code: **one** canonical harness home (`tests/fakes.py`), **one** in-process invoker (`call_tool`), self-describing fixture/test names, no second way to drive a tool.
