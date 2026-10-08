# F-949 — the proxy's Sentry thread imports `mcp`/`fastmcp` while the main thread imports `mcp.server.stdio`

**Severity:** High. An intermittent crash of the stdio proxy at startup; Claude Code
reports CONNECTION_CLOSED, and the next launch works.
**Files:** `src/stealth_chrome_devtools_mcp/observability.py` (`sentry_init`),
`src/stealth_chrome_devtools_mcp/server.py` (`_start_proxy_error_reporting`),
`tests/test_proxy_sentry_reporting.py` (`TestInitPlacement`).
**Seen:** a live 2.1.23 install (Python 3.13.11, mcp 1.28.1, fastmcp 2.14.7,
sentry_sdk 2.64.0): `server.main` -> `run_stdio_proxy` -> `anyio.run(_bridge)` ->
`from mcp.server.stdio import stdio_server` -> `KeyError: 'mcp.server'` in importlib's
`_find_and_load`.

---

## 1. Cause

`_start_proxy_error_reporting` (F-827) runs `sentry_init` on the daemon thread
`proxy-sentry-init`. `sentry_sdk.init` keeps its default `auto_enabling_integrations`,
so the MCP integration imports `mcp.server.lowlevel`, `mcp.server.streamable_http` and
`fastmcp` on that thread. The main thread meanwhile imports `mcp.server.stdio` in
`_bridge`. Two threads importing the same package at once is the race.

Reproduced here. It surfaces as `KeyError: 'mcp.server'` on the reporter's machine and
as `_DeadlockError` on `mcp.server.lowlevel` on this one: the same import-lock race, a
different symptom of it.

## 2. Fix

`sentry_init(*, auto_enabling_integrations=True)`. The proxy's call passes `False`; the
backend and the ops CLI keep the default. The proxy serves no MCP server, so the MCP,
httpx and starlette integrations buy it nothing, and its init gets cheaper. The one
`sentry_init` stays the one home; the parameter is the only difference between roles.

## 3. Evidence

Standalone stress script (not in the repo): each of N fresh subprocesses starts a thread
running the proxy's Sentry init, while the main thread imports `mcp.server.stdio` and
`httpx`. Sequential, on a machine at ~100% CPU.

| | runs | import errors | init time (median) |
|---|---|---|---|
| before (`origin/main`) | 40 | 10, all `_DeadlockError` on `mcp.server.lowlevel` | 7782 ms |
| after (this change) | 40 | 0 | 3175 ms |

The timings are from a loaded machine and are indicative only; the error counts are the
result. No `KeyError` was seen here, only its sibling `_DeadlockError`.

Pin: `test_the_proxys_sentry_auto_enables_no_integrations_and_the_backends_does` and
`test_the_proxy_bootstrap_calls_sentry_init_exactly_once`. Mutation (runtime rebinding in
a `-p` plugin, no file edited; the proxy bootstrap rebound to the old call without the
keyword): mutant 2 failed / 0 passed, control 2 passed / 0 failed, `__pycache__` cleared
before each.
