# F-941 — an abandoned CDP reply that fails is logged as "Transaction exception was never retrieved"

**Severity:** Low for users (nothing they called fails), but it is an ERROR on the
asyncio logger, so every one reaches Sentry and buries real reports.
**Files:** `embedded/cdp_transport.py` (`_retrieve`, `_protect`, module docstring),
`tests/test_cdp_transport.py`, `NAVMAP.md` (`cdp_transport.py` row).
**Sentry:** STEALTH-CHROME-DEVTOOLS-MCP-8W — 14 events from 2.1.12 to 2.1.21 (5 on
2.1.14, 2 each on 2.1.13, 2.1.18, 2.1.20, 2.1.21, 1 on 2.1.12), from four backend
ports. Every event has the same message:

```
Transaction exception was never retrieved
future: <Transaction method: Runtime.evaluate status: finished success: False>
ProtocolException: Inspected target navigated or closed
```

---

## 1. Cause

F-883 B1 made a cancelled caller leave its CDP reply pending: `cdp_transport` wraps
`Transaction.__await__` in `asyncio.shield`, so the cancellation lands on a throwaway
outer future and the registered `Transaction` stays pending for the listener to
finish.

The module's docstring claimed that `shield` "retrieves the abandoned outcome
itself when the outer future was cancelled". It does not. In CPython 3.12,
`shield._outer_done_callback` REMOVES the inner callback (the one that would read
the inner future's exception) as soon as the outer future is cancelled while the
inner one is still pending. When Chrome later answers the abandoned command with an
error, `Transaction.__call__` calls `set_exception`, nobody reads it, and when the
`Transaction` is collected asyncio logs it at ERROR.

The shape in the field: a `Runtime.evaluate` whose caller hit its CDP budget, then
the page navigated, and Chrome failed the pending command with
"Inspected target navigated or closed".

## 2. Fix

`_protect` adds `_retrieve` to every reply it shields: a done-callback that calls
`exception()` unless the reply was cancelled. On a reply someone does await it is a
no-op. It is a module function, so it holds no reference to anything.

The docstring's cost paragraph also said an abandoned reply keeps FOUR things alive.
Measured: once the outer future is cancelled and the caller's task is gone, the outer
future is collected and `_retrieve` is the only callback left on the reply. It now
says TWO: the `mapper` entry and the pending `Transaction`.

## 3. Evidence

- **RED** on main (`cb678b9`): `test_an_abandoned_reply_that_fails_is_never_logged_as_unretrieved`
  fails with `AssertionError: ['Transaction exception was never retrieved']`. The pin
  uses a real nodriver `Transaction`, cancels its caller, delivers the error through
  the listener's two lines, collects it, and reads the loop's exception handler (the
  path the 8W events took).
- **GREEN** with the fix: `tests/test_cdp_transport.py` 22 passed.
- **Mutation check** (runtime rebinding inside one pytest process; no file modified):
  `_retrieve` rebound to a no-op is killed by the pin; the unmutated control is green.
