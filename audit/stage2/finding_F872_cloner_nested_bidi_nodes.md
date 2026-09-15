# F-872 — four cloner aspects return nested BiDi transport nodes instead of values

**Status:** FIXED in this PR (product defect; live on 2.1.5 and on `main` at `267bac8`)
**Opened by:** mechanism review of `tab.evaluate` deep serialization, MEASURED against real headless Chrome 2026-09-15
**Source at:** `origin/main` = `267bac8`
**Severity:** MEDIUM-HIGH. Four of the 94 tools (`extract_element_structure`, `extract_element_events`, `extract_element_assets`, `extract_related_files`) and the seven tools composed from them return payloads whose nested lists hold `{'type': …, 'value': …}` transport records rather than the values a caller asked for. Nothing raises: the failure mode is a **correct-looking answer with wrong contents**, which is why every existing test and every E2E assertion was green over it.

---

## 1. What was observed

Measured with a standalone probe: its own headless Chrome on a temp `--user-data-dir`, a stdlib `http.server` on a loopback port, a page with children + an inline `onclick` + `addEventListener` listeners + an `<img>` + a linked stylesheet. The probe called `CDPElementCloner.extract_element_*` on the real tab — the same code path the tools take.

### `extract_element_structure` — RAW `tab.evaluate` return (`type=list`)

```
[['tag_name', {'type': 'string', 'value': 'div'}],
 ['class_list',
  {'type': 'array', 'value': [{'type': 'string', 'value': 'box'},
                              {'type': 'string', 'value': 'outer'}]}],
 ['children',
  {'type': 'array',
   'value': [{'type': 'object',
              'value': [['tag_name', {'type': 'string', 'value': 'span'}],
                        ['id', {'type': 'null'}],
                        ['class_name', {'type': 'string', 'value': 'child a'}],
                        ['text_content', {'type': 'string', 'value': 'one'}]]},
             …]}],
 …]
```

### …CONVERTED (what the tool actually handed back, pre-fix)

```
{'class_list': [{'type': 'string', 'value': 'box'}, {'type': 'string', 'value': 'outer'}],
 'children': [{'type': 'object',
               'value': [['tag_name', {'type': 'string', 'value': 'span'}],
                         ['id', {'type': 'null'}],
                         ['class_name', {'type': 'string', 'value': 'child a'}],
                         ['text_content', {'type': 'string', 'value': 'one'}]]},
              …],
 'tag_name': 'div',                       # top level: correct
 'dimensions': {'width': 732, 'height': 70, …},   # nested OBJECT: correct
 …}
```

Top-level scalars and top-level nested **objects** came back right; every **array** came back as a list of transport nodes.

### The other three, same run

| aspect | fields returned as BiDi nodes |
|---|---|
| `extract_element_structure` | `class_list[0..1]`, `children[0..2]` (5 nodes) |
| `extract_element_events` | `inline_handlers[0]`, `event_listeners[0]` (2 nodes); **plus** `framework_handlers` decayed from `{}` to `[]` |
| `extract_element_assets` | `images[0]`, `icons[0]` (2 nodes) |
| `extract_related_files` | `stylesheets[0..1]`, `scripts[0..1]` (4 nodes) |

Verbatim, from the same run:

```
extract_element_events   -> {'event_listeners': [{'type': 'object',
                              'value': [['event', {'type': 'string', 'value': 'click'}],
                                        ['type', {'type': 'string', 'value': 'attribute'}],
                                        ['detected', {'type': 'boolean', 'value': True}]]}],
                             'framework_handlers': [],   # a JS {} — now a LIST
                             …}
extract_related_files    -> {'stylesheets': [{'type': 'object',
                              'value': [['href', {'type': 'string', 'value': 'http://127.0.0.1:…/site.css'}],
                                        ['media', {'type': 'string', 'value': ''}], …]}, …]}
```

**Why no test caught it.** `tests/test_cloner_schemas.py` fed `FakeTab(evaluate_result=dict(...))` — a plain dict, which a real `Tab.evaluate` cannot produce for an object. The fixture short-circuited the conversion entirely (`isinstance(structure_data, dict) → return structure_data`). The E2E tier (`tests/test_e2e_data_tools.py:193`, `tests/test_e2e_hard_dom.py:88`) asserts substring presence over `json.dumps(structure)` — and the values ARE present, buried inside the nodes, so those pass over corrupted output too.

## 2. Mechanism

1. `nodriver` 0.47's `Tab.evaluate` (`.venv/Lib/site-packages/nodriver/core/tab.py:812-848`) unconditionally sends `SerializationOptions(serialization="deep", max_depth=10, …)` on **every** call, and returns `remote_object.deep_serialized_value.value` verbatim. `cdp/runtime.py:90-97` (`DeepSerializedValue.from_json`) keeps `json["value"]` exactly as the wire delivered it. `return_by_value` cannot undo this — the deep value is what Chrome sends.
2. Chrome's BiDi `RemoteValue` encoding is recursive: an object becomes `[[key, RemoteValue], …]`, an array becomes `[RemoteValue, …]`, at **every** depth (to `max_depth`).
3. The four aspects' JS files (`embedded/js/extract_structure.js`, `extract_events.js`, `extract_assets.js`, `extract_related_files.js`) each ended in `return result;` — a bare object — so each arrived fully deep-serialized.
4. The tolerance, `embedded/cdp_element_cloner.py:488` `_convert_nodriver_result`, walked **one** level: it unwrapped the top-level `[[key, node], …]` pairs, and its `"array"` branch (`:503`) returned `value_obj.get("value", [])` **verbatim** — i.e. the raw list of nodes. Its `"object"` branch recursed, but `_convert_nodriver_result([])` fails its own `len(data) > 0` guard and returns `[]`, so an empty JS object came back as an empty **list**.
5. The four callers (`:642`, `:680`, `:781`, `:848`) fed that result straight into their return value.

Net: the aspects never failed, so no `except`, no Sentry event, no red test — the payload was simply wrong below the first level.

**One aspect was already immune**: `extract_element_animations` reached this exact conclusion in F-846 and moved to `JSON.stringify` in the page + `json.loads` in Python. `browser_manager.get_page_state`'s viewport (F-844) and `window_sizing` use the same idiom, with the same comment. The four aspects here were simply never migrated.

**These four were NOT the last instances.** F-869 found the sibling on `main` — `browser_manager.py:1439-1447`'s `localStorage`/`sessionStorage` read, where `Object.keys(localStorage)` came back as `[{'type':'string','value':'alpha'}, …]` — and fixes it on its own branch (PR #103). This finding claims the four *cloner aspects*, not the tree.

### 2b. A second defect on the same line (found while fixing the first)

`_convert_nodriver_result`'s callers all began with `if hasattr(raw, "exception_details"): raise ToolError(f"JavaScript error: {raw.exception_details}")`. That branch **can never fire against nodriver 0.47**. `Tab.evaluate` returns the `ExceptionDetails` record ITSELF in the value's place (`if errors: return errors`), and `ExceptionDetails` has no `exception_details` attribute — its fields are `exception_id`, `text`, `line_number`, `column_number`, `script_id`, `url`, `stack_trace`, `exception`, `execution_context_id`, `exception_meta_data`. So every real JS error in an aspect script surfaced as `Unexpected return type: <class 'ExceptionDetails'> (raw: ExceptionDetails(…))`.

Measured against real headless Chrome, and the measurement moved the fix: **`.text` is the literal string `"Uncaught"` for every throw there is** — ReferenceError, TypeError, an explicit `throw new Error(…)` and a SyntaxError all produced it identically. A message built from `.text` would name nothing. The diagnostic is `.exception.description` (`'ReferenceError: nosuchthing is not defined\n    at <anonymous>:1:14…'`).

The only witness was `tests/test_cloner_error_convention.py`'s `JS_THREW = SimpleNamespace(exception_details="ReferenceError: x is not defined")` — a hand-rolled double that asserted the product's own mistaken belief about a library type. It is replaced by a `js_threw()` helper built from nodriver's own `ExceptionDetails`/`RemoteObject` constructors, field-for-field what Chrome produced.

## 3. Fix (this PR)

Not a second unwrapper — a **complete recursive BiDi decoder would be a parallel way to do what the `JSON.stringify` idiom already does** (convention 4: "a change that introduces a second way to do something already done is a defect"), and it would have to track every `RemoteValue` type the spec allows (`date`, `map`, `set`, `regexp`, `node`, `weakLocalObjectReference` back-references) forever. Instead the four aspects join the one idiom:

- **`embedded/js/extract_structure.js`, `extract_events.js`, `extract_assets.js`, `extract_related_files.js`** — every `return` (the success return **and** the `{error: 'Element not found'}` guard) is now `return JSON.stringify(…)`. A string is the one shape deep serialization leaves alone.
- **NEW leaf `embedded/js_aspect_answer.py`** — THE one home for reading a cloner JS aspect's `tab.evaluate` answer: `parsed` (string-or-raise → `json.loads` → dict-or-raise), `js_error` (the `ExceptionDetails` decode, §2b) and `unexpected_type` (moved from the engine, unchanged), plus `MAX_ERROR_CHARS = 200` and `TRUNCATION_MARKER` (appended only when the clamp actually cut, so a truncated message cannot read as Chrome's complete words). It is a leaf (`nodriver` + `tool_errors` only, no `server`). It exists as a module rather than an engine method because the engine is grandfathered at its actual LOC and this fix had to shrink it, not grow it.
- **`embedded/cdp_element_cloner.py`** — the four per-aspect `dict / list / _convert_nodriver_result / _unexpected_type` ladders collapse into five identical `js_aspect_answer.parsed(await tab.evaluate(js_code))` calls, one per JS aspect including animations.
- **`_convert_nodriver_result` is DELETED.** Nothing references it; vulture is clean.
- **LOC** (`tools/check_file_budgets.py`'s own count, not a hand tally): `cdp_element_cloner.py` 1012 → 947; the `GRANDFATHER` row ratchets 1013 → 947 (cap == actual, no padding). The new leaf is **83** LOC, governed by the 1000-LOC default and not grandfathered.

### 3b. Why this is not a second home for F-869

`js_aspect_answer.parsed` and F-869's `page_storage` reader share the `JSON.stringify` + `json.loads` **idiom** and are deliberately **two homes**, because they carry two different failure policies: this one raises `ToolError` and decodes an `ExceptionDetails`; that one distinguishes `StorageBlockedError` from `StorageReadError` for a page that refuses storage access. An idiom is not a home — unifying them would force one policy on both call sites, which is a different defect from the one being fixed. Stated here and in the leaf's own docstring so a later sweep does not read the pair as duplication.

## 4. Verification

- **Real Chrome, after the fix** (same probe, same page): `BiDi nodes still present: 0` for all four aspects. `children[0]` is `{'tag_name': 'span', 'id': None, 'class_name': 'child a', 'text_content': 'one'}`; `class_list` is `['box', 'outer']`; `framework_handlers` is `{}` again, not `[]`; `stylesheets[0]` is `{'href': 'http://127.0.0.1:…/site.css', 'media': '', 'disabled': False, …}`. The raw `tab.evaluate` return is now `type=<class 'str'>`.
- **RED**, on `main`'s product code with this PR's tests and fakes: **12 failed / 13 passed** in `tests/test_cloner_schemas.py` —
  - `test_js_aspect_parses_the_one_json_string[structure|events|assets|related_files]` (4): the fixture feeds the JSON string a real tab returns; the old code raised `Unexpected return type: <class 'str'>`.
  - `test_every_aspect_script_returns_one_json_string[extract_structure|extract_events|extract_assets|extract_related_files.js]` (4): the root-cause pin. `extract_animations.js` **passed** it on `main`, which is what calibrates the pin.
  - `test_nested_containers_survive_the_transport` (1): the regression pin, payload measured from the real page.
  - `test_structure_to_file_summary_shape`, `test_transport_split_styles_cdp_others_js`, `test_complete_composes_all_six_aspects` (3): the repointed fixtures, same cause.
  - (An intermediate run of only the first three groups measured 9/16; the final-state number is 12/13.)
- **RED** for §2b's `ExceptionDetails` defect, measured separately after the double was rebuilt from nodriver's constructors: **11 failed / 29 passed** in `tests/test_cloner_error_convention.py` — including all five `test_a_script_that_threw_raises[…]`, which had been **green** against the hand-rolled `SimpleNamespace`. That flip is the whole evidence that the double encoded the bug.
- **GREEN** after: `test_cloner_schemas` + `test_cloner_error_convention` + `test_cdp_element_cloner` + `test_animation_honesty` + `test_animation_schema_v2` = 221 passed. Full unit lane (`-m "not integration"`) = **2412 passed, 1 skipped, 180 deselected**. Real-Chrome tiers: `tests/test_e2e_data_tools.py` 5 passed, `tests/test_e2e_hard_dom.py` 4 passed.
- **Real Chrome, the throw path, after the fix**: a ReferenceError gives `JavaScript error: ReferenceError: nosuchthing is not defined\n at <anonymous>:1:14… (line 0, column 13)` — 128 chars, **no** truncation marker; a `throw new Error('y'.repeat(100000))` gives a 239-char message ending in the marker. Both halves are pinned (`test_a_throws_message_is_bounded_and_says_it_was_cut`, `test_a_short_throws_message_carries_no_truncation_marker`): a marker on every message would claim a cut that did not happen, which is the same lie inverted. And `extract_element_structure(tab, "#nope")` still answers `{'error': 'Element not found'}` — the not-found guard survives stringification, unchanged.
- `tests/fakes.py` grows `js_aspect_answer` — THE one home for "what a real tab hands back for a cloner JS aspect", the sibling of `animation_evaluate_map` (F-846). **Every** aspect fixture in `tests/` now routes through it: `test_cloner_schemas.py` (5 sites), `test_cloner_error_convention.py` (4), `test_animation_honesty.py`'s `complete_tab` (1) and `test_animation_schema_v2.py` (2). The tree-wide grep for `evaluate_result=dict(` / `evaluate_result={` leaves exactly one survivor, `test_cloner_error_convention.py`'s `test_animations_unexpected_return_type_raises`, where a non-string IS the thing under test.
- `test_animation_honesty.py`'s R11 pin gained teeth: it asserted key PRESENCE only, so with the fix in place its bare-dict fixture turned all four JS aspects into `{"error": "Unexpected return type…"}` blocks while the test stayed green — the exact hollowing its docstring forbids. It now asserts each JS aspect carries a real payload, and names `styles`' fixture-induced `Element not found` explicitly rather than exempting it silently.
- Deleted golden `tests/goldens/extract_element_structure_list_convert.json` — it pinned the deleted tolerance's output.

## 5. Blast radius (what a caller saw)

Four tools returned corrupted payloads directly:
`extract_element_structure`, `extract_element_events`, `extract_element_assets`, `extract_related_files`.

**Nine** more carried the same corruption through composition or persistence:
`extract_element_structure_to_file`, `extract_element_events_to_file`, `extract_element_assets_to_file` (there is deliberately no `extract_related_files_to_file`), `clone_element_complete`, `extract_complete_element_to_file`, `clone_element_to_file` (all three via `extract_complete_element`'s `structure`/`events`/`assets`/`related_files` blocks), and `clone_element_progressive` + `expand_children` + `expand_events` (they slice the stored extraction, so the stored `children`/`event_listeners` lists were lists of transport nodes). 13 of 94 in total.

How a caller notices: `structure["children"][0]["tag_name"]` raises `KeyError: 'tag_name'` (the dict has `type`/`value`), `assets["images"][0]["src"]` the same, and any length/filter over `related_files["stylesheets"]` matches nothing by `href`. A caller that only string-searched the JSON (as the E2E tier does) saw nothing wrong. Unaffected: `extract_element_styles` / `extract_element_styles_cdp` / `extract_complete_element_cdp` (CDP path, no `evaluate`) and `extract_element_animations` (fixed in F-846).

## 6. Not claimed / deliberately unchanged / what remains

**NAMED FOLLOW-UP — the unfinished half of F-858's convention 2.** The four non-animation scripts still answer a missed selector with `{"error": "Element not found"}` and their aspects still **RETURN** it (verified against real Chrome after the fix: `extract_element_structure(tab, "#nope")` → `{'error': 'Element not found'}`). That *is* a tool return in the shape convention 2 bans, outside any named KEEP — `extract_element_animations` alone raises it. F-872 deliberately did not change it: it is an error-convention decision, not a transport one, and folding a behaviour change into a transport fix would hide it. Two docs that asserted the opposite were **false** and are corrected in this PR — `cdp_element_cloner.py`'s "the five now raise, so parity is a raise" comment and CLAUDE.md's "the only two `{"error": ...}` dicts left" engine row. Whoever takes this up must decide it for all four at once, and `test_cloner_error_convention.py`'s AST guard is where the decision gets pinned.

- **`NaN` durations now serialize as `null`.** `extract_assets.js` reads `video.duration` / `audio.duration`, which are `NaN` before metadata loads; `JSON.stringify` writes `NaN` as `null`, so "duration unknown" and "duration absent" are no longer distinguishable. This is the same trade the animations aspect already made and documented (`tests/test_animation_schema_v2.py:638`), and it is strictly better than the pre-fix state, where the whole `videos` list was BiDi nodes and no duration was readable at all. Noted, not fixed: a per-field `Number.isFinite` guard is a schema change, not a transport one.
- No change to any aspect's field names, caps, or options; the schemas are identical — only the *depth* at which values are real.
- `extract_styles.js` and `comprehensive_element_extractor.js` are packaged but never evaluated by the engine, so they are untouched; the new script pin derives its set from the filenames in `cdp_element_cloner.py`'s own source, so they are excluded by construction rather than by an exception list.
- `tools/package_verify.py`'s `EXPECTED_JS` is unchanged — no script was added or removed.
- No new `STEALTH_MCP_*` knob, no `os.environ` read, no silent except, no `typing.Any`.
