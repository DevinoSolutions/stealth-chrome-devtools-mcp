# F-859 — the Windows herd gate wedges at `tools/list`, and the wedge rate roughly tripled when F-856 landed

**Status:** open (gate reliability; the underlying wait is a real product behaviour, but no
user-facing defect is demonstrated here)
**Opened by:** investigation of four Windows herd reds on 2026-09-04/05, requested 2026-09-05
**Severity:** **HIGH.** On the measured window since F-856 merged, **26% of gate attempts**
(6 of 23) had at least one Windows cell reddened by `test_startup_herd.py`, against **9%**
(6 of 67) in the month before. Every red is a required-check red on a branch that did not
touch startup, and every re-run of the identical sha has passed. A required check that a
quarter of attempts fails for reasons unrelated to the change is a retry button, not a gate.

**This finding does NOT claim F-856 is wrong.** F-856 fixed a real incident (a healthy
backend condemned under CPU starvation, every session seeing `CONNECTION_CLOSED`). What it
did was move the proxy's worst-case *silent* wait from 60s to 240s of wall time, and 240s is
exactly the number the herd test uses as its own backstop. The gate now measures the ceiling
F-856 chose.

---

## 1. What was observed

### 1.1 The reds

| date | run / attempt | sha | cell | job id | outcome |
|---|---|---|---|---|---|
| 2026-09-02 20:03 | #246 a1 | `0792d5b` (the F-856 merge) | transport | 100404214466 | `live census found [5836, 9172]` — two live backends |
| 2026-09-02 20:26 | #248 a1 | `00fafdb` | integration | 100411668503 | killed by `pytest-timeout` at 180s |
| 2026-09-02 20:26 | #248 a1 | `00fafdb` | transport | 100411668660 | `TimeoutError` at the 240s backstop |
| 2026-09-05 07:48 | #253 a1 | `2f43dab` | transport | 101272665658 | `TimeoutError` at the 240s backstop |
| 2026-09-05 12:13 | #259 a1 | `d340075` | transport | 101304623845 | `TimeoutError` at the 240s backstop |
| 2026-09-05 13:43 | #261 a1 | `c3aef6e` | integration | 101315920848 | killed by `pytest-timeout` at 180s |
| 2026-09-05 13:43 | #261 a2 | `c3aef6e` | transport | 101317965646 | `TimeoutError` at the 240s backstop |

Branches: `main`, `fix/F858-toolerror-sweep-cloner`, `refactor/serversplit-slices-4-6`,
`refactor/serversplit-slices-7-9`. None touches `singleton.py`, `scheduling_lag.py`,
`serve_startup.py` or `proxy_selfheal.py`. Re-running the same sha passed in every case
(#253 a2, #259 a2, #261 a2-integration, #261 a3).

### 1.2 The wedge has exactly one shape — and it is NOT the 30s assertion

**OBSERVED.** Every herd red since 2026-08-01, in both cells, is the same thing:

```
tests\test_startup_herd.py:201: in test_forty_cold_sessions_are_all_usable_within_30s
    await asyncio.wait_for(
tests\test_startup_herd.py:185: in _one_session
    tools = await client.list_tools()
E   TimeoutError
```

The `initialize` handshake **completed** — line 183's `async with Client(...)` returned and
`initialize_s` was recorded. What never returns is `list_tools()`, the first call that
genuinely reaches the backend. `asyncio.wait_for` then cancels the whole gather at
`HERD_HARD_TIMEOUT_SECONDS = 240.0`.

The `30s`-named assertion at line 233 has **never** fired in this era. The two apparent
symptoms are one mechanism observed through two different lane clocks:

| cell | `pytest --timeout` | what the reader sees |
|---|---|---|
| transport | 300 (`release-gate.yml:497`) | the herd's own `TimeoutError` at 240s |
| integration | **180** (`release-gate.yml:324`) | `pytest-timeout` kills the process at 180s, before the herd's 240s backstop can name itself |

The integration cell therefore **cannot** report this failure by name. Its
`+++ Timeout +++` dump (job 101315920848) shows MainThread parked in
`_overlapped.GetQueuedCompletionStatus` — an idle asyncio loop waiting on child I/O — plus
three leftover `serve_forever` fixture threads in `select.select`. No product frame appears,
because the wedged coroutine is suspended at an `await` and the wedged work is in a child
process. **The thread dump contains no information about the cause.** This is a variant of
F-780 (a harness timeout set below the bound it is supposed to observe).

### 1.3 The runner was NOT slow — control measurement

**OBSERVED.** If the wedge were "the hosted runner degraded", the rest of the lane would be
slow too. It is not. Subtracting the 240s the herd spends waiting:

| transport lane, since 2026-08-02 | n | median | range |
|---|---|---|---|
| passing lanes (total job time) | 83 | 295s | 254–369s |
| wedged lanes (total job time) | 7 | 522s | 294–533s |
| wedged lanes **minus the 240s backstop** | 7 | 282s | 253–293s |

A wedged lane, with the herd's wait removed, is if anything *faster* than a passing one.
Every other test in the same job ran at normal speed. The runner-degradation hypothesis does
not survive this.

---

## 2. Measured rate, before and after 2026-09-02

F-856 reached `main` at 2026-09-02T20:03Z (`0792d5b`, PR #76 → #77). Population: every
Windows `integration` and `transport` cell of the release gate, all attempts, since the herd
test was introduced on 2026-07-31. Cells that never ran pytest (three 3-second
action-resolution failures) are excluded. A cell counts as a herd failure only if its log
shows the herd test failing or the `pytest-timeout` kill landing on it.

### 2.1 Per cell

| period | integration | transport | both |
|---|---|---|---|
| A — herd introduced, 07-31 → 08-02 | 8/20 (40.0%) | 9/20 (45.0%) | 17/40 (42.5%) |
| B — steady state, 08-02 → 09-02T20:03 | 4/67 (6.0%) | 2/67 (3.0%) | 6/134 (4.5%) |
| C — post-F-856, 09-02T20:03 → 09-05 | 2/23 (8.7%) | 5/23 (21.7%) | 7/46 (15.2%) |

### 2.2 Per gate attempt (what a developer experiences)

An attempt is red if **either** Windows cell hit the herd.

| period | rate |
|---|---|
| A — introduction | 10/20 (50.0%) |
| B — steady state | 6/67 (9.0%) |
| C — post-F-856 | 6/23 (26.1%) |

### 2.3 Is the B → C change real?

Fisher exact, two-sided:

| comparison | counts | p |
|---|---|---|
| transport cell, B vs C | 2/67 vs 5/23 | **0.011** |
| both cells, B vs C | 6/134 vs 7/46 | **0.023** |

**Significant, with an important caveat stated in §4.1: calendar and code are perfectly
confounded.** No sha lacking F-856 ran after 2026-09-02T20:03, so "after F-856" and "after
that evening" are the same set of runs. The statistic rules out chance; it does not by itself
rule out an environmental change that began the same evening.

Two facts argue against a purely environmental story. Period A (the herd's own introduction,
42.5%) and period B both contain the *identical* wedge signature, so this is not a new failure
mode — it is a pre-existing one whose rate moved. And 2026-09-04 was completely clean (0/10),
which is unremarkable at a 15% per-cell rate (0.85¹⁰ ≈ 0.20) but does mean the effect is not
a step function that started on the merge and never stopped.

### 2.4 Two other signatures, one occurrence each

Not every red is the wedge. Two singletons are worth recording because both point at the
watchdog / self-heal machinery:

- **#246 a1** (`0792d5b`, the F-856 merge commit itself, transport):
  `AssertionError: live census found [5836, 9172] for port 55120`. The boot-log oracle passed
  (exactly one backend ever wrote a log) while the live process census saw two roots. Per
  `_our_backends_on_port`'s own docstring this can be a just-spawned duplicate that has not
  written its log yet — i.e. **a heal in progress**. The herd ran only 9 seconds here.
- **#239 a2** (`689cae9`, a **pre**-F-856 branch, integration):
  `McpError: the backend on port 58728 died while 'tools/list' was in flight; the call was NOT
  retried against its replacement`. That is F-838's `PendingCalls` report firing at ~53s.

**INFERENCE, n=1 each, do not lean on it:** the one run that failed *fast* with "backend died
in flight" is on a pre-F-856 tree, and post-F-856 the equivalent situation produces a
240s silence instead. That is what §3 predicts, but a single observation is not evidence.

---

## 3. Mechanism

### 3.1 The one hard number

**OBSERVED, measured locally against a fake clock (no processes, no I/O):**

```
REUSE_PATIENCE_SECONDS = 60.0   MAX_STRETCH = 4.0
  lag x1.0  -> window spent    60.0s of WALL time
  lag x2.0  -> window spent   120.0s of WALL time
  lag x4.0  -> window spent   240.0s of WALL time
  lag x8.0  -> window spent   240.0s of WALL time   (clamped)
```

`scheduling_lag.FairWindow` charges its budget in fair seconds — elapsed divided by the lag
its own `time.sleep(0.25)` naps measured, clamped at `MAX_STRETCH`. So one
`_same_identity_backend_ready(port)` call, which before F-856 gave up after exactly 60s of
wall time, can now consume up to exactly **240.0 seconds** of wall time.

`HERD_HARD_TIMEOUT_SECONDS` is **240.0**.

These two numbers being identical is the centre of this finding. A lag factor of 2 — utterly
ordinary for a `time.sleep(0.25)` on a 2-vCPU hosted runner with twelve launcher processes
cold-starting at once — already buys 120s, four times the herd's stated 30s spec.

### 3.2 Where that window sits on the herd's path

`_same_identity_backend_ready` has four patient call sites (the two `patience=0.0` discovery
calls never nap and are unchanged by F-856, as the module intends):

| site | role |
|---|---|
| `singleton.py:541`, `:564` | the cold-start lock winner — held **across** the readiness wait |
| `singleton.py:787` | the F-820 watchdog's confirmation probe |
| `singleton.py:920` | F-843's bridge-death discriminator |

The watchdog probes every 2s and opens the confirmation phase after 3 strikes (~6s). So the
maximum time a proxy can hold an unanswered `tools/list` before it either heals or errors
went from about **66s to about 246s**. `FairWindow`'s own docstring states the trade
explicitly: *"in-flight calls simply wait while the window is stretched."*

**INFERENCE — this is the best-supported mechanism, not a confirmed diagnosis.** During the
herd, one backend serves twelve proxies on a 2-vCPU runner. It answers the cheap readiness
probe (which is what releases the proxies) but is slow enough to miss three consecutive 2s
liveness probes. A watchdog opens the confirmation phase. Before F-856 that phase ended at
~66s: the backend was condemned and healed, or confirmed busy and the cycle restarted —
either way the herd got an answer or an error inside its 240s cap. After F-856 a single
confirmation phase can absorb the entire 240s cap on its own. The herd's backstop fires
first, and the test reports `TimeoutError`.

The lock winner's path compounds it: `_start_backend_holding_lock` holds the exclusive lock
through `_same_identity_backend_ready(port)`, so a slow cold start now blocks any re-attempt
for up to 240s instead of 60s.

### 3.3 The 240s is a CENSORED observation

Every wedge stops at the test's own cap. We know the wait *exceeded* 240s; we do not know
what it would have been. The numeric identity in §3.1 is suggestive precisely because it is
unfalsifiable with the current test — which is itself the problem.

### 3.4 The other half of F-856 probably did not cause this

F-856 also moved `process_cleanup`'s orphan reap off the first-serve path onto a daemon
thread (`serve_startup.after_serving`). The obvious worry is that the reap now runs
*concurrently* with twelve proxies' first `tools/list`, contending for the GIL in the backend
process.

**Evidence against:** `_recover_orphaned_processes` iterates `_load_tracked_pids()` — the
registry — and the herd runs in a fresh isolated `HOME` where that registry is empty. There
is nothing for it to reap. And the change's direction is *toward* the backend answering
sooner, not later. This hypothesis is recorded and ranked low, not dismissed: the temp-profile
sweep does touch the shared system temp dir, and no backend-side timing evidence exists to
check it against (§5).

---

## 4. Hypotheses considered

### 4.1 "The CI runner pool degraded on 2026-09-04/05"
**Rejected as the explanation, retained as an unremovable confound.**
Against: the runner-speed control in §1.3 — wedged lanes run everything except the herd at
normal or better speed. Against: the identical wedge signature exists throughout period B and
period A, so nothing new appeared. For: calendar and code are perfectly confounded (§2.3), and
four of the seven period-C reds fall on a single day. This cannot be fully separated without
running a pre-F-856 sha on today's pool — which is cheap to do and is the first
recommendation in §6.

### 4.2 "F-856's `FairWindow` stretched the silent wait past the herd's backstop"
**Best supported.** Arithmetic ceiling measured at exactly 240.0s = the test's cap (§3.1);
rate change significant at p=0.011 on the cell where the wedge is visible (§2.3); the one
fast "died in flight" failure is on a pre-F-856 tree (§2.4). Not proven: no backend-side
evidence exists for any run (§5), so the claim that a watchdog confirmation window is what
holds the call is inference from the code path, not observation.

### 4.3 "F-856's reap-off-the-serve-path starves the backend"
**Ranked low.** See §3.4. Nothing to reap in a fresh workspace; the change's direction helps.

### 4.4 "The cold-start lock / herd serialization regressed"
**Not supported.** A lock loser is bounded by `BACKEND_READY_TIMEOUT = 120.0`, not 240s, so
it cannot produce the observed 240s wedge on its own. The lock *winner* can now hold the lock
4x longer (§3.2), which is a real aggravator but not the shape of the failure.

### 4.5 "The exclusive lock failed and two backends spawned"
**Occurred once** (#246, §2.4) and is consistent with a heal in progress rather than a broken
lock — the durable boot-log oracle saw exactly one backend. Worth watching, not the main story.

### 4.6 "The herd's fake-stdin `serve_forever` servers interfere"
**Rejected.** The three `serve_forever` threads in the timeout dump are leftover HTTP fixture
servers from earlier tests in the same session, parked in `select.select`. The herd test
spawns no such server; it spawns launcher subprocesses. They are idle bystanders.

### 4.7 "The serversplit branches inflate proxy startup cost"
**Not supported.** Adding modules would raise `_source_fingerprint` and import cost, but
`refactor/serversplit-slice0` and `slices-1-3` passed repeatedly, and `2f43dab`
(`fix/F858-...`, no split) wedged.

---

## 5. Local reproduction — NOT attempted, and why

Per the investigation's own precondition, the herd must not be run on a contended machine,
because a starvation-confounded result is worth nothing on either outcome.

Measured on this workstation immediately before the decision:

| signal | value |
|---|---|
| running `pytest` processes | 0 |
| `python.exe` processes | 161 |
| `chrome.exe` processes | 118 |
| total processes | ~2,420 |
| CPU (6 samples, 3s apart) | 57, 59, 100, 63, 59, 90 % |
| processor queue length | 1, 3, 24, 21, 0, 1 |

No pytest was running, but four sibling agents are actively working: the machine sits at
57–100% CPU with the run queue spiking to 24 on 32 logical processors. That is the precise
regime F-856 exists to handle, so a wedge here would prove nothing about CI and a pass would
prove nothing either — and starting 40 launcher processes would risk the other agents' lanes
(see the repo's own "agent fleets exhaust Windows Chrome" experience). **Skipped deliberately.**

What *was* run locally is the safe, decisive part: the `FairWindow` ceiling measurement in
§3.1, against a fake clock, no processes, no I/O. Script left at
`…/scratchpad/fairwindow_ceiling.py`.

A valid repro needs an otherwise-idle machine, `CI=1` (to get the runner's `HERD_SIZE = 12`
rather than 40), and artificial CPU load calibrated to a measured nap-lag factor of 2–4.

---

## 6. What is NOT known

1. **What the backend was doing.** No run has ever produced backend-side evidence for this
   failure. The herd's 240s path raises a bare `TimeoutError` from `asyncio.wait_for` — it
   prints no `_summary(results)`, no `workspace_backend_logs(space)`, no per-slot state. The
   workspace is `rmtree`'d in `finally`, and `release-gate.yml` uploads only
   `release-evidence` JSON and identity JSON, never the herd's `log_dir`. **Every diagnosis in
   §3 is therefore inference from code, not from a log.**
2. **How many of the twelve sessions wedged.** `asyncio.gather` surfaces one exception; the
   other eleven slots are never reported.
3. **Whether the wait would have ended after 240s**, or is genuinely unbounded (§3.3).
4. **Whether a pre-F-856 sha wedges on today's runner pool.** Nobody has tried.

---

## 7. Recommended direction

**A red test does not exist yet, and the first three items below are worth doing regardless of
which hypothesis wins.** Ordered by cost-to-value.

### 7.1 Make the wedge speak (do this first — it is a test-only change)
Wrap the `asyncio.wait_for` at `tests/test_startup_herd.py:201` so the timeout path reports
what the passing path already knows how to print:

```python
try:
    await asyncio.wait_for(asyncio.gather(...), timeout=HERD_HARD_TIMEOUT_SECONDS)
except TimeoutError:
    done = [i for i, s in enumerate(slots) if s[0] is not None]
    pytest.fail(
        f"herd wedged at {HERD_HARD_TIMEOUT_SECONDS:.0f}s: {len(done)}/{HERD_SIZE} finished; "
        f"stuck slots {[i for i in range(HERD_SIZE) if i not in done]}\n"
        f"{_summary([s[0] for s in slots])}\n{workspace_backend_logs(space)}"
    )
```

`workspace_backend_logs` already exists and is used by the neighbouring asserts. This turns
every future occurrence into evidence instead of a bare `TimeoutError`, and it settles §6.1
and §6.2 on the next red at zero risk.

### 7.2 Raise the integration cell's `pytest --timeout` above the herd's own backstop
`release-gate.yml:324` uses `--timeout=180`; the herd's backstop is 240s. The integration cell
can never report this failure by name, and the process is killed before any per-test reporting
runs (this is also why the cell emits no `junit.xml` and then trips
`release_evidence: pytest: null`, a second red that is pure consequence). Either lift it to
300 like the transport cell, or lower `HERD_HARD_TIMEOUT_SECONDS` under it. **Same defect
class as F-780.** Do not do this without 7.1, or the cell will simply report a nameless
240s `TimeoutError` instead.

### 7.3 Separate the confound (one cheap experiment)
Re-run the gate on `2da61c0` (the last pre-F-856 main) a handful of times on today's runner
pool. If it wedges at the period-C rate, the cause is environmental and F-856 is exonerated.
If it wedges at the period-B rate, §4.2 is confirmed. This is the single highest-information
action available and costs only CI minutes.

### 7.4 If §4.2 is confirmed — what I would change
**Not** by lowering `MAX_STRETCH` or making it a `STEALTH_MCP_*` knob; both fight F-856's
reasoning, and the second is explicitly ruled out by F-853's rule.

The defensible change is that **a stretched window should not be silent**. F-856 correctly
decided that a starved prober's timeout is not evidence of death — but it left the in-flight
caller with no signal at all for up to 240s. The proxy already owns a vocabulary for this
(`observability.capture_lifecycle`, and `STRETCHED_EVENT` already fires once per stretched
window). The gap is that nothing reaches the *client*. Options, in the order I would try them:

1. **Bound the total, not the window.** `MAX_STRETCH` bounds one window; nothing bounds the
   watchdog's repeated confirm-reset-confirm cycles. A cumulative cap on how long one
   generation may go unconfirmed would make the worst case statable, which today it is not.
2. **Decouple the herd's backstop from the product's ceiling.** `HERD_HARD_TIMEOUT_SECONDS`
   (240) accidentally equals `REUSE_PATIENCE_SECONDS × MAX_STRETCH` (240). Whatever the fix,
   these two numbers must not be equal, or the gate will keep measuring the ceiling rather
   than the behaviour. Derive the test's backstop from the product constants with explicit
   headroom, so the relationship is stated rather than coincidental.
3. Only then consider whether 60s of base patience is still the right number for a cold start
   as opposed to a mid-session confirmation — they are different situations sharing one
   constant.

---

## 8. Relationship to neighbouring findings

- **F-856** — the change under examination. Its fix is sound; this finding is about the
  ceiling it chose and the fact that the ceiling is invisible to the caller.
- **F-820 / F-838 / F-843** — the watchdog, the self-heal and the death-cause discriminator
  are the machinery the stretched window sits inside. §2.4's two singletons are both theirs.
- **F-780** — "legacy `test.yml` timeout below harness bounds". §7.2 is the same defect,
  in `release-gate.yml`'s integration cell.
- **F-807 / F-509** — the cold-start lock and the half-born-backend window; the herd test was
  built for these and still guards them.
- **F-779** — the format this finding follows, and the precedent that a gate-reliability flake
  gets a measured rate rather than an anecdote.

---

## 9. Disposition

Recommend: land §7.1 and §7.2 as a test/CI-only change immediately — they cost nothing, they
cannot regress the product, and they convert the next occurrence into the evidence this
finding could not obtain. Run §7.3 in parallel to break the confound. Hold §7.4 until §7.3
answers, and do not touch `scheduling_lag.py` before then.


## 9. First red WITH the §7.1 phase markers (2026-09-11, run 34627494081, PR #91 head `d38c864`)

**OBSERVED** (integration cell, Windows/X64, `--timeout=300` so the herd could report itself):

```
herd wedged at 240s: 2/12 sessions finished; stuck sessions by phase:
{0: 'initialized@6.8s, awaiting tools/list', 1: 'initialized@7.0s, awaiting tools/list',
 4: 'initialized@6.7s, awaiting tools/list', 5: 'initialized@7.0s, awaiting tools/list',
 6: 'initialized@7.0s, awaiting tools/list', 7: 'initialized@7.0s, awaiting tools/list',
 8: 'initialized@6.6s, aw...'}
2/12 sessions | initialize p50=6.86s | tools/list p50=8.42s max=8.42s
[proxy-8552.log]
2026-09-11 17:35:33,100 ERROR stealth.proxy: backend did not become ready within 120s
2026-09-11 17:35:33,100 WARNING stealth.proxy: backend became unreachable; tearing down for reconnect
[proxy-5792.log]
2026-09-11 17:35:33,132 ERROR stealth.proxy: backend did not become ready within 120s
2026-09-11 17:35:33,134 WARNING stealth.proxy: backend became unreachable; tearing down for reconnect
```

What this settles that §6 could not:

* **§3.1 is the shape, by name.** Every stuck session had completed `initialize` (answered locally by
  its proxy at 6.6–7.0 s) and was waiting on `tools/list` — the first call that reaches the backend.
* **The backend WAS serving.** Two sessions got their full `tools/list` at 8.42 s. The other ten
  never did.
* **The stuck proxies were inside the readiness gate, and its wall budget was exactly 120 s.** Their
  logs say `backend did not become ready within 120s` at 17:35:33, i.e. 120 s after they answered
  `initialize` (17:33:33). 120 s is `REUSE_PATIENCE_SECONDS = 60` × a measured lag factor of 2 — the
  first row of §3.1's table. They then tore down "for reconnect"; a second identical attempt ends at
  240 s, which is the herd's backstop, so the observation is censored again (§3.3) — but this time
  the censoring is visible rather than inferred.
* **Not the code under test.** The same tree passed the 50-session herd locally twice in a row
  (6.8 s / 6.9 s, 50/50). The PR (F-862, `session_hygiene.py`) touches only the backend's session
  list sweep, which first runs 30 s after boot; nothing in the readiness path changed.
* **Still not known:** why ten proxies' `initialize` probes saw no 200 for 120 s while two sessions
  were served at 8 s. The wedge report carried proxy logs only — no `backend-<pid>.log` section was
  found in the workspace, which is itself a datum for §7.3 (the backend's own view of those 120 s is
  what would decide between "the backend stopped answering `initialize`" and "the probes never
  reached it").

This is the §7.4 evidence the maintainer asked to see before deciding on `MAX_STRETCH`; the
decision remains theirs.

## 10. Second phase-marked red, same shape, different cell (2026-09-11, run 34652563307 attempt 1, PR #93 head `69cdc8a`)

`transport (Windows/X64)` this time — the F-862 red (§9) was the integration cell. The PR
under test changes no runtime path (it pins the dependency versions the lock already
resolved and adds a check script), so this is the wedge on an unchanged tree:

```
herd wedged at 240s: 3/12 sessions finished; stuck sessions by phase:
{0: 'initialized@5.9s, awaiting tools/list', 2: ..., 4: ..., 5: ..., 6: ..., 7: ..., 9: ..., 10: ..., 11: ...}
3/12 sessions | initialize p50=5.94s p95=5.97s max=5.97s | tools/list p50=7.06s p95=7.16s max=7.16s
[proxy-5764.log] 22:11:38,343 ERROR stealth.proxy: backend did not become ready within 120s
[proxy-7512.log] 22:11:38,976 ERROR stealth.proxy: backend did not become ready within 120s
```

Same three facts as §9: every stuck session finished `initialize` locally (5.8–5.9 s); the
backend WAS serving (three sessions got `tools/list` at 7.1 s); the stuck proxies spent the
full 120 s readiness budget (`REUSE_PATIENCE_SECONDS` × lag 2) and tore down. Nine stuck
instead of ten. The rerun of that cell alone passed; the evidence aggregator then refused
attempt 1's records ("foreign evidence: workflow.run_attempt '1' != '2'"), so the practical
recovery remains a FULL rerun of the run, as on 2026-09-04.

Two phase-marked reds, both Windows, both with 2–3 sessions served at 7–8 s while the rest
starve inside the readiness gate for exactly its budget: the readiness path, not the
backend, is where the remaining question (§7.3, the backend's own view of those 120 s)
has to be answered.

## 11. A THIRD Windows red today, and a different shape: the backend died mid-flight (2026-09-11, PR #94 head `93e62ba`, attempt 1)

`transport (Windows/X64)` again, on the release PR (docs + version pins only). This time the
herd did not wedge: 29 s after the herd started (22:53:04 → 22:53:33) one session's
`tools/list` failed with the proxy's own in-flight verdict —

```
McpError: the backend on port 55079 died while 'tools/list' was in flight; the call was
NOT retried against its replacement — reissue it if it is safe to repeat
```

— and the captured stderr shows the proxy's `post_writer` hitting `httpx.ReadError`
(`httpcore.ReadError` under it): the TCP connection to the backend dropped while the POST was
waiting for headers. Because the herd failed on an exception rather than on its 240 s
backstop, the phase report and the proxy-log excerpts were NOT emitted; this section
therefore has less to say than §9/§10 about the other eleven sessions.

What this adds to the picture:

* Three Windows reds in one day (integration on `d38c864`, transport on `69cdc8a`, transport
  on `93e62ba`) across three PRs none of which touched the readiness or serving path. On the
  same day the Linux and macOS cells ran the same herd green every time.
* Two shapes now, not one: (a) the §3.1 wedge — proxies starve inside the readiness gate for
  exactly its budget while 2–3 sessions are served; (b) a backend that stops answering an
  in-flight request within 30 s of a 12-session cold start. (b) is what `proxy_selfheal`
  reports as `CONNECTION_LOST_CAUSE`; whether the backend process actually exited, or a
  sibling proxy's teardown/heal took it down, is exactly the §7.3 question (the backend's own
  log for those seconds), which the herd only prints on the wedge path.
* Recovery is still a FULL rerun (a `rerun-failed-jobs` cannot turn the aggregate green:
  `release_evidence` refuses records from an earlier attempt).

Suggested next step for the herd test itself, no product change: on ANY failure — exception
or timeout — dump the backend log(s) and every proxy log, not only on the 240 s wedge.

## 12. The day's full count, a correction to §9-§11, and why the wedge reports never had a backend log (2026-09-11, written after the 2.1.3 release)

### 12.1 §9-§11 read an artefact of the harness as a fact about the workspace

Every wedge report in §9-§11 shows two `proxy-*.log` sections and no `backend-<pid>.log`, and §9
recorded that absence as "itself a datum for §7.3". It was not. `release_gate_harness._backend_logs`
selected **the two newest `*.log` files by mtime** — right for the single-client journey it was
written for (one backend log, one proxy log) and wrong for a herd, whose twelve proxies all write
after the backend does. The backend log was in the workspace every time; the report simply never
included it. The §7.3 question ("what did the backend see during those 120 s") was answerable on
all three occasions and was lost with the throwaway home each time.

Fixed in this PR, test-side only:

* `_backend_logs` now includes every `backend-*.log` (the per-boot logs AND `backend-boot.log`,
  which is where a crash traceback or a uvicorn exit would land), then the two newest other logs.
* New `_proxy_warnings` / `workspace_proxy_warnings`: every proxy's WARNING-or-worse lines, by
  file, capped at the newest 120 — the fleet's view, which is what tells "one proxy's connection
  dropped" from "all twelve lost the backend in the same second".
* The herd hands the same evidence block over on EVERY failure shape — the 240 s backstop, each
  assertion, and (new) an exception out of a session, which is how §11's mid-flight death ended.
  The block also carries the booted-backend census, so a heal-and-respawn (two `backend-*.log`
  files) is visible in the message.
* `tests/test_release_gate_harness_logs.py` pins the selection (a backend log survives twelve newer
  proxy logs; a journey still shows both of its logs; the digest names only proxies that warned,
  keeps the newest lines and says how many it elided).

### 12.2 §11 undercounted: BOTH Windows cells were red on the release PR

Attempt 1 of the `93e62ba` gate (PR #94) failed the herd in **both** Windows cells, not one:

| cell | shape | detail |
|---|---|---|
| transport (Windows/X64) | mid-flight death (§11) | `tools/list` failed 28 s after the herd started; the proxy's session DELETE then got "All connection attempts failed" |
| integration (Windows/X64) | wedge (§3.1) | 4/12 finished (`tools/list` at 9.1-9.3 s); 8 stuck at `initialized@7.9s, awaiting tools/list`; two proxies logged `backend did not become ready within 120s` at 23:01:47-49 |

And the `main` push run for the merge commit `b8ea954` (the 2.1.3 release, 23:27 UTC) is red too:
integration (Windows/X64) wedged again (2/12 finished, stuck at `initialized@4.9s`), and
integration (macOS/ARM64) failed `test_window_sizing.py::…::test_headed_spawn_honours_a_size_that_fits`
with nodriver's "Failed to connect to browser" (Chrome launched — F-860's reaper found five of its
pids — but CDP never attached; not the herd, not F-859). So the released commit's own CI on `main`
did not go green; the tag's publish gate did.

### 12.3 What the mid-flight verdict implies, read against the code

"the backend on port 55079 died while 'tools/list' was in flight" is `proxy_selfheal`'s
`CONNECTION_LOST_CAUSE`, and it is only reached after `_confirm_bridge_verdict` has asked
`singleton._same_identity_backend_ready(port)` — the 60-fair-second patient gate. Inside a test that
ended 28 s after it started, that gate can only have returned False on its FAST exit: no socket on
the port AND the recorded pid no longer a live `--transport` process. The proxy's own session
DELETE confirms it ("All connection attempts failed" = nothing listening). So this was a backend
PROCESS that went away, not a stalled one.

No same-workspace path can do that so early: the record is written before the socket binds
(`_start_server_process`), the lock-holder keeps the lock until the reuse gate passes, and a loser
that later finds the gate failing still waits its 60 fair seconds before `_clear_stale_backend`
evicts. That leaves the backend exiting on its own (a crash, a uvicorn lifespan failure) or a kill
from outside the workspace — and only `backend-boot.log` / `backend-<pid>.log` can say which.
Those are what 12.1 now captures.

### 12.4 Today's measured rate, all CI runs, all attempts, every herd-bearing cell

Method: for every `CI` run created 2026-09-11 (22 runs, 27 attempts), the conclusion of each
`integration`/`transport` cell and, for a red one, the `FAILED` line of its log.

| cells | red | of which the herd | rate |
|---|---|---|---|
| Windows (integration + transport) | 5 | 5 | 5/54 = 9.3 % |
| Linux + macOS | 1 | 0 | 1/81 (the macOS headed-spawn connect above) |

The same survey over 2026-09-04 → 2026-09-12 (every `CI` run, every attempt; the pool had no runs on
09-06 … 09-10) puts today next to the last busy day:

| day | Windows integration + transport cells | red | rate |
|---|---|---|---|
| 09-04 | 8 | 0 | 0 % |
| 09-05 | 44 | 4 | 9.1 % |
| 09-11 | 54 | 5 | 9.3 % |

So the per-cell rate is NOT rising: it is ~9 % on both busy days, which with two Windows cells per
run is ~17 % of first-attempt runs red from the herd alone. Linux + macOS across the same eight days:
4 red cells of 594, none of them the herd (a service-worker node and a SIGTERM node on macOS on
09-04/09-05, then the two nodriver connect failures above).

By time of day (UTC), Windows cells only: 1/38 before 22:00 (`d38c864`, §9); **4/16 between 22:07
and 23:27** (`69cdc8a` §10, `93e62ba` ×2 §11/§12.2, `b8ea954` §12.2). The evening cluster is the
strongest single datum so far for the runner-starvation reading in §3: the tree did not change in
a way that touches readiness between those runs, the shape did not change, only the hour did.

Not F-859, seen in the same survey: `b6e1b6e` attempt 1's `unit-tests (Windows/X64 py3.13)` passed
all 2342 tests and then the `release_evidence.py emit` step exited with `-2147483644`
(`0x80000004`); `bc93d02`'s `offline-stealth (Linux/X64)` errored all three stealth nodes with the
same nodriver "Failed to connect to browser" as the macOS red. Two of the day's eight red cells are
therefore "Chrome launched, CDP never attached" on POSIX hosted runners — a family of its own.

### 12.5 Operational

Every recovery today was a FULL rerun (`POST …/actions/runs/<id>/rerun`); a `rerun-failed-jobs`
turns the cell green and then fails `release-evidence` on "foreign evidence: run_attempt '1' != '2'"
(§10, and `b6e1b6e` attempt 2 shows the same on six records). The §7.4 decision on `MAX_STRETCH`
is unchanged and remains the maintainer's; what this section adds is that the next Windows red
will carry the backend's own view of the stall, which is the evidence §7.3 said the decision needs.

## 13. The first mid-flight red WITH the backend's log names the killer: the client's Job Object (2026-09-14, v2.1.4 publish run 34797141480 attempt 1)

§12.1's harness change paid off on its first Windows red. `backend-4532.log` and
`backend-boot.log` show a clean boot (`startup job 'orphans' finished 0.0s after serving
began`, uvicorn `Application startup complete`) and then nothing — no traceback, no
`Shutting down`, an empty fault log — while six proxies logged `backend connection lost`
(`httpx.ReadError` mid-request) 0.4 s after serving began, at the same wall-clock instant
the first three of twelve sessions finished `tools/list` and closed
(`tools/list p50=7.44s`, herd start ≈ 01:51:40).

That is a `TerminateProcess`-class death delivered by the client closing a session, and
§12.3's "no same-workspace path can do that so early" was right about the PRODUCT's paths
but did not consider the CLIENT's: the `mcp` Python SDK wraps every stdio server in a
`KILL_ON_JOB_CLOSE` Job Object, the backend our proxy spawns inherits it, and the end of
the spawner's session terminates the job. Verified by experiment (3/3, both the
`TerminateJobObject` and the handle-close paths) and written up as **F-867**, which also
explains why only Windows cells ever show this shape (POSIX clients `killpg` a process
group the backend is not in). The rate in §12.4 is the probability that the lock-winner's
session closes while siblings are still in flight. The §7.4 `MAX_STRETCH` question is
now moot for this shape: it is not a stall.

Attempt 2 (a full rerun, per §12.5) was green and published 2.1.4.

### 13.1 F-867 is fixed in 2.1.5 — the rate must be RE-MEASURED, not assumed (2026-09-14)

`embedded/backend_launch.py` (branch `fix/f867-backend-escapes-client-job`) now creates the
backend outside every caller job: `CREATE_BREAKAWAY_FROM_JOB` when the client's job allows
it, otherwise a one-shot Task Scheduler task (the F-810 intermediary), otherwise today's
detached spawn. The scheduler rung is gated on the spawning process already being in the
logged-on console session, so it protects real user machines and interactive runners.
`tests/test_backend_escapes_client_job.py` is RED before the change and GREEN after, on
rung `scheduler`, for both the `TerminateJobObject` and the handle-close session ends. See
F-867 §6.

Three things this section must NOT be read as saying:

* **The ~9 %/cell Windows rate is not yet known to have fallen.** It is *expected* to reach
  0 % on cells where the scheduler rung serves, because §13's mechanism is exactly what
  that rung removes. Expected is not measured. The baseline to beat is §12.4's: Windows
  herd cells 5/54 on 2026-09-12, Linux+macOS 0/81. Re-measure over the next N gate runs
  before this section claims anything: the one post-fix Windows herd cell (run
  34842210967, 4m58s, green) is a data point against a ~9 %/cell failure mode, which is
  exactly the rate at which a single pass proves nothing.
* **Which rung serves on the CI Windows cell: read, and it is `scheduler`.** PR #100's
  first gate run (`34842210967`, head `07f11a9`, 2026-09-14 12:13 UTC) passed the F-867
  escape pin on all three Windows unit cells, each emitting `F-867 pin: backend escaped
  the client job via rung 'scheduler'` for both session-end paths. The pin did not skip,
  so GitHub-hosted Windows runners do have a logged-on console session and this cell IS
  protected — the "service account with no console session" worry in F-867 §6.5 does not
  apply to this runner image. `gate / transport (Windows/X64)` passed in 4m58s.
* **The local herd time after the fix, measured.** The herd is not just a gate here,
  it is the instrument: a 50-session run on 2026-09-14 is what caught `schtasks`
  silently truncating `/TR` at 253 characters, because the only symptom was the herd
  going from ≈ 11 s to 25.8–26.0 s across three runs while every task reported success
  (F-867 §6.3). After the cap fix (855e8c6), one local 50-session run: `herd=6.7s
  warm_join=0.9s | 50/50 sessions | initialize p50=4.25s | tools/list p50=6.06s`, and the
  winner proxy's only rung line was `backend spawned via the scheduler rung` — below the
  ≈ 11 s pre-F-867 local baseline, so the scheduler round trip costs the herd nothing
  it can measure. One run; not a rate.

  <!-- TODO(F-859 §13.1): record the Windows herd rate over the N gate runs after 2.1.5
  against the 5/54 baseline. The rung question is now settled (`scheduler`, run
  34842210967); this is the rate only, and 34842210967 is a single green run. -->
* **This closes only the mid-flight death shape.** §7.3's readiness-gate starvation and the
  §7.4 `MAX_STRETCH` decision are a different failure and remain the maintainer's call; a
  red that is a genuine STALL is still a stall.
