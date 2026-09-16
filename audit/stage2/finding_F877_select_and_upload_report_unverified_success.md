# F-877 — `select_option` reports success for an option it never selected (and for a control it never touched), and `upload_file` reports the files it was *given*, never the files the input *holds*

**Status:** FIXED in this PR (product defect; live on 2.1.6 and on `main` at `b0ae010`)
**Opened by:** `audit/stage2/finding_F876_paste_and_click_report_unverified_success.md` §6, which named both tools as the two remaining interaction tools that "answer `True` without asking the page anything" and explicitly left them unmeasured
**Source at:** `fix/F876-paste-click-verified` = `6f0bb6f` (= `main` `b0ae010` + the F-873 fix + the F-876 fix)
**Severity:** HIGH for `select_option` (eleven measured cases answer `True` having changed nothing the caller asked for — and one of them silently changes a **different** `<select>` on the page), MEDIUM for `upload_file` (one measured case answers a count of 2 for an input that holds 1; the rest of the class is structural — nothing is ever read back, and the success payload echoes absolute file paths)

---

## 1. What was measured, and where

Everything below is a measurement, not a reading of the code. All of it:

* **Chrome 152.0.7977.83** (`HeadlessChrome/152.0.0.0`, protocol `1.3`, build
  `@79460ebecaa5625e57a5fb679a735659e73dc687`), headless, Windows 11 — the same
  build F-873 and F-876 measured on.
* Driving the **product code path**: `DOMHandler.select_option` /
  `DOMHandler.upload_file` / `element_resolution.resolve_element`, imported from
  this worktree's `src/`.
* Each run on its own throwaway `--user-data-dir` under
  `%TEMP%\f877*\profile`. Never `~/.stealth-mcp`, never ports 19222/52554/7169,
  no process killed that the probe did not start, no credential entered anywhere.
* Against a local `file://` page written by the probe itself (no network), which
  logs every `input`/`change` event it receives with `event.isTrusted` and the
  control's value at that moment.

Three probes:

1. **§2a/§2c, sequential** — every case against one long-lived page, which is how
   a real session reaches these tools (state carries over between calls). This is
   the probe that surfaced the cross-control leak.
2. **§2a/§2c, isolated** — each case re-loads the page first, so no case can
   inherit another's focus or Chrome's typeahead buffer. Every number quoted below
   is from this probe unless the row says "sequential".
3. **§2b, the text arm's real matching rule** — what Chrome's `<select>` typeahead
   actually matches, since that is the whole of today's `text=` implementation.

---

## 2. Measured truth

### 2a. `select_option` — eleven cases answer `True` for a selection that did not happen

`DOMHandler.select_option(tab, selector, value=/text=/index=)`. `before`/`after` are
the `<select>`'s own `selectedIndex` and `value`, read straight from the page on
either side of the call. "page saw" is the event log, `isTrusted` included.

| # | call | before | after | page saw | answered | honest? |
|---|---|---|---|---|---|---|
| 1 | `#sel-basic, value="two"` | `0` / `one` | `1` / `two` | `change:false` | `true` | yes |
| 2 | `#sel-basic, index=2` | `1` / `two` | `2` / `three` | `change:false` | `true` | yes |
| 3 | `#sel-basic, text="Beta"` | `0` / `one` | `1` / `two` | `input:true`, `change:true` | `true` | yes |
| 4 | `#sel-basic, value="nonexistent"` | `0` / `one` | **`-1` / `""`** | `change:false` | `true` | **no** — and it *destroyed* the existing selection |
| 5 | `#sel-basic, index=99` | `-1` / `""` | `-1` / `""` | *(nothing)* | `true` | **no** |
| 6 | `#sel-basic, index=-1` | `-1` / `""` | `-1` / `""` | *(nothing)* | `true` | **no** |
| 7 | `#sel-basic, text="Delta"` | `0` / `one` | `0` / `one` | *(nothing)* | `true` | **no** |
| 8 | `#sel-disabled, text="Beta"` (fresh page) | `0` / `one` | `0` / `one` | *(nothing)* | `true` | **no** |
| 9 | `#sel-disabled, text="Beta"` (focus elsewhere, buffer expired) | `0` / `one` | `0` / `one` | **`input:true` + `change:true` on `#sel-basic`, which moved `one` → `two`** | `true` | **no — it changed a different control** |
| 10 | `#sel-empty` (0 options), `value="one"` | `-1` / `""` | `-1` / `""` | `change:false` | `true` | **no** |
| 11 | `#sel-empty` (0 options), `index=0` | `-1` / `""` | `-1` / `""` | *(nothing)* | `true` | **no** |
| 12 | `#sel-optdisabled, text="Delta Two"` (a `disabled` `<option>`) | `0` / `d1` | `0` / `d1` | *(nothing)* | `true` | **no** |
| 13 | `#not-file` — an `<input type="text">` | `value ""` | **`value "x"`** | `change:false` | `true` | **no — it wrote into a text box** |
| 14 | `#a-div` — a `<div>` | — | an expando `div.value = "x"` | *(nothing)* | `true` | **no** |
| 15 | `#sel-multi` (`multiple`), `value="two"` | `[]` | `["two"]` | `change:false` | `true` | yes (but see §6) |
| 16 | `#sel-optdisabled, value="d2"` (a `disabled` `<option>`) | `0` / `d1` | `1` / `d2` | `change:false` | `true` | yes-ish (see §6) |
| 17 | `#sel-basic`, no criteria at all | `1` / `two` | `1` / `two` | *(nothing)* | **raises** | yes |
| 18 | `#nope` — no such element | — | — | *(nothing)* | **raises** | yes |

Eleven of eighteen (rows 4–14) answer `True` for something that did not happen.
Three of those eleven are worse than a silent no-op:

* **Row 4** does not merely fail to select — `select.value = "nonexistent"` sets
  `selectedIndex` to `-1`, i.e. it **clears the selection the page already had**,
  fires a `change` saying so, and reports success. A caller who asked for a value
  that is not in the list gets a form in a state no user could have produced.
* **Row 9** is the one that is not a no-op at all. `send_keys` focuses the element
  first; a `disabled` `<select>` cannot take focus, so the keys go wherever focus
  already was. Measured in isolation (probe 3: select `#sel-basic` by text, wait
  1.5 s for Chrome's typeahead buffer to expire, then ask for `#sel-disabled`):
  `#sel-disabled` is untouched and `#sel-basic` moves from `one` to `two`, with a
  **trusted** `input` + `change` pair. The tool answers `True`. Nothing in the
  answer distinguishes this from row 3.
* **Row 13** is the same shape one control over: there is no check anywhere that
  the resolved element is a `<select>` at all, so `select.value = "x"` writes into
  whatever it resolved to. On an `<input type="text">` that is a real value change
  with a real `change` event.

### 2b. What the `text=` arm actually matches

The `text=` arm is `await select_element.send_keys(text)` — nodriver dispatches one
`char` event per character, and what consumes them is **Chrome's own `<select>`
typeahead**. So the tool's documented "Option text content" is in fact a
typeahead query, with all of that mechanism's properties. Measured, each case on a
freshly loaded page:

| query | against | result |
|---|---|---|
| `"Beta"` | `Alpha` / `Beta` / `Gamma` | selects `Beta` — exact text works |
| `"Bet"` | same | selects `Beta` — **a prefix is enough** |
| `"beta"` | same | selects `Beta` — **case-insensitive** |
| `"Delta"` | same | selects nothing |
| `"Delta Two"` | `Delta` / `Delta Two` *(disabled)* / `Epsilon` | selects nothing — **typeahead skips a `disabled` option** |
| `"Spaced Out"` | `<option>\n Spaced   Out\n</option>` (`.text` is `"Spaced Out"`) | no change event — the match resolves to the option that was **already** selected (typeahead searches from the option *after* the current one and wraps) |
| `"TextOne"` | `<option label="LabelOne">TextOne</option>` / `TextTwo` | selects **`TextTwo`** — the wrong option |
| `"Gamma"` immediately after a previous `text=` call (sequential probe) | `Alpha` / `Beta` / `Gamma` | selects nothing — the previous query is still in Chrome's buffer, so the search string is `"AlphaGamma"` |

So the arm is: case-insensitive **prefix** matching, over a **live buffer with a
~1 s timeout shared with the previous call**, searching **from the option after the
current one** with wraparound, skipping `disabled` options, and resolving
`label=`/text collisions in a way that picked the wrong option here. It is not
"select the option whose text is X", and it answers `True` for every one of those
outcomes.

Two of those properties are load-bearing for the fix and are preserved by it: the
prefix match and the case-insensitivity (§4a). The rest are the defect.

### 2c. `upload_file` — the count is the request, not the result

`DOMHandler.upload_file(tab, selector, paths)`. `after` is the input's own
`files.length` and `files[i].name`, read from the page.

| # | call | input | answered | `input.files` after | page saw | honest? |
|---|---|---|---|---|---|---|
| 1 | one path | `<input type="file">` | `{"uploaded": ["C:\\...\\a.txt"], "count": 1}` | `["a.txt"]`, 1 | `input:true`, `change:true` | yes, but see below |
| 2 | **two paths** | `<input type="file">` (no `multiple`) | `{"uploaded": [a, b], "count": 2}` | **`["a.txt"]`, 1** | `input:true`, `change:true` | **no** |
| 3 | two paths | `<input type="file" multiple>` | `{"uploaded": [a, b], "count": 2}` | `["a.txt", "b.txt"]`, 2 | `input:true`, `change:true` | yes |
| 4 | one path | `<input type="file" disabled>` | `{"uploaded": [a], "count": 1}` | `["a.txt"]`, 1 | `input:true`, `change:true` | yes — CDP attaches to a `disabled` input, which a user could not (§6) |
| 5 | one `.txt` | `<input type="file" accept=".png">` | `{"uploaded": [a], "count": 1}` | `["a.txt"]`, 1 | `input:true`, `change:true` | yes — `accept` is a picker filter, not a constraint (§6) |
| 6 | a path that does not exist | `<input type="file">` | **raises** `File not found: …` | unchanged | *(nothing)* | yes |
| 7 | one path | `<input type="text">` | **raises** `Selector '#not-file' is an <input type="text">, not type="file".` | — | *(nothing)* | yes |
| 8 | one path | `<input>` with **no `type` attribute** | **raises** `Node is not a file input element [code: -32000]` | — | *(nothing)* | yes — but that is *Chrome's* refusal, not the guard's: `element.attrs` is `{'id': 'no-type'}`, so `input_type` is `""` and the guard passes |
| 9 | one path | `<div>` | **raises** `resolved to <div>, not a file input.` | — | *(nothing)* | yes |
| 10 | zero paths | `<input type="file">` | **raises** `No file paths provided` | unchanged | *(nothing)* | yes |
| 11 | one path | `#nope` | **raises** `File input not found: #nope` | — | *(nothing)* | yes |

`upload_file` is in much better shape than `select_option`, and this finding says so
with the numbers: **ten of eleven** measured cases are honest, and every
"resolved to the wrong thing" case already raises. The defect is narrower and it is
exactly two things:

* **Row 2 is a false count.** `DOM.setFileInputFiles` with two files on an input
  that has no `multiple` attribute **succeeds** — the raw CDP call returns `None`,
  no error, no exception anywhere — and Chrome keeps only the first file. The tool
  reports `count: 2`. Driven again over an input that already held a file
  (sequential probe), Chrome keeps the **previous** file and fires no event at all,
  and the tool still reports `count: 2`.
* **Nothing is ever read back**, structurally. `{"uploaded": resolved, "count":
  len(resolved)}` is built entirely from the argument list, before the CDP call and
  regardless of it; it is the same sentence F-873 and F-876 wrote about `True`.
  There is no measurement here that can turn row 2 into a lie *and* leave rows 1
  and 3 honest, because none of the three asked the page anything — row 2 is simply
  the case where the unasked question would have had a different answer.

There is a third, non-behavioural problem in the same payload: `uploaded` carries
**absolute local paths** (`C:\Users\amind\AppData\Local\Temp\f877-…\a.txt`), which
name the operating user, into a value that travels to the MCP client. That is the
discipline F-869 names for a page's localStorage and F-876 names for an overlay's
text, one payload over.

---

## 3. Root cause

One sentence, and it is F-873's, third and fourth time: **the tool reports the
success of its own dispatch, not the success of the interaction.**

Concretely, in `dom_handler.py`:

* `select_option`'s three arms each end in a bare `return True` placed immediately
  after the thing they dispatched. The `value` arm returns after assigning
  `select.value`; the `index` arm returns after evaluating a script **whose entire
  body is inside an `if` that may not have run**; the `text` arm returns after
  `send_keys`, which is a keystroke dispatch whose consumer is a browser feature
  the tool does not model and cannot observe.
* There is no check that the resolved element is a `<select>`, so rows 13 and 14
  are not even about selects.
* `upload_file` composes its answer from `resolved` — the list it built from the
  caller's own argument — and never reads `input.files`.

The shape is identical to the two siblings, and so is the fix.

---

## 4. Fix

A new leaf, `embedded/control_state.py`, owns **"what does this form control hold
now, and did it take what was asked"** for both controls. `dom_handler.py` keeps
only the ORDER, exactly as it does for `type_text` / `paste_text` / `click_element`
after F-873 and F-876. The leaf imports `tool_errors` only, takes the element as an
argument, and every read is **one `JSON.stringify` round trip** (`Element.apply`
deep-serializes an object at every depth — F-872's rule, F-869's mechanism).

### 4a. `select_option`: resolve the option in one place, then read what the control holds

`SELECT_JS` is one `Element.apply` that, in a single synchronous block:

1. refuses anything that is not a `<select>` (rows 13 and 14 become a raise, not a
   write into someone else's control);
2. resolves the caller's criterion to a **target index** — the ONE matching rule,
   spelled out rather than delegated to a browser feature:
   * `index=` — the integer, if it is in range;
   * `value=` — the first `<option>` whose `value` is exactly that string;
   * `text=` — exact match on `option.text`, then exact match on `option.label`,
     then a case-insensitive **prefix** match on either, skipping `disabled`
     options. The prefix tier and the case-insensitivity are there because §2b
     measured them to be what the shipped arm did when it worked, and a fix that
     dropped them would break a caller who relies on `text="Bet"`. What is gone is
     the buffer, the wraparound, the `label`/text collision and the ability to
     type into a different element entirely;
3. if nothing resolved, returns `matched: false` **having changed nothing** (row 4's
   destroyed selection cannot happen: no assignment is reached);
4. otherwise sets `selectedIndex`, then dispatches `input` and then `change`, both
   bubbling — the pair, and the order, Chrome's own typeahead produced in §2b
   (`input:true` then `change:true`). The shipped `value`/`index` arms fired
   `change` alone and no `input` at all;
5. reads the control back **after** those events have run (they are synchronous, so
   a page handler that resets the select has already run) and returns
   `{matched, target_index, before, after, tag, multiple, option_count, …}` as a
   JSON string.

`control_state.verify_selected` is the verdict and the only thing that raises: no
match, or a match whose `selectedIndex` is not where it was aimed. `select_option`
returns a record:

```json
{
  "selector": "#country",
  "by": "text",
  "selected_index": 2,
  "selected_count": 1,
  "option_count": 12,
  "multiple": false,
  "changed": true
}
```

No option **text** and no option **value** is in it, and none is in any message the
leaf raises — a `<select>` is frequently a list of account numbers, and a raised
`ToolError` reaches the caller, the debug ring and Sentry at once (F-873's
discipline, unchanged). Indices and counts carry the whole of what a caller needs
to check, and `changed` distinguishes "it was already on that option" from "it
moved", which a bare `True` never could.

### 4b. `upload_file`: read `input.files`

`FILES_JS` is one `Element.apply` returning `{count, names, total_bytes}` read from
`input.files` **after** `send_file`. `control_state.verify_attached` raises when the
input holds a different number of files than were requested — which is row 2, and
which also covers "the input holds nothing" as its zero case. The record:

```json
{
  "selector": "#avatar",
  "requested": 2,
  "attached": 2,
  "multiple": true,
  "total_bytes": 3
}
```

`uploaded` — the absolute-path echo — is **gone**, and no path or file name appears
in any message the leaf raises. The caller supplied the paths; what it did not know
is how many of them the input took.

### 4c. The return-shape changes

Both are HARD golden regenerations of `tests/goldens/tool_surface.json`
(`PYTHONUTF8=1 python tools/dump_tool_surface.py --write`), done deliberately, in
this PR, with this justification:

| tool | was | now |
|---|---|---|
| `select_option` | `bool` — `True if selected successfully.` | the object above; the description says what the fields are and that a failed selection raises |
| `upload_file` | `{"uploaded": [absolute paths], "count": int}` | `{"selector", "requested", "attached", "multiple", "total_bytes"}` |

Neither record carries a `"selected": true` / `"uploaded": true` flag, for F-876
§4c's reason: it would be redundant with the counts, a failure raises, and a
redundant field is a second way to ask the same question.

---

## 5. Verification

* `tests/test_select_upload_verification.py` — hermetic pins (`FakeTab` plus the
  new `FakeSelect` and `FakeFileInput` in `tests/fakes.py`, both modelled on
  `FakeTextField`/`FakeClickTarget`: the answer is COMPUTED from the double's own
  state, never supplied by a test, so no fixture here can encode the bug).
* `tests/test_e2e_select_upload_verification.py` — real-Chrome pins on their own
  `tmp_empty_root` session root, marked exactly like the F-873/F-876 siblings.
* Counts and the RED→GREEN transition are in §5b.

### 5a. Characterization pins deliberately flipped

| test | was | now | why |
|---|---|---|---|
| `tests/goldens/tool_surface.json` (`select_option`, `upload_file`) | see §4c | see §4c | the HARD wire-surface golden, regenerated deliberately per `CONTRIBUTING.md`: both tools' return type and docstring changed on purpose, in this PR |
| `tests/test_dom_handler.py::test_select_option_*` | `assert result is True` | asserts the record | return-shape change; the claim each test makes (which arm ran, which JS was sent) is untouched |
| `tests/test_e2e_interaction_fidelity.py` (`select_option` arms) | `is True` | the record's `selected_index` | the faithful translation of the old claim, plus the one the old one could not make |
| `tests/fixture_app/interactions.html` + `app.js` | — | the F-877 block: a `disabled` select, an empty select, a `multiple` select, a select with a `disabled` option, and four file inputs (plain, `multiple`, `disabled`, and an `<input>` with no `type`) | every one is a shape §2a/§2c measured. No existing test touches any of them |

### 5b. Numbers

Recorded at the fix commit — see the PR body.

---

## 6. Not claimed / deliberately unchanged / what remains

* **A `multiple` `<select>` still cannot be driven to more than one selection.**
  The tool's signature takes exactly one `value` / `text` / `index`, so "the value
  list is partially valid" is not a state this API can be asked for at all.
  Measured (§2a row 15): `select.value = "two"` on a `multiple` select **replaces**
  the whole selection with that one option. The fix reports the resulting
  `selected_count` truthfully — which is how a caller now discovers the limit — and
  deliberately does not add a list parameter: that is a new capability, not a
  correction, and it belongs to whoever wants it with its own measurement.
* **A `disabled` `<option>` is still selectable through `value=`/`index=` and still
  unreachable through `text=`.** Measured, rows 12 and 16: the shipped `value` arm
  selects it (Chrome permits the assignment), the typeahead the `text` arm rode
  refuses it. The fix preserves both, because both are what the browser does and
  because unifying them would be this tool deciding something Chrome already
  decides (F-876's reasoning for the `disabled` click, verbatim). The asymmetry is
  named here rather than left to be discovered.
* **A `disabled` `<select>` now raises instead of silently typing somewhere else,
  but it is the read-back that raises, not a pre-check.** There is no
  "is this control enabled" guard added: the fix resolves the option, sets it, and
  reports that the control did not move. A `<select disabled>` whose `disabled` is
  removed by a script between the resolve and the set therefore still succeeds,
  which is the correct outcome and the one a pre-check would have broken.
* **`upload_file` still attaches to a `disabled` input, and still ignores
  `accept=`.** Measured, rows 4 and 5: `DOM.setFileInputFiles` attaches in both
  cases and Chrome fires a trusted `change`. A user could do neither. The tool does
  not refuse, for the same reason as above — CDP is the mechanism the tool is built
  on and this is what it does — and the record now says truthfully how many files
  the input holds, which is the fact a caller was missing.
* **`upload_file`'s type guard still cannot see an `<input>` with no `type`
  attribute.** Measured, row 8: `element.attrs` is `{'id': 'no-type'}`, so
  `input_type` is `""` and the guard passes; Chrome refuses the CDP call and the
  tool raises with Chrome's own wording (`Node is not a file input element [code:
  -32000]`). The outcome is correct and the message is diagnostic, so this PR does
  not widen the guard to consult the live DOM — that would be a second round trip
  to re-decide something already decided, and its only effect would be the wording.
  Named because a future reader will otherwise re-find it.
* **There is still no "did the page react" oracle**, and this finding does not want
  one — F-876 §6, unchanged. What `select_option` now asserts is stronger than
  F-873's "did anything change", because a `<select>` has a bounded, exact oracle
  (the resolved target index either is selected afterwards or is not) that a text
  field does not. That is why `select_option` raises for "already on the requested
  option **and** the requested option is not what it holds" and returns
  `changed: false` — not a failure — when the control was simply already there.
* **`type_text`'s tool-wrapper docstring still says `bool: True if typed
  successfully.`** F-876 §6 named this one-line truthfulness fix as a follow-up and
  this PR, changing two *other* tools' goldens, deliberately does not widen into it
  a second time. It remains open.
* **`select_option`'s new `text=` matching rule is a rule, and a rule can be wrong
  for someone.** It is three tiers (exact `text`, exact `label`, case-insensitive
  prefix over both, `disabled` options skipped) and it is chosen to keep every
  §2b case that worked working. A caller who was relying on Chrome's wraparound —
  asking for a prefix that matches the *currently selected* option in order to
  advance to the *next* matching one — will now get the current option and
  `changed: false` rather than a move. That is a deliberate loss of a behaviour no
  documentation ever claimed, and it is named rather than hidden.
* **Rows 9 and 13 are fixed by construction, not by verification.** The leaf never
  sends a keystroke and refuses a non-`<select>` before assigning anything, so the
  "typed into a different control" and "wrote into a text box" outcomes are not
  reachable to be verified. Their pins assert the raise, not a read-back.
