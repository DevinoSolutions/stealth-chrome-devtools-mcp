# CLAUDE.md — navigation map for agents

You are placing a change in `stealth-chrome-devtools-mcp`. This file is your map:
**where** things live, **what** each term means, and the **four conventions** a change
here must follow. It is name-only on purpose — you should be able to route a change to
the right file from this map *without reading function bodies*. The *why* behind the
architecture is in [`DESIGN.md`](./DESIGN.md); how to operate the backend is in
[`RUNBOOK.md`](./RUNBOOK.md); how to build/test/ship is in
[`CONTRIBUTING.md`](./CONTRIBUTING.md).

> This is a **local, single-user tool, 0 external users**. Priorities:
> maintainability, operability, performance.

---

## The four conventions (non-negotiable)

1. **One import form.** Always `from stealth_chrome_devtools_mcp.embedded.X import Y`
   (absolute-from-package; relative imports are banned). **No module under `embedded/`
   imports `server`** — it causes double tool-registration under runpy; pass
   `browser_manager` as an argument instead. See [DESIGN §8](./DESIGN.md#8-the-one-import-convention).
2. **One error convention.** Tools **raise** `tool_errors.ToolError` /
   `InstanceNotFoundError` on failure; success helpers return values. Do not add a
   `{"success": False}` dict — except to join a named KEEP contract
   ([DESIGN §9](./DESIGN.md#9-the-one-error-convention)).
3. **One cloner engine.** All DOM extraction lives in
   `embedded/cdp_element_cloner.py` (`CDPElementCloner`). The file/progressive cloners
   are thin adapters; never add extraction anywhere else, and never resurrect the
   deleted engines ([DESIGN §5](./DESIGN.md#5-the-cloner-subsystem-one-engine-deliberate-per-aspect-transport)).
4. **Golden discipline + "a second way is a defect."** Two-tier goldens: HARD
   invariants never bend; SOFT goldens update *deliberately*, in the same PR that
   changes a schema, with justification (see `CONTRIBUTING.md`). And the binding lens:
   **a change that introduces a second way to do something already done is a defect** —
   prefer extending the one home over adding a parallel path.

---

## Navigation map (the tree as it is)

Package root: `src/stealth_chrome_devtools_mcp/`. Two console scripts (`pyproject.toml`
`[project.scripts]`): `stealth-chrome-devtools-mcp` → `server:main` (the MCP server),
`stealth-chrome-devtools` → `cli:main` (the ops CLI).

### Top-level

| File | Owns |
|---|---|
| `server.py` | thin entrypoint — loads `embedded/server.py` as `__main__` via `runpy` (`main()` shim); its stdio branch is also **THE one place the PROXY process bootstraps its own observability** (`configure_logging("proxy")` + `_start_proxy_error_reporting`, F-827) — in the branch, never at the top of `main()`, or the runpy path would double-init |
| `__main__.py` | `python -m stealth_chrome_devtools_mcp` → `server.main()` |
| `cli.py` | the `stealth-chrome-devtools` ops CLI verbs (`status`/`doctor`/`stop`/`restart`/`cleanup`/`kill-orphans`/`serve`) |
| `settings.py` | **the one env home** — pydantic `Settings` + `get_settings()`; every `STEALTH_MCP_*` knob is a typed field here |
| `observability.py` | Sentry error shipping — hardcoded DSN, on by default, never raises (no-op under `STEALTH_MCP_NO_ERROR_REPORTING`); **the one PII scrubber** — `_scrub_event` (Sentry's `before_send`); **the one non-exception report** — `capture_lifecycle` (F-827: proxy transitions that are decisions, not crashes) |

### `embedded/` — the backend

**Lifecycle & transport**
| File | Owns |
|---|---|
| `server.py` | the real MCP server — the **33 tool bodies not yet moved** (the `cookies-storage`, `tabs`, `debugging`, `dynamic-hooks`, `progressive-cloning`, `network-debugging`, `file-extraction` and `element-extraction` bodies live in `tool_sections/` as of plan_SERVERSPLIT slices 1-8) + `app_lifespan` + `mcp`/`registry` + the **binding loop** that drives registration from THIS file's module body, once per execution of it (so the canonical import, the bare-name spec load and the runpy `__main__` load each get a full 94-tool app — a section module that decorated itself would register into the first execution only). The shared substrate the bodies reach for lives in `tool_runtime.py` |
| `singleton.py` | **backend lifecycle + the stdio proxy** — liveness probes (`_backend_http_ready`, `_probe_backend_status`), port selection (`_select_backend_port`, `DEFAULT_PORT`), the one identity+readiness reuse gate (`_same_identity_backend_ready`, `_source_fingerprint`, `REUSE_PATIENCE_SECONDS` — which spends its patience through `scheduling_lag.FairWindow`, F-856), cold-start lock (`_start_backend_holding_lock`), `run_stdio_proxy`. The `server.json` record moved to `backend_registry.py` (path names re-exported here for legacy callers); the proxy's *recovery from a dead backend* moved to `proxy_selfheal.py`; the watchdog LOOP moved to `backend_watchdog.py` — `_watch_backend_liveness` stays here as the wiring that knows which probes are ours |
| `backend_watchdog.py` | **THE one home for the proxy's mid-session liveness watchdog** — the SLOW witness (`watch_liveness`): F-820's strikes plus the confirmation phase. A leaf: both probes arrive as arguments, so it never imports `singleton` and the dead-vs-busy policy stays single-homed in the reuse gate |
| `scheduling_lag.py` | **THE one home for "was this process scheduled fairly, and what does a time budget owe it when it was not"** (F-856) — `FairWindow`, whose budget is charged in fair seconds (elapsed ÷ the lag its own naps measured), plus `MAX_STRETCH`, `REPORT_FACTOR` and the `proxy: patience extended under starvation` lifecycle report. It never decides alive-or-dead: only "has this window been spent", so `proxy_selfheal`'s ONE heal path is untouched. A leaf; the `_now`/`_wait` module functions are its single timing seam |
| `serve_startup.py` | **THE one home for "startup work that must not delay the backend's first serve"** (F-856) — `after_serving`, which runs one idempotent, best-effort startup job on a daemon thread. Its docstring carries the safety argument for reaping orphans CONCURRENTLY with serving. Deliberately not a general background-task runner: `clone_storage.spawn_background_sweep` keeps its own asyncio task, dedupe and trigger-time root capture |
| `proxy_selfheal.py` | **THE one home for "the backend under this proxy died — heal in place instead of exiting"** (F-838) — the backend-generation loop (`drive`, `_one_generation`), the bounded recovery (`heal_backend`, `HEAL_ATTEMPTS`, `MAX_CONSECUTIVE_HEALS`) and the in-flight-call failure report (`PendingCalls`). Also **THE one home for "which witness saw this death"** (F-843) — the generation-end causes (`WATCHDOG_CAUSE` / `CONNECTION_LOST_CAUSE` / `CONNECTION_RESET_CAUSE`) and `_confirm_bridge_verdict`, which settles dead-vs-busy for a bridge-first death with the SAME identity+readiness gate the watchdog uses (handed in as `confirm_alive`); the cause is reported, never branched on — there is one heal path. Also the home for **three of F-827's four proxy lifecycle reports** (`CONDEMNED_EVENT` / `HEALED_EVENT` / `TEARDOWN_EVENT` via `_report`, which only ever calls `observability.capture_lifecycle`); the fourth (eviction) is reported from its own site in `singleton.py`. A leaf: it imports no other embedded module and takes the startup path (`ensure_server_running`) as an argument, so the reuse gate, adoption order and cold-start lock — including the herd's serialization — stay single-homed in `singleton.py`. Never raises |
| `backend_registry.py` | **THE one home for the `server.json` record** — schema v2 (`SCHEMA_VERSION`, one entry per display context), read/write/clear (`read_backends`, `record_backend`, `forget_backend`, `clear_record`), the adoption order (`adoption_candidates`, `window_capable_first`), per-context port lookup (`port_for_context`, `own_or_first_port`, `port_conflict`), the entry normalizers (`read_record`, `first_backend`, `backend_on_port`, `recorded_int`, `fingerprint_mismatch` — the one reading of a recorded source fingerprint, F-829), plus `STATE_DIR`/`SERVER_STATE_FILE`/`PORT_FILE` |
| `display_context.py` | **THE one home for "can a window launched by THIS process be seen, and on which desktop"** — the opaque token (`display_context()`: `headless` / `unverified` / `win-session-N` / `wayland-…` / `x11-…` / `aqua-<uid>`) and `can_show_windows()`. Observational only: it never picks or enters another session (F-808) |
| `desktop_launch.py` | **THE one home for "hand a launch to the logged-on user's desktop"** (F-810, Windows only) — the availability probe (`available`, `_active_console_session_id`), the two predicates the guard and the spawn pipeline consult (`can_deliver_headed_window`, `should_delegate`), the Task Scheduler round trip (`launch_and_attach`, `_schtasks`) and the attached-browser process shim (`pid_shim`). The OS places the process; the tool still never picks a session |
| `browser_pid_registry.py` | **THE one home for `browser_pids.json`** — its schema, the owner stamp on every entry (`owner_pid`, `owner_create_time`), and the read-merge-write protocol every writer shares |
| `tool_registry.py` | `SECTION_TOOLS` + `ToolRegistry.section_tool` (registration, section gating, correlation-id stamping, the surrogate-safe return wrap) + the canonical **verb taxonomy** (module docstring) |
| `tool_runtime.py` | **THE one home for what a tool body reaches for beyond its own arguments** (plan_SERVERSPLIT slice 0) — the four constructed singletons (`browser_manager`, `network_interceptor`, `dom_handler`, `cdp_function_executor`), the re-exported module singletons, the four tuned knobs (`CDP_OPERATION_TIMEOUT`, `MAX_TIMEOUT_MS`, `EXECUTE_SCRIPT_TIMEOUT`, `MAX_USER_SCRIPT_BYTES`) and the guards that enforce them (`_BLOCKING_SCRIPT_PATTERNS`, `_script_rejection_reason`, `_clamp_timeout`, `_with_cdp_timeout`). A leaf — it imports no `mcp`, no `registry`, no `server`. Being a normal module it is loaded ONCE, unlike `server.py`, so a body that resolves `rt.<name>` at call time has exactly ONE patchable home (`tests/conftest.py`'s `patched_server`); `__all__` states that surface. **Migration truth: the bodies still in `server.py` reach these as bare names** through a migration alias import there, deleted in slice 12; a body that has moved into `tool_sections/` reaches them as `rt.<name>` |
| `tool_sections/` | the subpackage the 94 tool bodies move into, one module per `SECTION_TOOLS` section. `__init__.py` owns the contract (a section module exports only `SECTION` + `TOOLS`, **never** decorates, **never** imports `mcp`/`registry`/`server`, resolves everything as `rt.<name>` at call time) and `SECTION_MODULES`, the one enumeration `server.py`'s binding loop and `tests/source_scan.py` both walk. **Migration truth after slice 8: `cookies_storage.py` (3 tools), `debugging.py` (5), `tabs.py` (5), `dynamic_hooks.py` (10), `progressive_cloning.py` (10), `network_debugging.py` (10), `file_extraction.py` (9), `element_extraction.py` (9).** A module owns a WHOLE section, which is why `debugging.py` also holds `validate_browser_environment_tool` — filed among the element-extraction bodies in `server.py`, registered into `debugging` all along — and why `element_extraction.py` holds `extract_complete_element_cdp`, filed among the file-extraction bodies and registered into `element-extraction` all along. `dynamic_hooks.py` is the only module with SYNCHRONOUS bodies, which is what proves `tool_registry._surrogate_safe_returns`' sync branch survives the move. A section module also owns any constant only its own bodies read, which is why `_CAPTURE_OFF_NOTE` lives in `network_debugging.py`. The other three sections are still in `server.py` and arrive one slice at a time; `tests/source_scan.py`'s floor ratchets UP by one with each, so a dropped module reads as a collapsed set rather than a smaller one |
| `tool_errors.py` | the error convention — `ToolError`, `InstanceNotFoundError`, `_require_tab`, `_require_browser` |
| `logging_setup.py` | the observability spine — `resolve_log_dir`, `configure_logging`, `with_correlation_id`, `CorrelationIdFilter`; `with_correlation_id` is also **THE one place a failed tool call is recorded** (`_record_tool_failure` → `debug_logger.log_tool_failure`, F-835) — never add a per-tool `except` that logs; **the one home for log-file retention** — `roll_boot_log` (the launcher-side boot-log rotation, F-830) and `prune_old_logs` + its dead-backend post-mortem exemption (F-840); plus `backend_uvicorn_config` — the one home for the backend's uvicorn run-config (access logging off, graceful-shutdown timeout) |
| `process_cleanup.py` | orphan reaping — side-effect-free `__init__`, `activate()` at serve boundary (handlers armed synchronously; the REAP is handed to `serve_startup.after_serving`, F-856), `recover_orphans()` seam |
| `models.py` | pydantic data models (`BrowserInstance`, `BrowserState`, `NetworkRequest`, …) |
| `platform_utils.py` | OS-specific helpers |

**Browser & interaction**
| File | Owns |
|---|---|
| `browser_manager.py` | `BrowserManager` — spawn/list/close instances; `close_instance` offloaded teardown |
| `dom_handler.py` | DOM manipulation + element interaction |
| `element_resolution.py` | selector resolution that survives CDP document-node invalidation (route ALL selector resolution through here — never `tab.select`/`find` directly) |
| `proxy_forwarder.py` | authenticated egress-proxy forwarding + `_free_port` |
| `proxy_utils.py` | proxy string parsing + Chrome launch-arg helpers |
| `window_sizing.py` | **the one home for a spawn's requested window size** — the `--window-size` launch arg, the CDP `setWindowBounds` apply, and the post-launch measurement that makes the reported size truthful (F-804) |
| `spawn_contention.py` | **THE one home for "did this spawn fail because it was racing sibling spawns"** (F-834) — the in-flight threshold and the paragraph `contention_hint` appends to a failed spawn's error, including the explicit disclaimer that nodriver's root/`no_sandbox` advice does not apply. A sibling of `spawn_exhaustion`, not a fold-in: capacity and contention are different questions with different remedies. Both are appended at the ONE composition site in `browser_manager.spawn_browser`; the peak in-flight count they read is `BrowserManager._spawn_peak_in_flight` |
| `spawn_exhaustion.py` | **THE one home for "is this machine out of browser-process capacity, and what should the operator do about it"** (F-811) — the live Chromium-family count, the threshold (`_EXHAUSTION_PROCESS_THRESHOLD`), and the operator paragraph `exhaustion_hint` appends to a failed spawn's error. Its name matcher is deliberately narrower than `process_cleanup`'s ("how much of what WE spawn is running", not "may I kill this pid") — do not unify them. Never raises, never ships to Sentry |

**Cloner subsystem** (one engine + thin adapters + disk storage)
| File | Owns |
|---|---|
| `cdp_element_cloner.py` | **THE cloner engine** (`CDPElementCloner`) — the complete-element **clone schema** (its shape lives here, **not** in `models.py`) + every aspect (`styles` via CDP; `structure`/`events`/`animations`/`assets`/`related_files` via JS-eval). Every aspect **raises** `ToolError` on failure (F-858, convention 2); the only two `{"error": ...}` dicts left are EMBEDDED payload records, not tool returns — `extract_complete_element`'s per-aspect isolation and `_get_element_html`'s sub-field degradation, both named in `tests/test_cloner_error_convention.py` and gated there by AST |
| `file_based_element_cloner.py` | thin to-file adapter (`FileBasedElementCloner`, name KEPT) — owns `output_dir` only. A delegated extraction failure **propagates** (F-858): it used to be swallowed into a `{file_path, …}` answer with an empty summary, i.e. a file claiming to be a clone that failed |
| `progressive_element_cloner.py` | thin adapter (`ProgressiveElementCloner`) — `expand_*` slices from cached extraction; `_require_stored` is the one store-miss guard (raises, F-858) |
| `clone_storage.py` | on-disk **profile/clone quota + GC** (NOT extraction — see glossary "clone") |
| `aspect_options.py` | **THE one home for "a caller passed an option this aspect no longer has"** (F-851) — the retired-option table (`RETIRED`), the signature filter (`accepted`) and the per-aspect warning (`note`). A NAMED tolerance, never a widened `except`: a stale kwarg used to raise `TypeError` at the kwarg-binding site, which is OUTSIDE `gather`'s per-aspect isolation, so one retired string failed the whole complete clone |
| `js/` | the 7 browser-side extraction scripts (`extract_styles.js`, `extract_structure.js`, `extract_events.js`, `extract_animations.js`, `extract_assets.js`, `extract_related_files.js`, `comprehensive_element_extractor.js`) |

**Animations derivation** (six leaves downstream of the engine's animations
aspect; `cdp_element_cloner.extract_element_animations` -> `analyze()` is the ONE
call path in, so DOM extraction still has exactly one home). Reading order is the
dependency order — each imports only the ones above it, and none imports `server`.
| File | Owns |
|---|---|
| `animation_facts.py` | **THE one home for READING what the collector sent** — JSON field readers (`as_obj`/`as_rows`/`as_number`) and CSS token readers (comma lists + the list-cycling rule, `<time>`, keyframe selectors, easing classification, `specificity`, `prefers_reduced_motion`, `own_compound`). Also **THE one home for a derived value's confidence**: `Derived` welds a value to the confidence its own branch produced, and `claim`/`put` emit the pair or omit the field — a caller physically cannot supply a confidence it did not receive (F-850). Also **THE one home for the payload's shared value types** — `Facts`/`Record`, the `Caps` bounds with `caps_from`/`cap_message`, and `warn` (the one way a record grows a warning, so no site can hardcode `warnings: []`); the cap DEFAULTS are sized for a language model and justified from measurement in a comment there, never as `STEALTH_MCP_*` env knobs (F-853) |
| `animation_source.py` | **THE one home for WHERE a declaration lives** (F-849/F-857) — locating a rule's AUTHOR text in the sheet the collector captured (`rule_span`, `source_span`, `author_declaration`, `keyframe_span_for`; never Chrome's `cssText`, which is a re-serialization that matches nothing on disk), the `Span` that KEEPS the offset the slice was taken at plus `line_column`, the openable location (`open_location`: a linked sheet's href, else the DOCUMENT that contains the `<style>` — and nothing at all for a constructed sheet), and, when nothing is locatable, WHICH of the three causes it is (`missing_source_reason` / `pointer_reason` / `indirect_property` / `inline_declarations` / `blocked_stylesheet` — inline `style=""`, a stylesheet actually WITNESSED as cross-origin, or an adopted constructed sheet; never a guess). Turning a url into a path on disk stays with `extract_related_files`, the one URL→file answerer |
| `animation_edits.py` | **THE one home for EDIT RECIPES** (M10) — the per-knob cascade winner (`winning_rule`: `!important`, then specificity, then document order — degrading when a functional pseudo-class or a computed-value mismatch makes it undecidable, with `_mismatch_reason` deciding WHICH somewhere from the facts), and the token addressing (`token_verdict`, `_swap`, `_stamp_position`) that makes `find`/`replace` mechanically applicable without dropping the rest of the declaration. It composes recipes out of what `animation_source` returns; it never goes looking for a rule's text itself |
| `animation_advice.py` | trigger attribution, interaction/conflict warnings (closed code set), stagger grouping, and the `summary`/`overview` prose (templates over the payload, so they cannot contradict it) |
| `animation_waapi.py` | **THE one home for reading the live `Animation` objects** — timeline typing (`timeline_from_waapi`), live timing and keyframes, the records for everything the declared CSS does not describe (`build_waapi`: `element.animate()`, transitions, descendants, pseudo-elements), and the declared-vs-live reconciliation (`adopt_live_timelines`, which drops `duration_ms` for a scroll/view timeline). Declared CSS is `animation_analysis`; this is what is actually RUNNING |
| `animation_analysis.py` | **THE one home for the animations schema** — owns `analyze()`, the ONE composition site, plus per-animation records, keyframe resolution, derived timing, direction-aware checkpoints, the transition inventory, the `CLAIM_FIELDS`/`JUDGEMENT_BLOCKS` registry the confidence invariant test walks, the ONE editability verdict (`stamp_editable`: `editable` is "some recipe here applies", `not_editable` names the knobs that do not — a whole-record yes used to read as a promise about every knob, F-857), and the summary reduction for records the caller did not select (`summarize_detail`, which stamps a visible `detail_level` — never a silent shape difference between records) |

**Network, hooks, execution, storage, debug**
| File | Owns |
|---|---|
| `network_interceptor.py` | `NetworkInterceptor` — capture + the body caps (`_store_response`) |
| `dynamic_hook_system.py` | `DynamicHookSystem` — first-match-by-priority request hooks |
| `dynamic_hook_ai_interface.py` | AI-facing API for creating/managing hooks |
| `hook_learning_system.py` | hook examples/training surface |
| `cdp_function_executor.py` | direct JS function execution via CDP |
| `response_handler.py` | large-response handling + file fallbacks; **the one home for "can this payload survive the transport"** — `json_safe` (serializable, F-822) and `surrogate_safe` (utf-8-encodable, F-823) |
| `in_memory_storage.py` | `InMemoryStorage` — deliberately non-durable instance cross-check |
| `debug_logger.py` | in-memory debug log ring/view; `log_tool_failure` is the ring entry point for a failed tool call (ring only — the durable/Sentry-bridged log line is deliberately NOT written, F-835/F-782) |

### Tombstones — do NOT route a change to these (they were removed)

| Gone | Use instead |
|---|---|
| `embedded/element_cloner.py` | `embedded/cdp_element_cloner.py` (consolidated, M5b) |
| `embedded/comprehensive_element_cloner.py` | `embedded/cdp_element_cloner.py` (M5b) |
| `embedded/persistent_storage.py` | `embedded/in_memory_storage.py` (renamed, M15) |
| `embedded/response_stage_hooks.py` | removed (M12a) |
| `env_utils.py` | never existed — env home is `settings.py` |
| `hot_reload` / `reload_status` **tools** | removed (M2) — code edits apply via a **fresh backend** (source-fingerprint eviction), not a live reload |

---

## Glossary

One meaning per term. Where a term is irreducibly overloaded, each sense gets a
distinct qualified name; the bare word is retired from ambiguous surfaces.

| Term | THE one meaning | Not to be confused with |
|---|---|---|
| **backend** | the shared `python -m … --transport http` process running FastMCP + 94 tools, one per display context | the stdio proxy; "the server" (ambiguous — avoid); the pre-2.0.4 "exactly one process per machine" reading ([DESIGN §2.7](./DESIGN.md#27-display-context-where-a-window-launched-here-would-be-seen)) |
| **stdio proxy** | the short-lived per-Claude-Code-session process bridging stdio ↔ the backend's HTTP | the backend |
| **MCP session** | FastMCP's `mcp-session-id` handshake token (created by `initialize`, discarded by the liveness probe) | a browser session; a Claude Code session |
| **Claude Code session** | one client connection = one stdio proxy instance | an MCP session; a browser session |
| **browser session / named session** | a `spawn_browser(session_name=…)` profile-backed browser instance | any of the above; a "session root" |
| **browser-session root** | the on-disk `STEALTH_MCP_BROWSER_SESSION_ROOT` dir holding profiles/clones | a browser session (this is the *storage* for them) |
| **instance / instance_id** | one live browser managed by `BrowserManager`, keyed by `instance_id` | a browser session (an instance is the *runtime*; a session is the *named profile*) |
| **profile** | a Chrome user-data-dir (master, or a per-session clone) | a session (which *selects* a profile) |
| **profile clone** | a copy-on-spawn profile derived from the master snapshot | the **element clone** (DOM extraction) — always qualify |
| **in-memory storage** | the deliberately non-durable `InMemoryStorage` cross-check (M15 rename of `persistent_storage`) | durable disk state (there is none for instances) |
| **clone storage** | `clone_storage.py`: the on-disk profile/clone quota + GC subsystem | in-memory storage; the cloner *engine* |
| **cloner engine** | `CDPElementCloner`: the one canonical DOM-extraction engine (post-M5b) | clone storage (disk); a profile clone |
| **display context** | `display_context()`'s token for the desktop a window launched HERE would appear on ([DESIGN §2.7](./DESIGN.md#27-display-context-where-a-window-launched-here-would-be-seen)) | the `headless=` spawn argument (a caller's request); the `DISPLAY` env var (one Linux-only input) |
| **adoption** | which recorded backend a client reuses — `adoption_candidates`, asymmetric by design ([DESIGN §2.7](./DESIGN.md#27-display-context-where-a-window-launched-here-would-be-seen)) | reuse *identity* (`singleton._same_identity_backend_ready`) — adoption picks WHICH record to test, identity decides whether it passes |
| **backend registry** | `backend_registry.py` + `server.json`: **which backend to talk to**, one entry per display context | the **browser-pid registry** (below); `server.port` (`PORT_FILE`), write-only legacy; `singleton.lock` |
| **browser-pid registry** | `browser_pid_registry.py` + `browser_pids.json`: **which browsers are tracked and by whom** | the **backend registry** (above); in-memory storage; clone storage |

---

## Tool count = 94 (derived, never typed)

The authoritative count is the live registry:
`sum(len(v) for v in SECTION_TOOLS.values())` == **94** across 11 sections. The CLI's
`--list-sections` and description string derive their numbers from `SECTION_TOOLS`, and
a test asserts the printed total equals the registry count — so no hand-maintained
number can drift. If you add or remove a `@section_tool`, the count updates itself;
update the `94` in the docs to match (the count-assertion test will remind you).
