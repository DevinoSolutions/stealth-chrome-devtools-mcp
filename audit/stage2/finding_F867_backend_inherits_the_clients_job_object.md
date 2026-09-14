# F-867 — The shared backend inherits the MCP client's Job Object around the proxy that spawned it, and dies when that one client session ends

**Severity: HIGH** — on Windows, any MCP client that wraps its stdio server in a
kill-on-close Job Object (the reference `mcp` Python SDK does, unconditionally; the
startup-herd test is such a client) kills the backend shared by EVERY session the
moment the session whose proxy spawned it ends. The other sessions see the
"backend died mid-flight" shape (F-859 §11): a lost connection with no traceback, no
uvicorn shutdown, an empty fault log.
**Found:** 2026-09-14, from the first Windows herd red that carried the backend's own log
(F-859 §12's harness change), v2.1.4 publish run 34797141480 attempt 1.
**Status:** OPEN — root cause verified by experiment; fix approach needs the maintainer's
call (§4).

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

## 4. Fix candidates (maintainer's call)

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

## 5. Test that must exist before a fix

A Windows-only pin, hermetic: build the SDK's job (no `win32job` dependency — `ctypes`
`CreateJobObjectW` / `SetInformationJobObject` / `AssignProcessToJobObject`), assign a
helper process to it, have the helper call the real `_start_server_process` with the
module swapped for a sleeper, close the job, assert the sleeper is alive and NOT a member
of the job (`IsProcessInJob(child, job_handle)` — the job handle is ours, so this is the
precise query F-866's pin could not make). RED today on the table above.
