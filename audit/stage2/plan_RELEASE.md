# plan_RELEASE (E2E-9) — Release-gate hardening: real-transport E2E, cross-platform CI, install smoke, stealth verification, release contract

- **Status**: **APPROVED-IN-PRINCIPLE — all §7 decisions resolved (human,
  2026-07-15); queued behind the audit FIX pipeline.** Not yet started; enters the
  serial FIX queue after M4-Ph1+A1 → M5b → M14+A1 land (§ Position). Drafted
  2026-07-14 by the orchestrator in answer to the directive: *"what is remaining to
  get stealth-mcp stable into the market with E2E tests that guarantee that if they
  pass, it'll be working perfectly for the user."* This plan is the bridge from
  **audit-clean** to **market-shippable**. It is written to be executed by an
  **Opus High-effort subagent** one workstream at a time. The four release choices
  (three-OS gating, online detectors, smoke-blocks-publish, contract location) are
  **settled** — see §7.
- **Position in campaign**: executes **AFTER** the audit FIX pipeline lands
  (M4-Ph1+A1 → M5b → M14+A1) and its PRs merge through the human merge-gate, and
  **before** (or folded into) CODIFY. It depends on the known user-facing bugs
  being *either fixed by M5b/M14 or consciously accepted as documented
  limitations* — this plan does **not** fix product bugs (§1.2), it **gates and
  documents** them.
- **Branch**: `release/e2e-gate` off `main` at the post-M14 merge SHA (executor
  pins the exact SHA at start; re-anchors every claim at HEAD per house rule).
- **Nature**: tests + test fixtures + **CI workflow changes** + docs. The one
  plan in the campaign that is *allowed* to edit `.github/workflows/*` and add new
  pytest markers (`transport`, `stealth`, `perf`). **Zero `src/` production edits** (same hard rule as
  plan_E2E — any bug this suite hits is pinned with `@pytest.mark.characterization`
  and routed, never fixed here). No new *runtime* dependencies; new **test-extra**
  deps only, and only if unavoidable (§1.3).

---

## 0. Why this plan exists — the guarantee gap, stated precisely

The audit pipeline makes the code **internally correct and maintainable**. The
existing E2E suite (E2E-7/E2E-8: `tests/test_e2e_*.py`,
`tests/test_mcp_protocol_surface.py`, `tests/fixture_app/`) is strong but has
four structural gaps between "the suite is green" and "if it passes, it works
for the user":

| # | Gap | Today | Consequence |
|---|---|---|---|
| G-A | **Transport** | E2E drives tools via the in-process `.fn` seam and an *in-memory* FastMCP client (`test_mcp_protocol_surface.py`). | The real wire path a user gets — a client spawning the server over **stdio JSON-RPC**, `initialize` handshake, `tools/list`, `tools/call` — is **never exercised end-to-end against real Chrome**. A serialization/handshake/entrypoint break passes CI. |
| G-B | **Platform** | CI is **`ubuntu-latest` only** (unit matrix py3.11–3.13 + one Chrome/Xvfb integration job). | The user's own platform is **Windows**, which has **zero CI coverage**. Chrome discovery, profile/temp paths, the singleton port logic, and process handling are all OS-sensitive. macOS is also uncovered. |
| G-C | **Packaging** | `publish.yml` runs **unit-only** tests on ubuntu, then `uv build` + publish. | Nothing verifies that a **clean `uvx` / `pip install` of the built wheel** can actually spawn the server and drive a browser. A missing package-data file, broken console-script, or bad dependency pin ships green. |
| G-D | **Stealth** | The product's headline promise ("undetectable by anti-bot systems") is **asserted nowhere**. | A regression that reintroduces `navigator.webdriver`, a CDP leak, or a fingerprint tell ships undetected. |

This plan closes G-A…G-D and pushes the guarantee **as close to "works perfectly
on any site forever" as is honestly reachable** — then names, precisely, the wall
where the literal absolute stops being provable and starts requiring a cheat
(§8). It does this on two tiers: a **proven, absolute** guarantee for the surface
under our control (our code, protocol, fixtures, pinned Chrome versions —
verified as *properties*, not single cases: §W1–W5, W7 fuzz), and an
**asymptotic, monitored** guarantee for the open web (a growing real-site corpus,
structural fuzz coverage, and a continuous canary that catches any regression
within one cycle: §W6–W7). The user-facing **release contract** (§W5) states both
tiers and the residual gap in plain words — the strongest claim that is *true*
(§8.2), with the specific dishonest shortcuts enumerated and refused (§8.1).

### 0.1 Confirmed anchors (re-open at HEAD before writing)

| Claim | Verified 2026-07-14 |
|---|---|
| Console scripts | `pyproject.toml:47-49` — `stealth-chrome-devtools-mcp = "stealth_chrome_devtools_mcp.server:main"` (the MCP server; **stdio default**), `stealth-chrome-devtools = "...cli:main"`. |
| Transports | server supports stdio (default) **and** `--transport http` (loopback-default, **unauthenticated** — `tests/test_server_entrypoint.py`). Market-relevant: the HTTP surface has no auth by design; the contract (§W5) must state this. |
| FastMCP available as runtime dep | `pyproject.toml` deps: `fastmcp==2.11.2`. `fastmcp.Client` with a **stdio** transport can spawn the console script as a subprocess — **no new dependency** needed for W1 (executor verifies the exact 2.11.2 `Client`/stdio-transport API against the installed package before coding). |
| Existing markers | `pyproject.toml:75-78` — only `integration`, `characterization`. This plan **adds `transport`, `stealth`, and `perf`**. |
| CI shape | `.github/workflows/test.yml` — `unit-tests` (ubuntu, py3.11/3.12/3.13, `-m "not integration"`, `--cov-fail-under=55`), `quality` (ruff/ty/vulture/owners/budgets), `integration-tests` (ubuntu, google-chrome-stable + Xvfb, `-m integration --timeout=120`). All `runs-on: ubuntu-latest`. |
| Publish shape | `.github/workflows/publish.yml` — on `v*` tag: unit-only tests (ubuntu, py3.11–3.13) → `uv build` → PyPI publish → GH release advertising `uv tool install` / `uvx`. **No install smoke.** |
| Package classifier | `pyproject.toml` — `Development Status :: 5 - Production/Stable`, version `1.2.0`, already on PyPI. The "Production/Stable" claim is what this plan makes *true under test*. |
| Existing E2E coverage manifest | `test_e2e_functions_hooks.py::test_e2e_coverage_manifest` asserts `E2E_COVERED | E2E_EXEMPT == SECTION_TOOLS` (the F-108 tripwire pattern). W5 mirrors this for the transport tier. |

### 0.2 Acceptance principle — manual-QA parity (the "blind push" bar)

The bar the whole plan is held to: **a green RELEASE gate must be a faithful,
complete stand-in for the manual test pass a human would otherwise run before
shipping.** Every step you'd take by hand — install it into an MCP host, spawn a
browser, log into a real Cloudflare-fronted site, fill the awkward inputs, confirm
it isn't detected, extract data, **watch that each action returns quickly and the
process doesn't balloon in memory**, **see it recover cleanly when the browser
crashes under you**, close without orphaning a process — has a *named* automated
counterpart. When that holds, green ⇒ no human click-through needed; you push on
green. The three pillars of that hand-pass — **it works** (W1–W8), **it's fast and
lean** (W9), **it fails safe** (W10) — are each machine-checked.

Three properties make "green ⇒ blindly pushable" **true** rather than aspirational.
Skipping any one turns blind-push back into a leap of faith — itself the subtle
cheat §8 warns against:

1. **Parity-complete** — the manual protocol is written down (W8) and a tripwire
   fails CI if any manual step lacks a **live** (collected, not skip/xfail)
   automated test. You cannot silently forget to automate a step, or quietly
   downgrade one to skip.
2. **Flake-free** — a gate you trust blindly cannot be flaky; one flake means green
   no longer means "works." W8 makes flake a zero-tolerance quarantine event and
   keeps the two-run determinism proof (§4).
3. **Mutation-proven** — high line coverage with weak asserts catches nothing (the
   `assert True` family, §8.1). Mutation testing injects deliberate bugs into
   `src/` and requires the suite to go red; the **mutation-kill score** is the
   honest number behind "near-guarantee." Coverage says the line *ran*; mutation
   says a bug in it would have been *caught*.

This is the concrete, non-cheating form of the §6 asymptote tier: not "works on any
site forever," but **"every edge case a human would manually verify is verified for
you, and the green is trustworthy enough to push on."**

---

## 1. Scope

### 1.1 Workstreams (each = an independently executable, independently mergeable unit; one checkpoint commit per step)

| WS | Deliverable | Closes gap | Depends on |
|---|---|---|---|
| **W1** | Real **stdio-transport E2E** suite: a client subprocess-spawns the packaged server, does the MCP handshake, and drives a full user journey against real Chrome + the existing fixture app. | G-A | fixture app (exists) |
| **W2** | **Cross-platform CI**: Linux + Windows + macOS unit matrix; Windows **and macOS** integration + transport jobs with real Chrome — all three OSes **mandatory and gating** (human ruling 2026-07-15: the release claim is "works on Linux, Windows and macOS"). | G-B | W1 (so the new OS jobs run the transport suite) |
| **W3** | **Install-smoke**: build wheel → clean-env `uvx`/`pip install` → spawn → run the W1 minimal journey → assert. Wired into `publish.yml` (release gate) and a CI dry-run. | G-C | W1 |
| **W4** | **Stealth verification**: (a) *deterministic offline tier* — fixture page replicating key detection probes, gating; (b) *online tier* — real detector sites, opt-in/non-gating/informational. New `stealth` marker. | G-D | W1 pattern |
| **W5** | **Release contract** (`RELEASE_CONTRACT.md`) + **known-limitations register** + **transport coverage manifest** tripwire test. | ties all | W1, W4 |
| **W6** | **Continuous verification**: scheduled canary runs against a growing live real-site corpus + rotating detectors; regression alerting; auto-captured minimal repros (HAR + DOM + screenshot). Converts a point-in-time green suite into a continuously-proven one. | the **"forever"** axis | W1, W4 |
| **W7** | **Adversarial breadth**: property-based DOM-shape fuzzing (shadow DOM, nested iframes, CSP, SPA route swaps, lazy-load, canvas/WebGL), a curated real-site smoke corpus, a **Chrome-version matrix**, and **differential stealth** (stealth build vs vanilla headless). Converts "one fixture" into structural coverage of the interaction/detection feature space. | the **"any site"** axis | W1, W4 |
| **W8** | **Manual-QA parity** (the capstone mechanism): the manual sign-off protocol written as an enumerated manifest, each step mapped to a live E2E test + a parity tripwire; the lifecycle/leak/recovery and multi-instance-concurrency steps a human checks by hand; **mutation testing** proving the suite bites; and zero-flake quarantine discipline. Establishes the manifest + tripwire; **W9–W11 extend it** (each lands its MQ step + test together, so the tripwire stays green). Binds W1–W11 to the "blind push" bar (§0.2). | **green ⇒ blind push** | W1–W7 |
| **W9** | **Performance & resource budgets**: deterministic per-tool latency budgets and a session-wide memory/handle ceiling on the fixture app (gating), a large-payload stress tier, and a perf-regression baseline lane in the canary. Turns "correct" into "correct **and fast enough**" — the pillar W1–W8 never assert. | **"great performance"** | W1 |
| **W10** | **Resilience / fault-injection**: kill Chrome mid-session, close a tab under an active tool, navigate to a hanging endpoint, drop the network mid-op — assert each yields a **typed** error (never a hang or silent wrong-value) and the server **recovers** (next spawn/op works). The dynamic half of edge-case coverage that the static negative-space steps (W8) don't reach. | dynamic **edge cases** | W1 |
| **W11** | **Documentation-example tests**: every runnable code block in `README.md` / `docs/` is extracted and executed against the real server, so the first thing a user copy-pastes cannot silently rot. | **docs never lie** | W1, W5 |

### 1.2 Non-goals (hard OUT) — same discipline as plan_E2E §1.2

- **No `src/` production edits.** Every bug the transport/stealth suite hits is
  pinned `@pytest.mark.characterization` + F-id docstring and routed, never fixed
  here. Bugs known-open at execution time (unless closed by M5b/M14 first):
  E8-1 (`select_option` const-redeclaration silent no-op → returns True),
  E8-2 (click on disabled control returns True), E8-3 (`clear_first` bypasses
  readonly), E8-4 (range/color/date inputs unreachable), E7-1 (`type_text`
  no-clear contenteditable), E7-6 (`get_element_state` attrs-not-props),
  F-181 (stale-document-node −32000), F-165 (dup header loop), close-path flake.
  These become **documented known limitations** in §W5, and the transport E2E
  **pins their current behavior** so a later fix surfaces as a deliberate test
  update.
- **No new tools.** The absence of `dblclick` / `right-click` / `drag` /
  native-dialog tools (E2E-8 census) is a **documented limitation** in §W5, not
  built here.
- **No new runtime dependencies.** W1 uses `fastmcp.Client` (already a runtime
  dep). Any test-only helper goes under the `test` extra.
- **No security re-architecture.** The unauthenticated HTTP transport and the
  `create_python_binding` / exec trust boundary (M12b, REJECTED at triage) are
  **documented**, not changed.
- **No live-web goldens.** Stealth online tier asserts **invariants**
  (`navigator.webdriver === false`, no `cdc_`/CDP-leak globals, UA consistency),
  never a detector's numeric score (vendor-volatile).

### 1.3 Dependency policy

Prefer **zero** new deps. If W1 genuinely cannot drive stdio via `fastmcp==2.11.2`
`Client` (executor proves this against the installed package first), the fallback
is the official `mcp` SDK client **added only to the `test` extra**, never to
runtime `dependencies`. Adding anything to runtime `dependencies` is a **STOP**
condition — flag to team-lead.

---

## 2. Design

### 2.1 W1 — real stdio-transport E2E (`tests/test_e2e_transport.py`, `integration` + `transport` markers)

- **Client**: `fastmcp.Client` over a **stdio transport** that runs the
  **installed console script** `stealth-chrome-devtools-mcp` (NOT `python -m` on
  the source tree — the point is to exercise what a user's MCP host launches).
  Executor confirms the exact 2.11.2 API for constructing a stdio/command
  transport; documents the chosen call in the test module docstring.
- **Handshake assertions** (the bytes a real MCP host relies on): `initialize`
  succeeds and returns server name/version; `tools/list` returns the full tool
  set and every entry has a well-formed JSON schema (no `$ref` explosions, no
  missing `inputSchema`); a `tools/call` result deserializes to the same shape
  the `.fn` seam returns for one representative tool (parity check vs
  `test_mcp_protocol_surface.py`).
- **The user journey** (one real Chrome, headless, driven entirely through
  `tools/call` against the §plan_E2E fixture app served on loopback):
  `spawn_browser` → `navigate` (fixture index) → `list_tabs`/`get_active_tab` →
  `click_element`/`type_text`/`select_option` on the interaction page (assert the
  in-page action log via `execute_script`) → `get_page_content` /
  `get_element_state` (assert fixture ground-truth) → one extraction
  (`extract_element_structure` or a `clone_element_*`) → `take_screenshot`
  (PNG magic bytes + nonzero) → `close_instance`. This is the **canonical
  journey** W3 reuses for install-smoke.
- **Determinism**: same rules as plan_E2E §2.6 (loopback fixture only, bounded
  waits, PNG magic-bytes not pixels, every instance closed in `finally`,
  subprocess killed in teardown even on failure). The **one** spawn-per-test rule
  applies. Target ≤6 transport tests (handshake, schema-validity, parity, the
  journey, an error-shape round-trip, a graceful-shutdown check) so the added CI
  time is bounded (~+2–3 min).
- **Timeouts**: the subprocess spawn + handshake gets a generous bound (assert
  *does-not-hang*, never a tight duration). A hung handshake must fail fast with a
  captured stderr dump, not a CI-wall-clock timeout — wrap the client in an
  `asyncio.wait_for` with a clear failure message including the child's stderr.

### 2.2 W2 — cross-platform CI (`.github/workflows/test.yml` edit)

- **Unit matrix** → add `os: [ubuntu-latest, windows-latest, macos-latest]` ×
  the existing python-version matrix. Keep the coverage gate on **ubuntu only**
  (coverage numbers are OS-invariant; running `--cov-fail-under` thrice triples
  runtime for no signal) — other OSes run `-m "not integration"` without cov.
- **New Windows integration+transport job**: `runs-on: windows-latest`. Chrome is
  preinstalled on the GH Windows image (executor verifies the path / uses
  `browser-actions/setup-chrome` pinned by SHA if not); **no Xvfb** (Windows has
  a session display — run headless). Runs `-m "integration"` **and**
  `-m "transport"`. This is the job that finally covers the **user's own
  platform**.
- **macOS integration+transport job — MANDATORY** (human ruling 2026-07-15, §7
  decision 1 resolved): same shape, `runs-on: macos-latest`, Chrome via a pinned
  `setup-chrome` (or the preinstalled Chrome on the macOS image — executor
  verifies which is present and pins accordingly), no Xvfb. Runs
  `-m "integration"` **and** `-m "transport"`, gating, same footing as the
  Windows job. This is what backs the release claim "works on Linux, Windows
  and macOS" — a claim we only make because all three run the same real-Chrome
  suite on every PR.
- **Path-portability sweep** (verification, not code): the executor greps the new
  and existing E2E tests for POSIX-only assumptions (`/tmp`, forward-slash string
  prefixes, `:99` DISPLAY) and confirms they use `tmp_path`/`Path` and skip-guard
  `DISPLAY` only under Linux. Any Windows-only failure that surfaces is a
  **characterization pin + route**, not a src fix.
- **Env**: the Linux integration job keeps `STEALTH_MCP_BROWSER_SESSION_ROOT=/tmp/...`;
  the Windows job sets an equivalent under `RUNNER_TEMP`.

### 2.3 W3 — install smoke (`.github/workflows/publish.yml` edit + a reusable script)

- **Reusable step** `tools/install_smoke.py` (or a composite action): in a
  **clean throwaway environment**, `uvx stealth-chrome-devtools-mcp==<built
  version>` (or `pip install` the just-built wheel from `dist/`), then run the
  **W1 canonical journey** headless against a locally-served fixture copy, assert
  success, exit non-zero on any failure.
- **Wire into `publish.yml`** as a **required, hard-blocking job before `publish`**
  (`needs:`), on `ubuntu-latest` + `windows-latest` + `macos-latest` — all three
  gating (human ruling 2026-07-15, §7 decision 3 RESOLVED = **block, not warn**):
  the "works on Linux, Windows and macOS" claim covers the **install path** too,
  so a wheel that installs-and-runs on only two of the three (or fails smoke on
  any) **blocks the release tag** and nothing publishes. There is no warn-and-ship
  path — a red smoke means no PyPI upload.
- **CI dry-run**: add the same smoke as a non-blocking `workflow_dispatch` /
  nightly job in `test.yml` so packaging breakage is caught before tag time, not
  at release.
- **What it catches**: missing `embedded/js` package data (the `pyproject.toml`
  wheel-packaging note warns about exactly this), console-script breakage, a bad
  runtime pin, `requires-python` drift.

### 2.4 W4 — stealth verification (`tests/test_stealth.py`, new `stealth` marker)

Two tiers, because stealth is the one place determinism and realism genuinely
conflict:

- **Offline/deterministic tier (GATING)** — `integration` + `stealth`. A new
  fixture page `tests/fixture_app/stealth_probe.html` runs the *stable, local*
  half of the standard bot-detection probes in-page and writes results to the
  action log: `navigator.webdriver`, presence of CDP-leak globals
  (`window.cdc_*`, `$cdc_`, `__driver_evaluate`, etc.), `navigator.plugins`
  non-empty, `navigator.languages` present, `window.chrome` shape,
  `Notification.permission` consistency, UA vs `navigator.userAgentData`
  consistency, `Function.prototype.toString` native-code integrity on patched
  builtins. The test spawns via the MCP, navigates to the probe, and asserts each
  invariant. This tier is **deterministic and gates** (no external network).
- **Online/informational tier (NON-GATING, opt-in)** — `stealth` only, **excluded
  from every default run and from the release gate**; runs on
  `workflow_dispatch`/nightly. Drives the MCP against the **two detector pages the
  human chose (2026-07-15): CreepJS (`abrahamjuliot.github.io/creepjs`) and
  `bot.incolumitas.com`** — both research-oriented and tolerant of automated
  access — and records a report artifact (the full fingerprint/trust JSON + a
  screenshot per page). It **must not fail the build on a detector score**
  (vendor-volatile) — it asserts only the same hard invariants as the offline
  tier and logs the rest for human review. Network flakiness here is expected and
  tolerated by design. If either page later blocks automation or changes ToS, the
  mitigation is to drop it and log the removal — never to make it gating.
- **Marker wiring**: add `stealth: anti-bot/undetectability checks; offline tier
  gates, online tier is opt-in and non-gating` to `pyproject.toml` markers.
  Default `-m "not integration"` already excludes both tiers from the unit gate;
  the release gate runs the **offline** tier explicitly.

### 2.5 W5 — release contract + limitations register + transport manifest

- **`RELEASE_CONTRACT.md`** (**repo root** — human ruling 2026-07-15, §7
  decision 4 RESOLVED; it's a marketing-grade artifact and sits next to README):
  the honest, checkable guarantee. Sections:
  1. **Supported matrix** — OS × Python × transport (stdio/http), each marked
     *verified-by* (which CI job). Opens with the headline claim **"works on
     Linux, Windows and macOS"** (human ruling 2026-07-15) — permitted precisely
     because all three OSes run unit + integration + transport + install-smoke
     as gating CI; the claim's row cites those four jobs as its evidence.
  2. **Tool coverage table** — every advertised MCP tool × {covered by transport
     E2E journey | covered by existing E2E `.fn` suite | exempt+reason}.
  3. **Known limitations register** — the routed bugs still open at ship time
     (E8-1…E8-4, E7-1, E7-6, F-181, F-165, close-flake) + missing tool surface
     (dblclick/right-click/drag/native-dialog) + the unauthenticated-HTTP-transport
     and exec-trust-boundary notes. Each with a one-line user-facing statement and
     a tracking id.
  4. **The ceiling, stated plainly** — an absolute "works perfectly against any
     website" guarantee is **not** offered; the web (Cloudflare/detector/Chrome
     drift) makes it unattainable. What *is* guaranteed: every advertised tool
     verified end-to-end through the real MCP transport on supported platforms
     from a clean install, with stealth invariants held, as of the release SHA.
- **Transport coverage manifest tripwire** —
  `tests/test_e2e_transport.py::test_transport_coverage_manifest`, mirroring the
  existing F-108/E2E manifest: builds the live tool-name set from the served
  `tools/list`, asserts `TRANSPORT_COVERED | TRANSPORT_EXEMPT == all_tools`,
  disjoint, every exemption carrying a reason. Adding tool #N+1 without deciding
  its transport story breaks CI. (Most tools are legitimately exempt from the
  *journey* — they're covered by the E2E `.fn` tier; the manifest just forces the
  decision to be explicit, same philosophy as plan_E2E §2.4.)
- **`RELEASE_CONTRACT.md` is regenerable, not hand-maintained where possible**: a
  `tools/gen_release_contract.py` can emit the tool-coverage table from the two
  manifests so it never rots. Prose sections stay hand-written.

### 2.6 W6 — continuous verification (the honest answer to "forever")

A green suite at a release SHA says nothing about tomorrow — the web and Chrome
move under you. The only non-cheating way to approach "forever" is to **stop
treating verification as a point-in-time event and make it continuous**, with a
bounded, measured detection-and-response latency. This is monitoring, not a
promise the future can't break.

- **Scheduled canary job** (`.github/workflows/canary.yml`, `schedule:` cron +
  `workflow_dispatch`): runs the W1 journey + the W4 stealth invariants against
  (a) the local fixture (must always pass — a failure here is a real regression),
  and (b) a **live real-site corpus** (§2.7 W7) + rotating public detectors
  (informational, invariant-only — never gates a human's merge, but **alerts**).
- **Regression alerting**: on any canary failure, open/append a tracking issue
  and notify (GH issue + optional webhook). The metric this plan commits to is
  **MTTD/MTTR-to-regression = one canary cycle** (e.g. ≤24 h), *not* "never
  regresses." That is the achievable, measurable form of "forever."
- **Auto-repro capture**: a failed canary uploads a minimal artifact bundle —
  HAR, DOM snapshot, screenshot, the exact tool-call transcript — so a break is
  actionable immediately, not re-investigated from scratch. Converts "we'll know"
  into "we'll know *and* hold the repro."
- **Chrome/upstream drift watch**: the canary runs on the **current** Chrome (GH
  runner tracks stable) so a Chrome-stable bump that breaks stealth/CDP surfaces
  within one cycle; W7's version matrix covers *known* versions, the canary covers
  *new* ones as they ship.
- **Nightly soak lane**: a longer `workflow_dispatch`/cron run drives a high-count
  workload (≥1000 tool calls, or a fixed wall-clock) and asserts **no memory/handle
  growth and no latency drift across the run** — the endurance shape of a real
  multi-hour session, distinct from W9's short leak ceiling. Non-gating (too long
  for a PR), tracked + alerting; a slow leak that a 20-op test misses surfaces here.
- **Non-gating by construction**: the online/live half never blocks a PR or the
  release tag (that stays the deterministic gate, §3). W6 is the early-warning
  radar, not a wall — so live-web flake can't hold the pipeline hostage.

### 2.7 W7 — adversarial breadth (the honest answer to "any site")

"Any site" is an unbounded universal quantifier over an adversarial, changing
domain — unprovable by a finite suite. The non-cheating substitute is **maximal
structural + adversarial coverage of the feature space** a real site can present,
plus a corpus that *grows toward* the population it can never fully enumerate.

- **Property-based DOM fuzzing** (`hypothesis`, added to the `test` extra):
  generate structurally-varied pages — shadow DOM (open/closed), nested
  cross-origin-shaped iframes, CSP headers, SPA route swaps that detach nodes
  (the F-181 stale-node class), lazy-loaded/virtualized lists, `<canvas>`/WebGL,
  disabled/readonly/hidden controls (the E8-2/E8-3 class), contenteditable
  (E7-1) — and assert each interaction/extraction tool holds its **invariant**
  (right action logged, or a *typed* failure; never a silent wrong-True). This is
  where "perfectly" gets teeth: the known-bug classes are driven to zero **as
  properties**, not as one hand-picked case.
- **Curated real-site smoke corpus** (`tests/corpus/sites.toml`): a small,
  reviewed, ToS-checked set of stable public pages exercised by the W6 canary
  (informational). It must **span a named structural taxonomy**, not N arbitrary
  URLs — at least one representative of each of: cookie-consent walls, SPA
  route-swaps (History API, no full reload), lazy-load / infinite-scroll,
  shadow-DOM-heavy pages, deeply-nested / cross-origin iframes, each major heavy-JS
  framework (React / Vue / Angular), and one page behind each major anti-bot
  vendor class (Cloudflare / DataDome-style). Sampling the *structural population*
  is what makes the "any site" claim bite. It **grows** — every field bug
  reproduced becomes a new corpus entry (regression-forever for *seen* sites).
  Explicitly a sample; §8 quantifies the residual it can never close.
- **Chrome-version matrix**: the integration/transport job pins and rotates across
  the current + N recent Chrome stable versions (executor picks N by runner
  budget; ≥2), so a version-specific break is caught pre-release, not in the wild.
- **Differential stealth**: run the identical journey through the stealth build
  **and** a vanilla headless Chrome against the same probe; assert the detector
  distinguishes vanilla (flags it) but **cannot** distinguish stealth. A
  *relative* guarantee is far more robust to detector drift than any absolute
  score — it re-derives correctness from the contrast each run, so it stays true
  even as detectors change.

### 2.8 W8 — manual-QA parity (the "blind push" contract)

- **`tests/MANUAL_QA_PROTOCOL.md`** — **122 steps across 21 phases**, each with a
  stable `MQ-<n>` id and the id(s) of the automated test(s) covering it.
  Phases: installation/launch (MQ-1…4), browser lifecycle (MQ-5…12), stealth
  verification (MQ-13…21), navigation/interaction (MQ-22…37), negative/failure
  cases (MQ-38…46), tabs (MQ-47…51), cookies (MQ-52…54), network debugging
  (MQ-55…61), element extraction (MQ-62…70), progressive cloning (MQ-71…74),
  file extraction (MQ-75…79), CDP/JS functions (MQ-80…91), dynamic hooks
  (MQ-92…96), debugging tools (MQ-97…99), complex DOM structures (MQ-100…102),
  singleton/process management (MQ-103…108), cross-platform (MQ-109…110),
  completeness tripwires (MQ-111…113), **performance & resource budgets
  (MQ-114…117, W9)**, **resilience / fault-injection (MQ-118…121, W10)**, and
  **documentation examples (MQ-122, W11)**. Of these, **91 already have live
  automated tests** from the E2E-7/E2E-8 campaign and the singleton suite.
  **31 new tests** to write (stealth=8, lifecycle/leak=4, negative-space=4,
  transport/install=2, cross-platform CI=2, completeness=2, **performance=4**,
  **resilience=4**, **doc-examples=1**). This file *is* the coverage spec —
  writing a manual step you can't yet map is how a real gap gets recorded.
- **The manifest is extensible by design.** W8 builds the tripwire mechanism; the
  perf (W9), resilience (W10) and doc-example (W11) steps are appended by those
  workstreams, **each landing its MQ step and its test in the same commit** so
  the parity tripwire is green at every checkpoint. No workstream may add a manual
  step without its live test, or a test-less step — that is the whole point.
- **Parity tripwire** — `tests/test_manual_qa_parity.py`: parses the protocol,
  asserts every `MQ-<n>` maps to ≥1 test that is **collected and not
  `skip`/`xfail`**, and (reverse) that no step is unmapped. A new manual step
  without automation, or an automation silently downgraded to skip, fails CI. The
  machine-checked form of "we automated everything we'd otherwise click."
- **Negative-space coverage** (where the user's edge cases actually live) — the
  protocol explicitly checks the tool **fails correctly**: click a disabled control
  → typed failure, not silent `True` (E8-2); type into readonly → refused (E8-3);
  act on a detached/stale node → typed error (F-181); bad selector → typed error.
  First-class MQ steps, because blind-push means trusting the *failure* paths as
  much as the success paths. Where current behavior is a known bug, the MQ step is
  pinned to its characterization test **and flagged in the protocol as a known
  gap**, so a parity-green never masks it (this is the anti-cheat for §8.1's
  "assert nothing").
- **Lifecycle / leak / recovery** (real manual step, not yet covered): spawn→close
  N times asserts no orphaned Chrome processes / fd growth (psutil child-count
  before == after); kill Chrome mid-session asserts a typed error, not a hang; the
  close-path flake gets a bounded-wait fix **in the test harness** (not `src`) or a
  characterization pin + route.
- **Multi-instance concurrency** (manual step): two instances driven interleaved
  don't cross-talk (separate profiles/ports/tabs); the singleton path is exercised
  through the real transport.
- **Mutation testing** — add `mutmut` (or `cosmic-ray`) to the `test` extra; a
  `workflow_dispatch`/nightly job runs it over the interaction/extraction/hook
  modules and reports the **kill score**. Too slow for a per-PR gate, so it is a
  tracked release-readiness number, not a blocker; surviving mutants on
  user-facing paths become new tests. This is the evidence §0.2's "near-guarantee"
  is earned, not asserted.
- **Zero-flake quarantine** — any test that flakes once is quarantined (out of the
  gate, tracked as a bug) within one cycle. By policy the blind-push gate contains
  **no** known-flaky test — otherwise "green" is not trustworthy enough to push on.

### 2.9 W9 — performance & resource budgets (the "great performance" pillar)

A functionally-green suite can hide a tool that is *correct but unusably slow* or
that *leaks memory over a session*. W9 asserts the second half of "works super
well": fast enough, bounded footprint. All budgets are measured against the
**fixture app** (no live network), so they are **deterministic and gate** — the
same reason W4's offline tier gates.

- **Per-tool latency budgets (GATING)** — `tests/test_perf.py` (`integration` +
  new `perf` marker) drives each tool class through the real transport K times and
  asserts **p95 ≤ a budget**: e.g. `navigate` ≤ N s, `click_element`/`type_text`
  ≤ M s, an extraction/clone ≤ K s. Budgets are set from observed baselines × a
  safety factor (generous, so normal variance never flakes); a tool that silently
  regresses to 10× its baseline turns the gate red. The budgets live in one
  reviewed table at the top of the module, not scattered magic numbers.
- **Memory / handle ceiling (GATING)** — run a defined workload (spawn → a fixed
  sequence of navigations + interactions + extractions → close) under `psutil`;
  assert peak RSS, fd count and child-process count stay under a ceiling **and
  return to baseline after close**. Catches the leak class a short lifecycle test
  (W8) misses because it never drives enough operations.
- **Startup / handshake budget** — the subprocess spawn + MCP handshake completes
  within a bound (reuses W1's `wait_for` wrapper). Cold-start latency is the first
  thing a user feels; a regression here is user-visible.
- **Large-payload stress (GATING, generous bound)** — a fixture page with a very
  large DOM (≥10k nodes) plus a large captured-network set; assert
  extraction/clone/export complete within a bounded time **and** memory, no OOM,
  with correct output. This is where performance and correctness intersect on the
  payloads that actually hurt.
- **Perf-regression baseline lane (informational, in the W6 canary)** — store
  per-tool timing baselines as an artifact; the canary compares each run and alerts
  on a >X% regression. Non-gating (wall-clock is machine-variable) — the
  early-warning for *gradual* slowdown that no single-run budget would catch.
- **Discipline**: any measured breach that is a *product* perf bug → a
  characterization pin + route (a new F-id), **never** a `src/` fix here (§1.2).
  Budgets are tuned once against green baselines; padding a budget to hide a
  regression is the §8.1 "score-chase" cheat and is banned.

### 2.10 W10 — resilience / fault-injection (the dynamic edge cases)

W8's negative-space steps are **static** (a disabled button, a readonly field).
The edge cases users actually hit are **dynamic**: the browser dies, a tab vanishes,
the network drops mid-operation. W10 injects those faults and asserts the tool
**fails in a typed, recoverable way** — never a hang, never a raw `−32000`, never a
silent wrong-`True`, and the server stays usable afterward. `tests/test_resilience.py`
(`integration` marker; faults injected through the real transport where possible,
harness/CDP where injection requires it).

- **Crash recovery** (the single most important real-world fault — browsers *do*
  crash) — terminate the Chrome child mid-session (`psutil` kill), then call the
  next tool; assert a **typed error**, then assert a fresh `spawn_browser`
  **succeeds** (the server didn't wedge, no orphaned process left behind).
- **Tab-closed-under-tool** — close the active tab out of band, then call a
  tab-scoped tool; assert a typed error, not a hang or silent success.
- **Hanging / slow endpoint** — navigate to a fixture route that never completes
  (and one that is deliberately slow); assert the tool honors its timeout and
  returns a **typed timeout error within a bound**. This is the disciplined way to
  characterize the hang-prone class (cf. the `get_cookies` real-Chrome hang, E2E
  exempt) without wedging CI.
- **Network drop mid-op** — via CDP `Network.emulateNetworkConditions` (offline) or
  route-abort, cut connectivity mid-navigate / mid-capture; assert a typed error,
  recoverable.
- **Recovery invariant** — after *every* injected fault the server must be usable
  again: close/respawn works and leaves no orphan (ties back to W9's ceiling). A
  fault that leaves the server wedged is the finding; if it's a product bug it is
  characterization-pinned + routed, not fixed here.

### 2.11 W11 — documentation-example tests (docs never lie)

A user's first five minutes are spent copy-pasting from the README. A broken
example is a worse first impression than any internal bug, and no internal green
catches it. `tests/test_doc_examples.py`:

- **Extract-and-run** — parse fenced code blocks in `README.md` and `docs/*.md`
  that are marked **runnable** (an explicit fence info-string / marker, so authors
  opt in; prose and pseudo-code stay opt-out), execute each against the real
  server + fixture, assert success.
- **Claims-sync** — assert the README's advertised install command matches the
  actual console-script name, and its advertised tool list is a subset of the live
  `tools/list`. **Reuses W5's `gen_release_contract.py`** rather than a second
  source of truth (ADDENDUM_LENSES: no second way of doing something) — docs can't
  advertise a tool that isn't there.
- **Scope**: only marked-runnable blocks; a doc example that hits a known-bug path
  is pinned + flagged exactly like an MQ known-gap, never quietly deleted.

---

## 3. Sequencing (one checkpoint commit per step; suite stays green after each)

> Discipline (house rule): after every step run the appropriate gate. Local
> Windows invocation is `& ".venv\Scripts\python.exe" -m pytest ...` (uv is broken
> on the `&`+space OneDrive path; the worktree can use `uv run`). `--no-verify` is
> **BANNED** — pre-commit/pre-push hooks must run. Deviation from a confirmed
> anchor → **STOP and report**. Each workstream is independently reviewable;
> W1 must land before W2/W3 (they consume its journey).

- **RELEASE-1 — W1 transport suite.** Add the `transport` marker; write
  `tests/test_e2e_transport.py` (handshake, schema-validity, `.fn`-parity, the
  canonical journey, error round-trip, graceful shutdown). Verify locally with
  real Chrome headless. DoD: all transport tests green twice back-to-back (no
  flake); `-m "not integration"` unit suite unchanged; gates green.
  **Commit:** `RELEASE-1: real stdio-transport E2E over the packaged console script (G-A)`.
- **RELEASE-2 — W2 cross-platform CI.** Edit `test.yml`: three-OS matrix on unit,
  **Windows AND macOS** integration+transport jobs (both mandatory, both gating —
  human ruling 2026-07-15). DoD: the PR's own CI run shows the new jobs
  **executing and green** on Windows **and macOS** (this is the acceptance
  evidence, not a local run). Any OS-specific failure → characterization
  pin + route, re-run green. **Commit:** `RELEASE-2: Linux/Windows/macOS CI matrix + Windows+macOS integration+transport jobs (G-B)`.
- **RELEASE-3 — W3 install smoke.** `tools/install_smoke.py` + `publish.yml`
  `install-smoke` job gating `publish` + a CI dry-run. DoD: smoke job green on
  ubuntu+Windows+macOS in a dry-run; deliberately breaking package-data locally
  makes it RED (proof it bites). **Commit:** `RELEASE-3: clean-install smoke gating the release tag (G-C)`.
- **RELEASE-4 — W4 stealth.** `stealth_probe.html` + `test_stealth.py` (offline
  gating tier + online opt-in tier against **CreepJS + bot.incolumitas**) + marker.
  DoD: offline tier green twice; online tier runs under `workflow_dispatch` only,
  never in the default gate, and asserts invariants-only (never a detector score).
  **Commit:** `RELEASE-4: stealth invariant verification — offline gating + online informational (G-D)`.
- **RELEASE-5 — W5 contract + manifest.** `RELEASE_CONTRACT.md`,
  `gen_release_contract.py`, transport coverage manifest test. DoD: manifest test
  green; contract's tool table matches the live `tools/list`; adding a fake tool
  locally breaks the manifest. **Commit:** `RELEASE-5: release contract + known-limitations register + transport coverage manifest (§0 guarantee)`.
- **RELEASE-6 — W7 adversarial breadth.** Add `hypothesis` to the `test` extra;
  DOM-fuzz property suite; Chrome-version matrix in `test.yml`; differential
  stealth in `test_stealth.py`; `tests/corpus/sites.toml` seed. DoD: property
  suite green (known-bug classes either pass as properties or are
  characterization-pinned + routed, never silently); version matrix green across
  N Chromes; differential test proves vanilla-flagged / stealth-unflagged.
  **Commit:** `RELEASE-6: property-based DOM fuzzing + Chrome-version matrix + differential stealth (any-site breadth)`.
- **RELEASE-7 — W6 continuous verification.** `canary.yml` (cron +
  `workflow_dispatch`) running W1 journey + W4 invariants against fixture
  (gating-on-fixture) and the live corpus + detectors (informational + alerting);
  auto-repro artifact bundle; issue/webhook alert. DoD: a manual
  `workflow_dispatch` run is green on the fixture half and produces a report
  artifact for the live half; a deliberately-broken invariant opens an alert and
  uploads the repro bundle (proof the radar works). **Commit:**
  `RELEASE-7: scheduled canary + regression alerting + auto-repro (continuous 'forever' proxy)`.
- **RELEASE-8 — W8 manual-QA parity (capstone).** `MANUAL_QA_PROTOCOL.md` +
  `test_manual_qa_parity.py` tripwire; lifecycle/leak/recovery + multi-instance
  tests; `mutmut` in the `test` extra + a nightly mutation job; the flake-quarantine
  policy. DoD: parity tripwire green (every MQ step mapped to a live test;
  known-bug steps flagged, not hidden); lifecycle test proves zero orphan
  processes; a nightly mutation run produces a kill-score baseline; adding an
  unmapped MQ step locally fails the tripwire (proof it bites). **Commit:**
  `RELEASE-8: manual-QA parity manifest + lifecycle/concurrency + mutation proof (blind-push bar)`.
- **RELEASE-9 — W9 performance & resource budgets.** Add the `perf` marker;
  `tests/test_perf.py` (latency budgets, memory/handle ceiling, startup budget,
  large-payload stress) + the large-DOM fixture page + the canary baseline lane;
  **append MQ-114…117 + their tests to the protocol** (parity tripwire stays
  green). DoD: budget suite green twice (no flake on generous budgets); a
  deliberately-slowed tool locally turns it RED (proof it bites); memory ceiling
  holds and returns to baseline after close. **Commit:**
  `RELEASE-9: performance & resource budgets — latency + memory ceiling + large-payload stress (great-performance pillar)`.
- **RELEASE-10 — W10 resilience / fault-injection.** `tests/test_resilience.py`
  (crash-recovery, tab-closed-under-tool, hanging-endpoint timeout, network-drop)
  + the hanging fixture route; **append MQ-118…121 + their tests** (tripwire stays
  green). DoD: every fault yields a typed error and a successful respawn; no test
  hangs (all bounded); any wedge that surfaces is characterization-pinned + routed.
  **Commit:** `RELEASE-10: resilience / fault-injection — typed-error + recovery under crash/close/timeout/network-drop (dynamic edge cases)`.
- **RELEASE-11 — W11 documentation-example tests.** `tests/test_doc_examples.py`
  (extract-and-run runnable blocks + claims-sync reusing `gen_release_contract.py`);
  **append MQ-122 + its test** (tripwire stays green). DoD: every runnable README/docs
  example green; breaking an example locally turns it RED; claims-sync catches a
  fabricated tool name. **Commit:**
  `RELEASE-11: documentation-example tests — every runnable README/docs snippet executes (docs never lie)`.

---

## 4. Verification

- **Local (Windows)**: per step, `& ".venv\Scripts\python.exe" -m pytest <files> -q`;
  transport/stealth tiers need real Chrome (headless). Full `-m "not integration"`
  stays green throughout.
- **CI is the real acceptance surface for W2/W3** — the new Windows/macOS jobs and
  the install-smoke job must be **observed green on the PR**, since they can't run
  locally. Screenshot/log the green runs into the completion report.
- **Determinism proof**: run each new gating file (transport, offline stealth)
  twice back-to-back; zero flakes. Online stealth tier is exempt from this bar by
  design (documented).
- **Gates**: ruff format/check, ty, vulture, suppression-owners, file-budgets all
  green; `git diff` confirms **zero `src/` changes**; server.py budget untouched
  (this plan doesn't touch it). New CI YAML validated by the runs themselves.
- **Coverage gate**: the `--cov-fail-under=55` floor must still pass (adding tests
  only raises coverage; if a new test file imports src in a way that shifts
  numbers, reconcile — a DROP is the danger sign, per
  [[test-suite-scopes-unit-vs-full]]).

## 5. Risks & mitigations

- **Subprocess/stdio flake in CI** → generous handshake bound with captured child
  stderr on failure; kill-in-`finally`; one spawn per test.
- **Windows Chrome discovery differs** → use a pinned `setup-chrome` action;
  headless; any discovery gap that's a *product* bug becomes a characterization
  pin routed to a follow-up, not a src fix in this plan.
- **macOS runner cost** → accepted by the human (ruling 2026-07-15): macOS
  integration+transport is mandatory and gating, same as Windows — the runner
  minutes are the price of the "works on Linux, Windows and macOS" claim. If
  wall-clock becomes a problem, the mitigation is splitting/parallelizing jobs,
  never demoting an OS to optional.
- **Stealth online-tier flakiness / ToS** → non-gating, opt-in, invariant-only
  asserts, the two chosen detector pages (CreepJS + bot.incolumitas), results are
  artifacts for human review; never fails the build on a score. Both pages are
  research-oriented and automation-tolerant; if either changes ToS, drop-and-log
  it — never make it gating.
- **fastmcp 2.11.2 stdio API mismatch** → executor proves the API against the
  installed package **before** writing the suite; documented fallback is the `mcp`
  SDK client in the `test` extra only (runtime deps untouched — a STOP if that's
  not enough).
- **CI runtime growth** → transport suite ≤6 tests; OS matrix parallelizes; if the
  integration+transport wall-clock exceeds ~10 min, splitting is a follow-up, not
  a blocker.
- **Install-smoke false-greens** → the step must fail-closed: a spawn that returns
  no tools, or a journey step that errors, exits non-zero. Prove it bites by
  breaking package data locally once (RELEASE-3 DoD).

## 6. Findings interplay / honest framing

- **This plan gates and documents; M5b/M14 fix.** If E8-1…E8-4 / E7-1 / E7-6 /
  F-181 / F-165 / close-flake are closed by the time this runs, the transport E2E
  asserts the *fixed* behavior and the contract lists them as resolved. If still
  open, they are **characterization-pinned here** and entered in the
  known-limitations register — the guarantee is honest either way.
- **The guarantee delivered** (two tiers — a proof and an asymptote):
  1. **Proven, absolute — for the surface under our control.** *If the RELEASE
     gate (unit + integration + transport + offline-stealth + install-smoke,
     property-based DOM fuzz, **per-tool latency + memory-ceiling budgets**,
     **fault-injection recovery**, on all supported OS × Chrome-version cells) is
     green at a release SHA, then every advertised MCP tool has been driven
     end-to-end through the real stdio protocol against real Chrome from a clean
     install on each supported platform — **Linux, Windows and macOS, all three
     gating** (human ruling 2026-07-15), which is what licenses the contract's
     explicit "works on Linux, Windows and macOS" claim; every interaction/extraction
     invariant holds as a **property** (not one case); every tool returns **within
     a measured latency budget** with a **bounded memory footprint**; every injected
     fault (crash, tab-close, timeout, network-drop) yields a **typed, recoverable**
     error; stealth invariants hold **differentially** (vanilla flagged, stealth
     not); and every known deviation is documented.* This half is a genuine
     guarantee because the domain is closed (our code, our protocol, our fixtures,
     pinned Chromes).
  2. **Asymptotic, monitored — for the open web.** Against arbitrary live sites
     the guarantee is not "it works" but a **convergent process**: a growing
     corpus that regresses-forever on everything seen, structural fuzz coverage of
     the feature space, and a continuous canary that catches any new break within
     **one cycle** (committed MTTR). Coverage rises toward "any site forever"; the
     residual gap is **named and quantified** (§8), never papered over.
  Together these are the strongest thing that is *true*. Extending tier-1's
  absoluteness to the open web would require quantifying over sites, detectors and
  Chrome versions that don't exist yet — see §8 for why that last step cannot be
  taken without cheating, and the specific cheats this plan refuses.

## 7. Decisions — ALL RESOLVED (human, 2026-07-15)

No open decisions remain; the plan is fully specified for execution.

1. ~~**macOS CI now or fast-follow?**~~ **RESOLVED: macOS NOW, mandatory and
   gating alongside Linux and Windows** — the release contract makes the strong
   claim "works on Linux, Windows and macOS", so all three OSes run unit +
   integration + transport + install-smoke on every PR/tag. The
   Windows-now/macOS-later recommendation is superseded.
2. ~~**Online stealth detector pages**~~ **RESOLVED: CreepJS + bot.incolumitas**,
   both research-oriented and automation-tolerant. Online tier is opt-in,
   non-gating, invariant-only, artifact-producing; the offline probe gates
   regardless. Either page may be dropped-and-logged if its ToS changes.
3. ~~**Release-gate composition**~~ **RESOLVED: `install-smoke` HARD-BLOCKS
   tag/publish** on all three OSes — no warn-and-ship path.
4. ~~**Contract location**~~ **RESOLVED: `RELEASE_CONTRACT.md` at repo root**
   (marketing-grade, sits next to README).

---

## 8. The wall — why "works perfectly on any site forever" cannot be *promised* without cheating

This plan pushes as hard as engineering honestly allows toward that sentence (W6
continuous verification for *forever*, W7 fuzz + corpus + differential stealth for
*any site*, tier-1 properties for *perfectly*). It deliberately stops one step
short of *asserting* the literal absolute, because that last step is not an
engineering gap — it is a logical one. Four independent walls, each irreducible:

1. **"forever" vs a moving substrate.** Detectors (Cloudflare/DataDome/Akamai) and
   Chrome-stable change continuously and adversarially. A suite green at SHA *X*
   is evidence about the world *at X*, and about nothing at *X+1*. No amount of
   testing today constrains an adversary's move tomorrow. The honest maximum is
   *continuous re-proof with bounded detection latency* (W6) — a promise about
   **response time**, not about the future.
2. **"any site" is an unbounded universal quantifier over an adversary.** You
   cannot finitely verify ∀ sites — including sites built *specifically* to break
   this tool, and sites that don't exist yet. Fuzzing + a growing corpus (W7)
   raise coverage but sample a population they can never enumerate. Proving the
   universal would require the site distribution to be closed and known; it is
   neither.
3. **"perfectly / undetectable" is proving a negative in an open class.**
   "Detectable by *no* detector, including future ones" is not falsifiable-then-
   established by any finite test set — it is the anti-bot arms race, structurally
   open-ended. Differential testing (W7) gives a strong *relative* result
   ("indistinguishable from a real browser under these probes"), which is the
   strongest form that stays true under drift — but it is scoped to the probes
   run, not all probes conceivable.
4. **Your own honesty rule forbids the shortcut.** You said "without cheating." The
   *only* ways to make the plan's text read "works perfectly on any site forever"
   are cheats — so making that promise and honoring "without cheating" are
   mutually exclusive. The plan chooses "without cheating."

### 8.1 The cheats this plan explicitly refuses

Each of these would let the document *claim* the absolute. All are rejected on the
record so a future editor can't quietly reach for one:

- **Redefine the words.** Narrow "works" to "process doesn't crash", "any site" to
  "the fixture + corpus", "perfectly" to "returns a value", or "forever" to "at
  this SHA" — then keep the impressive sentence. (Definitional bait-and-switch.)
- **Snapshot the adversary.** Vendor a frozen copy of a detector page, pass it,
  and call it "verified against real anti-bot" — a detector you've frozen is no
  longer the adversary. (Same trap plan_E2E §1.2 already bans for goldens.)
- **Assert nothing.** `assert True`, `assert result is not None`, or catch-all
  `try/except: pass` in an E2E so it can never fail. (Green that proves nothing.)
- **Gate on the deterministic half, advertise the live half.** Run only the
  fixture in the release gate, then market it as "tested against the live web."
  (W6's live tier is therefore explicitly **non-gating and labeled informational**
  — its role is alerting, and the contract says so.)
- **Score-chase.** Assert a detector's numeric "human score ≥ N" — vendor-volatile,
  and passing it once ≠ passing it after their next model update. (W7 asserts
  *invariants and differentials*, never a vendor score.)
- **Silence the caveat.** Delete §8 / W5.4 and let the summary sentence stand
  alone. (This section exists precisely so that can't happen unnoticed.)

### 8.2 The wording the contract *will* carry (the non-cheating maximum)

> This release is continuously verified: every advertised tool is proven
> end-to-end through the real MCP transport, on every supported OS and Chrome
> version, from a clean install, with interaction invariants held as properties
> and stealth held differentially against a real browser. Against the open web it
> is not guaranteed to "always work" — the web and Chrome change adversarially —
> but it is **continuously re-verified against a growing corpus of real sites and
> detectors, and any regression is detected within one canary cycle and shipped
> with a reproduction.** Known limitations are enumerated in §Known-Limitations.

That is the closest a truthful document can stand to "works perfectly on any site
forever." If the requirement is the *literal* sentence with no asterisk, the only
way there is one of §8.1 — and this plan will not take it. If instead the
requirement is *"make the real guarantee as strong as it can honestly be,"* that
is exactly what W1–W7 deliver, and this section is the proof of where the honest
ceiling sits and why.
