# F-1015 — the SIGTERM pin signals the backend before uvicorn is serving (T-5901)

**Severity:** Medium for CI (a red integration lane), none for users. **Files:** `tests/test_clean_shutdown_noise.py` only. F-1015 was unclaimed.

## 1. Evidence

CI run 39656159105 (main 2b7eef9), integration (macOS/ARM64): `test_sigterm_on_a_real_backend_is_a_clean_shutdown` failed `assert "Application shutdown complete" in log`. The captured
log has `Started server process`, `Waiting for application startup`, `Application startup complete`, `Uvicorn running on http://127.0.0.1:50063`, and then NO `Shutting down`, `Waiting for
application shutdown` or `Application shutdown complete`. Instead: `SystemExit: 0` raised from `process_cleanup._signal_handler` inside uvicorn's `capture_signals` exit
(`signal.raise_signal`), with starlette's lifespan task `CancelledError` as the second exception. That is the shape of a server that left `_serve` WITHOUT calling `shutdown()`.

## 2. Cause

`uvicorn.Server._serve` is `await self.startup(); if self.should_exit: return; await self.main_loop(); await self.shutdown()`. The lifespan logs `Application startup complete` while
`startup()` has yet to bind the socket, log `Uvicorn running`, and return. The test waited on the lifespan's line (polled every 0.25 s) and sent SIGTERM at once; a SIGTERM that lands
before the `should_exit` check sets `should_exit` through uvicorn's handler and makes `_serve` return early: no `shutdown()`, the lifespan task is cancelled when the process then exits
through `capture_signals`' re-raise into our handler (`sys.exit(0)`), and the log has the traceback the test forbids. Not the F-1013 Host/Origin guard (it is middleware on requests and
this sequence has none) and not a health-route change; a timing window.

Reproduced the mechanism (not the macOS run: the pin is POSIX-only and this box is Windows) with a plain `uvicorn.Server` whose lifespan raises SIGTERM on the loop right after the startup
log: `Uvicorn running on ...` and then nothing, no `Shutting down` / `Application shutdown complete`, and the lifespan's shutdown never ran; the control (SIGTERM once `server.started`) prints
`Shutting down`, `Waiting for application shutdown`, the lifespan exit, `Application shutdown complete`, `Finished server process`.

## 3. Fix

The pin now also requires the server to ANSWER HTTP (`_answers_http`, any status) before it signals. Nothing else runs on uvicorn's loop until `startup()` has returned and the
`should_exit` check has passed, so an answer proves the window is closed. The assertions are unchanged (still the positive `Application shutdown complete` marker). A pin on the helper
(`test_a_server_is_ready_only_when_it_answers_http`): a free port, a bound-but-never-served port and a real answering server; the mutation "always ready" fails it.

## 4. Residuals

* Product: a stop sent inside that few-millisecond window still gets the early return (noise only: our handler's `sys.exit(0)` path runs the tracked-browser cleanup, and no browser can exist
  yet). Not changed: it is uvicorn's control flow.
* No RED on the macOS cell was possible from here; the evidence is the log's shape plus the reproduced mechanism above.
