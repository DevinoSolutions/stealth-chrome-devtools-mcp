# F-782 — a timed-out navigation permanently wedges the instance's CDP connection

**Status: OPEN.** Opened by RELEASE-10 (W10) from the controlled
hang-before-headers fault the new fixture routes made possible.
**Severity: HIGH** — this is the fault class W10 exists to find: a recoverable
error that is not actually recoverable. One navigation timeout costs the caller
the whole instance, and nothing in the response says so. The product's own
error message advises a recovery path that does not work (see also F-783).

---

## The finding

Point `navigate` at a route that accepts the TCP connection and then sends
nothing. The tool behaves exactly as specified: it fails on its own deadline,
inside the harness bound, with the M6-pinned message

```
Navigation to <url> timed out after 4000ms
```

Then **every subsequent CDP operation on that instance fails**. The next
`navigate` does not succeed; it burns the full `_with_cdp_timeout` budget and
raises the generic

```
CDP operation timed out after 35s (instance <id>). The browser may have crashed
or the connection dropped. Try closing the instance with close_instance and
spawning a new one.
```

The browser has not crashed and the connection has not dropped. Chrome is
healthy and the websocket is open. What is broken is nodriver's dispatcher.

## Mechanism (measured, not assumed)

1. `BrowserManager.navigate` bounds the navigation with
   `asyncio.wait_for(tab.get(url), timeout_seconds)`.
2. On timeout, `wait_for` **cancels** that task while nodriver's `Transaction`
   for the in-flight `Page.navigate` is still registered in
   `Connection.mapper`. The transaction future is now cancelled.
3. When Chrome eventually answers that navigate, `Connection._listener` calls
   the transaction, whose CDP generator raises `StopIteration`, and nodriver
   does `self.set_result(e.value)` on the already-cancelled future →
   `InvalidStateError`.
4. That exception escapes `_listener`, so **the listener task dies**. It is the
   one coroutine that resolves every pending future on that connection, so from
   that moment no `tab.send(...)` on this instance can ever complete.

The retry inside `navigate` makes it visible immediately: attempt 2 calls
`_replace_main_tab`, which calls `previous_tab.close()`, which is a `tab.send`
— and hangs until the outer wrapper cuts it.

Observed traceback shape (nodriver 0.47, `connection.py:123` → `:128` → `:444`):

```
StopIteration: (FrameId(...), LoaderId(...), None)
  ...
asyncio.exceptions.InvalidStateError: invalid state
Task exception was never retrieved
future: <Task finished ... Connection._listener() ...>
```

## Why this matters more than the timeout itself

`_with_cdp_timeout` is doing its job: callers stay bounded and always get a
message. Without it this would be an unbounded hang. But bounded-and-broken is
still broken — the instance is dead, and the only signal is a message that
misattributes the cause to a crashed browser.

It also means the W10 recovery invariant ("after every injected fault the
server must be usable again") does **not** hold for the navigation-timeout
fault, which is precisely the case plan_RELEASE §2.10 says is "the finding".

## Evidence

- Acceptance-shaped and passing (the timeout half is genuinely correct):
  `tests/test_resilience.py::test_load_wait_against_a_hang_times_out_with_the_pinned_message`,
  `tests/test_resilience.py::test_networkidle_wait_against_a_hang_times_out_with_the_pinned_message`.
  Both assert the exact M6 message bytes and that the failure took at least the
  product deadline, so an unrelated early error cannot pass as a timeout.
- Sensitivity control:
  `tests/test_resilience.py::test_slow_success_control_completes_when_released`
  drives the SAME route, releases it inside the deadline, and requires it to
  complete and serve its exact body. Without it, "it timed out" would be
  indistinguishable from "this route never works".
- The pin:
  `tests/test_resilience.py::test_a_navigation_timeout_wedges_the_instance_connection`
  (`@pytest.mark.characterization`). It asserts the *next* navigation raises,
  and its failure message says the fix landed — so closing this finding turns
  the pin red and forces a deliberate update.
- Fixture half proved without a browser:
  `tests/test_fixture_dynamic_routes.py::test_the_hang_before_headers_route_writes_no_byte_until_released`.

## What closing it requires

Either nodriver stops killing its listener on a cancelled transaction (upstream,
or a guarded `set_result`), or `BrowserManager.navigate` stops cancelling a CDP
transaction it cannot clean up — for example by draining/abandoning the
transaction explicitly, or by replacing the whole connection after a navigation
timeout rather than reusing it. Both are `src/`-side changes and are out of
scope for a test workstream.

## Routing

- MQ-128 in `tests/MANUAL_QA_PROTOCOL.md` is `planned`, not `satisfied`: its
  timeout half is proved, its recovery half is blocked on this finding, and the
  step says so in words.
- No `--mq` id in `release-gate.yml` is bound to the pin.
- W5 should carry this as a limitation: a navigation timeout is not a
  recoverable error today — the caller must close and respawn (and see F-783
  for what `close_instance` returns when they do).
