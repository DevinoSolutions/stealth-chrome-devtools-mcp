# F-787 — `wait_until="networkidle"` is a fixed 2-second sleep, not a network-quiescence wait

**Status: OPEN.** Opened by RELEASE-10 (W10) from the fault-injection
measurements the new controlled hang routes made possible.
**Severity: MEDIUM** — the failure mode is a *silent wrong success*: `navigate`
reports `success: True` for a page whose transfer is demonstrably still open.
A caller who chose `networkidle` precisely because they needed the page settled
gets no signal that it is not.

---

## The finding

`navigate(..., wait_until="networkidle")` does not observe the network at all.
The implementation (`embedded/browser_manager.py::_wait_for_navigation_condition`)
is:

```python
if wait_until == "networkidle":
    await asyncio.sleep(min(timeout_seconds, 2.0))
    return
```

So `networkidle` means "sleep up to two seconds, then declare victory". The
other two conditions are real: `domcontentloaded` awaits
`Page.domContentEventFired` and the default `load` awaits `Page.loadEventFired`,
both under the remaining budget. Only `networkidle` is synthetic.

What that produces, measured against W10's `/fault/hang-after-headers`
controller — a route that commits a chunked `200`, flushes a partial body, and
then sends nothing at all until the test releases it:

| Observation | Value |
|---|---|
| `navigate(wait_until="networkidle")` | returns, `success: True` |
| elapsed | ~2s, inside the 4s deadline |
| fixture controller `released` | `False` — the server has not sent the rest |
| `#partial` in the DOM | present (the committed prefix) |
| `#complete` in the DOM | **absent** — the release-only tail never arrived |

The page is committed and partly parsed; the transfer is open; the tool says the
navigation succeeded. Nothing in the return value distinguishes this from a page
that genuinely settled.

This is not a hang and not a raw `-32000`. It is the third failure shape W10
exists to catch, and the one hardest to notice from the outside.

## What it does *not* mean

`networkidle` still honours the navigation deadline: at a route that never
commits at all, `wait_until="networkidle"` times out with the same M6-pinned
message as `load`
(`tests/test_resilience.py::test_networkidle_wait_against_a_hang_times_out_and_recovers`
is the acceptance evidence for that half, and it is a real assertion, not a
characterization). The defect is confined to the *quiescence* promise the
parameter name and the tool's own docstring make.

## Evidence

Pinned by
`tests/test_resilience.py::test_networkidle_returns_before_the_transfer_completes`
(`@pytest.mark.characterization`). It asserts the success return, the
sub-deadline elapsed time, the un-released controller, the present `#partial`,
and the absent `#complete` — including the sensitivity guard that fails the pin
if the body ever *does* complete, so a real implementation surfaces as a
deliberate test update rather than a silently passing test.

The controller half is proved without a browser by
`tests/test_fixture_dynamic_routes.py::test_the_after_headers_route_commits_then_completes_only_on_release`,
so a red integration node can never be blamed on the fixture.

## Why it is not fixed here

`src/` production edits are a hard non-goal of plan_RELEASE (§1.2). A real
implementation means tracking in-flight requests (`Network.requestWillBeSent` /
`loadingFinished` / `loadingFailed`) and waiting for a quiet window — a genuine
behavioural change to a served tool, with its own timeout semantics to design.
That belongs to a scoped follow-up, not to a test workstream.

## Routing

- W5 narrows the advertised contract: `networkidle` is qualified only as a
  deadline-honouring wait condition, never as a completion guarantee.
- MQ-128 in `tests/MANUAL_QA_PROTOCOL.md` states the same exclusion in words, so
  a reader of the parity manifest cannot infer a quiescence claim from the step.
- The characterization node is **support-only**: it carries no `route:` token,
  because a valid `known-gap` needs the identical token in the node docstring,
  the manifest, and the W5 ledger. It does not satisfy MQ-128 and is not bound
  to any `--mq` id in `release-gate.yml`.
