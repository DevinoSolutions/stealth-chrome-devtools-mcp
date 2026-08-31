# F-845 (MED, live-evidenced) — `close_tab` left the active-tab pointer on a dead target

**Status:** FIXED on `fix/F844-F845-tab-state`.
**Found:** 2.0.8, by hand over the real stdio transport (not Sentry — the failure
surfaces as a websocket error from Chrome, which the tool re-raises verbatim).

## Symptom

Close the tab that is currently active, and the instance is wedged. Every
subsequent tab-scoped tool fails with:

```
server rejected WebSocket connection: HTTP 500
```

Measured on 2.0.8 against the hermetic fixture app:

| step | result |
|---|---|
| `list_tabs` | 2 tabs, both live |
| `close_tab(active_id)` | `true` |
| `execute_script` | **`server rejected WebSocket connection: HTTP 500`** |
| `execute_script` (retry) | **same error again** |
| `get_active_tab` | still reports the **closed** tab's id/url |
| `list_tabs` | correct — the closed tab is gone |
| `switch_tab(surviving_id)` | `true` — and the instance works again |

Two things make this worse than a plain error. The "server" in that message is
*Chrome's own DevTools endpoint*, so the wording sends the reader looking at the
MCP backend, which is healthy. And `get_active_tab` keeps confidently naming the
tab that no longer exists, so nothing in the tool surface tells the operator
what happened; only a manual `switch_tab` clears it.

## Cause

`BrowserManager` keeps the instance's active tab in
`self._instances[instance_id]["tab"]`. Exactly one method maintained it:

* `switch_to_tab` (`browser_manager.py:1327-1358` on 2.0.8) re-points it under
  `self._lock` — correct.
* `close_tab` (`browser_manager.py:1372-1397`) sent
  `uc.cdp.target.close_target(target_id)` and returned `True`. It never asked
  whether the target it just destroyed was the stored one, and never re-pointed.
* `get_tab` (`browser_manager.py:1264-1279`) returns the stored object verbatim,
  with no liveness check; `get_active_tab` delegates to it.

So after closing the active tab the pointer references a destroyed target.
`tool_errors._require_tab` sees a truthy object and hands it to the tool, which
opens `ws://…/devtools/page/<dead-target-id>` — and Chrome answers HTTP 500.

`list_tabs` survives because it does not read the stored tab at all: it goes
`get_browser` → `browser.update_targets()` → `browser.tabs`, the path F-771
hardened. That asymmetry is why the defect reads as "some tools broke".

## Fix

One new private method, `BrowserManager._repoint_after_close`, called by
`close_tab` after a successful `close_target`:

1. read the stored tab under `self._lock`; if its target id is not the one just
   closed, return — closing a **non-active** tab must not disturb anything (and
   must not pay for a target refresh);
2. `await browser.update_targets()`, then take the first entry of
   `browser.tabs` whose id is **not** the closed one;
3. store it under `self._lock` — mirroring `switch_to_tab`'s shape, which stays
   the reference for "make this tab the active one". When no page target
   survives, that store is `None`, so `tool_errors._require_tab` raises the
   honest typed error instead of letting a tool reach a dead websocket.

Three details are deliberate:

* **The closed id is excluded explicitly.** nodriver removes a destroyed target
  from `Browser.targets` only when the `Target.targetDestroyed` event arrives
  (`core/browser.py:223-231`); `update_targets()` itself never removes anything
  (it only updates known targets in place and appends unknown ones). The event
  races the refresh, so "the first page target" can still be the corpse.
* **No per-tab `await`** (F-771): after any `close_tab`, `browser.tabs` may
  yield raw `Connection` objects despite its `List[Tab]` annotation. Only
  metadata is read, exactly as `list_tabs` reads it.
* **The call sits outside `close_tab`'s `except` handler.** The tab really is
  closed by then; folding a re-point failure into that handler would report
  `False` for a successful close — the F-775b "lying failure" shape.

`browser_manager.py` is at an exact no-grow LOC cap (1532). The fix pays for
itself in the same file: the boilerplate `Args:/Returns:` docstring blocks on
`list_tabs`, `switch_to_tab`, `get_active_tab` and `close_tab` — which restated
their signatures and nothing else — are replaced by one-line summaries. This is
the payment method already ratified for `process_cleanup.py` under plan_F809.
Net change to the file: **0 lines**; the cap is neither raised nor padded.

## Tests

`tests/test_browser_manager_tab_rediscovery.py`, the file that already owns the
F-771/F-775 tab family, gains four hermetic pins:

* `test_close_tab_repoints_the_active_tab_to_a_survivor` — after closing the
  active tab, `get_active_tab` answers the **surviving** target id (identity,
  not "no exception"), and exactly one `update_targets()` was paid for;
* `test_close_tab_never_repoints_at_the_target_it_just_closed` — the fixture
  leaves the destroyed target listed in `browser.tabs` on purpose, modelling the
  `targetDestroyed` race above; a "first page target wins" scan would reproduce
  the HTTP 500;
* `test_close_tab_leaves_an_unrelated_active_tab_alone` — closing a non-active
  tab leaves the pointer identical and triggers no refresh;
* `test_closing_the_last_tab_leaves_the_honest_typed_error` — no survivor →
  stored `None` → `_require_tab` raises `InstanceNotFoundError`, not a raw
  websocket 500.

RED evidence (pre-fix, same pins): the stored tab is still the `FakeAttachedTab`
for the closed target — `assert <FakeAttachedTab object …> is None` and
`_get_tab_target_id(active) == 'T-other'` failing with `'T-main'`.

GREEN evidence over the real path (real headless Chrome, real stdio, isolated
`gate_workspace` backend on a free port, fixture app): open a second tab,
`switch_tab` to it, confirm `get_active_tab` names it, `close_tab` it —

```
F-845 execute_script after closing the ACTIVE tab:
  {'success': True, 'result': 'fixture-index-page', 'error': None}
F-845 get_active_tab after close:
  {'tab_id': 'C3A2BCD…', 'url': 'http://127.0.0.1:16568/index.html',
   'title': 'fixture-index-page', 'type': 'page'}
```

— i.e. the two calls that answered `server rejected WebSocket connection:
HTTP 500` and a stale tab id on 2.0.8 now answer from the surviving tab.

Note what the existing coverage could not have caught: `test_tabs_lifecycle`
(`tests/test_e2e_interaction.py`) and the release gate's soak churn both close a
tab, but the E2E node deliberately switches **away** first, and the soak's
post-close assertions (`isinstance(inst_state, dict)`,
`active.get("tab_id")`) are satisfied by a stale pointer. Closing the tab that
is *currently active* and then asserting *which* tab answers is the gap.

## Residuals (deliberately out of scope)

* **The message itself.** `server rejected WebSocket connection: HTTP 500` still
  reaches users verbatim from any *other* path that hands a stale tab to CDP —
  the wording names Chrome's DevTools endpoint as "server", which reads as the
  MCP backend. Classifying that string into an actionable `ToolError` is a
  separate finding (the F-828/F-816 message-classification family).
* **`get_tab` has no general liveness check.** This fix repairs the one writer
  that was known to invalidate the pointer. A tab destroyed by *the page* (a
  `window.close()`, a crashed renderer) still leaves a stale pointer, and
  `get_tab` would still return it. A liveness probe on every `get_tab` would put
  a CDP round trip on the hottest read in the tool surface; scoping this fix to
  the writer keeps the read free. Recorded rather than fixed.
* **`_require_tab`'s message.** A stored `None` produces
  `Instance not found: <id>`, but the instance exists — only its tab is gone.
  The shape is typed and honest about "there is nothing to act on"; a distinct
  "no active tab in this instance" message would be a nicer read, and belongs
  with the `tool_errors` message pass rather than here.
* **The re-pointed tab may be a raw `Connection`.** `switch_to_tab` has always
  stored `browser.tabs` entries, and the `get_active_tab` *tool* then does
  `await tab` (`server.py:1767`), which only `Tab` supports. This fix inherits
  that existing characteristic rather than adding to it; it is the F-771 family's
  remaining tail.
