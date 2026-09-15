# F-869 (MED, live-evidenced) — `get_instance_state` reported EMPTY storage with `partial: false` while a `TypeError` from our own code was logged at INFO

**Status:** FIXED on `fix/F869-page-state-storage-typeerror`.
**Found:** 2.1.5, by hand over the real stdio transport.
**Predicted:** by F-844's own residuals section, which named this exact read and
deferred it ("costs LOC this file does not have"). It was right about the shape
and wrong about the cost: on a page that really has storage the read does not
degrade to `{}`, it *raises*, and the raise is swallowed.

## 1. What was observed

Measured 2026-09-14 23:34:56 local, version 2.1.5, live backend pid 53836,
Windows 11, headless Chrome 152. A real-transport smoke did
`spawn_browser(headless=True)` → `navigate("https://www.google.com/")`
(success, title "Google") → `get_instance_state(instance_id)`.

The tool returned a full-looking record: 28 cookies, `"local_storage": {}`,
`"session_storage": {}`, `"console_logs": []`, `"partial": false`.

The backend log for that same call (correlation id `c5e09043b5d9`) says:

```
2026-09-14 23:34:56,804 INFO 53836 [c5e09043b5d9] stealth.backend: browser_manager.get_page_state: Storage access unavailable for 886a408c-4a8f-41ec-a096-8d06a1c1fee3: unhashable type: 'dict'
```

Three things are wrong with that pair of facts:

* `www.google.com` has localStorage entries, so `"local_storage": {}` is untrue.
* `"partial": false` asserts the record is complete. It was not.
* `unhashable type: 'dict'` is a **Python `TypeError` in this package**, not the
  "page blocks storage access" condition (`about:blank`, an opaque origin, a
  `data:` URL) that the INFO message claims. INFO records are not error-reported
  — `observability.py:599` initialises `LoggingIntegration(event_level=logging.ERROR)`
  — so the defect was invisible from *both* ends: the caller was told everything
  was fine, and nothing was ever shipped anywhere.

## 2. Cause (a) — the deep-serialized key array

`browser_manager.get_page_state`, 2.1.5, `browser_manager.py:1435-1447`:

```python
local_storage = {}
session_storage = {}

try:
    local_storage_keys = await tab.evaluate("Object.keys(localStorage)")
    for key in local_storage_keys:
        value = await tab.evaluate(f"localStorage.getItem('{key}')")
        local_storage[key] = value          # <-- :1442  the TypeError
```

`nodriver.core.tab.Tab.evaluate` (`.venv/Lib/site-packages/nodriver/core/tab.py:812-848`)
**always** sends deep serialization options and never asks for the value itself:

```python
ser = cdp.runtime.SerializationOptions(
    serialization="deep", max_depth=10,
    additional_parameters={"maxNodeDepth": 10, "includeShadowTree": "all"},
)
...
        if remote_object.deep_serialized_value:
            return remote_object.deep_serialized_value.value
```

and `cdp.runtime.DeepSerializedValue.from_json`
(`.venv/Lib/site-packages/nodriver/cdp/runtime.py:90-97`) keeps the payload
**raw** — `value=json['value']` — so it never walks into the graph:

```python
@classmethod
def from_json(cls, json: T_JSON_DICT) -> DeepSerializedValue:
    return cls(
        type_=str(json['type']),
        value=json['value'] if json.get('value', None) is not None else None,
        ...
```

A *primitive* therefore arrives plain, but an **array arrives as a list of BiDi
nodes**. Measured (not assumed) against Chrome 152 with the pinned nodriver 0.47,
over a real `http://127.0.0.1` origin with two localStorage entries set:

```
REPR Object.keys(localStorage): [{'type': 'string', 'value': 'alpha'}, {'type': 'string', 'value': 'beta'}]
TYPE: <class 'list'>
REPR getItem: '1'
HASH RAISED: TypeError unhashable type: 'dict'
```

That is the whole mechanism. `key` is a `dict`; `local_storage[key] = value`
hashes it; `TypeError: unhashable type: 'dict'` — the exact text in the live log
line. The `getItem` half looked fine (a string primitive survives deep
serialization), which is why only half this code path was ever suspected.

This is the **same trap F-844 closed for the viewport object literal eleven lines
below, in the same function**, whose fix comment still stands at
`browser_manager.py:1449-1451`:

```python
# ``JSON.stringify``, not a bare object literal: nodriver always
# sends deep serialization options, so an object comes back as
# ``[[key, {type,value}], …]`` — return_by_value cannot undo it.
```

F-844's finding named this follow-up explicitly
(`audit/stage2/finding_F844_get_instance_state_cookie_list_attributeerror.md`,
Residuals): *"`Object.keys(localStorage)` is an array, so `evaluate` returns
Chrome's deep serialization of it, not a list of strings… Converting them to the
same `JSON.stringify` idiom is the obvious follow-up."* It also recorded why the
live run did not catch it: *"`local_storage: {}` on a page that had no storage
anyway, so the shape is unproven either way."* An empty store never enters the
loop, so the defect is invisible on exactly the pages that were tested.

Two further properties of the old loop, both closed by the same fix:

* **Interpolation.** `f"localStorage.getItem('{key}')"` built JS out of
  page-controlled data. A key containing `'` is a syntax error; a key containing
  `');…` is script injection.
* **2N+2 round trips.** A page with 200 keys cost 402 CDP calls inside
  `get_instance_state`'s `browser_state_timeout_seconds` budget.

## 3. Cause (b) — the error policy that made it invisible

`browser_manager.py:1448-1461` (2.1.5):

```python
except (RuntimeError, ConnectionError) as e:
    debug_logger.log_warning(
        "browser_manager", "get_page_state",
        f"Storage access failed (connection issue) for {instance_id}: {e}",
    )
except Exception as e:
    # Pages may block storage access (cross-origin, opaque origins,
    # security policies)
    debug_logger.log_info(
        "browser_manager", "get_page_state",
        f"Storage access unavailable for {instance_id}: {e}",
    )
```

The comment states a narrow, legitimate condition; the `except Exception`
implements an unconditional one. Any exception whatsoever — including a defect in
this package — became the sentence "storage is unavailable on this page", at INFO,
with empty dicts flowing on into a record that then declared itself complete.

This contradicts the function's own docstring, three lines above the `try`
(`browser_manager.py:1415-1419`):

> Raises on a collection failure — `get_instance_state` is what turns that into
> its `partial` record.

`get_instance_state` does have that machinery and it is the project's named
sub-field degradation shape (F-746): `tool_sections/browser_management.py:313-330`
turns any exception out of `get_page_state` into
`{"partial": True, "detail_error": "Failed to collect full page state: …"}`.
The storage read was the one thing that never reached it.

It also passes `tests/test_no_silent_excepts.py` — the handler *does* log — which
is the limit of that AST census: it checks that something was said, not that the
right thing was said at the right level. A `log_info` with no `exc_info` on a
`TypeError` is not silence, but it is not a report either.

## 4. Blast radius

* **Which tools share the helper.** `get_page_state` has three callers:
  `get_instance_state` (`tool_sections/browser_management.py:292`) and two
  resources, `browser://{id}/state` and `browser://{id}/console`
  (`embedded/server.py:249`, `:299`). All three reported empty storage on every
  page that has any.
* **What a caller sees.** Nothing. `partial: false`, two empty dicts. An agent
  reading `local_storage: {}` on a logged-in app concludes the app keeps no local
  state, which is a wrong answer delivered with full confidence — worse than an
  error, because it is actionable.
* **Whether Sentry ever hears.** No. `LoggingIntegration(event_level=logging.ERROR)`
  (`observability.py:599`) ships ERROR records as events and reduces WARNING/INFO
  to breadcrumbs, which only travel attached to some *other* event. There was no
  other event: the exception never escaped. Per the memory note *external users on
  PyPI*, this is not a single-machine concern.
* **How long.** The loop predates F-844 (2.0.8) and is unchanged since; F-844
  fixed the two raising statements *around* it and left this one, documented, in
  place.

## 5. The fix

**(a) One read, one round trip, a JSON *string*.** The read moves to a new leaf,
`embedded/page_storage.py` — THE one home for "read a page's localStorage /
sessionStorage" — which asks the page for

```js
JSON.stringify((function(){
  function read(name){
    try{return {ok:true,entries:Object.entries(window[name])};}
    catch(e){return {ok:false,reason:String((e&&e.message)||e)};}
  }
  return {local:read('localStorage'),session:read('sessionStorage')};
})())
```

A string primitive survives deep serialization intact — the same idiom F-844
applied to the viewport, not a second one. Nothing is interpolated into the JS, so
the injection and the quote-in-a-key syntax error are structurally gone.
`window[name]` is read *inside* the `try` because the property access is what
throws on a blocked origin.

It is a leaf: it imports no other embedded module and takes the tab as an
argument. It exists as its own module for two reasons — `browser_manager.py` had
**zero** headroom under its 1529-LOC grandfather row, and the deep-serialization
argument is a paragraph that belongs with the JS rather than in the middle of
`get_page_state`.

**(b) Two outcomes, and they are told apart by the page, not by us.**

* `page_storage.StorageBlockedError` — Chrome itself threw while the page touched
  `window.localStorage` / `window.sessionStorage`. Measured message, from a
  `data:` URL on Chrome 152: *"Failed to read the 'localStorage' property from
  'Window': Storage is disabled inside 'data:' URLs."* This is the condition the
  old comment described, and it keeps exactly the old treatment: the existing
  INFO line, empty dicts, `partial: false`. An opaque origin really has no
  readable storage.
* **Everything else propagates.** `page_storage.StorageReadError` (the answer was
  not the JSON this module asked for — `tab.evaluate` hands back an
  `ExceptionDetails` husk rather than raising, so "it returned something" is not
  evidence it worked), a dying connection, or a `TypeError` from a future defect.
  It reaches `get_page_state`'s outer handler, which now logs **at WARNING with
  `exc_info`** and re-raises, and `get_instance_state` turns it into its
  `partial: True` + `detail_error` record.

`StorageReadError` covers *every* other way the answer can be wrong, including the
two that would otherwise escape as their own types: a non-JSON string
(`JSONDecodeError`) and JSON that is not an object (`AttributeError` on
`payload.get`). Both propagated correctly, but a module that says "there is no
third outcome" and then has four is a claim its code does not keep.

That is one degradation shape, the one that already existed, reached by raising —
which is what convention 2 and the function's own docstring already said. No
`{"success": False}` dict, no new field, no widened `except`, and the `except
Exception` that stays is narrower in effect than the one it replaces because the
expected condition now has its own named type in front of it.

`debug_logger.log_warning` grows one optional keyword, `error: Exception | None`,
forwarded as `exc_info` to the durable line. The in-memory ring shape is
unchanged, so `get_debug_view`'s tool contract is byte-stable.

The `except (RuntimeError, ConnectionError)` branch is **deleted**, not kept. It
swallowed a dying connection into the same untrue `{}` + `partial: false`; a
connection that is failing mid-collection is a degraded record by any reading.

**(c) The leak this fix must not introduce.** Caught in review of the first
commit, and worth its own section because it is a property of *fixes of this
shape*, not of this bug.

Making a silent failure visible means writing a message, and the first draft of
`page_storage` wrote `f"{store}: unexpected entries {rows!r}"` and
`f"{store}: unexpected entry {row!r}"`. `rows` **is the page's localStorage** —
where a logged-in app keeps its session token. That message travels three ways at
once:

* into the durable backend log (`~/.stealth-mcp/logs/`, retained by
  `logging_setup.prune_old_logs`),
* into `get_instance_state`'s `detail_error`, i.e. into the MCP client's hands,
* into a **Sentry breadcrumb** — `LoggingIntegration(event_level=logging.ERROR)`
  (`observability.py:599`) turns WARNING records into breadcrumbs that ride out
  attached to any later event, and `observability._scrub_event` strips emails and
  URL query strings, not a bare bearer token. Per the memory note *external users
  on PyPI*, that is other people's machines.

The defect being fixed never logged storage contents — it crashed before it could.
A fix that made the failure visible by quoting the data would have been strictly
worse than the bug. So `page_storage` states the rule in its module docstring and
every message reports **shape and count only**: a type name, an index, a field
count, a character count. Three parametrized pins embed a
JWT-shaped `SECRET` in every malformed answer and assert it reaches neither the
raised message, nor `detail_error`, nor any **formatted** log record (formatted,
not `getMessage()`, because the WARNING carries `exc_info` and the rendered
traceback is what a log file and a breadcrumb actually hold).

**The one page-supplied string that IS repeated, and its bound.**
`StorageBlockedError` carries the refusal text, because Chrome's own wording is
the diagnostic — *"Storage is disabled inside `data:` URLs"* is the answer an
operator needs — and `browser_manager.py:1447` logs it at INFO. But it is not
Chrome's word: measured on Chrome 152, `window.localStorage` is an **own accessor
with `configurable: true`**, so a page can `Object.defineProperty` a throwing
getter over it and author that string itself, at any length, straight into the
durable log and a Sentry breadcrumb. It is therefore capped at
`page_storage.BLOCKED_REASON_CHARS = 200` — Chrome's real message is 98
characters, so every genuine diagnostic survives whole — with a trailing `…` so a
reader can tell a cut message from a short one. Both halves are pinned:
`test_a_page_authored_refusal_is_truncated_to_the_budget` (RED without the cap: a
10 000-character reason came through whole) and
`test_chromes_own_refusal_survives_the_budget_whole`. The comment at the raise
site now says *page-controlled, bounded*, not "carries no stored value" — true but
the wrong reassurance.

## 6. Tests

`tests/test_page_state_storage.py` (new, hermetic, no browser):

* `test_storage_comes_back_from_the_shape_chrome_really_sends` — the deep
  serialized shape through `get_page_state`, asserting the **values**, not merely
  that nothing raised. RED before the fix with `assert {} == {'alpha': '1',
  'beta': '2'}`, and the run emitted
  `INFO stealth.backend: browser_manager.get_page_state: Storage access
  unavailable for i1: unhashable type: 'dict'` — the production log line,
  reproduced hermetically.
* `test_the_deep_serialized_key_array_is_never_hashed` — the mechanism isolated
  against `page_storage.read`.
* `test_a_key_with_a_quote_in_it_survives` — the interpolation half.
* `test_a_page_that_blocks_storage_is_still_not_partial` — the named tolerance,
  unchanged and now reached only by its own condition.
* `test_an_unexpected_storage_failure_is_reported_not_swallowed` — the policy:
  `partial: True`, the exception text in `detail_error`, and a WARNING record
  carrying `exc_info`. RED before the fix with `assert False is True` on
  `state["partial"]`.
* `test_a_blocked_page_is_logged_at_info_and_carries_no_traceback` — the two
  conditions must not collapse into one level.
* `test_an_answer_that_is_not_the_promised_json_is_an_error` — a husk, a non-JSON
  string, a JSON scalar, a record that is not an object and three malformed entry
  shapes are all read failures, never "no storage".
* `test_a_malformed_answer_never_quotes_the_storage_it_was_reading` and
  `test_the_storage_value_reaches_neither_the_log_nor_detail_error` — §5(c): the
  same seven malformed answers, each embedding a JWT-shaped `SECRET`, asserted
  absent from the raised message, from `detail_error` and from every formatted
  log record.
* `test_a_page_authored_refusal_is_truncated_to_the_budget` and
  `test_chromes_own_refusal_survives_the_budget_whole` — §5(c): the refusal text
  is bounded at `BLOCKED_REASON_CHARS` with a visible `…`, and the cap costs
  Chrome's own 98-character message nothing.

`DEEP_KEYS` in that module is the literal `repr` printed by the Chrome 152 probe,
per the memory notes *mocked fakes can encode the bug* and *fixtures from the same
serializer cannot fail*.

**`tests/test_instance_state_cookies.py` (F-844's home) is updated deliberately**,
in this commit, with the justification inline. Its `PAGE_JS` answered
`Object.keys(localStorage)` with `["ls-key"]` — a hand-shaped list of plain
strings, modelling the assumption the product got wrong. That fixture is the
reason this defect was green through F-844's own live-driven fix, and it is
exactly the failure mode that module's docstring warns about. It now answers the
one-shot read with a JSON string; its assertions (`{"ls-key": "ls-value"}`) are
unchanged.

Both fixtures key the viewport answer on **`innerWidth`**, not `JSON.stringify`.
`FakeTab._answer_for_js` returns the first substring that matches, and the storage
read and the viewport read now both *begin* with `JSON.stringify`, so a shared key
would have made dict insertion order decide which JSON the storage read received —
a fixture that is correct by accident. Each expression is keyed on a token unique
to it.

## 7. Files changed

| File | Δ |
|---|---|
| `embedded/page_storage.py` | **new**, 172 LOC (leaf) |
| `embedded/browser_manager.py` | +21 / −22 → **1528 LOC** |
| `embedded/debug_logger.py` | +10 / −1 (`log_warning(error=…)`) |
| `tools/check_file_budgets.py` | grandfather row **1529 → 1528** (ratchet DOWN, cap == actual) |
| `tests/test_page_state_storage.py` | **new** |
| `tests/test_instance_state_cookies.py` | +18 / −4 (SOFT golden, justified inline) |
| `CLAUDE.md`, `CHANGELOG.md` | navigation-map row + Unreleased entry |

## 8. Residuals (deliberately out of scope)

* **A collection failure still does not reach Sentry.** It is now in the durable
  log *with a traceback* and in the caller's `detail_error`, which is the whole
  finding. But `get_instance_state` catches the raise and returns `partial` (the
  F-746 contract), and `LoggingIntegration`'s `event_level` is ERROR, so no event
  is created. Raising the level, or capturing explicitly the way
  `observability.capture_lifecycle` does for proxy transitions, is a policy
  decision about the F-746 contract and belongs with whoever owns it.
* **`tests/test_no_silent_excepts.py` cannot see this class of defect.** It
  asserts a handler said *something*; it cannot assert the level matched the
  severity, or that a caught exception carried `exc_info`. A census of
  "`except Exception` whose only log is INFO/DEBUG" would have found this one, and
  probably others. Not attempted here — it is a new gate, not a fix.
* **`tab.evaluate` remains a shape hazard at every other call site.** F-844 said
  so and it is still true; this finding closes the third instance of it in one
  function. `DOMHandler.execute_script` (F-832) is the one seam that asks Chrome
  for the value by value and is safe. A sweep of the remaining bare
  `tab.evaluate` callers that expect a non-primitive is the durable fix and is
  larger than this finding.
* **There is deliberately NO named home for the `JSON.stringify` idiom here.**
  Three sites now use it (the viewport, and this module's two stores), and a
  fourth family — the cloner's nested BiDi nodes — is being measured under
  **F-872**, which owns the decision about whether these collapse into one home
  and where it lives. Extracting a shared helper in this branch would pre-empt
  that with a home chosen from three examples instead of four.
* **`console_logs: []` was not investigated.** The live record also carried an
  empty `console_logs`, which `PageState` defaults to and nothing in
  `get_page_state` ever populates. It may be a second untruthful field; it is a
  different mechanism and has no evidence yet.
* **`viewport` is still read by a second `tab.evaluate`.** Folding it into the
  same round trip as the storage read would save one CDP call, but it would move
  F-844's fix for no behavioural gain.
