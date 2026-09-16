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
  §7's "what this does not fix".
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
* **`settle`** — polls `read` until two consecutive answers agree, bounded by
  `SETTLE_BUDGET_SECONDS = 10.0`. The bound is argued from both sides: it is
  roughly 3× the worst smooth scroll §1b measured (which arrived between the
  0.5 s and 3 s samples), and it is a THIRD of `CDP_OPERATION_TIMEOUT` (30 s),
  so a scroll that outlasts it is **reported** as `settled: false` rather than
  raised as a CDP timeout — which is the whole point of the finding.
  `START_GRACE_SECONDS = 0.3` is the one subtlety: a smooth scroll begins on the
  next animation frame, so for the first frames "has not started" and "will
  never move" are the same reading, and settling on it would have replaced one
  lie with another. A reading equal to the ORIGIN may not settle before the
  grace; the caller passes `start_grace=0` when the page is already at the
  requested edge, because then nothing will move and there is nothing to wait
  for. That is what keeps a one-viewport page on the two-read fast path.
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
| `scrolled` | the position CHANGED between before and after |
| `at_edge` | the page is as far as `direction` goes (true for `max_scroll_y == 0`) |
| `settled` | the position stopped changing inside the budget |
| `settle_seconds` | what the settle actually cost |
| `direction` / `amount` / `smooth` | what the caller ASKED for (`amount` is ignored by `top`/`bottom`, and is echoed as the request, not as a distance travelled) |
| `scroll_x_before` / `scroll_y_before` | where the page was |
| `scroll_x_after` / `scroll_y_after` | where it ended up |
| `max_scroll_x` / `max_scroll_y` | how far it could go |

Both axes are reported because `direction="right"` moves X, and a Y-only record
would call a working horizontal scroll a no-op. The record deliberately does
**not** use §4's bare `max_scroll`: next to `max_scroll_x` that name would read
as ambiguous, so the pair is symmetric.

`max_scroll_y == 0` is an answer, never a raise — §4/§5's explicit requirement.
`ToolError` is still raised only for operational failure: an invalid direction
(rejected before any round trip, so `tests/test_error_typing.py`'s pin is
unchanged) and an evaluate that did not answer with the JSON the read asks for
(reporting `scroll_y: 0` for a read that did not happen would be this same class
of untruth). Messages report shape and count only — a type name, a character
count, a field count.

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

* `tests/test_scroll_page_verification.py` — 11 hermetic pins, driven through
  `fakes.ScrollingTab`, a new double that models the document's own scroll
  geometry: clamped `scrollTo`/`scrollBy`, a **JSON-string** read answer, and a
  smooth animation advanced one step per POSITION READ, so the mid-flight case
  is deterministic without a clock. Nothing in the double is copied from the
  defect.
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

### 7.5 What this does NOT fix

§6's first bullet stands: a page whose real scroller is a nested element (`body`
`overflow: hidden` + a scrolling `div`) is still not scrolled by
`window.scrollBy`/`scrollTo`, and `document.scrollingElement`'s extent is not
that div's. What has changed is that the tool no longer LIES about it — such a
page now answers `scrolled: false`, `max_scroll_y: 0`, `at_edge: true` instead
of `true`, so the caller can see it and reach for `execute_script`. Making
`scroll_page` find and drive a nested scroller is a separate change with its own
evidence requirement (which element is "the" scroller when several overflow?)
and is the named follow-up from this finding.
