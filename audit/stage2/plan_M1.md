# Stage 2 Plan — M1 App-Level Liveness

- **Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701`
- **Branch (audit):** `fix/singleton-version-aware-backend` · **Fix branch (Stage 3):** `audit/fixes-2026-07-02`
- **Date:** 2026-07-02
- **Batch:** {M1 app-level liveness}
- **Base tree:** **post-`plan_M3`** (M3 = {observability spine, M10a} is APPROVED and executes FIRST; this plan is written against the tree M3 leaves behind — see §1.3 for every anchor M3 shifts).
- **Status:** **APPROVED** (human, 2026-07-02) — cleared for Stage 3. Decisions: **~12s detection window kept** (`LIVENESS_PROBE_TIMEOUT=2.0`, watchdog `interval=2.0` × `failures_before_teardown=3`); **one PR of 4 commits** (M1-1…M1-4).
- **Findings closed:** **F-301** (Critical), **F-501** (Critical). **F-611** (Medium) — explicitly **OUT of scope**, justified in §8.
- **Context (do not re-derive):** LOCAL single-user tool, **0 users, breaking changes are FREE.** Priorities strict: (1) maintainability (2) operability (3) performance. `uv run` is BROKEN here — always `.venv\Scripts\python.exe` directly.

All line anchors below were **re-opened and confirmed at the pinned SHA** while writing this plan (§1.1). Because M1 runs **serially after M3's two PRs merge**, the executable tree is post-M3; §1.3 records exactly which anchors M3 moves so Stage 3 re-anchors by **symbol**, not by the raw line numbers here.

---

## The one fact that shapes this plan

The whole system's **only runtime liveness signal is a bare TCP connect** (`_server_is_healthy`, `singleton.py:83-89`). A backend whose asyncio/FastMCP dispatch loop is wedged still completes the kernel TCP handshake, so every consumer of that signal reads "healthy" forever. There is **already** a real application-level probe in the file — `_await_backend_http` (`:316-365`) does a real `initialize`→HTTP-200 against `/mcp/` — but it is called **once at startup and never again**. M1 does not invent a probe; it promotes the *mechanism* that already exists into a reusable single-shot check and routes the **runtime** consumers through it.

The elegant part: the codebase **already has** a full eviction + respawn + orphan-reap machine (`_clear_stale_backend` → `_start_server_process` → `process_cleanup` orphan recovery). The audit's own 3am-incident trace found that machine "only fires on a fresh backend start, **which M1 prevents**." So M1 adds **no new kill/respawn code** — it fixes the one signal that is jamming the existing recovery machine shut. Fixing `_find_running_server`'s health check un-gates eviction; fixing the watchdog's default check arms auto-recovery. That is the entire fix.

---

## 1. Scope

### 1.1 Confirmed code anchors at HEAD (re-opened while writing this plan)

**The weak signal and its callers (`embedded/singleton.py`):**

| Anchor @ pinned SHA | What it is | M1 action |
|---|---|---|
| `:83-89` `_server_is_healthy(port)` | bare TCP connect/close — the weak signal | **KEEP** (still correct for socket-lifecycle uses below); do **not** rename/delete |
| `:117-136` `_find_running_server()` | reuse/discovery gate; version match `:132`, health check **`:134`** | **REWIRE `:134`** TCP → app-probe |
| `:180-217` `_clear_stale_backend(port)` | the eviction path; early-return guard **`:188`** = `if _find_running_server() == port`; post-kill port-release wait **`:213`** | guard fixed **transitively** (via `_find_running_server`); **KEEP `:213` on TCP** (it correctly waits for the socket to *close*) — **no direct edit** |
| `:248-256` `_wait_for_server(port)` | post-spawn bind-wait under the lock, uses `_server_is_healthy` `:252` | **KEEP on TCP** (waits for "our just-spawned socket bound"; MCP readiness is enforced downstream by `_await_backend_http` in `run_backend`) |
| `:259-260` `_backend_http_url(port)` | returns `http://127.0.0.1:{port}/mcp/` | reuse (probe target) |
| `:284` / `:306` `_start_backend_holding_lock` / `ensure_server_running` | both call `_find_running_server()` | fixed **transitively** — no direct edit |
| `:316-365` `_await_backend_http(url, …)` | the **existing** app-level probe (`initialize`→200), **startup-only** | **DO NOT EDIT** (M3 owns its internals at `:358/:361`); its mechanism is the template for the new single-shot helper |
| `:368-398` `_watch_backend_liveness(port, *, interval=2.0, failures_before_teardown=3, is_healthy=None, sleep=None)` | the **sole** auto-recovery watchdog; default check **`:388`** = `lambda: _server_is_healthy(port)`; consecutive-failure loop `:390-397` | **REWIRE the default `:388`** TCP → app-probe; make the loop **await-aware** |
| `:459-461` `run_backend` | startup gate `if not await _await_backend_http(url): return`; arms watchdog via `backend_ready.set()` `:462` | no edit (M3 logs `:461`) |
| `:524-535` `monitor_backend` | `await _watch_backend_liveness(port)` `:529`, then teardown print `:530-534` | **no M1 edit** — picks up the new watchdog default; the print is converted to a `stealth.proxy` log **by M3** |

**Consumers outside singleton (`cli.py`):**
- `:95-107` `_cmd_status` → `port = singleton._find_running_server()` `:99`, prints `backend : running on port … / not running` `:101`.
- `:162-181` `_cmd_doctor` → same pattern `:173-174`.
- Both import the server with `STEALTH_MCP_NO_AUTO_RECOVERY=1` (read-only; never disturbs the backend). Verbs registered `:220-256` = status/profiles/cleanup/doctor/serve (no stop/restart — that is **M8**).

**Preserve, do not rewrite (audit "What's good"):**
- `embedded/process_cleanup.py` orphan-Chrome matching (exact `--user-data-dir` + `create_time`; `__init__:33` → `_recover_orphaned_processes():55`, runs at **import of a fresh backend**). M1 **un-blocks** it (a wedged backend stops reading as reusable → eviction → fresh backend imports the module → reap fires). **No edit.**
- Version-aware backend (issue #14): `_find_running_server`'s version gate `:132` and `tests/test_singleton_version_aware.py` (15+ tests). M1 changes **only the health signal at `:134`**, never the version **key** (that is M2). Tests are migrated minimally, not rewritten (§5.3).

**Dependency facts confirmed:** `httpx` and `anyio` are already deps (lazily imported inside `_await_backend_http` at `:325-326`); the new probe reuses them. **No new dependency.** FastMCP is launched via `mcp.run(transport="http"|"stdio")` (`server.py:4205/4207`); there is **no** `custom_route`/`/healthz`/health handler anywhere in `server.py` (only `@mcp.resource(...)` MCP resources) — this grounds the endpoint decision in §2.2.

### 1.2 Files to be touched

**Modified source (2):**
- `src/stealth_chrome_devtools_mcp/embedded/singleton.py` — add the single-shot app-probe `_backend_http_ready` + a `_probe_backend_status` reporter + a `LIVENESS_PROBE_TIMEOUT` constant; rewire `_find_running_server:134`; rewire `_watch_backend_liveness` default + make its loop await-aware + emit `stealth.proxy` WARNINGs on probe failure.
- `src/stealth_chrome_devtools_mcp/cli.py` — `_cmd_status`/`_cmd_doctor` report **responsive / wedged / down** via `_probe_backend_status` instead of the truthy/None of `_find_running_server`.

**New tests (4 files) + 1 migrated test file — see §5.**

**No other files.** No `server.py` edit (no `/healthz`), no `browser_manager.py` edit (F-611 is out), no `process_cleanup.py` edit.

### 1.3 Anchors that plan_M3 SHIFTS (re-anchor by symbol in Stage 3)

M3 edits `singleton.py` at `:229-231` (Popen→boot-log redirect, **+lines**), `:268/:293/:358/:361/:461` (cold-start logs, **+lines**), `configure_logging("proxy")` in `run_stdio_proxy`, and converts the `:515-520`/`:530-534` prints to `stealth.proxy` logs. Consequences for my anchors:

- **Unshifted** (they sit *above* M3's first edit at `:229`): `_server_is_healthy:83`, `_find_running_server:117-136` (**my `:134` edit is stable**), `_clear_stale_backend:180-217`.
- **Shifted down** (they sit *below* `:229`, by M3's cumulative insertions ≈ +8…+14 lines): `_backend_http_url`, `_await_backend_http`, **`_watch_backend_liveness`** (my main edit — expect it near `:376-382`, default-check near `:396-400`), `run_backend`, `monitor_backend`.
- **M3 already rewrote** `monitor_backend`'s teardown print into a `stealth.proxy` log → my liveness WARNINGs slot into the **same logger** M3 configured; I add nothing to `monitor_backend` itself.

**Stage-3 rule:** locate `_watch_backend_liveness` by `def _watch_backend_liveness` and its default by the substring `else (lambda: _server_is_healthy(port))`; locate the discovery edit by `if not _server_is_healthy(port):` **inside** `_find_running_server`. Do not trust the raw line numbers in §1.1 after M3 merges.

### 1.4 Explicit out-of-scope (stated so Stage 3 does not creep)
- **F-611 / `list_instances` CDP round-trip** — different subsystem (`browser_manager.py:141-156/588-603`), different mechanism (per-instance CDP `Runtime.evaluate`), Medium/unverified. Out; §8 justifies and names its natural home.
- **M8 recovery CLI** — no new verbs (`stop`/`restart`/`kill-orphans`), no PID/port surfacing beyond the responsive/wedged/down state. M1 changes *what status/doctor report*, not the operator's action set. (F-305 pid surfacing stays M8.)
- **M2 reuse-key / `hot_reload`** — M1 changes the health **signal** at `:134`, never the version **key** at `:132`.
- **M7** `close_instance` teardown; **M9** body store; **M4** `server.py` decomposition (so: no `/healthz` route).
- **`_await_backend_http` internals** — owned by M3 (its `:358/:361` logs). M1 reuses its *mechanism* via a new, separate function; it does not touch it.
- No drive-by refactors; a mid-implementation discovery becomes a **new finding**, not scope.

---

## 2. Approach + rejected alternatives

### 2.1 Chosen design

**A. One single-shot, synchronous app-probe — the reused mechanism.**
Add `_backend_http_ready(port: int, *, timeout: float = LIVENESS_PROBE_TIMEOUT) -> bool` next to `_backend_http_url`. It does exactly what `_await_backend_http` does for *one* attempt, synchronously (so sync callers can use it directly): `httpx.Client` → POST a real `initialize` to `_backend_http_url(port)` → `True` iff HTTP 200 → best-effort `DELETE` of the throwaway `mcp-session-id` → **never raises** (any exception, incl. connection-refused/timeout → `False`, matching `_server_is_healthy`'s fail-closed contract). Add `LIVENESS_PROBE_TIMEOUT = 2.0` beside `BACKEND_READY_TIMEOUT` (`:47`). The `initialize` body/headers are a small self-contained copy of `_await_backend_http`'s (`:329-342`) with a one-line comment pointing at its twin (see §2.2 rejected #4 for why *copy*, not *share*).

**B. Rewire discovery/reuse (`_find_running_server:134`).** One line: `if not _server_is_healthy(port):` → `if not _backend_http_ready(port):`; docstring updated from "socket-healthy" to "answers a real MCP `initialize`." This single change fixes **three** things at once, because the eviction guard (`_clear_stale_backend:188`) and both cold-start callers (`:284`, `:306`) all route through `_find_running_server`: a wedged same-version backend now returns **None** → it is no longer "reusable" → `_clear_stale_backend` falls through its guard and evicts it → `_start_server_process` respawns a fresh **same-version** backend → `process_cleanup` orphan-reap fires on that fresh import. **The recovery machine un-jams with one line.**

**C. Rewire the watchdog (`_watch_backend_liveness` default + await-aware loop).** Change the default check from `lambda: _server_is_healthy(port)` to an app-level check, and make the loop **await-aware** so the default can run off the event loop without blocking the stdio pump:
- Default check runs the sync `_backend_http_ready(port)` via `anyio.to_thread.run_sync` (returns a coroutine).
- Loop: `res = check(); if inspect.isawaitable(res): res = await res` — so an **injected sync `is_healthy`** (every existing watchdog test) still works unchanged, and the async default works too.
- **Signature, `interval=2.0`, and `failures_before_teardown=3` are unchanged** (preserves the existing tests and the hysteresis).
- On each *failed* probe, emit `logging.getLogger("stealth.proxy").warning(...)` (M3 already configured `stealth.proxy` at the top of `run_stdio_proxy`, and its `CorrelationIdFilter` stamps the line). The teardown decision is already logged by M3's converted `monitor_backend` print. **These `proxy-<pid>.log` lines are M1's verification medium** (§5).

Why this is safe against false positives (the core design tension):
1. The watchdog is **armed only after `backend_ready.set()`** (`run_backend:462`), i.e. after `_await_backend_http` already proved the backend answers `initialize`. It **never runs during cold start** → cold-start races cannot false-positive it.
2. A single healthy probe **resets** the counter (existing `:394`), so a transient blip never tears down.
3. The probe distinguishes the two real states correctly: a backend that **blocks the event loop** (the F-301 wedge) cannot answer *any* HTTP → probe fails → torn down (correct); a backend merely **awaiting one slow CDP response** keeps yielding the loop → the probe is serviced between yields → reads healthy (correct — that is one stuck request, not a dead backend).
4. **Teardown ≠ kill.** The watchdog only tears down the **proxy** (client reconnects). The backend is *killed* only if the *next* session's reuse-probe (B) *also* fails. A backend that was briefly busy and recovered passes that second, independent probe → it is **reused, not killed** → live browser sessions survive. A kill happens only for a genuinely persistent wedge.

**D. Report responsive/wedged/down in the CLI (`status`/`doctor`).** Add `_probe_backend_status() -> tuple[str, int | None]` to singleton: read `_read_server_state()`; if no recorded backend → `("none", None)`; else for recorded port P — socket closed (`not _server_is_healthy(P)`) → `("down", P)`; socket open but `not _backend_http_ready(P)` → `("wedged", P)`; both → `("responsive", P)`. `_cmd_status`/`_cmd_doctor` stop calling `_find_running_server()` for display and print one of: `not running` · `running (responsive) on port P` · `running but UNRESPONSIVE on port P — wedged; a new session will evict and respawn it`. (No pid — that is M8/F-305.) This closes the "status prints *running* through the whole outage" half of F-301.

**Net M1 surface:** one new probe fn, one new status reporter, one constant, one rewired line in discovery, one rewired default + await-aware loop + WARNING in the watchdog, two CLI verbs re-pointed. No new kill/respawn/bind logic — the existing, well-tested eviction+respawn is simply un-blocked.

### 2.2 Rejected alternatives

1. **A dedicated `/healthz` starlette route on FastMCP** (the finding's alternative). Rejected as primary: there is **no** custom-route wiring in `server.py` today (only `@mcp.resource`), so this means adding a starlette route + researching FastMCP's `custom_route` API **inside `server.py`** — the exact 4,207-line file **M4** decomposes and **M3** already edits. It also proves *strictly less* than `initialize`→200 (a `/healthz` answered by starlette proves the ASGI loop turns, but not that the **MCP session manager** is up — the very race `_await_backend_http`'s docstring warns about). Reusing the proven `initialize`→200 keeps M1 entirely inside `singleton.py`, disjoint from M3/M4. (Kept as a note: if a future maintainer wants a cheaper probe, `/healthz` is the place, owned by M4.)
2. **A raw `tools/list` round-trip instead of `initialize`.** Rejected: `tools/list` requires an already-initialized session id; `initialize` is the one call that needs no prior state and is exactly what `_await_backend_http` already validates. Same round-trip cost, less setup, proven.
3. **Keep the watchdog's `check()` synchronous and call the sync probe inline.** Rejected: the watchdog shares the proxy's event loop with the stdio pump; a blocking 2 s `httpx` call inline would freeze client↔backend forwarding for up to 2 s every 2 s. Running the sync probe via `anyio.to_thread.run_sync` + an await-aware loop keeps the pump responsive and still reuses the *one* sync probe implementation.
4. **Share one probe body between `_await_backend_http` and `_backend_http_ready` (extract a common helper).** Rejected in favor of a small **copy**: sharing forces an edit *into* `_await_backend_http`, re-entangling M1 with the exact function M3 owns (the cross-review ruling is that M1/M3's `singleton.py` regions stay disjoint). The duplicated surface is ~10 lines of a stable `initialize` dict; a `grep '"initialize"'` finds both. Consolidation is logged as a **future finding**, not done here.
5. **A backend-side self-check / heartbeat thread** (backend watches itself and exits on wedge). Rejected: a wedged event loop cannot run its own heartbeat (same blindness); the observer must be **out-of-process**. The proxy already is that out-of-process observer — use it.
6. **An external supervisor process** (systemd-style). Rejected: heavyweight for a single-user local tool; the proxy-per-session already provides a natural external watcher, and M8 will add the manual `restart` escape hatch. Over-engineering against priority (1) maintainability.
7. **Tune the threshold tighter (e.g. `failures_before_teardown=1`, sub-second interval) for faster detection.** Rejected: it trades away the hysteresis that prevents tearing down a briefly-busy backend, and it breaks the three existing watchdog tests that pin "exactly 3 consecutive." ~12 s bounded detection is a vast improvement over *infinite* and keeps the safety margin; leave 2.0/3.

---

## 3. Sequencing (smallest-first, each independently verifiable)

> Baseline before starting (must be green, on the post-M3 tree): `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed** (+ M3's added tests), coverage ≥ gate 39. Re-run the **full** suite after **every** step. One checkpoint commit per step (§6).

**Step 1 — `_backend_http_ready` probe + `LIVENESS_PROBE_TIMEOUT` (pure addition, no behavior change).**
Add the constant and the single-shot sync probe (§2.1-A). Pinning tests first (`tests/test_backend_liveness_probe.py`): against a stdlib `ThreadingHTTPServer` that returns `200` + an `mcp-session-id` → `True` **and** a `DELETE` is received; against a **raw-accept-no-response** socket (accepts TCP, never writes) → `False` within `timeout`; against a closed port → `False` fast; asserts it **never raises**.
*Verify:* `.venv\Scripts\python.exe -m pytest tests/test_backend_liveness_probe.py -q` green; full suite still green. Nothing else calls the probe yet.

**Step 2 — Rewire discovery/reuse (`_find_running_server:134`) [un-blocks eviction; closes F-301's reuse+eviction half].**
Change the one line TCP→app-probe; update the docstring. Write the behavior-pinning tests first (`tests/test_find_running_server_app_probe.py`): a recorded **same-version** backend whose port is a **wedged stub** → `_find_running_server()` is `None` (not reused); a recorded same-version backend on a **responsive stub** → returns the port. Then **migrate `tests/test_singleton_version_aware.py`** (§5.3) so its "reusable" cases stub `_backend_http_ready` alongside the existing `_server_is_healthy` stub.
*Verify:* the two new tests + the full migrated `test_singleton_version_aware.py` green; full suite green. This is the step with the **only** existing-test churn — isolate it.

**Step 3 — Rewire the watchdog (`_watch_backend_liveness` default + await-aware loop + WARNING) [arms auto-recovery; closes F-501].**
Change the default check to the `to_thread` app-probe; add the `inspect.isawaitable` guard; add the per-failure `stealth.proxy` WARNING. Signature/interval/threshold unchanged. Pinning tests first (`tests/test_watchdog_app_level.py`): with the **default** probe pointed at a **wedged stub**, the watchdog returns after exactly 3 failures **and** a `stealth.proxy` WARNING is captured (caplog); a probe that fails twice then succeeds does **not** tear down (counter reset); an **injected async** check and an **injected sync** check both drive the loop (pins await-awareness).
*Verify:* new tests green; **`tests/test_proxy_backend_death.py` still green unchanged** (its injected sync `is_healthy` callables pass through the await-aware guard; its end-to-end `backend.kill()` still trips the probe because a killed backend refuses the socket); full suite green.

**Step 4 — CLI responsive/wedged/down (`_probe_backend_status` + status/doctor).**
Add `_probe_backend_status` to singleton; re-point `_cmd_status`/`_cmd_doctor`. Pinning tests first (`tests/test_cli_status_wedged.py`): recorded state + **wedged stub** → output contains "UNRESPONSIVE"/"wedged"; + **responsive stub** → "responsive"; no state → "not running".
*Verify:* new tests green; full suite green + coverage ≥ 39.

Steps 2–4 are **independent** (all depend only on Step 1) and individually revertible; this order does the pure addition first, then the highest test-churn item (discovery) while the tree is otherwise untouched, then the two remaining consumers.

---

## 4. Breaking changes

**0 users — external-compatibility breaking changes: N/A.** No tool name, signature, config key, or MCP return shape changes. The 96-tool surface is untouched (no `server.py` edit).

**Observable behavior that changes (all intended, all internal/operational):**
- A **wedged** backend (socket open, dispatch loop dead) is now **detected and recovered** instead of hanging forever: the watchdog tears down the proxy within ~12 s; the next session's reuse-probe rejects it; the existing eviction respawns a fresh backend (which fires orphan-reap).
- `status`/`doctor` now print **three** states (`not running` / `running (responsive)` / `running but UNRESPONSIVE (wedged)`) instead of the old binary `running/not running`. A previously "running"-through-an-outage report becomes "UNRESPONSIVE."
- `_find_running_server()` now returns `None` for a same-version-but-wedged backend (previously returned its port). Any test asserting the old socket-only behavior is migrated in §5.3 (justified, minimal).
- Discovery/`status`/`doctor` and the watchdog now issue a lightweight `initialize`+`DELETE` round-trip to the backend where they previously did a bare socket connect. Benign and self-cleaning (identical to the startup probe); the CLI stays "read-only."

---

## 5. Test strategy

Guiding rule (superpowers TDD): the behavior-pinning test is written and shown to pass **in the same step** as the code. Keep **402 (+M3) green and coverage ≥ 39 at every checkpoint**; the new tests are net-positive on coverage.

### 5.1 The hard part — simulating a WEDGED (not dead) backend
Two hermetic, fast, localhost-only fixtures (no Chrome, no real backend, so they stay in the `not integration` suite):
- **Wedged stub** = a raw `socket.socket` bound to `127.0.0.1:0`, `listen()`, with a daemon thread that `accept()`s and then **holds the connection without ever writing a byte**. Effect: `_server_is_healthy(port)` → `True`; `_backend_http_ready(port)` → `False` after `timeout`. This is the exact "socket-open, app-dead" state the whole finding is about, and no existing test constructs it.
- **Responsive stub** = a stdlib `http.server.ThreadingHTTPServer` whose handler answers `POST` with `200` + an `mcp-session-id` header (and records `DELETE`s). Effect: `_backend_http_ready` → `True` and the session-cleanup `DELETE` is observable.
Both are provided as pytest fixtures yielding `(port, teardown)`; reused across Steps 1–4.

### 5.2 Behavior-pinning tests written BEFORE each change
- **Probe truth table** (Step 1): responsive→`True`+`DELETE`; wedged→`False`-within-timeout; down→`False`-fast; never raises.
- **Wedged backend is not reused** (Step 2): same-version recorded + wedged stub → `_find_running_server()` is `None`; responsive → returns port.
- **Watchdog arms on a wedge** (Step 3): default probe + wedged stub → returns after exactly 3 consecutive failures; a `stealth.proxy` WARNING is emitted (caplog on the `stealth.proxy` logger — verification via M3's spine); fail-fail-succeed → no teardown (reset); injected sync **and** async checks both work.
- **Status reports wedged** (Step 4): wedged→"UNRESPONSIVE"; responsive→"responsive"; none→"not running".

### 5.3 Existing tests that MUST change (enumerated) and why
- **`tests/test_singleton_version_aware.py`** (the issue-#14 suite; its module docstring notes it relies on "`_server_is_healthy` passes without any HTTP/browser/backend process"). Every case that expects `_find_running_server()` to return a **port** (`:60, :117, :284`-fed, and the reuse assertions) currently gets there because `_server_is_healthy` returns `True`. After Step 2, `_find_running_server` gates on `_backend_http_ready` instead, so those cases must **also** stub the app-probe: add `monkeypatch.setattr(singleton, "_backend_http_ready", lambda port, **kw: True)` (a shared fixture/autouse in that module is cleanest). Cases expecting `None` (version mismatch `:74, :87`; no state `:100`) are unaffected (they fail before the health check). **Migration is mechanical — stub one more function — the version semantics under test are unchanged.** The socket-level assertion at `:341` (`assert not _server_is_healthy(port)` after eviction) **stays as-is** — it pins the port-release wait, which M1 deliberately keeps on TCP.
- **No change needed:** `tests/test_proxy_backend_death.py` (injects sync `is_healthy` → await-aware guard passes them; end-to-end `backend.kill()` → socket refused → probe fails); `tests/test_singleton_fast_handshake.py` (patches `_find_running_server`'s *return value* directly `:92/:109`, above the health check).

### 5.4 Characterization tests for the undertested paths
- The watchdog's await-aware loop is new control flow — pinned by the sync+async injection test (5.2).
- `_probe_backend_status`'s three-way branch is new — pinned by the CLI test (5.2) and a direct unit test of the reporter.
- The probe's fail-closed-never-raises contract is characterized against wedged/down/garbage-response stubs (Step 1).

---

## 6. Rollback + checkpoint commit boundaries

- **Branch:** `audit/fixes-2026-07-02`, **serial after M3's two PRs merge** (base = post-M3). Stage-3 discipline: pinning tests before the change, **full suite green at every checkpoint**, deviation → **stop and report to Fable**.
- **One commit per Step (§3):** `M1-1 single-shot app probe + LIVENESS_PROBE_TIMEOUT` · `M1-2 discovery/reuse uses app probe (+version-aware test migration)` · `M1-3 watchdog default app probe + await-aware + proxy WARNING` · `M1-4 status/doctor report responsive/wedged/down`.
- **PR shape (recommended):** a **single PR** for M1 (four commits) — the batch is one finding-pair and ~2 source files; simpler than stacking. Keep the commit boundaries inside it. (If the human prefers, M1-1+M1-2 and M1-3+M1-4 split cleanly into two.)
- **What to revert if a step goes bad:** Step 1 is a pure addition (revert in isolation, nothing depends on it yet). Step 2 carries the **only** existing-test churn — if the version-aware migration destabilizes, revert just that commit; Steps 1/3/4 do not depend on it. Steps 3 and 4 are independent of each other. Fastest safe partial rollback: keep Step 1 (dead but harmless) and revert any consumer that misbehaves; the product is strictly more recoverable with even Steps 1–3 and no CLI change.
- **Checkpoint gate:** each commit must leave `-m "not integration"` green and coverage ≥ 39, verified with the venv python before moving on.

---

## 7. Risk (blast radius, worst case, early-warning signs)

1. **A too-aggressive probe tears down / respawns a healthy-but-busy backend → data loss in live browser sessions.** *Blast radius:* the shared backend and every session's live tabs. *Mitigations (layered, §2.1-C):* watchdog armed only post-readiness (never during cold start); single success resets the counter; the probe reads a properly-awaiting slow request as healthy (only a loop-**blocking** backend fails it — and that *is* the wedge); **teardown ≠ kill** — a kill needs a *second* independent reuse-probe failure, so a briefly-busy backend is reused, not killed. *Worst case:* a backend that blocks its event loop for >~12 s (already the F-301 bug) is respawned, losing that already-dead session's state. *Early warning:* repeated `liveness probe failed` / `tearing down for reconnect` WARNINGs in `proxy-<pid>.log` **without** a matching hard-death, and rapid respawns in `backend-boot.log` timestamps.
2. **Cold-start latency in the recovery path.** `_find_running_server` is called up to 3× during a locked cold start; a **wedged same-version** backend makes each probe wait `LIVENESS_PROBE_TIMEOUT` (~3×2 s ≈ 6 s) before eviction. *Mitigation:* only the wedged-recovery path pays it (down/healthy paths fail-fast/return-fast); 6 s to recover vs infinite hang today. *Early warning:* a slow first call **only** when recovering from a wedge.
3. **Fixed port 19222 + eviction interaction.** After the reuse-probe rejects a wedge, the existing `_clear_stale_backend` terminates the pid and waits (on TCP, kept) for the port to release before respawn. M1 does not change bind/evict logic — it only makes it **fire**. *Worst case:* if `terminate()` fails and the wedged process keeps 19222, respawn can't bind — but that is the **pre-existing** eviction behavior (M8/F-509 owns port fallback), now merely reachable. *Early warning:* "port still bound" after terminate in the logs; a second respawn attempt.
4. **Await-aware watchdog change mishandles an injected callable.** *Blast radius:* the recovery watchdog. *Mitigation:* `inspect.isawaitable` guard + the sync+async injection test + the unchanged `test_proxy_backend_death.py` suite gating it. *Early warning:* those watchdog tests red.
5. **Probe cost / thread-pool use.** An `httpx.Client` + `to_thread` hop every 2 s per session. *Mitigation:* negligible locally; anyio's default thread limiter (40) is untroubled by one probe/2 s; the per-event network path is untouched (perf is priority 3, order-of-magnitude only). *Early warning:* `proxy-<pid>.log` growth or CPU with idle sessions (none expected).
6. **CLI double-work / disturbance.** `status`/`doctor` now issue an `initialize`+`DELETE`. *Mitigation:* identical to the startup probe, self-cleaning, still under `STEALTH_MCP_NO_AUTO_RECOVERY=1`; the CLI never evicts. *Early warning:* a stray readiness session lingering on the backend (the `DELETE` prevents it).

**Overall worst case:** a persistently loop-blocking backend is respawned (losing an already-dead session) or a fixed-port rebind stalls — **both strict improvements over today's silent infinite hang, and both now visible in M3's logs.** Everything else degrades to "a little latency" or "a little CPU," never a functional regression, because every liveness path is **fail-closed** (probe error → treat as unhealthy → recover) and the kill/respawn path it un-blocks is the existing, version-aware-tested one.

---

## 8. Findings closed

- **F-301 (Critical — socket-only liveness across reuse, watchdog, eviction, status/doctor).** Closed by routing **every runtime consumer** of the weak signal through the app-probe: discovery/reuse (`_find_running_server:134`, §2.1-B), the auto-recovery watchdog (`_watch_backend_liveness` default, §2.1-C), the eviction guard (fixed **transitively** — the guard *is* `_find_running_server`), and `status`/`doctor` (now report responsive/wedged/down, §2.1-D). The bare TCP check is retained only for the two socket-lifecycle waits (bind, port-release) where "is the socket up/down" is the correct question. Verified via M3's `proxy-<pid>.log` (watchdog WARNING on wedge) and the wedged-stub tests.
- **F-501 (Critical — the watchdog specifically polls the weak signal; "10 s then indefinitely").** Closed by the **same watchdog edit** (§2.1-C), which is F-501's exact fix direction verbatim ("use the real MCP readiness probe … inside `_watch_backend_liveness`, not a bare TCP connect"): a wedged-but-listening backend now fails the probe, the counter reaches 3, and the proxy tears down for a clean reconnect that respawns a fresh backend — breaking the self-perpetuating outage.
- **F-611 (Medium — `list_instances` trusts process-liveness without a CDP round-trip). OUT of M1 scope.** Justification: it is a **different layer** (`browser_manager.py:141-156/588-603`, per-**browser-instance** CDP responsiveness) from M1's **backend-process** HTTP/MCP liveness (`singleton.py`); its fix is a **different mechanism** (`Runtime.evaluate`/`Target.getTargetInfo` per instance, not an HTTP `initialize`); it is **Medium, `verified:false`, adversarially `not_reviewed`**, whereas M1's mandate is the single surviving **Critical**. `browser_manager.py` is already opened by **M7** (`close_instance`) and is a **M6** characterization target — the CDP liveness probe belongs there (or a small standalone follow-up), not bundled into the singleton liveness fix where it would add crown-jewel-path blast radius for no closure of the Critical. **Recommendation:** file the CDP-probe as a task under M6/M7; it shares M1's spirit ("bookkeeping ≠ liveness") but not its surface.

---

## Appendix — open questions (RESOLVED by human, 2026-07-02)
1. **Probe timeout / detection window:** ✅ **Keep ~12 s** (`LIVENESS_PROBE_TIMEOUT = 2.0`, watchdog `interval=2.0`, `failures_before_teardown=3`) — preserves the existing watchdog tests and the anti-flap hysteresis.
2. **PR shape:** ✅ **One M1 PR of four commits** (M1-1…M1-4 checkpoint boundaries preserved inside it).

No open decisions remain — Stage 3 may execute this plan as written against the post-M3 tree.
