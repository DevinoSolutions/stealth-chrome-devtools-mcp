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

## 3. Fix (this PR)

Not a second unwrapper — a **complete recursive BiDi decoder would be a parallel way to do what the `JSON.stringify` idiom already does** (convention 4: "a change that introduces a second way to do something already done is a defect"), and it would have to track every `RemoteValue` type the spec allows (`date`, `map`, `set`, `regexp`, `node`, `weakLocalObjectReference` back-references) forever. Instead the four aspects join the one idiom:

- **`embedded/js/extract_structure.js`, `extract_events.js`, `extract_assets.js`, `extract_related_files.js`** — every `return` (the success return **and** the `{error: 'Element not found'}` guard) is now `return JSON.stringify(…)`. A string is the one shape deep serialization leaves alone.
- **`embedded/cdp_element_cloner.py`** — the four per-aspect `dict / list / _convert_nodriver_result / _unexpected_type` ladders collapse into ONE reader, `_js_answer`, which carries the nodriver citation in its docstring: exception-details check → `isinstance(str)` or `_unexpected_type` → `json.loads` → `isinstance(dict)` or `_unexpected_type`. `extract_element_animations` uses it too, so there is one reader for all five JS aspects rather than four-plus-one.
- **`_convert_nodriver_result` is DELETED.** Nothing references it; vulture is clean.
- **LOC**: `cdp_element_cloner.py` 1012 → 973; the `GRANDFATHER` row ratchets 1013 → 973 (cap == actual, no padding).

## 4. Verification

- **Real Chrome, after the fix** (same probe, same page): `BiDi nodes still present: 0` for all four aspects. `children[0]` is `{'tag_name': 'span', 'id': None, 'class_name': 'child a', 'text_content': 'one'}`; `class_list` is `['box', 'outer']`; `framework_handlers` is `{}` again, not `[]`; `stylesheets[0]` is `{'href': 'http://127.0.0.1:…/site.css', 'media': '', 'disabled': False, …}`. The raw `tab.evaluate` return is now `type=<class 'str'>`.
- **RED**, on `main`'s product code with the corrected fixtures: **9 failures** in `tests/test_cloner_schemas.py` —
  - `test_js_aspect_parses_the_one_json_string[structure|events|assets|related_files]` (4): the fixture feeds the JSON string a real tab returns; the old code raised `Unexpected return type: <class 'str'>`.
  - `test_every_aspect_script_returns_one_json_string[extract_structure|extract_events|extract_assets|extract_related_files.js]` (4): the root-cause pin. `extract_animations.js` **passed** it on `main`, which is what calibrates the pin.
  - `test_nested_containers_survive_the_transport` (1): the regression pin, payload measured from the real page.
- **GREEN** after: `tests/test_cloner_schemas.py` + `tests/test_cloner_error_convention.py` + `tests/test_cdp_element_cloner.py` = 63 passed. Full unit lane (`-m "not integration"`) = **2405 passed, 1 skipped**.
- `tests/fakes.py` grows `js_aspect_answer` — THE one home for "what a real tab hands back for a cloner JS aspect", the sibling of `animation_evaluate_map` (F-846). Every fixture that fed a dict now routes through it, so no future fixture can re-encode the bug.
- Deleted golden `tests/goldens/extract_element_structure_list_convert.json` — it pinned the deleted tolerance's output.

## 5. Blast radius (what a caller saw)

Four tools returned corrupted payloads directly:
`extract_element_structure`, `extract_element_events`, `extract_element_assets`, `extract_related_files`.

Seven more carried the same corruption through composition or persistence:
`extract_element_structure_to_file`, `extract_element_events_to_file`, `extract_element_assets_to_file`, `clone_element_complete` / `extract_complete_element_to_file` / `clone_element_to_file` (via `extract_complete_element`'s `structure`/`events`/`assets`/`related_files` blocks), and `clone_element_progressive` + `expand_children` / `expand_events` (they slice the stored extraction, so the stored `children`/`event_listeners` lists were lists of transport nodes).

How a caller notices: `structure["children"][0]["tag_name"]` raises `KeyError: 'tag_name'` (the dict has `type`/`value`), `assets["images"][0]["src"]` the same, and any length/filter over `related_files["stylesheets"]` matches nothing by `href`. A caller that only string-searched the JSON (as the E2E tier does) saw nothing wrong. Unaffected: `extract_element_styles` / `extract_element_styles_cdp` / `extract_complete_element_cdp` (CDP path, no `evaluate`) and `extract_element_animations` (fixed in F-846).

## 6. Not claimed / deliberately unchanged

- **The `{"error": "Element not found"}` payload the four scripts emit on a missed selector still passes through as a returned dict** rather than raising, exactly as before. Making it a raise (the shape `extract_element_animations` uses) is a separate error-convention decision, not a transport one, and folding it in here would hide a behaviour change inside a transport fix.
- No change to any aspect's field names, caps, or options; the schemas are identical — only the *depth* at which values are real.
- `extract_styles.js` and `comprehensive_element_extractor.js` are packaged but never evaluated by the engine, so they are untouched; the new script pin derives its set from the filenames in `cdp_element_cloner.py`'s own source, so they are excluded by construction rather than by an exception list.
- `tools/package_verify.py`'s `EXPECTED_JS` is unchanged — no script was added or removed.
- No new `STEALTH_MCP_*` knob, no `os.environ` read, no silent except, no `typing.Any`.
