# F-967 — concurrent unnamed `spawn_browser()` copies the profile ON the event loop; the frozen backend is then killed by a proxy while it owns every chat's browser

**Severity:** High (every chat's browser and the shared `default`/`fleet` sessions lost at once). Seen
2026-10-14, backend 38760 (2.1.32, port 58864), after a reboot at 03:27 and about ten chats
starting browsers at once.
**Files:** `embedded/clone_storage.py` (`resolve_profile_selection`, `_fallback_profile_selection`,
`_resolve_explicit`, `_resolve_shared`, `_choose_clone_dir`, `_prepare_clone`,
`_next_available_explicit_dir`), `embedded/profile_offload.py` (new), `embedded/clone_protection.py`
(new, moved out of `clone_storage`), `embedded/client_roots.py` (new, moved out of `clone_storage`),
`embedded/backend_eviction.py` (`protected(silence=)`, `STARVED_CEILING_SECONDS`, logging in
`terminate` / `clear_stale`), `embedded/backend_liveness.py` (`silence`), `embedded/singleton.py`
(`_protecting_browsers(starved=)`, `_clear_stale_backend`), `NAVMAP.md`, `CHANGELOG.md`.
Tests: `tests/test_clone_spawn_offload.py` (new, 12), `tests/test_backend_eviction.py` (+9).
**Branch:** `fix/f967-clone-copy-blocks-event-loop`, from `origin/main` (952ddb4, release 2.1.32).

---

## 1. What happened

Evidence is the backend and proxy logs of the incident, preserved in
`%TEMP%\f967-evidence\` (`backend-38760.log`, `backend-110556.log`, `proxies-0343-0349.log`) plus
`~/.stealth-mcp/logs/backend-72908.log`.

| Time (local) | Event |
|---|---|
| 03:45:39 | Backend 38760: the first `spawn_browser` of the burst starts. |
| 03:46:06 | A proxy logs "loop turning 27.1s ago [3/10]": the heartbeat the proxies read is ageing. |
| 03:47:15.269 | Proxy 74100: "confirmed gone after a lost connection". |
| 03:47:15-23 | 36 more proxy lines, "confirmed unusable". |
| 03:47:25.212 | Backend 38760's LAST line (`browser_manager.spawn_browser`, the first copy finishing). 106 s after the first. |
| 03:48:18.833 | Proxy 89700: "backend spawned via the breakaway rung (pid 72908)". 38760 is gone; no shutdown line. |
| 03:49:12.853 | Backend 72908's last log line. |
| 03:49:21.319 | Proxy 92400: breakaway spawn, pid 110556. 72908 is gone, no shutdown line. |
| after | 110556 binds the port, re-attaches master and fleet, reaps every chat's clone. |

`backend-38760-fault.log` exists and is 0 bytes, so this was not a crash or a fault dump: the process
was terminated from outside.

## 2. Cause 1 — the loop was held by synchronous profile work

`resolve_profile_selection` is `async` and every blocking call in its clone branch ran on the loop:
`_refresh_snapshot_if_stale`, `_copy_clone_from_source` -> `_copy_profile_tree` ->
`profile_copy.replace_tree` (~100 MB, 666 files, two passes and a 0.2 s settle), the explicit-session
copy, `_refresh_master_snapshot_if_safe("before-default-open")`, and a process-table walk
(`_profile_hold(master)` on EVERY unnamed spawn, `_dir_unavailable`, `_seed_source`,
`fleet_session.clone_seed`, the fallback's `_profile_hold`), which with 80-200 `chrome.exe` is one
`cmdline()` read per process. N concurrent spawns serialize on the loop, so the backend served
nothing for the sum of the copies: ~106 s.

The loop heartbeat (F-889, `backend_liveness.self_report`) counts only stamps at most 30 s old, so
a loop frozen longer than that reports nothing, the watchdog's vetoes run out, and the proxies
condemn a backend that is busy rather than dead.

## 3. Cause 2 — the killer

**What killed 38760 (and then 72908) was a proxy's heal.** Each proxy that condemned the backend
ran `proxy_selfheal.heal_backend` -> `singleton.ensure_server_running` -> the thread
`_start_backend_holding_lock`. That waits `REUSE_PATIENCE_SECONDS` (60 s) for the port's backend to
become reusable (`_same_identity_backend_ready`; every probe timed out, because the loop was still
frozen by the next copy), then calls `_clear_stale_backend` -> `backend_eviction.clear_stale`.
`backend_eviction.protected` is identity-gated: a backend of the SAME identity is "adoptable or
wedged", so it is evictable whatever it owns, and `backend_eviction.terminate` ended it with
`psutil.Process.terminate()` (TerminateProcess on Windows: no shutdown line, no atexit) while it
owned the shared browser, the fleet browser and about a dozen clones.

Timing agrees with that and with nothing else. First condemnation 03:47:15.27 plus the 60 s of
patience is 03:48:15, and proxy 89700 logs the spawn of 72908 at 03:48:18.8. 72908 (last log
03:49:12) is killed by the same path ~03:49:21: proxy 92400's second heal attempt at ~03:48:28 plus
60 s, and it spawns 110556 at 03:49:21.3. The only terminators in the tree are
`backend_eviction.terminate` (used by `_clear_stale_backend`, `stop_backend`, `restart_backend`),
`desktop_launch.py` and `backend_launch.py:321`; the last two launch, they do not kill a backend on
a port.

**Evidence gap, stated plainly.** `terminate` logged nothing, and the fault log is empty, so the
attribution is circumstantial (timing plus the only kill path that can reach a same-identity
backend owning browsers). The fix therefore also makes every kill log its pid, port and reason.

## 4. Fix

### 4.1 Off the loop (`embedded/profile_offload.py`)

`profile_offload.run(fn, *args, discard=)` is the one way to hand blocking profile work to a worker
thread:

* **Bounded**: `MAX_CONCURRENT_COPIES = 3`, one `asyncio.Semaphore` per loop, so ten spawns are not
  ten simultaneous 100 MB copies and do not starve `close_instance`'s teardown of the default
  executor.
* **Cancellation outwaits the thread.** A cancelled `await to_thread(copy)` returns at once while the
  copy runs on and publishes a complete, marked clone that nothing owns and that
  `_protect_clone_dir` shields from the sweep for the life of the process. `run` shields the
  worker, waits it out (re-cancel-proof), then runs `discard` and re-raises. `replace_tree`
  publishes in one rename, so `discard` never meets a half-copy.
* **The context travels** (`asyncio.to_thread` copies it), so log lines stay joined to their tool call.
  `directory_gate`'s reentrant holds are per-context, so it is NEVER handed to a worker: it stays
  held by `spawn_browser` on the loop around the whole call.

`clone_storage.resolve_profile_selection` is now an orchestrator over four synchronous units:
`_resolve_explicit` (the named branch: hold walk, walk to `<name>-N`, seed read, copy),
`_resolve_shared` (the shared half: hold read and the `before-default-open` refresh),
`_choose_clone_dir` + `clone_protection.reserve` (choose and reserve under one lock), and
`_prepare_clone` (freshen, seed choice, copy). Order is preserved: the clone directory is protected
BEFORE it is copied and before `spawn_background_sweep("pre-clone")` (F-834's invariant, which relied
on no `await` between choice and reservation; the choice is on a thread now, so a `threading.Lock`
says it), the AUTHORITATIVE hold read stays adjacent to the copy, F-920/F-910 seed-refresh atomicity
is untouched (`replace_tree` is unchanged), and F-834's retry semantics are untouched
(`_fallback_profile_selection` offloads its hold read and `fleet_session.clone_seed`). Cleanup
ownership: `made` / `held` lists record what each unit created and reserved, and
`clone_protection.discard` removes the former and releases the latter on cancel or failure.

`_next_available_explicit_dir` tests `fresh and candidate.exists()` before the walk
`_dir_unavailable` (a pure reorder: the walk only matters for a candidate that exists).

`clone_protection.py` and `client_roots.py` are verbatim moves out of `clone_storage.py`, which
stood at exactly 1000 of its 1000 lines (a cap that only ratchets down); it is now 996.

### 4.2 A starved backend is not a wedged one (`backend_eviction`, `backend_liveness`, `singleton`)

`backend_liveness.silence(path, port)` returns the unbounded age of the backend's loop stamp (None
when there is no entry, no sidecar, a pid mismatch, or a stamp from the future); `self_report` is the
same value bounded by `HEARTBEAT_STALE_SECONDS`. `backend_eviction.protected(..., silence=)` now
spares an identity-matching backend that is OURS, alive and owns a live browser when `silence()` is
at most `STARVED_CEILING_SECONDS` (300 s). It is asked at the KILL site only
(`_clear_stale_backend`, `starved=True`):

* the bind site (`_select_backend_port`) is unchanged: stepping aside from a starved backend there
  would spawn a second backend beside it;
* `stop` / `restart` stay unconditional: they are the operator's verbs;
* past 300 s, with no stamp, or with no live browser to protect, the old rule applies, so a truly
  dead loop is still evicted within minutes.

`terminate` and `clear_stale` log the pid / port / reason.

## 5. Audit: every blocking call on a spawn path

| Path | Blocking work | State |
|---|---|---|
| `resolve_profile_selection` clone branch | seed freshen, `_clone_seed` hold walk, tree copy, `_profile_hold(master)` | MOVED |
| `resolve_profile_selection` explicit branch | hold walk, walk to `<name>-N`, `_seed_source`, copy | MOVED |
| `resolve_profile_selection` `default` open | hold read, `before-default-open` refresh | MOVED |
| `_fallback_profile_selection` | `_profile_hold`, `fleet_session.clone_seed` | MOVED |
| `_next_available_explicit_dir` | walk before `exists()` | reordered (~20 walks removed) |
| `close_instance` seed refresh | `_refresh_master_snapshot_if_safe("after-default-close")` | already `asyncio.to_thread` (`browser_management.py`) |
| `_blocking_teardown`, `wait_for_exit_async`, `browser_reattach.held_by`, claim registry, `spawn_background_sweep`, `cookie_handoff._port_ws_url` | | already off the loop |
| `login_persistence.remove_saved_tabs` (named sessions: hold walk + rmtree, `_apply_post_launch`) | | NOT moved: follow-up |
| `browser_manager.list_instances` / `get_instance` -> `_discard_instance_unlocked` | walks, rmtree, sleeps under the manager lock | NOT moved: follow-up |
| failed-spawn teardown (`_teardown_failed_spawn`, `reap_launched_browsers`, `kill_browser_process` terminate/wait to ~5 s, `exhaustion_hint` `process_iter`) | | NOT moved: follow-up |
| `track_browser_process` registry file lock and write (tens of ms, can spin to 5 s) | | NOT moved: `process_cleanup.browser_processes` has no lock, a thread hazard |
| `check_browser_executable` cold probe, cookie translation CPU in `cookie_handoff` (tens of ms per live-seeded spawn), `require_allowed_seed_from(check_source=True)` walk when `seed_from` is given | | NOT moved: follow-up |
| process-table walk coalescing across concurrent spawns | | recommended follow-up (N walks for N spawns remain, now off the loop and bounded) |

Must never go to a thread: `directory_gate` (asyncio lock + ContextVar), browser_manager's instance
tables and nodriver objects, `_client_session_seed` (the fastmcp context), `spawn_background_sweep`
(`create_task`).

Known residual (pre-existing, accepted): a seed refresh running concurrently with a clone read can
meet the microsecond window between `replace_tree`'s two renames. Bounded by
`MAX_CONCURRENT_COPIES`; a single-flight refresh lock is the optional follow-up.

## 6. Tests

`tests/test_clone_spawn_offload.py` (new, 12). A ticker task samples the loop; each test bounds the
largest gap by `LOOP_STALL_BUDGET` (0.2 s) and every wait by `asyncio.wait_for(WAIT_BUDGET)`:
concurrent clone spawns keep the loop turning; the reservation exists before the copy starts; the
correlation id reaches the worker; a slow process-table walk, a named-session copy, the `default`
open and a retry selection each keep the loop turning; copies run in parallel but never more than
`MAX_CONCURRENT_COPIES`; a spawn cancelled mid-copy leaves no clone, no reservation and no staging
(and waits the copy out); a cancelled named spawn removes the session it created; a refusal raised
in the worker surfaces unchanged and releases the reservation; a spawn cancelled while queued for a
slot releases its reservation.

`tests/test_backend_eviction.py` (+9): `TestAStarvedBackendThatOwnsBrowsersIsNotEvicted` (a silent
but recent backend with browsers is spared and the cold-start lock spawns nothing beside it; one
silent past the ceiling is evicted), `TestProtectedWithAWitness` (inside / on the boundary of the
ceiling, no evidence is no protection, a stranger stays protected without any witness),
`TestTheKillNamesItself` (`terminate` and an eviction log pid, port and reason).

**RED on the unfixed tree** (method: `git archive origin/main` exported out of tree, the two new test
files copied in, run with `PYTHONPATH=<export>/src`, `__file__` of `clone_storage` and
`backend_eviction` confirmed to resolve into the export, bytecode writing off): 18 failed, 40 passed.
Loop gaps against the 0.2 s budget: four concurrent clone copies 2.97 s, a process-table walk
2.32 s, a named copy 1.82 s, a retry selection 1.75 s, the `default` open 0.95 s (the tests use an
injected slow copy; the incident's was ~100 MB of real files). The cancellation pins fail with "DID NOT RAISE
CancelledError" (the synchronous copy cannot be cancelled), the bound pins fail on the missing
module, and the eviction pins fail because the cold-start lock KILLS the starved backend and starts
another (`[('kill', 40123), ('start', 40123)]`). **GREEN** on the fix: 58 passed
(`test_clone_spawn_offload.py` + `test_backend_eviction.py`).

No existing pin or golden was modified.
