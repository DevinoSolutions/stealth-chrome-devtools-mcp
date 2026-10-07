# F-944 — an in-flight call is told "the backend died" before anyone asked, and the failing CI node keeps no logs

**Severity:** Medium for diagnosis, low for users. The call fails either way and is
not retried, but the message sends a reader looking for a crash that may never have
happened, and on CI the evidence that would settle it was deleted.
**Files:** `embedded/proxy_selfheal.py` (`PendingCalls.fail_all`, `_one_generation`),
`tests/test_proxy_selfheal.py`, `tests/conftest.py` (`pytest_runtest_makereport`).
**Seen:** release-gate `integration (Windows/X64)` on main `e8bd512` (run
37567787102), `test_s1_cpu_saturation_does_not_condemn_a_live_backend`, ~26 s into
its CPU-saturation window:

```
{'jsonrpc': '2.0', 'id': 41, 'error': {'code': -32603, 'message': "the backend on
port 52343 died while 'tools/call' was in flight; the call was NOT retried against
its replacement — reissue it if it is safe to repeat"}}
```

---

## 1. Cause

`_one_generation` answers every in-flight call FIRST, unconditionally, and only then
asks `_confirm_bridge_verdict` whether the backend survived. That order is right:
the calls are unanswerable either way and the client is owed its errors before the
slower check. But `fail_all` always said "died". When the bridge leg merely ended,
the verdict can still conclude `connection_reset`, which means the backend is alive
and only this proxy's connection broke. The message called that a death anyway.

The S1 failure cannot be explained from what CI kept. Run 37567787102 printed only
the client's frame. The isolated workspace, with every proxy and backend log, is
deleted at module teardown, so nothing could say whether the watchdog condemned the
backend or only the bridge broke.

## 2. Fix

- `fail_all(client_write, port, cause)` words the error by what the cause knows.
  `watchdog` gives "the backend on port N stopped answering and was condemned". Any
  other cause gives "the connection to the backend on port N broke": that includes
  the bridge ending, whose verdict is not in yet, and `None`, a generation that
  never became ready. The code (-32603), the method name and "NOT retried" are
  unchanged.
- `tests/conftest.py` adds a `pytest_runtest_makereport` hookwrapper. A failed CALL
  on a node with an isolated gate workspace (`space` with `log_dir`/`home_dir`) gets
  that workspace's proxy warnings and backend logs as report sections, the part of
  a failure pytest actually prints. The next time S1, or any node on such a
  workspace, fails on CI, the job log carries the proxy's own account.

The S1 root cause itself (a watchdog condemnation under load or a broken bridge) is
NOT fixed here, because it cannot be known from the evidence. This makes the next
occurrence readable.

## 3. Evidence

- **Pin:** `TestPendingCalls::test_the_error_says_only_what_the_cause_knows`, over
  `watchdog`, `_BRIDGE_ENDED` and `None`. The old message contains "died" and lacks
  "connection to the backend", so it fails the two bridge cases by construction.
- **Mutation check** (runtime rebinding in a `pytest_runtest_call` hook, no file
  modified): `WATCHDOG_CAUSE` rebound so the branch never matches gives 1 failed /
  2 passed. The control gives 3 passed.
- **Report sections:** a throwaway node with a fake `space` (a `proxy-4242.log`
  WARNING line) and `assert False` printed `proxy warnings` and `backend logs`
  sections carrying the marker lines. The file was deleted afterwards.
