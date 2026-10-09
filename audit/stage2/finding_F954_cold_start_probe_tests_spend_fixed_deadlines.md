# F-954 — the cold-start probe tests spend fixed deadlines that a starved lane exhausts

**Severity:** Low. Test-only. Two pre-push lanes on 2026-10-08 failed on a healthy
tree and cost a full re-run each (16:51 and 19:32 on a loaded machine).
**Files:** `tests/test_chrome_cold_start_probe.py` (`_LAUNCH_BUDGET_SECONDS`, new;
`_squatter`, new; the `squat` mode of the fake browser, removed). Read, unchanged:
`tools/chrome_cold_start_probe.py` (`probe_once`, `_json_version_answers`,
`_terminate`).
**Seen:**

| Lane | Test | Failure |
|---|---|---|
| pre-push for F-946 (16:51) | `test_the_launched_process_is_killed_before_the_record_is_returned` | `pid 122680 survived the probe` |
| pre-push for F-950 (19:32) | `test_a_port_the_browser_did_not_take_is_flagged` | `assert None == 20488` (`port_from_banner`) |
| pre-push for F-950 (19:32) | `test_a_squatter_on_the_reserved_port_does_not_score_as_a_launch` | `assert False is True` (`json_answered_before_banner`) |

All three pass alone (17 passed in 26 s).

---

## 1. Cause

`probe_once` is a loop with a wall-clock deadline: it reads the log for the banner and
asks the port for `/json/version` (each ask has a 0.25 s timeout) until the deadline.
The fake browser is a real Python subprocess, so its start-up is an interpreter start,
which is what a starved lane stretches. Two tests passed a deadline the lane then spent
before the child did anything.

**Squatter test (measured).** `deadline_seconds=3.0`, and the squatter was the CHILD:
it had to start, import, and bind inside 3 s. Starving the child's start by 4 s
(below) reproduces the failure with the same record shape as the lane log
(`json_answered_before_banner=False`, `deadline_ms=3000`, no banner).

**Port test (measured, by the same emulation).** `deadline_seconds=20.0`. The failing
record has `listening=False` and `ms_to_devtools_banner=None` yet its OUTPUT EXCERPT
contains the banner. The excerpt is read after `_terminate`, so the banner was
written after the 20 s deadline had already ended the loop. Starving the child's start
by 22 s reproduces it (the test then fails on the missing `bound-port` file, the same
cause one line later).

**Kill test (NOT reproduced; hypothesis).** That record has `listening=True` at
1.66 s, so the launch was fine and the deadline is not the cause. The failure is the
post-kill wait: the test polls `tasklist` for the pid for 10 s and then asserts it is
gone. Starving the start did not reproduce it. The leading hypothesis is Windows pid
REUSE: `probe_once` returns after `_terminate` and drops its `Popen`, which closes the
only handle holding the pid, so on a lane that is spawning processes continuously the
number can be handed to a new process and `tasklist` finds a live pid that is not the
probe's child. A second candidate is `taskkill /T` under load leaving a process behind,
but the pid under test is the venv trampoline's, not the real child's. No measurement
distinguishes them, so this test's wait is not changed.

## 2. Fix (test-only; the probe is unchanged)

- `_LAUNCH_BUDGET_SECONDS = probe.DEADLINE_SECONDS` (60 s) replaces the five
  `deadline_seconds=20.0` on tests that expect the launch to succeed or to die early.
  Success ends the loop at once, so the budget costs nothing on a healthy machine.
  The tests that run a deadline out on purpose (`silent`, 1.0 s; the squatter, 3.0 s;
  the BadStatusLine test, 5.0 s) keep their short deadlines.
- The squatter moves into the test process (`_squatter`, a `ThreadingHTTPServer` on
  the reserved port), up BEFORE the launch begins, and the launched browser runs in
  `silent` mode. The scenario is the real one (something else holds the port) and its
  readiness no longer depends on a child's start-up time. The fake's `squat` mode had
  no other user and is removed.

## 3. Evidence

Load emulation: a `-p` plugin that wraps `subprocess.Popen` so the fake browser sleeps
`F954_START_DELAY` seconds before running its script (no file modified).

| Tree | Delay | Result |
|---|---|---|
| before | 4 s | 1 failed (squatter), 16 passed |
| before | 22 s | `port_the_browser_did_not_take` failed; kill test passed |
| after | 0 | 17 passed (28.98 s) |
| after | 4 s | 17 passed (44.69 s) |
| after | 22 s | squatter, port, kill: 3 passed (61.86 s) |

`ruff check` and `ruff format --check`: clean.

## 4. Residual

`test_the_launched_process_is_killed_before_the_record_is_returned` can still flake
on the pid-reuse hypothesis. If it recurs, hold the `Popen` (wrap `probe._terminate`
to capture it and assert `process.poll() is not None`) so the pid cannot be reused
while it is asserted on.
