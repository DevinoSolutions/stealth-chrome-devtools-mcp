# F-940 — after the browser websocket reconnects, no tab can be opened: `new_tab` and every 25th `navigate` fail for the rest of the instance's life

**Severity:** High for users who hit it. It is not transient: once the browser-level
websocket has reconnected, every later attempt to open a tab fails the same way.
`navigate` takes that path on any navigation whose tracked tab went stale and on the
25th navigation, and because `navigation_count` resets only when the replacement
SUCCEEDS, every navigation after the 25th fails too: the instance cannot navigate
again. The message sent operators to restart a browser that was healthy.
**Files:** `embedded/tab_open.py` (new, the one home for opening a tab),
`embedded/browser_manager.py` (`_replace_main_tab`), `embedded/tool_sections/tabs.py`
(`new_tab`), `embedded/login_persistence.py` (the settings tab);
`tools/check_file_budgets.py` (`browser_manager.py` ratchets DOWN 1463 → 1454,
`- F-940`); `tools/gen_release_contract.py` + `RELEASE_CONTRACT.md` (the
`_replace_main_tab` residual row is closed and removed); `tests/fakes.py`
(`FakeBrowser` now models `createTarget`).
**Sentry:** STEALTH-CHROME-DEVTOOLS-MCP-B4 (4 events, 2.1.18), -B5 (1, 2.1.18),
-B0 (1, 2.1.1), -2K (1, 2.0.5). B4/B5 came from a backend on port 19222, B0 from
one on 61293. `server_name` is scrubbed on every event, so the data does not say
how many machines.

---

## 1. Symptom

| Issue | Release | Message |
|---|---|---|
| -2K | 2.0.5 | `RuntimeError: coroutine raised StopIteration` |
| -B0 | 2.1.1 | `ToolError: Browser has no usable page target (it may be shutting down or its last tab was closed); spawn a new instance or retry.` |
| -B4 | 2.1.18 | same as -B0, 4 events over 12 h |
| -B5 | 2.1.18 | `ToolError: Failed to create new tab: coroutine raised StopIteration` |

-2K and -B0/-B4 are one failure in two wordings: F-818 (commit 998dfbe, "Fixes
STEALTH-CHROME-DEVTOOLS-MCP-2K") caught the bare `RuntimeError` in
`_replace_main_tab` and re-raised it as the "no usable page target" `ToolError`.
-B5 is the same `StopIteration` out of the `new_tab` tool, which wraps any failure
as "Failed to create new tab".

## 2. Cause

All three places that open a tab — `new_tab`, `BrowserManager._replace_main_tab`
and `login_persistence`'s settings tab — called nodriver's
`Browser.get(url, new_tab=True)`. That sends `Target.createTarget` and then finds
the new `Tab` with a bare `next(filter(lambda item: item.type_ == "page" and
item.target_id == target_id, self.targets))`. `browser.targets` is filled by
exactly one thing: nodriver's `Target.targetCreated` handler.

Chrome sends `targetCreated` only on a session that asked for it with
`Target.setDiscoverTargets`, and nodriver asks once, in `Browser.start`. When the
browser-level websocket drops, `Connection.send` reconnects it silently on the next
call, and `Connection._register_handlers` skips the Target domain because it is
"enabled by default". The new session never asks for discovery, no `targetCreated`
arrives, the filter is empty, and `next()` raises `StopIteration` — which PEP 479
turns into `RuntimeError: coroutine raised StopIteration` because it escapes a
coroutine.

Nothing ever re-arms discovery, so the failure lasts for the rest of the instance.

### F-818 diagnosed it as a browser shutting down

F-818's commit reads the `StopIteration` as "no page target left (browser
mid-teardown, last tab closed, targets degraded to raw Connections)". Its pin built
that state by hand. The measurement below shows the field failure is a LIVE browser
with open tabs that has lost discovery; the reworded message told the operator to
spawn a new instance, which works only because the new instance has a fresh session.

## 3. Measurement (Chrome 154, Windows, local)

A probe spawned through `BrowserManager` and opened tabs with
`browser.get("about:blank", new_tab=True)`:

- fresh browser: 0 of 40 opens failed;
- after closing the browser-level websocket once (`browser.connection.websocket.close()`,
  then any `send` — nodriver reconnects): 3 of 3 opens failed with
  `RuntimeError: coroutine raised StopIteration`;
- on the reconnected session `Target.createTarget` itself succeeds — the fix's
  `Target.getTargetInfo` answers for the id it returned and the tab is then driven
  (§5) — so only the lookup fails.

## 4. Fix

`embedded/tab_open.py` is the one home for opening a tab; all three callers use
`tab_open.open_tab(browser, url)`.

- It sends the same `Target.createTarget` with nodriver's own arguments
  (`new_window=False`, `enable_begin_frame_control=True`), so the tab is the one
  `Browser.get` would have opened.
- If the `targetCreated` handler registered a `Tab` for the returned id (the normal
  case: Chrome sends the event before the reply), that `Tab` is returned.
- Otherwise it asks Chrome with `Target.getTargetInfo` and builds the `Tab` exactly
  as the handler does: `ws://{config.host}:{config.port}/devtools/{type_ or 'page'}/{target_id}`,
  `target=info`, `browser=browser`.
- It returns a `Tab`, never a bare `Connection`. If `update_targets` registered the
  new target as a bare `Connection` while `getTargetInfo` was in flight, that entry
  is replaced in place (`type(entry) is Connection` — the exact class, because `Tab`
  is a subclass). If a late `targetCreated` registered a `Tab` meanwhile, that one
  is returned and no second entry is added.
- It ends with `await browser.update_targets()`, which is what `Browser.get` ends
  with.

F-818's catch is removed: the `StopIteration` cannot occur, and any other failure of
`createTarget` keeps its own type so it still reaches Sentry as unexpected.

### Not done, deliberately: re-arming discovery

Sending `Target.setDiscoverTargets(discover=True)` on the reconnected session looks
like the smaller fix. On a fresh session Chrome answers it with a `targetCreated`
for EVERY existing target, and nodriver's handler appends each one again, so
`browser.targets` (and `list_tabs`) would list every open tab twice.

## 5. Evidence

- **RED, real Chrome:** `tests/test_e2e_new_tab_after_reconnect.py` spawns headless,
  sets `NAVIGATION_RECYCLE_THRESHOLD=1`, navigates, drops the browser websocket,
  then navigates again (which recycles the tab) and calls `new_tab`. On the unfixed
  tree it fails with B4's exact message.
- **GREEN, real Chrome:** the same test passes with the fix (1 passed, 125 s).
- **Hermetic:** `tests/test_tab_open.py` pins the opener against a session that
  discovers targets and one that does not (`FakeBrowser(discovers_targets=False)`),
  the in-place replacement of a bare `Connection`, and the no-duplicate rule for a
  late `Tab`. `tests/test_browser_manager_tab_rediscovery.py` pins that
  `_replace_main_tab` opens through `createTarget` and that a refused `createTarget`
  is not reworded into a `ToolError`. `tests/test_tool_errors.py` and
  `tests/test_login_persistence.py` moved to the same `FakeBrowser`.
- **Mutation check (runtime rebinding of `tab_open`'s helpers inside one pytest
  process; no file modified):** four mutants, all killed by `tests/test_tab_open.py`
  — the pre-fix lookup with no `getTargetInfo` fallback, `isinstance` in place of
  the exact-type check, `_register` that always appends, and `_register` that
  overwrites a `Tab` already registered. The unmutated control is green.

## 6. Residual

The reconnected session also misses `Target.targetDestroyed`, so a tab closed by the
page itself (not through `close_tab`) can stay in `browser.targets`. `list_tabs`
and `tab_identity.refreshed` go through `update_targets`, which adds targets it did
not know but does not remove ones Chrome no longer has. Not observed in Sentry; out
of scope here.
