# F-947 — the future-stamp heartbeat pin has a one-second margin, and a starved lane spends it

**Severity:** Low. Test-only; the product's bound is right. A slow lane fails the
pre-push hook on a healthy tree, which costs a full re-run (~50–70 min on a loaded
machine).
**Files:** `tests/test_backend_heartbeat.py`
(`TestTheRead::test_a_stamp_from_the_future_is_not_evidence`).
**Seen:** the release 2.1.24 pre-push lane on 2026-10-08 (`6d0b29f`, 2946 s, the
machine at under 6 GB free RAM with many installs running across sessions):

```
>       assert backend_liveness.self_report(record, PORT) is None
E       AssertionError: assert -29.95575737953186 is None
```

---

## 1. Cause

`backend_liveness.self_report` accepts a stamp whose age satisfies
`abs(age) <= HEARTBEAT_STALE_SECONDS` (30 s). The pin stamped
`time.time() + HEARTBEAT_STALE_SECONDS + 1.0`, which is 31 s in the future at the
moment of stamping. Every second spent between the stamp and the read moves a FUTURE
stamp's age back toward the window. The lane spent about 1.04 s there, the age read
-29.96, and the stamp was accepted.

The two past-stamp pins in the same class are safe from this. Delay makes a past
stamp older, so it moves AWAY from the bound they assert on the far side of. Only the
future direction runs the margin down.

## 2. Fix

The stamp is placed a whole bound past the edge,
`time.time() + 2 * HEARTBEAT_STALE_SECONDS` (60 s ahead). It is still rejected by
the symmetric bound the pin exists for. It now takes 30 s of scheduling delay, not
1 s, to cross back into the window. The product is unchanged.

## 3. Evidence

- `tests/test_backend_heartbeat.py`: 33 passed.
- **The pin still guards the symmetric bound** (mutation by runtime rebinding in a
  `pytest_runtest_call` hook of a `-p` plugin, no file modified): rebinding
  `backend_liveness.abs` to the identity, a one-sided bound that accepts any future
  stamp, gives 1 failed. Control: 1 passed.
- The margin arithmetic: the failure needed more than 1 s between stamp and read.
  The new margin needs more than 30 s, longer than any delay seen on this machine.
