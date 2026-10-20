# F-1007 — the health probe's background target runs at IDLE priority, so a CPU-saturated host makes a HEALTHY browser look frozen and the heal kills it

**Severity:** High (a signed-in `default`/`fleet` browser is closed and relaunched while nothing is wrong with
it). From the T-1665 report (5.2/5.3); tracked as T-2541.
**Files:** `embedded/browser_health.py` (`BrowserHealth._check`, two new keyword seams), `embedded/starvation.py`
(new), `embedded/shared_session.py` (new, moved out of `browser_health.py`), `embedded/tool_runtime.py`,
`embedded/tool_sections/browser_management.py` (re-pointed), `DESIGN.md`, `NAVMAP.md`, `CHANGELOG.md`.
Tests: `tests/test_browser_health.py` (+10), `tests/test_browser_exit_relaunch.py` (re-pointed import).
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
the one failure the module exists for. What separates the two is the HOST: a starved browser is slow because the
machine is saturated, a frozen one on a calm machine is not.

## 3. Fix

`starvation.verdict(browser_answers, host_load)` returns a log phrase only when BOTH hold, and `_check` asks it on
every failed probe before the count moves:

* the browser process answers `Browser.getVersion` over a fresh connection inside 5 s (the existing
  `fresh_browser_version`, the same question F-993's capture asks);
* machine-wide `psutil.cpu_percent(interval=1.0)` is at least 90.

Then the round is a WARNING ("starved, not frozen; not counted") and the failure count is left where it was.
Anything else (a process that does not answer, a calm host, a load that cannot be read, an answer that times
out) is the old verdict. Nothing is read when the probe passed, so a healthy round costs nothing extra. Both
inputs are keyword seams on `BrowserHealth`, like every other external effect.

A saturated host with a truly frozen browser is not healed while the saturation lasts; the first failed round
after it calms counts, and a relaunch onto a saturated host would fail its own connect anyway (F-1004 measured
that).

`browser_health.py` stood at exactly 1000 lines, a budget that only ratchets down, so the `default`/`fleet`
classifier (`shared_session_name`, `require_shared_session_named`, `is_shared_session`) and `holder_of` moved
verbatim into the new leaf `shared_session.py` (F-914/F-967's precedent). `browser_health.py` is now 953 lines.
The callers were re-pointed: `tool_runtime` exports `shared_session`, `close_instance`'s guard calls
`rt.shared_session.require_shared_session_named`.

## 4. Not done here

The task also asks for a verified-identity force-kill as the heal's final rung (when the close path leaves the
old process running, `heal` currently refuses to relaunch). That is a separate behaviour with its own data-loss
analysis (a kill of a profile holder), so it is split into its own task rather than riding a probe fix.

## 5. Tests

`TestAStarvedHostIsNotAFreeze` (6): a saturated host with an answering process is never healed over six failed
rounds and logs six "starved" lines; a process that is dead too is healed; a freeze on a quiet host is healed
(F-963's case); the load is read at each failure, so a host that calms makes the next two failures heal; the
process is asked over the browser's own endpoint; a passing probe asks nothing extra.
`TestTheStarvationVerdict` (4): both conditions are needed, an unreadable load and a late answer keep the old
verdict, the real reading is a percentage.

**RED** (method: the real `starvation.verdict` rebound to `None` at runtime in the unmodified test run, no file
edited, bytecode writing off): 3 of the 6 `TestAStarvedHostIsNotAFreeze` pins fail (the saturated-host pin
because the browser is closed and relaunched, the load-is-read-each-time pin for the same reason, the endpoint pin
because the process is never asked); the other 3 are the inverse pins and pass, as they must on the old behaviour.
The opposite mutation (the verdict always "starved") fails 5 of 6, including both inverse pins (a dead process and a
quiet-host freeze are no longer healed). **GREEN** on the fix: `test_browser_health.py` +
`test_browser_exit_relaunch.py`, 145 passed.
