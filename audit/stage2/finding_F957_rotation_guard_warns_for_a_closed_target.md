# F-957 — the rotation guard warns "not guarded" for a tab that simply closed

**Severity:** Low. A false WARNING, not a hole. It fails any test that forbids
warnings (`tests/test_e2e_fleet.py::test_a_fleet_of_six_browsers_answers_truthfully_about_every_page`,
CI run 37986386876 on PR #203, Linux integration; earlier a macOS flake on F-946's PR #197)
and trains readers to ignore the one warning that would mean a real unguarded target.
**Files:** `src/stealth_chrome_devtools_mcp/embedded/google_rotation_guard.py`
(`RotationGuard._handle`, the `Target.detachedFromTarget` branch),
`tests/test_google_rotation_guard.py` (`TestARefusedFetchEnable`).

The log line: `google_rotation_guard._settle: Chrome refused Fetch.enable on a target; it is not guarded`.

---

## 1. Measured (headless Chrome, scratch profile under `%TEMP%`, no sign-in, no real cookies)

The guard is a flat-session client: on `Target.attachedToTarget` it sends
`Fetch.enable`, `Target.setAutoAttach`, `Runtime.runIfWaitingForDebugger`.

- A session closed BEFORE our command reaches Chrome is torn down first. Chrome emits
  `Target.detachedFromTarget` for it, and only then answers each command we sent to the
  dead session with `{"code": -32001, "message": "Session with given id not found."}`.
  The reply carries NO `sessionId`.
- Wire order in every one of 17 refused replies (5 pages closed on attach, 0.5 s before
  we sent): the detach event came FIRST, the error SECOND. A command sent before the
  teardown is answered with a success (`closeTarget` then a pipelined `Fetch.enable`:
  `{"result": {}}`), so error-before-detach was not reproduced and has no mechanism: a
  command is only refused once the session is already gone, and the detach is emitted at
  teardown.
- No live target type refused `Fetch.enable`: 0 error replies across a run that attached
  42 pages, 2 `browser_ui`, 1 `background_page` and 1 `service_worker` (headless Chrome
  only; iframes ride the page type). Every refusal seen was `-32001` on a detached
  session.

## 2. Cause

`_handle`'s detach branch removed the session's ids from `_pending` (so the settle wait
is released) but not from `_fetch_enable_ids`, the set `_settle` consults to decide that
a refused reply was a `Fetch.enable`. The late `-32001` therefore matched, and warned.

## 3. Fix

The detach branch also drops that session's ids from `_fetch_enable_ids`. A refusal for
a session that is still attached is untouched and still warns (the real "not guarded"
case: a Google cookie-rotation request could slip through on that target). The log
stays shape-only; Chrome's text and code are not matched. No knob.

## 4. Evidence

- `tests/test_google_rotation_guard.py`: RED before the fix, 2 failed (detached-first
  does not warn; a detached target must not hide a live one) and 1 passed (the contrast:
  a still-attached target warns). After: 32 passed.
- Mutation (runtime rebinding of `RotationGuard._handle` to restore the old detach
  behaviour, no file modified): `TestARefusedFetchEnable` gives 2 failed, 1 passed.
- `tests/test_e2e_fleet.py`: 1 passed locally (57 s).
