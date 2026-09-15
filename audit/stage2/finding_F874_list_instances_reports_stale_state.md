# F-874 — `list_instances` reports the last navigation, not the instance

**Status:** FIXED in this PR (product defect; live on 2.1.6 and on `main` at `bb78878`)
**Opened by:** live use of the 2.1.6 backend over real stdio with ten headed browsers, MEASURED 2026-09-15 (Chrome 152, Windows 11); re-measured against real headless Chrome in this worktree
**Source at:** `origin/main` = `bb78878`
**Severity:** MEDIUM-HIGH. `list_instances` is the orienting tool — the one a caller reaches for to find out *which browser is where* before doing anything else. Nothing raises and nothing looks wrong: the record is well-formed, the field is named `current_url`, and the value is simply from an earlier moment. A caller that trusts it acts on the wrong tab.

---

## 1. What was observed

Four instances, same session, same second. `get_active_tab` on the same `instance_id` was correct every time.

| # | instance | `list_instances` said | the truth (`get_active_tab` / `execute_script`) | what had moved the page |
|---|---|---|---|---|
| 1 | YouTube | `current_url: "https://www.youtube.com/"`, `title: "YouTube"` | `…/results?search_query=lofi+hip+hop+radio`, `"lofi hip hop radio - YouTube"` | a click on the page's own search button |
| 2 | Amazon | `title: null` | `document.title` = `"Amazon.com. Spend less. Smile more."` | the page set its title after load; the `navigate` tool had returned `title: ""` |
| 3 | Wikipedia → `data:text/html,…` | `current_url` = the data URL (correct), `title: "Wikipedia, the free encyclopedia"` (stale) | the data: page has no title | the `navigate` tool itself |
| 4 | LinkedIn | the first tab's login url/title | the second tab (`/feed/`) | `switch_tab` |

Row 3 is the one that misleads on first reading: it looks as though url and title are captured at *different moments*. They are not — see §2b.

---

## 2. Mechanism

`BrowserInstance.current_url` / `.title` had exactly two writers in the whole tree:

* `browser_manager.spawn_browser`, once, at spawn:
  `instance.current_url = getattr(tab, "url", "") or instance.current_url`
* `browser_manager.update_instance_state`, called from exactly one place —
  `browser_manager.navigate`, after a successful navigation.

Nothing else ever refreshed them. `list_instances` read that pair and labelled it
`current_url`/`title`. So every way a page can move *without* the `navigate` tool —
an in-page click, a script navigation, a redirect after load, `switch_tab`,
`close_tab` re-pointing the active tab, a late `document.title` — left the tool
reporting a moment that had passed.

`get_active_tab` was right because it asked the TAB
(`getattr(tab, "url", "")`, `getattr(tab.target, "title", "")`), and nodriver keeps
`tab.target` current from `Target.targetInfoChanged`
(`Browser._handle_target_update` assigns `current_tab._target`;
`Browser.start` registers the handler and sends `Target.setDiscoverTargets`).
`browser_manager.list_tabs` asked the same way. **Two of the three tools that answer
"what page is this" wrote the same four expressions out by hand; the third did not
ask at all.**

### 2b. A second defect on the same cache (row 2 and row 3)

```python
if url:
    instance.current_url = url
if title:
    instance.title = title
```

Truthiness, not `is not None`. An empty title is what the page HAS — Amazon sets
its title after load, so `navigate` legitimately answered `title: ""`, and a bare
`data:text/html` document never sets one. Both times `if title:` was false and the
PREVIOUS value stayed. That is the whole of row 3: the url and the title were
captured in the same two lines of `navigate`, microseconds apart; the title was
then silently dropped. Row 2's `title: null` is the same guard at spawn, where the
field had never been written at all.

---

## 3. Fix (this PR)

**One home for the read.** New leaf `embedded/tab_identity.py`:

* `record(tab)` — THE `{tab_id, url, title, type}` record (a pure attribute read,
  no CDP, no `await`);
* `refreshed(browser, tab)` — one `Browser.update_targets()`
  (`Target.getTargets`) in front of it.

`browser_manager.list_tabs`, `tool_sections/tabs.get_active_tab` and
`tool_sections/browser_management.list_instances` all go through it. A leaf: it
imports no other embedded module, takes the tab as an argument, and does no error
handling — bounding and degrading are the caller's, because only the caller knows
whether one wedged browser should cost the whole answer or one row.

**Why `refreshed` and not the bare attribute read.** `tab.target` is only as fresh
as whatever `targetInfoChanged` nodriver has already processed. `getTargets`
answers from Chrome. It costs one round trip per browser, which is also why a
listing pays it once per browser rather than awaiting each tab — `Tab.wait()` has a
0.5 s floor and a rediscovered target is a raw `Connection` that cannot be awaited
at all (F-771). `get_active_tab` loses its `await tab` in the same trade: that call
refreshed nothing and raised `TypeError` outright on a rediscovered target.

**Honest names for the cache.** `BrowserInstance.current_url`/`.title` →
`last_navigated_url`/`last_navigated_title`, which is what they have always held.
The two surfaces that have no live browser to read now carry that pair under those
names instead of claiming `current_url`: `list_instances`' `stored` tier (the
instance is not in memory at all) and `get_instance_state`'s two `partial` records.
And the write guard is `is not None`.

**Cost and degradation.** Each active entry is bounded by exactly ONE CDP budget,
and the wrap sits on the one call that reaches Chrome — `tab_identity.refreshed`'s
`Target.getTargets`. The two manager lookups in front of it (`get_active_tab`,
`get_browser`) are lock-guarded dict reads and are deliberately unwrapped: a
`_with_cdp_timeout` there would claim a CDP bound over something that never speaks
CDP and charge the entry three budgets for one round trip. Entries are gathered
concurrently, so N wedged browsers cost ONE budget for the listing, not N.

An entry degrades on its own — `partial: True` + `detail_error` + the
`last_navigated_*` pair, deliberately with **no `current_url` key at all**, because
a cached value under that name is the defect. The degradation is also written to
the durable log (`rt.debug_logger.log_warning(..., error=exc)`) so a real bug
inside `tab_identity` is visible rather than a quiet partial row: the message is
shape-only — instance id and exception TYPE, never a url, which can carry a session
token in its query string and would reach the log and a Sentry breadcrumb — while
`error=` forwards the traceback as `exc_info` (F-869's addition).

The three record shapes are stated in `list_instances`' own docstring, which is
what the regenerated golden carries.

---

## 4. Verification

Hermetic — `tests/test_list_instances_live_state.py`, 9 pins, all RED at `bb78878`
for the defect and nothing else (measured before the fix):

| pin | RED reason at `bb78878` |
|---|---|
| active entry is the live tab | `assert 'https://live.test/login' == 'https://live.test/feed'` |
| `list_instances` == `get_active_tab` | `('https://live.test/', 'Home') != ('…/watch?v=abc', 'lofi hip hop radio')` |
| the read refreshes targets first | `update_targets_calls` `0 == 1` |
| a wedged browser degrades only its row | `KeyError: 'partial'` |
| N wedged instances cost one timeout | `KeyError: 'partial'` |
| a missing tab degrades, not raises | `KeyError: 'partial'` |
| stored tier names its values | `KeyError: 'current_url'` (the old key) |
| an empty title replaces the old one | `BrowserInstance has no field "last_navigated_url"` |
| `None` still means "nothing to say" | same |

Real Chrome — `tests/test_browser_integration.py::TestListInstancesLiveState`
(`integration`, headless, `tmp_empty_root` so nothing touches a real session root,
every page a `data:` URL so it needs no network). RED at `bb78878`
(`KeyError: 'partial'`), GREEN after. It covers all three mechanisms:

* a title the page sets after load reads `"Beta-late"` where `navigate` answered `"Alpha"` (row 2);
* a `history.pushState` url change reads through (row 1's shape, without a second document — `location.hash` is a no-op on a `data:` URL, measured, `pushState` is what moves it);
* `switch_tab` moves both fields to the new tab (row 4);
* and in every case `list_instances` and `get_active_tab` agree.

Suite: unit lane green; the three files that failed on the first full run were the
two `list_instances` smoke pins (they seeded only a cached pair, so they now seed a
live tab whose values differ from it — a shape assertion instead of a tautology)
and the tool-surface golden.

**Golden:** `tests/goldens/tool_surface.json` regenerated
(`python tools/dump_tool_surface.py --write`) for exactly ONE entry —
`list_instances`' description. HARD golden, deliberate, same commit, per
`CONTRIBUTING.md`.

**LOC:** `browser_manager.py` stays at its 1528 cap — the record builder left, the
`is not None` argument arrived. No cap moved.

---

## 5. Blast radius (what a caller saw)

Only `list_instances` reported the stale pair to a client, and it reported it on
every active instance, always. `get_instance_state`'s full (non-partial) record was
never affected: it is built from `PageState`, which reads `window.location.href` and
`document.title` live. Its two `partial` records did carry the cached pair, under
the name `current_url` — that is fixed here by naming, not by reading, since those
records exist precisely because the live read did not answer.

Nothing raised, nothing was logged, and no test caught it: the only two tests that
drove `list_instances` seeded a cached pair and asserted it came back, which is
true of both the defect and the fix.

---

## 6. Not claimed / deliberately unchanged / what remains

* **`BrowserInstance.last_navigated_*` is kept, not deleted.** It is a real answer
  to a real question ("what did the last navigation report"), it is what a
  degraded entry falls back to, and it is what the `stored` tier has. The defect
  was the name and the reader, not the field.
* **`browser_manager.list_instances` (the MANAGER's) still prunes on the OS
  process only, never a CDP probe** — F-611's characterization pin
  (`tests/test_bug_prone_tools.py::test_alive_process_stays_without_cdp_probe`) is
  untouched and still true. This finding changed what the TOOL reports about a
  surviving instance, not which instances survive.
* **`stored` entries carry no `partial` key.** `partial` means "a live read was
  attempted and did not answer"; a stored entry has no browser to attempt one
  against. `source: "stored"` and the `last_navigated_*` names carry that meaning
  instead.
* **The `get_active_tab` Connection hazard is closed as a side effect, not as a
  claim.** Removing `await tab` removes the `TypeError` a rediscovered target
  raised there. No pin is added for it here; it belongs to F-771's family.
* **Follow-up, `scroll_page`:** a second observation from the same session was
  investigated and is real, but is a different defect with a different remedy —
  see `finding_F875_scroll_page_returns_true_without_scrolling.md`. It is
  deliberately NOT fixed in this PR: the honest answer changes `scroll_page`'s
  return from `bool` to a record, which is its own schema change and its own
  deliberate golden update.
