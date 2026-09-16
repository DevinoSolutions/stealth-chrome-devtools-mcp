# F-878 — `scroll_page` cannot move a page whose real scroller is a nested element

**Status:** FIXED on `fix/F878-scroll-page-nested-scroller` (branched from
`fix/F875-scroll-page-verified` at `3287128`). See §5 for what changed and §6 for
what it still does not claim.
**Opened by:** F-875 §7.5 — the named follow-up. F-875 §6's first bullet was
"not measured: a page whose real scroller is a nested element", and its fix made
that page *visible* (`scrolled: false`, `max_scroll_y: 0`) rather than silently
wrong. This finding is the measurement it asked for.
**Source at:** `fix/F875-scroll-page-verified` = `3287128`
**Severity:** MEDIUM. Nothing raises, and since F-875 nothing lies. But the
`body{overflow:hidden}` + scrolling-`div` "app shell" is the default layout of
every SPA framework's starter template, and on such a page `scroll_page` cannot
move the content at all — for any of the six directions. The caller's only
recourse today is `execute_script`, which means the tool is unusable exactly
where infinite feeds, virtualised lists and lazy loading live.

---

## 1. The question this finding had to answer first

F-875 §7.5 did not leave "make it drive a nested scroller" as an implementation
task. It left a *question*: **which element is "the" scroller when several
overflow?** A page can have a dozen elements with `scrollHeight > clientHeight`.
Picking the wrong one is not a smaller version of the current defect — it is a
new one, because the tool would then report a true record about the wrong
element, which is harder to notice than reporting nothing.

So the rule had to be chosen from measurement, not from taste. §3 is that
measurement.

---

## 2. Fixtures

Twelve `data:` pages, driven through the product path
(`spawn_browser → navigate → scroll_page → execute_script`) against real
headless Chrome 152 on Windows 11, in a temp browser-session root (never
`~/.stealth-mcp`). Viewport as measured: **1888 × 977**.

| id | shape | why it is here |
|---|---|---|
| **a** | `<!DOCTYPE html>` + 8000 px of body content | the CONTROL: a plain document scroller |
| **b** | `html,body{overflow:hidden}` + one full-viewport `div{overflow:auto}` | the app shell — the finding's headline case |
| **c** | two side-by-side `div{overflow:auto}` panes, 25 % / 75 %, different heights | a sidebar and a main pane: which one is "the page"? |
| **d** | a 400 px `div{overflow:auto}` inside a document that ALSO scrolls | the case where the nested scroller must LOSE |
| **e** | `html{scroll-snap-type:y mandatory}`, four 100 vh sections | does snapping break the document path? |
| **e2** | app shell whose shell is the snap container | snap AND nested at once |
| **f** | no doctype (quirks mode), 8000 px of body content | `document.scrollingElement` is `body`, not `html` |
| **g** | app shell containing a centred `div{overflow:auto}` grid (60 vh × 80 vw) | outer-vs-inner: the shell is the page, the grid is a widget |
| **h** | app shell under a `position:fixed;inset:0` scrim | the fixed overlay that blinds `elementFromPoint` |
| **i** | three columns, 20 % / 35 % / 45 %, all `overflow:auto` | no pane reaches half the viewport |
| **j** | `div{overflow-x:auto;overflow-y:hidden}` with 9000 px of inline content | a HORIZONTAL scroller: does the axis matter? |
| **k** | `html,body{overflow-y:auto}` + a full-viewport shell + a 20 px stray sibling | the document barely overflows while the shell holds the content |

Fixtures a–f are the six §7.5 named; g–k were added because a–f did **not**
discriminate between the candidate heuristics at all (§3.2).

---

## 3. Measured matrix

### 3.1 Today's record, and what a human calls "the page"

`scroll_page(direction="bottom", smooth=True)` on each fixture — except **j**,
which is asked `direction="right"` because it is the horizontal case.

| fixture | `document.scrollingElement` | today's record | the element a human calls "the page" |
|---|---|---|---|
| a | `html`, `max_y 7023` | `scrolled: true`, `7023/7023`, `at_edge: true` | `html` — **right** |
| b | `html`, `overflow hidden`, `max_y 0` | `scrolled: false`, `0/0`, `at_edge: true` | `div#shell` (`max_y 7023`) — **wrong** |
| c | `html`, `max_y 0` | `scrolled: false`, `0/0` | `div#main` (`max_y 8023`) — **wrong** |
| d | `html`, `max_y 6023` | `scrolled: true`, `6023/6023` | `html` — **right** |
| e | `html`, `max_y 2931` | `scrolled: true`, `2931/2931` | `html` — **right** |
| e2 | `html`, `max_y 0` | `scrolled: false`, `0/0` | `div#deck` (`max_y 2931`) — **wrong** |
| f | `body`, `max_y 7023` | `scrolled: true`, `7023/7023` | `body` — **right** |
| g | `html`, `max_y 0` | `scrolled: false`, `0/0` | `div#shell` (`max_y 6098`) — **wrong** |
| h | `html`, `max_y 0` | `scrolled: false`, `0/0` | `div#shell` (`max_y 7023`) — **wrong** |
| i | `html`, `max_y 0` | `scrolled: false`, `0/0` | `div#reader` (`max_y 8023`) — **wrong** |
| j | `html`, `max_x 0` | `scrolled: false`, `0/0` | `div#strip` (`max_x 7112`) — **wrong** |
| k | `html`, `max_y 0` (its `body` has `max_y 20`) | `scrolled: false`, `0/0` | `div#shell` (`max_y 7023`) — **wrong** |

**Today: 4 of 12.** Every miss is the same miss — the document scroller cannot
move, so nothing moves, and (since F-875) the record says so honestly. The four
hits are exactly the four pages where `document.scrollingElement` IS the page:
standards mode (a), quirks mode (f), document-level scroll-snap (e), and — the
one that matters — a document that scrolls *while containing* a nested scroller
(d). **Snap and quirks mode are not the problem**; the nested scroller is.

### 3.2 The two candidate heuristics, on the six original fixtures

Both were run as specified, with `document.scrollingElement` as the fallback:

* **A** — the largest-area element with `scrollHeight > clientHeight` and
  computed `overflow-y` in `{auto, scroll}` whose viewport-clipped bounding box
  covers at least half the viewport.
* **B** — `document.elementFromPoint(innerWidth/2, innerHeight/2)`, then the
  first scrollable ancestor walking up.

| fixture | A picks | B picks | agree? |
|---|---|---|---|
| a | *(none)* → `html` | `html` | yes |
| b | `div#shell` | `div#shell` | yes |
| c | `div#main` | `div#main` | yes |
| d | *(none)* → `html` | `html` | yes |
| e | *(none)* → `html` | `html` | yes |
| e2 | `div#deck` | `div#deck` | yes |
| f | *(none)* → `body` | `body` | yes |

**The six original fixtures cannot choose between the heuristics** — they agree
on all seven rows, and both are right on all seven. That is why g–k exist: a
matrix on which every candidate scores the same is not evidence, and shipping on
it would have been the guess this finding was told not to ship.

### 3.3 Where they split

| fixture | A (≥ 50 % area) | B (centre walk) | right answer |
|---|---|---|---|
| g | `div#shell` ✓ | `div#grid` ✗ | `div#shell` |
| h | `div#shell` ✓ | *(none)* → `html` ✗ | `div#shell` |
| i | *(none)* → `html` ✗ | `div#list` ✗ | `div#reader` |
| j | *(none)* → `html` ✗ | *(none)* → `html` ✗ | `div#strip` |
| k | `body` (`max_y 20`) ✗ | `div#shell` ✓ | `div#shell` |

Four distinct failure modes, each measured rather than reasoned:

* **g — B walks INWARD.** The centre of an app shell is usually occupied by the
  shell's own scrollable widget (a data grid, a code viewer, a virtualised
  table). B's first scrollable ancestor from the centre is that widget, so
  "scroll the page to the bottom" would scroll a table inside it. A prefers the
  larger box and gets the shell.
* **h — B is blind behind a fixed overlay.** `elementFromPoint` returns what is
  *painted* at that point. A `position:fixed; inset:0` scrim (a chat widget's
  backdrop, a cookie banner's overlay, a modal's dimmer) is painted over the
  centre and is not scrollable, and none of its ancestors are either — so B
  walks to `null` and falls back to a document that cannot move. A still sees
  the shell underneath, because it never asks what is on top.
* **i — A's coverage floor is arbitrary.** A three-pane mail layout
  (rail 20 % / list 35 % / reader 45 %) has no pane reaching half the viewport,
  so A finds nothing at all. B finds whatever the centre happens to land in,
  which here is the middle column (the message LIST), not the reading pane.
  *(This is the one fixture whose human answer is contestable — see §6.)*
* **j — both are written for one axis.** Both candidates test
  `scrollHeight > clientHeight` and computed `overflow-y`, so a horizontally
  scrolling strip is invisible to them, and `scroll_page(direction="right")` is
  answered by a rule that only knows about `down`. This is not a wrong pick, it
  is a category the heuristics cannot express.
* **k — A's tie-break is the scrollbar.** `body`'s border box is the full
  viewport; the shell inside it is narrower by exactly the scrollbar's width
  (1888 vs 1873 px — **0.8 %**). A therefore ranks a `body` that can move 20 px
  above a shell that can move 7023 px, because a scrollbar decided the contest.

### 3.4 Hit rate

| rule | hits | misses |
|---|---|---|
| `document.scrollingElement` only (today) | **4 / 12** | b, c, e2, g, h, i, j, k |
| **A** — largest area, ≥ 50 % coverage floor | **9 / 12** | i, k (wrong pick); j (axis-blind) |
| **B** — centre point, walk to first scrollable ancestor | **8 / 12** | g, h, i (wrong pick); j (axis-blind) |
| **the chosen rule** (§4) | **12 / 12** | — |

### 3.5 Where does `scrollend` fire — and does it fire at all for an element?

F-875's settle waits for the page's own `scrollend` rather than for two reads to
agree (F-875 §8). Driving a nested element raises a question that cannot be
assumed: does `scrollend` fire on an element scroller the way it does on the
document, and is the listener target the same? Measured, same method as §3 —
Chrome 152, headless, through `spawn_browser → navigate`, one listener on the
ELEMENT, one on `window` and one on `document`, then one scroll, then the three
counters read back:

| fixture | scroller | behavior | at element | at `window` | at `document` | `'onscrollend' in` target |
|---|---|---|---|---|---|---|
| a control | document | smooth | 0 | **1** | **1** | true |
| a control | document | instant | 0 | **1** | **1** | true |
| f quirks | document (`body`) | smooth | 0 | **1** | **1** | true |
| b app shell | `div#shell` | smooth | **1** | 0 | 0 | true |
| b app shell | `div#shell` | instant | **1** | 0 | 0 | true |
| c two panes | `div#main` | smooth | **1** | 0 | 0 | true |
| e2 snap nested | `div#deck` | smooth | **1** | 0 | 0 | true |
| j horizontal (x axis) | `div#strip` | smooth | **1** | 0 | 0 | true |
| j horizontal (x axis) | `div#strip` | instant | **1** | 0 | 0 | true |
| b app shell, NO-OP scroll | `div#shell` | smooth | 0 | 0 | 0 | true |

Three things follow, and all three are load-bearing:

1. **`scrollend` DOES fire for a nested scroller** — smooth and instant, on both
   axes, including inside a `scroll-snap` container. No element case needs a
   degraded fallback, so none is added: the read-agreement path stays exactly
   what F-875 left it, the fallback for a browser without `onscrollend`.
2. **It does not bubble, and the document's is never dispatched at
   `document.scrollingElement`.** The two are mirror images: an element scroll is
   invisible at `window`, a document scroll is invisible at the scrolling
   element. So there is exactly ONE right target per scroller kind, and **both**
   wrong choices fail the same silent way — the latch never sets, the settle
   burns its whole 10 s budget, and the tool reports `settled: false` about a
   scroll that finished in a second. The listener is therefore armed on the SAME
   expression that receives the scroll (`var T = …`), which is `window` for the
   document scroller and the resolved element for a nested one, and a hermetic
   pin reads that target back out of the generated script rather than trusting
   it (§5.4).
3. **A no-op scroll fires nothing anywhere** — not at the element, not at
   `window`. So `moves` cannot be inferred from the event, it has to be computed,
   and for a nested scroller it has to be computed against **that element's**
   extent on the requested axis, not the document's. That is what `SCROLL_JS`
   does, synchronously, from the clamped target, before any frame.

`'onscrollend' in …` is true for an `HTMLElement` as well as for `window`, so the
feature detect is one expression on the target and needs no branch. (`'scrollend'
in window` is false on Chrome 152 — F-875 measured that; the `on`-prefixed form
is the one that answers.)

---

## 4. The chosen rule, and why the others lose

```
scroller(axis):                                 # axis comes from the direction
  1. if document.scrollingElement can move on AXIS  ->  it IS the scroller
  2. else, among elements that can move on AXIS and whose computed overflow on
     AXIS is auto|scroll, the one with the largest VIEWPORT-CLIPPED area;
     areas within 5 % of each other count as equal and the larger scrollable
     extent breaks the tie
  3. else the document scroller anyway  ->  "nothing scrolls" is an answer
```

**Rule 1 is the whole reason d, e, f and a stay right, and it is not a
tie-break — it is a precedence.** If the document can move, the document IS the
page: that is what the window scrolls, what the user's wheel scrolls at rest,
and what `document.scrollingElement` is defined to name. Fixture d is the proof:
a 400 px scrollable box sits inside a 6023 px scrolling document, and any
"largest scrollable" rule without rule 1 in front of it would have to be talked
out of choosing the box. Rule 1 also means **the SCROLL CALL for a document
scroller is byte-identical to F-875's** — `window.scrollTo` / `window.scrollBy`,
the same six templates, for all twelve direction/`smooth` combinations, and the
`scrollend` listener still armed on `window` — so the fix cannot regress the four
fixtures that already worked. The claim is scoped to that call on purpose. The
script AROUND it is not byte-identical and is not claimed to be: `READ_JS`
changed from `window.scrollX`/`scrollY` to the chosen element's
`scrollLeft`/`scrollTop` (equal by definition for the document scroller, and
measured equal in both compatibility modes), `SCROLL_JS` binds the target and the
extent to names (`var T=window; var E=(document.scrollingElement||…)`) instead of
spelling them at each use, and the resolver and the identity fields are new.

**Rule 2 beats B (the centre walk) on three measured grounds:** B looks
*inward* (g), B is blind to what is painted over the centre (h), and B's answer
depends on a single point, so a layout shift of a few hundred pixels changes
which element it names. Area is a property of the whole box and moves smoothly;
a point is a coin toss that a fixed overlay can rig.

**Rule 2 beats A (the ≥ 50 % floor) on two measured grounds:** the floor is a
number with nothing behind it — it excluded the only sensible answer on i and
cost nothing anywhere else — and A's plain `area >` comparison let a scrollbar's
width decide k. Both are removed: no floor, and a **5 % slack band** with the
larger extent breaking it. The 5 % is anchored to the measurement it exists for:
the scrollbar difference that created k's inversion is **0.8 %**, so the band is
six times the artefact it must absorb, while the smallest *real* pane difference
in the matrix (i's reader vs list, 830 k vs 645 k px²) is **29 %** — an order of
magnitude outside it. Remaining exact ties break on document order, which
prefers the OUTER of two nested candidates, agreeing with rule 2's own
preference for the bigger box.

**`document.scrollingElement` as the sole rule (today) loses on 8 of 12** and is
kept only where it belongs: as rule 1's subject and rule 3's fallback.

**Axis.** The rule takes the axis from `_DIRECTIONS` — the table that already
knows `right` moves X — so `direction="right"` on fixture j picks `div#strip`
(`max_x 7112`) and `direction="bottom"` on the same page correctly picks nothing
(`strip` is `overflow-y: hidden`; there is no vertical scroller on that page and
the honest answer is `max_scroll_y: 0`). Neither candidate heuristic could say
either of those things.

**Cost.** The pick walks `querySelectorAll('*')` and calls `getComputedStyle`
on every element that overflows. Measured on a 6007-element page: **2.59 ms per
pick** (mean of 10, `performance.now`). That is why it is picked **once per
call** and addressed by an index path afterwards: re-selecting on every settle
poll would spend ~520 ms of the page's main thread across a full 10 s settle
window, to answer a question whose answer does not change.

---

## 5. What changed

### 5.1 `embedded/scroll_position.py` — the leaf grew the pick

It was THE one home for "where is this page scrolled, and has it stopped". It is
now THE one home for "**which element is this page's scroller**, where is it
scrolled, and has it stopped". Everything new is here; `dom_handler.scroll_page`
gained one call and two record fields and nothing else (it is at 984 of its
1000-LOC budget, so it could not have taken the logic even if it should).

* **`SCROLLER_JS` / `scroller(tab, direction, amount)`** — §4's rule, in ONE
  `JSON.stringify` round trip, for the axis `_DIRECTIONS` says the direction
  moves. `JSON.stringify` for F-869's and F-872's reason, the same one `READ_JS`
  already carries. It returns a `Scroller`: an index path from
  `document.documentElement` (or `None` for the document scroller) and
  `is_document`. A path and not a stashed reference, because `tab.evaluate` is
  stateless and `window.__something = el` would leave the tool's bookkeeping on
  the page. It also **validates the whole request before the round trip**, so
  F-875's two "costs no round trip" pins (an invalid direction, a negative
  amount) still hold against the FIRST call the tool now makes.
* **`_RESOLVE_JS`** — the one resolver both the read and the scroll script
  prepend. If the path no longer names an element (the page re-rendered
  mid-scroll) it falls back to the document scroller, and the READ reports what
  it actually read — see the next bullet.
* **`Position` gained its own identity** — `tag`, `element_id`, `classes`,
  `is_document`, alongside the four offsets. A scroll position is meaningless
  without saying what was measured, and putting the identity in the READ rather
  than in the PICK is what makes the record unable to lie: the record's
  `scroller` / `scroller_is_document` come from the FINAL read, so a stale path
  that fell back reports the document it actually read, not the div it hoped
  for. `offset` is unchanged and is still the only thing compared (F-875 §7.2).
* **`_DIRECTIONS` is still ONE table**, and still six rows. The scripts gained
  a `{target}` and an `{extent}` placeholder instead of naming `window` and
  `document.scrollingElement` inline. For the document the substitution is
  literally `window` and `(document.scrollingElement||document.documentElement)`,
  so **the scroll call generated for a document scroller is byte-identical to
  F-875's** — which is the mechanical form of "the control fixture is unchanged".
  For a nested scroller both become `_el([…])`.
* **`SCROLL_JS` arms F-875's `scrollend` latch on the scroller that was picked.**
  The prologue binds `var T = {target}` (the scroll's receiver) and
  `var E = {extent}` (the same object, for the document spelled as the scrolling
  element so `scrollHeight` is readable), and the listener goes on `T`. `moves`
  and `supported` are computed off `E` and `T` respectively, so a nested
  scroller's "will this change anything" is answered against ITS extent on the
  requested axis and its own `onscrollend`. §3.5 is why this is a measurement and
  not a style choice: arming on `window` for an app shell, or on the element for
  a plain page, never fires and would burn the full settle budget in silence.
* **`_json_answer`** — the "the evaluate did not answer with the JSON we asked
  for" ladder, factored out of `read` because `scroller` needs exactly the same
  one. Messages still report shape and count only: a type name, a character
  count, a field count. Never the page's text.

### 5.2 The record grew two fields

| field | means |
|---|---|
| `scroller` | `{tag, id, classes}` of the element that was actually read — **shape only**, and bounded (`id` to 64 chars, at most 4 classes of 32 chars each), because a page authors both and a CSS-in-JS class name has no natural length |
| `scroller_is_document` | the element read IS `document.scrollingElement`, i.e. this was a plain page scroll |

They are in the record and not merely in the logs because §3.3 i and §6 are
real: on a page with several full-height scrollers the pick is a judgement, and
a caller that can SEE which element was driven can disagree with it and reach
for `execute_script` deliberately. A record that silently drove `div#list`
instead of `div#reader` would be F-875's defect wearing a better shape.

Nothing else in the record moved. `scrolled` still compares offsets only,
`at_edge` still carries its one pixel of far-edge slack, `settled` /
`settle_seconds` are unchanged.

### 5.3 Measured with the fix

Real Chrome 152, headless, Windows 11, through `spawn_browser → navigate →
scroll_page`, same twelve fixtures, on the MERGED code (F-878's pick plus F-875
§8's `scrollend` latch, armed per §3.5). `settle` is the record's
`settle_seconds`; a latch armed on the wrong target would show here as
`settled` missing and `settle` ≈ 10.0:

```
a  control        html        is_document=true   y 7023/7023  settle=1.485  scrolled settled at_edge
b  app shell      div#shell   is_document=false  y 7023/7023  settle=1.602  scrolled settled at_edge
c  two panes      div#main    is_document=false  y 8023/8023  settle=1.606  scrolled settled at_edge
d  nested-in-doc  html        is_document=true   y 6023/6023  settle=1.365  scrolled settled at_edge
e  snap document  html        is_document=true   y 2931/2931  settle=1.093  scrolled settled at_edge
e2 snap nested    div#deck    is_document=false  y 2931/2931  settle=1.001  scrolled settled at_edge
f  quirks         body        is_document=true   y 7023/7023  settle=1.475  scrolled settled at_edge
g  shell+grid     div#shell   is_document=false  y 6098/6098  settle=1.470  scrolled settled at_edge
h  shell+scrim    div#shell   is_document=false  y 7023/7023  settle=1.482  scrolled settled at_edge
i  three columns  div#reader  is_document=false  y 8023/8023  settle=1.595  scrolled settled at_edge
j  horizontal     div#strip   is_document=false  x  500/7112  settle=0.502  scrolled settled            (direction="right", amount=500 — so NOT at_edge, correctly)
k  stray overflow div#shell   is_document=false  y 7023/7023  settle=1.598  scrolled settled at_edge
```

Twelve of twelve pick the §3.1 human answer, every one `settled`, every one at
its true final offset, and every settle is the length of the smooth animation
(0.50–1.61 s) — none is anywhere near the 10 s budget, which is the end-to-end
form of §3.5's "the latch is on the right target". The pre-merge run of the same
matrix (F-878 alone, on F-875's read-agreement settle) picked the same twelve
elements and landed at the same twelve offsets.

### 5.4 Tests and goldens

* `tests/test_scroll_page_verification.py` — the F-875 pins stay, plus new ones
  driven through `fakes.ScrollingTab`'s **nested-scroller mode**, whose geometry
  is copied from §3's real-Chrome rows (`html` `max_y 0` with
  `overflow: hidden`; `div#shell` `max_y 7023`) and whose pick is answered by
  Chrome's own rule, not by the product's. The RED pin fails on the DEFECT:
  before the fix it reports `scrolled: false, max_scroll_y: 0` on a page whose
  shell can move 7023 px. The double models **two independent scroll
  containers**, so it can express fixture d (a page where the document AND a
  nested div can both move) and a **stale path** (`stale_path=True`: the
  resolver falls back to the document however the script is spelled). Since
  the F-875 §8 merge it also reads the latch's ARM TARGET out of the generated
  script (`var T=…`) independently of which container the scroll call moves, and
  only latches when the armed target is the container that moved — which is
  exactly §3.5's asymmetry, so arming on the wrong target is a RED test here and
  not a browser-only symptom: `test_the_scrollend_listener_is_armed_on_the_element_that_scrolls`
  (a nested scroll must arm on `_el(…)`, a document scroll on `window`) and
  `test_a_stalled_nested_scroll_is_not_a_finished_one` (a nested smooth scroll
  whose latch has not set is reported `settled: false`, never as finished on the
  strength of two agreeing reads).
* **What a hermetic pin here cannot hold.** `ScrollingTab` is not a JS engine:
  it applies Chrome's rule to its own geometry rather than executing
  `SCROLLER_JS`. So the hermetic fixture-d pin holds the tool's **wiring** —
  when the page answers "the document", the tool drives the document, reports
  the document's offsets and leaves the nested container alone — and the RULE
  itself is held only by real Chrome. Swapping rules 1 and 2 in the product
  leaves every hermetic pin green and fails
  `test_e2e…::test_a_scrolling_document_is_the_page_even_with_a_nested_scroller`.
  That is stated in both files' docstrings so nobody reads the hermetic pin as
  more than it is.
* `tests/test_e2e_scroll_page_verification.py` — the F-875 pins stay, plus real
  Chrome on fixture d (the `scrollingElement` precedence, the load-bearing pin
  for rule 1), the app shell (b), the outer-vs-inner shell (g) and the
  horizontal strip (j), each cross-checked against the page's own `scrollTop` /
  `scrollLeft` and against what the *other* container did. The app-shell pin also
  asserts `settle_seconds < 5.0`, so a latch armed on `window` for a nested
  scroller (a full 10 s budget, then `settled: false`) fails it outright rather
  than merely slowing it. **All twelve matrix
  fixtures are declared there as `data:` constants** (`F878_MATRIX`), in the
  finding's own lettering, so §3 is reproducible from the repo and not only
  from this prose — the four that carry assertions are the four a candidate
  heuristic gets wrong. `tmp_empty_root`, `data:` URLs, browsers closed in
  `finally`.
* `tests/goldens/tool_surface.json` (HARD) — **one tool, deliberate**:
  `scroll_page`'s description gains the two record fields. The input schema and
  the output schema are untouched (`{type: object, additionalProperties: true}`
  already covers a record that grew two keys); no other tool moved.
  Justification: a caller cannot use `scroller` without being told it is there,
  and the description is where the tool says what its record carries.

---

## 6. Not claimed

* **"The page" is not always a fact.** Fixture i (rail / list / reader, none
  reaching half the viewport) is the one row whose human answer is contestable:
  the rule picks the reading pane because it is the largest, and a user who
  meant "scroll the message list" gets the reader. This is why `scroller` is in
  the record. There is no measurement that settles it, and inventing a
  `STEALTH_MCP_*` knob for it would be a second way to do what naming the chosen
  element already does.
* **With no coverage floor, a tiny scroller wins by default whenever the
  document cannot move on the asked-for axis.** If the page's only
  `overflow:auto` element is a 100 × 100 legend on a fullscreen canvas, the rule
  picks the legend. Measured? No — reasoned from the rule, and left in
  deliberately: "there is exactly one scroller on this page" is a better guess
  than "nothing scrolls", and the record names it either way.
* **Because the rule is per-axis, this reaches ORDINARY pages too.** A normal
  article scrolls vertically, so `direction="bottom"` takes rule 1 and nothing
  changes — but its document almost never scrolls HORIZONTALLY, so
  `direction="right"` now falls to rule 2 and drives whatever horizontal
  scroller the page has: a wide `<pre>`, a code block, a carousel, a table
  wrapper. Before F-878 that call did nothing at all. This is a deliberate
  behaviour change and the better answer of the two, but it IS a change, and it
  is the one place the fix alters a page the finding never called broken. The
  record names the element, which is the whole mitigation. Not measured on a
  real article — reasoned from the rule and from fixture j.
* **A path that goes stale mid-scroll falls back to the document**, and the
  record then reports the document (§5.1). The fallback is reasoned rather than
  measured against a real SPA re-render; what IS pinned (hermetically, through
  `ScrollingTab(stale_path=True)`) is the record's two shapes — the identity is
  the document's, and `scroller_is_document` agrees with it. `scrolled` is also
  `False` across such a fallback by construction: `Position.moved_from` refuses
  to call the difference between two DIFFERENT elements' offsets a scroll.
* **The `scrollend` target measurement is Chrome 152's.** §3.5 establishes
  where the event is dispatched on this Chrome; the CSSOM View draft says an
  element's `scrollend` fires at the element and the document's at the
  `Document`, so the asymmetry is the specification and not a quirk, but no
  other engine or version was measured. A browser that lacked `onscrollend` on
  the armed target takes the read-agreement fallback (`supported` is asked of
  the target, not of `window`); a browser that HAD it but dispatched the event
  somewhere else would spend the 10 s budget and report `settled: false` — the
  cost is bounded and the record is honest, but it is a cost, and only Chrome
  152 is measured not to pay it.
* **Not measured: `iframe` content.** The pick walks `document.querySelectorAll`
  in the top document only. A page whose content is inside a same-origin iframe
  will report the top document's scroller. `scroll_page` has never crossed a
  frame boundary and this change does not start.
* **Not measured: shadow DOM.** `querySelectorAll('*')` does not pierce shadow
  roots, so a scroller inside a closed or open shadow tree is invisible to the
  pick. Web-component-heavy app shells are a plausible miss; no fixture here
  covers one.
* **Not measured: a page that changes its scroller between the pick and the
  settle** (a route change during a smooth scroll). The path either still
  resolves or falls back; which one happens on a real framework was not
  measured.
* **Not measured: cost on a DOM larger than ~6000 elements.** The 2.59 ms in §4
  is one page on one machine; the rule is O(elements) with a `getComputedStyle`
  per overflowing element, so a 60 000-element page is expected to cost roughly
  ten times that, once per call.
* **`scroll-snap` is not handled specially.** Fixtures e and e2 show the snap
  container lands at its true extent under `mandatory` snapping because the
  sections are exact multiples of the viewport. A snap container whose children
  are NOT viewport multiples will settle at the nearest snap point rather than
  at `max_scroll_y`, and the record will honestly report `at_edge: false`. That
  is a correct record about a snapping page, not a defect, and it is not
  measured here.
