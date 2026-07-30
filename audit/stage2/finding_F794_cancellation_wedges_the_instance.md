# F-794 — a cancelled call leaves its instance permanently wedged

**Status:** OPEN — characterized by plan_RELEASE W13, not fixed (W13 is zero-`src/`).
**Severity:** HIGH. Cancelling is the documented way to stop a call, and doing
it costs the caller the whole browser.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/browser_manager.py` —
the instance's CDP connection is not returned to a usable state when the awaiting
task is cancelled.
**Found by:** plan_RELEASE §2.13 (W13), MQ-141.

## The behavior

Cancel a confirmed in-flight navigation with `notifications/cancelled`. The
client's wait ends in milliseconds (see F-791 for the shape of that answer) and
the **server** is fine — `list_instances` answers, a fresh instance spawns,
navigates and closes. The **instance that was cancelled** is not fine. Its next
navigation burns the full CDP budget and returns:

```
Error calling tool 'navigate': CDP operation timed out after 30s
(instance 17e734a5-…). The browser may have crashed or the connection dropped.
Try closing the instance with close_instance and spawning a new one.
```

The fixture route was released before that navigation was issued, so nothing was
still holding the origin. The connection itself never recovers.

This is the same shape F-788 records for a navigation *timeout*: the operation
ends, the connection does not come back. F-794 is the cancellation entrance to
the same state — worth its own id because the trigger is a deliberate, supported
client action rather than a fault.

## Why it is worth a finding

Cancellation exists so a caller can change its mind cheaply. Here it is not
cheap: the instance — its profile, its cookies, its page state, its position in
a longer journey — is gone, and the only recovery is `close_instance` plus a new
spawn. An agent that cancels a slow page load to try a different one must
re-establish everything.

It is also the half that keeps MQ-141 `planned`. The step's scope includes "the
session still works afterwards"; the *server* does, the *instance* does not, and
covering only the half that holds cannot satisfy the step.

## Evidence

`tests/test_wire_semantics.py::test_cancelling_a_confirmed_in_flight_request_ends_it_with_code_zero`
(`@pytest.mark.characterization`, route:F-791 + route:F-794). The node pins both
findings because they are two properties of one measurement. For F-794 it
asserts the post-cancellation navigation FAILS and matches the exact tail bytes
of the timeout message, then proves server-level recovery separately by
spawning a fresh instance that navigates and closes cleanly — so "the product is
wedged" is excluded and the claim is narrowed to the instance.

Pinned in the direction that makes a fix red: an instance that survives its own
cancellation turns the node red and lets MQ-141's recovery half be claimed.

## Contract limitation wording (for W5 §Limitations)

> Cancelling an in-flight call ends the caller's wait but leaves that instance's
> CDP connection unusable: the next call on it fails with the CDP-timeout
> message. Recovery is `close_instance` plus a new `spawn_browser`; the server
> and other instances are unaffected.

## Routing

- MQ-141 in `tests/MANUAL_QA_PROTOCOL.md` is `planned`, behind this and F-791,
  with the node recorded as current support (non-acceptance).
- No `--mq` id in `release-gate.yml` is bound to the pin.
- Related: F-788 (a navigation timeout wedges the same connection) is the fault
  entrance to this state; F-789 (`close_instance` returns `False` after a crash)
  is why the advertised recovery path is itself imperfect. Together the three
  are the argument for W5 stating plainly that instance-level recovery means
  *respawn*, not *repair*.
