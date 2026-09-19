# F-882e — the landing read fails when the document goes away under it

`navigate` reported a failure about a page that was fine. The navigation itself
was `[accepted, committed]`; what raised was the read AFTER it — the one round
trip that asks where the tab landed — because the document it was describing was
replaced while that read was in flight.

The fourth sibling of F-882b / F-882c / F-882d, in the same E2E node, and the
first of the four that is a **product** defect rather than an oracle that named
too few truthful states.

## 1. What was observed

`tests/test_e2e_navigation_truthfulness.py::test_a_meta_refresh_answers_about_a_
real_document_either_side_of_it`, on `integration (macOS/ARM64)`, twice, on two
different branches a day apart:

| run / attempt | date (UTC) | PR / branch |
|---|---|---|
| 35316298288 attempt 1 | 2026-09-18 06:52 | #133 `fix/F887-sentry-expected-noise` |
| 35460514255 attempt 1 | 2026-09-19 18:27 | #136 `fix/F834-stage2-connect-deadline` |

Byte-identical in shape (only the instance uuid differs):

```
tests/test_e2e_navigation_truthfulness.py:301: in test_a_meta_refresh_answers_...
src/stealth_chrome_devtools_mcp/embedded/navigation_milestone.py:272: in landing
    answer = await tab.evaluate(LANDING_JS)
E   nodriver.core.connection.ProtocolException: Inspected target navigated or closed [code: -32000]

WARNING stealth.backend: browser_manager.navigate: Navigation attempt 1 failed
  for da856624-…: ProtocolException: Inspected target navigated or closed
  [code: -32000] [accepted, committed]
```

Neither run's failure belongs to the PR it ran on: the two branches share no
source change on this path, and `browser_connect.py` — the only candidate on
#136 — wraps `HTTPApi.get` (`Browser.start`'s one `version` call) and cannot be
reached by `tab.evaluate` → `Connection.send`, which goes over the websocket.
The classification that established this is
`C:\Users\amind\AppData\Local\Temp\f834b_classification.md`.

## 2. Mechanism

The fixture's `meta refresh` document reaches `load` at ~22.8 ms and schedules
its replacement at ~23.5 ms (measured by the E2E node itself, recorded in
F-882d). `load` is the milestone `navigate(wait_until="load")` returns on, so
the product's two round trips are:

1. `navigation_milestone.navigate` — returns the instant the latest document in
   the frame's loader chain reaches the milestone;
2. `navigation_milestone.landing` — ONE `Runtime.evaluate` of
   `JSON.stringify([location.href, document.title])`.

Between them the replacement can commit. Chrome then answers the in-flight
evaluate with a protocol error rather than a value:

* code `-32000`, message `Inspected target navigated or closed` — Chrome's
  wording, captured twice above. It is **not** measured locally: this box was
  carrying 153 Chrome and 378 python processes from concurrent agent fleets when
  this was written, where a deliberate reproduction of a ~700 µs race is not
  evidence either way (and a local `nodriver` spawn loses its fixed ≈2.75 s
  connect deadline on a loaded machine, F-834). The two CI tracebacks are the
  measurement; the wording in `SWAPPED_MESSAGE` is copied from them verbatim,
  and `tests/fakes.py` carries its own copy so a pin measures the product
  against Chrome's text rather than against the product's own constant.

F-882d had already named the state ONE instant later — the replacement committed
*before* the read went out, so the read answers `(landing url, "")`, truthfully,
about a document that has committed and not yet parsed its `<title>`. This is
the same race resolved one instant earlier, and it is the only one of the two
sides that had no answer.

`browser_manager.navigate` was right not to retry it: `Progress.accepted` is
true, and F-881 fixed that a post-acceptance failure is the page's own and must
never spend a second budget on a replaced tab. So the exception was reported to
the caller as a failed navigation, which is the defect — nothing was wrong with
the navigation, the page, or the tab.

## 3. Fix

All of it in `navigation_milestone`, the one home for "where did it land":

* `SWAPPED_CODE` / `SWAPPED_MESSAGE` and `document_swapped(error)` — the narrow,
  named key, on `element_resolution._is_disabled_agent_error`'s precedent. Both
  halves are required: `-32000` is Chrome's generic server error (it also
  carries `DOM agent hasn't been enabled`), so the code alone would re-read for
  faults that say nothing about a document moving. Deliberately not a new
  `_STALE_NODE_MARKERS` entry and not a `recoverable_race`: those answer "should
  this selector resolution be tried again", which is a different question with a
  different consumer.
* `LANDING_SWAP_RETRIES = 2` and `landing`'s loop over `_read_landing`. A
  document swap buys at most two extra reads; the LAST read sits outside the
  loop, so its failure — swap or not — is what the caller sees and there is no
  unreachable tail. The caller's own `asyncio.wait_for(remaining)` is still the
  only deadline; this module adds none.
* Nothing else changed. `browser_manager.py` is untouched (it is also at
  1493/1493 of its budget), and the warning line it already writes carries the
  exception type, Chrome's text and `Progress.describe()` — which is exactly
  what made the CI diagnosis possible.

**Why a re-read and not a second milestone wait.** The alternative considered
was to re-arm the lifecycle chain and wait for the NEW head to reach the
milestone. Rejected: the milestone belongs to the navigation and *was* reached;
the chain's listener is removed by the time `landing` runs, so this would be a
second "when is a document ready" home one function away from `navigate`
(convention 4); and the answer it would wait for is not more truthful than the
one a re-read gets — a committed, still-parsing landing is a whole document at
one instant and is already a named truthful state (F-882d). The contract is one
snapshot of ONE document, not "the document that has finished".

## 4. Verification

Hermetic, `tests/test_navigate_milestone.py` + `tests/fakes.py`
(`FakeTab(landing_swaps=N)`: the landing read is refused with Chrome's error N
times, and each refusal DELIVERS the held supersession first, so the refusal is
evidence of a real swap and the re-read is measurably about the NEW document):

| pin | claim | RED before |
|---|---|---|
| `…_a_document_that_commits_under_the_landing_read_is_read_again` | `landing` answers `(landing url, "")`, in two reads, and that pair is in the E2E node's own `_meta_refresh_states` | yes — `ProtocolException: Inspected target navigated or closed [code: -32000]` out of `landing` |
| `…_the_tool_answers_success_when_the_read_raced_the_refresh` | the same race through `BrowserManager.navigate`: `success: True`, one `Page.navigate`, no `_replace_main_tab` | yes — the exception reached the caller, under the identical warning line CI printed |
| `…_a_page_that_swaps_under_every_read_raises_and_keeps_its_tab` | the bound: exactly `LANDING_SWAP_RETRIES + 1` reads, Chrome's error neither swallowed nor reworded, still no stale-tab recovery | guard, not RED — the no-retry half already held; the read-count half is new |
| `…_another_protocol_error_from_the_landing_read_is_not_retried` (×2) | BOTH halves of the key: the swap's code under another subject, and the swap's message under another code, each raised from the FIRST read | guard — it is what fails if either half is dropped |

Mutation-checked with `__pycache__` cleared before each run:

| mutation | red |
|---|---|
| `document_swapped` keyed on the code only | `…[swap-code-other-subject]` |
| `document_swapped` keyed on the message only | `…[swap-message-other-code]` |
| `LANDING_SWAP_RETRIES = 0` | the two RED pins above |

The message-only mutation is the one the first round of this work did not catch:
the docstring and CLAUDE.md claim both halves are required, and nothing measured
the code half until the second case was parametrized in (review of `46b9112`).
Its error dict is a synthetic pairing — no measurement says Chrome sends that
message under `-32602` — and it is honest about being one: what it pins is the
KEY's shape, not a browser behaviour.

Suites run by explicit path (Windows, `uv run python -m pytest`):
`test_navigate_milestone.py` (29), `test_navigation_truthfulness.py`,
`test_navigate_race_recovery.py`, `test_no_silent_excepts.py`,
`test_silent_excepts_log.py`, `test_error_typing.py` — 93 passed;
`test_doc_claims.py`; `ruff format --check`, `ruff check`, `ty`, `vulture`,
`tools/check_file_budgets.py` all clean.

The real-Chrome node stays exactly as it is: it is the CI-side proof, and its
three-state oracle already names both states a re-read can land on, so a green
run there is evidence about the product and not about a rewritten expectation.

## 5. Blast radius (what a caller saw)

Any `navigate` to a page that replaces itself within about one CDP round trip of
the milestone — `meta refresh`, a `load`-time `location.replace`, a JS
challenge, the self-reload shape — could fail with a raw
`ProtocolException: Inspected target navigated or closed`, at random, while the
browser sat on a perfectly good page. Nothing was retried and nothing was
degraded, so the caller's only signal was a failed tool call; the tab, the
instance and the browser were all healthy, and an immediate second `navigate`
would usually succeed. Seen twice, both on `integration (macOS/ARM64)`, and on
no Windows or Linux cell in the same window — consistent with a scheduling race
that a loaded runner widens, not with a platform difference.

## 6. Not claimed / deliberately unchanged / what remains

1. **The wording is not measured on this machine.** `SWAPPED_MESSAGE` comes from
   two CI tracebacks, not from a local Chrome. If Chrome ever rephrases it, the
   key stops matching and the defect returns in its original shape — loudly, as
   the same exception, not silently. The code is pinned beside the message for
   exactly that reason: both would have to change.
2. **A re-read can still answer about the OUTGOING document.** If the retry's
   evaluate reaches Chrome before the replacement's execution context exists,
   Chrome may answer from the document that is on its way out. That is state 1
   of `_meta_refresh_states` — truthful at the instant it was read — but it is
   not "the newest document", and nothing here guarantees the newest.
3. **Three reads is a guess about depth, not a measurement.** One swap is
   measured; two is headroom for a landing that itself redirects. A page that
   replaces itself faster than a round trip, forever, still fails — deliberately:
   the alternative is a loop whose length the page chooses.
4. **`browser_manager.navigate` was not changed, and `pages_own` was not
   widened.** A surviving swap error is already not retried, because
   `_is_recoverable_navigation_error` does not match it (`"target closed"` is not
   a substring of `"target navigated or closed"`). Adding it to `pages_own`
   would be a second statement of a decision already made. What holds that in
   place is now a pin rather than a coincidence of substrings — which is worth
   knowing before anyone widens that marker list.
5. **A swap that survives the bound reaches the caller as a raw `nodriver`
   `ProtocolException`, not a `ToolError` — deliberately, and it is now pinned.**
   That is convention 2's shape only for failures the tool itself decides; a CDP
   error surfaces as the library's exception everywhere in this tree today, and
   it is exactly what both CI tracebacks printed, so nothing here is a
   regression. Wrapping it at this ONE call site would make the same class of
   browser failure arrive in two different shapes depending on which read raised
   it — the second-way defect — so the choice is to keep it uniform and say so.
   The pin asserts the type to state that the retry neither swallows nor rewords
   what it could not absorb; it is not a claim that a raw library exception is
   the best operator-facing answer. Giving CDP failures one convention-2 shape,
   with an operator message like "the page replaced itself faster than the
   landing could be read", is a real improvement and a separate change: its
   blast radius is every CDP call site, not one navigation.
6. **The same error can reach every other CDP read in the tree** — `page_storage`,
   `tab_identity`, `click_target`, the cloner aspects. Nothing here changes them,
   and nothing here should: `landing` is the one read that runs immediately after
   a navigation, i.e. the one place a document swap is the EXPECTED event rather
   than a surprise. A general "retry any read whose document moved" policy would
   be a different finding with a different argument.
7. **The E2E node remains the only real-Chrome witness**, and it is timing
   dependent: two hits across the macOS integration run/attempts sampled between
   2026-09-17 and 2026-09-19 (the census in the classification), and the same
   node is green on every other cell. A green run of it does not prove this fix
   works; the hermetic pins do, and the node is what proves the shape is real.
