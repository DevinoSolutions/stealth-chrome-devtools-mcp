# prep: F-809 implementation spec (draft, targeted at 2.0.5)

Read-only scoping pass, 2026-08-02, against **main @ `5f387c1`**. This supersedes the
numbers in `prep_F808_shutdown_noise_spec.md` (written at `8674f6a`, before plan_F808
Task 10 merged); that file remains the evidence trail for the FastMCP/uvicorn call-chain
analysis (its §1-§2) and is still accurate there. Everything below is re-measured at HEAD.

Ledger row: `DESIGN.md:491`. Sentry issues **STEALTH-CHROME-DEVTOOLS-MCP-1J** and **-1H**.

---

## 1. Symptom

On POSIX, a *clean* backend stop (`stealth-chrome-devtools stop`, or a singleton eviction,
both of which reach `singleton._terminate_backend` → `proc.terminate()` = SIGTERM) emits
ERROR-level log records. `observability.sentry_init()` installs
`LoggingIntegration(event_level=logging.ERROR)`, which ships **any** ERROR record from
**any** logger — so every graceful stop becomes 1-3 Sentry events. Windows is unaffected:
`Popen.terminate()` is `TerminateProcess` and runs no handler at all.

The records land on stderr (uvicorn's `StreamHandler`, mcp's root/lastResort), which
production redirects to `<logdir>/backend-boot.log` — **not** `backend-<pid>.log`, because
`logging_setup.configure_logging` sets `propagate = False` on `stealth.<role>`.

## 2. Noise sites, classified

| # | Site | Fires because | Verdict |
|---|---|---|---|
| **N1** | `embedded/process_cleanup.py:114` `_setup_cleanup_handlers` — `signal.signal(SIGTERM/SIGINT/SIGBREAK, self._signal_handler)` at :124/:126/:129 | uvicorn's `capture_signals` installs `handle_exit` on the same three signals *first* (lifespan startup runs inside its body), so ours **replaces** rather than coexists. `should_exit` is never set; uvicorn's whole graceful path is dead code. | **root cause**, not itself a log site |
| **N2** | `embedded/process_cleanup.py:148` `sys.exit(0)` | `SystemExit` is raised between bytecodes on the main thread while it is parked in the loop's `select()`, so it unwinds `run_forever` and cancels the lifespan + streamable-HTTP session tasks abnormally. Produces `uvicorn/lifespan/on.py` `"Exception in 'lifespan' protocol"` + `"Application shutdown failed. Exiting."`, and (when the unwind arrives as an anyio `ExceptionGroup`) `streamable_http_manager` `"Session … crashed"`. | **expected-during-shutdown noise**; fix the unwind, do not silence |
| **N3** | `embedded/server.py:3398` `mcp.run(transport="http", host=…, port=…)` | FastMCP 2.11.2 hard-codes `timeout_graceful_shutdown: 0`; `asyncio.wait_for(coro, 0)` on CPython 3.12 **always** raises, so uvicorn logs `"Cancel N running task(s), timeout graceful shutdown exceeded"` at ERROR on *every* graceful HTTP stop, independent of N1/N2. | **expected-during-shutdown noise**; fix by configuration |

**Not noise sites — checked and cleared:**

- `embedded/server.py:251` `app_lifespan`'s `finally` — its four `debug_logger.log_error`
  calls (:265/:272/:278/:294) are behind `if _SERVE_TRANSPORT != "http"` (:258), the
  deliberate B1 contract. Under the HTTP backend the whole teardown block is a no-op, so
  it contributes **zero** shutdown ERRORs. Leave it alone.
- `cli.py:413 _cmd_stop` / `:433 _cmd_restart` — thin front-ends over
  `singleton.stop_backend()` / `restart_backend()`; they only `print()` and return an
  exit code. No logging at all.
- `embedded/singleton.py:283 _terminate_backend` — swallows `psutil.Error`/`OSError`,
  logs nothing. `singleton.py:890` `_logger.error("backend did not become ready…")` is a
  cold-**start** failure in the proxy, not a shutdown path.
- `process_cleanup.activate()` (:54) — installs handlers; no error logging.

## 3. Fix, per site

**N1+N2 (one change).** Record the handler we displace and hand the signal back to it.
In `__init__` (:44, must stay side-effect-free — plain assignments are fine):
`self._previous_signal_handlers: dict[int, object] = {}` and
`self._shutdown_in_progress = False`. **Use `object`, not `Any`** — `typing.Any` is a
banned-api in `pyproject.toml` (TID251 + ANN401); the old spec's `dict[int, Any]` would
fail lint. In `_setup_cleanup_handlers`, capture each `signal.signal(...)` return value
(that *is* the displaced handler) into the dict, keeping the existing `hasattr` /
`sys.platform == "win32"` gating. In `_signal_handler`, rename `_frame` → `frame` and:

- if the previous handler is `callable(...)` **and** is not `signal.default_int_handler`,
  call it first (it is uvicorn's `handle_exit`, a cheap flag set — do it before the slow
  cleanup so a cleanup failure cannot strand the server), then run the cleanup, then
  **return** into the interrupted frame. No `sys.exit`.
- otherwise (SIG_DFL / SIG_IGN — i.e. standalone stdio) fall through to today's
  `self._cleanup_all_tracked(); sys.exit(0)`, byte-identical 1.x behaviour.
- the cleanup goes in a small `_run_shutdown_cleanup(self) -> None` guarded by
  `_shutdown_in_progress` (a second SIGTERM must not re-enter mid-`rmtree`) with one
  `except Exception` carrying a `debug_logger.log_error` (which satisfies
  `tests/test_no_silent_excepts.py` — do **not** add an allowlist entry).

Excluding `default_int_handler` is load-bearing: under stdio, SIGINT's prior disposition
*is* that handler, and delegating would swap today's clean `sys.exit(0)` for a
`KeyboardInterrupt` unwind. Keep `atexit.register(self._cleanup_all_tracked)` (:121) — it
is the only path for a plain interpreter exit.

**N3.** Pass `uvicorn_config={"timeout_graceful_shutdown": _GRACEFUL_SHUTDOWN_SECONDS}`
to `mcp.run` in the `http` branch, with a module-level `_GRACEFUL_SHUTDOWN_SECONDS = 2.0`
near `_SERVE_TRANSPORT` (:224). Plumbing is verified: FastMCP does
`config_kwargs.update(uvicorn_config or {})`, so the caller's value beats its hard-coded
`0`. Pick the value against `singleton._terminate_backend`'s `proc.wait(timeout=5)` window
(:305) — 2.0 s leaves ~3 s for the cleanup. Never `None` (uvicorn's "wait forever": an
open SSE stream would hang to SIGKILL).

**Rejected shapes** (reasons in the long spec §3.1, still valid): dropping our handler and
relying on atexit — `capture_signals` re-raises the captured signal against the restored
`SIG_DFL`, killing the process before interpreter shutdown; and filtering/`ignore_logger`
the records — that also silences a genuinely failed shutdown, which is the diagnostic
being masked.

## 4. LOC-budget payment plan

Re-measured at HEAD; both files are at cap with **zero** headroom, exactly as before:

| File | actual | cap (`tools/check_file_budgets.py`) |
|---|---|---|
| `embedded/server.py` | 3401 | 3401 (raise ratified by the human gate 2026-08-02, PR #57) |
| `embedded/process_cleanup.py` | 1023 | 1023 (plan_F808 Task 10, net −31) |

plan_F808 Task 10 has **merged** — `embedded/browser_pid_registry.py` exists (407 LOC), so
the long spec's §4 "sequence after Task 10" is satisfied and its collision analysis is now
historical. F-809 touches `__init__`, `_setup_cleanup_handlers`, `_signal_handler`, adds
`_run_shutdown_cleanup`; it only *calls* `_cleanup_all_tracked`, so Task 10's merge-safe
rewrite composes cleanly.

Payment:

1. **`process_cleanup.py` (needs ~+20).** A real in-file payment exists: the boilerplate
   `Args:`/`Returns:` docstring blocks on `_setup_cleanup_handlers` (:115-120) and
   `_signal_handler` (:132-140) are ~13 lines that say nothing the signature does not.
   Ruff's `D` rules are **off** (`pyproject.toml` documents the decision), so collapsing
   each to a one-liner is legal and lands the change near net-zero. Take this first.
2. **`server.py` (needs ~+4: one constant, one comment line, the kwarg).** No honest
   in-file offset; this needs a cap raise 3401 → measured. That is a **human gate** item
   (the 2026-07-12 `plan_M4ph1` C1 ruling: cap == actual, never pad, raises get flagged).
   Put it in the PR body citing the C4/M13 `browser_manager.py` precedent.
3. Do **not** route the handler into a new `shutdown_signals.py` leaf to dodge the gate —
   that splits the handler from the cleanup it triggers, i.e. a second home for one
   concern (CLAUDE.md convention 4).
4. Ratchet both caps to the printed value after `ruff format` +
   `python tools/check_file_budgets.py`, in the same commit, with an owner comment naming
   `plan_F809`.

## 5. Test pins that must move

Grepped `tests/` for `_signal_handler` / `_setup_cleanup_handlers` / `_cleanup_all_tracked`
/ `SIGTERM` / `send_signal`. Findings:

- **There is no pin on `_signal_handler` anywhere.** Nothing asserts today's
  `sys.exit(0)`, so no existing assertion has to be *rewritten* — the work is additive.
- Every test that constructs `ProcessCleanup` patches `_setup_cleanup_handlers` to a
  no-op: `tests/test_process_cleanup.py:42`, `tests/test_exception_handling.py:245`,
  `tests/test_process_cleanup_import_guard.py:24/30/45/56/68/83`. **Keep that idiom** —
  the new unit pins must not install real handlers except the one that has to, and that
  one restores every snapshotted disposition in teardown.
- `tests/test_process_cleanup_import_guard.py` pins "`__init__` installs no handlers and
  runs no recovery". The two new attributes are plain assignments and do not violate it,
  but **re-run this file** — it is the contract the `__init__` edit is closest to.
- `tests/test_lifespan_reentrancy.py:51/134` owns the `app_lifespan` teardown contract
  (`close_all` / `_cleanup_all_tracked` / `clear_all` run once, and not under HTTP).
  F-809 does not change `app_lifespan`, so this should stay green — treat a failure here
  as a signal the change leaked into the lifespan.
- New pins to add: unit file for the handoff (delegate called, no `SystemExit`; setup
  records the displaced handler; SIG_DFL still exits 0; `default_int_handler` is *not*
  delegated to; cleanup runs once across re-entrant signals; a raising cleanup still hands
  off) — see the long spec §5.1 for the six-row table. Plus one `server.py` pin asserting
  `mcp.run` carries a **positive** `timeout_graceful_shutdown` (assert positivity, not
  `2.0`, so tuning needs no golden update), and one POSIX-only `@pytest.mark.integration`
  end-to-end pin: SIGTERM a real HTTP backend, assert exit 0, assert the **positive**
  marker `Application shutdown complete` is present (this is what makes the pin immune to
  a log-suppression "fix"), and assert no `^(ERROR|CRITICAL)` line and no `Traceback`.
- **Integration landmine:** `tests/conftest.py` `setdefault`s `STEALTH_MCP_NO_AUTO_RECOVERY=1`
  and the child inherits it; `activate()` then returns **before** installing any handler,
  uvicorn's survives, and the test passes for entirely the wrong reason. Set it to `"0"`
  explicitly in the child env with a comment. (Production is unaffected —
  `singleton._start_backend_holding_lock` pops the key from `child_env`.)

## 6. Out of scope

- **`activate()`'s coupling** (:54): `no_auto_recovery` disables signal-handler
  installation as a side effect, not just orphan recovery. Arguably wrong, deserves its
  own finding; do not change it here.
- **`app_lifespan`'s HTTP no-op teardown** — the deliberate B1/RELEASE-FIX-B contract.
- **The `debug_logger.log_error` → Sentry volume problem** (`DESIGN.md:495`, issue `-P`,
  132 events from one user `SyntaxError`). Same *destination*, unrelated *cause*; a
  separate open ledger row.
- **Shrinking `_cleanup_all_tracked`** to fit the 5 s eviction window. If the integration
  pin shows the window is tight with several live browsers, raise it as a new finding.
- **A Windows twin** of the integration pin (would need `CREATE_NEW_PROCESS_GROUP` +
  `CTRL_BREAK_EVENT`); low value, 1J/1H are Linux.
- No new `STEALTH_MCP_*` knob — unknown keys crash `get_settings()`, and the house
  preference is universal defaults over config knobs.
