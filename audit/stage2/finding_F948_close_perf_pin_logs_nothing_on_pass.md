# F-948 — the close-performance pin has a 3 s bound with no recorded margin, and logs its time only when it fails

**Severity:** Low. Test-only; the product's close path is unchanged. A loaded runner
fails the release gate on a healthy tree, which costs a full gate re-run (~1 h).
**Files:** `tests/test_browser_integration.py`
(`TestClosePerformance::test_close_is_fast`).
**Seen:** the release 2.1.24 gate on 2026-10-08 (PR #193, `2e2a46e`, a version-bump-only
commit), `integration (Windows/X64)`:

```
E   AssertionError: close took 4.62s — teardown hang regressed
E   assert 4.625 < 3.0
```

---

## 1. Cause

The pin asserts `elapsed < 3.0` and prints the measured time only in its assertion
message. A passing run leaves no number behind. Across the last 43 Windows integration
jobs it passed 42 times and failed once, at 4.62 s. Nothing recorded showed how close
the passing runs had come to 3 s, so the bound's margin was never measured on the
runners that enforce it.

## 2. Fix

- The close time is printed under `capsys.disabled()`, the convention
  `tests/test_soak_stability.py` already uses, so every run, passing or failing, puts
  `[close-perf] close_instance took N.NNs` in the CI log.
- The bound moves to 5.0 s. The regression it guards still fails it: the old hang
  blocked the whole 5 s `wait_for` and then forced cleanup, 6–8 s per close, so it
  cannot finish under 5 s.

## 3. Evidence

- Census of `test_close_is_fast` in the logs of the last 43 Windows integration jobs:
  42 PASSED, 1 FAILED (4.62 s, the run above).
- The new log line works: a local run printed `[close-perf] close_instance took 10.23s`.
  That number is not a measurement of the product, because the machine was at 100% CPU,
  3.8 GB free RAM and 183 Chrome processes from other sessions. The CI runners, which
  enforce the bound, are where the margin is now recorded on every run.
