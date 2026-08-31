# F-782 — a failed tool call emits no error log record

**Status:** OPEN (log half) — characterized by plan_RELEASE W15, not fixed there
(W15 is zero-`src/`). **F-835 (2.0.8) closed the in-memory half**: the wrapper
now has an `except` that records the escaping exception in `debug_logger`'s ring
and re-raises it unchanged, so `get_debug_view` shows a failure. It deliberately
does NOT write the durable `_backend_logger.error` line — see "The one upside"
below, whose condition (redact first) is unanswered. What remains open is
exactly consequences 1 and 2 below, for the LOG file.
**Severity:** MEDIUM (operability). The backend log cannot answer "what failed?".
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/logging_setup.py`
(`with_correlation_id`), reached from
`src/stealth_chrome_devtools_mcp/embedded/tool_registry.py` (`section_tool`).
**Found by:** plan_RELEASE §2.15 (W15).

## The behavior

`with_correlation_id` is the one chokepoint every registered tool passes through.
Both branches (async and sync) are shaped:

```python
token = correlation_id_var.set(new_correlation_id())
_tool_call_logger.info("tool %s start", tool_name)
try:
    return await func(*args, **kwargs)
finally:
    _tool_call_logger.info("tool %s end (%.1fms)", tool_name, elapsed_ms)
    correlation_id_var.reset(token)
```

There is **no `except`**. Consequences:

1. a failing call logs the same `start` / `end` pair as a succeeding one, so the
   backend log cannot distinguish them at all;
2. nothing at `WARNING` or above is emitted, so a log level that filters INFO
   sees a failed call as complete silence;
3. the exception type and message are never written anywhere — the only copy
   goes to the MCP client.

Combined with F-781 (no correlation id on the exception), the two ends cannot be
joined from either side: the client has a message with no id, the log has an id
with no message.

## Why this is not merely cosmetic

`RUNBOOK.md`'s triage path starts at the backend log. For any tool failure that
path currently terminates immediately: the log shows the call was made and
returned, and nothing else. The failure is only visible in the client's
transcript.

## The one upside, recorded so it is not mistaken for a design

Because nothing logs the exception, no caller-supplied argument echoed into a
failure message (see `Invalid index value: {index}`,
`Invalid JSON in extraction_options: {value}`, and the raw `OSError` paths of
F-784) is written to the backend log or to stderr. W15's canary sweep therefore
finds **no disclosure** on those surfaces —
`TestSecretCanaries::test_no_canary_reaches_stdout_stderr_or_the_backend_log`
passes. That is a consequence of the gap, not a control. **If F-782 is fixed by
logging the exception, that node must be re-run and the logged record must go
through `release_evidence.redact` first**, or fixing an observability gap will
open a disclosure one.

F-835 took that condition literally. It closed the operability gap on the
surface where the argument echo is not a disclosure — the in-memory ring, which
is process-local and reaches only the client that already received those bytes —
and left the durable, Sentry-bridged log alone. The canary node was re-run: the
three absolute surfaces (stdout, stderr, backend log) stay clean, and the debug
view is asserted to carry a canary only inside a failure message the caller
itself received. Closing the log half still needs the redactor question
answered; it is the same question F-826 raises for the Sentry path.

## Pin

`tests/test_observability.py::TestCorrelation::test_a_failed_call_logs_no_error_record`
(`@pytest.mark.characterization`, `route:F-782`).

## Contract limitation wording (for W5 §Limitations)

> The backend log records that a tool call started and ended, with a per-call
> correlation id. It does not record whether the call succeeded, nor the error
> type or message when it failed. Failure detail exists only in the MCP client's
> transcript.
