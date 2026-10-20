# F-1007 — the health probe's background target runs at IDLE priority, so a CPU-saturated host makes a HEALTHY browser look frozen and the heal kills it

**Severity:** High (a signed-in `default`/`fleet` browser is closed and relaunched while nothing is wrong with
it). From the T-1665 report (5.2/5.3); tracked as T-2541.
**Files:** `embedded/browser_health.py` (`BrowserHealth._check`, two new keyword seams (`browser_answers`, `lag`)), `embedded/scheduling_lag.py` (`loop_lag`, `is_starved`),
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

## 2. Why "the browser process answers" cannot be the whole test

The task's first proposal (probe at the browser level first and never condemn a browser whose process answers)
would also disable F-963: that freeze left `/json` and `Browser.close` answering while no tab ran a script, i.e.
the browser process answered and the renderers did not. A rule "process answers -> not frozen" would never heal
the one failure the module exists for. What separates the two is whether the HOST is starving us. The first version
of this fix read machine-wide CPU with psutil; review rev246 rejected that as a second way to answer "was this
process scheduled fairly", which `scheduling_lag.py` already owns (F-856, self-measured, no CPU poll), so the signal
is that module's now.

## 3. Fix

`BrowserHealth._starved(error, data)` returns a log phrase only when ALL of these hold, and `_check` asks it on
every failed probe before the count moves:

* the probe's error is a `TimeoutError`. A `ProbeError` or a refused call is an answer the browser SENT, which no
  host load explains, so it counts as before;
* the browser process answers `Browser.getVersion` over a fresh connection inside 5 s (`fresh_browser_version`, the
  question F-993's capture asks);
* `scheduling_lag.loop_lag()` (one 0.25 s `asyncio` sleep, `actual / requested`, clamped to `MAX_STRETCH`) reads
  `is_starved`, i.e. at or above `REPORT_FACTOR` (2.0), the one threshold that module reports on.

Then `_excused` logs a WARNING ("starved, not frozen, not counted") and leaves the count where it was, but still
calls `_begin_incident`, so the F-993 capture of a browser that MIGHT be frozen is taken at its first failed probe
whatever the host is doing. The instance is re-checked for existence AFTER the extra awaits, before any verdict.

The excuse is bounded. `_starved_since[instance_id]` records when a run of excuses began; once
`backend_eviction.STARVED_CEILING_SECONDS` (300 s, the F-967 precedent for "slow, not wedged") has passed, `_excused`
logs an ERROR (`HungBrowserError`, so it reaches Sentry) and returns False, and the failure counts. Two counted failures
heal as always, so a real freeze that coincides with a busy host is healed about two rounds after the ceiling, never
skipped. A passing probe, a heal, or the instance leaving the loop clears the clock. Nothing is read when the probe
passed, so a healthy round costs nothing extra. Every input is a keyword seam on `BrowserHealth`.

`browser_health.py` stood at exactly 1000 lines, a budget that only ratchets down, so the `default`/`fleet`
classifier (`shared_session_name`, `require_shared_session_named`, `is_shared_session`), `holder_of` and
`relaunch_options` moved verbatim into the new leaf `shared_session.py` (F-914/F-967's precedent). `browser_health.py`
is now 989 lines. The callers were re-pointed: `tool_runtime` exports `shared_session`, `close_instance`'s guard calls
`rt.shared_session.require_shared_session_named`.

## 4. Not done here

The task also asks for a verified-identity force-kill as the heal's final rung (when the close path leaves the
old process running, `heal` currently refuses to relaunch). That is a separate behaviour with its own data-loss
analysis (a kill of a profile holder), so it is split into its own task (T-4686) rather than riding a probe fix.

## 5. Tests

`TestAStarvedHostIsNotAFreeze` (11): a starved host with an answering process is never healed over six failed
rounds; a dead process is healed; a freeze on an unstarved host is healed (F-963's case); the lag is read at each
failure; the process is asked over the browser's own endpoint; a passing probe asks nothing extra; a `ProbeError` is
not excused; the exact threshold (`REPORT_FACTOR` excused, a hair under it healed); an unreadable lag and a late process
answer keep the old verdict; a browser closed while we asked gets no verdict and no incident.
`TestTheStarvationCeiling` (4): the ceiling is eviction's 300 s; a round exactly AT it is still excused; one millisecond
past it counts with an ERROR and the next failure heals; a healthy round restarts the clock.
`TestAStarvedProbeStillStartsTheCapture` (1): an excused round opens exactly one F-993 incident, which a pass ends.
`tests/test_scheduling_lag.py::TestLoopLag` (6): the clamped factor, the nap, and the `is_starved` boundary.

**RED / mutations:** see the PR description; the method is runtime rebinding in a `git archive` export of the branch.
