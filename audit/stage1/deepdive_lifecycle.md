# Deep Dive DD-5: Lifecycle, Cleanup & Error-Handling

Scope: `browser_manager.py`, `process_cleanup.py`, the cross-cutting broad/silent
exception pattern across `embedded/`, `response_handler.py` disk-fallback behavior,
and `debug_logger.py` operability. Read-only static analysis; no browsers spawned,
no processes killed, per task rules. Findings filed as F-180..F-184 in
`findings_dd5.json`.

## 1. Teardown: does `close_instance` actually bound to 5 seconds?

`BrowserManager` has no `__aenter__`/`__aexit__` — confirmed by grepping the whole
`embedded/` package; the only hits for those tokens are an unrelated comment in
`singleton.py`. It is a plain class with `close_instance`, `close_all`, and
`cleanup_inactive` as the teardown surface. `close_all` (browser_manager.py:1327-1335)
is a sequential loop over `close_instance`, not concurrent, so its own worst case is
N times whatever `close_instance` costs.

`close_instance` (browser_manager.py:605-786) wraps its inner `_do_close()` coroutine
in `asyncio.wait_for(_do_close(), timeout=5.0)` (line 760). That reads as a hard
bound, and for the parts of `_do_close` that are genuine `await`s on CDP calls it is
one — `browser.connection.send(cdp_browser.close())` and `.disconnect()` are each
independently wrapped in their own `asyncio.wait_for(..., timeout=2.0)` (lines
654-657, 668-670).

The bound breaks down immediately after, at line 682:
`process_cleanup.kill_browser_process(instance_id)` — called synchronously, not
awaited, not dispatched via `asyncio.to_thread`/`run_in_executor`. Grepping the
entire `embedded/` package confirms `to_thread`/`run_in_executor` are used exactly
five times, all in `server.py`, none of them wrapping anything in
`browser_manager.py` or `process_cleanup.py`. `kill_browser_process` walks down into
`_kill_processes_for_metadata` → `_kill_process_by_pid`, which does
`process.terminate(); process.wait(timeout=3)` then `process.kill();
process.wait(timeout=2)` — each individually bounded via `psutil`'s own timeout
argument, but purely synchronous Python. `_kill_processes_for_metadata` loops this
**sequentially over every PID sharing the instance's `--user-data-dir`**
(process_cleanup.py:419-421), and Chrome's GPU/renderer/utility child processes
routinely inherit the same `--user-data-dir` flag as the parent — so a single
`close_instance` call against a wedged Chrome can walk N processes at up to 5s each,
entirely on the event-loop thread. `_stop_browser`'s `browser.stop()` (line 688,
into the third-party `nodriver` library) gets no bounding at all, in contrast to the
CDP calls immediately above it in the same function — the authors clearly knew to
bound awaitable CDP ops but didn't extend that discipline to the synchronous
process-kill/profile-cleanup path right next to it. `_cleanup_profile_dir` adds its
own synchronous `time.sleep(0.15)` retry loop (process_cleanup.py:463, 483, up to 5
attempts) on top.

The mechanism matters because Python's asyncio is single-threaded and cooperative:
`asyncio.wait_for`'s timeout fires via a loop-scheduled callback, and that callback
cannot run while the loop is stuck inside a synchronous call — so the "5 second"
budget is fictional whenever the slow part is one of these synchronous calls. Worse,
the `except asyncio.TimeoutError` fallback (lines 761-782, quoted in F-180) —
meant to force-cleanup after a real timeout — calls the *same* synchronous
`kill_browser_process`/`finalize_browser_process`/`cleanup_deferred_profiles`
functions a second time, now with no timeout wrapper at all.

`close_instance` is not shutdown-only plumbing. It's registered as a live MCP tool
(`server.py:1453`, calling `browser_manager.close_instance` at line 1469), and this
branch's own singleton architecture (`singleton.py`) means one backend process now
serves every concurrent Claude Code session. **Yes, teardown can hang far longer
than the advertised bound, and because the freeze is at the event-loop level, it
takes the entire shared backend down with it — not just the instance being closed.**
Filed as **F-180, Critical**.

## 2. Zombie processes: can cleanup kill the user's own Chrome, or fail to kill zombies?

Traced the full match chain: `kill_browser_process` → `_kill_processes_for_metadata`
→ `_get_browser_pids_for_profile`. The kill set is built by
`psutil.process_iter()` filtered first by process name
(`_is_browser_process_name`: substring match on chrome/chromium/msedge/edge/brave —
broad on its own) **and then** by an exact match against the specific instance's
`--user-data-dir` command-line argument (process_cleanup.py:328-336). A user's
regular daily-driver Chrome runs out of its own default profile directory, not the
temp/managed directory this MCP assigns per spawned instance, so it will not match.
Startup-time orphan recovery (`_recover_orphaned_processes` →
`_kill_processes_for_metadata(..., recovery=True)`) adds a second safety net: it
only kills PIDs whose `create_time()` predates this server session
(`< self._init_time`), explicitly to avoid killing anything spawned during the
current run (process_cleanup.py:371-410). Profile-directory deletion has its own
guard: `_cleanup_profile_for_metadata` refuses to delete anything flagged
`uses_custom_data_dir` unless it's also `auto_clone` (process_cleanup.py:504-505),
protecting user-supplied profile paths. The temp-profile sweep
(`_sweep_orphaned_temp_profiles`) only ever considers paths under the OS temp
directory matching a `uc_*` prefix, and skips anything still in the
currently-active-profile set.

**No, this does not appear able to kill the user's unrelated Chrome** under normal
operation — the design is conservative (profile-path scoped, session-time gated,
custom-dir protected) and does not rely on process name alone. This is a negative
result, not filed as a finding (nothing to fix), but is worth confirming since the
prior claim explicitly asked. The mechanism to detect zombies (multi-PID-per-profile
via `--user-data-dir` matching) is also reasonable and unlikely to under-match. The
cost of this correctness, per §1, is that it's all synchronous — the same kill path
that's safe about *what* it kills is unbounded about *how long* killing it takes.

## 3. Error-handling void: quantified

176 `except Exception` handlers across 20 of 23 `.py` files in `embedded/`
(`server.py` 22, `browser_manager.py` 21, `cdp_function_executor.py` 15,
`dom_handler.py` 15, `dynamic_hook_system.py` 15, `network_interceptor.py` 14,
`process_cleanup.py` 14, remaining 13 files 1-11 each). **Zero** bare `except:`
anywhere. Of the 176, 143 call `debug_logger.log_error`/`log_warning` or re-raise;
22 are truly silent — no logging call, no re-raise, and the bound exception variable
is never referenced anywhere in the handler body (e.g. `browser_manager.py:1165`,
`:1207` — `switch_to_tab`/`close_tab` both collapse any failure to a bare `return
False`; `network_interceptor.py:171`/`357` discard body-read errors with only a
comment; `singleton.py:293`/`358`/`361`, discussed in §5). This is one pattern
(inconsistent logging discipline on an otherwise-reasonable defensive-exception
style), filed as **F-181, Low** on its own.

The important compounding fact, found while chasing this thread into
`debug_logger.py`, is that the "143 logged" figure is largely theoretical — see §5.
`switch_to_tab`/`close_tab` sit directly in this deep-dive's tab-lifecycle scope and
were promoted into F-181's primary quote for that reason.

## 4. Timeouts: CDP ops and hardcoded sleeps

The prior claim that "CDP ops can hang forever" is **already substantially
mitigated** at the tool layer: `server.py` defines `_with_cdp_timeout` (line 139),
a thin `asyncio.wait_for` wrapper with a clear error message
("browser may have crashed... try closing the instance"), and it's used **59
times** across `server.py`'s tool functions. Inside `browser_manager.py`,
`asyncio.wait_for` appears 8 times (the CDP close/disconnect calls in
`close_instance`, both bounded at 2.0s). `proxy_forwarder.py` uses it 6 times.
This is good, deliberate practice, not absent.

What is *not* covered by that discipline is the synchronous side documented in §1:
`browser.stop()` and the `process_cleanup` kill/cleanup chain inside
`close_instance` get no timeout at all (not even an ineffective one), because
`asyncio.wait_for` cannot bound synchronous code regardless of where it's placed.
Hardcoded `time.sleep()` calls found: `process_cleanup.py:463,483` (0.15s profile-dir
retry, synchronous, reachable from the async close path — contributes to F-180);
`singleton.py:215,254` (inside `_wait_for_server`/`_exclusive_lock`, but these run
only on the dedicated `threading.Thread` that `_start_backend_holding_lock` spawns
specifically so they *don't* touch the event loop — confirmed fine, not a bug);
`server.py:809,875,900` (profile file-copy retry backoff in `_copy_profile_file`/
`_rmtree_robust`/`_copy_profile_tree` — plain `def`, not `async def`; whether these
block the event loop depends on whether their callers during `spawn_browser` invoke
them inline or via a thread, which is spawn-path territory outside this dive's
assigned scope and is flagged here only as a pointer for whichever dive covers
`spawn_browser`).

## 5. Observability: debug_logger and response_handler

**debug_logger is opt-in and in-memory.** `DebugLogger.__init__` sets
`self._enabled = False`; `log_error`/`log_warning`/`log_info` each open with
`if not self._enabled: return` — before anything is appended to the in-memory
buffers, not merely before the stderr echo. It only becomes `True` via
`DEBUG_LOGGING_ENABLED` (server.py:217-220), itself gated behind the
`STEALTH_BROWSER_DEBUG`/`DEBUG` env vars or an explicit `--debug` flag — off by
default. The one bypass mechanism (`_emit_stderr(..., force=True)`) is used exactly
once in the whole package, by the logger announcing its own activation. **In a
default/stock run, none of the 176 handlers from §3 — logged or not — produce any
output whatsoever**, in memory or on stderr. Even when enabled, there is no log
*file*: storage is a capped in-process ring buffer (500 errors / 1000 warnings /
2000 info), persisted only if something explicitly calls `export_to_file`. Filed as
**F-182, High** — this is the finding that most directly determines whether any of
the other findings are diagnosable after the fact.

**singleton.py's cold-start path has no logging integration at all** — the module
never imports `debug_logger`; its only diagnostics are two raw
`print(..., file=sys.stderr)` calls, both specific to a backend that *was* healthy
and later died (`monitor_backend`/`run_backend_guarded`). The cold-start failure
path — `_start_backend_holding_lock` running in a daemon thread, wrapping the
entire backend-spawn sequence in a bare `except Exception: pass` — has no equivalent.
Traced the consequence forward: if the daemon thread dies silently,
`_await_backend_http` polls for the full `BACKEND_READY_TIMEOUT` (120.0s), each
individual probe failure is *also* silently caught, and when the deadline expires
`run_backend()` just returns (no exception) — so `run_backend_guarded`'s only
stderr-printing branch (its `except Exception`) never fires, and the task group
tears down via its unconditional `finally: tg.cancel_scope.cancel()` with nothing
printed anywhere. Net effect: a 120-second silent stall, then a silent disconnect,
for exactly the failure mode (fresh backend process fails to start after a
`server.py` edit) this branch's own feature is built around. Filed separately from
the general broad-except pattern as **F-183, High**, since it's a distinct root
cause (a whole subsystem with no logging integration) rather than one more instance
of §3's pattern.

**response_handler.py's disk fallback has no quota, cleanup, or monitoring
anywhere.** `ResponseHandler.handle_response` spills any response over
`max_tokens` (20000 default) to a uniquely-named JSON file under
`~/.stealth-mcp/element_clones/` — 8 call sites in `server.py` (screenshots,
network-request dumps, element-asset/related-file extraction, `expand_children`,
etc.), so this is routine traffic, not an edge case. Grepped the whole package:
`element_clones` appears exactly once, the path definition itself. Confirmed this
is genuinely ungoverned by checking both cleanup mechanisms that *do* exist in the
codebase: the background storage sweep (`_run_storage_sweep` /
`_enforce_clone_storage_cap_in`) targets a structurally different directory
(`<session-root>/sessions`) and only recognizes entries carrying a
`.stealth_chrome_devtools_mcp_clone.json` marker, so it would not pick up flat
response-spillover files even if the paths coincided; the manual
`cleanup_clone_files` tool delegates entirely to
`file_based_element_cloner.cleanup_old_files`, a third, unrelated directory again.
Under the pre-singleton model this self-limited at process lifetime; under the new
persistent singleton backend it accumulates for the life of the machine. Filed as
**F-184, Medium**.

## Summary for main

- Can teardown hang forever / far longer than advertised: **yes** — the 5s
  `asyncio.wait_for` on `close_instance` does not bound the synchronous
  `process_cleanup` kill/cleanup calls it wraps, and since `close_instance` is a
  live tool on the shared singleton backend, one bad close can freeze the whole
  server for all sessions (F-180, Critical).
- Can cleanup kill the user's own unrelated Chrome: **no** — kill/delete logic is
  scoped by exact `--user-data-dir` match plus session-create-time and
  custom-dir guards; not filed as a finding (negative result).
- 5 findings filed (F-180..F-184):
  - F-180 Critical — close_instance's timeout is illusory (synchronous blocking
    defeats asyncio.wait_for), freezing the shared backend.
  - F-181 Low — 176 broad-except handlers, 22 truly silent (representative
    quote: browser_manager.py switch_to_tab).
  - F-182 High — debug_logger is disabled by default and in-memory-only; default
    config produces zero diagnostic output for any of the above.
  - F-183 High — singleton.py's backend cold-start failure path has no logging
    integration; silent 120s stall on exactly the "edit server.py, expect a
    fresh backend" workflow this branch implements.
  - F-184 Medium — response_handler's disk-fallback directory has no quota/
    cleanup/monitoring anywhere, worse now that the backend is a persistent
    singleton rather than per-session.
