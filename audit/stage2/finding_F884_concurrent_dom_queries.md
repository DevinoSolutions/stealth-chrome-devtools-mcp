# F-884 — concurrent selector resolution on one tab crashes with `-32000`

**Status**: fixed on `fix/F884-concurrent-dom-queries`.
**Severity**: high. Any client that pipelines MCP requests loses most of the
concurrent DOM calls it makes against a single tab; at five concurrent mixed
resolutions, **14 of 15 raised**.
**Files**: `src/stealth_chrome_devtools_mcp/embedded/element_resolution.py`
(the fix), `src/stealth_chrome_devtools_mcp/embedded/dom_handler.py`
(two call sites rewired).

---

## 1. Mechanism

The brief described this as "nodriver's `query_selector` path does
`dom.enable()` … `dom.disable()` around each call, so two overlapping calls
race". That is not what nodriver 0.47 does, and the real chain matters because
it decides what the fix has to be. Reading
`.venv/Lib/site-packages/nodriver/core/tab.py`, `DOM.disable` is sent from five
places and only one of them is paired with an `enable`:

| nodriver method | line | sends `DOM.disable` |
|---|---|---|
| `Tab.xpath` | 432 / 444 | yes — `enable()` … `finally: disable()`, and the `disable` failure is caught |
| `Tab.query_selector_all` | 529 | only inside `except ProtocolException`, before re-raising |
| `Tab.query_selector` | 586 | only inside `except ProtocolException`, before re-raising |
| `Tab.find_elements_by_text` | 678 | unconditionally at the end, no `enable`, uncaught |
| `Tab.find_element_by_text` | 776 | `finally:`, no `enable`, uncaught |

The actual chain has three links.

**(a) `DOM.getDocument` resets this CDP session's node-id bindings.** Blink's
`InspectorDOMAgent::getDocument` enables the agent and discards the frontend
bindings, so every node id previously handed to that session dies. Every
resolution in this codebase is `getDocument` followed by a query that *uses*
the id it returned, so two overlapping resolutions on one tab always lose: A
fetches the document, B fetches it and resets the table, A's query raises
`Could not find node with given id [code: -32000]`.

Measured directly (`_probe_session_scope.py`, Chrome 152):

```
conn1 node id: NodeId(5)
conn2 doc node id: NodeId(1)
CROSS-SESSION SAFE: conn1 node id still resolves -> '<div id="present">hello</div>'
SAME-SESSION: node id died on own re-fetch -> Could not find node with given id [code: -32000]
```

A **second** connection to the same target does not disturb the first. The
raced table is therefore per CDP session, which is per nodriver
`Connection`/`Tab` object — and that is what fixes the lock's scope.

**(b) nodriver replaces the recoverable error with an unrecoverable one.**
`Tab.query_selector` answers that `ProtocolException` by sending
`DOM.disable()` *before* re-raising. That send is itself a round trip, and it
fails with `-32000 "DOM agent hasn't been enabled"` once a sibling has already
disabled the agent. Because it is raised from inside the `except` block, it
**replaces** the stale-node error. Confirmed by walking `__context__`:

```
ProtocolException: DOM agent hasn't been enabled [code: -32000]
  <- ProtocolException: Could not find node with given id [code: -32000]
```

**(c) `element_resolution` cannot classify what it never sees.**
`_STALE_NODE_MARKERS` matches `"Could not find node with given id"` and
`"DOM Error while querying"`. `"DOM agent hasn't been enabled"` is neither, so
`recoverable_race` returns `None` and the bounded retry — which would have
absorbed link (a) on its own — never runs. The caller gets a bare `-32000`.

The XPath path produces a second wording from the same cause,
`"DOM agent is not enabled [code: -32000]"` (`DOM.performSearch` on a disabled
agent), which is why widening the marker list was never the right answer: the
message is Chrome's and there is no closed set of them.

**A fourth, independent site.** `dom_handler.query_elements` called
`elem.update()` once per returned element, and `Element.update()` sends
`DOM.getDocument(-1, True)` (nodriver `element.py:286`). That is link (a)
reached from outside any resolution: **one listing of twenty elements reset the
table twenty times** while a sibling was mid-query. `get_element_state` had the
same call. This is why the first version of the fix — a lock inside
`element_resolution` only — still failed the mixed-shape test at 1/15.

---

## 2. Measurements

All on Chrome 152 / nodriver 0.47, Windows, against a static `data:` page, five
rounds per cell, through `element_resolution.resolve_element` (scratch harness
`_repro_f884.py`, not committed). "failed" counts resolutions that raised.

### 2a. Failure rate vs. concurrency, element present

| concurrent resolutions | before | after |
|---|---|---|
| 1 | 0/5 | 0/5 |
| 2 | 0/10 | 0/10 |
| 3 | **5/15** | 0/15 |
| 5 | **15/25** | 0/25 |

The pattern before the fix is exactly `N - 2` failures per round, every round —
deterministic, not flaky. At N=2 the stale-node error is still raised but the
`disable` succeeds (the agent is still enabled), so the original marker text
survives and the existing retry absorbs it; that is why the defect only becomes
visible at three.

### 2b. Latency — the fix is faster in the common case

Mean wall time for one round of N concurrent resolutions of a **present**
selector:

| concurrent resolutions | before | after |
|---|---|---|
| 1 | 0.003 s | 0.001 s |
| 2 | 0.122 s | 0.005 s |
| 3 | 0.122 s | 0.005 s |
| 5 | 0.123 s | 0.009 s |

24x faster at N=3. Serialising is cheaper than racing because each race the
lock removes used to cost a `_SETTLE_SECONDS` backoff plus a full re-resolve.

### 2c. The first cut held the lock across the WAIT — what that cost

The lock originally wrapped `tab.select`/`select_all`/`find`/`xpath`, each of
which polls *inside* the same call. Reviewer's measurements through the real
handlers on real Chrome, and the same three after moving the wait out:

**Starvation.** `wait_for_element("#late", 6000)` concurrent with a click that
creates `#late` 200 ms later:

| | waiter | the click |
|---|---|---|
| base `6ca0ae9` | **True** @ 1.08 s | 0.33 s |
| lock across the wait | **False** @ 10.71 s | 10.72 s |
| wait outside the lock | **True** @ 1.02 s | 0.52 s |

The middle row is a *wrong answer*, not a slow one: the click could not resolve
`#b` until the waiter released, so the element could not be created until the
waiter had given up.

**Sibling latency.** `wait_for_element("#never", 1000)` concurrent with
`query_elements("p")`:

| | waiter | sibling |
|---|---|---|
| base | False @ 11.42 s | **0.25 s** |
| lock across the wait | False @ 11.06 s | **10.56 s** |
| wait outside the lock | False @ **1.02 s** | **0.01 s** |

The last row beats the base on both counts. The sibling is 25x faster than
base, and the waiter finally honours the 1 s it was given — base overshot its
own request by 10x, because `wait_for_element`'s outer loop wrapped nodriver's
10 s default.

**The tool's own budget.** Four concurrent absent-selector `query_elements`,
each under `CDP_OPERATION_TIMEOUT` (30 s):

| | outcome |
|---|---|
| base | 2 x ok (10.2 s, 20.7 s), 2 x `ProtocolException` (the F-884 crash) |
| lock across the wait | 2 x ok (10.1 s, 20.6 s), 2 x **`TimeoutError` @ 30 s** |
| wait outside the lock | 4 x ok @ **10.20–10.21 s** |

They now run concurrently rather than serialising, so the N-th caller no longer
pays N x the wait and nothing reaches the tool's budget.

### 2d. What the wait still costs

Each try is one locked round-trip pair, so a genuinely slow single query on a
huge document still delays a sibling by its own duration. And the defaults were
unified: nodriver waits 10 s in `select`/`find`/`select_all` but 2.5 s in
`xpath`, for no stated reason, so a genuinely absent **XPath** now takes 10 s to
answer "not found" where it took 2.5 s. That is deliberate — this module
advertises one contract for CSS and XPath — and a caller that wants less passes
`timeout`.

### 2e. Under DOM churn

A page mutating its body every 7 ms, three concurrent resolutions of a present
selector: 0/15 failed, mean round wall 0.018 s. The pre-existing
`documentUpdated` recovery still does its job; the lock did not replace it.

### 2f. Through the real tools, real Chrome

`tests/test_e2e_concurrent_dom_queries.py`, five concurrent calls per test:

| test | before | after |
|---|---|---|
| `wait_for_element` x5 | 3/5 raised | pass |
| `query_elements` x5 | 3/5 raised | pass |
| mixed `wait_for_element` + CSS + XPath, 15 calls | **14/15 raised** | pass |

---

## 3. The fix, and what was rejected

### Chosen: one `asyncio.Lock` per tab, held across each resolution attempt

`element_resolution._document_lock(tab)` returns the tab's lock;
`_resolve_with_recovery` takes it around each attempt. Every function in the
module routes through that loop, so no selector-resolving path can opt out.
`refresh_element(tab, element)` is the one home for the post-resolution
`Element.update()` and takes the same lock.

Four properties, each load-bearing:

* **Scope is the tab object**, because §1(a) measured that the raced table is
  per CDP session and a session is per `Tab`. A per-browser lock would cost
  every multi-tab caller for nothing.
* **Keyed by `id(tab)` with a `weakref.finalize`**, because nodriver's
  `Connection` defines `__eq__` and no `__hash__` — a `Tab` is *unhashable* and
  cannot key a dict, weak or otherwise. The finalizer is what makes the id safe
  against recycling.
* **Per attempt, not around the retry loop**, so the settle sleep never holds
  the lock and a churning tab still lets a sibling in between its own tries.
* **Not re-entrant, and nothing needs it to be**: no function in the module
  calls another, so a resolution never nests inside a held lock. This is also
  why the lock is *not* extended over `cdp_element_cloner.extract_complete_element`,
  which runs its six aspects through `asyncio.gather` — gather creates child
  tasks, and a non-re-entrant lock held by the parent would deadlock the styles
  aspect that resolves inside it.

### Rejected: add `"DOM agent hasn't been enabled"` to `_STALE_NODE_MARKERS`

The smallest possible diff, and wrong three times over. It treats the symptom
(a masked error) rather than the cause (an unsynchronised `getDocument`); the
message set is not closed, as the XPath path's different wording
`"DOM agent is not enabled"` proves; and it would make the retry loop a
lottery — at N=5, three of five resolutions collide on *every* round, so
bounded retries would still exhaust. It also leaves 2b's cost in place: every
absorbed race still pays a backoff. Rejected.

### Rejected: keep the DOM domain enabled for the tab's lifetime

The brief's suggested alternative. It does not address link (a) at all —
`getDocument` resets the bindings whether or not the agent stays enabled — and
it cannot address link (b) either, because the `disable` that masks the error
is sent by nodriver, not by us, so "we keep it enabled" only changes which call
gets the failure. Not measured beyond that, because the mechanism rules it out
before cost does.

### Adopted on review: own the wait loop so waiting happens outside the lock

This was first written up as *rejected* — "the better end state, but a polling
behaviour change, and F-884 is a crash". The review measured what deferring it
cost (§2c) and that reasoning does not survive the numbers: holding the lock
across nodriver's bundled wait turned a crash into a **wrong answer** and into
a tool-level `TimeoutError`, both for exactly the pipelining clients the fix
exists to serve. It is in this change.

`_wait_for` is the one home. Under the lock: nodriver's single-shot
`query_selector` / `query_selector_all` / `find_element_by_text`, and
`find_elements_by_text` for XPath (`DOM.performSearch` takes an XPath — it is
what `Tab.xpath` is built on). Between tries, with the lock released:
`_POLL_SECONDS`, bounded by the caller's own deadline. Both numbers are
nodriver's own (10 s budget, 0.5 s interval), so a caller that passed no
timeout waits exactly as long as it always did.

Two details the review called out and one it did not:

* `tab.select(selector, timeout=0)` would have been the smaller diff and is
  wrong: `Tab.select` still does `await self` per turn, i.e. F-881's 0.5 s
  `Tab.wait` floor, **inside** the lock. `query_selector` avoids it entirely.
* `Tab.xpath` brackets its poll loop in `dom.enable()`/`dom.disable()`, so even
  at `timeout=0` it is an indivisible multi-round-trip call. Dropping to
  `find_elements_by_text` removes that bracket too.
* `wait_for_element` already owned a 0.5 s poll loop and passed **no** timeout
  inward, so every one of its turns carried nodriver's 10 s default inside the
  caller's budget — which is why a 1 s request measured 11.42 s on base. It now
  passes `timeout=0`: one query per turn, its own loop is the wait.

The recovery loop is unchanged and still wraps each single-shot try, so link
(b)'s `DOM.disable()`-before-re-raise still only fires for a genuine error and
`recoverable_race` still classifies it.

### Rejected: a lock inside `element_resolution` only

Measured, not reasoned: it passed both single-shape e2e tests and still failed
the mixed-shape one 1/15, because `dom_handler` reached `Element.update()`
directly. Fixing the one home while leaving a second door open is the defect
this repo's fourth convention names.

---

## 4. Blast radius

Every selector-driven tool inherits the lock, since all of them route through
this module (convention: "never `tab.select`/`find` directly"). Audited callers:

* **`dom_handler.py`** — `query_elements`, `get_element_state`,
  `wait_for_element`, `click_element`, `type_text`, `paste_text`,
  `select_option`, `upload_file`, `scroll_page`. All reach `resolve_element` /
  `resolve_elements` / `resolve_by_text`. Two sites additionally called
  `elem.update()` and now call `refresh_element`; both previously carried a
  `hasattr(elem, "update")` guard, which moved into the one home so each site
  is one line.
* **`tool_sections/element_interaction.py`** — thin bodies over the above; no
  direct resolution, unchanged.
* **`cdp_element_cloner.py`** — calls `element_resolution.query_selector_all`,
  so the resolution itself is now serialised. Its *subsequent* use of the
  returned node id is not protected; see §6.
* **`browser_manager.navigate`** — consults `recoverable_race` but resolves no
  selector; untouched.
* **`text_entry.py` / `click_target.py` / `control_state.py`** — reach the page
  through `Element.apply`, which uses `DOM.resolveNode(backend_node_id=…)`.
  Backend node ids are stable across `getDocument`, so these never raced and
  are untouched.
* **Other tabs** — unaffected by design, pinned by
  `test_the_lock_is_per_tab_so_two_tabs_still_resolve_concurrently`.

No tool signature, return shape or error message changed. No new
`STEALTH_MCP_*` knob. `element_resolution.py` goes 350 → 456 LOC against the
1000 default; no budget moved.

---

## 5. Tests

**Hermetic** (`tests/test_element_resolution.py`, fast lane):

* `test_concurrent_resolutions_on_one_tab_are_serialised` — the RED. Three
  concurrent resolutions against `_RacingTab`, which answers an overlapping
  call with the *masked* error, exactly as measured. Asserts all three succeed
  **and** `max_in_flight == 1` **and** no attempt retried — so a fix that made
  the raise go away by retrying would not pass.
  Verified RED on unfixed source:
  `ProtocolException: DOM agent hasn't been enabled [code: -32000]`.
* `test_the_lock_is_per_tab_so_two_tabs_still_resolve_concurrently` — guards
  against over-serialising; passes before and after by construction, which is
  the point of it.
* `test_one_tab_gets_exactly_one_lock_and_loses_it_when_collected` — one lock
  per tab, and the table entry dies with the tab.
* `test_the_settle_sleep_between_attempts_does_not_hold_the_lock`.
* `test_refresh_element_takes_the_same_lock_as_a_resolution` — observes the
  lock from inside `Element.update()`, the site that made the first version
  of the fix insufficient.
* `test_refresh_element_tolerates_a_node_that_cannot_be_updated` — the
  `hasattr` tolerance both call sites used to carry, now in the one home.
* `test_a_waiting_resolution_does_not_hold_the_lock_between_tries` — reads
  `lock.locked()` from inside the sleep, three tries in a row.
* `test_the_wait_is_bounded_by_the_callers_timeout_not_nodrivers` and
  `test_the_timeout_is_spent_here_and_never_handed_to_nodriver` — the budget is
  spent in this module's loop, and the single-shot query takes no `timeout` at
  all.
* `test_resolve_element_never_calls_nodrivers_bundled_wait` — the fakes offer
  **only** the single-shot surfaces (`query_selector`,`query_selector_all`,
  `find_element_by_text`, `find_elements_by_text`), in
  `tests/test_element_resolution.py`, `tests/test_xpath_dispatch.py`,
  `tests/test_dom_handler.py` and the shared `tests/fakes.py`. A regression to
  `tab.select` is an `AttributeError`, not a silent slowdown — which matters
  because the B1 defect was invisible to every pin that existed.

**Integration** (`tests/test_e2e_concurrent_dom_queries.py`, `integration`
marker, real Chrome), five tests:

* the three concurrency cases in §2f — verified RED on unfixed source at 3/5,
  3/5 and 14/15;
* `test_a_waiter_does_not_starve_the_call_that_would_satisfy_it` and
  `test_a_waiter_does_not_delay_a_sibling_query_on_the_same_tab` — the two B1
  regressions, verified RED against `345a0db` (the lock-across-the-wait commit)
  with its exact symptoms: `False` for an element the concurrent click created,
  and `a 1 s waiter held the tab for 10.45 s`. The first asserts the RESULT,
  not a duration, because the failure it guards is a wrong answer.

---

## 6. Residuals

1. **The default Claude Code client serialises requests, so this is latent for
   it.** It was measured in-process, not through Claude Code. It is real for
   any pipelining client, and the product's own machinery overlaps requests —
   the stdio proxy does `start_soon` per request and the backend runs an anyio
   task group. It is also real for anything that fans out inside one tool call.

2. **A node id is still only safe while the lock is held.**
   `query_selector_all` hands back raw ids and the caller owns them after the
   release. `cdp_element_cloner.extract_complete_element_cdp` holds one across
   five further CDP calls (`_get_element_html`, computed styles, matched
   styles, event listeners, children); any concurrent resolution between two of
   them invalidates it, by the same §1(a) mechanism. Not fixed here: the span
   is long, holding the lock across it would block the tab for the clone's
   whole duration, and `extract_complete_element` fans its six aspects out
   through `asyncio.gather`, so a non-re-entrant lock held above that would
   deadlock. It needs its own finding, its own measurement and probably a
   re-entrant scope.

3. **Waiters no longer serialise — this residual is gone.** It was written as
   "concurrent waiters on an absent selector serialise, 4.59 s → 9.41 s for
   three at a 3 s timeout", and the review was right to dispute the framing:
   the cost was never confined to waiters among themselves. A waiter froze
   *every* DOM operation on that tab, including the one that would have
   satisfied it, for nodriver's 10 s default regardless of its own budget
   (§2c). Owning the wait loop removed both halves: four concurrent absent
   resolutions now answer at 10.20–10.21 s each rather than at 10/20/30 s, and
   a sibling query during a 1 s wait went from 10.56 s to 0.01 s — faster than
   the pre-fix base.

   What replaces it is smaller and named in §2d: one locked round-trip pair at
   a time, so a genuinely slow single query still delays a sibling by its own
   duration; and a genuinely absent **XPath** now waits 10 s rather than
   nodriver's 2.5 s, because the two languages were given one budget.

4. **The two wordings are Chrome's.** `"DOM agent hasn't been enabled"` and
   `"DOM agent is not enabled"` both appear, from `DOM.disable` and
   `DOM.performSearch` respectively. Nothing in the fix depends on either
   string — which is the point — but a future reader grepping for one will miss
   the other.

5. **`_DOCUMENT_LOCKS` growth is bounded by live tabs only**, via
   `weakref.finalize`. If a caller ever holds a `Tab` forever, its lock leaks
   with it; that is the tab's leak, not the table's.

6. **nodriver's `disable`-before-re-raise is still there.** The lock stops us
   generating the race that triggers it, but any future code path that reaches
   `Tab.query_selector` outside this module re-opens it. The convention already
   forbids that; nothing mechanically enforces it.
