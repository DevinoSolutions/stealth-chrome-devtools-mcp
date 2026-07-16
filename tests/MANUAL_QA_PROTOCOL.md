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

### MQ-53: Get cookies — returns set cookies
**Manual**: call `get_cookies` after setting one.
**Evidence**: blocked — `[KNOWN-BUG: get_cookies_hang]` prevents successful
real-Chrome cookie retrieval; HEAD has no success-path acceptance target.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_e2e_coverage_manifest` records the
exemption only. Schema and missing-instance checks are not behavioral coverage,
and no fake unit success may clear this step.

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

### MQ-111: Every advertised tool has ≥1 E2E test
**Manual**: compare `tools/list` output to the test manifest; no tool is untested.
**Evidence**: blocked — MQ-53 leaves `get_cookies` without successful behavioral
E2E coverage, so the every-tool claim is false at HEAD.
**Current support (non-acceptance)**: pytest:
`tests/test_e2e_functions_hooks.py::test_e2e_coverage_manifest` proves a
93-covered/one-exempt partition. The inventory node cannot convert its sole
`get_cookies` exemption into coverage.

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
- W11: `MQ-130` — documentation examples and claims sync.
- W12: `MQ-131..137` — security/trust-boundary verification.
- W13: `MQ-138..144` — concurrency, cancellation, framing, and independent
  protocol interoperability.
- W14: `MQ-145..149` — literal immutable immediate N-1 upgrade, migration,
  rollback, and artifact identity. The human/admin selects and records the
  immutable immediately
  preceding stable tag and artifact SHA-256; the executor verifies that exact
  identity. An arbitrary prior release or same-version reinstall is invalid.
- W15: `MQ-150..154` — failure observability, redaction, and local repro integrity.
- W16: `MQ-155..162` — stateful/PWA, dedicated/shared-worker, and
  international-text behavior.

Each reserved step must be appended in the **same commit** as its live acceptance
test and W5-ledger/parity update. No workstream may predeclare a reserved MQ as
planned coverage, and the current contiguity check must remain `MQ-1..113` until
the owning atomic commit lands.

Evidence-state counts are derived by the parity tooling from the entries above;
they are deliberately not copied into a hand-maintained summary table.
