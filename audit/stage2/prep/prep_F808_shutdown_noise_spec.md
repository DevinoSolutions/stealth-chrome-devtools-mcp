# prep: implementation spec for F-809 — a clean POSIX shutdown must emit zero ERROR logs

Produced 2026-08-02 by a read-only analysis agent against the MAIN checkout at
HEAD `8674f6a`. Sentry issues **STEALTH-CHROME-DEVTOOLS-MCP-1J** and
**STEALTH-CHROME-DEVTOOLS-MCP-1H** (release 2.0.3, teammate's Linux machine,
backend `python .../embedded/server.py --transport http --port 19222`).

Everything below is anchored to **symbols**, not line numbers, because
plan_F808 Task 10 is concurrently moving code inside `process_cleanup.py`.
Line numbers appear only as a reading aid and are marked as of `8674f6a`.

**Suggested finding id: `F-809`.** F-800…F-808 are all taken
(`audit/stage2/finding_F80*.md`, `CHANGELOG.md`, `DESIGN.md`); F-809 is the
next free slot. Sequence this **after** plan_F808 Task 10 lands — see §6.

> Landmine encountered while producing this spec, worth repeating: a shell
> `grep -rhoE "F-8[0-9]{2}"` through the rtk shim returned **only `F-802`**.
> The Grep tool over the same tree returned F-800 through F-808 across ~30
> files. Do not trust a shell-grep absence in this repo.

---

## 1. What actually happens on SIGTERM today

### 1.1 The call chain that installs signal handlers

`embedded/server.py` `main()` ends with, for the HTTP backend:

```python
mcp.run(transport="http", host=args.host, port=args.port)
```

FastMCP 2.11.2 (`.venv/.../fastmcp/server/server.py`):

- `FastMCP.run(transport, show_banner=True, **transport_kwargs)` →
  `anyio.run(partial(self.run_async, ...))` (:340-359)
- `run_async` → `run_http_async(transport=..., **transport_kwargs)` (:331-336)
- `run_http_async` builds `uvicorn.Config(app, host, port, **config_kwargs)` and
  `await server.serve()` (:1508-1526). **It hard-codes
  `config_kwargs = {"timeout_graceful_shutdown": 0, "lifespan": "on"}` and then
  `config_kwargs.update(uvicorn_config or {})`** — so a caller-supplied
  `uvicorn_config` wins. Remember this; §3.2 depends on it.

uvicorn 0.35.0 (`.venv/.../uvicorn/server.py`):

- `Server.serve()` → `with self.capture_signals(): await self._serve(...)`
- `capture_signals` (:313-331) does
  `original_handlers = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}`
  where `HANDLED_SIGNALS = (SIGINT, SIGTERM)` plus `SIGBREAK` on win32 (:31-36).
  It deliberately uses `signal.signal`, **not** `loop.add_signal_handler` — the
  module comment says so explicitly (:319-320).
- `handle_exit` (:333-338) appends to `self._captured_signals` and sets
  `self.should_exit = True`. `main_loop`/`on_tick` polls that flag every 100 ms
  (:224-254) and then runs `shutdown()`.
- `_serve()` → `startup()` → `await self.lifespan.startup()` — **our
  `app_lifespan` runs here, i.e. strictly after `capture_signals` installed
  uvicorn's handlers.**

`embedded/server.py` `app_lifespan` calls `process_cleanup.activate()`, which
(`embedded/process_cleanup.py`, `ProcessCleanup.activate`) returns early on
`get_settings().no_auto_recovery` and otherwise calls
`_setup_cleanup_handlers()`:

```python
atexit.register(self._cleanup_all_tracked)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, self._signal_handler)
if hasattr(signal, "SIGINT"):
    signal.signal(signal.SIGINT, self._signal_handler)
if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, self._signal_handler)
```

### 1.2 FINDING — our handler REPLACES uvicorn's, it does not coexist

This is the load-bearing fact and it changes the fix.

Both parties use `signal.signal` on the same three signals, and **ours runs
second** (lifespan startup happens inside `capture_signals`' body). The C-level
disposition for SIGTERM/SIGINT therefore ends up pointing at
`ProcessCleanup._signal_handler`. `uvicorn.Server.handle_exit` is never invoked
for the lifetime of the serve, `should_exit` is never set, and uvicorn's entire
graceful-shutdown path is unreachable dead code in this process.

Two corollaries the implementer must not break:

1. `capture_signals`' `finally` restores `original_handlers` — the
   **pre-uvicorn** dispositions, snapshotted before ours existed. Our handler is
   silently discarded at that point. Harmless today, but it means "our handler
   is installed forever" is false.
2. After restoring, `capture_signals` does
   `for captured_signal in reversed(self._captured_signals): signal.raise_signal(captured_signal)`
   (:327-331). For SIGTERM the restored disposition is `SIG_DFL` (nothing
   installed a SIGTERM handler before uvicorn), so that re-raise **terminates
   the process at the kernel level — `atexit` handlers never run.** This is why
   fix shape (b) is rejected in §3.1.

### 1.3 The unwind that produces the Sentry events

`ProcessCleanup._signal_handler` runs `self._cleanup_all_tracked()` and then
`sys.exit(0)`. CPython runs Python-level signal handlers between bytecodes on
the main thread; the main thread is inside the event loop (parked in
`selectors.select()`), so the `SystemExit(0)` is raised *inside the running
loop*, unwinds `run_forever`, and `asyncio.run`'s teardown cancels every pending
task — the uvicorn lifespan task and the streamable-HTTP session task groups
among them.

ERROR-level records that result (each one is a Sentry event, because
`observability.py` `sentry_init()` installs
`LoggingIntegration(event_level=logging.ERROR)` plus `AsyncioIntegration()`):

| # | Emitter | Record |
|---|---|---|
| E1 | `uvicorn/lifespan/on.py` `LifespanOn.main`, `except BaseException` (:87-97) | `logger.error("Exception in 'lifespan' protocol\n", exc_info=exc)` |
| E2 | `uvicorn/lifespan/on.py` `LifespanOn.shutdown` (:72-74) | `logger.error("Application shutdown failed. Exiting.")` — fires when `error_occured` was set by E1's path |
| E3 | `mcp/server/streamable_http_manager.py`, `except Exception` around `self.app.run(...)` (:285-286) | `logger.exception(f"Session {id} crashed")` — conditional: a bare `CancelledError`/`SystemExit` is `BaseException` and escapes this clause, but an anyio `ExceptionGroup` (an `Exception` subclass) does not |

E3's conditionality explains the reported "1-2 events per shutdown" rather than
a fixed count. **Do not treat this table as exhaustive** — the pin in §5 asserts
*zero* ERROR records and captures the real ones; use the table as a map, not a
checklist.

### 1.4 SECOND, INDEPENDENT NOISE SOURCE — this one survives fixing §1.2

Even with a perfectly graceful uvicorn shutdown, `Server.shutdown()` does:

```python
try:
    await asyncio.wait_for(self._wait_tasks_to_complete(),
                           timeout=self.config.timeout_graceful_shutdown)
except asyncio.TimeoutError:
    logger.error("Cancel %s running task(s), timeout graceful shutdown exceeded", ...)
```
(`uvicorn/server.py:278-289`)

FastMCP hard-codes `timeout_graceful_shutdown: 0`. On CPython 3.12.12
(this repo's interpreter), `asyncio.wait_for(coro, timeout=0)` wraps the awaitable
in `ensure_future`, finds the fresh task not-yet-done, cancels it, and **always**
raises `TimeoutError` — it never gets a chance to complete. Verified empirically
against `.venv` (a coroutine that returns immediately still raised
`TimeoutError`).

**So `logger.error("Cancel N running task(s), timeout graceful shutdown exceeded")`
fires on every single graceful HTTP shutdown, forever, independent of our signal
handler.** Fixing only §1.2 would trade E1/E2/E3 for this one and leave the
Sentry noise at one event per shutdown. Both halves are required to reach zero.

### 1.5 Platform differences

- **POSIX**: as described. `singleton`'s evictor is
  `proc.terminate()` → `proc.wait(timeout=5)` → `proc.kill()`
  (`embedded/singleton.py:313-318`), so `terminate()` is SIGTERM and there is a
  **5-second budget** before SIGKILL. Whatever the handler does must finish
  inside it (see §3.4).
- **Windows**: `Popen.terminate()` is `TerminateProcess` — no handler of any
  kind runs, ours or uvicorn's, and nothing is logged. That is why 1J/1H are
  POSIX-only. `SIGBREAK` (Ctrl+Break to a `CREATE_NEW_PROCESS_GROUP` child) is
  the only Windows path that reaches a handler; `_setup_cleanup_handlers`
  already installs for it, and uvicorn already has it in `HANDLED_SIGNALS`, so
  the same replace-not-coexist defect exists there and the same fix covers it.
- Neither party uses `loop.add_signal_handler`, so there is **no** asyncio
  wakeup-fd interaction to reason about. Do not introduce one: mixing
  `add_signal_handler` with uvicorn's `signal.signal` would reintroduce exactly
  the clobbering this fix removes.

---

## 2. Why the ERROR records become Sentry events

`observability.py` `sentry_init()` is on by default (hardcoded `_DSN`), opt-out
only via `STEALTH_MCP_NO_ERROR_REPORTING`, and installs
`LoggingIntegration(event_level=logging.ERROR)`. That integration ships **any**
`logging` record at ERROR or above from **any** logger, not just ours. uvicorn's
`uvicorn.error` logger and mcp's module loggers are therefore in scope.

Note the destination: `logging_setup.configure_logging` gives `stealth.<role>`
its own `RotatingFileHandler` with `propagate = False`, so uvicorn/mcp records
do **not** land in `backend-<pid>.log`. They go to `sys.stderr` (uvicorn's
`LOGGING_CONFIG` handler is a `StreamHandler` on `ext://sys.stderr`; mcp's
loggers propagate to a handler-less root and fall through to `logging`'s
lastResort handler, also stderr). In production stderr is redirected to
`<logdir>/backend-boot.log` by `singleton._start_backend_holding_lock`
(`stdout`/`stderr` both → the `backend-boot.log` handle). **`backend-boot.log`
is where the operator sees this, and stderr is what the test in §5.2 must
capture.**

Do not "fix" this by adding `ignore_logger(...)` to `observability.py` — see
§3.1(c).

---

## 3. The fix

### 3.1 Chosen shape: **(a) — hand the signal back to the host server**, plus the §1.4 config half

Rejected alternatives, with reasons grounded in the code above:

- **(b) "don't install our SIGTERM handler when uvicorn's is present; hook
  cleanup into lifespan/atexit instead."** *Rejected.* `atexit` does not run:
  `capture_signals` re-raises the captured signal after restoring the `SIG_DFL`
  disposition (§1.2 corollary 2), killing the process at the kernel level before
  interpreter shutdown. And lifespan-shutdown is not a substitute either —
  `app_lifespan`'s `finally` is explicitly a **no-op under HTTP** (`if
  _SERVE_TRANSPORT != "http"`, the deliberate B1/RELEASE-FIX-B contract: the
  lifespan runs once per MCP *session*, so session exit must not tear down
  shared browsers). Taking (b) would silently regress browser reaping on every
  POSIX SIGTERM. Do not take it.
- **(c) suppress or reclassify the resulting log records.** *Rejected as the
  primary fix.* E1/E2/E3 are truthful reports of an abnormal unwind that we
  cause; silencing them would also silence a *genuinely* failed shutdown, which
  is precisely the diagnostic the team-lead brief says is currently being
  masked. Fix the unwind. (§1.4's E4 is not our unwind, but its honest fix is
  still configuration — give uvicorn a real grace budget — not a filter.)

**(a)** is the structurally honest one: the process has exactly one host server;
our handler should do its cleanup and then let that host run its own documented
shutdown, instead of ejecting from under it.

### 3.2 Change 1 — `embedded/server.py` `main()`: give uvicorn a real grace budget

Anchor: the `if args.transport == "http":` branch at the end of `main()` (as of
`8674f6a`, `server.py:3386-3389`).

```python
if args.transport == "http":
    # F-809: FastMCP hard-codes timeout_graceful_shutdown=0, and
    # asyncio.wait_for(..., timeout=0) ALWAYS raises TimeoutError, so uvicorn
    # logs "Cancel N running task(s), timeout graceful shutdown exceeded" at
    # ERROR on every clean shutdown — one Sentry event per stop. A real budget
    # lets the connections drain quietly. Keep it well under singleton's
    # 5s terminate->kill window (singleton._evict / proc.wait(timeout=5)).
    mcp.run(
        transport="http",
        host=args.host,
        port=args.port,
        uvicorn_config={"timeout_graceful_shutdown": _GRACEFUL_SHUTDOWN_SECONDS},
    )
```

with a module-level `_GRACEFUL_SHUTDOWN_SECONDS = 2.0` near `_SERVE_TRANSPORT`.

Plumbing is verified: `FastMCP.run(**transport_kwargs)` → `run_async` →
`run_http_async(uvicorn_config=...)`, and `config_kwargs.update(user_config)`
means the caller value overrides FastMCP's `0`.

Pick the value against the 5 s eviction window, not against comfort: 2.0 s
leaves ~3 s of slack for the cleanup in §3.3. Do not use `None` (uvicorn's
"wait forever") — an open SSE stream would hang until SIGKILL.

### 3.3 Change 2 — `ProcessCleanup._setup_cleanup_handlers` / `_signal_handler`

Record what we displace, and hand control back to it.

`ProcessCleanup.__init__` (must stay side-effect-free — the contract
`tests/test_process_cleanup_import_guard.py` enforces is *no handler
installation, no recovery*; a plain attribute assignment is fine):

```python
self._previous_signal_handlers: dict[int, Any] = {}
self._shutdown_in_progress = False
```

`_setup_cleanup_handlers` — same signals, but capture the return value of
`signal.signal`, which **is** the handler being displaced:

```python
for signum in self._shutdown_signals():
    self._previous_signal_handlers[signum] = signal.signal(signum, self._signal_handler)
```

(Keep the existing `hasattr` / `sys.platform == "win32"` gating; a small private
`_shutdown_signals()` generator is fine, or keep the three `if` blocks and add
one assignment each — whichever costs fewer lines, see §6.)

`_signal_handler` — the behavioural core:

```python
def _signal_handler(self, signum, frame):
    debug_logger.log_info(
        "process_cleanup", "signal_handler",
        f"Received signal {signum}, initiating cleanup...",
    )
    delegate = self._previous_signal_handlers.get(signum)
    if callable(delegate) and delegate is not signal.default_int_handler:
        # A host server (uvicorn) owned this signal before us. Set its
        # graceful-stop flag FIRST so a failure in our cleanup cannot strand
        # the server, then clean up and return into the interrupted frame —
        # the loop unwinds through uvicorn's own shutdown path, not a
        # SystemExit erupting mid-select.
        delegate(signum, frame)
        self._run_shutdown_cleanup()
        return
    self._run_shutdown_cleanup()
    sys.exit(0)
```

with

```python
def _run_shutdown_cleanup(self) -> None:
    """Idempotent, never-raising cleanup for the signal path."""
    if self._shutdown_in_progress:
        return
    self._shutdown_in_progress = True
    try:
        self._cleanup_all_tracked()
    except Exception as error:
        debug_logger.log_error("process_cleanup", "signal_handler", error)
```

Four decisions worth stating so a reviewer does not "simplify" them away:

1. **`callable(delegate)` guard.** `signal.signal` returns `signal.SIG_DFL` /
   `signal.SIG_IGN` (`Handlers` enum ints, not callables) when nothing was
   installed. Under standalone stdio, SIGTERM's previous disposition is
   `SIG_DFL` → falls through to `sys.exit(0)`, so **the 1.x stdio contract is
   byte-identical**.
2. **`is not signal.default_int_handler`.** Under stdio, SIGINT's previous
   handler *is* `default_int_handler`, which raises `KeyboardInterrupt` — a
   noisier unwind than today's `sys.exit(0)` and a gratuitous behaviour change.
   Excluding it keeps stdio Ctrl-C exactly as it is while still delegating to a
   real host server.
3. **Delegate before cleanup.** `handle_exit` is a cheap flag set; the cleanup
   is seconds of `psutil` scanning and `shutil.rmtree`. Setting the flag first
   means a cleanup failure cannot leave the server running.
4. **Re-entrancy guard.** The cleanup is slow and a second SIGTERM (or the
   evictor's follow-up) would otherwise re-enter it mid-`rmtree`. One bool is
   enough; do not build a lock.

**Leave `atexit.register(self._cleanup_all_tracked)` in place.** It is the only
cleanup path for a plain interpreter exit, and `_cleanup_all_tracked` is
already a no-op when `browser_processes` is empty. It will *not* run after an
HTTP SIGTERM (§1.2 corollary 2) — that is expected, because the handler already
ran it.

### 3.4 Timing budget (state it, do not silently rely on it)

SIGTERM → handler (cleanup: `psutil.process_iter` twice per tracked instance +
`terminate`/`wait(3)`/`kill`/`wait(2)` per browser + up to 5 × 0.15 s rmtree
retries) → uvicorn notices `should_exit` within 100 ms → `shutdown()` drains for
up to `_GRACEFUL_SHUTDOWN_SECONDS`. The whole thing must land inside
`singleton`'s 5 s `proc.wait(timeout=5)` or the evictor SIGKILLs and the
shutdown is unclean again (quietly — a SIGKILL logs nothing, so the §5.2 pin
would still pass). **The pin must therefore also assert a clean exit and the
positive "Application shutdown complete" marker, not merely the absence of
ERROR lines.** That is spelled out in §5.2.

This spec does **not** attempt to shrink the cleanup itself. If the integration
pin shows the 5 s window is tight with several live browsers, raise it as a
separate finding rather than widening scope here.

### 3.5 Explicitly out of scope (but noted)

`activate()` returns early on `get_settings().no_auto_recovery`, so that knob
disables **signal-handler installation** as a side effect, not just orphan
recovery. That coupling is arguably wrong (an operator who turns off startup
recovery also loses shutdown cleanup), and it is a live hazard for the test in
§5.2 (see the landmine there). Do not change it as part of F-809 — it alters a
documented knob's behaviour and deserves its own finding. Just be aware of it.

---

## 4. Collision analysis with plan_F808 Task 10

Task 10's spec is `audit/stage2/prep/prep_F808_T10_anchored_spec.md`. Its scope
inside `process_cleanup.py`:

- **Moved out** to a new `embedded/browser_pid_registry.py` leaf (thin
  delegating wrappers left behind): `_normalize_path`,
  `_normalize_process_metadata`, `_LOCK_RETRIES` / `_LOCK_RETRY_DELAY` /
  `_file_lock`, `_load_tracked_pids`, `_save_tracked_pids`, `_clear_pid_file`.
- **Modified**: `track_browser_process` (adds `owner_pid` /
  `owner_create_time`), `_recover_orphaned_processes`,
  `_kill_processes_for_metadata`; adds `_owner_backend_alive`.
- **Touched indirectly**: `_cleanup_all_tracked` (must become merge-safe,
  because it calls `_save_tracked_pids` / `_clear_pid_file`).

F-809's scope: `__init__` (two new attributes), `_setup_cleanup_handlers`,
`_signal_handler`, a new `_run_shutdown_cleanup`, and `server.py`'s `main()`.

**Verdict: no functional collision.** Task 10's spec never mentions
`_setup_cleanup_handlers` or `_signal_handler`, and F-809 does not touch the
pid-file record, the normalizer, the lock, or the owner logic. The only shared
symbol is `__init__` (trivial, additive) and `_cleanup_all_tracked`, which F-809
*calls* but does not modify — so Task 10's merge-safety rewrite of it composes
cleanly.

**The real contention is the LOC budget** (§6). Sequence F-809 **after** Task 10
merges, and re-measure — never assume 1054.

One coordination note: after Task 10, a SIGTERM'd backend's
`_cleanup_all_tracked` will be merge-safe, so the F-809 handler's cleanup stops
being able to wipe a sibling backend's entries. That is a strict improvement and
needs no F-809 work; just do not re-order the two.

---

## 5. Tests

Repo conventions that bind here: the hermetic lane is `-m "not integration"`;
fixtures patch module attributes rather than touching real state; nothing may
write to the real `~/.stealth-mcp`; `tests/conftest.py` session-wide
`setdefault`s `STEALTH_MCP_NO_AUTO_RECOVERY=1`, `STEALTH_MCP_NO_ERROR_REPORTING=1`
and `STEALTH_MCP_CLONE_OUTPUT_DIR`; the autouse `_reset_settings_cache` fixture
clears `get_settings`' `lru_cache` around every test.

The existing suite has **no pin at all** on `_signal_handler` — every test that
touches `ProcessCleanup` patches `_setup_cleanup_handlers` to a no-op
(`tests/test_process_cleanup.py:41`, `tests/test_exception_handling.py:245`,
`tests/test_process_cleanup_import_guard.py`). Keep that idiom: unit pins must
not install real handlers except in the one place that has to (U2), and that one
restores them.

### 5.1 Hermetic unit pins — new file `tests/test_shutdown_signal_handoff.py`

Build instances with `ProcessCleanup.__new__(ProcessCleanup)` and set only the
attributes the handler reads (the idiom at `tests/test_process_cleanup.py:312-319`).
No `tmp_path` pid-file is needed for U1/U3-U6 because `_cleanup_all_tracked` is
patched out.

| Pin | Setup | Assertion | RED today? |
|---|---|---|---|
| **U1** `test_handler_delegates_to_host_server_handler` | `_previous_signal_handlers = {SIGTERM: recorder}`, `_cleanup_all_tracked` patched to record | `recorder` called once with `(SIGTERM, None)`; **no `SystemExit` raised**; cleanup ran | **Yes** — today it raises `SystemExit` and never calls a delegate |
| **U2** `test_setup_records_the_handler_it_displaces` | fixture snapshots `signal.getsignal` for SIGINT/SIGTERM(/SIGBREAK) and restores in teardown; install a sentinel for SIGTERM; call `pc._setup_cleanup_handlers()` | `pc._previous_signal_handlers[SIGTERM] is sentinel` | **Yes** — the dict does not exist |
| **U3** `test_stdio_fallback_still_exits_zero` | `_previous_signal_handlers = {SIGTERM: signal.SIG_DFL}` | `pytest.raises(SystemExit)` with `.code == 0` | No — guards the 1.x contract against regression |
| **U4** `test_sigint_default_int_handler_is_not_delegated_to` | `{SIGINT: signal.default_int_handler}` | `SystemExit`, **not** `KeyboardInterrupt` | No — guards decision 3.3(2) |
| **U5** `test_cleanup_runs_once_across_reentrant_signals` | call `_signal_handler` twice with a delegate present | `_cleanup_all_tracked` recorded exactly once; delegate called twice | **Yes** |
| **U6** `test_cleanup_failure_still_hands_off` | `_cleanup_all_tracked` raises `RuntimeError` | delegate was called; nothing propagates out of `_signal_handler` | **Yes** |

U2 caveat: it is the only pin that calls the real `signal.signal`. Guard it with
`pytest.mark.skipif(threading.current_thread() is not threading.main_thread())`
and restore every snapshotted disposition in a `finally`/fixture teardown —
leaking a handler would poison the rest of the session.

### 5.2 Integration pin — new file `tests/test_shutdown_is_quiet.py`

`@pytest.mark.integration` **and** `@pytest.mark.skipif(sys.platform == "win32", ...)`
— on Windows `Popen.terminate()` is `TerminateProcess`, no handler runs, and the
premise does not exist. (A Windows twin would need `CREATE_NEW_PROCESS_GROUP` +
`CTRL_BREAK_EVENT`; optional, low value, 1J/1H are Linux.)

Template: `tests/test_proxy_backend_death.py::TestProxyExitsOnBackendDeath`
(`:151-221`) — `_free_port()`, `subprocess.Popen([sys.executable, "-m",
"stealth_chrome_devtools_mcp", "--transport", "http", "--port", str(port),
"--host", "127.0.0.1"], env=env)`, `try/finally` that kills the survivor.

Deltas from that template:

- `stdout=subprocess.PIPE, stderr=subprocess.STDOUT` (uvicorn's ERROR records go
  to **stderr**, mcp's go to root→lastResort→stderr; merging keeps ordering).
- `env["STEALTH_MCP_BROWSER_SESSION_ROOT"]` and `env["STEALTH_MCP_LOG_DIR"]`
  under `tmp_path`.
- Leave `STEALTH_MCP_NO_ERROR_REPORTING=1` inherited from conftest. This test
  must never ship to the real Sentry — that is exactly what HEAD `8674f6a`
  ("Test runs no longer ship injected failures to the real Sentry", ~50k noise
  events in one 15-hour campaign) just finished cleaning up.
- **LANDMINE — `env["STEALTH_MCP_NO_AUTO_RECOVERY"] = "0"`.** conftest
  `setdefault`s it to `"1"`, the child inherits the test process env, and
  `activate()` returns **before** `_setup_cleanup_handlers()` when it is set. A
  backend spawned with the inherited default therefore installs **no handler of
  ours at all**, uvicorn's survives, and the test goes green for entirely the
  wrong reason — proving nothing. (Production does not have this problem:
  `singleton._start_backend_holding_lock` pops the key from `child_env`, which is
  why the fratricide/shutdown paths are live in the field.) Set it explicitly and
  add a comment saying why.
- Wait for readiness before signalling — poll `singleton._backend_http_ready(port)`
  (or a plain socket connect + HTTP probe) with a bounded deadline; signalling a
  backend that has not finished lifespan startup tests nothing.

Assertions, in this order:

1. `backend.send_signal(signal.SIGTERM)`; `backend.wait(timeout=30)`.
2. `assert backend.returncode == 0` — note this is **not** the discriminator:
   today's `sys.exit(0)` also yields 0. It only catches a crash-on-exit.
3. **Positive marker:** the captured output contains `Application shutdown complete`
   (uvicorn `lifespan/on.py:76`). This is what makes the pin resistant to a
   log-suppression "fix" — a silenced logger fails here.
4. **Negative:** no output line matches `^(ERROR|CRITICAL)` (uvicorn's default
   formatter is `%(levelprefix)s %(message)s`, rendering `ERROR:    ...`; colours
   are stripped when stderr is not a TTY) and the output contains no
   `Traceback (most recent call last)`.

Expected RED at HEAD: step 4 fails on `Exception in 'lifespan' protocol` and/or
`Application shutdown failed. Exiting.`, and — even after §3.3 alone —
on `Cancel N running task(s), timeout graceful shutdown exceeded`. That second
failure is the pin proving §3.2 was actually needed; do not weaken it.

### 5.3 One `server.py` pin

`test_http_serve_passes_a_real_graceful_shutdown_budget` — follow the existing
`--transport http` argv idiom in `tests/test_server_entrypoint.py`, patch the
module's `mcp.run`, drive `main()`, and assert the call carries
`uvicorn_config` with a **positive** `timeout_graceful_shutdown`. Assert
positivity, not the literal `2.0`, so tuning the constant does not need a
golden update.

### 5.4 Do not

- Do not run the backend by hand, touch `~/.stealth-mcp`, or run the full suite
  as part of "verifying the analysis" — the hermetic lane plus the one
  integration file is the contract.
- Do not add a `STEALTH_MCP_*` knob for any of this. Unknown `STEALTH_MCP_*`
  keys crash `get_settings()`, and the house preference is universal defaults
  over config knobs.
- Do not add anything to the `tests/test_no_silent_excepts.py` allowlist — the
  `except Exception` in `_run_shutdown_cleanup` carries a `debug_logger.log_error`
  call, which is what that gate requires.

---

## 6. LOC budgets — read this before writing a line

`tools/check_file_budgets.py`: global budget 1000 LOC; grandfathered files may
**never grow**; LOC is `len(read_text().splitlines())` measured after
`ruff format`.

Measured at `8674f6a`:

| File | LOC | Cap | Headroom |
|---|---|---|---|
| `embedded/process_cleanup.py` | 1054 | 1054 | **0** |
| `embedded/server.py` | 3389 | 3389 | **0** |

Both files F-809 must edit are exactly at cap. Task 10 will ratchet
`process_cleanup.py`'s cap **down** to its measured post-extraction LOC (its
spec §5 estimates ~940-965 and instructs the implementer to set cap == printed
value). So after Task 10 the headroom is still **zero, by construction**.

The rule the implementer must respect:

1. **Never pad a cap.** The house ruling (2026-07-12, `plan_M4ph1` C1) is
   cap == actual, measured after `ruff format`, no slack.
2. **Never route code to a new module just to dodge the gate.** A
   `shutdown_signals.py` leaf for ~25 lines would split the signal handler away
   from the cleanup it triggers — a second home for one concern, which is the
   defect lens in `CLAUDE.md` convention 4.
3. Therefore: land the change, run `ruff format`, run
   `python tools/check_file_budgets.py`, and **ratchet both caps to the printed
   LOC** in the same commit, with a comment above each entry in the C1/C4 style
   naming `plan_F809` and the reason (a signal-handoff that cannot be expressed
   in fewer lines). This is a cap **increase**, which is the one case that has
   historically required a human gate ruling — flag it to the team lead in the
   PR body rather than assuming it, and cite the `browser_manager.py` C4/M13
   precedent (in-file split, cap raised to actual, documented rationale).
4. Keep the diff genuinely small so the ask is small: prefer three inline
   assignments in `_setup_cleanup_handlers` over a new `_shutdown_signals()`
   helper if it costs fewer lines; keep the F-809 comments to the two that carry
   non-obvious constraints (why `default_int_handler` is excluded, why the
   graceful budget must stay under 5 s) rather than narrating the change.

---

## 7. Commit / release metadata

- Finding id: **F-809**. Write
  `audit/stage2/finding_F809_clean_posix_shutdown_ships_error_events_to_sentry.md`
  following the shape of `finding_F807_cold_start_lock_can_kill_a_healthy_backend.md`.
- The fix commit body must carry both cross-refs verbatim, on their own lines:
  ```
  Fixes STEALTH-CHROME-DEVTOOLS-MCP-1J
  Fixes STEALTH-CHROME-DEVTOOLS-MCP-1H
  ```
  There is no prior commit in this repo referencing a Sentry issue id
  (`git log --all --grep="STEALTH-CHROME-DEVTOOLS-MCP-"` is empty), so this
  establishes the convention — keep the exact `Fixes <ID>` form Sentry's commit
  integration recognises.
- `CHANGELOG.md` gets an F-809 entry under the target release alongside the
  existing `F-807` / `F-802`-style rows.
- `DESIGN.md`: one short paragraph is warranted — the "who owns the process
  signals" question is now a real architectural rule (*the host server owns
  shutdown; `process_cleanup` cleans and hands back*), and §1.2's
  replace-not-coexist trap will otherwise be rediscovered the next time someone
  adds a handler. Put it near the existing F-807 singleton material.
