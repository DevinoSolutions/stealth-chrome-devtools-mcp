# F-870 — nodriver gives a launched Chrome 2.75 s to answer, the caller gives it 120 s: on POSIX CI the gate dies in the 117 s nobody uses

**Severity: MEDIUM (product) / HIGH (gate throughput)** — no user data is lost
and no wrong answer is returned, but a real `spawn_browser` on a GitHub-hosted
Linux or macOS runner intermittently fails with nodriver's `Failed to connect to
browser` **although Chrome launched and is still running**, and every occurrence
costs a full release-gate rerun. The product has never handled it; the *tests*
have worked around it twice, in two places, for over a month
(`tests/e2e_helpers.py:113-140`, `tests/release_gate_harness.py:856-970`), and
it has never been filed.
**Found:** 2026-09-15, from run `34924931290` (a docs-only PR). **Oldest
occurrence: 2026-07-31** — six weeks unfiled.
**Rate: 5 occurrences in 107 completed runs (~4 %); Windows control 0 of 27
failed cells** (§2).
**Status:** NOT FIXED, deliberately. The *mechanism* is measured to 18 ms (§3)
and the cause is narrowed to one leading hypothesis by the "first four launches
fail, next ~170 pass" observation (§3.4). The *cure* is still unsized — nobody
has ever measured how long Chrome's first launch actually takes, because the
product kills it at 2.8 s and nodriver discards its stderr. §6 says why shipping
a number now would be a guess; §7 is the one experiment that produces the
number.

---

## 1. What was observed

### 1.1 The freshest instance

Run `34924931290` — workflow **`CI`** (`.github/workflows/test.yml`), branch
`docs/f867-production-rung` (PR #102), **attempt 1** — job `104240899486` =
`release-gate / install-smoke (wheel Linux/X64)`, step *"Install the exact
artifact and run W1's journey"*. PR #102 changed documentation only, so nothing
in this repo's source is implicated.

Two framing facts about that run, both load-bearing:

* **The gate is a reusable workflow.** `test.yml` (`name: CI`) has one job,
  `release-gate: uses: ./.github/workflows/release-gate.yml`, which is why every
  cell is named `release-gate / <cell>`. `release-gate.yml` is never dispatched
  directly, so `gh run list --workflow release-gate.yml` returns `[]`; the runs
  live under `test.yml` and (for tags) `publish.yml`. Noted because it silently
  breaks the obvious way to measure this family.
* **Attempt 1 failed on this family; attempt 2 had no failing job at all.** That
  is the cost, stated exactly: a docs-only PR bought a full gate rerun, and the
  rerun was clean. The failure is non-deterministic on identical source.

Five real `spawn_browser` calls, five identical failures:

```
fastmcp.exceptions.ToolError: Error calling tool 'spawn_browser': Failed to spawn browser:
                ---------------------
                Failed to connect to browser
                ---------------------
                One of the causes could be when you are running as root.
                In that case you need to pass no_sandbox=True
```

The backend log (`backend-2848.log`, dumped into the job log) gives the timing,
and the timing is the whole finding:

| # | profile | `Platform:` line (launch) | first `spawn_leak.reap` | launch → raise |
|---|---|---|---|---|
| 1 | `sessions/release-gate-warmup` | 03:28:56,201 | 03:28:59,004 | **2.803 s** |
| 2 | `sessions/release-gate-warmup` | 03:29:05,326 | 03:29:08,096 | **2.770 s** |
| 3 | `sessions/release-gate-warmup` | 03:29:17,431 | 03:29:20,200 | **2.769 s** |
| 4 | `sessions/release-gate-warmup-2` | 03:29:32,487 | 03:29:35,255 | **2.768 s** |
| 5 | `master` | 03:29:38,563 | 03:29:41,331 | **2.768 s** |

Five samples, spread of 35 ms, four of them inside 2 ms of each other. That is
not a machine under variable load timing out — it is a **fixed constant being
spent**. §3 shows which constant it is and derives it from nodriver's source to
within 18 ms.

### 1.2 Chrome launched, and was still alive and still initialising

Three independent facts, all from the same job log:

* **F-860's reaper found live pids every time** —
  `spawn_leak.reap: Failed spawn <uuid> left browser pid <N> running on
  <dir>; killing it (F-860)`, five times. `spawn_leak._started_after` reads
  `psutil.Process(pid).create_time()`, so those processes existed at reap time,
  ~2.8 s after launch. Chrome did not fail to start and did not crash by 2.8 s.
* **Chrome's own log shows it still in early init at 4.0 s.** The warmup spawn
  passes `--enable-logging --log-file=<log_dir>/chrome-warmup.log`
  (`tests/release_gate_harness.py:912-915`), and the gate dumped it:

  ```
  [chrome-warmup.log]
  [2916:2940:0915/032934.642416:ERROR:dbus/bus.cc:405] Failed to connect to the bus: ...
  [2916:2940:0915/032936.481037:ERROR:dbus/bus.cc:405] Failed to connect to the bus: ...
  ```

  pid 2916 is attempt #4's Chrome, launched 03:29:32.487. It logged at
  **+2.155 s** and again at **+3.994 s** — the second line is *after* the reap
  line at 03:29:35.255, i.e. Chrome was still running and still doing work when
  we had already given up on it and sent it a signal.
* **`DevTools listening on ws://…` appears in no log — but that is NOT evidence,
  and must not be read as any.** It appears in zero of the ~100 job logs the §2
  census downloaded, matched *and* passing alike, and the reason is structural:
  `nodriver` launches Chrome with `stdout=asyncio.subprocess.PIPE,
  stderr=asyncio.subprocess.PIPE` (`nodriver/core/browser.py:397-405`) and never
  reads or relays either pipe. Chrome's stderr — where that line goes — is
  swallowed by the library on every run. **Chrome's readiness is currently
  unobservable from CI evidence**, which is exactly why §7 has to create the
  observation rather than look harder for it. (The `chrome-warmup.log` above
  exists only because the harness passes `--log-file`, which captures Chrome's
  *logging* stream, not the DevTools banner on stderr.)

### 1.3 The failures were near-instant refusals, not timeouts

nodriver's probe is `urllib.request.urlopen(request, timeout=10)`
(`nodriver/core/browser.py:930-932`). A *timeout* would add up to 10 s per
attempt; a *connection refused* returns in microseconds. The measured
launch→raise of 2.768 s is nodriver's sleep budget **plus ~18 ms of total HTTP
cost for five probes** (§3.1). So for the whole 2.75 s window **nothing at all
was listening on the DevTools port** — the port was not slow to answer, it was
not bound.

### 1.4 nodriver's own advice is a red herring here, provably

The error text blames root and suggests `no_sandbox=True`. On CI the gate
already passes `sandbox: False` — `tests/e2e_helpers.py:79-83` sets
`_needs_no_sandbox` when `CI == "true"`, `sandbox_kwargs()` returns
`{"sandbox": False}`, and `browser_manager._resolve_launch_args` then appends
`--no-sandbox` explicitly
(`src/stealth_chrome_devtools_mcp/embedded/browser_manager.py:515-516`). The
backend log line confirms what actually ran:

```
browser_manager.spawn_browser: Platform: Linux | Root: False | Container: False | Sandbox: False | Browser: Google Chrome (/usr/bin/google-chrome)
```

`Root: False`, `Sandbox: False`. Both causes nodriver names are excluded by
measurement, not by argument. This matters operationally: the error a
maintainer sees points at the one thing that is already configured correctly,
which is why this family has been re-diagnosed from scratch every time it
appears. `spawn_contention.contention_hint` already carries an explicit
disclaimer that this advice does not apply (`embedded/spawn_contention.py`) —
but it is only appended when sibling spawns were in flight, and here they were
not.

### 1.5 What the retries and the profile progression actually are

The task that produced this finding asked whether the four attempts and the
`sessions/…` → `master` progression are a product retry or a fallback. Neither.

* **`BrowserManager.spawn_browser` does not retry.** It calls `_launch_browser`
  exactly once (`browser_manager.py:673`); there is no loop. Every attempt in
  §1.1 is a separate *caller-side* call.
* **Attempts 1–4 are the harness's warmup**, `WARMUP_ATTEMPTS = 4`
  (`tests/release_gate_harness.py:99`) with
  `await asyncio.sleep(WARMUP_BACKOFF_SECONDS * attempt)` between them
  (`:891-899`). The observed gaps — 6.28 s, 9.30 s, 12.23 s — are exactly
  3 s × 2, 3, 4. The record's `"attempts": 4` is that counter.
* **Attempt 5 is the canonical journey's own spawn**
  (`tests/release_gate_harness.py:1101`), which passes no `user_data_dir` and
  therefore lands on the default `master` profile. It is a *different caller*,
  not a fallback from a session profile.
* **`release-gate-warmup-2` is profile poisoning, not a retry policy.** Attempts
  1–3 all requested `user_data_dir="release-gate-warmup"`; attempt 4 got
  `-2`. That is `clone_storage._next_available_explicit_dir`
  (`clone_storage.py:888-901`) reacting to `_profile_has_running_browser`, whose
  POSIX fallback is `any((profile_dir / m).exists() for m in ("SingletonLock",
  "SingletonSocket", "SingletonCookie"))` (`clone_storage.py:95-98`). A failed
  spawn's Chrome creates those markers; F-860's reaper kills the **process** and
  leaves the **markers**, so the named profile reads as busy forever after and
  every later spawn on that name silently walks to `-2`, `-3`, …
  **This is a new, undocumented interaction between F-860's reap and
  `_next_available_explicit_dir`**, and it is worth its own line in §8: a
  user who names a session profile and hits this family once gets a *different*
  profile — and therefore different cookies and logins — on their next spawn,
  with nothing in the answer to say so.

Net wall-clock cost of the family in this one job: ~42 s of warmup and backoff
plus the journey spawn, then a red cell and a full gate rerun.

---

## 2. Measured rate

**Method.** Family = the cell's job log contains `Failed to connect to browser`.
A failed cell with any other shape is counted as NOT this family. Two
independent censuses, which agree on every cell they share:

* **Primary (teammate census, 2026-09-15):** 107 completed runs — 39 `CI`
  (`test.yml`, 2026-09-05 → 09-15), 40 `canary` (08-06 → 09-14), 28 `publish`
  (06-26 → 09-15). All jobs listed; logs downloaded and grepped for **all 58
  failed non-aggregate cells** (one Windows `coverage` log from 08-26 had
  expired, 404). Earlier attempts additionally enumerated for the 6
  multi-attempt runs.
* **Cross-check (this finding):** the last 25 `CI` runs, **every attempt**
  (990 job rows), 17 failed leaf cells, all logs fetched and grepped. It
  reproduced the primary census's classification on every overlapping cell,
  including all three September hits and the Windows zeroes.

### 2.1 Per cell

| cell | cells sampled | failed | **family** | the other failures were |
|---|---:|---:|---:|---|
| `integration (Linux/X64)` | 105 | 4 | **2** | F-843 bridge-witness; `/payload/text` |
| `integration (macOS/ARM64)` | 105 | 19 | **1** | the F-770 masked-UA streak (08-07→08-21), `/payload/text`, a sigterm assertion |
| `offline-stealth (Linux/X64)` | 105 | 1 | **1** | — |
| `install-smoke (wheel Linux/X64)` | 106 *(per attempt)* | 1 | **1** | — |
| `transport (Linux/X64)` | 105 | 1 | 0 | F-843 witness assertion |
| `install-smoke (sdist Linux)`, `install-smoke (macOS ×2)`, `offline-stealth (macOS)` | 105 ea | 0 | 0 | — |
| **Windows — every cell, every shape** | **27 failed cells examined** | 27 | **0** | herd wedges (F-859), cookie-survival assertions, one F-770 UA assertion, POSIX-only `backend_launch` `PermissionError`s |

**The Windows control is a clean zero: 0 of 27 failed Windows cells contain the
string.** Windows fails for its own reasons, and never for this one.

### 2.2 Per run, and the cost

* **5 occurrences across 5 runs**, all on **attempt 1**. 4 of 107 runs on the
  latest attempt; 5 of 107 counting attempt 1 of `34924931290`.
* **0 of 40 `canary` runs.**
* September only: **3 hits in 213 Linux + macOS real-Chrome cells.**
* **Earliest:** run `30606828271`, 2026-07-31 05:26 UTC, the v2.0.1 publish
  gate, `integration (Linux/X64)`, Chrome 150.0.7871.128, image
  20260720.247.2 — **six weeks old, and it predates F-860's reaper**, so this
  is not a regression introduced by that fix.
* **Latest:** run `34924931290` attempt 1, 2026-09-15 03:25 UTC,
  `install-smoke (wheel Linux/X64)`, Chrome 152.0.7977.82, image
  20260907.300.1 — **the first hit ever on that cell**.

So the honest headline is **~4 % of gate runs, ~1.4 % of POSIX real-Chrome
cells** — low, but each one is a red required check on a green change, and the
rerun is the whole gate.

### 2.3 What it does NOT correlate with

* **Not a Chrome or image version.** The same image `20260907.300.1` with Chrome
  152.0.7977.82 **passed ≥36 sampled Linux/macOS Chrome cells** in the same
  window — including the v2.1.5 release gate that ran **7 minutes after a hit**.
  The earliest and latest hits are 6 weeks and two Chrome majors apart.
* **Not a branch or a change.** Hits land on `main` pushes, a tag gate, and a
  docs-only PR alike.
* **Not the runner pool.** All runners are hosted `GitHub Actions N`; no label
  or name signal.
* **Not a product regression.** It predates F-860, F-866 and F-867, and the
  test-side workarounds naming it (§9) predate the sample window.

---

## 3. Mechanism

### 3.1 The one hard number: nodriver waits 2.75 s, and nothing can change it

`nodriver` 0.47.0, `nodriver/core/browser.py`, `Browser.start()`:

| line | code | cost |
|---|---|---|
| `:358-361` | `connect_existing = False` unless BOTH `config.host` and `config.port` are set; otherwise `host = "127.0.0.1"`, `port = util.free_port()` | — |
| `:397-405` | `await asyncio.create_subprocess_exec(exe, *params, …)` — **Chrome is now running** | — |
| `:411` | `self._http = HTTPApi((self.config.host, self.config.port))` | — |
| `:413` | `await asyncio.sleep(0.25)` | 0.25 s |
| `:414-422` | `for _ in range(5): try: self.info = … await self._http.get("version") except: await self.sleep(0.5) else: break` | 5 × 0.5 s |
| `:424-435` | `if not self.info: raise Exception("… Failed to connect to browser …")` | — |

Total patience = **0.25 + 5 × 0.5 = 2.75 s**, plus the cost of five
`GET http://127.0.0.1:<port>/json/version` calls. Measured launch→raise was
2.768 s (§1.1), i.e. **18 ms for five probes** — consistent only with immediate
`ECONNREFUSED` (§1.3).

> **It is five sleeps, not four.** The natural misreading is that the last
> iteration skips its nap. It does not: at `:418-420` the `if _ == 4:` guards
> **only** `logger.debug(...)` (indent 20); `await self.sleep(0.5)` sits one
> level out at indent 16, directly under `except`, so it runs on **every** failed
> iteration including the fifth. The clock settles it independently — four sleeps
> would give 2.25 s, which is 518 ms away from the five measured values, while
> five sleeps give 2.75 s, which is 18 ms away. **Source and wall clock agree on
> 2.75 s.**

Two properties make this a product problem and not a tuning problem:

* **It is hardcoded.** `nodriver.Config.__init__` (`nodriver/core/config.py:37`)
  exposes `user_data_dir`, `headless`, `browser_executable_path`,
  `browser_args`, `sandbox`, `lang`, `host`, `port`, `expert` — and **no**
  connection-timeout or max-tries field. There is no knob to raise. (Later
  nodriver releases add `browser_connection_timeout` / `browser_connection_max_tries`;
  0.47.0, the version this repo pins, does not have them. Confirmed by reading
  the installed `config.py`, not from release notes.)
* **The caller's patience is 43× larger and entirely unused.** The gate wraps
  the spawn in `SPAWN_TIMEOUT = 120.0` and `WARMUP_TIMEOUT = 150.0`
  (`tests/release_gate_harness.py:97-98`). nodriver abandons a launched,
  running Chrome after 2.75 s of a 120-second budget. **~117 seconds of
  allowance are never spent**, and no code in this repo can spend them, because
  `uc.start()` owns both the launch and the connect.

### 3.2 Why the product cannot currently wait

`browser_manager._launch_browser` is:

```python
config = uc.Config(headless=…, user_data_dir=…, sandbox=…,
                   browser_executable_path=…, browser_args=…)
return await uc.start(config=config)          # browser_manager.py:536-543
```

`uc.start()` → `Browser.create(config)` → `instance.start()`. Launch and connect
are one indivisible await. The product's *only* observation point is the
exception, and by then it holds no `Browser`, so it cannot even ask the process
whether it came up a second later. That is precisely the hole F-860 documented
from the other side (`audit/stage2/finding_F860_…md` §1.2, which already names
the "5 × 0.5 s" loop and that "neither path kills `self._process`"). F-860 fixed
the *leak*. **The launch failure itself was left in place, and this finding is
that residue.**

`browser_manager.spawn_browser`'s error handler then appends the two existing
hint paragraphs — `spawn_exhaustion.exhaustion_hint` (F-811, "is this machine
out of browser capacity") and `spawn_contention.contention_hint` (F-834, "was
this spawn racing siblings") — at the one composition site
(`browser_manager.py:777-782`). Neither fires here: one spawn at a time, and a
GitHub runner is not process-exhausted. So the user-visible error is nodriver's
raw text and nothing else. **There is no third hint home for "Chrome launched
but did not answer in time", and that is the gap a fix would fill** (§6).

### 3.3 What the 5.85 s attempts were (not a second launch)

Attempts 4 and 5 report `tool spawn_browser end (5850.4ms)` and `(5871.5ms)`
with reap lines ~3 s apart. That is **one** launch, not two: `spawn_leak.reap_launched_browsers`
walks candidate pids serially and each `cleanup._kill_process_by_pid` escalates
(terminate → wait → kill). Chrome's log line at +3.994 s (§1.2) is the same
process still alive across that escalation, and the second reap burst picks up
child pids that appeared meanwhile. The launch→*raise* figure is 2.768 s in all
five cases; the remainder is teardown.

### 3.4 The family is TRANSIENT, and on Linux it is specifically the job's FIRST launches

This is the most informative fact in the finding and it was not visible from the
`install-smoke` log alone, because that job dies at its first failure.

**Run `34911829422` (2026-09-15 00:07, `main` push, `integration (Linux/X64)`),
job `104200794999`** — the in-process integration lane, 180 selected tests, each
spawning real Chrome:

| time | event |
|---|---|
| 00:07:54.9 | collection ends; nothing has launched Chrome yet |
| — | spawn `675b05f5` on `sessions/ci-warmup` → **fail** (`warmup_once` attempt 1) |
| — | spawn `11364fa0` on `sessions/ci-warmup` → **fail** (attempt 2) |
| — | spawn `2c8534da` on `sessions/**ci-warmup-2**` → **fail** (attempt 3; note the suffix) |
| — | spawn `4c4c80c4` on `sessions/ci-basic-test` → **fail** (the test's own spawn) |
| 00:08:24.6 | `TestBrowserSpawnAndClose::test_spawn_and_close` **FAILED** — the run's **first** test |
| **00:08:30.1** | `test_spawn_with_relative_user_data_dir` **PASSED** on a fresh `integration-test-profile` — 5.5 s later |
| 00:08:31 → end | **~170 further tests, each spawning real Chrome, all PASSED** |

**The first four Chrome launches on that runner failed; the fifth, 5.5 s later,
succeeded, and so did every one of the ~170 after it.** Eight pids were reaped
across the four failures, on three different profile directories.

The 2026-09-11 `offline-stealth (Linux/X64)` hit has the same shape (≈29.5 s
window, four failed spawns, then the lane proceeds). Both Linux hits are the
job's opening launches.

**What this rules out, hard:**

* **H2 (port race) as the explanation.** A lost-port race is independent per
  launch. It does not hit exactly the first four and then miss 170 consecutive
  times. It remains possible as a rare *additional* contributor; it cannot be
  the mechanism.
* **H3 (Chrome wedged / never comes up).** Chrome comes up fine on this exact
  runner, 170 times, minutes later.
* **Any per-profile explanation.** The four failures span **three different**
  profile directories — `ci-warmup` (twice, reused), `ci-warmup-2` (fresh),
  `ci-basic-test` (fresh) — and the first success is on a fourth, fresh
  `integration-test-profile`. **Reused and fresh profiles both fail during the
  burst, and a fresh one succeeds immediately after it.** The profile is not the
  variable; a time-bounded whole-machine condition is.
* **The reaper racing the next launch.** The `-2` suffix (§1.5) first appears on
  attempt **3** — attempts 1 and 2 had already failed on the *un-suffixed*
  directory, before any leaked Chrome could have held it. The reaper's kill and
  the leftover `SingletonLock` are a **downstream consequence** of the family,
  not a cause of it. (Asked explicitly; answered explicitly.)

**What is left is a whole-machine, time-limited condition that the first
launches meet and later launches do not.** Three candidates remain, and they are
NOT equally supported:

| sub-cause | status |
|---|---|
| **Chrome's own cold start** — binary and shared libraries paged in from cold disk, `fontconfig` cache built, first-run profile work | **The leading one.** It is the only candidate that is *necessarily* present at launch #1 and *necessarily* absent by launch #5, with no other assumption. |
| **CPU/IO contention from the job's own setup** still draining (`uv sync`, the Chrome install, `apt-get`, Xvfb) when the first spawns land | **Plausible and additive.** Cannot be separated from the above by any existing evidence; §7 distinguishes them, because a machine-contention cause makes the *product-free* control launch slow too, whereas a pure page-in cause makes only the very first one slow. |
| **F-860's reaper escalation overlapping the next launch** (each failure spends up to ~3 s in terminate→wait→kill) | **Cannot be the cause; can only amplify.** There is nothing to reap before the *first* failure, so it cannot explain attempt 1 — and attempt 1 is the one that has to be explained. It plausibly worsens attempts 2–4 by competing for the same starved CPU. |

So H1 stands, considerably stronger and much narrower than "the runner is slow":
**the runner is not slow — the *first launch* is expensive, and nodriver's
2.75 s sits below that one-off cost while sitting far above the steady-state
cost.**

And it carries a sharp corollary that reframes the existing workarounds:
**the failed attempts are the warmup succeeding.** Each failed launch still
paged Chrome in. `warmup_once`'s three retries did warm the machine — they just
all reported failure, because each retry was itself measured against the same
2.75 s wall, and the wall is crossed only *after* the cache is warm. The warmup
is not broken; it is **structurally unable to report the success it produces.**

### 3.5 macOS is a different sub-shape and is NOT explained by §3.4

The one macOS hit (run `34658141944`, 2026-09-11 23:27) is
`test_window_sizing.py::TestRealChromeWindowSize::test_headed_spawn_honours_a_size_that_fits`,
which **FAILED at 98 %** of the run — mid-run, long after ~150 successful
spawns, with `test_spawn_and_close` having **PASSED** at 23:28:10. Five pids
were reaped; the next headed spawn 2.5 s later passed. Its test-to-test window
was ~46.5 s for a single attempt, which the timing census could not decompose.

So: same error string, same reaper signature, **but not the cold-start pattern**
— and it is the only **headed** spawn among the five hits. This is the same
occurrence F-859 §12.2 recorded and declined to attribute. **It is treated here
as probably-related and explicitly not explained**; H8 (display path / no window
manager) stays live for it and for it alone.

---

## 4. Hypotheses: what the evidence supports, and what it rules out

| # | hypothesis | verdict |
|---|---|---|
| **H1** | **Chrome's COLD-START time-to-DevTools on a hosted POSIX runner exceeds 2.75 s**, while its steady-state time is far below it. | **STRONGLY SUPPORTED — the leading hypothesis.** Everything fits: the 2.75 s fingerprint spent in full (§1.1); Chrome alive at 2.8 s and still logging init work at 4.0 s (§1.2); the port refusing connections, i.e. unbound, for the whole window (§1.3); and decisively, **the failures are the job's first four launches and launch #5 onward all succeed** (§3.4). Still **not proven**, because we have never observed Chrome answering *late* — the reaper kills the witness at 2.8 s, and nodriver swallows Chrome's stderr so the DevTools banner is unobservable (§1.2). §7 creates that observation. |
| **H2** | **`util.free_port()` lost the port.** `nodriver/core/util.py:132-143` binds `127.0.0.1:0`, reads the port, **closes the socket**, and returns the number; Chrome binds it later. Anything can take it in between. **nodriver's own source flags this**: `config.py:175-178` reads *"the host and port will be added when starting the browser, as by the time it starts, the port is probably already taken"*. | **RULED OUT as the mechanism** (§3.4): a per-launch race cannot select exactly the first four launches and then miss 170 consecutive ones. **Not ruled out as a rare additional contributor** — it would look identical in a single log, and this repo has been bitten by the class before (the herd's per-process bind probe self-colliding). §7 detects it for free. |
| **H3** | **Chrome is wedged, not slow** — stuck in early init and would never have answered. | **RULED OUT for the Linux hits** (§3.4): the same runner launched Chrome successfully ~170 times minutes later. The `dbus/bus.cc:405` noise is real but not fatal — and note nodriver already passes `--password-store=basic` (`config.py:116-128`), so the usual keyring/dbus cause is already suppressed. |
| **H3b** | a **per-profile** cause — a stale lock, a leaked Chrome holding the directory, the reaper's kill racing the next launch | **RULED OUT** (§3.4). The four failures span three different profile directories and the first success is on a fourth; the `-2` suffix appears only at attempt 3, *after* two failures on the un-suffixed directory. The profile effects are downstream (§1.5, §8). |
| **H4** | running as root / missing `--no-sandbox` (nodriver's own suggestion) | **RULED OUT.** `Root: False`, `Sandbox: False` in the backend log; `--no-sandbox` explicitly appended (§1.4). |
| **H5** | Chrome crashed or exited at launch | **RULED OUT.** F-860's reaper found live pids on all five attempts, and Chrome logged at +3.994 s (§1.2). |
| **H6** | spawn contention — sibling spawns racing (F-834) | **RULED OUT.** Strictly one spawn at a time; `contention_hint` did not fire. |
| **H7** | process exhaustion (F-811) | **RULED OUT.** `exhaustion_hint` did not fire; a fresh GitHub runner has no Chrome population. |
| **H8** | no window manager under Xvfb (the known CI note; `.github/workflows/release-gate.yml:970-976` starts a bare `Xvfb :99`, nothing clamps) | **RULED OUT for this cell.** The gate spawns `headless: True` (`_headless_spawn_kwargs`, `tests/release_gate_harness.py:838-853`), so `DISPLAY` is irrelevant to the launch. It stays live for the **macOS headed** instance in §2 / F-859 §12.2 (`test_headed_spawn_honours_a_size_that_fits`), which is headed — that one may be a different member of the same family and is not claimed here. |
| **H9** | a product regression in a recent release | **RULED OUT.** The trigger run is a **docs-only PR**; the earliest hit is 2026-07-31 and predates F-860/F-866/F-867 (§2.2); and the test-side workarounds naming this exact error predate the sample window (`tests/e2e_helpers.py:113`). |
| **H10** | a Chrome or runner-image version | **RULED OUT.** §2.3: the same image and Chrome build passed ≥36 sampled POSIX cells in the same window, including the v2.1.5 gate 7 minutes after a hit; hits span two Chrome majors over six weeks. |

**The honest summary.** The *mechanism* is proven to 18 ms: a hardcoded 2.75 s
window is spent in full against a Chrome that is alive and not yet listening.
The *cause* is now narrowed from three live hypotheses to one leading one —
**H1, in its cold-start form**, with H2 and H3 ruled out as mechanisms by the
"first four fail, next 170 pass" observation (§3.4). What is still missing is
the single direct measurement that would turn "strongly supported" into
"proven": **how long Chrome's first launch on these images actually takes to
open its DevTools endpoint.** Nobody has ever measured it, because the product
kills Chrome at 2.8 s and nodriver discards its stderr. §7 measures it.

The macOS headed hit (§3.5) is **not** covered by that summary and is not
claimed to share the cause.

---

## 5. What is and is not affected

**Affected**

* Any real `spawn_browser` on a GitHub-hosted Linux or macOS runner: the gate's
  `install-smoke`, `offline-stealth`, `integration` and `transport` cells.
* **Real users on slow or loaded machines**, in principle — nothing about the
  2.75 s window is CI-specific. It is simply that CI is where we have logs. No
  user report exists; this is stated as exposure, not as an observation.
* The named-profile lifecycle, indirectly: §1.5's `-2` walk means a user whose
  spawn hits this family loses their named profile's identity on the next
  spawn, silently.

**Not affected**

* Windows. §2's control is zero, and the two Windows-side gate families already
  filed are different shapes — F-859 (herd wedge at `tools/list`) and F-867
  (backend inherits the client's job object). Windows Chrome's cold start on
  the GitHub image evidently clears 2.75 s.
* Correctness of any tool's answer. This is a launch failure that is *reported*
  as a failure. Nothing is silently wrong.
* The leak. F-860 fixed that and its reaper is working correctly here — visibly,
  five times. This finding is about what the reaper is cleaning up *after*.

---

## 6. Why no fix ships with this finding

A fix is *conceivable* and sketched below, but shipping it now would violate
this repo's own rules on two counts, so it is written down rather than merged.

### 6.1 The candidates, each evaluated

| # | candidate | verdict |
|---|---|---|
| **(a)** | **Ask nodriver to wait longer.** | **IMPOSSIBLE in 0.47.** `Config.__init__` (`nodriver/core/config.py:37-101`) takes `user_data_dir, headless, browser_executable_path, browser_args, sandbox, lang, host, port, expert` — **no timeout and no max-tries field**. The `0.25 + 5 × 0.5` is literal in `Browser.start()` (`browser.py:413-420`). Verified by reading the installed package, not release notes. Later nodriver versions add `browser_connection_timeout` / `browser_connection_max_tries`; **upgrading nodriver is therefore a real candidate in its own right, and the cheapest one if the version is otherwise acceptable** — but this repo pins 0.47 and a nodriver bump is its own risk surface (the whole `element_resolution.py` subsystem exists because of 0.47 selector semantics), so it is a decision, not a drive-by. |
| **(b)** | **Wait on `/json/version` ourselves before handing off to nodriver.** | **Sound in principle, not small in practice.** It requires launching Chrome ourselves and handing nodriver `host`+`port` so it takes its `connect_existing` branch (below). That is a genuine one-home fix with no knob — and it is exactly the pattern `desktop_launch.launch_and_attach` already implements for F-810. Blocked on §6.2's hazards, not on the idea. |
| **(c)** | **Reduce the cold-start cost** (launch flags, smaller profile clone). | **LARGELY EXHAUSTED, and unmeasured.** nodriver's `_default_browser_args` (`config.py:116-128`) **already** passes `--no-first-run`, `--no-default-browser-check`, `--no-service-autorun`, `--disable-breakpad`, `--disable-dev-shm-usage`, `--homepage=about:blank` and `--password-store=basic` (which is the usual keyring/dbus suppressor). The obvious wins are taken. Remaining candidates — `--disable-background-networking`, `--disable-component-update` — are plausible but would be **chosen without evidence that they move the cold path at all**, which is the same guess §6.2(1) refuses. Note this cannot fix the cost that dominates a cold start: paging the Chrome binary in from disk. |
| **(d)** | **Stop discarding Chrome's stderr** so a post-mortem can distinguish "never listened" from "listened late". | **INDEPENDENTLY WORTH DOING, and not blocked by §7.** nodriver pipes both streams and never reads them (`browser.py:397-405`), which is why §1.2's DevTools banner is unobservable and why this family has been undiagnosable for six weeks. This is the same class of problem F-303 solved for the backend's own boot log. It fixes no red by itself — it is what makes the *next* red readable. |

**The shape a (b) fix would take.** `Browser.start()` takes the
`connect_existing = True` branch when both `config.host` and `config.port` are
set (`nodriver/core/browser.py:357-361`), in which case it **does not launch** —
it only attaches. And because `uc.start(config=…)` passes our object straight
through (`Browser.create`, `:83`, `if not config:` — ours is not `None`),
nodriver mutates *our* `config` in place, so after the raise **we still hold the
port it launched Chrome on**. So the product could, on exactly this error:
poll `http://127.0.0.1:{config.port}/json/version` (and/or
`<user-data-dir>/DevToolsActivePort`) with its own patience, and on an answer
attach via a second `uc.start(uc.Config(host=…, port=…))`.

**Why not now.**

1. **The wait cannot be sized from evidence.** H1 vs H3 is unresolved (§4). If
   H3 is right, this adds N seconds to every failure and fixes nothing. A
   number chosen without §7's measurement is a guess, and this repo does not
   ship guessed constants (cf. `animation_facts`'s cap defaults, justified from
   measurement, and the explicit ban on making them `STEALTH_MCP_*` knobs, F-853).
2. **The attach path has a real correctness hazard.** A `Browser` created on the
   `connect_existing` branch has **no `_process`**, so
   `_apply_post_launch`'s `getattr(browser, "_process", None) or
   desktop_launch.pid_shim(browser)` (`browser_manager.py:563`) would fall to
   F-810's Windows-only shim and the instance would go **untracked** — re-opening
   precisely the orphan-reaping hole F-860 closed. It also carries a *fresh*
   `uc.Config`, whose `user_data_dir` is not ours, so `actual_user_data_dir` and
   `uses_custom_data_dir` (`browser_manager.py:677-685`) would read wrong and the
   profile/clone lifecycle would lie. Both are solvable — the pid is recoverable
   via the same `process_cleanup._get_browser_pids_for_profile` the reaper uses —
   but not in a change that can be called small.
3. **There is nowhere to put it without a new leaf.** `browser_manager.py` is
   `GRANDFATHER`ed at **1529 LOC with cap == actual**
   (`tools/check_file_budgets.py:51-60`, ratcheted down by F-860 itself), so it
   may not grow by a line. The fix would need a new leaf beside
   `spawn_contention.py` / `spawn_exhaustion.py` / `spawn_leak.py` — which is in
   fact the right home shape, and §8 names it — plus a ratchet.
4. **"A second way is a defect."** `desktop_launch.launch_and_attach` already
   owns "launch elsewhere, then attach to a running Chrome" (F-810). A second
   attach path on POSIX must either reuse that seam or be argued as a
   deliberately separate one. That argument needs §7's answer first.

**What a fix must NOT be**, for the record: not a `STEALTH_MCP_*` knob (the env
home is `settings.py` and this is a tuned constant, not a user choice); not a
retry loop inside `spawn_browser` (the second launch meets the same 2.75 s wall,
which is exactly why the harness's four backed-off retries all failed in §1.1);
and not a bigger `SPAWN_TIMEOUT` (the harness comment at
`release_gate_harness.py:892-894` already worked this out — "a bigger
`WARMUP_TIMEOUT` cannot fix it", and it is right, because the budget is spent by
the library, not by the caller).

**The cheap, honest interim** — available today and not blocked by §7 — is to
stop making maintainers re-derive this. `spawn_browser`'s error currently hands
back nodriver's root/sandbox advice with no correction, at a site that already
composes two other hint paragraphs (`browser_manager.py:777-778`). A third leaf
alongside `spawn_contention` / `spawn_exhaustion` that recognises
`Failed to connect to browser`, states the 2.75 s window, states that Chrome was
launched and (per F-860) reaped, and says that root/sandbox is not the cause when
`Root: False | Sandbox: False`, would cost ~40 lines in a leaf plus one `+=` at
the composition site. It fixes no red, and it is not proposed as one — it turns
a recurring 30-minute re-diagnosis into a read.

---

## 7. The experiment — now running on every gate run

**Status: IMPLEMENTED in this PR.** What follows is what it does and how to read
it, not a proposal.

**It measures Chrome's cold time-to-DevTools on the gate's own images,
product-free, and it measures the FIRST launch specifically, because §3.4 says
that is the only one that matters.**

| piece | where |
|---|---|
| the probe | `tools/chrome_cold_start_probe.py` — stdlib only, imports no part of the package (pinned by a test that asserts so in a subprocess) |
| its hermetic test | `tests/test_chrome_cold_start_probe.py` — a real **fake-browser subprocess** that writes `DevToolsActivePort` after a configurable delay and serves `/json/version` on loopback; no real Chrome in the unit lane |
| the gate step | `Chrome cold-start probe (F-870)` in `.github/workflows/release-gate.yml`, on `integration`, `transport`, `offline-stealth` and `install-smoke` |
| the evidence | artifact kind `chrome-cold-start` (`tools/release_evidence.py` `ARTIFACT_KINDS`), landing at `release-evidence/<sha>/<job>/artifacts/<cell>/chrome-cold-start.json` (`_copy_artifacts` inserts the cell, `release_evidence.py:458-459`), plus the three per-cell human uploads |

It launches Chrome **twice**, back to back, on a fresh profile each time, and
reports per launch: `ms_to_devtools_banner`, `ms_to_json_version`,
`port_requested`, `port_from_banner`, `port_matches_request`, `pid`,
`exit_code_if_died`, `listening` and an `output_excerpt`. The first/second delta
is the whole question (§3.4): a page-in cause makes only launch #1 slow, a
contention cause makes both slow.

**Readiness is read from Chrome's own `DevTools listening on ws://…` banner**,
not from `DevToolsActivePort`. The file was tried first and rejected *on
measurement*: with a fixed `--remote-debugging-port=<n>` — which is what the
product passes — Chrome does **not write that file at all** (it is a
port-discovery mechanism for `--remote-debugging-port=0`). A first real run
returned a null reading for every launch while Chrome was demonstrably
listening. The banner works under both idioms, is Chrome's own statement, and
carries the port Chrome actually bound.

### 7.1 The placement decision, stated once

**The probe runs AFTER `Resolve image Chrome Stable identity`, not before it.**

Before would give a genuinely cold binary. It was rejected for two reasons.
First, the *product's* own first spawn also happens after that step, so measuring
there measures the conditions the product actually meets — and a number that does
not describe the product's situation cannot be compared to the 2.75 s window.
Second, `--freeze-updater` (F-819) has run by then; ahead of it, macOS Keystone
can swap Chrome Stable mid-measurement, which is the exact failure F-819 exists
to prevent and would make the reading describe two different binaries.

**The cost of that choice is stated in the data rather than hidden.**
`resolve_chrome._read_version` is not uniform across OSes: it execs
`chrome --version` on **Linux** (`resolve_chrome.py:128`), while Windows reads a
sibling version directory (`:110-117`) and macOS reads `Info.plist` (`:118-126`)
— neither of which execs the binary. So on Linux the binary is already paged in
when the probe runs, and **on Linux `ms_to_json_version` for launch #1 is a FLOOR
on the true cold cost, not the cold cost.** The record carries
`binary_prewarmed` (true on Linux, false on Windows/macOS) so the three OSes are
never compared blindly, and a test pins that derivation.

This supersedes the earlier draft of this section, which claimed the probe avoids
warming the binary *and* sits after a step that warms it. Both halves could not
be true; the second is.

### 7.2 The other design choices

* **The launch mirrors nodriver's, flag for flag.** `NODRIVER_DEFAULT_ARGS` is
  nodriver 0.47.0's `_default_browser_args` verbatim and `chrome_command`
  reproduces `Config.__call__` for the gate's `headless=True, sandbox=False`
  spawn — **no `--disable-gpu`** (nodriver never passes it) and no positional
  URL. This matters beyond tidiness: without `--password-store=basic` a headless
  Linux Chrome probes the keyring, and without `--no-pings` it does GCM
  registration work, neither of which the product's Chrome does — both would
  inflate the number being measured. A test compares the command against a real
  `nodriver.Config`, so a nodriver bump fails there instead of silently
  re-defining what is measured.
* **Ports use nodriver's own idiom**, not `--remote-debugging-port=0`:
  `_reserve_port` binds `:0`, reads the number and closes the socket, exactly as
  `nodriver/core/util.py:132-143` does, then hands it to Chrome. That reproduces
  the lost-port race and makes `port_matches_request` a real boolean, so **H2 is
  sized rather than merely asserted**. Both launches use it, so the only
  difference between #1 and #2 stays the state of the machine.
* **It calls `resolve_chrome._resolve_path()`, not `resolve_chrome()`** — the
  public one shells out for `--version`, and this script should not add a second
  exec of its own on top of §7.1's.
* **Output goes to a temporary FILE, not a pipe and not `DEVNULL`.** A pipe's
  kernel buffer can fill and block the child forever, and the probe would then be
  measuring its own deadlock; `subprocess.DEVNULL` is banned repo-wide (TID251)
  for exactly the habit this finding is about. The excerpt keeps **head 2000 +
  tail 2000** bytes, because `DevTools listening on ws://` is among the *first*
  lines Chrome writes and a tail-only excerpt would discard precisely the line
  §6.1(d) is about.
* **It cannot fail the job, and that is enforced at two levels.** Every narrow
  `except` is a judgement about a specific failure; one outer guard in `main()`
  is the contract — whatever escapes, a record naming it is still written and the
  exit code is still 0. That guard is not theoretical: `http.client.HTTPException`
  is **not** an `OSError` and urllib does not wrap it, so a socket that accepts
  and answers with a non-HTTP line raises `BadStatusLine` straight through — and
  that is reachable in exactly the case this probe studies. Both the narrow catch
  and the outer guard are pinned by tests.
* **It runs on Windows too.** Windows is the control, and "0 of 27 failed cells"
  (§2.1) is *absence of evidence*; a measured number is evidence of absence, and
  it costs ~0.7 s. One local Windows reading exists — **384 ms then 306 ms to
  `/json/version` against a 2750 ms budget** — but that is **n=1 on a single
  developer machine** (a second, loaded run measured ~441/433 ms). It is a
  plausible first explanation of why Windows never shows this family, **not a
  property of Windows**. The gate is what will produce the distribution.

On the `install-smoke` macOS cells (`stages: handshake`, partial by F-773) the
probe is the only Chrome the job launches. That is deliberate and harmless: it
measures the image, makes no navigation claim, and leaves the F-773 gap warning
exactly as true as it was.

**How to read the result:**

```
google-chrome --headless=new --no-sandbox --disable-gpu \
  --remote-debugging-port=<port chosen the same way nodriver chooses it> \
  --user-data-dir=<fresh temp dir>
```

then poll, every 50 ms for 60 s, and print:

* first time `<user-data-dir>/DevToolsActivePort` exists, **and the port written
  in its first line**;
* first time `http://127.0.0.1:<port>/json/version` returns 200;
* whether the two ports agree.

It is product-free deliberately: the product's own reaper kills the witness at
2.8 s (§4, H1), so the measurement cannot be taken from inside a failing spawn.
It runs on every gate run, costing ~2–5 s, so after ~20 runs there is a
*distribution* rather than one anecdote — and the family is intermittent, so a
distribution is the only thing that can show whether 2.75 s sits inside its tail.

**How to read the result:**

| observation | verdict |
|---|---|
| launch #1 answers at *t* > 2.75 s, launch #2 well under it, **same** port both times | **H1 confirmed, in its cold-start form.** nodriver's window is below this image's first-launch cost; the fix is §6.1(b)'s attach path or §6.1(a)'s nodriver bump, and the observed distribution of *t* for launch #1 **sizes the wait — measured, not guessed.** |
| **both** launches exceed 2.75 s, on the runs where they do | the cost is machine contention, not Chrome page-in. Same fix shape, but the wait must be sized against the contention window, and reducing the gate's own setup overlap becomes a candidate. |
| `DevToolsActivePort` carries a **different** port from the one chosen | **H2 confirmed** for that occurrence — `util.free_port()`'s bind-close race. A different fix entirely: hold the port until Chrome takes it. Expected to be rare (§4), but this is how it would be caught. |
| `DevToolsActivePort` never appears within 60 s | **H3 confirmed.** Chrome does not bring DevTools up on this image under these conditions; patience is irrelevant and the cause is upstream (dbus, cgroup IO, sandbox namespace). |
| every launch answers in well under 2.75 s, on every run, for weeks | the cold path is **not** reproducible outside the product, and the next step is §6.1(d) — capture Chrome's stderr in the product and wait for the family to recur with evidence attached. |

A secondary, near-free addition: have the poll loop also record the value of
`DBUS_SESSION_BUS_ADDRESS`, since §1.2's dbus errors say the runner's is
unparseable, and `--disable-features=…`/`dbus` suppression is a candidate
mitigation *if* H3 is what comes back.

---

## 8. What remains

* **The rate is known (§2) and the cause is narrowed to one leading hypothesis
  (§4), but not proven.** §7 is the blocker for a fix that is sized rather than
  guessed.
* **§6.1(d) — capturing Chrome's stderr — is not blocked by §7** and is the
  single highest-value unblocked item: it is the reason six weeks of this family
  produced no usable evidence.
* **§6.1(a) — the nodriver bump — is a decision someone should take
  deliberately.** Newer nodriver exposes the connect patience as a parameter,
  which would collapse this whole finding into one keyword argument. It is not
  taken here because a nodriver upgrade moves selector semantics that
  `element_resolution.py` exists to contain, and that is a separate, larger
  verification.
* **`release-gate-warmup-2` is an unfiled sub-defect** (§1.5): F-860's reaper
  kills the process and leaves `SingletonLock`/`SingletonSocket`, so
  `_profile_has_running_browser` reads the profile as busy forever and
  `_next_available_explicit_dir` silently walks the caller to a *different*
  profile. This is a user-visible identity change (different cookies, different
  logins) with nothing in the spawn's answer to announce it. It is a real defect
  independent of whether F-870 is ever fixed, and it belongs either in a
  follow-up to F-860 or in its own finding. **Not fixed here.**
* **The macOS headed instance is a different sub-shape and stays open** (§3.5).
  `test_headed_spawn_honours_a_size_that_fits`, mid-run at 98 %, the only
  **headed** spawn among the five hits, bounded at ~46.5 s test-to-test for a
  single attempt — so unlike every Linux hit, its window could **not** be shown
  to be the 2.75 s one. Same error string and same reaper signature; different
  position, different display path, undecomposable timing. It is the same
  occurrence F-859 §12.2 declined to attribute, and this finding declines too.
  H8 stays live for it alone.
* **No fix branch exists.** This finding ships alone on
  `docs/F870-posix-connect-family`. `CHANGELOG.md` is deliberately untouched —
  nothing shipped.

## Sources

* **Rate (§2), primary:** the CI census taken for this finding on 2026-09-15 —
  107 completed runs across `test.yml` / `canary.yml` / `publish.yml`, logs for
  all 58 failed non-aggregate cells. Working copy:
  `%TEMP%\ci_rate_census_posix_connect.md` (scratch, not committed; its
  headline numbers are reproduced in full in §2 so this finding does not depend
  on it surviving).
* **Rate (§2), cross-check:** the last 25 `CI` runs, every attempt, 990 job
  rows, 17 failed leaf-cell logs — run independently for this finding and in
  agreement on every shared cell.
* **Mechanism (§3):** `nodriver` **0.47.0** as installed
  (`.venv/Lib/site-packages/nodriver/`), read directly — `core/browser.py`
  lines 357-361, 397-405, 411-435, 899-933; `core/config.py` lines 37-101,
  116-133, 175-193; `core/util.py` lines 132-143.
* **Evidence (§1):** run `34924931290` attempt 1 job `104240899486`;
  run `34911829422` job `104200794999`; run `34658141944` job `103454858082`.

## Prior art (cited, not restated)

* `audit/stage2/finding_F860_failed_spawn_leaks_untracked_chrome.md` §1.2 —
  already identified the "5 × 0.5 s" loop and that neither raise path kills the
  process; §1.3 explicitly left *which* raise point fired unestablished. F-870
  establishes it (the connect loop, not the websocket handshake) and takes up
  the launch failure that F-860 scoped out.
* `audit/stage2/finding_F859_windows_herd_stall_rate.md` §12.2 — the macOS
  `test_headed_spawn_honours_a_size_that_fits` instance, recorded there as
  "not the herd, not F-859". §12.4 measures the POSIX side as 1/81 for that day.
  F-859 owns the Windows herd wedge; F-870 owns this.
* `tests/e2e_helpers.py:113-140` and `tests/release_gate_harness.py:856-970` —
  the two existing test-side workarounds, both naming
  `Failed to connect to browser` in their own comments. They are the reason this
  family has been survivable without ever being filed, and the reason it still
  costs a rerun when they are defeated.
* `.github/workflows/release-gate.yml:970-976` — the bare `Xvfb :99`, no window
  manager (the standing CI note). Relevant only to headed cells (§4, H8).
