# F-810 — self-healing headed spawns from a headless-context backend (Windows)

**Ruling (human, 2026-08-02):** the user explicitly asked that headed spawns "just
work" with no manual step. This AMENDS the F-808 ruling's "never enters another
session" — but only in mechanism, not in spirit: the tool still never *picks* a
session. It hands process creation to Windows Task Scheduler, and the OS itself
places the process in the logged-on user's interactive session. `display_context.py`
stays purely observational and MUST NOT be modified.

## The behavior

`spawn_browser(headless=False)` on a backend whose `display_context()` cannot show
windows (win32 only):

1. If a user is logged on at the console (active console session ≠ 0/none):
   delegate Chrome's *process creation* to a one-shot Task Scheduler task that runs
   "only when user is logged on" — Windows executes it on the user's desktop, so
   the window is visible **by construction**. The task launches Chrome with the
   exact stealth-filtered launch args the normal path would use, PLUS
   `--remote-debugging-port=<free port>`, then the SAME backend attaches via
   nodriver's first-class attach (`Config(host="127.0.0.1", port=P)` →
   `connect_existing=True` in `Browser.start`, verified against the installed
   nodriver). ONE backend; the instance lives where every other instance lives;
   all 94 tools work unchanged over the attached CDP websocket.
2. If delegation is impossible (non-win32, no active console session) or fails
   (schtasks error, DevTools port never ready): raise the existing F-808 `ToolError`,
   with the failure reason appended. The loud refusal is now the *fallback*, which
   is exactly when a loud error is correct.

No env knob, no installer, on by default (user's standing rule: universal defaults,
no config-knob workarounds).

## Mechanism facts (verified this session — do not re-derive)

- nodriver attach: `uc.Config(host=..., port=...)` → `Browser.start` sets
  `connect_existing = True`, skips `create_subprocess_exec`, `_process`/`_process_pid`
  stay `None`, connects `HTTPApi((host, port))`. `browser_args`/`user_data_dir` in
  the config are IGNORED on attach — they must go on the schtasks command line.
- `process_cleanup.track_browser_process` (file AT its 1023 LOC cap — do NOT edit
  it) reads ONLY `.pid` off the process object, then goes pid-based via psutil +
  `browser_pid_registry.new_entry`. A `types.SimpleNamespace(pid=<pid>)` shim is a
  valid process object for it.
- Teardown is already attach-safe: `close_instance` phase 2 sends CDP
  `Browser.close`; phase 3 calls `process_cleanup.kill_browser_process` (pid-based)
  first and guards every `browser._process` access; `browser.stop()` failures are
  caught+logged. No teardown changes needed.
- `schtasks /Create /TR` truncates ~261 chars → the task must run a launcher
  script file, not the raw Chrome command line.
- Guard today: `server.py:388-397` — pre-try, outside the except-wrap, message
  pinned by tests (grep for "cannot display a window" in tests/ and update pins
  DELIBERATELY in this PR; SOFT-golden discipline, justify in the commit).

## Files

**Create: `src/stealth_chrome_devtools_mcp/embedded/desktop_launch.py`** (new leaf,
< 1000 LOC, absolute-from-package imports only, never imports `server`):
- `available() -> bool` — True only on win32 with an active user console session:
  `ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()` not in `(0, 0xFFFFFFFF)`.
  Wrap the ctypes probe in a seam (`_active_console_session_id() -> int | None`)
  so tests fake it. Never raises.
- `async def launch_and_attach(browser_executable, launch_args, user_data_dir) ->
  tuple[Browser, int]` — returns `(browser, pid)`:
  1. free port P (reuse `proxy_forwarder._free_port`),
  2. write a PowerShell launcher under `backend_registry.STATE_DIR /
     "desktop-launch" / <uuid>.ps1`: `Start-Process -FilePath <exe> -ArgumentList
     <quoted args incl. --remote-debugging-port=P and --user-data-dir> -PassThru`
     → writes `$p.Id` to `<uuid>.pid` beside it. Quote every arg defensively
     (paths contain spaces).
  3. `schtasks /Create /F /TN stealth-mcp-launch-<uuid> /SC ONCE /ST 00:00 /TR
     "powershell.exe -NoProfile -ExecutionPolicy Bypass -File <script>"` then
     `schtasks /Run /TN ...` — via a `_schtasks(args) -> CompletedProcess` seam
     (subprocess.run, capture, 15s timeout) so tests fake it. No admin needed for
     a current-user logged-on-only task; do NOT pass /RU or /RP.
  4. poll (async, ~0.25s steps, 20s deadline): pid file exists AND
     `GET http://127.0.0.1:P/json/version` answers (urllib in a thread or
     httpx — match whatever the codebase already depends on).
  5. always (finally): `schtasks /Delete /F /TN ...`, delete the script; keep or
     delete the pid file after reading — do not leave the directory growing.
  6. attach: `await uc.start(config=uc.Config(host="127.0.0.1", port=P))`, set
     `browser._process_pid = pid` (nodriver initializes it None; teardown's
     `os.kill(browser._process_pid, 15)` fallback then works), return.
  7. any failure → raise `tool_errors.ToolError` naming the failed step (task
     create/run, timeout waiting for DevTools port). Clean up task+script first.
- Module docstring: THE one home for "delegate a browser launch to the user's
  desktop via Task Scheduler" — the F-808-ruling amendment lives here in prose.

**Modify: `src/stealth_chrome_devtools_mcp/embedded/browser_manager.py`** (AT its
1532 cap — every added line must be paid for; moving the `uc.Config` build out of
`_launch_browser` into the normal-path branch of a single seam is the intended
payment):
- `_launch_browser`: when `not options.headless and not
  display_context.can_show_windows() and desktop_launch.available()` → delegate to
  `desktop_launch.launch_and_attach(...)` (it needs the resolved
  `options.user_data_dir` — pass whatever the orchestrator resolved, the same value
  the normal Config gets); else the current Config+`uc.start` path unchanged.
- `_apply_post_launch`: current tracking condition is `if hasattr(browser,
  "_process") and browser._process:` — extend so an attached browser (with
  `_process` None but `_process_pid` set) is tracked via
  `SimpleNamespace(pid=browser._process_pid)`. An untracked delegated browser
  would be an orphan-reaping hole (F-808 fratricide lessons apply).
- Run `python tools/check_file_budgets.py` — browser_manager.py must stay ≤ 1532,
  server.py ≤ 3401, process_cleanup.py untouched at ≤ 1023. Caps are exact; do
  not pad, do not ratchet without flagging.

**Modify: `src/stealth_chrome_devtools_mcp/embedded/server.py`** (AT its 3401 cap):
- The guard becomes the fallback: fire only when delegation is unavailable —
  `if not headless and not display_context.can_show_windows() and not
  desktop_launch.available():` — and the message gains one clause saying a headed
  spawn is delivered automatically when a user is logged on at the desktop.
  Pay for the import + condition by tightening the message lines; net-zero LOC.
  Import `desktop_launch` module-level with the other embedded imports if a line
  can be paid for, else function-local above the guard (precedent: the
  platform_utils import inside spawn_browser).

**Tests (new: `tests/test_desktop_launch.py`; extend fakes in `tests/fakes.py`
only if the harness needs it — fakes.py is THE hermetic harness home):**
- `available()`: win32+active session → True; session 0 / 0xFFFFFFFF / probe
  raises → False; non-win32 → False. All via the seam, no real ctypes on CI.
- `launch_and_attach` happy path with faked `_schtasks`, faked port probe, real
  tmp pid file: asserts the script content quotes args, the task name round-trips
  create→run→delete, returns the pid file's pid, attaches with host/port config
  (fake `uc.start` capturing the Config).
- failure paths: schtasks create fails → ToolError + no task left behind (delete
  attempted); port never ready → ToolError after deadline + task deleted.
- guard: over the `call_tool` harness (positional-only), `can_show_windows` False
  + `available()` False → ToolError with the (updated) message; `available()` True
  + `launch_and_attach` raising → the delegation error surfaces, profile-clone
  NOT created first (the F-808 "no clone before refusal" invariant holds for the
  refusal branch; the delegation branch MAY clone — it is a real spawn attempt).
- `_apply_post_launch` tracks the pid-shim case (fake browser: `_process=None`,
  `_process_pid=1234` → `track_browser_process` called with `.pid == 1234`).
- Update the existing F-808 message pins deliberately (grep tests/ for
  "cannot display a window").
- NO test may create a real scheduled task or touch the real `~/.stealth-mcp`
  (monkeypatch STATE_DIR / the module's dir constant to tmp_path).
- Unit lane must stay green: `uv run python -m pytest` (worktrees need
  `uv sync --extra test --extra dev` first). Run the FULL unit lane, not a
  narrow selector (narrow selectors lose Chrome warmup).

**Docs (same PR):** CLAUDE.md nav-map row for `desktop_launch.py`; DESIGN §2.7
one paragraph on the amended ruling (Task Scheduler delegation: the OS places the
process, the tool still never picks a session); DESIGN §10 ledger row F-810;
CHANGELOG entry under an Unreleased/2.0.5 heading. Tool count stays 94.

**Out of scope:** non-Windows delegation, login-time autostart installer, any
`display_context.py` change, any `process_cleanup.py` change, re-adoption of
backends mid-session.

**Conventions (non-negotiable):** absolute-from-package imports; ToolError raises
(no success:False dicts); no module under embedded/ imports server; ruff format
clean; commit trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
