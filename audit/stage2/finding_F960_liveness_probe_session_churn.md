# F-960 — the proxy liveness heartbeat mints a TCP connection and an MCP session per beat

**Severity:** Medium-high (resource leak on the shared backend; no data loss). Measured on the owner's
machine 2026-10-10 against backend 2.1.27: ~5,600 MCP sessions listed, ~560 reaped per 30 s sweep,
~2,200 TCP connections in TIME_WAIT, ~38 stdio proxies attached.
**Files:** `embedded/backend_probe.py` (`HEALTH_PATH`, `health_url`, `Heartbeat`),
`embedded/session_hygiene.py` (`_health` route, `running`, `_forget_if_deleted`, `install(server)`),
`embedded/singleton.py` (`_watch_backend_liveness` builds a `Heartbeat`), `embedded/server.py`
(`install(mcp)`), `CHANGELOG.md`, `NAVMAP.md`, `DESIGN.md` §2.1.
Tests: `tests/test_liveness_heartbeat.py` (new, 16); pins updated in `tests/test_watchdog_app_level.py`,
`tests/test_watchdog_busy_vs_dead.py`, `tests/test_proxy_starvation_witness.py`.
**Branch:** `fix/f960-liveness-probe-session-churn`, from `origin/main` (a3bd392, release 2.1.27).

---

## 1. What happened

F-959's finding noted "~2900 sessions listed" as by design (F-862's 300 s window). On 2.1.27 the figure
was ~5,600 and the hygiene log reaped ~560 every 30 s, with ~2,200 sockets in TIME_WAIT. That is not by
design: it is the cost of the heartbeat, and it scales with the number of open chats.

## 2. Root cause

`singleton._watch_backend_liveness` -> `backend_watchdog.watch_liveness` runs `is_healthy` every ~2 s,
which was `singleton._backend_http_ready` -> `backend_probe.ready()`. `ready()` opens a NEW
`httpx.Client` per call (a new TCP connection, left in TIME_WAIT when closed), POSTs a real MCP
`initialize` (the backend builds a transport, a `ServerSession` and a task group) and DELETEs it. The
SDK's DELETE only marks the transport terminated (`StreamableHTTPServerTransport.terminate`); the entry
stays in `_server_instances` until `HygienicSessionManager`'s sweep removes it after
`ABANDONED_AFTER_SECONDS` (300 s). 38 proxies x 0.5 beats/s ~ 19 sessions/s x 300 s ~ 5,600 listed.

The probe is an `initialize` on purpose (F-301/F-501): a wedged backend with a dead dispatch loop still
accepts a TCP connect, and a freshly bound uvicorn answers 4xx while FastMCP's session manager is still
starting. That guarantee is kept; the means is not.

## 3. Fix

1. **A session-free health route on the SAME app and loop.** `session_hygiene.install(server)` registers
   `_health` on `backend_probe.HEALTH_PATH` (`/_stealth/health`; nowhere near `MCP_PATH` `/mcp/`) via
   FastMCP's `custom_route`, once per server. It answers 200 only while
   `HygienicSessionManager.running` (its task group exists and is not cancelled) and 503 otherwise, and
   creates no session. A wedged loop cannot answer; a manager still starting, or already stopped,
   answers 503. `server.py` passes `mcp` to the existing `install()` call (the one seam).
2. **A keep-alive heartbeat.** `backend_probe.Heartbeat(mcp_url)` owns ONE `httpx.Client` per
   watchdog and exposes `alive(timeout)` with `ready()`'s contract (one synchronous attempt, never
   raises). `singleton._watch_backend_liveness` builds it, drives `beat.alive` off-thread as before, and
   `close()`s it in a `finally`. A failed beat discards the client so the next one reconnects.
   **Compatibility:** a 404 (a backend that predates the route) switches the beat to `ready()` (today's
   `initialize` + DELETE); if that then fails, the flag resets and the next beat tries the route again,
   so a replaced backend is noticed. Cold start (`await_ready`) and the identity gate
   (`_same_identity_backend_ready` -> `_backend_http_ready`) keep the `initialize` probe: they are rare.
3. **Defence in depth for proxies already running the old probe.** `HygienicSessionManager.handle_request`
   drops the session from `_server_instances` / `_session_owners` / `last_seen` as soon as a DELETE has
   terminated its transport (`_forget_if_deleted`). A DELETE the transport refuses (bad protocol
   version) leaves the session alone. **What changes for the client:** a DELETEd id used to answer 404
   "Not Found: Session has been terminated" (the terminated transport); it now answers 404 "Session not
   found" (the unknown-id branch of the SDK manager). Same status, which is all the SDK client reads (it
   turns any 404 on a session request into "Session terminated", and F-959's resend keys on that).
4. **Logging unchanged.** The sweep still logs only when it reaps; with the DELETE fix and the new
   heartbeat it has almost nothing to reap, so the line mostly stops appearing.

One home: `backend_probe.py` still owns every "ask the backend" shape; no parallel probe module.

## 4. Tests

`tests/test_liveness_heartbeat.py` runs uvicorn in-process on a free 127.0.0.1 port with a tiny FastMCP
app and our hygiene manager (nothing of the live backend). Sessions listed are read server-side
(`len(_server_instances)`); TCP connections are counted server-side by wrapping uvicorn's
`H11Protocol.connection_made`. The fixture resets sse_starlette's process-global
`AppStatus.should_exit`.

- `test_n_heartbeats_list_no_session_and_use_one_connection`: 20 beats -> 0 sessions, 1 connection.
- `test_the_initialize_probe_still_pays_a_connection_per_ask` (control): 20 `ready()` -> 20 connections.
- `test_the_measurement_sees_listed_sessions_when_the_delete_is_not_honoured` (control): with the
  pre-F-960 DELETE behaviour, 20 `ready()` -> 20 listed sessions, so the zero above can fail.
- `test_the_default_watchdog_beats_without_leaking`: `_watch_backend_liveness` with its DEFAULT check,
  20 ticks -> 0 sessions, 1 connection, the beat closed when the watchdog ends.
- Route: 503 before the manager runs, 200 inside the lifespan, 503 after; no `mcp-session-id`, no session.
- Wedged socket (accepts, never answers) and a closed port read not alive; the failed beat's client is dropped.
- Fallback: against an app WITHOUT the route the beat stays alive via `initialize`, and a dead legacy
  backend reads dead and re-tests the route.
- DELETE: the session is unlisted at once, the id answers 404, a live SDK client is unaffected, and the
  SDK client's own DELETE on exit unlists; a refused DELETE (400) does not unlist.

Existing pins that named `_backend_http_ready` as the watchdog's default check now name
`Heartbeat.alive` (`test_watchdog_app_level`, `test_watchdog_busy_vs_dead`, `test_proxy_starvation_witness`).

RED, measured out of tree (`git archive origin/main src` exported to `%TEMP%`, `PYTHONPATH` at the export,
`module.__file__` verified): the new file cannot import against origin/main's product (no `Heartbeat`,
`HEALTH_PATH`, `install(server)`); with those two imports shimmed it fails 16 of 16. Mutations of the fix, by
editing an export of the fixed product: DELETE no longer unlists -> 2 fail; heartbeat always on `initialize`
-> 4 fail; route always 200 -> 1 fails; watchdog back on `_backend_http_ready` -> 1 fails; a failed beat
keeps its connection -> 1 fails.

## 5. Rollout

Backend and proxies upgrade separately. A new backend serves the route and unlists DELETEd sessions at
once, which relieves the old proxies still beating with `initialize` (their connections still churn).
A new proxy against an old backend falls back to `initialize`. Full relief needs both: upgrade, restart
the backend (a fresh process), and let chats reconnect so their proxies pick up the heartbeat.
