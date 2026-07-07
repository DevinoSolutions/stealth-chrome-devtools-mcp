# Deep Dive DD-2: Singleton / Global State / Version-Awareness / Storage Durability

## 0. Verdict up front

**Does the branch's version-aware backend mechanism actually work? YES, for what it explicitly targets — and NO for what the user actually experiences day to day.**

`singleton.py`'s version check (`_find_running_server` → compares `state["version"]` against `_server_version()`) correctly and verifiably prevents a **different installed package version** from silently reusing a stale backend. This is issue #14 as scoped, and `tests/test_singleton_version_aware.py` proves the eviction logic (lock → detect mismatch → terminate stale pid → start fresh) is correct via 15+ targeted unit tests plus one real-subprocess end-to-end test.

But `_server_version()` is `importlib.metadata.version("stealth-chrome-devtools-mcp")` — it reads **frozen distribution metadata written at the last `pip install -e .`**, not the content of `server.py`. `pyproject.toml:7` pins a static `version = "1.2.0"` (no `dynamic = [...]`, no `setuptools_scm`/`hatch-vcs`). Confirmed against the live checkout: `pip show` reports this exact repo as an editable install at version `1.2.0`, the on-disk `.dist-info/METADATA` says `Version: 1.2.0`, and `~/.stealth-mcp/server.json` right now records a **live backend (pid 104504) at version "1.2.0"** — the same version string the checkout would still report after any uncommitted source edit (confirmed: `git status` shows `M src/stealth_chrome_devtools_mcp/embedded/server.py` uncommitted right now, and that edit changes nothing `_server_version()` can see).

So: editing `server.py` and reconnecting the MCP client does **not** get you fresh code. `ensure_server_running()` → `_find_running_server()` sees the same version string, calls the existing backend healthy, and proxies to it — the pre-edit process, still running the pre-edit bytecode. The only ways to force a fresh backend are (a) manually kill the backend process, or (b) bump `pyproject.toml`'s version *and* reinstall so the dist-info regenerates. Neither is automated. This is exactly the friction the user has already identified from experience; this audit confirms the mechanism and explains precisely why.

## 1. Import-time instantiation inventory

Exactly 15 module-level singletons construct unconditionally at import, matching the brief's estimate:

| # | Name | File:line |
|---|---|---|
| 1 | `browser_manager` | `server.py:1297` |
| 2 | `network_interceptor` | `server.py:1298` |
| 3 | `dom_handler` | `server.py:1299` |
| 4 | `cdp_function_executor` | `server.py:1300` |
| 5 | `persistent_storage` | `persistent_storage.py:95` |
| 6 | `dynamic_hook_system` | `dynamic_hook_system.py:522` |
| 7 | `dynamic_hook_ai` | `dynamic_hook_ai_interface.py:343` |
| 8 | `element_cloner` | `element_cloner.py:648` |
| 9 | `process_cleanup` | `process_cleanup.py:1023` |
| 10 | `progressive_element_cloner` | `progressive_element_cloner.py:263` |
| 11 | `comprehensive_element_cloner` | `comprehensive_element_cloner.py:344` |
| 12 | `response_handler` | `response_handler.py:123` |
| 13 | `debug_logger` | `debug_logger.py:498` |
| 14 | `hook_learning_system` | `hook_learning_system.py:566` |
| 15 | `file_based_element_cloner` | `file_based_element_cloner.py:648` |

11 of 15 live in their own dedicated module (importable/constructible independently). 4 (`browser_manager`, `network_interceptor`, `dom_handler`, `cdp_function_executor`) are declared bare in the 4200+-line `server.py` itself, entangled with the FastMCP app object and every tool definition. This asymmetry is exactly why `hot_reload()` (below) can only ever patch those 4 — it re-binds `global` names in server.py's own namespace, which it cannot do for singletons that live inside another module and are imported via `from x import y` elsewhere.

Construction is not always inert:
- `ResponseHandler.__init__` / `FileBasedElementCloner` create their output directory on disk at import time (`response_handler.py:44`, `self.clone_dir.mkdir(parents=True, exist_ok=True)`) — confirmed independently by `tests/conftest.py`'s own comment explaining why it has to pre-set `STEALTH_MCP_CLONE_OUTPUT_DIR` before any import happens.
- `ProcessCleanup.__init__` (`process_cleanup.py:31-55`) registers `atexit`/`SIGTERM`/`SIGINT` handlers **and runs real orphan-browser-process recovery** by default — see F-124.

## 2. hot_reload(): the mechanism that looks like the fix, and isn't

`server.py:2975-3009` defines an MCP tool literally named `hot_reload`, in the `"debugging"` section, whose docstring promises "Hot reload all modules without restarting the server." It:

1. Only reloads 5 modules: `browser_manager`, `network_interceptor`, `dom_handler`, `debug_logger`, `models`. It never reloads `server.py` itself (where the actual tool implementations live) or `cdp_function_executor.py` — one of the *other three* singletons declared in the very same block as `browser_manager` at `server.py:1297-1300`, silently excluded with no comment.
2. For the modules it does reload, `browser_manager = BrowserManager()` (line 2997) constructs a **brand-new instance with an empty `_instances` dict**, discarding every live browser session, `_spawn_diagnostics` entry, `_proxy_forwarder`, and the running `_idle_reaper_task` (which is never cancelled — it keeps running, closed over the *old*, now-orphaned `self`). Any active browser session becomes silently unreachable (`get_instance`/`navigate`/`close_instance` all report "not found") while the real Chrome process and idle-reaper task keep running headless.
3. Zero test coverage: `grep -rn hot_reload tests/` returns no matches anywhere in the suite.

Net effect: the one tool purpose-built to answer "can I apply an edit without a full restart" cannot reload the file that most commonly changes, silently skips a sibling singleton, corrupts live state for what it does reload, and is entirely untested. It does not close the gap identified in §0.

## 3. Storage durability: `persistent_storage.py`

`InMemoryStorage` (`persistent_storage.py:4-95`) is a plain `Dict` guarded by `threading.RLock` — **zero disk I/O anywhere in the class**. It is not persistent in any sense; the name is a pure misnomer (the class itself is honestly named `InMemoryStorage`; the *module* and *variable* are not).

This turns out not to be an accidental bug so much as an unlabeled one: the codebase's own shutdown path treats it as scoped to the current process. `server.py:1260-1276` (`app_lifespan` teardown) explicitly calls `persistent_storage.clear_all()` on every graceful shutdown, logging "Clearing in-memory storage ... In-memory storage cleared." The code does not expect or rely on this surviving a restart anywhere — it's used as a secondary "instances storage still thinks exist but memory doesn't" cross-check inside `list_instances()` (`server.py:1422-1439`), not as a source of truth. So: everything in it is lost on every restart (including every version-driven eviction this branch adds), there is no cross-backend version isolation mechanism because there is no cross-backend persistence medium at all — trivially "isolated" only because nothing is shared. The risk is purely one of naming/expectation: a maintainer (or, concretely, this audit's own brief) reading "persistent_storage" reasonably assumes durability that was never designed in.

## 4. Thread/async safety

The codebase is consistently `asyncio`-based (`browser_manager.BrowserManager._lock`, `dynamic_hook_system.DynamicHookSystem._lock` are both `asyncio.Lock`, not OS threading locks — appropriate for the concurrency model in play). Discipline is mostly good in `BrowserManager` (dict mutations for `_instances` are consistently under `self._lock`; reads-then-later-use across `await` boundaries, e.g. in `get_navigation_tab`, degrade gracefully via explicit target-id re-validation rather than crashing).

`DynamicHookSystem` is the concrete counter-example: `self._lock = asyncio.Lock()` (line 169) is used by `create_hook` (line 353) and `remove_hook` (line 409) to mutate `self.instance_hooks`, but `add_instance`/`remove_instance` (lines 426-433) — plain synchronous `def`, called from `browser_manager._setup_dynamic_hooks` and `_discard_instance_unlocked` on every browser spawn/close — mutate the **same dict** with no lock at all (structurally cannot, since they're sync). Worse, the actual hot path — `setup_interception` and `_process_request_hooks`, invoked once per intercepted network request/response via `tab.add_handler(..., lambda event: asyncio.create_task(...))`, i.e. as an independent concurrent task per event — read `self.hooks`/`self.instance_hooks` without ever acquiring the lock. The lock exists and is correctly used by two of six touch points; the other four (including the highest-frequency one) bypass it entirely, so it provides no actual guarantee for the scenario it names in the docstring.

## 5. Testability / reset

No factory, DI seam, or reset hook exists for any of the 15 singletons — they are constructed once, at first import, for the lifetime of the interpreter. `tests/conftest.py` works around this rather than through it: it sets `STEALTH_MCP_CLONE_OUTPUT_DIR` *before* any import can happen (to redirect the two singletons that touch disk at construction), but never resets `browser_manager`, `persistent_storage`, `dynamic_hook_system`, etc. between tests. Individual test files (e.g. `test_process_cleanup.py`) sidestep the shared singleton by constructing their own fresh `ProcessCleanup()` per test — a reasonable pattern for testing the *class*, but it means the module-level singleton's own state (constructed once, for real, the moment the module is first imported by any test file) is never exercised or reset, only quietly ignored.

That first real construction is not harmless. `ProcessCleanup.__init__` (`process_cleanup.py:31-55`) runs `_recover_orphaned_processes()` by default — real `psutil`-based process matching and killing against `~/.stealth_browser_pids.json`, a real file in the developer's home directory, not sandboxed to any test. The only opt-out is `STEALTH_MCP_NO_AUTO_RECOVERY=1`, which `src/stealth_chrome_devtools_mcp/cli.py:35` sets proactively for the CLI's read-only `doctor` command, and which exactly one test (`test_singleton_fast_handshake.py:298`, for a subprocess it spawns) remembers to set — but `tests/conftest.py`, which every one of the ~30 test files shares, does not set it for the pytest process itself. See F-124.

## 6. Findings

6 findings, F-120..F-125, written to `audit/stage1/findings_dd2.json`:

- **F-120** (High): version-aware reuse only detects installed-metadata version changes, not source edits — confirms and explains the user's known issue.
- **F-121** (High): `hot_reload()` doesn't cover `server.py` or `cdp_function_executor.py`, and destroys live browser-session state for what it does reload; zero test coverage.
- **F-122** (Medium): `persistent_storage.py` is a pure in-memory misnomer; the codebase's own shutdown path already treats it as such.
- **F-123** (Medium): `DynamicHookSystem`'s `asyncio.Lock` is bypassed by 4 of 6 touch points, including the highest-frequency one (live request/response interception).
- **F-124** (High): `ProcessCleanup()` runs real orphan-process recovery (capable of killing real processes, deleting real profile dirs) at import time, gated by an opt-out env var the shared test fixture layer never sets.
- **F-125** (Medium): all 15 singletons construct unconditionally at import with no reset/DI seam; tests work around this rather than through it.
- **F-126** (Medium, lower confidence — inferred from documented OS/psutil semantics, not runtime-observed): the eviction path's `proc.terminate()`/`proc.kill()` (`singleton.py:200-208`) maps to Windows `TerminateProcess()`, which bypasses the app's own `SIGTERM` handler and graceful `app_lifespan` shutdown chain — every version-driven eviction on this platform likely relies on the *next* backend's orphan-recovery pass (F-124's mechanism) rather than a clean handoff.
