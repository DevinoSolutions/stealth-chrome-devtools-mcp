# Deep Dive DD-3: Element Cloner 5-Variant Duplication

## 1. Inventory

| # | Class | File | LOC | Extraction mechanism | Instantiation | MCP tools it backs |
|---|---|---|---|---|---|---|
| 1 | `ElementCloner` | `element_cloner.py` | 648 | JS-file injection (`_load_js_file()` + `tab.evaluate()`) for structure/events/animations/assets. `extract_element_styles` is a pure delegate to CDP (see below) — it loads no JS file at all, unlike its 4 siblings. | module singleton (`element_cloner`) | `extract_element_styles`, `extract_element_structure`, `extract_element_events`, `extract_element_animations`, `extract_element_assets`, `extract_element_styles_cdp`, `clone_element_complete`* |
| 2 | `ComprehensiveElementCloner` | `comprehensive_element_cloner.py` | 343 | ONE monolithic inline JS string (~216 lines, `js_code = f"""..."""`) covering styles/events/CSS-rules/pseudo-elements/animations/fonts/children in a single `tab.evaluate()` call. | module singleton (`comprehensive_element_cloner`) **+** a second, private instance created inside `FileBasedElementCloner.__init__` | `clone_element_complete` |
| 3 | `FileBasedElementCloner` | `file_based_element_cloner.py` | 647 | No extraction logic of its own — delegates to #1 and #2, then writes the result to a JSON file and hand-builds a small "summary" dict. 8 near-identical wrapper methods. | module singleton (`file_based_element_cloner`) | `extract_element_styles_to_file`, `extract_element_structure_to_file`, `extract_element_events_to_file`, `extract_element_animations_to_file`, `extract_element_assets_to_file`, `clone_element_to_file`, `extract_complete_element_to_file` |
| 4 | `ProgressiveElementCloner` | `progressive_element_cloner.py` | 265 | No extraction logic of its own — calls #2 once, stores the result in `persistent_storage` keyed by a generated `element_id`, then exposes `expand_*` accessors for incremental reads. Genuinely different concern (pagination over an already-fetched payload). | module singleton (`progressive_element_cloner`) | `clone_element_progressive` (+ `expand_styles`, `expand_events`, `expand_children`, `expand_css_rules`, `expand_pseudo_elements`, `expand_animations`, `list_stored_elements`, `clear_stored_element`, `clear_all_elements`) |
| 5 | `CDPElementCloner` | `cdp_element_cloner.py` | 320 | A **third, fully independent** re-implementation of "extract everything about this element" using raw CDP domain calls (`DOM.*`, `CSS.getComputedStyleForNode`, `CSS.getMatchedStylesForNode`, `DOMDebugger.getEventListeners`) — no JS injection anywhere. Its own module docstring claims this supersedes the other two: *"This provides 100% accurate element cloning by using CDP's native capabilities instead of limited JavaScript-based extraction."* | **created fresh on every MCP call**, inside the tool function body — the only one of the 5 that isn't a module singleton | `extract_complete_element_cdp` |

\* `clone_element_complete` (the ElementCloner method, orchestrating 6 JS-file evaluations via `asyncio.gather`) is itself a second "give me everything" engine, separate from `ComprehensiveElementCloner.extract_complete_element`. `FileBasedElementCloner` wraps *both* of them under confusingly similar names (`clone_element_complete_to_file` → uses #1's engine; `extract_complete_element_to_file` → uses #2's engine).

Total: **2,223 LOC across 5 files**, exposing **16 distinct MCP tools**, to answer one conceptual question: *"what does this DOM element look like?"*

### What genuinely differs (defensible)
- **Delivery mode**: inline JSON response vs. write-to-file-and-return-a-summary. Large payloads legitimately shouldn't blow up an MCP response — `FileBasedElementCloner` earns its place on that axis alone.
- **Pagination**: `ProgressiveElementCloner`'s store-once/expand-later pattern avoids re-extracting when an agent wants a different slice of an already-cloned element. This is a real, orthogonal capability.

### What is NOT genuinely different (duplicated)
- **The extraction engine itself.** Three independent implementations of "get complete element data" exist simultaneously and are all live, wired to separate tools, with three different output schemas:
  1. `ElementCloner.clone_element_complete` — 6 parallel JS-file evaluations, returns `{styles, structure, events, animations, assets, related_files}`.
  2. `ComprehensiveElementCloner.extract_complete_element` — 1 monolithic inline JS blob, returns `{html, styles, eventListeners, cssRules, pseudoElements, animations, fonts, children}` (flat, camelCase).
  3. `CDPElementCloner.extract_complete_element_cdp` — raw CDP calls, returns `{element: {html, computed_styles, matched_styles, event_listeners, children}, extraction_stats}` (nested, snake_case).

## 2. Duplication Evidence

**A. The "wrapper boilerplate" pattern is copy-pasted 8 times in `FileBasedElementCloner`.** Every `*_to_file` method does the same 4 things: call the underlying extractor, generate a filename, save it, hand-roll a summary dict. From `extract_element_styles_to_file` (`file_based_element_cloner.py:101-115`):

```python
            # Extract styles using element_cloner
            style_data = await element_cloner.extract_element_styles(
                tab,
                selector=selector,
                include_computed=include_computed,
                include_css_rules=include_css_rules,
                include_pseudo=include_pseudo,
                include_inheritance=include_inheritance
            )

            # Generate filename and save
            filename = self._generate_filename("styles")
            file_path = self._save_to_file(style_data, filename)

            # Create summary
```

The identical `filename = self._generate_filename(...)` / `file_path = self._save_to_file(...)` pair recurs verbatim (only the prefix string changes) in `extract_element_structure_to_file` (line 247-248), `extract_element_events_to_file` (308-309), `extract_element_animations_to_file` (368-369), `extract_element_assets_to_file` (428-429), `extract_related_files_to_file` (490-491), `extract_complete_element_to_file` (180-181), and `clone_element_complete_to_file` (541-542) — 8 occurrences of the same 2-line idiom, each hand-inlined instead of factored into the one helper that already exists (`_save_to_file`) plus a thin `_extract_and_save(prefix, coro, summary_fn)` that was never written.

**B. Two different "complete extraction" engines are called from within the same file, under confusingly similar names.** `file_based_element_cloner.py:171-173`:

```python
            complete_data = await self.comprehensive_cloner.extract_complete_element(
                tab, selector, include_children
            )
```

vs. `file_based_element_cloner.py:530-532`:

```python
            complete_data = await element_cloner.clone_element_complete(
                tab, element, selector, extraction_options
            )
```

The first belongs to `extract_complete_element_to_file`, the second to `clone_element_complete_to_file`. Different call signature, different underlying engine, different result shape — two methods whose names differ only in word order.

## 3. Divergence Risk (already drifted)

1. **A hang-avoidance fix landed in 1 of 5 sibling extraction primitives.** `ElementCloner.extract_element_styles_cdp` docstring (`element_cloner.py:552-565`):

   ```
           Extract complete styling information using direct CDP calls (no JavaScript evaluation).
           This prevents hanging issues by using nodriver's native CDP methods.
   ```

   `extract_element_styles` now does nothing but delegate to this CDP method (`element_cloner.py:54-62`). But `extract_element_structure`, `extract_element_events`, `extract_element_animations`, and `extract_element_assets` — the other 4 methods in the *same class*, doing conceptually the same "pull data out of the page" job — still call `self._load_js_file(...)` + `await tab.evaluate(js_code)` (e.g. `element_cloner.py:152-153`), the exact code shape the CDP rewrite exists to avoid. If the underlying hang bug is real, 4 of 5 extraction primitives in `ElementCloner` remain exposed to it.

2. **Dead defensive code for a schema swap that never happened.** `ProgressiveElementCloner.clone_element_progressive` only ever calls `comprehensive_element_cloner.extract_complete_element` (`progressive_element_cloner.py:40-42`), whose output is flat (`styles`, `eventListeners`, `cssRules`). Yet the very next lines defend against the *nested, CDP-shaped* schema that only `CDPElementCloner` produces (`progressive_element_cloner.py:56-70`):

   ```python
               base = {
                   "tagName": full_data.get("element", {}).get("html", {}).get("tagName")
                   or full_data.get("tagName", "unknown"),
                   "attributes_count": len(full_data.get("element", {}).get("html", {}).get("attributes", [])),
                   "children_count": len(full_data.get("children", [])),
                   "summary": {
                       "styles_count": len(full_data.get("element", {}).get("computed_styles", {}))
                       or len(full_data.get("styles", {})),
                       "event_listeners_count": len(full_data.get("element", {}).get("event_listeners", []))
                       or len(full_data.get("eventListeners", [])),
                       "css_rules_count": len(full_data.get("element", {}).get("matched_styles", {}).get("matchedCSSRules", []))
                       if isinstance(full_data.get("element", {}).get("matched_styles"), dict)
                       else len(full_data.get("cssRules", [])),
                   },
               }
   ```

   The same `.get("element", {}).get(X) or .get(Y)` fallback repeats in `expand_styles`, `expand_events`, and `list_stored_elements`. This is a fossil of an attempted or planned engine swap (comprehensive → CDP) for progressive cloning that was never wired up — and it silently masks shape mismatches (returns `0`/`{}` instead of surfacing an error) rather than failing loud.

3. **Two different "complete" extraction paths hide behind near-identical names** (`extract_complete_element_to_file` vs. `clone_element_complete_to_file`, section 2B above) — a maintainer fixing "complete element extraction" has even odds of finding and patching only one of the two live implementations, as already happened with the hang fix.

4. **Inconsistent singleton lifecycle.** `element_cloner`, `comprehensive_element_cloner`, `file_based_element_cloner`, and `progressive_element_cloner` are all constructed once at import time. `CDPElementCloner` is constructed fresh on every single tool invocation, inline in the tool function body (`server.py:3334-3338`):

   ```python
       tab = await browser_manager.get_tab(instance_id)
       if not tab:
           raise Exception(f"Instance not found: {instance_id}")
       cdp_cloner = CDPElementCloner()
       return await _with_cdp_timeout(cdp_cloner.extract_complete_element_cdp(tab, selector, include_children), instance_id=instance_id)
   ```

   Harmless today only because `CDPElementCloner.__init__` is a no-op. Separately, `FileBasedElementCloner.__init__` builds its own private `ComprehensiveElementCloner()` instance (`file_based_element_cloner.py:48`) instead of importing the shared `comprehensive_element_cloner` singleton that `comprehensive_element_cloner.py` already constructs at module scope — a second silently-redundant instance of the same stateless class.

## 4. Consolidation Path

Target shape: one extraction engine, CDP-native throughout (the mechanism `extract_element_styles_cdp` and all of `CDPElementCloner` already prove out, and the one explicitly documented as fixing a hang bug), parameterized by:

- `target: "inline" | "file"` — replaces `FileBasedElementCloner`'s 8 hand-rolled wrappers with 1 generic `_extract_and_deliver(coro, prefix, summary_fields)` helper. Each of the 8 current methods collapses to a ~5-line declaration (prefix + which fields go in the summary).
- A single canonical output schema for "complete element" data (pick the CDP-nested or the flat-camelCase shape, not both) — deletes `ComprehensiveElementCloner`'s 216-line inline-JS engine and `ElementCloner.clone_element_complete`'s 6-way JS-file orchestration in favor of the CDP-native primitives `CDPElementCloner` already has (`_get_computed_styles_cdp`, `_get_matched_styles_cdp`, `_get_event_listeners_cdp`, `_get_children_cdp`).
- `ProgressiveElementCloner` keeps its distinct store/expand role but loses the dual-schema fallback code once there is only one schema to defend against.

**Estimated LOC removable**: roughly 700-900 of the current 2,223 (~35-40%) — mainly `ComprehensiveElementCloner`'s inline-JS block (~220 lines), the 4 JS-file-based methods in `ElementCloner` plus their supporting `_load_js_file`/`_convert_nodriver_result` machinery (~250-300 lines) once replaced by CDP-native calls, and `FileBasedElementCloner`'s 8 duplicated wrappers collapsing into 1 helper (~250-300 lines saved).

**What would break**: tool *names* need not change (16 tools can remain thin call-throughs), but the *response schema* for `clone_element_complete`, `extract_complete_element_cdp`, and both file-based "complete" variants would necessarily converge onto one shape. Any consumer currently branching on `result["eventListeners"]` (flat/camelCase) vs. `result["element"]["event_listeners"]` (nested/snake_case) breaks. Given the audit's "breaking changes allowed" scope, unifying the schema is itself the highest-value part of this fix, not a side effect to avoid.

## 5. Test Protection

**None of the 5 classes' extraction logic is under test.** The only test files that reference these modules by name are `test_element_cloner_output_dir.py` and `test_clone_output_dir.py`, and both test `FileBasedElementCloner`'s output-*directory* resolution (the per-user-dir fix from commit `9778218`), not extraction correctness. `tests/conftest.py` only redirects that same output directory for the test session.

The remaining `test_clone_*` files (`test_clone_storage_cap.py`, `test_clone_sweep_race.py`, `test_clone_trash_recovery.py`, `test_clone_legacy_marker_classification.py`, `test_profile_clone_excludes_cache.py`) import disk-hygiene helpers from `server` directly — evidence from `test_clone_storage_cap.py:19-23`:

```python
import json
import os
import server
from server import _clone_is_auto, _enforce_clone_storage_cap_in
```

These test eviction/trash/storage-cap/profile-copy mechanics layered on top of the clone output directory — a different subsystem, unrelated to whether `extract_element_styles`, `extract_complete_element`, `clone_element_progressive`, or `extract_complete_element_cdp` return correct data for a real element.

**Consolidation today would be flying completely blind** on the one thing that matters: whether the unified extractor returns equivalent data to what the 3 existing engines return. Any consolidation PR needs extraction-correctness tests (even against a lightly mocked `tab`/CDP surface) as a prerequisite, not an afterthought.
