# Stage 2 Plan — RELEASE-FIX-A (Tier-A): fix the silent-correctness / lying-success class

> **Milestone id:** `RELEASE-FIX-A`. **Not** an M-series id — the M-series FIX pipeline
> is declared complete (12/12); this is a **distinct pre-release fix plan that gates
> plan_RELEASE**. Filename stays `plan_RELEASE_FIX_TierA.md`. Commit/plan tags and any
> `# noqa` owner tags use `RELEASE-FIX-A` as the owner string for budget/suppression
> provenance.

## Position & goal — why this plan gates plan_RELEASE

The final 7-Opus architecture review surfaced a class of Tier-A `src/` defects that
**lie**: a tool returns success while silently doing nothing correct (`query_elements`
swallows a stale-node error into `[]`; `create_python_binding` returns `success:true`
for a round-trip that is never wired), or crashes on a legal argument combination that
is then caught and reported as a generic error. These cannot be "released around":
per **plan_RELEASE §8.1, a characterization pin can NEVER satisfy a release claim** —
pinning a lie just freezes the lie. So the release gate must test *genuinely-fixed*
behaviour.

This is the one plan in the RELEASE sequence where **editing `src/` IS the job**
(plan_RELEASE W1–W16 forbid `src/` edits). It runs **BEFORE** W1 so that every later
workstream builds on true behaviour. Each defect is its own independently-revertible,
RED-first chunk.

---

## HARD GATE (base provenance)

- **Execution base = `main` @ `484e143`** (plan_RELEASE + MANUAL_QA_PROTOCOL landed on
  it; the FIX prerequisites `ac89b81` / `b80bd59` / `6b41c63` are proven ancestors).
  Branch from `484e143` (or a later `main`). If `main` has advanced, rebase onto it and
  re-verify the full gate before the first chunk.
- Working checkout may sit on `audit/fixes-2026-07-02-m14`, but this plan's chunks land
  on a **fresh branch off `main`** — one PR, human merge gate per PR.
- Verify the base compiles the gate clean **before** C1:
  `.venv\Scripts\python.exe -m pytest -m "not integration" -q` green, and the lint/type/
  budget gate green (§Verification).

---

## Binding discipline (non-negotiable; applies to every chunk)

- **Four conventions (CLAUDE.md):** one-import (`from stealth_chrome_devtools_mcp.embedded.X import Y`,
  absolute-from-package, no relative); **no `embedded/` module imports `server`** (pass
  `browser_manager`/deps as args); **one-error convention** — tools *raise*
  `tool_errors.ToolError` / `InstanceNotFoundError`, success helpers return values, **no
  new `{"success": False}` dicts** except a named KEEP contract; **one cloner engine**
  (`CDPElementCloner`) — never add extraction elsewhere.
- **`--no-verify` is BANNED.** If a hook/gate fails, fix the cause.
- **Every commit message ends** with:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- **NO new runtime dependencies.** Test-extras only (pytest/pytest-asyncio already
  present). All fixes use stdlib (`json`, `asyncio`, `urllib.parse`, `datetime`) +
  existing `Settings`.
- **Env has one home:** any new knob is a typed field on `settings.Settings`
  (`STEALTH_MCP_*`). `os.getenv`/`os.environ` are ruff-banned.
- **M6-pinned error-message bytes are preserved verbatim.** Do not reword any string a
  golden/characterization test asserts on.
- **File budgets never grow** (`tools/check_file_budgets.py`). Relevant caps:
  `browser_manager.py` = **1532**, `cdp_function_executor.py` = **1012**,
  `cdp_element_cloner.py` = **1013**, `server.py` = **3389**. `element_resolution.py`,
  `network_interceptor.py`, `models.py`, `dom_handler.py`, `settings.py` are **not**
  grandfathered (< 1000-LOC budget). Never pad or raise a cap.
- **Golden discipline:** a golden diff = **STOP and justify** in the same commit, or
  it's a real regression — fix the code. HARD invariants never bend.
- **RED-first (TDD):** for each chunk, write the failing test FIRST (it must fail for the
  *defect's* reason, not a harness error — verify *why* it's red), commit it or stage
  it, then the fix turns it green. One checkpoint commit per chunk; suite green at every
  checkpoint (a chunk may be two commits: `test(RED)` then `fix(GREEN)`, or one commit —
  keep each chunk revert-isolated).
- **Test runner:** `.venv\Scripts\python.exe -m pytest …` (uv's `pytest` console-script
  is broken on this `&`+spaces path — CONTRIBUTING §"uv run pytest caveat").
- **Tests home:** `tests/fakes.py` is THE hermetic harness (fake `Tab`/`Element`,
  positional-only `call_tool(server_mod, name, /, **kwargs)`). Extend it; don't fork it.

---

## Scope

### In scope (this plan)
| Chunk | Finding | Subsystem | One-line fix |
|---|---|---|---|
| C1 | **A1** | dom_handler / element_resolution | route multi-element `select_all` through recovery; stop swallowing -32000 into `[]` |
| C2 | **A5** | cdp_element_cloner | bind `matched_styles` whenever css_rules/pseudo/inheritance requested |
| C3 | **A6** | cdp_element_cloner / js | JSON-encode selector at the `$SELECTOR$` substitution site |
| C4 | **A3** | network_interceptor / settings / DESIGN | FIFO count cap on retained requests + per-`post_data` byte cap |
| C5 | **A7** | browser_manager | `asyncio.wait_for(tab.close(), timeout=2.0)` (LOC net-neutral) |
| C6 | **A4** | cdp_function_executor / **new** python_binding | **wire** `Runtime.bindingCalled` → `call_python_from_js` (real round-trip), machinery extracted to a new module |
| C7 | **A11** | models | **FIX** — aware-UTC `default_factory` (real 1-line fix, not a pin) |
| C8 | **A12** | proxy_forwarder / proxy_utils | PIN here (characterization); fix owned by plan_RELEASE W-B3 |

### Out of scope (explicitly deferred)
- **A2 / A8 / A9 / A10 — cross-OS correctness bugs.** They **cannot be verified** until
  plan_RELEASE **W2** builds Windows/macOS CI runners. A **follow-up FIX plan** fixes
  them against the live cross-OS gate (fixing blind now risks a second wrong guess). Do
  not touch them here.
- plan_RELEASE W1–W16 workstreams (CI matrix, packaging, proxy suite, etc.).

---

## The chunks

> Each chunk: **finding-id · files+lines · RED-first test · fix · acceptance/green-bar ·
> LOC-budget note · revert isolation.** Line numbers are HEAD-relative and MAY have
> drifted — **re-anchor by symbol** (function/marker), not by line, in Stage 3.

---

### C1 — A1: selector-atomicity violation + silent `[]` (HIGHEST SEVERITY)

**Files / lines**
- `embedded/dom_handler.py:88` — `elements = await tab.select_all(selector)` (inside
  `query_elements`, non-XPath branch) bypasses `element_resolution`.
- `embedded/dom_handler.py:201-208` — the enclosing broad
  `except Exception: … return []` swallows the -32000 stale-node `ProtocolException`
  into an empty list (lying "no matches").
- `embedded/dom_handler.py:725` — `iframe_elements = await tab.select_all("iframe")`
  (inside `get_page_content`, `include_frames` branch). Its outer `except` (line ~757)
  already `raise`s, so it does not silently swallow — but it still bypasses recovery.
- `embedded/element_resolution.py` — THE one home for selector resolution. It already
  has `resolve_element` (single, `tab.select`), `resolve_by_text` (`tab.find`), and
  `query_selector_all` (raw NodeIds). It has **no multi-`Element` `select_all` wrapper** —
  that gap is why `dom_handler` calls `tab.select_all` directly.

**Why the memory invariant demands this:** nodriver 0.47 `select`/`find`/`select_all`
resolve in two non-atomic CDP round-trips; a `DOM.documentUpdated` between them raises
`-32000` "Could not find node with given id". PR #35 established the HARD invariant:
**ALL selector resolution routes through `element_resolution.py`**, never `tab.select`/
`find`/`select_all` directly. `query_elements`/`get_page_content` are the two remaining
violators, and the broad `except → []` actively hides the race from users.

**Confirmed contract (ruling — bake into the test):** `query_elements` /
`get_page_content` route through `element_resolution` recovery; on **transient** `-32000`
they **retry-then-recover**; on **persistent** `-32000` after `_MAX_RESOLVES` they
**raise `ToolError`** (never silent `[]`); a **genuine zero-match still returns `[]`**.

**RED-first test** (`tests/test_dom_handler.py` or `tests/test_element_resolution.py`) —
assert **all three** arms:
1. `test_query_elements_recovers_from_transient_stale_node` — fake `Tab.select_all`
   raises `ProtocolException("… Could not find node with given id … -32000")` on the
   first call, returns a fake element list on the second. Assert `query_elements` returns
   the elements (recovery happened) — **RED today** (direct `tab.select_all` has no
   recovery; broad except returns `[]`).
2. `test_query_elements_raises_on_persistent_stale_node` — fake raises `-32000` on every
   call. Assert it raises `ToolError` (the recovery's post-`_MAX_RESOLVES`
   `ProtocolException` converted to `ToolError`) — **NOT** a silent `[]`. **RED today**
   (returns `[]`).
3. `test_query_elements_genuine_zero_match_returns_empty` — fake `select_all` returns
   `[]` (no exception). Assert `query_elements` returns `[]` (a real no-match must stay a
   normal empty list, not become an error). Guards against over-correcting arm 2.

**Fix**
1. Add to `element_resolution.py` a multi-element recovery wrapper mirroring
   `resolve_element`:
   ```
   async def resolve_elements(tab, selector, timeout=None) -> list[Element]:
       async def _do(): return await tab.select_all(selector, timeout=...) if timeout else await tab.select_all(selector)
       return await _resolve_with_recovery(f"select_all {selector!r}", _do)
   ```
   (Keeps the raw-NodeId `query_selector_all` untouched; `query_elements` needs
   `Element` objects with `.attrs`/`.text_all`/`.get_position()`, so it must use the
   `select_all` wrapper, not the NodeId path.)
2. `dom_handler.py:88` → `elements = await resolve_elements(tab, selector)`.
3. `dom_handler.py:725` → `iframe_elements = await resolve_elements(tab, "iframe")`.
4. **Stop swallowing** in `query_elements`: the broad `except Exception: return []`
   (201-208) must no longer turn a resolution failure into `[]`. Narrow it so
   per-selector resolution failure surfaces as `ToolError` (error-convention correct),
   while the *per-element* inner loop keeps its element-level `continue` (a single bad
   element must not abort the whole query — that's not the -32000 case). Recovery in
   `element_resolution` handles the transient race; a genuinely unresolvable selector
   surfaces after `_MAX_RESOLVES` (→ `ToolError`). A **successful** resolve that returns
   an empty `Element` list must still return `[]` (genuine zero-match — do NOT convert
   an empty result into an error).
5. (Note, not required) the XPath branch (`tab.xpath`, line 81) is a separate
   resolution path; if it exhibits the same -32000 race, add an xpath recovery wrapper
   in a follow-up — keep C1 scoped to the flagged `select_all` paths.

**Acceptance / green-bar**
- Both new RED tests pass; `query_elements` retries on -32000 and raises `ToolError` on
  persistent failure (never silent `[]`).
- Full unit suite green, incl. **94-tool tripwire** (`test_tool_registry.py`),
  `test_tool_dispatch.py`, and any existing `query_elements`/`get_page_content` tests
  (a happy-path `select_all` that returns normally must still return the list — the
  wrapper is transparent when no error fires).
- No new `tab.select_all` outside `element_resolution.py` (grep guard optional).

**LOC-budget:** `dom_handler.py` and `element_resolution.py` are NOT grandfathered
(< 1000). `element_resolution.py` grows by the small wrapper — fine.

**Revert isolation:** self-contained (new wrapper + two call-site swaps + one except
narrowing). Revert restores the exact prior behaviour.

---

### C2 — A5: `extract_element_styles` NameError on a legal arg combo

**Files / lines**
- `embedded/cdp_element_cloner.py:519` — `matched_styles = await tab.send(css.get_matched_styles_for_node(node_id))`
  is bound **only inside** `if include_css_rules:`.
- `cdp_element_cloner.py:552` — `if include_pseudo and len(matched_styles) > 3 …`
  references `matched_styles`.
- `cdp_element_cloner.py:562` — `if include_inheritance and len(matched_styles) > 4 …`
  references it too.
- Calling `extract_element_styles(include_css_rules=False, include_pseudo=True)` (or
  `include_inheritance=True`) → `NameError` → caught by the method's
  `except Exception: return {"error": f"CDP extraction failed: …"}` (line ~579) → a
  legal call is reported as a generic failure.

**RED-first test** (`tests/test_cdp_element_cloner.py`):
- `test_extract_styles_pseudo_without_css_rules` — fake `Tab` whose `css.get_matched_styles_for_node`
  returns a shaped tuple; call with `include_css_rules=False, include_pseudo=True`.
  Assert `result["pseudo_elements"]` is present and no `"error"` key. **RED today**
  (returns `{"error": "CDP extraction failed: …matched_styles…"}`).

**Fix:** fetch `matched_styles` whenever **any** of css_rules / pseudo / inheritance is
requested — hoist the `get_matched_styles_for_node` call so it runs under
`if include_css_rules or include_pseudo or include_inheritance:`, and keep the
`css_rules`/`inline`/`attributes` population under `if include_css_rules:`. (Do NOT
guard the reference with `matched_styles = None` + `len(None)` — that just relocates the
crash.) Preserve the `method == "cdp_direct"` schema shape exactly.

**Acceptance / green-bar**
- New RED test green; default-args behaviour (`include_css_rules=True`) byte-identical
  → existing **cloner goldens unchanged** (a golden diff here = STOP).
- Full suite green.

**LOC-budget:** `cdp_element_cloner.py` cap **1013** (currently at cap). Hoisting is a
move, not an add — must stay **net-neutral**. If the hoist adds a line, offset by
removing the now-redundant inner fetch. Verify with `tools/check_file_budgets.py`.

**Revert isolation:** single-method edit.

---

### C3 — A6: JS extractors break on quoted-attribute selectors

**Files / lines**
- Substitution site: `embedded/cdp_element_cloner.py:444-445`
  (`js_code.replace("$SELECTOR$", selector)` then `.replace("$SELECTOR", selector)`) and
  `:732` (`.replace("$SELECTOR", selector)`).
- JS templates embedding the placeholder as a raw quoted literal:
  `js/extract_structure.js:12`, `extract_events.js:19`, `extract_animations.js:11`,
  `extract_styles.js:2`, `comprehensive_element_extractor.js:2` → `const selector = "$SELECTOR$";`
  and `js/extract_assets.js:97` → `})('$SELECTOR', {` (single-quoted).
- A selector like `input[name="email"]` → `const selector = "input[name="email"]";`
  → invalid JS → the extractor throws → generic error.

**RED-first test** (`tests/test_cdp_element_cloner.py`):
- `test_selector_with_double_quote_produces_valid_js` — call the substitution helper (or
  a small extracted `_substitute_selector(js, selector)` seam) with
  `input[name="email"]` and assert the emitted JS parses as a valid single string
  literal — e.g. assert the emitted line equals `const selector = "input[name=\"email\"]";`
  (i.e. equals `f'const selector = {json.dumps(selector)};'`). **RED today** (produces
  the unescaped, quote-breaking form).

**Fix:** JSON-encode the selector at the **Python substitution site** so it becomes a
safe JS string literal. Because the JS templates already wrap the placeholder in quotes,
replace the **quoted** placeholder with `json.dumps(selector)` (which supplies its own
quotes), handling both quote styles:
```
enc = json.dumps(selector)            # e.g. '"input[name=\\"email\\"]"'
for quoted in ('"$SELECTOR$"', "'$SELECTOR$'", '"$SELECTOR"', "'$SELECTOR'"):
    js_code = js_code.replace(quoted, enc)
# defensive: any remaining bare placeholder also becomes a safe literal
js_code = js_code.replace("$SELECTOR$", enc).replace("$SELECTOR", enc)
```
Concretely: replace every quoted form (`"$SELECTOR$"`, `'$SELECTOR$'`, `"$SELECTOR"`,
`'$SELECTOR'`) with `enc`, then encode any remaining bare `$SELECTOR$`/`$SELECTOR` with
`enc` as a defensive fallback. `import json` at module top (stdlib; no new dep). Do this
in one place covering both substitution sites (444-445 and 732) — a shared
`_encode_selector_into(js_code, selector)` helper avoids a second way (convention lens).

**Acceptance / green-bar**
- New RED test green; for a **plain** selector (`div.foo`) `json.dumps` yields the
  identical `"div.foo"` → emitted JS byte-identical → **existing cloner goldens
  unchanged** (golden diff = STOP).
- Full suite green.

**LOC-budget:** `cdp_element_cloner.py` cap **1013**. The helper must be net-neutral —
it *replaces* the two-line `.replace(...).replace(...)` pairs, so extract-to-helper
should not grow the file; if it does, offset. JS files are not LOC-budgeted, but this
fix touches **no** JS file (Python-site only), minimizing churn.

**Revert isolation:** substitution-site-only; behaviour identical for plain selectors.

---

### C4 — A3: unbounded network capture (request count + `post_data` bytes)

**Files / lines**
- `embedded/network_interceptor.py:153-165` (`_on_request`) — builds `NetworkRequest`
  (incl. `post_data=request.post_data`, **no byte cap**) and stores it:
  `self._requests[request_id] = network_request` /
  `self._instance_requests[instance_id].append(request_id)` with **no cap on retained
  request COUNT** → unbounded growth over a long session.
- `network_interceptor.py:234-283` (`_store_response`) — the existing model: per-body
  cap (`network_body_max_bytes`) + total-store FIFO byte cap
  (`network_body_store_max_bytes`) via `_body_bytes`/`_body_order`. **Only response
  bodies are bounded.**
- `settings.py:70-72` — the three body knobs. `__init__` (22-31) holds the deques.

**RED-first tests** (`tests/test_network_interceptor.py`):
1. `test_retained_request_count_is_capped_fifo` — with the new count cap set small,
   capture N > cap requests; assert `len(self._requests)` (and the per-instance list)
   ≤ cap and the **oldest** request_ids were evicted FIFO. **RED today** (all retained).
2. `test_oversize_post_data_is_bounded` — capture a request whose `post_data` exceeds
   the new per-`post_data` byte cap; assert the stored `post_data` is dropped to `None`
   (metadata kept), mirroring the per-body-cap semantics. **RED today** (stored whole).

**Fix**
1. **Settings (env home):** add two typed fields to `settings.Settings`, mirroring the
   body knobs (0 = unbounded), named per the existing convention:
   - `network_request_max_count: int = Field(<default, e.g. 10_000>, ge=0)`
     (`STEALTH_MCP_NETWORK_REQUEST_MAX_COUNT`)
   - `network_post_data_max_bytes: int = Field(<default, e.g. 5 * 1024 * 1024>, ge=0)`
     (`STEALTH_MCP_NETWORK_POST_DATA_MAX_BYTES`)
2. **Request store:** add a single write chokepoint `_store_request(request_id, req,
   instance_id)` (mirror `_store_response`) that:
   - drops `req.post_data` to `None` if it exceeds `network_post_data_max_bytes`
     (encode/len on the string; keep metadata) — reuse the log-debug idiom;
   - appends to a FIFO order (`self._request_order: deque[str]` added in `__init__`) and,
     while `len(self._requests) > network_request_max_count`, evicts oldest: pop from the
     deque, delete from `self._requests`, and remove the id from its
     `self._instance_requests[...]` list (guard against already-evicted ids, mirroring
     `_store_response`'s stale-entry skip).
   - Callers (`_on_request`, and `import_from_json` if it inserts requests) MUST hold
     `self._lock` — same contract as `_store_response`.
   Replace lines 163-165 with `self._store_request(...)` under the existing lock.
3. **DESIGN.md §6** — update the "byte-bounded" section to state that capture is
   **count- and metadata-bounded**, not only body-bytes: add the request-count FIFO cap
   and the `post_data` byte cap and their env vars, alongside the existing body caps.

**Acceptance / green-bar**
- Both RED tests green; response-body cap tests (`_store_response`, F-605) still green
  (untouched).
- **`tests/test_doc_claims.py`** (the M14-S8 doc-claim accuracy harness) must stay
  green — the DESIGN §6 edit must remain factually consistent with the new
  Settings fields/behaviour it asserts.
- Full suite green.

**LOC-budget:** `network_interceptor.py` and `settings.py` are NOT grandfathered — free
to grow within the 1000-LOC budget.

**Revert isolation:** the DESIGN.md edit is a **separate commit** from the code edit
(CONTRIBUTING: docs touch and code touch revert independently). Settings + interceptor
land together (the behaviour needs the knob). Two commits: `fix(A3): …` + `docs(A3):
DESIGN §6 …`.

---

### C5 — A7: `close_instance` unbounded tab teardown

**Files / lines**
- `embedded/browser_manager.py:863-865` — `for tab in browser.tabs[:]: … await tab.close()`
  with **no** `asyncio.wait_for`, unlike the siblings `browser.connection.send(close())`
  (886-889, `timeout=2.0`) and `connection.disconnect()` (899, `timeout=2.0`). A wedged
  renderer hangs teardown → freezes the fleet (DESIGN §2.4: teardown is offloaded so one
  wedged close can't freeze the fleet — this path violates that).

**RED-first test** (`tests/test_browser_manager.py`):
- `test_close_instance_bounds_tab_close` — fake `Tab.close` that `await asyncio.sleep`s
  longer than the timeout (or hangs on an `Event` never set). Assert `close_instance`
  completes within a bounded time and continues to the next teardown phase (does not hang
  the call). **RED today** (awaits `tab.close()` unbounded). Use `asyncio.wait_for`
  around the call under test to assert boundedness deterministically.

**Fix:** wrap line 865 in `asyncio.wait_for(tab.close(), timeout=2.0)`, matching the
siblings. The enclosing `except Exception as tab_err` already catches `TimeoutError`
(subclass of `Exception`) and logs a warning, so a timed-out tab close is logged and the
loop proceeds — no new except needed. `asyncio` is already imported.

**Acceptance / green-bar**
- RED test green; existing `close_instance` phase tests (four-phase teardown) still
  green — the change only bounds phase 2.
- Full suite green.

**LOC-budget — the sharp one:** `browser_manager.py` is grandfathered at **1532** with a
strict **no-grow** cap. The edit replaces one line
(`await tab.close()`) with one line
(`await asyncio.wait_for(tab.close(), timeout=2.0)`, ≈75 chars @ 28-space indent, under
the 88 limit) → **net-zero LOC**. **Do NOT** add an extra `except TimeoutError` branch
(that would grow the file and duplicate the existing catch). Verify
`tools/check_file_budgets.py` prints `browser_manager.py: 1532/1532` after the edit —
**never pad or raise the cap.** If ruff reformats the wrapped call onto two lines,
offset one line elsewhere in the file (e.g. collapse an already-safe multi-line
expression) — but the single-line form fits, so this should not be needed.

**Revert isolation:** one-line change.

---

### C6 — A4: `create_python_binding` is a lying no-op → WIRE THE REAL ROUND-TRIP

**Ruling: option (a) — implement the genuine JS→Python round-trip.** No fail-honestly
shortcut: this is a **new working feature surface landing pre-release**, so its
correctness must be *proven* (a characterization/fixture-only pin cannot satisfy the
eventual release claim — plan_RELEASE §8.1).

**Files / lines (defect today)**
- `embedded/cdp_function_executor.py:739-793` (`create_python_binding`) — calls
  `runtime.add_binding(name)`, then injects a wrapper that overrides `window[name]` to
  call **`window.chrome.runtime.sendMessage`** (a Chrome-extension API, **not** the CDP
  binding channel) and returns `{"success": true, …}`. **No `Runtime.bindingCalled`
  handler is ever registered** (grep confirms), so the binding never fires.
- `cdp_function_executor.py:951-985` (`call_python_from_js`) — the Python side; currently
  **dead** (nothing invokes it).
- `cdp_function_executor.py:100` (`self._python_bindings`), `:1000`
  (`get_function_executor_info` reports `python_bindings`).
- `server.py:2869-2900` — the `@section_tool("cdp-functions") create_python_binding` MCP
  tool. **The tool stays registered** (removing it drops the registry to 93 → breaks the
  94-tripwire).

**How the CDP round-trip actually works (the design to implement)**
1. `Runtime.addBinding(name=…)` exposes `window[name]` as a function; when JS calls
   `window[name](payloadString)` (CDP bindings take a **single string** arg), the CDP
   runtime emits a `Runtime.bindingCalled` event `{name, payload, executionContextId}`.
2. Register a handler: `tab.add_handler(uc.cdp.runtime.BindingCalled, on_binding_called)`
   at `add_binding` time. The handler parses `payload` (JSON: `{callId, args}`),
   dispatches to `call_python_from_js(name, args)` (the existing Python side), gets the
   result, then completes the JS-side promise by
   `Runtime.evaluate` dispatching the wrapper's custom `${name}_response_${callId}`
   event with `{success, result|error}` — the wrapper JS at `:754-782` already listens
   for exactly that event, so the **wrapper stays**; only its *transport* changes from
   the bogus `window.chrome.runtime.sendMessage(...)` to `window[name](JSON.stringify({callId, args}))`.
3. Keep `_python_bindings` (now genuinely used) and the `get_function_executor_info`
   report (now truthful).

**LOC discipline — the resolution (NEW MODULE, not an in-file offset).**
`cdp_function_executor.py` is grandfathered at **1012 (no-grow, never pad/raise)**, and a
working handler + wiring is net-new surface. **Chosen approach: extract the binding
machinery into a new module** `embedded/python_binding.py` (new files are **not**
cap-constrained, and this is the cohesive "JS↔Python binding" concern — a clean seam, not
a dumping ground). The new module owns:
- the wrapper-script builder (moved verbatim from `:754-782`),
- `install_binding(tab, name, python_function, registry)` → `add_binding` + inject
  wrapper + register the `BindingCalled` handler,
- `on_binding_called(...)` → parse payload → `call_python_from_js` → dispatch the
  response event,
- `call_python_from_js` (**moved out** of `cdp_function_executor.py`, so the executor
  file **shrinks** by ~35 LOC — that removed surface is the offset that keeps the file
  comfortably ≤1012 even before counting the wrapper move).
`cdp_function_executor.py` keeps only a **thin delegation**: `create_python_binding`
stores into `self._python_bindings` and calls
`python_binding.install_binding(tab, name, fn, self._python_bindings)`, returning its
result. Net effect on the capped file: **shrinks** (dead `call_python_from_js` +
wrapper-string leave; a short delegation arrives). Verify
`tools/check_file_budgets.py` shows `cdp_function_executor.py ≤ 1012` **after** the move.
Conventions: new module is a leaf — it imports `debug_logger`, `tool_errors`, nodriver;
it **must not import `server`** (pass `tab`/`registry` as args), one-import absolute-from-
package form.

**Error convention:** on install failure, **raise `ToolError`** (drop the
`{"success": False}` returns at `:790,793`); success returns the binding descriptor.

**RED-first tests** (the round-trip must be *proven*, not just "no longer lies"):
1. **Hermetic unit** (`tests/test_python_binding.py`) — `test_binding_called_invokes_python_and_dispatches_response`:
   drive `on_binding_called` with a synthetic `BindingCalled` event
   (`payload=json.dumps({"callId":"x","args":[2,3]})`) against a fake `Tab` and a
   registered Python fn `lambda a,b: a+b`; assert (a) the Python fn ran with `(2,3)`, and
   (b) the handler issued a `Runtime.evaluate` dispatching
   `..._response_x` with `{success:true, result:5}`. **RED today** (no handler exists).
   Also assert the wrapper builder emits `window[name](JSON.stringify(...))`, **not**
   `chrome.runtime.sendMessage`.
2. **Live-Chrome integration** (`tests/…`, `@pytest.mark.integration`) —
   `test_create_python_binding_round_trip_end_to_end`: spawn a real browser, create a
   binding to a Python fn, `evaluate` `await window[name](args)` in the page, assert the
   **JS-visible return value** equals what Python returned. This is the real-coverage
   proof the ruling requires (runs in CI job 3 / Xvfb; needs Chrome).

**Acceptance / green-bar**
- **94-tool tripwire green** (tool stays registered); `test_tool_dispatch.py` green.
- Unit round-trip test green; integration round-trip test green under `-m integration`.
- vulture green: `call_python_from_js` is now **live** (invoked by the handler), not
  allowlisted-dead.
- `check_file_budgets.py`: `cdp_function_executor.py ≤ 1012`; new `python_binding.py`
  under the 1000 budget.
- Full unit suite green.

**LOC-budget:** capped file **shrinks** via the extraction (offset shown above); the new
module carries the added surface, cap-free. **No cap padded or raised.**

**Revert isolation:** the new module + the thin executor delegation + `server.py`
error-convention tweak revert together as one chunk.

---

### C7 — A11: naive datetime vs aware-UTC idle reaper — FIX (1-line)

**Ruling: FIX it (not a pin).** Pinning a known-crashing default freezes the bug
(plan_RELEASE §8.1).

**Files / lines**
- `embedded/models.py:27-28` — `created_at`/`last_activity` use
  `default_factory=datetime.now` (**naive**), while `update_activity` (:37) uses
  `datetime.now(tz=timezone.utc)` (**aware**). The idle-reaper cleanup path (in
  `browser_manager`) subtracting an aware "now" from a naive `last_activity` raises
  `TypeError: can't subtract offset-naive and offset-aware datetimes` (currently caught →
  latent). `timezone` is already imported (`models.py:3`) — no new import.

**RED-first test** (`tests/test_models.py`):
- `test_default_timestamps_are_utc_aware_and_reaper_safe` — construct
  `BrowserInstance(...)`; assert BOTH: (a) `created_at.tzinfo is not None` **and**
  `last_activity.tzinfo is not None`; (b) the **idle-reaper subtraction**
  `datetime.now(timezone.utc) - inst.last_activity` does **not** raise `TypeError` (the
  exact hot-path operation). **RED today** (naive defaults → aware-minus-naive TypeError).

**Fix:** `default_factory=lambda: datetime.now(timezone.utc)` for both `created_at` and
`last_activity`.

**Acceptance / green-bar:** RED test green; no golden pins a naive timestamp; full suite
green.

**LOC-budget:** `models.py` not grandfathered; net-zero.

**Revert isolation:** two-field edit.

---

### C8 — A12: proxy credentials not percent-decoded — PIN (fix owned by W-B3)

**Files / lines**
- `embedded/proxy_utils.py:53-54` — `username = parsed.username or None` /
  `password = parsed.password or None`. `urlsplit` returns userinfo **percent-encoded**
  (does not decode). A proxy URL `http://us%40er:p%40ss@host:8080` yields literal
  `us%40er`/`p%40ss`.
- `embedded/proxy_forwarder.py:231-232` — `credentials = f"{self.username}:{self.password}"`
  then base64 → forwards the still-encoded creds in `Proxy-Authorization`.
- (`:359-366` — the paired auth path per the review.)

**Disposition — CONFIRMED: PIN here; fix owned by plan_RELEASE W-B3 (the proxy suite).**
A12 is proxy-forwarding correctness that W-B3 owns end-to-end (real proxy harness);
fixing it in isolation here risks a second, un-suite-verified proxy touch. This plan only
**captures the behaviour as a characterization pin** and hands the fix to W-B3.

**PIN (this plan):** `tests/test_proxy_utils.py::test_proxy_creds_percent_encoding` with
`@pytest.mark.characterization` + docstring `# finding: A12 — creds not percent-decoded;
fix owned by plan_RELEASE W-B3`. It asserts the **current** (encoded) behaviour so the
W-B3 fix surfaces as a deliberate golden/pin update, not a silent pass. (W-B3's fix will
be `unquote(parsed.username/​password)` via stdlib `urllib.parse.unquote`; this plan does
NOT change `proxy_utils.py`/`proxy_forwarder.py`.)

**LOC-budget:** none (test-only). **Revert isolation:** test-only.

---

## Sequencing (recommended order + rationale)

Order: **C1 → C2 → C3 → C4 → C5 → C6 → C7 → C8.**

1. **C1 (A1) first** — the highest-severity **silent** bug (a success-shaped `[]` that
   hides a race). It restores the PR #35 HARD invariant that all later selector-driven
   behaviour depends on; do it before anything else touches DOM paths.
2. **C2, C3 (A5, A6) next — grouped by subsystem** (both in `cdp_element_cloner.py`).
   Landing them back-to-back means one re-anchor of that file, one golden re-verify pass,
   and a single LOC-budget check for the 1013-cap file. Both are net-neutral, low blast
   radius, and share the "legal call currently reported as generic error" shape.
3. **C4 (A3)** — network subsystem, isolated (`network_interceptor.py` + `settings.py` +
   DESIGN §6). Independent of C1–C3; sequenced after the cloner group to keep subsystem
   churn contiguous.
4. **C5 (A7)** — browser-lifecycle, the one **no-grow-cap** edit (1532). Isolated, one
   line; done alone so the budget check is unambiguous.
5. **C6 (A4)** — the largest chunk (new `python_binding.py` module + `bindingCalled`
   wiring + integration test); sequenced late so the smaller correctness fixes are
   already banked and green, and because it is the only chunk needing a live-Chrome
   integration pass. Must preserve the 94-tripwire (tool stays registered) and keep
   `cdp_function_executor.py` ≤ 1012 via the extraction offset.
6. **C7 (A11)** — trivial FIX; independent, anywhere, placed near the end.
7. **C8 (A12)** — PIN only (fix handed to W-B3); last, as it primarily hands off.

Each chunk is one PR-checkpoint (RED then GREEN), suite green at every checkpoint, so any
chunk reverts alone. Chunks touch disjoint files except C2/C3 (same file, sequenced
adjacently and re-anchored by symbol); C6 adds a new module rather than growing the
capped executor file.

---

## Verification (per chunk AND final)

Run with the venv Python directly (uv `pytest` is broken on this path):

**Unit suite + tripwire (every checkpoint):**
```
.venv\Scripts\python.exe -m pytest -m "not integration" -q
.venv\Scripts\python.exe -m pytest tests/test_tool_registry.py -q      # 94-tool tripwire
.venv\Scripts\python.exe -m pytest tests/test_tool_dispatch.py -q
.venv\Scripts\python.exe -m pytest tests/test_doc_claims.py -q         # after C4's DESIGN §6 edit
```

**Full lint/type/dead-code/budget gate (before opening the PR, and after C5 & C6):**
```
.venv\Scripts\python.exe -m ruff format --check
.venv\Scripts\python.exe -m ruff check
.venv\Scripts\python.exe -m ty check --exit-zero-on-warning src/stealth_chrome_devtools_mcp/
.venv\Scripts\python.exe -m vulture src/stealth_chrome_devtools_mcp/ tools/vulture_allowlist.py
.venv\Scripts\python.exe tools/check_suppression_owners.py
.venv\Scripts\python.exe tools/check_file_budgets.py     # browser_manager 1532/1532, cdp_element_cloner ≤1013, cdp_function_executor ≤1012, new python_binding.py ≤1000
```

**Coverage (CI parity, once at the end):**
```
.venv\Scripts\python.exe -m pytest -m "not integration" --cov=stealth_chrome_devtools_mcp --cov-fail-under=55 -q
```

**Integration (CI job 3 / Xvfb — REQUIRED for C6's real-round-trip proof):**
```
.venv\Scripts\python.exe -m pytest -m integration --timeout=120 -q     # needs Chrome; proves A4 JS→Python round-trip end-to-end
```

**Green-bar checklist**
- Full unit suite green; **94-tool tripwire** green (registry count unchanged — C6 keeps
  the tool registered).
- **Cloner goldens unchanged** by C2/C3 (any golden diff = STOP-and-justify; for these
  two it should be zero — plain-selector/default-arg paths are byte-identical).
- `test_doc_claims.py` green after the DESIGN §6 edit (C4).
- `check_file_budgets.py` green — **no cap grew**; `browser_manager.py` exactly 1532;
  `cdp_function_executor.py` ≤ 1012 (shrunk by the C6 extraction); new
  `python_binding.py` under the 1000 budget.
- vulture green — C6 makes `call_python_from_js` **live** (invoked by the new
  `bindingCalled` handler), not dead/allowlisted.
- **C6 integration round-trip green** (real JS→Python return value observed) — the
  new feature's coverage is real, not fixture-only (plan_RELEASE §8.1).
- Integration suite also run once locally for C1/C5 (they touch live-browser paths) —
  sanity pass.

---

## Resolved rulings (baked into the chunks above — no open decisions remain)

1. **A4 (C6):** **WIRE IT UP** — register `Runtime.bindingCalled` → `call_python_from_js`
   for a genuine round-trip. LOC resolved by **extracting a new module**
   `embedded/python_binding.py` (cap-free) + thin executor delegation, so
   `cdp_function_executor.py` **shrinks** and stays ≤ 1012. Proven by a hermetic unit
   test **and** a live-Chrome integration test (real coverage, no pin). Tool stays
   registered (94-tripwire intact).
2. **A11 (C7):** **FIX** — `default_factory=lambda: datetime.now(timezone.utc)`; RED test
   asserts tz-aware defaults **and** reaper-subtraction safety.
3. **A1 (C1):** confirmed contract — route through `element_resolution` recovery;
   transient `-32000` → retry-then-recover; persistent `-32000` → **raise `ToolError`**;
   genuine zero-match → **`[]`**. Test asserts all three arms.
4. **A12 (C8):** **PIN here** (characterization); fix owned by plan_RELEASE **W-B3**.
5. **Milestone id / filename:** milestone **`RELEASE-FIX-A`** (NOT M16 — the M-series FIX
   pipeline is complete 12/12; this is a distinct pre-release fix gating plan_RELEASE).
   Filename stays `plan_RELEASE_FIX_TierA.md`.
6. **Execution base:** `main` @ **`484e143`** (plan_RELEASE + MANUAL_QA_PROTOCOL landed;
   FIX prereqs `ac89b81`/`b80bd59`/`6b41c63` are proven ancestors).

---

### Header recap for the gate
Milestone **`RELEASE-FIX-A`**; execution base `main`@`484e143`; 8 chunks (C1 A1 · C2 A5 ·
C3 A6 · C4 A3 · C5 A7 · C6 A4 · C7 A11 · C8 A12); each RED-first + revert-isolated;
**A4 wired** (new `python_binding.py`, integration-proven); **A11 fixed**; **A12 pinned**
(fix → W-B3); A2/A8/A9/A10 deferred to a post-W2 cross-OS FIX plan; `--no-verify` banned;
commits `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`; no new runtime deps;
`browser_manager.py` 1532 & `cdp_function_executor.py` 1012 no-grow (C6 shrinks it);
94-tool tripwire + cloner goldens must stay green.
