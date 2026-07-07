# 3am Incident Simulation — stealth-chrome-devtools-mcp

**Scenario:** MCP server stopped responding mid-session. Claude's tool calls hang
indefinitely. An orphaned Chrome process is eating CPU. On-call engineer, zero
tribal knowledge, repo only.

Pinned branch: `fix/singleton-version-aware-backend` @ `2267b83`.

All commands below were actually run, read-only, against this repo/machine.
Output is copy-pasted verbatim (warnings included).

---

## 00:00 — Where do you look first?

No `docs/` directory exists anywhere in the repo (verified via full tracked-file
inventory, `git ls-files`, 83 files). The only doc-shaped file is `README.md`.
Scanning it top to bottom, operational content is a single "## CLI" section
(README.md:214-228) describing a `stealth-chrome-devtools` command:

```
stealth-chrome-devtools status       # backend running? session root + caps
stealth-chrome-devtools profiles     # list profiles with size / role / in-use
stealth-chrome-devtools cleanup      # preview reclaimable disk (DRY RUN)
stealth-chrome-devtools cleanup --apply               # actually reclaim
stealth-chrome-devtools cleanup --session-cap-gb 12   # preview at a tighter cap
stealth-chrome-devtools doctor       # check Chrome / environment
stealth-chrome-devtools serve --http --port 19222     # start the server
```

There is no troubleshooting section, no "if X then Y" incident guidance, no
mention of where any state lives on disk. This is a real, working CLI though
(confirmed by reading `src/stealth_chrome_devtools_mcp/cli.py` — `argparse`
subcommands `status`, `profiles`, `cleanup`, `doctor`, `serve`, nothing else).
**(F-306)**

## 00:02 — Run the status/doctor commands (read-only, as the repo's own docstring says is safe)

`cli.py`'s module docstring explicitly says read-only commands set
`STEALTH_MCP_NO_AUTO_RECOVERY=1` before import, "so merely running the CLI
never kills a running server's browsers or touches its profiles." Ran both,
for real, against the live machine:

```
$ uv run stealth-chrome-devtools status
backend     : running on port 19222
version     : 1.2.0
session root: C:\stealth-mcp-browser-sessions  (exists: True)
clone cap   : 10.0 GB  [STEALTH_MCP_CLONE_STORAGE_CAP_GB]
session cap : 20.0 GB  [STEALTH_MCP_SESSION_STORAGE_CAP_GB]

$ uv run stealth-chrome-devtools doctor
python      : 3.12.12
platform    : Windows-11-10.0.26200-SP0
session root: C:\stealth-mcp-browser-sessions  (exists: True)
backend     : running on port 19222
chrome      : C:\Program Files\Google\Chrome\Application\chrome.exe
```

This is genuinely useful — a real health-check surface exists, and it does
tell you the backend is "running." **But** reading `singleton.py`, the
"running" verdict behind both commands is `_find_running_server()`, which is
built entirely on `_server_is_healthy()`:

```python
def _server_is_healthy(port: int) -> bool:
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.close()
        return True
    except (socket.error, OSError):
        return False
```

A bare TCP connect/close. It does not send a single byte of MCP protocol, does
not check the backend's asyncio event loop is scheduling anything, does not
check FastMCP's session manager is alive. **In the exact incident scenario
given — "tool calls hang indefinitely" — the backend process is presumably
still running and its listening socket is presumably still accepting
connections; only its request-handling is wedged.** `status`/`doctor` would
report exactly what they reported above: healthy. This is the central blind
spot of the whole exercise. **(F-301)**

## 00:05 — Is there a PID to inspect further?

`status`/`doctor` output above has no PID, only a port. Grepping `cli.py`
confirms `_cmd_status`/`_cmd_doctor` only ever print `singleton._find_running_server()`
(the port) — never `pid`. But `singleton.py`'s `_write_server_state()` records
`{port, version, pid}` on every backend start, to `~/.stealth-mcp/server.json`.
Read it directly (read-only):

```
$ cat ~/.stealth-mcp/server.json
{"port": 19222, "version": "1.2.0", "pid": 104504}
```

Cross-checked against the live process table (read-only):

```
PowerShell> Get-CimInstance Win32_Process -Filter "ProcessId=104504" |
            Select ProcessId,ParentProcessId,Name,CreationDate,CommandLine

ProcessId       : 104504
ParentProcessId : 33616
Name            : python.exe
CreationDate    : 2026-07-02 8:26:44 AM
CommandLine     : "...\.venv\Scripts\python.exe" -m stealth_chrome_devtools_mcp
                   --transport http --port 19222 --host 127.0.0.1
```

Confirmed: the PID *does* exist in a real, recorded state file, and it *does*
identify the right process. But nothing in the documented tooling ever shows
it to you — you have to already know `~/.stealth-mcp/server.json` exists,
which is not mentioned anywhere in README or `--help`. This machine also has
~24 separate `stealth-chrome-devtools-mcp.exe` stdio-proxy processes alive
(one per active Claude Code session), all funneling into this single PID —
consistent with `singleton.py`'s documented multi-session design, but it also
means one wedged backend stalls every one of those sessions simultaneously.
**(F-305)**

## 00:08 — Where are the logs? Do they capture the hang?

Grepped every tracked `.py` file for `FileHandler`, `RotatingFileHandler`,
`logging.basicConfig` — zero matches, confirmed twice (once with a broken glob
that gave a false negative, re-run correctly the second time). The only
logging subsystem in the repo is `embedded/debug_logger.py`'s `DebugLogger`
class: plain Python lists (`self._errors`, `self._warnings`, `self._info`)
held in the process's own memory, `self._enabled = False` by default (gated
behind `STEALTH_BROWSER_DEBUG`, confirmed wired at `server.py:218`), emitting
to `stderr` only when enabled.

Then the actual blocker: `singleton.py`'s `_start_server_process()` — the
function that spawns the real HTTP backend (PID 104504 above) — launches it
with:

```python
kwargs: dict = {
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
    "stdin": subprocess.DEVNULL,
}
```

Every print, every uncaught traceback, every uvicorn log line, every
`debug_logger` stderr emission from the one process that matters is piped to
the null device at spawn time. There is no log file to `tail -f`. Not
"hard to find" — structurally absent, by design, for the backend process
specifically. **(F-303)**

Checked whether the in-memory debug state is at least reachable through the
MCP tools that expose it (`get_debug_view`, `export_debug_logs`,
`get_debug_lock_status` — confirmed present at `server.py:2459`, `2505`,
`2549`). They are ordinary `@section_tool` MCP tool functions, dispatched
through the same request path as every other tool. In the incident as
described ("tool calls hang indefinitely"), calling these to retrieve
diagnostics is itself a tool call that would hang. There's no side channel —
no second debug port, no signal-triggered dump, no periodic auto-flush to
disk. **(F-304)**

Even granting a hypothetical world with file-based logs: grepped for
`correlation_id`, `request_id`, `trace_id`, `traceid` across the whole tree.
The only `request_id` concept that exists is CDP network-capture IDs
(`network_interceptor.py`) — unrelated to MCP tool-call identity. There is no
way to tie a captured log line to "which tool call, from which session, was
in flight." **(F-308)**

One more thing surfaced while reading `debug_logger.py` closely: `log_error`/
`log_warning`/`log_info` acquire `self._lock` (untimed `with self._lock:`) and
call `_emit_stderr` — a blocking `print(..., file=sys.stderr)` — from inside
that lock. `_emit_stderr` only catches `(OSError, ValueError)`, not a
slow/undrained pipe backpressuring (which blocks rather than raising). If that
ever happens on any thread, every other thread's next log call hangs on the
untimed lock forever — a hang no `asyncio.wait_for` anywhere in the codebase
could rescue, since it's synchronous. This needs `STEALTH_BROWSER_DEBUG=true`
and a real (non-DEVNULL) stderr pipe to trigger, so it's most relevant to a
manually-run `serve` rather than the production singleton path — flagged with
lower confidence accordingly. **(F-307)**

## 00:15 — How do you determine singleton/backend state, and recover a wedged one?

Per `singleton.py`'s own docstring, the singleton is a file-lock-guarded
"start it once, everyone else proxies to it" design — `~/.stealth-mcp/`
holds `singleton.lock`, `server.port`, `server.json` (confirmed all three
present on this machine, directory-listed read-only above).

The one automatic recovery mechanism is `_watch_backend_liveness()`: after the
backend is confirmed up, it polls `_server_is_healthy()` every 2s and tears
the stdio proxy down after 3 consecutive failures, so the client (Claude Code)
sees a clean disconnect and reconnects — which respawns a fresh backend. This
is a real, tested mechanism (`tests/test_proxy_backend_death.py`), **but its
own test docstring frames the entire problem as the backend *dying*** (the
test literally does `backend.kill()` and asserts the proxy notices within
30s) — never a backend that stays alive but stops answering. Since the
watchdog's `is_healthy` check is the same TCP-connect-only function from
00:02, a wedged-but-socket-open backend defeats it exactly as it defeats
`status`/`doctor`. **(F-301, restated as the recovery-path consequence)**

Manual recovery, then: the CLI's dispatch table is

```python
_DISPATCH = {
    "status": _cmd_status,
    "profiles": _cmd_profiles,
    "cleanup": _cmd_cleanup,
    "doctor": _cmd_doctor,
    "serve": _cmd_serve,
}
```

No `stop`, `restart`, or `kill` verb exists. The only function that can evict
a backend, `_clear_stale_backend()`, is never CLI-exposed — it only runs
internally, mid-startup, when a *different-version* backend is detected during
a fresh spawn. For a same-version wedged backend, nothing in the shipped
tooling can evict it. The actual recovery path is: manually read
`~/.stealth-mcp/server.json` (undocumented, F-305) to get the PID, manually
`taskkill /PID 104504 /F` (undocumented, unsupported, not mentioned anywhere),
then start a new Claude Code session so `ensure_server_running()` sees
`_find_running_server() is None` and spawns a fresh backend. **(F-302)**

## 00:20 — How do you safely kill the orphaned Chrome without nuking the user's other windows?

Listed every `chrome.exe`/`chromium`-family process on this machine
(read-only, `Get-CimInstance Win32_Process`): **well over 80 chrome.exe
processes** across roughly half a dozen distinct parent trees, none
identifiable as "MCP-owned" vs. "the user's personal browsing session" from
name/PID/creation-time alone. This is the live, concrete version of the
danger the incident scenario is testing for.

Reading `embedded/process_cleanup.py`, the actual matching logic is well
engineered and genuinely safe:

- `_is_browser_process_name()` filters to chrome/chromium/edge/brave only.
- `_extract_profile_dir_from_cmdline()` reads the real `--user-data-dir` flag
  off each candidate process and matches it against tracked profile
  directories — not just "any chrome.exe."
- Recovery additionally requires `create_time < self._init_time` (the *new*
  server's start time) — so a browser spawned during the current run is never
  touched, only ones that predate this server session.
- `_kill_process_by_pid()` escalates gracefully: `terminate()` → wait 3s →
  `kill()` → wait 2s, logging every step.

This logic is correct and would not nuke the user's regular Chrome. **The gap
is not the matching, it's the trigger**: it only runs from
`ProcessCleanup.__init__()`, i.e. only as a side effect of a *fresh* backend
process starting. Per the 00:15 analysis, a fresh backend won't start until
the wedged one is manually killed — so the orphan-Chrome cleanup this section
praises is unreachable for exactly as long as F-301/F-302 block backend
recovery. There is no standalone `stealth-chrome-devtools kill-orphans`
command to run this matching logic without a full backend restart. **(F-302)**

## 00:25 — What would you have needed that doesn't exist?

- **A liveness check that means what it says** (F-301) — the single highest-value
  fix. Everything downstream (status/doctor accuracy, the watchdog, the
  eviction guard) inherits from this one function.
- **A stop/restart/kill-orphans verb in the ops CLI** (F-302) — even with
  perfect detection, there's still no supported way to act on it.
- **Any log file at all for the backend process** (F-303) — currently
  impossible by construction (`subprocess.DEVNULL`), not just missing.
- **An out-of-band way to pull debug state** (F-304) — the existing
  in-memory debug tools are unreachable during precisely the failure they
  exist to diagnose.
- **The PID surfaced in `status`/`doctor`** (F-305) — a one-line fix that
  unblocks the entire manual-recovery workaround.
- **Written incident documentation** (F-306) — none of the above requires a
  code change to partially mitigate; a TROUBLESHOOTING.md documenting today's
  actual (manual, undocumented) recovery steps would help immediately.
- **A correlation/request id** (F-308) — to identify *which* tool call wedged
  the backend, once logs exist at all.
- Also surfaced along the way: an untimed lock + blocking stderr write inside
  the debug logger itself (F-307) — a hang with no timeout able to rescue it,
  under specific (non-default) conditions.

## Verdict: could the on-call engineer have diagnosed this in time?

**No, not from the repo alone.** `status`/`doctor` exist, are genuinely useful,
and would be the correct first move — but for this specific incident (backend
alive, tool calls hanging) they report false-healthy, because their only
liveness signal is a bare TCP connect (F-301). Even after correctly suspecting
the backend, there is no supported command to confirm it, no PID surfaced to
act on it (F-305), no log file to inspect why it wedged (F-303), no way to
pull the in-memory debug state because the retrieval tools share the same
hung request path (F-304), and no stop/restart command to fix it (F-302) —
only a manual, undocumented "find the PID in a JSON file the README never
mentions, then taskkill it" workaround. The one thing that *is* solid and
safe is the orphaned-Chrome process matching itself (`process_cleanup.py`),
but it can't run until the backend is already dead, which is precisely the
step nothing else in the tooling helps you reach. Time-to-diagnosis: fast to
reach "I suspect the backend" (~5 min, thanks to a real ops CLI); unbounded to
reach "I have confirmed it and fixed it" using only documented tooling.
