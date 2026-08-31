# F-833 — `go_back` / `go_forward` / `reload_page` / `new_tab` never got `navigate`'s truthfulness guard

**Home:** `src/stealth_chrome_devtools_mcp/embedded/tool_errors.py`
(`_require_navigation_ok`, the one error-page detector, plus the new
`_require_landing_ok` entry point) and the four tool bodies in
`embedded/server.py`.

**Status:** fixed on `fix/F833-navigation-truthfulness` (branched from
`fix/F832-deep-serialization`).

---

## The defect

F-802 (shipped 2.0.1) made `navigate` truthful. A navigation Chrome cannot
perform — the host does not resolve, the connection is refused, the TLS
handshake fails — still *completes*: Chrome commits a `chrome-error://` page,
every Python-side step around it succeeds, and the tool used to answer
`{"url": "chrome-error://chromewebdata/", "success": true}`. The fix put one
guard in `tool_errors.py` and called it from `navigate`.

Four other tools move a tab exactly the same way, and none of them got it:

| Tool | How it lands on an error page | What it answered |
|---|---|---|
| `go_back` | a history entry whose host is now dead / offline | `True` |
| `go_forward` | the same, forward | `True` |
| `reload_page` | reloading while offline, or after the host went away | `True` |
| `new_tab` | the initial `url` will not load | the tab's payload, plus a stranded tab |

Same defect class, four more surfaces. The caller's next tool call (a
`query_elements`, a `get_page_content`) then runs against Chrome's error page
and reports *its* result as the truth about the page the caller thinks they are
on — which is the failure mode that makes a wrong "success" worse than an error.

`new_tab` had a second half: the tab is created *before* the navigation is
known to have worked, so the honest answer ("this did not load") has to not
leave the half-open tab behind, or the fix would only move the defect.

## The fix

One detector, five call sites. `_require_navigation_ok` — F-802's guard, and the
only place in the tree that knows what `chrome-error://` means — is unchanged in
substance; it now accepts either a navigation payload (`{"url": …}`) or the
landed URL itself, because a history move and a reload have a URL but no
payload.

The five tab-moving tools reach it through one new async entry point,
`tool_errors._require_landing_ok(landed, target, timeout, close_on_error=False)`,
which absorbs the *only* thing the five differ in — whether the landed URL is
already in hand or still has to be read off the tab:

* `navigate` hands in the payload `BrowserManager` already built (unchanged
  behaviour, one fewer name imported into `server.py`);
* `go_back` / `go_forward` / `reload_page` / `new_tab` hand in the tab.

For a tab, the landing is read with `window.location.href` — the same expression
`BrowserManager.navigate` reads its own final URL through. A tab's cached
`target.url` is refreshed by `update_targets()`, not by a history move, so it
would answer for the page the tab used to be on. The read is preceded by
`await tab` (nodriver's `Tab.wait()`: it returns on the first navigation event,
or after 0.5s) because `Tab.back()` is a bare
`Runtime.evaluate("window.history.back()")` that returns *before* Chrome has
committed anything, and it is bounded by `CDP_OPERATION_TIMEOUT` so the guard
that made the tool truthful cannot make it hang.

`close_on_error=True` — passed only by `new_tab`, the one of the four that
*creates* the tab — closes it before the raise. A history move onto an error
page deliberately does NOT close the tab: it is still the caller's tab, and they
can recover from it (go forward again, navigate elsewhere).

### Messages

Every one of the five raises `navigate`'s sentence, verbatim past the name of
the move:

```
Navigation to <target> failed: Chrome loaded an error page
(chrome-error://chromewebdata/). The host may not resolve, the connection may
have been refused, or the TLS handshake may have failed.
```

`navigate` and `new_tab` name the URL they were asked for. A history move
cannot — where it was going is only knowable after it got there — so it names
the direction instead (`the previous page`, `the next page`, `the reloaded
page`), which is the actionable half that exists. FastMCP surfaces the raise
under the tool's own name, so the direction is stated twice.

## Contract oddities found (NOT changed here)

**A history move with nowhere to go still reports success.** `Tab.back()` is a
bare `window.history.back()`. With no entry behind it, nothing happens, the URL
does not change, and `go_back` answers `True` for a move that never occurred.
That is untruthful in a *different* way — a no-op reported as a move, not a
failure reported as a success — and closing it means reading
`Page.getNavigationHistory` before the move and comparing entry indices
afterwards, which is a separate change with its own contract (what should
`go_back` do at the start of history: raise, or return `False`?). The current
behaviour is pinned as characterization in
`test_a_history_move_with_nowhere_to_go_still_reports_success` so the fix that
closes it has to update that node on purpose.

**F-800 is still open and untouched.** `reload_page(ignore_cache=…)` accepts the
argument and drops it (`tab.reload()` is called with no arguments, and
nodriver's own default is `ignore_cache=True`). Out of scope here; the fake's
`reload()` deliberately records only the fact of the reload so this file does
not accidentally pin the broken half.

## Residual risk

**The settle is a race, and it fails in the safe direction.** `await tab` waits
for the first navigation event or 0.5s. If Chrome has not committed the error
page by then, the guard reads the *old* URL — a real page — and the tool reports
success. So the guard can still miss a failure; it cannot invent one. Error
pages commit without a network round trip (that is what makes them error pages),
so the window is narrow in the cases the defect is actually about.

**Hermetic tier only.** What only a real browser can prove — that Chrome really
commits `chrome-error://chromewebdata/` — is F-802's evidence in
`tests/test_truthful_success_flags.py`, and the four tools inherit it by routing
through the same guard. A real-Chrome node for a history move onto a dead entry
(navigate to a live page, navigate to `.invalid`, `go_back`, `go_forward`) would
add end-to-end evidence and is the natural follow-up in that file.

## Tests

`tests/test_navigation_truthfulness.py` (31 nodes, hermetic, driven through the
real tool bodies via `fakes.call_tool`). RED on the base branch: 14 failed / 17
passed — the 17 are the truthful-half nodes, which must be green before and
after.

* the error-page raise, parametrized over all four tools;
* which move failed is named, parametrized over all four;
* `test_all_five_speak_with_one_voice` — the four messages compared against
  `navigate`'s own, so a second hand-rolled `startswith("chrome-error://")`
  anywhere shows up here as a message that drifted;
* the truthful half: a 404, a redirect, `data:` and `about:blank` are landings,
  not failures, for every tool;
* `new_tab` closes the tab it could not land (the browser listing is asserted
  *empty*, not just `closed is True`), and the history moves do NOT close a tab
  they did not open;
* a landing that never answers raises instead of hanging;
* the no-history contract above, pinned as characterization.

`tests/fakes.py` gains the tab-move seams (`FakeTab.back/forward/reload/close`,
`move_calls`, `closed`, `opened_by`; `FakeBrowser(opened_tab=…)`).
`FakeTab.back()` deliberately does NOT update `.url` — the real one does not
either, and a fake that did would hide the race the guard has to survive.

## Budgets

`embedded/server.py` is **net zero** and stays at its 3411/3411 cap: the guard
call replaces `return True` in the three history tools, replaces `new_tab`'s
redundant `await _with_cdp_timeout(tab, …)` settle (the guard does that wait
itself), and `_require_landing_ok` replaces `_require_navigation_ok` in the
import list rather than joining it. `embedded/tool_errors.py` 118 → 200 of its
1000 budget.
