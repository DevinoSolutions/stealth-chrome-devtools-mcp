# F-839 — The shared backend honors a session-scoped SIGBREAK it should never receive; its death is the residual "random disconnect"

**Severity: HIGH** — this is the class of incident the user still experiences on
2.0.7 after F-820: "we'd be running stealth mcp and it randomly just
disconnects."
**Found:** 2026-08-31, post-mortem of the 2026-08-30 18:43:57 backend death.
**Status:** FIX 1 SHIPPED (branch `fix/F839-ignore-sigbreak`, 2.0.8-unreleased) —
the backend now IGNORES SIGBREAK. Fix 2 (spawn-chain audit) is **partly
answered, see "What the console-immunity test actually proved" below**; fix 3
(F-838 proxy self-heal) remains a separate finding.

## The incident (all timestamps 2026-08-30, local)

The backend serving every session (worker pid 163320, port 19222, v2.0.7,
healthy — its last tool call completed normally at 16:13:24) logged:

```
18:43:57,458 INFO 163320 [-] stealth.backend: process_cleanup.signal_handler: Received signal 21, initiating cleanup...
18:43:57,458 INFO 163320 [-] stealth.backend: process_cleanup.cleanup_all: No browser processes to clean up
```

and exited cleanly. Four live proxies then struck out in lockstep
(`probe failed 1/3 … 3/3` between 18:43:59 and 18:44:10, all four logs) and
tore down — correctly, the backend was truly gone. Every attached Claude
session disconnected at once. No replacement backend existed until a session
cold-started one at 07:41 the next morning.

Signal 21 on Windows Python is **SIGBREAK = CTRL_BREAK_EVENT, a console
control event**. It can only arrive via a shared console
(GenerateConsoleCtrlEvent / os.kill(pgid, CTRL_BREAK_EVENT)).

## Why this is a product defect and not "someone stopped it"

1. **No product code path sends SIGBREAK.** Eviction kills via
   `psutil.Process(pid).terminate()` (singleton.py:300) and the CLI
   stop/restart verbs route to the same `_terminate_backend` — all
   TerminateProcess, which never runs a signal handler. A clean
   "Received signal 21" therefore CANNOT come from stop/restart/evict. The
   only remaining senders are session-scoped: a closing terminal/console, or
   session infrastructure (e.g., an MCP client killing its child process tree
   on session exit) whose console the backend worker turned out to share.
2. **The backend is supposed to be unreachable by those.** `_start_server_process`
   spawns with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`
   (singleton.py:382-386) precisely so no session's console events can touch
   it. The event arriving anyway means the detachment does not survive the
   full spawn chain (uv python shim → real worker inherits/acquires a console
   or group the flags were meant to sever), or this backend was born down a
   path that skips those flags. Either way: a process shared by N sessions had
   its lifetime tethered to one of them.
3. **Consequence profile is exactly the user's complaint**: nothing in any log
   explains WHY from the victim sessions' side; proxies just see a dead
   backend; the disconnect looks random because it depends on which unrelated
   terminal/session closed.

## Fixes (defense in depth, all universal)

1. **Ignore SIGBREAK in the backend** (process_cleanup.py:126 registered
   SIGTERM/SIGINT/SIGBREAK all as shutdown). Since no product path uses
   SIGBREAK, honoring it only serves accidental, session-scoped killers.
   Deliberate stops use TerminateProcess (stop/restart/evict) and are
   unaffected. Keep SIGTERM/SIGINT for POSIX operator semantics.
   **SHIPPED**: `_setup_cleanup_handlers` now installs `signal.SIG_IGN` for
   SIGBREAK and `_signal_handler` for SIGTERM/SIGINT, in the same loop, with
   F-809's re-install guard intact (a second install still records the
   disposition it displaced, never our own). Net-zero LOC — the file sits at
   its 1023 grandfathered cap.
2. **Audit the spawn chain for console/group leakage on Windows** — RED test:
   spawn a backend via the real `_start_server_process`, send
   CTRL_BREAK_EVENT to the launcher's group, assert the backend worker
   survives. (The uv python shim between Popen and the worker was the suspected
   leak — the same trampoline that makes recorded pids shims.)
   **PARTLY ANSWERED — see below.**
3. **F-838 (separate finding, files with this one): proxy self-heal.** Even
   with 1+2, backends can die (OOM crash on 2026-08-30 15:46 proved it). A
   proxy whose watchdog confirms DEAD should cold-start/adopt a replacement
   and re-bridge in place instead of exiting — turning any backend death into
   a slow call instead of a session disconnect.

## What shipped, and what the console-immunity test actually proved

`tests/test_sigbreak_immunity.py` (new) carries four pins:

| Pin | RED before the fix? |
|---|---|
| `test_sigbreak_installs_sig_ign_while_sigterm_sigint_shut_down` — hermetic, synthesises `signal.SIGBREAK` so the ubuntu-only CI lane guards a Windows-only defect | **YES** (`SIG_IGN != _signal_handler`) |
| `test_the_real_installed_disposition_is_sig_ign` — unmocked `signal.signal`, real `getsignal` readback, dispositions restored in fixture teardown | **YES** (Windows) |
| `test_a_second_install_still_records_the_original` — F-809 idempotency did not regress | **YES** |
| `test_the_backend_is_spawned_detached_and_in_its_own_group` — the real `_start_server_process` still asks for `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP` | No (regression guard) |
| `test_a_console_break_to_another_group_does_not_kill_the_backend` (integration, Windows) | **No — see below** |

The end-to-end test does what fix 2 asked for: a launcher process created with
`CREATE_NEW_PROCESS_GROUP` (isolated `USERPROFILE`/`HOME` state dir, ephemeral
port, never 19222/52554) spawns a real backend through the real
`singleton._start_server_process`; the test then delivers
`os.kill(launcher_pid, CTRL_BREAK_EVENT)` to the launcher's group and asserts
the backend still answers an `initialize` probe. It asserts the **launcher
dies** first (non-zero exit) so it cannot pass vacuously, and tears down the
whole spawned tree via psutil.

**Residual, stated honestly: this test passed with the fix reverted.** In that
chain — `sys.executable` launcher → `Popen(DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP)` — the console event never reached the backend at
all, so the detachment held and the leak did **not** reproduce. The test is
therefore a regression guard on console immunity end-to-end, not the RED pin
for the leak; the three unit pins above are the RED evidence for the fix.

Two things that narrows for F-838:

1. **The uv trampoline is no longer the leading suspect for THIS event.** The
   flags survived a real spawn from a console-attached parent. (The
   trampoline's extra hop was not exercised here, so it is not cleared — but
   it is no longer the only candidate.)
2. **There is a supported birth path that never sets the flags at all**:
   `cli.py::_cmd_serve` (`stealth-chrome-devtools serve --http`, and
   `stealth-chrome-devtools-mcp --transport http` directly) runs the backend
   **in the foreground of the invoking console**, in that console's process
   group. A backend born that way is console-attached by construction, and a
   Ctrl+Break in — or the closing of — that terminal reaches it. Backend
   163320 started at 15:44:55 and its log records no argv, so its birth path
   cannot be proven from the logs (see F-840); this path is the simplest
   explanation that requires no leak anywhere. Fix 1 covers it either way, and
   Ctrl+C (SIGINT) still stops a foreground serve exactly as before.

Also observed while reading the log dir: `~/.stealth-mcp/logs/backend-boot.log`
had grown to **794 MB** (unbounded append, one file for all boots). Adjacent to
F-840, not fixed here.

## F-840 (adjacent, MED): the log pruner destroyed the post-mortem

`backend-90396.log` and `backend-90396-fault.log` (the worker OOM-killed at
15:46) existed at 16:0x and were PRUNED before 08:31 next morning — the
death evidence was deleted within hours. Dead-backend logs (and fault logs
especially) must be exempt from pruning for a retention window, or pruning
must keep the N most recent dead backends. Without this, every "why did it
disconnect" investigation starts blind.

## Related

- F-820 (shipped, 2.0.7): eliminated FALSE condemnations of busy backends —
  validated live this same day (held busy at 15:44, condemned dead at 15:46).
  The 18:43 event is the complementary gap: a backend that really dies, for a
  reason it should have been immune to.
- F-829 (queued): fingerprint-misread eviction — the other known way the
  product kills a healthy shared backend.
