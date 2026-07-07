# Stage 2 Plan — M5a: propagate the CDP hang-avoidance path to the sibling extraction methods (F-142)

- **Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend`
- **Date:** 2026-07-03
- **Batch:** **{M5a}** — single item; executes **10th**, serially (`fix_order[9]`, immediately after **M6**, before M4-Ph1/M5b).
- **Base tree:** post-`plan_M3`(+A1) + `plan_M1` + `plan_M8`(+A1) + `plan_M2` + `plan_M7` + `plan_M11a`+`plan_M15` + `plan_M9` + **`plan_M6`**. Runs from M6's final commit on branch `audit/fixes-2026-07-02`.
- **Status:** **FOLDED INTO M5b** (human decision, 2026-07-03). This plan is NOT executed as a standalone Stage-3 fix. Rationale: the orchestrator verified in source that F-142's "hang" is **bounded** (`_with_cdp_timeout` → `asyncio.wait_for(30s)`, server.py:139-156), so the JS-eval path degrades-then-errors on a stale connection rather than freezing — which removes the "live latent bug, fix now standalone" rationale that split M5a from M5b. Combined with the finding here that only `styles`/`structure` have clean CDP parity (events = capability regression; animations/assets = no CDP path), the CDP convergence of all 5 aspects is done **once, holistically, inside M5b** (the 5→1 consolidation). **This file is retained as the analysis-of-record and a binding input to the M5b planner** (the per-aspect CDP feasibility table in §1/§2, the events-capability tradeoff, and the dedup ruling "converge onto CDPElementCloner, not a 3rd copy").
- **Context (pinned, not re-derived):** LOCAL single-user tool, **0 users, breaking changes are FREE.** Priorities: (1) maintainability (2) operability (3) performance. Element extraction/cloning is a **crown-jewel** feature. Baseline: `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed**, coverage gate **39**. `uv run` is BROKEN — never used.
- **Lenses (ADDENDUM_LENSES.md, binding):** modularity · **deduplication** (one canonical CDP-extraction home — does M5a leave ONE CDP path or add more copies?) · clarity · **conventions** (one way per thing — after M5a the siblings should extract the *same* way, not a third way). Security scope-reduced.

> **What M5a is:** the **bug-only** slice of the M5 cluster — propagate the existing hang-avoidance pattern (`extract_element_styles` → CDP) to the sibling `extract_*` methods that never received it (F-142). It is **NOT** the 700–900 LOC consolidation of the 5 cloner classes into 1 engine (**that is M5b**, gated on M6, planned AFTER this). Fix the hang exposure; do not rewrite the architecture.

---

## 1. Scope

### 1.1 The five siblings + confirmed anchors (re-opened at pinned SHA `2267b83d`)

All in `src/stealth_chrome_devtools_mcp/embedded/element_cloner.py` (class `ElementCloner`, singleton `element_cloner`, 649 lines):

| Method @ HEAD | Extraction mechanism (confirmed) | In M5a? |
|---|---|---|
| `extract_element_styles` **L28–65** | **CDP path (the fix already here).** Body is a thin delegator: `return await self.extract_element_styles_cdp(...)` inside try/except (L53–65). | reference (the template) |
| `extract_element_structure` **L116–169** | **JS-eval.** `js_code = self._load_js_file('extract_structure.js', …)` (L152) → `await tab.evaluate(js_code)` (L153). | **YES** |
| `extract_element_events` **L171–224** | **JS-eval.** `_load_js_file('extract_events.js', …)` (L207) → `await tab.evaluate(js_code)` (L208). | **YES** |
| `extract_element_animations` **L226–279** | **JS-eval.** `_load_js_file('extract_animations.js', …)` (L262) → `await tab.evaluate(js_code)` (L263). | **YES** |
| `extract_element_assets` **L281–356** | **JS-eval (inline).** reads `extract_assets.js` inline (L316–317), string-substitutes, `await tab.evaluate(js_code)` (L325); `fetch_external` uses `requests.get` (L337–350). | **YES** |

The CDP template — `extract_element_styles_cdp` **L541–646** — is the exact pattern to propagate:
```
import nodriver.cdp as cdp
await tab.send(cdp.dom.enable()); await tab.send(cdp.css.enable())
if element is None and selector: element = await tab.select(selector)
node_id = element.node_id  (or: dom.describe_node(backend_node_id=…).node.node_id)   # L578–584
… await tab.send(cdp.css.get_computed_style_for_node(node_id)) …                      # native CDP, no tab.evaluate
```
i.e. **replace `self._load_js_file(...) + await tab.evaluate(js_code)` with `await tab.send(cdp.<domain>.<cmd>(node_id))`**, and (mirroring `extract_element_styles`) keep the public method a thin `try: return await self.<aspect>_cdp(...) except: log + return {"error": …}` delegator.

**Out of the 5 but adjacent — `extract_related_files` L358–417** also uses `tab.evaluate` and is gathered by `clone_element_complete`. The brief names exactly styles/structure/events/animations/assets, so `extract_related_files` is **OUT of M5a**. It is a genuinely different operation (fetches imported CSS/JS over HTTP), has no CDP analogue, and is filed as a **discovery note** (§8) — not fixed here.

### 1.2 The CDP extractors that already exist (the dedup fact that shapes the whole plan)

`src/stealth_chrome_devtools_mcp/embedded/cdp_element_cloner.py` (class `CDPElementCloner`, **no singleton — F-144**, 321 lines) already contains CDP-native per-aspect extractors, `import nodriver as uc` / `uc.cdp.*`, each taking `(tab, node_id)`:

| CDP extractor @ HEAD | CDP domains | Covers which sibling aspect | Output schema |
|---|---|---|---|
| `_get_element_html` **L95–123** | `dom.describe_node`, `dom.get_outer_html` | **structure** (partial) | `{tagName, nodeId, nodeName, localName, nodeValue, outerHTML, attributes:[{name,value}]}` — **camelCase** |
| `_get_children_cdp` **L214–242** | `dom.request_child_nodes`, `dom.describe_node` | **structure** children | `[{html, computed_styles, depth}]` |
| `_get_event_listeners_cdp` **L175–212** | `dom.resolve_node`, `dom_debugger.get_event_listeners` | **events** (partial) | `[{type, useCapture, passive, once, scriptId, lineNumber, columnNumber, hasHandler, …}]` |
| `_get_computed_styles_cdp` **L125–145** / `_get_matched_styles_cdp` **L147–173** | `css.get_computed_style_for_node` / `css.get_matched_styles_for_node` | styles (already done in ElementCloner's own `_cdp`) | — |
| — (none) | — | **animations** | **no CDP extractor exists** |
| — (none) | — | **assets** | **no CDP extractor exists** |

This is the crux: **structure and events have an existing CDP path to reuse; animations and assets have none.** (See §2.1 for the capability gap that explains *why*.)

### 1.3 The to-file twins are covered transitively — no separate fix

`file_based_element_cloner.py` (648 lines): **every `*_to_file` method delegates to the base `ElementCloner` method** (whole-file scan: `.evaluate`=0, `.send`=0). Confirmed pairs: `extract_element_structure_to_file` (L206) → `element_cloner.extract_element_structure`; likewise events/animations/assets `_to_file`; `extract_element_styles_to_file` (L74, L102) → `element_cloner.extract_element_styles`. **Fixing the 4 base methods fixes the 4 to-file twins automatically.** M5a touches **no** `file_based_element_cloner.py` line. (F-141's to-file boilerplate is a separate finding, untouched here.)

### 1.4 Callers / blast radius (server.py, HEAD anchors — re-anchor by symbol, see §1.5)

Every tool entry point wraps the cloner coroutine in `_with_cdp_timeout` (`server.py:139–156`; `CDP_OPERATION_TIMEOUT = 30.0s`, `server.py:71`):

- Direct tools: `extract_element_structure` `:2728`, `_events` `:2764`, `_animations` `:2800`, `_assets` `:2836` (F-202 handle_response site `:2845`) — each `await _with_cdp_timeout(element_cloner.extract_element_<aspect>(...))`.
- To-file tools: `:3403/3439/3475/3511` → `_with_cdp_timeout(file_based_element_cloner.extract_element_<aspect>_to_file(...))`.
- `ElementCloner.clone_element_complete` **L464–539** (`asyncio.gather` of the 6 aspects, `return_exceptions=True`, L529) is reached via **`clone_element_complete_to_file`** tool `:3275` only.
- **Important correction to the brief:** the **`clone_element_complete` TOOL** `:2921` does **NOT** call `ElementCloner.clone_element_complete`; it calls **`comprehensive_element_cloner.extract_complete_element`** (`:2957`, a *different* engine — F-140). So the main `clone_element_complete` tool is **unaffected** by M5a; the 4 siblings' `gather` blast radius is only through `clone_element_complete_to_file`.

### 1.5 Predecessor-shift table (M5a runs 10th; base already contains every predecessor)

| Predecessor | Effect on M5a's targets |
|---|---|
| **M6** (9th, GATES M5a) | Added `tests/test_cloner_schemas.py` + `tests/goldens/*.json` + `tests/fakes.py`. **These goldens are M5a's safety net** (§5). M6 already **quirk-marked** the 4 JS-path siblings (`@pytest.mark.characterization`, docstring: "M5a will intentionally change this — update the golden when that fix lands", plan_M6 §2.1). M5a updates exactly those marked goldens. |
| **M2** (deletions) | `hot_reload`/`reload_status` deleted (`server.py ~:2974`). Tool bodies **below** shift **UP ≈ −66**; surface = **94 tools**. `element_cloner.py` itself untouched by M2 → the L28–646 method anchors above are stable. |
| **M3** (correlation wrapper + M10a logs) | Inserts **above** the tool region → server.py tool anchors shift **DOWN**. Use M3's durable logger/correlation-id for any timeout/error logging M5a adds. `element_cloner.py` line anchors unaffected. |
| **M7** | Flipped `get_tab` `touch_activity` default. Cloner tools call `browser_manager.get_tab(instance_id)` but rely on **no** touch side effect → **no impact**. |
| **M9** | Network capture off-by-default. Cloners don't read network bodies (`extract_element_assets` uses `requests.get` directly) → **no impact**. |
| **M11a / M15** | `in_memory_storage` rename touches `progressive_element_cloner.py`, **not** the 4 siblings → **no impact**. |

**Stage-3 rule:** the `element_cloner.py` / `cdp_element_cloner.py` line anchors above are in files predecessors don't churn and should hold; **re-anchor `server.py` references by `@section_tool(...)` + `async def <name>(`** (M2/M3 shift them). Any drift from a confirmed symbol → **STOP and report**.

### 1.6 Explicit out-of-scope (stated)

- **M5b** — consolidating the 5 cloner **classes** into 1 parameterized engine + converging the 3 complete-element schemas (F-140, F-203/F-601). M5a fixes the hang exposure in 4 sibling **methods**; M5b merges the duplicate **implementations**. **Do NOT consolidate here.**
- **M4** (server.py tool-body refactor), **M12a** (hooks), **M6** (the net itself — M5a *uses* it, doesn't edit it).
- **F-141** (8× to-file boilerplate), **F-144** (CDPElementCloner has no singleton), **F-143** (progressive dead fallback) — related, **not** M5a's.
- `extract_related_files` (not one of the 5) and the `clone_element_complete` **tool** (uses comprehensive engine) — untouched.
- No drive-by refactors; any discovery → a new finding (§8).

---

## 2. Approach + rejected alternatives

### 2.1 The load-bearing discovery: "propagate CDP to all 4" is not uniform — and not surgical for 2 of them

F-142's `fix_direction` says: "Port structure/events/animations/assets to CDP-native calls … **removing the `_load_js_file`/`tab.evaluate` path entirely once parity is confirmed**." The phrase **"once parity is confirmed"** is the whole ballgame. Confirmed at HEAD by comparing each JS file's output surface (`embedded/js/extract_*.js`) against what the CDP domains expose:

| Aspect | JS-eval produces (top-level keys) | CDP-native can produce | Parity? |
|---|---|---|---|
| **styles** | (already CDP) | `css.getComputedStyleForNode` + `getMatchedStylesForNode` fully expose it | ✅ **clean** (why the fix landed here first) |
| **structure** | `tag_name, id, class_name, class_list, text_content, inner_html, outer_html, attributes, data_attributes, dimensions{w,h,top,left,…}, children, scroll_info{scrollWidth,…}` | `_get_element_html` → `tagName, outerHTML, attributes`; `_get_children_cdp` → children | ⚠️ **partial** — CDP **lacks** `dimensions`/`scroll_info` (need `dom.get_box_model`), `text_content`, `inner_html`, split `class_list`/`data_attributes`; **schema camelCase vs snake_case** |
| **events** | `inline_handlers, event_listeners, framework_handlers, detected_frameworks` (React fiber / Vue vnode / Angular / jQuery) | `_get_event_listeners_cdp` (`DOMDebugger.getEventListeners`) → **only** `addEventListener`-registered listeners | ❌ **capability regression** — CDP **cannot** see inline `on*` handlers or **framework** handlers (React attaches one delegated root listener; per-element handlers are invisible to `getEventListeners`) |
| **animations** | `css_animations, css_transitions, css_transforms, keyframe_rules` (walks `document.styleSheets` for `@keyframes`) | **nothing** — CDP `Animation` domain is **event-driven** (`animationCreated`), no synchronous per-node keyframe read | ❌ **no CDP path** |
| **assets** | `images, background_images, fonts, icons, videos, audio` (+ `requests` fetch) | **nothing** — no CDP media/font enumerator; is DOM-traversal + computed-style + HTTP | ❌ **no CDP path** |

**Conclusion:** `styles` moved to CDP cleanly *because CSS is the one aspect the CDP domains fully model.* The other four were left on JS-eval not by oversight alone but because **CDP does not natively expose event handlers / keyframes / media enumeration.** So:
- **structure** — portable to CDP as a *subset* (deliberate golden diff; some fields drop unless `dom.get_box_model` etc. are added).
- **events** — portable only by **losing** inline + framework handler detection (a real regression for a crown-jewel feature).
- **animations, assets** — **not portable** without writing net-new CDP extraction logic that **does not exist anywhere in the repo** — i.e. a rewrite, which is **M5b's** job (F-140 picks CDP-native as canonical and rebuilds the engines around it).

### 2.2 The severity is bounded (state honestly — it changes the calculus)

The word "hang" overstates it. `tab.evaluate` at the sibling call sites is **not** wrapped locally, **but every tool entry point wraps the whole coroutine** in `_with_cdp_timeout` → `asyncio.wait_for(coro, 30.0s)` (`server.py:147–156`). On a misbehaving page the tool **returns a clear error after ≤30 s** ("CDP operation timed out …"), it does **not** wedge. And because `wait_for` is an `await`, the event loop keeps serving other requests during the wait — so the brief's "post-M1 watchdog respawns the wedged backend" blast radius is **low** for these bounded calls (they yield; they don't block the HTTP responder). This is consistent with the REPORT reconciliation ("prior 'CDP hangs forever' REFUTED — timeout wrapper at 59 sites; residual hang is the narrower M1/M7"). **F-142's true content:** 4 aspects run **page JS** to extract (bounded-timeout-then-fail on pathological pages; more fragile and more detectable than native CDP), whereas `styles` uses hang-immune native CDP. Real, worth recording — but not a live crown-jewel-wedging catastrophe.

### 2.3 Chosen approach (per-aspect, feasibility-tiered) + the dedup decision

**Mechanism (all tiers):** mirror the F-142 template exactly — public method becomes a thin `try: return await self.<aspect>_cdp(...) except Exception as e: debug_logger.log_error(...); return {"error": str(e)}` delegator; the `_cdp` worker uses `tab.send(cdp.<domain>.<cmd>(node_id))` with node_id resolved the way `extract_element_styles_cdp` already does (L578–584).

**Dedup decision (conventions/dedup lens):** where a CDP extractor already exists in `CDPElementCloner`, **reuse it** rather than adding a 2nd copy inside `ElementCloner`. `CDPElementCloner` is the engine F-140/M5b makes canonical, so delegating to it is a **down payment on M5b that survives consolidation**, and it avoids deepening the very duplication M5b must untangle. (Cost: `CDPElementCloner` has no singleton — F-144 — so instantiate one at module load or per call; its `__init__` L30–32 is trivial/stateless. And its extractors take `node_id`, so reuse the L578–584 node-id resolution as shared glue.)

**Per-aspect plan:**
- **structure → reuse `CDPElementCloner._get_element_html` (+ `_get_children_cdp` when `include_children`).** Behavior **not** preserved (subset + camelCase) → **deliberate M6 golden update** for `extract_element_structure`, justified in the PR. Optionally add `dom.get_box_model` to restore `dimensions` if the golden diff is judged too lossy (small, in-scope).
- **events → reuse `CDPElementCloner._get_event_listeners_cdp`.** Behavior **not** preserved and **capability-reduced** (loses inline/framework handlers) → deliberate golden update **plus** an explicit tradeoff note. **This is the aspect the human must sign off on** (§2.4).
- **animations, assets → NO existing CDP path.** **Recommended: DEFER to M5b** (writing CDP extractors for them is M5b-scale net-new work; they stay 30 s-bounded meanwhile). If the human requires full standalone M5a, the fallback is to add `extract_element_animations_cdp` / `_assets_cdp` to `CDPElementCloner` (the canonical home) — but flag it as effort-M work that M5b would otherwise own, and note animations likely **cannot** reach `@keyframes` parity via CDP at all.

### 2.4 The scope decision the human must make (pick one)

- **(A) NARROW M5a — recommended default.** Propagate CDP to **structure + events only** (existing extractors), deliberate golden updates, events capability tradeoff accepted; **animations + assets deferred to M5b**. Closes the "1 of 5 fixed" gap to **3 of 5**, ships the surgical, M5b-surviving part, avoids doing M5b's rewrite early. Effort **S**.
- **(B) FULL M5a.** A + write net-new `animations_cdp`/`assets_cdp` in `CDPElementCloner`. Achieves "all 5 CDP." Cost: **effort M**, meaningful net-new extraction code that **M5b will re-own**, animations parity likely unreachable. Contradicts "small, surgical; don't do work M5b throws away."
- **(C) FOLD M5a INTO M5b — also defensible.** Given §2.2 (bounded severity) + that M5b picks CDP-canonical for **all 5** anyway, do the convergence **once** in the consolidation PR with **one** deliberate golden update. Cost of waiting: ≤30 s-bounded degraded extraction on pathological pages for 4 aspects until M5b — low. Cleanest dedup outcome (never grows the duplication).

**Planner recommendation: (A)** if a standalone crown-jewel hardening is wanted now; **(C)** if M5b is imminent and the team prefers one clean convergence. **Not (B).** The rest of this plan is written to execute **(A)** (with the (B) fallback steps marked optional).

### 2.5 Rejected alternatives

1. **Inline the CDP calls into each of the 4 public methods** (no `_cdp` worker, no reuse). *Rejected — dedup/conventions:* copies CDP boilerplate 4× and diverges from the `styles` template; adds to the 5-module duplication right before M5b consolidates it.
2. **Add 4 fresh `extract_element_*_cdp` workers *inside* `ElementCloner`** (mirroring `extract_element_styles_cdp` locally, not reusing `CDPElementCloner`). *Rejected as default:* `ElementCloner.extract_element_styles_cdp` **already duplicates** `CDPElementCloner._get_computed_styles_cdp`/`_get_matched_styles_cdp`; adding 4 more local `_cdp` methods deepens exactly the duplication F-140/M5b removes. (Kept only as a fallback if cross-module reuse proves too coupled in Stage 3.)
3. **Full behavior-preserving CDP reimplementation of all 4** (match every JS-eval key). *Rejected as infeasible:* events' framework/inline handlers and animations' `@keyframes` are **not** expressible via CDP domains — "parity" is impossible for 2 of 4 without re-running page JS (which defeats the purpose).
4. **Delete the JS-eval path immediately** (F-142's "removing … entirely"). *Rejected now:* premature — for events/animations/assets the JS path is the **only** source of some data; deletion belongs in M5b after the canonical schema is decided.
5. **Do nothing / close F-142 as WONTFIX.** *Rejected:* the gap is real and the fix (for structure at least) is cheap and M5b-aligned; but this option's *spirit* (bounded severity) is why **(C)** is a legitimate choice for the human.

---

## 3. Sequencing (independently verifiable; one checkpoint commit per sibling)

> Discipline: each step self-contained, leaves the hermetic suite green; **run the full suite after every step**; deviation from a confirmed symbol → **STOP and report**. Baseline before starting (post-M6 tree): `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → 402 (+M6 tests) green, coverage ≥ 39.

**M5a-1 — Shared glue (node-id resolution + CDPElementCloner access).**
- Factor the element→`node_id` resolution (`extract_element_styles_cdp` L573–584) into a small private helper `_resolve_node_id(tab, element, selector)` in `ElementCloner`, and make `extract_element_styles_cdp` call it (behavior-preserving refactor — its own M6 golden must stay **green**, proving no change). Instantiate/import `CDPElementCloner` (F-144: no singleton) once.
- *Verify:* `… pytest tests/test_cloner_schemas.py -q` (styles golden unchanged) then `-m "not integration" -q`.

**M5a-2 — structure → CDP (first real propagation).**
- Rewrite `extract_element_structure` as a delegator to a `_cdp` worker that resolves node_id and calls `CDPElementCloner._get_element_html` (+ `_get_children_cdp` when `include_children`); optionally `dom.get_box_model` for `dimensions`. **Deliberately update** `tests/goldens/…structure….json` + adjust the quirk-marked structural assertion; PR notes the snake→camel/subset diff with justification.
- *Verify:* `… pytest tests/test_cloner_schemas.py -q` (structure golden updated, rest green) then full hermetic suite.

**M5a-3 — events → CDP (the capability-tradeoff step).**
- Same delegator pattern over `CDPElementCloner._get_event_listeners_cdp`. Update the events golden; PR **explicitly documents** the loss of `inline_handlers`/`framework_handlers`/`detected_frameworks` (§2.1). If the human rejects the tradeoff at review, **stop at structure** (M5a still closes part of F-142).
- *Verify:* `… pytest tests/test_cloner_schemas.py -q` then full hermetic suite.

**M5a-4 — (OPTIONAL, only if human picks (B)) animations + assets → CDP.**
- Add `extract_element_animations_cdp` / `extract_element_assets_cdp` to `CDPElementCloner`; delegate the two public methods; update both goldens; PR flags this as M5b-territory work done early. If not chosen, **animations/assets stay on JS-eval** and their goldens stay green (a filed follow-up references M5b).
- *Verify:* full hermetic suite; coverage delta recorded.

**M5a-5 — Hang-regression test + close-out.** (§5.2)

---

## 4. Breaking changes

**0 users → N/A for compatibility; the schema changes are the point and are gated by M6 goldens.**
- `extract_element_structure` output: **snake_case → camelCase, and a reduced key set** (loses `dimensions`/`scroll_info`/`text_content`/`inner_html` unless restored via `get_box_model`). Named golden diff.
- `extract_element_events` output: reduced to `addEventListener` listeners; **`inline_handlers`/`framework_handlers`/`detected_frameworks` disappear.** Named golden diff **+ capability note** (the one change a reviewer must consciously accept).
- `extract_element_animations` / `extract_element_assets`: **unchanged** under (A)/(C); changed only under (B).
- To-file twins (`*_to_file`): change **transitively** with their base method (same golden semantics) — no separate edit.
- Tool names, signatures, FastMCP schemas: **unchanged** (only return-payload shape moves).

---

## 5. Test strategy

### 5.1 M6 goldens are the net (behavior-preserving OR visible diff)
Each propagated sibling is driven by `test_cloner_schemas.py` against `tests/fakes.py::FakeTab` (records `.evaluate` and `.send`). M5a either keeps a sibling's golden **green** (if ever behavior-preserving) or **updates the quirk-marked golden deliberately** with a PR justification — precisely the workflow M6 §2.1/§4 built for F-142. The **styles** golden and all non-touched cloner goldens must stay **green** at every step (regression tripwire).

### 5.2 New hang-regression test (hermetic, deterministic)
Add a test proving the fixed siblings **took the CDP path, not JS-eval**: configure `FakeTab.evaluate` to **raise** `AssertionError("JS-eval path must not be used post-M5a")` while `FakeTab.send` returns canned CDP objects; assert each fixed sibling returns a valid dict and **never touched `.evaluate`**. This pins the fix without any real timeout/timing (keeps the 0-flaky asset sacred). Extend `FakeTab`'s canned-CDP map in the **one** `tests/fakes.py` home (dedup) — no per-test mocks.

### 5.3 Gates
Keep **402 (+M6) green**; coverage **≥ 39** (M5a adds CDP-branch coverage — record the delta, do **not** ratchet the gate). Integration/real-Chrome fidelity (does the CDP structure match the page pixel-for-pixel) is **out of hermetic scope** → note as an integration follow-up, consistent with M6 §1.4.

---

## 6. Rollback + checkpoint commits

- **Branch:** `audit/fixes-2026-07-02`, serial **after M6's final commit**. Stage-3 discipline: pin/golden first, full hermetic suite green at every checkpoint; **deviation from a confirmed symbol → STOP**.
- **One commit per sibling:** `M5a-1 factor node-id resolution (styles golden unchanged)` · `M5a-2 structure→CDP (golden updated)` · `M5a-3 events→CDP (golden updated + capability note)` · `[optional] M5a-4 animations+assets→CDP` · `M5a-5 hang-regression test`.
- **Rollback is per-sibling and cheap:** each `_cdp` delegation reverts with a single `git revert`; earlier siblings stay green. If events' tradeoff is rejected, drop M5a-3 and ship structure-only. **No production state to unwind.**
- **PR:** one `{M5a}` PR (stacked after M6), matching the "one PR per fix" convention. The deliberate golden diffs are the review surface.

---

## 7. Risk (blast radius · worst case · early warning)

- **A botched CDP propagation breaks a crown-jewel extractor.** *Blast radius:* one sibling returns wrong/empty data. *Mitigate:* per-sibling M6 golden + the §5.2 path-assertion; one aspect per commit. *Worst case:* a golden catches it pre-merge. *Early warning:* an **unmarked** M6 golden goes red (means real regression, not the intended diff — investigate, don't blind-update).
- **events capability regression ships unnoticed.** *Blast radius:* `extract_element_events` silently stops reporting inline/framework handlers. *Mitigate:* §2.1 table + M5a-3 PR note + human sign-off gate (§2.4). *Worst case:* reviewer accepts it knowingly (fine) — the danger is *unknowing* acceptance, which the explicit note prevents. *Early warning:* the events golden diff shows the three keys vanishing.
- **Doing work M5b redoes** (animations/assets rewrite). *Mitigate:* default scope (A) defers them; reuse `CDPElementCloner` so even structure/events work is M5b-aligned (delegation survives consolidation). *Worst case under (B):* effort-M throwaway — hence not recommended.
- **`clone_element_complete_to_file`'s `gather` masks a sibling failure** (`return_exceptions=True`, L529–534 stores `{"error": …}` per aspect). *Mitigate:* the direct `extract_element_*` tools exercise each sibling in isolation (goldens hit those). *Early warning:* a per-aspect `{"error"}` in the file-clone output.
- **Cross-module coupling to `CDPElementCloner`** (F-144 no singleton; `node_id` impedance). *Mitigate:* M5a-1 isolates the glue; `CDPElementCloner.__init__` is trivial. *Worst case:* revert to fallback alt-2 (local `_cdp` workers). *Early warning:* node-id resolution raises on `element` objects lacking `node_id`/`backend_node_id` — covered by a fake-element unit case.

---

## 8. Findings closed

- **F-142 (High — CDP hang-fix landed in 1 of 5 siblings) — partially/most-closed by M5a, with an honest scope caveat.** How: propagate the `extract_element_styles`→CDP delegation pattern to the siblings that have a CDP path — **structure + events** under recommended scope (A) → **3 of 5 on CDP**; **animations + assets** have **no CDP extractor in the repo** and are **deferred to M5b** (documented here, not silently dropped). Under (B) all 5 move to CDP. F-142's `fix_direction` caveat "once parity is confirmed" is the reason: parity is unreachable for events (framework/inline handlers) and animations (`@keyframes`) via CDP — recorded so the follow-up is explicit, not a rediscovery. Cited per brief.
- **F-140 (High — 3 disagreeing complete-element engines) — NOT closed here; M5b's.** M5a *aligns with* it by reusing `CDPElementCloner` (the engine F-140 makes canonical), reducing M5b's later work — but the 5→1 class consolidation and 3→1 schema convergence remain M5b.
- **Discovery note (new finding candidate):** `extract_related_files` (element_cloner.py:358–417) is a **6th** `tab.evaluate` extractor gathered by `ElementCloner.clone_element_complete`, with the same bounded JS-eval exposure and **no** CDP analogue (it fetches imported files over HTTP). Out of M5a's named-5 scope; file for M5b/standalone.
- **Severity reconciliation (record):** F-142's exposure is **bounded to 30 s** by `_with_cdp_timeout` at every tool entry (`CDP_OPERATION_TIMEOUT`, server.py:71/147) and yields the event loop while waiting — so the "hang can wedge the backend / trigger M1 watchdog respawn" blast radius is **low**. This is why scope (C) (fold into M5b) is a legitimate human choice, not a dereliction.

---

### Header recap for the gate
Pinned SHA `2267b83d3efda03f93936db2c34ded33aaa0d701` · 2026-07-03 · batch **{M5a}** · base = post-M3(+A1)+M1+M8(+A1)+M2+M7+M11a+M15+M9+**M6** · **status DRAFT awaiting human approval** (scope decision (A)/(B)/(C) required, §2.4). Lenses: after M5a the propagated siblings extract via the **one** canonical CDP engine (`CDPElementCloner`), not a 3rd hand-rolled path (dedup/conventions); the JS-eval path is removed **only** where CDP parity exists (clarity — no silent capability loss).
