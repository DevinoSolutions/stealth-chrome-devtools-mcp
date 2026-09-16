# F-883 — `execute_script` never awaits: `await` is a SyntaxError, a Promise is `{}`, a rejection is a success

**Status:** FIXED in this PR (product defect; live on 2.1.8 and on `main` at `6ca0ae9`)
**Opened by:** a live session driving the shipped MCP (Chrome 152, nodriver 0.47,
Windows 11, 2026-09-16).
**Source at:** `origin/main` = `6ca0ae9` (= release 2.1.8)
**Severity:** HIGH. `execute_script` is the default exec-family tool and the one
an agent reaches for when no other tool fits. Two of its three failure shapes are
*silent*: a returned Promise and a **rejected** Promise both answer
`{"success": true, "result": {}, "error": null}`, which a caller cannot tell from
a script that genuinely returned `{}`. The third is loud but self-contradictory:
the docstring forbids every blocking wait (`NEVER use synchronous XHR … Use
`await fetch(url)`) and the tool then rejects `await` as a SyntaxError — so the
only non-blocking wait the tool documents is the one it refuses to run.

---

## 1. What was observed

Two calls through the shipped MCP, against 2.1.8:

```
execute_script(script="const v = await new Promise(r => setTimeout(() => r(42), 100));
                       return {awaited: v, title: document.title};")
→ ToolError: Script raised an exception: SyntaxError: await is only valid in
  async functions and the top level bodies of modules
```

```
execute_script(script="return fetch('data:text/plain,hello').then(r => r.text())
                              .then(t => ({fetched: t}));")
→ {"success": true, "result": {}, "error": null}
```

The second is the dangerous one. Nothing in the answer says a Promise was
involved; `{}` is a legitimate value for a script to return, so a caller reading
`success: true` has no way to know the data never arrived.

---

## 2. Mechanism (measured, Chrome 152 headless, nodriver 0.47.0, Windows 11)

### 2a. The one send, and the flag that was missing

`DOMHandler.execute_script` → `_evaluate_by_value` sent exactly one command:

```python
cdp.runtime.evaluate(
    expression=expression,
    return_by_value=True,
    user_gesture=True,
    allow_unsafe_eval_blocked_by_csp=True,
)
```

`awaitPromise` defaults to **false**. Chrome therefore answers with the Promise
**object**, and `returnByValue` serializes it by its own enumerable properties —
of which a `Promise` has none. `{}` is not a truncation and not an envelope
(F-832's shape): it is a faithful by-value serialization of an object that
carries nothing. The same is true of `document.body` and of a plain `function`,
which is why the three are indistinguishable at the tool boundary:

| script (2.1.8) | answer |
|---|---|
| `return fetch(u).then(r => r.text());` | `{}` |
| `Promise.resolve({ok: 1})` | `{}` |
| `return Promise.reject(new Error('boom-reason'));` | `{}`, `success: true` |
| `return document.body;` | `{}` |
| `return function f(){};` | `{}` |

A rejection is worse than a lost value: `awaitPromise: false` means Chrome never
looks at the settlement, so there is no `exceptionDetails` for F-795's reader to
refuse, and the tool reports a failure as a success.

### 2b. The retry wrapper was not async

F-812 already decided that a top-level `return` is not a defect: the source is
evaluated as written, and *only* if Chrome names `Illegal return statement` is it
re-evaluated once as a function body. The wrapper it used was
`(() => {\n…\n})()` — an ordinary arrow function. So a script with a top-level
`await`:

* is not an expression → Chrome's complaint is the **await** SyntaxError, not the
  illegal-return one, so the retry never fires at all;
* and would have failed the same way inside a non-async wrapper even if it had.

The two complaints are the same *kind* of thing — a compile-time objection to how
we evaluated the source, not to the source — and only one of them was recognised.

### 2c. Why an unconditional async wrapper is not the fix

Wrapping every script changes what a script MEANS. Measured: `var installed = 1;
function f() {}` evaluated as written lands on the page (`window.f883_installed`
reads `1` from a later, separate call); inside a wrapper both become locals that
are thrown away. That is why F-812 chose a retry keyed on one error, and why
F-883 widens the *trigger* rather than removing it.

### 2d. What `awaitPromise` costs and does not cost

Measured after the fix, same browser, same page:

| case | 2.1.8 | fixed |
|---|---|---|
| `const v = await esDelayed(100, 42); return {awaited: v, title: document.title};` | `SyntaxError` | `{'awaited': 42, 'title': 'T'}` |
| `return fetch(u).then(r => r.text()).then(t => ({fetched: t}));` | `{}` | `{'fetched': 'hello'}` |
| `Promise.resolve({ok: 1})` (bare expression) | `{}` | `{'ok': 1}` |
| `return Promise.reject(new Error('boom-reason'));` | `{}`, success | `ToolError: … Error: boom-reason` |
| `Promise.reject(new Error('bare-boom'))` (bare expression) | `{}`, success | `ToolError: … Error: bare-boom` |
| `await 0; throw new Error('after-await');` | `SyntaxError` | `ToolError: … Error: after-await` (reason: `top-level 'await'`) |
| `throw new Error('sync-boom');` | `ToolError: … Error: sync-boom` | **unchanged** |
| `return {a: 1, b: [1, 2, {c: 'd'}], z: null};` | `{'a': 1, 'b': [1, 2, {'c': 'd'}], 'z': None}` | **unchanged** |
| `6 * 7` / `0` / `undefined` | `42` / `0` / `None` | **unchanged** |
| `return [[1,[2,[3,[4,[5,[6,[7,[8,[9,[10]]]]]]]]]]];` | whole | **unchanged** (no depth-10 cap — `returnByValue` is not deep serialization) |
| `return arguments[0] + arguments[1];` with `args=[3,4]` | `7` | **unchanged** |
| `return document.body;` / `return function f(){};` | `{}` | **unchanged** |
| `const o = {}; o.self = o; return o;` | `ToolError: Failed to execute script: Object reference chain is too long` | **unchanged** |
| `1/0` | `'Infinity'` | **unchanged** (`unserializableValue`) |
| `var f883_installed = 1; function f883f(){}` then `window.f883_installed` | `None` then `1` | **unchanged** |
| `return new Promise(() => {});` | returns `{}` **instantly** | bounded by `timeout_ms`, then `ToolError: CDP operation timed out` |

The last row is the one behaviour F-883 makes *slower*, and deliberately: a
Promise that never settles used to be answered instantly with a lie, and is now
answered at the caller's own deadline with the truth. The tab survives it — the
call immediately after the timeout returned `2` for `1+1` on the same tab.

### 2e. The same defect, reached through the other door

`cdp_function_executor` already passed `await_promise=True` on its sends, but its
two **wrappers** are where the caller's code runs and both were synchronous:

* `inject_and_execute_script` built `(function(){ … const result = (function(){
  <caller source> })(); return {success: true, result: result, …}; … })()`. The
  outer object is not a Promise, so `awaitPromise` has nothing to await, and an
  async caller script put the Promise OBJECT in `result` → `{}` again. A top-level
  `await` in `script_code` was a SyntaxError for the same reason as 2b.
* `call_discovered_function` (behind `call_javascript_function` **and**
  `execute_function_sequence`) did `const result = func.apply(context, args);` —
  so calling an `async` page function returned `{}` with `success: true`.

Both are fixed in their own home by making the wrapper `async` and awaiting the
inner call — no second evaluate path is added, and the sends already carried the
flag that makes it work.

---

## 3. Fix (this PR)

**A new leaf: `embedded/script_evaluation.py` — THE one home for "run a caller's
JS in the page and read its answer".** `dom_handler.py` was at 997 of its
1000-LOC budget and F-883 gives this seam a fourth decision to carry, so the seam
moved out whole (`json_value`, `script_value`, `evaluate`,
`as_async_function_body`, `run`, plus the two compile complaints). `dom_handler.py`
is 997 → 853 LOC; `DOMHandler.execute_script` is now a one-line delegation, so
every other DOM tool still reaches the page through the object it always did.
The module carries the arguments for all four findings welded into it (F-795,
F-812, F-832, F-883) so none can be re-derived from taste.

Three mechanisms, and no more:

1. **`await_promise=True` on THE one `Runtime.evaluate`.** A Promise becomes the
   value it resolves to; a rejection becomes `exceptionDetails`, which is the
   shape F-795's reader already refuses — so the silent success becomes a raise
   without a new error path.
2. **The wrapper is `async`, on both wrap paths.** The retry evaluates
   `(async () => {\n…\n})()`, and the `args` path — which has always wrapped
   immediately, because its body is already a function body — evaluates
   `(async function() { … })(args)`. Otherwise the tool would support `await` on
   one of its two call shapes.
3. **The retry fires for two compile complaints, not one.**
   `_FUNCTION_BODY_COMPLAINTS` pairs each with the phrase the message uses to name
   it, so a caller whose `await` was the trigger is not told about a `return` they
   did not write. Everything else keeps its own error and is still evaluated
   exactly once — F-812's narrowness is unchanged, which is what keeps a script
   with a side effect from applying it twice.

**`tool_errors._require_js_value` clamps the detail** (`JS_ERROR_CHARS = 200`,
`JS_ERROR_TRUNCATION = "…"`). It is THE one place a thrown script becomes the
error convention, and since this PR a *rejection reason* arrives there too:
`Promise.reject(new Error(<anything>))` is page-authored and unbounded, and the
message reaches the client, the debug ring (`log_tool_failure`) and Sentry at
once. The number is a third copy beside `js_aspect_answer.MAX_ERROR_CHARS` and
`page_storage.BLOCKED_REASON_CHARS` **on purpose**: `tool_errors` states three
times over that it imports nothing from `embedded` (it is why
`_require_landing_ok` takes its timeout as a parameter), and the three bounds
answer three different questions at three different homes.

**No new deadline.** `script_evaluation` deliberately does not pass CDP's own
`Runtime.evaluate` `timeout` as well: the bound is `tool_runtime._clamp_timeout`
+ `_with_cdp_timeout` at the tool body — the ONE home for that clamp — and a
second deadline is a second answer to "how long may a script run". A Promise that
never settles blocks the send exactly as `while(true)` blocks the renderer, and
both are killed by the same wrapper.

**The docstring now describes the tool.** It states that top-level `await` works,
that a returned Promise is awaited for you, that a rejection raises and is never
reported as a success, and that a script that never settles is killed at
`timeout_ms` — and a hermetic pin asserts those claims are in it, because a doc
claim nothing enforces drifts.

---

## 4. Verification

### Hermetic — `tests/test_execute_script_async.py` (21 nodes)

Pins the MECHANISM, because a `FakeTab` cannot resolve a Promise — only Chrome
can: `awaitPromise` rides on both sends; `userGesture` /
`allowUnsafeEvalBlockedByCSP` are not lost and `serializationOptions` is still
absent (F-832); both wrappers are `async`; a declaration still reaches the page
unwrapped; the retry fires for the await complaint and names it; an unrelated
failure is still evaluated exactly once; the reason is clamped and a short one is
not marked truncated.

The existing F-812 and F-832 pins (`tests/test_execute_script_return_wrap.py`,
`tests/test_execute_script_deep_values.py`) are unchanged except for the two
expressions the fix deliberately rewrote (`(async () => …)`,
`(async function() …)`), and `tests/test_error_typing.py`'s wrapped-retry node
now names the seam's new home.

### Real Chrome — `tests/test_e2e_execute_script_async.py` (7 nodes)

Against `/es_async.html`, appended at the END of `tests/fixture_routes.py` as
`es_*`: a deliberately inert local page whose only contribution is three Promises
the PAGE owns (`esDelayed` / `esRejects` / `esNever`) and one nested literal
(`esNested`). A value that arrives from `esDelayed` is proof the tool waited for
a resolution that did not exist when the script was sent — not proof that a
literal survived a round trip.

One node each: top-level `await`; a returned Promise's value; a rejected Promise
→ `ToolError` carrying its reason; a never-settling Promise → timeout inside a
window around `timeout_ms=1500` **and the tab still usable afterwards**; a sync
throw (unchanged); a nested object/array through both the direct and the awaited
path (same shape, falsy leaves included); and `args`, plain and awaited.

No fixed sleep is an oracle. The un-settling node reads a clock only to bound an
answer it already has, because `timeout_ms` is the thing under test.

### One SOFT golden, updated deliberately

`tests/goldens/tool_surface.json` changes by exactly ONE line: `execute_script`'s
`description`. That is the wire-visible half of the fix — the docstring is what an
agent reads before choosing the tool, and it told callers to use `await
fetch(url)` while the tool rejected it. The golden is regenerated in the same PR
that changes the schema it records (`PYTHONUTF8=1 python
tools/dump_tool_surface.py --write`), per CONTRIBUTING's two-tier rule. Nothing
else in the surface moves: no tool is added or removed, no parameter changes
name, type or default, and the count stays 94.

### Two existing pins moved, both deliberately

* `tests/test_execute_script_return_wrap.py::test_the_args_path_is_untouched` now
  expects `(async function() { return 1; })(7)`. What it protects — the args path
  wraps ONCE and never retries — is unchanged; only the wrapper's `async` keyword
  moved, and moving it is the fix.
* `tests/test_error_typing.py::test_function_body_retry_transport_failure_raises_tool_error`
  addresses `script_evaluation.as_async_function_body` instead of the deleted
  `DOMHandler._evaluate_as_function_body`. What it asserts — an operational
  failure of the WRAPPED attempt is a `ToolError` saying "Failed to execute
  script" — is byte-identical.

---

## 5. Blast radius (what a caller saw)

Every agent-authored script that touched the network, a timer, `Notification`,
`navigator.*`'s promise-returning APIs, IndexedDB, the Clipboard API, or any
`async` page function got `{}` and `success: true`. An agent reading that answer
concludes the page returned an empty object and moves on — typically by retrying
with a different selector, or by reporting to its user that the site has no data
where the data was simply never awaited. A rejected fetch — an auth failure, a
CORS refusal, a 500 — reported identically, so a failed call and an empty result
were the same string on the wire.

The workaround an agent had to find was to poll: run one script to start the
work and stash it on `window`, then a second to read the result back. That is two
round trips, a global on the page, and a race, in place of one `await`.

---

## 6. Not claimed / deliberately unchanged / what remains

1. **A rejection whose reason is a plain STRING loses its text.** Measured:
   `Promise.reject('plain-string-reason')` → `Script raised an exception: Uncaught
   (in promise)`. Chrome's `exceptionDetails.exception` for a primitive string
   carries `value`, not `description`, and `_require_js_value` reads
   `description` then falls back to `.text`. `Promise.reject(42)` DOES report
   `42` (a number's `description` is its text). Not fixed here: reading
   `exception.value` would put a page-authored string into an error message that
   reaches Sentry, and the F-869 discipline says a page's own strings ride only
   where the diagnostic requires it. The verdict — *it failed* — is correct in
   both cases; only the reason is thinner for one shape.

2. **A non-serialisable resolved value is still `{}`.** `return document.body`,
   `return function f(){}` and a Promise resolving to either answer `{}` exactly
   as they did on 2.1.8. That is `returnByValue`'s own behaviour and F-832's
   deliberate mapping, and it is untouched: this PR removes the case where a
   *Promise* was mistaken for such a value, not the case where the value really
   is unserialisable. A caller who needs a DOM node's data must ask for it as
   JSON, which is what every other tool in the tree already does.

3. **A cyclic object still raises the transport error.** `Object reference chain
   is too long [code: -32000]` comes from Chrome's serializer and is reported as
   operational (`Failed to execute script: …`), not as the script's own. Correct
   — the script did not throw — but the message names CDP rather than the cycle.

4. **The never-settling Promise is bounded, not cancelled in the page.** The
   `asyncio.wait_for` at the tool body cancels the *send*; the Promise itself
   stays pending in the page until the document goes away. That costs nothing
   measurable (a pending Promise holds no thread) and the tab is usable
   immediately afterwards — measured — but it is not the same thing as killing
   the work. CDP's `Runtime.evaluate` `timeout` would do that, and is deliberately
   not used: see §3's "no new deadline".

5. **`inject_and_execute_script` and `call_javascript_function` are fixed but not
   re-homed.** They keep their `{"success": …}` dict shape, which is
   `cdp_function_executor`'s long-standing KEEP contract and outside this
   finding's scope. Their wrappers are now `async` and their inner calls awaited;
   what they still do NOT do is distinguish a rejection from a thrown error in
   the shape they return — both land in the same `catch` and come back as
   `success: false` with `error.message`, which is that subsystem's convention,
   not `execute_script`'s.

6. **The blocking-pattern guard is unchanged.** `_BLOCKING_SCRIPT_PATTERNS` still
   rejects sync XHR, `while(true)`, `for(;;)` and the modal dialogs before any
   CDP is sent. F-883 makes the *recommended* alternative (`await fetch`) actually
   work; it does not loosen the guard, and a script that tries to block the
   renderer still costs zero round trips.

7. **`execute_python_in_browser` was not audited.** It translates Python to JS and
   runs it through its own path in `cdp_function_executor`; whether it shares this
   defect is not claimed either way here.
