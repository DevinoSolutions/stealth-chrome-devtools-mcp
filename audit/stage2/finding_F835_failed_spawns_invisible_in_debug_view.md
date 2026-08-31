# F-835 — a total spawn outage was invisible in the product's own debug view

**Status:** FIXED (2.0.8) — `fix/F835-failed-spawns-visible`.
**Severity:** MEDIUM (observability).
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/logging_setup.py`
(`with_correlation_id`, `_record_tool_failure`) and
`src/stealth_chrome_devtools_mcp/embedded/debug_logger.py`
(`log_tool_failure`, `_record_error`), reached from
`src/stealth_chrome_devtools_mcp/embedded/tool_registry.py` (`section_tool`).
**Found by:** live use, 2026-08-30.
**Relates to:** F-782 (the same wrapper, the LOG half — see "Scope" below),
F-826 (the unanswered PII/redaction question this fix routes around),
F-834 (the outage that produced the evidence).

## The defect

During an agent-fleet burst, 24 consecutive `spawn_browser` calls failed — a
total spawn outage, nothing could launch. `get_debug_view`, the surface an
operator watches to answer "is this thing healthy", reported:

```
"summary": { "total_errors": 0, ... }
```

Zero. The product said it was fine while every call was failing.

Mechanism: a failing tool raises (`ToolError`, the one error convention), and
the raised exception's ONLY copy went to the MCP client. `spawn_browser`'s own
failure path emits `debug_logger.log_info` lines, which land in the info ring;
nothing anywhere converted "this call raised" into an error record. The one
wrapper every registered tool passes through —
`logging_setup.with_correlation_id`, applied by `ToolRegistry.section_tool` —
was `try/finally` with **no `except`**, so its INFO `end` line was emitted
identically for a success and for a failure.

That is not a `spawn_browser` bug. It was true of all 94 tools: `total_errors`
counted only the handful of sites that happen to call `log_error` by hand
(5 in `server.py`, none in `browser_manager.py`), which is a count of internal
mishaps, not of failures the client actually received.

## The fix, and the seam

**Universal, at the wrapper — not a spawn-site patch.** `with_correlation_id`
gained an `except Exception` that hands the escaping exception to
`debug_logger.log_tool_failure(tool_name, error)` and re-raises with a bare
`raise`. Both branches (async and sync) get it, symmetrically.

Why this seam and not `browser_manager`'s spawn-failure site:

* it is the ONE place every tool already passes through, so every tool's
  failure becomes visible instead of the one tool someone remembered to patch —
  the same chokepoint argument that made this the home of the correlation id
  (F-308). A spot fix would have left 93 tools silent and invited a second
  mechanism later (CLAUDE.md convention 4);
* the recording lands INSIDE the correlation-id scope, so the ring entry
  carries the same id as the call's `start`/`end` log pair and the two can be
  joined;
* it costs nothing on the success path: no wrapper layer was added (the
  `except` is on the existing `try`), and CPython's zero-cost exceptions mean a
  non-raising call pays nothing at all. All 94 tools are on this path.

Four constraints the implementation holds, each pinned:

1. **The exception is never transformed.** Recorded, then `raise` — same
   object, same type, same args, no attributes added (`vars(exc) == {}` is
   already a pinned contract in `test_observability.py`).
2. **No double-recording.** `log_tool_failure` skips when the *same* exception
   (same type, same message, same correlation id) is already in the ring —
   which is what happens when a tool body logs its own failure and then raises
   it. `log_error`'s existing F-204 dedup cannot see that case, because the
   body files it under a different `component.method` signature. The check is
   exact, not a blanket "this call already logged something": a body that logs
   an *unrelated* error mid-call still gets its failure recorded.
3. **The recording can never break a tool call.** `_record_tool_failure`
   suppresses everything: a debug-ring problem must not replace or mask the
   error the client is owed. The recording is the only thing that can be lost.
4. **Idempotent under the runpy double-load.** No module-level mutable state
   was added, and `tool_registry`'s registration path is untouched — the
   `SECTION_TOOLS` idempotency guard and the 94-tool count are unchanged.

`CancelledError` is deliberately not recorded: `except Exception` does not
catch it, and a client disconnect is a shutdown signal, not a tool failure.

### Scope: the ring, not the log

`log_tool_failure` reaches the in-memory ring **without** the durable
`_backend_logger.error(...)` line `log_error` writes. That split is the point of
the change, not a shortcut, and it is why `log_error`'s ring half was extracted
into `_record_error` (one ring-append implementation, two entry points that
differ only in whether the record is also written to the log file).

F-782 — the same wrapper, characterized by plan_RELEASE W15 — states the
condition explicitly: *"If F-782 is fixed by logging the exception, that node
must be re-run and the logged record must go through `release_evidence.redact`
first, or fixing an observability gap will open a disclosure one."* A failure
message echoes the caller's own arguments (paths, selectors, indices, URLs), the
backend log is durable, and an ERROR record on `stealth.backend` is bridged to
the hardcoded Sentry DSN by `LoggingIntegration(event_level=logging.ERROR)`.
There is no redactor available to `src/` (`release_evidence` lives in `tools/`,
a script directory), and the PII question it belongs to is F-826's, which is
flagged for the human.

So F-835 closes the operability gap on the surface where the echo is not a
disclosure — the in-memory ring is process-local and returns only to the client
that already holds those bytes — and leaves the durable, egress-bridged log
alone. F-782 stays OPEN for its log half, with its characterization pin
unmodified and still green.

### One dependent fix: `clear_debug_view` now forgets the dedup signatures

"Clear the view, then watch" is the operator loop a live outage is diagnosed
with. `clear_debug_view` emptied the three rings and the stats but kept
`_seen_errors`, so every repeat of an already-seen failure was deduped against a
signature whose entry no longer existed — the ring would have stayed empty for
exactly the error the operator was watching for. The set is a projection of the
ring, so it now goes with it (`clear_debug_view` and the `clear_debug_view_safe`
rebuild path).

## Behaviour after the fix

A failed `spawn_browser` produces a ring entry filed under component `tool`,
method `spawn_browser`, with the `ToolError`'s message, the call's correlation
id and a traceback. 24 identical failures give `total_errors: 1` and
`stats["tool.spawn_browser.errors"] == 24` — the F-204 dedup is unchanged and
deliberate (one stored entry per distinct signature, every occurrence counted).
What can no longer happen is `0`.

## Tests

`tests/test_tool_failure_visibility.py` (13 pins, hermetic — no Chrome, no disk
profile, no transport), RED first against the reported symptom
(`total_errors == 0` after a failed spawn):

* the reported defect — a failed spawn lands, named by tool and message, with a
  correlation id; and the 24-call outage is visible through the real
  `get_debug_view` tool (`total_errors`, `stats`, `component_breakdown`);
* the property is general — `get_page_content` / `get_element_state` /
  `take_screenshot` failures land too (parametrized);
* the exception reaching the client is unchanged (type, args, `vars`);
* the recording stays in the ring and out of the backend log (the scope pin
  above — a `stealth.backend` handler asserts nothing at WARNING+ and no
  message bytes, while the ring holds the entry);
* a succeeding call records nothing; an error the body already logged is not
  recorded twice; an unrelated error logged mid-call does not hide the failure;
* a throwing debug ring breaks neither a failing nor a succeeding tool call;
* `CancelledError` is not recorded.

`tests/test_debug_logger.py::TestViewAndClear::test_clear_also_forgets_the_dedup_signatures`
covers the clear fix.

Deliberately updated (SOFT, in this PR, with reasons):

* `tests/test_observability.py::TestSecretCanaries::test_no_canary_reaches_stdout_stderr_or_the_backend_log`
  — stdout, stderr and the backend log stay ABSOLUTE; the debug view is now
  asserted to carry a canary only inside a failure message the caller itself
  received. Any other route (environment, header, cookie, script body, URL
  userinfo) remains a release blocker on every surface. It also clears the ring
  first, because the ring is process-wide and now collects the canary-bearing
  failures other tests in the file drive.
* `tests/test_observability.py::TestCorrelation::test_a_failed_call_logs_no_error_record`
  — behaviour unchanged, docstring updated to say why the log half is
  deliberately still open.
* `tests/MANUAL_QA_PROTOCOL.md` — MQ-151's "re-run if F-782 is ever fixed"
  condition fired; recorded what changed and what did not.

## Residual risk

* **The log half of F-782 is still open** (deliberate, above). An operator
  tailing `backend-<pid>.log` during an outage still sees only the `start`/`end`
  pair; the failure is in `get_debug_view` and in `export_debug_logs`. Closing
  it needs the redaction decision, which belongs with F-826 and the human.
* **Ring pressure.** A sustained outage of *distinct* failures now consumes the
  500-entry error ring (`MAX_ERRORS`) that was previously near-empty. It is a
  bounded ring with the same eviction it always had, and dedup collapses the
  repeated case, but the debug view is a busier surface than it was.
* **Cross-test coupling.** Any test that asserts on `get_debug_view` now sees
  every failed tool call in its process. Two such tests were scoped in this PR
  (the canary node, and the new file's autouse fixture); a future one must do
  the same.
