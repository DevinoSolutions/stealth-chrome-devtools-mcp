# F-1007 — the health probe's background target runs at IDLE priority, so a CPU-saturated host makes a HEALTHY browser look frozen and the heal kills it

**Severity:** High (a signed-in `default`/`fleet` browser is closed and relaunched while nothing is wrong with
it). From the T-1665 report (5.2/5.3); tracked as T-2541.
**Files:** `embedded/browser_health.py` (`BrowserHealth._check`, two new keyword seams (`browser_answers`, `canary`)), `embedded/scheduling_lag.py` (`IdleCanary`, `idle_canary`, `is_starved`, `ExcuseBudget`),
`embedded/shared_session.py` (new, moved out of `browser_health.py`), `embedded/tool_runtime.py`,
`embedded/tool_sections/browser_management.py` (re-pointed), `DESIGN.md`, `NAVMAP.md`, `CHANGELOG.md`.
Tests: `tests/test_browser_health.py`, `tests/test_scheduling_lag.py`, `tests/test_browser_exit_relaunch.py` (re-pointed import).
**Branch:** `fix/F1007-health-probe-starvation`.

---

## 1. The claim, checked

`probe_browser` opens `Target.createTarget {"url": "about:blank", "background": true}`, attaches and evaluates
`1+1`. Chrome lowers the priority class of a renderer process that has no visible widget (IDLE on Windows), so
under host CPU saturation that renderer gets almost no slices and the evaluate misses `PROBE_TIMEOUT_SECONDS`
(15 s). T-1665 reproduced it: a 60 s timeout with 48 CPU spinners running, the browser untouched.
`FAILURES_BEFORE_HEAL = 2` rounds later (4 minutes) `heal` closes the browser and relaunches the directory.
Reading the code on `origin/main` confirms that nothing between the failed probe and the count asks about
anything but the renderer: `_check` goes straight from `failure` to `self._failures[...] += 1`.

## 2. Why "the browser process answers" cannot be the whole test, and why the witness is a canary

The task's first proposal (probe at the browser level first and never condemn a browser whose process answers)
would also disable F-963: that freeze left `/json` and `Browser.close` answering while no tab ran a script, i.e.
the browser process answered and the renderers did not. A rule "process answers -> not frozen" would never heal
the one failure the module exists for. What separates the two is whether the HOST is starving the renderer. The
first version of this fix read machine-wide CPU with psutil; review rev246 rejected that as a second way to answer
"was this process scheduled fairly", which `scheduling_lag.py` already owns (F-856, self-measured, no CPU poll).

The second version asked the backend's own scheduling (`loop_lag`, one `asyncio` sleep after the failed probe).
rev246 then asked whether that signal fires in the case it exists to excuse: the backend runs at NORMAL priority and
the renderer at IDLE, so the backend can wake on time while the renderer is starved. **Measured** (2026-10-20, 32
CPUs, 64 normal-priority Python spinners for 6 s, a spawn-free setup: an IDLE-class counter process reporting
iterations per second, the real `scheduling_lag.loop_lag()` sampled repeatedly in a normal-priority asyncio loop,
an unloaded baseline first, 65 own PIDs killed and confirmed gone):

| | unloaded | 64 spinners |
|---|---|---|
| `loop_lag` factor | median 1.05, max 1.06 | median 1.05, p90 1.69, max 1.91 (0 of 15 samples at or above 2.0; the 1.6-1.9 samples were spinner start-up) |
| IDLE counter, iterations/s | about 24.3 million | 4.3% of that (about 23x slower) |

So `loop_lag` never reached `REPORT_FACTOR` while the IDLE process was starved to 4%: it is the wrong witness, and the
decision is to replace it. The witness is now a canary that runs at the priority that starves the renderer.

## 3. Fix

`BrowserHealth._probed` runs the probe inside `scheduling_lag.idle_canary()`: a child Python process lowers its own
priority to IDLE (`SetPriorityClass`, or `nice 19`), announces itself at normal priority, then wakes every
`CANARY_NAP_SECONDS` (0.1 s) and prints a byte. `IdleCanary.factor()`, read inside the window the probe just spent, is
`window / (wakes x nap)` clamped to `MAX_STRETCH`, so it measures DURING the probe, not after it, and reads the same
starvation the renderer suffers. It is `None` (no evidence) when the child never announced itself, exited, could not
be read, or the window is under `CANARY_MIN_WINDOW_SECONDS` (3 s). `BrowserHealth._starved(data, factor)` returns a
log phrase only when ALL of these hold, and `_check` asks it on every failed probe before the count moves:

* the probe's error was a `TimeoutError`: `_probed` reads the canary for a timeout only, so any other error reaches
  `_starved` with no factor. A `ProbeError` or a refused call is an answer the browser SENT, which no host load
  explains, so it counts as before;
* the canary's factor is at or above `REPORT_FACTOR` (`scheduling_lag.is_starved`, the one threshold);
* the browser process answers `Browser.getVersion` over a fresh connection inside 5 s (`fresh_browser_version`, the
  question F-993's capture asks).

Then `_excused` logs a WARNING ("starved, not frozen, not counted") and leaves the count where it was, but still
calls `_begin_incident`, so the F-993 capture of a browser that MIGHT be frozen is taken at its first failed probe
whatever the host is doing. The instance is re-checked for existence AFTER the extra awaits, before any verdict.

The excuse is bounded, in the `scheduling_lag` home (`ExcuseBudget`, ceiling `backend_eviction.STARVED_CEILING_SECONDS`,
300 s, the F-967 precedent for "slow, not wedged"): once the ceiling has passed, `_excused` logs an ERROR
(`HungBrowserError`, so it reaches Sentry) and returns False, and the failure counts. Two counted failures heal as
always, so a real freeze that coincides with a busy host is healed about two rounds after the ceiling, never skipped.
A passing probe, a heal, a relaunch under the same id (`relaunch_exited`) or the instance leaving the loop clears the
clock. A passing probe costs nothing extra to read (the canary is only read after a TIMEOUT), and the child is killed
and reaped in a `finally`. Every input is a keyword seam on `BrowserHealth`.

`browser_health.py` stood at exactly 1000 lines, a budget that only ratchets down, so the `default`/`fleet`
classifier (`shared_session_name`, `require_shared_session_named`, `is_shared_session`), `holder_of` and
`relaunch_options` moved verbatim into the new leaf `shared_session.py` (F-914/F-967's precedent). The callers were
re-pointed: `tool_runtime` exports `shared_session`, `close_instance`'s guard calls
`rt.shared_session.require_shared_session_named`.

**Known limit.** A canary that cannot start inside the window (a measured 2 s window with 32 spinners spawned
alongside it never saw the child announce itself) gives no reading, and no reading keeps the old verdict, the heal. The
canary starts BEFORE the 15 s probe, and the heal still needs two failed rounds, so this errs toward the pre-F-1007
behaviour, never toward skipping a heal.

## 4. Not done here

The task also asks for a verified-identity force-kill as the heal's final rung (when the close path leaves the
old process running, `heal` currently refuses to relaunch). That is a separate behaviour with its own data-loss
analysis (a kill of a profile holder), so it is split into its own task (T-4686) rather than riding a probe fix.

## 5. Tests

`TestAStarvedHostIsNotAFreeze`: a starved host (canary starved, process answering) is never healed over six failed
rounds; a dead process is healed; a freeze on an unstarved host is healed (F-963's case); the canary is read at each
failed probe and runs AROUND the probe (start, probe, stop), also when the probe raises; the process is asked over the
browser's own endpoint; a passing probe asks nothing extra; a `ProbeError` is not excused; the exact threshold
(`REPORT_FACTOR` excused, a hair under it healed); a canary with no reading, an unreadable canary and a late process
answer keep the old verdict; a browser closed while we asked gets no verdict and no incident.
`TestTheStarvationCeiling`: the ceiling is eviction's 300 s; a round exactly AT it is still excused; one millisecond
past it counts with an ERROR and the next failure heals; a healthy round restarts the clock.
`TestAStarvedProbeStillStartsTheCapture`: an excused round opens exactly one F-993 incident, which a pass ends.
`tests/test_scheduling_lag.py`: `TestIsStarved`, `TestIdleCanary` (kept-up reads 1.0, starved clamps, never-woke is
starved, the 2.0 boundary by one wake, minimum window, never announced / exited / failed spawn say nothing, stop is
idempotent and runs on an error, and a REAL IDLE child starts, is counted and is reaped) and `TestExcuseBudget` (the
ceiling is eviction's, at-the-ceiling is excused, one past is not, reset, per-subject runs).
`tests/test_browser_exit_relaunch.py`: a relaunch under the same id ends the starvation run.

**RED / mutations:** see the PR description; the method is runtime rebinding in a `git archive` export of the branch.
