# F-866 — The backend was never the process we detached: the Windows venv redirector re-spawned it in a kill-on-close job with a visible terminal window

**Severity: HIGH** — the shared backend serving 24 Claude Code sessions died
silently and its replacement's orphan sweep killed the browser that was open on
it. Every backend spawned on Windows since the `uv tool install` layout
(2.0.x) has had this shape; ~95 `backend-*-fault.log` files on the reporting
machine (2026-09-04 → 09-13) are backend births, most of them replacements.
**Found:** 2026-09-13, post-mortem of the 13:32 backend death.
**Status:** FIXED on branch `fix/F866-backend-bypasses-venv-redirector`.

## The incident (all timestamps 2026-09-13, local; machine = the maintainer's)

| Time | Event |
|---|---|
| 09-12 05:00:58 | backend 95556 (v2.1.3, port 52554, `win-session-1`) cold-started by a session that is gone |
| 13:27:44 | `spawn_browser` → instance `d2a98b2d…` (the only browser on the backend) |
| 13:32:16 | 95556's last log line: a routine session-hygiene tick |
| 13:32:38–46 | all 24 proxies: `probe failed 1/3 … 3/3 on port 52554` |
| 13:32:50–52 | all 24: `backend on port 52554 confirmed unusable` |
| 13:32:56 | one wins the cold-start lock (`singleton.lock` mtime) |
| 13:33:07 | backend 47284 starts on 52554 — so 95556 had released the port: dead, not wedged |
| 13:33:08–09 | 47284's `process_cleanup.recovery` kills the 22 Chrome pids recorded for `d2a98b2d…` ("Killed 1 orphaned browser processes") |
| 13:33:08 | all 24 proxies: `backend healed: re-bridging to port 52554` |

What 95556's death left behind: **nothing**. No Python traceback in
`backend-boot.log`, no uvicorn `Shutting down` (so not SIGINT/SIGTERM/SIGBREAK —
uvicorn handles all three and logs), a 0-byte `backend-95556-fault.log` (so no
C-level fault), no Application Error / WER event, no eviction line in any
proxy log (`_clear_stale_backend` is not silent). That signature — a process
that simply stops existing — has exactly two producers on Windows:
`TerminateProcess` (a job closing, `taskkill`) and `CTRL_CLOSE_EVENT` (a
console window closing: Python installs no handler for it, and the default
handler is `ExitProcess`).

## The spawn, as it actually happened

`_start_server_process` ran

    Popen([sys.executable, "-m", "stealth_chrome_devtools_mcp", "--transport",
           "http", ...], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)

In a Windows venv — and `uv tool install` builds one — `sys.executable` is
`Scripts\python.exe`, which is **CPython's venv launcher** (`PC/launcher.c`),
not an interpreter. Observed on the replacement backend:

    47284 python.exe   ← the real interpreter (4 threads, 114 MB, serves)
      └ parent 144748 python.exe   ← the redirector (1 thread, 1 MB, same argv, waits)
          └ parent 46820   ← the stdio proxy that won the lock

and `server.json` recorded **144748**, the redirector, as the backend's pid.

Three consequences, each verified on the live process (`IsProcessInJob`,
`AttachConsole` + `GetConsoleProcessList`, `EnumWindows`):

1. **A kill-on-close job.** The launcher puts its child in a Job Object with
   `KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK` (limit flags `0x3000`, read from
   inside a process started the same way). The redirector holds the only
   handle; when the redirector dies, the backend is terminated. `DETACHED_PROCESS`
   does not change the redirector's parent link, so a `taskkill /T` of the
   spawning proxy's tree reaches both.
2. **A console.** The redirector was spawned `DETACHED_PROCESS`, so it has no
   console. A console-subsystem child of a console-less parent is given a
   **new** console by Windows. `47284` has one (`conhost.exe 49696`, parent
   47284), shared with no other process.
3. **A visible terminal window.** With Windows Terminal as the default
   terminal (`HKCU\Console\%%Startup` delegation), that console is hosted by
   `OpenConsole.exe -Embedding` — spawned by Windows Terminal in the same second
   as the backend — and appears as a top-level Windows Terminal window
   (`CASCADIA_HOSTING_WINDOW_CLASS`, visible) titled
   `C:\Users\…\uv\tools\stealth-chrome-devtools-mcp\Scripts\python.exe`.
   Closing that window sends `CTRL_CLOSE_EVENT` to the backend. **That window
   is the backend.**

So the process that served every session had its lifetime tied to a
redirector nobody knew existed, and to a terminal window that looks like a
stray python process. F-839's `SIG_IGN` for SIGBREAK, and the detach flags
that were "supposed to make a console event unable to reach the backend in the
first place", were both applied to the redirector — the process that does
nothing — and never reached the process that mattered. F-839's own write-up
reasoned that a console event "can only arrive through a shared console" born
of the foreground `serve --http` path; the normal spawn path had one too.

Which of the two switches fired at 13:32:16 is not recoverable after the fact
(no kill event is logged by Windows, and the redirector's exit code is held by
nobody — `_start_server_process` discards the `Popen`). Both are the same
defect.

## Reproduction (Windows, any venv whose `Scripts\python.exe` is the launcher)

Spawn `[sys.executable, "-c", "import time; time.sleep(10)"]` with the exact
flags `_start_server_process` uses, then inspect the child's child:

| shape | pid recorded | real interpreter | in job | console |
|---|---|---|---|---|
| current: `sys.executable` (redirector) | redirector | its child | **yes** | **yes — visible Windows Terminal window** |
| fixed: `sys._base_executable` + `__PYVENV_LAUNCHER__=sys.executable` | the interpreter | itself | no | none |

`sys.prefix` and `sys.executable` inside the fixed-shape child name the venv, so
`stealth_chrome_devtools_mcp` imports from the tool's site-packages exactly as
before. `tests/test_backend_spawn_no_redirector.py` pins this table against the
real `_start_server_process`.

## The fix

`singleton._backend_interpreter()` returns `sys._base_executable` on Windows when
it differs from `sys.executable` (a venv redirector is in play), else
`sys.executable`; `_server_process_cmd` launches it. `_start_server_process` sets
`__PYVENV_LAUNCHER__` to the venv's python in the child environment when the
redirector was bypassed — the very variable the launcher sets for its own
child. CPython's `getpath` reads it to locate `pyvenv.cfg` and then removes it
from the environment before any user code runs, so the backend's Chrome
children never inherit it. POSIX venvs have no redirector (`python` is a
symlink), so nothing changes there; the OS-branch pins in
`tests/test_singleton_backend_logging.py` are unchanged except that the command's
interpreter is now `_backend_interpreter()`.

After the fix the detached, own-process-group flags apply to the process that
serves: no redirector, no kill-on-close job, no console, no terminal window, and
`server.json`'s recorded pid is the serving process.

## What this does NOT fix (named, not hidden)

* A backend restart still **kills** the browsers the dead backend owned
  (`process_cleanup.recovery` on startup) instead of re-adopting them. A
  long-running browser session survives only as long as the backend does; the
  fix makes the backend survive its spawner and the desktop, not an upgrade or
  a deliberate `restart`.
* A source-change eviction (a new session's proxy seeing a different
  fingerprint after `uv tool install`) terminates the running backend — and
  therefore its browsers — at whatever moment that next session starts.
* A `taskkill /T` of the spawning proxy's tree still reaches the backend by its
  parent link. Nothing observed does that; if it ever does, the answer is a
  spawn through a job-free intermediary (the `desktop_launch` Task Scheduler
  round trip already is one), not more flags.
