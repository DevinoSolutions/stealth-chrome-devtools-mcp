# F-959 — a healed proxy session is reaped five minutes later, and a 404 is never answered with a new session

**Severity:** High. Every chat whose backend died or was replaced lost the stealth server about
five quiet minutes after the heal, until a human reconnected it by hand. Reported by the owner
2026-10-09 (proxy 150780 on backend 34654), and it hits every chat moved off a stopped backend.
**Files:** `embedded/singleton.py` (`_proxy_streams`: record and replay `notifications/initialized`;
resend refused calls; end a generation whose session is gone), `embedded/proxy_selfheal.py`
(`PendingCalls` keeps the frame and resends a 404'd call once; `SESSION_LOST_CAUSE`;
`_one_generation` reports it without a death confirmation), `CHANGELOG.md`.
Tests: `tests/test_proxy_session_reinit.py` (new, 6).
**Branch:** `fix/f959-proxy-reinit-on-session-terminated`, from `origin/main` (3e7fa8f).

---

## 1. What happened

Proxy 150780's log ends with `17:22:08 backend healed: re-bridging to port 34654 (attempt 1/2)`.
Every stealth call after that answered `{"code": 32600, "message": "Session terminated"}`. The
backend's log (pid 88500) shows `session hygiene: reaped N abandoned MCP session(s) (no event
stream, silent > 300s)` from 17:27:32, about five minutes after the heal.

## 2. Root cause

F-838's heal replays the client's `initialize` so the new backend mints a new `mcp-session-id`.
It replayed nothing after that. The MCP SDK client (`mcp/client/streamable_http.py`, the
`_is_initialized_notification` branch of `post_writer`) opens the session's standing GET event
stream only when it SENDS `notifications/initialized`. The client's original one had gone to
the dead backend, so the healed session never had an event stream.

F-862's sweep (`session_hygiene.HygienicSessionManager`) treats "no event stream and silent for
`ABANDONED_AFTER_SECONDS` (300 s)" as an abandoned session and terminates it. A healed proxy
that went quiet for five minutes therefore lost its session. Its next POST got a 404, which the
SDK client turns into the error above. Nothing in the proxy treated that error as anything but
a reply, so every later call failed the same way.

Separately, any other route to a 404 had the same dead end: a backend restarted on the same port
faster than the watchdog's verdict, a session DELETEd or reaped for any reason. The proxy kept
the SDK client, and with it the stale session id, for the life of the generation.

The ~2900 sessions the backend listed are not part of the defect. They are the watchdog probes
of about 20 proxies (each `initialize` + `DELETE` every 2 s), kept listed for the 300 s window
and reaped about 300 per 30 s sweep, as F-862 designed.

## 3. Fix

1. `pump_client` records the client's `notifications/initialized`, and `replay()` yields
   `[initialize, notifications/initialized]`. `to_backend` sends the first, waits for its
   (swallowed) reply, then sends the rest. A healed session now has its event stream and is
   spared by the sweep like any live one.
2. `PendingCalls.track` keeps each request's frame. `from_backend` asks
   `PendingCalls.session_lost(reply)`: a reply that is exactly the SDK's
   `(32600, "Session terminated")` answer to a call not yet resent is held back from the
   client. The generation then ends as soon as nothing else is owed a reply, or after
   `SESSION_LOST_GRACE_SECONDS` (2 s) at most. `_one_generation` answers
   `SESSION_LOST_CAUSE` (no dead/alive confirmation, since the backend answered), and
   `drive` heals as for any other cause: `ensure_running` hands back the live backend, the new
   generation replays the handshake, and `to_backend` resends the held calls before anything
   newer.
3. A held call is resent ONCE. A 404 means the backend never ran it, so resending is safe even
   for a non-idempotent tool. If the fresh session refuses it again, that reply goes to the
   client unchanged. Calls that were merely in flight when the session vanished are still
   failed by `fail_all` and never resent, as before (F-838).

One heal path: a lost session goes through the same `heal_backend` / `ensure_running` loop and
the same flap budget as every other ending. No second reconnect mechanism was added.

## 4. Tests

`tests/test_proxy_session_reinit.py` uses real streamable HTTP in-process: the real SDK client
and server, FastMCP's app with our hygiene manager, uvicorn on loopback, and a reap window
shrunk to 0.5 s.

- `TestAHealedSessionIsNotReaped`: the owner's incident. Kill backend A, heal onto backend B,
  stay quiet past the reap window: B still lists the session and the next call needs no
  re-initialize.
- `TestAKilledBackendCostsNoCall`: kill the backend mid-session and start a new one on the SAME
  port before any watchdog verdict. The next call gets a 404 and is answered with its result
  after one re-initialize, and the fresh session survives the sweep.
- `TestAReapedSessionCostsNoCall`: the backend terminates the session (what the sweep or a
  DELETE does). The next two calls are served.
- `TestResendOnce` (3): only the SDK's exact answer counts, a call is resent once, and an id
  answered after its resend is eligible again.

RED, measured out of tree (export of the product with the venv overridden by `PYTHONPATH`):
`origin/main` fails all 6 (transport nodes with `{'code': 32600, 'message': 'Session
terminated'}`, the heal node with "the healed session was reaped"). The fix with only the
`notifications/initialized` replay removed fails the heal node and the kill node.

Harness note: stopping an in-process uvicorn sets sse_starlette's PROCESS-GLOBAL
`AppStatus.should_exit`, after which every SSE response in the process ends at once. The
new file resets it after each kill and in its fixture, because `test_session_hygiene.py`'s
end-to-end node leaves it set.

## 5. Rollout

The fix is in the stdio proxy, which is a separate process per chat that started on the
installed version of its day. A backend upgrade does not replace a running proxy. A chat
already showing "Session terminated" needs one reconnect of the MCP server (in Claude Code:
`/mcp`, then reconnect `stealth-chrome-devtools-mcp`). That restarts only the proxy, and the
conversation is kept. Chats that reconnect after the upgrade pick up the fixed proxy.
