# F-831 — seven tools advertise XPath; one honoured it, via a second way

**Status: FIXED** on `fix/F831-xpath-dispatch` (2026-08-31), branched from
`fix/F824-F828-classifier-reach`. Opened by **GitHub issue #15** (user-filed).
**Severity: MEDIUM** — no corruption and nothing wedges, but six of the seven
tools took a documented input and failed on it, and the seventh honoured it
through a path that bypassed the stale-document recovery every other selector
gets. A user reading the tool schema had no way to know which was which.

---

## What is proven

The reporter's repro, on a page containing the text "University of Ottawa":

| step | selector | result |
|---|---|---|
| `query_elements` | `//*[contains(text(),'University of Ottawa')]` | returns the element |
| `click_element` | **the same string** | `-32000 "DOM Error while querying"` |

That asymmetry is the whole defect in one screenshot: `query_elements` had a
private XPath branch and `click_element` did not, so `click_element` handed an
XPath to `DOM.querySelector` and Blink answered with its blanket query failure.
The reporter's own diagnosis — *"query and click use different
element-resolution paths"* — is exactly right, and the workaround they found
(translate the target to CSS by hand) is the cost users were paying.

A second-order cost worth naming: since F-828 widened `_STALE_NODE_MARKERS` to
include `"DOM Error while querying"`, that reply is now *classified as a
recoverable race* — so an XPath given to a CSS-only tool was re-resolved
`_MAX_RESOLVES` times, with backoff, before failing anyway. The recovery was
being spent on a selector that could never resolve on that path. Dispatching
correctly removes the retries as well as the failure.

`server.py` advertises `XPath` in seven element-interaction tool docstrings —
the text an MCP client shows the model as the parameter's contract:

| # | tool | `server.py` | advertised as | resolved through, before | XPath actually worked? |
|---|---|---|---|---|---|
| 1 | `query_elements` | L728 | `CSS selector or XPath (starts with '//')` | its own `selector.startswith("//")` → `tab.xpath` | **yes**, unprotected |
| 2 | `click_element` | L782 | `CSS selector or XPath` | `element_resolution.resolve_element` | no — resolved as CSS |
| 3 | `upload_file` | L814 | `CSS selector or XPath for the <input type="file"> element` | `element_resolution.resolve_element` | no |
| 4 | `type_text` | L846 | `CSS selector or XPath` | `element_resolution.resolve_element` | no |
| 5 | `paste_text` | L877 | `CSS selector or XPath` | `element_resolution.resolve_element` | no |
| 6 | `get_element_state` | L934 | `CSS selector or XPath` | `element_resolution.resolve_element` | no |
| 7 | `wait_for_element` | L958 | `CSS selector or XPath` | `element_resolution.resolve_element` | no |

`models.py` L69 (`ElementInfo.selector`) advertises it an eighth time, on the
row `query_elements` returns.

**Routing census — how many needed re-routing: zero.** Tools 2–7 already went
through `element_resolution` (the repo's one home for selector resolution);
they simply arrived there with an XPath string that `Tab.select` handed to
`DOM.querySelector`, which raised or matched nothing. Tool 1 was the only one
off the shared path, and only for XPath. So the fix is entirely a *dispatch*
question, not a plumbing one — which is exactly why it belongs in the one home.

The second way, verbatim (`dom_handler.py`, before):

```python
if selector.startswith("//"):
    elements = await tab.xpath(selector)          # <- direct, unprotected
    debug_logger.log_info(..., f"XPath query returned {len(elements)} elements")
else:
    elements = await resolve_elements(tab, selector)
    debug_logger.log_info(..., f"CSS query returned {len(elements)} elements")
```

`CLAUDE.md` states the rule this breaks twice over: **all selector resolution
routes through `element_resolution`, never `tab.select`/`find`/`xpath`
directly**, and **a second way to do something already done is a defect**. The
concrete cost of the bypass: an XPath in `query_elements` got none of the
F-817/F-824/F-828 recovery, so a `DOM.documentUpdated` mid-query surfaced the
raw `-32000` / `DOM Error while querying` to the caller under exactly the DOM
churn that recovery exists for.

---

## Root cause

Choosing between two selector *languages* is part of resolving a selector, but
it had no home. `element_resolution` documented only the CSS round trip, so the
one tool that wanted XPath grew its own branch where it happened to need it, and
the other six inherited a resolver that had never been told the question exists.

---

## The fix

**XPath dispatch moves into `element_resolution`, and the branch is deleted.**

### The detection contract (`xpath_expression` / `is_xpath`)

THE one place the CSS-or-XPath choice is made. Purely syntactic — it never
inspects the page — and therefore deterministic:

| input | verdict | expression dispatched |
|---|---|---|
| `xpath=//button[@id='go']` | XPath | `//button[@id='go']` (prefix stripped) |
| `XPath= //a ` | XPath | `//a` (prefix case-insensitive, both sides stripped) |
| `//a`, `//script[not(@src)]` | XPath | as written — **all the deleted branch ever accepted** |
| `/html/body` | XPath | as written |
| `(//div)[1]` | XPath | as written |
| `  //a  ` | XPath | `//a` |
| `#id`, `.cls`, `div > a`, `a[href]`, `*` | CSS | — |
| `./div` | **CSS** | — (`.foo` is a class selector far more often than a relative XPath; `xpath=./div` is how a caller asks for the latter) |
| `xpath=` with nothing after it | `ToolError` | — (neither a valid XPath nor a plausible CSS selector; say so rather than report a confusing not-found) |

The two unprefixed leading characters are chosen because **no CSS selector may
begin with `/` or `(`** — nothing is taken away from CSS to add XPath.

### The resolution paths

All three entry points dispatch, and all three run the XPath call inside the
**same** `_resolve_with_recovery` the CSS paths use — one retry loop, one
classifier, one bound (`_MAX_RESOLVES`), both languages:

| entry point | CSS | XPath |
|---|---|---|
| `resolve_element` | `tab.select` | `tab.xpath` → first match |
| `resolve_elements` | `tab.select_all` | `tab.xpath` |
| `query_selector_all` | `DOM.getDocument` + `DOM.querySelectorAll` | `DOM.performSearch` + `DOM.getSearchResults` (raw node ids) |

`tab.xpath` is typed `List[Optional[Element]]`; the helper drops the `None`
placeholders so no caller of this module has to defend against one.

### What was deleted

`dom_handler.query_elements`' `startswith("//")` branch, its `tab.xpath` call,
and its duplicated "XPath query returned N" / "CSS query returned N" log pair —
replaced by one unconditional `await resolve_elements(tab, selector)`. That is
the point of the fix, not a side effect. `query_elements`' returned shape is
unchanged (`ElementInfo`, same eight keys, `selector` still echoes the caller's
own string) and pinned.

### One regression this fix would otherwise have introduced

`select_option` does *not* advertise XPath, but it resolves through the same
shared path, so it now accepts one. Its `value` and `index` arms ran a **second**
lookup — `document.querySelector(selector)` inside `tab.evaluate` — which cannot
express an XPath: it would have matched nothing, done nothing, and still
returned `True`. Both arms now `apply(...)` to the element already resolved
above, which removes that second resolution as well as the silent-success. The
`text` arm already used the resolved element.

---

## Pins

`tests/test_xpath_dispatch.py` (hermetic — fake `Tab`/`Element` in the
`tests/test_element_resolution.py` effects-list idiom; no real Chrome):

| group | tests | asserts |
|---|---|---|
| (f) detection | 13 params + 6 | every row of the contract table above, including `//` legacy, prefix stripping, and `./div` staying CSS |
| (a) dispatch | 6 | XPath reaches `tab.xpath` / `performSearch` and **never** `select`/`select_all`; first-match; zero-match; `None`s dropped |
| (e) recovery | 3 | a `-32000` on the XPath path re-resolves, is bounded at `_MAX_RESOLVES`, and an unrelated error is never retried |
| (d) CSS regression | 2 | CSS still uses `select`/`select_all`/`querySelectorAll`, `tab.xpath` untouched |
| (b) shape | 3 | `query_elements(//a)` returns the same eight `ElementInfo` keys with the same values as the CSS run; goes through `element_resolution` (recovers from a stale node); `limit`/`text_filter` still apply |
| (c) the other tools | 6 params | `get_element_state`, `wait_for_element`, `click_element` × (`//button`, `xpath=//button`) all reach `tab.xpath` |
| select_option | 2 params | acts on the resolved element, no `querySelector` in the emitted JS |
| issue #15 | 1 | the reporter's literal `//*[contains(text(),'University of Ottawa')]` reaches the same XPath call from **both** `query_elements` and `click_element` |

**RED before the fix:** the file first failed to import at all
(`cannot import name 'is_xpath'`); with the detection helpers added and nothing
else, 16 of 38 failed — the detection group passed, and every
dispatch/recovery/routing test failed on `pop from empty list` (the fake's
`xpath` effects were never consumed, because resolution went to
`select`/`select_all`). The two `select_option` pins were added afterwards, for
the regression the dispatch itself would have introduced. Note that
`test_query_elements_xpath_keeps_the_css_result_shape` passed **both** before
and after: that is the compatibility pin doing its job.

**GREEN:** 41/41 in `tests/test_xpath_dispatch.py`; 148 passed across
`test_xpath_dispatch` + `test_element_resolution` + `test_dom_handler` +
`test_error_typing` + `test_cdp_element_cloner` + `test_tool_errors` +
`test_doc_claims` + `test_tool_dispatch`; 97 more across `test_tool_dispatch` +
`test_doc_claims` + `test_correlation_id` + `test_exception_handling` +
`test_cloner_schemas` + `test_check_suppression_owners`. `ruff check` /
`ruff format --check` clean over `src` and `tests`;
`tools/check_file_budgets.py` green.

---

## Budgets

| file | LOC | cap |
|---|---|---|
| `embedded/element_resolution.py` | 350 (was 229) | 1000 (not grandfathered) |
| `embedded/dom_handler.py` | 869 (was 873) | 1000 (not grandfathered) |
| `embedded/server.py` | **3411** | 3411 — **untouched**, net zero |

`server.py` is not edited at all: after the fix its seven docstrings are simply
true. `query_elements`' `(starts with '//')` parenthetical is now *narrower*
than reality rather than wrong, so correcting it is left to a docs pass rather
than spent against a zero-headroom cap.

---

## Residual risks

* **The JS-eval cloner aspects.** `cdp_element_cloner`'s CDP-native path
  (`extract_complete_element_cdp`, via `query_selector_all`) gains working
  XPath. Its JS-eval aspects (`extract_styles.js` and friends) re-resolve the
  selector with `document.querySelector(selector)` in the page, which throws on
  an XPath. Those tools never advertised XPath, and the outcome is a raised
  error rather than a silent wrong answer, so this is deliberately out of
  scope — but it is a *third* place a selector is resolved (in JS, in the page)
  and it is the natural next finding if XPath is ever advertised on the cloner
  tools.
* **`tab.xpath`'s own 2.5 s poll.** nodriver retries an empty XPath result for
  up to 2.5 s by default. That was already `query_elements`' behaviour and is
  unchanged here, but it now applies to six more tools: a genuinely absent
  XPath costs ~2.5 s where an absent CSS selector fails faster.
* **`DOM.performSearch` is not `DOM.querySelectorAll`.** The node-id XPath path
  uses CDP's search API, which also matches on plain text when handed a
  non-XPath string. It is only ever reached for selectors the detection contract
  has already classified as XPath, so that latitude is unreachable — but it is
  why the contract must stay strict rather than "try XPath, fall back to CSS".
* **Verified hermetically only.** No real-Chrome run: the fakes assert *which
  API is called with what*, not that Chrome resolves the expression. The CDP
  search pair is modelled on nodriver's own `find_elements_by_text`.
