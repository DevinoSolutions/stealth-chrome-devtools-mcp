# F-783 — the timeout and cancellation paths escape the one-error convention

**Status:** FIXED (2026-08-07, fix/sentry-2.0.8-batch) — `_with_cdp_timeout`'s
timeout raise is now `ToolError`; the cancellation path still propagates
`CancelledError` untouched (pinned by
`tests/test_error_typing.py::test_cdp_timeout_does_not_convert_cancellation`).
The W15 characterization pin in `tests/test_observability.py` was inverted into
a regression guard (`test_the_timeout_path_now_joins_the_one_error_convention`).
**Severity:** MEDIUM. A client cannot distinguish the commonest runtime failure
from an interpreter bug by type.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/server.py`
(`_with_cdp_timeout`).
**Found by:** plan_RELEASE §2.15 (W15). Previously flagged as the residual
`_with_cdp_timeout:148` item deferred out of the M4-Ph1 C3b error sweep; this
note is where it lands with a pin behind it.

## The behavior

`CLAUDE.md` convention 2: *tools raise `tool_errors.ToolError` /
`InstanceNotFoundError` on failure.* `_with_cdp_timeout` does not:

```python
except TimeoutError:
    tag = f" (instance {instance_id})" if instance_id else ""
    raise Exception(
        f"CDP operation timed out after {t:.0f}s{tag}. "
        "The browser may have crashed or the connection dropped. "
        "Try closing the instance with close_instance and spawning a new one."
    )
```

`type(exc) is Exception` — the base class, not `ToolError`. Since
`_with_cdp_timeout` wraps the great majority of CDP-touching tool bodies, the
single most likely runtime failure in the product is also the one failure a
client cannot classify by exception type. Any `except ToolError:` handler misses
it.

Two secondary observations pinned with it:

* **`{t:.0f}` renders any sub-second budget as `0s`** — a 10 ms timeout reports
  "timed out after 0s", which reads as a bug rather than a timeout. The bytes are
  pinned as-is.
* **`asyncio.CancelledError` is never converted.** Nothing in the tool path
  catches it, so it propagates as a `BaseException` through the correlation
  wrapper. That is arguably correct (a cancellation is not a tool failure), and
  the wrapper's `finally` still resets `correlation_id_var`, which is the
  property that matters. Recorded so the absence is deliberate and visible
  rather than assumed.

## Pins

`tests/test_observability.py`:

* `TestDiagnosticOracle::test_timeout_failure_pins_its_exact_bytes`
* `TestDiagnosticOracle::test_the_timeout_tag_is_omitted_without_an_instance`
* `TestErrorConventionGaps::test_the_timeout_path_escapes_the_one_error_convention`
  (`@pytest.mark.characterization`, `route:F-783`)
* `TestErrorConventionGaps::test_cancellation_propagates_unconverted`
  (`@pytest.mark.characterization`, `route:F-783`)

The two `pytest.raises(Exception)` uses carry `# noqa: B017 PERMANENT(...)`
because the broadness **is** the finding; narrowing them would erase it.

## Contract limitation wording (for W5 §Limitations)

> A CDP operation that exceeds its timeout raises a bare `Exception`, not
> `ToolError`. Clients that branch on exception type must treat the base class as
> a possible tool failure. A sub-second timeout budget is rendered as `0s` in the
> message.

## If it is ever fixed

Changing the raised class to `ToolError` does **not** change the message bytes,
so the two byte-pins stay green and only the two characterization nodes need
updating — deliberately, in the same commit.
