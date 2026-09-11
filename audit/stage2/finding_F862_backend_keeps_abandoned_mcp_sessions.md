# F-862 — the backend lists every MCP session it ever created forever; the proxies' 2 s liveness probes are the flood that fills it

**Status:** FIXED in this PR (product defect; mechanism measured hermetically 2026-09-11, root cause confirmed in source)
**Opened by:** the 2026-09-11 memory census requested by the maintainer (backend pid 114552: 6.7 GB RSS after 18.5 h, 5 live instances, 564 tool calls, body stores 0 bytes)
**Source at:** `origin/main` = `bc93d02`
**Severity:** HIGH. Unbounded backend memory growth proportional to fleet size × uptime; the 62-session workstation lost ~9 GB/day to it, and a backend that reaches swap makes every session's tool calls slow, which is exactly the shape the watchdog reads as "dead" (F-859 / [[cpu-starvation-condemns-healthy-backend]]).

---

## 1. What was observed

| fact | evidence |
|---|---|
| backend RSS 6.7 GB after 18.5 h with 5 live instances, 11 spawns, 564 tool calls, network body stores 0 bytes | psutil census 2026-09-11 ~11:40 on pid 114552 (PyPI 2.1.1), `get_network_capture_filters` on every instance |
| the `NetworkInterceptor` request store is count-bounded (10 000) and byte-bounded for bodies; `debug_logger` rings are capped (500/1000/2000); stored element clones are an explicit, manual store | `network_interceptor._store_request`, `settings.network_request_max_count`, `debug_logger.MAX_*` |
| **hermetic probe (`tools/probe_backend_memory.py`), isolated source-built backend:** 200 MCP sessions doing `initialize` + `tools/list` then ABANDONED (TCP closed, no DELETE) → RSS 101.5 → 182.2 MB, linear, **≈ 0.40 MB / session**; 20 navigations of two heavy pages on one headless browser → +5.6 MB total; 100 tool calls (screenshot / page content / network list / script / state) → +7 MB total | probe run 1, 2026-09-11 13:0x |
| **control:** 300 sessions `initialize` only then DELETE → 99.9 → 100.4 MB (flat at this scale); 300 abandoned `initialize`-only → 100.4 → 132.3 MB (**≈ 0.11 MB / session**, linear); 300 more terminated → 132.3 → 135.1 | probe run 2 (`--mode sessions`) |
| **at scale:** 2000 DELETE'd sessions → +3 … +14 MB (**≈ 2–7 KB each**, the terminated transport that stays listed); 2000 abandoned → 105.8 → 341.9 MB (**0.118 MB each**, linear); 2000 more DELETE'd → +13.7 MB | probe run 4 (`--sessions 2000`) |
| **with the fix, source-built backend:** after the 300/300/300 passes, the first sweep past the 300 s window reaped **916** sessions — the 300 abandoned AND the 600 DELETE'd ones the layer had kept listed, plus the anchor proxy's own probes — and every later sweep reaps ~15 (30 s ÷ the proxy's 2 s cadence), with ~148 always inside the window; RSS flat at 135–136 MB over 7 min | probe run 3 (`--linger 400`), backend log lines `session hygiene: reaped …` |
| every stdio proxy's watchdog probes the backend **every 2 s** with a real `initialize` (a new MCP session each time) and DELETEs it best-effort inside the same 2 s `httpx.Client` timeout | `singleton._backend_http_ready` (lines 427-475), `backend_watchdog.watch_liveness(interval=2.0)`, comment at `singleton.py:69` |
| the MCP session manager removes a session ONLY on its own DELETE or on an idle timeout; FastMCP 2.11.2 never sets one | `mcp 1.27.1 server/streamable_http_manager.py` lines 222-300 (`_server_instances` popped only in the idle-timeout branch, the crashed-session `finally`, and `terminate`); `fastmcp/server/http.py:250` constructs `StreamableHTTPSessionManager(app, event_store, json_response, stateless)` — no `session_idle_timeout` |
| 62 proxies × 0.5 probes/s = 31 sessions/s; 18.5 h = 2.06 M probe sessions. At 2–7 KB per DELETE'd session that is **4–14 GB with every DELETE succeeding**; each DELETE lost under load (the POST and the DELETE share one 2 s `httpx` budget) adds 0.12 MB. The observed 6.7 GB needs no lost DELETE at all | arithmetic on the rows above |

## 2. Mechanism

1. `singleton._backend_http_ready` POSTs `initialize` (→ the manager creates a transport, a `ServerSession`, a task group and runs `app_lifespan` for the new session), then DELETEs the session id. Both share one `httpx.Client(timeout=2.0)`.
2. The DELETE reaches `StreamableHTTPServerTransport._handle_delete_request` → `terminate()`, which closes the streams and marks the transport terminated so the id answers 404. **It is never removed from `_server_instances`**: the manager pops an entry only in its idle-timeout branch (never armed) and in the crashed-session `finally`, which is guarded by `not http_transport.is_terminated`. Every probe therefore leaves its terminated transport listed forever — the successful-DELETE channel, 2–7 KB each.
3. A DELETE that times out, errors, or never runs (proxy killed between the two calls) leaves the whole session alive — transport, `ServerSession`, task group, lifespan — at 0.12 MB. The same for every proxy that exits without DELETE-ing its own session, and for `_await_backend_http`'s readiness session. Nothing on the server side notices the client is gone: the manager keys sessions by id, not by connection.
4. `StreamableHTTPSessionManager` offers `session_idle_timeout`, but it counts REQUESTS only: a live proxy that holds its GET event stream and makes no tool call for an hour would be reaped too, so it cannot be used as-is even if FastMCP exposed it.

## 3. Fix (this PR) — the backend defends itself

- New leaf `embedded/session_hygiene.py` — **THE one home for "this MCP session was abandoned by its client — reap it"**. `HygienicSessionManager` subclasses the MCP manager and adds a sweep every `SWEEP_INTERVAL_SECONDS` (30 s): a session with **no standing GET event stream** and **no request for `ABANDONED_AFTER_SECONDS`** (300 s) is unlisted and terminated through the transport's own `terminate()` (idempotent, so an already-DELETE'd transport is simply unlisted) — a reaped id answers 404 exactly as a deleted one. Both channels — the listed-forever terminated transport and the fully alive abandoned session — end here. The GET stream is the discriminator: the MCP client opens it right after `initialize` and holds it for the session's life, so a live proxy idle for hours is never touched; a lost-DELETE probe or a dead proxy has no stream and goes silent.
- `install()` binds the class to `fastmcp.server.http.StreamableHTTPSessionManager`, the name `create_streamable_http_app` constructs by module attribute — the one seam; `embedded/server.py`'s http branch calls `rt.session_hygiene.install()` before `mcp.run(...)`. `tool_runtime` re-exports the module (the one home for what `server.py` reaches for).
- Universal: it covers the proxies' probes, dead proxies, and any foreign client. No knob.

## 4. Verification

- `tests/test_session_hygiene.py` (hermetic): sweep against fake transports with an injected clock — young stream-less session kept; abandoned one terminated and forgotten (tracker included); a session holding its GET stream never reaped however old; a request resets the clock; one exploding `terminate()` does not stop the sweep; the install seam pin (FastMCP still constructs the manager by that name); and an end-to-end run of a tiny FastMCP app in-process over real streamable HTTP: five abandoned probe-shaped sessions vanish inside the (shrunk) window, the connected client keeps working, a reaped id answers 404. RED before the module existed; 7/7 GREEN after.
- Probe run 3 (source-built backend with the fix, `--linger 400`): RSS flat at 135–136 MB for 7 minutes after 900 sessions; the backend's own log shows the first sweep past the window reaping 916 (300 abandoned + 600 DELETE'd + the anchor proxy's probes) and each later sweep ~15, i.e. exactly the anchor proxy's 2 s probe cadence. See §1.

## 5. Not claimed / follow-ups

- The probe CHURN itself (31 transports/s created and torn down on a 62-session fleet) is untouched: a cheaper liveness signal that does not open a session is a separate finding (F-864 candidate), as is the shared 2 s budget for POST + DELETE in `_backend_http_ready`.
- Per-session cost of a LIVE proxy session is not a leak and is not changed.
- The 0.4 MB (with `tools/list`) vs 0.11 MB (initialize-only) difference is the buffered `tools/list` response of 94 tools; not investigated further because the reaper frees it either way.
