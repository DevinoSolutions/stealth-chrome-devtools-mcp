# F-824 — the F-817 nodriver-race classifier never reached the navigate path

**Status: FIXED** on `fix/F824-F828-classifier-reach` (2026-08-31), opened by the
2.0.8 Sentry triage of the 2.0.7 fleet.
**Severity: MEDIUM** — bounded and non-corrupting (nothing wedges, no state is
left behind), but a `navigate` call fails outright with a raw `KeyError` whose
text is a class repr. It is the single most-used tool in the product, and the
recovery for this exact race had already shipped one module away.

---

## What is proven

`STEALTH-CHROME-DEVTOOLS-MCP-3N` — 4 events, first seen 2026-08-14, last seen
2026-08-30, releases 2.0.6/2.0.7:

```
Error calling tool 'navigate'
KeyError: <class 'nodriver.cdp.page.FrameStoppedLoading'>

  server.py, line 650, in navigate
  browser_manager.py, line 1209, in navigate
              await asyncio.wait_for(tab.get(url), timeout=timeout_seconds)
  nodriver\core\tab.py, line 476, in get
              await self
  nodriver\core\tab.py, line 1254, in wait
              self.remove_handler(wait_events, handler=handler)
  nodriver\core\connection.py, line 315, in remove_handler
                  del self.handlers[evt_dom]
```

That is, line for line, the race F-817 identified and fixed for selector
resolution: `Tab.wait` registers page-event handlers and drops them in a
`finally` via `Connection.remove_handler`, whose cleanup is a bare
`del self.handlers[evt_dom]`. The delete removes the whole key rather than one
handler, so an overlapping `wait` on the same tab finds the key gone and raises
`KeyError(<cdp event class>)`.

The sibling issue `STEALTH-CHROME-DEVTOOLS-MCP-4K` is the same race one frame
over (`get_active_tab` → `await tab` → `Tab.wait` → `remove_handler`), which is
how we know the exposure is the *await-the-tab* surface, not `navigate` itself.

---

## Root cause

`navigate` already had a one-shot recovery — it just used its own private
notion of "recoverable":

```python
# browser_manager.py, before
@staticmethod
def _is_recoverable_navigation_error(error: Exception) -> bool:
    if isinstance(error, asyncio.TimeoutError):
        return True
    message = f"{type(error).__name__}: {error}".lower()
    recoverable_markers = (
        "connection dropped", "connection closed", "connection lost",
        "websocket", "target closed", "target crashed", "session closed",
        "invalid state", "not attached",
    )
    return any(marker in message for marker in recoverable_markers)
```

`str(KeyError(cdp.page.FrameStoppedLoading))` is
`"<class 'nodriver.cdp.page.FrameStoppedLoading'>"` — it matches none of those
markers, so `_is_recoverable_navigation_error` returned `False` and the loop
re-raised on attempt 1 of 2. Nothing was wrong with the retry *mechanism*; the
verdict was reached by a classifier that had never heard of the nodriver races.

Why the F-817 recovery could not reach it: `element_resolution` is the one home
for *selector* resolution, and `navigate` resolves no selector. Both racy
surfaces it does touch — `tab.get(url)` (which awaits the tab) and
`_wait_for_navigation_condition`'s `tab.wait(...)` — sit inside `navigate`'s own
try/except.

---

## The fix

One classifier, asked from both places.

* `element_resolution._recoverable_race` becomes the public
  `element_resolution.recoverable_race(exc) -> str | None`, widened to accept
  any exception (it returns `None` for anything that is not one of the two known
  races) so a caller that catches broadly can ask it directly.
* `_is_recoverable_navigation_error` consults it:

```python
if isinstance(error, asyncio.TimeoutError) or recoverable_race(error):
    return True
```

The marker list stays where it is and keeps its own meaning (transport/target
failures that a fresh tab clears). Nothing about the nodriver races is
re-listed in `browser_manager` — the module docstring of `element_resolution`
now names `navigate` as the second caller so the coupling is documented at the
one home rather than discovered.

**Retry budgets are untouched.** `navigate` still makes at most 2 attempts,
`element_resolution` still re-resolves at most `_MAX_RESOLVES` (3) times. Only
the classification reaches further.

---

## Pins

`tests/test_navigate_race_recovery.py` (hermetic; a `FakeTab` whose first
`get()` raises the real nodriver exception objects):

| test | asserts |
|---|---|
| `test_navigate_recovers_from_the_nodriver_handler_race` | the 3N shape → `success`, `get` called twice |
| `test_navigate_recovers_from_the_stale_document_race` | the `-32000` half on the same path |
| `test_navigate_still_refuses_to_retry_an_unrelated_failure` | `RuntimeError` → raised on attempt 1, `get` called once |
| `test_navigate_uses_element_resolutions_classifier_object` | identity: one classifier, no copy |
| `test_the_navigation_classifier_answers_for_both_known_races` | the predicate itself, both races + both non-races |
| `test_navigate_stops_recovering_when_the_one_classifier_says_no` | delegation is live: neutralise the shared function and the recovery goes away |

RED before the fix: the first two failed with the raw `KeyError` /
`ProtocolException`, the last three with
`module 'browser_manager' has no attribute 'recoverable_race'` and
`classify(_handler_race()) is False`.

---

## Residual — the same race on paths this finding does not cover

`STEALTH-CHROME-DEVTOOLS-MCP-4K` (`get_active_tab`) shows the handler race also
escaping from `await tab` inside `server.py`'s `_with_cdp_timeout` call sites.
That is a *third* home for the question ("what does a tool do when awaiting a
tab races?") and the honest fix is not another `try` in `server.py` — which is
at its LOC cap and would need a fourth copy of the decision — but a wrapper that
owns "await this tab, tolerating the known race" the way `element_resolution`
owns "resolve this selector, tolerating them". Deliberately **not** done here:
it is a design choice about where that seam lives, not a classification bug, and
it wants its own finding (candidate: F-8xx, `await-tab` seam).
