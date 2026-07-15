# Manual QA Protocol — the "Blind Push" Manifest

Every step below is something a human would do by hand to sign off a release
before shipping. Each step has a stable `MQ-<n>` id and maps to one or more
automated tests. The tripwire test (`test_manual_qa_parity.py`) fails CI if any
MQ step has no live (collected, not skip/xfail) test covering it.

**If you'd check it by hand, it must be here. If it's here, it must be automated.**

Known-bug steps are marked `[KNOWN-BUG: <id>]` — they are pinned to a
characterization test and flagged as a known gap, so a green parity run never
quietly masks them. When the bug is fixed, the characterization marker drops
and the step becomes a first-class success assertion.

---

## Phase 1 — Installation & Launch

### MQ-1: Clean install from PyPI
**Manual**: `pip install stealth-chrome-devtools-mcp` (or `uvx`) in a fresh venv.
Verify the console script `stealth-chrome-devtools-mcp` is on `$PATH` and prints
help / version.
**Automated by**: `test_e2e_transport::test_install_smoke` (W3)

### MQ-2: Stdio server starts and completes MCP handshake
**Manual**: configure an MCP host (Claude Desktop, Cursor, etc.) with the stdio
transport; watch for `initialize` → server responds with name + version +
`tools/list` returning all 94 tools.
**Automated by**: `test_e2e_transport::test_handshake_and_tools_list` (W1)

### MQ-3: Tool schemas are well-formed
**Manual**: open `tools/list` response; every tool has a non-empty `description`
and a valid `inputSchema` (JSON Schema object, no dangling `$ref`).
**Automated by**: `test_e2e_transport::test_all_tool_schemas_valid` (W1)

### MQ-4: HTTP transport starts on loopback by default
**Manual**: `stealth-chrome-devtools-mcp --transport http`; verify bound to
`127.0.0.1`, not `0.0.0.0`.
**Automated by**: `test_server_entrypoint::test_http_host_defaults_to_loopback` (existing)

---

## Phase 2 — Browser Spawn & Lifecycle

### MQ-5: Spawn browser (headless)
**Manual**: call `spawn_browser` with headless=true. Verify success response
containing an instance ID.
**Automated by**: `test_e2e_interaction::test_browser_management_lifecycle` (existing E2E)
`test_e2e_transport::test_canonical_journey` (W1 — through real transport)

### MQ-6: Spawn browser (headed, if display available)
**Manual**: call `spawn_browser` without headless; a Chrome window appears.
**Automated by**: `test_e2e_interaction::test_browser_management_lifecycle` (existing, headless CI; headed tested locally where display available)

### MQ-7: List instances shows the spawned browser
**Manual**: call `list_instances`; the instance ID from MQ-5 appears.
**Automated by**: `test_e2e_interaction::test_browser_management_lifecycle` (existing)

### MQ-8: Get instance state returns expected fields
**Manual**: call `get_instance_state`; verify browser info, url, tabs present.
**Automated by**: `test_e2e_interaction::test_browser_management_lifecycle` (existing)

### MQ-9: Close instance cleanly — no orphaned Chrome processes
**Manual**: call `close_instance`; `list_instances` no longer shows it; verify no
orphaned `chrome` processes remain (Task Manager / `ps aux`).
**Automated by**: `test_manual_qa_parity::test_close_leaves_no_orphan_processes` (W8 — new, psutil child-count before == after)

### MQ-10: Spawn→close N times — no fd/process leak
**Manual**: repeat spawn→navigate→close 5 times; system stays healthy.
**Automated by**: `test_manual_qa_parity::test_spawn_close_cycle_no_leak` (W8 — new, N cycles with psutil assertion)

### MQ-11: Kill Chrome mid-session — typed error, not hang
**Manual**: spawn, navigate, then kill the Chrome process externally; next tool
call returns a typed error within a bounded time (no infinite hang).
**Automated by**: `test_manual_qa_parity::test_killed_browser_returns_typed_error` (W8 — new)

### MQ-12: Multi-instance — two browsers don't cross-talk
**Manual**: spawn two instances; navigate each to a different page; verify tabs
and page content are isolated.
**Automated by**: `test_manual_qa_parity::test_multi_instance_isolation` (W8 — new)

---

## Phase 3 — Stealth Verification

### MQ-13: navigator.webdriver is false
**Manual**: spawn browser, open DevTools console, type `navigator.webdriver` →
must be `false` (not `true`, not `undefined`).
**Automated by**: `test_stealth::test_navigator_webdriver_is_false` (W4 — offline probe)

### MQ-14: No CDP-leak globals
**Manual**: in console, check `window.cdc_*`, `$cdc_*`, `__driver_evaluate`,
`__webdriver_evaluate`, `__selenium_*`, `__fxdriver_*`, `__driver_unwrap`,
`calledSelenium`, `_Selenium_IDE_Recorder`, `_phantom`, `callPhantom`,
`phantom` → all must be absent/undefined.
**Automated by**: `test_stealth::test_no_cdp_leak_globals` (W4 — offline probe)

### MQ-15: navigator.plugins is non-empty
**Manual**: `navigator.plugins.length > 0` in console.
**Automated by**: `test_stealth::test_navigator_plugins_populated` (W4)

### MQ-16: navigator.languages is present and non-empty
**Manual**: `navigator.languages` → non-empty array, first element matches
`navigator.language`.
**Automated by**: `test_stealth::test_navigator_languages_present` (W4)

### MQ-17: window.chrome object exists with correct shape
**Manual**: `window.chrome` → object, `window.chrome.runtime` exists.
**Automated by**: `test_stealth::test_window_chrome_shape` (W4)

### MQ-18: User-Agent consistency (UA string vs userAgentData)
**Manual**: `navigator.userAgent` and `navigator.userAgentData.brands` reference
the same browser/version; no "HeadlessChrome" in the UA.
**Automated by**: `test_stealth::test_ua_consistency_no_headless_leak` (W4)

### MQ-19: Function.prototype.toString integrity on patched builtins
**Manual**: `Function.prototype.toString.call(navigator.permissions.query)` →
must contain `"[native code]"`, not reveal patching.
**Automated by**: `test_stealth::test_native_code_integrity` (W4)

### MQ-20: Automation-revealing Chrome flags are stripped
**Manual**: verify spawned Chrome was not launched with `--enable-automation`,
`--test-type`, `--remote-debugging-port=0`, or other automation tells.
**Automated by**: `test_stealth_args::*` (existing 30 tests — argument sanitizer)
`test_stealth::test_automation_flags_absent_at_runtime` (W4 — runtime CDP check)

### MQ-21: Differential stealth — vanilla headless IS detected, stealth IS NOT
**Manual**: visit a bot-detection page with both a vanilla `google-chrome
--headless` and the stealth browser; the stealth instance passes checks that
vanilla fails.
**Automated by**: `test_stealth::test_differential_stealth` (W7 — differential comparison)

---

## Phase 4 — Navigation & Page Interaction

### MQ-22: Navigate to a URL
**Manual**: call `navigate` with the fixture app URL. Verify page loads.
**Automated by**: `test_e2e_interaction::test_browser_management_lifecycle` (existing)
`test_e2e_transport::test_canonical_journey` (W1)

### MQ-23: Go back / go forward
**Manual**: navigate to page A, then page B; `go_back` → page A; `go_forward`
→ page B.
**Automated by**: `test_e2e_interaction::test_navigation_history_lifecycle` (existing)

### MQ-24: Reload page
**Manual**: call `reload_page`; page re-renders.
**Automated by**: `test_e2e_interaction::test_browser_management_lifecycle` (existing)

### MQ-25: Get page content
**Manual**: call `get_page_content`; verify HTML contains expected elements.
**Automated by**: `test_e2e_interaction::test_query_and_page_content` (existing)
`test_e2e_transport::test_canonical_journey` (W1)

### MQ-26: Take screenshot — valid PNG
**Manual**: call `take_screenshot`; open the result; it's a recognizable image.
**Automated by**: `test_e2e_interaction::test_screenshot_returns_valid_png` (existing — PNG magic bytes + nonzero)
`test_e2e_transport::test_canonical_journey` (W1)

### MQ-27: Query elements — finds expected elements by CSS selector
**Manual**: `query_elements` with `#btn-counter` → returns element info.
**Automated by**: `test_e2e_interaction::test_query_and_page_content` (existing)

### MQ-28: Click element — action fires, state changes
**Manual**: click `#btn-counter`; `#counter-value` text increments; action log
records `click:btn-counter`.
**Automated by**: `test_e2e_interaction::test_interaction_lifecycle` (existing)
`test_e2e_transport::test_canonical_journey` (W1)

### MQ-29: Type text into an input
**Manual**: call `type_text` on `#text-input`; verify the input value changes
and action log records keystrokes.
**Automated by**: `test_e2e_interaction::test_interaction_lifecycle` (existing)
`test_e2e_transport::test_canonical_journey` (W1)

### MQ-30: Paste text
**Manual**: call `paste_text` on `#textarea-input`; verify content pasted.
**Automated by**: `test_e2e_interaction::test_interaction_lifecycle` (existing)

### MQ-31: Select option from dropdown
**Manual**: call `select_option` on a `<select>` with value `beta` → option
selected; action log records `change:select-single`.
**Automated by**: `test_e2e_interaction::test_interaction_lifecycle` (existing)
`test_e2e_transport::test_canonical_journey` (W1)

### MQ-32: Wait for element — bounded wait succeeds on delayed reveal
**Manual**: click `#reveal-btn`; call `wait_for_element` for `#delayed-el` with
timeout ≥5s → element appears (200ms reveal vs 5s timeout → no flake).
**Automated by**: `test_e2e_interaction::test_interaction_lifecycle` (existing)

### MQ-33: Scroll page
**Manual**: call `scroll_page` down 500px; verify scroll position changed.
**Automated by**: `test_e2e_interaction::test_interaction_lifecycle` (existing)

### MQ-34: Upload single file
**Manual**: call `upload_file` on `#single-file`; verify the file is attached.
**Automated by**: `test_e2e_interaction::test_file_upload_lifecycle` (existing)

### MQ-35: Upload multiple files
**Manual**: call `upload_file` on `#multi-file` with multiple paths.
**Automated by**: `test_e2e_interaction::test_file_upload_lifecycle` (existing)

### MQ-36: Execute script — run JS and get return value
**Manual**: `execute_script("return document.title")` → returns the page title.
**Automated by**: `test_e2e_interaction::test_interaction_lifecycle` (existing — reads action log via execute_script)

### MQ-37: Get element state — returns computed properties
**Manual**: call `get_element_state` on `#styled-card`; verify tag, text,
visibility, attributes returned.
**Automated by**: `test_e2e_interaction::test_get_element_state_pins_current_shape` (existing)
`test_e2e_transport::test_canonical_journey` (W1)

---

## Phase 5 — Negative Cases (Failure Paths)

### MQ-38: Click disabled control — typed failure, not silent True
**Manual**: try clicking a `<button disabled>`; expect a failure response or
ToolError, NOT a silent `True` claiming success.
**Automated by**: `test_e2e_interaction_fidelity::test_click_respects_occlusion_and_offscreen` (existing characterization)
`test_manual_qa_parity::test_click_disabled_control_failure_shape` (W8 — new)
`[KNOWN-BUG: E8-2]` Currently returns True. Characterization-pinned until M5b fix.

### MQ-39: Type into readonly field — refused
**Manual**: try `type_text` on a `<input readonly>`; expect refusal or no-op,
not content modification.
**Automated by**: `test_manual_qa_parity::test_type_readonly_field_refused` (W8 — new)
`[KNOWN-BUG: E8-3]` Currently bypasses readonly. Characterization-pinned.

### MQ-40: Select option second call in same document — works (not silent no-op)
**Manual**: call `select_option` twice on the same `<select>` with different
values; both take effect.
**Automated by**: `test_e2e_interaction_fidelity::test_select_option_const_redeclaration` (existing characterization)
`[KNOWN-BUG: E8-1]` Second call is currently a silent no-op. Characterization-pinned.

### MQ-41: Range/color/date inputs — reachable by a typing tool
**Manual**: try to set a `<input type="range">`, `<input type="color">`,
`<input type="date">` via available tools; expect value to change.
**Automated by**: `test_manual_qa_parity::test_specialty_inputs_reachable` (W8 — new)
`[KNOWN-BUG: E8-4]` Currently unreachable. Characterization-pinned.

### MQ-42: Act on removed/stale element — typed error
**Manual**: query an element, navigate away (detaching it), act on the stale
reference; expect a typed error, not a crash or -32000 raw CDP error.
**Automated by**: `test_e2e_interaction::test_get_element_state_pins_current_shape` (existing — removed element raises)
`[KNOWN-BUG: F-181]` Some code paths return raw -32000. Characterization-pinned.

### MQ-43: Bad CSS selector — typed error, not crash
**Manual**: `query_elements` with `#[invalid` → expect a clear error message.
**Automated by**: `test_manual_qa_parity::test_bad_selector_typed_error` (W8 — new)

### MQ-44: Tool call with missing required parameter — validation error
**Manual**: call `navigate` without `url` → expect a schema validation error
from FastMCP, not a Python traceback.
**Automated by**: `test_mcp_protocol_surface::test_missing_required_param_validation_error` (existing)

### MQ-45: Tool call with wrong-type parameter — validation error
**Manual**: call `click_element` with `selector=123` (int not string) → schema
validation error.
**Automated by**: `test_mcp_protocol_surface::test_wrong_type_param_validation_error` (existing)

### MQ-46: Nonexistent tool call — tool-not-found error
**Manual**: call a tool named `does_not_exist` → clear error, not crash.
**Automated by**: `test_mcp_protocol_surface::test_tool_not_found_error_shape` (existing)

---

## Phase 6 — Tabs

### MQ-47: List tabs
**Manual**: after spawning and navigating, `list_tabs` returns ≥1 tab with the
navigated URL.
**Automated by**: `test_e2e_interaction::test_tabs_lifecycle` (existing)

### MQ-48: New tab
**Manual**: `new_tab` → tab count increases by 1.
**Automated by**: `test_e2e_interaction::test_tabs_lifecycle` (existing)

### MQ-49: Switch tab
**Manual**: open two tabs; `switch_tab` to the second; `get_active_tab` confirms.
**Automated by**: `test_e2e_interaction::test_tabs_lifecycle` (existing)

### MQ-50: Close tab
**Manual**: `close_tab` on the second tab; `list_tabs` no longer shows it.
**Automated by**: `test_e2e_interaction::test_tabs_lifecycle` (existing)

### MQ-51: Get active tab
**Manual**: `get_active_tab` returns the currently focused tab info.
**Automated by**: `test_e2e_interaction::test_tabs_lifecycle` (existing)

---

## Phase 7 — Cookies

### MQ-52: Set cookie
**Manual**: `set_cookie` with name/value; verify it persists.
**Automated by**: `test_e2e_interaction::test_cookies_lifecycle` (existing)

### MQ-53: Get cookies — returns set cookies
**Manual**: call `get_cookies` after setting one.
**Automated by**: E2E_EXEMPT — `get_cookies` hangs against real Chrome (nodriver CDP bug). Covered hermetically in unit tests.
`[KNOWN-BUG: get_cookies_hang]` See E2E_EXEMPT entry. Protocol-level test only.

### MQ-54: Clear cookies
**Manual**: `clear_cookies` → `get_cookies` (or `execute_script` reading
`document.cookie`) shows them gone.
**Automated by**: `test_e2e_interaction::test_cookies_lifecycle` (existing — verifies via execute_script)

---

## Phase 8 — Network Debugging

### MQ-55: List network requests after a page load
**Manual**: navigate, then `list_network_requests`; expect the navigation
request in the list.
**Automated by**: `test_e2e_data_tools::test_network_debugging_lifecycle` (existing)

### MQ-56: Get request details
**Manual**: pick a request from the list; `get_request_details` returns URL,
method, headers.
**Automated by**: `test_e2e_data_tools::test_network_debugging_lifecycle` (existing)

### MQ-57: Get response details + content
**Manual**: `get_response_details` returns status code; `get_response_content`
returns the body (with capture_bodies enabled).
**Automated by**: `test_e2e_data_tools::test_network_debugging_lifecycle` (existing)

### MQ-58: Search network requests
**Manual**: `search_network_requests` with a URL substring → filters to match.
**Automated by**: `test_e2e_data_tools::test_network_debugging_lifecycle` (existing)

### MQ-59: Export / import network data
**Manual**: `export_network_data` writes a file; `import_network_data` reads
it back.
**Automated by**: `test_e2e_data_tools::test_network_debugging_lifecycle` (existing)

### MQ-60: Set / get network capture filters
**Manual**: `set_network_capture_filters(capture_bodies=True)` then verify via
`get_network_capture_filters`.
**Automated by**: `test_e2e_data_tools::test_network_debugging_lifecycle` (existing)

### MQ-61: Modify headers
**Manual**: `modify_headers` to add a custom header; trigger a request; verify
the header was sent (via echo endpoint).
**Automated by**: `test_e2e_data_tools::test_network_debugging_lifecycle` (existing)

---

## Phase 9 — Element Extraction & Cloning

### MQ-62: Extract element styles
**Manual**: `extract_element_styles` on `#styled-card` → returns color, bg,
padding, border values matching the fixture CSS.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing)

### MQ-63: Extract element structure
**Manual**: `extract_element_structure` → returns tag hierarchy, attributes,
children.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing)
`test_e2e_transport::test_canonical_journey` (W1)

### MQ-64: Extract element events
**Manual**: `extract_element_events` on an element with listeners → lists them.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing)

### MQ-65: Extract element animations
**Manual**: `extract_element_animations` on an animated element → returns
animation info.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing)

### MQ-66: Extract element assets
**Manual**: `extract_element_assets` → returns images/backgrounds used.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing)

### MQ-67: Extract element styles via CDP
**Manual**: `extract_element_styles_cdp` → returns computed styles.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing)

### MQ-68: Extract related files
**Manual**: `extract_related_files` → lists stylesheets, scripts referenced.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing)

### MQ-69: Clone element complete
**Manual**: `clone_element_complete` on `#styled-card` → returns full clone data.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing)

### MQ-70: Extract complete element via CDP
**Manual**: `extract_complete_element_cdp` → full CDP-path extraction.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing)

---

## Phase 10 — Progressive Cloning

### MQ-71: Clone element progressive → stored element
**Manual**: `clone_element_progressive` → returns stored element ID.
**Automated by**: `test_e2e_data_tools::test_progressive_cloning_lifecycle` (existing)

### MQ-72: Expand styles / events / children / css_rules / pseudo_elements / animations
**Manual**: call each `expand_*` on the stored ID; data grows.
**Automated by**: `test_e2e_data_tools::test_progressive_cloning_lifecycle` (existing)

### MQ-73: List stored elements
**Manual**: `list_stored_elements` shows the stored ID.
**Automated by**: `test_e2e_data_tools::test_progressive_cloning_lifecycle` (existing)

### MQ-74: Clear stored element / clear all elements
**Manual**: `clear_stored_element` removes one; `clear_all_elements` removes all.
**Automated by**: `test_e2e_data_tools::test_progressive_cloning_lifecycle` (existing)

---

## Phase 11 — File Extraction

### MQ-75: Clone element to file
**Manual**: `clone_element_to_file` → file created on disk.
**Automated by**: `test_e2e_data_tools::test_file_extraction_lifecycle` (existing)

### MQ-76: Extract complete element to file
**Manual**: file extraction variant → file on disk.
**Automated by**: `test_e2e_data_tools::test_file_extraction_lifecycle` (existing)

### MQ-77: Extract styles/structure/events/animations/assets to file
**Manual**: each `*_to_file` variant works.
**Automated by**: `test_e2e_data_tools::test_file_extraction_lifecycle` (existing)

### MQ-78: List clone files
**Manual**: `list_clone_files` → shows the files written.
**Automated by**: `test_e2e_data_tools::test_file_extraction_lifecycle` (existing)

### MQ-79: Cleanup clone files
**Manual**: `cleanup_clone_files` → files removed.
**Automated by**: `test_e2e_data_tools::test_file_extraction_lifecycle` (existing)

---

## Phase 12 — CDP / JavaScript Functions

### MQ-80: List CDP commands
**Manual**: `list_cdp_commands` → non-empty list of domain.method strings.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-81: Execute CDP command
**Manual**: `execute_cdp_command` with `Runtime.evaluate` → returns result.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-82: Get execution contexts
**Manual**: `get_execution_contexts` → ≥1 context.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-83: Discover global functions / object methods
**Manual**: `discover_global_functions` → finds `calcTotal`;
`discover_object_methods` on `window.appAPI` → finds `getUser`, `setFlag`.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-84: Call JavaScript function
**Manual**: `call_javascript_function("calcTotal", args=[3, 4])` → returns 7.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-85: Inspect function signature
**Manual**: `inspect_function_signature("calcTotal")` → shows params `a, b`.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-86: Inject and execute script
**Manual**: `inject_and_execute_script` with inline JS → returns result.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-87: Create persistent function
**Manual**: create a persistent function; call it; survives across navigations
within the same page context.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-88: Execute function sequence
**Manual**: `execute_function_sequence` with a chain of calls → final result.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-89: Create Python binding
**Manual**: `create_python_binding` → JS-callable function backed by Python logic.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

### MQ-90: Execute Python in browser (if py2js installed)
**Manual**: `execute_python_in_browser` → Python→JS transpiled and executed.
If `py2js` not installed, graceful error (not crash).
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing — asserts graceful error if py2js missing)

### MQ-91: Get function executor info
**Manual**: `get_function_executor_info` → status of the function execution system.
**Automated by**: `test_e2e_functions_hooks::test_cdp_functions_lifecycle` (existing)

---

## Phase 13 — Dynamic Hooks

### MQ-92: Create dynamic hook → trigger → details → remove
**Manual**: `create_dynamic_hook` matching a URL pattern; trigger a navigation
that matches; `get_dynamic_hook_details` shows the hook fired; `remove_dynamic_hook`.
**Automated by**: `test_e2e_functions_hooks::test_dynamic_hooks_lifecycle` (existing)

### MQ-93: Create simple dynamic hook (shorthand)
**Manual**: `create_simple_dynamic_hook` → verify it acts like a full hook.
**Automated by**: `test_e2e_functions_hooks::test_dynamic_hooks_lifecycle` (existing)

### MQ-94: List dynamic hooks
**Manual**: after creation, `list_dynamic_hooks` shows the hook.
**Automated by**: `test_e2e_functions_hooks::test_dynamic_hooks_lifecycle` (existing)

### MQ-95: Validate hook function
**Manual**: `validate_hook_function` with valid/invalid code → appropriate verdict.
**Automated by**: `test_e2e_functions_hooks::test_dynamic_hooks_lifecycle` (existing)

### MQ-96: Hook documentation tools — return non-empty content
**Manual**: each of `get_hook_documentation`, `get_hook_examples`,
`get_hook_requirements_documentation`, `get_hook_common_patterns` returns
non-empty useful text.
**Automated by**: `test_e2e_functions_hooks::test_hook_doc_tools_return_content` (existing)

---

## Phase 14 — Debugging Tools

### MQ-97: Debug view lifecycle
**Manual**: `get_debug_view` → state dict; `clear_debug_view` → cleared;
`export_debug_logs` → log data.
**Automated by**: `test_e2e_functions_hooks::test_debugging_lifecycle` (existing)

### MQ-98: Debug lock status
**Manual**: `get_debug_lock_status` → current lock state.
**Automated by**: `test_e2e_functions_hooks::test_debugging_lifecycle` (existing)

### MQ-99: Validate browser environment
**Manual**: `validate_browser_environment_tool` → environment check report.
**Automated by**: `test_e2e_functions_hooks::test_debugging_lifecycle` (existing)

---

## Phase 15 — Complex DOM Structures

### MQ-100: Shadow DOM — characterization of current reach
**Manual**: navigate to a page with shadow DOM; try to query/extract inside it;
verify behavior matches documented limitations.
**Automated by**: `test_e2e_hard_dom::test_shadow_dom_characterization` (existing characterization)

### MQ-101: Nested iframes — characterization of current reach
**Manual**: navigate to a page with nested iframes; try cross-frame operations.
**Automated by**: `test_e2e_hard_dom::test_iframe_characterization` (existing characterization)

### MQ-102: Deep nesting (≥3 levels)
**Manual**: query/extract on deeply nested fixture elements → works.
**Automated by**: `test_e2e_data_tools::test_element_extraction_lifecycle` (existing — fixture has ≥3 levels)

---

## Phase 16 — Singleton / Process Management

### MQ-103: Singleton detects and reuses existing backend
**Manual**: start the server; start a second instance with the same version;
second instance reuses the backend (no double-bind).
**Automated by**: `test_singleton_version_aware::test_reuses_backend_with_matching_version` (existing)

### MQ-104: Version mismatch triggers restart
**Manual**: start backend v1; connect with v2 → old backend replaced.
**Automated by**: `test_singleton_version_aware::test_ignores_backend_with_mismatched_version` (existing)

### MQ-105: Port fallback when preferred port is occupied
**Manual**: occupy port 19222; start the server → picks a different free port.
**Automated by**: `test_singleton_port_fallback::test_squatted_preferred_returns_a_different_free_port` (existing)

### MQ-106: Stop and restart backend cleanly
**Manual**: stop the backend; restart → comes up on a valid port, state cleared.
**Automated by**: `test_singleton_stop_restart::test_responsive_backend_is_stopped_and_state_cleared` (existing)

### MQ-107: Fast handshake — initialize responds without waiting for backend
**Manual**: send `initialize` immediately → response arrives before backend boot.
**Automated by**: `test_singleton_fast_handshake::test_initialize_answered_without_backend` (existing)

### MQ-108: Stdio proxy exits when stdin closes
**Manual**: close the MCP host → the server process exits (no orphan).
**Automated by**: `test_singleton_fast_handshake::test_stdio_entrypoint_exits_when_stdin_closes` (existing)

---

## Phase 17 — Cross-Platform

### MQ-109: All of the above on Windows
**Manual**: repeat the full QA pass on Windows.
**Automated by**: W2 Windows CI job — runs unit + integration + transport suites.

### MQ-110: All of the above on macOS
**Manual**: repeat the full QA pass on macOS.
**Automated by**: W2 macOS CI job — mandatory and gating (human ruling 2026-07-15, plan_RELEASE §7.1 RESOLVED); runs unit + integration + transport suites, same footing as Windows. Backs the release claim "works on Linux, Windows and macOS".

---

## Phase 18 — Tool Coverage Completeness

### MQ-111: Every advertised tool has ≥1 E2E test
**Manual**: compare `tools/list` output to the test manifest; no tool is untested.
**Automated by**: `test_e2e_functions_hooks::test_e2e_coverage_manifest` (existing F-108 tripwire)

### MQ-112: Every advertised tool has ≥1 transport-tier test OR explicit exemption
**Manual**: same check through the real transport layer.
**Automated by**: `test_e2e_transport::test_transport_coverage_manifest` (W5 — new tripwire)

### MQ-113: Every MQ step in this file maps to a live test
**Manual**: this IS the manual step. Automated by the parity tripwire.
**Automated by**: `test_manual_qa_parity::test_every_mq_step_has_live_test` (W8 — the self-referential tripwire)

---

## Phase 19 — Performance & Resource Budgets

### MQ-114: Each tool returns within its latency budget
**Manual**: navigate/click/type/extract on the fixture app "feel instant" — no
multi-second stall on a simple action. A tool that suddenly takes 10× as long is
a regression a human would notice immediately.
**Automated by**: `test_perf::test_tool_latency_budgets` (W9 — p95 over K runs, per-tool budget on the fixture)

### MQ-115: A full session stays within a memory/handle ceiling and cleans up
**Manual**: run a realistic workload (spawn → many navigations/interactions/
extractions → close); memory doesn't balloon, and after close there are no
leftover Chrome processes or file handles.
**Automated by**: `test_perf::test_memory_and_handle_ceiling` (W9 — psutil RSS/fd/child-count under ceiling, returns to baseline after close)

### MQ-116: Startup + handshake completes within a budget
**Manual**: launching the server into the MCP host and getting the first
`tools/list` back is quick, not a long cold-start hang.
**Automated by**: `test_perf::test_startup_handshake_budget` (W9 — spawn + MCP handshake within bound)

### MQ-117: Large-payload extraction is bounded in time and memory
**Manual**: extract/clone a very large element (≥10k-node DOM) and export a large
network capture; it completes in reasonable time without OOM and returns correct
output.
**Automated by**: `test_perf::test_large_payload_stress` (W9 — large-DOM fixture, bounded time+memory, correctness preserved)

---

## Phase 20 — Resilience / Fault-Injection

### MQ-118: Chrome crash mid-session → typed error, then recovers
**Manual**: kill the browser process while a session is live; the next tool call
returns a clear typed error (not a hang or raw −32000), and a fresh `spawn_browser`
works afterward — the server didn't wedge.
**Automated by**: `test_resilience::test_crash_recovery` (W10 — psutil-kill child, assert typed error + successful respawn, no orphan)

### MQ-119: Tab closed under an active tool → typed error, not a hang
**Manual**: close the active tab out of band, then call a tab-scoped tool; it
returns a typed error, doesn't hang or silently succeed.
**Automated by**: `test_resilience::test_tab_closed_under_tool` (W10)

### MQ-120: Navigation to a hanging endpoint times out cleanly
**Manual**: navigate to an endpoint that never finishes loading; the tool honors
its timeout and returns a typed timeout error within a bound, rather than blocking
forever.
**Automated by**: `test_resilience::test_hanging_endpoint_timeout` (W10 — bounded typed timeout; the disciplined form of the get_cookies-hang class)

### MQ-121: Network drop mid-operation → typed error, recoverable
**Manual**: cut connectivity mid-navigate/mid-capture (offline emulation); the
tool returns a typed error and the session is usable again afterward.
**Automated by**: `test_resilience::test_network_drop_recoverable` (W10 — CDP offline emulation / route-abort)

---

## Phase 21 — Documentation Examples

### MQ-122: Every runnable README/docs example executes successfully
**Manual**: copy-paste each runnable code example from the README/docs and run it;
it works as written. The advertised install command and tool names are real.
**Automated by**: `test_doc_examples::test_runnable_examples` + `test_doc_examples::test_claims_sync` (W11 — extract-and-run marked blocks; claims-sync reuses `gen_release_contract.py`)

---

## Summary

| Phase | Steps | Already automated | New (this plan) |
|---|---|---|---|
| Installation & Launch | MQ-1..4 | 2 | 2 (W1, W3) |
| Browser Lifecycle | MQ-5..12 | 4 | 4 (W8) |
| Stealth | MQ-13..21 | 1 (arg sanitizer) | 8 (W4, W7) |
| Navigation & Interaction | MQ-22..37 | 16 | 0 (all existing) |
| Negative Cases | MQ-38..46 | 5 | 4 (W8) |
| Tabs | MQ-47..51 | 5 | 0 |
| Cookies | MQ-52..54 | 2 | 0 (MQ-53 exempt) |
| Network | MQ-55..61 | 7 | 0 |
| Extraction | MQ-62..70 | 9 | 0 |
| Progressive Cloning | MQ-71..74 | 4 | 0 |
| File Extraction | MQ-75..79 | 5 | 0 |
| CDP/JS Functions | MQ-80..91 | 12 | 0 |
| Dynamic Hooks | MQ-92..96 | 5 | 0 |
| Debugging | MQ-97..99 | 3 | 0 |
| Complex DOM | MQ-100..102 | 3 | 0 |
| Singleton/Process | MQ-103..108 | 6 | 0 |
| Cross-Platform | MQ-109..110 | 0 | 2 (W2) |
| Completeness | MQ-111..113 | 1 | 2 (W5, W8) |
| Performance & Resource Budgets | MQ-114..117 | 0 | 4 (W9) |
| Resilience / Fault-Injection | MQ-118..121 | 0 | 4 (W10) |
| Documentation Examples | MQ-122 | 0 | 1 (W11) |
| **Total** | **122** | **91** | **31** |

91 of 122 steps already have live automated tests. 31 new tests to write —
stealth verification is the biggest single block (8), followed by performance (4)
and resilience (4). The manifest covers the three pillars of the manual pass:
**it works** (functional/stealth/cross-platform, W1–W8), **it's fast and lean**
(latency + memory budgets, W9), and **it fails safe** (typed-error recovery under
crash/close/timeout/network-drop, W10) — plus the docs a user's first five minutes
depend on (W11). Once W1–W11 land, the green gate is a machine-verified stand-in
for this entire document, and you push on green.
