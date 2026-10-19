# F-1003 — `spawn_browser` reads `main_tab` before a slow-starting Chrome has opened its first page; the attempt is torn down and a healthy Chrome is killed

**Severity:** High (every spawn on a large real profile pays two cold relaunches, and the Chrome killed each time
was coming up fine). Seen 2026-10-18 on the live backend; tracked as GetItDone T-3758.
**Files:** `embedded/browser_connect.py` (`first_page`, `NoFirstPageError`), `embedded/browser_manager.py`
(`spawn_browser`), `NAVMAP.md`, `CHANGELOG.md`.
Tests: `tests/test_browser_connect.py` (+7), with double fixes in `tests/fakes.py` (`FakeBrowser`, `FakeTab.type_`) and
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
* is bounded by the module's `CONNECT_PATIENCE_SECONDS`, counted from when the launch returns (the endpoint has just
  answered). No second constant and no `STEALTH_MCP_*` knob: the endpoint answering and the first page are the same
  "Chrome is still coming up", so one ceiling;
* gives up at once if the launched process has exited (`browser._process.returncode is not None`, the rule the
  endpoint wait already uses), raising `NoFirstPageError` that names the milestone ("Chrome answered on its DevTools
  endpoint but opened no page within 30 s" / "... but exited (code N) before opening a page");
* for an ATTACHED browser (no `_process`, the `cdp_attach` door) is just `browser.main_tab`, exactly as before, so
  `browser_reattach`'s own `IndexError` handling is untouched.

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
  already exited (no polling at all); an attached browser (no waiting, no `update_targets`, the old `IndexError` on an
  empty list); and a source pin that `spawn_browser` calls `first_page` and no longer reads `.main_tab`.
* RED: the new nodes were run against the pre-fix behaviour by runtime rebinding in a `git archive` export of
  `origin/main` (58387c8), binding `browser_connect.first_page` to `return browser.main_tab`: 6 of 7 failed and the
  attach node passed, as it must (that door is unchanged). Nothing was mutated in the worktree.
* Three test doubles modelled a launched Chrome with a `main_tab` but no page target (`FakeBrowser`, `LaunchedChrome`,
  and `FakeTab` had no `type_`); they now carry the page the real `Browser` would, which is what lets the existing
  spawn-wiring and spawn-budget nodes keep exercising `spawn_browser`.

## 5. Residuals

`first_page` is not under a `spawn_budget.phase`; its own 30 s ceiling is the bound, and a `update_targets()` that
never answers (a wedged websocket) would be bounded only by the tool's outer cancel, as the preceding `Browser.start`
read already is.
