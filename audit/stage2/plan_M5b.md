# Stage 2 Plan — M5b: consolidate the 5 cloner engines into 1 canonical engine

- **Status:** **APPROVED by human 2026-07-03 (approve as-is).** Gate rulings: Q1 events transport = **KEEP JS-eval** (per-aspect best-transport inside the one engine; zero capability loss); Q2 F-744 tool renames = **DEFER to M4-Ph2** (+ sharpen the 4 docstrings now); Q3 = keep `FileBasedElementCloner` class name (default accepted).
- **ADDENDUM — structure transport ruling (human, 2026-07-18, at M5b-1 execution):** **KEEP JS-eval for `structure` too** (not CDP as §2.1 originally scheduled). Same rationale the human already accepted for events (Q1): the JS path is bounded by the ≤30 s `_with_cdp_timeout` and CDP-structure would drop `scroll_info` + force snake→camel and re-derivation of `id`/`class_list`/`text_content`/`inner_html`/`data_attributes` from `outerHTML`+`attributes` — a capability/ergonomics regression on the crown-jewel feature for no benefit the timeout wrapper doesn't already provide. **Consequence:** the ONLY CDP aspect is `styles`; `structure`/`events`/`animations`/`assets`/`related_files` are all verbatim JS moves. The `extract_element_structure` golden **stays GREEN** (was §5.2/§2.1's one scheduled structure change — now retired); the only deliberate golden changes are the complete-schema 3→1 convergence (F-140) and (if normalized) the `file_based` summary contract. Lowers risk (no new CDP structure code).
- **Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend` (HEAD == pinned SHA).
- **Date:** 2026-07-03.
- **Batch:** **{M5b}** — executes **12th** (last of the enumerated Stage-3 serial order); base = pinned SHA + M3(+A1) + M1 + M8(+A1) + M2 + M7 + M11a+M15 + M9 + M6 + M12a + **M4-Ph1(+A1)**.
- **Context (pinned, not re-derived):** LOCAL single-user tool, **0 external users, breaking changes are FREE.** Priorities: (1) maintainability, (2) operability, (3) performance (order-of-magnitude only). Element cloning is the **crown-jewel** feature *and* the worst duplication in the repo. Test baseline at HEAD: **402 passed** via `.venv\Scripts\python.exe -m pytest -m "not integration" -q` (~64s); by my base tree the count is 402 + all predecessor test additions (M6 alone adds the dispatch/cloner/bug-prone nets). Coverage gate **fail_under=39**. `uv run` is **BROKEN** in this checkout (path has `&` + spaces) — **ALWAYS** use `.venv\Scripts\python.exe` directly.
- **Lenses (ADDENDUM_LENSES.md, binding — they weight within priority 1):** **Modularity** (each module understandable in isolation) · **Deduplication** (each concept in exactly one place) · **Clarity** (self-describing names; renames are legitimate fixes) · **Conventions** (one way per thing; *a fix that introduces a second way of doing something is a defect*). Security: no re-architecture — only quote-backed regressions (none in scope here).
- **Findings in scope:** F-140 (High), F-203 (High), F-601 (High), F-142 (High, folded from M5a), F-744 (Medium, routed), F-141 (Medium), F-143 (Medium), F-144 (Low).

> **What M5b is:** the 5→1 consolidation of the element-cloning subsystem — **converge onto `CDPElementCloner` as the canonical extraction engine** (binding dedup ruling), express file/in-memory/progressive behavior as thin adapters over it, delete the redundant whole-file engines, and consciously decide the events transport tradeoff **in full context** (do not silently regress). Target from STAGE2_BRIEF: **one canonical engine, −700–900 LOC.**

---

## 0. What was verified at HEAD before writing (results inline)

All anchors below were confirmed in source at the pinned SHA via AST + targeted reads. The path is `src/stealth_chrome_devtools_mcp/embedded/` (the brief's older `src/...` shorthand and the LOC-only `embedded/` shorthand both resolve here).

### 0.1 The 5 modules — actual LOC + public method inventory (the mirror matrix)

| Module | LOC @ HEAD | Class / singleton | `.evaluate` | `.send` | Public surface (confirmed via AST) |
|---|---|---|---|---|---|
| `element_cloner.py` | **649** | `ElementCloner` / `element_cloner` (L648) | 5 | 5 | `extract_element_styles`(L28, CDP delegator), `extract_element_structure`(L116, JS), `extract_element_events`(L171, JS), `extract_element_animations`(L226, JS), `extract_element_assets`(L281, JS), `extract_related_files`(L358, JS+HTTP), `clone_element_complete`(L464, `asyncio.gather` of 6), `extract_element_styles_cdp`(L541, CDP worker) |
| `file_based_element_cloner.py` | **648** | `FileBasedElementCloner` / `file_based_element_cloner` (L648) | **0** | **0** | 8 `*_to_file` twins (styles/complete/structure/events/animations/assets/related_files/clone_complete) + `list_clone_files`(L595) + `cleanup_old_files`(L625); private `_generate_filename`(L59), `_save_to_file`(L137), `_safe_process_framework_handlers`(L50) |
| `comprehensive_element_cloner.py` | **344** | `ComprehensiveElementCloner` / `comprehensive_element_cloner` (L344) | 1 | 0 | **ONE** method `extract_complete_element`(L39–342, **304 LOC** monolithic inline JS) |
| `cdp_element_cloner.py` | **321** | `CDPElementCloner` (**NO singleton — F-144**) | 0 | 13 | `extract_complete_element_cdp`(L33), `_get_element_html`(L95), `_get_computed_styles_cdp`(L125), `_get_matched_styles_cdp`(L147), `_get_event_listeners_cdp`(L175), `_get_children_cdp`(L214) + 4 dict-mapper helpers |
| `progressive_element_cloner.py` | **266** | `ProgressiveElementCloner` / `progressive_element_cloner` (L263) | 0 | 0 | `clone_element_progressive`(L30, calls comprehensive), `expand_styles/events/children/css_rules/pseudo_elements/animations`, `list_stored_elements`(L231), `clear_stored_element`(L250), `clear_all_elements`(L258); private `_get_store`/`_save_store` |
| **TOTAL** | **2,228** | | | | (finding's "2,223" differs only by trailing-newline counting — immaterial) |

**The duplication, precisely:** three engines answer "extract COMPLETE element data" with three different schemas (F-140): `ElementCloner.clone_element_complete` → flat multi-key (gather of the 5 `extract_*`); `ComprehensiveElementCloner.extract_complete_element` → flat camelCase incl. `selector,url,timestamp`; `CDPElementCloner.extract_complete_element_cdp` → nested snake_case under `element`. Additionally, **styles-level** CDP extraction is duplicated: `ElementCloner.extract_element_styles_cdp` (L541-646) and `CDPElementCloner._get_computed_styles_cdp`+`_get_matched_styles_cdp` are two independent CDP implementations of the same thing.

### 0.2 Which MCP tool calls which engine (confirmed via server.py bodies at HEAD)

| MCP tool (HEAD line) | Engine.method it calls |
|---|---|
| `extract_element_styles`(2667), `_structure`(2703), `_events`(2739), `_animations`(2775), `_assets`(2811) | `element_cloner.extract_element_<aspect>` |
| `extract_element_styles_cdp`(2849) | `element_cloner.extract_element_styles_cdp` |
| `extract_related_files`(2886) | `element_cloner.extract_related_files` |
| **`clone_element_complete`(2921)** | **`comprehensive_element_cloner.extract_complete_element`** ← confirms the brief's correction; NOT `ElementCloner.clone_element_complete` |
| `clone_element_progressive`(3062), `expand_*`(3085–3207), `list_stored_elements`(3208), `clear_stored_element`(3219), `clear_all_elements`(3235) | `progressive_element_cloner.*` |
| `clone_element_to_file`(3246) | `file_based_element_cloner.clone_element_complete_to_file` |
| `extract_complete_element_to_file`(3281) | `file_based_element_cloner.extract_complete_element_to_file` |
| **`extract_complete_element_cdp`(3309)** | **`CDPElementCloner()` constructed fresh per call**, then `.extract_complete_element_cdp` (F-144) |
| `extract_element_<aspect>_to_file`(3342–3521), `list_clone_files`(3522), `cleanup_clone_files`(3533) | `file_based_element_cloner.*` |

**Critical:** `ElementCloner.clone_element_complete` (L464) is reached **only** through the `clone_element_to_file` tool (via `file_based_element_cloner.clone_element_complete_to_file`). No tool calls it directly — the direct "complete" tool goes to `comprehensive`.

### 0.3 `extract_element_styles` thin-delegator (the propagation template) + `extract_element_styles_cdp`

`ElementCloner.extract_element_styles`(L28-65) body is exactly (L53-65): `try: return await self.extract_element_styles_cdp(...) except Exception as e: debug_logger.log_error("element_cloner","extract_styles",e); return {"error": str(e)}`. `extract_element_styles_cdp`(L541-646) resolves node_id at **L573-584** (`if element is None and selector: element = await tab.select(selector)`; then `node_id = element.node_id` / else `describe_node(backend_node_id=...)`) — the shared glue. `CDPElementCloner._get_*` methods take `(tab, node_id)`, so reusing them requires this same node-id resolution.

### 0.4 Dead dual-schema fallback (F-143) — confirmed unreachable

`clone_element_progressive`(L40-42) calls `comprehensive_element_cloner.extract_complete_element`, whose output is **flat** (`styles`, `eventListeners`, `cssRules`, `tagName` — verified by reading the comprehensive JS monolith's return shape). The fallback at L56-70 reads `full_data.get("element", {}).get("computed_styles"|"event_listeners"|"matched_styles"...)` — the **nested CDP schema** that only `CDPElementCloner` produces and that is **never wired** into `ProgressiveElementCloner`. So every `full_data.get("element",{}).get(X) or full_data.get(Y)` picks the right-hand (flat) branch; the left branch is dead. Same pattern repeats in `expand_styles`(93-104), `expand_events`(141-146), `list_stored_elements`(231-248).

### 0.5 The 8 `_to_file` boilerplate methods (F-141) + transitive inheritance

`file_based_element_cloner.py` has **0** `.evaluate` and **0** `.send` calls: every `*_to_file` method delegates to a base extractor (`element_cloner.extract_element_*` for the 6 aspect/related twins, and its own `ComprehensiveElementCloner()` instance for `extract_complete_element_to_file`) then repeats the identical 4-step shape: extract → `_generate_filename(prefix)` → `_save_to_file(data, filename)` → hand-build a bespoke `summary` dict. The filename+save pair is copy-pasted verbatim at 8 sites (L112-113, 180-181, 247-248, 308-309, 368-369, 428-429, 490-491, 541-542). **Fixing a base extractor fixes its `_to_file` twin transitively.**

### 0.6 The singleton construction sites (F-144)

Module-level singletons: `element_cloner`(element_cloner.py:648), `file_based_element_cloner`(:648), `comprehensive_element_cloner`(comprehensive:344), `progressive_element_cloner`(progressive:263). **`CDPElementCloner` has NONE** — the `extract_complete_element_cdp` tool (server.py:3309 region, HEAD anchor) does `cdp_cloner = CDPElementCloner()` inside the tool body per call. Also `FileBasedElementCloner.__init__`(:48) constructs its **own private** `ComprehensiveElementCloner()` instead of importing the shared singleton. Both `__init__`s are no-ops (`pass`), so it is currently harmless — but an inconsistent construction convention.

### 0.7 M6's cloner net (the gate) — confirmed spec from plan_M6.md

`tests/test_cloner_schemas.py` + `tests/goldens/` are **created by M6** (which runs 9th, before me), so they exist in my base tree though `tests/goldens/` is empty at the pinned SHA. Two-tier design (plan_M6 §2.1/§2.2/§5.2):
- **Tier (a) — HARD structural invariants** (run every suite; violating one = stop signal): top-level key set, error shape `{"error": …}`, and the **nesting difference** distinguishing the 3 complete-element engines (flat vs flat+camelCase vs nested-under-`element`).
- **Tier (b) — SOFT per-engine goldens** in `tests/goldens/<engine>.json`, one per engine, **quirk-marked** (`@pytest.mark.characterization`, docstring naming the F-id + which fix will change it), updated **deliberately** in the touching PR as a reviewed diff.
- **Coverage of the net:** the 3 complete-element engines (F-140), the 5 `ElementCloner.extract_*`, `ProgressiveElementCloner.expand_*` + `list_stored_elements`, the `FileBasedElementCloner` to-file `summary` shape. **structure + events goldens are already M5a-marked "will change"; styles + the rest are the green tripwire.**
- The dispatch net (`test_tool_dispatch.py`) asserts the **F-108 tool count = 94** and the `.fn` unwrap seam. My changes must keep the count at 94 unless I deliberately change it (I do not — see §2.4).

### 0.8 Two existing behavioral tripwires beyond M6 (must survive or be deliberately updated)

`tests/test_element_cloner_output_dir.py` and `tests/test_clone_output_dir.py` both **instantiate `FileBasedElementCloner`** and assert its `output_dir` contract (default = `default_clone_output_dir()` = `~/.stealth-mcp/element_clones`; relative anchored to package; absolute verbatim; mkdir-on-init; never inside the installed package — GitHub issue #5). They also pin `ResponseHandler.clone_dir` sharing that helper. **Binding constraint:** the file-writing capability and the `FileBasedElementCloner(output_dir=…)` constructor must remain importable and behavior-identical, or these two files get **deliberately updated** with justification (they are behavioral, not characterization-quirk, so I keep them green rather than update them — see §5).

### 0.9 Predecessor shifts confirmed at HEAD (see §1.3 for the symbol-anchored table)

- **M4-Ph1 STEP 0** rewrites every cloner module's imports to `from stealth_chrome_devtools_mcp.embedded.X import Y`; collapses `file_based_element_cloner.py:21-24`'s try/except dual-import to the one form; removes the `sys.path.append` hacks in comprehensive(:19) and file_based(:15-16). At HEAD these are still bare (`from element_cloner import element_cloner`, etc., confirmed at server.py:27-45) — I re-anchor post-STEP-0.
- **M4-Ph1 A1** (approved full 22-site envelope sweep) converts the cloner tool bodies' error paths: `clone_element_complete`'s invalid-JSON `raise` → `raise ToolError(...)`; all ~40 instance-not-found `raise Exception("Instance not found: {id}")` → `_require_tab`/`_require_browser` → `InstanceNotFoundError`; **but KEEPS** `clone_element_to_file`'s and `expand_children`'s input-validation value-returns (`{"error":"Invalid ... JSON/max_count/depth_range"}`) as deliberate input-validation contracts. So at my base tree the cloner tool bodies use `_require_tab`/`ToolError` — my edits preserve that envelope.
- **M4-Ph1 C1** created `embedded/clone_storage.py` = server.py-side profile/clone **disk** storage (trash/quota/eviction). **NOT a cloner engine. I must not converge cloner code into it.**
- **M4-Ph1 C5** made `_with_cdp_timeout` the single CDP-timeout wrapper at server.py call sites. Every cloner tool call stays routed through it.
- **M7 / F-745** flipped `browser_manager.get_tab`/`get_browser` `touch_activity` default to `False` (M7's file, not mine). F-164 cfe half is in `cdp_function_executor.py` (M7's file, not mine). No M5b action.

---

## 1. Scope

### 1.1 Files touched

| File | Change | Findings |
|---|---|---|
| `src/stealth_chrome_devtools_mcp/embedded/cdp_element_cloner.py` | **The canonical engine.** Absorb the per-aspect + complete extraction as the single home; add a module-level singleton `cdp_element_cloner`; add the node-id resolution helper; add a `to_file` adapter seam (or a thin file-writer collaborator). Grows modestly; net still deletes far more than it adds. | F-140, F-601, F-144 |
| `src/stealth_chrome_devtools_mcp/embedded/element_cloner.py` | **DELETE the whole file** at the end, after the aspect tools are re-pointed at the canonical engine and `extract_related_files` (the one no-CDP-analogue aspect) is rehomed. Until deletion, it shrinks as methods move/delegate. | F-140, F-203, F-601 |
| `src/stealth_chrome_devtools_mcp/embedded/comprehensive_element_cloner.py` | **DELETE the whole file.** Its sole method `extract_complete_element` is superseded by the canonical engine's complete-extraction; the `clone_element_complete` tool re-points. | F-140, F-601 |
| `src/stealth_chrome_devtools_mcp/embedded/file_based_element_cloner.py` | **Collapse the 8 boilerplate `_to_file` methods to one `_extract_and_save(prefix, extraction_coro, summary_fn)` helper** + ~5-line call sites; re-point its base-extractor calls at the canonical engine; import the shared engine singleton instead of constructing its own `ComprehensiveElementCloner()`. **Keep the `FileBasedElementCloner` class + `output_dir` contract** (§0.8 tripwires). May be deleted-and-replaced by a `to_file` adapter **only if** the `output_dir` contract and `FileBasedElementCloner(output_dir=…)` constructor are preserved for the two existing tests — default plan keeps the class, dedups its body. | F-141, F-601, F-144 |
| `src/stealth_chrome_devtools_mcp/embedded/progressive_element_cloner.py` | Re-point `clone_element_progressive` at the canonical engine's complete-extraction; **delete the dead dual-schema `.get("element",{})...` fallbacks** (F-143) and read the one canonical shape; keep the stateful `expand_*`/store surface. | F-143, F-601 |
| `src/stealth_chrome_devtools_mcp/embedded/server.py` | Re-point the cloner tool bodies at the canonical engine + the surviving adapters; construct the `cdp_element_cloner` singleton import (F-144) so `extract_complete_element_cdp` stops doing per-call construction; preserve the M4-Ph1 `_require_tab`/`ToolError`/`_with_cdp_timeout` envelope; sharpen the 4 clone/extract docstrings per F-744 (names unchanged in Ph1 — §2.5). | F-144, F-744 |
| `tests/test_cloner_schemas.py` + `tests/goldens/*.json` | Update the SOFT goldens that legitimately change (enumerated §5.2); keep HARD invariants + untouched goldens green. | (gate) |
| `tests/test_element_cloner_output_dir.py`, `tests/test_clone_output_dir.py` | **Keep green** (do not update) by preserving the `FileBasedElementCloner`/`output_dir` contract; update **only** if a rename of the class is chosen (not the default). | (tripwire) |

### 1.2 Explicitly OUT of scope

- **`clone_storage.py`** — server.py-side disk quota/GC (M4-Ph1 C1). The naming boundary is crisp: `clone_storage` = disk trash/quota/eviction of clone *files*; the M5b engine = element *extraction*. **Do NOT converge the engine into it.**
- **MCP tool NAME renames** (F-744's `clone_*` → `extract_*` at the tool surface, and F-760/F-743) — **deferred to M4-Ph2** (rename-once ruling, §2.5). Ph1 sharpens docstrings only; the M6 count-94 tripwire names stay stable.
- **`cdp_function_executor.py`** (M7's F-164 cfe half, F-745) — different subsystem.
- **F-165** (dup HeaderEntry loop in `dynamic_hook_system._execute_hook_action`) — M9/M12a territory, different subsystem, stays open.
- **The server.py registry/error-envelope/storage extraction** (M4-Ph1 owns) — I consume its outputs, I do not redo them.
- **Real-Chrome value fidelity** (does the CDP output match the page pixel-for-pixel) — integration, not hermetic; note as an integration follow-up (consistent with M6 §1.4).
- **`extract_related_files`'s HTTP-fetch behavior** — it has no CDP analogue (fetches imported CSS/JS over HTTP). It is **rehomed, not rewritten** (§2.2).

### 1.3 Predecessor-shift table (symbol-anchored — re-anchor in Stage 3 by symbol, not line)

| Predecessor | Symbol @ its output | Shift for M5b |
|---|---|---|
| **M4-Ph1 STEP 0** | every cloner module's `import` lines | Now `from stealth_chrome_devtools_mcp.embedded.X import Y`. `file_based_element_cloner.py:21-24` try/except dual-import **collapsed** — do **NOT** re-add. `sys.path.append` in comprehensive/file_based **removed**. Re-anchor my new imports to this one form. |
| **M4-Ph1 A1 (C3a/C3b)** | cloner tool bodies in server.py | `clone_element_complete` invalid-JSON → `ToolError`; instance-not-found → `_require_tab`→`InstanceNotFoundError`; `clone_element_to_file`/`expand_children` input-validation value-returns **KEPT**. My tool-body re-points preserve these. |
| **M4-Ph1 C1** | `embedded/clone_storage.py` (new) | Exists; is storage, not a cloner. Keep the boundary. cli.py `server._*` storage calls now point at `clone_storage.*` — not my concern. |
| **M4-Ph1 C5** | `_with_cdp_timeout` | Single CDP-timeout wrapper at server.py call sites; every cloner tool call stays wrapped in it. |
| **M6** | `tests/fakes.py` (`FakeTab` records `.evaluate`+`.send`), `tests/conftest.py` fixtures, `tests/test_cloner_schemas.py`, `tests/goldens/` | My pinning tests reuse `FakeTab` (the one home). My golden updates go through this net. F-108 count = **94**. |
| **M2** | `server.py` | `hot_reload`/`reload_status` deleted (not cloner tools). |
| **M15** | `persistent_storage` → `in_memory_storage` | `progressive_element_cloner.py:14` references `in_memory_storage` (its store backend). My progressive edits keep that reference. |
| **M7 / F-745** | `browser_manager.get_tab`/`get_browser` | `touch_activity` default now `False`. `_require_tab` (M4-Ph1) calls `get_tab` — unaffected semantically for my purpose. |
| **M3** | `section_tool` wrapper | Correlation-id stamp; schema byte-identical. My tool-body edits leave the FastMCP schema untouched (payload shape only moves). |

---

## 2. Approach + rejected alternatives

### 2.0 The end-state shape (what survives, what dies)

**One canonical engine: `CDPElementCloner` in `cdp_element_cloner.py`** (renamed conceptually to "the element extraction engine"; the *class* may be renamed to `ElementExtractor` for clarity — a legitimate clarity-lens fix — but see §2.5 for why the *module/tool* names stay put in Ph1). It owns:
- **Per-aspect extraction** for all 5 aspects (styles, structure, events, animations, assets), each a public method with a thin `try: return await self._<aspect>_cdp_or_js(...) except: log + {"error": …}` delegator, mirroring the `extract_element_styles` template (§0.3). Transport per aspect is decided in §2.1.
- **Complete extraction** — one `extract_complete_element(tab, selector, ...)` that composes the per-aspect methods (replacing all three of today's disagreeing complete engines) and returns **one** canonical schema.
- **The node-id resolution helper** `_resolve_node_id(tab, element, selector)` (factored from `extract_element_styles_cdp` L573-584) — one home for the impedance-match every CDP call needs.
- **A module-level singleton** `cdp_element_cloner` (F-144).
- **A `to_file` seam**: file-writing stays a *thin adapter* (`FileBasedElementCloner` keeping its `output_dir` contract) that calls the engine and writes — not a parallel extraction implementation.

**Dies:** `element_cloner.py` (whole file), `comprehensive_element_cloner.py` (whole file). `file_based_element_cloner.py` and `progressive_element_cloner.py` **survive as thin adapters** over the engine (they carry genuinely distinct behavior — disk output + stateful expansion — that is not extraction duplication).

**Why `CDPElementCloner` is the canonical home (binding ruling, and independently correct):** it is the only engine already CDP-native (13 `.send`, 0 `.evaluate`); CDP extraction is hang-immune and more accurate/less detectable than page-JS; F-140's own `fix_direction` variants point at it; and the `extract_complete_element_cdp` tool already exposes it. Converging *onto* it (not building a 3rd copy) is the deduplication answer. **Caveat that shapes §2.1:** "canonical *home*" ≠ "CDP transport for every aspect" — a per-aspect best-transport design *inside the one module* is legitimate and is what I choose, because events/animations/assets cannot reach parity via CDP (§2.1).

### 2.1 The per-aspect transport table + the EVENTS decision (the consciously-made tradeoff)

Reusing plan_M5a's binding per-aspect CDP-feasibility analysis (verified against `embedded/js/extract_*.js` output surfaces vs CDP domain capabilities):

| Aspect | JS-eval produces | CDP-native can produce | Parity? | **M5b transport decision** |
|---|---|---|---|---|
| **styles** | (already CDP) | `css.getComputedStyleForNode` + `getMatchedStylesForNode` fully model it | ✅ clean | **CDP** — the one existing CDP path; collapses the `ElementCloner.extract_element_styles_cdp` vs `CDPElementCloner._get_computed/matched_styles_cdp` duplication into the engine's methods. |
| **structure** | `tag_name,id,class_name,class_list,text_content,inner_html,outer_html,attributes,data_attributes,dimensions,children,scroll_info` | `_get_element_html`→`tagName,outerHTML,attributes`; `_get_children_cdp`→children; `dom.get_box_model`→dimensions | ⚠️ partial (CDP lacks `text_content`/`inner_html`/split `class_list`/`data_attributes`/`scroll_info`; camelCase vs snake_case) | **CDP** via `_get_element_html`(+`_get_children_cdp`), **plus `dom.get_box_model`** to restore `dimensions`. Deliberate golden diff (subset + camelCase). |
| **events** | `inline_handlers, event_listeners, framework_handlers, detected_frameworks` (React fiber / Vue vnode / Angular / jQuery) | `_get_event_listeners_cdp` (`DOMDebugger.getEventListeners`) → **only** `addEventListener`-registered listeners | ❌ **capability regression** — CDP cannot see inline `on*` handlers or framework/synthetic handlers (React attaches one delegated root listener; per-element handlers are invisible) | **KEEP JS-eval** (see decision below). |
| **animations** | `css_animations, css_transitions, css_transforms, keyframe_rules` (walks `document.styleSheets` for `@keyframes`) | **nothing** — CDP `Animation` domain is event-driven (`animationCreated`), no synchronous per-node keyframe read | ❌ no CDP path | **KEEP JS-eval.** |
| **assets** | `images, background_images, fonts, icons, videos, audio` (+ HTTP fetch) | **nothing** — no CDP media/font enumerator | ❌ no CDP path | **KEEP JS-eval.** |
| **related_files** (6th, gathered by complete) | imported CSS/JS fetched over HTTP | none | ❌ no CDP path | **KEEP JS-eval + HTTP.** Rehomed, not rewritten. |

**THE EVENTS DECISION (made consciously, in full context — not silently regressed):**

> **DECISION: KEEP the JS-eval transport for events (and animations, assets, related_files). Converge onto `CDPElementCloner` as the canonical *home/engine*, but house a per-aspect best-transport design inside it: CDP for styles + structure, JS-eval for events/animations/assets/related_files.**

Rationale (the tradeoff argued both ways, then resolved):

- **The case for forcing all-CDP** (rejected): maximal dedup — one transport, delete every JS file, no `.evaluate` anywhere. It is the "purest" convergence and would let the engine be a single CDP class.
- **The case for keeping JS for events** (chosen): events is a **crown-jewel** capability. `getEventListeners` **cannot** report inline `on*` handlers or framework/synthetic handlers — the exact data a user cloning a live component most needs (React/Vue/Angular/jQuery handlers). Forcing CDP here is a **silent capability regression** on the tool's headline feature. The binding directive is explicit: *"M5b's 5→1 consolidation must consciously decide the events tradeoff … and NOT silently regress it."* Preserving observable capability for the crown-jewel feature outranks transport uniformity. And plan_M5a's severity reconciliation removes the urgency argument for CDP: the JS path is **bounded to ≤30 s** by `_with_cdp_timeout` at every tool entry and **yields the event loop** while waiting (it degrades-then-errors on a pathological page; it does not wedge the backend). So the only thing CDP buys for events is marginal robustness/stealth on pathological pages — paid for with a real capability loss. Not worth it.
- **animations + assets + related_files**: no CDP path exists in the repo at all; forcing them to CDP is net-new extraction code that would *lose* data (`@keyframes`, media enumeration) — pure downside. Keep JS.

**Consequence for the "one way per thing" (conventions) lens:** the convention M5b establishes is **"one canonical engine, one node-id resolver, one complete-schema, one delegator shape per aspect"** — *not* "one transport." Transport is a per-aspect property justified by capability, documented in the engine (a short module docstring table + a `# transport: CDP|JS + why` comment on each aspect method). This is a *single documented rule* ("use CDP where it reaches parity, JS where it doesn't, and never a third hand-rolled copy"), not a second way of doing the same thing. The defect the lens forbids — *two* implementations of the *same* extraction — is exactly what M5b removes (3 complete engines → 1; 2 styles-CDP copies → 1).

**Net capability change vs today:** **zero capability loss.** Every aspect keeps producing what it produces today (events keeps inline/framework detection; animations keeps `@keyframes`; assets keeps media). The *only* observable schema changes are structure (snake→camel + `dimensions` via box-model) and the **complete-element schema convergence** (3 schemas → 1). This is a materially safer consolidation than the all-CDP reading and is the one I recommend to the human.

### 2.2 Where `extract_related_files` and the aspect methods live after consolidation

`extract_related_files` (element_cloner.py:358-416, JS+HTTP, gathered by complete-extraction) has no CDP analogue and is a genuinely distinct operation. It **moves into the canonical engine** as a JS-transport aspect method (so `element_cloner.py` can be deleted). The 5 aspect methods likewise move in: styles/structure become CDP (reusing `CDPElementCloner._get_*`), events/animations/assets become JS methods carried over from `ElementCloner` (their `_load_js_file` + `tab.evaluate` bodies move verbatim). The engine thus contains: 6 aspect methods (mixed transport), `extract_complete_element` (composing them), `_resolve_node_id`, the existing CDP `_get_*` primitives, and the JS `_load_js_file` helper (moved from `ElementCloner`).

### 2.3 The `_to_file` dedup shape (F-141)

`FileBasedElementCloner` keeps its class + `output_dir` contract (tripwires §0.8) but its body collapses to **one** helper:
```
async def _extract_and_save(self, prefix, extraction_coro, summary_fn):
    data = await extraction_coro
    filename = self._generate_filename(prefix)
    file_path = self._save_to_file(data, filename)
    return {"file_path": str(file_path), "extraction_type": prefix, "summary": summary_fn(data)}
```
Each of the 8 methods becomes a ~5-line call site supplying `prefix`, the engine extraction coroutine, and a small `summary_fn` lambda. This also fixes the finding's noted inconsistency (`file_path` str-vs-Path, drifting summary key sets) by forcing one contract. The base-extractor calls re-point from `element_cloner.*` to the canonical `cdp_element_cloner.*` engine, and `__init__` imports the shared engine singleton instead of `ComprehensiveElementCloner()` (F-144).

### 2.4 The singleton-vs-per-call decision (F-144)

**Add `cdp_element_cloner = CDPElementCloner()` at module scope** in `cdp_element_cloner.py` (alongside the 4 existing singletons' convention), import it in server.py, and change the `extract_complete_element_cdp` tool body to use the singleton instead of `cdp_cloner = CDPElementCloner()` per call. `FileBasedElementCloner` imports the shared engine singleton rather than building its own. This makes construction uniform across all surviving cloner objects (the convention every sibling already follows), so the next person adding per-instance state gets singleton semantics everywhere. Tool count unchanged (94).

### 2.5 The F-744 naming resolution (the tension resolved)

**DECISION: rename freely at the INTERNAL level now (module-internal methods, class, engine concept); KEEP the MCP tool NAMES stable in Ph1 (sharpen their docstrings), defer the tool-name renames to M4-Ph2.**

The tension: at the M4-Ph1 gate the human ruled F-760/F-743 **tool** renames = "caveat-now, rename-at-Ph2" (rename once, where tools are already being re-homed; don't churn the M6 count-94 tripwire names mid-pipeline). F-744's routing says "standardize on `extract_*` during cloner consolidation." These reconcile by **splitting the rename into two altitudes**:

- **Internal (rename now — clarity/dedup lens, no external surface):** the canonical class/engine gets a self-describing name; the 3 disagreeing complete-methods collapse into one clearly-named `extract_complete_element`; dead methods are deleted. These do not touch any MCP tool name or the count-94 tripwire, so the rename-once concern does not apply — this *is* the moment the internals are being rewritten.
- **MCP tool names (defer to Ph2 — honor the rename-once ruling):** the 4 tools `clone_element_complete`, `clone_element_to_file`, `extract_complete_element_to_file`, `extract_complete_element_cdp` keep their names in M5b. Renaming them (`clone_*`→`extract_*`) *would* churn the M6 count-94 tripwire's expected names and duplicate the churn M4-Ph2's per-section move already schedules. **Instead, in M5b I sharpen the 4 docstrings** so each states which is authoritative and the file-safe/accuracy differentiator (F-744's core complaint is navigability — "which one is the accurate one?" — which a good docstring answers without a rename). The canonical verb taxonomy (`extract_*` = element capture) is already recorded in `tool_registry.py`'s docstring by M4-Ph1 C2 (F-760); M4-Ph2 executes the tool-name renames from it.

Why this altitude split and not "rename tools now": the M4-Ph1 human ruling on the *sibling* rename question (F-760/F-743) is the governing precedent, and its stated rationale (don't churn the tripwire names mid-pipeline; rename once at the section move) applies identically to F-744's tool names. Renaming internals now is free (no tripwire, no external surface) and is the legitimate clarity fix; renaming the tool surface now would violate the rename-once principle the human already set. **The genuinely-contestable part goes to the gate** (§7 open questions): the human may prefer to pull the 4 tool renames into M5b since the tool *bodies* are already being touched — a defensible reading of "already being touched." I recommend against it (tripwire churn + double-churn with Ph2), but flag it.

### 2.6 Rejected alternatives

1. **Converge onto `element_cloner.py` / `ElementCloner` as canonical** (its `fix_direction` in F-601/F-203 floats "consolidate into element_cloner.py" / "comprehensive_element_cloner"). *Rejected — violates the binding dedup ruling and is worse on merits:* the ruling is explicit ("converge onto CDPElementCloner … not a 3rd copy"); and `ElementCloner` is JS-first (only styles is CDP), so making it canonical would keep the tool on the more-fragile/more-detectable transport for styles and re-duplicate the CDP primitives that already live in `CDPElementCloner`. Converging onto the CDP-native engine is both the ruling and the better design.
2. **Force all 5 aspects to CDP** (the "purest" one-transport convergence; plan_M5a's option B applied wholesale). *Rejected — silent capability regression:* events loses inline/framework handlers, animations loses `@keyframes`, assets loses media enumeration — all crown-jewel data, for no benefit beyond ≤30 s-bounded robustness the timeout wrapper already provides. Contradicts the binding "do not silently regress events."
3. **Keep 5 modules, just make them delegate** (thin-wrapper each of the 4 non-canonical onto the 5th, delete nothing). *Rejected — half-measure:* leaves 5 files, 5 import units, and the F-140 "which of 5 do I edit" problem largely intact (a break in any file still disables its tool); the brief's target is −700–900 LOC, achievable only by deleting the redundant whole-file engines. Delegation-without-deletion is the M5a down-payment, not the M5b consolidation.
4. **Fold file-writing into the engine** (delete `FileBasedElementCloner`, have the engine write files directly). *Rejected — breaks a live contract and muddies cohesion:* two existing tests pin `FileBasedElementCloner(output_dir=…)` + the `output_dir` resolution (issue #5); and mixing "extract" with "write to disk with per-user dir resolution" lowers cohesion. Keep file-writing a thin adapter (its own concern), dedup its *body*.
5. **Do the whole thing in one commit.** *Rejected — crown-jewel blast radius:* a single mega-diff across 6 files makes a golden regression un-bisectable. Sequence it one deletable-engine at a time, suite green each step (§3).
6. **Rename the 4 MCP tools in M5b** (execute F-744 fully now). *Rejected as default* (kept as a gate question, §2.5/§7): churns the M6 count-94 tripwire names and double-churns with M4-Ph2; the rename-once ruling governs.

---

## 3. Sequencing (independently verifiable; one checkpoint commit each; suite green at every checkpoint)

> Discipline: each step self-contained; **run `.venv\Scripts\python.exe -m pytest -m "not integration" -q` after every step**; deviation from a confirmed symbol → **STOP and report**. Pin/behavior-lock BEFORE each change. One PR for {M5b}, stacked last.

**M5b-1 — Build the canonical engine surface in `cdp_element_cloner.py` (additive; nothing deleted yet).**
- Factor `_resolve_node_id(tab, element, selector)` into the engine from `extract_element_styles_cdp` L573-584 (behavior-preserving).
- Add public per-aspect methods on the engine: `extract_element_styles` (CDP, wrapping the existing `_get_computed/matched_styles_cdp`), `extract_element_structure` (CDP via `_get_element_html`+`_get_children_cdp`+`get_box_model`), and **move** the JS-transport methods `extract_element_events`/`_animations`/`_assets`/`extract_related_files` + the `_load_js_file` helper over from `ElementCloner` (bodies verbatim, imports re-anchored to STEP-0 form). Each public method is a thin `try/except → {"error":…}` delegator.
- Add `extract_complete_element(tab, selector, ...)` composing the 6 aspect methods into the ONE canonical schema; add the module-level `cdp_element_cloner = CDPElementCloner()` singleton (F-144).
- *Pin first:* add engine-level tests to `test_cloner_schemas.py` driving the new methods against `FakeTab`; capture the new complete-schema golden as `tests/goldens/canonical_engine.json`.
- *Verify:* full hermetic suite green (no tool re-pointed yet → existing goldens untouched).

**M5b-2 — Re-point the 5 aspect tools + `extract_complete_element_cdp` at the engine singleton; update structure golden.**
- server.py: `extract_element_<aspect>` tools call `cdp_element_cloner.extract_element_<aspect>`; `extract_complete_element_cdp` uses the singleton (drop per-call `CDPElementCloner()`), preserving `_require_tab`/`_with_cdp_timeout`/`ToolError`.
- **Deliberately update** the `structure` SOFT golden (snake→camel + `dimensions` via box-model); events/animations/assets/styles goldens stay **green** (transport unchanged for those under §2.1 — events/animations/assets stay JS; styles was already CDP). PR documents the structure diff.
- *Verify:* `pytest tests/test_cloner_schemas.py -q` (structure golden updated, rest green) then full suite.

**M5b-3 — Re-point `clone_element_complete` + progressive at the engine; converge the complete schema; delete F-143 fallback; DELETE `comprehensive_element_cloner.py`.**
- `clone_element_complete` tool → `cdp_element_cloner.extract_complete_element`. `progressive.clone_element_progressive` → same; **delete** the dead dual-schema `.get("element",{})...` fallbacks (F-143) and read the canonical shape; keep `expand_*`/store.
- Delete `comprehensive_element_cloner.py` (whole file) and its import.
- **Deliberately update** the complete-element goldens (the 3→1 schema convergence — this is the F-140 change M6 anticipated); update the progressive golden for the new flat canonical shape. HARD invariants (error shape, top-level key presence) stay asserted.
- *Verify:* `pytest tests/test_cloner_schemas.py -q` (complete + progressive goldens updated) then full suite.

**M5b-4 — Dedup `file_based_element_cloner.py` (F-141) + re-point at engine + shared singleton; keep `output_dir` contract.**
- Introduce `_extract_and_save(prefix, extraction_coro, summary_fn)`; rewrite the 8 `_to_file` methods as ~5-line call sites; re-point base-extractor calls to `cdp_element_cloner.*`; `__init__` imports the shared engine singleton (F-144).
- *Verify:* the two `output_dir` tripwire tests (`test_element_cloner_output_dir.py`, `test_clone_output_dir.py`) stay **green** (contract preserved); the to-file `summary` golden in `test_cloner_schemas.py` updated only if the summary key set is deliberately normalized (documented); full suite.

**M5b-5 — DELETE `element_cloner.py`; final singleton/import cleanup; F-744 docstring sharpening.**
- Confirm nothing imports `element_cloner`/`ElementCloner` (all aspect + related_files methods now live in the engine; `extract_element_styles_cdp` tool re-points to `cdp_element_cloner.extract_element_styles`). Delete the file + its imports.
- Sharpen the 4 clone/extract tool docstrings (F-744; names unchanged). Confirm F-108 count still **94**.
- *Verify:* full hermetic suite green; coverage delta recorded (do NOT ratchet the 39 gate here).

**Which module dies at which step:** `comprehensive_element_cloner.py` → M5b-3; `element_cloner.py` → M5b-5. `file_based_element_cloner.py` + `progressive_element_cloner.py` survive as thin adapters. `cdp_element_cloner.py` becomes the engine.

**Partial-landing validity:** M5b-1 alone (additive engine, no deletions) is a valid green landing. M5b-1..3 (both redundant complete-engines gone, structure+complete converged) is a valid landing that closes the F-140 core. Each later step is independently revertible.

---

## 4. Breaking changes (0 users → free; these are the point, gated by M6 goldens)

- **Complete-element schema convergence (F-140):** the 3 disagreeing schemas (flat / flat-camelCase+`selector,url,timestamp` / nested-under-`element`) become **ONE** canonical schema. The `clone_element_complete`, `clone_element_to_file`, `extract_complete_element_to_file`, `extract_complete_element_cdp`, and `clone_element_progressive` tools all now return that one shape. Named golden diffs.
- **`extract_element_structure` output:** snake_case → camelCase, subset of keys, `dimensions` restored via `dom.get_box_model`. Named golden diff.
- **events / animations / assets / styles / related_files outputs: UNCHANGED** (transport unchanged per §2.1). **No capability loss** — the deliberate, in-context outcome of the events decision.
- **`file_based` `summary` dicts:** normalized to one contract (may change a few keys that were inconsistent across the 8 copies — F-141). Named golden diff if so.
- **`extract_complete_element_cdp` construction:** singleton instead of per-call (F-144) — no observable output change.
- **MCP tool names, signatures, FastMCP schemas, tool count (94): UNCHANGED.** Only return-payload shapes move. Docstrings sharpened (F-744).
- **Deleted modules:** `element_cloner.py`, `comprehensive_element_cloner.py` no longer importable (0 external importers; internal importers re-pointed).

---

## 5. Test strategy

### 5.1 Behavior pins BEFORE each change
Every step pins first: M5b-1 adds engine tests + the new-schema golden before any tool re-points; M5b-2..4 update the specific SOFT golden in the same commit as the change, as a **reviewed diff**. All pins reuse the **one** `tests/fakes.py::FakeTab` home (records `.evaluate` + `.send`) — no per-test mocks (dedup/conventions lens). A path-assertion test (FakeTab configured so `.evaluate` raises for CDP-only aspects) proves styles/structure took the CDP path and events/animations/assets took the JS path — pinning the §2.1 transport decision deterministically (no timing).

### 5.2 EXACT SOFT goldens updated (+ justification) vs HARD invariants held

**SOFT goldens UPDATED (each a deliberate, PR-justified diff):**
| Golden | Why updated |
|---|---|
| `structure` (M5a-marked "will change") | CDP transport: snake→camel, subset, `dimensions` via box-model (M5b-2). |
| the 3 complete-element engine goldens → **one** `canonical_engine` golden | F-140 3→1 schema convergence (M5b-1 captures new; M5b-3 retires the 3 old). This is the change M6 built the two-tier net for. |
| `progressive` (`clone_element_progressive` output) | reads the canonical complete shape; dead dual-schema branches removed (M5b-3). |
| `file_based` to-file `summary` shape | only if the 8 inconsistent summary key-sets are normalized to one contract (M5b-4); documented. |

**SOFT goldens HELD GREEN (the tripwire — an unmarked red here = real regression, investigate):**
- `styles`, `events`, `animations`, `assets` — transport unchanged (§2.1), output unchanged. (events green is the *proof* the capability was not regressed — its golden keeps `inline_handlers`/`framework_handlers`/`detected_frameworks`.)

**HARD invariants HELD (violating one = STOP signal, per M6 §2.2):**
- Error shape is always `{"error": …}` on failure for every method.
- Every complete-element result exposes the canonical top-level key set (asserted structurally, not by full snapshot).
- The `file_based` to-file result always has `{file_path, extraction_type, summary}`.
- Tool dispatch invariants (`test_tool_dispatch.py`): F-108 count = **94**, `.fn` unwrap seam intact.

**Non-M6 behavioral tripwires HELD GREEN:** `test_element_cloner_output_dir.py` + `test_clone_output_dir.py` — the `FileBasedElementCloner`/`output_dir`/issue-#5 contract (§0.8). Kept green by preserving the class + constructor + resolution; **not** updated (they are behavioral, not quirk-characterization).

### 5.3 Expected test-count delta + coverage
- **Adds** engine-level tests to `test_cloner_schemas.py` (per-aspect + complete against `FakeTab`) + the transport-path assertion: expect **+several** tests, net positive. **Removes** none of M6's structure (goldens are updated in place, not deleted; three complete-engine goldens collapse to one file — a net −2 golden files but the assertions consolidate).
- Deleting `element_cloner.py` + `comprehensive_element_cloner.py` removes their lines from the coverage denominator; the surviving engine gains body coverage from the new tests. **Net coverage: expected flat-to-up.** Record the delta; **do NOT ratchet `fail_under=39`** (out of scope — a separate hygiene step, consistent with M6 §1.4 and M4-Ph1's stance). If deletion of two large files *raises* the percentage, that is a side effect, not a gate move.

---

## 6. Rollback + checkpoints

- **Branch:** the pipeline fix branch (`audit/fixes-2026-07-02`), {M5b} stacked **last**. Stage-3 discipline: pin/golden first, full hermetic suite green at every checkpoint; deviation from a confirmed symbol → STOP.
- **One commit per step** (M5b-1..5). Rollback is per-step `git revert`:
  - Revert M5b-5 → `element_cloner.py` restored, tools re-point back. Revert M5b-4 → file_based body restored. Revert M5b-3 → `comprehensive_element_cloner.py` restored, complete tools re-point back. Revert M5b-2 → aspect tools re-point back. Revert M5b-1 → engine additions removed.
- **Partial-landing validity** (§3): M5b-1 (additive), or M5b-1..3 (F-140 core closed), are both valid green stopping points. No production state to unwind.
- **The golden diffs are the review surface** — a reviewer approves the schema convergence by approving the deliberate golden updates; an *unmarked* golden going red is the stop signal.

---

## 7. Risk (blast radius · worst case · early warning)

- **This is the crown-jewel feature — a botched consolidation breaks element cloning.** *Blast radius:* the entire cloner tool family (28 tool sites) routes through one engine. *Mitigate:* one deletable-engine per commit; SOFT goldens per aspect + the transport-path assertion; HARD invariants as tripwires; suite green each step. *Worst case:* a golden catches a schema regression pre-merge. *Early warning:* an **unmarked** golden (styles/events/animations/assets, or a HARD invariant) goes red → real regression, investigate, do **not** blind-update.
- **Events capability regressed by accident despite the decision.** *Blast radius:* `extract_element_events` silently drops inline/framework handlers. *Mitigate:* events stays on JS transport (unchanged), so its golden must stay **green** — a red events golden is an immediate stop; the transport-path assertion forbids `.send`-only for events. *Early warning:* the events golden loses the three framework keys.
- **Deleting `element_cloner.py`/`comprehensive_element_cloner.py` orphans an importer.** *Blast radius:* import error at collection. *Mitigate:* grep every importer before each delete (server.py + the two survivor adapters + tests); the two `output_dir` tests import `file_based`, which survives. *Early warning:* `pytest` collection error.
- **The `output_dir`/issue-#5 contract broken by the file_based dedup.** *Blast radius:* clones write to a read-only path under an MCP client (the original bug). *Mitigate:* keep `FileBasedElementCloner(output_dir=…)` + resolution untouched; the two tripwire tests must stay green. *Early warning:* those tests red.
- **Node-id impedance** (`CDPElementCloner._get_*` take `node_id`; some callers pass `element`). *Mitigate:* the one `_resolve_node_id` helper (M5b-1) handles `node_id`/`backend_node_id`/`selector`; a fake-element unit case covers the no-node-id branch.
- **Golden churn confuses M4-Ph2 / M14.** *Mitigate:* tool names + count-94 held stable (§2.5); only payload schemas + docstrings move; the taxonomy for Ph2 renames already recorded by M4-Ph1 C2.

**Open questions for the human gate (genuine decisions only):**
1. **Events transport — confirm the KEEP-JS decision (§2.1).** I recommend keeping JS-eval for events (zero capability loss on the crown-jewel feature; the ≤30 s timeout wrapper already bounds the fragility). The alternative is all-CDP with a conscious capability loss. **This is the one tradeoff the directive says the human must consciously accept.** Do you accept KEEP-JS, or do you want all-CDP?
2. **F-744 tool renames (§2.5):** rename the 4 `clone_*`/`extract_*` MCP tools **now** in M5b (bodies already touched), or **defer to M4-Ph2** (rename-once, avoid tripwire churn)? I recommend defer + sharpen docstrings now.
3. **`FileBasedElementCloner` class rename:** the class could be renamed for clarity, but the two existing tests import it by name. Keep the name (default), or rename + update the two tests deliberately?

---

## 8. Findings closed / partial / out (one line each)

- **F-140 (High)** — **CLOSED.** Three disagreeing complete-element engines converge into one `extract_complete_element` on the canonical engine with one schema (M5b-1/3).
- **F-203 (High)** — **CLOSED.** Five overlapping modules → one engine + two thin adapters; `file_based` stops wrapping both `ComprehensiveElementCloner` and the `element_cloner` singleton (M5b-3/4/5).
- **F-601 (High)** — **CLOSED.** Same root cause as F-203; the independently-reimplemented extractors (`comprehensive`, `element_cloner` CDP-styles copy) are deleted/absorbed into the one engine.
- **F-142 (High, folded from M5a)** — **CLOSED (transport convergence decided holistically).** styles+structure on CDP; events/animations/assets/related_files consciously KEPT on JS (bounded ≤30 s; zero capability loss). The "converge onto CDPElementCloner" ruling is honored as the canonical home with a documented per-aspect transport.
- **F-141 (Medium)** — **CLOSED.** 8 `_to_file` boilerplate methods collapse to one `_extract_and_save` helper + ~5-line call sites; summary contract unified (M5b-4).
- **F-143 (Medium)** — **CLOSED.** Dead dual-schema `.get("element",{})...` fallbacks in `progressive_element_cloner` deleted; single canonical shape read (M5b-3).
- **F-144 (Low)** — **CLOSED.** `cdp_element_cloner` singleton added at module scope; `extract_complete_element_cdp` tool + `FileBasedElementCloner` use the shared singleton, not per-call/private construction (M5b-2/4).
- **F-744 (Medium, routed)** — **PARTIAL (internal renames + docstrings now; MCP tool-name renames deferred to M4-Ph2 by the rename-once ruling).** The 4 tool docstrings are sharpened so each states which is authoritative and the accuracy/file-safe differentiator; the `clone_*`→`extract_*` tool-name standardization rides M4-Ph2 (§2.5). Flagged as gate question 2.

---

### Header recap for the gate
Pinned SHA `2267b83d3efda03f93936db2c34ded33aaa0d701` · 2026-07-03 · batch **{M5b}** · base = pinned SHA + M3(+A1)+M1+M8(+A1)+M2+M7+M11a+M15+M9+M6+M12a+**M4-Ph1(+A1)** · **status DRAFT — awaiting human approval.** Canonical engine = `CDPElementCloner` (the home, per-aspect transport inside it); **events KEPT on JS-eval — zero capability loss, consciously decided** (gate Q1). `element_cloner.py` + `comprehensive_element_cloner.py` DELETED; `file_based`/`progressive` survive as thin adapters. Estimated LOC delta: **−700 to −850** (delete ~649 + ~344 whole files; add a modest complete-extraction + 6 aspect methods + node-id helper to the engine; collapse 8× `_to_file` boilerplate; the two survivors + engine net-shrink). Lenses: one engine, one node-id resolver, one complete-schema, one delegator shape per aspect, one construction convention — the transport is a single documented per-aspect rule, not a second way of doing the same thing.
