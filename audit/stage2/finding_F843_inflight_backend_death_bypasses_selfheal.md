# F-843 — a backend that dies WITH A CALL IN FLIGHT bypasses the self-heal entirely

**Status: RESOLVED** on `fix/F843-fast-death-heal`.
**Severity: HIGH (reproduced live, 8/8 sessions, two independent harnesses,
against the installed 2.0.8)** — this is the last remaining user-visible
disconnect. F-838 shipped the recovery; F-843 is the discovery that the
recovery was wired behind a discriminator that answers *no* to exactly the
deaths a user experiences.

---

## Symptom

The backend dies (crash, OOM, an operator `kill`, an eviction) while the user
is mid-task. The client gets **one** contractual `-32603` for the call that was
in flight, and then the MCP server is gone for good: every later request meets
`anyio.ClosedResourceError`, the 94 tools vanish, and only a manual `/mcp`
reconnect brings them back. F-838's heal — which exists precisely for this —
never runs.

The same backend death in an **idle** session heals silently and correctly.
Same product, same kill, opposite outcome, decided entirely by whether a call
happened to be in flight at that moment.

## Empirical evidence

Reproduced against **installed 2.0.8**, in isolated workspaces, by two
independently written harnesses:

| Harness | Sessions with a call in flight at kill | Recovered |
|---|---|---|
| battery (run 1) | 4 | **0** |
| battery (run 2) | 4 | **0** |
| churn | 6 | **0** |

**8/8 in-flight sessions across the two battery runs, and 6/6 in the churn run,
were permanently disconnected.** Both battery runs failed identically — the
same log sequence, the same second, not a flaky race.

The decisive control is in the churn run itself: **the same run recorded 12
successful heals** — every one of them on a session that was *idle* when its
backend died. The bridge stays quiet, the watchdog runs its course, F-838
works exactly as designed. So the fault is not in the heal; it is in what gets
routed to it.

### The two proxy-log signatures, side by side

**HEALED** — idle session, the watchdog is the witness:

```
WARNING stealth.proxy: probe failed 1/3 on port 19222
WARNING stealth.proxy: probe failed 2/3 on port 19222
WARNING stealth.proxy: probe failed 3/3 on port 19222
WARNING stealth.proxy: backend on port 19222 confirmed unusable
WARNING stealth.proxy: backend healed: re-bridging to port 19222 (attempt 1/2)
```
Two `backend-<pid>.log` files on disk: the corpse and its replacement.

**DIED** — a call in flight, the bridge is the witness:

```
WARNING stealth.proxy: backend connection lost
  (traceback: httpx.ConnectError: All connection attempts failed)
WARNING stealth.proxy: probe failed 1/3 on port 19222
WARNING stealth.proxy: backend became unreachable; tearing down for reconnect
```
No `heal attempt` line. No `backend healed` line. **Exactly one**
`backend-<pid>.log` ever written — no replacement was even attempted. The proxy
process exits roughly **one second** after the kill.

That middle line is the tell: the watchdog got through *one* strike of three
before the proxy was already gone.

## Mechanism

`embedded/proxy_selfheal.py`, as shipped in 2.0.8:

```python
# _one_generation, :256-286
async with anyio.create_task_group() as generation:

    async def bridge() -> None:
        try:
            await connect(url, replay_msg, armed)
        except Exception:
            _logger.warning("backend connection lost", exc_info=True)
        finally:
            generation.cancel_scope.cancel()          # <-- :264

    async def monitor() -> None:
        await armed.wait()
        await watch(port)                              # F-820: 3 strikes + confirm
        _report(CONDEMNED_EVENT, ...)
        died.set()
        generation.cancel_scope.cancel()

...
return died.is_set()
```

```python
# drive, :331
if not confirmed_dead:
    return                                             # -> the caller tears down
```

There are **two witnesses to a backend death and they run at wildly different
speeds**:

* `monitor` is the SLOW one. The F-820 watchdog needs three 2s strikes plus an
  identity+readiness confirmation — ~12s at the very best.
* `bridge` is the FAST one. When the backend process dies while a request is
  outstanding, the HTTP leg fails in **milliseconds**: the POST raises
  `httpx.ConnectError`, `streamablehttp_client` unwinds, `connect()` raises.

Only the slow witness could set `died`. So the fast one wins the race, its
`finally` cancels the generation cancel-scope at `:264`, `monitor` is killed
mid-first-strike, `died` is never set, `_one_generation` returns `False`, and
`drive` takes `if not confirmed_dead: return` at `:331`. The entire recovery —
`heal_backend`, the herd's cold-start-lock convergence, the re-bridge — is
skipped, and the pre-F-838 teardown runs.

An idle session has no outstanding request, so nothing breaks the bridge, so
the slow witness gets to finish and everything works. **That is the whole
discriminator, and it is the wrong question.** `died.is_set()` does not mean
"the backend died"; it means "the WATCHDOG is the one that noticed", which is
true only when nothing was happening.

### Why the test suite never caught it

Every node in `tests/test_proxy_selfheal.py` drives a fake `watch` that returns
to signal death. Not one of them ends a generation through the bridge leg. The
defect lives in the ordering of two real tasks over a real HTTP connection, and
no fake transport reproduces it — which is exactly how it shipped alongside a
suite that looked thorough.

## Fix

A discriminator change, not new machinery. The question becomes *"did the
backend leg end for a reason recovery answers"*, and the dead-vs-busy question
is settled the same way for both witnesses — by the identity+readiness gate
(`singleton._same_identity_backend_ready`) that F-820's own confirmation phase
and the cold-start lock already trust.

* `_one_generation` returns a **cause** (or `None`) instead of a bool. The
  bridge leg ending while `armed` claims an internal `_BRIDGE_ENDED` sentinel;
  the verdict then comes from `_confirm_bridge_verdict`, driven off-thread
  (that gate blocks on socket probes and a real `initialize`):
  * gate says **not ready** → `connection_lost` → `CONDEMNED_EVENT` shipped
    (with `cause`), heal.
  * gate says **alive, same identity** → `connection_reset` → *not* condemned
    (nothing died; the condemnation count in F-827 stays truthful), heal
    anyway — because healing is the same one path either way.
* `armed` is the other half of the discriminator, and it is what keeps the fix
  narrow. A generation whose backend never answered an `initialize` keeps its
  pre-F-838 answer (`None` → teardown): a proxy whose backend never booted
  already spent its own 120s and must not then grind through a recovery budget.
* **The cause never branches the recovery.** Healing *is* `ensure_running`,
  which reuses a live same-identity backend and spawns only when there is none
  — so "the backend is gone" and "only our connection to it is gone" already
  have one correct answer between them. The cause is carried purely so the
  reports can tell the two incidents apart, and it is counted against the
  **same** flap budget: a connection that keeps breaking is a proxy pretending
  to be alive exactly as much as a backend that keeps dying.
  `MAX_CONSECUTIVE_HEALS` / `HEAL_STREAK_RESET_SECONDS` semantics are unchanged.
* `CONDEMNED_EVENT` and `HEALED_EVENT` gain a `cause=` field
  (`watchdog` / `connection_lost` / `connection_reset`), so "how often does
  stealth condemn a backend" remains ONE query — now with the witness attached.

`proxy_selfheal.py` stays a leaf: the gate arrives as the `confirm_alive`
argument, it imports no other embedded module, and it still never raises (the
confirmation is wrapped, and a confirmation that cannot answer at all reads as
DEAD — recovery is the safe direction, since it re-enters the startup path
which re-asks the same gate).

### What was preserved, and how it is known

* **Client-went-away stays a plain teardown**, *structurally*. A client EOF
  ends `pump_client`, which cancels the proxy's OUTER task group
  (`singleton._proxy_streams`, `:970`). That cancellation is a `BaseException`,
  so `bridge`'s `except Exception` never sees it, and it unwinds through
  `_one_generation`'s `async with` — before `pending.fail_all` or any
  confirmation can run. There is no code path from a departed client to a heal.
  Pinned twice: `test_the_client_going_away_still_exits_without_healing`
  (no heal, no confirmation) and `test_the_client_going_away_reports_nothing`
  (no Sentry event).
* **Never-armed generations** keep the old answer. Pinned by
  `test_a_bridge_failure_before_readiness_is_not_an_incident`.
* **The flap guard** is untouched; both new causes feed the same counter.

## Measured recovery (live, this machine)

From the transport node's own proxy log, real launcher, real backend, real kill:

```
12:11:22.064  backend connection lost
12:11:23.946  probe failed 1/3 on port 10076        (the watchdog, still on strike 1)
12:11:27.996  backend on port 10076 confirmed gone after a lost connection
12:11:43.761  backend healed: re-bridging to port 10076 (attempt 1/2)
```

~6s of confirmation (a dead backend fails the gate on its first refused
connection; the residual is Windows connect/psutil latency, not the 60s
patience window) plus ~16s of spawn-and-readiness: **~21s end to end**, well
inside the 90s the recovery is allowed. The client's next `tools/list` returned
all 94 tools on the same, never-disconnected stdio session.

## Cost

The fast path now pays the confirmation before it may heal. For a genuinely
dead backend that is a few seconds (no socket, no process of ours → the gate
fails immediately). For a *busy* one it can be up to `REUSE_PATIENCE_SECONDS`
(60s) — deliberately, and identically to what the watchdog path already
accepts: spending 60s to avoid respawning a healthy backend is the F-820/F-807
trade, not a new one.

## Pins

`tests/test_proxy_selfheal.py` (hermetic):

* `test_a_bridge_failure_confirmed_dead_heals_the_next_generation` — **THE
  regression pin.** Written RED first against the unfixed code and failed for
  the right reason: proxy log `backend connection lost`, exactly ONE generation
  opened, `heal_backend` never called (`assert 1 == 2`).
* `test_a_bridge_failure_over_a_live_backend_still_converges` — the other
  verdict: recovery still routes through the one gate and re-bridges.
* `test_a_bridge_failure_before_readiness_is_not_an_incident` — `armed` is the
  narrowing half; a never-ready backend is not healed for, and is not even
  confirmed.
* `test_the_client_going_away_still_exits_without_healing` — outer cancellation
  propagates; no heal, no confirmation.

`tests/test_proxy_sentry_reporting.py`:

* `test_a_bridge_first_death_is_condemned_under_its_own_cause` — same event
  name, `cause="connection_lost"`.
* `test_a_live_backend_behind_a_broken_leg_is_never_condemned` — a reset ships
  nothing.
* `test_the_watchdogs_verdict_ships_...` now also asserts `cause="watchdog"`;
  `HEALED_EVENT` asserts the cause it healed for.
* **SOFT golden updated deliberately (same PR):**
  `test_a_backend_that_merely_ended_reports_nothing` armed the generation and
  then asserted silence — that *was* the defect encoded as a contract. It is
  restated as the premise it meant to protect
  (`test_a_backend_that_never_became_ready_reports_nothing`), and the client-exit
  half it claimed to cover gets its own honest node.

`tests/test_proxy_backend_death.py::TestBackendDeathWithACallInFlight`
(integration + transport, `CAN_RUN`-guarded, isolated `gate_workspace`): drives
the absolute installed launcher over real stdio, hands it a `tools/list`
without awaiting it, kills the recorded backend's process tree while that call
is in flight, and asserts the in-flight call is answered, the SAME client comes
back with 94 tools inside 100s, a **second** `backend-<pid>.log` exists, and
the log shows `backend connection lost` + `backend healed` with the pre-F-843
`tearing down for reconnect` **absent**. Verified both ways on this machine:
GREEN at HEAD in 29s; RED against the reverted 2.0.8 sources (pycache cleared)
— the reissued call is never answered at all and the node fails on
`TimeoutError` after 105s, i.e. the permanent disconnect, reproduced.

## Residuals — NOT fixed here

1. **The eviction path now ends in ADOPTION, not a disconnect — a deliberate
   behaviour change worth a human read.** When a newer-source proxy evicts the
   shared backend (F-829 / `_start_backend_holding_lock` → `_clear_stale_backend`
   → `_terminate_backend`), every old proxy's bridge breaks. Under this fix they
   confirm, find the backend gone, and heal — where before they simply exited.
   Analysis says this cannot produce a stale-source duplicate: `_source_fingerprint`
   is computed **from disk at call time**, so an old proxy computes the *new*
   digest and adopts the new backend through the reuse gate; and if it loses that
   race, `_start_backend_holding_lock` finds the cold-start lock held and returns,
   while any spawn it does win re-executes `python -m …` against the same on-disk
   (new) source. So the outcome is "old sessions survive an upgrade" rather than
   "one final disconnect by design". **This exposure is not new** — F-838 already
   had it on the watchdog path, and this change deliberately matches that
   behaviour exactly rather than inventing a policy. Not measured live. If the
   maintainer wants eviction to remain a hard disconnect, that is a separate,
   explicit decision in `singleton`, not a `proxy_selfheal` tweak.
2. **Herd-heal convergence has never been exercised in anger.** The cold-start
   lock's winner/adopter split is pinned only by the two-thread hermetic node
   (`TestTheHerdSerializesOnTheColdStartLock`). Before F-843 the in-flight
   sessions never reached the heal at all, so a *real* herd heal has never
   happened. Now every busy session in a fleet will confirm and heal within the
   same second of a shared backend's death. **Recommended follow-up: a
   multi-client kill test** — N concurrent gate-workspace clients, all with a
   call in flight, one backend killed, asserting exactly ONE replacement spawns
   and all N clients recover.
3. **The `connection_reset` branch has no field evidence.** It has never been
   observed live (every reproduced case confirmed DEAD). It exists so a broken
   leg over a healthy backend cannot be reported as a condemnation, and is
   unit-pinned only.
4. **One call is still lost, by design.** `PendingCalls` fails the in-flight
   request rather than replaying it, because a non-idempotent `tools/call` must
   never be silently re-run. A client that does not reissue will surface that
   single `-32603` to the user even though the session recovered.
5. **`embedded/singleton.py` is now at exactly 1000/1000 LOC.** The one added
   `confirm_alive=` kwarg took it from 999 to 1000 (still inside the
   un-grandfathered budget — no cap was raised or padded). No honest offset was
   available in the region: the twin `headers` dicts that `_backend_http_ready`
   and `_await_backend_http` duplicate are a documented plan_M1 §2.2 ruling
   ("M1/M3 singleton regions stay disjoint; consolidating is a future finding"),
   not spare change. The next change to this file must extract, not add.
