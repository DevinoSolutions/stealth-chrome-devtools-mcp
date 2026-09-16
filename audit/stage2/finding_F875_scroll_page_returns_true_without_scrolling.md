# F-875 — `scroll_page` returns `True` for a scroll that has not happened

**Status:** FIXED on `fix/F875-scroll-page-verified` (branched from `main` at `b0ae010`). Was OPEN: live on 2.1.6 and on `main` at `bb78878`. See §7 for what changed and what it measures now; §5 records why it was not folded into F-874's PR.
**Opened by:** live use of the 2.1.6 backend (2026-09-15): `scroll_page(direction="bottom")` on `stackoverflow.com/questions` returned `true` and `window.scrollY` read `0` immediately after
**Source at:** `origin/main` = `bb78878`
**Severity:** MEDIUM. Nothing raises. The tool's own docstring says the return is "True if scrolled successfully", and it is `True` in two cases where nothing was scrolled at all and one where the scroll is still in flight. A caller that reads elements after it reads the wrong viewport.

---

## 1. What the report claimed, and what is actually true

The report offered three candidate causes. **Measured, real headless Chrome 152,
product code path (`spawn_browser` → `navigate` → `scroll_page` → `execute_script`),
temp profile root, this worktree, 2026-09-15:**

| candidate | verdict |
|---|---|
| the scroll is applied to the wrong scrolling element | **NO.** Ruled out. |
| smooth scrolling had not finished when read | **YES**, partially — measured 70 % of the way on an 8016 px page. |
| the page had not rendered its content yet | **YES** — and this is what produces `scrollY` exactly `0`. |

### 1a. Not the wrong element

On both pages measured, `document.body.scrollHeight` and
`document.documentElement.scrollHeight` were **equal at every sample**
(977 / 2792 / 2858 / 2930 / 3376 / 3387 / 8016), `document.scrollingElement` was
`html`, and `overflow-y` was `visible` on both `html` and `body`. An `instant`
scroll through the product landed at exactly `maxScroll`:

```
instant_returned: true   (call took 0.109 s)
instant_immediately_after: scrollY 7039, maxScroll 7039     # 8016px data: page
```

So `window.scrollTo({top: document.body.scrollHeight})` reaches the bottom on these
pages. Rewriting it to `document.documentElement.scrollHeight` would change nothing
here. (That does not prove it for a page whose real scroller is a `div` — but it
does rule it out as the cause of THIS report.)

### 1b. Smooth returns before the scroll settles

`DOMHandler.scroll_page` ends with `await asyncio.sleep(0.5 if smooth else 0.1)`.
That is a fixed nap, not a settle. Measured:

```
# deterministic data: page, document 8016px, viewport 977px, maxScroll 7039
baseline                   scrollY 0
smooth_returned            true     (call took 0.505 s)
smooth_immediately_after   scrollY 4910     <-- 70 % of the way
smooth_after_3s            scrollY 7039     <-- arrived

# stackoverflow.com/questions, once actually loaded (maxScroll 1815)
so_smooth_returned         true
so_smooth_immediately_after  scrollY 1747   <-- 96 % of the way
so_smooth_after_3s           scrollY 1815
```

So `true` is a promise not yet kept, exactly as the report guessed — but it never
produces `0`.

### 1c. What produces exactly `0`: nothing to scroll

The first probe caught what the reporter almost certainly hit. `navigate` to
`stackoverflow.com/questions` returned `success: true` with
`title: "Just a moment..."` — Cloudflare's interstitial — and that document is
**exactly one viewport tall**:

```
before_scroll:       scrollY 0, docScrollHeight 977, innerHeight 977   # maxScroll = 0
scroll_returned:     true    (call took 0.503 s)
immediately_after:   scrollY 0
after_3s:            scrollY 0, docScrollHeight 2858                   # real page arrived
```

There was nothing to scroll, the script did exactly what it was told, and the tool
returned `true`. `scrollY == 0` is the honest state of that page; `true` is not an
honest answer about it.

---

## 2. Mechanism

`tool_sections/element_interaction.scroll_page` → `dom_handler.DOMHandler.scroll_page`
(`src/stealth_chrome_devtools_mcp/embedded/dom_handler.py`). The body builds one
`window.scrollBy`/`scrollTo` string, evaluates it, naps, and:

```python
await tab.evaluate(script)
await asyncio.sleep(0.5 if smooth else 0.1)

return True
```

`return True` is unconditional. It reports *the evaluate did not throw* and is
documented as *the page scrolled*. Nothing reads `window.scrollY` before or after,
so the tool cannot distinguish any of: it scrolled; it is still scrolling; the page
was not scrollable; the page was not laid out yet.

---

## 3. Blast radius

Every caller of `scroll_page` (default `smooth=True`, default budget 0.5 s). The
larger the page, the larger the shortfall — the 8016 px measurement above landed
2129 px short. Lazy-loading pages are the common case for wanting `bottom` at all,
and those are exactly the pages whose height is still growing when the nap ends.

---

## 4. Proposed remedy (implemented — see §7)

The honest answer is not a `bool`. Sketch, for the PR that takes this:

* poll `window.scrollY` on a bounded budget until it stops changing (or the target
  is reached), instead of a fixed nap — a settle, not a sleep;
* return a record: the position reached, `max_scroll`, and whether it settled, so
  "the page is not scrollable" and "we ran out of budget mid-scroll" are
  distinguishable from "arrived";
* keep raising `ToolError` only for a genuine failure (convention 2) — "nothing to
  scroll" is an answer, not a failure, so it must not become a `False` that reads
  as one.

That is a tool schema change: a deliberate `tests/goldens/tool_surface.json`
regeneration, its own hermetic pins, and its own real-Chrome pin.

---

## 5. Why this is not in F-874's PR

F-874's change is "one home for *what page is this tab showing*". This one is
"a return value that claims more than it checked" in a different tool, a different
module (`dom_handler`), with a different remedy and its own golden update. Folding
it in would put two unrelated schema changes behind one review.

---

## 6. Not claimed

* **Not measured:** a page whose real scroller is a nested element (`body`
  `overflow:hidden`, a scrolling `div`). `document.body.scrollHeight` would be
  wrong there, but that is a separate hypothesis and this report is not evidence
  for it. **Still true after the fix, and now VISIBLE rather than silent** — see
  §7.5, which is now a pointer: it was measured and closed as **F-878**.
* **Not measured:** `direction` values other than `bottom`. The `return True` is
  unconditional for all of them, so the truthfulness defect is shared; the
  magnitude is not. *(The fix is per-direction by construction: the record
  reports the axis the requested direction moves, and `up`/`top`/`left`/`right`
  are pinned hermetically.)*
* The Cloudflare interstitial is not itself a product defect — `navigate` returned
  truthfully about the document Chrome had committed. It is the reason a caller
  reaches a settled-looking page that is one viewport tall.

---

## 7. What changed

Branch `fix/F875-scroll-page-verified`. The nap became a **settle** and the bool
became a **record**, exactly as §4 sketched.

### 7.1 The new leaf — `embedded/scroll_position.py`

THE one home for "where is this page scrolled, and has it stopped". It owns:

* **`READ_JS` / `read`** — the scroll offsets AND the extent in ONE
  `JSON.stringify` round trip. `JSON.stringify` for F-869's and F-872's reason:
  `Tab.evaluate` always sends `serialization="deep"` and returns the value raw,
  so an object literal arrives as BiDi `RemoteValue` nodes while a string
  arrives intact. The extent is measured off `document.scrollingElement` — the
  element CSSOM View says `window.scrollTo`/`scrollBy` actually move, which §1a
  measured as `html` on every sampled page. Offsets are `Math.round`ed, because
  sub-pixel positions are real (zoom, HiDPI, the tail of a smooth animation) and
  a settle comparing floats would never see two reads agree.
* **`settle`** — waits for the PAGE to say the scroll finished, bounded by
  `SETTLE_BUDGET_SECONDS = 10.0`. The bound is argued from both sides: it is
  roughly 3× the worst smooth scroll §1b measured (which arrived between the
  0.5 s and 3 s samples), and it is a THIRD of `CDP_OPERATION_TIMEOUT` (30 s),
  so a scroll that outlasts it is **reported** as `settled: false` rather than
  raised as a CDP timeout — which is the whole point of the finding. What ends
  the wait is `scrollend`, not the reads; §8 is the measurement that forced
  that, and it is the one thing in this fix that a first attempt got wrong.
* **`SCROLL_JS` / `start`** — the scroll and the arming of the `scrollend` latch
  in ONE round trip, which also answers `moves` (will this change the offset at
  all, computed from the clamped target synchronously) and `supported` (does
  this browser have `onscrollend`). `moves` is what tells `settle` not to wait
  for an event that will never fire, and it replaced an `at_edge` guess with the
  page's own exact answer.
* **`_DIRECTIONS` / `script` / `Position.at_edge`** — ONE table for what a
  direction means (its axis, the edge it heads for, and the JS that goes there),
  not a script table beside an edge table. It also retires a latent bug the old
  ladder had: `-{amount}` turned a negative `amount` into JS's decrement
  operator (`--500`), a syntax error; the template interpolates a pre-negated
  value now.

A leaf: `tool_errors` only, tab as an argument, `_now`/`_sleep` as its single
timing seam (the `scheduling_lag` pattern). It never decides whether a scroll
*succeeded* — only where the page is and whether it has stopped.

### 7.2 The record

`DOMHandler.scroll_page` (and the `scroll_page` tool) return:

| field | means |
|---|---|
| `scrolled` | the scroll OFFSET changed between before and after |
| `at_edge` | the page is as far as `direction` goes (true for `max_scroll_y == 0`) |
| `settled` | the OFFSET stopped changing inside the budget |
| `settle_seconds` | what the settle actually cost |
| `direction` / `amount` / `smooth` | what the caller ASKED for (`amount` is ignored by `top`/`bottom`, and is echoed as the request, not as a distance travelled) |
| `scroll_x_before` / `scroll_y_before` | where the page was |
| `scroll_x_after` / `scroll_y_after` | where it ended up |
| `max_scroll_x` / `max_scroll_y` | how far it could go |

Both axes are reported because `direction="right"` moves X, and a Y-only record
would call a working horizontal scroll a no-op. The record deliberately does
**not** use §4's bare `max_scroll`: next to `max_scroll_x` that name would read
as ambiguous, so the pair is symmetric.

**Offsets and extent are never compared together.** `scrolled` compares
`Position.offset` — `(x, y)` — and so does the settle's "two consecutive reads
agree". A lazy-loading page grows its document while standing perfectly still,
so comparing whole readings is wrong in both directions at once: content
appended below a stationary viewport reads as "it scrolled" (with identical
`scroll_y_before` and `scroll_y_after` in the same record), and a page that
stopped moving but is still filling in never agrees with itself, spends the
whole 10 s budget on every scroll and then reports `settled: false` about a
viewport that has not moved since the first 100 ms. The EXTENT the caller gets
is the final read's, which is the freshest one there is.

`max_scroll_y == 0` is an answer, never a raise — §4/§5's explicit requirement.
`ToolError` is still raised only for operational failure, and it is raised
ONCE: `scroll_position.script`/`read` already speak the error convention, so
`scroll_page` re-raises a `ToolError` unchanged and keeps its blanket handler
(now `raise ... from e`) for everything else. Re-wrapping doubled the sentence
("Failed to scroll page: Invalid scroll direction: …") and dropped the cause.
The two rejections both happen before any round trip:

* **an invalid direction**, and
* **a negative `amount`** — `amount` is a distance and `direction` is the only
  thing that carries a sign. Before this fix a negative amount had two readings
  and both were wrong: `down` with `-500` silently scrolled UP, and `up` with
  `-500` interpolated `top: --500` and died as a JS syntax error. Interpolating
  a pre-negated value (which is how the `--500` syntax error was closed) would
  have turned the loud half into the silent half — `direction: "up"` in a record
  about a page that went down — so it is refused instead.

An evaluate that did not answer with the JSON the read asks for is the third
operational failure; reporting `scroll_y: 0` for a read that did not happen is
this same class of untruth. Messages report shape and count only — a type name,
a character count, a field count.

**`at_edge` carries one pixel of slack on the FAR edge**
(`EDGE_TOLERANCE_PX`). `Math.round(window.scrollY)` and an already-integer
`scrollHeight - clientHeight` round independently, so under fractional zoom or a
non-integer device pixel ratio a page resting at its true bottom can read one
pixel short of it. The near edge needs none: `scrollY` is never below zero.

### 7.3 Measured with the fix

Real Chrome 152, headless, Windows 11, an 8000 px `data:` page in a 977 px
viewport (`max_scroll_y` 7023), through the product path
`spawn_browser → navigate → scroll_page`:

```
smooth bottom      wall 1.594 s   settle 1.592 s   y 7023/7023   scrolled settled at_edge
smooth top         wall 1.584 s   settle 1.581 s   y    0/7023   scrolled settled at_edge
instant bottom     wall 0.126 s   settle 0.124 s   y 7023/7023   scrolled settled at_edge
one viewport tall  wall 0.119 s   settle 0.117 s   y    0/0      scrolled=False at_edge settled
```

So the smooth case takes the ~1.6 s it actually needs instead of answering at
0.5 s and 70 % of the way, and the instant fast path costs 0.126 s against the
0.109 s §1a measured for the old fixed nap — two extra round trips (the before
and after reads) for an answer that is true.

### 7.4 Tests and goldens

* `tests/test_scroll_page_verification.py` — 15 hermetic pins, driven through
  `fakes.ScrollingTab`, a new double that models the document's own scroll
  geometry: clamped `scrollTo`/`scrollBy`, a **JSON-string** read answer, a
  smooth animation advanced one step per POSITION READ (so the mid-flight case
  is deterministic without a clock), and a `growing_content` mode that appends
  document without moving the viewport — the one shape that tells an offset
  comparison from a whole-reading one. Nothing in the double is copied from the
  defect. Measured against that double, the whole-reading comparison answers
  `scrolled: True` with `Position(y=0, max_y=8039)` before and
  `Position(y=0, max_y=10039)` after, and the offset comparison answers
  `False`; the settle returns in 0.065 s instead of spending the budget.
* `tests/test_e2e_scroll_page_verification.py` — 2 real-Chrome pins on `data:`
  pages under `tmp_empty_root`, cross-checking the record against the page's own
  `window.scrollY` and against Chrome's own `document.scrollingElement` extent.
* `tests/goldens/tool_surface.json` (HARD) — one tool, deliberate: `scroll_page`'s
  `output_schema` moves from FastMCP's `_WrappedResult` `{result: boolean}` to
  the `{type: object, additionalProperties: true}` every other dict-returning
  tool already serves, and the description carries the record. Input schema
  untouched; no other tool moved. §4 named this regeneration as the cost.
* Two characterization pins moved, each with a comment saying why:
  `test_e2e_interaction.py` asserted the bare truthiness of the return (a record
  is truthy whatever it says) and now reads the fields;
  `test_e2e_dynamic_sites.py` asserted `is True` in front of an
  IntersectionObserver check that a non-scroll would have turned into a false
  pass, and now asserts `scrolled is True`.

### 7.5 What this does NOT fix — **taken up and closed as F-878**

§6's first bullet stood when this was written: a page whose real scroller is a
nested element (`body` `overflow: hidden` + a scrolling `div`) was still not
scrolled by `window.scrollBy`/`scrollTo`, and `document.scrollingElement`'s
extent is not that div's. What THIS fix changed is that the tool no longer LIES
about it — such a page answered `scrolled: false`, `max_scroll_y: 0`,
`at_edge: true` instead of `true`, so the caller could see it and reach for
`execute_script`.

The named follow-up — *which element is "the" scroller when several overflow?* —
is **`audit/stage2/finding_F878_scroll_page_nested_scroller.md`**, and it is
FIXED. It answered the question with twelve `data:` fixtures measured through
the product path against real Chrome 152 (on which `document.scrollingElement`
alone is right 4 times out of 12, the two obvious heuristics 9 and 8, and the
document-first + largest-viewport-clipped-area rule that shipped 12/12), and it
extended THIS finding's leaf rather than adding a second path: `scroll_position`
picks the scroller once per call and `dom_handler.scroll_page` reports it as
`scroller` / `scroller_is_document`. Rule 1 of that rule — if the document
scroller can move, it IS the page — is what keeps everything measured here
unchanged: the same element is driven by the same `window.scrollTo` /
`window.scrollBy` call, and the `scrollend` listener is still armed on
`window`. (F-878 measured why that last clause has to be said out loud: an
ELEMENT scroll's `scrollend` fires at the element and does not bubble to
`window`, while a DOCUMENT scroll's is never dispatched at
`document.scrollingElement` — so §8's latch has one right target per scroller
kind, and F-878 arms it on whatever received the scroll.) F-878 §6 carries
what IT does not claim (iframes, shadow DOM, a page whose only scroller is
tiny).

---

## 8. The settle's first stop condition was wrong, and CI caught it

§7's first implementation stopped the settle when **two consecutive reads agreed
on the offset**. That is not a stop condition; it is a guess about timing, and
the full gate on PR #113 failed on it — **run 35046780659, job 104638194821,
macOS/ARM64** (Windows and Linux passed), inside this finding's own verification:

```
tests/test_e2e_scroll_page_verification.py::test_a_smooth_scroll_is_reported_where_it_landed
    assert record["scroll_y_after"] == await eval_js(...)
E   assert 3498 == 4898
```

The record described a position the page had already left — the same class of
untruth this finding exists to retire, now committed by the fix.

### 8.1 The mechanism, measured

A plain document smooth scroll runs on Chrome's **compositor** thread, while
`window.scrollY` is read on the **main** thread and only advances when a frame
commits to main. Block the main thread and the reads go stale while the scroll
keeps going. Reproduced on this machine (Chrome 152 headless, Windows 11) by
blocking the renderer's main thread for a fixed slice out of every 5 ms and
polling at the product's own `POLL_INTERVAL_SECONDS`:

| main-thread long task | longest run of AGREEING mid-flight reads |
|---|---|
| none | **0 ms** (6 runs, ~78 mid-flight read pairs) |
| 120 ms | 121 ms (2 reads) |
| 250 ms | 250 ms (2 reads) |
| 400 ms | 400 ms (2 reads) |

**The false-agreement window is exactly as long as the long task.** A long task
is unbounded, so no count of agreeing reads and no fixed quiet window can be
correct against it — both were considered and both are defeated by a long enough
stall. The idle machine never reproduced it, which is why the CI cell saw it
first: a loaded macOS/ARM64 runner is where a multi-hundred-millisecond renderer
stall is ordinary.

### 8.2 The fix: ask the page, do not infer from timing

`SCROLL_JS` now arms a one-shot `scrollend` listener **in the same round trip
that performs the scroll** (so there is no window where the scroll could finish
before anything was listening), and `READ_JS` reports that latch alongside the
offsets — the same round trip, so a finished flag can never be paired with a
stale offset. `scrollend` fires when the scroll position has finished changing,
including at the end of a compositor-driven smooth scroll, and it **latches**:
jank can only delay our observation of it, never make a running scroll look
finished. Measured across the same jank levels, `ended` was first observed at the
true final position (7023/7023) **every time, never early**.

The one case that must not wait for it is a scroll that moves nothing — a page
already at the requested edge, or with nothing to scroll — because that fires no
`scrollend` at all. So `SCROLL_JS` also answers `moves`, computed synchronously
from the clamped target before any frame, measured correct on all six shapes
(from the top, already at the bottom, `scrollBy` at the bottom, to the top,
already at the top, and a one-viewport page). That exact answer replaced the
`at_edge` guess the first implementation used. Feature detection is
`'onscrollend' in window` — measured `true` on Chrome 152, where
`'scrollend' in window` is `false`, the event not being an own property of
`window`; without it the settle falls back to read agreement, which is weaker but
is all there is.

### 8.3 Measured after the rewrite

Same page and viewport, through the product path, with the tool's answer checked
against an independent `Math.round(window.scrollY)` immediately afterwards:

```
             idle                     renderer janked 250 ms / 5 ms
smooth bottom  1.477 s  7023/7023  OK   0.512 s (already there)   OK
smooth top     1.589 s     0/7023  OK   2.100 s     0/7023        OK
instant bottom 0.124 s  7023/7023  OK   0.262 s  7023/7023        OK
one viewport   0.119 s     0/0     OK   0.515 s     0/0           OK
```

`OK` = the record's `scroll_y_after` equals the page's own live read. It does in
**every** case now, including under the jank that produced the CI failure. The
fast path is unchanged (0.124 s instant, 0.119 s one-viewport, against F-875's
0.126 s), because a scroll that moves nothing and an instant scroll both answer
from the latch or the no-op flag rather than waiting.

### 8.4 The pin, and that it is load-bearing

`tests/test_scroll_page_verification.py::test_a_mid_flight_stall_is_not_a_finished_scroll`
drives a `ScrollingTab` whose position repeats for three reads mid-flight and
then resumes (`stall_at` / `stall_reads`), modelling exactly the measured
renderer stall. Reverting the stop condition to read agreement while leaving the
pin alone:

```
NEW (wait for the page)    scroll_y_after=7039 of max=7039  settled=True  at_edge=True   -> pin PASSES
OLD (two agreeing reads)   scroll_y_after=880  of max=7039  settled=True  at_edge=False  -> pin FAILS
```

880 of 7039, reported as `settled: True` — the CI shape (3498 of 7039)
reproduced hermetically. A second pin,
`test_without_scrollend_the_settle_falls_back_to_read_agreement`, keeps the
no-`onscrollend` path alive so it degrades rather than hanging for the budget.
