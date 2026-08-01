# Evidence: Windows startup-herd hang on CI (feeds F-509) — 2026-08-01

Captured while releasing 2.0.3. Recorded here because the failing job produced
**no pytest report**, so nothing in the release-evidence bundle preserves it.

## What happened

Commit `b628b61` (the 2.0.3 release commit) ran the full gate matrix **twice**,
concurrently, from two triggers:

| Run | Trigger | Result |
|---|---|---|
| `30708485317` "Publish to PyPI" | tag `v2.0.3` | **33/33 green** → published |
| `30708274939` "CI" | push to `main` | RED: `transport (Windows/X64)`, `integration (Windows/X64)` |

Same commit, same tree, same runner image (`win25-vs2026` / `20260714.173.1`),
same Chrome (`150.0.7871.115` — verified identical in both runs'
`chrome-identity.json`). One passed, one failed. The failure is therefore
**timing-dependent, not a property of the tree**.

## The transport symptom

`tests/test_startup_herd.py::test_forty_cold_sessions_are_all_usable_within_30s`
hit `HERD_HARD_TIMEOUT_SECONDS` (240s). Sessions were blocked in
`client.list_tools()` — i.e. past the locally-answered `initialize`, waiting on
the backend that never served them:

```
tests\test_startup_herd.py:185: in _one_session
    tools = await client.list_tools()
...
mcp\shared\session.py:292: in send_request
    response_or_error = await response_stream_reader.receive()
E   asyncio.exceptions.CancelledError
→ TimeoutError (240s)
```

This is the second bullet of the test's own docstring — "the lock race or the
readiness-poll backoff left someone behind (**the F-509 window** where a
half-born backend's port can be misclassified as foreign lives here)". The
hosted Windows runner is exactly the environment that enables F-509: detached
process cmdlines are not visible to `psutil` there, so `_backend_pid_on_port`
comes back empty and `_port_is_foreign_held` can misread a healthy,
half-born backend's port as foreign.

## The integration symptom

`integration (Windows/X64)` in the same run produced `"pytest": null` and no
`junit.xml` at all — the job hung and was killed rather than failing a test.
Its evidence artifact is 1733 bytes against 10213 for the green run's.

## Two gaps this exposed

1. **The herd test loses all its diagnostics on the hard-timeout path.**
   `_booted_backend_logs` / `_our_backends_on_port` are only read *after*
   `asyncio.gather` returns, so the one failure mode that most needs the
   backend logs is the one that discards them. The timeout path should dump
   `workspace_backend_logs` before raising.
2. **No release-evidence is emitted for a job that hangs**, so the aggregate
   can only say "pytest: null" — true, but it names no cause.

## Not the cause

* **F-806** (`reconcile_launched_browser_version`, an unbounded CDP
  `Browser.getVersion` on the spawn path) was reverted out of `b628b61`
  before this run. The hang reproduced without it.
* **Sentry-on-by-default** (the 2.0.3 change) is paid by the *backend* and the
  ops CLI only — `server.py::main()` routes stdio to `run_stdio_proxy()` and
  returns before `runpy`, so no proxy calls `sentry_init()`. It adds ~1.9s to a
  one-time backend cold start, which does not explain a 240s hang, and it was
  equally present in the green run.
