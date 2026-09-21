# F-900 — the proxy bridge rides a deprecated client, and its inherited read timeout silently loses the standing event stream

## 1. The mechanism

`embedded/singleton.py`'s `_proxy_streams.run_backend` — the stdio proxy's leg to
the backend — opened its transport as:

```python
async with streamablehttp_client(url) as (backend_read, backend_write, _):
```

Two separate things are wrong with that one line, and only one of them is the
deprecation the finding is named after.

**(a) The name.** At the pinned `mcp` 1.27.1, `streamablehttp_client` is
`@deprecated("Use `streamable_http_client` instead.")`. Measured — the warning
fires on the CALL, before the context is even entered:

```
>>> cm = sh.streamablehttp_client("http://127.0.0.1:1/mcp")
DeprecationWarning: Use `streamable_http_client` instead.
```

So every proxy start raised it from our own call site. The replacement does not
take `timeout`/`sse_read_timeout` at all; it takes the `httpx.AsyncClient`
itself, which is the API this tree already speaks through
`backend_client.http_client` (F-891).

**(b) The timeouts, which is the half that cost something.** Called with no
arguments, the bridge inherited the SDK's defaults. Measured:

```
streamablehttp_client(url)  ->  httpx.Timeout(connect=30.0, read=300.0,
                                              write=30.0, pool=30.0)
MCP_DEFAULT_TIMEOUT = 30.0 ; MCP_DEFAULT_SSE_READ_TIMEOUT = 300.0
```

`read=300.0` is a deadline on **being idle**. The bridge holds a standing GET
event stream for the life of a Claude Code session; the backend has nothing to
push on it while the session is quiet, and `mcp.server.streamable_http` sends no
SSE keepalive — only a `Connection: keep-alive` HTTP header, which is not data.
So the stream read-times-out, and the SDK's `handle_get_stream` does this:

```python
while attempt < MAX_RECONNECTION_ATTEMPTS:      # == 2
    try:   ... aiter_sse ... ; attempt = 0      # reset only on a NORMAL end
    except Exception: attempt += 1              # a read timeout lands here
    if attempt >= MAX_RECONNECTION_ATTEMPTS:
        logger.debug("GET stream max reconnection attempts ... exceeded")
        return                                  # for good, at DEBUG
    await anyio.sleep(DEFAULT_RECONNECTION_DELAY_MS / 1000)   # 1 s
```

Two idle windows and the standing stream is gone permanently — at DEBUG, with
nothing said to the client and nothing said to the backend. At the inherited
defaults that is **≈ 601 s (2 × 300 s + 1 s) of quiet**.

**Why that matters is F-862.** `session_hygiene.sweep_once` decides a session was
abandoned by exactly one discriminator:

```python
if GET_STREAM_KEY in transport._request_streams:
    self.last_seen[session_id] = now      # an open event stream IS activity
    continue
if now - self.last_seen[...] < ABANDONED_AFTER_SECONDS:   # 300.0
    continue
... await transport.terminate()
```

and its module docstring states the promise this defeats: *"a live proxy — even
one idle for hours — is never touched"*. It is touched. After ~601 s the live
proxy holds no GET stream; after `ABANDONED_AFTER_SECONDS` more of no tool call
it looks exactly like the population the sweep exists to reap, and its session is
terminated. `last_seen` is refreshed to `now` on every sweep while the stream is
present, so that second clock only starts once the stream is gone: **≈ 601 + 300
≈ 900 s (~15 min) of continuous idleness**, which is a Claude Code session left
alone over lunch. The watchdog's 2 s `initialize` probes do not save it — those
open their own throwaway sessions (`backend_probe`) and never touch the bridge
session's `last_seen`.

**And it does not heal.** The user meets it as a 404 on the next tool call,
answered to the client as a `Session terminated` JSON-RPC error — and that is
where it stays. Traced in the installed SDK (review M1, re-verified here):
`_handle_post_request` answers a 404 by sending a `JSONRPCError(32600,
"Session terminated")` into the read stream and then **returning** (`:350-356`),
*before* `raise_for_status()` at `:358`. No exception is raised, no stream is
closed, and `self.session_id` is never cleared — it is assigned in exactly two
places, `__init__` and `_maybe_extract_session_id_from_response` (`:145`,
`:180`), neither reachable from the 404 branch. So nothing unwinds the
`async with` at the bridge, `_one_generation` never ends, `proxy_selfheal` never
heals and never re-bridges, and the watchdog is probing a backend that is
perfectly healthy so it never condemns. The transport keeps stamping the dead id
on every later POST: **every subsequent tool call for the rest of that Claude
Code session answers the same error.** This is not a self-healing blip — it is a
permanently dead MCP session under a live, healthy proxy and a live, healthy
backend, unrecoverable without restarting the client.

An earlier draft of this finding said "a bridge death, and an F-838 re-bridge".
That was wrong in the direction that matters: it sized the defect as one failed
call, which is a reason to deprioritise it.

Note that (b) is not caused by (a). The deprecated function honours the two
numbers it is given — it builds `httpx.Timeout(timeout, read=sse_read_timeout)`
and hands the client down to the replacement. Passing none is what inherited the
defaults.

### A correction to two of our own documents

Both `CLAUDE.md`'s `backend_client.py` row and this task's brief stated that
`streamablehttp_client`'s `StreamableHTTPTransport` *"IGNORES `timeout` /
`sse_read_timeout` with a runtime warning"*. **That is false at mcp 1.27.1 and is
corrected by this change.** The `@deprecated` overload with the ignore-warning is
`StreamableHTTPTransport.__init__`'s, and `streamablehttp_client` never passes
those parameters to the transport — it constructs `StreamableHTTPTransport(url)`
inside `streamable_http_client` and puts the numbers on the httpx client. The
docstring in `backend_client.opened` ("measured, it still honours
`timeout`/`sse_read_timeout`") was the accurate one. The migration is a
migration; the defect is the inherited default.

## 2. Evidence

All measured in this worktree against the pinned `mcp==1.27.1`, `httpx==0.28.1`.

**The warning, and what the bridge ran under** (`-W always`):

| Fact | Measured |
|---|---|
| `streamablehttp_client.__deprecated__` | `Use \`streamable_http_client\` instead.` |
| `streamable_http_client.__deprecated__` | `None` |
| warning raised by CALLING it | `DeprecationWarning: Use \`streamable_http_client\` instead.` |
| the bridge's effective timeouts | `Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)` |
| `MAX_RECONNECTION_ATTEMPTS` | `2` |
| `DEFAULT_RECONNECTION_DELAY_MS` | `1000` |
| `httpx.Timeout(30.0, read=None)` legal | yes — `read: None` |

The lane confirms the first one independently: before the change, pytest's own
warnings summary reported `DeprecationWarning: Use \`streamable_http_client\`
instead.` against the node that drives `_proxy_streams`; after it, the only
`DeprecationWarning` left in the whole proxy lane is third-party
(`authlib` via `fastmcp`).

**The idle stream, against a real loopback socket.** A server that answers
`initialize` and then holds the GET stream open sending nothing — an idle MCP
session exactly as our backend serves one. The clock is scaled down; the
behaviour under test is the SDK's.

| read clock | GET stream opens during 2.6–3.5 s idle | at |
|---|---|---|
| `0.4 s` | **2, then abandoned for good** | +0.48 s, +2.02 s |
| `0.3 s` (the committed pin) | **2, then abandoned for good** | — |
| `None` | **1, held for the whole window** | +0.44 s |

Scaled to the shipped defaults, "2 then abandoned" happens at ≈ 601 s.

**Import cost** (`-X importtime`, warm, this worktree):

| | Measured |
|---|---|
| `backend_client`'s own self-time in singleton's import tree | 517–711 µs (5 runs), median **0.65 ms** |
| heavy modules in `sys.modules` after `import singleton` | **NONE** of `mcp`, `httpx`, `fastmcp`, `nodriver`, `uvicorn`, `starlette` |
| whole-tree self-time sum, before → after | 225.2 → 252.6 ms median of 5 |

The whole-tree number is **noise, not the change**: the two ranges overlap
(219–251 vs 236–267) on a OneDrive-backed venv whose cold file opens are a known
confounder, and the attributable cost is one module's 0.65 ms. The 2.2 ms of
`typing` that the new tree charges to `backend_client` is re-attribution — it is
now alphabetically first in singleton's import tuple, and `backend_env` plus six
other modules singleton already imported load `typing` at module level anyway
(verified: importing `backend_env` alone puts `typing` in `sys.modules`).

## 3. The rule chosen

**The bridge's read clock is unbounded, and the transport it rides on is the one
this tree already has.**

```python
BRIDGE_READ_TIMEOUT: float | None = None
```

*Why `None` and not a bigger number.* Any finite read deadline is a deadline on
being idle, and `MAX_RECONNECTION_ATTEMPTS == 2` means a finite number does not
degrade gracefully — it loses the standing stream permanently, just later. A
larger number would move the failure past most sessions and leave it in place for
the long-lived ones, which are precisely the sessions a proxy exists to serve.

*Why an unbounded read is safe here.* Nothing about the bridge's liveness was
ever the transport's job. The UNIVERSAL bound is the watchdog: a backend that
stops answering is condemned by F-820 in ≈12 s (≤ ≈242 s with F-889's heartbeat
veto fully engaged — still tighter than the 300 s it replaces), `proxy_selfheal`
ends the generation and `PendingCalls` answers whatever was in flight; F-889's
heartbeat is the backend's own witness. A tool call's own CDP work is bounded on
top of that by `tool_runtime._clamp_timeout` + `_with_cdp_timeout` at the tool
body — which covers anything that awaits CDP, **not** every way a body can
block, so it is named second and never alone (review N2). A transport read
timeout is a **second answer to "is the backend still there"** — convention 4 —
and it is the worse one, because it cannot tell an idle session from a dead
backend and the other mechanism can.

It was not a per-call deadline in the first place, which makes the replacement
strictly better rather than merely equivalent: `_handle_post_request` has no
`except`, so a `ReadTimeout` escaped `tg.start_soon(handle_request_async)` into
`streamable_http_client`'s own task group and tore down the **whole bridge
generation** — killing every other in-flight call and forcing a full re-bridge.

*Why extend `backend_client.http_client` rather than add a second policy
function.* The seam is documented as THE one transport and the one place the two
clocks are decided. A second constructor would be a second place the connect
clock, `follow_redirects` and the client's construction are decided — exactly
the drift the seam exists to prevent. `None` is not a new concept in that
function either: it is httpx's own spelling of "no deadline" on the same clock
the parameter has always set. The parameter is renamed `budget_seconds` →
`read_seconds`, because there are now two consumers and only one of them has a
budget; a parameter called `budget_seconds` holding `None` would read as "no
budget", a claim about the whole call rather than about one clock.

The bridge does **not** use `backend_client.opened` — it is a transparent pipe
and owns its own streams — so what it borrows is the transport and nothing else.
`terminate_on_close=True` is passed explicitly although it is also the SDK's
default, for the reason `opened` already gives: the DELETE is the thing that is
depended on, and a default is a value a dependency bump can change silently.

## 4. Blast radius

- `embedded/singleton.py` — `_proxy_streams.run_backend` only. The bridge's
  message flow, the local `initialize` answer, the initialize swallow, the
  session-id sequencing, F-838's re-bridge, F-843's witness, F-889's
  never-exits and both exits are untouched. 965 → 977 LOC (cap 1000).
- `embedded/backend_client.py` — one new constant, one renamed parameter, one
  widened type. The CLI's behaviour is unchanged: it still passes a float and
  still gets `read == that float`, pinned.
- `tests/test_proxy_selfheal.py` — the `wired_proxy` fixture patched the SDK by
  name and is re-pointed. Its 126-node lane is otherwise untouched.
- Not touched: `backend_probe` (hand-rolled POST, no client library, by design),
  `cli_call`, `session_hygiene`.

## 5. Tests

`tests/test_proxy_bridge_transport.py` (10 nodes, hermetic, ~7 s). RED first:
6 failed / 3 passed against the shipped bridge. Three of those six are red by
`AttributeError` on a constant that did not exist rather than by behaviour —
inherent to introducing a constant, and named so the "6/9" is read for what it
is (review N1).

| Node | Pins |
|---|---|
| `test_the_bridge_warns_about_nothing_from_its_own_call_site` | drives the REAL `_proxy_streams` and the REAL SDK over `httpx.MockTransport`; no `DeprecationWarning` naming the replacement, and the handshake actually happened so it cannot pass vacuously |
| `test_nothing_under_src_imports_or_calls_the_deprecated_name` | AST, not grep: an import, a bare reference or an attribute access fails; a docstring that NAMES the deprecated function to explain why it is unused does not |
| `test_the_sdk_still_takes_a_client_and_terminates_on_close` | the two SDK parameters the bridge depends on, plus that the deprecated alias still carries its marker |
| `test_the_bridge_asks_the_one_seam_for_its_client` | the bridge goes through `backend_client.http_client`, with `BRIDGE_READ_TIMEOUT` |
| `test_the_two_clocks_reach_the_httpx_client` | drives the REAL seam with the REAL argument: `read is None`, `connect == CONNECT_TIMEOUT_SECONDS`, `follow_redirects` |
| `test_the_cli_budget_is_still_bounded_on_the_same_seam` | the seam's first consumer kept its contract |
| `test_the_bridge_read_clock_is_unbounded_and_the_constant_says_why` | the policy VALUE, because the value is the decision |
| `test_the_sdk_gives_an_abandoned_event_stream_up_for_good` | `MAX_RECONNECTION_ATTEMPTS == 2`, read from the SDK rather than restated — the reason the policy cannot be a large number |
| `test_a_bounded_read_permanently_abandons_an_idle_event_stream` | MEASURED against a real loopback socket on an OS-assigned port: bounded → 2 opens then gone, `BRIDGE_READ_TIMEOUT` → 1 held. Only the second half pins OUR decision; the first is an SDK fact true with or without the fix |
| `test_the_tripwire_ends_the_run_when_the_heal_path_is_reached` | the fence itself, proven rather than asserted — see below |

**The fixture fences off the heal path, and that fence was bought.** The first
RED run of this file reached `proxy_selfheal.drive` → `heal_backend` →
`singleton.ensure_server_running` and **cold-started a real backend on the
developer's machine** (pid 55240, port 21770, written into the real
`~/.stealth-mcp/server.json` at 01:18:20). F-886 spared the two live siblings, so
nothing was evicted and no browser died — that was the rule working, not the test
being safe. The stray backend was killed and its entry dropped through the one
writer (`backend_registry.forget_entries`, matched on context+port+pid), and the
fixture stubs `heal_backend`, redirects `STATE_DIR`/`SERVER_STATE_FILE`/
`PORT_FILE` into `tmp_path`, and makes `ensure_server_running` a tripwire.

**The tripwire raises a `BaseException` subclass, and that is not a stylistic
choice** (review M2). It was an `AssertionError` at `50420b9`, and
`heal_backend` drives `ensure_running` inside
`except Exception:  # PERMANENT(a backstop must not raise)`
(`proxy_selfheal.py:325`) — so the fence that the fixture docstring and this
section both present as the guard the incident bought **could not fire**.
Measured by mutating `_RealStartupReached` back to `Exception`: the node fails,
and the captured log reads `heal attempt 1/2 failed` / `2/2 failed` across NINE
retry rounds in ten seconds — nine entries into the real startup path, each one
swallowed and none of them reported. Restored to `BaseException`, the run ends
at the first entry. Only the `heal_backend` stub was actually keeping the path
out of reach, and a later agent who re-points that stub trusting the tripwire
would have got the incident back. A hermetic proxy test must make the real
startup path unreachable rather than merely unlikely — and must prove it.

Lanes run (all green): the 7 `test_proxy_*` files + the new one (126 + 9),
the 9 `test_singleton_*` files (103), `test_stealthy_cli.py` +
`test_session_hygiene.py` + `test_clean_shutdown_noise.py` (117 passed,
1 skipped).

## 6. Residuals

1. **The bridge's session DELETE is best-effort, and this change does not make
   it better.** `_proxy_streams` ends both of its exits with
   `tg.cancel_scope.cancel()`, and anyio cancellation is level-triggered, so the
   `await transport.terminate_session(client)` in the SDK's `finally` is very
   likely skipped — a proxy that exits normally probably leaves its session
   behind. That was equally true before F-900 (`terminate_on_close` defaulted to
   `True` then too), it is one of the two populations F-862's sweep exists for,
   and with the standing stream now held for the life of the session the sweep
   can once again tell the two apart. Fixing it means shielding the terminate on
   the way out, which is a separate decision about cancellation semantics and is
   deliberately not taken here.
2. **`fastmcp` 2.11.2 still calls the deprecated function**
   (`fastmcp/client/transports.py:266,285`), which is why
   `tests/test_session_hygiene.py` — a test that drives a fastmcp CLIENT — still
   emits the warning. Our `src/` is clean and pinned clean; that one is a third
   party's, we use fastmcp as a SERVER in production, and it is not ours to fix.
3. **The read policy is unbounded in the write direction too** only insofar as
   `write`/`pool` keep `CONNECT_TIMEOUT_SECONDS`; that was not measured as a
   problem and is left as the SDK would have it.
4. The scaled-clock node asserts the SDK's behaviour, not ours. If a future
   `mcp` bump adds an SSE keepalive or makes the GET stream reconnect
   indefinitely, that node still passes while the REASON for `None` weakens —
   the constant's comment is where to re-read, not the pin.
5. **`tests/test_singleton_fast_handshake.py` has the same unfenced shape, and
   it is NOT fixed here — it wants its own finding.** Pre-existing, not
   introduced by F-900, and deliberately not run during this work. It drives
   `singleton._proxy_streams` at `:70`, `:205` and `:292` with no stub of
   `ensure_server_running`, no stub of `proxy_selfheal.heal_backend` and no
   redirection of `STATE_DIR`/`SERVER_STATE_FILE`/`PORT_FILE` in the pytest
   process — its `_isolated_subprocess_env` guards only the CHILD and says so.
   Two of the four nodes are structurally reachable rather than merely
   unfenced: `TestFastHandshakeEndToEnd::test_initialize_local_then_tools_list_
   forwarded` (`:174`) and `TestProxyExitsOnClientDisconnect::test_proxy_
   returns_when_client_stream_closes` (`:259`), because a real backend
   subprocess passes the readiness gate so a bridge break is confirmed in
   seconds — the same fast path that cold-started pid 55240. The other two are
   saved only by a 120 s `BACKEND_READY_TIMEOUT` losing a race to their own 5 s
   `fail_after`, which is luck, not a fence. **`tests/conftest.py` is decisive
   here**: its only two autouse fixtures are `_stealth_logger_hygiene` and
   `_reset_settings_cache`, neither of which redirects `HOME`/`USERPROFILE` or
   blocks spawning, and `backend_registry.STATE_DIR` derives from
   `Path.home()` — so **every fence in this repo must be per-file**, and the
   remedy is ~5 lines per file (`tests/test_proxy_backend_death.py:249-253`
   plus `:260`), not one shared fixture. Worth its own finding number.
