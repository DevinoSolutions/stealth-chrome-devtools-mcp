# F-1014 — `confirm_ours` refuses OUR OWN healthy Chrome when it is slow to answer (T-5899)

**Severity:** Medium (a spawn fails on a loaded host; no data loss). **Files:** `embedded/devtools_port.py` (`_answer`, `_running`, `IDENTITY_PATIENCE_SECONDS`, `IDENTITY_POLL_SECONDS`);
`tests/test_devtools_port.py`, `tests/test_devtools_port_identity.py`; `NAVMAP.md`, `CHANGELOG.md`. F-1014 was unclaimed (`git log --all`, every `audit/stage2` on every remote branch).

## 1. Evidence

CI run 39659325786 (attempt 1, PR `release/2.1.39`), integration (macOS/ARM64): `test_a_subframes_navigation_never_answers_for_the_main_frame` ERROR at setup, a plain
`spawn(headless=True)`: `F-1009: the browser on this spawn's DevTools port did not say which process it is within 10 s` (`devtools_port.py:145`, via `browser_connect.start`).
The log's own summary: `spawn_leak.reap: Failed spawn ... left browser pid 13852 running ...; killing it`, so the pid we launched WAS running and nothing else
held the port. Tests before and after on the same runner spawned fine, each in about 2 s; this spawn took 14 s (10 s of it the budget). One spawn in the lane, so a host-speed
tail, not a deterministic fault.

## 2. Cause

F-1009 made the identity proof a CDP round trip (`SystemInfo.getProcessInfo`) bounded by a flat 10 s. A healthy Chrome on a starved runner can answer later than that, and the
check treated "no answer yet" as "not ours". Why that one call is slow was not measured (it is answered by the browser process and, in Chromium, gathers per-process data; a slow
first answer on a runner with no GPU is the likely shape, but that is a guess, not a finding).

## 3. Fix

Waiting longer cannot attach a stranger: the proof is the PID in the answer, and a stranger is refused the moment it answers (or cannot: no `SystemInfo`). So the floor stays at
10 s and, past it, the same request keeps being waited on while the pid WE launched is running, up to `IDENTITY_PATIENCE_SECONDS` = 60 s (inside the 120 s launch phase, which stays
the outer bound). Details that matter:

* ONE request, never a retry: an abandoned CDP transaction can take the listener with it, so the request task is waited on with `asyncio.wait` (which cancels nothing) and cancelled
  only when the wait is over, whichever way (answer, silence, caller's cancellation) and always before `_drop` closes the socket.
* The liveness check is `psutil.Process(launched_pid)` for that one pid (running and not a zombie); no process scan. A launcher that is gone or unreadable gets the floor only
  (the wrapper-that-exits case therefore keeps the pre-fix 10 s).
* Fail closed is unchanged: silence past the patience, a browser with no `SystemInfo`, or a different pid is a `ToolError`, the socket is dropped and nothing is registered.
* A late answer logs a WARNING with the seconds it took, so a slow host shows up in the log and the next measurement has data.

## 4. Pins (hermetic) and mutations

RED against the pre-fix `confirm_ours` (constants added so the pins import): `test_a_slow_answer_from_our_own_running_launch_is_accepted` and
`test_a_stranger_answering_late_is_still_refused` fail (2 failed, 27 passed over the two files). Green after: 29 passed.
Mutations: `_running` always True fails `test_a_launcher_that_is_gone_gets_no_wait_past_the_floor`; `_running` always False fails the slow-answer and late-stranger pins; deleting the
clock bound fails `test_silence_past_the_patience_is_refused_and_the_request_ended`; deleting the request cancel fails the same pin.
Method: runtime rebinding through a pytest plugin outside the repo for the first two, a source edit with a verified restore (in this private worktree, not an exported tree) for the last two.

## 5. Residuals

* The reason the answer is slow is not established; a real macOS run with the new WARNING line is what will tell.
* A stranger that accepts the socket and never answers now holds a spawn up to 60 s (was 10 s) while our own launcher is running; the launch phase still bounds it at 120 s.
* A launcher that daemonizes keeps the 10 s floor.
