# plan_E2E — Fixture web app + deterministic all-tool E2E suite (user-directed)

- **Status**: APPROVED by human directive 2026-07-09 ("Make a web app it can run to
  interact with all possible elements to test its extensive tools and basically
  check that the right actions are performed, the right information is retrieved
  from the stealth cmd. Use its command line to test deterministic tests.")
- **Position in campaign**: inserted after plan 8/12 (M6, merged in PR #30);
  executes BEFORE M12a. Does not renumber the remaining approved plans
  (M12a → M4-Ph1+A1 → M5b → M14+A1 still queued in that order).
- **Branch**: `audit/fixes-2026-07-02-e2e` off main @ 8a7e55d.
- **Nature**: tests + test fixtures ONLY. Zero production-source changes, zero
  new dependencies, zero CI-workflow changes.

## 0. Goal

M6 pinned tool behavior against **fakes** (hermetic, no Chrome). This plan adds
the complementary net: a **self-contained fixture web app** served locally, and
an integration suite that drives a **real Chrome** through the MCP's own tools
(the "stealth cmd" surface) against that app, asserting two things per tool:

1. **The right action was performed** — every interactive element in the app
   writes an exact entry to an in-page action log; tests assert the log.
2. **The right information was retrieved** — the app embeds fixed ground-truth
   values (texts, styles, JSON payloads, cookies); tests assert tool output
   equals those values exactly.

Deterministic: no external network, no timing-sensitive assertions, fixed
content, everything same-origin against a stdlib HTTP server on 127.0.0.1.

## 1. Scope

### 1.1 Files (all new unless noted)

| File | Purpose |
| --- | --- |
| `tests/fixture_app/index.html` | Hub page: nav links, page sentinel text |
| `tests/fixture_app/interact.html` | Interaction targets + action log |
| `tests/fixture_app/extract.html` | Extraction/cloning ground-truth component |
| `tests/fixture_app/network.html` | fetch/XHR trigger buttons (same-origin) |
| `tests/fixture_app/cookies.html` | cookie/localStorage setters |
| `tests/fixture_app/hooks.html` | global functions + hookable API surface |
| `tests/fixture_app/styles.css` | shared stylesheet (extract_related_files target) |
| `tests/fixture_app/app.js` | `logAction()` helper + page wiring (one shared file) |
| `tests/conftest.py` (EDIT) | session-scoped `fixture_app_server` fixture |
| `tests/test_fixture_app_server.py` | hermetic smoke: server serves pages + API endpoints; no-external-URL guard |
| `tests/test_e2e_interaction.py` | STEP 2 sections (integration marker) |
| `tests/test_e2e_data_tools.py` | STEP 3 sections (integration marker) |
| `tests/test_e2e_functions_hooks.py` | STEP 4 sections (integration marker) + coverage manifest |
| `tests/test_mcp_protocol_surface.py` | hermetic FastMCP protocol-layer tests |

### 1.2 Non-goals (hard OUT)

- **No production-source edits.** Bugs found E2E are characterized with
  `@pytest.mark.characterization` + F-id/finding docstring (M6 convention),
  never fixed here. Known bugs the suite WILL hit and must pin, not fight:
  - `clone_element_complete` does not forward `selector` to the 4 JS
    sub-extractors (M6 finding, routed to M5b) — E2E asserts current shape.
  - Adapter not-found non-uniformity (routed to M4-Ph1).
  - FileBased error-swallow returning success-shaped dicts (F-141, M5b).
- **No new pip dependencies** (stdlib `http.server` + existing `requests`).
- **No CI workflow edits** — the suite rides the existing
  `pytest -m integration` job (Chrome + Xvfb, `--timeout=120`) unchanged.
- **No goldens for live-Chrome output** (Chrome-version-volatile). Goldens stay
  an M6/fakes concept. E2E asserts invariant keys + fixture-pinned exact values.
- Stdio/proxy CLI transport tests — already covered by
  `test_server_entrypoint.py` / `test_cli*.py` / `test_singleton_*.py`.

## 2. Design

### 2.1 Fixture app contract

- **Action log**: `app.js` defines
  `window.__actions = []` and `logAction(kind, id, detail?)` pushing
  `"<kind>:<id>"` (plus `detail` when given) and mirroring into
  `<pre id="action-log">`. Every interactive element wires its natural event to
  `logAction` (`click:btn-counter`, `change:select-single`, `keydown:text-input`,
  `submit:hook-form`, …). Tests read it via `execute_script`
  (`JSON.stringify(window.__actions)`) and assert exact entries/order.
- **Ground truth** (pinned constants; tests import nothing — they repeat the
  literal in the assert so a fixture edit surfaces as a test diff):
  - `#styled-card` (extract.html): `color: rgb(17, 34, 51)`,
    `background-color: rgb(250, 235, 215)`, `padding: 12px`,
    `border: 2px solid rgb(68, 85, 102)`; `::before` content `"FIXTURE-BEFORE"`;
    CSS animation `fixture-pulse 2s` with `animation-play-state: paused`
    (paused ⇒ computed styles never vary mid-test); one `addEventListener`
    click + one mouseover listener; `<img>` with a tiny data-URI PNG and a
    `background-image` data-URI; nested children ≥3 levels deep.
  - `window.calcTotal = function calcTotal(a, b) { return a + b; }` and
    `window.appAPI = { getUser(){…fixed dict…}, setFlag(v){…}, version: "1.0-fixture" }`
    (hooks.html).
  - Select options `alpha|beta|gamma`; checkbox `#check-me`; radio group
    `flavor`; `#text-input`; `#textarea-input`; file inputs `#single-file` /
    `#multi-file`; `#btn-counter` increments `#counter-value` text;
    `#reveal-btn` click ⇒ `setTimeout(200ms)` ⇒ `#delayed-el` becomes visible
    (bounded-wait target for `wait_for_element`, asserted with a ≥5 s tool
    timeout so scheduling jitter can't flake it); tall `#scroll-spacer`.
  - Cookies: `client_cookie=from-js` (button), `tool_cookie` (set by the
    `set_cookie` tool), `fixture_cookie=server-set` (from `/api/set-cookie`).
- **No external URLs anywhere** in `tests/fixture_app/` — enforced by a smoke
  test that greps the fixture files for `http://` / `https://` (relative URLs
  only). This is the determinism backstop.

### 2.2 HTTP server fixture (session-scoped, in `tests/conftest.py`)

- `ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)` on a daemon thread;
  yields `base_url`; `shutdown()` on teardown; `log_message` overridden to
  silence stderr. `_FixtureHandler` = `SimpleHTTPRequestHandler` rooted at
  `tests/fixture_app` (via `directory=`) plus dynamic routes:
  - `GET /api/json` → `200 {"ok": true, "value": 42, "source": "fixture"}`
    (`application/json`)
  - `POST /api/echo` → echoes `{"body": <raw>, "headers": {<lowercased subset>}}`
  - `GET /api/set-cookie` → `Set-Cookie: fixture_cookie=server-set; Path=/`
  - `GET /redirect` → `302 Location: /api/json`
- Ephemeral port ⇒ no collisions locally or in CI; same-origin fetches from the
  pages use **relative** URLs so the port never appears in fixture files.

### 2.3 E2E test pattern (chunky section-walks)

- Conventions copied from `tests/test_browser_integration.py`: importlib load of
  `embedded/server.py`, `.fn` unwrap, `pytestmark = pytest.mark.integration` +
  Chrome-availability skip guard, `_sandbox_kwargs()` for root/container/CI.
- **One spawn per test function** (nodriver binds websockets to the running
  loop; pytest-asyncio auto mode gives function-scoped loops — a shared
  module-scoped browser is a cross-loop hazard, so it is banned here). To keep
  runtime sane, tests are **chunky**: each test spawns once (headless), walks
  5–12 related tools in sequence with asserts, and closes in `finally`.
  Target ≤16 spawning tests total; integration job stays well under its
  current shape (+~2–3 min worst case).
- Each test starts with `navigate` to the page it needs — a fresh load resets
  `window.__actions`, so no cross-test log bleed. Tests that mutate browser-wide
  state (cookies, capture filters) reset it themselves (`clear_cookies`,
  restore filters) — which doubles as coverage of those tools.
- Network assertions NEVER count total requests (Chrome background noise);
  they filter by our URL substrings (`/api/json`, `/api/echo`) only. Body
  capture is explicitly enabled per-instance via
  `set_network_capture_filters(capture_bodies=True)` (post-M9 the default is
  OFF — this suite is also the live proof of the M9 opt-in path).

### 2.4 Coverage manifest (tripwire)

`tests/test_e2e_functions_hooks.py` ends with `test_e2e_coverage_manifest`:
builds the live 94-name set from `server.SECTION_TOOLS`, asserts
`E2E_COVERED | E2E_EXEMPT == all_names` and the two sets are disjoint.
`E2E_EXEMPT` is a literal `{name: "reason"}` dict — every exemption must carry a
reason (e.g. `hot_reload`-family: unit-covered server-lifecycle tools;
`import_network_data`: exercised hermetically in `test_network_interceptor.py`).
Adding tool #95 without deciding its E2E story breaks this test — same
philosophy as the F-108 pin. Aim: exemptions ≤10 of 94.

### 2.5 Protocol-surface tier ("use its command line", hermetic)

`tests/test_mcp_protocol_surface.py` (NOT integration-marked): drives tools
through the **FastMCP layer** (schema validation + serialization) instead of the
raw `.fn` seam, with M6 fakes patched in — the layer nothing currently tests.
Preferred mechanism: `fastmcp.Client(server.mcp)` in-memory transport
(fastmcp==2.11.2; executor verifies the exact API against the installed
package first). Fallback if the in-memory client fights the harness:
`(await server.mcp.get_tools())[name].run(arguments={...})`, which still
exercises schema validation. ~6 tests: happy-path `list_instances` via
protocol == seam result; missing required param → validation error (not a
crash); wrong-type param → validation error; sync hook-doc tool via protocol;
tool-not-found error shape; result serialization of a dict-returning tool.

### 2.6 Determinism rules (suite-wide, enforced in review)

1. No `sleep`-then-assert. Bounded polling loops (deadline + short interval)
   or tool-native waits only.
2. No assertions on wall-clock durations except generous upper bounds already
   conventional in this repo (e.g. "did not hang").
3. No dependence on animation/transition progress — the fixture pauses its
   animation; computed-style asserts use paused values.
4. No external hosts; no DNS; every URL is `base_url`-relative or `data:`.
5. No screenshot pixel asserts — PNG magic bytes + nonzero size only.
6. Every spawned instance closed in `finally`; every written file under
   `tmp_path` or the conftest-redirected clone-output dir.

## 3. Steps (one commit per step, orchestrator commits)

- **STEP 1 — fixture app + server fixture + hermetic smoke.**
  All `tests/fixture_app/*`, the `fixture_app_server` fixture, and
  `test_fixture_app_server.py` (server serves every page with 200 + sentinel,
  API routes respond exactly as §2.2, no-external-URL guard, action-log helper
  present in `app.js`). Runs in the unit job (no marker). DoD: smoke green
  hermetically; fixture files are plain ASCII; zero external URLs.
- **STEP 2 — `test_e2e_interaction.py`**: browser-management (8),
  tabs (5), element-interaction (12), cookies-storage (3) — each tool called
  ≥1× against the fixture app with action-log/ground-truth asserts.
  (upload_file: reuse existing pattern against `#single-file`/`#multi-file`;
  navigation history via go_back/go_forward across index→interact.)
- **STEP 3 — `test_e2e_data_tools.py`**: network-debugging (10, with
  capture_bodies opt-in ON; body of `/api/json` parses to `value == 42`;
  export to `tmp_path`; modify_headers asserted via `/api/echo` echo — if the
  tool's current implementation doesn't propagate, PIN the current behavior
  with a characterization marker and record the finding), element-extraction
  (9), progressive-cloning (10, one `clone_element_progressive` then every
  `expand_*` against the stored id, then list/clear), file-extraction (9, files
  land in the redirected clone-output dir; list + cleanup asserted).
- **STEP 4 — `test_e2e_functions_hooks.py` + protocol tier + manifest**:
  cdp-functions (13; `execute_python_in_browser` may lack the optional `py2js`
  extra — assert the graceful-error shape if missing, skip-if-import only as
  last resort), dynamic-hooks (10; create → trigger via page call → details
  show effect → remove; 4 doc tools = non-empty-string asserts), debugging (5),
  `test_mcp_protocol_surface.py`, and the §2.4 coverage manifest.

## 4. Verification

- Local (Windows, THE only sanctioned invocation):
  - `& ".venv\Scripts\python.exe" -m pytest tests/test_fixture_app_server.py tests/test_mcp_protocol_surface.py -q` (hermetic tiers)
  - `& ".venv\Scripts\python.exe" -m pytest -m integration tests/test_e2e_interaction.py tests/test_e2e_data_tools.py tests/test_e2e_functions_hooks.py -q` (real Chrome, headless)
  - full `-m "not integration"` suite stays green (662 + new hermetic tests)
- Determinism proof: run each new E2E file twice back-to-back; zero flakes.
- Gates: ruff format/check clean; no src file touched (git diff confirms);
  vulture/ty unaffected (tests excluded); file budgets untouched.
- CI: existing 5 checks; integration job now also runs the new E2E files.

## 5. Risks & mitigations

- **Chrome noise requests** → §2.3 URL-substring filtering only.
- **Timing flake** (wait_for_element, close latency) → fixture reveal is
  200 ms vs ≥5 s tool timeout; no other timing-coupled asserts.
- **Loop binding** → one spawn per test function, chunky walks (§2.3).
- **CI runtime** → ≤16 spawning tests; if the integration job exceeds ~8 min
  in practice, split is a follow-up, not a blocker.
- **Windows/Linux path differences** in file-extraction asserts → assert via
  `Path` operations, never string prefixes.
- **Port reuse** → ephemeral port per session; base_url threaded through
  fixtures, never hardcoded.

## 6. Findings interplay

- Proves live: M9 capture opt-in (OFF default ⇒ no bodies; ON ⇒ bodies), M9
  byte-cap knobs untouched (unit-covered), F-108 (94) via the manifest.
- Expected characterization pins (do NOT fix): M5b selector-forwarding gap in
  `clone_element_complete`; F-141 FileBased error-swallow; M4 adapter
  non-uniformity; anything new → `@pytest.mark.characterization` + docstring
  F-id/description, and enumerate in the completion report for routing to
  M4-Ph1 / M5b / M14+A1.
