# F-1003 — `spawn_browser` reads `main_tab` before a slow-starting Chrome has opened its first page; the attempt is torn down and a healthy Chrome is killed

**Severity:** High (every spawn on a large real profile pays two cold relaunches, and the Chrome killed each time
was coming up fine). Seen 2026-10-18 on the live backend; tracked as GetItDone T-3758.
**Files:** `embedded/browser_connect.py` (`first_page`, `NoFirstPageError`), `embedded/browser_manager.py`
(`spawn_browser`), `NAVMAP.md`, `CHANGELOG.md`.
Tests: `tests/test_browser_connect.py` (+8), `tests/test_spawn_budget.py` (+1), with double fixes in `tests/fakes.py` (`FakeBrowser`, `FakeTab.type_`) and
`tests/test_spawn_budget.py` (`LaunchedChrome`).
**Branch:** `fix/F1003-first-page-target`, from `origin/main` (58387c8).

---

## 1. What happened

`spawn_browser(user_data_dir=<copy of a 275 MB profile>, headed)` returned
`spawn_retries ["Exception: list index out of range", "Exception: list index out of range"]` and succeeded on attempt
3. Each failed attempt's Chrome log shows Chrome reaching its post-start point right when it was killed. Profiles
measured at 275-871 MB, with extensions and restore-on-startup, behave this way.

## 2. Cause

`BrowserManager.spawn_browser` does `browser = await self._launch_browser(...)` and then, on the next line,
`tab = browser.main_tab`.

nodriver 0.47's `Browser.start` calls `update_targets()` exactly ONCE, right after `/json/version` answers, and
`Browser.main_tab` is `sorted(self.targets, key=lambda x: x.type_ == "page", reverse=True)[0]`.

On a large profile Chrome answers `/json/version` before it has created its first page target, so `targets` is empty
and `main_tab` raises `IndexError`. The attempt is torn down, `spawn_leak.reap_launched_browsers` (F-860) kills the
Chrome, and `_SPAWN_ATTEMPTS` launches another from cold. This is the same shape as F-834 stage 2 one milestone later:
the endpoint answered, so the Chrome is not dead, it is not finished.

**The latent shape.** When `targets` holds only non-page targets (`background_page`, `service_worker`, `browser_ui`
— measured present on these profiles at startup), `main_tab` does not raise: the sort puts non-pages after pages but
there is nothing to put first, so it silently returns a NON-page target as the instance's tab. Every later tab tool
then acts on a service worker.

## 3. Fix

One home, convention 4: `browser_connect.py` is already "how long a freshly launched Chrome gets to answer before
this spawn attempt is called failed". It gains ONE function, `first_page(browser) -> Tab`:

* re-reads the targets with nodriver's own `await browser.update_targets()` until a target with `type_ == "page"`
  exists, then returns `browser.main_tab`;
* polls at the module's `POLL_SECONDS` through its `_now`/`_sleep` seam;
* waits for up to the module's `CONNECT_PATIENCE_SECONDS`, counted from when the launch returns (the endpoint has just
  answered). That is a SECOND, sequential wait after the endpoint wait, so one slow attempt can wait up to twice the
  ceiling; the two are the same "Chrome is still coming up", so no second constant and no `STEALTH_MCP_*` knob;
* is decorated with `spawn_budget.phase("first-page wait", "POST_LAUNCH_BUDGET_SECONDS")` (60 s, above the 30 s
  ceiling), because nodriver's `update_targets` has no deadline and a wedged websocket would otherwise park the spawn
  (F-969). The review of this PR found the unbounded first draft;
* gives up at once if the launched Chrome is gone, by `process_exit.browser_is_alive` (the repo's one rule: `_process`
  if there is one, else the recorded pid), raising `NoFirstPageError` that names the milestone ("Chrome answered on
  its DevTools endpoint but opened no page within 30 s" / "... but exited before opening a page");
* is decided by its CALLER, not by `_process`: the first draft skipped any browser with `_process is None` as
  "attached", and the review showed that is wrong for the F-810 delegated headed launch, which `cdp_attach` connects to
  (so nodriver leaves `_process` None and `_process_pid` is stamped) but which is a fresh Chrome of ours with the same
  race. `browser_reattach`'s adoption of an already-open Chrome is the real attach; it reads `main_tab` itself, names
  its own `IndexError`, and never calls `first_page`.

`spawn_browser` calls it in place of the bare `main_tab`. `NoFirstPageError` is a plain `Exception` subclass: it is a
spawn-ATTEMPT failure that the `_SPAWN_ATTEMPTS` loop retries, like nodriver's own "Failed to connect to browser",
not a tool error. `.main_tab` is read once more in `src/` (census by grep: the other hits are the method name
`_replace_main_tab` and prose): `browser_reattach`, an ATTACH that handles `IndexError` by name. No other spawn,
relaunch or heal path reads it after a launch; `_replace_main_tab` opens its own tab.

## 4. Evidence

* Live reproduction above (two `IndexError` retries, third attempt succeeds).
* `tests/test_browser_connect.py` drives nodriver's REAL `Browser.start` and real `update_targets` over the existing
  fake endpoint/clock, with `Target.getTargets` answering from a schedule on the virtual clock and every
  `TargetInfo` built by nodriver's own constructor. Nodes: the page appears after 3 s (the start-time read is empty,
  `main_tab` raises `IndexError`, `first_page` returns the page at ~3 s); only `background_page`/`service_worker` at
  first (the start-time `main_tab` IS the background page, `first_page` returns the page); never a page (raises
  `NoFirstPageError` at the 30 s ceiling); the launcher exits mid-wait (raises within two polls); the launcher had
  already exited (no polling at all); a delegated launch with no `_process` (waited for, liveness by pid); an `update_targets` that never answers (ends in
  the named spawn-budget stage); and, through the real `spawn_browser`, a Chrome that opens no page (the held
  `Browser` is stopped by the one teardown, nothing published); and a source pin that `spawn_browser` calls `first_page` and no longer reads `.main_tab`.
* RED: the new nodes were run against the pre-fix behaviour by runtime rebinding in a `git archive` export of
  `origin/main` (58387c8), binding `browser_connect.first_page` to `return browser.main_tab`: all 9 new nodes (8 in `test_browser_connect.py`, 1 in `test_spawn_budget.py`) failed and the other 25
  nodes of those two files passed. Nothing was mutated in the worktree.
* Three test doubles modelled a launched Chrome with a `main_tab` but no page target (`FakeBrowser`, `LaunchedChrome`,
  and `FakeTab` had no `type_`); they now carry the page the real `Browser` would, which is what lets the existing
  spawn-wiring and spawn-budget nodes keep exercising `spawn_browser`.

## 5. Residuals

A Chrome that opens its page only after `CONNECT_PATIENCE_SECONDS` still fails the attempt (by name) and is retried by
`_SPAWN_ATTEMPTS`; a profile that slow is outside anything measured. The worst case per attempt is the endpoint wait
plus this one.
