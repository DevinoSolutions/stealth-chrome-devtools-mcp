# Stealth Chrome DevTools MCP - Tool Test Results

**Date**: 2026-05-20
**Test Instance**: `9e5527a5-bf8b-4f7d-b14e-91f8d8a2ea44`
**Test Target**: httpbin.org/html, httpbin.org/get
**Total Tools**: 97

---

## Complete Tool Inventory (97 tools)

### Browser Lifecycle (4)
| # | Tool | Description |
|---|------|-------------|
| 1 | `spawn_browser` | Launch new browser instance |
| 2 | `close_instance` | Close a browser instance |
| 3 | `list_instances` | List all active instances |
| 4 | `get_instance_state` | Get detailed instance state |

### Navigation & Pages (7)
| # | Tool | Description |
|---|------|-------------|
| 5 | `navigate` | Navigate to URL |
| 6 | `reload_page` | Reload current page |
| 7 | `go_back` | Browser back |
| 8 | `go_forward` | Browser forward |
| 9 | `get_page_content` | Get HTML/text content |
| 10 | `take_screenshot` | Screenshot page |
| 11 | `scroll_page` | Scroll in any direction |

### Tabs (6)
| # | Tool | Description |
|---|------|-------------|
| 12 | `list_tabs` | List all tabs |
| 13 | `new_tab` | Open new tab |
| 14 | `switch_tab` | Switch to tab |
| 15 | `close_tab` | Close a tab |
| 16 | `get_active_tab` | Get current tab info |
| 17 | `wait_for_element` | Wait for element to appear |

### DOM Interaction (6)
| # | Tool | Description |
|---|------|-------------|
| 18 | `query_elements` | Query DOM with CSS/XPath |
| 19 | `click_element` | Click an element |
| 20 | `type_text` | Type text character-by-character |
| 21 | `paste_text` | Paste text into input |
| 22 | `press_key` | Press keyboard key |
| 23 | `select_option` | Select dropdown option |

### JavaScript Execution (8)
| # | Tool | Description |
|---|------|-------------|
| 24 | `execute_script` | Run JS in page context |
| 25 | `call_javascript_function` | Call JS function by path |
| 26 | `inject_and_execute_script` | Inject + execute JS |
| 27 | `create_persistent_function` | Survives page reloads |
| 28 | `discover_global_functions` | Find all global JS functions |
| 29 | `discover_object_methods` | Find methods on an object |
| 30 | `inspect_function_signature` | Inspect function params |
| 31 | `execute_function_sequence` | Chain function calls |

### Python/CDP Execution (5)
| # | Tool | Description |
|---|------|-------------|
| 32 | `execute_cdp_command` | Raw CDP command |
| 33 | `list_cdp_commands` | List available CDP commands |
| 34 | `create_python_binding` | Bridge Python to browser |
| 35 | `execute_python_in_browser` | Run Python transpiled to JS |
| 36 | `get_execution_contexts` | List JS execution contexts |

### Cookies & Storage (4)
| # | Tool | Description |
|---|------|-------------|
| 37 | `get_cookies` | Get cookies |
| 38 | `set_cookie` | Set a cookie |
| 39 | `clear_cookies` | Clear cookies |
| 40 | `upload_file` | Upload file to input |

### Network Interception (10)
| # | Tool | Description |
|---|------|-------------|
| 41 | `list_network_requests` | List captured requests |
| 42 | `search_network_requests` | Search with filters |
| 43 | `get_request_details` | Request details |
| 44 | `get_response_details` | Response details |
| 45 | `get_response_content` | Response body |
| 46 | `set_network_capture_filters` | Filter capture by type |
| 47 | `get_network_capture_filters` | Get current filters |
| 48 | `modify_headers` | Modify HTTP headers |
| 49 | `export_network_data` | Export to JSON file |
| 50 | `import_network_data` | Import from JSON file |

### Dynamic Hooks (10)
| # | Tool | Description |
|---|------|-------------|
| 51 | `create_dynamic_hook` | Create request hook with Python |
| 52 | `create_simple_dynamic_hook` | Simplified hook creation |
| 53 | `list_dynamic_hooks` | List all hooks |
| 54 | `get_dynamic_hook_details` | Hook details |
| 55 | `remove_dynamic_hook` | Remove a hook |
| 56 | `validate_hook_function` | Validate hook code |
| 57 | `get_hook_documentation` | Hook docs |
| 58 | `get_hook_examples` | Hook examples |
| 59 | `get_hook_common_patterns` | Common hook patterns |
| 60 | `get_hook_requirements_documentation` | Requirements docs |

### Element Cloning - Complete (6)
| # | Tool | Description |
|---|------|-------------|
| 61 | `clone_element_complete` | Full clone (inline) |
| 62 | `clone_element_progressive` | Lightweight + expand later |
| 63 | `clone_element_to_file` | Clone to JSON file |
| 64 | `extract_complete_element_cdp` | CDP-native extraction |
| 65 | `extract_complete_element_to_file` | CDP extraction to file |
| 66 | `list_clone_files` | List saved clone files |

### Element Cloning - Granular (12)
| # | Tool | Description |
|---|------|-------------|
| 67 | `extract_element_styles` | CSS styles |
| 68 | `extract_element_styles_cdp` | CDP-native styles |
| 69 | `extract_element_styles_to_file` | Styles to file |
| 70 | `extract_element_structure` | DOM structure |
| 71 | `extract_element_structure_to_file` | Structure to file |
| 72 | `extract_element_events` | Event listeners |
| 73 | `extract_element_events_to_file` | Events to file |
| 74 | `extract_element_animations` | CSS animations |
| 75 | `extract_element_animations_to_file` | Animations to file |
| 76 | `extract_element_assets` | Images/fonts/etc |
| 77 | `extract_element_assets_to_file` | Assets to file |
| 78 | `extract_related_files` | Related CSS/JS files |

### Progressive Expansion (6)
| # | Tool | Description |
|---|------|-------------|
| 79 | `expand_styles` | Expand stored element styles |
| 80 | `expand_children` | Expand child elements |
| 81 | `expand_css_rules` | Expand CSS rules |
| 82 | `expand_events` | Expand event listeners |
| 83 | `expand_pseudo_elements` | Expand pseudo elements |
| 84 | `expand_animations` | Expand animations |

### Element Storage (4)
| # | Tool | Description |
|---|------|-------------|
| 85 | `list_stored_elements` | List stored elements |
| 86 | `get_element_state` | Get stored element state |
| 87 | `clear_stored_element` | Clear one element |
| 88 | `clear_all_elements` | Clear all elements |

### Debug & Server (9)
| # | Tool | Description |
|---|------|-------------|
| 89 | `get_debug_view` | Debug logs/stats |
| 90 | `clear_debug_view` | Clear debug logs |
| 91 | `get_debug_lock_status` | Debug lock info |
| 92 | `export_debug_logs` | Export debug logs |
| 93 | `hot_reload` | Reload server modules |
| 94 | `reload_status` | Module load status |
| 95 | `validate_browser_environment_tool` | Environment check |
| 96 | `cleanup_clone_files` | Clean old files |
| 97 | `get_function_executor_info` | Function executor state |

---

## Test Results - Round 1 (2026-05-20)

### PASSED (43/97)

| # | Tool | Notes |
|---|------|-------|
| 1 | `spawn_browser` | Instance created, profile cloned from master-snapshot |
| 2 | `list_instances` | Correct count, states accurate |
| 3 | `validate_browser_environment_tool` | Chrome found, no issues, is_ready=true |
| 4 | `reload_status` | All 6 modules loaded (browser_manager, network_interceptor, dom_handler, debug_logger, models, persistent_storage) |
| 5 | `navigate` | httpbin.org loaded (example.com had DNS failure) |
| 6 | `take_screenshot` | Saved to file, correct message |
| 7 | `get_page_content` | HTML + text content returned, Moby-Dick text correct |
| 8 | `get_active_tab` | Correct tab_id, url, title, type |
| 9 | `list_tabs` | 1 tab initially, 2 after new_tab |
| 10 | `query_elements` | Found h1, returned tag_name, text, bounding_box, attributes |
| 11 | `execute_script` | Works when using expression syntax (no `return` keyword) |
| 12 | `call_javascript_function` | `document.querySelector("h1")` returned object |
| 13 | `get_cookies` | Returned all cookies; filtered by URL correctly |
| 14 | `set_cookie` | Set `test_cookie=hello_from_mcp` on httpbin.org |
| 15 | `clear_cookies` | Cleared httpbin.org cookies |
| 16 | `list_network_requests` | 8 requests captured (including data URIs from chrome error page) |
| 17 | `search_network_requests` | Filtered by url_pattern `httpbin`, got 2 results with status codes |
| 18 | `get_network_capture_filters` | Empty include/exclude (no filters set) |
| 19 | `new_tab` | Opened httpbin.org/get, returned tab_id |
| 20 | `switch_tab` | Switched back to first tab |
| 21 | `close_tab` | Closed second tab |
| 22 | `scroll_page` | Scrolled down 300px |
| 23 | `go_back` | Navigated back |
| 24 | `go_forward` | Navigated forward |
| 25 | `reload_page` | Reloaded current page |
| 26 | `clone_element_progressive` | Got element_id `elem_ea939b57ae2b` for h1 |
| 27 | `expand_styles` | Returned styles (empty for plain h1) |
| 28 | `expand_children` | 2 children found |
| 29 | `expand_events` | No events (expected for plain h1) |
| 30 | `list_stored_elements` | 1 element stored with metadata |
| 31 | `clear_stored_element` | Cleared successfully |
| 32 | `get_execution_contexts` | Main context, origin=httpbin.org |
| 33 | `get_function_executor_info` | Version 1.0.0, 4 capabilities, 21 CDP commands |
| 34 | `create_persistent_function` | `window.getPageInfo` created, survives reloads |
| 35 | `call_javascript_function` (persistent) | Called `window.getPageInfo()`, returned {title, url, h1} |
| 36 | `list_cdp_commands` | 21 Runtime domain commands listed |
| 37 | `list_dynamic_hooks` | Empty (none created yet) |
| 38 | `get_debug_view` | 0 errors, 0 warnings, 0 info |
| 39 | `get_debug_lock_status` | No lock held |
| 40 | `hot_reload` | Reloaded 5 modules (but see BUG B4) |
| 41 | `cleanup_clone_files` | 0 deleted (none old enough) |
| 42 | `list_clone_files` | Empty list |
| 43 | `discover_global_functions` | Auto-saved to file (response too large: 2M tokens) |

### BUGS FOUND (4)

#### B1: `get_instance_state` - Partial data + crash (MEDIUM)
- **Symptom**: Returns basic info (instance_id, state, url) but `detail_error: "Failed to collect full page state: Exception: Failed to get page state: 'list' object has no attribute 'get'"`
- **Impact**: Can't get full instance state; only partial info returned
- **Likely cause**: Code assumes a dict somewhere but gets a list (possibly tabs/cookies response)

#### B2: `execute_cdp_command` - Unknown command format (MEDIUM)
- **Symptom**: Both `getHeapUsage` and `Runtime.getHeapUsage` return "Unknown CDP command"
- **Impact**: Can't execute raw CDP commands
- **Note**: `list_cdp_commands` returns names like `getHeapUsage` (no domain prefix), but that format doesn't work either

#### B3: `wait_for_element` - Type validation on timeout param (LOW)
- **Symptom**: `'5000' is not of type 'integer'` when passing timeout
- **Impact**: Can't set custom timeout (default may still work)
- **Likely cause**: MCP schema sends integer as string, server-side validation rejects it

#### B4: `hot_reload` - Disconnects active browser instances (HIGH)
- **Symptom**: After hot_reload, active instance becomes "stored", URL resets to `chrome://newtab/`, subsequent tool calls fail with "Instance not found"
- **Impact**: Any hot_reload during active work kills browser connections
- **Likely cause**: Module reload clears in-memory instance registry but doesn't re-register active connections

### NOT YET TESTED (~54 tools)

#### Form Interaction (needs form page)
- `type_text`, `paste_text`, `press_key`, `select_option`, `upload_file`

#### Network Deep
- `get_request_details`, `get_response_details`, `get_response_content`
- `modify_headers`, `set_network_capture_filters`
- `export_network_data`, `import_network_data`

#### Dynamic Hooks
- `create_dynamic_hook`, `create_simple_dynamic_hook`
- `get_dynamic_hook_details`, `remove_dynamic_hook`
- `validate_hook_function`
- `get_hook_documentation`, `get_hook_examples`, `get_hook_common_patterns`, `get_hook_requirements_documentation`

#### Element Extraction (complete)
- `clone_element_complete`, `clone_element_to_file`
- `extract_complete_element_cdp`, `extract_complete_element_to_file`

#### Element Extraction (granular)
- `extract_element_styles`, `extract_element_styles_cdp`, `extract_element_styles_to_file`
- `extract_element_structure`, `extract_element_structure_to_file`
- `extract_element_events`, `extract_element_events_to_file`
- `extract_element_animations`, `extract_element_animations_to_file`
- `extract_element_assets`, `extract_element_assets_to_file`
- `extract_related_files`

#### Progressive Expansion
- `expand_css_rules`, `expand_pseudo_elements`, `expand_animations`

#### Element Storage
- `get_element_state`, `clear_all_elements`

#### Advanced JS
- `inject_and_execute_script`, `execute_function_sequence`
- `inspect_function_signature`, `discover_object_methods`
- `create_python_binding`, `execute_python_in_browser`

#### Debug/Server
- `export_debug_logs`, `clear_debug_view`, `close_instance`

---

## Known Issues - Hanging/Stalling

### SafeMeet Session Analysis (`34a130b4`) — CORRECTED

**Initial analysis was incorrect.** The first pass found 36 "hanging" tool_use entries without
matching tool_result, but deeper investigation revealed this was a transcript parsing error:

- **Total stealth MCP tool_use entries**: 388
- **Resolved (matched tool_result)**: 0 — because tool results in Claude Code JSONL transcripts
  are embedded inside `user` message content blocks, NOT as standalone `type: "tool_result"` entries
- **Context compactions**: 370 lines — compaction drops old tool results from the transcript
- **Session interrupts**: 226 lines — user Ctrl+C or session crashes

**Conclusion**: The JSONL transcript format does NOT reliably show whether MCP tool calls
completed or hung. Tool results are embedded in `user` messages and get dropped during
context compaction. The "36 hanging calls" were simply tool_use entries after the first
compaction (line 810) whose results were compacted away.

### What We CAN Say

From the SafeMeet session, the user reported that the MCP "just stays there" during use.
Possible causes (all still hypothetical without server-side logging):

1. **SPA load events** — `navigate` defaults to `wait_until: "load"` which may never fire
   on pages with persistent WebSocket/SSE connections (SafeMeet uses SSE for chat)
2. **CDP connection drops** — Chrome may crash or disconnect without raising an exception
3. **Large profile clones** — `spawn_browser` may block on `shutil.copytree` for large profiles
4. **No asyncio timeouts** — Tool calls may lack hard timeouts on CDP operations

### CONFIRMED: Live Hanging Behavior (SafeMeet session pasted by user)

User pasted an actual session excerpt showing the hang pattern in real-time:

```
1. list_instances → WORKS (returns 5 instances, all "alive")
2. close_instance × 3 → WORKS (closes 3 stale instances)
3. take_screenshot (User A) → HANGS → user presses Escape → Interrupted
4. take_screenshot (User B) → HANGS → user presses Escape → Interrupted
5. execute_script (User A) → HANGS → user presses Escape → Interrupted
6. execute_script (User B) → HANGS → user presses Escape → Interrupted
7. execute_script (User A, health check) → HANGS → Interrupted
8. execute_script (User A, retry) → HANGS → Running 1m 15s+ with no response
```

**Key insight**: Server-internal tools (list_instances, close_instance) respond instantly.
CDP-dependent tools (take_screenshot, execute_script) hang indefinitely. This proves:

- The MCP server process is alive and responsive
- The CDP WebSocket connections to the Chrome instances are DEAD/STALE
- The server has NO timeout on CDP operations — it awaits forever
- `list_instances` checks in-memory state only, not actual CDP connectivity
- User pressing Escape + Claude retrying creates an infinite hang loop

**Root cause**: Stale CDP connections with no timeout enforcement or health-checking.

### Bug Classification

| Bug | Severity | Description |
|-----|----------|-------------|
| **B5** | **CRITICAL** | No asyncio timeout on CDP operations — hangs forever on dead connections |
| **B6** | **HIGH** | `list_instances` reports stale instances as "alive" — no CDP health-check |
| **B7** | **MEDIUM** | No connection recovery — dead CDP connections are never retried or cleaned up |
| **B8** | **LOW** | Escape+retry loop — interrupted tool calls don't signal the server to abort |

### What We Need To Fix

1. **Add `asyncio.wait_for(coro, timeout=30)` to ALL CDP-dependent tool handlers**
   - navigate, take_screenshot, execute_script, click_element, query_elements, etc.
   - On timeout: return error message instead of hanging
2. **Add CDP health-check to `list_instances` and `get_instance_state`**
   - Ping the CDP connection (e.g., `Runtime.evaluate("1+1")`)
   - Mark instances as "stale" or "disconnected" if ping fails
3. **Add connection recovery or auto-cleanup**
   - If CDP ping fails, attempt reconnection
   - If reconnection fails, mark instance as dead and remove from registry
4. **Add per-tool-call cancellation support**
   - When user interrupts, the MCP server should abort the pending CDP operation

---

## Stress Test Plan

### Target Sites
1. **Heavy SPA**: Complex React/Vue app with dynamic content
2. **Form-heavy**: Site with inputs, selects, file uploads
3. **Network-heavy**: Site with many API calls, WebSocket connections
4. **Anti-bot**: Sites with Cloudflare, CAPTCHA challenges
5. **Large DOM**: Sites with thousands of elements

### Scenarios to Test
- [ ] Rapid sequential tool calls (timing/race conditions)
- [ ] Long-running pages (idle timeout behavior)
- [ ] Multiple concurrent instances
- [ ] Large page content extraction
- [ ] Network interception on heavy traffic
- [ ] Element cloning on complex styled components
- [ ] Hook creation and request modification
- [ ] Tab management under load
