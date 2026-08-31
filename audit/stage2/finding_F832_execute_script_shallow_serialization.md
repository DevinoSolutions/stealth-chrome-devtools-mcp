# F-832 — `execute_script` returned a CDP envelope instead of the script's value

**GitHub issue:** [#17 — "execute_script returns deep-serialized CDP output instead
of plain JSON values"](https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/issues/17)
(filed 2026-07-01, severity Medium, usability/ergonomics)

**Home:** `src/stealth_chrome_devtools_mcp/embedded/dom_handler.py`
(`DOMHandler.execute_script`) — the one eval home the `execute_script` tool body
in `embedded/server.py` delegates to.

**Status:** fixed on `fix/F832-deep-serialization`. Closes #17.

---

## The defect

`DOMHandler.execute_script` evaluated through `nodriver`'s `Tab.evaluate`, which
does two things this tool does not want.

**1. It asks Chrome for a deep-serialized result, not the value.** `Tab.evaluate`
hard-codes

```python
ser = cdp.runtime.SerializationOptions(
    serialization="deep",
    max_depth=10,
    additional_parameters={"maxNodeDepth": 10, "includeShadowTree": "all"},
)
```

and sends it on every `Runtime.evaluate` with `return_by_value=False`. A deep
serialization is a **BiDi-shaped graph** — every node is a `{"type": …, "value":
…}` record, an object's `value` is a list of `[key, node]` pairs — capped at
depth 10. So the caller of `execute_script("({a: {b: 1}})")` got a CDP envelope
to unwrap rather than `{"a": {"b": 1}}`, and anything past depth 10 was simply
gone. That is exactly what #17 reports.

**2. It reads the answer back with truthiness tests.** The tail of
`Tab.evaluate`:

```python
if remote_object:
    if return_by_value:
        if remote_object.value:                  # <-- falsy trap
            return remote_object.value
    else:
        if remote_object.deep_serialized_value:  # <-- falsy trap
            return remote_object.deep_serialized_value.value
return remote_object                             # <-- the husk
```

`if remote_object.value:` cannot distinguish *"there is no value"* from *"the
value is falsy"*. A script evaluating to `0`, `""`, `false` or `null` failed that
test and fell straight through to `return remote_object` — so the caller received
a bare `RemoteObject` **husk** in place of the number, string or boolean they
asked for. A husk is not JSON-serializable, which is how the same root cause also
reached `response_handler` as a crash (fixed separately and complementarily on
its own branch; this finding is about `execute_script` returning the right thing
in the first place).

Both defects are one root cause: **the value was inferred rather than read.**

## The fix

`execute_script` now evaluates through a raw `Runtime.evaluate` with
`return_by_value=True` (`DOMHandler._evaluate_by_value`) and reads the answer
with an explicit **None-vs-absent** check (`_json_value`) instead of a truthiness
one.

What the command sends, and why each part is deliberate:

| Parameter | Value | Why |
|---|---|---|
| `return_by_value` | `True` | the whole fix — Chrome JSON-serializes the result itself |
| `serialization_options` | **not sent** | CDP documents it as *overriding* `returnByValue`; sending both would quietly reinstate the envelope |
| `user_gesture` | `True` | carried over from nodriver's own call — dropping it regresses handlers gated on user activation |
| `allow_unsafe_eval_blocked_by_csp` | `True` | carried over — dropping it regresses every page with a strict CSP |
| `await_promise` | **not sent** (false) | unchanged from the old path; see residuals |

### The None-vs-absent mapping (the falsy-trap note)

`_json_value` never asks "is this truthy". It asks, in this branch order:

| Chrome's `RemoteObject` | `execute_script` returns |
|---|---|
| `type = "undefined"` | `None` |
| a **present** `value` (including `0`, `""`, `false`) | that value, **verbatim** |
| no `value`, `subtype = "null"` | `None`, by its own named branch |
| no `value`, an `unserializableValue` (`Infinity`/`NaN`/`-0`) | that token as a string |
| nothing serializable at all (a live DOM node, a cycle) | the `description` string |
| no result object at all | `None` |

Two notes on the choices:

* **`undefined` and `null` both become `None`.** Python has one nullish value and
  the tool's payload is JSON, so there is nowhere truthful to put the
  distinction; inventing a sentinel or a second payload key would be a schema
  change for a difference callers cannot act on. What matters — and what the
  tests pin — is that each is reached by its **own named branch**, and that
  *neither* is reached by the accidental "the value was falsy, so there must not
  be one" fallthrough that was the bug. A `0` and a `null` are now different
  answers, which is the part that was broken.
* **A `RemoteObject` is never returned.** The last two rows exist so the tool
  composing `{"success": True, "result": …}` cannot be handed something that is
  not JSON-serializable. This is the "guard against non-JSON-serializable CDP
  responses defensively" requirement, and it is a *fallback*, not a cap: no size
  limit is added here (see residuals).

### One error convention, one guard

The raw command hands `exceptionDetails` over explicitly instead of substituting
it for the value. `_script_value` consults it **first** — Chrome answers a throw
with *both* a result object (the thrown value) and the details, so reading the
result first would report the exception as the script's value and call it a
success, which is precisely the F-795 defect — and routes the record into
`tool_errors._require_js_value`. That guard stays the **one** place a thrown
script becomes the error convention; nothing here grows a second `hasattr` check
or a second message.

### F-812 preserved exactly

The single retry on Chrome's `Illegal return statement` is untouched: evaluate
verbatim, and only if that one error comes back, evaluate once more as
`(() => {…})()`. Both attempts now go through `_evaluate_by_value`, so the retry
— which is the path the most common agent-written script (`return document.title;`)
actually takes — is by-value too. `_evaluate_as_function_body` keeps its
signature and its "report the script's own error, not our strategy's" message.

## Tests

RED first, then green. Hermetic throughout — a `FakeTab` answering the real
`Runtime.evaluate` command with records built from `nodriver`'s own
`RemoteObject` / `ExceptionDetails` constructors. No browser, no `~/.stealth-mcp`.

`tests/test_execute_script_deep_values.py` (new, 16 tests): every one RED against
the unfixed `dom_handler` (17 failures on the first draft, before two overlapping
F-812 pins were folded into one), all green after.

* **(a) deep values** — a 4-level nested object and an array of objects come back
  whole; the command carries `returnByValue: true`, no `serializationOptions`,
  and still carries `userGesture` / `allowUnsafeEvalBlockedByCSP`.
* **(b) the falsy trap** — parametrized over `0`, `""`, `false`, `null`: each
  survives with its JSON *type* intact, and `0` reaches the tool payload as
  `{"success": true, "result": 0, "error": null}`.
* **(c) F-795** — a throwing script still raises `ToolError` carrying the JS
  error, evaluated exactly once; and the details win even when a result object
  came back alongside them.
* **(d) F-812** — the single retry still fires exactly once, first attempt
  verbatim, second wrapped, and by-value. The retry's *narrowness* stays pinned
  in its own home, `tests/test_execute_script_return_wrap.py`.
* **(e) undefined / null / husk** — `undefined` → `None`, `null` → `None`,
  `Infinity` → `"Infinity"`, an unserializable node → its `description`, an
  absent result → `None`.

Migrated / extended:

* `tests/test_execute_script_return_wrap.py` — same seven F-812 pins, moved onto
  the CDP seam (attempts counted off `tab.cdp_frames`, answers are
  `(result, exceptionDetails)` pairs). No pin was weakened or dropped.
* `tests/fakes.py` — `FakeTab.send` now routes `Runtime.evaluate` to the **same**
  canned answers `FakeTab.evaluate` uses (`_answer_for_js`), so a test says "this
  JS answers with X" once whichever seam the code under test takes; an explicit
  `cdp_responses["evaluate"]` still wins, so no existing test moved. Added
  `js_result()` / `js_threw()`, the two `Runtime.evaluate` answer builders. The
  strict `StopIteration`-only tolerance in `send` is unchanged.

Green: 100 tests across `test_execute_script_deep_values`,
`test_execute_script_return_wrap`, `test_error_typing`,
`test_execute_script_guard`, `test_tool_errors`, `test_dom_handler`; plus 388
across every other `fakes.py` consumer (the shared-harness blast radius).
`tools/check_file_budgets.py` clean.

## Budgets

`embedded/dom_handler.py` is **not** grandfathered, so its cap is the 1000-LOC
default. 873 → 964, i.e. 36 lines of headroom left. `embedded/server.py` is
untouched (3411/3411).

## Residuals (accepted, not fixed here)

1. **`execute_script`'s payload is uncapped.** The tool returns its dict directly
   rather than through `response_handler.handle_response`, and
   `MAX_USER_SCRIPT_BYTES` caps the *input* script only. `return_by_value=True`
   can therefore produce a larger payload than the depth-10 envelope did for a
   script that returns a big object. No second cap was added here on purpose —
   inventing one would be a parallel size policy next to `response_handler`'s.
   Routing `execute_script` through `handle_response` is the right fix and is a
   *schema* change (the `{"success", "result", "error"}` shape is pinned in
   `test_tool_errors.py` and on the wire), so it belongs in its own change.
2. **Top-level `await` + `return` still fails** with Chrome's `await` SyntaxError
   before the illegal-return retry can help. Known, unchanged, out of scope.
3. **A returned Promise is still not awaited.** `await_promise` stays false, as
   it was — turning it on would change what a script *means* (a pending promise
   would now hold the call open to the timeout) and is not part of this defect.
   A script returning a promise gets `{}` by value, same as before it got a husk.
4. **`_json_value`'s husk fallback is a description string, not structured data.**
   A caller who evaluates to a live DOM node gets `"div#app"` rather than an
   error. That is deliberate — it keeps the tool from crashing on a payload it
   cannot serialize — but it is a lossy answer, and a caller wanting the node
   should use the element tools.
