# F-942 — `execute_script` reports a page that navigated under the script as "Failed to execute script"

**Severity:** Medium. The message says the script failed to run, when it may have run
and caused the navigation (a form submit, a click, a `location` change). A caller that
trusts it runs a side-effecting script a second time. It was also the largest live
issue in Sentry.
**Files:** `embedded/script_evaluation.py` (`NAVIGATED_UNDER_SCRIPT`, `evaluate`, module
docstring), `tests/test_execute_script_async.py`,
`tests/test_observability_expected_noise.py`, `NAVMAP.md` (`script_evaluation.py` row).
**Sentry:** STEALTH-CHROME-DEVTOOLS-MCP-8J — 100 events from 2.1.9 to 2.1.21, 18 of
them on 2.1.21 (the current release) on 2026-10-06; -AY — 2 events on 2.1.18, the same
failure grouped under a different stack:

```
ToolError: Failed to execute script: Inspected target navigated or closed [code: -32000]
  <- ToolError (the same text)            # the tool body's re-raise
  <- ProtocolException: Inspected target navigated or closed
```

---

## 1. Cause

`script_evaluation.evaluate` sends one `Runtime.evaluate` with `awaitPromise`. When the
page navigates or its tab closes while that command is in flight, Chrome answers it
with `-32000 Inspected target navigated or closed`. `evaluate` wrapped every send
failure the same way, `ToolError(f"Failed to execute script: {e!s}") from e`, which
reads as "the script did not run".

Because the chain ended in nodriver's `ProtocolException`, Sentry's `error-convention`
class (ours over ours) did not drop it, so every one shipped.

## 2. Fix

`evaluate` checks `navigation_milestone.document_swapped(e)` first. That is THE one test
for this error (code AND message, F-882e), imported rather than re-keyed. On a match it
raises `NAVIGATED_UNDER_SCRIPT`:

> The page navigated or its tab closed while the script was running, so the script's
> result was lost (Chrome: Inspected target navigated or closed). The script may have
> run before that, so anything it does (a click, a form submit, a location change) may
> already have happened: check the page before running it again. To act and then read,
> run the action in one call and read the new page in the next.

"May have run", not "ran": a navigation already under way when the script was sent can
take the document before the script starts.

It is raised `from None`. The outcome is recognised by name and explained, so the chain
is ours alone and `error-convention` drops it. Every other failure, including another
`-32000` message, keeps "Failed to execute script" and its `ProtocolException`, and still
ships.

Not done: retrying. Re-running a script whose side effects may already have happened is
exactly what the new message warns against.

## 3. Evidence

- **RED** on main (`cb678b9`):
  - `test_a_page_that_navigates_under_the_script_is_named_as_that` fails: the tool says
    "Failed to execute script".
  - `TestTheBudgetIsTheProductAnswering::test_a_script_whose_page_navigated_is_dropped`
    fails: the real tool chain ships.
  - Both pins drive the real tool body over a `ProtocolException` built by nodriver's
    own constructor from `fakes.TARGET_SWAPPED_ERROR` (Chrome's wording, copied, not
    imported from the product).
- **Controls, green on both trees:** the same code with another message
  ("Cannot find context with specified id") still answers "Failed to execute script" and
  still ships.
- **GREEN** with the fix: the three touched test files, 112 passed.
- **Mutation check** (runtime rebinding inside one pytest process; no file modified):
  - `document_swapped` → always False is killed by the two named pins.
  - `document_swapped` → code only, message ignored, is killed by both controls.
  - The unmutated control is green.
