# F-838 — the proxy exits instead of healing, so any backend death is a user-visible disconnect

**Severity:** HIGH (user-visible: the MCP server goes dead mid-session)
**Area:** `embedded/singleton.py` (stdio proxy) → new `embedded/proxy_selfheal.py`
**Status:** FIXED on `fix/F838-proxy-self-heal`
**Related:** F-820 (busy ≠ dead — the verdict this consumes), F-839 (ignore
SIGBREAK — removes one killer), F-829 (an unreadable fingerprint is not an edit
— removes another), F-807 (the cold-start lock's patience), F-808 (adoption
order).

---

## 1. The problem

The stdio proxy runs a liveness watchdog against the backend it is bridged to
(`singleton._watch_backend_liveness`). Since F-820 its verdict is careful: three
missed 2s probes only *open* a confirmation phase, and the confirmation is the
same identity+readiness gate the cold-start lock trusts, so a merely BUSY
backend survives and only a genuinely dead one is condemned.

But the only thing the proxy *did* with a confirmed-dead verdict was:

```python
await _watch_backend_liveness(port)
_logger.warning("backend became unreachable; tearing down for reconnect")
tg.cancel_scope.cancel()
```

— log, tear down, exit. That rests on a premise: *the MCP client will respawn
the stdio server*. It does not. Claude Code does not reliably respawn a stdio
MCP server mid-session, so a real backend death left the user with a dead
`stealth` server until they ran `/mcp` and reconnected by hand.

And real deaths happen. On 2026-08-30 alone:

* **15:46** — a backend OOM-crashed.
* **18:43** — a backend was killed by a console `CTRL_BREAK`. That one was very
  likely born console-attached through the foreground `serve --http` path, which
  never gets the `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` flags
  `_start_server_process` applies.

F-839 (ignore SIGBREAK) and F-829 (unreadable fingerprint ⇒ no eviction) each
remove one **cause**. Neither is a **backstop**: the next cause — an OOM, an
antivirus kill, a Windows update reboot of a service, a bug not yet written —
still ends every session bridged to that backend. F-838 is the backstop, and it
must be indifferent to cause and to how the backend was born.

## 2. The fix

On a confirmed-dead verdict the proxy now **heals in place**: it obtains a
replacement backend and re-bridges its HTTP side onto it while the stdio side
stays connected to the client. The client experiences one failed call, not a
dead server.

The orchestration lives in a new leaf, `embedded/proxy_selfheal.py`
(precedent: `desktop_launch.py`, `spawn_exhaustion.py`, `window_sizing.py` —
each "THE one home for X"). It imports no other embedded module and receives
what it needs as callables, so `singleton.py` keeps every policy it already
owned.

```
_proxy_streams
  └── proxy_selfheal.drive(port, url_for, connect, watch, replay, pending,
                           client_write, ensure_running, await_ready)
        └── loop over backend GENERATIONS
              ├── _one_generation()  → True iff CONFIRMED dead
              │     ├── connect(url, replay_msg, armed)   = singleton.run_backend
              │     └── watch(port) after armed           = _watch_backend_liveness
              ├── pending.fail_all()                      → in-flight calls answered
              └── heal_backend()                          → a replacement port, or None
```

### 2.1 It heals through the startup path, never beside it

`heal_backend` calls the `ensure_running` it is handed. In production that is
`singleton.ensure_server_running` — **the exact function `run_stdio_proxy` calls
at boot**. So the heal inherits, without restating a line of any of them:

* the one identity+readiness reuse gate (`_same_identity_backend_ready`),
* the F-808 adoption order (`backend_registry.adoption_candidates`),
* port selection (`_select_backend_port`, incl. the F-509 foreign-occupant
  fallback),
* the cold-start lock (`_start_backend_holding_lock`).

A test pins that `_proxy_streams` hands `singleton.ensure_server_running` and
`singleton._await_backend_http` to the heal, precisely so a future "quick fix"
cannot grow a second cold-start path here (CLAUDE.md convention 4).

### 2.2 The thundering herd

When a *shared* backend dies, every proxy bridged to it confirms the death
inside the same second and calls `heal_backend` at once. Two properties make
that safe, and both are pre-existing:

1. **They converge on one port.** `heal_backend` passes the *dead* port as the
   preferred port, so `_select_backend_port` re-derives the same target for all
   of them (it is our own display context's recorded port; a dead backend leaves
   the socket free, so no fallback fires).
2. **They serialize on `singleton.lock`.** `ensure_server_running` spawns
   `_start_backend_holding_lock`, which takes the exclusive file lock. Exactly
   one proxy wins and cold-starts; the rest find the lock held, return
   immediately, and wait for the winner's backend on the same port through
   `_await_backend_http` — which is what the lock already does for a 40-session
   startup herd.

`TestTheHerdSerializesOnTheColdStartLock` pins this hermetically: with
`LOCK_FILE`/`STATE_DIR` redirected into `tmp_path` and the spawn stubbed, two
concurrent `_start_backend_holding_lock` calls produce exactly **one** spawn.

### 2.3 Bounded-retry policy (two axes)

| Bound | Value | Why |
|---|---|---|
| `HEAL_ATTEMPTS` | 2 | One attempt can legitimately lose a race it should not pay for twice (the OS not yet having released the dead port; adopting a replacement that dies during its own boot). Beyond that the machine, not the backend, is the problem. |
| `HEAL_ATTEMPT_SECONDS` | 45.0 | Generous against a cold start (`STARTUP_TIMEOUT` is 30s for the socket alone, and a herd's adopters wait behind the winner) but far short of the proxy's 120s `BACKEND_READY_TIMEOUT`: two attempts stay inside the window a user would still describe as "one slow call". |
| `HEAL_BACKOFF_SECONDS` | 1.0 | Never a tight loop. |
| `MAX_CONSECUTIVE_HEALS` | 3 | The flap guard on the other axis. `heal_backend` bounds *one* recovery; without this, `drive`'s generation loop would re-heal a backend that keeps dying — a tight retry loop in slow motion. (Found by a test: the first draft looped.) |
| `HEAL_STREAK_RESET_SECONDS` | 300.0 | A generation that lived five minutes was a real working session, so it earns the heal budget back. A proxy up for eight hours must keep being healed. |

When the budget is spent, `drive` returns and the caller runs **exactly the
pre-F-838 teardown**, log line and all. Healing is a backstop, not a promise; a
proxy that pretended to be alive forever would be the failure mode this fix
exists to end.

### 2.4 Busy still never heals

F-820's distinction is untouched and depends on nothing here. "Busy" is
`_watch_backend_liveness` resetting its strike run and **not returning**; a
generation that never ends never reaches `heal_backend`. `_watch_backend_liveness`
was not modified at all — no line of it, and no line of
`test_watchdog_busy_vs_dead.py` (including the WARNING-level pin at :185 that
F-827 depends on). `TestDrive::test_a_busy_backend_never_heals` pins the drive
half.

### 2.5 In-flight calls are failed, never replayed

`PendingCalls` records the requests **written to** the backend that have no
answer yet, and answers them with a JSON-RPC error (`-32603`) naming the method
and stating the call was *not* retried. Two consequences, both deliberate:

* An unanswered id is an unbounded wait for whatever drives the client. One
  clear error is strictly better.
* Tracking at the *write*, not at the read from the client, is the contract:
  messages still buffered when the backend died were never seen by it, so
  forwarding them to the replacement is correct rather than a retry. Only the
  genuinely in-flight ones are failed — a non-idempotent `tools/call` is never
  silently re-run against a fresh backend.

### 2.6 The re-bridge is a real handshake

Generation 2+ replays the client's own `initialize` message (captured once in
`pump_client`) into a fresh `streamablehttp_client` against the new URL. That is
what mints a new `mcp-session-id` on the replacement. `init_swallowed` and
`backend_initialized` moved *inside* `run_backend`, so they are per-generation:
each backend answers our initialize exactly once, and the client never sees a
second initialize result. `armed` likewise became per-generation, which fixes a
latent hazard the old shared `backend_ready` event would have introduced —
otherwise the watchdog would have been armed against a replacement while it was
still cold-starting.

## 3. LOC discipline

`singleton.py` was at 999/1000 after F-829 — no headroom. The net delta is
**zero**: it ends at 999 LOC. The additions (the `proxy_selfheal` import,
`init_message`, `pending`, the two `pending` calls, the `drive` call site) were
paid for by the code that moved out (`run_backend_guarded`, `monitor_backend`,
`backend_ready`) plus compression of four comments *inside the functions this
change touches*. No cap was raised, nothing was padded, and
`proxy_selfheal.py` (282 LOC) needs no grandfather entry — new modules only have
to stay under the 1000-LOC budget.

## 4. Goldens updated (SOFT, same PR, with justification)

**`tests/test_proxy_backend_death.py`** — pinned "confirmed dead ⇒ the proxy
returns". Its *substance* is unchanged (the proxy must reach a bounded end
instead of parking forever on a dead backend), but the bounded end is now
"heal, else tear down". Two edits:

* the module docstring records the premise change and points at
  `test_proxy_selfheal.py` for the heal path;
* the end-to-end case is renamed
  `test_proxy_returns_when_backend_dies_and_cannot_be_healed` and states its
  premise explicitly by stubbing `proxy_selfheal.heal_backend` to `None`.

That stub is not only honesty about what is being pinned. This test kills a
**real** backend; without it, the heal would run the real cold-start path
against the developer's live `~/.stealth-mcp` record and possibly adopt their
running backend. Stating the premise is also what keeps the test hermetic.

**`tests/test_watchdog_busy_vs_dead.py`** — **not touched.** It exercises
`_watch_backend_liveness`, which this change does not modify.
**`tests/test_singleton_cold_start_patience.py`** — not touched.

## 5. New tests (`tests/test_proxy_selfheal.py`, 18 cases, all hermetic)

* `TestHealBackend` — returns the replacement port; prefers the dead port (the
  herd's convergence); is bounded at `HEAL_ATTEMPTS` and gives up; survives a
  raising startup path; drives the blocking startup path off-thread.
* `TestDrive` — a confirmed death heals and opens the next generation against
  the new port; an unhealable death returns for the legacy teardown; a **busy**
  backend never heals; a flapping backend stops being healed; a generation that
  lived earns the budget back; in-flight calls are failed, not replayed.
* `TestPendingCalls` — an answered call is no longer in flight; failures are
  reported once.
* `TestProxyStreamsRebridges` — over a fake streamable-HTTP transport (memory
  streams): the proxy opens a **second** transport against the **new** port,
  replays a fresh `initialize` on it, and serves a `tools/list` issued
  afterwards, with the client's stdio side never disconnected; the replayed
  initialize's answer is not shown to the client; an unhealable death still
  tears the proxy down and opens no generation 2.
* `TestHealUsesTheOneStartupPath` — `_proxy_streams` hands
  `singleton.ensure_server_running` / `singleton._await_backend_http` to the heal.
* `TestTheHerdSerializesOnTheColdStartLock` — two concurrent heals, one spawn.

No real Chrome, no real backend, no real port, no write to `~/.stealth-mcp`.

## 6. Residual risks

* **The reduced startup herd must be re-run.** This change touches the
  cold-start path (`ensure_server_running` is now called from a second caller,
  mid-session and concurrently across proxies). The standing rule applies: run
  the reduced herd before merge. The hermetic lock test proves serialization of
  the *lock*; it cannot prove the per-process bind probe behaves at fleet scale.
* **Death-window message loss.** A message received from `to_backend_rx` and
  cancelled before `pending.track` cannot happen (no await between them), but a
  message handed off by `to_backend_tx` at the instant the receiver is cancelled
  can still be lost by anyio's memory stream. That window existed before F-838;
  it is now smaller (the proxy survives, so a later call succeeds) rather than
  larger.
* **A wedged-but-listening replacement.** If the replacement comes up wedged,
  `_await_backend_http` fails and the heal spends its budget; the flap guard
  then ends the proxy. That is the intended, honest outcome.
* **`ty` reports one new warning** (`anyio.to_thread.run_sync` on a
  `BrokenWorkerInterpreter` union member) — a resolver artefact, not a defect;
  the gate runs `--exit-zero-on-warning` and is green.
