# Stage 2 Plan — M3 Observability Spine + M10a Silent-Except Logging

- **Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701`
- **Branch (audit):** `fix/singleton-version-aware-backend` · **Fix branch (Stage 3):** `audit/fixes-2026-07-02`
- **Date:** 2026-07-02
- **Batch:** {M3 observability spine, M10a silent-except logging}
- **Status:** **APPROVED** (human, 2026-07-02) — cleared for Stage 3. Decisions: **two stacked PRs** (PR-A = M3 steps 1–6; PR-B = M10a steps 7–8 stacked on PR-A); **`log_info`→INFO file default** (`STEALTH_MCP_LOG_LEVEL` is the runtime override).
- **Findings closed:** M3 = F-303, F-503, F-304, F-183, F-182, F-308, F-204 · M10a = F-181 · **Amendment A1 = F-764** (verified lock re-entrancy deadlock)
- **Context (do not re-derive):** LOCAL single-user tool, **0 users, breaking changes are FREE.** Priorities strict: (1) maintainability (2) operability (3) performance. `uv run` is BROKEN here (path has `&`+spaces) — always the venv python directly.

All line anchors below were **re-opened and confirmed at the pinned SHA** while writing this plan (see §1). The repo-wide claim "zero `FileHandler`/`RotatingFileHandler`/`basicConfig`/`getLogger` anywhere in `src/`" was **re-verified by grep and is TRUE** — there is currently no stdlib logging of any kind; the only logger is the in-memory `DebugLogger`.

---

## The one load-bearing fact that shapes this whole plan

There are **two distinct processes**, and they have **two distinct blindness problems**. Confusing them is the main design trap:

1. **The backend** — one detached, shared `python -m stealth_chrome_devtools_mcp --transport http` process, spawned by `singleton._start_server_process` (`singleton.py:241`) with **`stdout/stderr/stdin = subprocess.DEVNULL`** (`singleton.py:229-231`) and `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` (`:234-237`). This process runs FastMCP, all 96 tools, and every `debug_logger.*` call. Its output is the black hole (F-303/F-503). Note: an **import-time crash** in `embedded/server.py` (F-183's "freshly-edited server.py with a syntax error") dies *before* any in-process logging could install itself → only a raw stream redirect at `Popen` can capture it.

2. **The stdio proxy** — one short-lived process **per Claude Code session**, running `run_stdio_proxy` → `_bridge` → `_proxy_streams` (`singleton.py:567/549/401`). Its stderr is inherited from the MCP client, but the **cold-start orchestration it runs in a daemon thread** (`_start_backend_holding_lock` `:272-294`, `_await_backend_http` `:316-365`, `run_backend` `:459-461`) **swallows every cold-start failure** and, on the 120 s readiness timeout, returns silently and tears the proxy down with no trace (F-183).

M3 therefore installs logging on **both** fronts, writing to files under the **already-established per-user state dir** so evidence survives the death of either process.

---

## 1. Scope

### 1.1 Confirmed code anchors at HEAD (re-opened while writing this plan)

**State-dir convention to reuse (already in the codebase):**
- `singleton.py:34` — `STATE_DIR = Path.home() / ".stealth-mcp"`
- `singleton.py:40` — `SERVER_STATE_FILE = STATE_DIR / "server.json"` (records `{port, version, pid}` of the running backend — the operator's handle to "which backend/pid is live")
- `singleton.py:50-51` — `_ensure_state_dir()` → `STATE_DIR.mkdir(parents=True, exist_ok=True)`
- `response_handler.py:11-25` — `default_clone_output_dir()`: the **env-overridable per-user-dir pattern** established by commit `9778218` (`STEALTH_MCP_CLONE_OUTPUT_DIR` → else `~/.stealth-mcp/element_clones`). The M3 log dir reuses this exact idiom (`STEALTH_MCP_LOG_DIR` → else `~/.stealth-mcp/logs`).

**M3 anchors:**
- `singleton.py:218-245` — `_start_server_process`; DEVNULL kwargs at `:229-231`, detach flags `:234-237`, `Popen` `:241` (**F-303, F-503**).
- `singleton.py:263-269` — `_server_version()`; silent `except Exception: return "0.0.0"` at `:268-269`.
- `singleton.py:272-294` — `_start_backend_holding_lock` (daemon thread, `ensure_server_running` `:310-312`); silent `except Exception: pass` at `:293-294` (**F-183 primary**).
- `singleton.py:316-365` — `_await_backend_http`; per-probe silent `except Exception: pass` at `:358-359` and `:361-362`; polls to `BACKEND_READY_TIMEOUT = 120.0` (`:47`).
- `singleton.py:459-461` — `run_backend`: `if not await _await_backend_http(url): return` (silent 120 s give-up).
- `singleton.py:507-535` — `run_backend_guarded` (`:515-520` its only `print(..., file=sys.stderr)`), `monitor_backend` (`:530-534` the other print).
- `debug_logger.py:41` — `self._enabled = False` (**F-182**).
- `debug_logger.py:47-60` — `_emit_stderr` (gated on `_enabled` unless `force=True`).
- `debug_logger.py:62-99` — `log_error`; `if not self._enabled: return` `:72-73`; dedup block `:75-84` with **`self._seen_errors.clear()` at `:82-83`** (**F-204**); dedup key `:76`.
- `debug_logger.py:101-126` / `:128-155` — `log_warning` / `log_info`; same `_enabled` gate `:111-112` / `:138-139`.
- `debug_logger.py:157-223` — `get_debug_view` / `get_debug_view_paginated`; return shape `:205-223` carries `component/method/timestamp` but **no request/session/correlation id** (**F-308**).
- `debug_logger.py:498` — `debug_logger = DebugLogger()` (the one global singleton shared by all sessions — F-204's root).
- `embedded/server.py:54` — `parse_bool_env` (env-flag helper to reuse).
- `embedded/server.py:217-220` — `DEBUG_LOGGING_ENABLED = parse_bool_env("STEALTH_BROWSER_DEBUG") or parse_bool_env("DEBUG")`.
- `embedded/server.py:1212-1217` — **`section_tool(section)`** decorator: `SECTION_TOOLS[section].append(func.__name__); return mcp.tool(func)`. **The single chokepoint all 96 tools pass through → the correlation-id insertion point (F-308).**
- `embedded/server.py:1279-1303` — `mcp = FastMCP(...)`, singletons constructed, `if DEBUG_LOGGING_ENABLED: debug_logger.enable()`.
- `embedded/server.py:2459-2559` — the 4 debug tools: `get_debug_view` `:2460`, `clear_debug_view` `:2487`, `export_debug_logs` `:2505`, `get_debug_lock_status` `:2549` — all `@section_tool("debugging")`, i.e. dispatched through the same request path as every other tool (**F-304**).
- `embedded/server.py:4139-4207` — backend `if __name__ == "__main__"` block; `--debug`/`debug_logger.enable()` `:4142-4143`; `mcp.run(transport=...)` `:4204-4207` (**where `configure_logging("backend")` is installed**).
- `src/stealth_chrome_devtools_mcp/server.py:14-34` — top-level launcher `main()`: stdio path → `run_stdio_proxy` `:31`; http/standalone path → `runpy.run_path(EMBEDDED_DIR/"server.py")` `:34` (this is *why* an `embedded/server.py` import error escapes in-process logging).

**M10a anchors — the 21 truly-silent broad `except Exception` handlers (AST-verified, see §5.4):**

| # | File:line | Body | Owner |
|---|---|---|---|
| 1 | `browser_manager.py:1165` | `return False` (switch_to_tab) | M10a-7a |
| 2 | `browser_manager.py:1207` | `return False` (close_tab) | M10a-7a |
| 3 | `dom_handler.py:197` | `await element.click()` fallback | M10a-7a |
| 4 | `dom_handler.py:301` | send_keys Ctrl+A fallback | M10a-7a |
| 5 | `dom_handler.py:390` | CDP key-event fallback | M10a-7a |
| 6 | `dom_handler.py:661` | `continue` (loop) | M10a-7a |
| 7 | `network_interceptor.py:171` | `pass  # body unavailable … (expected)` | M10a-7b |
| 8 | `network_interceptor.py:271` | `continue  # skip if body can't be decoded` | M10a-7b |
| 9 | `network_interceptor.py:357` | `pass  # body unavailable … (expected)` | M10a-7b |
| 10 | `dynamic_hook_system.py:318` | `response_headers = {}` fallback | M10a-7b |
| 11 | `proxy_utils.py:109` | `return prefix + "<redacted>"` | M10a-7c |
| 12 | `proxy_utils.py:124` | `return "<redacted>"` | M10a-7c |
| 13 | `proxy_forwarder.py:405` | `break` (loop) | M10a-7c |
| 14 | `platform_utils.py:301` | `continue` (loop) | M10a-7c |
| 15 | `server.py:198` | `return` | M10a-7d |
| 16 | `server.py:997` | `roots = []` fallback | M10a-7d |
| 17 | `server.py:1226` | `continue  # Tool may already be removed` | M10a-7d |
| 18 | `singleton.py:268` | `return "0.0.0"` | **M3-6** (F-183 file) |
| 19 | `singleton.py:293` | `pass` (cold-start) | **M3-6** (F-183) |
| 20 | `singleton.py:358` | `pass` (probe) | **M3-6** (F-183) |
| 21 | `singleton.py:361` | `pass` (probe) | **M3-6** (F-183) |

> **Reconciliation with F-181's "22":** F-181 asserted 22 truly-silent handlers; the AST pass (walk every `ExceptHandler`, keep `except Exception` only, exclude any that re-raise / call `log_*` / `print` / reference the bound exception) finds **exactly 21**. The delta is one boundary case, `debug_logger.py:288` (`clear_debug_view_safe`'s fallback), which F-181 likely counted but which **does** call `_emit_stderr` at `:293` and so is not truly silent by the strict definition. The 21-row table above is the authoritative Stage-3 checklist. `debug_logger.py:288`'s handler is already reworked as part of M3-3 (debug_logger changes), so nothing is lost either way.

> **Overlap note:** rows 18–21 live in `singleton.py` and are the same handlers F-183 (M3) must instrument. They are fixed in **M3 step 6**, *not* in the M10a batch, to avoid double-editing the file. M10a therefore covers **17 handlers across 8 files** (rows 1–17).

### 1.2 Files to be touched

**New file (1):**
- `src/stealth_chrome_devtools_mcp/embedded/logging_setup.py` — the observability module (file-logging config, correlation `ContextVar`, `logging.Filter`, log-dir resolution, old-log pruning). Named `logging_setup` (not `logging`) so it never shadows the stdlib on the bare-name `sys.path` the embedded package uses.

**Modified source (M3):**
- `embedded/singleton.py` — Popen redirect (F-303/F-503); `configure_logging("proxy")` in `run_stdio_proxy`; cold-start tracing at `:268, :293, :358, :361, :461` (F-183).
- `embedded/debug_logger.py` — unconditional recording + bridge each `log_*` to the stdlib file logger (F-182, F-303, F-304); LRU dedup eviction + correlation stamp (F-204, F-308).
- `embedded/server.py` — `configure_logging("backend")` + `sys.excepthook`/`faulthandler` in the `__main__` block; correlation-id wrapper in `section_tool` (F-308).

**Modified source (M10a):**
- `embedded/browser_manager.py`, `embedded/dom_handler.py`, `embedded/network_interceptor.py`, `embedded/dynamic_hook_system.py`, `embedded/proxy_utils.py`, `embedded/proxy_forwarder.py`, `embedded/platform_utils.py`, `embedded/server.py` — add a level-appropriate log to rows 1–17.

**Tests (new + changed) — see §5.**

### 1.3 Explicit out-of-scope (stated here so Stage 3 does not scope-creep)
- **M1 app-level liveness probe** (next plan). M3 must *enable* verifying M1 later — its logs are how you will confirm a liveness fix — but M3 does **not** add a `/healthz`/MCP liveness probe.
- **M8 recovery CLI / `doctor`** — surfacing pid/port/log-path via a CLI verb (mentioned in F-503's fix direction) belongs to M8.
- **M2** (reuse-key / delete `hot_reload`), **M7** (teardown executor), **M9** (body-store cap).
- **M10b full error-envelope unification** — absorbed into M4-Ph1 later. **Do not redesign error shapes now.** M10a only *adds a log line* before the existing return/pass/continue; the sentinel values (`False`/`{}`/`"<redacted>"`) are left exactly as-is.
- **F-204 full instance-id threading through all 287 call sites** — explicitly deferred (L-effort architecture change). M3 closes F-204's two concrete harms at the logger + durable-file layer instead (see §8).
- The debug-export tools' output files (`export_to_file_paginated` writes `debug_log.json`/`.pkl` to the process CWD) are **not** relocated here — that is an M15/M4 storage concern. M3's new log dir is independent.
- No drive-by refactors. Any new problem discovered mid-implementation becomes a **new finding**, not extra scope.

---

## 2. Approach + rejected alternatives

### 2.1 Chosen design

**A. One tiny stdlib-logging module (`logging_setup.py`).** Exposes:
- `resolve_log_dir() -> Path` — `STEALTH_MCP_LOG_DIR` env override, else `singleton.STATE_DIR / "logs"` (reuses the existing convention; pure, then `mkdir`).
- `configure_logging(role: str) -> Path` — **idempotent**; installs one `RotatingFileHandler` (`maxBytes≈5 MB`, `backupCount=3`, `delay=True`, `encoding="utf-8"`) on a dedicated logger `stealth.<role>` writing to **`<logdir>/<role>-<pid>.log`**, at level `INFO` (raise/lower via `STEALTH_MCP_LOG_LEVEL`). Attaches `CorrelationIdFilter`. Sets `logger.propagate = False`. Returns the path. Also calls `prune_old_logs()`.
- `correlation_id_var: ContextVar[str]` (default `"-"`), `new_correlation_id()`, and `CorrelationIdFilter` that stamps `record.correlation_id`.
- Format: `%(asctime)s %(levelname)s %(process)d [%(correlation_id)s] %(name)s: %(message)s`.
- `prune_old_logs(keep_days=7, keep_files=50)` — best-effort sweep so per-pid files don't accumulate forever.

**B. Backend file logging (F-303).** `configure_logging("backend")` is the **first statement** in `embedded/server.py`'s `__main__` block (`:4140`), plus `sys.excepthook`/`threading.excepthook` that log fatal tracebacks and `faulthandler.enable(<logfile>)` for hard/C-level faults. FastMCP/uvicorn already log through stdlib `logging`, so their records flow to the file once the root/`stealth` logger has a handler.

**C. Popen raw-stream redirect (F-303/F-503, the import-crash safety net).** `_start_server_process` opens `<logdir>/backend-boot.log` (append) and passes it as **`stdout` and `stderr`** to `Popen`; **`stdin` stays `DEVNULL`.** This captures the one class of failure in-process logging structurally cannot — an `embedded/server.py` import/boot crash that dies before `configure_logging` runs (exactly F-183's "edited server.py, fresh backend fails to launch"). Decision recorded: **redirect, not keep-DEVNULL**, precisely because in-process logging can't cover pre-`main()` death.

**D. Route `debug_logger` through the file logger + make recording unconditional (F-182, F-304).** In `debug_logger.log_error/log_warning/log_info`: (1) **remove the `if not self._enabled: return` gate on recording** — always append to the in-memory ring **and** emit one record to `logging.getLogger("stealth.backend")` (`error→ERROR`, `warning→WARNING`, `info→INFO`); (2) `_enabled` now governs **only** the legacy `_emit_stderr` echo. Consequence: the durable file gets every error/warning/info the moment it happens, through the handler's own per-emit flush — this **is** the "background/crash flush," and it is strictly better than a periodic flush task (no lock race, so it also sidesteps F-307's untimed-lock deadlock). **This is how F-304 is genuinely closed:** the in-memory debug *tools* can still hang with the wedged request path, but the data they would have shown is already on disk, so the operator no longer needs those tools at the one moment they are unreachable.

**E. Per-call correlation id at the `section_tool` chokepoint (F-308).** Wrap the registered function so every tool call sets `correlation_id_var` to a fresh short id (via `try/finally` token reset), and log one `INFO` "tool `<name>` start/end (`<ms>`)" pair. The `CorrelationIdFilter` then stamps that id onto **every** log line emitted during the call — backend file, debug_logger entries, and (later) M1's liveness logs. Because it is one wrapper on the one decorator all 96 tools share, F-308 is closed without touching call sites. The wrapper must preserve the FastMCP tool schema (`functools.wraps` + a `tools/list` pinning test — see Risk §7).

**F. Cold-start tracing in the proxy (F-183).** `configure_logging("proxy")` at the top of `run_stdio_proxy`; then replace the four silent swallows with `stealth.proxy` logs: `_start_backend_holding_lock`'s `except` (`:293`) logs the exception + traceback; `run_backend` logs an explicit error when `_await_backend_http` exhausts its 120 s deadline (`:461`); the two probe `except`s (`:358/:361`) log at `DEBUG`; `_server_version()`'s fallback (`:268`) logs at `DEBUG`. The existing `:515-520`/`:530-534` stderr prints are converted to `stealth.proxy` logs too (kept as `WARNING`).

**G. F-204 dedup fix.** Replace `self._seen_errors.clear()` (`:82-83`) with **single-oldest LRU eviction** (`OrderedDict` move-to-end + `popitem(last=False)` at cap) so hitting the cap forgets one signature, not all 1000 (no re-log burst). The cross-session-suppression harm is neutralized at the durable-file layer: the file emit in (D) is **not** deduped and carries the correlation id, so every session's every error is on disk regardless of the in-memory dedup. (Full per-session `instance_id` threading stays out of scope — see §8.)

**H. M10a (F-181).** For rows 1–17, add one `debug_logger.log_warning`/`log_info` (now durable via D) naming the component/method and the exception, immediately before the existing sentinel return/pass/continue. **Level is per-handler, not blanket WARNING** — see §3 step 7. Sentinels and control flow are unchanged.

### 2.2 Rejected alternatives

1. **`loguru`/`structlog` dependency instead of stdlib `logging`.** Rejected: adds a dependency to a tool whose audit already flags 34 dep vulns; stdlib `RotatingFileHandler` + one `Filter` covers every requirement; maintainability favors zero new deps. Breaking-change freedom doesn't argue *for* a dep.
2. **Keep `Popen` on `DEVNULL` and rely solely on in-process backend logging.** Rejected: cannot capture import-time/boot crashes (F-183's core scenario) — the process dies before any handler exists. The boot-log redirect is cheap insurance for exactly the failure M3 exists to make visible.
3. **A single shared `backend.log` with rename-based rotation.** Rejected on Windows: `RotatingFileHandler` rollover does `os.rename`, which raises `WinError 32` if the *other* backend (during the ≤5 s evict-overlap in `_clear_stale_backend`) still holds the file open. **Per-pid filenames** (`backend-<pid>.log`, `proxy-<pid>.log`) eliminate cross-process rotation contention entirely; a prune sweep bounds the file count. (This directly answers the brief's Windows/two-backend risk prompt.)
4. **A periodic asyncio background flush task for the `DebugLogger` buffers (F-304's literal fix direction).** Rejected in favor of per-emit file writes (D): a periodic task competes for `DebugLogger._lock` and re-opens the exact deadlock surface F-307 warns about, and still loses the last interval on a hard crash. Per-emit flush loses nothing and adds no task.
5. **Env-gated logging (keep the `STEALTH_BROWSER_DEBUG` gate, just point it at a file).** Rejected: the whole point of F-182/F-303 is that the default install is a black box. Logging must be **unconditional** (errors/warnings always; the noisy per-event `info` stream governed by `STEALTH_MCP_LOG_LEVEL`, default INFO).
6. **Global root-logger `basicConfig`.** Rejected: risks double-handlers on module re-import (the backend is launched via `runpy.run_path`, which can import `server` twice) and would capture unrelated library logging at unintended levels. A dedicated `stealth.*` logger with `propagate=False` and an idempotent installer is contained and predictable.
7. **Full `instance_id` threading through all 287 `debug_logger` call sites now (F-204's architecture fix).** Rejected as out-of-scope L-effort; the correlation `ContextVar` + undeduped file give the session dimension where it is actually consumed (the durable log) without editing 287 sites.

---

## 3. Sequencing (smallest-first, each independently verifiable)

> Baseline before starting (must be green): `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed**, coverage ≈40.9% ≥ gate 39. Re-run after **every** step. One checkpoint commit per step (§6).

**Step 1 — `logging_setup.py` module (foundation; no behavior change yet).**
Create the module (§2.1-A). Pinning test first (`tests/test_logging_setup.py`): `configure_logging("backend")` creates `<STATE_DIR>/logs/backend-<pid>.log`, emits an `INFO` line that lands in the file, is idempotent (calling twice adds no second handler), respects `STEALTH_MCP_LOG_DIR`, and `CorrelationIdFilter` stamps the current `correlation_id_var`.
*Verify:* `.venv\Scripts\python.exe -m pytest tests/test_logging_setup.py -q` green; full suite still 402.

**Step 2 — Backend file logging wired in (F-303 in-process half).**
Add `configure_logging("backend")` + `sys.excepthook`/`threading.excepthook`/`faulthandler.enable` at the top of `embedded/server.py` `__main__` (`:4140`).
*Verify:* new test `tests/test_backend_logging.py::test_backend_writes_startup_log` runs the backend far enough to emit startup and asserts a `backend-<pid>.log` with the FastMCP startup line; assert an injected `sys.excepthook` fatal is recorded.

**Step 3 — Popen raw redirect (F-303/F-503 boot half).**
`embedded/singleton.py:229-231`: replace `stdout/stderr = DEVNULL` with an opened append handle to `<logdir>/backend-boot.log`; keep `stdin = DEVNULL`.
*Verify:* `tests/test_singleton_backend_logging.py::test_boot_crash_is_captured` — point `_start_server_process` at a module that raises on import, assert the traceback text appears in `backend-boot.log`. Existing `test_singleton_version_aware.py::test_start_server_process_records_current_version_and_pid` must still pass (assert it does not assert on `DEVNULL`).

**Step 4 — `debug_logger` → file bridge + unconditional recording (F-182, F-304).**
Modify `log_error/log_warning/log_info`: unconditional record + `stealth.backend` emit; `_enabled` now gates only `_emit_stderr`. **Update the 3 behavior-pinning tests that assert the opposite (see §5.3).**
*Verify:* new `tests/test_debug_logger_file_bridge.py` — a default (un-`enable()`d) `DebugLogger` now records **and** emits an `ERROR` to a captured handler; suite green with the 3 tests rewritten.

> **AMENDMENT A1 (2026-07-02, human-approved at the lens-delta gate) — F-764 lock re-entrancy deadlock.** Verified at conf 1.0 by the orchestrator: `export_to_file_paginated` acquires the non-reentrant `self._lock` (`:363`, `acquire(timeout=5.0)` succeeds) then calls `get_debug_view_paginated` (`:373`) which re-enters `with self._lock:` (`:183`) → **unconditional self-deadlock while holding the logger lock**; the `finally`/`except` never run. Post-Step-4 (unconditional recording) every `log_*` call would then block on the held lock too — so this MUST land inside Step 4. **Change:** `self._lock = threading.Lock()` (`:40`) → `threading.RLock()`. **Pinning test (written first, in `test_debug_logger_file_bridge.py`):** populate the logger, call `export_to_file_paginated()` from one thread, assert it returns within a bounded wait (fails by hanging on the pre-change code — run pre/post to prove). Scope limit: lock-type + test only; the `_lock_owner` bookkeeping and export internals stay as-is (F-703's serialization-engine concern remains deferred).

**Step 5 — Correlation id at `section_tool` (F-308).**
Wrap the registered func in `section_tool` (`:1214-1216`) to set/reset `correlation_id_var` and log start/end at INFO. `functools.wraps` to preserve schema.
*Verify:* new `tests/test_correlation_id.py` — calling a trivial registered tool stamps a non-`"-"` id on records emitted inside it and resets after; **`test_server_call_conventions.py` and a new `tools/list`-schema snapshot assert the tool signature/JSON schema is unchanged.**

**Step 6 — F-204 dedup LRU + cold-start tracing (F-183) [closes singleton rows 18–21].**
(a) `debug_logger.py:82-83`: `clear()` → single-oldest LRU eviction; stamp each entry with `correlation_id_var.get()`. (b) `embedded/singleton.py`: `configure_logging("proxy")` in `run_stdio_proxy`; add logging at `:293` (error+traceback), `:461` (explicit 120 s-timeout error), `:358/:361` (DEBUG), `:268` (DEBUG); convert prints `:515-520`/`:530-534` to `stealth.proxy` logs.
*Verify:* `tests/test_debug_logger.py::TestErrorDedup` updated to assert eviction (not wipe) keeps the set ≤ cap; `tests/test_singleton_cold_start_logging.py::test_coldstart_failure_is_logged` forces `_start_backend_holding_lock` to raise and asserts `proxy-<pid>.log` records it; `test_proxy_backend_death.py` still green.

**Step 7 — M10a silent excepts (F-181), batched by subsystem (rows 1–17).** Each sub-step adds a level-appropriate log then the existing sentinel:
- **7a — interaction hot path:** `browser_manager.py:1165,1207` (**WARNING** — real degraded tab ops); `dom_handler.py:197,301,390` (**DEBUG** — deliberate fallback chains, WARNING would be noise); `dom_handler.py:661` (**DEBUG**).
- **7b — network/hooks:** `network_interceptor.py:171,357` (**DEBUG** — comment says "expected"); `:271` (**DEBUG**); `dynamic_hook_system.py:318` (**WARNING** — header parse fell back to `{}`).
- **7c — proxy/platform:** `proxy_utils.py:109,124` (**WARNING**, and **must log only the exception, never the un-redacted value**); `proxy_forwarder.py:405` (**DEBUG**); `platform_utils.py:301` (**DEBUG**).
- **7d — server dispatch:** `server.py:198,997` (**WARNING**); `:1226` (**DEBUG** — comment says already-removed is expected).
*Verify:* per sub-step, a targeted test asserts the chosen handler now emits a record when its `try` body raises (e.g. `test_silent_excepts_log.py::test_switch_to_tab_logs_on_failure`); suite green after each.

**Step 8 — regression guard (keeps M10a from rotting).**
Add `tests/test_no_silent_excepts.py`: an AST test (modeled on `test_server_call_conventions.py`) that fails if any `except Exception` in `embedded/` swallows with no `log_*`/`raise`/exception-reference, **except** an explicit allowlist of the intentionally-silent handlers not in scope here (none should remain after steps 6–7; the allowlist starts empty and is the documented seam for future deliberate silences).
*Verify:* the guard passes at the post-step-7 tree and fails if a silent `except Exception` is reintroduced (prove by a temporary local edit, then revert).

---

## 4. Breaking changes

**0 users — external-compatibility breaking changes: N/A.** No tool name, signature, config key, or return shape changes for any of the 96 tools. The `section_tool` wrapper (step 5) is explicitly designed to leave every tool's FastMCP schema byte-identical (pinned in step 5's test).

**Observable behavior that does change (all intended, all internal):**
- The backend and proxy now **write log files** under `~/.stealth-mcp/logs/` (new dir, new disk writes). Governed by `STEALTH_MCP_LOG_DIR` / `STEALTH_MCP_LOG_LEVEL`.
- `DebugLogger` now **records unconditionally** (previously off unless `STEALTH_BROWSER_DEBUG`/`--debug`). `get_debug_view`/`export_debug_logs` therefore return populated data by default. `enable()`/`disable()` now toggle only the stderr echo. **This flips the contract three existing tests pin — they are rewritten, not deleted (see §5.3).**
- The backend process's raw `stdout`/`stderr` go to `backend-boot.log` instead of the null device.
- 17 previously-silent handlers + 4 singleton handlers now emit a log line on failure (no control-flow change).

---

## 5. Test strategy

Guiding rule (superpowers TDD): **the behavior-pinning test is written and shown to change/pass in the same step as the code.** Keep the 402 green and coverage ≥ 39 at every checkpoint; the module + new tests add coverage (net positive against the gate).

### 5.1 Behavior-pinning tests written BEFORE each change
- **Log file is created on backend start** (step 1/2): assert `backend-<pid>.log` exists and contains the startup line after `configure_logging("backend")` + backend boot.
- **Boot crash leaves a trace** (step 3): a deliberately-crashing backend module → traceback present in `backend-boot.log`.
- **Silent cold-start now emits** (step 6): force `_start_backend_holding_lock` to raise → record in `proxy-<pid>.log`; force `_await_backend_http` deadline → explicit timeout error logged.
- **Correlation id present on tool-call log lines** (step 5): id is non-`"-"` inside a tool call, resets to `"-"` after, and two concurrent `asyncio` tool tasks get distinct ids (ContextVar isolation).
- **A specific silent except now emits** (step 7, one per sub-step): raise inside the guarded `try`, assert a record with the component/method + exception.
- **Dedup evicts, not wipes** (step 6): at `MAX_SEEN_ERRORS`, a new signature evicts exactly the oldest; set stays ≤ cap; a still-recent signature is still deduped.

### 5.2 Characterization tests for risky/undertested areas
- `logging_setup` idempotency + Windows path handling (no second handler on re-call; `RotatingFileHandler` opens with `delay=True` so import never fails on a locked dir).
- `tools/list` **schema snapshot** for a representative tool from each section (guards the `section_tool` wrapper against FastMCP signature loss) — this is new coverage of a currently-untested surface (ties into M6's intent).
- The AST silent-except guard (step 8) is itself the characterization net for the M10a class.

### 5.3 Tests that MUST change because behavior changes (enumerated)
1. `tests/test_debug_logger.py::TestEnableGating::test_logging_is_noop_until_enabled` (`:14-22`) — asserts `total_* == 0` when not enabled. **Rewrite** to assert recording is now unconditional and that `enable()`/`disable()` toggle only the stderr echo.
2. `tests/test_debug_logger.py::TestEnableGating::test_disable_stops_further_logging` (`:24-30`) — asserts `total_info == 1` after `disable()`. **Rewrite** to assert `disable()` does not stop *recording*.
3. `tests/test_exception_handling.py::TestDebugLoggerCaps::test_disabled_logger_does_not_accumulate` (`:80-90`) — asserts a disabled logger stores nothing. **Rewrite** to the new unconditional-recording contract.
4. `tests/test_debug_logger.py::TestErrorDedup::test_seen_error_set_clears_at_cap` (`:72-81`) — its assertion `len(_seen_errors) <= MAX_SEEN_ERRORS` still holds under LRU, but its **intent/name** ("triggers a clear") is now wrong. **Update** name+comment to "bounded by eviction," assert the oldest signature was evicted and a still-recent one retained.

> Tests that should stay green unchanged (spot-checked while planning): `test_exception_handling.py::test_stderr_catches_only_os_and_value_errors` (we keep `_emit_stderr`'s `OSError/ValueError` behavior), all `TestBufferCaps`/`TestViewAndClear` (they call `enable()` explicitly), the whole `test_singleton_version_aware.py` / `test_singleton_fast_handshake.py` / `test_proxy_backend_death.py` suites (we add logging around, not inside, their asserted logic — confirm no test asserts `DEVNULL`).

### 5.4 How the M10a enumeration was produced (reproducible)
`ast.walk` over every `.py` in `embedded/`, keep `ExceptHandler` whose type is exactly `Exception`, classify **truly-silent** = (no `raise`) ∧ (no call whose dotted name contains `log_error/log_warning/log_info/logger/logging/print/_emit/traceback/warn`) ∧ (bound name, if any, never referenced in the body). Result: **176** broad `except Exception`, **0** bare `except:`, **21** truly-silent (the §1.1 table). Stage 3 should re-run this exact classifier as the acceptance check for step 8.

---

## 6. Rollback + checkpoint commit boundaries

- **Branch:** `audit/fixes-2026-07-02`. Per Stage-3 discipline: serial execution, pinning tests before the change, **full suite green at every checkpoint**, deviation → stop and report to Fable.
- **Checkpoint granularity:** **one commit per sequencing step** (§3). Suggested messages: `M3-1 logging_setup module`, `M3-2 backend file logging`, `M3-3 popen boot-log redirect`, `M3-4 debug_logger file bridge (unconditional)`, `M3-5 correlation id via section_tool`, `M3-6 F-204 LRU + F-183 cold-start tracing`, then `M10a-7a…7d`, `M10a-8 silent-except guard`.
- **PR structure (recommended):** two stacked PRs on the shared branch — **PR-A = M3 spine (steps 1–6)**, **PR-B = M10a (steps 7–8) stacked on PR-A** (M10a's log calls depend on M3-4 making `debug_logger` durable). This honors "one PR per fix" while respecting the {M3,M10a} batch dependency. If the human prefers a single PR, keep the same commit boundaries inside it.
- **What to revert if a step goes bad:** because each step is one commit and each is behind its own test, a red suite at checkpoint *N* means `git revert`/reset that single commit; earlier checkpoints remain green and shippable. The only step that changes existing test expectations is **M3-4** (the 3 rewrites) — if it destabilizes, it reverts cleanly on its own (steps 1–3 are pure additions). Steps 7a–7d are independent of each other and can be reverted individually.
- **Fastest safe rollback of the whole batch:** steps 1–3 are additive (new module + redirect) and safe to keep even if 4–8 are reverted; the product is strictly more observable with just 1–3.

---

## 7. Risk (blast radius, worst case, early-warning signs)

**What this logging change could itself break:**

1. **Windows `RotatingFileHandler` rollover lock (WinError 32).** *Blast radius:* a rollover firing while a second process holds the file → rename fails. *Mitigation:* per-pid filenames (no two processes share a structured log) + `delay=True`. Python's `logging` already routes handler errors through `handleError` (prints to stderr, does not crash). *Worst case:* a single dropped rollover line. *Early warning:* `handleError` noise or a stuck `.log.1` in the log dir.
2. **Two backends briefly coexisting** (the ≤5 s `_clear_stale_backend` evict window). *Mitigation:* per-pid structured logs make this a non-event; `backend-boot.log` is append-mode (interleave, never corruption). *Early warning:* two `backend-<pid>.log` with overlapping timestamps — expected and harmless.
3. **Hot-path INFO cost (network interception / dynamic hooks per-event).** Making `info` recording unconditional adds a lock+append+format on paths that today are no-ops. *Blast radius:* the crown-jewel network path. *Mitigation:* file handler default INFO with `STEALTH_MCP_LOG_LEVEL` to raise to WARNING; step 7b keeps the per-event network/hook handlers at **DEBUG** (dropped by default); the in-memory ring stays hard-capped. *Worst case:* measurable slowdown on a pathological high-QPS interception session → operator sets `STEALTH_MCP_LOG_LEVEL=WARNING`. *Early warning:* `backend-<pid>.log` growing MB/s or interception latency regressions. **Perf is priority 3 and order-of-magnitude only — do not micro-optimize; measure first.**
4. **Double-logging via `propagate`.** *Mitigation:* dedicated `stealth.*` logger, `propagate = False`, idempotent installer (guard against a second handler on `runpy` re-import). *Early warning:* duplicated lines.
5. **FastMCP tool-schema loss from the `section_tool` wrapper.** *Blast radius:* every tool (tools/list) if FastMCP introspects the wrapper instead of the wrapped func. *Mitigation:* `functools.wraps` + the mandatory `tools/list` schema snapshot test in step 5; if FastMCP ignores `__wrapped__`, fall back to setting `wrapper.__signature__ = inspect.signature(func)` or dropping the wrapper for a FastMCP middleware hook. *Early warning:* step-5 schema test red, or tools losing parameters in `tools/list`. **This is the highest-uncertainty step — gate it hard on the pin.**
6. **ContextVar leakage across async tasks.** *Mitigation:* `token = var.set(...)` / `var.reset(token)` in `try/finally` inside the wrapper; test with two concurrent tool tasks. *Early warning:* correlation ids bleeding between calls in the log.
7. **Log-dir permission / creation failure** (read-only `$HOME`, sandbox). *Mitigation:* reuse `_ensure_state_dir`'s `exist_ok=True`; `configure_logging` must **degrade to a no-op (never raise)** if the dir can't be made — logging must never take down the backend. *Early warning:* a startup log-setup warning on stderr (the one place it can still go).
8. **Disk growth** from per-pid proxy logs (one proxy per session). *Mitigation:* `prune_old_logs(keep_days=7, keep_files=50)` on each `configure_logging`; `backupCount=3` per file. *Early warning:* `~/.stealth-mcp/logs/` file count climbing.

**Overall worst case:** a bad `section_tool` wrapper (risk 5) silently changes tool schemas — caught by the step-5 pin before merge. Everything else degrades to "a little noise" or "a little disk," never to a functional regression, because M10a adds only log lines and M3's file setup is fail-open.

---

## 8. Findings closed (each with how)

- **F-303 (backend stdout/stderr = DEVNULL, no file logger; final High).** Closed by (B) in-process `RotatingFileHandler` under `~/.stealth-mcp/logs/backend-<pid>.log` at unconditional INFO+, **and** (C) the `Popen` raw redirect to `backend-boot.log` for pre-`main()` crashes. The grep-verified "no file logging anywhere" state is directly reversed.
- **F-503 (detached/DEVNULL/no-log pillar; final High).** Same file sink as F-303; the DEVNULL redirect is removed. *Explicitly NOT closed here:* the supervisor/auto-restart (M1) and the `doctor` CLI that surfaces pid/port/log-path (M8) — flagged out-of-scope in §1.3. M3 delivers the "there is now a log to read" half.
- **F-304 (debug tools share the hung request path; final High).** **Genuinely addressed, not deferred:** by routing every `debug_logger.*` call to the durable file per-emit (D), the diagnostic data no longer lives *only* behind the tool calls that hang with the wedged backend — it is already on disk. The tools themselves are left as-is (still fine when the backend is healthy); an out-of-band control port is explicitly re-scoped to M1/M8. The concrete harm ("the one moment the data matters is the one moment it's unreachable") is eliminated.
- **F-183 (silent 120 s cold-start then blind teardown; final High).** Closed by (F): the proxy configures logging and the four silent swallows (`singleton.py:293, 461, 358, 361`) + the version fallback (`:268`) now emit; `run_backend` logs an explicit line when `_await_backend_http` exhausts its deadline, so the teardown always has a cause on disk. Also closes silent-except rows 18–21.
- **F-182 (logger off by default, nothing flushed; final Medium).** Closed by (D): recording is unconditional; `_enabled` governs only the stderr echo; every record is durably written per-emit. The "default install → empty debug view / lost-on-crash" harm is gone.
- **F-308 (no MCP request/correlation id; final Medium).** Closed by (E): a per-call id set once at the `section_tool` chokepoint and stamped on every record by `CorrelationIdFilter` — a stuck request is now identifiable by id, not timestamp guesswork.
- **F-204 (global dedup with no session dimension + clear-all wipe; final Medium).** **Genuinely addressed:** the `clear()`-all-at-cap (`:82-83`) becomes single-oldest LRU eviction (no re-log burst), and the cross-session-suppression harm is neutralized because the durable file emit (D) is **not** deduped and carries the correlation id — every session's every error reaches disk. The full 287-call-site `instance_id` refactor is **explicitly re-scoped/deferred** (L-effort architecture, §1.3) with justification: the contextvar+file design supplies the session dimension where it is consumed, so the mass refactor is unnecessary for closing the operability harm.
- **F-181 (22 truly-silent excepts; final Low).** Closed by (H)/step 7 for the 17 in-scope handlers (rows 1–17) with per-handler-appropriate levels, plus rows 18–21 via F-183 (step 6); step 8's AST guard prevents regressions. §1.1 records the 21-vs-22 reconciliation.
- **F-764 (High, lens re-scan; orchestrator-verified conf 1.0 — `export_to_file_paginated` self-deadlock via non-reentrant lock re-entry at `:363`→`:373`→`:183`).** Closed by **Amendment A1** in Step 4: `RLock` + re-entrancy pinning test. Without this, the tool `export_debug_logs` permanently wedges the backend, and Step 4's unconditional recording would widen the deadlock to every `log_*` call.

---

## Appendix — open questions (RESOLVED by human, 2026-07-02)
1. **PR shape:** ✅ **Two stacked PRs**, as planned (PR-A = M3 steps 1–6; PR-B = M10a steps 7–8 stacked on PR-A).
2. **`info`→file level:** ✅ **`log_info→INFO` default**, as planned (complete record; drop to WARNING via `STEALTH_MCP_LOG_LEVEL` if a session gets too noisy). No open decisions remain — Stage 3 may execute this plan.
