# F-881 — `navigate(wait_until="load")` returns before the page has loaded

**Status:** FIXED in this PR (product defect; live on 2.1.6 and on `main` at `3311be9`)
**Opened by:** two Windows/X64 full-gate failures on unrelated PRs (runs
35046485374 and 35055359548), both
`tests/test_browser_integration.py::TestListInstancesLiveState::test_live_title_url_and_tab_switch`
at line 996 — `assert nav["title"] == "Alpha"` → `AssertionError: assert '' == 'Alpha'`.
macOS and Linux green; several other Windows gates green.
**Source at:** `origin/main` = `3311be9`
**Severity:** MEDIUM-HIGH. `navigate` is the tool every session starts with, and
`wait_until` is the one promise it makes about *when* it answers. It does not keep
it: for `"load"` and `"domcontentloaded"` the wait is a no-op, so the tool reads
`window.location.href` / `document.title` — and returns control to a caller that
will immediately click, type or extract — while the parser may still be running.
On a fast machine a 0.5 s dead sleep hides it; on a loaded CI runner it showed.

This finding was opened under the working title "`list_instances` title lags
target info" and the premise that `tab_identity.refreshed`'s `Target.getTargets`
read was the stale one. The measurement below says otherwise: the assertion that
failed is `navigate`'s own answer, three tool calls before `list_instances` is
asked anything, and `list_instances` is downstream of the same navigation.
`tab_identity` is untouched.

---

## 1. What was observed

The test navigates a fresh headless Chrome to
`data:text/html,<title>Alpha</title><h1>A</h1>` with the tool's defaults
(`wait_until="load"`) and asserts the title the tool itself returned. Twice on
Windows the tool returned `title: ""` for a document whose `<title>` is in the
first 30 bytes of its own URL.

---

## 2. Mechanism (measured, Chrome 152 headless, nodriver 0.47.0, Windows 11)

`BrowserManager.navigate` does, in order:

1. `await asyncio.wait_for(tab.get(url), timeout)` — nodriver's `Tab.get`:
   `await self.send(cdp.page.navigate(url))`, then `await self` → `Tab.wait()`.
2. `await self._wait_for_navigation_condition(tab, wait_until, remaining)`.
3. `tab.evaluate("window.location.href")`, `tab.evaluate("document.title")`.

### 2a. The `load` wait is a no-op

Step 2 for `"load"` is `tab.wait(uc.cdp.page.LoadEventFired)`. nodriver 0.47's
`Tab.wait(self, t: Union[int, float] = None)` takes a *duration*; its body is
`if not t: t = 0.5; await asyncio.wait([...])`. A class is truthy, so the whole
wait is skipped and the call returns after registering and un-registering five
handlers. The `"domcontentloaded"` branch is the same call with
`DomContentEventFired`. Measured across 30 navigations: **0.01–0.06 ms**.

The line dates from the initial vendoring (`73749a2`) and has never waited.
F-787's statement that "the other two conditions are real: `domcontentloaded`
awaits `Page.domContentEventFired` and the default `load` awaits
`Page.loadEventFired`" was a reading of the source, not a measurement, and is
false.

### 2b. What `tab.get` actually waits for

`Tab.wait()` with no duration waits ≤ 0.5 s for the FIRST of `FrameStoppedLoading`,
`FrameDetached`, `FrameNavigated`, `LifecycleEvent`, `LoadEventFired` — and then
**deletes every handler for those five events** (`Connection.remove_handler` is
`del self.handlers[evt]`), which is also F-824's `KeyError` race.

In the shipped product nothing has enabled the `Page` domain on the tab (nodriver
enables a domain only when a handler for one of its events is registered at the
moment of a `send`, and `wait()` registers its handlers after the send and sends
nothing), so **no event ever arrives and the wait is a flat 0.5 s sleep**:

| `navigate` tool, unmodified product, 30 trials, `data:` page | |
|---|---|
| wall time per call | 521.5 – 528.7 ms, every trial |
| `title` correct | 30 / 30 |

That 0.5 s is the only reason the title is ever right: the parser has half a
second to reach `<title>` before `document.title` is asked. It is dead time on
every navigation a caller makes, and it is not a guarantee — the two CI failures
are the runner taking longer than that to schedule the renderer's parse.

### 2c. Where the title comes from, and when it exists

Probe through the product path (spawn via `tool_sections.browser_management`,
real headless Chrome, temp session root), with `Page` enabled by the probe's own
handlers so `tab.get` returns on `FrameNavigated` (commit) as it would after this
fix. Time zero is the instant `tab.get` returned; 30 trials, one Chrome:

| milestone after `tab.get` returned | min | p50 | p90 | max |
|---|---|---|---|---|
| `_wait_for_navigation_condition("load")` returned | 0.01 ms | 0.02 ms | 0.03 ms | 0.06 ms |
| `Page.frameNavigated` (commit) | −0.1 ms | −0.1 ms | 0.0 ms | 0.0 ms |
| `Page.domContentEventFired` | 7.7 ms | 9.95 ms | 11.7 ms | 14.5 ms |
| `Page.loadEventFired` | 7.7 ms | 10.0 ms | 11.8 ms | 14.5 ms |
| `document.title` (`Runtime.evaluate`) == expected | 8.0 ms | 10.85 ms | 12.7 ms | 15.4 ms |
| `Target.getTargets` title == expected | 8.3 ms | 12.2 ms | 16.0 ms | 47.0 ms |

Reading: `tab.get` returns AT commit; the document is parsed and `load` fires
~8–15 ms later; the first `document.title` round trip lands ~1 ms after `load`
(the evaluate queues behind the parse on the renderer main thread); target-info
title trails the DOM title by ~1–3 ms (one outlier at 47 ms). On this machine the
evaluate always lost the race to the parser, so `document.title` was never empty
— but that is scheduling luck on a fast box, and the CI runner did not have it.

So of the two hypotheses this finding was opened with:

* **(a) "the test asks before the title has propagated to target info"** — no:
  target info was never consulted by the failing assertion.
* **(b) "the product should read `document.title` rather than target info"** —
  no: the product already reads `document.title`, and it was empty because the
  page had not parsed its `<title>` yet.

It is **(c): `navigate` reads the page at commit, not at load, because its
`wait_until` wait has never waited.** At the instant it asked, `''` was the honest
`document.title`; the instant was wrong.

### 2d. What `Page.lifecycleEvent` says, and why the fix keys on `loaderId`

Same setup, `Page.setLifecycleEventsEnabled(true)`, four navigation shapes
(events timed from the `Page.navigate` send):

| shape | `Page.navigate` response | `init` (commit) | `DOMContentLoaded` | `load` |
|---|---|---|---|---|
| `data:` page (trial 1) | +62.2 ms, `loaderId=7F17…` | +61.7 | +61.9 | **+62.5** |
| `data:` page (trial 3) | +9.9 ms, `loaderId=37C4…` | +11.2 | +19.3 | +19.4 |
| unresolvable host | +21.3 ms, `errorText=net::ERR_NAME_NOT_RESOLVED`, `loaderId=9862…` | +36.8 (`chrome-error://chromewebdata/`) | +51.6 | +54.1, **same loaderId** |
| same document (`…#frag`) | +1.2 ms, **`loaderId=None`** | — | — | — (no lifecycle event at all) |

Three facts the fix is built on:

1. The `Page.navigate` response and the new document's events are **not ordered**
   — in trial 1 `DOMContentLoaded` preceded the response and `load` followed it by
   0.3 ms. A wait that arms its listener after the response can miss the event
   and then waits the whole budget; a wait that arms it before the send and
   remembers what it saw cannot.
2. A failed navigation **commits an error page under the same `loaderId`** and
   fires `load` for it, so F-802/F-833's `chrome-error://` detector keeps working
   unchanged once the wait is real.
3. A same-document navigation answers **`loaderId: null`** and fires nothing;
   there is nothing to wait for and the tool must return at the response.

An older document's `load` (a page still loading when `navigate` is called) can
arrive while the listener is armed; keying on the `loaderId` the response names
is what makes it not count.

---

## 3. Fix (this PR)

**One home for "this navigation reached the milestone the caller asked for".**
New leaf `embedded/navigation_milestone.py`:

* `MILESTONES` — `wait_until` → the `Page.lifecycleEvent` name that marks it:
  `load` → `"load"`, `domcontentloaded` → `"DOMContentLoaded"`,
  `networkidle` → `"init"` (commit) followed by F-787's fixed sleep, moved here
  byte-for-byte and still not a quiescence wait.
* `navigate(tab, url, wait_until)` — arms ONE `LifecycleEvent` handler, enables
  lifecycle events, sends `Page.navigate`, and returns when the event named for
  **that response's `loaderId`** has been seen — whether it arrived before the
  response or after. `loaderId: null` returns at once. An unknown `wait_until`
  raises `ToolError` naming the three accepted values instead of silently meaning
  `load`.
* The handler is removed in a `finally` through nodriver's own
  `remove_handler`, tolerating the `KeyError` a concurrent `Tab.wait()` leaves.

`BrowserManager.navigate` makes one call under its existing budget —
`asyncio.wait_for(navigation_milestone.navigate(tab, url, wait_until), timeout)`
— and reads `window.location.href` / `document.title` after it. `tab.get` and
`_wait_for_navigation_condition` are gone from this path: the first was a
0.5 s sleep, the second a no-op. Nothing else calls `Tab.get` in the tree.

**Cost.** One `Page.setLifecycleEventsEnabled` per navigation (~1 ms; nodriver
sends `Page.enable` ahead of it the first time). A navigation to a `data:` page
now answers in ~20 ms instead of ~525 ms, and answers *after* `load`.

---

## 4. Verification

Hermetic — `tests/test_navigate_milestone.py`. `tests/fakes.py`'s `FakeTab` now
models a navigation the way Chrome answers one (built from §2d): `send(Page.navigate)`
answers `(frameId, loaderId, errorText)`, delivers `init`/`DOMContentLoaded`/`load`
lifecycle events to registered handlers either BEFORE the response (`lifecycle="before"`),
AFTER it on the loop (`"after"`), or never (`"never"`), and a `title_at_load=`
page answers `document.title` as `""` until its `load` has been delivered — the
measured Amazon/CI shape. RED at `3311be9` for the defect and nothing else:

| pin | RED reason at `3311be9` |
|---|---|
| `navigate` reports the title the page has at load, not at commit | `assert '' == 'Alpha'` — the CI failure, reproduced without Chrome |
| a `load` that arrived before `Page.navigate` answered still counts | `assert '' == 'Alpha'` |
| `domcontentloaded` returns on that event and does not wait for `load` | `assert '' == 'Alpha'` (`title_at_dcl`) |
| the milestone is keyed on the response's `loaderId` — an older document's `load` does not end the wait | passes at HEAD only because HEAD never waits; it pins the fix's shape |
| a same-document navigation returns at the response | shape pin |
| an unknown `wait_until` raises naming the accepted values | `DID NOT RAISE` |
| `networkidle` still sleeps F-787's fixed window after commit | shape pin |

The F-824 pins (`tests/test_navigate_race_recovery.py`) and the referrer pins
(`tests/test_extra_headers_cdp.py`) are re-pointed from `tab.get` to the
`Page.navigate` send, which is where the navigation now leaves the product;
their assertions are unchanged in meaning.

Real Chrome — `tests/test_browser_integration.py::TestListInstancesLiveState`,
unchanged, run five times in a row; and `tests/test_resilience.py`'s
navigation nodes (hang-before-headers timeouts for all three `wait_until`
values, the F-787 characterization at the hang-after-headers route, the error-page
landing) — see the PR for counts.

**LOC:** `browser_manager.py` 1528 → below its cap: `_wait_for_navigation_condition`
left and one call replaced two; the cap ratchets DOWN to the new actual.

---

## 5. Blast radius (what a caller saw)

Every `navigate` call, on every platform, answered ~0.5 s late and *before* the
page had loaded: `title` could be `""` (Amazon's late title in F-874 row 2 was in
part this), a caller's next `click_element`/`query_elements` ran against a page
still being parsed (which is what `tests/e2e_helpers.navigate_and_settle`'s poll
was papering over), and `wait_until="domcontentloaded"` bought nothing at all.
Nothing raised, nothing logged.

---

## 6. Not claimed / deliberately unchanged / what remains

* **`tab_identity` is untouched.** Target-info title trails `document.title` by
  ~1–3 ms (§2c) and both are after `load`; `list_instances`' `title` was not the
  value that failed. Reading `document.title` there would add a per-instance
  `Runtime.evaluate` to answer a lag this finding could not observe mattering.
* **F-787 (`networkidle` is a fixed sleep) stays OPEN and unchanged in effect.**
  The sleep moved into `navigation_milestone` so `wait_until` has one home; it is
  now keyed to the committed document (`init`) instead of to `tab.get`'s 0.5 s,
  and `Page.lifecycleEvent`'s own `networkIdle` is one table row away when that
  finding is taken up. Its characterization pin is unchanged.
* **`net::ERR_ABORTED` (a download, or a navigation superseded before commit)
  commits nothing and fires nothing**, so the wait now runs to the budget and
  raises the existing timeout `ToolError`. Before, it answered `success: true`
  with the *previous* page's url and title. Honest, but slow; a dedicated
  answer for downloads is its own finding.
* **A concurrent `Tab.wait()` on the same tab** (`go_back`/`go_forward`/
  `reload_page`/`new_tab` still use it) deletes every `LifecycleEvent` handler on
  exit — nodriver's `remove_handler`, F-824's race. If it fires mid-wait the
  navigation waits out its budget rather than answering wrongly. Two tool calls
  moving one tab at once is a caller's race and is not serialized here.
* **`go_back`/`go_forward`/`reload_page`/`new_tab` still settle through
  `await tab`** (`_require_landing_ok`). They promise no `wait_until`, and their
  0.5 s floor is F-833's; unchanged.
* **The e2e test is unchanged.** Its premise — `navigate(wait_until="load")`
  answers with the loaded page's title — was right; the product was not keeping it.
* **The `Page` domain is now enabled on every tab that navigates**, and stays
  enabled (nodriver never sends `Page.disable`). A consumer that later relies on
  `Tab.wait()` gets real events instead of a 0.5 s sleep; no such reliance is
  known.
