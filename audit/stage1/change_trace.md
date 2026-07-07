# Stage 1 — Empirical Change-Trace

Pinned SHA: `2267b83` (branch `fix/singleton-version-aware-backend`). Repo read-only; this document and `findings_change_trace.json` are the only outputs.

Three invented, realistic feature requests are traced end-to-end against the actual source (not guessed): a SMALL single-behavior tweak, a MEDIUM new-tool-end-to-end capability, and a CROSS-CUTTING change chosen specifically to stress the architecture (per-session log correlation, since the backend is a version-gated singleton process shared across concurrent MCP sessions).

---

## Request 1 (SMALL) — Add a `locale` option to `spawn_browser`

**Ask:** Agents can already override `timezone_id` on `spawn_browser` (applied post-launch via CDP). Add an analogous `locale: Optional[str] = None` (e.g. `"en-US"`) applied via CDP `Emulation.setLocaleOverride`, so `navigator.language`/date-formatting/anti-bot fingerprint checks can be controlled the same way timezone already is.

**Touch list, in order:**

1. `src/stealth_chrome_devtools_mcp/embedded/models.py` — `BrowserOptions` class (~line 85): add `locale: Optional[str] = Field(default=None, ...)`.
2. `src/stealth_chrome_devtools_mcp/embedded/server.py` — `spawn_browser()` tool wrapper (line 1306): add `locale: Optional[str] = None` kwarg + docstring `Args:` entry, and pass it into the `BrowserOptions(...)` construction inside the `for spawn_attempt in range(3):` retry loop.
3. `src/stealth_chrome_devtools_mcp/embedded/browser_manager.py`:
   - `_apply_timezone_override` (line 117) is the direct template for a new sibling `_apply_locale_override(tab, locale)` (calls `uc.cdp.emulation.set_locale_override(...)`), added near it.
   - `BrowserManager.spawn_browser` (line 311, runs to line 547 — see F-208) — add the new call alongside the existing, verified `_apply_timezone_override` invocation (browser_manager.py:438-440).
   - `_build_spawn_diagnostics` (line 93) already echoes `timezone_id` (lines 98, 109) into spawn diagnostics; parity means deciding whether `locale` should be echoed too.
4. **Conditional** — `extra_headers`/`Accept-Language`: if "locale" is meant to also affect the HTTP header (not just `navigator.language`), the `spawn_browser` wrapper in server.py needs to merge that into `extra_headers` before `network_interceptor.setup_interception(...)` is called — a second, easy-to-miss mechanism, because CDP emulation and HTTP headers are unrelated code paths that happen to both need touching for "locale" to be fully consistent.

**Files/modules touched:** 3 required (`models.py`, `server.py`, `browser_manager.py`); +1 conditional (`network_interceptor.py`, only if header consistency is in scope).

**Dragged-in unrelated code:** `BrowserManager.spawn_browser` is a single ~236-line method (browser_manager.py:311-547) that already interleaves `BrowserInstance` construction, proxy config/forwarder startup, platform/executable detection, idle-timeout resolution, stealth-arg merging, `uc.Config`/`uc.start()` launch, CDP post-launch overrides, process-cleanup tracking, and a 3-attempt retry/fallback loop driven from the caller. Verified (browser_manager.py:330-340): proxy config, platform info, and idle-timeout resolution are already crammed into the first 10 lines of the `try:` block — there is no isolated "post-launch CDP overrides" step to hook into; finding the correct insertion point means reading most of the method. See finding F-208.

**Test protection:** `tests/test_stealth_args.py` covers launch-arg construction at the unit level (mocked), but nothing exercises `_apply_timezone_override`/CDP emulation calls without a real browser — that requires the `integration` marker (`tests/test_browser_integration.py`, needs Chrome installed). A locale-override bug (wrong CDP method/param, or wrong ordering vs. tab-ready) passes every non-integration test and only surfaces manually. Nothing asserts `BrowserOptions` fields and `spawn_browser()`'s kwargs stay in sync — they are two hand-maintained parallel parameter lists today (verified: the pydantic model in models.py and the plain-kwarg tool signature in server.py both separately list `headless`, `user_agent`, `viewport_width`, `viewport_height`, ...) — so a forgotten pass-through is a silent no-op, not a test failure.

---

## Request 2 (MEDIUM) — New tool `download_response_body`: save a captured network response body straight to a file

**Ask:** `get_response_content` (server.py:2129) returns a captured response body inline (UTF-8 text, or base64 for binary) via `network_interceptor.get_response_body(tab, request_id)` (network_interceptor.py:334) — expensive/lossy for large binary payloads and forces the MCP client to persist it itself. Add a tool that writes the body directly to a file and returns the path, the way `take_screenshot` does for images.

**Touch list, in order:**

1. `src/stealth_chrome_devtools_mcp/embedded/network_interceptor.py` — reuse `NetworkInterceptor.get_response_body(tab, request_id)` (line 334) unchanged.
2. **The actual crux of this trace: choosing a file-write convention.** Three non-unified precedents already exist in this codebase for "write an artifact to disk," verified by direct inspection:
   - `response_handler.py`: `default_clone_output_dir()` (line 11) resolves `~/.stealth-mcp/element_clones`, override via `STEALTH_MCP_CLONE_OUTPUT_DIR`; consumed automatically by `ResponseHandler.handle_response()`'s size-based fallback-to-file.
   - `file_based_element_cloner.py`: `FileBasedElementCloner.__init__` (line 29) calls the *same* `default_clone_output_dir()` for its default, but then owns an independent `_generate_filename`/`_save_to_file` pair (lines 59, 137) to actually write JSON — partial reuse, partial reimplementation.
   - `take_screenshot` (server.py:1978) uses neither: no `file_path` → `tempfile.NamedTemporaryFile`; caller-supplied `file_path` → written as-is, with **no** per-user-directory enforcement at all (verified, server.py:2003-2007). See finding F-205.
   A new tool modeled on the nearest sibling by name (`get_response_content`, which returns inline data) has no single blessed "write bytes to the right place" helper to call; the closest analogous "save an artifact" tool (`take_screenshot`) is the least consistent precedent of the three.
3. `src/stealth_chrome_devtools_mcp/embedded/server.py`:
   - New `async def download_response_body(instance_id, request_id, file_path: Optional[str] = None)`, decorated `@section_tool(...)`. Real judgment call: `"network-debugging"` (10 existing tools, verified) or `"file-extraction"` (9 existing tools, verified) both fit equally well.
   - Resolve `tab` via `browser_manager.get_tab(instance_id)`; wrap the body fetch in `_with_cdp_timeout` (existing convention, e.g. server.py:2146).
   - Must not repeat the `handle_response` await-mismatch bug (F-202) if the new tool reuses `response_handler` for any part of its response shaping.
4. `tests/` — a new test file. Existing precedent is `tests/test_clone_output_dir.py` and `tests/test_element_cloner_output_dir.py` — **two separate existing test files** for what is conceptually one concern (output-directory resolution) applied to two different cloners; a third, parallel file is the path of least resistance rather than one shared test asserting all file-writing tools honor `STEALTH_MCP_CLONE_OUTPUT_DIR` consistently.
5. Bookkeeping: `tests/test_server_call_conventions.py`'s docstring hardcodes "all 96 tools"; `SECTION_TOOLS` totals (verified via grep: browser-management 8, cdp-functions 13, cookies-storage 3, debugging 7, dynamic-hooks 10, element-extraction 9, element-interaction 12, file-extraction 9, network-debugging 10, progressive-cloning 10, tabs 5 — sums to 96) shift by one. Nothing regenerates or enforces this count; it is prose that a human must remember to update.

**Files/modules touched:** 2 required source files (`network_interceptor.py` read-only reuse, `server.py` new tool) + 1 new test file = 3; plus a real, currently-unresolved design decision spanning 3 *existing but disagreeing* files that must be read (not modified) to make that decision.

**Dragged-in unrelated code:** none of the 5 element-cloner files need code changes, but discovering "how does this codebase normally persist an artifact" requires reading parts of `response_handler.py`, `file_based_element_cloner.py`, and `take_screenshot`'s body in `server.py` to learn they disagree — there is no doc or single source of truth that says so up front.

**Test protection:** `tests/test_response_handler.py` (74 lines) and `tests/test_network_interceptor.py` (118 lines) are real no-mock unit suites for the two subsystems being composed — good baseline. But neither exercises the *combination* (response bytes flowing through to a file on disk), so the actual integration point this new tool creates is untested by anything already in the suite and needs a new test written from scratch, with no existing fixture to model it on beyond the two output-dir tests noted above.

---

## Request 3 (CROSS-CUTTING) — Per-session log correlation

**Ask:** The backend is a version-gated **singleton** process (this branch's own theme) that can serve multiple concurrent MCP client sessions from one running process. Operators debugging "session X is misbehaving" cannot filter logs to one session today, and — more seriously — errors are de-duplicated **globally** across all sessions, so a genuine, session-specific recurring failure can be silently swallowed the moment any other concurrent session hits a same-shaped error first. Add an `instance_id`/session dimension to `DebugLogger` and thread it through.

**Touch list, in order:**

1. `src/stealth_chrome_devtools_mcp/embedded/debug_logger.py` — `DebugLogger` class: extend `log_info`/`log_warning`/`log_error` with `instance_id: Optional[str] = None`; extend the `_seen_errors` dedup key (line 76, verified: `f"{component}.{method}.{type(error).__name__}.{str(error)}"` — no instance dimension today) to include it; extend stored entry shape; decide whether the `_stats` counters (also global today) become per-instance.
2. Every one of the **287 verified `debug_logger.*` call sites across 15 files** needs an `instance_id=` threaded in wherever one is in scope (usually is — most call sites sit inside methods that already received `instance_id` or `tab`). Verified per-file counts: `browser_manager.py` 44, `process_cleanup.py` 34, `dynamic_hook_system.py` 34, `element_cloner.py` 28, `cdp_function_executor.py` 24, `file_based_element_cloner.py` 20, `comprehensive_element_cloner.py` 14, `response_stage_hooks.py` 13, `cdp_element_cloner.py` 11, `dom_handler.py` 11, `dynamic_hook_ai_interface.py` 10, `network_interceptor.py` 8, `progressive_element_cloner.py` 3, `proxy_forwarder.py` 3, `server.py` 30.
3. `server.py`'s own calls inside `app_lifespan` (line 1231, server startup/shutdown) have no `instance_id` in scope — they're process-lifecycle, not session events — so the new parameter must stay optional everywhere, which means nothing *forces* the other ~270+ in-scope call sites to actually pass it. An AST-level guard analogous to `tests/test_server_call_conventions.py` (which already proved this team will write such guards when a convention matters) would be needed to keep this from rotting on the next PR; none exists for this today.
4. Every other module-level singleton holding shared state is a candidate for the same question: `persistent_storage` (InMemoryStorage) already keys by `instance_id` (browser_manager.py:480) and is the one subsystem actually ready for this; `dynamic_hook_system`/`hook_learning_system` are not instance-scoped today (a hook registered via `create_hook` applies across all instances unless its own `matches()` predicate filters — a data problem, out of scope for a logging change, but discovered while tracing this request).
5. `tests/test_debug_logger.py` (109 lines) needs new assertions for per-instance dedup; any other unit test that currently asserts on `debug_logger` call arguments needs signature updates (not fully enumerated here — would need a repo-wide sweep).

**Files/modules touched:** 16 minimum to keep the call convention consistent (`debug_logger.py` + the 15 files with existing call sites); realistically most of the 287 call sites individually if implemented the "obvious" way (mechanically adding a kwarg), versus adopting a `contextvars.ContextVar` set once per spawned browser task to avoid the 287-site edit — a real architectural alternative, but one this codebase has made no prior use of anywhere in `embedded/` (verified: no `contextvars` import exists in the package today).

**Dragged-in unrelated code:** this trace is the one that best exposes the architecture. Every file in `embedded/` except `models.py`, `persistent_storage.py`, `proxy_utils.py`, and `platform_utils.py` calls `debug_logger` directly, because logging was added ad hoc per call site rather than via a per-instance logger object handed down from `spawn_browser`. There is no "logger factory" / "bound logger" concept anywhere — `debug_logger` is imported as the same single global instance in every file, never instantiated per instance (verified: `debug_logger = DebugLogger()` is defined exactly once, debug_logger.py:498).

**Test protection:** none of the 34 test files assert anything about log *correlation* — `test_debug_logger.py` tests `DebugLogger` in isolation, and `tests/test_singleton_version_aware.py`/`tests/test_singleton_fast_handshake.py` (the two tests that actually model "multiple sessions, one backend") test proxy/handshake behavior, not logging. Separately — and this affects manual verification of *all three* traced requests, not just this one — the singleton's reuse-vs-restart decision is keyed on installed **package version** metadata, not on `embedded/*.py` content (see finding F-206): a developer iterating across 287 call sites in 15 files, manually testing between edits, keeps talking to the pre-edit backend process unless the version is bumped or the process is killed each time. None of the 34 test files catch this failure mode because they `import server` in-process (verified via `grep -l 'import server' tests/*.py`), bypassing the singleton/proxy entirely.

---

## Cross-request observations

- Two of three traces (SMALL, MEDIUM) funnel through `server.py`, which is simultaneously the file holding all 96 `@mcp.tool` registrations *and* ~500 lines of unrelated clone-storage-eviction filesystem logic (F-201) — a maintainability tax paid on nearly every future change regardless of which trace it resembles.
- All three traces surface a version of the same shape: a piece of shared/global state (element-cloner logic, file-output convention, or debug-log storage) was solved 2-5 times independently instead of once, because there was never a single place that was obviously "the" place to extend (F-203, F-204, F-205, F-207).
- The singleton backend's version-string-gated reuse (F-206) is not caught by the request itself but by the *process of manually verifying* any of the three changes — it is a standing tax on development velocity that applies uniformly regardless of which trace a developer is working.
