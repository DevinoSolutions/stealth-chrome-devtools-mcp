# F-882 — `navigate` times out on a loaded page whose document replaced itself

**Status:** FIXED in this PR (product defect; live on 2.1.8 and on `main` at `6ca0ae9`)
**Opened by:** a ten-site fleet test on 2.1.8, Chrome 152 headed, nodriver 0.47,
Windows 11, 2026-09-16 11:51 local. Three of the ten navigations raised
`Navigation to <url> timed out after 30000ms` while the browser was sitting on a
fully loaded page.
**Source at:** `origin/main` = `6ca0ae9` (release 2.1.8)
**Severity:** HIGH. `navigate` is the tool every session starts with. The three
sites that failed are `mail.google.com`, `youtube.com` and `reddit.com` — not an
adversarial corner, the most ordinary destinations there are — and each cost the
caller the full 30 s budget and then a raise, with a log line whose reason was
literally empty. F-881's PR body named this class as open ("net::ERR_ABORTED /
superseding JS/meta redirect times out once"); the fleet test shows it is the
common case, not a corner.

---

## 1. What was observed

| url sent | what `navigate` answered | what the tab actually showed afterwards |
|---|---|---|
| `https://mail.google.com/` (signed out) | `Navigation … timed out after 30000ms` | `https://workspace.google.com/intl/en-US/gmail/`, title present, `performance.getEntriesByType('navigation')[0].type == "reload"`, `loadEventEnd` 1.9 s |
| `https://www.youtube.com/` | timed out after 30000ms | `https://www.youtube.com/`, title `YouTube`, nav entry type `navigate`, `redirectCount` 0, `loadEventEnd` 1.03 s, full app rendered |
| `https://www.reddit.com/` | timed out after 30000ms | `https://www.reddit.com/?solution=…&js_challenge=1&jsc_token=…`, title `Reddit - The heart of the internet`, type `navigate`, `loadEventEnd` 0.96 s |
| `https://www.amazon.com/` | `success: true, title: ""` | title `Amazon.com. Spend less. Smile more.`; nav entry type `reload` — the first document had no title and the page reloaded itself |

The backend log for each failure is

```
browser_manager.navigate: Navigation attempt 1 failed for <id>:
tool navigate end (30045.0ms)
```

— note the EMPTY reason after the colon. `str(TimeoutError())` is `""`, so the
one durable record of the failure said only that something had failed. Exactly
one attempt was made, which is correct per F-881 (a timeout *after* acceptance is
the page's own and is not retried on a replaced tab).

Every row shares one shape: the nav entry the page ends on has `redirectCount`
0 and type `navigate`/`reload`, i.e. the document the tab is showing is **not
the document Chrome committed for the `loaderId` our `Page.navigate` answered
with** — it is a later one that replaced it.

---

## 2. Mechanism (measured, Chrome 152 headless, nodriver 0.47.0, Windows 11)

F-881's `navigation_milestone.navigate` waits for `Page.lifecycleEvent` name
`load` carrying the exact `loaderId` `Page.navigate` answered with. Every shape
below is a local fixture page (`tests/fixture_routes.py`, the `nav_*` routes);
the full `Page.lifecycleEvent` + `Page.frameNavigated` +
`Page.frameStartedNavigating` + `Page.frameRequestedNavigation` stream was
recorded for each, timed relative to the `Page.navigate` response.

### 2a. `Page.navigate` answers at the commit of the loader it names

| shape | `frameStartedNavigating` | response | our `init` |
|---|---|---|---|
| (a) head-script replace | 5.5 ms | 8.6 ms | 10.9 ms |
| (b) meta refresh | 6.2 ms | 11.5 ms | 14.0 ms |
| (c) self-reload | 5.1 ms | 8.8 ms | 11.0 ms |
| (d) js challenge | 5.1 ms | 7.9 ms | 9.7 ms |
| (e) 302→302→slow | 5.3 ms | 14.3 ms | 16.7 ms |
| (g) 2.5 s document | 5.4 ms | **3010 ms** | 3011.7 ms |

The response tracks the commit, not the send — (g) proves it. The `loaderId` is
known ~3 ms *earlier* still, from `Page.frameStartedNavigating`. The response and
the document's own events remain unordered (F-881 §2d measured `DOMContentLoaded`
0.3 ms *before* the response), which is why every event is buffered here.

### 2b. The supersession, and why the old wait never ended

| shape | our loader's last event | the replacement's `init` | the replacement's `load` | our `load` |
|---|---|---|---|---|
| (a) head script `location.replace` | `init` 10.9 ms | 32.6 ms (+21.7) | 38.9 ms | **never** |
| (d) js challenge (cookie + `replace`) | `init` 9.7 ms | 30.0 ms (+20.3) | 38.6 ms | **never** |
| (b) `meta refresh` 0 | `load` **22.8 ms** | 36.5 ms (+22.5) | 45.4 ms | 22.8 ms |
| (c) self-reload once | `load` **26.5 ms** | 32.4 ms (+21.4) | 39.9 ms | 26.5 ms |

(a) and (d) are the defect: the document that carried our `loaderId` was
destroyed by a script running in its own `<head>`, before it could fire `load`,
and the wait then had nothing left to wait for. (b) and (c) are the same family
but the first document *does* reach `load` first — which is why Amazon answered
`success: true, title: ""` rather than timing out, and why the Gmail/YouTube/
Reddit rows are the ones that hung.

### 2c. A subframe's loader is a different frame, and can be filtered out

Same-origin `<iframe src="/nav/head-replace">` inside a titled host:

```
13.7 ms  init  loader=6CCEC23D  frame=966836E7            <- the host (main frame)
23.5 ms  init  loader=36F8D421  frame=907517DE            <- the iframe's first doc
31.6 ms  init  loader=24A8E307  frame=907517DE parent=966836E7
39.0 ms  init  loader=267C6010  frame=907517DE parent=966836E7   <- it replaced itself
40.8 ms  load  loader=267C6010  frame=907517DE
40.9 ms  load  loader=6CCEC23D  frame=966836E7            <- the host
```

Two loaders committed, loaded and were replaced inside the page while we waited,
all under the SUBFRAME's `frameId`. Filtering on the frame `Page.navigate`
returned is what keeps them out of the chain.

### 2d. `Page.setLifecycleEventsEnabled(true)` REPLAYS the current document

This was not known when F-881 shipped and it is the one thing that could make a
frame-wide rule wrong. Isolated measurement (`Page.enable` re-sent at 1073 ms,
`Page.setLifecycleEventsEnabled(true)` re-sent at 2077 ms, on a page that
committed under loader `0449A0E7` at 73.7 ms):

```
1073.7  --- re-sending Page.enable ---
1074.5  networkAlmostIdle / firstMeaningfulPaint / networkIdle   loader=0449A0E7
2077.2  --- re-sending setLifecycleEventsEnabled ---
2078.8  commit            loader=0449A0E7
2078.9  DOMContentLoaded  loader=0449A0E7
2078.9  load              loader=0449A0E7
```

So enabling lifecycle events re-sends the CURRENT document's whole lifecycle,
with the name **`commit`** where a live navigation says **`init`**, under that
document's loaderId. The tool sends it immediately *before* `Page.navigate`, so
the replay always describes the page being LEFT — and keying commits on `init`
alone makes it impossible to read as a supersession.

### 2e. `net::ERR_ABORTED` has two meanings, and they need different answers

A URL serving `Content-Disposition: attachment`:

```
7.6 ms   frameStartedNavigating  /nav/download  loader=73995AFF
12.7 ms  RESPONSE  loader=73995AFF  errorText='net::ERR_ABORTED'
(nothing else in 3 s; href still about:blank)
```

Nothing commits, nothing fires, the tab does not move. The shipped code waited
the whole budget for a milestone that could never arrive.

But the displayed page navigating ITSELF away while our navigation is still
pending aborts it too — and there the replacement IS the answer. Six runs of
"navigate to a document the server answers in 2.5 s; the page leaves after
1.0 s":

| run | response | errorText | the replacement's `init` | landed on |
|---|---|---|---|---|
| 0 | 1006 ms | `net::ERR_ABORTED` | +10.8 ms | `/nav/landing?from=preempt` |
| 1 | 1002 ms | `net::ERR_ABORTED` | +10.7 ms | `/nav/landing?from=preempt` |
| 2–5 (earlier timing) | ~1200 ms | `net::ERR_ABORTED` | +11.6…+14.1 ms, or **before the response** in 2 of 6 | `/nav/landing?from=preempt` |

Worst measured gap **14.1 ms**; in two of six runs the replacement had already
committed when the abort response arrived — the same unordered delivery F-881
measured. A rule that raises immediately on `net::ERR_ABORTED` would call every
pre-empted navigation a download.

### 2f. Two `tab.evaluate` calls can straddle a navigation

Found by the real-Chrome node for shape (b) before it could ship. The
post-navigation read was

```python
final_url = await tab.evaluate("window.location.href")
title     = await tab.evaluate("document.title")
```

Two round trips. On the `meta refresh` shape the refresh fired between them and
the tool answered

```
{'success': True, 'url': 'http://127.0.0.1:60306/nav/meta-refresh', 'title': 'Nav Landing'}
```

— the FIRST document's url with the SECOND document's title, a record no
document ever had.

---

## 3. Fix (this PR)

**`embedded/navigation_milestone.py` — follow the FRAME's loader chain.**

* `_Chain(frame_id, milestone, progress, own)` is the whole rule. `own` is the
  loaderId `Page.navigate` answered with. Every `Page.lifecycleEvent` is
  buffered from before the send and folded in arrival order:
  * a different `frameId` is ignored outright (§2c);
  * a commit (`init`, never `commit` — §2d) for our frame under a loader that is
    not in the chain is appended, **but only after ours has committed** (or, when
    ours aborted and never will, as the head of the chain);
  * the milestone is satisfied when it has been seen for `loaders[-1]`, the
    LATEST document — re-evaluated after every single event, so the answer is the
    first instant at which it is true rather than a function of event batching.
* Ours always commits unless the answer was `net::ERR_ABORTED`: a non-aborted
  `errorText` commits its `chrome-error://` page under our own loaderId (F-881),
  so F-802/F-833's detector is untouched; a same-document navigation answers
  `loaderId: null` and returns at the response, unchanged.
* `net::ERR_ABORTED` seeds an EMPTY chain and waits `ABORTED_GRACE_SECONDS`
  (1.0 s — ~70× the 14.1 ms worst case of §2e) for the document that took our
  place, **measured from the response, not from the call** (the abort arrives a
  second or more in; a grace anchored at the call is already spent on arrival),
  and clipped to HALF the remaining budget (a grace that expires at the caller's
  own deadline loses the race to the enclosing `wait_for` and the named answer is
  replaced by a bare cancellation). Only a grace that passes with nothing in it
  is a download, and then a `ToolError` names it.
* `landing(tab)` is the post-navigation read: ONE `JSON.stringify` round trip for
  both fields (§2f), same discipline and same reason as `page_storage.READ_JS`.
* `Progress` grew `committed` and `superseded`, and `describe()` — the one
  phrasing of "what did this attempt get as far as".
* `networkidle` is unchanged in effect: its milestone IS `init`, so the chain is
  satisfied the moment our own document commits and no later supersession can
  reach it. It keys to the FIRST commit, deliberately — F-787 stays open and
  making `networkidle` outlast a redirect would be a new promise, not a kept one.

**`embedded/browser_manager.py`** keeps the policy and nothing else: the budget,
the retry decision (unchanged), the ONE call into `navigation_milestone.landing`,
and the failed-attempt warning, which now reads

```
Navigation attempt 1 failed for <id>: TimeoutError:  [accepted, committed, superseded by 1 later document(s)]
```

The raised `ToolError` carries the same clause. No field is the page's; the url
was already in the message and the three facts are ours.

**Not added:** any `STEALTH_MCP_*` knob, any `typing.Any`, any second wait.
`navigation_milestone.py` is still a leaf (`nodriver` + `tool_errors` + stdlib
`json`), and `browser_manager.py`'s LOC cap RATCHETS DOWN 1496 → 1493.

---

## 4. Verification

### Hermetic (`tests/test_navigate_milestone.py`, `tests/fakes.py`)

`FakeTab` grew the supersession model (`supersede_after` / `supersede_count` /
`supersede_last_milestone` / `supersede_url` / `title_after_supersede`), an
`iframe_loader` that delivers a whole set under a different `frameId`, a
`navigate_error` that models the abort — with and without a document taking its
place — and the enable-time replay, which it now delivers on EVERY tab because
every real navigation meets it.

13 new nodes. RED at `6ca0ae9` (src swapped to HEAD, tests unchanged):

```
FAILED test_a_document_replaced_before_its_load_answers_at_the_replacements
FAILED test_the_whole_chain_is_followed_not_just_the_first_replacement
FAILED test_domcontentloaded_is_followed_across_a_replacement_too
FAILED test_a_page_that_keeps_replacing_itself_still_times_out_and_says_so
FAILED test_the_failed_attempt_warning_carries_the_reason_not_an_empty_colon
FAILED test_a_download_is_named_after_the_grace_never_after_the_whole_budget
6 failed, 15 passed
```

(the three abort/grace nodes added after the real-Chrome run are not in that
count; `24 passed` on the fix.) The four guard nodes — subframe loader, the
enable-time replay, "our own `load` first", `networkidle` keys to the first
commit — are green on BOTH sides by design: they pin what must not change.

**Gate follow-up (PR #121, run 35138478980).** The last two of those guard
nodes were green locally (CPython 3.13) and red on every CI lane (3.11 / 3.12
/ 3.13 across the three platforms): the fake scheduled the replacement on the
loop with `call_soon`, and how many loop turns fall between our milestone
landing and the tool's landing read is the host's — `asyncio.wait_for` wraps
its awaitable in a Task on the older interpreters, which is one extra turn,
and the page had moved to `LANDING` by the time `landing()` read it.
Reproduced deterministically with the `68ce29f` files on CPython 3.11 (3/3
runs, exactly those two nodes). The fix is `FakeTab(supersede_held=True)`: the
replacement is HELD until the test calls `deliver_supersession()`, so the page
cannot move before the read regardless of scheduling, and a rule that keyed to
the replacement now has nothing to key to and times out — mutation-checked
(`networkidle → load` reds the `networkidle` node; "reached only once a
replacement is in the chain" reds both). `24 passed` on 3.11 and 3.13, 3/3
each, random order. The `tests/test_resilience.py` pin on the timeout message
is a SOFT golden and was updated in the same PR — see its docstring for the
justification: the suffix is the accepted/unaccepted line F-881 added, and a
pinned message with no reason was the empty-colon defect in another form.

### Real Chrome (`tests/test_e2e_navigation_truthfulness.py`, `@pytest.mark.integration`)

Eight nodes, one per shape, all driving the real tools against the local
`nav_*` fixture routes, with two oracles that are not the tool under test: the
page's own sentinel read through `execute_script`, and the fixture server's
ledger of which documents the browser actually fetched, read over plain HTTP.
Each node passes `timeout=8000` so a regression is an eight-second named failure
rather than a thirty-second one.

RED at `6ca0ae9`:

```
FAILED test_a_document_replaced_before_load_answers_about_the_replacement   8.03 s
FAILED test_a_js_challenge_lands_on_the_solved_document                     8.03 s
FAILED test_a_download_is_answered_at_once_and_leaves_the_tab_alone         8.04 s
FAILED test_a_pending_navigation_the_page_pre_empted_answers_about_the_winner 8.07 s
4 failed, 4 passed
```

— every failure consuming its whole budget, which is the defect itself. GREEN on
the fix: `8 passed in 35.70s`, slowest node body 1.78 s (the deliberate 1.6 s
slow-subresource wait), every other body ≤ 1.28 s.

---

## 5. Blast radius (what a caller saw)

Any site whose first document re-navigates before `load` — a head-script
`location.replace`, a JS challenge, a consent or region redirect implemented in
script — cost the caller the entire `timeout` and then raised, on a page that had
finished loading seconds earlier. Three of ten ordinary sites. Any URL that is a
download cost the same. The single durable record of either was a log line with
no reason in it.

---

## 6. Not claimed / deliberately unchanged / what remains

* **A document that reaches `load` before its replacement commits is still
  answered about at its own `load`** — shapes (b) and (c), the Amazon row. That
  answer is truthful at the instant it is made and the tab moves on a few
  milliseconds later; waiting past it would be a quiescence wait, which
  `navigate` does not promise and cannot bound. Pinned both hermetically
  (`test_a_document_that_loads_before_its_replacement_answers_at_its_own`) and on
  real Chrome. **A caller that needs "and it stopped moving" still has no tool
  for it**, and that is the honest residual of this finding.
* **F-787 (`networkidle` is a fixed sleep) stays OPEN and unchanged in effect**,
  and now explicitly keys to the FIRST commit (§3).
* **`ABORTED_GRACE_SECONDS` costs a real download 1 s.** That is the whole price
  of not calling a pre-empted navigation a download, against the 30 s it used to
  cost. It is a wall-clock constant and the only one this finding adds; it is
  justified from §2e and clipped to half the remaining budget.
* **A page that replaces itself forever still spends the caller's budget** and
  then raises — correctly, and now with `superseded by N later document(s)` in
  the message. There is no cap on chain length, deliberately: any cap would be a
  guess about how many redirects a real site is allowed.
* **The pre-emption outcome is timing-dependent in Chrome and both outcomes are
  handled.** Measured: when the page's own navigation is issued *before* ours,
  ours cancels it and wins (6/6); when it is issued while ours is pending, it
  cancels ours (6/6). The tool answers about whichever document committed. The
  E2E node fixes the timing (1.5 s margin) so it tests one of the two; the other
  is what shape (e) and the hermetic pins cover.
* **A `meta refresh` with a non-zero delay is not measured.** Only `content="0;…"`
  is; a delay long enough to land after the caller returned is the same residual
  as the first bullet.
* **Cross-origin iframes are not measured.** They are separate CDP targets and
  their lifecycle events never reach this connection at all; the frame filter is
  measured against a SAME-origin subframe (§2c), which is the harder case.
* **On the abort path the chain adopts the FIRST main-frame `init`, whatever it
  is.** With `own is None` there is no "after ours committed" gate to pass, so
  any commit in that frame during the grace becomes the chain head — including
  one belonging to a *concurrent* `navigate`/`reload_page` on the same tab rather
  than to the page that pre-empted us. Two tool calls moving one tab at once is
  a caller's race and is not serialized here (the same stance F-881 §6 takes for
  a concurrent `Tab.wait()`); the url and title answered would still be truthful
  about what the tab is showing, but the `success: true` would name a url the
  caller did not ask for. Left because the alternative — refusing to adopt
  anything on the abort path — is the 30 s timeout this finding exists to close.
* **`LANDING_JS` trusts the page's own `JSON.stringify`.** A page can replace it
  and author the `(url, title)` pair `landing()` returns, which is the input
  F-802/F-833's `chrome-error://` detector reads. This is a property of the
  idiom, not of this change: `page_storage.READ_JS` (F-869) has the identical
  exposure and for the identical reason — `Tab.evaluate` returns deep-serialized
  values raw, so a JSON string is the only shape that survives the transport.
  Recorded rather than fixed because a second way to read a url would be the
  defect this repo's fourth convention names, and because the honest fix is
  `Runtime.evaluate` with an isolated world, which is its own finding.
* **The `commit`-named replay is ignored, not consumed.** If a future Chrome
  emitted `init` for a replay, the chain would adopt the page being left. The
  measurement above is the only thing standing between those two readings, which
  is why it is recorded here and pinned in `FakeTab`.
* **F-881 §6's `net::ERR_ABORTED` bullet is now superseded**; that finding's §6
  carries a pointer here.
* **`graphify update .` was run in this worktree** — see the PR notes; the
  worktree has no `graphify-out/` of its own, so the index the main checkout
  keeps is what must be refreshed after merge.
