# F-804 — a headed spawn's window size is clamped by the desktop, and the spawn reports the request as though it had been applied

**Status: RESOLVED** on branch `fix/headed-viewport` (2.0.1 stabilization).
**Severity: MEDIUM** — a *truthfulness* defect first, a functional gap second.
The size a headed window ends up with is partly the OS's call, which is
legitimate; reporting a size the window never had is not. Every caller that
believed the echo — screenshot framing, coordinate math, responsive-breakpoint
work — was reasoning about a window 1.9x wider than the real one.

---

## The finding

`spawn_browser(viewport_width=1920, viewport_height=1080)` in **headed** mode
produced a window of about **1044x788** (inner viewport 1028x617), while the
spawn result echoed the request straight back:

```json
{"viewport": {"width": 1920, "height": 1080}, "spawn_diagnostics": {...}}
```

`spawn_diagnostics` carried no size information at all, so nothing anywhere in
the response contradicted the echo. In **headless** mode the same request *was*
honoured exactly (outer 1920x1080), which is what made the headed case read as
"the viewport argument is silently ignored headed".

## What was actually happening

Measured, not inferred — a probe spawned through `BrowserManager.spawn_browser`
and read back `Browser.getWindowForTarget` bounds plus
`window.outerWidth`/`innerWidth`:

| spawn | requested | CDP bounds | `outerWidth`x`outerHeight` | `innerWidth`x`innerHeight` | `screen` |
|---|---|---|---|---|---|
| headed | 900x600 | 900x600 | 900x600 | 884x429 | 1024x768 |
| headed | 1200x800 | 1044x788 | 1044x788 | 1028x617 | 1024x768 |
| headed | 1920x1080 | 1044x788 | 1044x788 | 1028x617 | 1024x768 |
| headless | 1200x800 | 1200x800 | 1200x800 | 1184x685 | 800x600 |

Three things fall out of that table:

1. **The size WAS being applied.** `tab.set_window_size(...)` (CDP
   `Browser.setWindowBounds`) ran on every spawn and worked: 900x600 landed
   exactly. The argument was never ignored.
2. **Headed Chrome clamps to the work area of the LAUNCHING process's desktop.**
   Anything larger came back at ~1044x788 — the same number for 1200x800 and for
   1920x1080, which is the signature of a clamp rather than of a dropped argument.
   *Corrected 2026-08-02:* this finding originally read that 1024x768 as an
   ordinary monitor limit, which implied a machine driving an RTX 3080 had a
   ~1024x768 screen. It was **Session 0's default desktop**, whose size is exactly
   1024x768 (measured via `GetDesktopWindow`'s rect, not inferred) — the backend
   had been cold-started from a non-interactive session, and that is also why the
   browser was invisible. See
   `finding_F808_headed_spawn_is_invisible_when_the_backend_was_cold_started_from_a_non_interactive_session.md`.
   *Provenance:* that measurement was taken ad hoc during the F-808 Task-10 prep
   session (2026-08-02) and lives only in that session's transcript. No repo
   artifact reproduces it — there is no test or script that measures the Session 0
   desktop, so treat the exact 1024x768 as a one-time observation rather than a
   standing, re-checkable claim.
   The remedy below (report requested/actual/clamped truthfully) is unchanged and
   still correct; only the stated cause was wrong.
3. **Headless does not clamp**, because there is no window manager to clamp
   against. That is the entire headed/headless asymmetry; nothing mode-specific
   existed in the product code.

Adding `--window-size=W,H` alone does **not** fix the size: the clamp applies to
the launch arg exactly as it applies to the CDP call (row 2 was measured both
with and without the arg, byte-identical results). `--force-device-scale-factor`
was investigated and **rejected**: DPI was not the cause (`devicePixelRatio` was
1 throughout), and the flag is on the stealth block list in
`platform_utils._stealth_blocked_args` ("DPI/scale mismatch detectable"), so
adding it would have traded a reporting bug for a fingerprint tell.

So the functional half of the finding is largely unfixable-by-design — the
window manager gets the last word — and the reporting half was the whole
defect: the tool claimed an outcome it had never checked.

## The fix

New module `src/stealth_chrome_devtools_mcp/embedded/window_sizing.py` — the one
home for "the requested window size", owning both transports of that one fact
and, crucially, the measurement:

* `append_size_arg(args, options)` adds `--window-size=W,H` (an explicit caller
  `--window-size` in `browser_args` still wins) so the window is *born* at the
  requested size rather than resized a beat later — this is what removes the
  visible resize on a headed launch and what covers a `setWindowBounds` that
  cannot run. It is stealth-neutral and survives `merge_browser_args`.
* `apply_and_measure(tab, options)` applies the CDP bounds exactly as before,
  then reads back `Browser.getWindowForTarget` and `window.innerWidth/Height`
  and returns `{requested, actual, inner_viewport, measured, clamped}`.
  Applying stays unguarded (a window that cannot be sized is still a failed
  spawn); only the *measurement* is guarded, degrading to `measured: false`
  rather than taking a working browser down for a diagnostic.

`browser_manager._apply_post_launch` now returns `(timezone_id, metrics)`; the
spawn orchestrator writes `spawn_diagnostics["window_size"] = metrics` and sets
`instance.viewport` to the **measured** size. The spawn result's `viewport` is
therefore no longer an echo of the argument — it is what the window is.

Post-fix, same machine:

```
headed   requested 1920x1080 -> viewport 1044x788   clamped: true
headless requested 1920x1080 -> viewport 1920x1080  clamped: false
headed   requested  900x600  -> viewport 900x600    clamped: false
```

`browser_manager.py` **shrank** 1531 -> 1526 LOC (the sizing block moved out); no
budget was raised.

## Tests

`tests/test_window_sizing.py`, two tiers, one file:

* unit (6 nodes, no Chrome) — the `--window-size` arg is built for headed *and*
  headless spawns, an explicit caller arg wins, and the arg survives the real
  `BrowserManager._resolve_launch_args` pipeline (i.e. the stealth filter does
  not strip it);
* integration (3 nodes, real Chrome, both modes) — a 900x600 request (fits any
  desktop Chrome runs on) is honoured exactly headed *and* headless; a
  deliberately oversized 9000x7000 headed request is clamped, and in **every**
  case the reported size equals `window.outerWidth`/`outerHeight` read from the
  page itself, with `clamped` telling the truth about which happened.

Both tiers were confirmed RED against the pre-fix tree (4 failed / 5 passed with
the two production files reverted) and GREEN after (9 passed).

## Residual

The `viewport_*` parameter names still say "viewport" while they set the
**window** size — inner is always smaller by the browser chrome (900x600 window
=> 884x429 viewport). The docstrings now say so explicitly and
`spawn_diagnostics["window_size"]["inner_viewport"]` reports the real CSS
viewport, but renaming the parameters is a wire-visible change and was left out
of a stabilization branch.
