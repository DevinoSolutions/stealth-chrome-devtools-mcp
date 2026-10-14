# F-969 — `spawn_browser` awaits a running Chrome with no deadline: a stall after launch hangs the call and leaves the Chrome running

**Severity:** Medium (rare, but at least twice in five days on the macOS integration cell (2.1.28 and 2.1.33 release gates); the cost per occurrence is an unbounded hang
of one tool call plus a live orphan Chrome). Seen 2026-10-14 in the v2.1.33 publish gate (run 38554874913, attempt
1, macOS/ARM64).
**Files:** `embedded/spawn_budget.py` (new), `embedded/browser_manager.py` (`_launch_browser`,
`_apply_post_launch`, `_setup_dynamic_hooks`, three decorators), `embedded/platform_utils.py` (one docstring
sentence), `tools/check_file_budgets.py` (`browser_manager.py` 1442 -> 1441), `NAVMAP.md`, `CHANGELOG.md`.
Tests: `tests/test_spawn_budget.py` (new, 11), `tests/e2e_helpers.py` (`bounded_spawn`,
`pending_task_report`), `tests/test_resilience.py` (the two spawns go through it), `tests/fakes.py`
(`FakeChrome` and `ProcessTable` moved here from `tests/test_spawn_leak.py`, verbatim).
**Branch:** `fix/f969-bound-spawn-launch`, from `origin/main` (3fd38b7, release 2.1.33).

---

## 1. What happened

`tests/test_resilience.py::test_tab_closed_under_a_running_tool_has_one_terminal_outcome` errored AT SETUP with
"Failed: Timeout (>300.0s) from pytest-timeout" on the macOS/ARM64 integration cell of the publish gate. Setup is
the `instance` fixture: one `spawn_browser(headless=True, **sandbox_kwargs())`. The other 276 selected nodes passed,
and Linux and Windows passed the same node. The node had passed in the 82 macOS integration job logs the census could read (latest attempt of each run, back to
2026-10-07), including four runs on F-967's code; §2 records that this undercounts, since a failed first attempt
with a green rerun is invisible to it.

The pytest-timeout thread dump (`publish-2133-macos-integration.log` lines 574-635) is the evidence:

| What the dump shows | What it rules out |
|---|---|
| The event loop idle in `selectors.select` | A loop held by blocking work (F-967's defect) |
| Both default-executor threads (`asyncio_0`, `asyncio_1`) parked in `work_queue.get(block=True)` | H1: executor saturation. H2: a stuck offloaded worker holding a `profile_offload` slot. No worker was running anything |
| One `asyncio-waitpid-213` thread alive | A child process was still running: the spawn had launched a Chrome |
| A captured `server._client_session_seed: No active context found.` warning | The spawn had reached `_clone_profile_dir_for_session`, i.e. got through F-967's first offloaded step; two executor threads is what the clone path produces (the background sweep overlapping `_prepare_clone`) |

So the spawn was awaiting something that is neither a thread nor the loop: a reply from a running Chrome.

## 2. Cause

`BrowserManager.spawn_browser` awaited three things with nothing around them:

1. `_launch_browser` -> `uc.start(config)` -> nodriver's `Browser.start`. Its `/json/version` wait is bounded
   (nodriver's five tries; our `browser_connect` ceiling is 30 s), but what follows is not:
   `connection.send(Target.setDiscoverTargets)` and `update_targets()` -> `Target.getTargets`. nodriver gives a
   `Transaction` no deadline.
2. `_apply_post_launch`: `reconcile_launched_browser_version` is bounded (10 s, F-806) and says so in its docstring,
   adding "the unbounded awaits that FOLLOW this one are untouched here: pre-existing and belong to their own
   finding". They did not have one. `window_sizing.apply_and_measure` begins with `await tab.set_window_size(...)`,
   explicitly "not guarded"; `_apply_tab_overrides` sends the header and timezone overrides; `ensure_session_restore`
   and `google_rotation_guard.arm` talk to the browser too.
3. `_setup_dynamic_hooks`: `dynamic_hook_system.setup_interception` (CDP sends) inside an `except Exception` that
   logs and returns False, so a stall there is invisible as well as unbounded.

A Chrome that stalls after launching therefore hangs the call for as long as the caller waits. The failure handler
(`except Exception` / `except CancelledError`) that would stop the `Browser` and reap the launched Chrome only runs
when the await ENDS, so the Chrome stays running beside the hung call.

**Attribution, stated plainly.** The exact await the CI spawn was parked on is not known: the thread dump cannot see
a coroutine and nothing logged it (§4 fixes that for the next occurrence). That it is one of these three is the
inference from §1; none of them is F-967's code, and F-967 is not implicated by anything in the dump.

**The hang predates F-967.** The release log records the same node failing the same way on 2026-10-10: attempt 1 of
the 2.1.28 release gate (release PR #209, merged at 24b4ec3) failed on macOS in
`test_resilience::test_tab_closed_under_a_running_tool...` with a 300 s setup timeout in an unnamed in-process spawn,
and its full rerun was green (source: the release log, taken from the release owner; this census could not see it,
because the jobs API serves only the latest attempt of a run, so a failed attempt 1 with a green rerun reads as a
pass). Two occurrences in the macOS integration cell in five days, one before and one after F-967, with the same
signature, settle H3: the earlier "1 in 5 F-967-era runs" figure is a small-sample coincidence and not evidence
against F-967, and the true rate is higher than the 1-in-83 the latest-attempt census shows.

## 3. The budgets, and the evidence for their size

A budget that fires on a slow but healthy spawn turns a working browser into a failure, which is worse than the
hang. Both are ceilings, not waits.

**Census.** `ms_to_json_version` is the F-870 cold-start probe every gate cell prints before the suite: one cold
launch (`launch: 1`) and one warm (`launch: 2`) with the runner otherwise idle. Read from the latest-attempt
integration job logs of the last ~45 CI/publish runs (2026-10-05 to 2026-10-14):

| Cell | Jobs | Cold launches | min | median | p99 / max (cold) | Warm launches |
|---|---|---|---|---|---|---|
| macOS/ARM64 | 92 | 89 | 3.2 s | 5.7 s | 9.9 s | median 1.3 s, max 2.6 s |
| Windows/X64 | 45 | 44 | 3.8 s | 5.5 s | 20.2 s | median 0.34 s, max 1.1 s |
| Linux/X64 | 45 | 45 | 0.5 s | 5.4 s | 23.9 s | median 0.22 s, max 0.28 s |

178 cold launches, slowest 23.9 s (one Linux outlier). With the cell's other load, `browser_connect` records a
6-way fleet spawn (1 lead + 5 in 3 lanes on 3 cpus) at 26.0 s and the CI log at 14.1 s. These are times to the
DevTools endpoint answering, which is most of `Browser.start`; the target-discovery round trips after it are local
and, for a Chrome that has just answered `/json/version`, measured elsewhere at under a millisecond (0.78 ms median,
`browser_connect`).

| Budget | Value | Margin |
|---|---|---|
| `LAUNCH_BUDGET_SECONDS` | 120 s | 5.0x the slowest cold launch (23.9 s), 4.6x the worst fleet spawn (26.0 s), 12x the slowest macOS one |
| `POST_LAUNCH_BUDGET_SECONDS` (per phase) | 60 s | 2x `CDP_OPERATION_TIMEOUT` (30 s, the bound every other CDP tool lives under) for a phase of at most six local exchanges, each of which is milliseconds |

**What is not measured**, said rather than implied: no CI log times `_apply_post_launch` or the hook setup, so the
60 s is sized from the existing 30 s CDP budget and the local-round-trip timing, not from a post-launch census. And
the cold OneDrive-venv spawn the brief asked for was not run: local real-Chrome runs were excluded (RAM at 1-2 GB).
The cold-venv cost is Python import time before `spawn_browser` is entered and does not occur inside any of the three
bounded phases.

**What a stall now costs.** One stalled attempt costs its budget (120 s for the launch), then `_SPAWN_ATTEMPTS`
retries onto a fresh clone, so the worst case is bounded at 3 attempts rather than unbounded. A stall inside a
phase that a retry then clears turns a 300 s kill into ~125 s and a pass.

## 4. Fix

### 4.1 Product (`embedded/spawn_budget.py`)

`phase(stage, budget_name)` is a decorator applied to `_launch_browser`, `_apply_post_launch` and
`_setup_dynamic_hooks`. It runs the phase under `asyncio.timeout(budget)`; an expiry of THAT deadline (not a
`TimeoutError` the phase raised itself, checked with `scope.expired()`) becomes a `ToolError` naming the stage and
the budget. The budget is looked up by constant name when the phase runs, so a test patches one module attribute.

It is a decorator rather than a wrapper at the call site so the bound travels with the method: `spawn_browser` and
the heal's relaunch (F-963, `spawn_browser(options, instance_id=)`) reach the same three. `browser_manager.py` is
capped at 1442 lines and the call-site form cost about +13; the decorators cost +4, paid by collapsing
`_launch_browser`'s docstring (1441).

A whole phase rather than each `send`: every exchange inside it is bounded by the phase, which is no looser than
bounding each (a phase whose six healthy sends sum past 60 s is not a healthy phase), at one line per phase instead
of one per send. Cancelling a send is safe: `cdp_transport` exists so that cancelling a `Transaction` cannot kill the
connection's listener (F-883 B1).

**No second teardown.** The `ToolError` is an ordinary `Exception` out of the phase, so it lands in
`spawn_browser`'s own `except Exception`: `_teardown_failed_spawn` stops a held `Browser`, or, with no handle,
`spawn_leak.reap_launched_browsers` kills the pid `Attempt` identifies (the registered `Browser` for the stamped
`uc.Config`, so a timed-out `uc.start` is still findable); then `kill_browser_process(instance_id)` ends a tracked
browser and removes its disposable clone. The tool body's `except Exception` then runs `_SPAWN_ATTEMPTS` as for any
other failure and its final message names the stage. Nothing is added to any of those paths.

What a stalled LAUNCH does not clean: the clone directory of a launch that never became tracked stays on disk
until the storage sweep, exactly as for any other failed launch today (`Failed to connect to browser` included): its
sweep PROTECTION is released by the tool body, which is what lets the sweep take it. Not changed here.

### 4.2 Test tier (`tests/e2e_helpers.py`)

`bounded_spawn(spawn, **kwargs)` runs a spawn under `SPAWN_BOUND` = 240 s: above one stalled attempt plus a retry that
lands (120 s + a spawn), below the 300 s the release gate kills a node at. On overrun it prints
`pending_task_report()` (every other pending task and its whole coroutine await chain, down to the future it is parked
on), cancels the spawn (the product reaps what it launched) and raises `TimeoutError` with the report. The
`instance` fixture and `_assert_fresh_spawn_works` in `tests/test_resilience.py` use it, so a recurrence names its
await instead of producing a thread dump that cannot.

## 5. Pins and how RED/GREEN were measured

`tests/test_spawn_budget.py` (11 nodes). The doubles NEVER answer rather than raise (a raising double is the failure
the product already handled); every await on a stalled path is under `asyncio.wait_for` with a named budget
(`WAIT_BUDGET`, 30 s), so a regression FAILS and cannot hang the suite. Real `BrowserManager.spawn_browser`,
`_launch_browser`, `_apply_post_launch`, `spawn_leak`, `process_cleanup` (kill, record, clone removal) and, for the
retry node, the real tool body, `clone_storage` and a real profile copy; faked: `nodriver.start`, the tab, and
`psutil`'s table (`fakes.ProcessTable`).

| Node | Pins |
|---|---|
| `test_a_phase_that_never_returns_names_its_stage_and_budget` | the `ToolError` names stage and budget |
| `test_a_phase_that_answers_is_untouched_...` | a result passes through; a phase's own `TimeoutError` is not rewritten |
| `test_the_three_spawn_phases_carry_the_bound` | each phase keeps `__wrapped__` (the decorator cannot be dropped silently) |
| `test_a_launch_that_never_answers_...` | launch stall: named stage, the launched Chrome reaped by pid, no record, no in-flight spawn |
| `test_a_post_launch_exchange_that_never_answers_...` | window-size stall: stage named, held `Browser` stopped once, process dead, record gone, clone dir removed |
| `test_a_dynamic_hook_setup_that_never_answers_...` | the hook setup's stall (inside an `except Exception` that would have hidden it) |
| `test_a_healthy_spawn_is_not_cut_off` | the control: a prompt spawn succeeds under the same budgets |
| `test_a_timed_out_attempt_is_retried_and_leaves_nothing_behind` | through the tool body: attempt 1 stalls, 2 and 3 are fresh distinct clones; all three Chromes killed, no record, no protected clone left |
| `test_bounded_spawn_*` (3) | the fixture wrapper names the await; passes a prompt result; its bound is between the product's and pytest-timeout |

**RED**, run out of tree and labelled as that: `git archive 3fd38b7` exported to `%TEMP%\f969-red`, the new/changed
test files copied over it, and one stub `embedded/spawn_budget.py` (the two constants and a `phase` that returns the
function unchanged) so the tests import and exercise the UNBOUNDED 3fd38b7 behaviour; `PYTHONPATH` pointed at the
export's `src` and `__file__` of `browser_manager` and `spawn_budget` printed and confirmed to be the export's;
`__pycache__` cleared; no stash and no shared worktree touched. Result in §6.

**GREEN**: the same file on the fix branch, `uv run python -m pytest tests/test_spawn_budget.py`.

## 6. Results

* **RED** (3fd38b7 + stub `spawn_budget`, out of tree, `__file__` confirmed): **6 failed, 5 passed** in 155.8 s.
  The six are the three stall nodes, the retry node, the decorator node and `test_a_phase_that_never_returns_...`:
  each stalled spawn hung until `WAIT_BUDGET` (30 s) cancelled it, which is the defect (an unbounded await). The five
  that pass are the controls that must pass either way: the healthy spawn, the pass-through, and the three
  `bounded_spawn` nodes (that wrapper is test-side and does not depend on the product fix).
* **GREEN** (fix branch): `tests/test_spawn_budget.py` **11 passed in ~4 s**; the whole `-m "not integration"` tier
  **4357 passed, 1 skipped** (574.6 s, Windows, Python 3.13).
* `ruff format --check`, `ruff check`, `vulture`, `check_suppression_owners`, `check_file_budgets`,
  `check_pinned_imports` clean. `ty` reports the same pre-existing diagnostics and none in `spawn_budget.py`.
* Not run locally, by instruction: any real-Chrome test. The macOS/Windows/Linux integration cells are the CI
  verification.

## 7. Follow-ups, not done

* The per-send granularity (a budget per exchange) if a post-launch census ever shows the phase budget is too coarse.
* A post-launch timing log line (`phase` duration) so the next census can size `POST_LAUNCH_BUDGET_SECONDS` from
  measurement instead of from the CDP budget.
* Bound `browser_reattach`'s adoption connect (`cdp_attach`) the same way if it ever shows this shape; it is outside the
  three spawn phases (a delegated headed launch, `desktop_launch.launch_and_attach`, IS inside `_launch_browser`) and
  was not implicated.
