# F-1016 — a failed tab close logs an empty message (T-5900)

**Severity:** Low. **Files:** `embedded/tab_close.py` (`log_failure`, `_describe`), `tests/test_close_instance_offload.py`. F-1016 was unclaimed.

## 1. Evidence

CI run 39564101655 (main bffee6f), integration (Windows/X64): `test_a_fleet_of_six_browsers_answers_truthfully_about_every_page` failed its teardown assertion with
`['browser_manager.close_instance: Failed to close tab for f88a69aa-...: ']`. The same log shows an asyncio `Browser.update_targets` task that died on `ConnectionRefusedError`
(WinError 1225): a Chrome endpoint was already gone around that time.

## 2. Cause of the EMPTY text

`close_instance` sends `Target.closeTarget` under `asyncio.wait_for(..., 2.0)`; on expiry that raises `TimeoutError()`, whose `str()` is `""`. `tab_close.log_failure` logged only
`{exc}`. So the failure is, with high probability, that one close outran its 2 s bound while six Chromes were torn down at once, but the log could not say so.

## 3. Fix

The line is `Failed to close tab for <id>: <Type>` for a text-less exception and `<Type>: <text>` otherwise; the gone-tab debug path uses the same text. The pin
(`test_a_tab_close_that_times_out_names_its_type`) fails on the pre-fix line (the message ends at `: `). The existing `str(error) in warnings[0]` controls still hold.
No line moved in `browser_manager.py` (at its 1438-line cap).

## 4. Not done

The race itself is NOT proven: nothing in the run's log shows whether Chrome was slow to answer or already exiting when the command was sent (the nearby `ConnectionRefusedError`
points at the latter). It was therefore not excused in `TEARDOWN_WARNINGS_WITH_A_REAPER`, nor was the 2 s bound changed. A recurrence now says `TimeoutError`, which separates a slow Chrome
(timeout) from a dead one (a connection error with text).
