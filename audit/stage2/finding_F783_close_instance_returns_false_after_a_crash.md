# F-783 — `close_instance` returns `False` for a browser that has already died

**Status: OPEN.** Opened by RELEASE-10 (W10) from the crash-recovery fault
injection.
**Severity: MEDIUM** — the cleanup itself is correct; the *report* is not. A
caller following the product's own printed advice sees a failure signal for an
operation that in fact succeeded, and has no way to tell that apart from a real
cleanup failure.

---

## The finding

Kill the Chrome process tree the server says it owns, confirm every process is
gone, then call the next tool. `navigate` fails promptly and typed, with an
actionable message:

> `... The browser may have crashed or the connection dropped. Try closing the
> instance with close_instance and spawning a new one.`

Do exactly that. `close_instance` returns **`False`**.

Everything the recovery contract actually needs *does* happen — measured in the
same node:

| Recovery requirement | Result |
|---|---|
| next tool call fails typed, bounded, with a message | **yes** |
| no process from the killed tree survives | **yes** |
| the crashed instance's profile directory is removable | **yes** |
| a freshly spawned instance navigates and closes cleanly | **yes** |
| `close_instance` reports success | **no — returns `False`** |

So the single defect is the return value of the one call the error message
tells the user to make.

## Why it is worth a finding

`close_instance` is declared `-> bool: True if closed successfully`. A caller
cannot distinguish "the browser was already gone, nothing left to do" from "I
could not clean this up" — and the second is the case that would warrant
escalation. In an agent loop, a `False` here is exactly the kind of signal that
triggers a retry storm against an instance that no longer exists.

It also blocks the W10 recovery invariant for the crash fault, which is why
MQ-126 is `planned` rather than `satisfied`.

## Evidence

`tests/test_resilience.py::test_crash_recovery_after_the_owned_chrome_is_killed`
(`@pytest.mark.characterization`). The node kills the tree enumerated from
`process_cleanup`'s own tracking table (so the process killed is provably the
one the product thinks it owns), awaits confirmed exit before the next call (so
the tool is not merely racing a dying browser), and then pins `close is False`
with a failure message that says the finding is fixed. It also asserts the four
requirements that DO hold, so they cannot silently regress behind the known one.

## What closing it requires

`BrowserManager.close_instance` should treat "the process is already gone" as a
successful close rather than a failed one — the post-conditions (untracked, no
process, profile reclaimable) are all met on that path. That is a `src/` change
and a plan_RELEASE non-goal here.

## Routing

- MQ-126 in `tests/MANUAL_QA_PROTOCOL.md` is `planned`, with this node recorded
  as current support (non-acceptance).
- No `--mq` id in `release-gate.yml` is bound to the pin.
- Related: F-782 (a navigation timeout wedges the connection) is the other half
  of "the advertised recovery path does not fully work". Together they are the
  argument for W5 stating plainly that instance-level recovery today means
  *respawn*, not *repair*.
