# F-867 — The shared backend inherits the MCP client's Job Object around the proxy that spawned it, and dies when that one client session ends

**Severity: HIGH** — on Windows, any MCP client that wraps its stdio server in a
kill-on-close Job Object (the reference `mcp` Python SDK does, unconditionally; the
startup-herd test is such a client) kills the backend shared by EVERY session the
moment the session whose proxy spawned it ends. The other sessions see the
"backend died mid-flight" shape (F-859 §11): a lost connection with no traceback, no
uvicorn shutdown, an empty fault log.
**Found:** 2026-09-14, from the first Windows herd red that carried the backend's own log
(F-859 §12's harness change), v2.1.4 publish run 34797141480 attempt 1.
**Status:** FIXED — branch `fix/f867-backend-escapes-client-job` (PR #TBD), ships in 2.1.5.
Fix: `embedded/backend_launch.py`, the one home for how a backend is created — a
three-rung Windows spawn (`breakaway` → `scheduler` → `plain`) that puts the backend
outside every caller job when the spawner is in the logged-on console session (§6).

## 1. The evidence (run 34797141480 attempt 1, `gate / transport (Windows/X64)`, tag v2.1.4 = main `f9a4cf9`, WITH F-866)

Twelve cold sessions, one backend. The backend's own log:

```
01:51:47,028 backend process starting (pid=4532 …)
01:51:47,075 server.startup: Starting Browser Automation MCP Server...
01:51:47,091 startup job 'orphans' finished 0.0s after serving began
```

and `backend-boot.log` ends with uvicorn's `Application startup complete` /
`Uvicorn running on http://127.0.0.1:50180`. Nothing after that: no traceback, no
`Shutting down`, a 0-byte `backend-4532-fault.log`. Then, 0.4 s later, every proxy at
once:

```
01:51:47,502-556  proxy-7920/6832/6900/8272/3848/4900: backend connection lost   (httpx.ReadError mid-request)
01:51:51,6xx      … backend on port 50180 confirmed gone after a lost connection
```

The herd's phase markers: `3/12 sessions finished` with `tools/list p50=7.44s` — three
sessions completed their `tools/list` and their `async with Client(...)` block EXITED at
about herd+7.4 s, which is 01:51:47.5 on the wall clock. The backend died at the instant
the first client sessions closed.

## 2. The mechanism (verified)

`mcp/os/win32/utilities.py` (`create_windows_process`) creates every stdio server inside
a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and no `BREAKAWAY_OK`, and assigns
the server process to it right after `Popen`. Our stdio proxy is that server. When the
proxy wins the cold-start lock and spawns the backend, the backend is created INSIDE that
job — Job Object membership is inherited by children, and `DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP` say nothing about jobs. When the client's session ends it either
calls `TerminateJobObject` (`terminate_windows_process_tree`, if the proxy has not exited
within its grace) or simply drops the last job handle; with `KILL_ON_JOB_CLOSE` both
terminate every member, and the backend serving eleven other sessions is a member.

Reproduced 2026-09-14 (`$TEMP/f867_job_inherit.py`, Windows, main `f9a4cf9`): a job
built exactly as the SDK builds it, a child assigned to it that calls the real
`singleton._start_server_process` (module swapped for a sleeper), then either
`TerminateJobObject` or `CloseHandle` only:

| session-end action | proxy | backend spawned by it |
|---|---|---|
| `TerminateJobObject` | dead | **dead** (3/3 runs) |
| `CloseHandle` only (`KILL_ON_JOB_CLOSE`) | dead | **dead** (3/3 runs; one earlier run survived by a spawn-before-assign race) |

F-866 did not and could not address this: it removed the launcher's OWN job (the one the
venv redirector created) and the console, but a job the CLIENT wraps around the proxy is
outside the product's reach from inside — `CREATE_BREAKAWAY_FROM_JOB` is refused
(`ERROR_ACCESS_DENIED`) unless the job carries `BREAKAWAY_OK`, which the SDK's does not.
F-866's write-up named the residual: "a `taskkill /T` of the spawning proxy's tree still
reaches the backend by its parent link … the answer is a spawn through a job-free
intermediary". This finding shows the job, not only the tree walk, is real and fires in CI.

## 3. What it explains

* **F-859's Windows-only herd rate (~9 % per cell, 0 % on Linux/macOS).** POSIX clients
  use `killpg` on a process GROUP; the backend is spawned with `start_new_session=True`
  and is in its own group, so it survives. Only Windows has the job. The rate is the
  probability that the lock-winner's session is among those that close while others are
  still mid-`tools/list`.
* **Local mass disconnects when a Claude Code session exits.** Claude Code logs
  `Terminating MCP server process tree` at session end. Whether it uses a job or a tree
  walk, the backend spawned by that session's proxy is inside both. 2026-09-13 19:49: every
  stealth process on the machine, backend included, vanished in the same second with no
  proxy logging a strike — consistent with the sessions closing and taking the backend
  with them. Not proven for the node client (no job/tree flag was captured); the Python
  SDK case is.

## 4. Fix candidates (maintainer's call) — TAKEN: (2) then (1), in that order as rungs

1. **Spawn the backend through a job-free intermediary on Windows.** The product already
   owns one: `desktop_launch`'s Task Scheduler one-shot (`_schtasks`, F-810) launches a
   process under the Task Scheduler service, outside any caller job and outside the
   caller's process tree, in the interactive session. Cost: the environment and the
   command must be handed over explicitly (the task does not inherit `os.environ`;
   `/TR` is capped near 261 chars, F-810 already solved this with a launcher script),
   `schtasks` must be available (it is on every Windows SKU; CI runners run as a service
   account — a headless runner needs the fallback below), and the boot-log redirect
   (F-303) must be re-done by the intermediary since `Popen`'s stdout handle cannot cross.
2. **Try `CREATE_BREAKAWAY_FROM_JOB` first, fall back to the current spawn.** Cheap, and
   correct for clients whose job sets `BREAKAWAY_OK`; a no-op for the Python SDK's job
   (refused) and unknown for Claude Code's. Worth having as the first rung either way.
3. **Do nothing in the product; make the proxy exit promptly on stdin EOF** so
   `TerminateJobObject` is never reached. Does NOT help: `KILL_ON_JOB_CLOSE` fires on the
   handle close regardless (table above, row 2).

The candidate that fixes the verified case is (1), with (2) as a free first rung. It
touches `singleton._start_server_process` only through a new leaf (`backend_launch.py`?)
so the reuse gate, adoption order and cold-start lock stay where they are.

**Taken:** candidates (1) and (2), as rungs in that order — (2) `CREATE_BREAKAWAY_FROM_JOB`
first because it is free, then (1) the Task Scheduler intermediary, then today's spawn as
the last rung so a runner that has neither still starts a backend. Candidate (3) was
rejected on the evidence in §2's table: the handle-close row fires regardless of how
promptly the proxy exits. §6 is the fix as built; §5's pin is written and green.

## 5. Test that must exist before a fix

A Windows-only pin, hermetic: build the SDK's job (no `win32job` dependency — `ctypes`
`CreateJobObjectW` / `SetInformationJobObject` / `AssignProcessToJobObject`), assign a
helper process to it, have the helper call the real `_start_server_process` with the
module swapped for a sleeper, close the job, assert the sleeper is alive and NOT a member
of the job (`IsProcessInJob(child, job_handle)` — the job handle is ours, so this is the
precise query F-866's pin could not make). RED today on the table above.

## 6. The fix (branch `fix/f867-backend-escapes-client-job`, 2.1.5)

### 6.1 Where it lives

A new leaf, `src/stealth_chrome_devtools_mcp/embedded/backend_launch.py` (664 LOC at `1c9eed3`):
**THE one home for "spawn the backend where no MCP client's Job Object can reach it"**.
Nothing else in the tree may create a backend. `singleton._start_server_process` keeps
everything that is not the creation itself — the command (`_server_process_cmd`,
`_backend_interpreter`'s F-866 base-interpreter choice), the child environment, F-830's
boot-log roll, and the `PORT_FILE` / `server.json` record afterwards — and makes exactly
one call:

```python
launched = backend_launch.spawn(cmd, child_env, boot_log)   # -> Launched(pid, rung)
```

`singleton.py` went 994 → 979 LOC and no longer imports `subprocess`; the reuse gate,
adoption order and cold-start lock are untouched, as §4 required.

It is a leaf: `backend_registry` for the state dir, and `desktop_launch` reached lazily
for the ONE `schtasks` seam, the ONE pid-file reader and the ONE task teardown, so none
of those gets a second home. `desktop_launch` in turn now imports nodriver and
`requests` lazily, inside the two delegation functions that actually use them, so that
reaching those seams is cheap: the stdio proxy does NOT import `browser_manager`, so a
module-level nodriver import there landed in every proxy's cold start to serve a browser
path the proxy never takes. Measured with `-X importtime`: reaching the seams costs
**≈ 175 ms warm, down from ≈ 470 ms**. An earlier draft of this write-up claimed the
proxy had already loaded nodriver by then, and used that to argue the import was free;
that was false in both halves, and the lazy import is the fix rather than the excuse.

### 6.2 The rungs

POSIX is one rung, `posix` — `start_new_session=True`, exactly as before, because §3
established POSIX was never exposed. Windows climbs three:

| rung | what it does | when it serves |
|---|---|---|
| `breakaway` | `CREATE_BREAKAWAY_FROM_JOB` on the detached spawn, PROVEN to have left every job | the caller is in no job, or in a single job that carries `BREAKAWAY_OK` |
| `scheduler` | a one-shot Task Scheduler task creates the backend | the spawner is in the logged-on console session and a non-redirector `pythonw.exe` exists |
| `breakaway-partial` | the rung-1 child is KEPT although it is still in some job | a nested job chain freed the innermost job only AND no scheduler rung is available — logged at WARNING |
| `plain` | today's `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP`, plus the breakaway bit if rung 1 proved it permitted | none of the above — the backend is inside the client's job, and the log says so |

**Rung 1 is not trusted on its return value.** This is the one thing the implementation
learned that §4 did not know: measured on this branch, under a NESTED job chain the flag
leaves only the INNERMOST job. A proxy started through a console-script launcher whose
own job permits breakaway therefore gets a *successful* `Popen` and a backend still
inside the client's outer job. So the rung is accepted only when the new process is
proven to be in **no job at all** — `IsProcessInJob(child, NULL)` asked through the
`Popen`'s own handle, never by reopening the pid, because Windows reissues pid numbers.
An unanswerable query is not proof and leaves the spawn standing, with a WARNING: the
check exists to catch a partial breakaway, not to invent a reason to throw away a
backend that was probably fine. The SDK's own job refuses the flag outright with
`ERROR_ACCESS_DENIED` (§2), which drops a rung; any other `OSError` is a real spawn
failure and propagates.

**A partial escape is not discarded blindly.** A child that got out of one job is
strictly better than one that got out of none, so rung 2's viability is decided FIRST
(`_scheduler_plan`, before anything is killed). Scheduler available → the partial child
is discarded at an age where it has bound nothing, and the scheduler runs. Scheduler
unavailable → the child is KEPT and returned as the `breakaway-partial` rung, at
WARNING, saying exactly what it is: out of the innermost job, an enclosing kill-on-close
job would still take it, and nothing here can do better. Symmetrically, once rung 1 has
proven breakaway PERMITTED, rung 3 asks for it too — on a client whose job allows it,
that is the difference between escaping one job and escaping none.

**Rung 2 pays §4's four costs explicitly, plus two §4 did not foresee.**

* *`pythonw.exe`, and why not PowerShell.* The intermediary must have no console, or a
  window flashes on every cold start — that is the whole reason it is `pythonw` and not
  `python`. PowerShell was rejected on three counts: `Start-Process` would put a console
  window on screen unless hidden by yet another wrapper; PowerShell cannot merge a
  child's stdout and stderr into ONE append handle, which is what F-303 needs
  (`backend-boot.log` must receive an import-time traceback); and the pid it could hand
  back would be the wrapper's, not the interpreter that serves — which is precisely the
  bug F-866 just fixed. It must also be the pythonw beside the BASE interpreter: a
  venv's `Scripts\pythonw.exe` is the redirector, and running the launcher under it
  would rebuild F-866's kill-on-close job around the backend. When only a redirector is
  available, the rung is dropped rather than reintroduce that.
* *Env and command hand-off.* A scheduled task inherits nothing. The ENTIRE child env
  dict — as the caller built it, nothing hand-picked — plus the argv and the boot-log
  path travel in a JSON spec beside the launcher, in the user-private
  `~/.stealth-mcp/backend-launch/` — and the working directory too, captured in `spawn`
  rather than in the launcher, because every other rung inherits it for free and only
  this one has to be told. That is not cosmetic: a scheduled task starts in `system32`,
  and `clone_storage`'s last-resort clone seed reads the spawner's cwd, so a backend
  launched from the wrong directory would seed a clone from the wrong place. The command line carries two paths only and the launcher
  addresses its spec, pid and error files off its own path, because `/TR` has a hard
  length limit. The task and every scratch file are deleted in a `finally`.
* *The `/TR` limit is 253, not "~261", and it fails silently.* Measured 2026-09-14 on
  Windows 11 10.0.26200: `schtasks` stores at most **253** characters of `/TR`, and a
  longer command is truncated to its first 253 **with exit code 0** — no error, no
  warning, nothing a caller can branch on. The stored task then fails at run time with
  Last Result 2 (file not found), the launcher never runs, the 20 s pid deadline
  expires, and the spawn quietly drops to the `plain` rung — which is to say the whole
  fix silently switches itself off. §4 above repeats F-810's documented "~261"; that
  figure is wrong by eight characters and the guard is now 253. To buy headroom the
  per-attempt token also shrank from 32 hex characters to **12**, so task names are
  `stealth-mcp-backend-<12 hex>` and scratch files `<12 hex>.py/.json/.pid/.err`.
* *The orphan sweep, and the predicate that would not have worked.* A spawner killed
  mid-launch leaves its task and its scratch behind, so a sweep runs before each
  scheduler spawn. It is a FILESYSTEM sweep: every `<token>.json` spec in
  `~/.stealth-mcp/backend-launch/` older than twice the 20 s pid deadline has its
  `stealth-mcp-backend-<token>` task deleted BY NAME and its files removed. The obvious
  alternative — `schtasks /Query` and delete tasks whose spec is absent — was written
  first and is wrong: `_cleanup` deletes the task before the files, so a killed spawner
  leaves task AND spec, which is the only orphan class that exists and the exact one
  that predicate cannot see. The age fence is what makes it safe for a herd: a live
  sibling's spec is younger than the deadline and is never touched. A clean directory
  costs zero `schtasks` calls.
* *The boot log and the pid.* A `Popen` stdout handle cannot cross the scheduler, so the
  launcher re-opens the rolled boot log itself and hands the backend stdout AND stderr —
  F-303's property survives. It then writes the backend's real pid through a temp file
  and `os.replace`, so the poller can never read half a number, and so the pid recorded
  in `server.json` is the interpreter that serves (F-866), never the launcher's. The
  poller's deadline is 20 s.
* *The session gate, and why it is a gate and not a preference.* Task Scheduler places a
  "run only when the user is logged on" task in the CONSOLE session. Taking this rung
  from anywhere else would silently move the backend to another desktop and make the
  display context `singleton` records a lie. So `_same_session_as_console` compares this
  process's session id with the active console session and the rung is taken only on
  equality — the OS then puts the backend exactly where it already was. F-808's ruling
  holds: the tool still never PICKS a session. A refusal is a WARNING naming both ids
  and the rung it drops to.

Everything is logged on `stealth.proxy`, not on a logger of this module's own, because
`configure_logging("proxy")` installs the file handler on that exact name with
`propagate = False` — a `stealth.backend_launch` record would reach no handler and the
rung would be invisible in the one log a post-mortem reads. The rung that served goes
out through the single `_log_rung` line, in one spelling, at INFO; the two that mean
"this backend is not actually safe" — `breakaway-partial`, and every refusal of the
session gate — are WARNING and name F-867.

### 6.3 Evidence

`tests/test_backend_escapes_client_job.py` is §5's pin, built as §5 specified —
`ctypes` only, no `win32job` dependency, and the membership query uses OUR OWN job
handle, which is the precise question F-866's pin could not ask (Claude Code's tool
shell is itself inside a job, so `IsProcessInJob(h, NULL)` is True for anything spawned
from a tool).

| | result |
|---|---|
| before the fix | **RED** — `assert not member` failed: "the backend was created INSIDE the client's job" |
| after the fix | **GREEN**, both `close_handle` and `terminate_job` parametrizations, ~2.4 s |
| rung that served locally (Claude Code's tool-shell job, this machine) | `scheduler`, 0.78 s |

The pin reads the rung off the `stealth.proxy` INFO line, asserts it BEFORE the escape
assertions — a plain spawn cannot pass them, and a red reading "rung 'plain' served"
explains itself where "the backend died" would not — and `warnings.warn`s it, because
pytest prints the warnings summary for passing tests too, so every gate cell's log will
say which rung served there. On a runner with no logged-on console session the test
`pytest.skip`s loudly: the scheduler rung cannot exist there, so the fix is *unverified*
on that cell rather than falsely green.

**The startup herd is what caught the `/TR` truncation, and no unit test would have.**
A 50-session herd run on 2026-09-14 produced a 255-character command, which `schtasks`
stored as its first 253 characters and reported success for. Every session then paid the
20 s pid deadline before falling to `plain`:

| run | herd wall time |
|---|---|
| baseline (rung serving normally) | ≈ 11 s |
| the three truncated runs | 25.8 s, 25.9 s, 26.0 s |

The task's Last Result was 2, file not found. Nothing in the product logged an error,
because there was no error to log — the failure's whole signature was a herd three times
slower than it should be, which is exactly the shape F-859 exists to measure. This is the
second time on this branch that a silent success has had to be turned into a measured
fact (the first was rung 1's "successful" partial breakaway).

`tests/test_backend_launch.py` is the hermetic unit tier: rung order, the env/cwd
hand-off, `/TR` within the 253-char limit, cleanup on every path, the age-fenced sweep
(including that a young sibling's spec survives it), the POSIX
branch, and the launcher script executed for real including an import-time crash landing
in the boot log. SOFT goldens moved deliberately in the same change: the Windows
`creationflags` equality asserts now include the breakaway bit, and six test files
re-point `singleton.subprocess.Popen` → `subprocess.Popen` because `singleton` no longer
imports `subprocess`. F-866's real-process pin still passes, now via rung 2, with
`children() == []`.

**The one red on that gate run was a Windows-only-verification gap, the second on this
branch.** `34842210967` was red ONLY on the POSIX unit and coverage cells, and only in
`tests/test_backend_launch.py`: the rung tests are about Windows behaviour and ran on
Linux and macOS, where a faked `winerror=5` cannot exist. They are being made
Windows-only — a test-only change, no product code. The reason it reached CI at all is
structural and worth naming: F-867's own escape pin is Windows-only, so the leaf's unit
tests were never exercised on POSIX locally, and a Windows-green local run said nothing
about them. This is the same shape as the earlier gap on this branch, and the standing
lesson holds — all-POSIX-red with Windows-green is platform divergence, not flake.

### 6.4 What CI and production measured, and what is still a prediction

**Which rung serves under the real Claude Code client: read, and it is `breakaway`.**
2.1.5 went live on this machine at 23:05 local on 2026-09-14, when Claude Code
reconnected its MCP servers and 56 proxies (one per session) started within 100 s of
each other against the freshly upgraded tool venv. The lock winner, proxy 5732, wrote:

```
2026-09-14 23:05:48,339 INFO 5732 [-] stealth.proxy: backend spawned via the breakaway rung (pid 53836)
```

and an independent `IsProcessInJob` read, taken 12 minutes later from an unrelated
process, agreed: backend 53836 is in **no** job, while proxy 5732 and both trampoline
ancestors above it (45444, 11980) are **in** one. So §6.5's open question about Claude
Code's own node client is answered by measurement: it DOES put the proxy in a Job
Object, and that job PERMITS breakaway — rung 1 serves in production, and the
scheduler rung, which is what the SDK's job forces in the pin and on CI, is never
needed under Claude Code. `~/.stealth-mcp/backend-launch/` was empty and
`schtasks /Query` listed zero `stealth-mcp-backend-*` tasks afterwards, as rung 1
leaves nothing behind. The same herd produced one probe strike (`probe failed 1/3`)
on 27 of the 56 proxies at 23:06:49–50, the moment a 4.0 s `spawn_browser` held the
backend; no proxy reached 2/3, nothing was condemned, and the backend stayed up.

**Which rung serves on CI: read, and it is `scheduler`.** PR #100's first gate run
(`34842210967`, head `07f11a9`, 2026-09-14 12:13 UTC) ran the escape pin on all three
Windows unit cells — py3.11, py3.12, py3.13, runner root `D:\a\…` — and every one of
them PASSED and emitted, for both the `close_handle` and `terminate_job`
parametrizations:

```
UserWarning: F-867 pin: backend escaped the client job via rung 'scheduler'
```

Two things follow, and the first was genuinely open until now. GitHub-hosted Windows
runners DO have a logged-on console session, so `_same_session_as_console` passes and
the scheduler rung is available there; the pin did NOT skip, which means these cells
verify the fix rather than merely failing to contradict it. §6.5's worry about a service
account with no console session, and the harness-side `BREAKAWAY_OK` follow-up it
proposed, do not apply to this runner image. Rung `breakaway` served nowhere: the
runner's client job, like the SDK's, does not permit it.

**The Windows cells are green.** `gate / transport (Windows/X64)`, the twelve-session
herd cell, passed in 4m58s; `integration (Windows/X64)` in 12m57s; `coverage
(Windows/X64)` passed.

**The herd RATE is still a prediction.** One green run is a data point, not a rate.
F-859 §12.4's baseline is Windows herd cells 5/54 (2026-09-12) against Linux+macOS
0/81, and a ~9 %/cell failure mode is not disproved by a single pass — expected 0 %,
to be re-measured over the next N gate runs.
<!-- TODO(F-867 herd): record the Windows herd rate over the N gate runs after 2.1.5 against the 5/54 baseline. Expected 0 % where the scheduler rung serves. Still NOT asserted; 34842210967 is one run. -->

So: a mechanism that **explains** the Windows-only herd rate, a pin that **proves** the
backend survives the client's job on three CI cells and locally, and a rate that remains
unmeasured.

### 6.5 What is NOT fixed

* **A spawner outside the console session.** SSH, a Windows service, session 0, and some
  RDP layouts fall to `plain` (or `breakaway-partial`, if the client's job allowed a
  partial escape) and remain exposed as before. This is deliberate: the alternative is
  moving the backend to a desktop the caller did not ask for. This section previously
  worried that CI was such an environment and proposed a harness-side `BREAKAWAY_OK`
  follow-up; §6.4 measured it and the worry does not apply — GitHub-hosted Windows
  runners have a logged-on console session and the scheduler rung serves there. The
  follow-up is therefore NOT needed, and the exposure is limited to real
  non-console-session hosts.
* **Claude Code's own node client** — now measured (§6.4, 2026-09-14 23:05 local): it
  DOES wrap the proxy in a Job Object, and that job permits `CREATE_BREAKAWAY_FROM_JOB`,
  so rung 1 serves there. What its tree flags are on session end (kill-on-close or not)
  is still not captured; it no longer matters for the backend, which is outside the job
  either way, but §3's local mass-disconnect observation stays consistent-with, not
  proven.
* **A backend that dies for any other reason** — an upgrade's source-change eviction, a
  deliberate `restart` — still KILLS the browsers it owned on the way back up rather
  than re-adopting them. Unchanged from 2.1.4, and unrelated to the job.
* ~~**`desktop_launch._run_task` has no `/TR` length guard at all**~~ — **CLOSED**, see
  `finding_F879_desktop_launch_tr_guard.md`. It was the same silent truncation on the
  same API one function away: a deep enough state dir would push it past 253 and produce
  the same exit-0 success, the same Last Result 2 and the same absence of any error to
  log, surfacing as a 20 s timeout blaming the DevTools port. The guard landed as its own
  finding, as this bullet asked. Note what moved with it: `TR_MAX_CHARS` and
  `TOKEN_CHARS` are **no longer defined in `backend_launch`** — they live beside
  `_schtasks` in `desktop_launch` (with `tr_overflow`, the one home for the comparison
  too), and this module's scheduler rung reads them there at call time, so there is one
  measured 253 in the tree rather than two that could drift. The two paths still differ
  on what over-length MEANS: a rung boundary here, a raised `ToolError` there, because
  the headed hand-off has no fallback rung and this one does.
