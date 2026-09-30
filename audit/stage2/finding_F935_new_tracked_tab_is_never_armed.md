# F-935 — a tab the manager starts driving after spawn was never armed: no capture, no extra headers, no timezone

**Severity:** Medium for users. When it happens, capture stops silently for the
rest of the instance's life, and the spawn's `extra_headers` and `timezone_id`
stop applying. `navigate` still answers success and nothing reports the loss.
It is also Medium for CI: the Windows integration cell went red on 3 of the 6
runs before 2.1.16 shipped.
**Files:** `embedded/browser_manager.py`, `embedded/network_interceptor.py`,
`embedded/tool_runtime.py` (the fix); `tools/check_file_budgets.py` (the
`browser_manager.py` row moves to its honest actual, 1447 → 1474, `+ F-935`);
`tests/test_e2e_replaced_tab_capture.py` and `tests/test_network_interceptor.py`
(the pins).

---

## 1. Symptom

Three different Windows integration tests failed the same way. Each spawned,
navigated, passed a readyState check, then found its first request "never
captured":

| Run / job | Test | Message |
|---|---|---|
| 36669145824 (main d0ff3da) | `test_e2e_data_tools.py::test_network_debugging_flow` | fetch to /api/json was not captured |
| same | `test_e2e_dynamic_sites.py::test_completed_text_base64_binary_chunked_and_http_errors` | /payload/text was never captured |
| 36730416285 attempt 2 (PR #174) | `test_network_debugging_flow` | fetch to /api/json was not captured |
| 36730416285 attempt 3 (PR #174) | `test_e2e_extra_headers.py::test_spawn_with_extra_headers_launches_and_sends_them` | the navigation request was never captured |

All four ran on the same image (20260922.246.2, Chrome 153.0.8010.53). The page
loaded every time; only the capture was empty.

## 2. Cause

`spawn_browser` arms ONE tab. `NetworkInterceptor.setup_interception` registers
the `RequestWillBeSent`/`ResponseReceived` handlers on that tab's CDP
connection. `BrowserManager._apply_post_launch` sends `extra_headers` and the
timezone override to the same tab. All three are per-target, and nothing
re-applied them when the tracked tab changed. Five paths change it:

- `get_navigation_tab`'s stale-tab recovery. It runs when `update_targets()`
  raises or the tracked target is missing, and replaces the tab without closing
  the old one.
- `NAVIGATION_RECYCLE_THRESHOLD` (25 navigations).
- `navigate`'s retry after a recoverable failure.
- `switch_to_tab`.
- `_repoint_after_close`.

After any of them, the tools drive a tab nobody is listening on.

**Corrected by F-936:** the gate reds went on with this fix in place, and all of
them missed on the spawn tab itself. This mechanism is real, but it is not what
those runs hit.

The CI logs carry no debug output, so they cannot say which path those runs
took. The first navigation after spawn goes through `get_navigation_tab`. A
transient failure of its `update_targets()` on a loaded runner fits all three
tests, and reproduces the symptom exactly (§3). This is inferred, not observed.

### Ruled out on the way

- **The unreferenced `asyncio.create_task` in the handlers.** The loop holds
  tasks weakly. But every suspension in `_on_request` is an `asyncio.Lock`
  acquire, and a task waiting on one is reachable through `lock._waiters`, so it
  cannot be collected mid-flight. The handlers now keep a strong reference
  anyway (`_handler_tasks`), which is hygiene rather than the fix.
- **Store eviction.** The 10,000-row cap is never approached in one test.
- **nodriver's `remove_handler`**, which drops every handler of a type. No
  caller removes network events.
- **nodriver dropping events it cannot parse.** A 40-iteration local probe
  instrumented `parse_json_event`. Chrome 153 fails to parse
  `Network.requestWillBeSentExtraInfo` (`KeyError: 'privateNetworkRequestPolicy'`)
  in 39 of 40 spawns. `Network.requestWillBeSent` itself always parsed, and
  capture missed 0/40. The ExtraInfo drift is real, but capture does not
  subscribe to that event.

## 3. Reproduction (RED before the fix)

`tests/test_e2e_replaced_tab_capture.py` spawns with `extra_headers` and
`timezone_id`, then navigates once and asserts that request was captured, which
proves the fixture works. It moves the tools onto a new tab through the
product's own path, navigates again, and asserts on the new request. There are
three cases:

- **recycle-threshold:** `NAVIGATION_RECYCLE_THRESHOLD` is set to 1.
- **failed-health-check:** `update_targets` raises once.
- **switch-tab:** `new_tab` followed by `switch_tab`.

Each case asserts the tracked target changed. Without that, a pass would prove
nothing. Against the unfixed tree, all three failed with
`a request on the new tracked tab was never captured`.

## 4. Fix

`BrowserManager._arm_tracked_tab(instance_id, tab)` is the one home for a
newly tracked tab. It applies the instance's stored options through
`_apply_tab_overrides`, the same helper `_apply_post_launch` now uses for the
spawn tab, so headers and timezone have one code path rather than two. Then it
runs every registered tab armer. It never
raises: the tab works and the navigation it serves is the caller's answer, so a
failure is logged instead. `_replace_main_tab` calls it, which covers recovery,
recycle and retry. So do `switch_to_tab` and `_repoint_after_close`.

`tool_runtime` builds the interceptor first and hands
`network_interceptor.arm_tab` to `BrowserManager(tab_armers=...)`. It is a
constructor argument, not a module-body call, because F-904's rule forbids the
latter. `arm_tab` repeats `setup_interception` with the
`block_resources` the instance was armed with. It does nothing for an instance
that was never armed or was cleared at close. `setup_interception` now records
which targets already carry the handlers, so arming a target twice (for example
switching back to the spawn tab) never doubles the handlers.

## 5. Verification

- With the fix, the E2E pin passes 3/3 and `test_e2e_extra_headers.py` passes 2/2.
- Mutation check by runtime rebinding, via a `-p` plugin in the test process:
  - Clearing the registered armers fails all three cases with "never captured".
  - Replacing `_arm_tracked_tab` with armers-only fails all three on the missing
    extra header.
- Four hermetic pins in `tests/test_network_interceptor.py`:
  - one registration per target;
  - a second tab of an armed instance is captured and gets `setBlockedURLs`;
  - handler tasks are held until done;
  - never-armed and closed instances are left alone.

## 6. Not done here

- `new_tab` without `switch_tab` still opens a tab that is not armed. Capture
  follows the tab the tools drive, not every tab in the browser.
- `dynamic_hook_system.setup_interception` (Fetch-domain hooks) and window
  sizing are still applied to the spawn tab only. They belong in
  `_arm_tracked_tab` once someone measures that re-enabling Fetch on a second
  target is safe.
- `dynamic_hook_system.py:297` and `python_binding.py:175` still create
  unreferenced handler tasks.
