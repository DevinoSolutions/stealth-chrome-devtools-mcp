# F-788 — a timed-out navigation permanently wedges the instance's CDP connection

**Status: FIXED** (unreleased, on `fix/F883-execute-script-awaits`). Opened by
RELEASE-10 (W10) from the controlled hang-before-headers fault the new fixture
routes made possible; closed from the other end, by F-883's B1 review, which
found the SAME mechanism reachable through `execute_script`.

**The fix is not in `browser_manager.navigate`.** Nothing about the navigation
deadline was wrong: what was wrong is that cancelling a Python await cancelled
nodriver's `Transaction` while it was still registered in `Connection.mapper`.
So the fix sits one layer below every deadline in the tree —
`embedded/cdp_transport.py` wraps `Transaction.__await__` in `asyncio.shield`,
which moves the cancellation onto a throwaway outer future and leaves the
registered one pending, exactly as the listener expects to find it. That covers
`navigate`'s OWN inner `asyncio.wait_for` (`browser_manager.py:1181`), which is
a different scope from `_with_cdp_timeout` and is why this finding was measured
directly rather than inferred from the `execute_script` case.

**Measured closed**, Chrome 152 / nodriver 0.47, two independent ways:

* `tests/test_resilience.py::test_a_navigation_timeout_wedges_the_instance_connection`
  — the characterization pin this finding names below — went RED with its own
  message, "a normal navigation succeeded after a timeout — F-788 is fixed and
  MQ-128 can be promoted from planned to satisfied". It is now inverted, in this
  same change, to `test_a_navigation_timeout_leaves_the_instance_usable`, which
  asserts the full recovery invariant (driveable, closes clean, fresh spawn).
* A standalone probe against a route that withholds its response headers for 9 s
  with `timeout=2000`: before, `Connection._listener()` finished with
  `InvalidStateError('invalid state')` and the next call timed out after 10 s;
  after, the follow-up `execute_script` answered `{'success': True, 'result':
  42}`.

**One thing the fix does not do, and cannot.** A `navigate` that times out has
already handed `Page.navigate` to Chrome, and cancelling our await does not
un-send it: measured, the page DID land on the slow route once its headers
arrived (`document.title == 'Late'` afterwards). What stops is the rest of the
tool body — the retry, the post-navigation reads, the state update — and the
instance stays usable. "The navigation was cancelled" was never true of the
browser; it is true of our waiting for it.
**Severity: HIGH** — this is the fault class W10 exists to find: a recoverable
error that is not actually recoverable. One navigation timeout costs the caller
the whole instance, and nothing in the response says so. The product's own
error message advises a recovery path that does not work (see also F-789).

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

## What closing it required (and what was rejected)

This section asked for one of two things: nodriver stops killing its listener on
a cancelled transaction, or the caller stops cancelling a transaction it cannot
clean up. The shipped fix is the second, taken at the narrowest point — the
await itself — so no caller has to remember anything:

* **Taken:** shield `Transaction.__await__`. One assignment covers every send in
  the tree, ours and nodriver's own (`Tab.evaluate`, `Element.apply`,
  `Browser.update_targets`, `Tab.close`), none of which pass through a seam of
  ours. Nothing in nodriver ever cancels a Transaction deliberately, so no
  library path loses anything.
* **Rejected — pop our entry out of `mapper`:** the listener's `pop` is
  unguarded too, so a missing entry is a `KeyError` that ends the listener the
  same way, one line earlier.
* **Rejected — shield at `tool_runtime._with_cdp_timeout`:** it protects the
  send by detaching the whole operation, so a cancelled `navigate` finishes in
  the background. Shipped briefly as `529cec0` and caught by
  `tests/test_wire_semantics.py`; reverted.
* **Rejected — guard `set_result` / replace `Transaction`:** a double of a
  library object in the hot path of every command.
* **Rejected — replace the connection after a timeout:** it treats a healthy
  connection as disposable, and the tab, its handlers and its enabled domains go
  with it.

## Routing

- MQ-128 in `tests/MANUAL_QA_PROTOCOL.md` was `planned` behind this finding and
  its recovery half is now proved by the inverted node; the step is updated in
  the same change.
- No `--mq` id in `release-gate.yml` is bound to the pin; binding one is a
  release-gate decision, not this fix's.
- W5's limitation is retired: a navigation timeout IS recoverable now. What
  remains true is F-789's half — what `close_instance` returns for a browser
  that is already gone.
