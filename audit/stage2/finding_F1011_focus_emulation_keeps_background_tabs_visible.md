# F-1011 — a minimized window or a background tab is "hidden" to its page: rAF stops and timers clamp to 1 Hz

**Severity:** Medium-High for headed use (a tab the caller is not looking at runs 10x slower on timers and not at all on frames).
Follows F-1010's residual "a second tab is still a background tab". Tracked as T-5166.
**Files:** `embedded/window_sizing.py` (`enable_focus_emulation`), `embedded/browser_manager.py` (`_apply_tab_overrides`),
`embedded/browser_reattach.py` (`_attach_one`), `NAVMAP.md`, `CHANGELOG.md`, `audit/stage2/finding_F1010_covered_window_renderer_backgrounded.md` (pointer).
Tests: `tests/test_focus_emulation.py`, `tests/test_browser_reattach.py`, `tests/test_extra_headers_cdp.py`.
**Branch:** `fix/F-1011-focus-emulation`. Number F-1011 was free (no branch or ref claimed it).

---

## 1. Measurement (lead, Windows, Chrome 155, headed, 5 s windows)

| | Setup | visibilityState | rAF | 100 ms timer ticks | longest gap |
|---|---|---|---|---|---|
| A | foreground, 1 tab | visible | 285 | 49 | 106 ms |
| B | window minimized | hidden | 0 | 5 | 1055 ms (1 Hz clamp) |
| C | B + `Emulation.setFocusEmulationEnabled(true)` | visible, hasFocus true | 5 (the active tab of a minimized window paints ~1 frame/s) | 47 | 110 ms |
| E | B + `Page.setWebLifecycleState('active')` | unchanged (not a fix) | | | |
| F | background tab (2 tabs, window normal) | hidden | 0 | 6 | 1121 ms |
| G | F + focus emulation | visible | 143 | 48 | 128 ms |
| H | G after a navigation, window minimized | visible | 286 | 48 | 111 ms (persists across navigation in the same target) |

Turning the call off again restored B, so the effect is the call. OS priority (psutil nice): focus emulation does NOT change the
renderer's process priority; a background tab's renderer stays IDLE.

## 2. Fix

`window_sizing.enable_focus_emulation(tab)` sends `uc.cdp.emulation.set_focus_emulation_enabled(True)`. It lives in `window_sizing` because `browser_manager.py` sits exactly at its grandfathered LOC cap (1438) and a helper there would have needed the cap raised; the file's net change is 0 lines. It is called from
`_apply_tab_overrides`, the one place that already arms a tab's per-target overrides (headers, timezone), FIRST, so a failing header
or timezone command cannot skip it. That home is already reached by:

- the spawn's first tab (`_apply_post_launch`);
- every tab tracked afterwards via `_arm_tracked_tab` (F-935): `new_tab` (through `tab_binding.adopt`), `switch_tab`, a replaced main tab
  (`_replace_main_tab`), a survivor after `close_tab`;
- the tab of a browser a later backend re-attaches to (F-888): `_attach_one` now calls `manager._apply_tab_overrides(tab, options)`
  (its options carry no headers or timezone, those are in `not_restored`, so this is focus emulation alone; CDP state died with the backend that set it).

Best effort: a refusal is logged (`debug_logger.log_warning`) and swallowed; a spawn or a tab never fails because of it. Applied headed and headless
alike: no concrete reason to differ, but the headless effect was not measured. No second code path was added.

## 3. Residuals, stated plainly

- **OS priority stays Idle** for a background tab's renderer. Only `--disable-renderer-backgrounding` changes that, and F-1010 deliberately keeps it
  stealth-blocked (`platform_utils._stealth_blocked_args`). On a starved machine a background tab is therefore still CPU-starved; its page just no
  longer believes it is hidden.
- **The active tab of a minimized window paints about one frame per second** (C: rAF 5): timers run, frames barely.
- **Page-opened popups (`window.open`) were not driven through the home** until a tool moved onto them (`switch_tab` armed them). CLOSED by F-1012
  (`audit/stage2/finding_F1012_popups_armed_on_creation.md`): a `Target.targetCreated` listener hands such a popup to the same `_arm_tracked_tab`.
- Mac and Linux were not measured; the call is cross-platform CDP and is sent everywhere.
- Survival of the emulation across target rediscovery (F-775a) or a `Connection` swap is not measured; `_arm_tracked_tab` re-applies it on every `switch_tab`, which bounds the exposure.

## 4. Verification

Hermetic pins (`tests/fakes.py` `FakeTab`):

- `test_focus_emulation.py::test_the_spawn_tab_gets_focus_emulation`, `::test_a_refused_focus_emulation_does_not_fail_the_spawn`,
  `::test_a_failing_header_command_cannot_skip_focus_emulation`, `::test_new_tab_gets_focus_emulation` (the real `new_tab` tool against the real manager),
  `::test_a_refused_focus_emulation_does_not_skip_the_rest_of_the_arming` (the refusal must not skip the tab's headers or the network armer);
- `test_browser_reattach.py::TestAnAdoptedInstanceTellsTheTruth::test_the_adopted_tab_gets_focus_emulation`.
- `test_extra_headers_cdp.py::test_spawn_sends_no_header_command_when_extra_headers_is_empty` used to assert an EMPTY frame list; it now asserts no
  `Network.setExtraHTTPHeaders` frame (the claim it was named for).

Mutation, by runtime rebinding in an out-of-tree copy (`src` + `tests` copied to a temp dir, `PYTHONPATH` pointing at it, module path asserted; NOT a source edit):

| Rebinding | Result |
|---|---|
| `enable_focus_emulation` is a no-op | 5 of 5 fail: spawn, spawn-refused, header-order, `new_tab`, arming-continues (re-attach pin also fails, 6 in all) |
| `enable_focus_emulation` sends without the try/except | 2 fail: spawn-refused and arming-continues (`_arm_tracked_tab` would swallow the refusal but also skip the headers and every armer) |
| focus emulation moved after the headers/timezone commands | 1 fails (header-order pin) |

Real Chrome, branch code, own `BrowserManager` (not the shared backend), throwaway profile under `%TEMP%`, headed, 3 s measurement in each tab after a
third tab made the first two background tabs; `D` is a control opened through `tab_open.open_tab` (not armed):

| Tab | visibilityState | hasFocus | rAF | 100 ms ticks |
|---|---|---|---|---|
| A: spawn tab, background | visible | true | 171 | 30 |
| B: `new_tab`, background | visible | true | 173 | 30 |
| D: control, not armed, background | hidden | false | 0 | 4 |

The browser was closed through `close_instance`; no process with the profile on its command line remained.
