# F-1023 — a cold-started backend nobody adopts idles forever (T-5655)

Status: fixed. Class: resource leak, found during the T-5587 headed check.

## Defect

A stdio proxy's cold start spawns the shared backend (`backend_launch.spawn`; on
Windows the rungs are breakaway, Task Scheduler, plain). If the proxy is gone before the
backend is serving, the backend is left running with no client. On the Task Scheduler
rung this is the common shape: the task runs the launcher at BelowNormal and Low I/O
(F-932, F-1004), so a loaded box takes a long time to boot it.

Reproduced 2026-10-21 in an isolated HOME (`USERPROFILE`/`HOME` pointed at a temp dir,
`STEALTH_MCP_NO_ERROR_REPORTING=1`): `python -m stealth_chrome_devtools_mcp` was started,
held 45 s and closed. Result: the proxy exited rc 0; the backend it spawned on port 19222
(pid 175580) finished booting 41 s after the spawn on a swapping machine, listened, and
was still running idle after the proxy was gone: ~139 MB, no session, no browser, nothing
that would ever stop it. (The Windows breakaway rung served in that run, the same as a
scheduled spawn from the point of view of this defect: only the spawn differs.)

Why nothing ended it: the only idle mechanism in the tree is the browser idle reaper
(`browser_manager._run_idle_reaper`), which reaps idle BROWSERS and is disabled by default;
`session_hygiene` reaps abandoned MCP sessions, not an unused backend; the liveness
machinery (`backend_liveness`, `backend_watchdog`, `backend_eviction`) only judges
backends a proxy is talking to, or evicts a stale one when a NEW proxy starts.

## Fix

Reuse `session_hygiene` (it already owns the question "did a client ever come, and is it
gone"), no second reaper:

* `HygienicSessionManager.ever_served`: set by `handle_request`, i.e. any request that
  reaches `/mcp`. The health route (`/health`, F-960) deliberately does not count: it is
  what a watchdog asks, and the orphan has no watchdog.
* `session_hygiene.exit_if_unadopted(has_browsers, stop)`: after
  `UNADOPTED_AFTER_SECONDS` (the same 300 s as `ABANDONED_AFTER_SECONDS`, one definition
  of "gone, not thinking"), stops the process iff NO request ever reached `/mcp` AND the
  backend hosts no browser (an adopted persistent-profile browser, F-888, is a reason to
  stay). An adopted backend is never touched however quiet it later goes. Any unanswerable
  question keeps the backend.
* `--exit-if-unadopted` on the backend argv, passed by the cold start
  (`singleton._server_process_cmd`) and so also by `restart_backend`; `build_arg_parser`
  accepts it, `__main__` stamps `_EXIT_IF_UNADOPTED`, and `app_lifespan` (http, once per
  process) starts the watch. A backend an operator starts with `serve --http` does not
  get the flag and behaves as before.
* The stop is `signal.raise_signal(SIGTERM)`: the handler chain (`process_cleanup` then
  uvicorn's) runs, uvicorn unwinds and tracked browsers are cleaned exactly as for any
  other stop.

## Evidence

* The repro above, with the pid and the idle backend still serving after the proxy died.
* `tests/test_unadopted_backend_exit.py` and two tests in
  `tests/test_lifespan_reentrancy.py` (RED on the old code, GREEN now): an unreached,
  browser-less backend stops; one reached even once, or hosting a browser, or whose browser
  question fails, or with no manager, does not; the grace is waited first; the grace is the
  abandonment window; the cold-start argv carries the flag and an operator parse does not;
  the lifespan arms the watch once and only when asked.
* Real backend, isolated HOME, grace shortened by patching the module constant: see the
  PR body.

## Not done, on purpose

No env knob and no second reaper. The grace is not applied to a backend that has served a
client: that policy (exit when the LAST session leaves) would turn every Claude Code restart
into a cold start and is not what the task asked.
