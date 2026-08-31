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

There are **two front-ends** over a **shared backend process**:

- the **MCP tool surface** — **94 tools** exposed over HTTP (and bridged to stdio),
  defined in `stealth_chrome_devtools_mcp.embedded.server` and registered through
  `stealth_chrome_devtools_mcp.embedded.tool_registry`;
- the **ops CLI** — the `stealth-chrome-devtools` verbs (`status` / `doctor` / `stop`
  / `restart` / `cleanup` / `kill-orphans` / `serve`) in
  `stealth_chrome_devtools_mcp.cli`, for a human to inspect and operate the backend.

Both talk to a shared ***backend***: a detached
`python -m stealth_chrome_devtools_mcp --transport http` process that hosts FastMCP
and all 94 tools. There is **one backend per (source fingerprint, display context)** —
in practice one process on a headless box, and at most two on a desktop box that is
also SSH'd into ([§2.7](#27-display-context-where-a-window-launched-here-would-be-seen)).
A Claude Code session connects through a short-lived ***stdio proxy*** that bridges
stdio ↔ the backend's HTTP; the ops CLI talks to the same backend over the same HTTP
contract. Keeping the tool surface and the CLI as thin front-ends over a shared
backend is what lets N client sessions share one browser fleet without N competing
servers.

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

**One failed probe is not a licence to kill (F-807).** On the cold-start path "not
answering" means *terminate*, so the verdict there is deliberately patient: the
winner of the startup lock holds it until the backend answers a real `initialize` —
not merely until its socket binds — and any lock-holder gives a **same-identity**
backend (version AND source fingerprint both match) up to `REUSE_PATIENCE_SECONDS`
(60 s) of retried probes before it may evict. Identity gates the grace on purpose: a
stale record still evicts immediately so an upgrade takes effect now, and a dead one
(no socket, no live process) fails the first probe so crash recovery stays fast.
Discovery's hot path stays single-shot (`patience=0`) and the watchdog's 2 s
`LIVENESS_PROBE_TIMEOUT` is untouched; `tests/test_startup_herd.py` proves the result
at scale — 40 simultaneous real stdio sessions, exactly one logical backend.

### 2.2 The port is the CHOSEN port — never re-hardcode it

`singleton.DEFAULT_PORT` is `19222`, but the backend runs on whatever port
`singleton._select_backend_port` chose. That function keeps the recorded port if it
is free or held by our own backend, and only when a **foreign** process occupies the
target does it fall back to an OS-assigned free port (`_free_port`, from
`proxy_forwarder`). The chosen port, plus the version, pid, and source fingerprint,
are handed off through `~/.stealth-mcp/server.json`, which records one backend
per **display context** (F-808) so a headless and a desktop backend can coexist:

```json
{
  "schema": 2,
  "backends": {
    "win-session-1": {
      "port": 19222, "version": "...", "pid": 12345,
      "source_fingerprint": "...", "display_context": "win-session-1"
    }
  }
}
```

The flat `{port, version, pid, source_fingerprint}` record every release up to
2.0.3 wrote still reads, as one backend classified `unverified`. Read entries out
of the record through `backend_registry`'s accessors — nothing outside that module
branches on `schema`.

Recording a backend also **supersedes by port**: any other entry claiming that port
is dropped, because only one process can hold a loopback listener, so a second entry
naming it is by construction a leftover (a v1 record, or a context token that changed
under a backend that did not — a Windows session id is reassigned across an RDP
reconnect). Entries on other ports, `unverified` included, survive.

Discovery and reuse **read the recorded port**; they never assume `19222`. `stop`
forgets the stopped backend's own display-context entry and clears `server.json` only
once nothing else is recorded, so the next start falls back to `DEFAULT_PORT` when it
was the last backend on the machine. **Never re-hardcode `19222`** anywhere in the
path — the port is data, not a constant.

### 2.3 Source-fingerprint reuse, and an always-fresh dev backend

A running backend is reused **only** if it matches on two independent checks, ANDed
at the one reuse gate `singleton._same_identity_backend_ready` — which discovery's
`_find_running_server` calls single-shot and the cold-start lock calls with the §2.1
patience window, so identity and readiness have exactly one home:

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

### 2.7 Display context: where a window launched here would be seen

Chrome inherits its parent's window station, so whether a headed browser is
**visible** is decided by the process that launches it — never by the caller and
never by the `headless` flag. On the machine F-808 was reported from, the shared
backend had been cold-started by an SSH client in Windows **Session 0** (isolated
since Vista; its desktop is never composited onto a user's screen), so every
desktop session on that box reused it and got a browser that was fully driveable
over CDP and permanently invisible. `spawn_browser` reported `state: "ready"`,
`headless: false`, `window_size.measured: true` — all true, none of them an
observation of a window.

`embedded/display_context.py` makes that property explicit and **observational**:
it reports OUR OWN context and never tries to pick or enter someone else's session.
`WTSGetActiveConsoleSessionId()` is deliberately not used *here* — on the reporting
machine the active console session was 2 while the user's desktop lived in session 1,
so any "find the interactive session" heuristic is wrong on somebody's machine. (F-810
reads it in `desktop_launch.py` for a different question — "is anyone logged on at
all" — and still never picks a session; see the amendment below.) The token is
`headless` (PROVEN invisible), `unverified` (unclassifiable — treated as capable, so
a broken probe can never block headed browsing), or a specific desktop:
`win-session-N`, `wayland-<display>`, `x11-<display>`, `aqua-<uid>`.

**Identity is a preference, not an equality test — in one direction only.** The
asymmetry lives in `backend_registry.adoption_candidates` and is the whole fix:

- A client that **cannot prove** it has a desktop (`headless` or `unverified`)
  adopts **any** recorded backend, window-capable entries first. This is what makes
  an SSH session's headed spawn visible: it converges on the desktop backend, and
  the window opens on the real desktop. For such a client display context is a
  *preference*, never a filter.
- A client that **can prove** it has a desktop adopts only its **own** context's
  entry plus `unverified` ones. Every other proven context is excluded — foreign
  desktops as much as `headless`. Identity cannot separate them (a sibling desktop
  runs the same install, so version and fingerprint both match), yet a browser
  spawned there renders on a window station this user cannot see. Refusing costs
  one cold start.

`unverified` is adoptable on **both** sides, because it is what every record written
up to 2.0.3 reads as. Refusing it would evict a healthy backend the moment a user
upgrades. **Only a PROVEN verdict moves anything** — that rule also governs
`port_conflict`, where treating `unverified` as a conflict would divert the spawn to
a random free port, send eviction at the wrong port, and leak the live 2.0.3 backend
and its Chrome processes for good.

**F-810 amends the ruling in mechanism, not in spirit.** Refusing is correct but it
is not *service*: a user who asks for a headed browser wants a headed browser, with
no manual step. So on Windows, when this backend's own context cannot show a window
and a user IS logged on at the console, `embedded/desktop_launch.py` hands Chrome's
**process creation** to Task Scheduler — a one-shot task that runs "only when the
user is logged on" — and **Windows itself** places the process in that user's
interactive session. The window is visible by construction rather than by our guess.
The same backend then attaches over CDP (`uc.Config(host, port)` →
`connect_existing`), so there is still exactly ONE backend, the instance lives in the
same registry as every other instance, and all 94 tools work unchanged.

This does **not** reintroduce session-picking. `display_context.py` is untouched and
still observational; the tool still never selects or enters a session. The one new
OS read, `WTSGetActiveConsoleSessionId()`, answers only "is delegation on offer at
all" — it is never used to *choose* where the browser goes, which is precisely the
heuristic 2.7 rejects. On the reporting machine that call returns 2 while the desktop
lives in session 1, and F-810 is still correct there: it asks the scheduler for "the
logged-on user's session", and the OS resolves it.

Where delegation is impossible (non-Windows, nobody logged on) or fails,
`spawn_browser(headless=False)` **raises** a `ToolError` naming the context, the
delegation attempt, and the remedies (start a backend from a desktop session; or pass
`headless=True`) — the loud refusal is now the fallback, which is exactly when a loud
error is correct. Silent headed→headless degradation was rejected as the same defect
wearing a different hat. Headless spawns from a `headless` context are unaffected —
CI depends on exactly that, and a headless spawn is never delegated.

---

## 3. The observability spine

File logging exists on both fronts, all under `logging_setup.resolve_log_dir()`
(`~/.stealth-mcp/logs` unless `STEALTH_MCP_LOG_DIR` overrides):

- `backend-<pid>.log` — the in-process `RotatingFileHandler` (installed by
  `logging_setup.configure_logging("backend")`);
- `backend-boot.log` — the raw `Popen` stdout/stderr redirect the parent opens for the
  child, so a crash **before** `main()` (bad import, syntax error) still leaves a trace
  instead of vanishing into `DEVNULL`. Shared by every boot and held open by the
  running child, so it can only be rotated by the **launcher**, between two backends:
  `logging_setup.roll_boot_log` does that in `singleton._start_server_process` (F-830);
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

That `.env` is `~/.stealth-mcp/.env`, never the cwd's. The backend is a shared
singleton and MCP clients launch it with cwd set to whatever project the user
opened, so a cwd-relative env file made this server read the *host project's*
application config — fatal under the strict schema (an ordinary `DATABASE_URL`
took the backend down for every connected session), and silently wrong when the
key happened to be one of ours (`PORT`, `DEBUG`, `SENTRY_DSN`). Scoping the file
to our own state dir is also what keeps `extra="forbid"` honest: it now guards a
file the operator wrote. `settings.py` is a leaf module and may not import the
package, so it recomputes the state-dir path that `embedded/backend_registry.py`'s
`STATE_DIR` canonically defines — the one deliberate duplication, commented at
both ends.

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

**What is captured is the page's traffic, not the browser's** (F-803). Chrome emits
its own non-web requests on every launch — `chrome://new-tab-page/*`, extensions,
`devtools://`, error pages — which outnumbered the real page requests 24-to-1 in a
live 2.0.0 measurement. Requests whose URL uses a browser-internal scheme
(`chrome:`, `chrome-error:`, `chrome-extension:`, `chrome-native:`, `chrome-search:`,
`chrome-untrusted:`, `devtools:`, `about:`) are therefore **dropped at capture time**,
by the one predicate `network_interceptor.is_internal_url`. To debug the browser
itself, set `STEALTH_MCP_NETWORK_CAPTURE_INTERNAL_URLS=1` globally or pass
`set_network_capture_filters(capture_internal_urls=True)` for one instance — the same
per-instance-overrides-global vocabulary as `capture_bodies`, deliberately **not** a
second filter mechanism.

Each captured row's `resource_type` (`Document`, `XHR`, `Script`, …) is read off the
CDP event by the one helper `network_interceptor.resource_type_of`, which exists
because nodriver's generated dataclasses spell the field `type_`, not `type` — the
pre-fix `hasattr(event, "type")` probe was permanently False, so every row carried
`resource_type=None` and `list_network_requests(filter_type=…)` could never match
(F-803). `ResponseReceived` backfills the type when `RequestWillBeSent` omitted it.

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
| **F-509 A2** | cold-start lock **losers** commit to their own `_select_backend_port` result; behind a foreign squatter the winner's `_free_port()` pick differs, so a loser polls a port nothing will bind for the full `BACKEND_READY_TIMEOUT` (120 s) before self-healing. Recorded in `TRIAGE_final-review_to_plan_RELEASE.md` A2 (singleton.py, MED). Fix shape: losers re-read the record after the lock resolves, or serialize port choice under the lock. OPEN. |
| **F-809** | a clean POSIX shutdown emits ERROR logs, so every graceful stop ships Sentry events (`STEALTH-CHROME-DEVTOOLS-MCP-1J`, `-1H`). Two independent causes: `ProcessCleanup._signal_handler` **replaces** uvicorn's `handle_exit` rather than coexisting with it (both use `signal.signal`, ours installs second), so `sys.exit(0)` erupts inside the running loop and the lifespan/session tasks unwind abnormally; and FastMCP 2.11.2 hard-codes `timeout_graceful_shutdown: 0`, where `asyncio.wait_for(coro, 0)` **always** raises, so uvicorn logs "Cancel N running task(s)" on every graceful HTTP stop regardless. Fix shape: clean up, then hand the signal back to the host server, plus a real grace budget under singleton's 5 s terminate→kill window. Windows is unaffected (`TerminateProcess` runs no handler). OPEN. |
| **F-810** | desktop delegation is **Windows-only**. A Linux/macOS backend with no desktop of its own still refuses a headed spawn; there is no `sudo -u`/`launchctl asuser` equivalent path, and no login-time autostart installer. The scheduled task is one-shot and torn down in a `finally`, but a hard-killed backend can still leave `stealth-mcp-launch-<uuid>` behind — nothing sweeps stale ones. OPEN. |
| **F-811** | the exhaustion hint makes the symptom legible; it does not make it stop. Why parallel agent fleets leave hundreds of orphaned Chrome processes, and whether backend cold-start contention (45 `backend-*.log` files in one day, 9 backends dying at boot inside 26 minutes) is a separate defect, are unanswered. Two accepted residuals: the hint is appended per attempt at `browser_manager`'s S1, so a spawn that exhausts all three `server.py` retries can carry it up to three times in the joined message (de-duplicating means editing `server.py` at its 3401 cap; the common case is one, since `_fallback_profile_selection` returns `None` on the first failure when no fallback profile exists); and the same signal is deliberately NOT surfaced on the *success* path as a `spawn_diagnostics` field, which would warn an agent *before* the failure but costs a process-table walk on every spawn and also touches `server.py`. OPEN. |
| **`_PROTECTED_CLONE_DIRS` lifetime** | `clone_storage._protect_clone_dir` shields an in-flight clone from the cap sweep, but protection is released only on a clean `close_instance`; a spawn that dies between `_protect_` and its instance record leaks a permanently sweep-exempt entry in a process-global set. Bounded and small, never audited. OPEN. |
| **`PORT_FILE` vestige** | `server.port` is written by `singleton` and cleared by `stop_backend`, with **no reader anywhere in `src/`**. Now that `backend_registry` owns the record, deleting it is a one-line removal plus the test fixtures that redirect it. OPEN. |
| **Proxy-side `debug_logger` records reach no file** | `debug_logger` emits to the `stealth.backend` logger; `logging_setup.configure_logging(role)` attaches a handler only to `stealth.<role>` with `propagate = False`. In a stdio-proxy process only `stealth.proxy` is wired, so every `debug_logger` warning raised proxy-side is discarded. F-808 made this load-bearing: `display_context`'s "session probe refused / raised → unverified" warnings fire in the proxy, exactly where an operator would look to explain an invisible spawn. OPEN. |
| **Handled `ToolError`s ship to Sentry at full volume** | `debug_logger.log_error` emits `_backend_logger.error(...)` **unconditionally and un-deduped** (deliberate, F-182/F-204: the durable file must have every occurrence), and `observability.py` installs `LoggingIntegration(event_level=logging.ERROR)`. So an ordinary user-side error becomes a Sentry event per occurrence — Sentry issue `STEALTH-CHROME-DEVTOOLS-MCP-P` is **132 events from a single user-script `SyntaxError`**. The durable-file requirement and the Sentry volume want different gates; today there is one. OPEN. |
| **WebSocket 404 in `window_sizing.apply_and_measure`** | Post-launch measurement raised a WebSocket-404 on a teammate machine (Sentry `STEALTH-CHROME-DEVTOOLS-MCP-1E`, **one event**, not reproduced). Measurement is already guarded — it degrades to `measured: false` rather than failing the spawn — so the visible cost is a spawn that reports an unmeasured size. Root cause unknown; too thin to act on. OPEN. |

**Not debt (delivered):** the F-104 error-envelope sweep (M10b) was **delivered** via
M4-Ph1 Amendment A1 and is **closed** — it is intentionally absent from this ledger.
Recording it would be a false claim.
