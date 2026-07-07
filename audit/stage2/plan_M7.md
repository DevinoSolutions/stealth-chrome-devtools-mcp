# Stage 2 Plan — M7 `close_instance` Teardown Must Not Freeze the Shared Backend (+ F-745 touch-activity clarity, + F-608 fallback-PID identity check)

- **Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend`
- **Date:** 2026-07-03
- **Batch:** {**M7** = F-180 (core) + F-164 (ruled split), **F-745** (clarity lens, re-homed to M7), **F-608** (operability, re-homed from M8)}; **F-611** explicit in/out ruling below.
- **Base tree:** post-`plan_M3`(+A1 DebugLogger→RLock) + `plan_M1` + `plan_M8`(+A1 port fallback) + `plan_M2`. M7 executes **fifth**, serially, from M2's final commit.
- **Status:** **APPROVED** (human, 2026-07-03) — cleared for Stage 3. Decisions: **F-745 Option A** (flip `touch_activity` default to `False`; ship now, tunable via `BROWSER_IDLE_TIMEOUT`; M4 may add per-interaction precision later); **F-164 split** (fix the `cdp_function_executor` tab-in-hand half now; `server.py:_with_cdp_timeout` half re-homed to M4). Q1 (nodriver `stop()` signature) is a Stage-3 confirmable, not a gate decision — the design handles both. Orchestrator verified 3 load-bearing anchor claims in source (the `:761-782` re-run-kill fallback, `navigate` touch at `:986`, the F-608 `:411-413` gap).
- **Test baseline:** `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed**, coverage gate **39**. `uv run` is BROKEN — never used.

---

## The one fact that shapes this plan

`close_instance` wraps its whole teardown in `asyncio.wait_for(_do_close(), timeout=5.0)` (`browser_manager.py:760`), which **reads as a hard 5-second bound but is not one.** The calls that do the real kill work — `process_cleanup.kill_browser_process` (`:682`), `browser.stop()` via `_stop_browser` (`:688`→`:134-138`), and `process_cleanup.finalize_browser_process` + `cleanup_deferred_profiles` (`:743-744`) — are **synchronous**. `asyncio.wait_for`'s cancellation is cooperative: it can only fire at an `await` boundary, and a synchronous call never yields one. So while those calls run, the **single event loop is blocked** and the 5.0 s timer physically cannot fire.

This is not a shutdown-only helper. `close_instance` is a **live MCP tool** (`server.py:1453`→`browser_manager.close_instance:1469`) on the **one detached per-user backend shared by every session**. One client closing one wedged Chrome freezes the whole loop — every other session's dispatch, every browser, and the idle reaper — for the duration.

**Honest bound (adversarial downgrade Crit→High, preserved):** the freeze is **not infinite**. `_kill_process_by_pid` bounds its waits (`process_cleanup.py:880` `wait(timeout=3)`, `:902` `wait(timeout=2)`), and on Windows `terminate()==kill()==TerminateProcess` is immediate, so the typical freeze is sub-second and the worst case is bounded **~5 s × N-pids**, not forever. M7 does not fix an infinite hang; it makes the **advertised `wait_for(5.0)` bound real** by moving the synchronous kill off the event loop, so one wedged close can no longer stall every other session even for those bounded seconds.

**Interplay with M1 (now in the base tree):** M1's liveness watchdog treats "the backend cannot answer an HTTP `initialize`" as a wedge and, after ~3 failed probes (~12 s), tears down the proxy → the next session's reuse-probe evicts and **respawns the backend, killing every live browser** (`plan_M1.md` §2.1-C point 3, §7). An event-loop-blocking `close_instance` is exactly that "can't answer HTTP" state. So post-M1 an unfixed F-180 does not merely *pause* sessions — a long enough synchronous close can **trip M1 into nuking the shared backend**. M7's offload keeps the loop yielding during the kill, so the backend keeps answering `initialize` and the watchdog stays green. **M7 and M1 are complementary: M1 recovers from a wedge by respawning; M7 removes the wedge so recovery is never needed.**

---

## 1. Scope

### 1.1 Confirmed code anchors at HEAD (re-opened and verified at the pinned SHA while writing this plan)

**`embedded/browser_manager.py`** (1335 lines):

- **F-180 — `close_instance`** `:605`; inner `async def _do_close` `:617`, which acquires `async with self._lock` at `:618` and **holds it across the entire teardown** (`:618-757`). Inside, in order:
  - close-tabs loop `:627-640` (awaitable `tab.close()`, unbounded but yields);
  - CDP `browser.close()` — `asyncio.wait_for(..., 2.0)` `:654-657` (**already bounded**, awaitable);
  - `connection.disconnect()` — `asyncio.wait_for(..., 2.0)` `:668-670` (**already bounded**, awaitable);
  - **`process_cleanup.kill_browser_process(instance_id)` `:682` — SYNCHRONOUS, blocks the loop;**
  - `await self._stop_browser(browser)` `:688` (`_stop_browser` `:134-138`: calls `browser.stop()` **synchronously**, then awaits the result only *if* it is a coroutine) — **the sync `browser.stop()` invocation blocks the loop;**
  - `await self._close_proxy_forwarder(instance_id)` `:696` (awaitable);
  - direct `browser._process.terminate()/kill()` + `os.kill(_process_pid, 15)` retry loop `:703-728` — synchronous but **non-waiting** (fast);
  - clear `browser._process`/`_process_pid` reach-ins `:730-733`, `instance.state = BrowserState.CLOSED` `:735`;
  - **`process_cleanup.finalize_browser_process(instance_id)` + `cleanup_deferred_profiles()` `:743-744` — SYNCHRONOUS;**
  - `del self._instances[instance_id]` `:752`, `_spawn_diagnostics.pop` `:753`, `persistent_storage.remove_instance` `:755`, `return True`.
  - Outer: `return await asyncio.wait_for(_do_close(), timeout=5.0)` `:760`; `except asyncio.TimeoutError` fallback `:761-782` **re-acquires the lock and re-runs the same synchronous `kill_browser_process`/`finalize_browser_process`/`cleanup_deferred_profiles` at `:768-770` with NO timeout wrapper** (F-180's "makes it worse"); `except Exception → return False` `:783-785`.
- The nodriver private reach-ins `browser._process` / `browser._process_pid` (`:703-733`) — **third-party-interop necessity; stay in place** (modularity lens accepts them; documented non-issue).
- **F-745 — `get_tab`** `:1060` (`touch_activity: bool = True` `:1063`; `if touch_activity: await self.touch_instance(...)` `:1077-1078`); **`get_browser`** `:1082` (`touch_activity: bool = True` `:1085`; touch `:1099-1100`).
- Idle machinery that `touch_activity` feeds: `touch_instance` `:219` (`async with self._lock` `:229` → `instance.update_activity()` `:232`); `_run_idle_reaper` `:235`; `cleanup_inactive` `:1296` (reads `instance.last_activity` `:1319`, calls `close_instance` per idle id `:1322-1323`, **outside the lock**); config `DEFAULT_IDLE_TIMEOUT_SECONDS = 600` `:63`, `_idle_timeout_seconds_default = _parse_nonnegative_int_env("BROWSER_IDLE_TIMEOUT", 600)` `:71` — **the reaper is ON by default (600 s)**. `navigate` already touches **independently** of `get_tab`: `await self.touch_instance(instance_id)` `:986`.
- Existing lock-free bookkeeping seam: `_discard_instance_unlocked` `:162-191` (pops `_instances`/`_spawn_diagnostics`/`_proxy_forwarders`, calls `finalize_browser_process`+`cleanup_deferred_profiles`, `persistent_storage.remove_instance`, `dynamic_hook_system.remove_instance`). Used by pruning paths; a useful reference shape for M7's Phase-1 claim (see §2.1). Its own synchronous finalize is the same family as F-180 but on a different method — **noted, not in scope** (§1.4).

**`embedded/process_cleanup.py`** (1023 lines):

- **F-608 — `_kill_processes_for_metadata`** `:348` (`recovery: bool = False` `:352`). `pids_to_kill = _get_browser_pids_for_profile(...)` `:367`; `fallback_pid = metadata.get("pid")` `:368`; `stored_create_time = metadata.get("create_time")` `:369`.
  - `if recovery:` `:371-410` — filters profile PIDs by `create_time < _init_time` `:375-388`, and for the empty-profile fallback (`:390-408`) verifies `psutil.Process(fallback_pid).create_time()` against **both** `_init_time` (`actual_create_time < self._init_time`, `:400`) **and** the stored value (`stored_create_time is None or abs(actual - stored) < 1.0`, `:396-399`).
  - **`else:` `:411-413` — the non-recovery (explicit-close) branch:** `if not pids_to_kill and isinstance(fallback_pid, int): pids_to_kill = {fallback_pid}` — **zero identity verification.**
  - `_kill_process_by_pid` `:837` bounds its own waits: `wait(timeout=3)` `:880`, `wait(timeout=2)` `:902`.

**`embedded/server.py`** (4207 lines) — F-164 site + F-745 tool call sites:

- **F-164 — `_with_cdp_timeout(coro, timeout, instance_id)`** `:139-156`: on `asyncio.TimeoutError` it only re-raises with a message that **already** advises "Try closing the instance with close_instance and spawning a new one" `:155`. **Has no `tab`/`connection` handle** — only `instance_id`. Never issues `Runtime.terminateExecution`.
- **F-745 — 50 `get_tab(instance_id)` call sites** (all bare, no `touch_activity` arg) + 1 `get_browser` (`:2650`); the tool wrapper `close_instance` `:1453` and the `@section_tool` dispatch chokepoint (`server.py:1212-1217` at HEAD, wrapped by M3 for correlation).

**`embedded/cdp_function_executor.py`** (842 lines) — F-164 second site:

- **`execute_python_in_browser(self, tab, python_code)`** `:680`: `await asyncio.wait_for(self.inject_and_execute_script(tab, js_code), timeout=10.0)` `:696-699`; on `TimeoutError` returns `{"success": False, "error": "Python execution timeout - code may have infinite loop or syntax error"}` `:701-702`. **Has a live `tab` handle.** `list_cdp_commands` `:103` catalogs `terminate_execution` but nothing invokes it.

**F-611 anchors (for the ruling, §8):** `_browser_process_is_alive` `:141-160`; `list_instances` `:588`.

### 1.2 Files to be touched (source + tests)

**Source (M7 core — F-180, F-608, F-745-primary):**
- `embedded/browser_manager.py` — refactor `close_instance` to offload the synchronous kill off the event loop under a real timeout (F-180); flip `get_tab`/`get_browser` `touch_activity` default to `False` + honest docstrings (F-745, Option A — §2.1-F).
- `embedded/process_cleanup.py` — add the fallback-PID **identity** check to the non-recovery branch (F-608), factored so both branches share one identity idiom.

**Source (F-164 — split ruling, §2.1-G / §8):**
- `embedded/cdp_function_executor.py` — best-effort `Runtime.terminateExecution` + honest message at `execute_python_in_browser` (the site that owns a `tab`).
- *(server.py `_with_cdp_timeout` half re-homed to M4 — see §8; NOT edited here.)*

**Tests (new + changed) — see §5:**
- New: `tests/test_close_instance_offload.py` (F-180 pinning: loop-stays-responsive, real-bound, idempotency).
- New: `tests/test_touch_activity_semantics.py` (F-745: read does not bump, explicit touch does, idle reaper reaps a read-only-polled instance).
- Extend: `tests/test_process_cleanup.py` (F-608: fallback identity match/mismatch/None).
- Extend: `tests/test_cdp_timeout.py` **iff** it depends on `get_tab` touching (verify `:411`) — see §5.3.

### 1.3 Anchors that predecessors SHIFT (re-anchor by symbol in Stage 3)

M7 runs **fifth** (post-M3+A1, M1, M8+A1, M2). Effect on my anchors, by file:

- **`browser_manager.py` — my anchors are UNSHIFTED.** The only predecessor that edits this file is **M3's M10a-7a**, which adds one level-appropriate log line inside the `except Exception` handlers of **`switch_to_tab`** (`plan_M3.md` §1.1 row 1, `:1165`) and **`close_tab`** (row 2, `:1207`). Both handlers sit **below** every M7 anchor: `_stop_browser:134`, `_browser_process_is_alive:141`, `touch_instance:219`, `close_instance:605-785`, `navigate touch:986`, `get_tab:1060`, `get_browser:1082`. So `close_instance`, `get_tab`, `get_browser` keep their HEAD line numbers; only code below `~:1132` drifts by ≈ +2 lines. **Cross-review ruling confirmed:** "M7 planner: browser_manager.py low-conflict (M10a-7a logs switch_to_tab/close_tab; M7 edits close_instance)." M3 also makes `debug_logger` record **unconditionally to file**, so every WARNING/INFO M7 adds inside `close_instance` is **durably captured for free** (no extra wiring).
  - *Behavioral note (not a line collision):* `close_tab` calls `self.get_tab(instance_id)` (`:1178`) and `self.get_browser(instance_id)` (`:1143`/`:1191`). After F-745 flips the default (§2.1-F), those internal calls stop touching — intended (tab-close/switch are not "keep-alive" activity for the reaper). Re-anchor M3's log edits by the `return False` handler substring, not by line.
- **`process_cleanup.py` — my anchor (`_kill_processes_for_metadata:348-413`) is UNSHIFTED.** M8 "thin-triggers" `process_cleanup` without editing it (`plan_M8.md` §1.4); M3/M1/M2 do not touch it. **Overlap flag (M11a, runs AFTER me):** M11a refactors `process_cleanup`'s **init/guard** + `_file_lock` (`:216-225`) + adds a public `recover_orphans()` seam (`state.json` cross-review). Those regions are **disjoint** from `_kill_processes_for_metadata` (`:348-413`). M11a rebases over my F-608 edit; I flag it in the WHEN-DONE message. Anchor correction carried forward: `_recover_orphaned_processes` is **defined** at `:599`.
- **`server.py` — re-anchor by symbol.** M3 inserts the `section_tool` correlation wrapper (`:1212-1217`) and M10a logs (`:198/:997/:1226`); **M2 deletes `hot_reload`/`reload_status` (`~:2974-3038`)**, shifting everything below down by ≈ −66 lines. F-164's `_with_cdp_timeout` (`:139`) sits **above** all of these → **line-stable**, but it is **re-homed to M4** (§8) so M7 does not edit `server.py` for F-164. (If the human elects F-745 Option B, §2.2, the interactive-tool bodies shift under M2's deletion — re-anchor each by `async def <tool_name>`.)
- **`cdp_function_executor.py` — UNSHIFTED.** No predecessor edits it. `execute_python_in_browser:680` / `wait_for(10.0):696` are HEAD-accurate.

**Stage-3 rule:** locate `close_instance` by `async def close_instance(self, instance_id`; the inner offload target by the `process_cleanup.kill_browser_process(` and `finalize_browser_process(` substrings inside `_do_close`; the F-608 branch by `else:` immediately following `if recovery:` inside `_kill_processes_for_metadata`; the accessors by `async def get_tab(` / `async def get_browser(`; the F-164 site by `async def execute_python_in_browser`. Do not trust raw `:NNN` in `server.py` after the M3/M2 merge.

### 1.4 Explicit out-of-scope (stated so Stage 3 does not creep)

- **M11a** — `process_cleanup` **init/guard** refactor, **F-607** (`_file_lock` silent-yield, `:216-225`), and the public **`recover_orphans()`** seam. Same FILE as F-608, **different REGION** (`:348-413` vs init/`:216-225`). Flagged for sequencing; M11a lands after M7.
- **F-164's `_with_cdp_timeout` half (`server.py:139-156`)** — re-homed to **M4** (owns `server.py` + the F-603 timeout-preamble convention); it also needs to resolve a `tab` from `instance_id`, which is coupling M7 should not add. M7 fixes only the `cdp_function_executor` site that already owns a `tab` (§2.1-G, §8).
- **F-611** — the `list_instances` per-instance CDP liveness probe. **Ruled OUT of M7**, re-homed to M6 + filed as follow-up (§8).
- **F-745 Option B** (explicit per-interactive-tool touch in `server.py`) — **not** the primary; presented as an alternative the human may elect, whose natural home is M4's dispatch/registry work (§2.2, §8).
- `_discard_instance_unlocked`'s own synchronous `finalize_browser_process`/`cleanup_deferred_profiles` (`:172-173`) — same blocking family, **different method**, reached by pruning not by `close_instance`. Not in scope; a mid-fix discovery here becomes a **new finding**.
- **M9** (network body store), **M5** (cloners), **M4** (`server.py` decomposition, incl. the `section_tool` registry + eviction extraction), **M6** (the characterization suite itself). The nodriver `_process`/`_process_pid` reach-ins stay.
- No drive-by refactors; any new problem discovered mid-implementation becomes a **new finding** (schema carries `modularity|duplication|clarity`), not scope.

---

## 2. Approach + rejected alternatives

### 2.1 Chosen design

**F-180 — make the `wait_for(5.0)` bound real by offloading the synchronous kill to a worker thread, under a real timeout, with the `_instances` lock released across the blocking section.**

The teardown splits into three phases with **one** lock acquisition:

**A. Phase 1 — claim (under `self._lock`, on the loop, O(microseconds)).**
Acquire `self._lock`; if `instance_id not in self._instances` → `return False` (natural **idempotency**: a second/concurrent close finds nothing to claim). Otherwise **pop** the instance's records *now* — `self._instances.pop(instance_id)`, `self._spawn_diagnostics.pop(instance_id, None)`, `self._proxy_forwarders.pop(instance_id, None)` — capturing local refs (`browser`, `instance`, `proxy_forwarder`). Set `instance.state = BrowserState.CLOSED`. Release the lock. After Phase 1 the instance is **claimed and invisible**: `get_instance`/`get_tab` return `None`, a concurrent `close_instance` returns `False`, and no reaper can double-close it. This is the same "pop-everything-then-tear-down" shape `_discard_instance_unlocked` (`:162-191`) already uses — reused as the model, not copied.

**B. Phase 2 — graceful CDP teardown (on the loop, no lock, already bounded).**
Keep the existing awaitable, already-bounded steps exactly as they are, just **outside** the lock: the close-tabs loop, `browser.close()` under `wait_for(2.0)`, `connection.disconnect()` under `wait_for(2.0)`, and `_close_proxy_forwarder`. These `await` and yield, so the outer timeout can already preempt them; they must not run under the lock (that is what would make every other session's `get_tab`/`spawn` queue behind a slow close).

**C. Phase 3 — blocking kill (off the loop, no lock, real timeout).**
Factor the synchronous work into one private sync helper `_blocking_teardown(instance_id, browser)` that runs, in order: `process_cleanup.kill_browser_process(instance_id)`; `browser.stop()` (the synchronous nodriver call); the `browser._process.terminate()/kill()`/`os.kill` retry loop (`:703-728`); `process_cleanup.finalize_browser_process(instance_id)` + `cleanup_deferred_profiles()`. Invoke it as:
```
await asyncio.wait_for(asyncio.to_thread(self._blocking_teardown, instance_id, browser), timeout=CLOSE_KILL_TIMEOUT)
```
Now the timer bounds a **coroutine that actually yields** (`to_thread` awaits a thread future), so `wait_for` can fire. If it fires, the worker thread keeps running to completion in the background (Python cannot kill a thread) doing its already-bounded psutil `wait(3)`/`wait(2)` calls — but **the event loop is never blocked**. `CLOSE_KILL_TIMEOUT` is a named constant (default 5.0, env-overridable via the existing `_parse_nonnegative_int_env`/`parse_float_env` convention) so the "5 s" is one self-describing symbol, not a magic literal.
  - **`browser.stop()` async-return edge:** `_stop_browser` (`:134-138`) calls `browser.stop()` and awaits the result *only if it is a coroutine* — a defensive guard for nodriver API drift. A coroutine cannot be awaited inside `to_thread` (no loop there). Design: `_blocking_teardown` calls `browser.stop()`; if the return is awaitable it does **not** run it in-thread — it returns a sentinel so `close_instance` awaits that coroutine back on the loop under a small `wait_for`. In the pinned nodriver, `Browser.stop()` is synchronous, so this path is the rare edge; it is handled, not assumed away. (Open question Q1, §Appendix, flags confirming the pinned nodriver's `stop()` signature.)

**D. Phase 4 — finalize bookkeeping (no lock needed).**
`persistent_storage.remove_instance(instance_id)` (its own module's concern; already called lock-free elsewhere) and close the captured `proxy_forwarder` (`await proxy_forwarder.close()` on the loop, or `asyncio.create_task` as today's fallback does). Because Phase 1 already popped every shared dict under the lock, **no lock re-acquire is required** — a strict improvement over the brief's "reacquire → finalize" sketch (one lock round-trip instead of two; nothing in Phase 4 depends on the kill result). `return True`.

**E. Timeout semantics on expiry (fixes F-180's "fallback makes it worse").**
On `asyncio.wait_for` TimeoutError in Phase 3: the instance is **already de-registered** (Phase 1), so there is nothing to re-pop and **no reason to re-run the synchronous kill inline** (the exact defect at `:768-770`). Instead: emit a durable WARNING (`Chrome kill for {id} exceeded {CLOSE_KILL_TIMEOUT}s; worker thread continues in background, orphan will be reaped by process_cleanup`) and `return True`. The still-running worker thread finishes the bounded kill; anything it misses is caught by `process_cleanup`'s deferred-profile cleanup and by M8's `kill-orphans` / the next fresh-backend orphan reap (the well-engineered `--user-data-dir` + `create_time` matcher the REPORT says to preserve). **No second unbounded synchronous kill path remains.**

**F. F-745 — flip `get_tab`/`get_browser` `touch_activity` default to `False` (Option A: honest reads, browser_manager-local, zero server.py churn).**
Change both signatures to `touch_activity: bool = False` and rewrite the docstrings to state plainly: *"Pure read; does NOT refresh the idle timer. Pass `touch_activity=True` (or call `touch_instance`) to record activity."* Verified rationale: **every** current `get_tab`/`get_browser` toucher is a genuine tool invocation (50 tool sites + a few internal callers that are themselves tool-driven) — there is **no** background/non-tool caller silently touching — so F-745 is a **clarity defect, not a behavior bug** (the name lies; the writes are legitimate). Flipping the default makes the name honest. The one activity signal that must survive — **navigation** — already touches **independently** at `navigate:986`, so it is unaffected. The resulting behavior delta is **exactly the brief's stated intent** (§4): *"a long-idle instance is no longer kept alive by read-only polling."* Post-flip, the idle reaper is fed by explicit activity (navigation) rather than by every incidental read.
  - This is fully inside `browser_manager.py` (two defaults + two docstrings), introduces **no second accessor and no second way** (conventions lens), and needs **no `server.py` edit** (no M4 collision).
  - The residual — an agent that interacts without ever navigating (e.g. a long click/type flow) for > `BROWSER_IDLE_TIMEOUT` (default 600 s) would be reaped — is (a) acceptable for a 0-user local tool where breaking changes are free, (b) mitigated by navigation touching and by the reaper being tunable/disable-able (`BROWSER_IDLE_TIMEOUT=0`), and (c) fully closeable by the **optional** Option B (§2.2) if the human wants per-interaction precision. This precision-vs-churn trade is the **one open question for the human** (Q2, §Appendix).

**G. F-164 — best-effort recovery only where a `tab` is in hand; re-home the rest (see §8 for the full ruling).**
At `cdp_function_executor.execute_python_in_browser` (`:696-702`), on `TimeoutError`, **before** returning the error, best-effort `await tab.send(uc.cdp.runtime.terminate_execution())` inside its own `try/except` (never raises), and change the returned message to be **honest**: *"Execution exceeded 10s; attempted Runtime.terminateExecution — if the tab stays unresponsive, close_instance and respawn."* The adversarial caveat is stated in a code comment: `terminate_execution` is **not** a reliable interrupt for a synchronous JS loop, so this is best-effort, not a guarantee. The `server.py:_with_cdp_timeout` half (no `tab` handle; M4-owned) is **not** edited here.

**Net M7 source surface:** `close_instance` restructured into 4 phases + one new `_blocking_teardown` helper + one `CLOSE_KILL_TIMEOUT` constant (browser_manager.py); two accessor defaults flipped + docstrings (browser_manager.py); one shared fallback-identity idiom added to the non-recovery branch (process_cleanup.py); one best-effort terminate + honest message (cdp_function_executor.py). No new kill/respawn machine — the existing `process_cleanup` matcher and M8/M1 recovery are reused.

### 2.2 Rejected alternatives

**F-180 offload mechanism:**
1. **`loop.run_in_executor(None, ...)` instead of `asyncio.to_thread`.** Functionally equivalent (`to_thread` *is* `run_in_executor` on the default executor + `contextvars` copy). Rejected as the primary spelling only because `asyncio.to_thread` is the higher-level, self-describing idiom and matches the existing convention (`server.py` already uses `asyncio.to_thread` for storage sweeps `:779` and snapshot refresh `:1478`). One way per thing (conventions lens).
2. **`ProcessPoolExecutor` / a subprocess kill.** Rejected: the blocking work is I/O-bound psutil/OS calls, not CPU-bound; a process pool adds pickling, a spawned interpreter, and its own teardown fragility for zero benefit. Threads are correct here.
3. **Fire-and-forget kill task (`asyncio.create_task(to_thread(...))`, return immediately).** Rejected: it makes `close_instance` return before the browser is actually dead, breaking the tool's contract (callers, incl. the idle reaper and `close_all`, assume the instance is gone on return) and hiding kill failures. The awaited `wait_for(to_thread(...))` keeps the contract *and* the bound: bounded wait, honest result, background completion only on the rare timeout.
4. **Kill via `process_cleanup` out-of-band (register the pid, let a reaper thread kill it) instead of inline.** Rejected as the mechanism: it re-architects the close path and duplicates a second "who kills the browser" owner (dedup/conventions defect). The inline offload keeps the one existing kill path, just off the loop. (`process_cleanup` remains the **fallback** reaper for the timeout-expiry orphan, §2.1-E — which is its existing job, not a new one.)
5. **Hold `self._lock` across the `to_thread` await (simplest diff).** Rejected: an `await` under the lock is a yield point, so every other session's `get_tab`→`touch_instance`, `spawn`, `list_instances`, and any concurrent `close` would block on the lock for the whole kill. Today they block anyway (frozen loop), so this "works," but it squanders the entire point of the fix (loop responsive, sessions independent). Popping under the lock in Phase 1 and releasing is the design.
6. **Keep the `TimeoutError` fallback re-running the kill (today's `:768-770`), just wrapped in `to_thread`.** Rejected: after Phase 1 the instance is already de-registered, so a second kill is redundant; re-running it (even offloaded) risks double-`finalize`/double-`del` races and re-introduces the "two kill paths" the finding flags. Expiry → log + trust the background thread + `process_cleanup` (§2.1-E).

**F-745 shape:**
7. **Rename to a `get_tab_and_touch` wrapper, leaving `get_tab` read-only (the finding's second option).** Rejected as primary: it forces **all 50 `server.py` call sites** (+ internal) to choose read-vs-touch — massive churn squarely in M4's file, and it introduces a **second accessor** (a "second way," which the conventions lens defines as a defect). Flipping the single existing default is smaller, honest, and adds no second way.
8. **Option B — flip the default AND add explicit `touch_instance` to each interactive tool in `server.py`.** The classification data shows the interactive/read split does **not** align with `@section_tool` sections (e.g. `element-interaction` contains both `click_element` [interactive] and `get_element_state` [read]; `cdp-functions` mixes `execute_script` [interactive] and `inspect_function_signature` [read]) — so a per-section dispatch rule is **too coarse**, and precision requires ~20 per-tool one-liners in `server.py`. Rejected as primary (server.py/M4 churn + per-tool judgment) but **offered to the human** (Q2): it is the only way to keep non-navigating interaction alive, and its natural home is M4's dispatch/registry work. Recorded as a re-home candidate in the WHEN-DONE message.
9. **Leave the default True and only document the write (the finding's "at minimum").** Rejected: documentation-only is the config-knob/band-aid pattern the standing guidance rejects (ship every-user structural defaults), and it does not deliver the brief's intended reaper behavior.

**F-164 shape:**
10. **Add `terminate_execution` to `_with_cdp_timeout` too (full bundling as the brief's "F-180/F-164" framing implies).** Rejected here: `_with_cdp_timeout` holds no `tab` — it would have to reach back through `browser_manager.get_tab(instance_id, touch_activity=False)` mid-timeout, adding server→manager coupling inside M4's file for a Medium, adversarially-downgraded finding whose error text already advises close+respawn. Re-homed to M4 (§8). M7 does the honest half where a `tab` is already in hand.
11. **Do nothing for F-164 (rule it fully OUT, since it is not a close/freeze bug).** Considered — it is defensible (downgraded Medium, bounded wait, remediation text present, `terminate_execution` unreliable). Rejected in favor of the small honest `cdp_function_executor` improvement, because the brief explicitly assigned F-164 to M7's close set and the `tab`-in-hand fix is low-risk, real value (the python-exec path attempts actual recovery + stops lying about "prevents hangs").

---

## 3. Sequencing (independently verifiable steps; one commit each)

Serial on branch `audit/fixes-2026-07-02`, based on M2's final commit. Pinning test written and shown passing **in the same commit** as its change. `-m "not integration"` green + coverage ≥ 39 at every checkpoint (verified with `.venv\Scripts\python.exe`).

- **M7-1 — F-180 executor offload.** Restructure `close_instance` into Phases 1–4 (§2.1-A–E); add `_blocking_teardown` + `CLOSE_KILL_TIMEOUT`. **Verify:** new `tests/test_close_instance_offload.py` (loop-stays-responsive under a 30 s stubbed kill; real ≤ ~6 s bound; idempotent double-close) green; full suite green.
  `.venv\Scripts\python.exe -m pytest tests/test_close_instance_offload.py -q`
- **M7-2 — F-608 fallback identity check.** Factor a shared `_fallback_pid_identity_ok(fallback_pid, stored_create_time)` (identity-only: `stored is None or abs(actual - stored) < 1.0`) and call it in the non-recovery branch before trusting `fallback_pid`; the recovery branch keeps its extra `_init_time` predate clause layered on top. **Verify:** extended `tests/test_process_cleanup.py` (match→kill, mismatch→skip, None→kill) green; existing recovery + non-recovery tests still green.
  `.venv\Scripts\python.exe -m pytest tests/test_process_cleanup.py -q`
- **M7-3 — F-745 default flip.** `touch_activity: bool = False` on `get_tab`/`get_browser` + honest docstrings. **Verify:** new `tests/test_touch_activity_semantics.py` (read does not bump `last_activity`; `touch_activity=True` bumps; `navigate` bumps; `cleanup_inactive` reaps a read-only-polled instance) green; check `tests/test_cdp_timeout.py` still green (§5.3).
  `.venv\Scripts\python.exe -m pytest tests/test_touch_activity_semantics.py tests/test_cdp_timeout.py -q`
- **M7-4 — F-164 best-effort recovery (cdp_function_executor only).** Best-effort `terminate_execution` + honest message at `execute_python_in_browser`. **Verify:** extended `tests/test_cdp_timeout.py` (on timeout, `tab.send(terminate_execution)` attempted; still returns the error dict; message updated) green.
  `.venv\Scripts\python.exe -m pytest tests/test_cdp_timeout.py -q`
- **Full checkpoint after each:** `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → 402 (+ new) green, coverage ≥ 39.

Steps are independent: M7-1 (browser_manager close path), M7-2 (process_cleanup), M7-3 (browser_manager accessors), M7-4 (cdp_function_executor) touch disjoint code and can land/revert in isolation.

---

## 4. Breaking changes

0 users → no external breakage. Behavior deltas (all acceptable per the "breaking changes are FREE" mandate):

- **`close_instance` returns on a real bound and the loop stays responsive.** On a wedged Chrome, it now returns within `CLOSE_KILL_TIMEOUT` (~5 s) instead of blocking the whole backend for ~5 s × N; the kill may complete in a background thread after the tool returns (orphan reaped by `process_cleanup`/M8). The happy path is unchanged (fast close, `True`). Double-close now returns `False` on the second call (was: re-entered teardown).
- **Idle eviction fires more predictably (F-745).** Read-only tool calls (`get_page_content`, `get_element_state`, `take_screenshot`, `extract_*`, `get_cookies`, …) **no longer refresh** the idle timer; navigation still does. A long **non-navigating** session can now be reaped after `BROWSER_IDLE_TIMEOUT` (default 600 s) — the brief's intended delta; tunable via env, and fully preservable via Option B (Q2).
- **`_kill_processes_for_metadata(recovery=False)` now skips a `fallback_pid` whose `create_time` does not match the stored value** (a recycled PID) — it previously killed it blind. It still kills current-session processes (no `_init_time` predate filter is added to the non-recovery branch — that clause stays recovery-only).
- **`execute_python_in_browser` attempts `Runtime.terminateExecution` on timeout** and returns an honest message. No signature change.

No public tool signatures change; no tool is added or removed.

---

## 5. Test strategy

Guiding rule (superpowers TDD): pinning test written and shown passing in the same commit. All new tests are **hermetic** (fakes only, no real Chrome, no backend) so they stay in `-m "not integration"` and are net-positive on coverage. `test_browser_integration.py`'s existing `close_instance` guards ("must be fast" `:169`, "kill the whole tree" `:203`) are **integration** (real Chrome) and are **not** in the 402 baseline — M7 does not rely on them; it adds hermetic equivalents.

### 5.1 The hard part — proving the loop stays responsive during a stuck kill (the exact F-180 scenario)

A fake `BrowserManager` instance seeded with one fake `browser` whose `_process`/`stop` are stubs. Monkeypatch `process_cleanup.kill_browser_process` to `time.sleep(30)` (a wedged synchronous kill). Concurrently:
- start an asyncio "heartbeat" task that increments a counter every 0.05 s;
- `await close_instance(id)` with `CLOSE_KILL_TIMEOUT` set to ~1 s.

**Assertions (fail without the fix, pass with it):**
- `close_instance` returns within ≈ `CLOSE_KILL_TIMEOUT` (≪ 30 s);
- the heartbeat counter advanced by ≥ ~15 during the close (**loop never froze** — this is the whole finding);
- the instance is gone from `_instances` on return (claimed in Phase 1, so removed even on kill-timeout);
- a WARNING was recorded (durable via M3) and the kill path was **not** re-run inline.
Baseline pre-change run of the same test (against today's synchronous `close_instance`) is documented as RED (counter frozen / 30 s wall) to prove the test bites.

### 5.2 Idempotency / concurrency

- **Double close:** two sequential `close_instance(id)` → first `True`, second `False`, kill work invoked exactly once (spy on `_blocking_teardown`).
- **Close during close:** two `close_instance(id)` scheduled concurrently → exactly one claims (Phase 1 pop under lock), the other returns `False`; no double-`del`/double-`finalize`.
- **Happy path:** fast stubbed kill → returns `True` quickly, instance removed, `persistent_storage.remove_instance` called once.

### 5.3 F-745 touch semantics (first tests for this machinery — currently 0 exist)

- `get_tab(id)` / `get_browser(id)` (new default) → `last_activity` **unchanged**;
- `get_tab(id, touch_activity=True)` → `last_activity` **advances**;
- `navigate(...)` → `last_activity` advances (via `:986`, unaffected by the flip);
- `cleanup_inactive()` reaps an instance that has only been **read** (never navigated) past its timeout, and does **not** reap one that navigated within the window — pins the intended reaper delta.
- **Existing-test check:** `tests/test_cdp_timeout.py:411` calls `bm.get_tab(instance_id)`; confirm it does not assert on `last_activity`/touch (it is a CDP-timeout test) — expected unaffected; if it incidentally depends on touch, adjust to pass `touch_activity=True` explicitly (mechanical). No other existing test references `touch_activity`/`last_activity`/`cleanup_inactive`/`update_activity` (grep-confirmed: 0 hits).

### 5.4 F-608 fallback identity

Extend `tests/test_process_cleanup.py` (unit; already uses `patch.object(pc, "_get_browser_pids_for_profile", ...)` and `mock_proc.create_time`):
- profile lookup empty + `fallback_pid` whose `create_time` **matches** `stored_create_time` (`abs < 1.0`) → **killed**;
- profile lookup empty + `fallback_pid` whose `create_time` **mismatches** stored (recycled PID) → **NOT killed** (new identity guard);
- profile lookup empty + `stored_create_time is None` → **killed** (best-effort parity with the recovery branch's `None` allowance).
**No existing test changes:** the current non-recovery test (`test_process_cleanup.py:231-248`) supplies a **non-empty** `_get_browser_pids_for_profile` result, so it takes the profile path and never reaches the fallback branch M7 hardens — it stays green unmodified. The recovery create_time tests (`:166-226`) are untouched.

### 5.5 F-164

Extend `tests/test_cdp_timeout.py`: a fake `tab` whose `inject_and_execute_script` never resolves within 10 s → assert `execute_python_in_browser` returns the error dict AND `tab.send` was called with a `terminate_execution` command object AND the message is the updated honest text; a `tab.send` that itself raises is swallowed (still returns the error dict).

---

## 6. Rollback + checkpoint commits

- **Branch:** `audit/fixes-2026-07-02`, **serial after M2's final commit** (base = post-M3+A1, M1, M8+A1, M2). Stage-3 discipline: pinning tests before the change, full suite green at every checkpoint, **deviation → stop and report to Fable**.
- **One commit per step (§3):** `M7-1 offload close_instance kill to a worker thread under a real timeout (F-180)` · `M7-2 verify fallback_pid identity on non-recovery cleanup (F-608)` · `M7-3 get_tab/get_browser default to read-only touch_activity=False (F-745)` · `M7-4 best-effort terminate_execution on python-exec timeout (F-164)`.
- **PR shape:** a single M7 PR of four commits (one finding-cluster, ≤ 3 source files) is simplest; the commit boundaries split cleanly if the human prefers (M7-1 alone is the load-bearing one).
- **What to revert if a step goes bad:** every step is independent. M7-1 is the highest-risk (control-flow rewrite of the close path) — revert it in isolation and the other three stand. M7-3's default-flip is a one-line revert if the reaper delta is unwanted. M7-2 and M7-4 are additive guards, revertible alone.
- **Checkpoint gate:** each commit leaves `-m "not integration"` green + coverage ≥ 39, verified with the venv python before proceeding.

---

## 7. Risk (blast radius, worst case, early-warning signs)

1. **The offloaded kill races the `_instances` dict / a re-used `browser` ref.** *Blast radius:* the closing instance only. *Mitigation:* Phase 1 pops all shared state under the lock and hands the worker thread **only local refs** (`browser`, `instance_id`); Phases 2–4 touch no shared dict that another coroutine can also mutate for that id (it is already removed). *Worst case:* the background kill thread outlives the tool return and logs a late finalize — harmless. *Early warning:* `close_instance` WARNINGs about background-continuation in `backend-<pid>.log` (M3).
2. **Orphaned Chrome if the timeout expires mid-kill.** *Blast radius:* one Chrome process tree may briefly survive. *Who reaps it:* the still-running worker thread (bounded psutil waits) finishes it in almost all cases; anything left is caught by `process_cleanup`'s deferred-profile cleanup and by **M8 `kill-orphans`** / the next fresh-backend orphan reap (the preserved `--user-data-dir` + `create_time` matcher). *Early warning:* orphan count > 0 from `doctor`/`kill-orphans`; deferred-profile entries accumulating.
3. **F-745 default-flip reaps an actively-used but non-navigating instance.** *Blast radius:* one instance in a long click/type-only flow past 600 s. *Mitigation:* navigation touches; `BROWSER_IDLE_TIMEOUT` is tunable/`0`-disable-able; Option B (Q2) fully removes the risk. *Worst case:* an agent mid-form loses its instance after 10 min idle-of-navigation → respawn. *Early warning:* idle-reaper "Closed N idle instance(s)" logs coinciding with active sessions.
4. **Worker-thread exhaustion under many simultaneous closes.** *Blast radius:* the default thread pool (anyio/asyncio default max 40). *Mitigation:* closes are rare and short; one thread per in-flight close; N concurrent closes ≪ 40 in any realistic local use. *Early warning:* `to_thread` scheduling latency in logs (none expected locally).
5. **`browser.stop()` returns a coroutine in a future nodriver (async-stop edge).** *Mitigation:* `_blocking_teardown` detects the awaitable and defers it to the loop under a small `wait_for` (§2.1-C); pinned nodriver is synchronous (Q1). *Early warning:* a "stop returned coroutine" log line on the offload path.
6. **F-164 `terminate_execution` gives false confidence.** *Mitigation:* it is best-effort by construction (code comment + honest message state it does not interrupt a synchronous JS loop); the wait is still bounded and the remediation text still points at close+respawn. *Early warning:* repeated python-exec timeouts on the same tab despite the terminate attempt → the message tells the operator to respawn.

**Overall worst case:** a persistently wedged Chrome close returns bounded while its kill finishes in a background thread (orphan reaped downstream), or a non-navigating idle session is reaped and respawned — **both strict improvements over today's whole-backend freeze**, and both visible in M3's logs. Every path is fail-safe: the instance is always de-registered (Phase 1), so the manager's view never leaks a half-closed instance.

---

## 8. Findings closed

- **F-180 (High — `close_instance` sync-kill under a fictional `wait_for(5.0)` freezes the shared backend).** **Closed.** The synchronous kill (`kill_browser_process`, `browser.stop()`, `finalize_browser_process`/`cleanup_deferred_profiles`, the `terminate/kill` loop) moves into `_blocking_teardown` run via `asyncio.to_thread` under `wait_for(CLOSE_KILL_TIMEOUT)` with the `_instances` lock released across it (§2.1-A–E) — so the advertised bound is now **real** and one wedged close can no longer stall any other session's dispatch. The "makes it worse" `TimeoutError` fallback (`:768-770`) is deleted: on expiry the already-de-registered instance's orphan is handed to `process_cleanup`/M8, never re-killed inline. Honest about the bound: this makes the ~5 s × N freeze **non-blocking** (background thread), it does not claim to kill Chrome faster.
- **F-164 (Medium, downgraded — CDP timeout wrappers cancel the Python wait but never issue `Runtime.terminateExecution`, leaving the tab wedged).** **Partially closed in M7; remainder re-homed to M4.** M7 fixes the site that owns a `tab` — `cdp_function_executor.execute_python_in_browser` — with a best-effort `terminate_execution` + an honest message that stops implying the tab is recovered (§2.1-G). The `server.py:_with_cdp_timeout` half is **ruled OUT of M7**: it holds no `tab` (would need to reach back into `browser_manager` mid-timeout), lives in M4's `server.py`, and belongs with M4's F-603 timeout-preamble convention work; its error text already advises close+respawn. Honest caveat recorded in code: `terminate_execution` is not a reliable interrupt for a synchronous JS loop. **Re-home flagged to M4** in the WHEN-DONE message.
- **F-745 (Medium, clarity — `get_tab`/`get_browser` `touch_activity=True` default silently refreshes the idle timer on every read).** **Closed (Option A).** Both defaults flip to `False` and the docstrings state the write, making the accessor names honest with **no second accessor and no `server.py` churn** (§2.1-F). Verified that every current toucher is a genuine tool call (no silent background caller), so this is a clarity fix whose behavior delta (reads stop keeping instances alive; navigation still does) **is exactly the brief's stated intent**. The optional per-interaction precision (Option B) is offered as Q2 and re-homed to M4 if elected.
- **F-608 (Medium — non-recovery `fallback_pid` killed with no identity check).** **Closed.** The non-recovery branch (`:411-413`) gains the same `create_time` **identity** check the recovery branch already uses, factored into one shared `_fallback_pid_identity_ok` idiom (dedup lens — one canonical identity check), extending the well-engineered matcher the REPORT says to preserve. The recovery-only `_init_time` predate clause is **not** added to the non-recovery branch (that branch legitimately kills current-session processes). No existing test changes (the current non-recovery test never reaches the fallback path); new tests cover match/mismatch/None.
- **F-611 (Medium, `verified:false` — `list_instances` trusts process-liveness, no CDP round-trip). Ruled OUT of M7.** **Justification:** (1) it is **unverified** — the audit never empirically confirmed the "alive process, dead CDP" harm — so shipping an unproven behavior change into the crown-jewel `list_instances`/tool path during a serial fix wave is the wrong risk; (2) a CDP liveness probe (`Runtime.evaluate`/`Target.getTargetInfo`) in `list_instances` needs the **same** bounded/off-loop discipline M7 is only now establishing for teardown — doing it before that pattern is proven and before M6 covers `list_instances` risks importing the very hang M7 fixes into a read path; (3) it is a **feature-add** (new health semantics), not a freeze-fix — it shares M7's spirit ("bookkeeping ≠ liveness", the same principle M1 applied to the backend) but not its surface or risk profile. **Disposition:** re-home to **M6** (characterize `list_instances`' liveness semantics first) and **file a standalone follow-up finding** for a bounded per-instance CDP liveness probe, to be built on M7's `wait_for`+offload pattern once M6 coverage exists. This matches M1's own recommendation (`plan_M1.md` §8) verbatim in spirit.

---

## The four lenses applied to this design (where each shaped a choice)

- **Modularity →** the blocking kill is isolated in one self-contained `_blocking_teardown(instance_id, browser)` that takes only local refs and shares no state with the loop — understandable in isolation, testable without a real Chrome. The fallback-identity check is one small pure predicate. *(Shaped:* handing the worker thread locals, not `self._instances`, so the phase boundary is a clean seam.)*
- **Deduplication →** `_fallback_pid_identity_ok` gives the create_time identity check **one canonical home** used by both `_kill_processes_for_metadata` branches (F-608); the timeout-expiry orphan is handed to the **existing** `process_cleanup` matcher rather than a second inline kill (deleting the `:768-770` duplicate kill path); no second accessor is introduced for F-745. *(Shaped:* rejecting alt-4/alt-6/alt-7, each of which would have created a second "who kills / who reads" owner.)*
- **Clarity →** `CLOSE_KILL_TIMEOUT` names the "5 s" so a light model sees the bound as a symbol; `get_tab`/`get_browser` stop lying (name = pure read); the F-164 message stops implying recovery it cannot deliver. *(Shaped:* choosing the default-flip over documentation-only, and over a `get_tab_and_touch` split that would have doubled the accessor surface.)*
- **Conventions →** the offload uses `asyncio.to_thread`, the same idiom `server.py` already uses for off-loop work (one way per thing); the F-745 fix keeps a single accessor with a single boolean; the interactive-touch policy, if elected (Option B), is routed to its one natural home (M4 dispatch) rather than sprayed as a second per-tool convention. A fix that would introduce a second way (rename-split, second kill path, per-section dispatch touch) was rejected on exactly this ground.

---

## Appendix — open questions (RESOLVED by human, 2026-07-03)

- **Q1 (Stage-3 confirmable, not a gate decision):** confirm the pinned `nodriver` `Browser.stop()` is synchronous during implementation; the §2.1-C design handles the coroutine-return edge either way, so Stage 3 may drop the edge branch iff provably unreachable.
- **Q2:** ✅ **Option A** — flip `get_tab`/`get_browser` `touch_activity` default to `False` and ship now; only navigation keeps an instance alive; tunable via `BROWSER_IDLE_TIMEOUT`. Per-interaction precision (Option B) is deferred to M4 if real usage shows it is needed.
- **Q3:** ✅ **Split** — M7 fixes the `cdp_function_executor.execute_python_in_browser` (tab-in-hand) half now with best-effort `terminate_execution` + honest message; the `server.py:_with_cdp_timeout` half is re-homed to M4.
