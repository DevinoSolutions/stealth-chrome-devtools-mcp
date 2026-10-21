# F-1010 — a covered Chrome window on Windows is backgrounded: `visibilityState` "hidden", renderer at Idle priority, pages 50-1000x slower

**Severity:** High for headed use (every `navigate` on a window another window covers can time out). Reported from
the Videofy lane on 2026-10-20; tracked as T-4665.
**Files:** `embedded/platform_utils.py` (`default_disabled_features`, `merge_browser_args`),
`embedded/login_persistence.py` (`disable_dbsc` opt-out branch), `NAVMAP.md`, `README.md`, `CHANGELOG.md`.
Tests: `tests/test_platform_utils.py`, `tests/test_login_persistence.py`, `tests/test_desktop_launch.py`.
**Branch:** `fix/F1010-renderer-backgrounding`. Number F-1010 was free (highest claimed on any branch, ref or
`origin/main` `audit/stage2`: F-1008).

---

## 1. The claim, checked against the code

The report says `spawn_browser` "strips `--disable-renderer-backgrounding` and
`--disable-backgrounding-occluded-windows`". True, but it is the CALLER-args path:
`platform_utils._stealth_blocked_args` lists both under "Puppeteer / Playwright signature flags" (blame: 853abd6,
"Strip detectable browser args to preserve stealth"), and `filter_stealth_args` removes them from `browser_args`.
Nothing in the product ever ADDED either switch, so a default spawn already launched without them. The defect is
therefore not a regression: a default Windows spawn has always let Chrome's native occlusion tracker background
the window, and a caller who tried the obvious escape hatch (`browser_args=["--disable-renderer-backgrounding"]`)
had it stripped, with a warning.

## 2. Cause

Chrome on Windows runs `CalculateNativeWinOcclusion` (on by default): it asks the OS which of its top-level
windows are covered by other windows and reports a covered one as hidden. The page then sees
`document.visibilityState == "hidden"`, Blink stops producing frames and throttles timers, and the browser lowers
the renderer process to Idle priority. On a machine that is doing anything else (the reporter's was thrashing) an
Idle renderer gets almost no CPU, so pages run 50-1000x slower, `navigate` hits its 30 s bound and scripts take
10-90 s. A headed window on a person's own desktop is exactly the window other windows cover.

F-1007 is the same Chrome behaviour met from the other side (its probe target is a `background: true` tab, so it
is hidden by construction); it fixed the HEALTH verdict, not the page speed.

## 3. The options, weighed on fingerprint

| Option | Reaches | Page-JS visible? | Verdict |
|---|---|---|---|
| A. Allow `--disable-renderer-backgrounding` and `--disable-backgrounding-occluded-windows` | occlusion (all OSes that have it) AND the Idle priority of any hidden tab | No: switches are argv, not exposed to page script; `chrome://version` and the process list show them | Rejected as the default: widest, and it is exactly the pair the filter exists to keep off the argv |
| B. `--disable-features=CalculateNativeWinOcclusion` | the cause on Windows: the window is never judged covered, so it stays `visible` and at Normal priority | No. The only page-observable result is `visibilityState` staying `"visible"` under a covering window, which is indistinguishable from an uncovered window | **Chosen** |

What "not page-visible" rests on: a page cannot read the browser's command line, and neither the occlusion tracker
nor the renderer's process priority is exposed to script. This is from the Chromium behaviour described above, NOT
a measurement made here (no real-Chrome test was run for this change; see the follow-up below). The residual
fingerprint of B is one feature name in `chrome://version` and the process command line, on Windows only.

B was chosen over A because it removes the reported slowdown at its cause, adds no Puppeteer switch to the argv,
and costs nothing on Mac and Linux (it is not named there: those platforms have no such tracker).

## 4. Fix

`platform_utils.default_disabled_features()` returns `("CalculateNativeWinOcclusion",)` when
`platform.system() == "Windows"` and `()` otherwise; `merge_browser_args` appends it as a
`--disable-features=` argument, next to the sandbox args and the masked User-Agent it already supplies.
Chrome honors ONE `--disable-features` switch (the last), and nodriver emits its own first, so the spawn path
(`browser_manager._resolve_launch_args`: `merge_browser_args` then `login_persistence.protect_logins`) folds every
`--disable-features=` into one through `login_persistence.merge_disable_features`: nodriver's own names, then the
caller's, then ours, then the DBSC ones, each named once, placed last. No second way to merge was added.

One change in `login_persistence.disable_dbsc` was required for that to hold: its opt-out
(`STEALTH_MCP_NO_DISABLE_DBSC`) used to return the args untouched, which was harmless while only a caller could
supply a `--disable-features` and is wrong now that every Windows spawn carries one: Chrome would keep the last
switch and nodriver's site-isolation pair would silently be dropped. The opt-out still adds no DBSC feature but now
folds a switch that is already in the args (and still returns args with none untouched).

The delegated headed launch (`desktop_launch`) receives the same `launch_args`, so the window on the user's desktop
carries the switch too; a test pins it through the real `_resolve_launch_args`.

The blocklist is unchanged: both Puppeteer switches stay stripped from caller args, and a comment says why.

## 5. Residual, stated plainly

- **A second tab is still a background tab.** `new_tab` opens a foreground tab (`tab_open.open_tab` passes no
  `background`) and the earlier one becomes
  hidden; a hidden tab's renderer is backgrounded whatever the occlusion tracker says, and driving it without
  `switch_tab` (which activates it) stays slow. The reporter's own verified workaround was "this flag plus a single
  tab" for the same reason. Fixing that needs `--disable-renderer-backgrounding` (option A) or activating the tab a
  tool is about to drive; neither is done here.
  Followed up in F-1011 (`finding_F1011_focus_emulation_keeps_background_tabs_visible.md`): CDP focus emulation keeps every driven tab visible; the Idle OS priority remains.
- **Mac and Linux are not covered.** Mac has its own occlusion state (the cross-platform
  `--disable-backgrounding-occluded-windows` is its switch); the report did not come from there and nothing was
  measured, so nothing is emitted.
- A caller cannot turn the default back off. Nobody has asked, and a knob would be a second way.

## 6. Verification

RED on `origin/main` (a `git archive` export, outside the tree, the new test files copied over unmodified product
code, module path confirmed to resolve into the export): 8 failed, 12 passed in the targeted selection. The 12
that pass on main are the negative pins (no backgrounding switches on any OS; nothing named on Linux/Darwin) and
`filter_stealth_args` cases; they hold both before and after.

GREEN after the fix, hermetic files: `tests/test_platform_utils.py`, `tests/test_login_persistence.py`,
`tests/test_desktop_launch.py`, `tests/test_stealth_args.py`, `tests/test_window_sizing.py`, `tests/test_stealth.py`.

Mutation, by runtime rebinding in a `git archive`-style export outside the tree (`pytest -p <plugin>` rebinding
`platform_utils` names; the method is labelled because it is NOT a source edit):

| Rebinding | Result |
|---|---|
| `default_disabled_features = lambda: ()` | 7 of the 12 selected pins fail |
| `merge_browser_args` also appends the two Puppeteer switches | 6 fail (the no-backgrounding pin on all three platforms and the three exact-argv pins) |
| `default_disabled_features` returns the Windows tuple on every OS | the two "other platforms" pins fail |

## 7. Not done here: a real-Chrome measurement (CI or follow-up)

On a Windows runner, headed, spawn a browser, cover its window with another top-level window, and read
`document.visibilityState` plus a 100 ms `setTimeout` drift / `requestAnimationFrame` count over 5 s: expected
`"visible"` and an undisturbed cadence with the fix, `"hidden"` and a 1 s timer clamp without it. Repeat with a
second tab to put a number on the residual in section 5. Not run locally: the dev machine was out of RAM for a
real browser.
