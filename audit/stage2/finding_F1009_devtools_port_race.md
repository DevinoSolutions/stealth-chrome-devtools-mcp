# F-1009 — a spawn can attach to a SIBLING's Chrome through the DevTools port nodriver picked

**Severity:** High (master-profile-loss class: a later `close_instance` of the wrongly registered browser kills a sibling,
possibly the shared `fleet` or `default` browser). GetItDone task 4802.
**Files:** NEW `embedded/devtools_port.py`; `embedded/browser_connect.py` (`start`); `embedded/browser_manager.py`
(`_launch_browser` calls `browser_connect.start`, net zero LOC, still 1438/1438); `embedded/desktop_launch.py`;
`NAVMAP.md`, `CHANGELOG.md`.
Tests: NEW `tests/test_devtools_port_identity.py`, `tests/test_devtools_port.py`; `tests/test_desktop_launch.py`,
`tests/test_spawn_budget.py` and `tests/fakes.py` (the fakes now model a browser that can say which process it is).
**Branch:** `fix/F1009-devtools-port-race`. F-1009 was unclaimed in git (`git log --all` had no `F-1009`).

## 1. Cause

`nodriver.Browser.start` sets `port = util.free_port()` (bind, read the port, release) and launches Chrome with
`--remote-debugging-port=<port>`, then polls `HTTP /json/version` on that port. Between the release and Chrome's own bind
any process can take it; a sibling Chrome from a concurrent spawn is the likely one. The squatter answers
`/json/version`, nodriver builds a `Connection` to ITS websocket and the spawn registers that browser as its own
(`spawn_leak` reaps only the launcher pid, so the failure of our own Chrome is invisible). The delegated launch (F-810)
had the same shape with `proxy_forwarder._free_port()` chosen seconds before Chrome bound it; its "also require OUR
launcher pid" comment did not stop the attach, because the pid is read from the launcher and the endpoint was never asked.

## 2. What was tried and why it was dropped (both measured in CI, PR #256)

1. **`--remote-debugging-port=0`** (nothing to steal; read the port from `DevToolsActivePort`). CI's
   `test_product_offline_stealth_gate` then reported `navigator.webdriver == true` on Linux, Windows and macOS (run
   39491828225). The cause was not established; not shippable.
2. **Concrete port + wait for `DevToolsActivePort` naming it.** Chrome writes that file only for port 0 (`cdp_endpoint`'s
   NAVMAP row already records it absent on a running browser started with `--remote-debugging-port=9223`). Every real
   spawn then failed with "did not write DevToolsActivePort … within 30 s" (run 39494983718, macOS offline-stealth and
   integration; the Windows jobs died with no evidence). Dropped, and every file-based piece with it.

## 3. Fix (one proof, both launch paths)

The one thing a squatter cannot fake is which process it is. `devtools_port.confirm_ours(browser, launched_pid)`:

* sends `SystemInfo.getProcessInfo` (read-only, answered by the browser process itself) on the connection nodriver already
  built, bounded by `IDENTITY_TIMEOUT_SECONDS` (10 s, inside the 120 s launch phase);
* requires the pid of the `browser` process to be `launched_pid` or a descendant of it (`owned_pids`, psutil; a wrapper
  that forks the real binary is the descendant case);
* on a mismatch calls `Connection.disconnect()` (a websocket close; NOT `Browser.close`, which is a command that would
  shut the sibling down) and raises a `ToolError` naming both pids; on silence it disconnects and raises a named
  `ToolError`;
* treats "no launched pid", "the browser does not offer the query" (`ProtocolException`) and "no `browser` process in
  the answer" as unverifiable and allows them, logging the first two (a hardening must not turn into an outage).

Callers: `browser_connect.start` (`nodriver.start` then `confirm_ours`; `BrowserManager._launch_browser` calls it in place
of `uc.start`) and `desktop_launch.launch_and_attach` (after `cdp_attach.attach`, before `attached = True`, so the
existing `finally` kills only OUR identity-stamped pid). A failed `_launch_browser` leaves no registered instance, so
`close_instance` has nothing to close, and `spawn_leak` reaps only the launched pid.

## 4. Pins (hermetic; real `Browser.start` and `BrowserManager._launch_browser` over a faked OS, no Chrome)

Mutation method: runtime rebinding of `devtools_port` names through an autouse fixture in a pytest plugin kept OUTSIDE the
repo (`MUT=<name>`), applied in the test process only, no source file edited. Base run 69 passed
(`test_devtools_port.py`, `test_devtools_port_identity.py`, `test_desktop_launch.py`).

| Required pin | Test | Mutation | Result |
|---|---|---|---|
| stolen port cannot attach us to a sibling | `test_a_stolen_port_cannot_attach_us_to_a_sibling`, `test_our_own_chrome_on_the_port_is_attached`; desktop `test_a_squatter_holding_the_port_is_never_attached` | `no_check` (`confirm_ours` does nothing, i.e. the old behaviour) | RED, 9 failures incl. these |
| stale `DevToolsActivePort` not trusted | `test_a_stale_port_file_is_not_trusted` (a file naming the squatted port sits in the profile; the sibling is still refused) | `file_trust` (believe a file naming the port) | RED (this pin fails; the mutation also breaks fakes that have no profile, which is noise) |
| no answer within budget, named | `test_a_browser_that_never_says_who_it_is_times_out_by_name`, `test_the_identity_wait_fits_inside_the_launch_phase` | `unnamed_timeout` (bare `TimeoutError`) | RED (1) |
| mismatch never touches the foreign browser | `test_a_refused_foreign_connection_is_dropped_not_closed` (no `Browser.*` command sent, connection disconnected), `test_a_mismatch_never_touches_the_foreign_browser` (`ProcessTable`: sibling `terminate_calls == 0` and alive, ours reaped once, no instance, `close_instance(...) is False`) | `close_foreign` (sends `Browser.close`); `no_disconnect`; `kill_foreign` | RED (3; 5; the ProcessTable pin plus 6 others) |
| descendant of the launcher is ours | `TestOwnedPids::test_a_wrapper_owns_what_it_forked` (a real child process), `test_a_descendant_of_the_launched_pid_passes` | `no_descendants` | RED (1) |

Incident to state: the `kill_foreign` mutation terminated "every process but ours" through the real
`psutil.process_iter()` in the tests that do not patch psutil. It raised on its first process (an access-denied system
entry) before reaching any user process, and a process census afterwards showed the machine intact, but the mutation was
unsafe and was not re-run. The `ProcessTable` pin that matters runs against a patched psutil and is what went RED.

## 5. Residuals (not done, stated)

* A launch that loses the port race now FAILS BY NAME (retryable) instead of attaching to the sibling; it does not
  transparently pick another port, because the port is nodriver's to choose before Chrome starts.
* nodriver's own `Browser.start` sends read-only `Target.setDiscoverTargets` / `getTargets` to whatever answers before the
  check runs; nothing that closes or navigates anything is sent.
* Fail-open when the browser has no `SystemInfo.getProcessInfo` or reports no `browser` process. Real Chrome has both.
* A launcher that daemonizes (the pid we hold exits and the browser is not its descendant) would be refused as foreign.
  nodriver launches the binary directly on every platform here; the delegated launch records the Chrome pid itself.
* Not run against real Chrome (rules): the premise that `SystemInfo.getProcessInfo` reports the browser process pid is the
  protocol's documented behaviour and is exercised in CI by every integration spawn, which now sends it.
* Why `--remote-debugging-port=0` makes `navigator.webdriver` true was not investigated.
* The delegated Windows path is covered only by the hermetic fakes.
