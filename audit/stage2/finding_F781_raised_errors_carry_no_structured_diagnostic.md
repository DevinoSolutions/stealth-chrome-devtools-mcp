# F-781 — a raised tool error carries no code, phase, next step, or correlation id

**Status:** OPEN — characterized by plan_RELEASE W15, not fixed (W15 is zero-`src/`).
**Severity:** MEDIUM (operability). No data loss, no incorrect result; a failure is
simply harder to act on and harder to trace than the plan assumed.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/tool_errors.py`,
`src/stealth_chrome_devtools_mcp/embedded/logging_setup.py`.
**Found by:** plan_RELEASE §2.15 (W15), "structured diagnostic oracle".

## What §2.15 asked for

> assert a stable error type/code, request or operation correlation where exposed,
> failed phase, actionable local next step, exact M6-pinned message bytes, and no
> protocol/stdout contamination.

Of those, the product provides exactly two: a **stable error type** and **exact
message bytes**. Everything else is absent from the value the caller receives.

## What is actually there

`ToolError` is a bare `Exception` subclass with no `__init__`, no attributes and
no payload:

```python
class ToolError(Exception): ...
class InstanceNotFoundError(ToolError): ...
```

So for any raised failure:

* `vars(exc) == {}` — no `error_code`, no `phase`, no `next_step`;
* `hasattr(exc, "correlation_id")` is `False`;
* the only machine-readable content is `str(exc)`, which is why every message
  W15 touches is pinned byte-for-byte.

A correlation id **does** exist, but it never reaches the caller.
`logging_setup.with_correlation_id` sets `correlation_id_var` for the duration of
the call and `CorrelationIdFilter` stamps it onto log records. The exception
propagates through that wrapper's `try/finally` untouched. Net effect: a user who
reports "it failed" cannot quote the one token that would locate the call in the
backend log, and the operator cannot ask them for it.

Three of the highest-traffic messages additionally offer **no recovery step at
all** — not even a pointer at the tool that would fix the situation:

| Message | Next step offered |
|---|---|
| `Instance not found: {instance_id}` | none (does not mention `list_instances` or `spawn_browser`) |
| `Invalid index value: {index}. Must be a number.` | none (echoes the bad value, never a good one) |
| `Invalid JSON in extraction_options: {value}` | none |

By contrast the CDP-timeout and script-size messages *do* name a concrete local
action, so the capability exists in the codebase and is applied unevenly.

## Pins

`tests/test_observability.py`:

* `TestCorrelation::test_the_raised_error_carries_no_correlation_id`
* `TestErrorConventionGaps::test_the_error_types_carry_no_code_phase_or_next_step`
* `TestRecoveryGuidance::test_the_commonest_failures_offer_no_next_step`

Each is `@pytest.mark.characterization` with `route:F-781` in its docstring. They
record present behavior; none of them satisfies an MQ success requirement.

## Contract limitation wording (for W5 §Limitations)

> A failed tool call returns a typed exception and a fixed message string. It
> carries no error code, no failed-phase field, no next-step field, and no
> correlation id. A correlation id is generated per call and appears in the
> backend log only; it is not returned to the client, so a user-reported failure
> cannot be correlated to a log line without a timestamp search.

## If it is ever fixed

Adding fields to `ToolError` changes no message bytes and is therefore safe with
respect to the M6 pins, but the three characterization nodes above must be
updated in the same commit that adds them — that is what they are for.
