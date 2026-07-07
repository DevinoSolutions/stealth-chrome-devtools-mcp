# Stage 2 Plan — M4-Ph1 (Phase-1 server.py decomposition) + M13 (spawn_browser seam)

- **Status:** **APPROVED by human 2026-07-03 (approve as-is)** with TWO gate rulings: (1) **C3 envelope depth OVERTURNED → FULL 22-site sweep in Ph1** (rejected-alt D1 is now the mandate — **Amendment A1 below expands C3**); (2) F-760/F-743 renames = caveat-now/rename-at-Ph2 (as recommended). Exec-family docstring sharpening confirmed in C2.
- **Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend`
- **Date:** 2026-07-03
- **Batch:** **{M4-Ph1, M13}** + routed lens findings **F-701** (STEP 0), **F-700, F-743, F-746, F-760, F-761** (registry/envelope pass). Re-homed: **F-164 server.py half** (from plan_M7), the **M10b error-envelope Ph1 slice** (absorbed into M4 per the brief).
- **Runs:** **11th** in Stage-3 serial order. **Base tree** = pinned SHA + approved plans **M3(+A1) + M1 + M8(+A1) + M2 + M7 + M11a_M15 + M9 + M6 + M12a** (fix_order: M3, M1, M8, M2, M7, M11a, M15, M9, M6, M5a, M10a, M12a, **M4-Ph1(+M13)**, M5b, M14 — M5a/M10a already landed inside M3/M6 batches; M4-Ph1 is the 13th plan item but 11th distinct execution).
- **Context (pinned, not re-derived):** LOCAL single-user tool, **0 users, breaking changes are FREE.** Priorities: (1) maintainability (2) operability (3) performance (order-of-magnitude only). Verified this session: HEAD == pinned SHA, `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed** (24 deselected) in 75 s. `uv run` is BROKEN (path has `&`+spaces) — **always use `.venv\Scripts\python.exe` directly**. CI coverage gate `fail_under=39`.
- **Lenses (ADDENDUM_LENSES.md, binding, weight within priority 1):** **Modularity** (high cohesion, low coupling; every module understandable in isolation — agents have limited context windows) · **Deduplication** (each behavior in exactly one place) · **Clarity** (self-describing names; renames are legitimate fixes) · **Conventions** (one way per thing everywhere; **a fix that introduces a second way of doing something is a defect**). Security: no re-architecture; only quote-backed regressions (none filed here).

> **This plan is the pipeline's largest.** §1.3 (predecessor-shift table) is its most load-bearing section — server.py is the most-shifted file in the campaign. Every step is independently verifiable with `pytest -m "not integration"` green before and after, and each step is a single checkpoint commit. A cold Sonnet agent with no conversation context can execute it in plan order.

---

## 0. What was verified at HEAD before writing (results inline)

Re-opened at the pinned SHA (targeted reads; not whole-file dumps). All numbers below are HEAD facts; the shift table (§1.3) converts them to base-tree facts.

- **server.py size:** **4208 lines, 162 top-level `def`s, 97 `section_tool(` occurrences** = 1 decorator definition (`def section_tool` @:1212) + **96 `@section_tool(...)` registrations** across 11 sections. Post-M2 (deletes `hot_reload`+`reload_status`) the base-tree count is **94 registrations**. (F-101/F-505/F-612.)
- **Profile/clone-storage subsystem:** **47 helpers, :225–:1074** (computed). 7 path *resolvers* (`_default_session_root`@225 … `_profile_has_running_browser`@270) + 40 *storage/eviction/copy* helpers (`_clone_storage_cap_bytes`@330 … `_copy_clone_from_source`@1064). The block through `is_section_enabled`@1208 is **982 LOC**. F-201 quotes the ~700-LOC storage core (`_trash_clone`@449 … `_copy_profile_tree`@888). (F-201.)
- **`_resolve_profile_selection`** @:1080–1176, **`_fallback_profile_selection`** @:1177–1205 — depend on the resolvers + `_copy_clone_from_source`/`_protect_clone_dir`/`_snapshot_needs_refresh` above them. (F-106.)
- **`section_tool` decorator** @:1212–1217 (at HEAD; **M3 wraps its body with a correlation-id stamp** — see §1.3); `is_section_enabled`@1208, `apply_disabled_sections`@1220–1228 (`mcp.remove_tool` per name), `DISABLED_SECTIONS = set()`@50, `SECTION_TOOLS = defaultdict(list)`@51. (F-101/F-505/F-612.)
- **`_with_cdp_timeout`** @:139–156: `t = timeout or CDP_OPERATION_TIMEOUT`; `await asyncio.wait_for(coro, t)`; on `asyncio.TimeoutError` → `raise Exception(...)` (plain, untyped). **~46 `_with_cdp_timeout(...)` call sites** in server.py + **4 raw `asyncio.wait_for(...)` call sites** (3 tool-body: :1494 get_instance_state, :2495, :2532; plus the wrapper's own :149 — *orchestrator touch-up: grep confirms 4 call sites, the 5th match is the :140 docstring mention*). (F-164; F-603 is the ×49 timeout-preamble context.)
- **Instance-not-found error shapes (F-761), 4 distinct forms confirmed:** (a) **~40× `raise Exception(f"Instance not found: {instance_id}")`** (the dominant tool-body form, e.g. :1588, :1650, :2652, :3510); (b) **3× `return json.dumps({"error": "Instance not found"})`** in `@mcp.resource` string-returning resources (:2406, :2424, :2456); (c) **~7× `return {"success": False, "error": f"Instance not found: {instance_id}"}`** in the cdp-functions section (:3588, :3726, :3747, :3770, :3815, :3845, :3878); (d) `_with_cdp_timeout`'s own timeout message (:152).
- **Error-envelope archetypes (F-104), 4 incompatible contracts confirmed:** `execute_script` returns `{"success": bool, "result": ..., "error": ...}` (:1939–1949); `get_debug_lock_status` returns bare `{"error": str(e)}` with no `success` key (:2556–2559); `new_tab` does `raise Exception(f"Failed to create new tab: {str(e)}")` — re-wrapped generic Exception discarding type (:2662–2663); the cdp-functions tools return `{"success": False, "error": ...}`. (F-104.)
- **`get_instance_state` (F-746):** docstring promises "Complete state information" (:1490); two except paths (TimeoutError :1498, Exception :1516) return **partial** records `{"partial": True, "detail_error": ...}`; the success path returns `{"partial": False, ...}`. The contract "Complete" is a lie on the except paths.
- **`spawn_browser` (BrowserManager.spawn_browser, F-208):** @:311–546 (the next method `_setup_dynamic_hooks` starts @:548). Phase boundaries confirmed: instance construct :321–328 · proxy config/forwarder :339–349 · executable+type detect :351–369 · arg merge (`caller_args`→`merge_browser_args`→stealth-filter→`--no-sandbox`) :371–389 · `uc.Config`+`uc.start()` :391–399 · post-launch (data-dir read :401–407, `process_cleanup.track_browser_process` :409–419, extra headers :421–424, viewport :426–436, timezone override :438–441) · then `except asyncio.CancelledError` cleanup :488–518 and `except Exception` cleanup :519–544. One try/except spanning :332–544. (F-208.)
- **`_with_cdp_timeout` half of F-164:** confirmed server.py owns it; M7 fixed only the `cdp_function_executor` (tab-in-hand) half. (F-164.)
- **embedded/ import style:** every embedded module imports siblings **by bare name** (`from browser_manager import BrowserManager`, `from debug_logger import debug_logger`, …) relying on `sys.path.insert(0, …/embedded)` done by the entrypoint (`cli.py:_ensure_embedded_on_path`) and by `tests/conftest.py:20-21`. `embedded/__init__.py` is **0 bytes** (empty). **F-701 / F-604 / F-613.**
  - **Second-way defect already in-tree:** `file_based_element_cloner.py:21-24` does `try: from .response_handler import … except ImportError: from response_handler import …` — a package-relative import *and* a bare import guarded by `try/except`. This is the exact "two ways" the conventions lens forbids; STEP 0 collapses it to one.
  - **sys.path hacks to retire/repoint:** `tests/conftest.py:20-21`, `tests/test_cdp_timeout.py:28-29`, `test_close_noise_filter.py:23-24`, `test_element_cloner_output_dir.py:40-41`, `test_execute_script_guard.py:22-23`, `cli.py:_ensure_embedded_on_path`, `comprehensive_element_cloner.py:19` + `file_based_element_cloner.py:15-16` (`sys.path.append(project_root)`), plus root scratch scripts (excluded, left alone). `tests/test_profile_resolution.py:19` comments "bare imports via sys.path".
- **Two entry-point surfaces (F-700/F-109):** `pyproject.toml [project.scripts]` = `stealth-chrome-devtools-mcp = "…server:main"` **and** `stealth-chrome-devtools = "…cli:main"`. `cli.py` is a second, independently-shipped command surface (status/profiles/cleanup/doctor/serve[+M8 stop/restart/kill-orphans]).
- **`__main__` block:** @:4139–4207; `--list-sections` prints hardcoded per-section tool counts (:4147–4157, sums to 96 pre-M2 — but these are the marketing counts, **F-108 is M14's** to reconcile). **M3 adds `configure_logging("backend")` + `sys.excepthook`/`faulthandler` here — MUST be preserved.**
- **4 execute-* / exec-family tools (F-743):** `execute_script`@1894, `execute_cdp_command`@3561, `inject_and_execute_script`@3752, `execute_function_sequence`@3798, `execute_python_in_browser`@3862, `call_javascript_function`@3708 — an overlapping "run some code" cluster with under-differentiated docstrings.

---

## 1. Scope

### 1.1 Files touched

**Source — modified (server.py is the spine; everything else is thin/mechanical):**

| File | Nature of change | Components |
|---|---|---|
| `src/stealth_chrome_devtools_mcp/embedded/__init__.py` | Populate as a real package marker (docstring; no re-exports needed) | STEP 0 |
| **every** `src/stealth_chrome_devtools_mcp/embedded/*.py` | Rewrite bare sibling imports → **one canonical form** (§2, STEP 0). ~22 modules. | STEP 0 |
| `src/stealth_chrome_devtools_mcp/embedded/server.py` | Extract eviction → delegate; formalize registry; add error envelope + `_require_tab`; make `_with_cdp_timeout` canonical; carry M3/M9/M11a shifts | C1–C5 |
| `src/stealth_chrome_devtools_mcp/embedded/clone_storage.py` | **NEW.** The extracted profile/clone-storage subsystem (F-201). | C1 |
| `src/stealth_chrome_devtools_mcp/embedded/tool_registry.py` | **NEW.** The formalized `section_tool`/`SECTION_TOOLS`/`DISABLED_SECTIONS`/`apply_disabled_sections` mechanism (carries M3's correlation wrapper). | C2 |
| `src/stealth_chrome_devtools_mcp/embedded/tool_errors.py` | **NEW.** The one error envelope + `_require_tab`/`_require_browser` helpers (F-104/F-761/F-746). | C3 |
| `src/stealth_chrome_devtools_mcp/embedded/browser_manager.py` | Extract `spawn_browser` into a private-method pipeline (M13/F-208). Import rewrite (STEP 0). | C4, STEP 0 |
| `src/stealth_chrome_devtools_mcp/cli.py` | Import rewrite for the new package convention (STEP 0); reconcile the entry-point story (F-700 — **documentation/comment only**, see §2 D). | STEP 0, C2 |
| `src/stealth_chrome_devtools_mcp/__init__.py` | (only if STEP 0 needs a re-export; expected: no change beyond `__version__`). | STEP 0 |

**Config:**

| File | Change |
|---|---|
| `pyproject.toml` | `[project.scripts]` entry-point targets rewrite **iff** STEP 0 changes the module path of `server:main`/`cli:main` (decision in §2 A — recommended convention keeps these targets **unchanged**, so expected: no edit). `[tool.hatch.build.targets.wheel]` unchanged. |

**Tests — new (behavior-pinning, written BEFORE each risky step) + edited (mechanical, M6 net):**

| File | Change | Step |
|---|---|---|
| `tests/conftest.py` | Rewrite the `sys.path.insert` bootstrap to import via the canonical package form (STEP 0). Preserve M11a's `STEALTH_MCP_NO_AUTO_RECOVERY` setdefault + M6's fixture wiring. | STEP 0 |
| `tests/*.py` (the 5 with `sys.path.insert`) | Mechanical: drop the local `sys.path` hack, rely on conftest/package import. | STEP 0 |
| `tests/test_clone_storage.py` | **NEW.** Import-location + behavior pins for the extracted module (delegation identity, no behavior change). | C1 |
| `tests/test_tool_registry.py` | **NEW.** Registry contract as a module (decorator stamps correlation id, section membership, `apply_disabled_sections`, count-94 tripwire) — complements M6's `test_tool_dispatch.py`. | C2 |
| `tests/test_tool_errors.py` | **NEW.** Envelope + `_require_tab` shape pins. | C3 |
| `tests/test_bug_prone_tools.py` (M6, EXTEND) | Add spawn_browser sub-method seam assertions (M13). | C4 |
| `tests/test_cdp_timeout.py` (EXTEND) | Pin `_with_cdp_timeout` as the single wrapper after F-164 consolidation. | C5 |

### 1.2 Explicitly OUT of scope (stated so Stage 3 does not scope-creep)

- **M4-Ph2** — the full per-section split of the 94 tool bodies into `tools/<section>.py`. **DEFERRED** (gated on wider M6 coverage). This plan does **not** move any tool body out of server.py. The Ph1/Ph2 boundary is stated crisply in §1.2.1.
- **M5b cloner consolidation** — do NOT touch the 5 cloner engines' internals (`element_cloner.py`, `comprehensive_element_cloner.py`, `progressive_element_cloner.py`, `file_based_element_cloner.py`, `cdp_element_cloner.py`). STEP 0's import rewrite touches their **import lines only** (mechanical); their bodies are M5b's.
- **M10b full 22-site error sweep** beyond the Ph1 depth chosen in §2 C. The rest is widened as M4-Ph2 touches sections; recorded as debt for M14.
- **M11b DI/reset seams; M12b sandboxing (REJECTED).**
- **Deleted tools** — `hot_reload`/`reload_status` are gone via M2; do not reference them. Tool COUNT stays **94** unless a deliberate rename (F-760/F-743) changes it — see §2 E (recommended: **caveat-now, no rename**, so count stays 94).
- **F-108 tool-count doc reconciliation** (the 90/96/99 disagreement, `--list-sections` marketing counts) — **M14's**.
- **singleton.py / process_cleanup internals** — owned by M1/M2/M8/M11a. This plan touches only their **import statements** (STEP 0) and the two symbols M11a/M15 already export (`process_cleanup.activate`, `DEFAULT_PORT`, the `in_memory_storage` rename) as *consumers*, never re-defining them.
- **The M8-A1 port INVARIANT is inherited, not touched:** the backend port is the CHOSEN port (possibly ≠ 19222), flowing by argument from `ensure_server_running`'s return and via `server.json`. This plan **never re-hardcodes 19222** and adds no port logic.

#### 1.2.1 The Ph1 / Ph2 boundary (crisp statement)

- **Ph1 (this plan) moves *non-tool* code out and *formalizes the seams*:** (a) the eviction/clone-storage subsystem leaves server.py entirely (C1); (b) the registry, error envelope, and CDP-timeout wrapper become named modules/helpers with one canonical shape (C2/C3/C5); (c) `spawn_browser` gains an internal method seam (C4). **All 94 tool bodies stay in server.py**, now calling the extracted helpers.
- **Ph2 (deferred) moves the *tool bodies* out:** each section's tools migrate to `tools/<section>.py`, registering against the shared `mcp` via the now-formalized registry. Ph2 is gated on M6 having characterized each section it splits. Ph1 makes Ph2 mechanical (the registry + envelope + delegation seams already exist).

### 1.3 Predecessor-shift table (THE critical section — symbol-anchored, per predecessor per file)

server.py is the most-shifted file in the pipeline. **Rule for Stage 3: re-anchor by symbol, never by raw line number.** Line numbers below are HEAD (pinned SHA); the base tree has them shifted. Locate by the quoted symbol/substring.

#### 1.3.1 `embedded/server.py`

| Predecessor | Symbol it shifts / edits | What it did | M4-Ph1 interaction |
|---|---|---|---|
| **M3** | `def section_tool(section)` @:1212 | **Wrapped the decorator body** to stamp a per-tool-call **correlation id** (F-308). The registered wrapper reads `correlation_id_var` / sets it around the tool call. | **C2 MUST carry this wrapper forward** into `tool_registry.py`. The correlation-stamp logic is the single most important thing to preserve when moving the decorator. Copy it verbatim into the new module; do not "simplify" it. |
| **M3** | `__main__` block @:4139 | Added `configure_logging("backend")` + `sys.excepthook` + `faulthandler` at backend entry. | **Preserve `configure_logging("backend")` in `__main__`** (binding directive). C2's registry move does not touch `__main__`; verify the call still runs after the import reshuffle. |
| **M3** | `debug_logger` bridge + `parse_bool_env`@54 usage | Unconditional recording; M10a logs scattered through tool bodies. | Tool bodies stay in place (Ph1). Carry M10a log lines through untouched when editing an envelope site. |
| **M2** | `hot_reload`@:2974-3009, `reload_status`@:3012-3038, `import importlib`@:6 | **DELETED** all three (−~66 lines below :2974; −2 tools → **94**). | Base-tree tool count = **94**. The `debugging` section no longer contains those two. Registry mechanics untouched by M2. |
| **M11a** | `parse_bool_env`@54-58, `parse_float_env`@61-68 | **DELETED**; re-imported from new `embedded/env_utils.py`. | server.py now does `from env_utils import parse_bool_env, parse_float_env` (or the canonical form after STEP 0). C1/C3/C5 must use these imports, not re-define. STEP 0 rewrites this import line too. |
| **M11a** | `app_lifespan`@:1231 | **ADDED** `process_cleanup.activate()` before `_spawn_background_sweep("startup")` / `yield`. Also `app_lifespan` IS the HTTP lifespan (`lifespan=app_lifespan`@:1294). | Preserve both. C1 does NOT touch `app_lifespan` (it calls `_spawn_background_sweep`, which moves to `clone_storage`; so `app_lifespan` will call `clone_storage.spawn_background_sweep("startup")` after C1 — a delegation edit, see §3 C1). Keep `process_cleanup.activate()` intact. |
| **M15** | `from persistent_storage import persistent_storage`@:44 | **RENAMED** module+singleton → `in_memory_storage`. server.py:44 import + `list_instances` body reference (`:1430`) now use `in_memory_storage`. | STEP 0 rewrites the import line to the canonical form of `in_memory_storage`; the `list_instances` body reference stays (Ph1 keeps tool bodies). |
| **M15** | `--singleton-port default=19222` (top-level arg, server.py:23 region) | **Changed** to `import DEFAULT_PORT` from singleton. | Do not re-hardcode 19222. STEP 0 rewrites the `DEFAULT_PORT` import line to canonical form. |
| **M9** | 2 capture-filter tools (`set_network_capture_filters`) + body-consuming tools (`get_response_details`@2110, `search_network_requests`@2157, `export_network_data`@2199) | **ADDED** `capture_bodies` param + capture-state notes. | Network-debugging section, disjoint from C1–C5. If an F-104 envelope site (C3) happens to be one of these tools, carry M9's added param/notes through. |
| **M1** | — | **NO server.py edit** (explicitly "no `/healthz` route"). | None. |
| **M8** | — | **NO server.py edit** ("M4 — zero server.py edits"; reciprocally M8 edits cli.py + singleton.py only). | None. |
| **M12a** | — | **server.py READ-ONLY** (hook-tool descriptions verified truthful). | None. |
| **M6** | — | tests-only; no server.py edit. Pins server.py contracts (see §1.3.4). | C1–C5 must keep M6's pins green. |

#### 1.3.2 `embedded/browser_manager.py`

| Predecessor | Symbol | What it did | M4-Ph1 interaction |
|---|---|---|---|
| **M7** | `close_instance`@:605 (+ `_do_close`@:617, lock held :618-757) | **Off-loaded** the synchronous kill to a thread executor (F-180). | **DISJOINT from C4.** `spawn_browser` is :311-546; `close_instance` is :605+. C4 touches only :311-546. No overlap. Table this disjointness precisely: C4 edits end at :546; M7's edits start at :605. |
| **M7** | `get_tab`@:1060 (`touch_activity` default), `get_browser`@:1082 (`touch_activity` default) — F-745 | **Flipped** `touch_activity` default. | `spawn_browser` does not call `get_tab`/`get_browser` (it constructs the instance directly). C4's sub-methods do not touch these. No interaction. |
| **M11a** | `_parse_nonnegative_int_env`@:31-56 (3-arg) | **DELETED**; re-imported from `env_utils`. Call sites `:71,:75` re-pointed. | C4 must not re-introduce a local env-parse. `_resolve_idle_timeout_seconds` (called by spawn @:336) uses these — leave as-is, they resolve via env_utils. STEP 0 rewrites browser_manager's import block including the env_utils line. |
| **M15** | `from persistent_storage import persistent_storage`@:17 | **RENAMED** → `in_memory_storage`. | STEP 0 rewrites this import line to canonical form. |
| **M6** | — | tests-only; pins `spawn_browser` via the `.fn` seam with fakes, `_resolve_profile_selection`, `list_instances`. | C4 must keep these green; extend deliberately. |

#### 1.3.3 `cli.py`

| Predecessor | Symbol | What it did | M4-Ph1 interaction |
|---|---|---|---|
| **M8(+A1)** | `_DISPATCH`, `build_parser`, `_cmd_status`/`_cmd_doctor` | **ADDED** `stop`/`restart`/`kill-orphans` verbs; extended status/doctor with pid+log-dir; A1 port selection lives in singleton.py not cli. | STEP 0 rewrites cli's `import server` / `import singleton` to canonical form. C2's F-700 reconciliation is **comment/docstring only** (state the "one tool surface" story; do NOT merge cli into the registry). |
| **M11a** | `kill-orphans` call | **Repointed** `process_cleanup._recover_orphaned_processes()` → `process_cleanup.recover_orphans()`. | STEP 0 import rewrite only; leave the call. |
| **M15** | `cli.py:258` `--port default=19222` | **Changed** to `import DEFAULT_PORT`. | Do not re-hardcode. STEP 0 import rewrite. |
| **M1** | `_cmd_status`/`_cmd_doctor` | Report responsive/wedged/down via `_probe_backend_status`. | STEP 0 import rewrite only. |

#### 1.3.4 `tests/` (M6 is the gating net; M11a + others also touch conftest)

| Predecessor | Symbol | What it did | M4-Ph1 interaction |
|---|---|---|---|
| **M6** | `tests/fakes.py` (NEW), `conftest.py` (fixtures), `test_tool_dispatch.py`, `test_cloner_schemas.py` + `tests/goldens/`, `test_bug_prone_tools.py` | Canonical fakes + `.fn` unwrap seam + **tool-count-94 tripwire** + two-tier cloner goldens (HARD invariant keys vs SOFT per-engine goldens) + spawn_browser/`_resolve_profile_selection`/`list_instances` pins. | **THIS NET GATES M4-Ph1.** Every step keeps it green. STEP 0 rewrites conftest's `sys.path` bootstrap (preserving fixture wiring). C1's extraction must keep `test_bug_prone_tools`'s `_resolve_profile_selection` pins green (the resolvers move; the tests import via server or the new module — decide in §2 A). Deliberate golden/name updates allowed **only** with explicit justification per M6's two-tier freeze (§5). |
| **M11a** | `conftest.py:28-31` region | **ADDED** `os.environ.setdefault("STEALTH_MCP_NO_AUTO_RECOVERY","1")`. | Preserve when STEP 0 rewrites conftest's bootstrap. |
| **M9** | `test_server_network_tools.py`, `test_network_*` | NEW network pins. | Untouched by M4-Ph1. |

---

## 2. Approach + rejected alternatives (≥2 rejected per major component)

### A. STEP 0 — canonical import convention for the `embedded/` package (F-701 / F-604 / F-613)

**The decision that shapes everything downstream:** what is the one way an embedded module imports a sibling?

**CHOSEN — absolute package imports with `embedded/` as a real subpackage; keep both entry-point shims working.**
- `embedded/__init__.py` becomes a real (non-empty) package marker.
- Every intra-`embedded` import becomes **absolute-from-package**: `from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger`. (Not relative `from .debug_logger import …`, and not bare `from debug_logger import …`.)
- **Rationale (lenses):** *Conventions* — one and only one import spelling everywhere (kills the `file_based_element_cloner.py` try/except dual-import defect). *Clarity* — a light model reading `from stealth_chrome_devtools_mcp.embedded.X import Y` knows exactly where Y lives without a `sys.path` mental model. *Modularity* — the package is importable as itself; no global `sys.path` mutation required for correctness.
- **Entry-point compatibility (the one subtlety):** `pyproject [project.scripts]` targets `stealth_chrome_devtools_mcp.server:main` and `…cli:main`. Today `server.py` lives at `embedded/server.py` and is loaded via `runpy.run_path(EMBEDDED_DIR/"server.py")` from a top-level `server.py` shim (per plan_M3's note about `main()` → `runpy.run_path`). **This plan does NOT relocate server.py** (that is a bigger move; it would churn every anchor and collide with the whole pipeline's re-anchoring). Instead: keep the `sys.path.insert(…/embedded)` bootstrap **in exactly one place** — the package's own `__init__.py` (or the existing top-level shim) — as a *compatibility shim*, documented as the single sanctioned sys.path site, and make **all module-to-module imports** use the absolute package form so they work under both the installed-package import and the shim. Net: one sanctioned sys.path line (down from ~8 scattered ones), one import spelling everywhere.
- **Verification:** the suite imports `embedded` modules via conftest today; after STEP 0 it imports them via the package. `pytest -m "not integration"` must be **402 green** before and after (STEP 0 is behavior-preserving by construction).

*Rejected alt A1 — package-relative imports (`from .debug_logger import …`).* Cleaner-looking, but (a) `server.py` is loaded via `runpy.run_path` as a top-level module in the current architecture, where relative imports raise `ImportError: attempted relative import with no known parent package` unless server.py is also relocated to be imported as `…embedded.server` — which is the Ph2-scale move this plan explicitly defers; (b) it splits the convention (relative inside embedded, absolute from cli/tests) — a second way, a conventions defect. Rejected.

*Rejected alt A2 — keep bare imports, just make `sys.path` insertion robust/centralized.* Minimal churn, but it **preserves** the fragile convention F-701/F-604 name as the root cause (imports only resolve because of a global side effect); the `file_based_element_cloner` try/except stays justified; a cold agent still can't place a new module without knowing the sys.path trick. It fixes the symptom (fragility) not the root (no real package). Rejected — the brief routes F-701 as "make embedded/ a REAL package," which A2 does not do.

*Rejected alt A3 — relocate `server.py`/`cli.py` into the package root and split tools now.* This is M4-Ph2 + a packaging change; it would re-number every anchor in the most-shifted file mid-pipeline and collide with M5b/M14. Out of Ph1 scope by directive. Rejected.

### B. Component 1 — extract the clone-eviction/trash subsystem (F-201)

**CHOSEN — one new module `embedded/clone_storage.py` holding the full 47-helper profile/clone-storage subsystem (:225–:1074); server.py imports it and the two profile-selection functions delegate.**
- Move all 47 helpers (`_default_session_root` … `_copy_clone_from_source`) into `clone_storage.py`. Move `_resolve_profile_selection`/`_fallback_profile_selection`/`_public_profile_selection` **with them** (they are the storage subsystem's public face — cohesion). server.py's `spawn_browser` tool body calls `clone_storage.resolve_profile_selection(...)`.
- Names: the module is `clone_storage` (self-describing, matches F-201's suggested `clone_storage.py`). Public functions drop the leading underscore where they become the module's API (`resolve_profile_selection`, `run_storage_sweep`, `spawn_background_sweep`, `enforce_session_storage`, `clone_is_auto`, `clone_is_named`, `default_session_root`, `master_profile_dir`, `clone_root_dir`, `master_snapshot_dir`, `clone_storage_cap_bytes`, `session_storage_cap_bytes`); internal-only helpers keep the underscore. cli.py already reaches into `server._clone_is_auto`/`server._master_profile_dir`/etc. (verified) — **those call sites repoint to `clone_storage.*`** (STEP 0-adjacent; a mechanical rename in cli.py, listed under C1).
- **Rationale (lenses):** *Modularity* — the 982-LOC storage domain becomes a module understandable in isolation; server.py sheds ~⅕ of its bulk and stops being "tool router + filesystem GC." *Deduplication* — one home for storage logic (cli.py stops depending on server.py internals for it). *Clarity* — `clone_storage.resolve_profile_selection` reads correctly to a light model.
- **Mechanical discipline (M5b guard):** if any helper references a cloner path, keep the reference mechanical (import path only); do not touch cloner internals.

*Rejected alt B1 — split into two modules (`profile_paths.py` resolvers + `clone_eviction.py` sweep/trash).* More "pure," but the resolvers and the sweep/copy helpers are tightly coupled (the sweep needs `clone_root_dir`, `clone_is_auto`; the copy needs `profile_ignore_names`), so a 2-way split creates a chatty cross-module dependency for zero isolation benefit — coupling where the domain doesn't demand it (modularity lens says don't). Rejected; revisit only if `clone_storage.py` itself grows unwieldy in Ph2.

*Rejected alt B2 — leave the functions in server.py, only move them below the tools (cosmetic reorder).* Keeps them one import unit with the tools — F-201's exact complaint (a break in storage code disables all tools; the 14 import-server tests still couple). Does not close the finding. Rejected.

### C. Component 2 — formalize the section_tool registry (F-101/F-505/F-612) + F-760/F-743/F-700

**CHOSEN — extract the registry mechanism into `embedded/tool_registry.py`, carrying M3's correlation wrapper verbatim; server.py imports `section_tool`, `SECTION_TOOLS`, `DISABLED_SECTIONS`, `is_section_enabled`, `apply_disabled_sections` from it.**
- `tool_registry.py` owns: `SECTION_TOOLS` (the section→names map), `DISABLED_SECTIONS`, `is_section_enabled`, `section_tool(section)` (the decorator, **with M3's correlation-id stamp preserved**), `apply_disabled_sections()`. It takes the `mcp` instance as a parameter or reads it via a small `register(mcp)` init, since the decorator calls `mcp.tool(func)` — **decision: pass `mcp` in** (the registry is constructed with a reference to the FastMCP app; server.py does `registry = ToolRegistry(mcp)` then `section_tool = registry.section_tool`). This keeps the module free of a server.py import cycle.
- **F-760 (verb taxonomy: `list_` vs `get_` etc. across 94 tools) — DECISION: caveat-now, rename-in-Ph2.** Define the ONE verb-prefix rule as documented convention **now** (in the module docstring of `tool_registry.py` + a note for M14): `list_*` = enumerate many; `get_*` = fetch one/detail; `create_*`/`spawn_*` = make; `execute_*`/`call_*` = run code; `extract_*`/`clone_*` = element capture; `set_*`/`modify_*`/`clear_*` = mutate; `discover_*`/`inspect_*` = introspect. **Do not rename tools in Ph1.** Rationale: a rename changes the tool COUNT-stable public surface and the M6 tripwire's names mid-pipeline; renames belong with Ph2's per-section move (where each section's tools are already being touched and re-registered). Recording the rule now makes Ph2 renames mechanical and gives M14 the canonical taxonomy.
- **F-743 (4+ exec-family tools with undifferentiated docstrings) — DECISION: caveat-now (docstring disambiguation), no rename.** In Ph1, tool bodies stay; the cheap, safe win is to **sharpen the docstrings** of the 6 exec-family tools (`execute_script`, `execute_cdp_command`, `inject_and_execute_script`, `execute_function_sequence`, `execute_python_in_browser`, `call_javascript_function`) so each states what it does and when to use it vs the others. This is a clarity-lens fix that does not change signatures or the count. (If the human prefers, defer even this to M14 — see open questions.)
- **F-700 (cli.py = second entry-point surface) — DECISION: reconcile by documentation, not by merge.** The two surfaces serve different roles (MCP tool surface for AI-driving vs ops CLI for the human operator) — merging them is wrong. The reconciliation is: (a) a docstring in `tool_registry.py` and a comment in `cli.py` stating the one-sentence "two surfaces, one backend" story (MCP tools drive the browser; `cli` inspects/operates the backend and never reimplements browser logic — cli.py's own docstring already says this); (b) hand the canonical statement to M14 for CONTRIBUTING/DESIGN. No code merge. Rationale: F-700 "extends F-109" and F-109's home is documentation; the conventions-lens win is *naming the one story*, not collapsing two legitimately-separate surfaces.
- **Rationale (lenses):** *Modularity* — the dispatch mechanism is a named, isolated module (M6 already treats it as a unit). *Deduplication* — `apply_disabled_sections`/`is_section_enabled`/`SECTION_TOOLS` live in one place. *Conventions* — one verb rule (documented), one registration path.

*Rejected alt C1 — replace `@section_tool` + FastMCP with a hand-rolled dispatch table.* Re-architecture; would drop FastMCP's schema generation and break the `.fn` seam M6 depends on. The brief says formalize, not replace. Rejected.

*Rejected alt C2 — rename tools now to enforce the verb taxonomy (rename-now for F-760/F-743).* Tempting (0 users, breaking-free), but it changes the M6 tool-count-94 tripwire's expected names mid-pipeline and creates a second batch of churn when Ph2 re-homes the same tools. Renaming twice (once now, once at the section move) is itself a conventions smell. Better to rename **once**, at Ph2, from the taxonomy recorded now. Rejected for Ph1; recommended for Ph2.

*Rejected alt C3 — keep the decorator in server.py, only add a docstring.* Leaves the mechanism co-located with 94 bodies; doesn't give Ph2 the clean seam. Half-measure. Rejected.

### D. Component 3 — ONE error envelope + `_require_tab` (F-104 + F-761 + F-746)

**CHOSEN — `embedded/tool_errors.py` defines the single envelope + tab/browser guards; apply to the quoted worst offenders + the instance-not-found cluster in Ph1; widen the rest in Ph2.**
- The envelope: **one** success/failure shape. Decision on shape: since ~40 tool bodies already `raise Exception(...)` and FastMCP surfaces raised exceptions to the MCP client as errors, the **canonical convention is "raise a typed error; helpers return values on success"** — NOT a `{"success": bool, ...}` dict on every tool (that would be a *second* way competing with the 40 raising sites, i.e. a conventions defect). So:
  - `tool_errors.py` defines `class ToolError(Exception)` (optionally with a `code`), and `InstanceNotFoundError(ToolError)`.
  - **`_require_tab(instance_id) -> Tab`** and **`_require_browser(instance_id) -> Browser`**: fetch from `browser_manager`, and on miss `raise InstanceNotFoundError(f"Instance not found: {instance_id}")`. This collapses the ~40 `if not tab: raise Exception(...)` and the ~7 `return {"success": False, "error": "Instance not found"}` and the 3 `return json.dumps({"error": ...})` into **one** call/one shape.
  - The **cdp-functions cluster** (the ~7 `{"success": False, "error": ...}` sites) is the one place returning dicts; **decision: convert those to raise `InstanceNotFoundError` via `_require_*` too**, so there is a single instance-not-found shape (F-761's core). The tools that genuinely need a structured result keep returning their result dict on success — the envelope governs the *error* path only.
- **F-746 (`get_instance_state` "Complete" lie):** fix the **contract**, not by forcing completeness. Change the docstring from "Complete state information" to the truthful "Full page state, or a partial record (`partial: True`) with `detail_error` if collection times out or fails." The partial-record shape already exists (:1501-1533) and is honest — the docstring is the defect. (Cheap clarity-lens fix; no behavior change.)
- **Ph1 depth (the explicit decision):** **envelope module + `_require_tab`/`_require_browser` + convert the 4 quoted F-104 archetypes + the full instance-not-found cluster (~50 sites, all mechanical: `if not tab: raise Exception(...)` → `tab = await _require_tab(instance_id)`).** The instance-not-found conversion is low-risk (identical behavior: still raises, still "Instance not found: {id}") and closes F-761 fully. The 4 F-104 archetypes (`execute_script` dict, `get_debug_lock_status` bare dict, `new_tab` re-wrap, cdp-functions dicts) are converted to the one convention. **Not** in Ph1: auditing all 22 except-blocks for envelope conformance — those without an instance-not-found or a quoted-archetype shape are widened in Ph2. Recorded as M14 debt.
- **Rationale (lenses):** *Conventions* — one instance-not-found shape, one error convention (raise), replacing 4 incompatible ones. *Deduplication* — `_require_tab` is the single guard (the brief's floated shape). *Clarity* — the `get_instance_state` docstring stops lying.

*Rejected alt D1 — full 22-site envelope sweep in Ph1.* Higher blast radius on the most-trafficked file, and many of the 22 are not instance-not-found or quoted archetypes (they're operation-specific catches whose right shape depends on the tool). Sweeping them now, before Ph2 characterizes each section, risks changing an error contract a caller relies on without a pinning test. The brief offers this as an option but flags the alternative (envelope + `_require_tab` + quoted offenders) as the disciplined Ph1 depth. Rejected for Ph1; it becomes M10b/Ph2 work.

*Rejected alt D2 — make every tool return a `{"success": bool, "result", "error"}` dict (dict-envelope everywhere).* This is the *other* candidate convention. Rejected because it would fight the ~40 existing `raise` sites and FastMCP's native exception surfacing — adopting it means rewriting 40 raising sites into returns AND changing what the MCP client sees for every tool, a far larger and riskier change than standardizing on "raise." Standardizing on the majority convention (raise) is the smaller, safer, more consistent move. Rejected.

*Rejected alt D3 — leave `get_instance_state` returning partials but rename it `get_instance_state_best_effort`.* A rename ripples to the M6 tripwire and callers; the docstring fix achieves the honesty with zero surface change. Rejected.

### E. Component 4 — spawn_browser seam (M13: F-208 + F-106)

**CHOSEN — extract `BrowserManager.spawn_browser` into a pipeline of small private methods, each independently unit-testable via the existing `.fn`/fake seam.**
- Decompose the :311-546 body along the confirmed phase boundaries into private methods on `BrowserManager`:
  - `_build_instance(options) -> BrowserInstance` (:321-328)
  - `_resolve_proxy(options) -> (proxy_config, proxy_forwarder, launch_proxy_server)` (:339-349)
  - `_resolve_launch_args(options, launch_proxy_server) -> (launch_args, browser_executable, browser_type)` (:351-397)
  - `_launch_browser(config) -> (browser, tab)` (:391-399)
  - `_apply_post_launch(browser, tab, instance, options, instance_id)` — tracking + headers + viewport + timezone (:401-441)
  - `spawn_browser` becomes a short orchestrator calling them in sequence under the existing try/except (cancel + error cleanup paths stay in the orchestrator).
- **`_resolve_profile_selection` (F-106)** already lives in server.py and is **already hermetically unit-tested** by `tests/test_profile_resolution.py` (M6 gap-filled it). In C1 it moves to `clone_storage.py`. Its ~10-branch retry/fallback structure gets the **same treatment optionally**: the branch logic is already a separate function (`_fallback_profile_selection`) — decision: **keep its current shape** (do not further decompose in Ph1), because M6's tests pin it and the win is small; just move it into `clone_storage` with a clear public name. Extend `test_profile_resolution.py` only if the move changes an import path a test asserts.
- **Rationale (lenses):** *Modularity* — the 236-line god method becomes 5 methods each understandable/testable in isolation (the M6/M13 seam). *Conventions* — one construction sequence, named phases. Every sub-method is `async` and takes explicit args (no hidden state beyond `self`), so M6/Ph2 can drive each with a fake.

*Rejected alt E1 — extract into free functions in a new `browser_spawn.py`.* They need `self` (the lock, `_instances`, `_spawn_diagnostics`, `_proxy_forwarders`, `process_cleanup` tracking). Free functions would take `self` as a param or duplicate state access — awkward and couplier than private methods. Rejected; private methods keep cohesion inside `BrowserManager`.

*Rejected alt E2 — leave spawn_browser monolithic, add only a test.* F-208/M13's whole point is the *seam* (untestable-without-integration is the defect); a test that drives the 236-line body end-to-end still can't isolate a phase. Doesn't close the finding. Rejected.

### F. Component 5 — F-164 server.py half (`_with_cdp_timeout` canonical)

**CHOSEN — make `_with_cdp_timeout` the single canonical CDP-timeout wrapper for every server.py call site; convert the raw `asyncio.wait_for(...)` copies to it.**
- The 5 raw `asyncio.wait_for(...)` sites in server.py: :1494 (`get_instance_state`), :2495, :2532 (plus the wrapper's own :149 which stays). Convert the tool-body ones (:1494, :2495, :2532) to `_with_cdp_timeout(...)` where the semantics match (a CDP coroutine with a timeout), OR — where they wrap a non-CDP coroutine (e.g. a state collection with its own env timeout) — leave them but note why (they are not CDP-hang guards). Decision: **audit each of the 3; convert those that are CDP-hang guards; leave + comment those that aren't.** M7 already fixed the `cdp_function_executor` half (tab-in-hand); this closes the server.py half so there is one wrapper name for "guard a CDP await."
- **Rationale (lenses):** *Deduplication* — one timeout-wrapper home (F-603's ×49 preamble collapses toward one helper). *Conventions* — one way to bound a CDP await.
- **Verify M7 didn't already claim this:** plan_M7 §"F-164" explicitly says its half is the `cdp_function_executor` tab-in-hand copies and re-homes the `server.py _with_cdp_timeout` half to M4 (confirmed in state.json: "F-164: split (cfe tab-in-hand half in M7; server.py `_with_cdp_timeout` half → M4)"). No double-ownership.

*Rejected alt F1 — introduce a NEW unified timeout decorator wrapping every tool.* A second mechanism competing with the existing `_with_cdp_timeout` — conventions defect, and a much bigger change. Rejected.

*Rejected alt F2 — leave the 3 raw `wait_for` sites as-is.* Leaves the F-164 server.py half open (the finding's split is only half-closed). Rejected.

---

## 3. Sequencing — independently verifiable steps (one checkpoint commit each)

**STEP 0 is first and mechanically verifiable.** Every step: `pytest -m "not integration"` green before and after; each is one commit. **Order dependencies noted.** Steps within a component that are order-free are marked.

- **STEP 0 — packageize `embedded/` + one import convention (F-701/F-604/F-613).** DEPENDENCY: first (everything imports).
  1. Write behavior-pinning check: a tiny test asserting `import stealth_chrome_devtools_mcp.embedded.server` (and a few siblings) succeeds and exposes known symbols — RED before the change if the package form isn't wired.
  2. Populate `embedded/__init__.py`; centralize the single sanctioned `sys.path` shim; rewrite **all** intra-embedded imports to the absolute package form; collapse `file_based_element_cloner.py`'s try/except dual-import to the one form; rewrite `cli.py`/`conftest.py`/the 5 test files' bootstraps.
  3. Green: **402 passed** (STEP 0 changes no behavior). Coverage unaffected.
  - *Valid stopping point:* STEP 0 alone is a shippable improvement (one import convention, real package).

- **C1 — extract `clone_storage.py` (F-201).** DEPENDENCY: after STEP 0 (uses the new import form). Order-free vs C2/C3/C4/C5 in principle, but do it **second** (largest LOC move; get it behind us).
  1. Write `tests/test_clone_storage.py`: pins that the extracted functions produce identical results (delegate identity) + that `clone_storage.resolve_profile_selection` is what `spawn_browser` calls. Reference `test_profile_resolution.py` (keep green).
  2. Move the 47 helpers + the 3 profile-selection functions into `clone_storage.py`; server.py imports and delegates; repoint cli.py's `server._clone_is_auto`/etc. → `clone_storage.*`; repoint `app_lifespan`'s `_spawn_background_sweep` call → `clone_storage.spawn_background_sweep`.
  3. Green: 402 + new tests. `test_profile_resolution.py` and M6's `test_bug_prone_tools` green.
  - *Valid stopping point:* STEP 0 + C1 is a valid landing (the brief names "extract the ~700 LOC eviction logic" as a Ph1 deliverable on its own).

- **C2 — formalize `tool_registry.py` (F-101/F-505/F-612; +F-760/F-743/F-700 caveats).** DEPENDENCY: after STEP 0. Order-free vs C1/C3/C5.
  1. Write `tests/test_tool_registry.py`: the decorator stamps M3's correlation id (assert the wrapper behavior is preserved), section membership, `apply_disabled_sections`, count-94 tripwire (mirrors M6). RED if the registry isn't yet a module.
  2. Extract the registry (carrying M3's correlation wrapper verbatim; construct with `mcp`); server.py imports from it; add the verb-taxonomy docstring (F-760) + sharpen the 6 exec-family docstrings (F-743) + the two-surfaces comment (F-700). Preserve `configure_logging("backend")` in `__main__`.
  3. Green: 402 + M6's `test_tool_dispatch` (94 tripwire) + new tests.

- **C3a — `tool_errors.py` + `_require_tab` + instance-not-found cluster + 4 archetypes (F-761/F-746 + the mechanical half of F-104).** DEPENDENCY: after STEP 0. Best **after C2** (fewer moving anchors in tool bodies) but order-free vs C1/C4/C5. **[A1] Split out of the former single C3 — this is the low-risk mechanical half.**
  1. Write `tests/test_tool_errors.py`: envelope shape, `_require_tab`/`_require_browser` raise `InstanceNotFoundError` on miss, return the handle on hit; one representative tool per shape converted. **[A1]** Also add the shape-group pinning tests (A1 §A1.2) that pin the NEW post-conversion behavior, RED-first.
  2. Add the module; convert the ~50 instance-not-found sites (mechanical) + the 4 F-104 archetypes; fix `get_instance_state` docstring. Carry M9's added params/notes at any network-tool envelope site.
  3. Green: 402 + M6 net (the `get_tab → raise "Instance not found" → delegate` adapter shape M6 pins per section must still hold — the message text is unchanged) + new tests.
  - *Valid stopping point:* STEP 0 + C1 + C2 + C3a is the plan's original core Ph1 triad (extract eviction + registry + the mechanical envelope); C3b can land in a later session.

- **[A1] C3b — full-sweep of the remaining operation-specific tool-body error handlers (the rest of F-104).** DEPENDENCY: **after C3a** (reuses `tool_errors.py`'s `ToolError`; C3a establishes the convention the group tests pin). Its own checkpoint commit, independently revertible.
  1. Extend `tests/test_tool_errors.py` with the remaining shape-group pins from A1 §A1.2 (one test per current-shape-group, pinning the post-conversion contract), RED-first where feasible.
  2. Convert every **CONVERT**-classified site in A1 §A1.3 to `raise ToolError(...)` (preserving message text); leave every **KEEP**-classified site (deliberate fallbacks, result-dict contracts, parse-guards that are the tool's input-validation contract) exactly as-is, each already justified in A1 §A1.3. Carry M9's params/notes on any network tool touched.
  3. Green: 402 + M6 net + the expanded `test_tool_errors.py`.

- **C4 — spawn_browser sub-method pipeline (M13/F-208).** DEPENDENCY: after STEP 0. Order-free vs C1/C2/C3/C5 (different file, browser_manager.py). Disjoint from M7's close_instance.
  1. Extend `tests/test_bug_prone_tools.py` (M6): assert `spawn_browser` still spawns via the seam AND that the new sub-methods are individually callable with fakes (the seam M13 exists to create).
  2. Extract `_build_instance`/`_resolve_proxy`/`_resolve_launch_args`/`_launch_browser`/`_apply_post_launch`; `spawn_browser` becomes the orchestrator. Cleanup paths stay in the orchestrator.
  3. Green: 402 + M6's spawn_browser pins.

- **C5 — `_with_cdp_timeout` canonical (F-164 server half).** DEPENDENCY: after STEP 0. Best **after C3a** (get_instance_state's :1494 wait_for is one candidate site and C3a already touches that function's docstring). Order-free otherwise.
  1. Extend `tests/test_cdp_timeout.py`: pin `_with_cdp_timeout` as the single wrapper; the converted sites behave identically.
  2. Convert the CDP-hang-guard `asyncio.wait_for` sites to `_with_cdp_timeout`; comment the non-CDP ones.
  3. Green: 402 + `test_cdp_timeout`.

**Order-free set:** {C1, C2, C4} are on largely disjoint regions/files and can be reordered. C3a reads best after C2; **[A1] C3b strictly after C3a**; C5 reads best after C3a. STEP 0 strictly first. **[A1] Recommended full order: STEP 0 → C1 → C2 → C3a → C3b → C4 → C5** (C3b immediately after C3a keeps the envelope work contiguous).

---

## 4. Breaking changes (visible to the single local operator)

0 external users — breaking is free. Deltas the maintainer will observe:

- **Import paths move (STEP 0):** intra-package imports become `from stealth_chrome_devtools_mcp.embedded.X import Y`. Any personal scratch script doing `from debug_logger import …` after adding `embedded/` to `sys.path` still works via the retained single shim, but the sanctioned form changes. Root scratch scripts (excluded) are left alone.
- **New modules:** `clone_storage.py`, `tool_registry.py`, `tool_errors.py` exist; `server.py` shrinks by ~982 LOC (storage) and delegates registry/errors.
- **cli.py internal references** to `server._clone_is_auto`/`server._master_profile_dir`/etc. now point at `clone_storage.*` (operator-invisible; same output).
- **Error shape unification (C3a):** instance-not-found is now uniformly a raised `InstanceNotFoundError` with message `Instance not found: {id}` (was: 4 shapes incl. 2 dict/JSON returns). A caller that string-matched on the old JSON `{"error": "Instance not found"}` from the 3 resource endpoints would see a raised error instead — acceptable (0 users; the MCP client sees an error either way). `get_debug_lock_status`/`new_tab` error shapes align to the one convention.
- **[A1] Full-sweep error-shape deltas (C3b) — every delta the sweep adds, per A1 §A1.3:**
  - `get_debug_lock_status` (:2558): error path was `return {"error": str(e)}` (no `success` key) → now `raise ToolError(str(e))`.
  - `new_tab` (:2662): was `raise Exception(f"Failed to create new tab: ...")` → `raise ToolError(...)` (typed; same message).
  - `spawn_browser` terminal (:1418): was `raise Exception(f"Failed to spawn browser: ...")` → `raise ToolError(...)`.
  - `validate_browser_environment_tool` (:3051): error path was a **rich diagnostic dict** `{"error", "platform_info", "is_ready": False, "issues", "warnings"}` → **[A1] KEEP** (this dict IS the tool's documented success/failure contract — a validator that always returns a report, never raises; converting it would delete the diagnostic payload). Justified keep.
  - `expand_children` (:3142/:3148) + `clone_element_to_file` (:3273): `return {"error": "Invalid ..."}` input-validation guards → **[A1] KEEP** (these are the tool's *input-validation* result contract for a bad `max_count`/`depth_range`/JSON arg, not an operational error report; the tool deliberately returns a value describing the bad input). Justified keep. *(Optional: A1 notes these could raise a `ToolValidationError` in Ph2 if the human later wants even input-validation unified; deferred, not part of the "one error-report convention" mandate.)*
  - `clone_element_complete` (:2952): `raise Exception("Invalid JSON in extraction_options: ...")` → `raise ToolError(...)` (already raises; typed).
  - `select_option` (:1815): `raise Exception("Invalid index value: ...")` → `raise ToolError(...)` (already raises; typed).
  - `execute_script` (:1944) + `create_python_binding` (:3857): `{"success": bool, "result", "error"}` result-envelope → **[A1] KEEP shape** (the SUCCESS path returns the same-shaped dict; this is a genuine result-carrying contract, not an error-only shape — the sweep does NOT make success/failure asymmetric). Documented as the named result-dict exception to the raise-convention. Justified keep.
  - `query_elements` (:1665): `except → debug_logger.log_error(...)` then **continues the loop** (per-element resilience) → **[A1] KEEP** (deliberate best-effort fallback, not an error report; matches the "don't break deliberate fallbacks" ruling).
  - `get_response_content` (:2150, `UnicodeDecodeError` → base64-encode) + `export_debug_logs` (:2544, timeout → guidance string) + `clear_debug_view` (:2500, timeout → `return False`) + `get_instance_state` (:1498/:1516, timeout/error → partial record) → **[A1] KEEP** (each is a deliberate fallback/alternate-representation, not an error report; `get_instance_state`'s partial is the F-746-blessed honest shape).
  - **Net:** the tool surface now has **one error-report convention** (`raise ToolError`/`InstanceNotFoundError`); the handful of result-dict / diagnostic-dict / deliberate-fallback contracts are explicitly named exceptions, each because the dict/value IS the contract (not an inconsistent error shape).
- **`get_instance_state` docstring** no longer claims "Complete" (behavior unchanged — it already returned partials).
- **Tool count:** unchanged at **94** (no renames in Ph1). Verb-taxonomy and exec-family docstrings sharpened (text only).
- **No entry-point change:** `stealth-chrome-devtools-mcp` and `stealth-chrome-devtools` still resolve (STEP 0 keeps their `[project.scripts]` targets).

---

## 5. Test strategy

- **M6 net stays green at every checkpoint** — `test_tool_dispatch.py` (94 tripwire, `.fn` seam, registry contract, per-section `get_tab → raise → delegate` adapter shape), `test_cloner_schemas.py` (two-tier goldens), `test_bug_prone_tools.py` (spawn_browser/`_resolve_profile_selection`/`list_instances`), `tests/fakes.py`, conftest fixtures.
- **Deliberate M6 updates (the only ones, each justified per the two-tier freeze):**
  - **conftest bootstrap rewrite (STEP 0):** the `sys.path.insert` lines change to the package-import form. This is a *harness* edit, not a golden/name edit; M6's fixtures and the `STEALTH_MCP_NO_AUTO_RECOVERY` setdefault are preserved verbatim. Justification: STEP 0's whole point is the import convention; conftest is one of the sanctioned import sites.
  - **`test_bug_prone_tools.py` extension (C4):** ADD sub-method seam assertions. Does not change existing spawn_browser pins. Justification: M13 creates the seam these new assertions exercise.
  - **No golden JSON changes.** The cloner goldens (`tests/goldens/`) are untouched (C1–C5 don't touch cloner internals). If C1's move of storage helpers somehow shifted a golden (it must not — storage ≠ cloner output), that would be a red flag to stop, not update.
  - **No tool-name changes** → the 94 tripwire's expected names are unchanged.
- **NEW behavior-pinning tests BEFORE each risky step:** `test_clone_storage.py` (C1), `test_tool_registry.py` (C2), `test_tool_errors.py` (C3a **+ C3b**), extensions to `test_bug_prone_tools.py` (C4) and `test_cdp_timeout.py` (C5). Each written RED-first where it pins new structure.
- **[A1] Shape-group pinning tests neutralize the D1 risk (the reason the full sweep was originally rejected — contract changes without pins).** `test_tool_errors.py` gains one test per *current-shape-group* pinning the **NEW post-conversion** behavior, written RED-first, enumerated in A1 §A1.2: (G1) instance-not-found → raises `InstanceNotFoundError` w/ exact message [C3a]; (G2) generic re-wrap (`spawn_browser`/`new_tab`) → raises `ToolError` w/ same message [C3a/C3b]; (G3) bare `{"error": str(e)}` (`get_debug_lock_status`) → raises `ToolError` [C3b]; (G4) input-validation raises (`select_option`/`clone_element_complete`) → raises `ToolError` [C3b]; (G5) result-envelope `{success,result,error}` (`execute_script`/`create_python_binding`) → **pinned UNCHANGED** (asserts the dict shape survives) [C3b guard]; (G6) diagnostic-dict (`validate_browser_environment_tool`) → **pinned UNCHANGED** [C3b guard]; (G7) deliberate-fallback (`query_elements` loop-continue, `get_response_content` base64, `get_instance_state` partial, `clear_debug_view`/`export_debug_logs` timeout) → **pinned UNCHANGED** [C3b guard]. The KEEP groups (G5–G7) get *characterization* pins so a future edit can't silently break a deliberate contract.
- **Expected test-count delta:** +4 new test files + extensions to 2 existing files; C3b adds ~7 shape-group tests on top of C3a's. Baseline **402 → ~432-450** (final count reported at execution). No deletions.
- **Coverage gate (CI `fail_under=39`):** extraction moves covered lines between files but does not reduce total covered lines; the new modules are exercised by the new tests. Net coverage **rises or holds** (the storage subsystem gains `test_clone_storage.py`; the registry gains `test_tool_registry.py`; errors gain `test_tool_errors.py`). No gate risk; if anything, ratchet-up candidate for M14 (not this plan).

---

## 6. Rollback + checkpoints

- **Per-step revert:** each step is one checkpoint commit with its tests; `git revert <step>` restores the prior green state. STEP 0 is behavior-preserving, so reverting it is clean.
- **Partial-landing validity (explicit):**
  - **STEP 0 alone** — valid, shippable (real package + one import convention). 
  - **STEP 0 + C1** — valid (the brief names eviction extraction as a standalone Ph1 deliverable).
  - **STEP 0 + C1 + C2** — valid (extraction + registry formalization).
  - **[A1] STEP 0 + C1 + C2 + C3a** — valid (the original core Ph1 triad: extract eviction + formalize registry + the mechanical envelope). C3b may land in a later session.
  - **[A1] + C3b** — completes the full 22-site sweep (F-104 CLOSED). **C3b is its own checkpoint commit and independently revertible:** `git revert <C3b>` drops only the operation-specific conversions and leaves C3a's `tool_errors.py` + instance-not-found unification intact (C3b adds no new module and touches only tool-body error lines + `test_tool_errors.py`).
  - **+ C4 (M13)** and **+ C5 (F-164)** are additive seams; landing without them still closes the Ph1 headline.
- **Stop rule:** any checkpoint that isn't 402-green (or M6-net-green) → stop, report, do not proceed (fix-branch discipline: deviation → stop).

---

## 7. Risk (this plan touches the most-trafficked file in the repo)

- **Blast radius:** server.py (94 tools import through it), browser_manager.py (every spawn), the import graph of all ~22 embedded modules (STEP 0). **[A1] The full-sweep (C3b) widens C3's blast radius from the ~50 mechanical instance-not-found sites to every tool-body error path (18 tool-body except handlers at HEAD, minus 2 M2-deleted = 16 in base; see A1 §A1.1).** Worst case: STEP 0 breaks an import spelling and the whole backend fails to load → caught immediately (suite won't collect). C1's delegation misses a helper reference → `NameError` at first storage sweep → caught by `test_clone_storage`/`test_profile_resolution`.
- **Worst case per component:** 
  - STEP 0 — a module that imports a sibling at *runtime* (function-local import, e.g. `browser_manager.py:652` `import nodriver.cdp.browser`, `:704 import os`, `cdp_function_executor.py:718 import py2js`) is missed; mitigate by grepping **all** `import`/`from` lines (done in §0) including function-local ones, and by the "402 green before/after" gate.
  - C3a — converting an instance-not-found site changes an error path a tool's own except block depended on; mitigate by keeping the message text identical and converting mechanically (`if not tab: raise Exception(...)` → `tab = await _require_tab(...)`), and by M6's per-section adapter-shape pin.
  - **[A1] C3b (the full sweep) — the elevated-risk step; blast radius = every tool-body error path.** The specific danger the human's mandate raises: **converting a deliberate fallback or a result-dict contract into a raise** (which would delete a diagnostic payload or make a tool's success/failure asymmetric). Mitigated by: (a) the KEEP list in A1 §A1.3 is explicit and pre-classified — C3b converts ONLY the CONVERT-tagged sites; (b) the G5/G6/G7 characterization pins (§5) assert the KEEP contracts survive, so an over-eager conversion turns a green suite red; (c) message text preserved on every CONVERT; (d) C3b is a separate revertible commit.
  - C4 — a spawn phase reordered changes launch behavior; mitigate by extracting **in place** (same statements, same order, just grouped into methods) and by M6's spawn_browser seam pins.
- **Early-warning signs:** suite collection error (STEP 0 import break); `test_profile_resolution`/`test_clone_storage` red (C1); the 94 tripwire red (C2 miscount, e.g. a tool accidentally dropped from a section); a golden diff (should never fire — signals accidental cloner touch); `test_cdp_timeout` red (C5 semantics drift); **[A1] a G5/G6/G7 characterization pin red (C3b broke a deliberate fallback or result-dict contract — STOP, that site belongs on the KEEP list); a tool that used to return a diagnostic dict now raising (e.g. `validate_browser_environment_tool` — signals a wrong conversion).**
- **Serial-order safety:** M5b (next) touches cloner internals; C1's mechanical cloner-path references must not pre-empt it. M14 (last) owns doc reconciliation; the taxonomy/two-surfaces notes are recorded FOR M14, not applied as M14's edits.

---

## 8. Findings closed / partially-closed / not-closed (one line each)

**Closed:**
- **F-701** (embedded/ not a real package) — STEP 0: real package + one absolute-import convention; the try/except dual-import defect collapsed.
- **F-604** (sys.path fragility) — STEP 0: single sanctioned sys.path shim; imports resolve as a package.
- **F-613** (import strategy inconsistency) — STEP 0: one import spelling everywhere.
- **F-201** (eviction logic embedded in server.py) — C1: 47-helper subsystem extracted to `clone_storage.py`; server.py delegates; cli.py stops depending on server.py internals for storage.
- **F-101 / F-505 / F-612** (server.py god object; registry indirection) — **partially** via C2 (registry formalized into `tool_registry.py`) + C1 (−982 LOC). The **full** per-section body split is **M4-Ph2** (deferred). Ph1 closes the "mechanism is un-isolated" half.
- **F-761** (4 instance-not-found shapes) — C3: unified to one raised `InstanceNotFoundError` via `_require_tab`/`_require_browser`.
- **F-104** (18 tool-body except blocks, ≥4 error shapes) — **[A1] MOVED to CLOSED via Amendment A1 (full sweep).** C3a converts the instance-not-found cluster + the 4 archetypes; C3b converts every remaining operation-specific error-report handler to the one `ToolError`/`InstanceNotFoundError` convention (deliberate fallbacks + result-dict contracts explicitly kept with justification — see A1 §A1.3). One error convention across the tool surface. Count reconciliation vs F-104's "22" in A1 §A1.1.
- **F-746** (`get_instance_state` "Complete" lie) — C3: docstring made truthful.
- **F-164** (server.py `_with_cdp_timeout` half) — C5: canonical CDP-timeout wrapper for server.py call sites (M7 closed the cfe half).
- **F-208** (spawn_browser god method) — C4/M13: extracted to a testable private-method pipeline.

**Partially closed:**
- **F-106** (`_resolve_profile_selection` mixes decision + side effects) — moved to `clone_storage.py` with a clear public name (C1); its retry/fallback shape is preserved (M6-pinned), not further decomposed in Ph1. Deeper decomposition is Ph2 debt if it proves painful.
- **F-101/F-505/F-612** — see above (registry half closed; body-split deferred).

**Explicitly NOT closed (recorded as known debt for M14):**
- **F-760** (verb taxonomy) — the ONE rule is *documented now*; **renames are Ph2** (done once, at the section move, from the recorded taxonomy). Debt: M14 codifies the rule; Ph2 applies renames.
- **F-743** (exec-family docstrings) — docstrings *sharpened now* (clarity); any rename is Ph2. (If the human defers even the docstring polish, it becomes M14's — see open questions.)
- **F-700 / F-109** (two entry-point surfaces) — reconciled by *documentation* (the "two surfaces, one backend" story), handed to M14 for CONTRIBUTING/DESIGN; no code merge (the surfaces are legitimately separate).
- **F-108** (tool-count 90/96/99 disagreement, `--list-sections` marketing counts) — **M14's**, untouched here.
- **[A1] M10b (full error envelope)** — **DELIVERED via Amendment A1**, not deferred/absorbed-later. The full tool-body error-shape sweep the brief filed under M10b is completed inside C3a+C3b. **For M14's debt ledger: the F-104 / M10b error-envelope debt line is REMOVED** (nothing left to widen at the tool layer; any future non-tool handler drift is ordinary maintenance, not tracked M10b debt).
- **F-603** (×49 timeout preamble) — C5 collapses the server.py `_with_cdp_timeout` duplication toward one helper; the broader preamble consolidation across modules is not Ph1. Debt.
- **M4-Ph2** (full per-section tool-body split) — deferred, gated on M6 breadth. The single largest remaining M4 item; Ph1 makes it mechanical.

---

## 9. Overlap warnings for the Stage-3 integrator

- **browser_manager.py vs M7:** C4 edits **only** `spawn_browser` (:311-546) and its new sub-methods; M7 edited `close_instance` (:605+) and `get_tab`/`get_browser` (:1060/:1082). **Disjoint by ~60 lines** — no collision. Re-anchor C4 by `async def spawn_browser(`.
- **server.py decorator region vs M3:** C2 moves the `section_tool` decorator that **M3 wrapped with the correlation stamp**. **Carry M3's wrapper verbatim.** Re-anchor by `def section_tool(` and preserve `configure_logging("backend")` in `__main__`.
- **server.py imports vs M11a/M15:** STEP 0 rewrites the import lines M11a (`env_utils`), M15 (`in_memory_storage`, `DEFAULT_PORT`) already changed. Re-anchor by the imported symbol name, not the line.
- **server.py network tools vs M9:** C3a/C3b's envelope must not clobber M9's `capture_bodies` param / capture-state notes if it touches `get_response_details`/`search_network_requests`/`export_network_data`/`set_network_capture_filters`. **[A1] Note:** none of these 4 network tools appears on the C3b CONVERT list (they have no operation-specific error-report except block at HEAD — verified in A1 §A1.1's enumeration), so C3b does not touch them; only the C3a instance-not-found guard applies where they call `get_tab`/`get_response`.
- **cli.py vs M8/M11a/M15/M1:** STEP 0 rewrites cli imports; C1 repoints cli's `server._*` storage calls to `clone_storage.*`. Leave M8's verbs, M11a's `recover_orphans`, M15's `DEFAULT_PORT`, M1's `_probe_backend_status` calls intact.
- **conftest vs M6/M11a:** STEP 0 rewrites the bootstrap; preserve M6 fixtures + M11a's `STEALTH_MCP_NO_AUTO_RECOVERY` setdefault.

---

## Amendment A1 — full 22-site envelope sweep (human-ordered at gate 2026-07-03)

**Status:** the human APPROVED plan_M4ph1 as-is at the 2026-07-03 gate, with two rulings: (1) F-760/F-743 renames stay caveat-now/rename-at-Ph2 (planner recommendation accepted); exec-family docstring sharpening confirmed inside C2. (2) **§2 C3 envelope depth OVERTURNED — the full 22-site sweep is now the mandate** (the planner's rejected-alt D1 becomes required). This amendment records the enumeration, the D1-risk mitigation (pinning tests before conversion), and the in-place deltas already marked `[A1]` in §3/§4/§5/§6/§7/§8/§9 above. Same amendment style as plan_M3 (A1: RLock) and plan_M8 (A1: port fallback).

### A1.1 Enumeration + count reconciliation vs F-104's "22" (verified at HEAD)

**Grep used (run in the sandbox against `src/stealth_chrome_devtools_mcp/embedded/server.py` at the pinned SHA):** a regex pass matching `^\s*except\b(.*):`, capturing each handler's body (deeper-indented lines) and its enclosing `def`, then tagging whether that `def` is `@section_tool`-decorated (scan the ≤3 lines above the `def` for `@section_tool`). Cross-checked with a `json.dumps({"error"` scan for the string-returning `@mcp.resource` endpoints.

**Raw counts at HEAD:**
- **57** total `except` clauses in server.py.
- **~30** of those live in the profile/clone-storage helpers (`:150`–`:997`) that **move to `clone_storage.py` in C1** (mostly `except OSError: pass/continue/return` filesystem-resilience guards) + pure parse guards (`parse_float_env`, `_profile_refresh_days`, `_is_relative_to`, etc.). **Not tool-body error contracts — out of the F-104 sweep** (they are deliberate filesystem/parse fallbacks; C1 relocates them verbatim).
- **4** in `app_lifespan` (`:1252/:1257/:1263/:1275`) — cleanup handlers that `debug_logger.log_error(...)` and continue teardown (**deliberate best-effort shutdown, KEEP**; M11a owns `app_lifespan`'s `activate()` addition).
- **1** in `apply_disabled_sections` (`:1226`) — `mcp.remove_tool` may-already-be-removed guard (**registry mechanism, KEEP**; C2 moves it to `tool_registry.py` verbatim).
- **3** string-returning `@mcp.resource` endpoints (`get_browser_state_resource:2406`, `get_cookies_resource:2424`, `get_console_resource:2456`) returning `json.dumps({"error": "Instance not found"})` — the F-761 variant (c). These are **resources, not `@section_tool` tools**, and they return **strings** by MCP contract. **[A1 decision] Convert the instance-not-found path to `raise InstanceNotFoundError` (C3a)** — FastMCP surfaces the raised error for resources too; this removes the 3rd instance-not-found shape. (Counted under C3a, not C3b.)
- **20** `except` clauses inside `@section_tool` bodies — the F-104 tool-body contract handlers. **2 of these are in M2-deleted tools** (`hot_reload:3008`, `reload_status:3037`), so the **base tree has 18** tool-body handlers.

**Reconciliation (the plan_M3-style delta):** F-104's headline says "**22** separate except-Exception/bare-except blocks … each hand-roll their own error shape." My re-grep finds **20 in `@section_tool` bodies at HEAD** (**18** post-M2), **plus** the **3** `@mcp.resource` string endpoints and the **1** `apply_disabled_sections` registry guard that F-104's prose also references as contract-bearing (it quotes `get_debug_lock_status`, `new_tab`, `hot_reload`, `reload_status`, and the resource shape). **20 tool-body + 3 resource ≈ 23; minus the `select_option`/`get_response_content`-style non-error handlers F-104 didn't count as "error shapes" lands at F-104's ≈22.** The exact integer is immaterial to the sweep's completeness: **the sweep's scope is defined by BEHAVIOR (every hand-rolled *error-report* contract), not by matching the number 22.** Recorded like plan_M3's "21 truly-silent vs F-181's 22": **my authoritative sweep set = the 18 post-M2 tool-body handlers + the 3 resource endpoints = 21 sites**, classified CONVERT/KEEP in §A1.3. (The 2 M2-deleted tools are gone; the `app_lifespan`×4 + `apply_disabled_sections`×1 are non-tool deliberate-mechanism handlers, kept as noted.)

### A1.2 Shape-group pinning tests (BEFORE conversion — neutralizes the D1 rejection reason)

The reason the full sweep was originally rejected (rejected-alt D1) was "contract changes without pinning tests." A1 removes that objection: `tests/test_tool_errors.py` gains **one test per current-shape-group**, each pinning the **post-conversion** contract, written **RED-first** where feasible. Enumerated:

| Group | Current shape (HEAD sites) | Post-conversion pin | Step |
|---|---|---|---|
| **G1** instance-not-found (raise) | ~40 `raise Exception("Instance not found: {id}")` | `_require_tab`/`_require_browser` → `raise InstanceNotFoundError`, exact message | C3a |
| **G1'** instance-not-found (json) | 3 `@mcp.resource` → `json.dumps({"error":"Instance not found"})` (:2406/:2424/:2456) | now `raise InstanceNotFoundError` | C3a |
| **G1''** instance-not-found (dict) | ~7 cdp-functions `return {"success":False,"error":"Instance not found: {id}"}` | now `raise InstanceNotFoundError` | C3a |
| **G2** generic re-wrap | `spawn_browser:1418`, `new_tab:2662` `raise Exception("Failed …")` | `raise ToolError(...)`, same message | C3a(new_tab/spawn terminal already touched) / C3b |
| **G3** bare error dict | `get_debug_lock_status:2558` `return {"error": str(e)}` | `raise ToolError(str(e))` | C3b |
| **G4** input-validation raise | `select_option:1815` `raise Exception("Invalid index …")`, `clone_element_complete:2952` `raise Exception("Invalid JSON …")` | `raise ToolError(...)`, same message | C3b |
| **G5** result-envelope error paths **[orchestrator touch-up: CONVERT — see A1.5]** | `execute_script:1944`, `create_python_binding:3857` (+ its `"No function found"` validation return) — error paths `{"success":False,…}` | error paths now `raise ToolError(...)`, message preserved; **success dicts pinned VERBATIM** (`{"success":True,"result",…}` / cfe result unchanged) | C3b |
| **G6** diagnostic dict (KEEP) | `validate_browser_environment_tool:3051` `{"error","platform_info","is_ready":False,"issues","warnings"}` | **UNCHANGED** — characterization pin | C3b guard |
| **G7** deliberate fallback (KEEP) | `query_elements:1665` (log+continue), `get_response_content:2150` (base64), `clear_debug_view:2500` (return False), `export_debug_logs:2544` (guidance str), `get_instance_state:1498/1516` (partial record), `expand_children:3142/3148` + `clone_element_to_file:3273` (input-validation value-return) | **UNCHANGED** — characterization pins | C3b guard |

G6–G7 are pinned as *characterization* tests so a later edit can't silently break a deliberate contract; G1–G5 are pinned to the new raised-error behavior (G5 additionally pins the two SUCCESS dict shapes verbatim).

### A1.3 Per-site classification (CONVERT vs KEEP-with-justification) — the 21-site sweep set

**CONVERT (→ `raise ToolError` / `InstanceNotFoundError`, message preserved):**
| Site @ HEAD | Current | Convert to | Step |
|---|---|---|---|
| ~40 instance-not-found `raise` (e.g. :1588, :1650, :2652, :3510) | `raise Exception("Instance not found: {id}")` | `_require_*` → `InstanceNotFoundError` | C3a |
| `get_browser_state_resource:2406`, `get_cookies_resource:2424`, `get_console_resource:2456` | `return json.dumps({"error":"Instance not found"})` | `raise InstanceNotFoundError` | C3a |
| ~7 cdp-functions dicts (:3588, :3726, :3747, :3770, :3815, :3845, :3878) | `return {"success":False,"error":"Instance not found: {id}"}` | `raise InstanceNotFoundError` | C3a |
| `spawn_browser:1418` | `raise Exception("Failed to spawn browser: …")` | `raise ToolError(...)` | C3a/C3b |
| `new_tab:2662` | `raise Exception("Failed to create new tab: …")` | `raise ToolError(...)` | C3a/C3b |
| `get_debug_lock_status:2558` | `return {"error": str(e)}` (no `success`) | `raise ToolError(str(e))` | C3b |
| `select_option:1815` | `raise Exception("Invalid index value: …")` | `raise ToolError(...)` | C3b |
| `clone_element_complete:2952` | `raise Exception("Invalid JSON in extraction_options: …")` | `raise ToolError(...)` | C3b |
| **[A1.5 touch-up]** `execute_script:1944` | `return {"success":False,"result":None,"error":str(e)}` | `raise ToolError(str(e))`; SUCCESS dict `{"success":True,"result",…}` verbatim (G5 pin) | C3b |
| **[A1.5 touch-up]** `create_python_binding:3857` (+ its `"No function found"` validation return) | `return {"success":False,"error":…}` | `raise ToolError(...)`; success path (cfe result pass-through) verbatim | C3b |

**KEEP (the dict/value/fallback IS the contract — converting would be a defect; each justified):**
| Site @ HEAD | Current | Why KEEP |
|---|---|---|
| `validate_browser_environment_tool:3051` | `{"error","platform_info","is_ready":False,"issues","warnings"}` | **Diagnostic contract:** a validator that ALWAYS returns a report (never raises) — the error dict carries `platform_info`/`issues`/`warnings` the caller consumes. Raising would delete the diagnostic payload. |
| `expand_children:3142`, `expand_children:3148`, `clone_element_to_file:3273` | `return {"error":"Invalid max_count/depth_range/JSON …"}` | **Input-validation result contract:** the tool deliberately returns a value describing a bad *argument* (not an operational failure). Out of scope of the "one *error-report* convention" mandate. (A1 notes a Ph2 option to unify these under a `ToolValidationError` if the human later wants even input-validation raised — deferred, flagged, not part of this sweep.) |
| `query_elements:1665` | `except → log_error(...); continue` | **Deliberate per-element resilience:** the loop continues so one bad element doesn't abort the whole query. Not an error report. |
| `get_response_content:2150` | `except UnicodeDecodeError → base64-encode` | **Alternate representation**, not an error — binary bodies are returned base64. |
| `clear_debug_view:2500` | `except asyncio.TimeoutError → return False` | **Boolean status fallback** (the tool's `-> bool` contract): timeout means "couldn't clear", a legitimate `False`, not an error envelope. |
| `export_debug_logs:2544` | `except asyncio.TimeoutError → return guidance str` | **`-> str` contract:** returns actionable guidance ("try smaller limits / gzip-pickle"); the string IS the tool's output type. |
| `get_instance_state:1498/1516` | `except → partial record {"partial":True,"detail_error":…}` | **F-746-blessed honest partial** — the whole point of the F-746 fix is that this partial shape is truthful. KEEP; only the docstring changes (C3a). |
| `spawn_browser:1381` (inner) | `except → accumulate + fallback + re-raise` | **Retry control-flow**, not an error report — accumulates attempt errors, tries `_fallback_profile_selection`, re-raises only when exhausted. |
| `app_lifespan:1252/1257/1263/1275` | `except → log_error; continue teardown` | **Best-effort shutdown** — cleanup must not abort on one failure. (M11a-owned region.) |
| `apply_disabled_sections:1226` | `except → continue` (tool already removed) | **Registry idempotence guard** (C2 moves it verbatim). |
| ~30 storage helpers (`:349`–`:997`, `except OSError: pass/continue/return`) | filesystem-resilience | **Deliberate FS fallbacks; relocated to `clone_storage.py` verbatim by C1** — not tool error contracts. |

**Summary [as touched-up in A1.5]:** of the 21-site sweep set, **~55 call-sites CONVERT** (the ~40 instance-not-found + 3 resources + ~7 cdp-dicts, all C3a; plus `get_debug_lock_status`, `new_tab`, `spawn_browser` terminal, `select_option`, `clone_element_complete`, `execute_script`, `create_python_binding` in C3a/C3b) and **the operation-specific KEEP set is 6 tool-body handlers** (`validate_browser_environment_tool`, `query_elements`, `get_response_content`, `clear_debug_view`, `export_debug_logs`, `get_instance_state`) **+ the 3 input-validation value-returns** (`expand_children`×2, `clone_element_to_file`) **+ the non-tool mechanism handlers** (`app_lifespan`×4, `apply_disabled_sections`, storage helpers). Every KEEP is justified above; the human mandate is "one convention," honored by making `raise ToolError` the single **error-report** convention while explicitly naming the diagnostic/fallback/input-validation contracts that legitimately return values.

### A1.4 Net effect on findings

- **F-104 → CLOSED** (was partially-closed): the full tool-body error-report sweep is done; the only dicts/values that remain are documented non-error contracts.
- **M10b → DELIVERED via A1** (not deferred/absorbed-later). **The F-104/M10b error-envelope debt line is REMOVED from M14's ledger** (§8 [A1]).
- No change to F-701/F-201/F-761/F-746/F-164/F-208/F-106/F-760/F-743/F-700 dispositions (§8).

### A1.5 Orchestrator touch-up (cross-review 2026-07-03): the G5 result-envelope pair moves KEEP → CONVERT

**Ruling:** `execute_script:1944` and `create_python_binding:3857` (plus the latter's `"No function found in Python code"` validation return, for intra-tool coherence) **CONVERT their error paths to `raise ToolError(...)`** with messages preserved; their **success-path shapes are untouched and pinned verbatim** (`execute_script` keeps `{"success": True, "result": …, "error": None}`; `create_python_binding` keeps its cfe result pass-through). The A1.2 G5 row, A1.3 tables, and summary above are already updated to reflect this; **this section supersedes any §4 [A1] line describing these two tools' error paths as unchanged.**

**Why the KEEP was overruled (three grounds):**
1. **The approved base plan already committed archetype (a) to conversion** — §2 D (human-approved as-is): "The 4 F-104 archetypes (`execute_script` dict, `get_debug_lock_status` bare dict, `new_tab` re-wrap, cdp-functions dicts) are converted to the one convention." A1's mandate was to go *further* than the base plan, never narrower; pulling `execute_script` back to KEEP regressed below the approved baseline.
2. **A1's own C3a made the KEEP incoherent:** C3a converts `create_python_binding`'s instance-not-found dict (:3845, one of the ~7 cdp-dict sites) to a raise while its except-block would keep returning `{"success": False}` — one tool, two error conventions, the exact cross-tool (here intra-tool) inconsistency F-104 names.
3. **Conventions lens:** post-sweep, every other tool reports errors by raising; a model consuming this server must not need per-tool knowledge of "check `.success` for these two." The envelope-symmetry argument optimizes intra-dict aesthetics over cross-surface uniformity; the vestigial `success: True` key on the happy path is harmless and explicitly pinned.

**Boundary note (unchanged from A1):** dicts produced *inside* `cdp_function_executor` and passed through by other cdp-functions tools are cfe-internal shapes (M7's file, out of this plan's scope) — server.py-level error reports are what this sweep unifies; any cfe-internal drift is ordinary maintenance, not tracked debt.
