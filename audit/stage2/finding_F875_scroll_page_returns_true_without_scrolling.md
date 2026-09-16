# F-875 — `scroll_page` returns `True` for a scroll that has not happened

**Status:** OPEN (product defect; live on 2.1.6 and on `main` at `bb78878`). Measured, NOT fixed — see §5 for why it is not folded into F-874's PR.
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

## 4. Proposed remedy (not implemented)

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
  for it.
* **Not measured:** `direction` values other than `bottom`. The `return True` is
  unconditional for all of them, so the truthfulness defect is shared; the
  magnitude is not.
* The Cloudflare interstitial is not itself a product defect — `navigate` returned
  truthfully about the document Chrome had committed. It is the reason a caller
  reaches a settled-looking page that is one viewport tall.
