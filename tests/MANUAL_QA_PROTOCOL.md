# Manual QA Protocol — the "Blind Push" Manifest

Every step below is something a human would do by hand to sign off a release
before shipping. Each step has a stable `MQ-<n>` id and exactly one explicit
evidence state. This is a design-time manifest: it records what HEAD proves and
what remains unresolved; it does not turn planned work into present coverage.

**If you'd check it by hand, it must be here. If it's here, release readiness
requires real acceptance evidence.**

### Evidence grammar

- `satisfied` — every `pytest:` reference is an exact, fully-qualified node ID
  collected at HEAD, runs without skip/xfail in the required gate, and asserts
  the manual outcome. A green characterization test never qualifies.
- `known-gap` — an exact current test pins incomplete or incorrect behavior.
  It is valid only when the cited node is marked `characterization` and both its
  docstring and this manifest contain the same exact `route:<F-id>` token (for
  example, `route:F-181`).
  Characterization is useful regression evidence, but it does **not** satisfy
  the success criterion. A workstream name, informal bug name, or unnumbered
  “finding” is not a route.
- `blocked` — a known product or infrastructure condition prevents the manual
  outcome. A blocked step fails release readiness; a fake, schema-only, or
  missing-instance unit test cannot clear it.
- `planned` — the acceptance evidence is designed but does not exist at HEAD.
  A future node is prefixed `planned-pytest:` and must never be collected or
  counted as current coverage. CI-only evidence is prefixed `planned-runtime:`.

An `Evidence` line contains the acceptance target only. Current pytest
acceptance uses `pytest: tests/<file>.py::<node>`; future acceptance uses
`planned-pytest:` or `planned-runtime:`. A shallow current node may be recorded
on a separate `Current support (non-acceptance)` line. That annotation is outside
the evidence ledger and can never change an MQ state or satisfy readiness.

Runtime evidence uses the exact `release-evidence/v1` ledger. A child record
lives at
`release-evidence/<release_sha>/<job_id>/<matrix_cell>.json` and contains:
`schema: release-evidence/v1`, `release_sha`,
`workflow {name,run_id,run_attempt,event}`,
`job {id,matrix_cell,terminal_outcome}`,
`runner {os,arch,image_os,image_version}`, `python_version`,
`chrome {path,executable_version,launched_major}`,
`pytest {junit_sha256,executed_node_ids,skipped,xfail,failed}`,
`artifacts [{name,path,kind,sha256}]`, and `mq_ids`. `aggregate.json` lists the
exact required Ubuntu, Windows, and macOS cells and hashes every child ledger. It
fails closed on a missing or duplicate cell, non-success terminal outcome,
skip/xfail/failure, stale release SHA, or hash mismatch. A job name, prose claim,
screenshot, or unverified artifact path is not runtime evidence.

### Parity and readiness rules

1. Current IDs are unique and contiguous from `MQ-1` through `MQ-113`.
2. Every `pytest:` node must match `pytest --collect-only` exactly. Bare module
   shorthands, stale names, wildcards, and class-less method names are invalid.
3. Every `planned-pytest:` node is non-evidence until it lands, collects, runs in
   the required gate, and this entry is deliberately changed to `satisfied`.
4. `known-gap`, `blocked`, and `planned` all remain unsatisfied for release
   readiness. In particular, a characterization pin cannot be relabeled as a
   success assertion merely because it is green.
5. The parity tripwire must report state counts and fail the release-readiness
   assertion while any step is not `satisfied`; it must also reject a current
   `pytest:` reference that is skipped, xfailed, absent, or only deselected from
   the gate responsible for that claim.
6. CI-only steps use the runtime-evidence requirements above. The tripwire must
   round-trip the `release-evidence/v1` child records and `aggregate.json`, not
   accept workflow prose.
7. A `known-gap` entry is rejected unless its exact node is collected with the
   `characterization` marker and the identical `route:<F-id>` appears in the node
   docstring, this manifest, and the W5 ledger. Current-support annotations are
   parsed separately and ignored when computing readiness.

Known bugs retain their tracking IDs below. At HEAD, zero MQ entries qualify as
`known-gap`: unrouted characterization nodes are support-only and their MQs stay
`planned`. Only a characterization marker plus the matching `route:<F-id>` in
the node docstring, this manifest, and the W5 ledger may establish a future
`known-gap`; it can never establish successful automation.

---

## Phase 1 — Installation & Launch

### MQ-1: Clean install of the exact candidate artifact
**Manual**: in a fresh environment, install the locally built candidate wheel by
its recorded path after independently verifying its SHA-256. Verify the installed
console script `stealth-chrome-devtools-mcp` is on `PATH`, prints help/version,
and runs the canonical journey. The path and hash must identify the exact files
that publishing will consume; rebuilding between smoke and publish is forbidden.
**Evidence**: planned — planned-runtime: W3 exact-candidate install-smoke ledger
for all required Ubuntu, Windows, and macOS cells, including the candidate
artifact path and SHA-256 in `artifacts`.

A real public-index `pip install stealth-chrome-devtools-mcp==<version>` check is
a separate post-publish observation. It cannot gate the publish that must happen
before that index artifact exists, and it cannot substitute for candidate-artifact
evidence.

### MQ-2: Stdio server starts and completes MCP handshake
**Manual**: configure an MCP host (Claude Desktop, Cursor, etc.) with the stdio
transport; watch for `initialize` → server responds with name + version +
`tools/list` returning all 94 tools.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_handshake_and_tools_list`.

### MQ-3: Tool schemas are well-formed
**Manual**: open `tools/list` response; every tool has a non-empty `description`
and a valid `inputSchema` (JSON Schema object, no dangling `$ref`).
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_all_tool_schemas_valid`.

### MQ-4: HTTP transport starts on loopback by default
**Manual**: `stealth-chrome-devtools-mcp --transport http`; verify bound to
`127.0.0.1`, not `0.0.0.0`.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_http_transport_binds_ipv4_loopback`.
**Current support (non-acceptance)**: pytest:
`tests/test_server_entrypoint.py::test_http_host_defaults_to_loopback` checks the
parsed default but never starts HTTP or inspects the bound socket.

---

## Phase 2 — Browser Spawn & Lifecycle

### MQ-5: Spawn browser (headless)
**Manual**: call `spawn_browser` with headless=true. Verify success response
containing an instance ID.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_browser_lifecycle_and_history`.

### MQ-6: Spawn browser (headed, if display available)
**Manual**: call `spawn_browser` without headless; a Chrome window appears.
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_spawn_browser_headed_when_display_available`.

### MQ-7: List instances shows the spawned browser
**Manual**: call `list_instances`; the instance ID from MQ-5 appears.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_browser_lifecycle_and_history`.

### MQ-8: Get instance state returns expected fields
**Manual**: call `get_instance_state`; verify browser info, url, tabs present.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_instance_state_fixture_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_browser_lifecycle_and_history` checks only a
dict containing the instance ID, not browser info, URL, and tabs.

### MQ-9: Close instance cleanly — no orphaned Chrome processes
**Manual**: call `close_instance`; `list_instances` no longer shows it; verify no
orphaned `chrome` processes remain (Task Manager / `ps aux`).
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_close_leaves_no_orphan_processes`.

### MQ-10: Spawn→close N times — no fd/process leak
**Manual**: repeat spawn→navigate→close 5 times; system stays healthy.
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_spawn_close_cycle_no_leak`.

### MQ-11: Kill Chrome mid-session — typed error, not hang
**Manual**: spawn, navigate, then kill the Chrome process externally; next tool
call returns a typed error within a bounded time (no infinite hang).
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_killed_browser_returns_typed_error`.

### MQ-12: Multi-instance — two browsers don't cross-talk
**Manual**: spawn two instances; navigate each to a different page; verify tabs
and page content are isolated.
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_multi_instance_isolation`.

---

## Phase 3 — Stealth Verification

### MQ-13: navigator.webdriver is false
**Manual**: spawn browser, open DevTools console, type `navigator.webdriver` →
must be `false` (not `true`, not `undefined`).
**Evidence**: planned — planned-pytest:
`tests/test_stealth.py::test_navigator_webdriver_is_false`.

### MQ-14: No CDP-leak globals
**Manual**: in console, check `window.cdc_*`, `$cdc_*`, `__driver_evaluate`,
`__webdriver_evaluate`, `__selenium_*`, `__fxdriver_*`, `__driver_unwrap`,
`calledSelenium`, `_Selenium_IDE_Recorder`, `_phantom`, `callPhantom`,
`phantom` → all must be absent/undefined.
**Evidence**: planned — planned-pytest:
`tests/test_stealth.py::test_no_cdp_leak_globals`.

### MQ-15: navigator.plugins is non-empty
**Manual**: `navigator.plugins.length > 0` in console.
**Evidence**: planned — planned-pytest:
`tests/test_stealth.py::test_navigator_plugins_populated`.

### MQ-16: navigator.languages is present and non-empty
**Manual**: `navigator.languages` → non-empty array, first element matches
`navigator.language`.
**Evidence**: planned — planned-pytest:
`tests/test_stealth.py::test_navigator_languages_present`.

### MQ-17: window.chrome object exists with correct shape
**Manual**: `window.chrome` → object, `window.chrome.runtime` exists.
**Evidence**: planned — planned-pytest:
`tests/test_stealth.py::test_window_chrome_shape`.

### MQ-18: User-Agent consistency (UA string vs userAgentData)
**Manual**: `navigator.userAgent` and `navigator.userAgentData.brands` reference
the same browser/version; no "HeadlessChrome" in the UA.
**Evidence**: planned — planned-pytest:
`tests/test_stealth.py::test_ua_consistency_no_headless_leak`.

### MQ-19: Function.prototype.toString integrity on patched builtins
**Manual**: `Function.prototype.toString.call(navigator.permissions.query)` →
must contain `"[native code]"`, not reveal patching.
**Evidence**: planned — planned-pytest:
`tests/test_stealth.py::test_native_code_integrity`.

### MQ-20: Automation-revealing Chrome flags are stripped
**Manual**: verify spawned Chrome was not launched with `--enable-automation`,
`--test-type`, `--remote-debugging-port=0`, or other automation tells.
**Evidence**: planned — planned-pytest:
`tests/test_stealth.py::test_automation_flags_absent_at_runtime`.
**Current support (non-acceptance)**: pytest:
`tests/test_stealth_args.py::TestFilterStealthArgs::test_strips_enable_automation`;
pytest: `tests/test_stealth_args.py::TestFilterStealthArgs::test_strips_test_type`;
pytest:
`tests/test_stealth_args.py::TestFilterStealthArgs::test_strips_remote_debugging_port`;
pytest:
`tests/test_stealth_args.py::TestFilterStealthArgs::test_strips_remote_debugging_pipe`.
These unit nodes exercise sanitization but do not inspect the launched process.

### MQ-21: Differential stealth — vanilla headless IS detected, stealth IS NOT
**Manual**: visit a bot-detection page with both a vanilla `google-chrome
--headless` and the stealth browser; the stealth instance passes checks that
vanilla fails.
**Evidence**: planned — planned-pytest:
`tests/test_stealth.py::test_differential_stealth`.

---

## Phase 4 — Navigation & Page Interaction

### MQ-22: Navigate to a URL
**Manual**: call `navigate` with the fixture app URL. Verify page loads.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_browser_lifecycle_and_history`.

### MQ-23: Go back / go forward
**Manual**: navigate to page A, then page B; `go_back` → page A; `go_forward`
→ page B.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_browser_lifecycle_and_history`.

### MQ-24: Reload page
**Manual**: call `reload_page`; page re-renders.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_browser_lifecycle_and_history`.

### MQ-25: Get page content
**Manual**: call `get_page_content`; verify HTML contains expected elements.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_page_content_fixture_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_upload_screenshot_and_content` checks only a
page sentinel in the serialized response, not the expected element set.

### MQ-26: Take screenshot — valid PNG
**Manual**: call `take_screenshot`; open the result; it's a recognizable image.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_screenshot_returns_png_bytes`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_upload_screenshot_and_content` accepts PNG
or JPEG magic bytes. The node is not a routed characterization and cannot prove
the PNG criterion.

### MQ-27: Query elements — finds expected elements by CSS selector
**Manual**: `query_elements` with `#btn-counter` → returns element info.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_query_elements_fixture_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_interaction_controls_and_log` asserts the
selector returns one item but not that its element information is correct.

### MQ-28: Click element — action fires, state changes
**Manual**: click `#btn-counter`; `#counter-value` text increments; action log
records `click:btn-counter`.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_interaction_controls_and_log`.

### MQ-29: Type text into an input
**Manual**: call `type_text` on `#text-input`; verify the input value changes
and action log records keystrokes.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_type_text_value_and_keyboard_log`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_text_input_scroll_and_wait` asserts the live
value but not the requested keyboard/action log.

### MQ-30: Paste text
**Manual**: call `paste_text` on `#textarea-input`; verify content pasted.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_text_input_scroll_and_wait`.

### MQ-31: Select option from dropdown
**Manual**: call `select_option` on a `<select>` with value `beta` → option
selected; action log records `change:select-single`.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_interaction_controls_and_log`.

### MQ-32: Wait for element — bounded wait succeeds on delayed reveal
**Manual**: click `#reveal-btn`; call `wait_for_element` for `#delayed-el` with
timeout ≥5s → element appears (200ms reveal vs 5s timeout → no flake).
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_text_input_scroll_and_wait`.

### MQ-33: Scroll page
**Manual**: call `scroll_page` down 500px; verify scroll position changed.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_scroll_page_exact_delta`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_text_input_scroll_and_wait` scrolls to the
bottom and checks only `scrollY > 0`, not the requested 500px operation.

### MQ-34: Upload single file
**Manual**: call `upload_file` on `#single-file`; verify the file is attached.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_upload_screenshot_and_content`.

### MQ-35: Upload multiple files
**Manual**: call `upload_file` on `#multi-file` with multiple paths.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_upload_multiple_files_exact_names`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_upload_screenshot_and_content` checks the
file count but not that both requested names are attached.

### MQ-36: Execute script — run JS and get return value
**Manual**: `execute_script("return document.title")` → returns the page title.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_browser_lifecycle_and_history`.

### MQ-37: Get element state — returns computed properties
**Manual**: call `get_element_state` on `#styled-card`; verify tag, text,
visibility, attributes returned.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_get_element_state_fixture_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_get_element_state_pins_current_shape` is a
characterization without an exact `route:<F-id>` token and cannot satisfy or
serve as a valid known-gap entry.

---

## Phase 5 — Negative Cases (Failure Paths)

### MQ-38: Click disabled control — typed failure, not silent True
**Manual**: try clicking a `<button disabled>`; expect a failure response or
ToolError, NOT a silent `True` claiming success.
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_click_disabled_control_failure_shape`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction_fidelity.py::test_form_semantics` characterizes the
current silent-`True` behavior but its docstring has no exact `route:<F-id>`
token.
`[KNOWN-BUG: E8-2]` Currently returns True; acceptance remains planned until a
routed fix and success assertion land.

### MQ-39: Type into readonly field — refused
**Manual**: try `type_text` on a `<input readonly>`; expect refusal or no-op,
not content modification.
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_type_readonly_field_refused`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction_fidelity.py::test_form_semantics` characterizes the
current readonly behavior but contains no exact routed finding ID.
`[KNOWN-BUG: E8-3]` Current behavior is not accepted; the success assertion is
planned.

### MQ-40: Select option second call in same document — works (not silent no-op)
**Manual**: call `select_option` twice on the same `<select>` with different
values; both take effect.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction_fidelity.py::test_select_option_second_call_succeeds`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction_fidelity.py::test_rich_input_types` characterizes the
current second-call failure but contains no exact routed finding ID.
`[KNOWN-BUG: E8-1]` Second call is currently a silent no-op; acceptance remains
planned.

### MQ-41: Range/color/date inputs — reachable by a typing tool
**Manual**: try to set a `<input type="range">`, `<input type="color">`,
`<input type="date">` via available tools; expect value to change.
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_specialty_inputs_reachable`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction_fidelity.py::test_rich_input_types` characterizes
unreachable controls but contains no exact routed finding ID.
`[KNOWN-BUG: E8-4]` Currently unreachable; acceptance remains planned.

### MQ-42: SPA root replacement — fresh public selector re-query
**Manual**: on the SPA fixture, query a generation-tagged selector, trigger a
History API route change and root replacement, then issue a fresh query and
action using the same selector. Assert only the new generation and its action
oracle. The public surface exposes no retained live-node or stale-handle
contract.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_dynamic_sites.py::test_spa_history_route_swap_and_requery`.
**Current support (non-acceptance)**: F-181's stale-document-node internals are
characterization support only; they do not establish a public stale-handle
acceptance contract.

### MQ-43: Bad CSS selector — typed error, not crash
**Manual**: `query_elements` with `#[invalid` → expect a clear error message.
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_bad_selector_typed_error`.

### MQ-44: Tool call with missing required parameter — validation error
**Manual**: call `navigate` without `url` → expect a schema validation error
from FastMCP, not a Python traceback.
**Evidence**: planned — planned-pytest:
`tests/test_mcp_protocol_surface.py::test_navigate_missing_url_validation_error`.
**Current support (non-acceptance)**: pytest:
`tests/test_mcp_protocol_surface.py::test_missing_required_param_is_validation_error`
omits every parameter and matches only “valid”; it does not assert that `url` is
named or that no traceback leaks.

### MQ-45: Tool call with wrong-type parameter — validation error
**Manual**: call `click_element` with `selector=123` (int not string) → schema
validation error.
**Evidence**: planned — planned-pytest:
`tests/test_mcp_protocol_surface.py::test_click_selector_wrong_type_is_validation_error`.
**Current support (non-acceptance)**: pytest:
`tests/test_mcp_protocol_surface.py::test_wrong_type_param_is_validation_error`
uses `navigate` with a list-valued `instance_id`, not the specified
`click_element(selector=123)` call.

### MQ-46: Nonexistent tool call — tool-not-found error
**Manual**: call a tool named `does_not_exist` → clear error, not crash.
**Evidence**: planned — planned-pytest:
`tests/test_mcp_protocol_surface.py::test_unknown_tool_error_shape`.
**Current support (non-acceptance)**: pytest:
`tests/test_mcp_protocol_surface.py::test_unknown_tool_raises` proves only that
some exception is raised, not a clear tool-not-found protocol error.

---

## Phase 6 — Tabs

### MQ-47: List tabs
**Manual**: after spawning and navigating, `list_tabs` returns ≥1 tab with the
navigated URL.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_list_tabs_includes_navigated_url`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_tabs_lifecycle` asserts IDs/counts but not
the navigated URL.

### MQ-48: New tab
**Manual**: `new_tab` → tab count increases by 1.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_tabs_lifecycle`.

### MQ-49: Switch tab
**Manual**: open two tabs; `switch_tab` to the second; `get_active_tab` confirms.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_switch_tab_changes_active_tab`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_tabs_lifecycle` calls `switch_tab` but does
not call `get_active_tab` afterward to confirm the switch.

### MQ-50: Close tab
**Manual**: `close_tab` on the second tab; `list_tabs` no longer shows it.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_tabs_lifecycle`.

### MQ-51: Get active tab
**Manual**: `get_active_tab` returns the currently focused tab info.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_interaction.py::test_get_active_tab_matches_focused_target`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_interaction.py::test_tabs_lifecycle` checks only that the returned
ID belongs to the tab set, not that it is the focused target.

---

## Phase 7 — Cookies

### MQ-52: Set cookie
**Manual**: `set_cookie` with name/value; verify it persists.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_cookies_lifecycle`.

### MQ-53: Get cookies — returns the exact value that was set
**Rewritten by W5** (plan_RELEASE §2.5 option (a) — the hard block cleared). The
former step was `blocked` on a `[KNOWN-BUG: get_cookies_hang]` that, measured,
belongs to the in-process `.fn` seam and not to the served path (F-777): over
real stdio against real Chrome, retrieval works and is now asserted.

**Manual**: `set_cookie` a value on a real `http://` origin, then `get_cookies`
and compare the returned value **byte for byte** with what you set. Presence, a
non-empty list, or a type check is not this step; the VALUE is. Then
`clear_cookies` and re-read to confirm it is gone.
**Evidence**: satisfied — pytest:
`tests/test_e2e_transport_cookies.py::test_real_transport_cookie_round_trip`.

Bounds this step does **not** exceed: the node runs in the `transport` lane, so
its evidence is **Linux/X64 and Windows/X64 only** — macOS/ARM64 is excluded
under F-773, and no macOS cookie claim exists. It is a dedicated node, never the
representative journey (§2.5 disqualifies that as per-tool evidence), and it is
the row backing `get_cookies` in `tools/release_tool_claims.json`, which the
`release-evidence` job re-verifies against every run's records.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_e2e_coverage_manifest` records the
covered/exempt partition only — `get_cookies` moved into `E2E_COVERED` when the
node above landed, and `E2E_EXEMPT` is now empty. Membership in that manifest is
an inventory fact; it never converted, and cannot convert, into a success claim.

### MQ-54: Clear cookies
**Manual**: `clear_cookies` → `get_cookies` (or `execute_script` reading
`document.cookie`) shows them gone.
**Evidence**: satisfied — pytest:
`tests/test_e2e_interaction.py::test_cookies_lifecycle`.

---

## Phase 8 — Network Debugging

### MQ-55: List network requests after a page load
**Manual**: navigate, then `list_network_requests`; expect the navigation
request in the list.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_navigation_request_appears_in_network_list`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_network_debugging_flow` finds a later
`/api/json` fetch, not the navigation request.

### MQ-56: Get request details
**Manual**: pick a request from the list; `get_request_details` returns URL,
method, headers.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_network_request_details_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_network_debugging_flow` checks only that
request details are a dict.

### MQ-57: Get response details + content
**Manual**: `get_response_details` returns status code; `get_response_content`
returns the body (with capture_bodies enabled).
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_network_response_details_and_content_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_network_debugging_flow` asserts exact body
content but only the type of response details, not the status code.

### MQ-58: Search network requests
**Manual**: `search_network_requests` with a URL substring → filters to match.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_network_search_returns_only_matching_requests`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_network_debugging_flow` proves a matching URL
appears but does not prove non-matching requests are excluded.

### MQ-59: Export / import network data
**Manual**: `export_network_data` writes a file; `import_network_data` reads
it back.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_network_export_import_round_trip`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_network_debugging_flow` checks file creation
and a truthy import result but not round-trip data fidelity.

### MQ-60: Set / get network capture filters
**Manual**: `set_network_capture_filters(capture_bodies=True)` then verify via
`get_network_capture_filters`.
**Evidence**: satisfied — pytest:
`tests/test_e2e_data_tools.py::test_network_debugging_flow`.

### MQ-61: Modify headers
**Manual**: `modify_headers` to add a custom header; trigger a request; verify
the header was sent (via echo endpoint).
**Evidence**: satisfied — pytest:
`tests/test_e2e_data_tools.py::test_network_debugging_flow`.

---

## Phase 9 — Element Extraction & Cloning

### MQ-62: Extract element styles
**Manual**: `extract_element_styles` on `#styled-card` → returns color, bg,
padding, border values matching the fixture CSS.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_element_styles_full_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_element_extraction_ground_truth` asserts
color and padding but not background and border ground truth.

### MQ-63: Extract element structure
**Manual**: `extract_element_structure` → returns tag hierarchy, attributes,
children.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_element_structure_full_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_element_extraction_ground_truth` asserts
element and depth sentinels but not the full tag/attribute/children contract.

### MQ-64: Extract element events
**Manual**: `extract_element_events` on an element with listeners → lists them.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_element_events_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_element_extraction_ground_truth` checks only
that the result is a dict, not that the fixture listeners are listed.

### MQ-65: Extract element animations
**Manual**: `extract_element_animations` on an animated element → returns
animation info.
**Evidence**: satisfied — pytest:
`tests/test_e2e_data_tools.py::test_element_extraction_ground_truth`.

### MQ-66: Extract element assets
**Manual**: `extract_element_assets` → returns images/backgrounds used.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_element_assets_image_and_background_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_element_extraction_ground_truth` asserts one
data-URI image but not the fixture background assets.

### MQ-67: Extract element styles via CDP
**Manual**: `extract_element_styles_cdp` → returns computed styles.
**Evidence**: satisfied — pytest:
`tests/test_e2e_data_tools.py::test_element_extraction_ground_truth`.

### MQ-68: Extract related files
**Manual**: `extract_related_files` → lists stylesheets, scripts referenced.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_related_files_stylesheet_and_script_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_element_extraction_ground_truth` asserts only
`styles.css`, not the referenced scripts.

### MQ-69: Clone element complete
**Manual**: `clone_element_complete` on `#styled-card` → returns full clone data.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_clone_element_complete_fixture_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_clone_element_complete_current_shape` is a
characterization without an exact routed finding ID.

### MQ-70: Extract complete element via CDP
**Manual**: `extract_complete_element_cdp` → full CDP-path extraction.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_complete_element_cdp_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_element_extraction_ground_truth` asserts only
that the response is a dict, not complete fixture ground truth.

---

## Phase 10 — Progressive Cloning

### MQ-71: Clone element progressive → stored element
**Manual**: `clone_element_progressive` → returns stored element ID.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_progressive_clone_returns_ground_truth_summary_and_id`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_progressive_cloning_walk` is a
characterization without an exact routed finding ID and pins empty summaries.

### MQ-72: Expand styles / events / children / css_rules / pseudo_elements / animations
**Manual**: call each `expand_*` on the stored ID; data grows.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_progressive_expansions_match_fixture_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_progressive_cloning_walk` pins empty
styles/events/CSS/pseudo-element/animation expansions without an exact route.

### MQ-73: List stored elements
**Manual**: `list_stored_elements` shows the stored ID.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_progressive_list_contains_stored_id`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_progressive_cloning_walk` contains a working
list sub-assertion inside an unrouted characterization node.

### MQ-74: Clear stored element / clear all elements
**Manual**: `clear_stored_element` removes one; `clear_all_elements` removes all.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_progressive_clear_removes_target_and_all`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_progressive_cloning_walk` contains working
clear calls inside an unrouted characterization node but does not read back the
empty state after each operation.

---

## Phase 11 — File Extraction

### MQ-75: Clone element to file
**Manual**: `clone_element_to_file` → file created on disk.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_clone_element_to_file_content_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_file_extraction_walk` proves a reported JSON
path exists but does not validate that it contains the requested clone.

### MQ-76: Extract complete element to file
**Manual**: file extraction variant → file on disk.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_complete_element_to_file_content_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_file_extraction_walk` proves a reported JSON
path exists but does not validate complete-element content.

### MQ-77: Extract styles/structure/events/animations/assets to file
**Manual**: each `*_to_file` variant works.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_aspect_to_file_contents_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_file_extraction_walk` proves paths exist but
does not validate each aspect file against fixture ground truth.

### MQ-78: List clone files
**Manual**: `list_clone_files` → shows the files written.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_list_clone_files_contains_created_paths`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_file_extraction_walk` checks only a minimum
count, not membership of each path created by the test.

### MQ-79: Cleanup clone files
**Manual**: `cleanup_clone_files` → files removed.
**Evidence**: satisfied — pytest:
`tests/test_e2e_data_tools.py::test_file_extraction_walk`.

---

## Phase 12 — CDP / JavaScript Functions

### MQ-80: List CDP commands
**Manual**: `list_cdp_commands` → non-empty list of domain.method strings.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_list_cdp_commands_are_domain_method_strings`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` proves only a
non-empty list; pytest:
`tests/test_e2e_functions_hooks.py::test_execute_cdp_command_rejects_domain_qualified_name`
is an unrouted characterization of the convention mismatch.

### MQ-81: Execute CDP command
**Manual**: `execute_cdp_command` with `Runtime.evaluate` → returns result.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_execute_cdp_runtime_evaluate_domain_qualified`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` succeeds only with
the bare command `evaluate`; pytest:
`tests/test_e2e_functions_hooks.py::test_execute_cdp_command_rejects_domain_qualified_name`
is an unrouted characterization of `Runtime.evaluate` failing.

### MQ-82: Get execution contexts
**Manual**: `get_execution_contexts` → ≥1 context.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_execution_contexts_non_empty`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` checks only that the
result is a list, not that it contains at least one context.

### MQ-83: Discover global functions / object methods
**Manual**: `discover_global_functions` → finds `calcTotal`;
`discover_object_methods` on `window.appAPI` → finds `getUser`, `setFlag`.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_discovery_finds_fixture_functions_and_methods`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` checks only list
types, not the named fixture functions/methods.

### MQ-84: Call JavaScript function
**Manual**: `call_javascript_function("calcTotal", args=[3, 4])` → returns 7.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_call_javascript_function_exact_result`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` calls different
arguments and searches a serialized blob for `5` rather than asserting the exact
result contract above.

### MQ-85: Inspect function signature
**Manual**: `inspect_function_signature("calcTotal")` → shows params `a, b`.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_function_signature_fixture_parameters`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` checks only a dict,
not parameters `a, b`.

### MQ-86: Inject and execute script
**Manual**: `inject_and_execute_script` with inline JS → returns result.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_inject_script_returns_fixture_result`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` checks only a dict,
not the injected script's result.

### MQ-87: Create persistent function
**Manual**: create a persistent function; call it; survives across navigations
within the same page context.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_persistent_function_survives_navigation`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` creates a function
but never calls it or navigates.

### MQ-88: Execute function sequence
**Manual**: `execute_function_sequence` with a chain of calls → final result.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_function_sequence_chains_multiple_calls`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` executes a sequence
containing only one call and therefore does not prove chaining.

### MQ-89: Create Python binding
**Manual**: `create_python_binding` → JS-callable function backed by Python logic.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_python_binding_is_callable_from_javascript`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` creates a binding
but never invokes it from JavaScript.

### MQ-90: Execute Python in browser (if py2js installed)
**Manual**: `execute_python_in_browser` → Python→JS transpiled and executed.
If `py2js` not installed, graceful error (not crash).
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_execute_python_success_or_typed_dependency_error`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` checks only a dict;
it does not distinguish successful execution from a clear optional-dependency
error.

### MQ-91: Get function executor info
**Manual**: `get_function_executor_info` → status of the function execution system.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_function_executor_info_contract`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_cdp_functions_walk` checks only that the
response is a dict, not the status fields.

---

## Phase 13 — Dynamic Hooks

### MQ-92: Create dynamic hook → trigger → details → remove
**Manual**: `create_dynamic_hook` matching a URL pattern; trigger a navigation
that matches; `get_dynamic_hook_details` shows the hook fired; `remove_dynamic_hook`.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_dynamic_hook_fires_on_matching_request`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_dynamic_hooks_lifecycle` creates,
lists, inspects, and removes a hook but never triggers a matching request or
asserts that it fired.

### MQ-93: Create simple dynamic hook (shorthand)
**Manual**: `create_simple_dynamic_hook` → verify it acts like a full hook.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_simple_dynamic_hook_matches_full_behavior`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_dynamic_hooks_lifecycle` creates the
shorthand form but does not prove equivalent behavior.

### MQ-94: List dynamic hooks
**Manual**: after creation, `list_dynamic_hooks` shows the hook.
**Evidence**: satisfied — pytest:
`tests/test_e2e_functions_hooks.py::test_dynamic_hooks_lifecycle`.

### MQ-95: Validate hook function
**Manual**: `validate_hook_function` with valid/invalid code → appropriate verdict.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_validate_hook_function_verdicts`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_hook_doc_tools` checks only that one
valid input returns a dict; it does not assert valid and invalid verdicts.

### MQ-96: Hook documentation tools — return non-empty content
**Manual**: each of `get_hook_documentation`, `get_hook_examples`,
`get_hook_requirements_documentation`, `get_hook_common_patterns` returns
non-empty useful text.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_hook_doc_tools_required_content`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_hook_doc_tools` checks non-empty dicts,
not required useful text or examples.

---

## Phase 14 — Debugging Tools

### MQ-97: Debug view lifecycle
**Manual**: `get_debug_view` → state dict; `clear_debug_view` → cleared;
`export_debug_logs` → log data.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_debug_view_clear_and_export_round_trip`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_debugging_tools` checks return types but
does not read back the cleared state or validate exported log content.

### MQ-98: Debug lock status
**Manual**: `get_debug_lock_status` → current lock state.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_debug_lock_status_contract`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_debugging_tools` checks only a dict,
not lock-state fields or values.

### MQ-99: Validate browser environment
**Manual**: `validate_browser_environment_tool` → environment check report.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_functions_hooks.py::test_browser_environment_report_contract`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_debugging_tools` checks only a dict,
not the environment report fields or verdict.

---

## Phase 15 — Complex DOM Structures

### MQ-100: Shadow DOM — characterization of current reach
**Manual**: navigate to a page with shadow DOM; try to query/extract inside it;
verify behavior matches documented limitations.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_dynamic_sites.py::test_shadow_dom_support_contract`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_hard_dom.py::test_shadow_dom_characterization` is marked
characterization but contains no exact routed finding ID.

### MQ-101: Existing iframe variants — direct metadata discovery
**Manual**: on the existing same-origin, `srcdoc`, and sandboxed iframe variants,
query the iframe elements themselves and verify direct metadata (tag, id, and
attributes) is discoverable. Nested same-origin or cross-origin interaction or
content targeting, recursive frame traversal, and targeting controls inside a
frame are explicitly unsupported.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_dynamic_sites.py::test_existing_iframe_variants_direct_metadata`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_hard_dom.py::test_iframe_characterization` covers the existing
one-level same-origin, `srcdoc`, and sandboxed variants, has no exact routed
finding ID, and does not assert this direct-metadata contract.

### MQ-102: Deep nesting (≥3 levels)
**Manual**: query/extract on deeply nested fixture elements → works.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_data_tools.py::test_deep_nesting_query_and_extraction_ground_truth`.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_data_tools.py::test_element_extraction_ground_truth` checks a
deep sentinel in extraction output but does not query the nested element.

---

## Phase 16 — Singleton / Process Management

### MQ-103: Singleton detects and reuses existing backend
**Manual**: start the server; start a second instance with the same version;
second instance reuses the backend (no double-bind).
**Evidence**: planned — planned-pytest:
`tests/test_e2e_singleton_process.py::test_second_client_reuses_live_backend`.
**Current support (non-acceptance)**: pytest:
`tests/test_singleton_version_aware.py::TestVersionAwareReuse::test_reuses_backend_with_matching_version`
uses a listening socket and mocked readiness rather than two live clients.

### MQ-104: Version mismatch triggers restart
**Manual**: start backend v1; connect with v2 → old backend replaced.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_singleton_process.py::test_version_mismatch_restarts_live_backend`.
**Current support (non-acceptance)**: pytest:
`tests/test_singleton_version_aware.py::TestVersionAwareReuse::test_ignores_backend_with_mismatched_version`
asserts selection returns `None`; it does not replace a live backend.

### MQ-105: Port fallback when preferred port is occupied
**Manual**: occupy port 19222; start the server → picks a different free port.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_singleton_process.py::test_live_server_falls_back_from_occupied_preferred_port`.
**Current support (non-acceptance)**: pytest:
`tests/test_singleton_port_fallback.py::TestSelectBackendPort::test_squatted_preferred_returns_a_different_free_port`
tests the selector helper but never starts the server.

### MQ-106: Stop and restart backend cleanly
**Manual**: stop the backend; restart → comes up on a valid port, state cleared.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_singleton_process.py::test_live_backend_stop_then_restart`.
**Current support (non-acceptance)**: pytest:
`tests/test_singleton_stop_restart.py::TestStopBackend::test_responsive_backend_is_stopped_and_state_cleared`
proves the stop half only and does not restart a live backend.

### MQ-107: Fast handshake — initialize responds without waiting for backend
**Manual**: send `initialize` immediately → response arrives before backend boot.
**Evidence**: satisfied — pytest:
`tests/test_singleton_fast_handshake.py::TestFastHandshake::test_initialize_answered_without_backend`.

### MQ-108: Stdio proxy exits when stdin closes
**Manual**: close the MCP host → the server process exits (no orphan).
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_stdio_disconnect_exits_without_orphan`.
**Current support (non-acceptance)**: pytest:
`tests/test_singleton_fast_handshake.py::TestEntrypointExitsOnDisconnect::test_stdio_entrypoint_exits_when_stdin_closes`
asserts the proxy exits but kills captured backend children during cleanup rather
than asserting no orphan remains.

---

## Phase 17 — Cross-Platform

### MQ-109: All of the above on Windows
**Manual**: repeat the full QA pass on Windows.
**Evidence**: planned — planned-runtime: one `release-evidence/v1`
`aggregate.json` for the release SHA requiring and hashing the Ubuntu, Windows,
and macOS child cells, with MQ-1..108 mapped to exact executed nodes; the Windows
child must record successful unit/integration/transport execution, runner and
Chrome identity, and zero skipped/xfail/failed required nodes.

### MQ-110: All of the above on macOS
**Manual**: repeat the full QA pass on macOS.
**Evidence**: planned — planned-runtime: the same `release-evidence/v1`
`aggregate.json` for the release SHA requiring and hashing the Ubuntu, Windows,
and macOS child cells, with MQ-1..108 mapped to exact executed nodes; the macOS
child must record successful unit/integration/transport execution, runner and
Chrome identity, and zero skipped/xfail/failed required nodes.

---

## Phase 18 — Tool Coverage Completeness

### MQ-111: Every advertised tool has a visible state in the release contract
**Rewritten by W5** (plan_RELEASE §2.5 — "these states are never inferred from
F-108 set equality"). "Every tool has ≥1 E2E test" was an *inventory*
requirement: set equality over a coverage manifest, which proves membership and
not behaviour. W5 replaces it with the requirement a reader can act on — every
served tool carries a **state**, and every claimed success is backed by ledger
evidence that the run produced.

**Manual**: compare `tools/list` output with `RELEASE_CONTRACT.md` §5. Every
served tool appears exactly once with a state; every `release-qualified-success`
row names a passing node, a transport, a site shape and its OS cells; every other
row carries a tracking id and a user impact. No tool is silently absent, and no
tool is qualified by exemption, by counting, or by membership in a manifest.
**Evidence**: satisfied — pytest:
`tests/test_release_contract.py::test_the_tool_table_covers_every_served_tool_exactly_once`.
**Current support (non-acceptance)**: pytest:
`tests/test_release_contract.py::test_every_served_unqualified_row_carries_a_tracking_id_and_impact`
proves the tracking-id/impact half, and
`tests/test_e2e_functions_hooks.py::test_e2e_coverage_manifest` still proves the
covered/exempt partition — an evidence *tier*, never a qualification. The number
of release-qualified tools is derived from the claim ledger and re-verified
against each run's records; every tool without its own per-tool transport
assertion is bounded by F-776, not by this step.

### MQ-112: Every advertised tool has ≥1 transport-tier test OR explicit exemption
**Manual**: same check through the real transport layer.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_transport_coverage_manifest`. The future test
must validate the live served tool set and reasoned exemptions; a hand-written
set without behavioral evidence does not satisfy MQ-111.

### MQ-113: Every MQ step in this file maps to a live test
**Manual**: this IS the manual step. Automated by the parity tripwire.
**Evidence**: planned — planned-pytest:
`tests/test_manual_qa_parity.py::test_every_mq_step_has_live_test`. It must
enforce the grammar and readiness rules above, including `release-evidence/v1`;
reject a `known-gap` without the characterization marker and identical
`route:<F-id>` in node docstring/manifest/W5 ledger; and prove every
`Current support (non-acceptance)` annotation is excluded from readiness. Mere
presence of an `MQ-` heading is insufficient.

### MQ-130: The documented examples run, and the README's claims match the code
**Manual**: copy the marked command blocks out of `README.md` / `RUNBOOK.md` into
a throwaway directory with `STEALTH_MCP_BROWSER_SESSION_ROOT` pointed inside it,
run each one, and confirm every one exits 0 without touching the real
browser-session root. Then confirm the README's `pip install` line names the
published distribution and version, that the ops verbs are shown under
`stealth-chrome-devtools` (not `stealth-chrome-devtools-mcp`), that every tool
its table advertises is served by the live registry, and that its served and
release-qualified counts are the ones in `RELEASE_CONTRACT.md`.
**Evidence**: satisfied — pytest:
`tests/test_doc_examples.py::test_documented_example_runs`,
`tests/test_doc_examples.py::TestInstallClaims`,
`tests/test_doc_examples.py::TestToolClaims`. The runnable half is a
parametrized node per marked fence whose id carries the source file, the fence
ordinal, and the exact command; the claims half derives both counts from W5's
`tools/gen_release_contract.py::tool_rows` ledger API — no second source — and
`TestToolClaims::test_the_overclaim_check_catches_a_planted_claim` is the
control proving a served-unqualified tool cannot be advertised as qualified.
Fence-execution sensitivity is proved by
`tests/test_doc_examples.py::test_the_runner_would_fail_a_broken_example`.

> **Contiguity note.** This step lands out of order: W11 is stacked directly on
> W5, so `MQ-114..129` (W7, W9, W10) do not exist at this commit and the
> `MQ-1..113` + `MQ-130` sequence has a hole. The contiguity rule below is
> satisfied once those workstreams land; until then the parity tripwire must
> treat `MQ-130` as present-but-non-contiguous rather than as a missing step.

---

## W12 — security and trust-boundary verification (MQ-131..137)

These steps test the boundary that exists. **They do not pretend an exec-capable
local automation server is a sandbox**, and an untrusted MCP client is out of
scope for this release. Read every `Evidence` line literally: five of the seven
steps are `planned`, because the halves that need real Chrome are not verified.
Their `Current support (non-acceptance)` lines record what IS proved without
letting it satisfy the step.

These landed while `MQ-114..130` (W7, W9, W10, W11) are still reserved, so the
contiguity check stays `MQ-1..113` plus this block until those workstreams land.

### MQ-131: Transport, bind exposure, and the threat contract
**Manual**: read `RELEASE_CONTRACT.md` §6. Confirm the table names all nine
dimensions for both stdio and HTTP, that each row states whether a test or only
a description stands behind it, and that the untrusted-client exclusion is
stated in words. Start the server with `--transport http` and confirm it listens
on `127.0.0.1` only, and that no `STEALTH_MCP_*` variable changes that.
**Evidence**: satisfied — pytest:
`tests/test_security_boundary.py::test_http_bind_defaults_to_literal_loopback`,
`tests/test_security_boundary.py::test_backend_spawn_argv_pins_the_loopback_host`,
`tests/test_security_boundary.py::test_no_environment_knob_can_change_the_bind_host`,
`tests/test_release_contract.py::test_the_threat_contract_is_generated_from_the_policy`,
`tests/test_release_contract.py::test_the_threat_contract_states_the_untrusted_client_is_out_of_scope`.

### MQ-132: Browser-JavaScript and host-Python execution boundaries
**Manual**: with a harmless canary value, confirm that JavaScript submitted to
`execute_script` stays in browser execution contexts, and that
`create_python_binding` code runs with the host server's privileges.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_browser_js_canary_stays_in_the_page`. The
browser-side half needs real Chrome and is NOT verified.
**Current support (non-acceptance)**: pytest:
`tests/test_security_boundary.py::test_host_python_execution_sites_are_exactly_the_declared_set`
pins the host-`exec`/`eval` INVENTORY so a new site cannot appear unannounced.
It is not an isolation result: host execution at the user's privileges is
intended behaviour and is recorded as a trust requirement, never as a control.

### MQ-133: The complete normal `*_to_file` / import / export matrix
**Manual**: drive every filesystem tool to a throwaway directory and confirm
exact bytes and exact destination for each.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_every_to_file_tool_writes_its_declared_bytes`.
Ten of the twelve filesystem paths need a live browser and are NOT verified.
**Current support (non-acceptance)**: pytest:
`tests/test_security_boundary.py::TestFilesystemDestinationMatrix` covers the
three paths reachable without a browser (`export_network_data`,
`import_network_data`, `export_debug_logs`), and
`tests/test_security_boundary.py::test_every_to_file_tool_is_in_the_filesystem_inventory`
keeps the inventory itself from going stale.

### MQ-134: Traversal, absolute, and platform path semantics
**Manual**: for each filesystem tool, pass a relative path, an absolute path, a
`..` traversal, and a mixed-separator path; record where each actually lands.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_to_file_path_semantics_matrix`. Covering only
the hermetic subset cannot satisfy a step whose scope is every filesystem tool.
**Current support (non-acceptance)**: pytest:
`tests/test_security_boundary.py::TestFilesystemDestinationMatrix::test_dot_dot_traversal_is_accepted_and_escapes_the_given_directory`
and its siblings pin the exact resolved destination for the hermetic paths.
Traversal and absolute paths are ACCEPTED — an intended capability under the
trusted-caller model, recorded so it is never mistaken for containment.

### MQ-135: Symlink, junction, and reparse-point semantics
**Manual**: point a filesystem tool through a directory link on each OS and
record whether it follows the link to its real target.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_to_file_link_semantics`. The one current probe
skips where a runner cannot create a link, and a skip is an absent measurement.
**Current support (non-acceptance)**: pytest:
`tests/test_security_boundary.py::TestFilesystemDestinationMatrix::test_a_symlinked_directory_is_followed_to_its_real_target`.

### MQ-136: Overwrite, collision, and cleanup
**Manual**: write twice to one destination and confirm the documented overwrite
behaviour; confirm cleanup removes what it claims to.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_clone_file_overwrite_and_cleanup`. The
`cleanup_clone_files` half is NOT verified.
**Current support (non-acceptance)**: pytest:
`tests/test_security_boundary.py::TestFilesystemDestinationMatrix::test_an_existing_target_is_overwritten_without_warning`
and
`tests/test_security_boundary.py::TestFilesystemDestinationMatrix::test_two_exports_to_the_same_name_leave_one_file`.
The pinned behaviour is
that there is no overwrite policy: no refusal, no backup, no signal.

### MQ-137: Uploads, the redaction matrix, and the no-download limitation
**Manual**: upload a canary file and confirm exact bytes and name reach the page;
confirm every secret class is absent from diagnostics while error type, code and
correlation survive; confirm the contract states that no download tool exists.
**Evidence**: planned — planned-pytest:
`tests/test_e2e_transport.py::test_upload_file_delivers_exact_bytes_and_name`.
The upload half needs real Chrome and is NOT verified.
**Current support (non-acceptance)**: pytest:
`tests/test_security_boundary.py::TestRedactionPolicy` proves all eight secret
classes absent and the actionable fields intact,
`tests/test_security_boundary.py::TestRedactionPolicy::test_a_bare_token_outside_its_structure_is_not_redacted`
pins the policy's real boundary, and
`tests/test_security_boundary.py::test_no_download_tool_is_served` keeps the
stated absence true against the live registry.

---

## W15 — observability on failure (MQ-150..154)

These steps ask what the product tells you when it breaks, and what that telling
costs the operator. Read the `Evidence` lines literally: **two of the five are
satisfied and three are `planned`**, and the three are planned because the
capability itself is absent from `src/`, not because a test is missing. W15 is a
zero-`src/` workstream, so each gap is a characterization pin plus a contract
limitation (F-781..F-786 under `audit/stage2/`) and never a fix.

The headline result is the one that would have blocked the release: **no secret
canary was disclosed on any surface.** Eight canaries were planted in real
failing tool calls and searched byte-for-byte across stdout, stderr, the backend
log records, the debug-logger view, the policy-processed diagnostic and the
local repro bundle. Note *why* the log surfaces are clean — F-782 means a failed
call is never logged at all, so the argument-echoing messages never reach a log.
That is a consequence of an observability gap, not a control, and MQ-151 must be
re-run if F-782 is ever fixed.

These landed while `MQ-122..129` (W9, W10) and `MQ-138..149` (W13, W14) are still
reserved, so the contiguity check stays `MQ-1..113` plus the landed blocks until
those workstreams land.

### MQ-150: A failure names a stable type, exact bytes, and a correlated call
**Manual**: drive a validation failure, an unknown-instance failure, a CDP
timeout, a cancelled call and a filesystem failure. For each, confirm the error
type is stable, the message is the documented one character-for-character, and
that the client can tie the failure to a backend log line. Confirm nothing is
written to stdout while the stdio transport is live.
**Evidence**: planned — planned-pytest:
`tests/test_observability.py::TestDiagnosticOracle::test_the_diagnostic_carries_code_phase_and_correlation`.
The correlated half cannot be satisfied: a raised error carries no error code,
no failed phase, no next step and no correlation id (F-781), and the failure is
never logged at all (F-782), so there is nothing on either side to correlate.
Covering only the type-and-bytes half cannot satisfy a step whose scope is a
structured, correlated diagnostic.
**Current support (non-acceptance)**: pytest:
`tests/test_observability.py::TestDiagnosticOracle` pins the exact message bytes
of the validation, instance-not-found, timeout and filesystem failures,
`tests/test_observability.py::TestCorrelation::test_a_failing_call_still_stamps_one_id_on_its_log_pair`
proves one 12-hex id spans the start/end pair of a failing call, and
`tests/test_observability.py::TestStdoutPurity::test_a_burst_of_failures_writes_nothing_to_stdout`
proves the framing channel stays uncontaminated. The gaps are pinned by
`tests/test_observability.py::TestCorrelation::test_the_raised_error_carries_no_correlation_id`,
`tests/test_observability.py::TestCorrelation::test_a_failed_call_logs_no_error_record`,
`tests/test_observability.py::TestErrorConventionGaps::test_the_error_types_carry_no_code_phase_or_next_step`,
`tests/test_observability.py::TestErrorConventionGaps::test_the_timeout_path_escapes_the_one_error_convention`
and
`tests/test_observability.py::TestErrorConventionGaps::test_filesystem_paths_leak_raw_stdlib_exceptions`.

### MQ-151: No planted secret reaches any diagnostic surface
**Manual**: put a unique value in URL credentials, a URL query value, an
`Authorization` header, a cookie, an environment variable, a DOM/form value, a
filesystem path component and a script argument. Drive failures that touch each
one, then grep stdout, stderr, the backend log, the debug view, the repro bundle
and the generated reproduction command for every value. Any hit is a release
blocker.
**Evidence**: satisfied — pytest:
`tests/test_observability.py::TestSecretCanaries::test_no_canary_survives_the_canonical_policy`,
`tests/test_observability.py::TestSecretCanaries::test_no_canary_reaches_stdout_stderr_or_the_backend_log`,
`tests/test_observability.py::TestSecretCanaries::test_no_canary_reaches_the_written_bundle`,
`tests/test_observability.py::TestSecretCanaries::test_the_bundle_writer_refuses_a_canary_bearing_value`,
`tests/test_observability.py::TestSecretCanaries::test_the_actionable_fields_survive_the_policy`,
`tests/test_observability.py::TestSecretCanaries::test_the_structural_rules_catch_the_url_classes_unregistered`,
`tests/test_observability.py::TestSecretCanaries::test_credential_entries_are_dropped_entirely`.
The redactor is W12's canonical policy API imported from
`tools/release_evidence.py`; W15 adds no second redactor or policy table. The
first node is parametrized over all eight secret classes, and
`tests/test_observability.py::TestSecretCanaries::test_the_control_proves_the_search_can_fail`
is the control that keeps the sweep from passing vacuously — it asserts every
canary IS present before redaction.

### MQ-152: Diagnostic capture is bounded, marked, and encoding-safe
**Manual**: induce an oversized DOM/body/stderr failure. Confirm per-field and
total limits hold, that a truncated value is explicitly marked as truncated,
that the written bytes are valid UTF-8, that the write is atomic, and that
replay leaves nothing behind.
**Evidence**: planned — planned-pytest:
`tests/test_observability.py::TestBoundedCapture::test_a_bounded_value_is_marked_and_checksummed`.
Two halves cannot be satisfied: **no surface emits an inline truncation marker or
a checksum of dropped content** (F-785), so a bounded value cannot be recognised
as bounded from the value alone, and **no artifact write is atomic** (F-786) —
there is no write-then-rename anywhere in the tree.
**Current support (non-acceptance)**: pytest:
`tests/test_observability.py::TestBoundedCapture::test_an_oversized_payload_is_replaced_by_a_bounded_envelope`
pins the spill envelope and its exact `reason` string,
`tests/test_observability.py::TestBoundedCapture::test_a_payload_within_budget_passes_through_untouched` pins the pass-through
edge, `tests/test_observability.py::TestBoundedCapture::test_the_debug_view_reports_total_versus_returned` pins the counter pair
that stands in for a marker, `tests/test_observability.py::TestBoundedCapture::test_the_spilled_bytes_are_valid_utf8` proves the
one write site that declares its encoding round-trips real multibyte UTF-8, and
`tests/test_observability.py::TestBoundedCapture::test_the_per_field_and_total_bundle_caps_are_enforced` pins W6's per-field and
per-list refusals. The absences are pinned by
`tests/test_observability.py::TestBoundedCapture::test_no_diagnostic_surface_emits_an_inline_truncation_marker`,
`tests/test_observability.py::TestBoundedCapture::test_there_is_no_total_byte_budget_across_the_whole_record`,
`tests/test_observability.py::TestBoundedCapture::test_the_json_exports_are_ascii_by_default_not_declared_utf8` and
`tests/test_observability.py::TestBoundedCapture::test_no_local_artifact_write_is_atomic`. Per-cap enforcement itself is already
covered by `tests/test_network_interceptor.py::TestBodyStoreByteCaps`,
`tests/test_response_handler.py` and `tests/test_debug_logger.py::TestBufferCaps`;
W15 does not restate them.

### MQ-153: A failure tells the operator what to do next
**Manual**: for each failure class, confirm the message names a concrete local
action — a tool to call, a setting to change, or a file to inspect — and that the
action it names actually exists.
**Evidence**: planned — planned-pytest:
`tests/test_observability.py::TestRecoveryGuidance::test_every_failure_class_names_a_next_step`.
The three highest-traffic messages — `Instance not found`, `Invalid index value`
and `Invalid JSON in extraction_options` — name no recovery action at all
(F-781). A step whose scope is every failure class cannot be satisfied by the
subset that happens to carry guidance.
**Current support (non-acceptance)**: pytest:
`tests/test_observability.py::TestRecoveryGuidance::test_the_timeout_names_a_local_recoverable_action`
and
`tests/test_observability.py::TestRecoveryGuidance::test_the_script_guard_names_the_tool_to_use_instead`
prove the two guided messages name a tool that is actually served, and
`tests/test_observability.py::TestRecoveryGuidance::test_the_export_timeout_names_its_alternative_format` pins the export-timeout
string against the live source. `tests/test_observability.py::TestRecoveryGuidance::test_the_commonest_failures_offer_no_next_step`
pins the gap so the unguided set cannot grow silently.

### MQ-154: A sanitized transcript replays locally and mutates nothing external
**Manual**: from a throwaway directory, replay a sanitized transcript against the
deterministic fixture and confirm it reproduces the same typed failure. Confirm
no DNS lookup or public request is made, no issue/comment/webhook is created, and
nothing is written outside the destination.
**Evidence**: satisfied — pytest:
`tests/test_observability.py::TestLocalReplay::test_the_transcript_replays_to_the_same_typed_failure`,
`tests/test_observability.py::TestLocalReplay::test_the_replay_resolves_no_name_and_opens_no_socket`,
`tests/test_observability.py::TestLocalReplay::test_the_replay_writes_nothing_outside_the_destination`,
`tests/test_observability.py::TestLocalReplay::test_the_destination_resolver_refuses_every_non_throwaway_target`,
`tests/test_observability.py::TestLocalReplay::test_the_bundle_writer_exposes_no_upload_or_notification_flag`.
The destination resolver and bundle writer are W6's, imported from
`tools/canary_repro.py`; W15 adds no second resolver. The replay runs with
`socket.socket`, `socket.create_connection` and `socket.getaddrinfo` all replaced
by a raising stub, so a network attempt fails the test rather than succeeding
quietly, and the no-external-mutation claim is asserted against the real CLI
parser rather than left as a comment.

---

## Reserved MQ ranges

The current design-time manifest ends at `MQ-113`. The identifiers below are
reservations only: they are not headings, current steps, planned evidence, or
coverage.

W7 owns eight deterministic site-shape behaviors:

- `MQ-114` — fresh selector re-query after a SPA History API route swap and node
  replacement: `tests/test_e2e_dynamic_sites.py::test_spa_history_route_swap_and_requery`.
  No stale element or backend handles are retained or exercised.
- `MQ-115` — direct iframe metadata and the explicit limitation on a true-origin
  A→B→A fixture:
  `tests/test_e2e_dynamic_sites.py::test_cross_origin_a_b_a_direct_metadata_and_limit`.
  No recursive traversal, frame switching, control targeting, or child-frame
  content extraction is claimed.
- `MQ-116` — IntersectionObserver lazy load plus virtualized and finite-infinite
  lists: `tests/test_e2e_dynamic_sites.py::test_intersection_observer_lazy_load`
  and
  `tests/test_e2e_dynamic_sites.py::test_virtualized_and_finite_infinite_lists`.
- `MQ-117` — strict response-header CSP:
  `tests/test_e2e_dynamic_sites.py::test_strict_csp_surface`.
- `MQ-118` — final browser-visible auth, redirect, and CORS outcome only:
  `tests/test_e2e_dynamic_sites.py::test_auth_redirect_cors_preflight`. No
  intermediate redirect-hop, authentication-exchange, request/response, or
  preflight-event inspection is claimed.
- `MQ-119` — completed text body, base64 binary body, fully assembled chunked
  body, and 4xx/5xx outcomes only:
  `tests/test_e2e_dynamic_sites.py::test_completed_text_base64_binary_chunked_and_http_errors`.
  No truncated-stream or download claim is made.
- `MQ-120` — page-runtime SSE and WebSocket lifecycle only:
  `tests/test_e2e_dynamic_sites.py::test_sse_and_websocket_lifecycle`. No
  network-debugging event or frame capture is claimed.
- `MQ-121` — tab list, switch, inspect, and close for a `target=_blank` popup,
  plus custom-element, template, and nested-slot light-DOM or explicit script
  escape-hatch limits:
  `tests/test_e2e_dynamic_sites.py::test_custom_elements_slots_and_popup_lifecycle`.
  No shadow-root piercing or native popup-control targeting is claimed.

The remaining ownership reservations are:

- W9: `MQ-122..125` — performance/resource budgets.
- W10: `MQ-126..129` — resilience/fault injection.
- ~~W11: `MQ-130` — documentation examples and claims sync.~~ **Landed** above as
  a current step with its acceptance test; no longer a reservation.
- ~~W12: `MQ-131..137`~~ — **landed**; the steps are headings above.
- W13: `MQ-138..144` — concurrency, cancellation, framing, and independent
  protocol interoperability.
- W14: `MQ-145..149` — literal immutable immediate N-1 upgrade, migration,
  rollback, and artifact identity. The human/admin selects and records the
  immutable immediately
  preceding stable tag and artifact SHA-256; the executor verifies that exact
  identity. An arbitrary prior release or same-version reinstall is invalid.
- ~~W15: `MQ-150..154`~~ — **landed**; the steps are headings above. MQ-151 and
  MQ-154 are satisfied; MQ-150, MQ-152 and MQ-153 are `planned` because the
  capability they require is absent from `src/` (F-781..F-786), not because a
  test is missing.
- W16: `MQ-155..162` — stateful/PWA, dedicated/shared-worker, and
  international-text behavior.

Each reserved step must be appended in the **same commit** as its live acceptance
test and W5-ledger/parity update. No workstream may predeclare a reserved MQ as
planned coverage, and the current contiguity check must remain `MQ-1..113` until
the owning atomic commit lands.

Evidence-state counts are derived by the parity tooling from the entries above;
they are deliberately not copied into a hand-maintained summary table.
