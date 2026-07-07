# Stage 2 Plan — {M11a guarded ProcessCleanup init} + {M15 serialize BrowserInstance storage}

- **Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend`
- **Date:** 2026-07-03
- **Batch:** **{M11a, M15}** + routed lens findings **F-607, F-720, F-763** (→ M11a) and **F-722, F-762** (→ M15).
- **Base tree:** post-`plan_M3`(+A1) + `plan_M1` + `plan_M8`(+A1) + `plan_M2` + `plan_M7`. This batch executes **sixth**, serially, from M7's final commit.
- **Status:** **APPROVED** (human, 2026-07-03) — cleared for Stage 3. Decisions: **F-122 = rename** to `in_memory_storage` (durability rejected); **pid-file = relocate now** to `STATE_DIR/browser_pids.json` (M11a-3, one-time cutover). Cosmetic names (`env_utils.py`, `in_memory_storage`) accepted as the planner's defaults. Orchestrator verified the critical invariant in source: `app_lifespan` (server.py:1231) is wired as the HTTP lifespan (:1294), so `activate()` keeps a fresh backend reaping while import-time reaping is removed.
- **Context (pinned, not re-derived):** LOCAL single-user tool, **0 users, breaking changes are FREE.** Priorities: (1) maintainability (2) operability (3) performance. `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed**, coverage gate **39**. `uv run` is BROKEN — never used.
- **Lenses (ADDENDUM_LENSES.md):** modularity · deduplication (one canonical home) · clarity (self-describing names; renames legitimate) · conventions (one way per thing; a second way is a defect). This batch is heavy on **dedup + conventions + clarity** — `env_utils` consolidation, constant export, and the `persistent_storage` rename are direct lens closures.

> **Why M11a and M15 are batched:** both are small storage/singleton edits on **disjoint files** (M11a → `process_cleanup.py`, `conftest.py`; M15 → `persistent_storage.py`, `browser_manager.py`). They share no code. Each is written below as a clearly separated section so **each can be approved, implemented, and reverted independently.** The only cross-link is F-762 (one divergent state-root site, `process_cleanup.py:38`, lives in M11a's file though F-762 is routed to M15) — handled under M11a-3 and noted in both places.

---

## 1. Scope

### 1.1 M11a — files + confirmed anchors (re-opened at pinned SHA)

**Core finding IDs:** F-124, F-125, F-103 (import-time reap) · **routed:** F-607 (`_file_lock`), F-720 + F-763 (env parsing).

`src/stealth_chrome_devtools_mcp/embedded/process_cleanup.py` (1024 lines) — **primary edit target**

| Anchor @ SHA | What it is | M11a action |
|---|---|---|
| `class ProcessCleanup:25` | `"""Manage tracked browser process cleanup and orphan profile recovery."""` | — |
| `__init__:31-55` | state (`pid_file:38`, `tracked_pids:39`, `browser_processes:40`, `orphan_profile_max_age_seconds:41`, `_init_time:47`); **env guard `:52-53`** `os.getenv("STEALTH_MCP_NO_AUTO_RECOVERY","").strip().lower() in {"1","true","yes","on"}` → early-return; else `_setup_cleanup_handlers():54` + `_recover_orphaned_processes():55` | **REFACTOR** (make side-effect-free; move handlers+recovery behind an explicit `activate()`) |
| `_parse_nonnegative_int_env:57-78` (`@staticmethod`, 2-arg) | int-env parse, `minimum` hardcoded `0`; called at `:41` | **DELETE** → import from `env_utils` (F-720) |
| `_file_lock:214-234` | non-blocking lock; **`except (OSError, IOError): yield` at `:224-225`** — yields *as if acquired* (**F-607**); used by `_load_tracked_pids:247` + `_save_tracked_pids:271` | **HARDEN** (do not yield on acquire-failure) |
| `_load_tracked_pids:236-254` / `_save_tracked_pids:258-282` | both wrapped in a broad `try/except` that **logs a warning and degrades** (`return {}` / skip) | reuse — already tolerant of a raising lock |
| `pid_file:38` | `Path(os.path.expanduser("~/.stealth_browser_pids.json"))` — bare `$HOME` dotfile, underscore-named, **not** under `STATE_DIR` (**F-762**, 3rd divergent site) | **RELOCATE under `STATE_DIR`** *(decision — §2)* |
| `_recover_orphaned_processes:599` | `_load_tracked_pids` → `_kill_processes_for_metadata(recovery=True)` per entry → `_clear_pid_file` → `_sweep_orphaned_temp_profiles`. **The create_time + `--user-data-dir` matcher REPORT §"preserve" protects.** | **PRESERVE** matcher; add a public `recover_orphans()` wrapper |
| `process_cleanup = ProcessCleanup():1023` | module singleton | keep construction; it is now **side-effect-free** |

`tests/conftest.py` (102 lines) — sets `STEALTH_MCP_CLONE_OUTPUT_DIR` via `os.environ.setdefault:28-31`; **never** sets `STEALTH_MCP_NO_AUTO_RECOVERY` (**F-124** test gap). **ADD** an `os.environ.setdefault("STEALTH_MCP_NO_AUTO_RECOVERY","1")` beside `:28-31`.

**Also touched by M11a** (small, symbol-anchored):
- `embedded/server.py` — `parse_bool_env:54-58` (`{1,true,yes,on,enabled}`) + `parse_float_env:61-68` → **DELETE**, import from `env_utils` (F-720); `app_lifespan:1231` startup (before `_spawn_background_sweep("startup"):1246` / `yield:1247`) → **ADD** `process_cleanup.activate()` (the fresh-backend reap trigger).
- `embedded/browser_manager.py` — `_parse_nonnegative_int_env:31-56` (3-arg, `minimum=0`) → **DELETE**, import from `env_utils`; call sites `:71,:75` re-point.
- `cli.py` — repoint M8's `kill-orphans` from the direct `process_cleanup._recover_orphaned_processes()` call → `process_cleanup.recover_orphans()`.

`src/stealth_chrome_devtools_mcp/embedded/env_utils.py` — **NEW FILE** (F-720 canonical home): `parse_nonnegative_int_env(name, default, minimum=0)`, `parse_bool_env(name, default=False)`, `parse_float_env(name, default)`.

### 1.2 M15 — files + confirmed anchors (re-opened at pinned SHA)

**Core finding IDs:** F-207 (silent field-drop), F-122 (misnomer) · **routed:** F-722 + F-762 (constants / state-root).

`src/stealth_chrome_devtools_mcp/embedded/persistent_storage.py` (95 lines) — **primary edit target → RENAME**

| Anchor @ SHA | What it is | M15 action |
|---|---|---|
| `class InMemoryStorage:4` | thread-safe dict behind an `RLock` (**already honestly named**) | keep class name |
| `store_instance:16-34` | re-projects the caller's dict into a **hardcoded 6-field** dict (`instance_id/state/created_at/current_url/title/tabs`), dropping everything else (**F-207**) | **REPLACE** with store-as-passed |
| `get_instance:46` / `list_instances:56` / `clear_all:65` / `get:74` / `set:85` | in-memory accessors | unchanged |
| `persistent_storage = InMemoryStorage():95` | module singleton; name **lies** (in-memory, wiped on shutdown) (**F-122**) | **RENAME** module → `in_memory_storage.py`, singleton → `in_memory_storage` |

`src/stealth_chrome_devtools_mcp/embedded/browser_manager.py` — `store_instance` **call site `:480-485`** hand-builds `{state, created_at, current_url, title}` from `instance` (a `BrowserInstance`) + live `tab` (**F-207**). `BrowserInstance` constructed at `:323`. Import `:17` `from persistent_storage import persistent_storage`. **EDIT** call site to serialize the model; **rename** the import.

`src/stealth_chrome_devtools_mcp/embedded/models.py` (204 lines) — `BrowserInstance(BaseModel):18-32`, **pydantic v2.11.7** (`pyproject` pinned). Fields: `instance_id, state (BrowserState str-Enum), current_url, title, created_at, last_activity, headless, user_agent, viewport`; `update_activity:30`. **NO EDIT** (serialize via `.model_dump(mode="json")` at the call site).

**F-722 / F-762 constant sites:**
- `embedded/singleton.py:34` `STATE_DIR = Path.home()/".stealth-mcp"`, `:41 DEFAULT_PORT = 19222` — **canonical homes. IMPORT source only — NO EDIT.**
- `server.py:23` (top-level) `--singleton-port` `default=19222` → import `DEFAULT_PORT`.
- `cli.py:258` `--port` `default=19222` (literal appears **twice** — default + help text) → import `DEFAULT_PORT`, fix help.
- `embedded/response_handler.py:22-25` `Path.home()/".stealth-mcp"/"element_clones"` (env `STEALTH_MCP_CLONE_OUTPUT_DIR:22`) → `STATE_DIR/"element_clones"` (F-762 pure re-type, zero behavior change).
- `embedded/server.py:225-231` `_default_session_root` — divergent root (`C:\stealth-mcp-browser-sessions` on Windows / `~/.stealth-mcp-browser-sessions` POSIX), **NOT** under `STATE_DIR` (F-762) → **DEFER relocation** *(decision — §2)*.

**Rename blast radius (enumerated):** importers `browser_manager.py:17`, `progressive_element_cloner.py:14`, `server.py:44`; usages `browser_manager` (180/480/755/776), `progressive_element_cloner` (25/28), `server` (1266/1273/1430); the diagnostics string `server.py:3028` `'persistent_storage'`; tests `test_exception_handling.py` (28 import, 206-226 usage, 346/363 `patch("browser_manager.persistent_storage")`), `stress_memory_leak.py` (29/96-99/214-215, a `stress_*` script — **not** pytest-collected, still fixed).

### 1.3 Anchors that predecessors SHIFT (re-anchor by SYMBOL in Stage 3)

Base = post-M3(+A1) + M1 + M8(+A1) + M2 + M7. Effect on my anchors, by file:

- **`process_cleanup.py` — my anchors are ABOVE M7's region → line-stable; two symbols below it shift.** M7 (`plan_M7` step M7-2) adds `_fallback_pid_identity_ok` and hardens the non-recovery branch **inside `_kill_processes_for_metadata:348-413`** (F-608). My edits — `__init__:31-55`, `_file_lock:214-234`, the env-parse removal `:57-78`, `pid_file:38` — all sit **above `:348`** and are **unshifted**. Only `_recover_orphaned_processes:599` and the module singleton `:1023` sit **below** M7's insertion → re-anchor by `def _recover_orphaned_processes(` and `^process_cleanup = ProcessCleanup()`. **M8 does NOT edit this file** (`plan_M8` §1.2: "thin-trigger only — avoids overlap with M11a"). **`plan_M7` §1.3 confirms:** "M11a refactors process_cleanup's init/guard + `_file_lock` (:216-225) + adds a public `recover_orphans()` seam … disjoint from `_kill_processes_for_metadata` (:348-413). M11a rebases over my F-608 edit."
- **`browser_manager.py` — my anchors are UNSHIFTED.** The only predecessors that touch it (M3's M10a-7a logs in `switch_to_tab:~1165` / `close_tab:~1207`; M7's `close_instance:605-785`) sit **below** my `:17` import, `:31-56` parse def, `:71/:75` calls, `:323` construction, and `:480` store call. Re-anchor `store_instance` by the `persistent_storage.store_instance(instance_id, {` substring.
- **`embedded/server.py` — re-anchor EVERYTHING by symbol** (M3 inserts the `section_tool` wrapper `:1212-1217` + M10a logs; **M2 deletes `hot_reload`/`reload_status` `~:2974-3038`**, shifting everything below up ≈ −66). My anchors: import block `:40-48` and `parse_bool_env/parse_float_env:54-68` sit **above** all inserts → near-stable (anchor by `def parse_bool_env` / the `from persistent_storage import` line). `app_lifespan:1231` → anchor by `async def app_lifespan(` + `_spawn_background_sweep("startup")` + the bare `yield`. `list_instances:1421` → `async def list_instances(`. **`modules_to_check:3020-3032` (holds the `'persistent_storage'` string) falls inside M2's `~:2974-3038` deletion window — VERIFY at Stage 3 whether it survives; if present, update the string; if M2 deleted it, no action.**
- **`singleton.py` — I only IMPORT `STATE_DIR:34` / `DEFAULT_PORT:41`; no edit.** M2 adds a `SOURCE_ROOT` sibling beside them (`plan_M2` §1.1 `:23`) and M8 extracts `_terminate_backend` — neither changes the constant *names*, so an import-by-name is shift-proof.
- **`cli.py` — M8 already added the `kill-orphans` verb + `_cmd_stop/_cmd_restart` + dispatch/subparsers, and left the `:258` port literal to me** (`state.json:105`: "2 literals left to M15"). Re-anchor my two edits by `_recover_orphaned_processes(` (inside M8's `_cmd_kill_orphans`, → `recover_orphans(`) and `serve.add_argument("--port"`.
- **`response_handler.py`, `models.py`, `conftest.py`, `persistent_storage.py`** — no predecessor edits them; anchors are HEAD-accurate.

### 1.4 Explicit out-of-scope (stated)

- **M7's F-608 region** (`_kill_processes_for_metadata:348-413`) — coexist, do not touch.
- **The orphan-MATCHING logic** (`--user-data-dir` + `create_time`, REPORT §"preserve") — PRESERVE exactly. M11a changes **WHEN** recovery runs, never **HOW** it matches.
- **M11b — the full DI/reset seam for all 15 singletons** (F-125's general form) — **DEFERRED** (TRIAGE: "DEFER, couple with M4 Ph2"). M11a is the **cheap guarded-init + conftest opt-out**, not a DI framework.
- **`_default_session_root` relocation** (`embedded/server.py:225-231`) — DEFERRED (high on-disk migration harm; overlaps M4). See §2 / §7.
- **M4** (server.py decomposition — but I do make two small symbol-anchored server.py edits M4 must rebase over), **M9, M6, M5, M14** — not touched.
- No drive-by refactors; any discovery → a new finding, not a silent edit.

---

## 2. Approach + rejected alternatives

### M11a-A · Guarded init (F-124/F-125/F-103) — **RECOMMENDED: side-effect-free `__init__` + explicit `activate()` at the backend-startup boundary**

`__init__` keeps only pure state (incl. `_init_time`). The two side effects it does today (`_setup_cleanup_handlers()`, `_recover_orphaned_processes()`) move behind a public `activate()`:

```
def activate(self):                       # called once, at backend serve startup
    if parse_bool_env("STEALTH_MCP_NO_AUTO_RECOVERY"):   # F-763 canonical parser
        return
    self._setup_cleanup_handlers()
    self._recover_orphaned_processes()

def recover_orphans(self):                # public seam for CLI kill-orphans
    self._recover_orphaned_processes()
```

`embedded/server.py` `app_lifespan` startup gains one line — `process_cleanup.activate()` — before `_spawn_background_sweep("startup")`. **Confirmed viable:** the HTTP backend runs `app_lifespan` (`embedded/server.py:1231`, `lifespan=app_lifespan:1294`), so a fresh backend still installs teardown handlers and reaps orphans — the invariant the brief requires. Mere `import` (tests, scripts, the ops CLI's read-only commands, `python -m … --transport stdio` proxy) now does **zero** reaping, with no per-caller opt-out needed.

- **Rejected — env-guard-only + conftest (the minimal reading):** keep recovery in `__init__` gated by the env var, and only add the `setdefault` to `conftest`. Simpler and it edits neither `server.py` nor `__init__`'s body — but it leaves the F-124 footgun for **every non-test importer** ("runs for real on the mere act of importing … or transitively via `import browser_manager`"), and it is exactly the **config-knob opt-out workaround** the maintainer's standing preference rejects (ship every-user-correct defaults, not per-caller flags). The env var survives in the recommended design too, but only to *disable* an explicit boundary call — not as the thing standing between `import` and a process kill.
- **Rejected — lazy singleton via PEP-562 `__getattr__`:** `from process_cleanup import process_cleanup` resolves the name at the importer's import time, so `browser_manager`/`server` would still force construction+recovery immediately. Laziness buys nothing here and adds indirection (clarity cost).
- **Rejected — full DI/reset seam (factory + injectable holders for all 15 singletons):** that is **M11b**, explicitly deferred; over-scoped for a cheap fix.
- **`conftest` opt-out is added regardless** (belt-and-suspenders + it documents intent + mirrors the existing `STEALTH_MCP_CLONE_OUTPUT_DIR` `setdefault` convention). Under the recommended design it also makes `activate()` a guaranteed no-op if any test exercises the lifespan.

*Lens:* **conventions** — recovery gets **one** trigger home (the serve boundary) instead of being entangled with import; **clarity** — `__init__` name no longer hides a process-killing side effect (its own docstring today admits "run startup orphan recovery").

### M11a-B · `env_utils.py` home (F-720) + guard parser (F-763)

One new `embedded/env_utils.py` holds the whole "typed env var with fallback" family: `parse_nonnegative_int_env(name, default, minimum=0)` (the **3-arg superset** — `browser_manager`'s 2-arg calls pass `minimum=0` implicitly, identical behavior), `parse_bool_env`, `parse_float_env`. `browser_manager`, `process_cleanup`, and `server` import from it and delete their local copies. The `process_cleanup` guard (F-763) routes through `parse_bool_env`, so `STEALTH_MCP_NO_AUTO_RECOVERY=enabled` — silently ignored today because the inline set `{"1","true","yes","on"}` omits `"enabled"` — now works.

- **Rejected — put the helpers in an existing module** (`platform_utils`, or leave `parse_bool/float` in `server.py` and import from there): `server.py` is about to be decomposed by M4 and importing env helpers *from* the 4208-line god module is precisely the coupling the modularity lens flags. A dedicated leaf module (no embedded imports → no cycle risk) is the one canonical home.
- **Rejected — keep two `_parse_nonnegative_int_env` copies "because they're tiny":** byte-identical duplicated logic under an identical name across two files is the textbook deduplication finding (F-720). One home.
- **Name:** `env_utils.py` per LENS_DELTA/state.json routing. (Open question if the human prefers another; the plan is name-agnostic.)

### M11a-C · `_file_lock` (F-607)

Change the acquire-failure branch so it **does not yield as if the lock were held**. Preferred shape: a **bounded blocking acquire** (retry the non-blocking lock a few times with a short sleep; on final failure `raise`). Both callers (`_load_tracked_pids`, `_save_tracked_pids`) already sit inside a broad `try/except` that logs a warning and degrades (`return {}` / skip the write), so a raise converts a **silent race into a logged, safe skip** — strictly better on the operability lens.

- **Rejected — switch to a fully blocking lock (`LK_LOCK` / `flock` without `LOCK_NB`):** an indefinitely-held lock by a wedged peer would hang the tracking path — the opposite failure. Bounded retry keeps the bound.
- **Rejected — signal failure via a sentinel/return value instead of raising:** the contextmanager shape can't return a value to a `with` cleanly without changing both call sites; raising is the idiomatic "couldn't acquire" signal and needs no caller change (they already catch).
- **Out of cheap scope (noted, not fixed):** F-607 also notes load and save are two separate calls, so a load-modify-save is not atomic across processes. A single lock spanning read-modify-write is a larger change; flagged as a residual, not attempted in this batch.

### M15-A · Serialize the model (F-207) — **`instance.model_dump(mode="json")`**

At `browser_manager.py:480`, update the model's live fields, then serialize the **whole** `BrowserInstance` and pass the plain dict to storage:

```
instance.current_url = getattr(tab, "url", "") or instance.current_url
instance.update_activity()
persistent_storage.store_instance(instance_id, instance.model_dump(mode="json"))
```

`store_instance` stops re-projecting and **stores the dict as passed** (shallow copy; `instance_id` is already a model field). `mode="json"` yields JSON-native types (datetime→ISO string, `BrowserState`→its value) — matching the old code's `created_at.isoformat()` and future-proofing any later on-disk dump. All fields the finding names as dropped (`headless`, `user_agent`, `viewport`, `last_activity`) now survive. **Readers unaffected:** `list_instances` (`server.py:1441-1446`) reads only `instance_id/state/current_url/title`, all still present; the shutdown cross-check (`server.py:1266`) reads only `"instances"`. The old `tabs: []` key is **verified unused** by any reader and dropped.

- **Rejected — pass the `BrowserInstance` object into `store_instance` and let it call `model_dump`:** couples the generic storage module to the `models` module (modularity lens). Keep serialization at the caller; storage stays model-agnostic (it also holds progressive-cloner KV via `get/set`).
- **Rejected — custom `to_dict()` on the model / `pickle`:** pydantic v2 already provides `model_dump`; a hand-rolled serializer is a second way to do a solved thing (conventions), and `pickle` is non-portable and unreadable.
- **Non-serializable-field risk:** none in `BrowserInstance` — every field is a scalar/enum/str/dict; the live browser handle and lock live in `BrowserManager`'s separate tracking, **not** in the model. (§7.)

### M15-B · The misnomer (F-122) — **RENAME, do not make durable** — ✅ APPROVED (human, 2026-07-03: rename)

Rename module `persistent_storage.py` → `in_memory_storage.py`, singleton `persistent_storage` → `in_memory_storage` (class `InMemoryStorage` already honest). Update the enumerated importers/usages/string/tests (§1.2).

- **Rejected — make it actually durable (write-through to disk + restore on init):** the store is **deliberately** process-scoped — `app_lifespan` shutdown calls `clear_all()` on every graceful exit (`server.py:1273`, logging "Clearing in-memory storage") and it is only ever a **secondary cross-check** in `list_instances`, never a source of truth (F-122 quote). Durability would contradict that intent and risk **resurrecting stale instances that point at dead browsers** — a behavior change needing M6 characterization, for **zero** benefit at 0 users. Renaming closes the clarity finding with no behavioral risk. (Durability, if ever wanted, is a separate feature item.)
- **New name:** `in_memory_storage` proposed (mirrors the honest class name). Open question if the human prefers `instance_store` / `memory_store`.

### M15-C · Export the constants (F-722) + one state-root convention (F-762)

Import `DEFAULT_PORT`/`STATE_DIR` from `singleton` at the three pure re-type sites (`server.py:23`, `cli.py:258`, `response_handler.py:25`). No behavior change — same values, one definition. Importing `singleton` is **side-effect-free** (it constructs no singletons and imports no `process_cleanup`/`browser_manager`), so this never triggers a reap.

- **Import-order handling:** `response_handler.py` (embedded) → flat `from singleton import STATE_DIR`. `cli.py` `build_parser` → call `_ensure_embedded_on_path()` then `from singleton import DEFAULT_PORT` (already the file's idiom). Top-level `server.py:23` → it already inserts `EMBEDDED_DIR` on `sys.path` at `:16-18` **before** building the parser, so `from singleton import DEFAULT_PORT` is reachable there; the argparse default becomes `DEFAULT_PORT`.
- **F-762 (conventions):** `response_handler` now nests under `STATE_DIR` via the import (the site that was already logically there, just re-typed). The **`process_cleanup.py:38` pid file** (M11a's file) is relocated under `STATE_DIR` in **M11a-3** (see decision below). The **`_default_session_root` divergent root is DEFERRED** — relocating it silently orphans the developer's existing cloned/named profiles (potentially large, user-relied-upon) and ripples into every profile helper + `cli` + `conftest`, and it lives in the server.py M4 is about to decompose. Recommended as its own migration-aware item; flagged residual in §8.
- **Rejected — move `DEFAULT_PORT`/`STATE_DIR` into a new `constants.py`:** the brief pins `singleton` as the owner ("export … don't move singleton's runtime logic"); an import-by-name closes the drift without moving anything.

### Decision — pid-file relocation (F-762, `process_cleanup.py:38`) — ✅ APPROVED: RELOCATE (human, 2026-07-03)

**RECOMMENDED: relocate** `~/.stealth_browser_pids.json` → `STATE_DIR / "browser_pids.json"` inside the M11a init refactor (M11a owns this file; the underscore-named bare-`$HOME` dotfile is the exact convention violation F-762 names). **One-time cutover cost:** the pre-existing old-path file is ignored once — low harm, because PID tracking is ephemeral (a fresh backend re-tracks its own children) and `recover_orphans` is best-effort. Pinned by a test asserting the new path. **Alternative (safer, weaker):** leave the path, file a follow-up — rejected because it leaves a Low-severity convention violation in a file already open on the bench, against the maintainer's universal-fix preference. **This relocation is the only M11a behavior change with an on-disk footprint; it is called out in §4 and §7.**

---

## 3. Sequencing (independently verifiable steps · one checkpoint commit each)

> Baseline before starting (green on the post-M7 tree): `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed**, coverage ≥ **39**. **Write the pinning test in each step BEFORE the change; run the full suite after every step.**

**M11a-1 — `env_utils` consolidation (F-720, F-763).** Create `embedded/env_utils.py`. Repoint `browser_manager` (delete `:31-56`, import; fix `:71/:75`), `process_cleanup` (delete `:57-78`, import; route the `:52` guard through `parse_bool_env`), `server` (delete `:54-68`, import).
`… pytest tests/test_env_utils.py tests/test_process_cleanup.py -q`

**M11a-2 — guarded init + conftest (F-124/F-125/F-103).** Make `__init__` side-effect-free; add `activate()` + `recover_orphans()`; wire `process_cleanup.activate()` into `app_lifespan` startup; add the `conftest` `setdefault`.
`… pytest tests/test_process_cleanup_import_guard.py tests/test_server_entrypoint.py -q`

**M11a-3 — `_file_lock` (F-607) + `kill-orphans` repoint + pid-file relocation (F-762).** Harden the lock; repoint `cli` `kill-orphans` → `recover_orphans()`; move the pid file under `STATE_DIR` *(if approved)*.
`… pytest tests/test_process_cleanup.py tests/test_cli.py -q`

**M15-1 — serialize the model (F-207).** Edit `browser_manager:480` (`model_dump(mode="json")`) + `store_instance` (store-as-passed). Update `test_exception_handling.py:206-226` to the new contract (pinned first).
`… pytest tests/test_exception_handling.py -q`

**M15-2 — rename `persistent_storage` → `in_memory_storage` (F-122).** Rename file + singleton; update importers (`browser_manager:17`, `progressive_element_cloner:14`, `server:44`), the `server:3028` string *(if it survives M2)*, `test_exception_handling` import + patch targets, `stress_memory_leak`.
`… pytest -m "not integration" -q` (full — rename touches many modules)

**M15-3 — export constants (F-722) + response_handler root (F-762).** Import `DEFAULT_PORT` at `server.py:23` + `cli.py:258` (+ help text); import `STATE_DIR` at `response_handler.py:25`.
`… pytest tests/test_cli.py tests/test_response_handler.py tests/test_server_entrypoint.py -q`

Steps are ordered by dependency (M11a-1 provides the parser M11a-2's guard needs; M15-1 precedes the M15-2 rename so the contract test moves with the file). M11a-1/2/3 and M15-1/2/3 are otherwise independent and independently revertible.

---

## 4. Breaking changes

**0 users — no external contract exists; all "breaks" are internal and free.** Behavioral notes:

- **Bare `import` no longer reaps** (M11a-2, recommended design) — intentional; the fresh backend still reaps via `app_lifespan.activate()` (pinned by test). No workflow that *needs* import-time reaping exists (the CLI and proxy explicitly do **not** want it).
- **`store_instance` contract change** (M15-1) — stores the full serialized model instead of a 6-field subset; readers get *more* keys, the keys they read are unchanged. `test_exception_handling.py` re-pinned.
- **`persistent_storage` → `in_memory_storage` rename** (M15-2) — breaks only the 3 internal importers + tests (all updated). No external importer exists.
- **pid-file relocation** (M11a-3, *if approved*) — one-time cutover on this dev machine (old `~/.stealth_browser_pids.json` ignored once). The only change with an on-disk footprint.
- **M8 coordination:** M8's `_server()` `os.environ.setdefault("STEALTH_MCP_NO_AUTO_RECOVERY","1"):35` becomes **redundant-but-harmless** for import under the recommended design (import is side-effect-free regardless); `kill-orphans` still explicitly triggers recovery via `recover_orphans()`. No M8 revert needed.

---

## 5. Test strategy (pin BEFORE the change)

**M11a**
- **F-124 (the footgun):** assert constructing `ProcessCleanup()` (recommended design: with **no** env var) does **not** call `_setup_cleanup_handlers`/`_recover_orphaned_processes` — spy/monkeypatch and assert zero calls on bare construction/import. This is the exact "no process kill on bare import" pin the brief demands.
- **Fresh-backend reap invariant (critical):** assert `activate()` **runs** recovery when the env is unset and is a **no-op** when `STEALTH_MCP_NO_AUTO_RECOVERY` is truthy; assert `app_lifespan` startup calls `process_cleanup.activate()` (so a real backend still reaps). Prevents the "M11a silently disabled recovery" regression.
- **conftest opt-out:** assert the env var is set during the test session.
- **F-720/F-763 (`test_env_utils.py`, new):** `parse_nonnegative_int_env` (default, negative→default, `minimum`), `parse_float_env`; `parse_bool_env` truthy set **including `"enabled"`**; assert the `NO_AUTO_RECOVERY` guard honors `"enabled"` (the F-763 silent-fail); assert `browser_manager`/`process_cleanup`/`server` no longer define a local parser (single source).
- **F-607:** simulate `msvcrt.locking`/`fcntl.flock` raising on acquire → assert `_file_lock` does **not** yield-as-acquired (raises/skips) and that `_load`/`_save` degrade with a logged warning rather than racing.
- **recover_orphans seam:** assert `recover_orphans()` invokes `_recover_orphaned_processes`; assert `cli` `kill-orphans` calls `recover_orphans()` (patch + assert).

**M15**
- **F-207 (the pin the brief names):** build `BrowserInstance(headless=True, user_agent="ua", viewport={"width":800,"height":600})`, store via the new path, `get_instance`, and assert `headless`/`user_agent`/`viewport`/`last_activity` **survive** — the exact silent-drop the finding describes.
- **F-122 rename:** `from in_memory_storage import in_memory_storage` works; no module remains importable as `persistent_storage`; `test_exception_handling` import + `patch("browser_manager.in_memory_storage")` updated.
- **F-722:** `cli serve --port` default `== singleton.DEFAULT_PORT`; top-level `--singleton-port` default `== DEFAULT_PORT`; `response_handler` clone dir `== STATE_DIR/"element_clones"`; no bare `19222`/`".stealth-mcp"` literal remains at those sites.
- **Whole-suite gate:** **402 still green, coverage ≥ 39** after every step (new `test_env_utils`, import-guard, and field-round-trip tests add coverage).

---

## 6. Rollback + checkpoint commits

- **Branch:** `audit/fixes-2026-07-02`, serial **after M7's final commit**.
- **One commit per step** (6 total): `M11a-1 env_utils` · `M11a-2 guarded-init+conftest` · `M11a-3 file_lock+seam+pidpath` · `M15-1 serialize-model` · `M15-2 rename-storage` · `M15-3 export-constants`.
- **Independently revertible:** M11a (3 commits) and M15 (3 commits) share no files; either half reverts without touching the other. Within M11a, M11a-3 reverts alone; M11a-2 reverts alone (M11a-1 stands, `env_utils` is inert if unused). Within M15, M15-3 (constants) and M15-1 (serialize) revert alone; M15-2 (rename) is the widest — revert restores the old module name across the enumerated sites.
- **Stage-3 discipline:** pinning test first in every step; re-anchor by symbol per §1.3; **any deviation from a confirmed anchor → STOP and report**, do not improvise.

---

## 7. Risk (blast radius · worst case · early warning)

**M11a**
- **Recovery silently disabled (the nightmare).** *Blast radius:* orphaned Chrome processes + temp profiles accumulate across restarts. *Mitigation:* the fresh-backend-reap pin + the confirmed fact that the HTTP backend runs `app_lifespan` (`lifespan=app_lifespan:1294`); `kill-orphans` remains a manual backstop. *Worst case:* a mis-wired `activate()` → no auto-reap, caught by the pin and by profiles piling up in the session root. *Early warning:* pin red; growth of `uc_*` temp profiles.
- **`_file_lock` raise is noisier than the old silent yield.** *Worst case:* under contention a save is **skipped** (logged) instead of racing — safer, not worse. *Early warning:* more "Failed to save PID file" warnings under contention (now visible, previously masked — an improvement).
- **`env_utils` import cycle.** *Mitigation:* the module imports only stdlib (`os`) — no embedded imports, no cycle possible.

**M15**
- **Serializing a non-serializable field.** *Assessed:* `BrowserInstance` holds only scalars/enum/str/dict; live handles + locks live in `BrowserManager`, not the model → `model_dump(mode="json")` cannot hit a live object. *Worst case (future field):* a later non-serializable field raises in `store_instance`; the `:480` call sits inside browser-manager's try path and a `mode="json"` dump surfaces it loudly at spawn. *Early warning:* spawn-time serialization error in `test_process_cleanup`/spawn tests.
- **Rename misses a site → runtime `ImportError`.** *Mitigation:* the enumerated importer/usage/string/test list (§1.2) + a full green suite (M15-2 runs the whole suite). *Early warning:* `ImportError` at backend start or a test-collection error. *Watch:* the `server.py:3028` string only errors *silently* (a diagnostics tool drops a module) — verify it against M2's deletion.

---

## 8. Findings closed

**M11a**
- **F-124** (import-time process kill) — **CLOSED.** Side-effect-free `__init__`; recovery + handler-install move to an explicit `activate()` fired once at the backend serve boundary (`app_lifespan`); `conftest` sets the opt-out as defense-in-depth.
- **F-125** (15 singletons construct unconditionally, no DI/reset seam) — **PARTIAL.** The acute `process_cleanup` slice (its import-time *side effects*) is removed; the general factory/DI seam for all 15 is **M11b — explicitly DEFERRED**.
- **F-103** (module-import side effects) — **PARTIAL.** Removes `process_cleanup`'s import-time recovery/handler side effect; `server.py`'s other import-time side effects (`XPOOL_SAFE_MODE` toggle, cookie-compat install, the 4 bare-scope singletons) are **M4/M11b** territory, not this batch.
- **F-607** (`_file_lock` yields on acquire-failure) — **CLOSED.** Bounded-acquire-or-raise; callers already log+degrade. Residual (load/save non-atomicity) flagged, not fixed (out of cheap scope).
- **F-720** (`_parse_nonnegative_int_env` ×2 + scattered parse family) — **CLOSED.** One `embedded/env_utils.py` home; three modules import it.
- **F-763** (guard's narrower truthy set silently drops `"enabled"`) — **CLOSED.** The guard routes through canonical `parse_bool_env`.

**M15**
- **F-207** (silent field-drop) — **CLOSED.** `BrowserInstance.model_dump(mode="json")` serialized whole; `store_instance` stores as-passed.
- **F-122** (`persistent_storage` in-memory misnomer) — **CLOSED** by rename → `in_memory_storage` (durability deliberately rejected; §2 M15-B).
- **F-722** (`DEFAULT_PORT`/`STATE_DIR` never imported) — **CLOSED.** Exported from `singleton`, imported at `server.py:23`, `cli.py:258`, `response_handler.py:25`.
- **F-762** (one state-root convention) — **PARTIAL.** `response_handler` nested under `STATE_DIR`; pid file relocated under `STATE_DIR` *(if approved)*. Residual: `_default_session_root`'s divergent, non-home root **DEFERRED** (migration harm to existing profiles; overlaps M4) — recommended as a dedicated migration-aware follow-up.

**Deferred, stated explicitly:** M11b (full DI/reset seam for 15 singletons); `_default_session_root` relocation; `_file_lock` load-modify-save atomicity.

---

## Appendix — the four lenses, where each shaped a choice

- **Deduplication →** one `env_utils.py` for the whole parse family (F-720); one imported `DEFAULT_PORT`/`STATE_DIR` instead of four literals (F-722). *(Shaped: rejecting "keep the tiny copies" and "import parsers from server.py".)*
- **Clarity →** `in_memory_storage` stops the name from lying (F-122); a side-effect-free `__init__` stops hiding a process kill behind construction (F-124). *(Shaped: choosing rename over durability, and the explicit `activate()` boundary over an import side effect.)*
- **Conventions →** recovery gets **one** trigger (the serve boundary); the guard uses the **one** canonical bool parser so `"enabled"` behaves like everywhere else (F-763); serialization uses pydantic's `model_dump`, not a second hand-rolled path (F-207). *(Shaped: rejecting per-caller env opt-out as the primary mechanism, and rejecting a custom `to_dict`.)*
- **Modularity →** storage stays model-agnostic (caller serializes); `env_utils` is a stdlib-only leaf (no cycles). *(Shaped: rejecting "pass the model into `store_instance`".)*
