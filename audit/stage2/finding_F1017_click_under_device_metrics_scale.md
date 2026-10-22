# F-1017 — `click_element` landed at `x / scale` under a device-metrics override, and said it hit (T-5907)

**Severity:** HIGH (a silent wrong click: the tool reported the intended element and the page received the press elsewhere).

## Finding

Reported from a real 360 px walk of unotes.net (uNotes commit 22fa8d9): with `Emulation.setDeviceMetricsOverride` at a 360 px mobile
viewport, `click_element` at (99, 291.4) registered at (79.2, 233.1), 0.8x, and the record said `hit_is_target: true`.

## Measurement (Chrome 155, headed, a clone, `data:` page with a capturing `mousedown`/`mousemove` listener)

`Input.dispatchMouseEvent` at CSS (100, 290); button box (60, 260, 80x60); `getContentQuads` and `getClientRects` always answered the CSS box.

| override | page received | target |
|---|---|---|
| none (1908x785) | (100, 290) | button |
| 360x740 mobile, dsf 3 | (100, 290) | button |
| 360x740 mobile, dsf 1 | (100, 290) | button |
| 360x1000 mobile, dsf 3 (window taller) | (100, 290) | button |
| 360x740 mobile, dsf 3, `scale: 0.8` | (125, 362.5) | body |
| 360x740 mobile, dsf 3, `scale: 1.25` | (80, 232) | body |

Cause: `Input.dispatchMouseEvent` takes widget coordinates and the override's `scale` maps widget to CSS (`css = x / scale`). Nothing reports
it: `Page.getLayoutMetrics`, `visualViewport.scale`, `devicePixelRatio`, `innerWidth`, `outerWidth` and `screen.width` are identical with and
without it. `deviceScaleFactor`, `mobile` and the window size are not involved.

### Which states do NOT reproduce it (addendum, same day)

The reporter says a RAW `Input.dispatchMouseEvent` lands 1:1 in their 360 px emulation and only `click_element` lands at 0.8x, and suspected Chrome's
mobile auto zoom-out. Measured on Chrome 155, headed clone, button at CSS (60, 260, 80x60), input at (100, 290), no `scale` in the override unless
stated; every line is the `clientX/Y` the page's capturing `mousedown` saw:

| state | `visualViewport.scale` | `innerWidth` | raw dispatch | old `click_element` |
|---|---|---|---|---|
| 360x740 mobile, dsf 3, `viewport` meta `device-width`, 450 px of content | 1 | 450 | (100, 290) | (n/a, same quads) |
| same, `viewport` meta `width=450` (Chrome fits the layout: the zoom-out suspect) | 0.8 | 450 | (100, 290) | (100, 290) |
| same, then `Emulation.setPageScaleFactor(1.25)` | 1.25 | 450 | (100, 290) | not run |
| same, `screenWidth/Height` 450x925 | 1.25 | 450 | (100, 290) | not run |
| any of the above plus `scale: 0.8` / `1.25` | unchanged | unchanged | (125, 362) / (80, 232) | same |

So auto zoom-out, the page scale factor and a differing screen size all leave raw input and layout coordinates in agreement, and
`getContentQuads` / `getClientRects` are in the same layout space; `click_element`'s mapping (the quad centre, no division by `devicePixelRatio` or
any window ratio: nodriver 0.47 `Element.mouse_click` passes `Position(quads[0]).center` straight to `Input.dispatchMouseEvent`) is correct in all of
them. `spawn_browser` applies no metrics override of its own (it sizes the window), so the override reaches the page only through
`execute_cdp_command`/`call_javascript_function`. The one input that makes the old path land at 0.8x is the override's `scale` parameter (1.25: the
reported `99 -> 79.2` is exactly `x / 1.25`), which DevTools' device mode sets when it fits the emulated screen into the window. The reporter's raw
dispatch landing 1:1 would then mean it was sent already divided, or in an override without the `scale`; their exact params had not arrived when this
was written. The real-Chrome node `test_click_under_mobile_auto_zoom_out_lands_one_to_one` pins the zoom-out state as a guard (green on the old and the
new code: the probe must find nothing to correct there).

### The uNotes sequence, call for call (exact params, 2026-10-21 23:10Z)

`spawn_browser()` with no arguments (1920x879 headed window), `Emulation.setFocusEmulationEnabled {enabled: true}`, then
`setDeviceMetricsOverride {width: 360, height: 780, deviceScaleFactor: 2, mobile: true}` (no `scale`, applied BEFORE navigating), a viewport meta of
`width=device-width, initial-scale=1, maximum-scale=5, user-scalable=yes, viewport-fit=cover`. The report: raw dispatch at the `getBoundingClientRect` point
lands 1:1, `click_element` at the same point lands at x/1.25 (99 -> 79.2). NOT reproduced here, on this machine (native ratio 2.5, so 2.5/2 = 1.25 was the
natural suspect):

* through the shared backend (older code), on a `data:` page, on `https://dev.unotes.net/login` (the email input) and on a fixed bottom sheet with a
  `translateY(0)` transform plus a button below the fold (which `scroll_into_view` moves): `click_element` and raw dispatch both landed exactly on the
  `getBoundingClientRect` centre every time (100,290), (180,419), (310,420), (100,390);
* through the real tool path in-process (`tests/test_e2e_click_under_emulation.py::exact_report_sequence`: spawn, the two CDP calls, navigate, raw
  press+release, `click_element`, every CDP command `click_element` sent recorded off `tab.send`), HEADED, on `origin/main` (`git archive` export) and on
  this branch: raw (100, 290), click (100, 290). The old path sent `dom.get_document, query_selector, scroll_into_view_if_needed, resolve_node,
  call_function_on, get_content_quads, dispatch_mouse_event x2, runtime.evaluate` (the last is nodriver's red dot): no `Emulation` command of any kind,
  so the tool does not re-issue or clobber the override. This branch sends the same plus the probe's `call_function_on` reads and one extra
  `dispatch_mouse_event` (the unscaled probe move).

So the state that was reported has not been found; the override `scale` remains the only input measured to move `click_element`'s coordinates, and the
branch corrects it and raises on any miss it can observe. If their mapping is something else, the new verification turns it from a silent miss into a
`ToolError` carrying the page's real `mousedown` coordinates, which is the data the next round needs. The node
`test_the_reported_sequence_clicks_where_a_raw_dispatch_lands` keeps the exact sequence as a guard (green on the old and the new code).


## Where the input is dispatched

One place. `dom_handler.click_element` was the only caller of `Element.mouse_click` (nodriver 0.47, `Tab.mouse_click` at the box centre in CSS
px); `type_text`, `paste_text` (`Input.insertText`, key events) and `select_option` (key events, `.apply`) send no pointer coordinates, and no
tool uses `Input.dispatchTouchEvent` or drag. `execute_cdp_command` is a raw escape hatch and passes coordinates through by design.

## Fix

`click_target.click` replaces `Element.mouse_click` as the coordinate click (convention 4: one home, the module that already owns "where was this
click aimed"). It dispatches at the centre of the first content quad (`DOM.getContentQuads`, top-level viewport: what `mouse_click` always used and the
only space that is right inside an iframe; the hit-test in `AIM_JS` stays frame-relative and the difference is carried as an offset). It observes the
page's own events with a capturing listener (`ARM_PROBE_JS` / `READ_PROBE_JS`; `composedPath` for the real target, plus the host of a CLOSED shadow root
that holds the element): a `mouseMoved` measures the factor `k = sent / seen`, the move at `aimed * k` is observed until it lands (at most 4 moves,
tolerance 2 px for Chrome's integer `clientX`), then `mousePressed` is sent, the `mousedown` is read BEFORE `mouseReleased`, and a press outside an
element the hit-test named raises `ToolError` (convention 2). Under `scale < 1` an input beyond the emulated widget is dropped, not clamped (measured: at
0.8 a move to x=330 never arrived, x=280 arrived at 350), so the first probe is tried at 1.0, 0.5 and 0.25 of the aimed point; nothing observed on any of
them with a target the hit-test names raises. A press the page did not report raises too, except for a disabled control (Chrome sends it no
`mousedown`; `reason: disabled` already says so). Once `mousePressed` is sent, no failure falls back to `Element.click` (review: a page that navigates
on the press made the read throw and the generic fallback sent a second, untrusted click). An element with no hit-testable point still goes to
`Element.mouse_click` (which draws nodriver's red `flash_point` dot as before; the dot is NOT drawn on the new path), which is also what produces the
synthetic fallback for a boxless one.

The probe's handle is `window[Symbol.for("stealth.click.probe")]`, a disposer: not a string-named global (visible to `Object.getOwnPropertySymbols`/`Reflect.ownKeys`), and the next arm calls it, so a
failure that skipped the read cannot leave a second listener logging `composedPath` for every later mouse event. Every dispatch and read is bounded by 5 s
(a `mousedown` handler that calls `alert()` holds `Input.dispatchMouseEvent` open).

`document.elementFromPoint` answers the shadow HOST for an element inside any shadow root, so `AIM_JS`'s `hit_is_target` now accepts the host (an
open-root element used to read `covered`).

## Tests

* `tests/test_e2e_click_under_emulation.py` (real Chrome, headless): 360 px mobile, dsf 3, scale 1.0 / 1.25 / 0.8, asserts the page's own
  `mousedown` target and coordinates. RED on the unfixed code at 1.25 and 0.8 (the page saw `body`, e.g. at (125, 362) for 0.8); green after. Review additions, each RED when its fix is
  reverted in a temp copy of `src` and green after: edge-of-viewport at 0.8, an iframe button at 1.0 / 0.8 / 1.25, a closed-shadow button at 1.0 / 0.8, and
  arming the probe twice leaves one listener (counts `composedPath` calls after a read).
* `tests/test_paste_click_verification.py`: hermetic, `FakeClickTarget(input_scale=..., frame_offset=..., navigates_on_press=..., swallows_press=...)`
  derives where input lands from the scale and the frame; scales 1.25 / 0.8 / 2.0, a press that drifts raises, an unscaled click sends one probe move, a
  page that navigates on the press gets exactly one press and no synthetic click, a probe beyond the viewport is retried nearer, an input that never
  reaches the page raises without pressing, a press the page never reported raises, an iframe click is aimed in top-level coordinates, a disabled
  control that reports no press is not a miss.

## Residual

* A page that stops `mousemove`/`mousedown` propagation at `window` in the capture phase before our listener runs hides the observation; the click then
  raises "could not be verified" (after the press, which is not repeated) or, for a move, "no probe move reached the page".
* Inside an iframe under a scale a probe can fall outside the frame (the frame's window sees nothing there); the click raises rather than missing.
* The `point` in the answer is still the frame-relative `getClientRects` centre; the point dispatched is the quad centre. They are equal in the top
  document.
* A page that navigates on `mousedown` cannot be verified (the document with the listener is gone); no raise, no second click.
* Nothing here changes `execute_cdp_command`.
