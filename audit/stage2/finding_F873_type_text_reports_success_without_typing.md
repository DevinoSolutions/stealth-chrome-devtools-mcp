# F-873 — `type_text` reports success for text it never entered, and for an Enter that cannot submit

**Status:** FIXED in this PR (product defect; live on 2.1.6 and on `main` at `bb78878`)
**Opened by:** live use on 2.1.6 over real stdio transport, headed Chrome 152 on Windows 11, 2026-09-15
**Source at:** `origin/main` = `bb78878`
**Severity:** HIGH. `type_text` is one of the twelve interaction tools and the only one that enters text. It returned `{"result": true}` in every failing case below, so a caller — an agent — proceeded as though the page had the input. Nothing raised, nothing logged, and every existing test was green over it: two of them, in `tests/test_e2e_interaction_fidelity.py`, explicitly PINNED the tool answering `True` while the page took nothing.

---

## 1. What was observed

Four measurements on the live 2.1.6 backend, real stdio transport, headed Chrome 152:

1. **Controlled page.** `data:text/html,<form id="f" onsubmit="location.hash='submitted:'+document.getElementById('q').value;return false"><input id="q" name="q"><button type="submit">go</button></form>` — `type_text(selector="#q", text="abc\n", parse_newlines=True)` → returned `true`; afterwards `#q` holds `"abc"` and `location.hash` is `""`. The characters landed; the trailing newline did **not** act as an Enter. A real Enter in a single-input form performs implicit submission.
2. **Amazon.com homepage.** `type_text(selector="#twotabsearchtextbox", text="usb c hub")`, with and without `clear_first`, before and after `click_element` on the same selector → returned `true`; afterwards `.value` is `""` and `document.activeElement.id == "twotabsearchtextbox"`. Repeated 3×.
3. **Gmail inbox** (signed-in profile). `type_text(selector='input[name="q"]', text="invoice")` → returned `true`; `.value` stays `""`.
4. **YouTube homepage.** `type_text(selector='input[name="search_query"]', text="lofi hip hop radio")` → returned `true`; `.value` is `"lofi hip hop radio"`. Works. A plain `<input>` on the `data:` page works too.

Two distinct defects share one shape — **reported success, changed nothing**:

* **A** — `parse_newlines`' Enter cannot submit a form.
* **B** — the tool never asks the page whether the characters landed, so *any* refusal reads as success.

---

## 2. Measurement

All measurements below: standalone probes driving plain `nodriver` 0.47 against a real Chrome (`Chrome/152.0.7977.83` — the same build as the live session), each on its own throwaway `--user-data-dir`, never the real `~/.stealth-mcp` and never a live process. Detector for a submit is a `submit` listener incrementing a counter, not `location.hash` (a `data:` document is an opaque origin and a hash assignment there is not a reliable witness).

### 2a. Defect A — which Enter shape submits

One-input form + submit button + `submit` listener; text entered first, then one Enter:

| Enter shape | events the page saw | submits |
|---|---|---|
| `element.apply(… new KeyboardEvent('keydown',{key:'Enter'}) …)` — **what shipped** | `keydown:Enter:0:false` (untrusted, `charCode` 0) | **0** |
| `Input.dispatchKeyEvent("rawKeyDown", key=Enter, vk=13)` + `keyUp` | `keydown:Enter:13:true` | **0** |
| `Input.dispatchKeyEvent("keyDown", key=Enter, text="\r", vk=13)` + `keyUp` | `keydown:Enter:13:true`, `keypress:Enter:13:true` | **1** |
| …plus a separate `char` event carrying `"\r"` | `keydown`, `keypress`, **`keypress` again** | **2** |

So: implicit submission is performed on the **keypress**, a trusted `keydown` alone does not produce one, and `keyDown` carrying `text` is both necessary and sufficient. A separate `char` event on top submits the form **twice**.

Shift+Enter, same form: **still 1 submit** — Blink's implicit submission does not consult the modifier. In a `<textarea>`, plain and shifted Enter both insert `"\n"` and submit 0 times. The modifier's only real job is that a chat app's `event.shiftKey` branch becomes reachable.

### 2b. Defect B — what the dispatch does and does not reach

`nodriver`'s `Element.send_keys` (`core/element.py:708`) is:

```python
await self.apply("(elem) => elem.focus()")
[await self._tab.send(cdp.input_.dispatch_key_event("char", text=char)) for char in list(text)]
```

A lone `char` event **commits** the character — `keypress` and `input` fire, trusted — but `keydown` and `keyup` never do. Measured against the fixture page's key probe, and already PINNED as a finding by `tests/test_e2e_interaction_fidelity.py` before this work began.

Controls that take every key event and move nothing (`elem.value=''` first, then type; `before`/`after` are the element's own `.value`):

| control | typed | before | after | changed |
|---|---|---|---|---|
| `<input readonly>` | `INJECT` | `""` | `""` | no |
| `<input type=range>` | `80` | `"50"` | `"50"` | no |
| `<input type=date>` | `2024-01-02` | `""` | `""` | no |
| `<input type=color>` | `#123456` | `"#000000"` | `"#000000"` | no |
| `<input type=number>` | `42` | `""` | `"42"` | **yes** |

Identical under the old `char`-only dispatch and the new full lifecycle. `type_text` answered `True` for the first four.

A literal `"\n"` **character** (i.e. `parse_newlines=False`) is dropped by Chrome in both a single-line input and a `<textarea>` — `"a\nb"` lands as `"ab"` and submits nothing, under both dispatches. The only thing that can produce a newline or a submit is a real Enter key press. Behaviour here is unchanged by the fix.

i18n round trip into an `<input>`, `"héllo 日本語 👍🏽 ß"`: byte-exact under `char`-only, under the full lifecycle (with `windowsVirtualKeyCode` 0 for every non-ASCII character) and under `Input.insertText`. Into a `contenteditable` div, both dispatches insert and `.value` is `undefined`, which is why the read-back reads `textContent` there.

### 2bb. The clear fallback was a no-op that made things worse

`type_text`'s `clear_first` fallback (taken when the programmatic `elem.value = ''` throws) sent WebDriver's private-use codepoints through `send_keys`. Measured against an `<input value="preset-value">`, focused:

| clear | value afterwards |
|---|---|
| old fallback — `send_keys("a")` then `send_keys("")` | `"apreset-value"` |
| `text_entry.clear_via_keyboard` (Ctrl+A, Delete over CDP) | `""`, and a subsequent `type_characters(…, "new")` gives `"new"` |

CDP has never spoken the WebDriver protocol, so those codepoints were dispatched as literal `char` text: the fallback prepended three junk characters and cleared nothing. `paste_text` already had a correct CDP version of the same thing inline; both call the one function now.

### 2c. What defect B's site-specific trigger is NOT

The Amazon/Gmail runs above are the same *class* as the table in §2b — every event delivered, nothing moved — but their particular trigger did **not** reproduce here. Ruled out by measurement, all on Chrome 152:

| hypothesis | measurement | result |
|---|---|---|
| headless vs headed | `char`-only into `#twotabsearchtextbox` on the real amazon.com, headless | lands `"usb c hub"` |
| the product's own stealth spawn is the difference | `DOMHandler.type_text` (product code) into amazon.com + youtube.com, **headed** | both land |
| the tab was not the active tab | background tab (a second tab activated over it), `document.hasFocus() == false` | lands |
| the window was not the OS-focused window | `Browser.setWindowBounds(windowState="minimized")`, `hasFocus == false`, `visibilityState == "hidden"` | lands |
| the page's script cancels the key | amazon.com and youtube.com both took `char`, `keyDown(text)` and `insertText` alike | no cancel observed |

So the Amazon/Gmail trigger remains **unreproduced** and is recorded as such (§6). What this finding fixes is not that trigger: it is that **the trigger does not matter**, because the tool now checks. The four rows of §2b are a deterministic, in-repo reproduction of the same silent lie, and they are what the new pins assert.

---

## 3. Root cause

**A.** `dom_handler.type_text`'s `parse_newlines` branch built the Enter **inside the page**: `element.apply("(elem) => { … elem.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', …})); … }")`, having first spliced `"\n"` into `elem.value` by hand. An event a script constructs is `isTrusted: false` and carries no `charCode`, so no `keypress` follows and Blink performs no implicit submission. The hand-spliced `"\n"` was then sanitised away by any single-line `<input>` — which is why measurement 1 read back `"abc"` and not `"abc\n"`, and why the defect looked like "the newline was eaten" rather than "the Enter was never pressed".

**B.** Nothing between "dispatch the events" and `return True` asked the page anything. `type_text`'s only failure branch was `resolve_element` returning nothing; a resolved element that then refused every character reached `return True` unconditionally.

Both are the same root cause one level up: **the tool reported the success of its own dispatch, not the success of the interaction.**

---

## 4. Fix

New leaf `embedded/text_entry.py` — **THE one home for pressing a key in a page, and for proving the text landed**:

* `press_key` — the ONE way a key reaches the page. Exactly two events: a `keyDown` carrying `text` (so Chrome synthesises the keypress) and a `keyUp` that does not. Never a third, because §2a measured a third as a second submit.
* `press_enter(tab, shift=…)` — Enter as `text="\r"`, `key`/`code` `"Enter"`, `windowsVirtualKeyCode` 13, Shift as modifier bit 8.
* `type_characters` — per character, full lifecycle, re-focusing the element before each one as the shipped path did.
* `clear_via_keyboard` — the ONE select-all + Delete. `type_text`'s fallback used to send WebDriver's private-use codepoints (U+E009 for Ctrl, U+E017 for Delete) through `send_keys`, which dispatches them as literal `char` text: CDP has never spoken that protocol, so the fallback inserted two junk characters and cleared nothing. `paste_text` already had a correct CDP version inline; both now call the one function, and the inline copy is deleted.
* `READ_JS` + `entered_text` — the ONE read-back, answering with a JSON **string** because `Element.apply` returns `result[0].value` and a script that threw lands there as `None`. A non-`str` answer is "could not be read" and raises; it is never mistaken for an empty field. `contentEditable` is read from `textContent`, which is the only thing such an element has.
* `verify_received` — raises unless the element's text moved.

`dom_handler.type_text` keeps only the ORDER, and it is load-bearing: a line's characters are verified **before** that line's Enter, never after, because an Enter that submits may navigate and a read against the detached element would report a failure the page had in fact accepted. An empty line is skipped entirely, which is what keeps the common `"query\n"` — type, submit, done — from reading back across its own navigation.

`dom_handler.py` 960 → **779 LOC**; `text_entry.py` is 204. No `GRANDFATHER` row is involved: both are under the 1000-LOC default.

### 4a. Why the check is "did anything change" and not "does it contain what I typed"

An input mask, an autocomplete that rewrites, a `number` field that normalises, a page that upper-cases — all of those DID receive the input and would fail a `typed in observed` test. A stricter check would turn each into a new false alarm, which is the same defect wearing the opposite sign. "Nothing moved" is exactly the failure that shipped, and it catches all five measured shapes (the four in §2b plus the Amazon/Gmail `"" → ""`).

### 4b. Why no message carries the typed text

A failed tool call reaches the durable log, the debug ring and Sentry at once (F-835), and the field that refused may be a password box. Every message here reports shape and count only — the selector, how many characters were typed, how many the element holds. Same discipline and the same reason as F-869's storage reader.

---

## 5. Verification

* `tests/test_type_text_verification.py` — 12 hermetic pins (9 RED before the fix, the rest positive controls and the clear pin): the Enter is a real `Input.dispatchKeyEvent`; its `keyDown` carries `text="\r"` + `code` + vk 13; it is dispatched exactly twice (`keyDown`, `keyUp`) and never a third time; `shift_enter` carries modifier 8; each character gets a `keyDown`+`keyUp` pair; a refusing control raises with the selector and the count; the message never carries the typed text; an unreadable read-back raises; a control that accepts still returns `True`; `clear_first`'s baseline is the state AFTER the clear; the clear fallback is Ctrl+A then Delete; empty text is not a failure.
* `tests/test_e2e_type_text_verification.py` — 9 real-Chrome pins (6 RED before the fix), own `tmp_empty_root` session root: `#enter-form` (one field, no submit button) is submitted by `parse_newlines`; keydown AND keyup fire, all trusted; `readonly`/`range`/`date`/`color` each raise; `number`, the key probe and an i18n string still succeed with the value to prove it.
* `tests/fakes.py` gains `FakeTextField`, a page-backed element double whose `send_keys` is nodriver 0.47's `Element.send_keys` reproduced verbatim — the char-only dispatch IS the thing under test, so a stub that merely appended the text would have made the pins pass against the very dispatch they exist to reject. `FakeTab` routes a key event carrying `text` to the focused field per EVENT, so a double dispatch shows up there exactly as it double-submits in Chrome.
* Unit lane at the fix: **2496 passed, 1 skipped** (2484 before this work + the 12 new hermetic pins). FULL integration lane (`-m integration`, real Chrome, 189 nodes): first run **1 failed, 185 passed, 1 skipped, 2 xfailed** — the single failure was the fifth flip in §5a, which the fix was expected to move; confirmation run after flipping it, **186 passed, 1 skipped, 2 xfailed, 0 failed** (962 s).
* §1 measurement 1 re-run against the fix, product code path (`DOMHandler.type_text`), real headless Chrome, same `data:` form page:

```
measurement_1_form: {"returned": true, "value": "abc", "submitted": "submitted:abc"}
refusal_is_reported: RAISED ToolError: Failed to type text: typed 9 character(s)
  into '#ro' but the element's text did not change (still 0 character(s)) — the
  page did not accept the input. The control may be read-only or disabled, a
  non-text input type (range/date/color cannot be typed into), or governed by a
  script that cancels key events.
```

  The form submits; a refusal names the selector and the counts and carries none of the typed text.

### 5a. Characterization pins deliberately flipped (SOFT goldens)

| test | was | now | why |
|---|---|---|---|
| `test_e2e_interaction_fidelity.py::test_keyboard_fidelity_and_enter_submit` | asserted NO `key:down:`/`key:up:` and `"submit:enter-form" not in actions` | asserts the full trusted lifecycle and that the form IS submitted | both were pinned as FINDINGS with the src line numbers that caused them; this is the fix those comments anticipated |
| `test_e2e_interaction_fidelity.py::test_form_semantics` (readonly arm) | `assert await type_text(#readonly-input, "INJECT")` | `pytest.raises(ToolError)` | the pin's own comment called the silent success a FINDING |
| `test_e2e_interaction_fidelity.py::test_rich_input_types` (range/date/color arms) | `assert await type_text(...)` then "value unchanged" | `pytest.raises(ToolError)` | same; `number` is unchanged and is the positive control |
| `test_silent_excepts_log.py::test_type_text_clear_fallback_logs_at_debug` | `element.send_keys` was the clear fallback seam | `tab.send` is | the fallback moved to the one CDP `clear_via_keyboard`; the DEBUG log line it asserts is unchanged |
| `test_stateful_i18n.py::test_the_real_input_tools_emit_no_composition_at_all` | `["beforeinput:a", "input:a", "beforeinput:b", "input:b"]` | a `keydown:`/`keyup:` pair now wraps each character's `beforeinput`/`input` | the page logs key events too and the char-only dispatch produced none; the test's actual claim — that NEITHER input tool produces a composition event — is untouched, and `paste_text`'s one `beforeinput:ab`/`input:ab` is unchanged |

---

## 6. Not claimed / deliberately unchanged / what remains

* **The Amazon/Gmail trigger is not identified.** §2c lists what was ruled out by measurement. What is claimed is narrower and provable: those runs are in the class the fix converts from a silent `True` into a named `ToolError`, and a caller will now be told. If the trigger recurs it will arrive as an error naming the selector rather than as a false success — which is itself the diagnostic that was missing.
* **`paste_text` is not verified.** It inserts via `Input.insertText` and returns `True` on the same unchecked basis. It shares `clear_via_keyboard` now but not the read-back. Deliberately out of scope for one PR; the mechanism (`entered_text` + `verify_received`) is already a leaf that takes an element, so joining it is a small follow-up, not a redesign.
* **`click_element` is not verified either** and has the same shape: `tests/test_e2e_interaction_fidelity.py::test_form_semantics` still pins `click_element` on a `disabled` control returning `True` with no click dispatched. That pin is left alone — it is a different question (did the browser act on a coordinate) with a different check.
* **The `"did anything change"` boundary** (§4a) means a control that accepts only part of what was typed still answers `True`. Named here rather than silently narrowed.
* **Typing at a page rather than into a field now raises.** A caller using `type_text` to fire a page-level keyboard shortcut (`"/"` to focus a search box, `"j"`/`"k"` navigation) aimed at a container that holds no text will get a `ToolError` where it used to get `True`. That is the correct answer for a tool whose one job is entering text — the message names the selector and says nothing landed — but it IS a behaviour change for that use, and a `press_key` tool is the right home for it if it is wanted. `text_entry.press_key` is already the leaf such a tool would call.
* **`shift_enter` still cannot stop a form submitting**, because Blink does not consult the modifier (§2a). The parameter does what it can do and what it is for: make the page's `shiftKey` branch reachable.
