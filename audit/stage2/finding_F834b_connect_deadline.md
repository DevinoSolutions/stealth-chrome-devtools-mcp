# F-834 stage 2 — a Chrome that is still opening its DevTools endpoint is killed as a failed spawn, because nodriver stops asking after 2.75 s

**Status:** FIXED on this branch.
**Closes:** the fix F-870 §6 wrote down and declined to ship, now that F-870 §7's
experiment has produced the number §6.2(1) said it was missing.
**Prior art, cited and not restated:** F-870 (the mechanism, to within 18 ms, and
the probe), F-860 (the reap that kills the Chrome), F-834 stage 1 (the retry that
hides it), F-811 / F-859.

---

## 1. What was observed

`tests/test_e2e_fleet.py::test_a_fleet_of_six_browsers_answers_truthfully_about_every_page`
fails on `release-gate / integration (macOS/ARM64)` with one warning on the
backend's durable channel and nothing else wrong:

```
tests/test_e2e_fleet.py:722: in test_a_fleet_of_six_browsers_answers_truthfully_about_every_page
    assert others == [], [record.getMessage() for record in others]
E   AssertionError: ['spawn_leak.reap: Failed spawn 92ed3a92-ae73-45a5-81b6-83f5b08f4e4e
    left browser pid 11697 running on
    /Users/runner/work/_temp/stealth-mcp-session-root/sessions/fleet-tabswitch;
    killing it (F-860)']
```

Four of the last five macOS gate runs: run **35454765486** attempts 1 and 2
(PR #134), run **35316298288** attempt 3 (PR #133), publish run **35304880367**
attempt 1. Each full rerun costs ~45 min.

The fleet itself succeeds. Its own stdout line, same run:

```
fleet of 6: spawn 26.0s (1 lead + 5 in 3 lanes on 3 cpus), navigate 2.5s,
actions 1.7s, total 30.4s (roles ['clone', 'explicit', 'master'])
```

Six browsers spawned, six pages answered truthfully, everything reclaimed. One
member's FIRST attempt failed, F-834 stage 1's retry took the same directory and
succeeded, and the only surviving trace is the warning the reap logged.

The oracle is not being over-strict. `test_e2e_fleet.py:686-698` states the
standard deliberately: while the fleet is being DRIVEN, **nothing** on the
backend's durable channel is acceptable, because that is what F-874's
truthfulness guarantee rests on. A killed browser is exactly the class of event
it exists to catch.

## 2. The defect

**The product treats "Chrome has not opened its DevTools endpoint yet" as
"Chrome failed to start", because the deadline that decides is a loop count in
nodriver's source and the product never gets asked.**

`nodriver/core/browser.py:411-435`, verbatim:

```python
self._http = HTTPApi((self.config.host, self.config.port))
util.get_registered_instances().add(self)
await asyncio.sleep(0.25)
for _ in range(5):
    try:
        self.info = ContraDict(await self._http.get("version"), silent=True)
    except (Exception,):
        if _ == 4:
            logger.debug("could not start", exc_info=True)
        await self.sleep(0.5)
    else:
        break

if not self.info:
    raise Exception("... Failed to connect to browser ...")
```

`0.25 s + 4 × 0.5 s` is **2.75 s of waiting**, and there is no `Config` field
behind it in 0.47 (F-870 §6.1(a), re-verified against the installed package
here). The wall-clock window is that plus what five refusals cost, which is the
platform's, and the review's virtual clock hid the difference — so it is
measured here:

| shape | one refused `/json/version` | nodriver's whole window |
|---|---|---|
| POSIX loopback | near-instant (F-870 §1.3: "near-instant refusals, not timeouts") | ≈2.75 s |
| Windows loopback | **2023–2060 ms** (measured locally, six fetches, a never-bound port and nodriver's own reserved-then-closed idiom alike) | ≈12.9 s |
| a port that LISTENS and never answers | **10 019 ms** — `urlopen(…, timeout=10)`, nodriver's own | ≈52.5 s |

Two consequences worth stating plainly. The macOS cell this finding is about is
the first row, so 2.75 s is the right number there. The Windows cell's 4250 ms
cold start does **not** fail, and the second row is why — not luck, and not
anything the product does.

What follows from the raise is all correct and all wasted: `_launch_browser`
never returns a `Browser`, so `_teardown_failed_spawn` takes its no-handle
branch, `spawn_leak.reap_launched_browsers` kills the Chrome that was coming up
(F-860 — correct: with no handle, the profile is the only thing that identifies
it) and logs the WARNING, and `_SPAWN_ATTEMPTS` launches a second Chrome from
cold onto the directory the reap just freed (F-834 stage 1).

## 3. Measurement

### 3.1 Chrome routinely misses the window — this is what F-870 §7 was for

The F-870 probe (`tools/chrome_cold_start_probe.py`) runs on every gate cell
**before** the suite, product-free, with the runner otherwise idle and launching
exactly ONE Chrome. `ms_to_json_version` is time from `Popen` to a served
`/json/version` — the same fact nodriver's loop is waiting for:

| cell | gate run | launch 1 (cold) | launch 2 (warm) |
|---|---|---|---|
| macOS/ARM64 | 35304880367 | **3943.6 ms** | 581.6 ms |
| macOS/ARM64 | 35316298288 | **4786.0 ms** | 1289.7 ms |
| macOS/ARM64 | 35454765486 | **5839.1 ms** | **2138.7 ms** |
| Windows/X64 | 35316298288 | **4250.0 ms** | 344.0 ms |
| Linux/X64 | 35316298288 | 685.8 ms | 232.6 ms |

The probe's own summary line records the window it is being compared against:
`"nodriver_connect_window_ms": 2750`.

Read straight off that table:

* **Two of the three cells have never once answered inside 2.75 s on a cold
  binary.** macOS overshoots by 1.4×–2.1×; Windows by 1.5×.
* **The warm path has no real margin on macOS either.** 2138.7 ms is 78 % of the
  window for a single launch on an idle machine.
* Linux fits comfortably, which is why this reads as a macOS defect and is not
  one.

A local run on the development box (Windows/X64, `--cell local-Windows-X64`,
2026-09-19) measured 358.0 ms and 335.5 ms — a machine with Chrome already
resident. It is reported because it is the counter-example: the window is only
missed where the image is slow or loaded, which is every hosted runner and no
developer laptop, and is why this has only ever failed in CI.

### 3.2 Why the fleet is the shape that fails

The probe measures ONE launch on an IDLE machine. The fleet launches six in
three lanes on three cpus, and its own instrumentation says the spawn phase
takes 26.0 s. A follower's launch is competing for the cpus that page Chrome in,
so the warm 2138.7 ms best case is the floor, not the estimate. No probe
measures the loaded case; the 30 s budget below is sized for it by margin, not
by a reading, and that is stated rather than hidden.

### 3.3 The retry proves the browser was fine

The spawn that failed is the spawn that then succeeded, on the same directory,
within the same test. Nothing about the environment changed between the two —
only that the second Chrome launched onto a warmer machine. A Chrome that
"failed to start" and then started is a Chrome that had not finished starting.

## 4. The fix

`embedded/browser_connect.py`, a new leaf: `install()`, `installed()`,
`CONNECT_PATIENCE_SECONDS` and nothing else. It wraps `HTTPApi.get` so that a
`version` fetch waits out an endpoint that is not open yet, bounded by **our**
budget, and `browser_manager._launch_browser` calls `install()` once, ahead of
the F-810 delegation branch, so every launch in the tree is covered.

**`HTTPApi.get` is the only seam there is, and that is a fact about the
lifecycle, not a preference.** The decision being changed is made strictly
between "Chrome is spawned" and "the endpoint answers". Before that instant
there is no process to wait for — nodriver creates it — and after it
`Browser.start` has already raised. `HTTPApi.get` is the one call inside that
interval. It also has exactly **one** caller in nodriver 0.47
(`browser_source.count('_http.get("') == 1`, pinned), so the wrapper reaches
that decision and nothing else; it is nonetheless keyed on the endpoint name so
a future nodriver that grows a second caller does not inherit the patience
silently.

`CONNECT_PATIENCE_SECONDS = 30.0`: ~5× the worst measured cold launch (5839.1 ms)
and ~14× the worst warm one (2138.7 ms), with the margin carrying §3.2's
unmeasured load. It is a **ceiling, not a wait** — an endpoint that opens in
300 ms costs 300 ms, and the common path adds nothing at all. The patience is
charged ONCE per launch (the deadline is stamped on the `HTTPApi` instance,
which nodriver builds fresh in each `start`), so nodriver's four remaining
attempts see it spent, fail immediately, and add only their own 2 s of sleeps.

A launcher that has already EXITED short-circuits the wait, and so does one that
exits *mid-wait* — `returncode` is re-read every pass. So the one case this
could have slowed down, Chrome dying at launch, is instead **faster** than
2.1.9, which polls a port nobody will open for its whole window. (Review
measured the mid-wait shape at 3.6 s; it is pinned here now, not only measured.)

### 4.0.1 The ceiling is for LAUNCHES WE OWN (review M1)

`_launched_process` resolves the owning browser **once** per `HTTPApi` and reads
nodriver's own `_process`. `None` means nobody here launched it — the
`connect_existing` branch, which since F-888 merged has ONE home, `cdp_attach`,
with two consumers through it (`desktop_launch`'s delegated launch and
`browser_reattach`'s adoption of a browser a dead backend left running) — and
such a call takes nodriver's window unchanged. The pin is driven through
`cdp_attach.config_for` + `cdp_attach.attach` rather than a hand-built `Config`,
so it follows that home if it moves; `browser_reattach`'s door is the same call.

Decided from measurement, not caution. An attach targets an endpoint that is
**already open**, and a `/json/version` fetch against a live one costs **0.78 ms
median** (measured locally, ten fetches; min 0.51 ms, max 170.8 ms on the
first). nodriver's five attempts are already ~1000× the healthy cost there, so
patience buys nothing — while a stale recorded port would have cost 32.5 s
inside a user-facing `spawn_browser` call, where 2.1.9 cost 2.75 s. **There is
no second constant for the attach door**: the cheapest honest answer was no
change at all, and a number nothing measured would be the guess this repo
refuses. Both shapes are pinned
(`test_an_attach_does_not_get_the_launch_ceiling`, and the launch nodes beside
it); mutating the witness so an attach looks like a launch fails that pin with
305 attempts against 5.

An HTTP **error** answer is also not a closed socket: `urllib.error.HTTPError`
is excluded from the retry set, so a squatter answering 500 fails in nodriver's
window rather than ours (review L1, which measured the old behaviour at 32.5 s).

### 4.1 How this clears F-870 §6.2's four blockers

| F-870 §6.2 | here |
|---|---|
| (1) "the wait cannot be sized from evidence" | §7's experiment has run: §3.1 is five cell-runs of the number it was built to produce. |
| (2) "the attach path has a real correctness hazard" — a `connect_existing` `Browser` has no `_process`, so the instance goes untracked and the profile lifecycle lies | **avoided entirely**: there is no attach. The `Browser` is the one nodriver launched, with its own `_process` and our own `Config`. |
| (3) "nowhere to put it without a new leaf" | the leaf exists, and `browser_manager.py`'s cap **ratchets down** 1493 → 1490 (paid for by `_replace_main_tab`'s signature-echo `Args:`/`Returns:` block, the plan_F856 mechanism). |
| (4) "a second way is a defect — `desktop_launch.launch_and_attach` owns attach" | no second attach path is created. The delegated launch goes through the same `Browser.start`, so it gets the same patience from the same line. |

And against F-870's "what a fix must NOT be": not a `STEALTH_MCP_*` knob; not a
retry loop inside `spawn_browser`; not a bigger `SPAWN_TIMEOUT`.

### 4.2 Alternatives rejected, each read against the source first

* **Re-call `Browser.start`.** It refuses re-entry while `_process` is live
  (`browser.py:349-353`, "ignored! this call has no effect when already
  running"); clearing the guard makes it spawn a second Chrome.
* **Recover with `Browser.create(host=…, port=…)` after the raise.** Works —
  nodriver mutates our `Config` with the host and port it chose, so both are in
  hand — and is exactly F-870 §6.1(b). It is rejected on §6.2(2): a `Browser`
  that cannot kill its own Chrome.
* **Set `config.host`/`config.port` so nodriver attaches.** Then the launch is
  ours: a second home for spawning Chrome (convention 4).
* **Copy `Browser.start` and widen the loop.** A double of a library function in
  the one path every browser takes.

## 5. Tests

`tests/test_browser_connect.py`, eleven nodes. Every one drives nodriver's
**real** `Browser.start` — its own 0.25 s lead, its own `range(5)`, its own 0.5 s
between attempts and its own "Failed to connect to browser" — over a fake
endpoint, because a hand-written double of that loop would be a copy of the very
constants that are the defect and would keep passing the day nodriver changed
them. Only the OS is faked: subprocess, endpoint, websocket `Connection`, clock.
No Chrome is launched. The 2.5 s of `Browser.sleep` and the whole 30 s patience
are virtual; nodriver's initial `await asyncio.sleep(0.25)` is **not** — it is
reached before any seam of ours exists and costs each node a real quarter second
(review L4; the module docstring said "2.75 s virtual" and was 0.25 s off).

| node | claim |
|---|---|
| `…_answers_after_nodrivers_window_is_connected_to` | an endpoint opening at 4.0 s (past 2.75 s, inside 30 s) yields a live `Browser`, answered on nodriver's FIRST attempt |
| `…_given_up_on_without_the_patience` | the sensitivity control: identical scenario through the unwrapped `HTTPApi.get` raises "Failed to connect", after 5 attempts and 2.5 s — the shape 2.1.9 ships |
| `…_never_opens_costs_the_patience_and_no_more` | the ceiling is real and bounded at patience + nodriver's own remaining sleeps |
| `…_launcher_that_already_exited_is_not_waited_for` | process death before the wait ends it; none of the patience is spent |
| `…_launcher_that_exits_mid_wait_ends_the_wait_there` | the shape production sees — Chrome starts, then dies at t=1.0 s — ends there, not at the ceiling (review L2) |
| `…_an_attach_does_not_get_the_launch_ceiling` | `connect_existing` takes nodriver's own window: 5 attempts, 2.5 s (review M1) |
| `…_a_server_that_answered_is_not_an_endpoint_that_is_still_opening` | a 500 fails in nodriver's window, not ours (review L1) |
| `…_only_the_version_endpoint…` | one attempt for any other endpoint |
| `…_install_is_idempotent` | runpy executes `server.py` three times; the launch path calls it per spawn |
| `…_the_launch_path_installs_the_patience_before_it_launches` | the wiring, ahead of the F-810 branch |
| `…_the_window_this_extends_is_still_the_one_in_nodrivers_source` | the premise, pinned against library source: the 0.25, the `range(5)`, the 0.5, the message, and the single `_http.get(` caller |

**Mutation checks**, each with every `__pycache__` under `src/` and `tests/`
deleted first:

* `install()` neutered to a no-op → 3 nodes fail, including the core claim, on
  "Failed to connect to browser".
* `_launched_process` made to answer a live process for everything, i.e. an
  attach that looks like a launch → `…_an_attach_does_not_get_the_launch_ceiling`
  fails with **305 attempts against 5**.

Restored: 11 passed.

Suites re-run green by explicit path: `test_browser_connect` (8),
`test_spawn_leak` / `test_concurrent_spawn_collision` /
`test_spawn_exhaustion_hint` / `test_spawn_headed_requires_display` /
`test_clone_storage` / `test_clone_storage_cap` / `test_profile_lock` (100),
`test_browser_manager_list_tabs` / `test_browser_manager_tab_rediscovery` /
`test_no_silent_excepts` / `test_silent_excepts_log` / `test_cdp_transport` /
`test_error_typing` (57), `tools/check_file_budgets.py`.

## 6. Residuals — what this does NOT fix, and what it costs

1. **The fleet test itself is CI-only evidence here.** It is marked
   `integration` and spawns six real Chromes; it was not run on the development
   box, which has live human-login Chromes on it and is memory-starved. The
   claim that this closes the fleet red rests on the mechanism (§2), the
   measurement (§3) and the pins (§5) — not on a green fleet run. **The gate is
   the proof and it has not run yet.**
2. **30.0 s is sized by margin for the loaded case, not measured for it.** §3.1
   measures one launch on an idle machine. Nothing measures six on three cpus.
   If a cell is ever slower than 30 s to open an endpoint, this fails exactly as
   before, one line later.
3. **A Chrome that starts and then hangs without ever listening costs the
   ceiling per attempt, and `_SPAWN_ATTEMPTS` spends it three times** (review
   M2, whose arithmetic corrects this section's first draft of "up to 90 s"):
   **32.5 s per attempt and 97.6 s for three** on the instant-refusal shape the
   review measured, which is the POSIX one; ≈42 s and ≈126 s on Windows, where a
   refusal itself costs 2.03 s; and for a port that LISTENS and never answers,
   the bound is `urlopen`'s own 10 s per attempt either way — ≈82 s for one and
   ≈246 s for three, against ≈52.5 s and ≈157 s in 2.1.9, which was already the
   worst shape there is. `_launched_process` covers the process that EXITS,
   which is what a broken binary or a fatal flag produces; it cannot see one
   that is alive and stuck. Nothing in the product bounds the three attempts —
   `spawn_browser` carries no `_with_cdp_timeout` and no deadline — so the
   client's transport timeout is the bound, and past it the operator loses the
   joined spawn error and the F-811/F-834 hints with it.

   **Clipping the later attempts was considered and DECLINED.** They are not a
   repeat of the same experiment: F-834 stage 1 re-selects the profile between
   them, so a `clone` gets a fresh directory and a held `master` falls through
   to a reserved clone, and attempt 2 genuinely can succeed where attempt 1
   could not. The smallest form of the clip — a module-level "a launch already
   spent the whole ceiling" flag — is process-global state that outlives the
   spawn, so an unrelated later spawn would inherit this one's verdict. The
   honest bound is a spawn-WIDE deadline, which `spawn_browser` does not have at
   all today and which changes the error the operator sees; it belongs in its
   own change, not smuggled in here.
4. **It patches a library class.** The precedent is `cdp_transport.install()`
   and the discipline is the same (idempotent, one seam, marker on the wrapper,
   original reachable), but it is still a patch, and
   `test_…_still_the_one_in_nodrivers_source` is the tripwire for the day
   nodriver's source moves under it.
5. **This should be deleted on a nodriver bump.** F-870 §6.1(a) and §8 record
   that later nodriver exposes `browser_connection_timeout` /
   `browser_connection_max_tries`. When the bump happens — a decision gated on
   `element_resolution.py`'s 0.47 selector semantics, not on this — the right
   change is to pass `CONNECT_PATIENCE_SECONDS` to those fields and remove this
   module, rather than keep both.
6. **F-870's other open items are untouched**: §6.1(d) (nodriver discards
   Chrome's stderr, so a launch that never listens is still unreadable in a
   post-mortem), the `release-gate-warmup-2` sub-defect, and the macOS headed
   sub-shape of §3.5. This finding closes the connect deadline and nothing else.
7. **The oracle was not weakened.** No warning class was added to the fleet
   test's allowances, and `spawn_leak`'s WARNING is unchanged — it still fires
   for the spawn that genuinely leaves a Chrome behind. The change is that a
   slow start is no longer one of those.
