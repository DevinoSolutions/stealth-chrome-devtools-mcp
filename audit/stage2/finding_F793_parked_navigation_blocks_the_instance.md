# F-793 — a parked navigation blocks every other call on the same instance

**Status:** OPEN — characterized by plan_RELEASE W13, not fixed (W13 is zero-`src/`).
**Severity:** MEDIUM. Concurrency is advertised by the API shape and does not
hold within one instance; the blocked call reports a misleading crash message.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/browser_manager.py` /
`element_resolution.py` — one CDP connection per instance, with no queueing and
no per-call fairness. The message comes from
`embedded/server.py::_with_cdp_timeout`.
**Found by:** plan_RELEASE §2.13 (W13), while building MQ-140's
reversed-completion node — whose first draft used one instance and failed here.

## The behavior

With W7's fixture holding a navigation open on instance A (barrier-confirmed in
flight), a second `tools/call` against **the same instance** does not queue
behind it and does not run alongside it. It waits out its own CDP budget and
fails:

```
Error calling tool 'execute_script': CDP operation timed out after 10s
(instance 56665fb0-…). The browser may have crashed or the connection dropped.
Try closing the instance with close_instance and spawning a new one.
```

Nothing has crashed and no connection has dropped. The instance is healthy; it
is busy.

Three measurements bound the claim:

| what | result |
|---|---|
| 4 short calls issued together on ONE instance | **all four answer**, each with its own payload |
| a short call issued while a navigation is parked on the SAME instance | **times out** (10 s) |
| the same short call on a DIFFERENT instance at the same moment | **answers normally** |

So intra-instance concurrency works for calls that complete quickly; what does
not work is a call arriving behind an operation that is genuinely parked.

## Why it is worth a finding

The tool surface (`instance_id` on every call, `list_instances`,
`close_instance`) reads as a concurrent, multi-instance API, and nothing in the
docstrings says a slow call makes its instance unavailable. An agent that issues
a navigation and a probe together gets a "the browser may have crashed" for the
probe and, following the message's own advice, closes a perfectly good instance.

The message is the sharper half of the defect: **"busy" is reported as
"crashed"**. Compare F-783, which records the same message escaping the one-error
convention; this is the same string arriving in a case where it is not merely
untyped but wrong.

## Evidence

`tests/test_wire_semantics.py::test_a_parked_navigation_blocks_every_call_on_the_same_instance`
(`@pytest.mark.characterization`, route:F-793). The parked navigation is
confirmed in flight by the fixture barrier before the second call is issued, so
the blocker demonstrably exists; the pin asserts the blocked call FAILS and
matches the exact tail bytes of the timeout message, so a fix (queueing, or an
honest "instance busy") turns it red.

Its cross-instance control is a separate node —
`::test_reversed_completion_keeps_each_result_on_its_own_request` — which runs
the identical call shape on a second instance while the first is parked, and
succeeds. Without it, "the second call timed out" would be equally consistent
with "that call never works".

## Contract limitation wording (for W5 §Limitations)

> Calls against one instance are serialized on its single CDP connection. While
> a long-running operation (for example a navigation to a slow origin) is in
> progress, another call on the same instance does not queue: it fails with the
> CDP-timeout message, which says the browser may have crashed. Concurrency
> across separate instances is unaffected.

## Routing

- MQ-139 in `tests/MANUAL_QA_PROTOCOL.md` is satisfied for concurrent SHORT
  calls on one instance and for cross-instance isolation; this finding is the
  stated bound on that claim.
- MQ-140 is satisfied cross-instance for exactly this reason, stated in the node.
- No `--mq` id in `release-gate.yml` is bound to the pin.
- Related: F-783 (the timeout path escapes the error convention), F-788 (a
  navigation timeout wedges the connection), F-794 (a cancellation wedges the
  instance).
