# DESIGN — why `stealth-chrome-devtools-mcp` is built this way

Audience: the maintainer and any agent placing a change. This document explains the
**invariants and rationale** behind the architecture — the things a reader must not
break without understanding *why they exist*. It is the *why*; `CLAUDE.md` is the
*where* (the navigation map + glossary), `RUNBOOK.md` is the *how to operate*, and
`CONTRIBUTING.md` is the *how to change*. Terms in **bold-italic** like ***backend***
are pinned in the glossary in [`CLAUDE.md`](./CLAUDE.md#glossary); this document uses
them in exactly that sense.

> Context that shapes every decision below: this is a **local, single-user tool**
> with **0 external users**. Priorities, in order: **maintainability, operability,
> performance**. Four lenses are binding on every change — **modularity ·
> deduplication · clarity · conventions**. The conventions lens has a sharp edge:
> *a fix that introduces a second way of doing something is a defect.*

---

## 1. Two surfaces, one backend

There are **two front-ends** over **one shared backend process**:

- the **MCP tool surface** — **94 tools** exposed over HTTP (and bridged to stdio),
  defined in `stealth_chrome_devtools_mcp.embedded.server` and registered through
  `stealth_chrome_devtools_mcp.embedded.tool_registry`;
- the **ops CLI** — the `stealth-chrome-devtools` verbs (`status` / `doctor` / `stop`
  / `restart` / `cleanup` / `kill-orphans` / `serve`) in
  `stealth_chrome_devtools_mcp.cli`, for a human to inspect and operate the backend.

Both talk to the **one** ***backend***: a single detached
`python -m stealth_chrome_devtools_mcp --transport http` process that hosts FastMCP
and all 94 tools. A Claude Code session connects through a short-lived ***stdio
proxy*** that bridges stdio ↔ the backend's HTTP; the ops CLI talks to the same
backend over the same HTTP contract. Keeping the tool surface and the CLI as thin
front-ends over one backend is what lets N client sessions share one browser fleet
without N competing servers.

**The tool count is 94, and it is derived, not typed.** The authoritative source is
the live `SECTION_TOOLS` registry (`sum(len(v) for v in SECTION_TOOLS.values())`).
The CLI's `--list-sections` output and description string derive their numbers from
that registry so no hand-maintained count can drift again (see `CONTRIBUTING.md` and
the count-assertion test). The canonical **verb taxonomy** — the one tool-naming rule
(`list_*`, `get_*`, `create_*`/`spawn_*`, `execute_*`/`call_*`, `extract_*`/`clone_*`,
`set_*`/`modify_*`/`clear_*`, `discover_*`/`inspect_*`) — lives in the
`tool_registry` module docstring. This document and `CONTRIBUTING.md` reference it;
they do not restate it (dedup lens — one home per rule).

---

## 2. Backend lifecycle

All lifecycle logic lives in `stealth_chrome_devtools_mcp.embedded.singleton`. (That
this one module still owns both the backend lifecycle and the stdio proxy is
recorded debt — see [§10, F-740](#10-known-debt-ledger).)

### 2.1 Liveness is an app-level probe, never a bare TCP connect

The only correct signal that the backend is *alive and answering* is a real MCP
`initialize` request that returns HTTP 200 — implemented in
`singleton._backend_http_ready` (it POSTs a throwaway `initialize` and deletes the
session it created). A bare socket connect (`singleton._server_is_healthy`) only
proves *something* holds the port; it is used as a cheap first gate, **not** as the
liveness answer.

`singleton._probe_backend_status` collapses this into four states the CLI reports and
the lifecycle code branches on:

| State | Meaning |
|---|---|
| `none` | no recorded backend |
| `down` | recorded, but nothing answers the socket |
| `wedged` | socket open, but no real `initialize` reply (hung) |
| `responsive` | socket open **and** `initialize` returns 200 |

A ***wedged*** backend is the important one: it un-jams by driving the *existing*
eviction/respawn/orphan-reap machine (`restart`, or a source-change eviction). M1
added **no new kill code** — it added the honest state that tells the operator which
recovery to run.

### 2.2 The port is the CHOSEN port — never re-hardcode it

`singleton.DEFAULT_PORT` is `19222`, but the backend runs on whatever port
`singleton._select_backend_port` chose. That function keeps the recorded port if it
is free or held by our own backend, and only when a **foreign** process occupies the
target does it fall back to an OS-assigned free port (`_free_port`, from
`proxy_forwarder`). The chosen port, plus the version, pid, and source fingerprint,
are handed off through `~/.stealth-mcp/server.json` with exactly these keys:

```json
{ "port": 19222, "version": "...", "pid": 12345, "source_fingerprint": "..." }
```

Discovery and reuse **read the recorded port**; they never assume `19222`. `stop`
clears `server.json`, so the next start falls back to `DEFAULT_PORT`. **Never
re-hardcode `19222`** anywhere in the path — the port is data, not a constant.

### 2.3 Source-fingerprint reuse, and an always-fresh dev backend

A running backend is reused **only** if it matches on two independent checks, ANDed
at the reuse gate in `singleton._find_running_server`:

1. `state["version"] == _server_version()`, and
2. `state["source_fingerprint"] == _source_fingerprint()`.

`singleton._source_fingerprint` is a **SHA-256 over every package `*.py` file**
(content-hashed, so it is immune to mtime/OneDrive churn). The version key is **ANDed
with** the fingerprint at the gate — it is *not* folded into the digest (the digest is
pure source). Consequences that must not regress:

- An **in-place source edit changes the fingerprint**, so the old backend no longer
  matches and is evicted + respawned. *The one way new code reaches the backend is a
  fresh backend* — which is exactly why `hot_reload` / `reload_status` were **deleted**
  (M2). Do not reintroduce a live-reload path; it would be a second, weaker way to do
  what a fresh spawn already does correctly.
- An **empty fingerprint never matches** (fail-closed): a read error yields `""`, and
  `""` short-circuits the reuse gate to a miss → respawn. Never make an empty or
  missing fingerprint compare equal.

### 2.4 Teardown is offloaded so one wedged close can't freeze the fleet

`browser_manager.close_instance` runs the blocking browser kill via
`asyncio.to_thread(self._blocking_teardown, …)` under a **real**
`asyncio.wait_for(…, timeout=self.CLOSE_KILL_TIMEOUT)` (default `5.0`s, from
`settings`), and it does so with the `_instances` lock **released** across the await.
Before M7 a single hung close held the lock and froze every other session's dispatch;
the `wait_for` bound is now real because the blocking work is off the event loop.

### 2.5 `in_memory_storage` is deliberately non-durable

`stealth_chrome_devtools_mcp.embedded.in_memory_storage.InMemoryStorage` (singleton
`in_memory_storage`) is an in-process dict, **cleared on every graceful shutdown**,
and used only as a **secondary cross-check** in the `list_instances` tool (the live
`browser_manager` is the source of truth; storage fills in ids not currently active).
Durability was **rejected on purpose** — persisting instances across restarts would
resurrect dead browsers pointing at gone pids. The M15 rename from `persistent_storage`
fixed a misnomer that invited exactly that mistake. `BrowserInstance` is serialized
whole via `model_dump(mode="json")` (no silent field drop).

### 2.6 Process cleanup activates at the serve boundary, not at import

`stealth_chrome_devtools_mcp.embedded.process_cleanup.ProcessCleanup.__init__` is
**side-effect-free**. Orphan recovery and signal/atexit handler installation move
behind `ProcessCleanup.activate()`, called **once** in the server's `app_lifespan`
startup. Mere *import* — by the test suite, by the ops CLI's read-only verbs, by the
stdio proxy — does **zero** reaping. A public `ProcessCleanup.recover_orphans()` seam
backs the `kill-orphans` CLI verb. This is why importing the package never kills a
stray Chrome you were mid-debugging.

---

## 3. The observability spine

File logging exists on both fronts, all under `logging_setup.resolve_log_dir()`
(`~/.stealth-mcp/logs` unless `STEALTH_MCP_LOG_DIR` overrides):

- `backend-<pid>.log` — the in-process `RotatingFileHandler` (installed by
  `logging_setup.configure_logging("backend")`);
- `backend-boot.log` — the raw `Popen` stdout/stderr redirect the parent opens for the
  child, so a crash **before** `main()` (bad import, syntax error) still leaves a trace
  instead of vanishing into `DEVNULL`;
- `proxy-<pid>.log` — one per stdio proxy (`configure_logging("proxy")`).

Every MCP request is stamped with a **correlation id** at the one chokepoint every
tool passes through: `tool_registry.ToolRegistry.section_tool` wraps each tool with
`logging_setup.with_correlation_id`, and `CorrelationIdFilter` stamps it onto every
log line (with an INFO start/end pair per call). One id ties a request's log lines
together across the backend.

`DEVNULL` for a spawned backend is a **banned API** (it hid every backend crash); the
spawn path uses the logging redirect above. See the banned-API table in
`pyproject.toml`.

---

## 4. Environment configuration has ONE home

Every environment variable the tool reads goes through
`stealth_chrome_devtools_mcp.settings` — a typed pydantic `Settings(BaseSettings)`
model, read once via the process-cached `get_settings()`. It is the Python equivalent
of a strict schema for `.env`: typed coercion, and **loud rejection of unknown
`STEALTH_MCP_*` keys** (a typo fails at startup rather than being silently ignored).

This replaced a scatter of ad-hoc `parse_bool_env` / `parse_float_env` /
`_parse_nonnegative_int_env` helpers with divergent truthiness rules (F-720/F-763).
`os.getenv` and `os.environ` are **banned APIs** repo-wide (see `pyproject.toml`
banned-api table) precisely so a second env-parsing path cannot grow back — the
canonical move to add a knob is *add a typed field to `Settings`*, not read the
environment directly. Application knobs live in the `STEALTH_MCP_*` namespace; a few
legacy unprefixed names (`BROWSER_*`, `CDP_*`, `PORT`, `DEBUG`, …) and host-detection
vars (`DISPLAY`, `USERNAME`, …) are read verbatim via `validation_alias` so no
operator's existing config breaks.

> There is **no** `env_utils.py` module. `settings.py` is the env home; any doc or
> mental model that expects a separate `env_utils` is stale.

---

## 5. The cloner subsystem: one engine, deliberate per-aspect transport

### 5.1 One canonical extraction engine

`stealth_chrome_devtools_mcp.embedded.cdp_element_cloner.CDPElementCloner` (singleton
`cdp_element_cloner`) is the **one** DOM-extraction engine. The former separate
engines were consolidated onto it (M5b): `element_cloner.py` and
`comprehensive_element_cloner.py` were **deleted**, and the two remaining cloner
modules are **thin adapters** that own only their delivery concern, not extraction:

- `file_based_element_cloner.FileBasedElementCloner` — writes each extraction to a
  file; it owns `output_dir` and nothing else, delegating every aspect to the engine
  through one `_extract_and_save` helper. (The class name is deliberately **KEPT**;
  it protects two `output_dir` tripwire tests. Do not "clean up" the name.)
- `progressive_element_cloner.ProgressiveElementCloner` — extracts once via the engine
  then serves `expand_*` slices from the cached result in `in_memory_storage`; it does
  no extraction of its own.

A change to *what* a clone captures belongs in `cdp_element_cloner`, never in an
adapter and never in one of the deleted modules.

### 5.2 Transport table — and why EVENTS must stay JS-eval

Each aspect uses a **fixed transport**, and this is load-bearing, not incidental:

| Aspect | Transport |
|---|---|
| `styles` | **CDP** (`CSS.getComputedStyleForNode` / `getMatchedStylesForNode`; result `method="cdp_direct"`) |
| `structure` | JS-eval (`extract_structure.js`) |
| `events` | **JS-eval** (`extract_events.js`) |
| `animations` | JS-eval (`extract_animations.js`) |
| `assets` | JS-eval (`extract_assets.js`) |
| `related_files` | JS-eval (`extract_related_files.js`) |

Only `styles` uses CDP; every other aspect is JS-eval (the browser-side scripts live
in `embedded/js/`). The composer `CDPElementCloner.extract_complete_element` fans out
to all six and gathers them.

**The events rationale must survive.** It is tempting to "purify" the engine to
all-CDP, but that would silently break event capture: CDP
`DOMDebugger.getEventListeners` sees **only** `addEventListener`-registered listeners.
It **misses inline `on*` handlers and framework/synthetic handlers** — React, for
instance, attaches **one** delegated root listener, so per-element handlers are
invisible to CDP. JS-eval reads what the page actually wired up, at **zero capability
loss**. This transport split is pinned by a test
(`tests/test_cloner_schemas.py::TestCanonicalEngine::test_transport_split_styles_cdp_others_js`
and `test_js_aspect_passes_dict_through`) so a future all-CDP refactor fails loudly.

> A legacy all-CDP composite, `extract_complete_element_cdp` (events via CDP
> `DOMDebugger.getEventListeners`), remains as a **distinct, explicitly-named tool**
> for callers who want the pure-CDP nested shape. It is not the canonical surface and
> the adapters do not use it — it is kept, not dead. The `extract_element_styles` /
> `extract_element_styles_cdp` twin-tool merge is deferred to Ph2 (see §10).

### 5.3 `clone_storage` (disk) is not the cloner engine (extraction)

Two subsystems share the word "clone" and must never be merged:

- `stealth_chrome_devtools_mcp.embedded.clone_storage` — the on-disk **profile/clone
  quota + GC** subsystem (named-profile storage, cap trimming, trash recovery). It is
  about *disk*.
- `CDPElementCloner` — the **DOM-extraction** engine. It is about *reading the page*.

The glossary pins both ("profile clone" vs "element clone"). A consolidation that
routed a cloner into `clone_storage` (or vice-versa) would be a category error.

---

## 6. Network capture is off by default and byte-bounded

Response **metadata** is always captured; response **bodies** are **opt-in**
(`STEALTH_MCP_NETWORK_CAPTURE_BODIES`, default `False`). When on, the body store is
byte-bounded, always:

- per-body cap **5 MiB** (`STEALTH_MCP_NETWORK_BODY_MAX_BYTES`) — an oversize body is
  dropped to `None`, metadata kept;
- total-store cap **128 MiB** (`STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES`) with **FIFO
  eviction** of the oldest bodies until under cap.

Both caps are enforced at the single write chokepoint
`network_interceptor.NetworkInterceptor._store_response`. `0` on either cap means
unbounded.

The **request** store is bounded symmetrically (A3), so a long session cannot leak
memory through unbounded request retention:

- retained-count cap **10 000** (`STEALTH_MCP_NETWORK_REQUEST_MAX_COUNT`) with **FIFO
  eviction** of the oldest requests until under cap;
- per-`post_data` cap **5 MiB** (`STEALTH_MCP_NETWORK_POST_DATA_MAX_BYTES`) — an
  oversize `post_data` is dropped to `None`, the rest of the request metadata kept.

These are enforced at the single write chokepoint
`network_interceptor.NetworkInterceptor._store_request` (both the live capture path and
JSON import route through it). `0` on either cap means unbounded. All five knobs are
typed fields on `Settings` (§4) — not hand-rolled `os.getenv`.

---

## 7. Dynamic hooks are first-match-by-priority, not a chain

A CDP `Fetch.RequestPaused` disposition is **terminal** — exactly one disposition
(`continueRequest` / `continueResponse` / `failRequest` / `fulfillRequest`) resolves
each paused request. So `dynamic_hook_system.DynamicHookSystem` sorts matching hooks by
priority (ascending; lower number = higher priority, default `100`) and runs **only
the highest-priority match**. Lower-priority matches are **shadowed — they do not
run**. When more than one hook matches, a runtime WARNING names the winner and the
shadowed hooks. This is the *domain's* semantics (you cannot "chain" a terminal
disposition), not a bug — do not rewrite it into a middleware chain.

---

## 8. The ONE import convention

- Every intra-package import uses the **absolute-from-package** form:
  `from stealth_chrome_devtools_mcp.embedded.X import Y`. Relative imports are banned
  (`pyproject.toml` `ban-relative-imports = "all"`).
- **No module under `embedded/` may import `server`.** `server.py` is loaded as
  `__main__` via `runpy.run_path(run_name="__main__")` from the top-level
  `stealth_chrome_devtools_mcp.server` shim; an embedded module importing `server`
  would trigger a **double registration** of every tool under runpy. Helpers that need
  the browser manager take it as an **argument** (e.g. `tool_errors._require_tab`),
  which is exactly why the error helpers live in a leaf module.
- There is **exactly one** sanctioned `sys.path` shim (`embedded/__init__.py`, which
  puts `embedded/` on the path). Do not add a second `sys.path` insert anywhere.

---

## 9. The ONE error convention

Tools report failure by **raising** a typed error — `tool_errors.ToolError` (or
`tool_errors.InstanceNotFoundError`) — not by hand-rolling a `{"success": False, …}`
dict on every tool (M4-A1; ~40 raise sites). Success helpers **return values**. The
guard helpers `tool_errors._require_tab` / `_require_browser` raise on a missing
instance and take `browser_manager` as a parameter (so the leaf module never imports
`server`).

**Named KEEP contracts** — here the returned dict/value *is* the contract; converting
it to a raise would be the defect:

- result-envelope success dicts: `execute_script`, `create_python_binding`
  (the `{"success": …, "result"/"error": …}` shape a caller destructures);
- the diagnostic dict: `validate_browser_environment_tool`;
- input-validation value-returns: `expand_children`, `clone_element_to_file` bad-arg
  paths;
- deliberate resilience/fallbacks: `query_elements` (loop resilience),
  `get_response_content` (base64 alternative / nullable), `get_instance_state`
  (blessed partial), `clear_debug_view` (bool), `export_debug_logs` (guidance string).

If you are adding a tool, the default is *raise `ToolError` on failure, return the
value on success*. Reach for a dict only to join one of the KEEP families above, and
say so.

---

## 10. Known-debt ledger

Recorded, **not fixed** here — each line quotes the disposition already in the audit
record so it is traceable, not re-derived. These are the deliberate "not yet" seams;
if you touch one of these files, this is the split/cleanup that is *expected but not
owed* by your change.

| Item | Debt (recorded disposition) |
|---|---|
| **F-740** | `singleton.py` is really `backend_lifecycle` + `stdio_proxy`; split deferred to a post-M2 wave (sequence-critical — several plans edit this file serially). Severity High. |
| **F-702** | `BrowserManager` wants a 6-concern split (M4-Ph2 era). |
| **F-703** | `DebugLogger` hides a serialization engine; extract in a wave-4 cleanup. |
| **F-603** | the `_with_cdp_timeout` timeout-preamble idiom is duplicated across modules; only the `server.py` half consolidated in M4 — cross-module dedup remains. |
| **F-106** | deeper `spawn_browser` decomposition beyond the M13 seam. |
| **M4-Ph2** | full per-section `server.py` split + the deferred tool renames **F-760** (verb taxonomy), **F-743** (exec-family), **F-744 remainder**, and the `extract_element_styles` / `extract_element_styles_cdp` twin-tool merge (both route to the same engine CDP method post-M5b). |
| **F-606** | hook `matches()` re-parses its expression each call — no compile cache. OPEN. |
| **F-765** | a `poll_until` helper to fold repeated polling loops. OPEN. |
| **M1 probe-body dedup** | `_backend_http_ready` deliberately copies ~10 lines of the `initialize` probe body to stay disjoint from M3-owned code; one shared probe body in `singleton` is a future cleanup. |
| **M11b DI seam** | a general factory/DI seam for the import-time singletons (F-125 remainder) is deferred; M11a removed only `process_cleanup`'s import-time side effects. |

**Not debt (delivered):** the F-104 error-envelope sweep (M10b) was **delivered** via
M4-Ph1 Amendment A1 and is **closed** — it is intentionally absent from this ledger.
Recording it would be a false claim.
