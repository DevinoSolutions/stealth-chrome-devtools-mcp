# F-1012 — a popup the page opens stays a hidden tab until a tool switches to it

**Severity:** Medium (headed use: a popup an OAuth or payment flow opens runs timers at 1 Hz and paints no frames, and carries none of the spawn tab's overrides, until a caller `switch_tab`s onto it).
Residual of F-1011 (its "page-opened popups" bullet). Tracked as T-5166.
**Files:** `embedded/tab_open.py` (`arm_popups`, `_arm_popup`), `embedded/browser_manager.py` (`spawn_browser`, one call after the instance is stored), `embedded/browser_reattach.py` (`_attach_one`, one call),
`tools/check_file_budgets.py` (annotation only), `NAVMAP.md`, `CHANGELOG.md`, `audit/stage2/finding_F1011_focus_emulation_keeps_background_tabs_visible.md` (pointer).
Tests: `tests/test_popup_arming.py`, `tests/test_browser_reattach.py::TestAnAdoptedInstanceTellsTheTruth::test_a_popup_of_the_adopted_browser_is_armed_on_creation`.
**Branch:** `fix/F-1011b-popup-focus`. Number F-1012 was free (no branch, ref or finding claimed it).

---

## 1. Cause

F-1011 arms a tab from `BrowserManager._apply_tab_overrides`, reached by the spawn tab and by every tab the backend TRACKS (`_arm_tracked_tab`: `new_tab`, `switch_tab`,
a replaced main tab, a survivor after `close_tab`). A tab the PAGE opens (`window.open`, `target=_blank`) is created by Chrome, not by `tab_open.open_tab`, and nothing
tracked it: nodriver registers it in `browser.targets` from its own `Target.targetCreated` handler (`Browser._handle_target_update`) and that is all. It was armed only if a
caller later moved onto it with `switch_tab`. Until then it was hidden to its page, and it carried no extra headers, no timezone override and no network capture.

## 2. Fix (one arming path, not a second)

`tab_open.arm_popups(browser, arm, *args)` adds a listener on `browser.connection` for `cdp.target.TargetCreated`. For a target with `type_ == "page"` and an `opener_id` it
schedules `arm(*args, tab)`, with `arm` = `BrowserManager._arm_tracked_tab` and `args` = `(instance_id,)`: the SAME home `new_tab` and `switch_tab` reach, so a popup gets focus
emulation, headers, timezone and every armer, and nothing here knows what arming is. It is registered once per browser, from `spawn_browser` (a spawn, after the instance is stored) and `_attach_one`
(a re-attach: the dead backend's listener died with its connection), in both cases AFTER the instance is stored: `_arm_tracked_tab` finds no instance before that and
would skip the popup silently. Pinned for both sites (an ordering spy on `arm_popups`).

- **No new discovery.** The event is already delivered on that connection (nodriver turned discovery on in `Browser.start`); this is a second HANDLER on it, not a second
  `setDiscoverTargets`/`setAutoAttach` (the F-940 warning about re-asking for discovery does not apply). Handlers run in registration order, so nodriver's own handler has
  registered the `Tab` before ours looks for it (`tab_open.find`, which also builds one if only a bare `Connection` is there).
- **Popups only.** A tab `open_tab` creates has no `openerId` and its caller arms it, so nothing is armed twice. Service workers, iframes and shared workers are not `page` targets.
- **Idempotent with `switch_tab`.** `_arm_tracked_tab` on the same tab again repeats focus emulation, headers and timezone (all set-to-a-value) and network capture does not
  double its handlers (`_armed_targets` is keyed by target id); pinned.
- **Best effort.** The listener is a synchronous callback that only creates a task (a task, not an awaited handler, so a slow popup never holds up nodriver's listener loop);
  the task holds a strong reference (`_POPUP_TASKS`), is bounded by `POPUP_ARM_BUDGET_SECONDS` (5 s: a popup that closes mid-arm leaves its command unanswered for good, nodriver's
  listener ends on the closed socket and resolves nothing, so an unbounded await would pin the Tab, its websocket and the instance; the timeout is logged) and ends in `except Exception` + `log_warning` (`# noqa: BLE001  PERMANENT(...)`), because `_arm_tracked_tab` reads the
  instance OUTSIDE its own try, so a dead browser would otherwise surface as a never-retrieved task exception. A popup that closes before the task runs is `None` from `find` and skipped.
- **LOC.** `browser_manager.py` stays at its 1438 cap: the call (+1) is paid by tightening `_arm_tracked_tab`'s docstring (-1); the budget annotation says so. The listener lives in
  `tab_open.py` (180 lines of 1000).

## 3. Verification

Hermetic (`tests/fakes.py` `FakeBrowser` / `FakeTab`, a real `cdp.target.TargetCreated` / `TargetInfo`, delivered the way nodriver's listener delivers, `FakeTab._deliver`):

- `test_a_page_opened_popup_is_armed_without_a_switch_tab`, `::test_the_popup_gets_the_rest_of_the_arming_too` (headers and the armer, not focus alone),
  `::test_a_tab_the_backend_opened_itself_is_not_armed_twice`, `::test_only_a_page_target_is_a_popup` (3 target types),
  `::test_a_refused_call_breaks_neither_the_popup_nor_the_listener` (the refused popup keeps its headers; a second popup is still armed),
  `::test_a_popup_that_vanished_before_it_could_be_armed_is_not_an_error` (asserts nothing was armed and that not one CDP frame was sent on the browser connection or any tab), `::test_a_popup_that_never_answers_is_given_up_on_and_forgotten`
  (a `FakeTab` whose `send` never returns, under its own `asyncio.wait_for`: the task set empties and a warning is logged),
  `::test_a_popup_nodriver_registered_as_a_bare_connection_is_armed_as_a_tab` (a real `Connection` in `browser.tabs`: `find` swaps in a `Tab` through `_register`, listed once),
  `::test_the_listener_is_registered_once_per_browser` (exactly one `TargetCreated` handler after spawn),
  `::test_the_listener_is_registered_after_the_instance_is_stored`, `::test_a_failing_armer_is_logged_not_raised`, `::test_a_failing_instance_lookup_is_logged_not_raised`,
  `::test_arming_a_popup_again_on_switch_tab_adds_no_second_capture_handler` (real `NetworkInterceptor.arm_tab`);
- re-attach: `test_a_popup_of_the_adopted_browser_is_armed_on_creation`, `test_the_popup_listener_is_registered_once_and_after_the_store` (one `TargetCreated` handler on the adopted browser's connection).
- The spawn tests use the manager's REAL `get_instance` (only the liveness probe is faked) and so does the re-attach arming test; an earlier version replaced the lookup with a local function, which would have hidden an arming that runs before the instance is stored.
- The popup tests run the REAL `spawn_browser` (only launch, first page, hooks and liveness stubbed), so the registration site itself is what they exercise.
- Five existing stub browsers (`test_focus_emulation`, `test_extra_headers_cdp`, `test_google_rotation_guard`, `test_desktop_launch`, `test_spawn_budget`) gained the `connection` a real
  nodriver `Browser` has (a `FakeTab`, or `_OwnProcess.add_handler` in the spawn-budget double), because `spawn_browser` now registers a handler on it.

Mutation, by runtime rebinding in an out-of-tree copy (`src` + `tests` + `pyproject.toml` copied to a temp dir, `PYTHONPATH` pointing at it, module path asserted; not a source edit):

| Rebinding | Result |
|---|---|
| none (control) | 116 of 116 pass (popup file + the whole re-attach file) |
| `arm_popups` a no-op everywhere | 6 fail: 5 popup pins (not the negative ones) + the re-attach pin |
| no-op only in `browser_manager` (the spawn) | 5 fail, the re-attach pin passes |
| no-op only in `browser_reattach` | 1 fails (the re-attach pin) |
| opener filter dropped | 1 fails (own tab armed twice) |
| type filter dropped | 3 fail (the three non-page types) |
| `_arm_popup` without its try/except | 1 fails (failing instance lookup); needed that pin: the failing-armer pin alone did not catch it, `_arm_tracked_tab` swallows an armer error itself |
| arming reduced to focus emulation only | 3 fail (rest-of-arming, refused-keeps-headers, no-double-handlers) |
| `_arm_popup` without its `wait_for` | 1 fails (the never-answers pin, by its own 5 s test budget) |
| `find` invents a tab for a vanished popup | 1 fails (the vanished pin, via its armer) |
| listener registered BEFORE the instance store, in the copy's SOURCE (an edit, not a rebinding), spawn / re-attach | 1 fails each (the ordering pins) |
| `find` returns the bare `Connection` instead of registering a `Tab` | 1 fails (the bare-connection pin) |
| a second `add_handler` for `TargetCreated` | 7 fail (once-per-browser pins at spawn and re-attach, plus every pin that counts frames) |
| the vanished-popup path sends a frame | 1 fails (the frame-count assertion) |

Real Chrome (Windows, Chrome 155, headed), the branch code through its own `BrowserManager` (not the shared backend; fenced state/session roots via `tests/operator_fence.py`, a throwaway
nodriver profile). A local page runs `window.open()` with a user gesture; the first tab is then brought back to the front with `Target.activateTarget`, so the popup is a BACKGROUND tab; with no
`switch_tab`, 3 s are measured in the popup. `baseline` is `main` (3747ee7), reproduced by rebinding `arm_popups` to a no-op (main has no listener):

| | visibilityState | hasFocus | rAF | 100 ms ticks |
|---|---|---|---|---|
| baseline (main) | hidden | false | 1 | 4 |
| F-1012 | visible | true | 172 | 30 |

Both Chromes were closed through `close_instance` and their pids confirmed gone.

Two more popup shapes and the negative case, same setup, branch code (the listener is observed through a counter around `_arm_tracked_tab`):

| Opened by | `openerId` on `targetCreated` | armed by the listener | background popup |
|---|---|---|---|
| `window.open()` | set | 1 call | visible, rAF 172, 30 ticks |
| `<a target=_blank rel=noopener>` click | set (noopener does not drop it) | 1 call | visible, rAF 173, 30 ticks |
| `Target.createTarget` (`tab_open.open_tab`) | not set | 0 calls | (armed by its caller) |

(`openerId` is present on the created event; a later `Target.getTargets` read of the same popup reports it null, with `openerFrameId` set. The listener reads the event, which is what matters.)

## 4. Residuals, stated plainly

- **OS priority stays Idle** for a background popup's renderer (F-1011's residual, unchanged).
- **A popup that closes within a few milliseconds** is the case that used to HANG, not warn: its pending command is never answered (nodriver's listener breaks on `ConnectionClosed` without resolving
  pending transactions), so an unbounded await would have kept the task, the `Tab` and its websocket alive for the life of the instance. Arming is now bounded by `POPUP_ARM_BUDGET_SECONDS` (5 s); on
  expiry one warning is logged ("gave up") and the task ends. A popup already gone before the task runs is skipped with no frame sent. Neither is a failure.
- **Coverage as measured on real Chrome:** `window.open()` and `<a target=_blank rel=noopener>` both carry `openerId` and are armed once; a tab made by `open_tab` (`Target.createTarget`) carries none and
  is not armed by the listener. Other ways to open a page target (a popup from a `chrome://` page, an extension, `Target.createTarget` issued by a caller with its own `openerId`) were not measured.
- **A browser-level websocket that dropped and reconnected** no longer receives `targetCreated` (F-940 documents why), so its later popups are not armed on creation and fall back to
  the old `switch_tab` arming. Not measured here; no worse than before this change.
- **Popup traffic is now captured from creation** when network capture is armed for the instance (before, only after a `switch_tab`). That is what arming a tab means, but it is a visible change.
- **The cost per popup is unbounded by design.** Every popup the page opens now gets a CDP websocket (the first `send`), `Emulation`, header and timezone commands and, when capture is armed, `Network.enable`
  plus two handlers whose requests and responses are appended to the instance's `network_data` for as long as the popup lives. A page that opens many popups (ads, an OAuth flow that retries) multiplies
  that. Nothing caps it; a popup that closes frees its websocket, but the captured entries stay in `network_data`, which has the same lifetime and size policy as for any tab. Not measured under load.
- Popups of a re-attached browser are covered (pinned with a fake); not measured on a real re-attach.
- Mac and Linux were not measured; the listener is plain CDP.
