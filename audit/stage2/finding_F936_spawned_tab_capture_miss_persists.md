# F-936 — capture misses the first requests on a freshly spawned tab, after F-935

**Severity:** Medium for CI: it made every attempt of the v2.1.17 publish gate
red. It is Medium for users too if the gate is telling the truth, because an
instance that misses capture stays silent. Cause **not yet known**. This change
is the instrument that should find it.
**Files:** `tests/e2e_helpers.py` (the spies and `capture_miss_report`), plus the
five assertion sites that missed on the gate:
- `tests/test_e2e_data_tools.py`
- `tests/test_e2e_extra_headers.py`
- `tests/test_e2e_network_capture_shape.py`
- `tests/test_e2e_replaced_tab_capture.py` (two sites)

---

## 1. Symptom

The v2.1.17 publish run 36776048519 contains F-935 and went red on all three
attempts:

| Attempt | Cell | Test | Message |
|---|---|---|---|
| 1 | Windows integration | `test_e2e_data_tools.py::test_network_debugging_flow` | fetch to /api/json was not captured |
| 2 | macOS integration | `test_e2e_extra_headers.py::test_spawn_with_extra_headers_launches_and_sends_them` | the navigation request was never captured |
| 3 | macOS integration | `test_e2e_network_capture_shape.py::test_capture_shape_resource_type_filter_and_no_internal_noise` | no network requests captured at all |
| 3 | macOS integration | `test_e2e_replaced_tab_capture.py::…[recycle-threshold]` | the spawn tab's first request (line 118) |
| 3 | Windows integration | `test_e2e_extra_headers.py::…` | the navigation request was never captured |

Every one misses on the spawn tab's first navigation or on the fetch right
after it. F-935's replacement-tab paths are never involved; the recycle case
failed before it recycled anything. In each case the page itself loaded:
`navigate` answered, and a `body` query and a readyState read went through the
same tab. So the tab's CDP connection answered commands while its network
capture stayed empty.

This corrects F-935. Its finding (§2) and its CHANGELOG entry say the Windows
gate reds were the unarmed-replacement-tab mechanism. That was inferred and
never observed, and the reds continued with the fix in place. F-935 is still a
real defect, proven RED against real Chrome. But it is not what these runs hit.

## 2. What was ruled out, statically

- **Something removing the network handlers.** No code under `src/` calls
  `remove_handler` for a Network event or sends `Network.disable`.
  `navigation_milestone` removes only `Page.lifecycleEvent`.
- **The interceptor lock bound to another event loop.** pytest-asyncio gives
  each test its own loop, and the interceptor is a process singleton. But no
  `async with self._lock` block in `network_interceptor.py` contains an await
  (AST census). The lock is therefore never contended and never binds to a loop.
- **A GC'd handler task.** `_on_request` never suspends while it holds a lock,
  and F-935 made the tasks strongly referenced anyway.

## 3. The instrument

`e2e_helpers.warmup_once` now installs two spies, idempotently. Every E2E module
already calls `warmup_once` from its autouse fixture.
- `nodriver.cdp.util.parse_json_event`, which is what `Connection._listener`
  calls on every event, records each `Network.*` event and whether it parsed.
- `network_interceptor._on_request` records each call per instance and whether
  the request was stored.

On a miss, `capture_miss_report(iid)` puts the following into the assertion
message:
- the Network events parsed in the last 30 s;
- `_on_request` calls and stores for that instance;
- the rows listed, the armed state and targets, and the filters;
- the tracked tab's handler table, its `enabled_domains`, its listener task
  state and its websocket close code;
- the browser's targets.

The spies only observe: each returns the original's result and re-raises its
exception unchanged. The report never raises.

Each outcome points at a layer:
- **No `requestWillBeSent` parsed:** the events never reached nodriver, so
  Network is not enabled on the session, or they went to another target.
- **Parsed but 0 `_on_request` calls:** dispatch is broken. Look for a missing
  handler in the handler table, or a dead listener.
- **Calls but nothing stored:** `_on_request` dropped the request. Look at the
  filters or at a swallowed exception.

## 4. Verification

- Unmutated, the capture E2E tests pass with the spies installed: 7 passed
  (`test_e2e_extra_headers`, `test_e2e_network_capture_shape`,
  `test_network_debugging_flow`, the three `test_e2e_replaced_tab_capture`
  cases).
- A forced miss comes from an out-of-tree `-p` plugin that drops
  `Connection.add_handler` for `RequestWillBeSent`, via `type.__setattr__`
  because nodriver's metaclass refuses a plain class assignment. The report read:
  `requestWillBeSent 4 ok, 0 failed`, `_on_request calls … 0`, and
  `handlers {'ResponseReceived': 1}`. That names the missing handler, which is
  the layer the mutation removed.

## 5. Next

Read the report from the next red gate. Then write the RED pin and the fix for
whichever layer it names, under this finding number.
