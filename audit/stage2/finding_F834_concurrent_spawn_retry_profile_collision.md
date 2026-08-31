# F-834 — Concurrent spawn_browser retries collide on the shared `-{pid}-retry` clone dir; a "ready" instance self-destructs seconds later

**Severity: HIGH** (agent-fleet workloads — the primary local usage pattern)
**Found:** 2026-08-30, live stress test of v2.0.7 (3 Opus agents spawning concurrently + 8-proxy herd)
**Status:** FIXED — see "Fix shipped" below (branch `fix/F834-per-attempt-clone-dirs`).
The F-835/F-836/F-837 sections embedded further down remain OPEN and are not
covered by that fix.

## Symptom (as a client sees it)

Under concurrent `spawn_browser` load against one backend:

1. Most spawns fail after ~12s with nodriver's misleading
   `Failed to connect to browser … you need to pass no_sandbox=True`
   (sandbox was already off; the advice text is not the cause). Observed 9/10
   failure rate with 3 concurrent spawning clients.
2. Worse: a spawn occasionally RETURNS SUCCESS (`state: "ready"`, instance_id
   issued) and the browser is dead by the very next call —
   `Instance not found`, `list_instances` = []. The client did nothing wrong.

## Hard evidence (backend-163320.log, 2026-08-30, v2.0.7)

Correlation `b42ef5202147` — one spawn call, first attempt failed, retry succeeded:

```
15:55:24,151 tool spawn_browser start
15:55:25,199 browser_manager.spawn_browser: Platform: …    <- attempt 1
15:55:37,381 browser_manager.spawn_browser: Platform: …    <- attempt 2 (retry)
15:55:38,160 process_cleanup.track_process: Tracking browser process 129608
             for instance 068ea987… profile …f876e3d7f2ec-163320-retry
15:55:38,408 tool spawn_browser end (14256.4ms)            <- SUCCESS returned
15:55:40,911 process_cleanup.cleanup_profile: Removed temp profile for
             068ea987…: c:\stealth-mcp-browser-sessions\sessions\
             stealth-chrome-devtools-mcp-f876e3d7f2ec-163320-retry   <- SAME correlation id, AFTER success
15:55:40,919 process_cleanup.untrack_process: Stopped tracking 129608
15:55:40,919 process_cleanup.cleanup_deferred_profiles: Finalized 1 deferred entry
15:55:45,592 browser_manager.discard_instance: Removed stale browser instance
             068ea987…: browser process is not running     <- next call finds a corpse
```

Same fate for instance `4abf84f9` (spawned 15:55:48.293, dead by 15:55:53.126).
Chromium-family process count stayed flat during the failure run (86→84), so
failed spawns are not leaking browsers — the kills are the product's own cleanup.

## Mechanism

`clone_storage._unique_clone_dir` (clone_storage.py:858-865) names the retry
clone `{base}-{os.getpid()}-{suffix}` — but `os.getpid()` is the **backend's**
pid, identical for every concurrent spawn in the process. The only uniqueness
guard is `_profile_has_running_browser(candidate)`, which is False for ALL
concurrent spawns during their pre-launch window (classic TOCTOU). Result: N
concurrent retrying spawns copy into and launch Chrome from the SAME directory
(`…-163320-retry` above); the copies/Chrome singleton locks fight, most attempts
die with "Failed to connect", and a failed attempt's (deferred) profile cleanup
deletes the directory out from under the one attempt that won — killing an
instance the tool already reported as ready.

The post-success `cleanup_profile` firing under the succeeding call's own
correlation id (15:55:40,911) needs pinning down during the fix: it is either a
late failure-path cleanup task from attempt 1 of the same call, or the
`cleanup_deferred_profiles()` sweep (browser_manager.py:187/308/360/730)
finalizing another spawn's deferred delete of the shared path. Either way the
shared deterministic path is the root enabler.

## Fix direction

1. Make the retry/fallback clone dir unique per spawn ATTEMPT, not per process:
   add a monotonic counter or uuid4 slice to the suffix in `_unique_clone_dir`
   (and audit `_available_clone_dir`'s `-{pid}` / `-{pid}-{index}` ladder for the
   same TOCTOU under in-process concurrency — `_profile_has_running_browser` is
   not a reservation).
2. Ownership guard in cleanup: never delete a profile dir that a currently
   tracked live instance owns (`process_cleanup` knows the mapping — it logged
   both `track_process` and the fatal `cleanup_profile` for the same instance).
   Deferred finalization must re-check ownership at fire time, not at defer time.
3. RED test: two concurrent spawns in one backend where attempt 1 of each fails;
   assert distinct user-data dirs and that the surviving instance's profile
   still exists after `cleanup_deferred_profiles()` runs.

## Mechanism refinement (load-2's spawn_diagnostics data)

Successful uncontended spawns return `profile_role: "master"` on
`C:\stealth-mcp-browser-sessions\master` with `snapshot_reason:
"before-master-open"`; a spawn that succeeded UNDER contention returned
`profile_role: "clone"`, `clone_source: "master-snapshot-retry"` with a
`spawn_retries` array holding a swallowed failure. So the full race is
two-stage: (1) N concurrent spawns all try to open the MASTER profile first —
Chrome's own profile singleton lets exactly one win and the rest fail
nodriver-side with "Failed to connect to browser"; (2) the losers fall back to
the retry clone, whose path is shared per-process (`-{pid}-retry`), and collide
again there. Both stages funnel concurrent callers into one directory.
Phase-2 data (no contention): spawns 2-for-2, 1.4-2.5s, cold start of backend +
first browser measured at 9.26s total. Memory pressure is NOT a trigger — an
instance loss reproduced at 75.7 GB free.

load-2 latency/error tallies (133 calls): spawn_browser 5/20 ok under the full
run; every other tool 97%+ ok; transport survived the whole run — all failures
were tool-level, above a healthy transport. The misleading
"running as root / pass no_sandbox=True" advice was independently flagged by
both load agents as costing diagnosis time.

## F-837 (separate defect, MED): get_page_content fallback gap

The backend's large-response file fallback engaged at 138.91 KB and 282.83 KB
(tidy fallback dict returned), but a 59,734-character response was returned
INLINE and overflowed the MCP client's token ceiling ("result exceeds maximum
allowed tokens"). The fallback threshold sits ABOVE the client's practical
limit, leaving a band that is too big to deliver and too small to divert.
Fix: drop the fallback threshold below the client ceiling (response_handler.py
owns large-response handling — check overlap with queued F-822 before filing a
separate PR).

## Additional live data (same test, load-1's run)

- headless=true failed 8/8 for one client while headless=false succeeded FIRST
  try — consistent with the collision theory because headed launches take the
  F-810 desktop-delegation path (different launch pipeline) while all the
  concurrent headless spawns contend in-process.
- **Discriminating experiment RESULT (16:12:23, backend-163320.log corr
  ddcab088df98):** a single spawn with zero concurrent spawners succeeded in
  1482 ms, instance e68f3eb7 stayed alive ~57s and closed cleanly. Combined
  with 4 other clean solo spawn/close cycles at 16:07-16:12 (1.4-2.5s each):
  there is NO separate headless-specific defect — the concurrency collision
  fully explains the failure pattern. (Caveat: headless flag inferred from the
  agent's instruction, not visible in the backend log — spawn logs do not
  record the headless arg, itself a minor log-completeness gap.)
- **F-835 (observability, MED):** after 24+ consecutive failed spawns,
  `get_debug_view` reports `total_errors: 0` — failed spawns log only INFO
  ("Platform: …") lines; the ToolError raise never lands in the debug ring as an
  error. A total spawn outage is invisible to the operator's debug view.
- **F-836 (contradiction, LOW):** `validate_browser_environment_tool` returns
  `recommended_args: ["--no-sandbox","--disable-setuid-sandbox"]` while
  `browser_manager.stealth_filter` deliberately strips exactly those args
  ("Stripped 4 detectable arg(s)"). One tool recommends what another discards —
  the recommendation text is stale vs. the stealth filter policy.

## Fix shipped

Branch `fix/F834-per-attempt-clone-dirs`. Three layers; RED tests first, in
`tests/test_concurrent_spawn_collision.py` (hermetic — no Chrome, no real
`~/.stealth-mcp`; `os.getpid()` is the one live pid any test uses).

### Which deletion path actually fired (the open question at line 55, answered)

**`ProcessCleanup.cleanup_deferred_profiles()`** — the sweep, NOT a late
failure-path cleanup from attempt 1 of the same call.

Two independent reasons, both from reading the code against the log:

1. **The instance_id rules attempt 1 out.** Each retry attempt is a *separate*
   `browser_manager.spawn_browser(options)` call and mints its own
   `instance_id = str(uuid.uuid4())` (first statement of
   `browser_manager.spawn_browser`). Attempt 1's
   failure path (`except Exception` → `kill_browser_process`) can therefore only
   ever touch attempt 1's own id and its own (base-clone) directory. The fatal
   log line names `068ea987…` — the id tracked at 15:55:38,160 for the
   **`-retry`** dir, i.e. attempt 2's, the one that succeeded.
2. **The log's three lines are `cleanup_deferred_profiles`'s exact emission
   order.** `cleanup_profile: Removed temp profile for 068ea987` (40,911) →
   `untrack_process: Stopped tracking 129608` (40,919) →
   `cleanup_deferred_profiles: Finalized 1` (40,919) is precisely
   `_cleanup_profile_for_metadata` → `untrack_browser_process` → the loop's
   closing summary. No other call site emits that triple.

The sweep runs from `browser_manager.py:187` (`_discard_instance_unlocked`),
`:308` (`_blocking_teardown`), `:360` (idle reaper) and `:730` (spawn cancel).
At 15:55:40.9 a *sibling* spawn was tearing down, so :187/:308 are the live
candidates; the sweep is global — it walks **every** tracked entry, not the
caller's — which is how a sibling's teardown reached the winner's record.

It fired because the winner's tracked launcher pid (129608) was already gone
(`psutil.pid_exists` false) while its browser was not: a second Chrome launched
against a user-data-dir another Chrome already holds hands its command line to
the incumbent via the profile singleton and **exits**. The shared `-retry` dir
manufactured exactly that. The sweep then took the `active_profile_dirs`
snapshot once at sweep start and passed it down, so its "is anyone using this
directory" answer was a *defer-time* answer.

### Layer 1 — per-attempt uniqueness (`clone_storage.py`)

`_unique_clone_dir` stamps `_attempt_token()` (`{pid}-{itertools.count()}`) into
the name, so it is unique by construction and the timestamp fallback rung is
gone. `_available_clone_dir`'s `-{pid}` / `-{pid}-{index}` ladder had the same
TOCTOU (every concurrent loser got the same `-{pid}`) and is replaced by one
token. All three selectors — plus `_next_available_explicit_dir` — now ask
`_dir_unavailable`, which consults the **existing** in-flight reservation set
(`_protect_clone_dir`) as well as liveness; `_profile_has_running_browser` is a
liveness check, not a reservation, and that is stated in the code. There is no
`await` between selection and `_protect_clone_dir`, so select-and-reserve is
atomic on the event loop. The now-dead post-selection `if
_profile_has_running_browser(clone)` re-check in `resolve_profile_selection` is
removed.

### Layer 2 — cleanup ownership guard (`process_cleanup.py`)

New `_profile_claimed_by_live_instance(normalized, skip_id)`: true when a
tracked instance *other than* the one being cleaned holds that directory with a
live pid. It is consulted in `_cleanup_profile_dir` — the single home every
caller (kill / finalize / deferred / recovery / startup sweep) already routes
through — and the skip is logged at INFO. `cleanup_deferred_profiles` now
passes **no** pre-computed active-profile set, so ownership is re-measured per
entry at FIRE time; the sweep-start snapshot was the defer-time answer.

### Layer 3 — honest error text (`spawn_contention.py`, new leaf)

`contention_hint(in_flight)` renders a paragraph naming the count, the cause,
and — explicitly — that nodriver's root/`no_sandbox` advice does **not** apply
here. It is appended at the one composition site in `browser_manager`, right
beside F-811's `exhaustion_hint`, each carrying its own `"\n\n"` so the site
stays a bare concatenation. `BrowserManager` tracks `_spawns_in_flight` and the
per-burst `_spawn_peak_in_flight`; the hint reads the **peak**, because one
race's losers fail in sequence and by the last of them the live count is 1
again — the last loser is exactly the caller most likely to be reading.

### Not fixed here (deliberate)

Stage 1 of the two-stage race — N concurrent spawns all finding the **master**
profile free and all opening it — is unchanged. Reserving master would need a
matching release on the close path in `server.py` (at its LOC cap), and a leaked
master reservation would silently force every later spawn to clone. Layer 1
makes the *losers* of that race land in distinct directories, which is what
turns the incident from a mutual kill into an ordinary retry.

## Related

- The nodriver "run as root / no_sandbox" error text is a red herring for this
  failure mode; worth wrapping with an honest hint (ties into the F-811
  exhaustion-hint pattern — "concurrent spawn contention" is a distinct cause).
- Found while validating F-820: the busy-vs-dead watchdog itself behaved
  correctly during the same window (held a busy backend at 15:44, condemned the
  genuinely OOM-killed one at 15:46).
