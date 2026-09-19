# F-889 — a starved proxy condemns a healthy shared backend, and then exits

**Severity**: CRITICAL — the whole fleet loses its MCP server at once, the backend
that was serving it is killed and replaced, and every session must be reconnected by
hand. 829 condemnations in seven days.

**Status**: FIXED (this PR), in four parts: (a) strikes are spent in fairly scheduled
seconds, (b) the backend is a second witness to its own liveness, (c) the proxy
retries instead of exiting, (d) a newer backend is adopted rather than evicted.

---

## 1. The mechanism

Measured 2026-09-18 13:30–14:00 UTC on release 2.1.8.

The machine had **2.4 GB free of 125.7 GB** and 114 stdio proxies whose working sets
had been paged out to ~0 MB. The backend — pid 173824 on port 52554 — **was healthy
throughout**: an MCP `initialize` against it answered in **227 ms**, and its log
carries no error for the whole window.

The proxies' logs carry the mechanism verbatim:

```
13:32  WARNING stealth.proxy: probe failed 1/3 on port 52554
13:32  WARNING stealth.proxy: probe failed 2/3 on port 52554
       ... condemnation, heals that could not complete, and the proxy EXITING
```

Claude Code renders that last step as **"Connection closed"**, on every session at
once.

Three separate decisions compose into the outage, and each one is individually
defensible:

1. **`backend_watchdog.watch_liveness` counts a strike per failed 2 s probe**, three
   in a row, then opens a confirmation phase. The strike phase is paced by
   `await anyio.sleep(2.0)` — wall clock. A process that is not being scheduled sleeps
   2 s of wall time in which it was awake for almost none of it, then issues a probe
   whose 2 s budget is spent the same way. **The timeout is evidence about the prober,
   not about the backend**, and the watchdog cannot tell the two apart. This is
   exactly the distinction F-856 drew for the reuse gate and `scheduling_lag` exists
   to make — the strike phase was left out because, pre-F-820, strikes never condemned
   on their own.

2. **The confirmation is the same starved probe, one layer down.**
   `_same_identity_backend_ready` DOES spend its patience in fair seconds, but
   `FairWindow` is bounded at `MAX_STRETCH = 4.0`, and under this load the probe
   failed for longer than 60 fair seconds bought. The confirmation concluded "dead"
   about a backend answering in 227 ms.

3. **A condemnation is a heal, and a heal that cannot complete is an exit.**
   `proxy_selfheal.drive` allows `HEAL_ATTEMPTS = 2` at `HEAL_ATTEMPT_SECONDS = 45` —
   which under starvation is not enough wall time to bring a backend up — then calls
   `_teardown(...)` and RETURNS. `singleton._proxy_streams` reads that return as "tear
   down for reconnect" and cancels the task group, and the proxy process exits. The
   premise under that exit has been known to be false since F-838's own docstring:
   *"MCP clients do not reliably respawn a stdio server mid-session"*. F-838 removed
   the exit for a backend death it could heal; it left it for a heal that fails.

F-886 (shipped in 2.1.9) stops a COLD START from evicting a backend that owns live
browsers. It does not touch this path: the watchdog condemns, the heal calls
`ensure_server_running`, and the replacement's arrival is what takes the port.

**The single sentence.** A starved client cannot distinguish "the backend is slow"
from "I was not scheduled", and today it resolves that ambiguity by killing a shared
resource and then killing itself.

## 2. Evidence

| fact | value | source |
|---|---|---|
| free memory at the incident | 2.4 GB of 125.7 GB | machine snapshot |
| stdio proxies resident | 114, working set ≈ 0 MB | process table |
| backend `initialize` latency, during the outage | 227 ms | direct probe, pid 173824 |
| backend errors in its own log for the window | 0 | `stealth-backend.log` |
| `proxy: backend condemned` (cause=watchdog), 7 days | 829 | Sentry |
| what the user saw | `Connection closed`, every session | Claude Code |

The asymmetry is the finding: **the only process that reported a problem was the one
that had no CPU**, and the one it reported the problem about was answering in a fifth
of a second.

## 3. The rules chosen

### (a) A strike must be earned in fairly scheduled seconds

`scheduling_lag.FairWindow` is THE one home for "was this process scheduled fairly",
and it is consumed, never modified. On the first strike of a run the watchdog arms a
window whose patience is the nominal time the REMAINING ticks should take —
`interval * (failures_before_teardown - 1)` — and it may not conclude until that
window is spent. Each tick is charged at the lag the tick's own nap measured, because
`expired()` is asked every tick once a run is open.

Two properties make this safe rather than a slowdown:

- **On an idle machine nothing changes.** Charged per tick is
  `elapsed_tick / factor`, where `elapsed_tick = nap_actual + probe` and
  `factor = nap_actual / interval` (clamped at 1.0). That is
  `interval * (1 + probe / nap_actual)`, which is **≥ `interval`, always** — so
  `failures_before_teardown - 1` ticks always spend the window, and the human-pinned
  ~12 s hard-down detection window (`plan_M1` appendix, 2026-07-02) is byte-identical.
- **Under starvation it stretches and terminates.** `MAX_STRETCH = 4.0` bounds the
  window at four times its patience in wall seconds, so a starved run delays
  condemnation by at most `interval * (N-1) * 4` and never forever.

The pause IS the measurement — `FairWindow.nap` waits and observes its own lag in one
call, which is what keeps the two from drifting apart — so the default tick runs
`window.nap` on a worker thread (it sleeps; run inline it would freeze the stdio pump,
plan_M1 §2.2 rejected alternative #3). A caller that injects its own `sleep` owns the
pause and therefore its measurement: the window then charges wall seconds, which is
what the pre-F-889 watchdog did and what the `interval=0` unit tests require.

### (b) The backend is a witness to its own liveness

**Condemnation now needs two witnesses, and the second one is the backend.** The
backend stamps a wall timestamp and its pid into its own `server.json` entry every
`HEARTBEAT_INTERVAL_SECONDS` (3 s) from an **asyncio task on its event loop**; the
proxy reads it with **no HTTP, no socket and no thread** — one small local JSON read.

| probe | heartbeat | verdict |
|---|---|---|
| times out | fresh (≤ 30 s old, pid matches) | busy, **or I am starved** — never dead. Strike run resets. |
| times out | stale | the confirmation phase runs, exactly as today |
| times out | absent | the confirmation phase runs, exactly as today |

**Why the event loop, and how a wedged-but-alive backend is still caught.** The
failure the watchdog exists for (F-501) is a backend whose dispatch loop is dead while
its socket stays open. A heartbeat on an OS thread, or in a signal handler, would keep
stamping through exactly that failure and would defend the one backend that deserves
condemning. On the event loop it cannot: the same wedge that stops answering
`initialize` stops the stamp, the stamp ages past `HEARTBEAT_STALE_SECONDS`, and the
strikes plus the confirmation condemn it as they always did. **The heartbeat can only
ever say "I am scheduled and my loop is turning" — which is precisely the fact a
starved prober cannot establish and has no other way to obtain.**

The threshold is 10 missed stamps (30 s of 3 s). Sized against the incident and not
against taste: a backend answering `initialize` in 227 ms is stamping, and a backend
starved badly enough to miss ten consecutive loop iterations while its socket still
answers is a state nobody has observed. Both numbers live in `backend_liveness`, with
their relation stated, so a reader cannot change one without seeing the other.

The schema change is backward compatible in both directions. The two fields are
optional and additive on a v3 entry: a 2.1.9 proxy reading a v3 record ignores them
(`backends_in` copies entries whole), and a 2.1.9 backend writes none, so a
2.2.0 proxy reads "absent" and behaves exactly as 2.1.9 did. `SCHEMA_VERSION` does
not move, because nothing about the record's SHAPE changed — only two more optional
fields on an entry that already tolerates hand-editing and two prior schemas.

### (c) The proxy never exits because the backend is unreachable

The exit is deleted, not lengthened. After a failed heal — or after
`MAX_CONSECUTIVE_HEALS` — the proxy **backs off and keeps retrying for as long as its
stdio pipe is open**: `RETRY_BASE_SECONDS = 2.0`, doubling, capped at
`RETRY_MAX_SECONDS = 60.0`, jittered ±25% so a fleet of 114 orphaned proxies does not
re-enter `ensure_server_running` in the same second. A generation that never became
ready (`_await_backend_http` exhausted) is now also an incident to retry rather than a
reason to leave; that was the pre-F-838 behaviour F-838 deliberately preserved, and it
is the last door the exit still had.

`MAX_CONSECUTIVE_HEALS` becomes the trigger for backoff rather than for exit: three
deaths in a row still mean "stop hammering", but "stop hammering" is a 60 s wait, not
the end of the session.

**What the client experiences.** The MCP server stays connected. Tool calls issued
while no backend is up are answered with a JSON-RPC error saying the backend is
unreachable and the proxy is retrying — the client's own reconnect logic is never
engaged, no window is closed, and when a backend comes back the very next tool call
works. The proxy still exits the instant the CLIENT closes stdin, which is the only
lifetime it ever legitimately had; `_proxy_streams` cancels the task group from
`pump_client`'s EOF, so the exit path is unchanged and `drive` simply never returns
on its own.

The one report name changes with it. `TEARDOWN_EVENT`
(`"proxy: teardown after failed heal"`, ERROR) described a thing that no longer
happens; it is `UNREACHABLE_EVENT` (`"proxy: backend unreachable, retrying"`) now,
shipped once per backoff with the same `reason` (`unhealable` / `flapping`), plus the
attempt number and the delay. A search for "how often does stealth still disconnect"
is answered by its successor: how often a proxy enters retry.

### (d) A newer backend is adopted, never evicted

A mixed-version fleet had one failure mode left, and 2.1.8/2.1.9 living side by side
is how it was found (`mixed-install-fleet-evicts-in-a-loop`): two identities on one
desktop each evict the other on every proxy start. F-886 stops that when the loser
owns browsers. It does not stop it when neither does, and a fleet mid-upgrade is
exactly the population where neither does yet.

`singleton._identity_matches` gains one clause: **a recorded version strictly NEWER
than ours matches.** A newer backend is therefore reusable, so `clear_stale` returns
before the kill site, `_select_backend_port` never steps on its port, and the
watchdog's confirmation passes for it. An OLDER backend is evicted exactly as before
(if unprotected), and SAME version + different digest keeps issue #14's
editable-install behaviour untouched — that comparison is still
`backend_registry.fingerprint_mismatch`, so F-829's unreadable sentinel keeps its one
meaning.

The comparison lives in `build_identity.newer`, beside `version` — the one home for
"which build is THIS process running" — as a plain numeric-segment compare.
Deliberately not `packaging.version`: it is not a declared dependency, and our own
versions are `X.Y.Z`. Anything that does not parse as such is **not** newer, so the
answer fails closed onto today's behaviour, and `UNKNOWN_VERSION` (`"0.0.0"`) is never
newer than anything.

**The residual, named**: an older proxy in front of a newer backend forwards
`tools/list` verbatim, so the CLIENT sees the newer backend's tools and may call them.
Nothing breaks, because the proxy has no tool knowledge of its own to be wrong about —
it answers `initialize` locally with its own `serverInfo` and is a transparent pipe for
everything else. What the client sees is the newer server, which is the direction a
fleet should converge in.

## 4. Blast radius

**`backend_watchdog.watch_liveness`** gains two optional collaborators (`heartbeat`,
`fair_window`) and keeps every existing parameter and default. With `interval=0.0`
(every existing unit test) the window has patience 0.0 and is expired on its first
ask, so the strike arithmetic is unchanged; with no heartbeat supplied the second
witness is absent and the confirmation phase runs exactly as it did.

**`backend_liveness`** grows the heartbeat: two constants, the reader (`self_report`),
the writer (`stamp`) and the backend-side task (`beat` / `start_beating`). It remains
a leaf — `backend_registry` is still its only import of ours — and its existing four
functions are untouched.

**`backend_registry`** gains `stamp_heartbeat`, one more writer under the module's
existing read-merge-write protocol. It matches on PORT, writes nothing when no entry
claims that port (so a heartbeat can never resurrect an entry `forget_entries` has
just dropped), and takes its path as a parameter like every other function here.

**`proxy_selfheal.drive`** no longer returns on its own. `_teardown` is replaced by
`_unreachable`; `heal_backend`, `PendingCalls`, `_one_generation` and the cause
vocabulary are untouched.

**`singleton`** is wiring only: one heartbeat binding in `_watch_backend_liveness`,
one clause in `_identity_matches`, one `backend_env.scrub` call in
`_start_server_process` (F-890), and the two now-unreachable teardown lines in
`backend_leg` deleted. The file was at 999 of its 1000-LOC budget, which is what
forced every other line of this change into a leaf; it is at 999 still.

**`embedded/server.py`** starts the heartbeat on the http serve path only. A stdio
standalone backend records nothing and has no proxy watching it, so it stamps nothing.

Not touched, deliberately: `scheduling_lag` (consumed, never modified — including
`MAX_STRETCH`), `tests/test_singleton_cold_start_patience.py`,
`_same_identity_backend_ready`'s own patient loop, the adoption order, the cold-start
lock, and `backend_eviction`'s protection rule.

## 5. Tests

RED-first, hermetic, on doubles.

`tests/test_proxy_starvation_witness.py` — (a) and (b):

- a starved strike run (a `fair_window` factory whose window reports unexpired) does
  NOT condemn after `failures_before_teardown` misses, and does not consult the
  patient confirmation gate at all;
- once the window IS spent, the same run condemns — so the window delays a verdict
  and never suppresses one;
- a FRESH heartbeat resets the strike run, reports at INFO, and never pays the
  confirmation probe;
- a STALE heartbeat and an ABSENT heartbeat each fall through to the confirmation and
  condemn exactly as today;
- the heartbeat is read WITHOUT `anyio.to_thread.run_sync`, pinned by the existing
  exact-call-list assertion in `test_watchdog_busy_vs_dead.py`;
- the default `fair_window` is `scheduling_lag.FairWindow` and it is constructed with
  `interval * (failures_before_teardown - 1)` (the one-policy pin);
- the idle-machine arithmetic: with a real `FairWindow` and a real (short) interval,
  three misses still condemn on the third strike.

`tests/test_backend_heartbeat.py` — (b)'s two halves:

- `backend_registry.stamp_heartbeat` updates the entry on THAT port, leaves every
  other entry byte-identical, and writes nothing when no entry claims the port;
- a stamp survives a concurrent `record_backend` of a different port;
- `backend_liveness.self_report` answers the age for a fresh stamp, `None` for a stale
  one, `None` for an absent one, `None` when `heartbeat_pid` disagrees with the
  entry's `pid`, and `None` for a hand-edited non-numeric stamp;
- a stamp from the future beyond the threshold is not evidence;
- `start_beating` is idempotent per process (the runpy triple-execution case) and
  stamps on the loop it is started on;
- a 2.1.9-shaped entry (no heartbeat fields) reads as absent, and stamping one does
  not change any other field.

`tests/test_proxy_retry_forever.py` — (c):

- a heal that never succeeds does NOT return from `drive`: the loop keeps going and
  the delays follow the bounded, capped, jittered schedule;
- `MAX_CONSECUTIVE_HEALS` consecutive deaths trigger a backoff, not a return;
- in-flight calls are answered with the JSON-RPC error on every generation, including
  the ones that end in a backoff;
- a backend that comes back after a backoff is re-bridged, the replay is re-sent, and
  the retry counter resets;
- `UNREACHABLE_EVENT` ships once per backoff with `reason`, `attempt` and `delay`;
- client cancellation still unwinds `drive` immediately (the one exit).

`tests/test_mixed_version_adoption.py` — (d):

- a recorded version strictly newer than ours matches identity, so
  `_same_identity_backend_ready` passes and `_clear_stale_backend` spares it;
- an older one does not match and is evicted when unprotected;
- same version + different digest does not match (issue #14 preserved);
- `build_identity.newer` on the ordering table, including `UNKNOWN_VERSION`,
  unparseable strings, unequal segment counts and a non-`str` recorded value.

`tests/test_backend_env_scrub.py` — F-890; see that finding's §5.

Updated deliberately, with the justification convention 4 requires — these pinned the
behaviour (c) reverses, and each is now the negative of what it was:

- `test_proxy_selfheal.py::test_an_unhealable_death_returns_for_the_legacy_teardown`
  and `::test_an_unhealable_death_still_tears_the_proxy_down` — an unhealable death
  now opens a backoff and a generation 2, and `drive` does not return;
- `test_proxy_selfheal.py::test_a_flapping_backend_stops_being_healed` — flapping
  still stops the healing, and now enters backoff instead of returning;
- `test_proxy_sentry_reporting.py`'s two teardown pins — the event is
  `UNREACHABLE_EVENT` with the same `reason` values;
- `test_proxy_backend_death.py`'s unhealable premise — restated as "the proxy stays
  up";
- `test_e2e_lifecycle_resilience.py`'s two log-grep rows — the `giving up` wording is
  gone; the rows follow it.

## 6. Residuals

1. **Calls issued DURING a backoff window are buffered, not answered.**
   `PendingCalls` tracks at the WRITE to the backend, so a request the client sends
   while no backend exists sits in `to_backend_tx`'s 1024-slot buffer until a
   generation opens. This is unchanged behaviour — the same buffer already absorbs the
   120 s `BACKEND_READY_TIMEOUT` of an ordinary cold start — but the CONSEQUENCE
   changed direction: before this PR the proxy exited and the client abandoned those
   calls, and now they wait. For a long outage that is a client hanging on a tool call
   instead of losing its server. It is the deliberate trade (c) makes, and beyond 1024
   buffered messages `pump_client` itself blocks.
2. **The heartbeat is a liveness claim, not a usefulness claim.** A backend whose
   event loop turns but whose tool bodies all fail keeps stamping, and this witness
   will keep vetoing its condemnation. That is correct — the watchdog's job was never
   to judge tool health — but it means the ONE failure this makes slower to detect is
   a backend that is scheduled and responsive at the loop level while being unable to
   answer `initialize`. The confirmation phase still catches it; it just waits for the
   stamp to age first (≤ 30 s).
3. **Two writers, no lock, on `server.json`.** The heartbeat write is atomic
   (`_write`'s tmp + `os.replace`) and re-reads first, but it does not hold the
   cold-start lock, so a `record_backend` landing between the read and the write can
   lose one stamp. The cost is one 3 s interval of extra age, and the next stamp
   corrects it. Making the backend take the cold-start lock every 3 s would serialise
   it against every proxy start on the machine, which is a much worse trade.
4. **The heartbeat cadence costs one small file write every 3 s per backend.** It is
   an `os.replace` of a sub-kilobyte file. On a spinning disk or a synced folder that
   is not free, and the state dir is `~/.stealth-mcp` — which on this developer's
   machine is NOT under OneDrive, but need not be true of every user.
5. **`MAX_STRETCH` still bounds (a).** A process starved beyond 4× for the whole strike
   run will still spend its window. What saves it then is (b), and what saves it if
   (b) is absent (a 2.1.9 backend) is nothing — a mixed fleet gets (a) and (c) but not
   (b), so the standing "upgrade together" rule still holds.
6. **(d) has no downgrade story.** A fleet that rolls BACK leaves a newer recorded
   backend that every older proxy now adopts, and the rollback does not take effect
   until that backend dies. This is the intended direction of the asymmetry; it is
   named here because "my downgrade did nothing" is otherwise a mystery.
7. **§(e): the 81 temp-profile Chrome processes are NOT this product's, and nothing
   should be done about them.** Traced end to end:
   - `browser_manager._launch_browser` has exactly two branches
     (`browser_manager.py:534-546`) and both pass `options.user_data_dir` —
     `desktop_launch.launch_and_attach(...)` or `uc.Config(user_data_dir=...)`.
     `_resolve_launch_args` never touches it.
   - `BrowserManager.spawn_browser` has ONE caller in `src/`
     (`tool_sections/browser_management.py:169`) and it always sets
     `user_data_dir=selected_user_data_dir` from `clone_storage.resolve_profile_selection`.
   - `resolve_profile_selection` has three returns, all `str(<concrete path>)`
     (`clone_storage.py:963-968`, `:973-978`, `:896` via `_copy_clone_from_source`);
     its only other exit RAISES. `_fallback_profile_selection` returns a dict carrying
     the same non-empty dir, a fresh `resolve_profile_selection`, or `None` — and
     `None` is re-raised by the caller (`browser_management.py:190-191`), never
     launched on.
   - `desktop_launch.launch_and_attach` passes the dir into `uc.Config`
     (`desktop_launch.py:505-510`) and writes the resulting argv into its launcher
     script.
   - **The discriminator is the prefix.** nodriver 0.47's `Config.__init__` does
     synthesize a profile when none is given — but through
     `temp_profile_dir()`, which is `tempfile.mkdtemp(prefix="uc_")`
     (`nodriver/core/config.py:245-248`). The orphans are `tmp*`, which is
     `mkdtemp()`'s DEFAULT prefix and therefore cannot be nodriver's. Measured on this
     box: 1024 `tmp*` directories in `%LOCALAPPDATA%\Temp`, 459 of them holding a
     Chrome `Default\`, and **zero** `uc_*` directories — consistent with "no path in
     this product has ever synthesized one".
   - The 8 live `tmp*` Chrome roots at the time of the trace all share one parent, a
     different local project's scraper (`-m src.main browser-scraper --workers 9`),
     and their argv lacks `--remote-allow-origins=*`, which nodriver's `Config` adds
     unconditionally to every browser this product launches.
   - `process_cleanup._sweep_orphaned_temp_profiles` globs
     `PROFILE_SWEEP_PREFIX = "uc_"` (`process_cleanup.py:50`, `:572`). Nothing under
     `src/` matches `tmp*`, and **nothing should**: reaping a foreign Chrome is
     precisely what F-811 forbids, and `tmp*` is the default prefix of every Python
     program on the machine.

   **Recommendation: none.** No code change, no sweep, no widened matcher.
