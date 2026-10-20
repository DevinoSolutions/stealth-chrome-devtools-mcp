# F-1009 — a spawn can attach to a SIBLING's Chrome through the DevTools port nodriver picked

**Severity:** High (master-profile-loss class: a later `close_instance` of the wrongly registered browser kills a sibling,
possibly the shared `fleet` or `default` browser). GetItDone task 4802.
**Files:** NEW `embedded/devtools_port.py`; `embedded/browser_connect.py` (`LaunchConfig`, `start`, `_own_endpoint_get`);
`embedded/browser_manager.py` (`_launch_browser`, net zero LOC, still 1438/1438); `embedded/desktop_launch.py`;
`embedded/cdp_endpoint.py`; `embedded/browser_cmdline.py`; `NAVMAP.md`, `CHANGELOG.md`.
Tests: NEW `tests/test_devtools_port_identity.py`, `tests/test_devtools_port.py`; `tests/test_desktop_launch.py`,
`tests/test_live_master_seed.py`, `tests/test_browser_reattach.py`, `tests/test_doc_claims.py`, `tests/test_spawn_leak.py`.
**Branch:** `fix/F1009-devtools-port-race`. F-1009 was unclaimed in git (`git log --all` had no `F-1009`).

## 1. Cause

`nodriver.Browser.start` sets `port = util.free_port()` (bind, read the port, release) and launches Chrome with
`--remote-debugging-port=<port>`, then polls `HTTP /json/version` on that port. Between the release and Chrome's own bind
any process can take it; a sibling Chrome from a concurrent spawn is the likely one. The squatter answers
`/json/version`, nodriver builds a `Connection` to ITS websocket and the spawn registers that browser as its own
(`spawn_leak` reaps only the launcher pid, so the failure of our own Chrome is invisible). The delegated launch (F-810)
had the same shape with `proxy_forwarder._free_port()` chosen seconds before Chrome bound it; its "also require OUR
launcher pid" check did not stop the attach.

## 2. Fix (one way, both launch paths)

* Chrome is asked for port `0` (`devtools_port.with_port_zero`, used by `browser_connect.LaunchConfig.__call__` and
  `desktop_launch.launch_and_attach`).
* The port is whatever Chrome writes to `DevToolsActivePort` (line 1 port, line 2 `/devtools/browser/<guid>`) in OUR
  profile. `devtools_port.clear_stale` deletes a leftover file before the launch (fail closed if it cannot be removed and
  still exists); `own_endpoint` insists on a path.
* `/json/version` on that port must report the websocket the file names (`devtools_port.verify`); a mismatch raises a
  `ToolError` naming both, BEFORE nodriver builds any `Connection`, so no CDP message (and no `Browser.close`) can reach
  the foreign browser. A failed launch leaves no registered instance, so `close_instance` has nothing to close and
  `spawn_leak` reaps only our own pid.
* The wait is inside the existing `CONNECT_PATIENCE_SECONDS`; a launcher that exits or a Chrome that never writes the file
  is named (`devtools_port.exited` / `missing`). nodriver swallows exceptions from that fetch, so the named failure is kept
  on `LaunchConfig.failure` and re-raised by `browser_connect.start`.
* Consumers that read the port off argv: a browser on `=0` has none, so `browser_cmdline.debug_port(pid, expect_dir)` falls
  back to the profile's file (only when the argv value is `0` and the pid was already joined to that profile).
  `devtools_port.PORT_FILE` / `read` are now the ONE spelling of the file; `cdp_endpoint`'s third rung uses them.

## 3. Pins (hermetic; real `Browser.start` and `BrowserManager._launch_browser` over a faked OS, no Chrome)

Mutation method: runtime rebinding of the named functions through an autouse pytest fixture (`MUT=<name>`), applied in
the test process only, no source file edited; base run 93 passed.

| Required pin | Test | Mutation | Result |
|---|---|---|---|
| stolen port cannot attach us to a sibling | `test_a_stolen_port_cannot_attach_us_to_a_sibling`, `test_chrome_is_not_handed_a_port_somebody_else_can_take` | `old_port` (nodriver's pre-picked port, no file, no identity); `read_none` | RED (11 and 9 failures) |
| stale `DevToolsActivePort` not trusted | `test_a_stale_port_file_is_not_trusted`, `..._alone_is_not_an_endpoint`; desktop `test_a_stale_port_file_is_removed_before_the_launch`; `TestClearStale` | `no_clear_stale` | RED (5 failures) |
| missing file times out within budget, named | `test_a_chrome_that_never_writes_its_port_fails_by_name_inside_the_budget`; launcher-exit variant | `unnamed_timeout`; `no_exit_check` | RED (2; 1) |
| mismatch never touches the foreign browser | `test_a_foreign_answer_on_our_port_is_refused_by_name`, `test_a_mismatch_never_touches_the_foreign_browser` (ProcessTable: sibling `terminate_calls == 0`, ours 1, no connection, no instance, `close_instance` is False), desktop `test_a_foreign_answer_on_the_files_port_is_refused_and_never_attached` | `no_verify` | RED (6) |

RED on the unmodified code (before the fix): all 8 identity pins failed (connected to the sibling's websocket; argv port
`14894` not `0`; DID NOT RAISE). Also pinned: `TestDebugPortFindsAPortZeroBrowser`,
`test_a_browser_launched_on_port_zero_is_found_through_its_port_file`.
A deliberately changed pin: `test_spawn_leak::test_a_real_failed_nodriver_start_is_resolved_to_its_pid` now matches
"DevToolsActivePort" (the named reason) instead of nodriver's generic "Failed to connect to browser".

## 4. Residuals (not done, stated)

* No pid / `--user-data-dir` check over `SystemInfo.getProcessInfo`: not testable without a real Chrome, and a false
  positive would fail good spawns. File + websocket identity is the witness.
* Not run against real Chrome (rules): the premise that Chrome launched with `=0` writes `DevToolsActivePort` before its
  endpoint answers is Chrome's documented behaviour (puppeteer relies on it); the delegated Windows path is covered only by
  the hermetic fakes.
* `clear_stale` deletes the file even if a sibling Chrome is live on the SAME profile (a launch on a held profile is not
  refused); that browser keeps running and only its witness file is removed. Our own Chrome would hand off to it and exit,
  which `exited` names.
* A cdp-attach (`connect_existing`) is untouched: it targets an endpoint that is already open and keeps nodriver's own window.
