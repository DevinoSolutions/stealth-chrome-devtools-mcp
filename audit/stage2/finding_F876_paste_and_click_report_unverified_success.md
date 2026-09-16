# F-876 — `paste_text` reports success for text the page refused, and `click_element` reports success for a click the target never received

**Status:** FIXED in this PR (product defect; live on 2.1.6 and on `main` at `b0ae010`)
**Opened by:** `audit/stage2/finding_F873_type_text_reports_success_without_typing.md` §6, which named both tools as sharing `type_text`'s shape and left them out of that PR on purpose
**Source at:** `fix/F873-type-text-silent-failure` = `258e1df` (= `main` `b0ae010` + the F-873 fix)
**Severity:** HIGH for `paste_text` (same class as F-873: the tool answers `True` for text the page did not take, five of seven measured controls), MEDIUM for `click_element` (the click is really dispatched, but the tool cannot say WHERE it landed — six of nine measured shapes deliver the click to something other than the target, or to nothing at all, and every one of them answers `True`).

---

## 1. What was measured, and where

Everything below is a measurement, not a reading of the code. All of it:

* **Chrome 152.0.7977.83** (`C:\Program Files\Google\Chrome\Application\chrome.exe`),
  headless, Windows 11 — the same build F-873 measured on.
* Driving the **product code path**: `DOMHandler.paste_text` /
  `DOMHandler.click_element` / `element_resolution.resolve_element`, imported from
  this worktree's `src/`.
* Each run on its own throwaway `--user-data-dir` under `%TEMP%\f876*\profile`.
  Never `~/.stealth-mcp`, never ports 19222/52554/7169, no process killed that the
  probe did not start.
* Against a local `file://` page written by the probe itself (no network).

Five probes: the tool-answer matrices (§2a, §2b), the per-path/per-trust
breakdown (§2c), the click-point equivalence check the fix's design rests on
(§2d), and the two the review added — the disabling ancestor (§2e) and the
re-enabling descendant (§2f).

---

## 2. Measured truth

### 2a. `paste_text` — five of seven controls refuse the text and the tool says `True`

`DOMHandler.paste_text(tab, selector, text)` with its default `clear_first=True`.
`before`/`after` are the element's own `value` and `textContent`, read straight
from the page on either side of the call.

| selector | pasted | before (`value` / `text`) | after (`value` / `text`) | tool answered | honest? |
|---|---|---|---|---|---|
| `<input readonly>` | `INJECT` | `""` / `""` | `""` / `""` | `true` | **no** |
| `<input type=range value=50>` | `80` | `"50"` / `""` | `"50"` / `""` | `true` | **no** |
| `<input type=date>` † | `2024-01-02` | `""` / `""` | `""` / `""` | `true` | **no** |
| `<input type=color>` | `#123456` | `"#000000"` / `""` | `"#000000"` / `""` | `true` | **no** |
| non-editable `<div>` | `INJECT` | `undefined` / `"PLAIN"` | `""` / `"PLAIN"` | `true` | **no** |
| `<div contenteditable>` | `hello-ce` | `undefined` / `""` | `""` / `"hello-ce"` | `true` | yes |
| plain `<input>` | `usb c hub` | `""` / `""` | `"usb c hub"` / `""` | `true` | yes |

Exactly F-873's §2b matrix, reproduced through the *other* text tool. `paste_text`
inserts with `Input.insertText` rather than per-character key events, and that
difference changes nothing: the five refusing controls refuse an insert just as
they refused the keys.

† **`date` is a build-dependent row and is not pinned.** It refused the insert on
this local Chrome 152.0.7977.83 build, which is why it is in the matrix. PR #110's
gate measured all three CI cells **accepting** digit entry into an
`<input type="date">`, so a refusal is not a property of the control. The E2E pins
here therefore parametrize `readonly`, `range`, `color` and the non-editable
`<div>` only, and `date` is out of the hint `text_entry.verify_received` raises
for the same reason (§4a).

One row deserves its own sentence, because it is new. The non-editable `<div>`'s
`value` is `undefined` **before** the call and `""` **after** it. Nothing about
the paste did that: `clear_first`'s programmatic `elem.value = ''` does not throw
on a `<div>` — it silently **creates an expando property** called `value` on the
element. So the tool's own clear step is what invented the empty string, and any
read-back that trusts `elem.value` on a non-input reads that expando rather than
the element's content. The read-back this fix uses (`text_entry.READ_JS`) is
unaffected *because* its baseline is taken AFTER the clear, so the expando is
present in both samples and the comparison is still "did anything move".

### 2b. `click_element` — what the page actually received

Same run, `DOMHandler.click_element(tab, selector)`, the page logging every
`click` listener it owns.

| case | tool answered | what the page received |
|---|---|---|
| plain `<button>` (control) | `true` | `click:plainbtn` |
| `<button>` fully covered by a `z-index:10` overlay | `true` | **`click:overlay`** — the overlay, not the target |
| `<button>` under a `pointer-events:none` overlay (control) | `true` | `click:pen` — passes through, correct |
| `<button disabled>` | `true` | **nothing** |
| `<button style="pointer-events:none">` | `true` | **nothing** |
| zero-size `<button>` (`width:0;height:0`, still laid out) | `true` | **nothing** |
| `<button style="display:none">` | `true` | `click:gone` — but see §2c: an **untrusted, synthetic** click |
| `<button style="visibility:hidden">` | `true` | **nothing** |
| `<button style="position:absolute;left:-500px;top:-500px">` | `true` | **nothing** |
| resolved, then removed from the DOM before the click | raises | nothing |

Six of the nine either deliver the click to a different element or deliver it to
nobody, and the tool reports the same `True` for all of them as for the control.
The detached case is the one that already behaves: the tool re-resolves the
selector and `resolve_element` answers nothing, so it raises
`ToolError: Failed to click element: Element not found: #detach`. (Driving the
*stale handle* directly, as a caller cannot, `Element.mouse_click` raises
`Exception: could not find position for <button id="detach">`.)

### 2c. Which path each click took, and whether it was trusted

The same seven targets, with `Element.mouse_click` (the primary path) and
`Element.click` (the error-only fallback) driven separately from a clean log, and
`event.isTrusted` recorded:

| case | `getClientRects()[0]` | `elementFromPoint` at that point | `mouse_click` | page saw (primary) | page saw (fallback) |
|---|---|---|---|---|---|
| plain button | 45.7 × 21 | `BUTTON#plainbtn` (same) | returned | `click:plainbtn:trusted` | `click:plainbtn:untrusted` |
| covered | 66.4 × 21 | **`DIV#overlay`** | returned | `click:overlay:trusted` | `click:covered:untrusted` |
| disabled | 67.9 × 21 | `BUTTON#disabled` (same) | returned | *(nothing)* | *(nothing)* |
| `pointer-events:none` | 67.1 × 21 | **`BODY`** | returned | *(nothing)* | `click:pe-none:untrusted` |
| zero-size | **0 × 0** | **`BODY`** | returned | *(nothing)* | `click:zero:untrusted` |
| `display:none` | **no box at all** | `HTML` | **raises** `could not find position` | *(nothing)* | `click:gone:untrusted` |
| `visibility:hidden` | 24.9 × 21 | **`BODY`** | returned | *(nothing)* | `click:vis-hidden:untrusted` |
| off-viewport (`left:-500px`) | 33.5 × 21 at **`(-483.2, -489.5)`** | **`null`** | returned | *(nothing)* | — |

Two rows of that table need their own sentence. The off-viewport button keeps a
real 33.5 × 21 box at a **negative** point; `scroll_into_view()` does **not**
bring it back (it is `position:absolute` outside the flow, and the point is
unchanged after the scroll — measured both before and after); the coordinate
click is dispatched at those negative coordinates and reaches nobody; and
`document.elementFromPoint` answers `null` there, which is a distinct fact from
"something else was on top" and gets its own reason code. Separately, a button
inside an **open shadow root** hit-tests to `DIV#host` (`same: false`,
`host.contains(button)` — i.e. the element's own `contains` — `false`), which is
Chrome's retargeting and is a known edge of this whole approach (§6).

Three facts come out of this table and all three are load-bearing:

1. **`elementFromPoint` at the click point is a complete oracle for "did the
   target get it".** It names the overlay for the covered case, `BODY` for the
   three that hit nothing, and the target itself for the two that work — and for
   `disabled`, where the hit-test *does* name the button and Chrome still
   suppresses the activation, so the element's own `disabled` flag is the second
   fact the record needs.
2. **`display:none` silently downgrades the tool to a synthetic click.**
   `mouse_click` raises (no content quads), `click_element`'s `except` logs at
   DEBUG and falls back to `Element.click`, which is `(el) => el.click()` inside
   the page: an **untrusted** click with no coordinate and no hit-testing. The
   whole thrust of `tests/test_e2e_interaction_fidelity.py::test_click_fidelity_is_trusted_input`
   is that this tool dispatches trusted input; the caller is never told when it
   did not.
3. **The fallback is not useless**, which is why this fix does not delete it: for
   `pointer-events:none`, zero-size, `visibility:hidden` and `display:none` it is
   the only thing that reaches the element at all. It is a different *kind* of
   click, and the honest fix is to name which one happened.

### 2d. The click point has to be `getClientRects()[0]`, not `getBoundingClientRect()`

`Element.mouse_click` clicks `Position(quads[0]).center`, where the quads come
from `DOM.getContentQuads` — the element's **first box**, not its bounding box.
A record that named a different point would be describing a click that never
happened. Measured on four shapes:

| element | nodriver's click centre | `getClientRects()[0]` centre | `getBoundingClientRect()` centre |
|---|---|---|---|
| plain `<button>` | `(22.828125, 10.5)` | `(22.828125, 10.5)` | `(22.828125, 10.5)` |
| `<a>` wrapped over **4 line boxes** | `(39.5859375, 30.5)` | `(39.5859375, 30.5)` | `(39.5859375, **59**)` |
| padded + bordered `<button>` | `(54.984375, 136.5)` | `(54.984375, 136.5)` | `(54.984375, 136.5)` |
| inline `<img>` | `(133.765625, 126.0)` | `(133.765625, 126.0)` | `(133.765625, 126.0)` |

`getClientRects()[0]` is byte-equal to nodriver's centre in every shape including
the wrapped inline, where the bounding box is 28.5 px off. So the aim probe reads
`getClientRects()[0]` and **nothing else**: an element with no client rects has
no box at all, and `AIM_JS` answers `rendered: false` with a zeroed rect and a
`null` point rather than falling back to `getBoundingClientRect()`, which returns
an all-zero rect there and would invent a click point at the viewport origin.

### 2e. `elem.disabled` does not see a disabling ANCESTOR

Measured on the same Chrome, a `<button>` inside a `<fieldset disabled>` against
a plain `<button disabled>` and an enabled control:

| target | `elem.disabled` | `matches(':disabled')` | `elementFromPoint` | page received |
|---|---|---|---|---|
| `<fieldset disabled><button>` | **`false`** | **`true`** | itself | **nothing** |
| `<button disabled>` | `true` | `true` | itself | **nothing** |
| `<button>` (control) | `false` | `false` | itself | `click:enabled:trusted` |

The IDL attribute reflects only the element's OWN `disabled` content attribute.
So the fieldset row hit-tests to itself *and* reports `disabled: false`, which is
`hit_is_target: true, reason: null` — a record claiming the click reached a
control that never acted on it, i.e. exactly the false claim this finding
retires, reintroduced one level up. The aim therefore asks `matches(':disabled')`,
which is also what covers `<option disabled>` and every other inherited form.

### 2f. `pointer-events: none` does not mean the click was lost

Same run. A `pointer-events: none` span containing a child that re-enables them:

| target | computed `pointer-events` | `elementFromPoint` | `contains(hit)` | page received |
|---|---|---|---|---|
| `#pe-off` (child `#pe-on` re-enables) | `none` | **`SPAN#pe-on`** | **`true`** | `click:pe-on:trusted`, `click:pe-off:trusted` |

The click reaches the child and **bubbles to the target**, so both listeners fire.
A `reason` order that consulted `pointer-events` before the hit-test would report
`pointer-events-none` for a click that worked. The hit-test therefore splits the
decision: when the hit is the target or inside it, the only remaining question is
whether the target could act (§2e); only when it is NOT is there a cause to name.

---

## 3. Root cause

**Both are one sentence, and it is F-873's sentence.** `paste_text` ends

```python
await tab.send(cdp.input_.insert_text(text))
return True
```

and `click_element` ends

```python
await element.mouse_click()   # or, on any error, await element.click()
return True
```

Neither asks the page anything between the dispatch and the `return`. The tool
reports **the success of its own dispatch, not the success of the interaction** —
which is exactly what F-873's §3 concluded about `type_text`, one level up from
either tool's mechanics.

The two differ in what an honest answer even *is*, and that is why they get
different fixes:

* For `paste_text` the question is closed and already has a home: "did the
  element's text move". `text_entry` answers it.
* For `click_element` there is no such oracle. "Did the page react" is unbounded
  (a navigation, a fetch, a re-render, nothing at all — a correct click on a
  correct button may legitimately change nothing observable). What IS bounded and
  IS decidable is two facts: which *kind* of click was dispatched, and what was
  under the click point. §2c shows those two together explain every measured
  failure. So `click_element` gains a record, not a verdict.

---

## 4. Fix

### 4a. `paste_text` joins `text_entry`'s read-back — no new mechanism

`dom_handler.paste_text` now reads the baseline with `text_entry.entered_text`
**after** the clear, sends the one `Input.insertText`, reads again, and hands both
to `text_entry.verify_received`. Not one line of new "read a field back" code: the
leaf F-873 created is the one home and this is its second consumer, which is the
whole reason it took an element as an argument.

Consequences that follow from reusing it rather than re-deriving it:

* the failure message carries **the selector and two counts and nothing else** —
  never the pasted text, because a raised `ToolError` reaches the caller, the
  debug ring (`log_tool_failure`, ring-only per F-782/F-835) and Sentry, and the
  field may be a password box (F-873 §4b, F-869's discipline);
* the check is **"did anything change"**, with the same named cost: a control
  that normalises the paste back to the string it already held now raises
  (F-873 §4a, and §6 below);
* empty `text` is not a failure — pasting nothing that changes nothing is not a
  refusal, and the guard is skipped, exactly as `type_text` skips an empty line.

`paste_text`'s signature and return type are **unchanged** (`bool`). What changed
is that the `True` is now earned.

### 4b. `click_element` gains a record, and a new leaf owns its one JS read

New leaf `embedded/click_target.py` — **THE one home for "where was this click
aimed, and what was under that point"**:

* `AIM_JS` + `aim(element)` — the ONE read. A single `Element.apply` returning a
  JSON **string** (same shape discipline, and the same reason, as
  `text_entry.READ_JS` and `page_storage.READ_JS`: `apply` hands back
  `result[0].value`, and a script that threw lands there as `None`, so a non-`str`
  answer is "could not be read" and says so rather than being mistaken for an
  empty answer). It reports the target's first client rect, the click point
  computed from it (§2d), `document.elementFromPoint` at exactly that point, and
  four flags that §2c/§2e proved are needed to read the hit:
  `matches(':disabled')` — **not** `elem.disabled`, §2e — plus computed
  `pointer-events`, computed `visibility`, and whether the hit element is the
  target or inside it.
* `_shape(...)` — the tag/id/class descriptor, and the ONLY thing said about any
  element. **No text content, ever**, on either the target or the hit: an overlay
  is frequently a modal or a consent banner and its text is the page's, not the
  tool's to echo into an MCP payload. Bounded twice: `MAX_CLASSES` caps how MANY
  classes are named and `MAX_TOKEN_CHARS` caps how long any one of them — or the
  id — may be, because a count is not a bound when one hashed class name from a
  build tool can outweigh eight ordinary ones.
* `reason(...)` — the closed code set, decided in one place from the facts above.
  The hit-test splits it: `not-rendered` → `off-viewport` → `zero-size`, then
  **if the hit is NOT the target or inside it** `not-visible` →
  `pointer-events-none` → `covered`, and **if it is** `disabled` → `None`.
  `off-viewport` is checked before `zero-size` because `elementFromPoint`
  answering `null` is a different fact from a degenerate box, and only one of the
  two can be read from the hit. The split itself is §2f's measurement, not taste:
  a `pointer-events: none` element whose child re-enables them DOES receive the
  click, so a flag consulted before the hit-test would name a cause for a click
  that worked.
* `record(...)` — composes the returned dict. `COORDINATE` / `SYNTHETIC` are the
  two dispatch kinds and they are this module's constants.

A leaf in the sense `CLAUDE.md` uses: it imports `tool_errors` and nothing else
from the package, the element arrives as an argument, and it has **no error
policy** — the one thing it raises is "the page did not answer with the promised
JSON", the same not-a-policy `text_entry.entered_text` raises.

`dom_handler.click_element` keeps the ORDER and the POLICY:

```
resolve → scroll_into_view → sleep → aim (one apply, BEFORE the click)
        → mouse_click, or on error DEBUG-log + element.click()
        → record(selector, aim, dispatch)
```

The aim is read **before** the click and the docstring says so: it is the state
the click was aimed at. Reading it afterwards would describe a page the click may
already have changed (a modal that closed, a navigation that detached the node),
and for the `display:none` row there would be no node left to ask at all.

**What `click_element` deliberately does NOT do:**

* It does not raise for any of the five failing shapes. Every one of them is a
  fact about the page, not a failure of the tool: a real user clicking those
  coordinates gets the same result, and an agent clicking a `display:none`
  element on purpose is a legitimate use the synthetic fallback serves. The
  record names what happened; the caller decides.
* It does not delete the synthetic fallback (§2c fact 3) — it labels it.
* It does not acquire a "did the page react" oracle. Navigation, DOM mutation and
  network are unbounded and the absence of any of them is not evidence. Named as
  a limit in §6.

### 4c. The return-shape change

`click_element` returns `dict[str, object]` instead of `bool`:

```json
{
  "selector": "#covered",
  "dispatch": "coordinate",
  "point": {"x": 41.1953125, "y": 18.5},
  "size": {"width": 66.390625, "height": 21.0},
  "target": {"tag": "button", "id": "covered", "classes": []},
  "hit": {"tag": "div", "id": "overlay", "classes": []},
  "hit_is_target": false,
  "reason": "covered"
}
```

There is no `"clicked": true` field. It would be redundant — `dispatch` is
present on every successful return and strictly more informative, and a failure
raises — and a redundant field is a second way to ask the same question.

`point`, `size` and `hit` are `null` together for the `not-rendered` row, because
an element with no box has no click point to name.

---

## 5. Verification

* `tests/test_paste_click_verification.py` — hermetic pins (`FakeTab`,
  `FakeTextField` and the new `FakeClickTarget`, all from `tests/fakes.py`).
* `tests/test_e2e_paste_click_verification.py` — real-Chrome pins on their own
  `tmp_empty_root` session root, marked exactly like the F-873 sibling.
* Counts, the RED→GREEN transition and the narrow confirmation lane are recorded
  in §5b.

### 5a. Characterization pins deliberately flipped (SOFT goldens)

| test | was | now | why |
|---|---|---|---|
| `tests/goldens/tool_surface.json` (`click_element`) | `output_schema` `{"result": {"type": "boolean"}}`, description ending `bool: True if clicked successfully.` | the record's object schema, description ending with the record's fields | the HARD wire-surface golden, regenerated **deliberately** with this justification per `CONTRIBUTING.md`: the tool's return type and its docstring both changed on purpose, and this is the same PR |
| `tests/goldens/tool_surface.json` (`paste_text`) | description ending `bool: True if pasted successfully.` | `bool: True — the text was pasted AND the page took it.` | the old line was the claim this finding shows to be false; the schema is unchanged |
| `tests/goldens/tool_surface.json` (`type_text`) | description ending `bool: True if typed successfully.` | `bool: True — … AND the field's own read-back showed them land.` | F-873 corrected `DOMHandler.type_text`'s docstring and left the WRAPPER's Returns line alone, so the served surface still carried the claim F-873 disproved. Folded in here rather than left as a follow-up, because this PR is regenerating that golden anyway and a third stale line in the same file is not worth a second regeneration |
| `test_e2e_interaction_fidelity.py::test_form_semantics` (disabled arm) | `assert await click(...) is True` with a docstring bullet calling the silence a FINDING | asserts the record's `reason == "disabled"` | the pin's own comment ("the tool cannot tell you the control was inert (FINDING: no disabled-state guard)") named this fix |
| `test_e2e_interaction_fidelity.py` — the 7 bare `assert await click(...)` sites inside the TWO tests this PR already edits | truthy-only, i.e. vacuous for a dict return | `reason == "covered"` + the hit's id (occlusion), `hit_is_target is True` (five plain controls), `dispatch == "coordinate"` (offscreen) | a non-empty dict is always truthy, and these were near-vacuous before too since the tool raises on failure. Scoped to the two tests already touched; the other ~17 sites across four files are §6's named follow-up, partly because `fix/F873-type-text-silent-failure` edits this file concurrently |
| `test_e2e_dynamic_sites.py` (6 asserts) | `assert await click(...) is True` | `assert (await click(...))["dispatch"] == "coordinate"` | the faithful translation of the old claim ("a click was really dispatched") and deliberately **not** the stronger `reason is None`, which would be a new claim on six real pages this PR did not measure |
| `test_xpath_dispatch.py::test_the_issue_15_repro_selector_clicks` | `assert await DOMHandler.click_element(...) is True` | asserts the returned record's `selector` | a return-shape change; the test's claim (one grammar, both tools) is untouched. Its `_FakeElement` gains an `apply` answering the minimum well-formed aim — that double exists to pin which resolution surface a selector reaches, and modelling geometry is `FakeClickTarget`'s job |
| `test_silent_excepts_log.py::test_click_element_mouse_click_fallback_logs_at_debug` | `assert result is True` | asserts `dispatch == "synthetic"` | same return-shape change; the DEBUG line it exists to pin is byte-unchanged, and the new assert additionally proves the fallback is *labelled* |
| `test_silent_excepts_log.py::test_paste_text_clear_fallback_logs_at_debug` | `element.apply` failed for EVERY call | only the clear fails; the read-back answers | the read-back is a second `apply` on the same element, so a double that fails all of them would make the tool raise for an unreadable field before it ever reached the clear fallback this test pins |
| `tests/fakes.py` | — | gains `FakeClickTarget`; `FakeTextField` gains `insert()` | the click double's aim answer is COMPUTED from its own state (including the off-viewport rule, derived from its geometry rather than asserted), on `FakeTextField`'s model, so no fixture can encode the bug. `insert()` is the ONE place the text double commits text, reached by both the key-event and the `Input.insertText` seam |
| `tests/fixture_app/interactions.html` + `app.js` | — | five new targets (`pe-none`, `zero-size`, `display-none`, `vis-hidden`, `offviewport`) and two paste targets (a plain and a contenteditable `<div>`) | every one is a shape §2b/§2c measured; each logs its own click with `isTrusted`, so "the target received nothing" and "it received a SYNTHETIC one" are distinguishable from the action log alone. No existing test clicks any of them |

### 5b. Numbers

* `tests/test_paste_click_verification.py` at the RED commit (`654e35c`): **18
  failed, 4 passed** of 22 nodes. Every failure is the defect, not a harness
  error — `DID NOT RAISE ToolError` for the paste rows, `isinstance(True, dict)`
  / `'bool' object is not subscriptable` for the click rows, `At index 1 diff:
  'mouse_click' != 'aim'` for the ordering pin (no aim is read at all), and an
  `ImportError` for the leaf that does not exist yet. At the fix: **26 passed**
  (three nodes added after the RED commit, each once its own probe measured the
  shape: `off-viewport`, the disabling ancestor, the re-enabling descendant, plus
  the token-length bound).
* `tests/test_e2e_paste_click_verification.py` at the RED commit: **13 failed, 4
  passed** of 17 nodes, same reasons. At the fix: **18 passed** (the `date` paste
  row was dropped — see the `†` note in §2a — and the `off-viewport` and
  `<fieldset disabled>` click rows added).
* The `<fieldset disabled>` pin was confirmed load-bearing by reverting the one
  token in `AIM_JS` to `!!elem.disabled`, clearing `__pycache__` and re-running
  the parametrized node: **1 failed, 5 passed** — only the fieldset row, exactly
  the shape §2e measured. Restored immediately.
* Narrow confirmation lane, `STEALTH_MCP_NO_ERROR_REPORTING=1 PYTHONUTF8=1`,
  `test_paste_click_verification` + `test_type_text_verification` +
  `test_dom_handler` + `test_tool_dispatch` + `test_mcp_protocol_surface` +
  `test_error_typing` + `test_silent_excepts_log` + `test_tool_sections_contract`
  + `test_doc_claims` + `test_xpath_dispatch` + `test_release_contract`:
  **194 passed**.
* The five integration nodes whose asserts this PR flipped, run individually:
  `test_e2e_interaction_fidelity::test_form_semantics` and
  `::test_click_respects_occlusion_and_offscreen` — **2 passed** (re-run after
  the tightened asserts, together with the E2E file: **20 passed**);
  `test_e2e_dynamic_sites::test_spa_history_route_swap_and_requery`,
  `::test_virtualized_and_finite_infinite_lists`,
  `::test_custom_elements_slots_and_popup_lifecycle` — **3 passed**.
* LOC (`tools/check_file_budgets.py`'s own rule — every line, blanks and comments
  included): `dom_handler.py` 904 → **940**, `text_entry.py` 250 → **276**,
  `click_target.py` **293**. All three under the 1000-LOC default; no
  `GRANDFATHER` row is involved and none moved.
* The full unit lane and the full integration lane are the coordinator's
  pre-push gate and are deliberately **not** claimed here.

---

## 6. Not claimed / deliberately unchanged / what remains

* **There is still no "did the page react" oracle, and this finding does not want
  one.** Navigation, DOM mutation, network activity and focus changes are all
  unbounded, all racy, and none of their absence is evidence: a correct click on a
  correct button can legitimately change nothing a tool can see within any
  deadline. What `click_element` now reports is bounded and decidable — which kind
  of click was dispatched, and what was under the point — and §2c shows that pair
  explains every measured failure. A caller that needs "did it work" still has to
  assert on the page.
* **The aim is read BEFORE the click, so it describes the page the click was aimed
  at, not the page afterwards.** A page that moves an overlay away in the same
  frame as the click is reported as covered; a page that puts one up is not. That
  is the correct claim to make (see §4b) but it is a claim about a moment, and it
  is stated in the tool's docstring rather than left for a caller to discover.
* **`reason: "covered"` does not name a CAUSE beyond the hit element's shape.**
  The record says the click point was over `div#overlay`; it does not say why, and
  it deliberately carries none of that element's text (§4b). An operator who needs
  to know what the overlay IS has `query_elements` and `get_page_content`.
* **The disabled row still dispatches a real coordinate click.** Chrome suppresses
  the activation; the tool does not pre-check and refuse. Adding a refusal would
  be a second way to decide what the browser already decides, and it would break
  the legitimate case of clicking a control that a script enables between the
  probe and the click. The record names it; the click still goes out.
* **`paste_text` inherits F-873's two named costs, one of them only in part.** A
  control that accepts only some of what was pasted still answers `True` (F-873
  §4a), unchanged. The other — a value that is legitimately IDENTICAL afterwards
  reads as a refusal — applies here **only under `clear_first=False`**: the
  default empties the field first, so the baseline is `""` and re-pasting the
  text the field already held still registers as a change. Pasting into a
  pre-filled field with `clear_first=False` the exact string it already contains
  is the one shape that raises wrongly.
* **`Element.mouse_click` can return having sent nothing, and the record would
  still say `coordinate`.** nodriver's implementation catches `AttributeError`
  from `get_position()` and returns; `get_position` itself returns `None` on an
  `IndexError` from `getContentQuads`, and `mouse_click` then logs a warning and
  returns. Neither path raises, so `click_element`'s fallback is not taken and
  `dispatch` reads `"coordinate"` for a click that was never dispatched. It is
  narrow — the `display:none` shape this PR measured raises a plain `Exception`
  and does reach the fallback (§2c) — but it is the one residual over-claim in
  the record, and the remedy would be patching nodriver, which this PR does not
  do.
* **Inside an iframe the two geometries diverge.** `getClientRects()` is
  iframe-relative while `DOM.getContentQuads` is main-frame relative, so the aim
  point and nodriver's click point would not agree. Unreachable today —
  `element_resolution` does not pierce iframes — and named here so it is not
  discovered the day it becomes reachable.
* **About thirty `assert await click(...)` sites in the E2E suite are vacuous for
  a dict return** (any non-empty dict is truthy), and were near-vacuous before it
  since the tool raises on failure. This PR tightened the ones inside the two
  files it already edits — seven in `test_e2e_interaction_fidelity.py` (the two
  tests it touched) and six in `test_e2e_dynamic_sites.py` — to assert
  `dispatch`, `hit_is_target` or `reason`. The remaining ~17, in
  `test_e2e_data_tools.py`, `test_e2e_hard_dom.py`, `test_e2e_interaction.py` and
  the rest of `test_e2e_interaction_fidelity.py`, are a test-hygiene follow-up
  deliberately left out: rewriting them all would widen this diff across files
  this PR has no other reason to touch, and `fix/F873-type-text-silent-failure`
  edits one of them concurrently.
* **`select_option` and `upload_file` are the two remaining interaction tools that
  answer `True` without asking the page anything.** `select_option`'s `text` arm
  in particular still calls `send_keys` on a `<select>` and returns unconditionally
  (`dom_handler.py`), which is F-873's shape a third time, and `upload_file`
  returns the paths it was given rather than the files the input holds. Both are
  out of scope here and neither has been measured; naming them is the claim, not
  diagnosing them.
* **The overlay/covered detection uses `document.elementFromPoint`, which
  retargets across a shadow boundary.** Measured (§2c): a button inside an OPEN
  shadow root hit-tests to `DIV#host`, and the button's own `contains(host)` is
  `false`, so `hit_is_target` reads `false` and `reason` reads `covered` for a
  click that in fact reached the button. That is a false alarm, it is named here
  rather than discovered later, and the narrower fix (`elementsFromPoint` /
  walking `shadowRoot.elementFromPoint`) is a second traversal this PR
  deliberately does not add. The fixture page's shadow-root cases live in
  `tests/test_e2e_hard_dom.py` and are untouched.
* **`verify_received`'s message changed wording**, from "typed N character(s)" to
  "entered N character(s)", and its hint no longer names `date`. The first is
  because `paste_text` reaches the same controls through one `Input.insertText`
  and a message about key events would be about the wrong mechanism half the
  time. The second is evidence, not taste: the local Chrome 152 build refused
  digits into a date field and PR #110's gate measured all three CI cells
  accepting them, so naming it as a known refusal would be a claim the evidence
  does not support. No test pinned the old wording; F-873's finding quotes it as
  a historical measurement and is deliberately left alone.
