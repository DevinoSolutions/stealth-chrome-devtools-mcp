# plan_M9 — Bound the response-body store + make body capture opt-in/off-by-default

**Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend`
**Date:** 2026-07-03
**Batch:** `{M9}` (single item; executes **8th**, serially — plan-of-record `fix_order[7]`)
**Base tree:** post-`plan_M3`(+A1) + `plan_M1` + `plan_M8`(+A1) + `plan_M2` + `plan_M7` + `plan_M11a`+`plan_M15`. Runs from M15's final commit on branch `audit/fixes-2026-07-02`.
**Status:** **APPROVED** (human, 2026-07-03) — cleared for Stage 3. Decisions: **capture OFF-by-default** (`STEALTH_MCP_NETWORK_CAPTURE_BODIES` default False; `get_response_content` still live-refetches; `search`/`export` bodies empty until enabled, with a `capture_note`); **cap defaults 128 MiB total store / 5 MiB per-body** (env-tunable). Orchestrator verified in source: `get_response_content:2143-2146` live-refetches independent of the store; `self._responses[:182]` unbounded; the M10a-7b log at `:171` is carried through.
**Closes:** **F-605** (High, the sole order-of-magnitude memory item in the audit). **F-606** and **F-609** explicitly ruled **OUT** (§8, different files).

> Context (pinned, not re-derived): LOCAL single-user tool, **0 users, breaking changes are FREE.** Priorities: (1) maintainability (2) operability (3) **performance — M9 is the ONE order-of-magnitude perf/memory item in the whole audit.** Network interception is a **crown-jewel feature**; M9 bounds its memory **without gutting it**. Baseline: `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed**, coverage gate **39** (HEAD ~40.86%). `uv run` is BROKEN — never used.

---

## 1. Scope

### 1.1 Files touched (source + tests)

**Primary source (1):**
- `src/stealth_chrome_devtools_mcp/embedded/network_interceptor.py` (607 lines @ HEAD) — the body store lives here; all cap + opt-in logic lands here.

**Secondary source (1, thin, symbol-anchored):**
- `src/stealth_chrome_devtools_mcp/embedded/server.py` (4208 lines @ HEAD) — extend the two capture-filter tools with a `capture_bodies` flag and add a capture-state note to the body-consuming tools. **No change to `spawn_browser`/`setup_interception`** (see §1.4 — the opt-in gates *body fetch*, not handler registration).

**Tests (new):**
- `tests/test_network_interceptor_cap.py` — hermetic byte-bound pins (M9-1).
- `tests/test_network_capture_optin.py` — default-off / enable-restores pins (M9-2).
- `tests/test_server_network_tools.py` — tool-layer `capture_bodies` param + capture-state signal (M9-2). *(If a network-tools test module already exists post-M2/M8, extend it instead of adding a second — conventions.)*

**Explicitly NOT touched:** `models.py` (`NetworkResponse` keeps its shape — capture state is signalled at the interceptor/tool layer, not by a new model field; §2 rejected alt); `dynamic_hook_system.py` (F-606, M12a); `debug_logger.py` (F-609, M3).

### 1.2 Confirmed anchors (re-opened at pinned SHA `2267b83d`)

`network_interceptor.py` — the body store and its write/read/free sites:

| Anchor @ HEAD | What it is | M9 action |
|---|---|---|
| `__init__:20-25` | `self._requests:21`, **`self._responses:22`** (the body store — plain dict, no cap), `self._instance_requests:23`, `self._instance_filters:24`, `self._lock:25` | **ADD** `self._body_bytes`, `self._body_order` (deque); resolve env defaults at module import |
| `_on_response:144-187` | body fetch `156-172` (CDP `get_response_body` `:159`; **M10a-7b DEBUG log site — the `except Exception:` at `:171`, `pass` `:172`**), build `NetworkResponse(...body=body):174-180`, **store `self._responses[request_id] = network_response:182`** under `_lock:181` | **REWRITE**: gate body fetch on effective capture; route the store through `_store_response` |
| `set_capture_filters:190-207` | writes `self._instance_filters[instance_id] = {"include":…, "exclude":…}` `:204-206` (overwrites whole dict) | **EXTEND**: add `capture_bodies` param; **merge** (do not clobber include/exclude) |
| `get_capture_filters:209-217` | returns `self._instance_filters.get(instance_id, {"include":[],"exclude":[]})` `:217` | **EXTEND**: return resolved `capture_bodies` + store-usage stats |
| `search_requests:219-291` | reads `response.body` `266-268`; **M10a-7b DEBUG log site — `except Exception:` `:271`, `continue` `:272`** | **UNCHANGED** (read path; M10a log untouched) |
| `get_response:324-332` | `self._responses.get(request_id)` | unchanged |
| `get_response_body (method):334-359` | **live CDP re-fetch** (`tab.send(get_response_body)` `:345`); **M10a-7b DEBUG log site — `except Exception:` `:357`, `pass` `:358`** | **UNCHANGED** (store-independent live fetch; M10a log untouched) |
| `import_from_json:431-471` | **second write site** — `self._responses[resp.request_id] = resp:470` (bypasses `_on_response`) | **REROUTE** through `_store_response` (cap applies to imports too) |
| `clear_instance_data:594-606` | pops `self._requests`/`self._responses` per req_id `601-604` | **ADD** byte-accounting decrement on pop |

`server.py` — interception wiring + the body-store-consuming tools:

| Anchor @ HEAD | What it is | M9 action |
|---|---|---|
| `network_interceptor = NetworkInterceptor():1298` | module singleton (one interceptor shared by all instances) | unchanged |
| `spawn_browser:1306` → `setup_interception(tab, instance.instance_id, block_resources):1397-1398` | registers `_on_request`/`_on_response` handlers **unconditionally** (this is F-605's "default-on") | **UNCHANGED** — see §1.4 |
| `get_response_details:2110` → `get_response(request_id):2122` | returns stored `NetworkResponse` | **ADD** capture-state note when `body is None` |
| `get_response_content:2129` → **`get_response_body(tab, request_id):2146`** | **live CDP re-fetch — store-independent** | **UNCHANGED** (works with capture off — key de-risker) |
| `search_network_requests:2157` → `search_requests(...):2185` | store-backed body search | **ADD** capture-state note when bodies absent |
| `export_network_data:2199` → `export_to_json(...):2213` | store-backed body export | **ADD** capture-state note |
| `set_network_capture_filters:2235` → `set_capture_filters(...):2253` | filter setter tool | **EXTEND**: `capture_bodies: Optional[bool] = None` |
| `get_network_capture_filters:2258` → `get_capture_filters(...):2270` | filter getter tool (the "is capture on?" surface) | passes through extended dict |

### 1.3 Anchors that predecessors SHIFT (re-anchor by SYMBOL in Stage 3)

- **`network_interceptor.py` — ONLY `plan_M3` (M10a-7b) edits this file** (verified: `grep network_interceptor audit/stage2/*.md` → hits only in `plan_M3.md`; M1/M2/M7/M8/M11a/M15 do **not** touch it). M10a-7b inserts one **DEBUG** log line into each of the three body-read `except Exception` handlers — `:171` (in `_on_response`), `:271` (in `search_requests`), `:357` (in `get_response_body`) — keeping the existing `pass`/`continue` sentinel (plan_M3 §1.1 rows 7-9 + §3 step 7b). **Net effect on M9's rewrite regions:** the `_on_response` body-fetch `except` gains a DEBUG log, so the store line `:182` and the `NetworkResponse(...)` block `:174-180` shift down ≈ **+1**. **Re-anchor by symbol, not line:** `async def _on_response(`, the body-fetch `try:` block + its `except Exception:` (← **carry the M10a-7b DEBUG log verbatim**), `self._responses[request_id] = network_response` (the line rerouted to `_store_response`), `async def set_capture_filters(`, `async def get_capture_filters(`, `async def import_from_json(` (its `self._responses[resp.request_id] = resp` line), `async def clear_instance_data(` (the pop loop). **`:271` and `:357` are in methods M9 does NOT rewrite → their M10a logs stand untouched.** (state.json cross_review_notes L177 ruling honored.)
- **`server.py` — re-anchor EVERYTHING by symbol** (`plan_M11a_M15` §1.3 pattern). Predecessors churn this file: **M2 deletes `hot_reload`/`reload_status` ~2974-3038** (−66 below that point — *below* my tool region, so my anchors don't shift from it); **M3 inserts the `section_tool` correlation wrapper ~1212-1217 + M10a-7d logs at 198/997/1226** (*above* my tool region → my `2061-2270` anchors shift **down** by M3's inserts); **M11a adds `process_cleanup.activate()` in `app_lifespan` ~1246** (+1 above); **M15 renames `persistent_storage`→`in_memory_storage`** (imports/usages) — churn, no material shift to my region. Re-anchor: `network_interceptor = NetworkInterceptor()`, `async def spawn_browser(`, `async def get_response_details(`, `async def get_response_content(`, `async def search_network_requests(`, `async def export_network_data(`, `async def set_network_capture_filters(`, `async def get_network_capture_filters(`. **Any deviation from a confirmed symbol → STOP and report** (Stage-3 discipline).

### 1.4 Explicit out-of-scope (stated)

- **`spawn_browser` / `setup_interception` (server.py:1397) are NOT changed.** The opt-in gates *whether `_on_response` fetches + stores the body*, **not** whether the CDP handlers are registered. Request/response **metadata** capture stays on (cheap, useful, and it is what `list_network_requests`/`get_response_details` read). This is the triage's "**filtered by default**" reading — bound the bytes, keep the traffic map. Flipping `setup_interception` off entirely would gut the crown-jewel feature; rejected (§2).
- **M10a-7b's three DEBUG logs** — carried through (`:171`) / left untouched (`:271`, `:357`); never deleted or duplicated.
- **F-606** (eval compile cache, `dynamic_hook_system.py:119-131`) and **F-609** (buffer-outside-lock, `debug_logger.py:282-293`) — both in **different files**, ruled OUT (§8).
- **Metadata-count growth** (the `_requests`/`_instance_requests` dicts grow with *request count*, independent of body bytes) — **not** F-605's scope (F-605 is body **bytes**, the order-of-magnitude driver). Flagged as a residual/new-finding candidate in §7, not fixed here.
- M5 (cloners), M4 (server.py decomposition), M12a (hooks), M6 (characterization suite). No drive-by refactors — discoveries → new findings.

---

## 2. Approach + rejected alternatives

**Chosen design — two orthogonal, composable controls, both living in the existing `_instance_filters` home (conventions lens):**

1. **Always-on byte caps (bounds the feature even when fully enabled).** A single insertion chokepoint `_store_response(request_id, resp)` (called under `self._lock`) enforces:
   - **per-body cap** `STEALTH_MCP_NETWORK_BODY_MAX_BYTES` (default **5 MiB** = `5*1024*1024`): a body larger than this is not stored (its `.body` is dropped to `None`, metadata kept), DEBUG-logged.
   - **total-store cap** `STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES` (default **128 MiB** = `128*1024*1024`): a running `self._body_bytes` counter; when an insert pushes it over, evict **oldest-captured** bodies (FIFO via `self._body_order: deque`) — null each evicted `.body`, subtract its bytes, keep metadata — until back under cap, DEBUG-logging each eviction. `0` = "no cap" (documented sentinel).
2. **Capture opt-in / off-by-default.** `STEALTH_MCP_NETWORK_CAPTURE_BODIES` (bool, default **False**). `_on_response` resolves the effective flag = per-instance `_instance_filters[instance_id]["capture_bodies"]` if set, else the global default. **Off → skip the CDP `get_response_body` fetch entirely** (memory *and* per-response latency win) and store metadata-only (`body=None`). **On → fetch as today, then `_store_response` applies the caps.** One-step enable, extending the existing filter path: `set_network_capture_filters(instance_id, capture_bodies=True)` (per-instance) or `STEALTH_MCP_NETWORK_CAPTURE_BODIES=1` (global). This is the exact fix for F-605's adversarial gap ("`_on_response` applies NO filter … even `set_capture_filters` cannot bound response-body memory") — the response path now honors the filter.

Both env knobs are parsed via **`env_utils`** (`parse_bool_env`, `parse_nonnegative_int_env`) at module import — the F-720/F-602 canonical parser, **not** hand-rolled `os.getenv` (a second parser would be a conventions defect). Import idiom matches the file: `from env_utils import parse_bool_env, parse_nonnegative_int_env` (sibling of the existing `from debug_logger import debug_logger`).

**Why M9-1 (caps) closes the OOM on its own:** even if capture stayed on, the total cap makes the body store's resident memory `≤ STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES` regardless of session length. M9-2 (opt-in) is an *additional* reduction (default ≈ 0 body bytes). This matches triage: "cheap cap kills the risk."

### Rejected alternatives

- **Per-body size cap only (no total cap).** *Rejected:* N bodies each just under 5 MiB still sum to unbounded memory over a long session — doesn't bound the aggregate, which is the OOM driver. Kept as *one half* of the chosen design, not the whole.
- **Count cap only (keep last N responses, e.g. `deque(maxlen=N)`).** *Rejected as the primary bound:* body sizes vary by 3-4 orders of magnitude (200-byte redirect vs 50 MB media), so a count cap gives no memory guarantee — 500 media bodies ≫ 5000 JSON bodies. A **byte** cap is the honest memory bound. (A count cap on *metadata* is a possible separate follow-up — §7.)
- **LRU eviction (move-to-end on every read).** *Rejected vs FIFO:* LRU adds bookkeeping to every read path (`get_response`, `search_requests`, `export`) for marginal benefit on a debugging tool where recency-of-capture ≈ recency-of-interest. FIFO (evict oldest-captured) is O(1), lock-simple, and sufficient. (Recorded as a tracked trade-off, not a defect.)
- **Spill bodies to disk instead of memory.** *Rejected for a local tool:* trades an OOM for unbounded disk growth + a new cleanup/quota surface (cf. F-184 `element_clones` spill has no quota) + serialization cost on the hot `_on_response` path. Adds a whole subsystem to bound a feature that a byte cap bounds in ~15 lines. Over-scoped.
- **Store nothing; always re-fetch on demand.** *Rejected:* the live re-fetch path already exists (`get_response_body` method, used by `get_response_content`) but CDP evicts response bodies from its own buffer quickly — re-fetch is unreliable for later reads, which is *why* the store exists. Removing the store breaks `search_network_requests(response_contains=…)` and `export_network_data` bodies entirely. The chosen design keeps the store (bounded) AND leverages the existing re-fetch: `get_response_content` stays fully functional with capture off (the reason off-by-default is low-harm).
- **Flip `setup_interception` (server.py:1397) off by default — register no handlers.** *Rejected:* that drops request/response **metadata** too (`list_network_requests` goes empty), gutting the crown-jewel feature for everyone, not just body bytes. Gating body *storage* inside `_on_response` bounds memory while keeping the traffic map — "filtered by default," per triage.
- **Add a `body_capture: str` field to `NetworkResponse` to signal skip/evict reason.** *Rejected (minimal scope):* touches `models.py`, churns a shared model, and the signal is better surfaced at the tool boundary (`get_network_capture_filters` reports state + store stats; body-consuming tools add a note). Keeps M9 to two files. Noted as an option if the human wants per-response provenance.
- **A new `set_body_capture(...)` tool / a separate `_body_capture_enabled` dict (a second capture-control path).** *Rejected (conventions lens — a second way to do something is a defect):* body capture is a *capture filter*; it belongs in `_instance_filters` alongside include/exclude, set via the existing `set_network_capture_filters` tool. One home, one tool.

*Lenses:* **Conventions** — one capture-control home (`_instance_filters` + `set_network_capture_filters`), one env parser (`env_utils`), one insertion chokepoint (`_store_response`). **Deduplication** — `_on_response` and `import_from_json` both route their store through `_store_response`; the cap is enforced in exactly one place. **Modularity** — all memory logic stays inside `NetworkInterceptor`; `env_utils` is a stdlib-only leaf (no cycle). **Clarity** — `_store_response`, `_body_bytes`, `capture_bodies` are self-describing; a lighter model can place a change without reading callers.

---

## 3. Sequencing (independently verifiable; one commit per step)

> Pinning discipline (from every approved plan): **write the pinning test in each step BEFORE the change; run the full suite after every step; deviation from a confirmed symbol → STOP.** Baseline before starting (post-M15 tree): 402 passed, coverage ≥ 39.

**M9-1 — Byte-cap the store (per-body + total). Closes the OOM.**
- `__init__`: add `self._body_bytes: int = 0` and `self._body_order: deque = deque()` (`from collections import deque`); module-level `_MAX_BODY_BYTES = parse_nonnegative_int_env("STEALTH_MCP_NETWORK_BODY_MAX_BYTES", 5*1024*1024)`, `_MAX_STORE_BYTES = parse_nonnegative_int_env("STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES", 128*1024*1024)`.
- Add `_store_response(request_id, resp)` (must be called under `self._lock` by callers): handle overwrite (subtract prior counted bytes); per-body skip (drop `.body`→`None` + DEBUG log if over `_MAX_BODY_BYTES`); else store + `self._body_bytes += len(body)` + `self._body_order.append(request_id)`; then evict-oldest loop until `self._body_bytes <= _MAX_STORE_BYTES` (pop `_body_order` left, skip already-freed ids, null body, subtract, DEBUG log).
- Reroute `_on_response:182` and `import_from_json:470` to `_store_response`.
- `clear_instance_data:601-604`: on pop, if the response had counted bytes, `self._body_bytes -= …`.
- *Pinning test (`test_network_interceptor_cap.py`, hermetic — no browser):* construct `NetworkInterceptor`, monkeypatch the caps low, feed synthetic `NetworkResponse` bodies via the store path; assert (a) a single over-`_MAX_BODY_BYTES` body is NOT stored (`get_response(...).body is None`), (b) after inserting bodies summing > `_MAX_STORE_BYTES`, `_body_bytes <= _MAX_STORE_BYTES` and the **oldest** ids were evicted (FIFO), (c) `clear_instance_data` returns `_body_bytes` to 0.
- *Verify:* `.venv\Scripts\python.exe -m pytest tests/test_network_interceptor_cap.py -q` then `-m "not integration" -q` (402+new green, cov ≥ 39).

**M9-2 — Capture opt-in / off-by-default + one-step enable.**
- Module-level `_DEFAULT_CAPTURE_BODIES = parse_bool_env("STEALTH_MCP_NETWORK_CAPTURE_BODIES", False)`.
- `_on_response`: compute `capture = _instance_filters.get(instance_id, {}).get("capture_bodies")`; if `capture is None: capture = _DEFAULT_CAPTURE_BODIES`. Wrap the body-fetch block `156-172` in `if capture and tab:` — **preserving the M10a-7b DEBUG log** inside the `except`. Body stays `None` when off.
- `set_capture_filters`: add `capture_bodies: Optional[bool] = None`; **merge** into the existing entry (read-modify-write, don't clobber include/exclude).
- `get_capture_filters`: return resolved `capture_bodies` + `{"body_store_bytes": self._body_bytes, "body_store_max_bytes": _MAX_STORE_BYTES, "body_max_bytes": _MAX_BODY_BYTES}`.
- `server.py`: `set_network_capture_filters` gains `capture_bodies: Optional[bool] = None` (pass through, update docstring); body-consuming tools (`get_response_details`, `search_network_requests`, `export_network_data`) add a `capture_note` when bodies are absent because capture is off ("response-body capture is off; enable via set_network_capture_filters(capture_bodies=True) or STEALTH_MCP_NETWORK_CAPTURE_BODIES=1").
- *Pinning test (`test_network_capture_optin.py` + `test_server_network_tools.py`):* default-off ⇒ `_on_response` stores metadata, `body is None`, and `tab.send(get_response_body)` is **not** called (assert via a spy tab); `set_capture_filters(capture_bodies=True)` ⇒ body fetched + stored; `get_capture_filters` reports the resolved flag + stats; include/exclude survive a `capture_bodies`-only update; the M10a-7b DEBUG log at `_on_response`'s body-fetch except still fires when the fetch raises (carry-through pin).
- *Verify:* `.venv\Scripts\python.exe -m pytest tests/test_network_capture_optin.py tests/test_server_network_tools.py -q` then full `-m "not integration" -q`.

**M9-3 — Observability + docs hardening (lightest checkpoint).**
- Confirm skip/eviction DEBUG logs route through `debug_logger` (durable-to-file via M3); confirm **no new silent `except`** was introduced (so `test_no_silent_excepts.py`, added by M10a-8 with an empty allowlist, stays green — the only `except` M9 touches already carries M10a-7b's log).
- Update the `spawn_browser` / network-tool docstrings to state: body capture is **off by default**, metadata is still captured, `get_response_content` still live-refetches, and the env knobs + `capture_bodies` flag.
- *Verify:* full `-m "not integration" -q` → 402 + new tests green, coverage ≥ 39; `test_no_silent_excepts.py` green.

> M9-1 alone removes the OOM; M9-2/M9-3 add the reduction + ergonomics. If the human defers M9-2, M9-1 still ships the fix.

---

## 4. Breaking changes (0 users → free; documented for the maintainer)

- **Body-capture default flips to OFF.** Fresh instances store response **metadata** (status/headers/content_type/url) but **not body bytes** until enabled. Impact by tool:
  - `list_network_requests`, `get_request_details`, `get_response_details` (status/headers) — **unaffected** (metadata still captured).
  - **`get_response_content` — unaffected** (live CDP re-fetch, store-independent) — the primary "show me a body" path keeps working.
  - `search_network_requests(response_contains=…)` and `export_network_data` bodies — **return empty until capture enabled**; both now emit a `capture_note` so it doesn't read as broken.
- **Tool signature additions:** `set_network_capture_filters` gains `capture_bodies: Optional[bool] = None`; `get_network_capture_filters` returns extra keys (`capture_bodies`, `body_store_bytes`, `body_store_max_bytes`, `body_max_bytes`).
- **Memory profile change (the point):** body-store resident bytes are now `≤ STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES` (default 128 MiB) when capture is on, and ≈ 0 body bytes when off. Imported captures (`import_network_data`) are capped too.
- **New env knobs:** `STEALTH_MCP_NETWORK_CAPTURE_BODIES` (bool, default off), `STEALTH_MCP_NETWORK_BODY_MAX_BYTES` (default 5 MiB), `STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES` (default 128 MiB); `0` = no cap.

---

## 5. Test strategy

**Pinning BEFORE change (per step, hermetic — synthetic `NetworkResponse`, no live browser):**
- **The exact OOM scenario, bounded:** feed bodies whose sum exceeds the total cap; assert `_body_bytes` stays `≤ _MAX_STORE_BYTES` and oldest entries evicted (FIFO). Feed one body over the per-body cap; assert it is not stored (metadata kept, `.body is None`). *(M9-1)*
- **Default-off retains no bodies:** with `_DEFAULT_CAPTURE_BODIES=False` and no per-instance override, `_on_response` stores metadata only and does not call the CDP body fetch (spy tab asserts zero `get_response_body` sends). *(M9-2)*
- **Enable restores full capture:** `set_capture_filters(capture_bodies=True)` ⇒ body fetched + stored + counted; `get_capture_filters` reflects it; include/exclude preserved across a `capture_bodies`-only update. *(M9-2)*
- **Import path is capped:** `import_from_json` of a payload exceeding the total cap ends with `_body_bytes ≤ cap`. *(M9-1)*
- **`clear_instance_data` accounting:** after capture+clear, `_body_bytes == 0`. *(M9-1)*
- **M10a-7b carry-through pin:** force `_on_response`'s body fetch to raise with capture ON; assert the DEBUG record still emits (the log line survived M9's rewrite). *(M9-2)*
- **Tool-layer signal:** `search_network_requests`/`export_network_data` include a `capture_note` when capture is off; `set_network_capture_filters(capture_bodies=…)` round-trips through `get_network_capture_filters`. *(M9-2)*

**Whole-suite gate:** **402 still green + coverage ≥ 39** after every step (new tests add net coverage). `test_no_silent_excepts.py` (M10a-8) stays green — verify no new silent `except`.

---

## 6. Rollback + checkpoint commits

- **Branch:** `audit/fixes-2026-07-02`, serial **after M15's final commit**. Stage-3 discipline: pinning test first each step; full suite green at every checkpoint; **any deviation from a confirmed symbol → STOP and report to the orchestrator** (do not improvise).
- **One commit per step (3):** `M9-1 byte-cap body store` · `M9-2 body-capture opt-in default-off` · `M9-3 capture observability+docs`.
- **Independently revertible:** M9-1 stands alone (the OOM fix); M9-2 reverts alone (restores always-fetch, leaving the cap); M9-3 is docs/guards only. Reverting M9-2 does not touch M9-1's structures.
- **Re-anchor by symbol** (§1.3) before each edit; the M10a-7b DEBUG logs are a **rebase input**, not something to rewrite.
- **PR:** one PR for `{M9}` (three commits), stacked after M11a/M15 on the shared branch — matches the "one PR per fix" convention used across the batch plans.

---

## 7. Risk (blast radius · worst case · early warning)

- **Cap too low silently drops a body the user wanted.** *Blast radius:* a specific response absent from `search`/`export`. *Mitigate:* observable (DEBUG skip/evict logs, now durable via M3; `get_network_capture_filters` surfaces `body_store_bytes` vs max) + tunable (env) + `get_response_content` still live-refetches it. *Worst case:* re-fetch or raise the cap. *Early warning:* "evicted/skipped" DEBUG lines; `body_store_bytes` near max.
- **Off-by-default makes the feature look broken to someone expecting capture** (incl. the maintainer's own muscle memory). *Mitigate:* `get_response_content` unaffected; body-consuming tools emit `capture_note`; `get_network_capture_filters` shows the off state + how to enable; one-step global flip via env. *Worst case:* momentary confusion, resolved by the note. *This is the key human decision — see below.*
- **Eviction mid-read race.** *Mitigate:* all store/evict/read inside `NetworkInterceptor` serialize on `self._lock`; `get_response_content`'s re-fetch is store-independent (no shared state). *Worst case:* none within the lock. *Early warning:* n/a.
- **Cap interacts with the dynamic-hook system that reads bodies (M12a).** If a hook's logic expects `response.body` and capture is off, it sees `None`. *Mitigate:* document; **flagged as an overlap for M12a** (queued next) — hooks that need bodies must enable capture (or M12a live-fetches). Not a file overlap (`dynamic_hook_system.py` ≠ `network_interceptor.py`), a behavioral one.
- **Residual (not M9 scope):** metadata dicts (`_requests`/`_instance_requests`) still grow with **request count** (small per entry, but unbounded over a very long session). F-605 is body **bytes** (the order-of-magnitude item); a metadata count-cap is a candidate **new finding** for a follow-up, noted here, not fixed (no drive-by).

---

## 8. Findings closed

- **F-605 (High — CLOSED).** Per-body + total **byte caps** with FIFO eviction (always-on bound: resident body memory `≤ STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES`, default 128 MiB, independent of instance close) **+ body-capture opt-in/off-by-default** (metadata-only when off). One insertion chokepoint `_store_response` covers both write sites (`_on_response`, `import_from_json`); `clear_instance_data` keeps accounting exact. The adversarial-strengthened gap — "`_on_response` applies NO filter, so `set_capture_filters` cannot bound response-body memory" — is directly fixed: `_on_response` now honors `_instance_filters["capture_bodies"]`.
- **F-606 (Medium — OUT).** Reason: eval-compile-cache lives in **`dynamic_hook_system.py:119-131`** (M12a/hooks territory), a different file, not co-located with the `network_interceptor.py` cap edit. REPORT §Performance names it the "minor" adjacent note explicitly. Fix independently under M12a if desired.
- **F-609 (Low — OUT).** Reason: buffer-outside-lock lives in **`debug_logger.py:282-293`** (`clear_debug_view_safe`), M3/debug_logger territory — plan_M3's 21-vs-22 reconciliation already reworks that exact handler (§1.1 note). Not in `network_interceptor.py`.

---

## Appendix — the four lenses, where each shaped a choice

- **Conventions →** body capture is a *capture filter*, so it lives in the **one** existing home (`_instance_filters`) and is set via the **one** existing tool (`set_network_capture_filters`), not a new tool/dict; env knobs use the **one** canonical parser (`env_utils`), not ad-hoc `os.getenv` (F-602). *(Shaped: rejecting a separate `set_body_capture` path and a hand-rolled parser.)*
- **Deduplication →** a single `_store_response` chokepoint enforces the cap once; both write sites (`_on_response`, `import_from_json`) route through it. *(Shaped: rejecting an inline cap duplicated at each store site.)*
- **Modularity →** all memory-bounding state and logic stay inside `NetworkInterceptor`; the server tool layer only passes a flag and surfaces state. `env_utils` is a stdlib-only leaf → no import cycle. *(Shaped: keeping `spawn_browser`/`setup_interception` untouched.)*
- **Clarity →** `_body_bytes`, `_body_order`, `_store_response`, `capture_bodies` are self-describing; `get_network_capture_filters` reports capture state + store usage so the off-by-default behavior is legible without reading code. *(Shaped: signalling at the tool boundary instead of a silent `None`.)*
