# F-945 — the fence pin compares two reads of the live registry taken minutes apart

**Severity:** Low for users (test-only), Medium for the lane: a pre-push failure that
no change caused, and one that teaches the reader to distrust a red lane.
**Files:** `tests/operator_fence.py` (`REGISTRY_BYTES_AT_INSTALL`,
`_read_registry_bytes`, `_pids_named_by`, `install`), `tests/test_operator_fence.py`
(`test_the_conftest_holds_what_the_fence_protects`).
**Seen:** the 2026-10-07 F-944 pre-push lane:

```
assert frozenset({21348, 48344, 52456}) == frozenset({21348, 52456})
```

Backend 48344 registered in the operator's real `~/.stealth-mcp/server.json` after
conftest import and before the pin ran.

---

## 1. Cause

`conftest._FENCED_LIVE_BACKEND_PIDS` is `install()`'s answer, read from the real
`server.json` at conftest import. The pin then called `recorded_backend_pids()`,
which reads the same real file again, up to ~10 minutes later. Other Claude Code
sessions on the machine start and stop backends at any time, so the two reads can
differ with nothing wrong in the code.

## 2. Fix

`install()` keeps the exact bytes it read (`REGISTRY_BYTES_AT_INSTALL`, `None` when
absent or unreadable) and derives the pids from them (`_pids_named_by`). The pin
replays those bytes into a decoy state dir, points `REAL_STATE_DIR` at it, and runs
the real reader (`recorded_backend_pids()`) over it. Both sides now come from one
read. The assertion is still strict equality; it is not loosened.

Its purpose survives: a reader that silently answered `frozenset()` for a record
that has entries would no longer equal the global, because the global is derived
from the same bytes by the same parser. An absent record replays as an absent file
and must give the empty set on both sides.

## 3. Evidence

- Pin passes against the fixed code.
- Mutation (runtime rebinding in a `-p` plugin, no file edited): the conftest-side
  capture is forced to `frozenset()` while the replayed record names pids; the pin
  fails. Counts are in the commit report.
- Control run after clearing `__pycache__`: passes.
