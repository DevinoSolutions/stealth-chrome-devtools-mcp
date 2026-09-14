# PROMPT — Fix F-867 end to end (RED pin → job-free backend spawn → PR → merge → release 2.1.5 → tool upgrade)

You are working in the repo `stealth-chrome-devtools-mcp` (GitHub `DevinoSolutions/stealth-chrome-devtools-mcp`),
local checkout `C:\Users\amind\OneDrive\Desktop\Projects\CUSTOM MCPs & PRODUCTIVITY\stealth-chrome-devtools-mcp`.
`main` is at `701c1db` (2.1.4 released and installed as the uv tool). Working tree is clean. No branch exists yet.

The maintainer's instruction, verbatim: **"Alriht continue lets get this done end to end."** That is the go-ahead to
fix F-867 completely without further questions: RED pin first, implement the fix, PR, gate green, merge, release 2.1.5,
publish to PyPI, upgrade the local uv tool, update memory, report. Do not ask for approach approval — the approach is
decided below (§3). Confirmed defects need no go-ahead at any step (fix + test + PR + merge + release).

Read first (in this order, do not skip): `CLAUDE.md` (navigation map + four conventions), then
`audit/stage2/finding_F867_backend_inherits_the_clients_job_object.md` in full, then
`audit/stage2/finding_F866_backend_spawned_through_venv_redirector.md`, then the code named in §4.

---

## 1. The defect (F-867, verified 3/3 by experiment)

`mcp/os/win32/utilities.py::create_windows_process` (the reference Python MCP SDK) wraps EVERY stdio server in a
Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and NO `BREAKAWAY_OK`, assigned right after `Popen`.
Our per-session stdio proxy is that server. When the proxy wins the cold-start lock and spawns the shared HTTP backend
via `singleton._start_server_process`, the backend is created INSIDE that job (job membership is inherited;
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` say nothing about jobs). When that ONE client session ends, either
`TerminateJobObject` (`terminate_windows_process_tree`) or a plain last-handle `CloseHandle` kills every member —
including the backend serving every other session. Symptom shape: "backend died mid-flight" (F-859 §11) — connection
lost, no traceback, no uvicorn shutdown, empty fault log.

Evidence: v2.1.4 publish run `34797141480` attempt 1, `gate / transport (Windows/X64)`: backend pid 4532 booted
01:51:47.028–.091, connection lost 01:51:47.5, exactly when the first 3/12 herd sessions closed (`tools/list p50=7.44s`).

Experiment (`$TEMP/f867_job_inherit.py`, uses `win32job`; the pin must NOT — ctypes only):

| session-end action | proxy | backend spawned by it |
|---|---|---|
| `TerminateJobObject` | dead | **dead** (3/3) |
| `CloseHandle` only (`KILL_ON_JOB_CLOSE`) | dead | **dead** (3/3) |

`CREATE_BREAKAWAY_FROM_JOB` is refused with `ERROR_ACCESS_DENIED` unless the job has `BREAKAWAY_OK` (the SDK's does not).
POSIX is immune (`killpg` on the process group; backend uses `start_new_session=True`) — which is exactly why the herd
reds are Windows-only (~9 %/cell, F-859 §13). Claude Code logs "Terminating MCP server process tree" at session end —
the node client's job/tree flags were not captured; the Python SDK case is proven. Candidate "exit promptly on stdin
EOF" does NOT help (row 2 fires on handle close regardless).

Environment landmine: **Claude Code's own tool shell runs inside a Job Object**, so `IsProcessInJob(h, NULL)` is
always True for anything you spawn from a tool. F-866's pin therefore made only a relative assertion. The F-867 pin
must query with OUR OWN job handle (`IsProcessInJob(child, job_handle)`), which is precise regardless of the harness.

---

## 2. Standing constraints (all still in force — violate none)

- Never `--no-verify`. Never skip hooks or signing.
- Tests never write to the real `~/.stealth-mcp` (monkeypatch `singleton.PORT_FILE`, `_ensure_state_dir`,
  `_server_version`, `_write_server_state`, `STEALTH_MCP_LOG_DIR`; see fixture `isolated_state` in
  `tests/test_backend_spawn_no_redirector.py`).
- **Never modify `tests/test_singleton_cold_start_patience.py`.**
- Tests/probes never touch ports **19222** or **52554** (live backends of other sessions).
- LOC caps in `tools/check_file_budgets.py` are exact; ratchet DOWN only; if a file is over, EXTRACT into a new
  leaf, never raise a cap. `singleton.py` is 994 LOC against the 1000 default — you have ~6 lines of room there,
  so the fix MUST live in a new leaf and singleton gains only the call.
- No new `STEALTH_MCP_*` env knobs. No `typing.Any`. No `os.environ` reads outside `settings.py` (building a child
  env dict from `os.environ` at the spawn site, as `_start_server_process` already does, is the existing pattern).
- Universal fixes over knobs. Claims must be measured and truthful.
- Never run two full hermetic test lanes concurrently.
- `STEALTH_MCP_NO_ERROR_REPORTING=1` and `PYTHONUTF8=1` in EVERY test env.
- Use `rtk proxy git` / `rtk proxy gh` / `rtk proxy grep` (the rtk shim mangles `-hE`, `\(`, `;;` in `case`, jq `\(`,
  and returns false-negative zero matches for escaped `\|` alternations — never claim "X appears nowhere" from one grep).
- **Do NOT add attribution lines to commit messages or PR descriptions.**
- Do not touch `scheduling_lag.py` / `MAX_STRETCH` (maintainer's call, not part of this).
- Merge PRs on green. Gate reruns must be FULL reruns (`gh run rerun <id>`), never rerun-failed.
- NEVER `uv tool install .` / `--from .` from source while uvx@latest sessions exist (fingerprint eviction ping-pong
  kills every browser). Ship via PyPI, THEN `uv tool upgrade stealth-chrome-devtools-mcp`.
- Never run `stealth-chrome-devtools-mcp.exe --anything` as verification.
- Don't kill other sessions' processes without surfacing it. Live right now: backends on 7169 (2.1.1), 19222 (2.1.3),
  52554 (2.1.4) recorded in `~/.stealth-mcp/server.json`.
- Hooks: a PreToolUse hook requires `graphify query "<question>"` (or `graphify explain` / `graphify path`) BEFORE
  reading source files — include this in any subagent prompt. After code changes run `graphify update .`.
  Bypass-permissions mode asks you to prefer the Bash tool (cat/sed/grep/heredocs) over Read/Edit/Write where it can
  do the job. Write complex scripts to `$TEMP` with the Write tool when quoting gets hairy.
- Pre-commit runs `tools/check_pinned_imports.py` (~5 min cold on OneDrive, 8 s warm) — run hooks in the foreground.
  Pre-push runs the unit lane (5–20 min) so SSH pushes time out: push detached over HTTPS with
  `Start-Process` and the credential helper `%TEMP%\gh-cred.cmd` (contents `@gh auth git-credential %*`).
- Claude Code kills background tasks under low memory (128 GB box, often 1.5–4 GB free): run hooks/tests
  foreground or fully detached, never as a Claude background task you depend on.
- OneDrive-hosted `.venv` cold file opens take seconds: re-run a "slow" spawn shape warm before blaming the change.
- When the user asks a question, report findings and stop. Otherwise act; do not ask "shall I".

---

## 3. The decided fix (do not re-litigate)

Combination of finding §4 candidates (2)+(1), in a NEW leaf `src/stealth_chrome_devtools_mcp/embedded/backend_launch.py`
(THE one home for "spawn the backend where no client job can reach it"; imports no other embedded module except
what it must — keep it a leaf like `backend_watchdog.py`; it may import `desktop_launch._schtasks`/`_system_binary`
style helpers only if that does not create a second home — prefer reusing `desktop_launch._schtasks` as the ONE
schtasks seam rather than duplicating it; if reuse means importing `desktop_launch` (which imports nodriver/psutil/
requests), that is acceptable; do NOT create a second `_schtasks`).

Rungs, Windows only (`sys.platform == "win32"`); POSIX keeps the exact current `Popen(start_new_session=True)`:

1. **Breakaway rung (free):** `Popen(cmd, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
   CREATE_BREAKAWAY_FROM_JOB, ...)`. On `OSError` with `winerror == 5` (ERROR_ACCESS_DENIED) fall to rung 2.
   Also fall to rung 2 if, after a successful breakaway spawn, the process is still in a job we can detect
   (optional; not required — the pin decides membership with its own handle).
2. **Job-free intermediary rung:** a one-shot Task Scheduler task (`schtasks /Create /F /TN stealth-mcp-backend-<token>
   /SC ONCE /ST 00:00 /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File <script>"`, then `/Run`, then
   `/Delete /F` in a `finally`), exactly the F-810 round trip. The task runs under the Task Scheduler service, outside
   any caller job and outside the caller's process tree. Costs you MUST handle:
   - **Env hand-off:** the task does not inherit `os.environ`. The launcher script must set every variable the
     backend needs: `__PYVENV_LAUNCHER__` (F-866, when `cmd[0] != sys.executable`), all `STEALTH_MCP_*` present in the
     spawner's env EXCEPT `STEALTH_MCP_NO_AUTO_RECOVERY` (popped today), `PYTHONUTF8`, `PYTHONIOENCODING` if set,
     `PATH`, `SYSTEMROOT`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TEMP`/`TMP`, `HOME` if set. Simplest truthful
     approach: write the ENTIRE child env dict (as built today) into the launcher script as `$env:NAME = '<value>'`
     lines via `_ps_quote`, so nothing is hand-picked and nothing drifts from the Popen path. Never put secrets in
     the task's `/TR` (it is world-readable in the task store); the script file lives in the state dir
     (`backend_registry.STATE_DIR / "backend-launch"`), which is user-private, and is deleted in `finally`.
   - **`/TR` length cap (~261 chars):** the command line goes in the `.ps1`, never in `/TR` (F-810 precedent,
     `desktop_launch._launcher_script`).
   - **Boot-log redirect (F-303/F-830):** `Popen`'s stdout handle cannot cross. The launcher script must redirect
     the backend's stdout+stderr to the rolled boot log itself: call `logging_setup.roll_boot_log(log_dir)` in the
     SPAWNER (the only place rotation is allowed — see `roll_boot_log` docstring), then pass the resulting path to the
     script, which does `Start-Process ... -RedirectStandardOutput <path> -RedirectStandardError <path2>`.
     PowerShell refuses the same file for both; use `backend-boot.log` for stdout and write stderr to the same file
     by launching via `cmd.exe /c "<python> <args> >> "<boot_log>" 2>&1"` from Start-Process, OR run the
     interpreter under `Start-Process -FilePath cmd.exe -ArgumentList '/c ...'`. Pick one, measure that an
     import-time crash lands in the boot log (that is what F-303 exists for), and document the choice in the module
     docstring. `-WindowStyle Hidden` + `cmd.exe` would flash no console under the scheduler (the task has no
     interactive console); verify no visible window appears — the maintainer explicitly asked that no terminal window
     ever appear ("if a terminal window HAS to spawn, make sure that's on the background").
   - **Pid hand-back:** `_write_server_state` needs the REAL backend pid. The launcher writes `$p.Id` to a pid file
     (F-810's `Set-Content ... $p.Id`). If you launch via `cmd.exe`, `$p.Id` is cmd's pid, not python's — so either
     launch python directly with `-RedirectStandardOutput` and accept stderr in a sibling file
     `backend-boot.err.log` (then teach `roll_boot_log`'s reader / `release_gate_harness` nothing new — check first
     what reads `backend-boot.log`: `rtk proxy grep -rn "backend-boot" src tests tools`), or have the BACKEND
     self-register its pid (it already writes `server.json` via `backend_registry.record_backend`? — check
     `graphify query "who writes server.json pid"` before deciding). Preferred: direct python launch, stdout→boot
     log, stderr→boot log via a second `-RedirectStandardError` to the SAME path is refused by PowerShell — so use
     `.NET` `System.Diagnostics.ProcessStartInfo` with `RedirectStandardOutput/Error` and an async copy? Too heavy.
     **Decision rule:** whichever variant you pick, the F-303 pin (`tests/test_backend_boot_crash*.py` — find it) must
     still pass and the pid recorded must be the interpreter that serves (F-866 pin: `proc.children() == []`).
     Poll the pid file up to `SCHTASKS`-scale deadline (15–20 s), fail loud with the schtasks stderr if the task
     never ran.
   - **Availability / fallback:** `schtasks` may be unavailable or refused for a CI service account
     (`gate / transport (Windows/X64)` runs as a service). If `/Create` or `/Run` fails, or the pid file never
     appears, fall to rung 3 and log a WARNING naming F-867 so the run's logs show which rung served.
3. **Fallback rung:** the current plain spawn (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`), unchanged, so a
   headless runner without a scheduler still starts. Log which rung won at INFO via `_logger` (`stealth.singleton`
   convention — check `configure_logging`) and via `observability.capture_lifecycle` ONLY if there is an existing
   lifecycle event shape that fits; do not invent a fifth F-827 report without reading `proxy_selfheal._report`.

Wire-up: `singleton._start_server_process` keeps its env-building and state-writing; the `Popen` block
(lines ~414–432 today) becomes `pid = backend_launch.spawn(cmd, child_env, stdout_target/boot_log path)` — one
call, so singleton stays under 1000 LOC and the reuse gate / adoption order / cold-start lock never move.
Keep `_backend_interpreter`, `_server_process_cmd`, `__PYVENV_LAUNCHER__` (F-866) exactly as they are.

Seams for tests (no test may create a real scheduled task except the Windows real-process pin, which MAY, since the
finding §5 requires a real spawn — but it must clean the task up in `finally` and use a unique token):
`backend_launch._schtasks` → reuse `desktop_launch._schtasks` (monkeypatchable), `backend_launch._popen`,
`backend_launch._breakaway_supported`/similar. Follow `tests/test_desktop_launch.py::FakeSchtasks` for the
schtasks double.

---

## 4. Code you must read (after `graphify query`) before writing

- `src/stealth_chrome_devtools_mcp/embedded/singleton.py`: `_backend_interpreter`, `_server_process_cmd`,
  `_start_server_process` (L379–436), `_terminate_backend`, `_clear_stale_backend`, `_start_backend_holding_lock`,
  `_write_server_state`.
- `src/stealth_chrome_devtools_mcp/embedded/desktop_launch.py`: `_system_binary`, `_schtasks`, `_ps_quote`,
  `_launcher_script`, `_read_pid`, `_run_task`, `_cleanup`, `launch_and_attach` (the whole F-810 round trip and
  its two-quoting-layer rationale — reuse, don't re-derive).
- `src/stealth_chrome_devtools_mcp/embedded/logging_setup.py`: `roll_boot_log`, `resolve_log_dir`.
- `src/stealth_chrome_devtools_mcp/embedded/backend_registry.py`: `STATE_DIR`, `record_backend`.
- `tests/test_backend_spawn_no_redirector.py` (F-866 pins — the template: `isolated_state`, `_CHILD` sleeper,
  `_in_job`, the real-process pin's `_server_process_cmd` swap and `_write_server_state` capture).
- `tests/test_desktop_launch.py` (`FakeSchtasks`, how `_schtasks` is faked).
- `tests/test_startup_herd.py::test_fifty_cold_sessions_are_all_usable_within_30s` and `tests/release_gate_harness.py`
  (the herd; it is the Windows CI cell that has been red ~9 %).
- `tools/check_file_budgets.py` (GRANDFATHER table — do not add a row for the new leaf; it must fit 1000).
- `pyproject.toml` L358 (singleton ruff suppressions), L444 (ty exclude) — the new leaf gets NO suppressions and NO
  ty exclusion; it must be clean under ruff + ty + vulture (`tools/vulture_allowlist.py` only if truly needed).
- `CHANGELOG.md` (top is `## 2.1.4`; add a `## Unreleased` section above it), `CLAUDE.md` navigation map
  (add a `backend_launch.py` row under Lifecycle & transport; amend the `singleton.py` row), `DESIGN.md` if it
  describes the spawn (grep `DETACHED_PROCESS`), `RUNBOOK.md` if it tells operators how the backend is launched.

---

## 5. TODO list — execute in order, tick each

### A. Orientation
- [ ] `graphify query "how does singleton._start_server_process spawn the backend and how does desktop_launch._schtasks launch a process"` (hook-mandated), then read the files in §4.
- [ ] `rtk proxy grep -rn "backend-boot" src tests tools` — learn who reads the boot log before choosing the redirect variant.
- [ ] `graphify query "who writes server.json pid"` — confirm whether the backend self-registers (affects pid hand-back).
- [ ] Create branch: `rtk proxy git checkout -b fix/f867-backend-escapes-client-job`.

### B. RED pin (finding §5) — `tests/test_backend_escapes_client_job.py`
- [ ] Windows-only (`skipif sys.platform != "win32"`), hermetic, **ctypes only** (`CreateJobObjectW`,
      `SetInformationJobObject(JobObjectExtendedLimitInformation, LimitFlags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000)`,
      `AssignProcessToJobObject`, `IsProcessInJob`, `CloseHandle`). No `win32job`.
- [ ] Helper process script (write to tmp_path): patches `singleton` like `isolated_state` does (env
      `STEALTH_MCP_LOG_DIR`, `PORT_FILE`, `_ensure_state_dir`, `_server_version`), swaps `_server_process_cmd` for
      `[real_cmd[0], "-c", "import time; time.sleep(120)"]` keeping the REAL interpreter, captures pid via
      `_write_server_state`, calls the REAL `singleton._start_server_process(4321)`, prints the pid, sleeps.
      Port 4321 is a dummy; nothing binds it.
- [ ] Test body: create job; `Popen` helper (stdout PIPE, `CREATE_NO_WINDOW`); `AssignProcessToJobObject` BEFORE
      reading the pid line (assign first, then the helper spawns — avoid the spawn-before-assign race the experiment
      hit once; you can gate the helper on a stdin byte: helper waits for `sys.stdin.readline()` before spawning).
- [ ] Assert `IsProcessInJob(helper, job)` is True (sanity: the job is real). Then `CloseHandle(job)` (row 2 — the
      weaker action; if the backend survives handle close it survives Terminate too — add a second parametrized case
      for `TerminateJobObject` if cheap). Sleep ~1 s. Assert helper dead AND **sleeper alive** AND
      `IsProcessInJob(sleeper, job_handle_dup)` is False — you need the job handle still open for the precise query,
      so `DuplicateHandle`/keep a second handle, or query membership BEFORE closing (membership cannot change after
      spawn) and assert alive AFTER closing. Kill the sleeper in `finally`.
- [ ] Run: `STEALTH_MCP_NO_ERROR_REPORTING=1 PYTHONUTF8=1 uv run python -m pytest tests/test_backend_escapes_client_job.py -x -q`.
      Confirm it FAILS for the right reason (sleeper dead / in job), not a harness error. Record the output for the PR.

### C. GREEN — `embedded/backend_launch.py` + wiring
- [ ] Write the leaf per §3 (module docstring = the F-867 mechanism + the three rungs + why each cost is paid).
- [ ] Wire `singleton._start_server_process` to call it (Windows path only; POSIX unchanged). Keep singleton ≤ 1000 LOC.
- [ ] Run the new pin → GREEN. Run `tests/test_backend_spawn_no_redirector.py` → still GREEN (F-866: no redirector
      child, venv resolved, no console). If rung 2 served, `proc.children() == []` must still hold (that is why the
      pid handed back must be python's, not cmd's/powershell's).
- [ ] Add hermetic unit tests for the leaf with a fake Popen (`OSError(winerror=5)`) and `FakeSchtasks`-style double:
      rung order, env hand-off content (every key of the child env appears in the script; `STEALTH_MCP_NO_AUTO_RECOVERY`
      absent; `__PYVENV_LAUNCHER__` present exactly when redirector bypassed), `/TR` under 261 chars, task deleted in
      `finally` on success AND on failure, fallback when `/Create` fails, fallback when pid file never appears,
      POSIX path untouched (`start_new_session=True`, no creationflags).
- [ ] F-303 boot-crash pin still green (find it: `rtk proxy grep -rln "boot_crash\|backend-boot" tests`).
- [ ] `graphify update .`

### D. Docs
- [ ] Finding status line → `**Status:** FIXED — <commit/PR>, 2.1.5. Fix: …` and add a §6 "The fix" (rungs,
      measured results: which rung served locally under Claude Code's harness job, and on CI).
- [ ] `audit/stage2/finding_F859_windows_herd_stall_rate.md` §13: note F-867 fixed in 2.1.5; the herd rate claim
      must be re-measured, not asserted — say "expected 0 %; verify over the next N gate runs".
- [ ] `CHANGELOG.md`: `## Unreleased` → `### Fixed — the backend escapes the MCP client's Job Object (F-867)` in the
      same narrative style as the 2.1.4 entry (what happened, mechanism, fix, what users see).
- [ ] `CLAUDE.md`: new `backend_launch.py` row; amend `singleton.py` row ("the spawn itself moved to `backend_launch.py`").
- [ ] `DESIGN.md`/`RUNBOOK.md` only where they describe the spawn flags or the process tree.
- [ ] Memory: update `f867-backend-inherits-client-job-object.md` (OPEN → FIXED in 2.1.5, which rung serves where)
      and `release-2-0-0-resume-point.md` (resume point → 2.1.5). Keep `MEMORY.md` index lines accurate.

### E. Lanes
- [ ] Unit lane: `STEALTH_MCP_NO_ERROR_REPORTING=1 PYTHONUTF8=1 uv run python -m pytest -m "not integration" -q -p no:cacheprovider`
      (5–20 min; foreground or detached-to-file, never a Claude background task).
- [ ] Targeted integration: `tests/test_startup_herd.py`, the sigbreak/boot-crash pins, `tests/test_desktop_launch.py`,
      `tests/test_backend_spawn_no_redirector.py`, the new pin. Not concurrently with the unit lane.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run ty check && python tools/check_file_budgets.py && python tools/check_pinned_imports.py && python tools/check_suppression_owners.py` (run whatever `CONTRIBUTING.md` names as the gate).

### F. Ship
- [ ] Commit(s) on `fix/f867-backend-escapes-client-job` — message style `fix(F-867): the backend escapes the MCP client's Job Object …`. **No attribution lines.** Let pre-commit run (~5 min cold).
- [ ] Push detached over HTTPS (pre-push lane): write `$TEMP/push.ps1` with `git -c credential.helper="%TEMP%\gh-cred.cmd" push -u https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp.git fix/f867-backend-escapes-client-job`, run via `Start-Process powershell -ArgumentList ... -RedirectStandardOutput $TEMP/push.log`; poll the log.
- [ ] `rtk proxy gh pr create --base main --title "fix(F-867): …" --body-file $TEMP/pr_f867.md` (body: defect, mechanism, fix rungs, RED-then-GREEN evidence, which rung served on each CI cell, docs touched). **No attribution lines.**
- [ ] Watch the gate: `rtk proxy gh run list --branch fix/f867-... --limit 5`, `rtk proxy gh run view <id> --log-failed`. Read the Windows transport cell's logs: confirm which rung served (the INFO line) — if the CI service account cannot use schtasks, the fallback rung serving is EXPECTED and the herd may still be red on that cell at the old rate; say so truthfully in the PR rather than claiming CI proves the fix. Flake → FULL rerun (`gh run rerun <id>`), diff against last green log for the same cell.
- [ ] Merge on green: `rtk proxy gh pr merge <n> --merge`.

### G. Release 2.1.5
- [ ] `rtk proxy git checkout main && rtk proxy git pull && rtk proxy git checkout -b release/2.1.5`.
- [ ] `python "$TEMP/release_bump.py" 2.1.4 2.1.5` (bumps pyproject, README ×3, uv.lock, CHANGELOG `## Unreleased` → `## 2.1.5`, regenerates RELEASE_CONTRACT.md and checks it). Script is at `C:\Users\amind\AppData\Local\Temp\release_bump.py`; if `$TEMP` was purged, recreate from the F-866 release commit's diff (`rtk proxy git show f9a4cf9 --stat` shows the files touched).
- [ ] Commit `release: 2.1.5`, detached HTTPS push, PR, gate green (full rerun on flake), merge.
- [ ] Tag the MERGE commit: `rtk proxy git fetch && rtk proxy git tag v2.1.5 <merge-sha> && push the tag` (HTTPS detached). `publish.yml` re-runs the gate at the tag and publishes to PyPI — watch it to completion, read the publish job log.
- [ ] Confirm on PyPI (`pip index versions stealth-chrome-devtools-mcp` or the JSON API), THEN `uv tool upgrade stealth-chrome-devtools-mcp` and confirm `uv tool list` shows 2.1.5. If lingering proxies hold `python.exe` and the upgrade fails, surface which pids (other sessions') and do not kill them silently.

### H. Report
- [ ] Final message: what was wrong, what the fix does (rungs), RED→GREEN evidence, which rung served locally and on
      CI (measured, from logs), gate results, PR numbers, release/tag/publish run ids, tool version installed, and
      what remains open (F-863, F-864 probe churn, mcp bump needs pydantic 2.12, stale `server.json` entries,
      lingering "tearing down for reconnect" proxies — unfiled; herd-rate re-measurement pending).

---

## 6. Truthfulness rules for the write-up

- Say which rung ACTUALLY served in each environment you ran (local under Claude Code's harness job; each CI cell).
  Do not claim the herd rate is fixed until measured over real gate runs; say "explains" vs "proves" precisely.
- If schtasks is refused on the CI runner, the Windows CI cell is NOT protected by rung 2 — state that the fix
  protects real user machines (interactive sessions) and that CI needs a follow-up (e.g., the herd harness could
  build its job with `BREAKAWAY_OK`, which is legitimate since the harness is the client) — file that as a finding
  note, do not silently widen scope into the harness in this PR unless the pin cannot otherwise go green on CI.
- Every number (LOC, timings, rates, run ids) must come from a command you ran.
