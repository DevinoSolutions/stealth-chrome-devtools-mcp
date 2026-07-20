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
| `server.py` | thin entrypoint — loads `embedded/server.py` as `__main__` via `runpy` (`main()` shim) |
| `__main__.py` | `python -m stealth_chrome_devtools_mcp` → `server.main()` |
| `cli.py` | the `stealth-chrome-devtools` ops CLI verbs (`status`/`doctor`/`stop`/`restart`/`cleanup`/`kill-orphans`/`serve`) |
| `settings.py` | **the one env home** — pydantic `Settings` + `get_settings()`; every `STEALTH_MCP_*` knob is a typed field here |
| `observability.py` | optional Sentry error shipping (no-op unless `SENTRY_DSN` set) |

### `embedded/` — the backend

**Lifecycle & transport**
| File | Owns |
|---|---|
| `server.py` | the real MCP server — all 94 tool bodies + `app_lifespan` |
| `singleton.py` | **backend lifecycle + the stdio proxy** — liveness (`_backend_http_ready`, `_probe_backend_status`), port selection (`_select_backend_port`, `DEFAULT_PORT`, `server.json`), source-fingerprint reuse (`_source_fingerprint`), `run_stdio_proxy` |
| `tool_registry.py` | `SECTION_TOOLS` + `ToolRegistry.section_tool` (registration, section gating, correlation-id stamping) + the canonical **verb taxonomy** (module docstring) |
| `tool_errors.py` | the error convention — `ToolError`, `InstanceNotFoundError`, `_require_tab`, `_require_browser` |
| `logging_setup.py` | the observability spine — `resolve_log_dir`, `configure_logging`, `with_correlation_id`, `CorrelationIdFilter` |
| `process_cleanup.py` | orphan reaping — side-effect-free `__init__`, `activate()` at serve boundary, `recover_orphans()` seam |
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

**Cloner subsystem** (one engine + thin adapters + disk storage)
| File | Owns |
|---|---|
| `cdp_element_cloner.py` | **THE cloner engine** (`CDPElementCloner`) — every aspect (`styles` via CDP; `structure`/`events`/`animations`/`assets`/`related_files` via JS-eval) |
| `file_based_element_cloner.py` | thin to-file adapter (`FileBasedElementCloner`, name KEPT) — owns `output_dir` only |
| `progressive_element_cloner.py` | thin adapter (`ProgressiveElementCloner`) — `expand_*` slices from cached extraction |
| `clone_storage.py` | on-disk **profile/clone quota + GC** (NOT extraction — see glossary "clone") |
| `js/` | the 7 browser-side extraction scripts (`extract_styles.js`, `extract_structure.js`, `extract_events.js`, `extract_animations.js`, `extract_assets.js`, `extract_related_files.js`, `comprehensive_element_extractor.js`) |

**Network, hooks, execution, storage, debug**
| File | Owns |
|---|---|
| `network_interceptor.py` | `NetworkInterceptor` — capture + the body caps (`_store_response`) |
| `dynamic_hook_system.py` | `DynamicHookSystem` — first-match-by-priority request hooks |
| `dynamic_hook_ai_interface.py` | AI-facing API for creating/managing hooks |
| `hook_learning_system.py` | hook examples/training surface |
| `cdp_function_executor.py` | direct JS function execution via CDP |
| `response_handler.py` | large-response handling + file fallbacks |
| `in_memory_storage.py` | `InMemoryStorage` — deliberately non-durable instance cross-check |
| `debug_logger.py` | in-memory debug log ring/view |

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
| **backend** | the single shared detached `python -m … --transport http` process running FastMCP + all 94 tools | the stdio proxy; "the server" (ambiguous — avoid) |
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

---

## Tool count = 94 (derived, never typed)

The authoritative count is the live registry:
`sum(len(v) for v in SECTION_TOOLS.values())` == **94** across 11 sections. The CLI's
`--list-sections` and description string derive their numbers from `SECTION_TOOLS`, and
a test asserts the printed total equals the registry count — so no hand-maintained
number can drift. If you add or remove a `@section_tool`, the count updates itself;
update the `94` in the docs to match (the count-assertion test will remind you).
