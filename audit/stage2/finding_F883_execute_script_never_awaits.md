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

### 2f. B1 — a Promise that settles AFTER `timeout_ms` killed the tab (review blocker; fixed)

Turning on `awaitPromise` made a latent crash reachable, and it was the worst
class this product has: silent, surviving the call that caused it, and reported
to the operator as "the browser may have crashed". Found by the independent
review of `68cf1c4`; reproduced here before anything was changed.

`tool_runtime._with_cdp_timeout` was a bare `asyncio.wait_for(coro)`. On expiry
it cancels *coro*, which cancels nodriver's `Transaction` — a bare
`asyncio.Future` — while its entry is STILL in `Connection.mapper`:
`Connection.send` is `self.mapper[the_id] = tx; return await tx`, with no
`finally`. When Chrome answers late, `Connection._listener` does
`tx = self.mapper.pop(id); tx(**message)`; `Transaction.__call__` ends in
`set_result` with no cancelled-future guard, inside the listener's `else:` branch
with no `try`/`except`. The `InvalidStateError` propagates out of `_listener`
and **ends the listener task**. From then on the connection dispatches no
responses and no events; every later call on that tab times out.

Measured (Chrome 152, nodriver 0.47, the branch's own `esDelayed`), both shapes:

| shape | before the shield | after |
|---|---|---|
| A. `execute_script("return esDelayed(9000,'late')", timeout_ms=1500)`, then `6*7` on the same instance | `Connection.mapper` held `[9]` after the timeout; after the late answer `_listener_task.done() == True` with `InvalidStateError('invalid state')`; follow-up `CDP operation timed out after 10s` | listener `ALIVE`, follow-up `{'success': True, 'result': 42}` |
| B. `navigate(url=<9 s-header route>)`, times out, then `6*7` | `mapper` held `[8]`; listener `DEAD (InvalidStateError)`; follow-up timed out | **still dead** — see below |

At 2.1.8 shape A is unreachable for `execute_script` only because without
`awaitPromise` the call never times out; **shape B is live on 2.1.8** and on
every release that has `navigate`'s inner `wait_for`.

**Shape B is F-882's shape, and it is a release-blocking product defect.**
`navigate` is bounded twice: the tool body's `_with_cdp_timeout` (now shielded)
AND `browser_manager.navigate`'s own bare
`asyncio.wait_for(navigation_milestone.navigate(...), timeout=timeout_seconds)`
at `browser_manager.py:1181` (plus the two `tab.evaluate` reads at `:1193` /
`:1197` under the remaining budget). The inner `wait_for` cancels the
`Page.navigate` transaction; when Chrome commits late — the F-882 report is
exactly a navigation that times out while the page *does* load — the listener
dies and the instance is dead from then on. That is a strong mechanical candidate
for the user's "browsers randomly closing" / "sessions randomly disconnect"
reports: the browser is fine, the tab's CDP listener is gone, and every tool
answers with the generic timeout. `browser_manager.navigate` and
`navigation_milestone` are owned by the F-882 agent and are deliberately NOT
touched here; the measurement and the census below are handed over instead.

**Census of the other bare `asyncio.wait_for` sites over a CDP send** (each is
the same cancellation-while-registered shape; none is changed in this PR):
`browser_manager.py:1181/1193/1197` (navigate), `:875/:894/:904/:929/:950`
(close paths — the connection is being torn down, benign), `cdp_function_executor.py:846`,
`tool_errors.py:213` (`_require_landing_ok`'s settled-URL read),
`tool_sections/browser_management.py:372` (`get_instance_state`, deliberately not
the wrapper), `tool_sections/debugging.py:71/:109`.

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

**The bound is SHIELDED, at its one home (B1).** `_with_cdp_timeout` now runs the
work as a detached task and awaits `asyncio.shield(task)` under the same
`wait_for`: on expiry the shield is cancelled, the task is not, nodriver's
`Transaction` is never cancelled, Chrome's late answer lands on a healthy future,
the listener lives, and the value is discarded. A done-callback
(`_discard_outcome`) retrieves the abandoned outcome so asyncio never logs "Task
exception was never retrieved" about a call already reported as timed out. The
deadline is byte-identical; the reviewer's proposed location
(`script_evaluation.evaluate`) was rejected by the coordinator as a second way —
every tool bounded by the wrapper had the same exposure — and the two
alternatives the reviewer rejected (popping our `mapper` entry, which turns the
later `pop` into a `KeyError` that kills the listener the same way; a hand-built
Transaction double in the hot path) were not re-litigated. What the shield COSTS
is named in its docstring: a timed-out multi-step operation now runs to
completion in the background instead of stopping part-way.

**The retry is keyed on the RECORD, not the message (review nit 1).** A page
controls `exception.description`, so `throw new Error("Illegal return statement")`
used to send a side-effecting script round the wrapper twice — pre-existing from
F-812, and F-883 had added a second spoofable phrase. `_function_body_reason`
now reads the raw `exceptionDetails`: `exception.class_name` must be
`SyntaxError` AND the description must carry no stack frame. Measured on Chrome
152: a compile complaint's description is the bare `SyntaxError: <message>`;
every thrown or rejected Error's is `error.stack` and carries `\n    at`. The
reviewer's suggested witness — absence of `exceptionDetails.stackTrace` — does
NOT work as stated: nodriver reports `stack_trace=None` for every shape,
including page throws, because Chrome sends it only under `Runtime.enable`, which
this seam does not send. The residual is in §6.

**The docstring now describes the tool.** It states that top-level `await` works,
that a returned Promise is awaited for you, that a rejection raises and is never
reported as a success, and that a script that never settles is killed at
`timeout_ms` — and a hermetic pin asserts those claims are in it, because a doc
claim nothing enforces drifts.

---

## 4. Verification

### RED, measured — the pins run against 2.1.8's own source

Both new files were run against `6ca0ae9`'s `src/stealth_chrome_devtools_mcp/embedded/`
(the release-2.1.8 tree, `script_evaluation.py` removed) with the *new* tests in
place. Hermetic: **17 failed, 5 passed in 3.01 s**. Real Chrome: **6 failed, 1
passed in 54.14 s**. The four headline nodes and their exact 2.1.8 messages:

| node | 2.1.8 |
|---|---|
| `test_top_level_await_runs_instead_of_being_a_syntax_error` | `ToolError: Script raised an exception: SyntaxError: await is only valid in async functions and the top level bodies of modules` |
| `test_a_returned_promise_answers_with_the_value_it_resolves_to` | `AssertionError: assert {} == {'k': 'es-fetched-value', 'n': 9}` |
| `test_a_rejected_promise_raises_carrying_its_reason` | `Failed: DID NOT RAISE ToolError` |
| `test_a_promise_that_never_settles_is_killed_at_timeout_ms` | `Failed: DID NOT RAISE ToolError` — 2.1.8 answered `{}` *instantly* |

The five hermetic and one real-Chrome nodes that were **already green on 2.1.8**
are the regression guards, and their staying green is the point: the CSP /
user-gesture flags and the absent `serializationOptions` (F-832), a top-level
declaration still evaluated unwrapped, an unrelated failure still evaluated
exactly once, an illegal `return` still named as a `return` (F-812), a short
reason not marked truncated, and a synchronous throw still carrying its own text.

`tests/fakes.py` gained `JsPromise` / `js_promise` for this, and it is the
mechanism the RED depends on: the double answers a script whose value is a
Promise **two different ways depending on the `awaitPromise` it reads off the
frame** — the settlement when it was asked for, and `RemoteObject(type=object,
subtype=promise, value={})` when it was not, which is what Chrome sends
(measured). A double that answered the settlement either way would have been
green for the defect, which is exactly how the first draft of two of these nodes
passed against 2.1.8 before the model existed.

### B1 pins — RED without the shield, GREEN with it (same tree, shield lines removed)

```
tests/test_e2e_execute_script_async.py::test_a_promise_that_settles_after_the_timeout_leaves_the_instance_usable
    RED:   ToolError: CDP operation timed out after 10s   (the follow-up 6*7 — dead tab)
tests/test_cdp_timeout.py::TestWithCdpTimeoutMechanism::test_timeout_does_not_cancel_the_inner_coroutine
    RED:   AssertionError: the shield, not the work, is what the timeout cancels
tests/test_cdp_timeout.py::TestWithCdpTimeoutMechanism::test_a_late_set_result_lands_on_a_healthy_future
    RED:   AssertionError: the Transaction must never be cancelled
                                                            -> 3 passed in 15.28 s
```

The second hermetic node is nodriver's exact mechanism with a bare `Future` in
the Transaction's place: after the timeout, `set_result` on it must be a normal
completion — under the old wrapper it was the `InvalidStateError` that killed the
listener. `test_timeout_cancels_inner_coroutine`, the pin that stood in that
file asserting the OLD behaviour, is inverted deliberately in the same PR. The
E2E node polls `window.esSettled` through the tool itself — every poll is a call
that would time out if the listener were dead — rather than sleeping, and its
settle time (2.5 s) is comfortably past its `timeout_ms` (800).

### Hermetic — `tests/test_execute_script_async.py` (28 nodes)

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

### Real Chrome — `tests/test_e2e_execute_script_async.py` (8 nodes)

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
path (same shape, falsy leaves included); `args`, plain and awaited; and the B1
pin — a Promise that settles AFTER `timeout_ms` (`esDelayed(2500)` under
`timeout_ms=800`), after which the instance must still answer.

No fixed sleep is an oracle. The un-settling node reads a clock only to bound an
answer it already has, because `timeout_ms` is the thing under test; the
late-settle node polls `window.esSettled` THROUGH the tool until the page has
recorded the settlement, so every poll is itself the liveness witness.

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

4. **A timed-out script is bounded, not cancelled in the page.** The wrapper
   abandons the *send* (shielded — B1); the Promise stays pending in the page
   until the document goes away, and the detached task holds its `Transaction`
   in `Connection.mapper` until Chrome answers, or forever for a Promise that
   never settles (one dict entry — the shape 2.1.8 already left behind). The tab
   is usable afterwards for BOTH settlement shapes that were measured — never
   settles (`esNever`) and settles late (`esDelayed`, §2f) — and both are pinned
   on real Chrome. A Promise that settles late is DISCARDED, not delivered.
   CDP's `Runtime.evaluate` `timeout` would kill the work in the page and is
   deliberately not used: see §3's "no new deadline".

4b. **The shield changes what a timed-out MULTI-STEP operation does.** Before,
   `wait_for` cancelled it part-way (and, if a send was in flight, killed the
   listener); now it runs to completion detached. A `type_text` that times out
   keeps typing in the background; a `scroll_page` keeps settling. Named as the
   price of not killing the tab — the alternative was a dead instance — and it
   is bounded by the operation's own remaining work, never by a new deadline.

4c. **`navigate`'s inner `wait_for` is NOT shielded — release-blocking, handed
   over (§2f).** `browser_manager.py:1181` cancels the `Page.navigate` transaction
   itself, below the shielded wrapper, so a navigation that times out while
   Chrome commits late STILL kills the tab (measured, this PR's own probe, after
   the shield). It is F-882's shape; the file is the F-882 agent's; the census of
   every other bare `asyncio.wait_for` over a CDP send is in §2f for the
   follow-up finding.

4d. **The retry trigger can still be spoofed by a page that overwrites `.stack`.**
   `const e = new SyntaxError('Illegal return statement'); e.stack = 'SyntaxError:
   Illegal return statement'; throw e;` — measured: `class_name='SyntaxError'`,
   no frame in the description — passes the record check and re-runs the script
   inside the wrapper. The trivial impostors (`throw new Error(...)`, a plain
   `throw new SyntaxError(...)`) no longer do. The only airtight fix is not to
   decide from Chrome's error record at all (a JS parse on our side), which is
   out of proportion; "a page that goes to that length can make our client run
   its script twice" is now a written decision rather than an accident, and a
   pin holds the narrowed rule.

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

7a. **`json.dumps` on `args` now joins the convention** (review nit 4): a
   non-JSON-serializable entry raises `ToolError` from the one home instead of a
   raw `TypeError`. Unreachable through MCP (args arrive as parsed JSON).

7b. **A trailing expression that is a Promise is now awaited** (review nit 5):
   `fetch('/slow')` as the last statement used to answer `{}` at once and now
   blocks up to `timeout_ms`. That is the intended fix; the docstring names the
   one-token remedy (`void fetch(...)`).

7c. **The version string is gone from the docstring** (review nit 7): "Before
   this fix", not "Before 2.1.9" — the CHANGELOG entry is under `## Unreleased`.

7. **`execute_python_in_browser` was not audited.** It translates Python to JS and
   runs it through its own path in `cdp_function_executor`; whether it shares this
   defect is not claimed either way here.
