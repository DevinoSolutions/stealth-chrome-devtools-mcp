# F-1019 — a background tab that paints gets ~1 frame/s even though it reads `visible`

**Severity:** Medium (pages that animate or poll on `requestAnimationFrame`, and anything waiting for a paint, stall in a tab the caller is not looking at).
Residual of F-1011 / F-1012. Tracked as T-5903.
**Files:** `embedded/paint_hold.py` (new), `embedded/tab_binding.py` (`tab_for_caller` holds, `forget_tab` forgets), `tests/test_e2e_background_tab_frames.py` (new),
`tests/test_paint_hold.py` (new), `NAVMAP.md`, `CHANGELOG.md`, `audit/stage2/finding_F1011_...md` (pointer).
**Branch:** `fix/T-5903-background-tab-raf`. Number F-1019 was the first free above F-1017 (F-1014..F-1016 on `fix/T-5899-ci-integration-flakes`, F-1017 on
`fix/T-5907-click-scale-under-metrics-override`; F-1018 left to `feat/T-5597-backend-bearer-token`).

---

## 1. The report and the repro

On PyPI 2.1.39, headed: a tab opened with `new_tab` and left behind another tab reports `visible`, `hasFocus() == true`, 30 timer ticks per 3 s, but
`requestAnimationFrame` fires ~3 times per 3 s (foreground ~185). The lead's proof run is `prove2139/scripts/s1.py`. Run unchanged against an isolated backend on
this code it reproduced (`plain_bg_tab`: visible, hasFocus true, rAF 3 in 3 s, timers 30) with no popup involved; a variant with only the two `new_tab`s gave 4 and 3.

## 2. Cause

My first 14 setups used a page that only COUNTS `rAF` and measured 181 per 3 s everywhere. `/bg` in s1.py **writes the DOM every frame**. Bisected with the page's own loop,
same browser, same arming:

| bg tab (armed, focus emulation on, `visible`) | frames / 2 s |
|---|---|
| page that only counts `rAF` | ~120 (181 per 3 s) |
| page that writes the DOM each frame | **2-4** |
| same page, foreground | ~120 |

Focus emulation changes what the page believes (`visible`, focused), not the compositor of a hidden widget, which makes frames only for damage-free pages. A page with something to paint
is held to ~1 frame/s. Headless is identical (2), so it is not a window-manager effect; a covered window, a minimized one and OS focus on a popup window are irrelevant to it.

## 3. Levers (page writes the DOM every frame; bg tab; frames per 2 s; headed unless stated)

| Lever | Frames | Verdict |
|---|---|---|
| baseline (armed) | 2 | |
| `Page.setWebLifecycleState('active')` | 2 | no effect |
| re-toggling `Emulation.setFocusEmulationEnabled` | 2 | no effect |
| tab created with `enableBeginFrameControl=False` | 2 | no effect |
| `--disable-renderer-backgrounding` / `--disable-backgrounding-occluded-windows` / `--disable-background-timer-throttling` / all three | 2 | no effect; stay stripped |
| `Emulation.setDeviceMetricsOverride` | 18 | rejected: changes the viewport |
| `Page.bringToFront` (comparison) | ~113 | steals the visible tab |
| `Target.createTarget(newWindow=True)` (also `background=True`) | 121 | works, opens an OS window per tab; the option for a later `new_tab(new_window=True)` |
| **`Page.startScreencast`, huge `everyNthFrame`, never acked** | **92-121** | **adopted, for driven tabs** |

`startScreencast` marks the WebContents captured, so Chrome keeps its compositor producing; with an `everyNthFrame` that is never reached no frame is encoded and no
`Page.screencastFrame` event arrives (0 events). It survives a navigation (103) and `Page.captureScreenshot` (104); `stopScreencast` puts the tab back to 4; headless gives 120 against 2;
the active tab of a MINIMIZED window goes 3 -> 55-88. Not measured: Mac and Linux (CDP, sent everywhere).

## 4. Cost, and why the hold is per driven tab with a linger (not per tab)

First design: hold every armed tab in `enable_focus_emulation`. Measured (own headed browser, psutil `cpu_times` over the browser's own process tree only, pages that paint every frame):

| Setup | CPU | bg fps per tab |
|---|---|---|
| 10 bg tabs, no hold, 30 s | 2.9 cpu-s = 10% of a core | 1.5 |
| 10 bg tabs, all held, 30 s | 42.2 cpu-s = **138%** | 5.5 (saturated) |
| 3 bg tabs, no hold, 15 s | 0.8 cpu-s = 5% | 2.0 |
| 3 bg tabs, all held, 15 s | 21.1 cpu-s = **140%** | 21.6 |

~14x for a fleet of painting background tabs, on a CPU-starved machine: rejected. So the hold goes only to a tab a tool is acting on and ends `LINGER_SECONDS` (10) after the last call.
3 tabs: ~45% of a core per painting held tab (140% / 3 held, 5% unheld). Re-measured with that design (10 painting bg tabs, only tab 0 driven, 30 s): no hold 3.3 cpu-s = 11% of a core, 1.3 fps per tab; tab 0 held 41.7 cpu-s = **138%**, tab 0 at 54 fps and the
other nine at ~23 fps. So the hold is NOT confined to its tab. Measured with 4 painting tabs: none held 1/1/1/1 fps; tab 0 held 50/20/20/19; tab 0 released 3/3/3/3. While a tab is driven,
the painting tabs of its window run too, so the bound is TIME (the linger after the last call), not tab count. A window nobody drives stays throttled; an agent that drives a tab
keeps its window's painting tabs running for the length of its work plus 10 s (lead ruling: 10 s; 30 s was the first choice). `LINGER_SECONDS` is one constant; a shorter one trades fewer idle-painting
seconds for a frame-less gap between slow calls.

## 5. Fix

`paint_hold.hold(tab)`, called from `tab_binding.tab_for_caller` (renamed body `_resolve`): the one resolver every page tool goes through (`RESOLVERS` derives the page tools from it), so
no per-tool sprinkling. One `_Hold` and one asyncio task per target id; the entry is claimed before the first await, so concurrent calls send ONE `startScreencast`; a call on a held tab
only moves its deadline; the timer sends `stopScreencast` when the deadline passes. `tab_binding.forget_tab` (called by `close_tab`) cancels the task. A different `Tab` object for
the same target id (a rediscovered target, a new CDP session) replaces the entry and cancels the old task. Best effort: any refusal is logged and never reaches the tool. Focus emulation
(F-1011) is unchanged and still always on.

## 6. Tests, RED -> GREEN, mutation

`tests/test_e2e_background_tab_frames.py` (real Chrome, `integration`; headless always, headed where `display_context.can_show_windows()`), pages that paint every frame:
- a background `new_tab` tab driven through `execute_script` makes >= 30 frames in 2 s and reads `visible`;
- an un-driven background tab (its loop started straight over CDP) stays <= 15 frames in 2 s; a driven one (linger set to 2 s through the module constant) makes >= 30, falls back to <= 15
  after the linger, and is held again by the next call;
- a page-opened popup (F-1012) behind a later tab, then driven, makes >= 30.

| Run | Result |
|---|---|
| RED: `paint_hold.hold` a no-op (= pre-fix) | driven-tab and throttle-bound tests fail, 2 frames (4 of 6; the popup test also in its first run with the earlier design) |
| GREEN: as shipped | 6 passed |
| mutation: timer never releases | the 2 throttle-bound tests fail |
| mutation: every armed tab held, never released (the first design) | the 2 throttle-bound tests fail |
| mutation: focus emulation no-op | 6 of 6 fail (`hidden`) |

Hermetic `tests/test_paint_hold.py` (10): start with the never-reached frame; 8 concurrent calls on a slow tab send one start; the deadline moves and the stop follows the LAST call; a
released tab is re-held; `forget_tab` cancels the task and sends nothing; a new session for the same target replaces the entry and cancels the old task; a refused start leaves no entry and
raises nothing; a refused stop still ends the task; `tab_for_caller` holds what it returns. Mutations: claim-after-await (3 fail), deadline never moves (2), forget without cancel (2),
resolver unhooked (1). Method: a pytest plugin rebinding at collection time (`-p raf_mutate`), nothing in the tree edited.

## 7. Residuals

- While a tab is driven, the painting tabs in its window run at the cost above (measured 138% of a core for 10 painting tabs). That is what a visible window costs; it is bounded by time, not by tab count.
- IDLE OS priority of a background renderer is unchanged (F-1011).
- A tab in its own window (`new_tab(new_window=True)`) needs no hold but opens a window per tab; not built.
- A tool that reaches a tab without `tab_for_caller` (one that uses a stored tab directly) does not hold it; none was found: the resolvers are `_require_tab`, `tab_for_caller`, `navigate_callers_tab`.
