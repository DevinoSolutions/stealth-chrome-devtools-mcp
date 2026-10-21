# 2.1.38 headed prove (T-5166, T-5587, F-1009), 2026-10-21

Real isolated HTTP backend from a clean PyPI venv (own HOME/USERPROFILE/session root, NO_AUTO_RECOVERY), driven by an MCP client. Unnamed headed clones only. Raw values in `evidence.json`; screenshots alongside.

## 1. Popup / background tab (#261 / #259)
2.1.38 does NOT contain #261 (popup armed at creation, ships in 2.1.39); the values below are the 2.1.38 behaviour.
- Popup (window.open from click_element), BEFORE switch_tab: {'visibilityState': 'visible', 'hidden': False, 'hasFocus': True, 'rAF_in_3s': 183, 'timer100ms_in_3s': 31}
- Popup AFTER switch_tab back to opener (popup in background): {'visibilityState': 'visible', 'hidden': False, 'hasFocus': False, 'rAF_in_3s': 183, 'timer100ms_in_3s': 31}
- Plain background tab (new_tab, blank tab in front): {'visibilityState': 'visible', 'hidden': False, 'hasFocus': True, 'rAF_in_3s': 5, 'timer100ms_in_3s': 27}
- Note: the background tab reports visible/hasFocus true (focus emulation) and timers run, but rAF ran only 5 times in 3 s against ~183 for the popup window.
- Shots: s1_01_opener, s1_02_popup_before_switch, s1_03_popup_background, s1_04_plain_bg_tab

## 2. HTTP guard (#262, ships in 2.1.39)
2.1.38 raw status codes: normal 200, forged Host evil.example 200, forged Origin http://evil.example 200 (all with an MCP session id: ACCEPTED, as expected on 2.1.38). spawn_browser, navigate, take_screenshot all worked.

## 3. Concurrent clones (F-1009)
Two concurrent spawns in 9.5 s. A: port 8023, pid 141544; B: port 8022, pid 132380. Listener pid equals browser pid for both. Marker read only on own port; neither answer names the other's port or pid. Details: {'distinct_ports': True, 'distinct_pids': True, 'listener_is_browser': True, 'A_marker_only_on_A': True, 'B_marker_only_on_B': True, 'B_answer_names_A_port': False, 'A_answer_names_B_port': False, 'B_answer_names_A_pid': False}. Shots: s3_clone_A_*, s3_clone_B_*.

## 4. Logged-in page (read-only snapshot seed)
Clone seeded from C:\stealth-mcp-browser-sessions\master-snapshot (clone_source default-seed), copy landed under the isolated root (True), snapshot unchanged before/after (True). https://app.sendly.now redirected to https://app.sendly.now/auth/login titled "Sign in | Sendly": password and email inputs present. Result: NOT signed in (login page). No credentials typed. Shot: s4_sendly_app.
