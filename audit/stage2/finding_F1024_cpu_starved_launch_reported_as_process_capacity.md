# F-1024 — a CPU-starved launch is reported as "out of process capacity" (T-5632)

Status: fixed. Class: wrong operator diagnosis on an error path (additive text only).

## Defect

F-811's `spawn_exhaustion.exhaustion_hint` decided from one number: the count of every
process whose name contains `"chrom"`, against a fixed 120. Two faults, one verdict:

1. **The count over-counts.** The substring matches things that are not a Chromium
   browser. Measured on the maintainer's box (`tasklist`, 2026-10-21): 243 `chrome.exe`
   and 29 `stealth-chrome-devtools-mcp.exe` (our own stdio proxy, one per Claude Code
   session). `chromedriver`, `ChromeSetup`, `chrome-devtools-mcp` and Electron helpers
   match the same way.
2. **The verdict ignores why the launch failed.** nodriver gives a launched Chrome a
   fixed connect deadline (~2.75 s of polling, F-834). A Chrome that DID start and is
   merely late under CPU load fails exactly like one that never could. On a box with
   100% CPU and a high Chrome count the message said the machine was out of process
   capacity and sent the operator to `kill-orphans --force` and `cleanup --apply`,
   which reap browsers that were not the problem while the real cause (CPU) went
   unnamed.

## Fix

One home, `embedded/spawn_exhaustion.py`:

* `_is_browser_name`: exact browser names (`chrome`, `chrome_proxy`,
  `chrome_crashpad_handler`, `chromium`, `chromium-browser`, `chrome-sandbox`, macOS
  `Google Chrome ...` / `Chromium Helper ...`), `.exe` stripped, case-folded. Lookalikes
  no longer count. `live_chromium_processes` (also read by `browser_health`'s forensics)
  uses it, so the count is the same number everywhere.
* `LaunchEvidence(chrome_started, cpu_percent)` and `observe()`: the two facts that tell a
  starved launch from a capacity one. `chrome_started` comes from
  `spawn_leak.started(attempt)` (the pid nodriver registered for that exact attempt);
  `cpu_percent` is `psutil.cpu_percent` over 0.2 s, sampled only when a Chrome started
  (that window blocks the loop, and a launch that never began cannot be "starved").
  `observe` never raises.
* `exhaustion_hint(pid_file, evidence)`: a Chrome that started on a machine at >= 90% CPU
  gets the CPU-starved paragraph (retry when idle or spawn one at a time; do not reap),
  whatever the process count. Otherwise the capacity paragraph, and only when the count of
  real browser processes is at the limit (120). Below both, `None`, byte-identical error.
* `BrowserManager.spawn_browser` samples the evidence FIRST in the `except Exception`
  block, before `_teardown_failed_spawn` ends the late Chrome (after it the CPU has
  dropped and the Chrome is gone, so the sample would read an idle machine for a launch
  that died of load). Still one composition site, still `or ""`, still a raised error
  (convention 2).

## Evidence

* Names measured, not assumed: see above. The new matcher is exact-name so an unmeasured
  spelling errs toward NOT counting, the safe direction for a hint that tells an operator
  to reap.
* `tests/test_spawn_exhaustion_hint.py` (RED on the old code, GREEN now): lookalike names
  are not counted; a starved launch names the CPU and not the cap (also with the count
  above the limit); a Chrome that never started is not called starved; an idle CPU or no
  reading below the cap says nothing; the cap is still named at the limit; `observe`
  samples the CPU and never raises; the call site reads the evidence before the teardown.
* Mutation: see the PR body.

## Not done, on purpose

No knob (an unknown `STEALTH_MCP_*` key crashes `get_settings()`). The 90% / 0.2 s are
module constants. The F-834 `spawn_contention` paragraph is a different question and keeps
its own home.
