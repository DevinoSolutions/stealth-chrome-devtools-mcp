# F-1009 — a spawn can attach to a SIBLING's Chrome through the DevTools port nodriver picked

**Severity:** High (master-profile-loss class: a later `close_instance` of the wrongly registered browser kills a sibling,
possibly the shared `fleet` or `default` browser). GetItDone task 4802.
**Files:** NEW `embedded/devtools_port.py`; `embedded/browser_connect.py` (`LaunchConfig`, `start`, `_own_endpoint_get`);
`embedded/browser_manager.py` (`_launch_browser`, net zero LOC, still 1438/1438); `embedded/desktop_launch.py`;
`embedded/cdp_endpoint.py`; `NAVMAP.md`, `CHANGELOG.md`.
Tests: NEW `tests/test_devtools_port_identity.py`, `tests/test_devtools_port.py`; `tests/test_desktop_launch.py`,
`tests/test_browser_reattach.py`, `tests/test_doc_claims.py`, `tests/test_spawn_leak.py`.
**Branch:** `fix/F1009-devtools-port-race`. F-1009 was unclaimed in git (`git log --all` had no `F-1009`).

## 1. Cause

`nodriver.Browser.start` sets `port = util.free_port()` (bind, read the port, release) and launches Chrome with
`--remote-debugging-port=<port>`, then polls `HTTP /json/version` on that port. Between the release and Chrome's own bind
any process can take it; a sibling Chrome from a concurrent spawn is the likely one. The squatter answers
`/json/version`, nodriver builds a `Connection` to ITS websocket and the spawn registers that browser as its own
(`spawn_leak` reaps only the launcher pid, so the failure of our own Chrome is invisible). The delegated launch (F-810)
had the same shape with `proxy_forwarder._free_port()` chosen seconds before Chrome bound it; its "also require OUR
launcher pid" check did not stop the attach.

## 2. Fix (one proof, both launch paths)

The first version asked Chrome for `--remote-debugging-port=0` (no port to steal). CI's offline-stealth gate then failed on
Linux, Windows and macOS with `navigator.webdriver == true` (`test_product_offline_stealth_gate`, run 39491828225). The
cause was not established, so that route was dropped: the argv keeps the concrete port nodriver (or the delegated launch)
chose, pinned by `test_chrome_keeps_the_concrete_port_in_its_argv`. What changed is what is believed about an answer:

* `devtools_port.clear_stale` deletes a leftover `DevToolsActivePort` before either launch (fail closed if it cannot be
  removed and still exists).
* For a launch of ours (`browser_connect.LaunchConfig`) the patched `HTTPApi.get` asks nothing of the port until the Chrome
  we started has written `DevToolsActivePort` in OUR profile naming that port with a websocket path
  (`devtools_port.own_endpoint(profile, port)`). Chrome writes it only after it bound the port, so a Chrome that lost the
  race writes nothing and the spawn fails by name (`devtools_port.missing`), inside the existing
  `CONNECT_PATIENCE_SECONDS`.
* `/json/version` must then report the websocket the file names (`devtools_port.verify`); a mismatch raises a `ToolError`
  naming both, BEFORE nodriver builds any `Connection`, so no CDP message (and no `Browser.close`) can reach the foreign
  browser. A failed launch leaves no registered instance, so `close_instance` has nothing to close and `spawn_leak` reaps
  only our own pid. A launcher that exits first is named (`devtools_port.exited`).
* nodriver swallows exceptions from that fetch, so the named failure is kept on `LaunchConfig.failure` and re-raised by
  `browser_connect.start`.
* The delegated launch (F-810) keeps its pre-picked port too, and its readiness poll now also requires
  `desktop_launch._verify_identity` (the file names the port, the answer reports the file's websocket). Only OUR launcher
  pid is ever killed.
* `devtools_port.PORT_FILE` / `read` are the ONE spelling of the file; `cdp_endpoint`'s third rung uses them.

## 3. Pins (hermetic; real `Browser.start` and `BrowserManager._launch_browser` over a faked OS, no Chrome)

Mutation method: runtime rebinding of the named functions through an autouse pytest fixture (`MUT=<name>`), applied in the
test process only (a plugin kept outside the repo), no source file edited; base run 79 passed.

| Required pin | Test | Mutation | Result |
|---|---|---|---|
| stolen port cannot attach us to a sibling | `test_a_stolen_port_cannot_attach_us_to_a_sibling` (sibling holds the port, nothing attached, named failure); `test_our_own_chrome_on_the_port_is_attached` | `nodriver_as_before` (trust whatever answers, as before the fix) | RED, 7 failures incl. this pin |
| stale `DevToolsActivePort` not trusted | `test_a_stale_port_file_is_not_trusted`, `test_a_stale_file_that_matches_the_squatter_exactly_is_still_not_trusted`; desktop `test_a_stale_port_file_is_removed_before_the_launch`; `TestClearStale`; `TestOwnEndpoint` | `no_clear_stale`; `any_port_file` | RED (5; 2) |
| missing file times out within budget, named | `test_a_chrome_that_never_writes_its_port_fails_by_name_inside_the_budget`; launcher-exit variant | `unnamed_timeout`; `no_exit_check` | RED (4; 1) |
| mismatch never touches the foreign browser | `test_a_foreign_answer_on_our_port_is_refused_by_name`, `test_a_mismatch_never_touches_the_foreign_browser` (ProcessTable: sibling `terminate_calls == 0`, ours 1, no connection, no instance, `close_instance` is False), desktop `test_a_squatter_answering_the_port_is_never_attached` | `no_verify` | RED (6) |

`test_spawn_leak::test_a_real_failed_nodriver_start_is_resolved_to_its_pid` now matches "DevToolsActivePort" (the named
reason) instead of nodriver's generic "Failed to connect to browser".

## 4. Residuals (not done, stated)

* A launch that loses the port race now FAILS BY NAME (retryable) instead of attaching to the sibling; it does not
  transparently pick another port, because the port is nodriver's to choose before Chrome starts.
* No pid / `--user-data-dir` check over `SystemInfo.getProcessInfo`: not testable without a real Chrome, and a false
  positive would fail good spawns. File + websocket identity is the witness.
* Not run against real Chrome (rules). The premise that Chrome writes `DevToolsActivePort` only after binding its
  DevTools port, and before the endpoint answers, is the same one `cdp_endpoint` and puppeteer already rely on; the delegated
  Windows path is covered only by the hermetic fakes.
* Why `--remote-debugging-port=0` makes `navigator.webdriver` true was not investigated.
* `clear_stale` deletes the file even if a sibling Chrome is live on the SAME profile (a launch on a held profile is not
  refused); that browser keeps running and only its witness file is removed. Our own Chrome would hand off to it and exit,
  which `exited` names.
* A cdp-attach (`connect_existing`) is untouched: it targets an endpoint that is already open and keeps nodriver's own window.
