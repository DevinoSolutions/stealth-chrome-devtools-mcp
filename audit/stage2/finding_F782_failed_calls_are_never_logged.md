# F-782 — a failed tool call emits no error log record

**Status:** OPEN — characterized by plan_RELEASE W15, not fixed (W15 is zero-`src/`).
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

## Pin

`tests/test_observability.py::TestCorrelation::test_a_failed_call_logs_no_error_record`
(`@pytest.mark.characterization`, `route:F-782`).

## Contract limitation wording (for W5 §Limitations)

> The backend log records that a tool call started and ended, with a per-call
> correlation id. It does not record whether the call succeeded, nor the error
> type or message when it failed. Failure detail exists only in the MCP client's
> transcript.
