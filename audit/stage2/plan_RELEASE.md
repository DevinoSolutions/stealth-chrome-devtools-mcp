# plan_RELEASE (E2E-9) — Release-gate hardening: transport, platforms, packaging, trust boundaries, resilience, and an honest release contract

- **Status**: **REMEDIATED AFTER INDEPENDENT COVERAGE VALIDATION; REVALIDATION
  REQUIRED BEFORE IMPLEMENTATION.** Not yet started. The four human release
  choices recorded on 2026-07-15 remain settled (§7), but they are decisions, not
  evidence that the gate exists. This revision removes claims disproved at HEAD
  and turns each into an acceptance condition. Phase 0 must be rerun at the exact
  execution HEAD and return GO or GO-with-changes before RELEASE-1 begins.
- **Position in campaign**: executes **AFTER** the audit FIX pipeline lands
  (M4-Ph1+A1 → M5b → M14+A1) through the human merge-gate and **before** (or
  folded into) CODIFY. Before RELEASE-1, record the three prerequisite merge
  SHAs and prove each is an ancestor of the execution base with
  `git merge-base --is-ancestor <sha> HEAD`. A matching file or branch name is
  not proof. If any ancestor check fails, STOP and raise the sequencing item;
  never silently reorder the campaign. Known user-facing bugs must be fixed by
  those streams or consciously retained as characterized, routed limitations.
- **Branch/stack**: the execution PR stack starts from the human-designated most
  recently worked branch after the ancestor proof, not from an assumed `main`.
  Record base branch and exact SHA in the first completion note. Each RELEASE-n
  PR is stacked on its predecessor, remains unmerged, and is held at the human
  merge-gate.
- **Nature**: tests + test fixtures + **CI workflow changes** + docs. The one
  plan in the campaign that is *allowed* to edit `.github/workflows/*` and add new
  pytest markers (`transport`, `stealth`, `perf`). **Zero `src/` production
  edits** (same hard rule as plan_E2E — any bug this suite hits is pinned with
  `@pytest.mark.characterization` and its F-id docstring, routed, and never fixed
  here). No new runtime dependencies; test-extra dependencies only when
  unavoidable (§1.3). No embedded module may import `server` (runpy
  double-registration hazard). M6-pinned error message bytes are preserved.

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

This plan closes G-A…G-D and the independent validation gaps without pretending
that correctness, speed, safe failure, and documentation exhaust user experience.
W12–W16 add trust-boundary security, wire concurrency/cancellation and independent
interoperability, upgrade/migration, actionable failure observability, and
stateful/PWA/internationalized site shapes. It uses two tiers: a deterministic
release gate for the precisely named runner/Chrome/tool surface under our
control, and a separately labelled informational live-web corpus. The
user-facing contract (§W5) states both tiers and the residual wall (§8); no live
claim is inferred from a fixture-only gate.

### 0.1 Current support and confirmed anchors — context, not acceptance evidence

Everything in this table is a remediation-baseline observation. None of it may
be copied into `release-evidence/v1` or treated as release acceptance; only the
current-SHA workflow ledger defined in W5 can do that.

| Claim | Verified 2026-07-14 |
|---|---|
| Console scripts | `pyproject.toml:47-49` — `stealth-chrome-devtools-mcp = "stealth_chrome_devtools_mcp.server:main"` (the MCP server; **stdio default**), `stealth-chrome-devtools = "...cli:main"`. |
| Transports | server supports stdio (default) **and** `--transport http` (loopback-default, **unauthenticated** — `tests/test_server_entrypoint.py`). Market-relevant: the HTTP surface has no auth by design; the contract (§W5) must state this. |
| FastMCP available as runtime dep | `pyproject.toml` pins `fastmcp==2.11.2`. Validation proved that `fastmcp.Client` can initialize the real installed server, list 94 tools, and call a tool over stdio **when given the absolute Windows launcher path**. A bare `stealth-chrome-devtools-mcp` failed in the mandated non-activated `.venv` environment (`WinError 2`). W1 therefore canonicalizes an absolute installed launcher and repeats this proof before writing tests; a bare-name assumption is forbidden. |
| Existing markers | `pyproject.toml:75-78` — only `integration`, `characterization`. This plan **adds `transport`, `stealth`, and `perf`**. |
| CI shape | `.github/workflows/test.yml` — `unit-tests` (ubuntu, py3.11/3.12/3.13, `-m "not integration"`, `--cov-fail-under=55`), `quality` (ruff/ty/vulture/owners/budgets), `integration-tests` (ubuntu, google-chrome-stable + Xvfb, `-m integration --timeout=120`). All `runs-on: ubuntu-latest`. |
| Publish shape | `.github/workflows/publish.yml` — on `v*` tag: unit-only tests (ubuntu, py3.11–3.13) → `uv build` → PyPI publish → GH release advertising `uv tool install` / `uvx`. **No install smoke.** |
| Package classifier | `pyproject.toml` — `Development Status :: 5 - Production/Stable`, version `1.2.0`, already on PyPI. The "Production/Stable" claim is what this plan makes *true under test*. |
| Existing E2E coverage manifest | `test_e2e_functions_hooks.py::test_e2e_coverage_manifest` is a real **set-equality** tripwire: 93 declared covered names plus the sole exemption `get_cookies` equal the 94-tool registry. It does not prove behavioral depth. Validation found no successful-cookie-retrieval test for `get_cookies`; the 94-tool release claim is blocked until a real success path passes, or the supported count/surface is explicitly narrowed. |
| Manual protocol at remediation baseline | The executable baseline is MQ-1…113. W7 reserves MQ-114…121; W9 MQ-122…125; W10 MQ-126…129; W11 MQ-130; W12 MQ-131…137; W13 MQ-138…144; W14 MQ-145…149; W15 MQ-150…154; W16 MQ-155…162. Each step is appended only in the same commit as its live evidence and parity update. No existing-count estimate is trusted or repeated. |

### 0.2 Acceptance principle — manual-QA parity (the "blind push" bar)

The bar the whole plan is held to: **a green RELEASE gate must be a faithful
stand-in for the deterministic manual pass a human would otherwise run before
shipping.** Installation, transport, browser work, stealth invariants,
performance, fault recovery, trust boundaries, concurrent/cancelled requests,
upgrade, diagnostics, persistent state, and internationalized input each have
named evidence. Live public sites remain informational and never license a
deterministic claim. Green authorizes a blind push only for the qualified
supported matrix and advertised surface recorded by W5.

Three properties make "green ⇒ blindly pushable" **true** rather than aspirational.
Skipping any one turns blind-push back into a leap of faith — itself the subtle
cheat §8 warns against:

1. **Parity-complete** — W8 parses exact evidence identifiers and fails if any MQ
   step lacks live evidence. Pytest evidence must be a fully qualified collected
   node id that passes on the applicable gate: no skip, xfail, xpass, deselection,
   conditional non-collection, or characterization result counts as success.
   Workflow evidence must name a stable job id and be verified in the current PR
   run; prose labels and screenshots alone are not machine evidence.
2. **Flake-free** — a gate you trust blindly cannot be flaky; one flake means green
   no longer means "works." W8 makes flake a zero-tolerance quarantine event and
   keeps the two-run determinism proof (§4).
3. **Mutation-informed** — high line coverage with weak asserts catches nothing (the
   `assert True` family, §8.1). Mutation testing injects deliberate bugs into
   `src/` and requires the suite to go red; the **mutation-kill score** is the
   honest diagnostic behind test strength. Coverage says the line *ran*;
   mutation says a sampled bug would have been caught. The mutation lane is
   informational and cannot be described as part of the blocking release proof.

This is the concrete, non-cheating form of the §6 asymptote tier: not "works on
any site forever," but **"every claimed, deterministic release behavior has
passing current-SHA evidence, and every exclusion is visible."**

---

## 1. Scope

### 1.1 Workstreams (16 stacked, independently reviewable checkpoints)

| WS | Deliverable | Closes gap | Depends on |
|---|---|---|---|
| **W1** | Canonical real-stdio harness: resolve the **absolute installed console launcher**, prove FastMCP 2.11.2 can spawn it, then drive the canonical real-Chrome journey through MCP. | G-A | prerequisite ancestor proof; fixture app |
| **W2** | Reusable three-OS release gate on **Ubuntu x64, Windows x64, and macOS ARM64**: per-OS coverage, integration/transport, exact runner/Chrome identity, lifecycle cases, and one stable aggregate required check with ruleset evidence. | G-B | W1 |
| **W3** | Build the distribution once, smoke-test that exact artifact on the three W2 runner cells, and publish that same artifact; identical blocking semantics for PR qualification and tag publish. | G-C | W1, W2 |
| **W4** | Acceptance-complete deterministic stealth probe plus CreepJS and bot.incolumitas as explicitly informational online observations. | G-D | W1, W2 |
| **W5** | Qualified root `RELEASE_CONTRACT.md`, known-limitations register, and one generated evidence/tool-surface source of truth. `get_cookies` blocks a 94-tool claim until its real success path passes. | honest claims | W1–W4 |
| **W6** | Scheduled informational observation and **local-only fixture reproduction**. It writes only to the runner/local workspace and never uploads repros, opens issues, posts webhooks/comments, or otherwise mutates external systems. | drift visibility | W4, W5 |
| **W7** | Deterministic structural fixture expansion and property tests against the W2 image-provided Chrome Stable; append MQ-114…121 atomically. Live sites and other Chrome versions/channels remain informational observations. | site breadth | W1, W4 |
| **W8** | Exact manual-QA evidence grammar and parity tripwire for baseline MQ-1…113 plus W7's MQ-114…121, lifecycle/concurrency evidence, flake discipline, and informational mutation analysis. Characterization is visible evidence of a known gap, never success evidence. | blind-push integrity | W1–W7 |
| **W9** | Deterministic latency/resource budgets and a specified ≥10k-node/large-network stress oracle; append MQ-122…125 with their tests. | performance | W1, W8 |
| **W10** | Deterministic fault injection with separately controlled hanging and slow routes, bounded teardown, typed outcomes, and recovery; append MQ-126…129 with their tests. | resilience | W1, W7, W9 |
| **W11** | Runnable documentation examples and claims sync that **reuse W5's generator**; append MQ-130 with its test. | docs | W5, W8 |
| **W12** | Canonical threat/redaction policy plus security tests for exec-capable tools, imports/exports, uploads, and every `*_to_file` path; append MQ-131…137 with their tests. | security | W1, W5, W8 |
| **W13** | Wire concurrency, cancellation, backpressure, client disconnect, framing, and independent official-MCP-client interoperability; append MQ-138…144 with their tests. | protocol UX | W1, W8, W9 |
| **W14** | Literal N-1→current upgrade/migration/rollback smoke using the human/admin-supplied immediately preceding stable release tag and immutable artifact SHA-256; append MQ-145…149 with their tests. | upgrade safety | W3, W5, W8 |
| **W15** | Failure observability reusing W12's canonical redaction policy: correlation, bounded diagnostics, actionable local repro, and clean stderr/stdout; append MQ-150…154 with their tests. | diagnosis | W6, W8, W10, W12, W13 |
| **W16** | Dedicated/shared/service-worker, state/PWA, and internationalized fixtures; append MQ-155…162 with their tests. | modern-site UX | W7, W8 |

Dependency graph (the RELEASE sequence is deliberately serial even where edges
would permit parallel work):

```text
W1 → W2 → W3
       └→ W4 → W5 → W6
│         └──→ W7 ─┐
└──────────────────→ W8 → W9 → W10 → W11 → W12 → W13 → W14 → W15 → W16

Additional required edges:
W1→W4,W7,W12,W13; W2→W4; W5→W8,W11,W12,W14; W6→W15;
W7→W8,W10,W16; W8→W9,W11,W12,W13,W14,W15,W16; W9→W10,W13;
W10→W15; W12→W15; W13→W15
```

### 1.2 Non-goals (hard OUT) — same discipline as plan_E2E §1.2

- **No `src/` production edits.** Every bug the transport/stealth suite hits is
  pinned `@pytest.mark.characterization` + F-id docstring and routed, never fixed
  here. A characterization test records current behavior; it never satisfies an
  MQ success requirement, a supported-tool claim, or a release DoD. Bugs
  known-open at execution time (unless closed by M5b/M14 first):
  E8-1 (`select_option` const-redeclaration silent no-op → returns True),
  E8-2 (click on disabled control returns True), E8-3 (`clear_first` bypasses
  readonly), E8-4 (range/color/date inputs unreachable), E7-1 (`type_text`
  no-clear contenteditable), E7-6 (`get_element_state` attrs-not-props),
  F-181 (internal stale-live-node −32000 characterization; unsupported and not a
  public acceptance surface), F-165 (dup header loop), close-path flake.
  These become **documented known limitations** in §W5, and the transport E2E
  **pins their current behavior** so a later fix surfaces as a deliberate test
  update. Message text at all M6-pinned error sites is byte-preserved.
- **No new tools.** The absence of `dblclick` / `right-click` / `drag` /
  native-dialog tools (E2E-8 census) is a **documented limitation** in §W5, not
  built here.
- **No new runtime dependencies.** W1 uses `fastmcp.Client` (already a runtime
  dep). Any test-only helper goes under the `test` extra.
- **No security re-architecture.** W12 verifies and documents the existing trust
  boundary; it does not add sandboxing, authentication, or production policy.
  The unauthenticated HTTP transport and `create_python_binding`/exec boundary
  remain explicit limitations. A test that reveals unsafe behavior is pinned,
  routed, and may narrow the supported contract; it is never "fixed" in `src/`.
- **No live-web goldens.** Stealth online tier asserts **invariants**
  (`navigator.webdriver === false`, no `cdc_`/CDP-leak globals, UA consistency),
  never a detector's numeric score (vendor-volatile).
- **No external mutations from W6/W15 automation.** Those workstreams may write
  only the throwaway runner/local workspace and sanitized job log. They do not
  upload repro bundles, open issues, post comments, invoke webhooks, send
  notifications, or write to third-party systems. Human follow-up is outside this
  plan. W3's distribution handoff remains the separately scoped run artifact.
- **No second mechanism.** ADDENDUM_LENSES applies throughout: the W1 journey is
  imported by W3; W5's evidence/parser/generator API is consumed by W8 and W11;
  W12 extends that API with the canonical threat/redaction policy; later
  observability composes W6's local writer with that policy. Parallel sources of
  truth are forbidden.
- **No hook bypass.** `--no-verify` is banned. Pre-commit and pre-push run at each
  checkpoint; failures are fixed or raised, never bypassed.

### 1.3 Dependency policy

Prefer **zero** new deps. If W1 genuinely cannot drive stdio via `fastmcp==2.11.2`
`Client` after using the absolute installed launcher (executor repeats the proven
initialize/list/call check first), STOP W1 and document the mismatch. The
official `mcp` SDK is required later as W13's independent interoperability client
and may be added only to the `test` extra; it is not a way to paper over a failed
W1 foundation. Adding anything to runtime `dependencies` is a STOP condition.

---

## 2. Design

### 2.1 W1 — canonical absolute-launcher stdio E2E

Files: `tests/release_gate_harness.py` (the one reusable journey) and
`tests/test_e2e_transport.py` (`integration` + `transport`).

- **Launcher resolver is part of the assertion.** Given the interpreter for the
  environment under test, resolve the console entry point from that
  environment's scripts directory (`Scripts/…exe` on Windows, `bin/…` on
  POSIX), canonicalize it with `Path.resolve()`, and require an absolute existing
  executable inside that environment. Log the path. Do not use a bare command,
  mutate PATH, invoke `python -m` from the source tree, or silently fall back to
  another checkout. W3 calls the same resolver for each clean install.
- **Foundation proof before test authoring.** With pinned FastMCP 2.11.2 and the
  resolved launcher, connect over stdio, complete `initialize`, assert protocol
  and server name/version, obtain `tools/list` (94 registry entries at the
  remediation baseline), and successfully call `list_instances`. Record the
  exact client construction in the harness docstring. If this proof fails at the
  execution HEAD, STOP; do not substitute an in-process client or pretend the
  transport pillar exists.
- **Handshake/schema assertions.** Every served tool has a unique name and a
  well-formed `inputSchema`; one representative `tools/call` result is
  shape-equivalent to the existing `.fn` seam without requiring byte-identical
  incidental formatting. Protocol stdout contains framing only; diagnostics go
  to bounded captured stderr.
- **Canonical journey.** Drive one real headless Chrome entirely through
  `tools/call` against the literal-IPv4 loopback fixture:
  `spawn_browser` → `navigate` → `list_tabs`/`get_active_tab` →
  `click_element`/`type_text`/`select_option` → assert the fixture action oracle
  with `execute_script` → assert page/element ground truth → one structural
  extraction → PNG-magic screenshot → `close_instance`. The function returns a
  versioned, JSON-serializable result record consumed unchanged by W3; W3 may not
  implement a second smoke journey.
- **Bounds and teardown.** Every await has an outer bound. Capture stderr on
  failure with a byte/line cap. Close browser instances and the fixture in
  `finally`, terminate the child only after a bounded graceful close, and prove
  no child remains. Use literal `127.0.0.1` and port `0`; assert the emitted URL is
  IPv4 so IPv6-first `localhost` resolution cannot create a false failure.
- **Scope honesty.** The journey proves representative transport behavior, not
  all advertised tools. W5's evidence ledger is the only place a per-tool claim
  may be made.

### 2.2 W2 — reusable three-OS gate and stable required check

Create one reusable workflow (for example
`.github/workflows/release-gate.yml`, `workflow_call`) and have `test.yml` call
it. W3 invokes the same workflow for tags; job semantics may not be copied into a
second YAML source of truth.

- **Qualified runner cells.** The release cells are exactly GitHub-hosted Ubuntu
  x64, Windows x64, and macOS ARM64. Use explicit labels available at
  implementation time and fail early unless `runner.os` and `runner.arch` match
  the contract (`Linux/X64`, `Windows/X64`, `macOS/ARM64`). Record image OS,
  image version, architecture, Python version, and runner label as a job artifact.
  A label migration is a red gate requiring contract review, not an invisible
  architecture change.
- **Unit and coverage evidence per OS.** Preserve Python 3.11–3.13 unit coverage
  on all three OSes. At least one Python cell per OS runs coverage with the same
  reviewed floor and uploads its own report; a Linux-only report or a merged
  report that can hide a red platform is insufficient. Targeted platform tests
  must exercise the actual `msvcrt`/`fcntl` lock branch, detached-process and
  signal behavior, session-root creation, literal-IPv4 port-0 allocation,
  singleton PID/port ownership, profile-handle cleanup, and repeated
  spawn/close. Platform-specific conditional collection is listed in evidence;
  an applicable case may not be skipped or xfailed.
- **Real-Chrome evidence per OS.** Each OS runs integration, transport, and
  offline stealth with no `continue-on-error`. Before testing, resolve the exact
  **image-provided Chrome Stable** executable, emit its canonical path and
  version, then after
  spawn assert through process inspection/CDP that the launched binary and
  `Browser.getVersion` product match that identity. Auto-discovery selecting a
  different channel is a failure. Do not install or claim another Chrome
  version/channel: production has no executable selector, and this
  zero-`src/` plan cannot create one. Linux alone may use Xvfb where a headed case
  requires it; headless cases do not inherit `DISPLAY=:99` assumptions.
- **Portable temp/session roots.** Use runner-native temporary directories and
  `pathlib`; do not hard-code `/tmp`, separator strings, `localhost`, or CRLF/LF
  goldens. Tests use `127.0.0.1` and port `0`. The contract remains scoped to the
  hosted runner accounts; it does not infer ordinary-user ACL coverage from an
  administrative Windows runner.
- **Stable aggregate.** The final reusable workflow has these exact blocking job
  ids: `quality`, `unit-tests`, `coverage`, `integration`, `transport`,
  `offline-stealth`, `build-dist`, `package-verify`, `install-smoke`, and
  `release-evidence`. A final stable id/name `release-gate` uses `if: always()`
  and directly lists every one in `needs`; each matrix job represents all of its
  required cells. It fails unless every dependency and required cell is
  `success`; failure, skipped, cancelled, missing, duplicate, or neutral is red.
  W2 introduces the available quality/test edges, W3 adds build/package/smoke,
  and W5 adds evidence; no later workstream may replace the aggregate or forget a
  direct edge. No matrix child is itself the public required-check contract.
- **Ruleset acceptance evidence.** A human configures the repository ruleset to
  require only the stable aggregate name. The completion report records a
  read-only ruleset/API or repository-settings capture and a PR experiment in
  which (a) a deliberate `quality` failure and (b) one deliberate matrix-cell
  failure independently make `release-gate` red and block merge, followed by the
  restored green run. Bite proofs mutate only the in-job checkout/input and are
  never pushed as bypass commits. If ruleset state cannot be proved, W2 is
  incomplete and the OS gate may not be advertised.

### 2.3 W3 — build once, test exact artifacts, publish those artifacts

- **One build job per commit.** The reusable gate gains a `build-dist` job that
  checks out the target SHA once, runs `uv build` once, validates wheel/sdist
  metadata and expected package data, writes SHA-256 hashes plus version to a
  manifest, and uploads `dist/` as an immutable run artifact. No smoke or publish
  job may run `uv build`, rebuild from checkout, or download the same version
  from PyPI.
- **Package verification child.** `package-verify` downloads the one build
  artifact, checks the manifest hashes, metadata/version, wheel/sdist membership,
  and embedded package data, and emits its evidence record. It is distinct from
  installation and is a direct `release-gate` dependency.
- **Exact-artifact smoke.** `tools/install_smoke.py` creates a fresh environment,
  installs the downloaded local wheel or sdist by absolute path with caches
  disabled, resolves that environment's launcher through W1's resolver, verifies
  installed version/package-data origin, and runs W1's unchanged canonical
  journey. The smoke matrix covers both publishable artifacts on Ubuntu x64,
  Windows x64, and macOS ARM64; each cell verifies the manifest hash before
  install. Every cell is a dependency of `release-gate`.
- **PR semantics.** Every in-scope pull request builds once and runs the complete
  blocking release gate, including the six install-smoke cells. There is no
  dispatch-only, scheduled-only, warning, or `continue-on-error` substitute for
  PR qualification. Path filtering may not omit packaging-relevant changes.
- **Tag semantics.** `publish.yml` at the tag SHA invokes the same reusable gate,
  including a fresh one-time build and all required cells. It asserts the tag
  version matches artifact metadata. `publish` needs the green aggregate,
  downloads the already-hashed `dist/` artifact from that run, rechecks hashes,
  and uploads those exact files. It never rebuilds. A failed, skipped, or
  cancelled smoke cell prevents PyPI and GitHub release publication.
- **Bite proof without repository mutation.** Inside a dedicated in-job negative
  test, copy the downloaded artifact to a throwaway path, corrupt only that copy
  (remove a required member or alter a byte), and prove `package-verify`/smoke and
  the publish precondition reject it. The original hashed artifact remains
  untouched; no temporary branch, commit, push, tag, or publication is created.

### 2.4 W4 — stealth verification (`tests/test_stealth.py`, new `stealth` marker)

Two tiers, because stealth is the one place determinism and realism genuinely
conflict:

- **Offline/deterministic tier (GATING).**
  `tests/fixture_app/stealth_probe.html` is a passive collector, not its own
  judge. It publishes exactly one `window.__STEALTH_PROBE_RESULT__` object with
  schema version, start/finish monotonic timestamps, `complete: true`, and a
  keyed raw-observation record; it dispatches one named completion event. The
  test waits for the event/result with a bound, validates the closed schema, and
  fails on absent, duplicate, extra, or unserializable fields.
- **Versioned signal specification.** One reviewed table in `test_stealth.py`
  names every signal, collection expression, expected predicate, supported
  platform/Chrome allowance, and failure-control. It covers
  `navigator.webdriver`; an explicit exhaustive list/prefix policy for known
  CDP leak globals; plugins; languages; `window.chrome` required members;
  `Notification.permission` versus `permissions.query`; UA/platform and
  available user-agent client hints; patched builtin
  `Function.prototype.toString`; and absence of automation-revealing process
  flags. "Truthy", "looks normal", and silently accepting unavailable values are
  not predicates. A Chrome-major change that invalidates an allowance requires a
  reviewed table update.
- **Process evidence.** Flag assertions inspect the actual launched Chrome
  process command line and reuse W2's exact-binary identity; they do not infer
  runtime stealth from `test_stealth_args.py`. Existing arg-sanitization tests
  remain unit evidence only.
- **Ordered prerequisite CDP activity.** The probe loads in an `armed` state and
  cannot collect until explicitly released. For both the MCP browser and vanilla
  control, execute and assert this exact successful method sequence before
  release: `Runtime.enable` → `Page.enable` → `Network.enable` →
  `DOM.getDocument` → `Runtime.evaluate` of a nonce sentinel →
  `Page.captureScreenshot`; then call the probe's start function with one final
  `Runtime.evaluate`. Persist the ordered method/result transcript and fail on a
  missing, reordered, errored, or post-collection prerequisite. This prevents a
  pristine page from passing a leak check that real CDP activity would fail.
- **Sensitivity controls.** For each collector family, run a deterministic
  fixture/control that introduces the forbidden observation and prove the
  predicate fails. Resolve the vanilla control from W2's **same exact
  image-provided Chrome Stable executable/version** and match headless mode,
  viewport, locale, UA, fresh-profile policy, loopback network, and the ordered
  CDP sequence. Only the intentional treatment—product stealth behavior versus
  the documented vanilla automation control—may differ. Require the specified
  vanilla signals to fail and every supported product predicate to pass. If
  config identity is not proved or vanilla is not detected, the test is invalid.
- **Cross-OS gate.** The offline suite runs in all W2 real-Chrome cells. Its
  result artifact contains schema version, OS/architecture, exact Chrome
  identity, raw observations, predicate outcomes, and control outcomes, with no
  secrets or local profile contents.
- **Online/informational tier (NON-GATING, opt-in)** — `stealth` only, **excluded
  from every default run and from the release gate**; runs on
  `workflow_dispatch`/nightly. Drives the MCP against the **two detector pages the
  human chose (2026-07-15): CreepJS (`abrahamjuliot.github.io/creepjs`) and
  `bot.incolumitas.com`** — both research-oriented and tolerant of automated
  access — and writes a bounded, redacted report to the local job workspace (raw
  fingerprint material is not uploaded). It **must not fail the build on a detector score**
  (vendor-volatile) — it asserts only the same hard invariants as the offline
  tier and logs the rest for human review. Network flakiness here is expected and
  tolerated by design. If either page later blocks automation or changes ToS, the
  mitigation is to drop it and log the removal — never to make it gating.
- **Marker wiring**: add `stealth: anti-bot/undetectability checks; offline tier
  gates, online tier is opt-in and non-gating` to `pyproject.toml` markers.
  The release gate selects the offline tests explicitly by node/secondary marker;
  marker overlap may not accidentally collect the online tests. CI proves the
  default gate contains zero public URLs.

### 2.5 W5 — release contract + limitations register + transport manifest

- **Qualified matrix, not an OS-family generalization.** The root
  `RELEASE_CONTRACT.md` names the exact release-SHA evidence: GitHub-hosted
  Ubuntu x64, Windows x64, and macOS ARM64; CPython cells actually run; and the
  image-provided Chrome Stable executable/version recorded by W2. It explicitly
  excludes untested Linux distributions, self-hosted runners, Windows ARM64,
  Intel macOS, IPv6-only loopback, non-stable Chrome channels, and future runner
  images. "Linux, Windows, and macOS" may appear only with those qualifiers in
  the same paragraph/table.
- **Transport qualification.** stdio is qualified by W1/W2/W3. HTTP is described
  separately as unauthenticated and loopback-default; it is qualified only to
  the extent exact live tests support it. Do not let stdio evidence license an
  HTTP claim.
- **Upgrade qualification.** The contract names only W14's human/admin-supplied,
  ordering-verified, hash-pinned **literal N-1** stable tag as the migration
  baseline. It makes no arbitrary-older, same-version, prerelease, or local-build
  upgrade/rollback claim.
- **One acceptance ledger schema and path.** `tools/release_evidence.py` is the
  sole parser/generator for closed schema `release-evidence/v1`. Every required
  job/matrix cell writes exactly
  `release-evidence/<release_sha>/<job_id>/<matrix_cell>.json` with fields:
  `schema`; `release_sha`; `workflow{name,run_id,run_attempt,event}`;
  `job{id,matrix_cell,terminal_outcome}`;
  `runner{os,arch,image_os,image_version}`; `python_version`;
  `chrome{path,executable_version,launched_major}` (or explicit `null` for a
  non-browser job); `pytest{junit_sha256,executed_node_ids,skipped,xfail,failed}`;
  `artifacts[{name,path,kind,sha256}]`; and `mq_ids`. Outcomes and IDs use closed
  enums/patterns; lists are deterministically sorted and duplicate-free.
- **Fail-closed aggregate evidence.** `release-evidence` uses the same parser to
  generate
  `release-evidence/<release_sha>/release-gate/aggregate.json`. Its conditional
  aggregate record lists the exact required job/cell keys and the path+SHA-256 of
  every child record. It rejects a missing/extra/duplicate child, wrong SHA or
  workflow run, non-success terminal outcome, skipped/xfail/failed required node,
  missing Chrome identity for a browser cell, JUnit/artifact hash mismatch, or
  missing/duplicate MQ id. `release-gate` directly needs both this child and all
  children it validates; the ledger cannot turn a failed job green.
- **One evidence API for contract and parity.** `tools/gen_release_contract.py`
  imports `release_evidence.py` and emits the generated matrix/tool tables; W8
  imports that same parser, and W11 imports the contract generator's public API.
  Negative tests cover unknown/missing fields, malformed SHA/run/job/cell,
  duplicate nodes/MQs, invalid outcome, skipped/xfail evidence, stale release
  SHA, absent/wrong Chrome identity, and JUnit/child/artifact hash mismatch.
  Generated output is reproducible and CI fails on drift. The §0.1 `Current
  support` table is prose context and is structurally forbidden as ledger input.
- **Exact meaning of `release-qualified-success`.** A row names the precise user
  outcome asserted, fully-qualified passing node, required transport, fixture or
  site shape, and required OS cells, and each is present as current-run success
  evidence in the ledger. A schema/type/non-null assertion, `.fn`-only call,
  representative journey, error-only test, exemption, or characterization cannot
  satisfy a transport, site-shape, manual-QA, or cross-OS success claim. Each
  served tool is `release-qualified-success`, `served-unqualified` with tracking
  id/user impact, or `not-served`; these states are never inferred from F-108 set
  equality.
- **`get_cookies` hard block.** At W5 start, successful real-browser cookie
  retrieval has not been proved. The generator therefore refuses a
  94-release-qualified-tool statement. W5 may finish only after either (a) a
  real Chrome + real transport success test sets a cookie, retrieves it, and
  asserts its value, or (b) the contract and all generated claims explicitly say
  **93 release-qualified tools plus `get_cookies` served-unqualified**, with its
  route/limitation, and MQ-53/MQ-111 are explicitly rewritten as manual
  verification of that visible exclusion and of every **release-qualified**
  tool—rather than retaining false retrieval/all-served success requirements.
  Option (b) needs a passing generator/docs test proving the tool is never
  presented as qualified; it does not relabel its characterization. A
  mock-only success, missing-instance error, schema check, or
  characterization cannot satisfy option (a). No other tool may inherit a
  success claim from F-108 set equality alone.
- **Limitations and ceiling.** Enumerate every still-open E8-1…4, E7-1, E7-6,
  F-181 internal non-acceptance/unsupported stale-live-node characterization,
  F-165, close-flake finding; missing interaction surface; HTTP/exec trust
  boundaries; architecture/channel exclusions; native IME limits; live-web
  informational status; and any W12–W16 routed finding. It also emits W7's exact
  public-surface exclusions: stale live handles; recursive/frame-targeted content
  or interaction; redirect chains/loops; typed loading-failure/truncation;
  downloads; MCP-network SSE/WS detail; and generic/closed-shadow access. Each has
  user impact and exact evidence status. The contract refuses an "any site
  forever" or universal-undetectability promise (§8).

### 2.6 W6 — scheduled informational observation

A green suite at a release SHA says nothing about tomorrow — the web and Chrome
move under you. W6 provides repeatable read-only observations a human may inspect;
it is not a release gate, a permanent-coverage promise, or a commitment to detect
or repair drift within any interval.

- **Scheduled read-only canary** (`.github/workflows/canary.yml`, cron + manual
  dispatch): runs W1/W4 against the local fixture and, separately, read-only
  observations against the approved live corpus/detectors. Local deterministic
  failures make the job red. Public-page reachability or vendor result changes
  are labelled informational in the log and cannot gate or license a claim.
- **No external mutation or notification.** The workflow has read-only repository
  permissions and no issue, PR-comment, webhook, email, chat, third-party upload,
  or write-token step. A red scheduled run is the signal a human may inspect.
  This plan does not promise automatic paging or MTTR.
- **Local fixture repro only.** One bounded helper writes only synthetic fixture
  call ids, fixture URL/path ids, deterministic oracle values, and the W2
  environment/Chrome identity to a caller-supplied throwaway directory. It does
  not capture general DOM, screenshot, request headers/bodies, cookies, profile
  data, or live-site content. It refuses default home/repository destinations,
  overwrite, and link traversal. W12 later defines the one canonical threat and
  redaction API used for richer diagnostics; W6 does not invent one.
- **Chrome/upstream observation.** A scheduled run records whichever
  image-provided Chrome Stable the runner supplies at that future run. It neither
  retroactively qualifies the release SHA nor establishes support for historical,
  future, or non-stable Chrome versions/channels; those remain informational.
- **Nightly soak lane**: a longer manual/scheduled run drives a high-count
  workload (≥1000 tool calls, or a fixed wall-clock) and asserts **no memory/handle
  growth and no latency drift across the run** — the endurance shape of a real
  multi-hour session, distinct from W9's short leak ceiling. Non-gating (too long
  for a PR); a breach is visible as a red scheduled run, with no automatic
  external notification.
- **Non-gating by construction**: the online/live half never blocks a PR or tag
  and is never cited as deterministic evidence. W6 is drift observation, not a
  promise that an arbitrary site works.

### 2.7 W7 — adversarial breadth (the honest answer to "any site")

"Any site" is an unbounded universal quantifier over an adversarial, changing
domain—unprovable by a finite suite. W7 therefore owns eight exact deterministic
shapes, MQ-114…121, in `tests/test_e2e_dynamic_sites.py`. Origin A and origin B
are independent `127.0.0.1:0` servers; every controller is event-backed and
released before teardown. Page/route names, protocol behavior, independent
oracles, tests, and MQs are atomic:

| MQ | Pages/routes and exact protocol | Independent acceptance oracle | Exact test node(s) |
|---|---|---|---|
| **MQ-114** | A:`/spa_history.html` performs `pushState` → `replaceState` → `back`/`popstate`; every transition replaces `#route-root` and increments a generation token. | After every transition the test issues a **fresh public selector query and action**, then asserts exact route/action/generation logs and the newly selected element's content. It does not retain or exercise an old backend node. | `tests/test_e2e_dynamic_sites.py::test_spa_history_route_swap_and_requery` |
| **MQ-115** | A:`/frames/a_outer.html` → B:`/frames/b_middle.html` → A:`/frames/a_inner.html`, with fixed frame metadata tokens. | `get_page_content(include_frames=True)` returns the top-level page plus metadata for its **direct B child**, and the A-B-A page causes no hang/crash. No recursive inner-A content, frame control targeting, or cross-frame interaction is asserted. | `tests/test_e2e_dynamic_sites.py::test_cross_origin_a_b_a_direct_metadata_and_limit` |
| **MQ-116** | A:`/lazy_virtual_infinite.html`; one IntersectionObserver target; a 1,000-logical-row/20-node recycled pool; A:`/api/feed?page=0..3` returns exactly four ordered 25-row pages and page 4 returns the declared terminal sentinel. | Lazy token appears only after controlled intersection; virtual node identities recycle while logical ids/text remain exact; finite-infinite load ends at 100 unique ordered rows with four requests and no page-4 append. | `tests/test_e2e_dynamic_sites.py::test_intersection_observer_lazy_load`; `tests/test_e2e_dynamic_sites.py::test_virtualized_and_finite_infinite_lists` |
| **MQ-117** | A:`/csp/strict` returns exact `Content-Security-Policy: default-src 'self'; script-src 'nonce-e2e9'; connect-src 'self'; object-src 'none'; base-uri 'none'`; page attempts nonce script, inline script/eval, self fetch, and B fetch. | Header bytes match; nonce script and self fetch succeed; inline script/eval and B fetch are blocked; ordered `securitypolicyviolation` entries name the expected effective directives and blocked origins. | `tests/test_e2e_dynamic_sites.py::test_strict_csp_surface` |
| **MQ-118** | A:`/auth/basic` requires an exact Authorization header and returns a fixed final body/status/header set; A:`/redirect/start` resolves to `/redirect/final`; A:`/redirect/to-b` resolves to B:`/redirect/final`; B:`/cors/echo` handles OPTIONS/POST and `/cors/blocked` omits ACAO. | Assert the sent auth header, final loaded page token, final response status/headers, and allowed/blocked CORS final outcomes. Redirect hop ids/order/count and loop diagnosis are not asserted. | `tests/test_e2e_dynamic_sites.py::test_auth_redirect_cors_preflight` |
| **MQ-119** | A:`/payload/text` returns fixed text; `/payload/binary` returns 4,096 seeded bytes; `/payload/chunked` returns three chunks that complete normally; `/status/418` and `/status/503` return fixed final headers/bodies. | Public network tools return ordinary request metadata, exact final status/headers, completed text, completed binary body in declared base64 form, completed assembled chunked body, and preserved 4xx/5xx bodies/hashes. No loading-failure, truncation, or download behavior is asserted. | `tests/test_e2e_dynamic_sites.py::test_completed_text_base64_binary_chunked_and_http_errors` |
| **MQ-120** | A:`/events/sse` emits ids 1..3 then EOF; A:`/events/ws` sends `alpha,beta,gamma` then close 1000. The page records both streams in a bounded sentinel. | Through public `execute_script`, wait for and assert the page sentinel's finite SSE/WS connectivity, order, data, and close state. No MCP network-debugging SSE-event or WebSocket handshake/frame/message visibility is asserted. | `tests/test_e2e_dynamic_sites.py::test_sse_and_websocket_lifecycle` |
| **MQ-121** | A:`/popup_components.html` has a tokenized `target="_blank"` link to `/popup_target.html`; a `fixture-card` custom element, cloned template, nested light-DOM slots, and an open-shadow escape-hatch sentinel expose bounded lifecycle logs. | Popup flow is exactly `list_tabs` → `switch_tab` → assert URL/content → `close_tab`; generic selector/script asserts custom-element light DOM, lifecycle, template, and slot logs. Open shadow is inspected only through explicit `execute_script`; no generic or closed-shadow support is asserted. | `tests/test_e2e_dynamic_sites.py::test_custom_elements_slots_and_popup_lifecycle` |

- **Atomic ownership.** All eight MQ entries, every page/route/controller, the
  test nodes above, fixture enumeration backstop, and W5 ledger rows land in W7's
  one checkpoint. No placeholder, shorthand taxonomy claim, or route without its
  independent oracle lands first.
- **Public-surface limitations generated into W5.** Public element tools
  re-resolve selectors and expose no actionable stale live-node handle, so stale
  handle semantics are excluded. Frame support is limited to direct-child
  metadata from `get_page_content(include_frames=True)`; there is no public
  frame-targeting or recursive nested-frame interaction/content contract, and
  same-/cross-origin control targeting is unsupported/not gated. Redirect
  interception overwrites redirect request ids and exposes no chain field, so hop
  order/count and loop diagnosis are excluded. There is no typed public contract
  for loading failure/truncated bodies and no MCP download tool, destination,
  completion, or path contract; downloads are unsupported/not gated. SSE/WS are
  page-runtime sentinel checks only—MCP network debugging does not qualify SSE
  events or WebSocket handshakes/frames/messages. Custom-element light DOM is
  generic; open shadow is an `execute_script` escape hatch, and generic/closed
  shadow support is excluded.
- **Seeded variation only around the table.** Property cases may vary bounded
  counts/text/ordering using a recorded seed, but cannot replace any fixed oracle.
  Success is the exact outcome or specified typed failure—not type/non-null,
  silent `True`, or characterization.
- **Chrome/differential scope.** W7 uses only each W2 runner's image-provided
  Chrome Stable. W4's vanilla/product differential uses the same exact executable,
  version, operational config, and ordered CDP sequence. Other Chrome
  versions/channels may be observed only in scheduled informational runs and are
  never release-qualified; no installer or nonexistent product selector is used.
- **Live corpus wording.** `tests/corpus/sites.toml` is a read-only, ToS-reviewed
  input to W6 scheduled informational observation. It is a sample, never a
  gate, permanent site guarantee, framework/vendor guarantee, or substitute for the
  table above.
- **Explicit HTTPS limitation.** The deterministic fixture is loopback HTTP and
  does not cover TLS negotiation, certificate errors/pinning, HSTS, mTLS, or
  HTTPS/mixed-content policy. A read-only scheduled HTTPS observation may be
  logged, but no TLS/certificate claim enters W5 without a future deterministic
  fixture and separately approved plan. Dedicated/shared/service workers are W16.

### 2.8 W8 — manual-QA parity (the "blind push" contract)

- **Numbering contract.** W8 establishes exact executable parity for remediation
  baseline MQ-1…113 plus W7's atomically landed MQ-114…121. It does not trust a
  prose estimate of mappings: it resolves each from current collection and the
  current-SHA ledger. W9 reserves MQ-122…125, W10 MQ-126…129, W11 MQ-130, W12
  MQ-131…137, W13 MQ-138…144, W14 MQ-145…149, W15 MQ-150…154, and W16
  MQ-155…162. A later
  workstream appends its reserved steps only in the same commit as their tests
  and evidence-ledger update; no placeholder MQ entry lands alone.
- **Exact evidence grammar.** Every MQ heading contains machine-readable lines
  generated/validated from W5's ledger and matches the protocol at HEAD:
  `Evidence: satisfied`, `known-gap`, `blocked`, or `planned`. Current pytest
  evidence uses `pytest:` plus an exact fully qualified collected node id and
  required runner-cell set; future nodes use `planned-pytest:` and CI-only future
  evidence uses `planned-runtime:`. Runtime success must resolve to the exact
  `release-evidence/<release_sha>/<job_id>/<matrix_cell>.json` child through W5's
  parser; it cannot be reconstructed from prose or a job display name.
  Labels such as "Windows CI", partial names, comments, or screenshots alone are
  invalid. The parser owns and round-trips this grammar.
- **Static parity tripwire.** `tests/test_manual_qa_parity.py` asserts IDs are
  contiguous/unique, evidence tokens parse, pytest node ids exist in current
  `--collect-only` output, workflow/job/cell ids exist and feed `release-gate`,
  every W5 ledger MQ reference resolves, and no evidence target is orphaned.
- **Runtime parity.** A small pytest evidence plugin emits exact collected and
  terminal outcomes/JUnit for each child ledger. W5's shared parser—not a second
  W8 reader—validates them. The aggregate verifier rejects missing,
  deselected, skipped, xfailed, xpassed, conditionally uncollected, cancelled, or
  non-success required evidence. CI-only workflow evidence is accepted only from
  the current SHA's completed PR/tag run. A stale successful run is not evidence.
- **Characterization is not success.** A characterized node may appear only as
  `known-gap` with its matching route. `known-gap`, `blocked`, and `planned` all
  remain unsatisfied for release readiness; none can satisfy a supported-tool or
  blind-push claim. A separately authorized fix or an explicit reviewed narrowing
  of the manual/contract scope must update the MQ text and evidence honestly to
  `satisfied`; relabelling the green characterization is forbidden.
- **Negative-space coverage** (where the user's edge cases actually live) — the
  protocol explicitly checks the tool **fails correctly**: click a disabled control
  → typed failure, not silent `True` (E8-2); type into readonly → refused (E8-3);
  and bad selector → typed error. For SPA replacement, public tools perform a
  fresh selector query/action as MQ-114 requires; they expose no actionable stale
  live-node handle. **MQ-42 is this public SPA fresh-selector re-resolution
  contract and reuses W7 MQ-114
  `tests/test_e2e_dynamic_sites.py::test_spa_history_route_swap_and_requery`.**
  F-181 stale live-node behavior is internal non-acceptance characterization/
  unsupported surface, never a planned public acceptance test.
  First-class MQ steps, because blind-push means trusting the *failure* paths as
  much as the success paths. Where current behavior is a known bug, the MQ step is
  pinned to its characterization test **and flagged in the protocol as a known
  gap**, so a parity-green never masks it (this is the anti-cheat for §8.1's
  "assert nothing").
- **Lifecycle / leak / recovery**: spawn→close
  N times asserts no orphaned Chrome processes / fd growth (psutil child-count
  before == after); kill Chrome mid-session asserts a typed error, not a hang; the
  close-path flake gets a bounded-wait fix **in the test harness** (not `src`) or a
  characterization pin + route.
- **Multi-instance concurrency** (manual step): two instances driven interleaved
  don't cross-talk (separate profiles/ports/tabs); the singleton path is exercised
  through the real transport.
- **Mutation analysis** may add one test-extra tool and run manually/scheduled.
  Its score is informational, recorded with the mutation scope, and never a
  blocking-release or coverage-completeness claim. Surviving user-path mutants
  route to tests; the score cannot be padded by excluding hard modules silently.
- **Flake rule.** A required test that flakes loses valid success evidence and
  blocks the affected claim. `skip`/`xfail` quarantine is not a green workaround;
  the choices are fix the harness, retain a routed characterization and narrow
  the contract, or keep the gate red.

### 2.9 W9 — performance & resource budgets (the "great performance" pillar)

A functionally-green suite can hide a tool that is *correct but unusably slow* or
that *leaks memory over a session*. W9 asserts the second half of "works super
well": fast enough, bounded footprint. All budgets are measured against the
**fixture app** (no live network), so they are **deterministic and gate** — the
same reason W4's offline tier gates.

- **Reviewed workload/budget table.** `tests/test_perf.py` (`integration` +
  `perf`) owns one table naming each measured user operation, fixture state,
  warm-up count, sample count, statistic, runner cell, timeout, latency budget,
  memory budget, and correctness oracle. Only operations in that table are
  claimed. Before freezing values, collect two clean current-SHA baseline runs on
  each gating cell; derive a documented generous factor/floor once, check the
  numbers into the test, and retain the raw summary in the completion report.
  Later budget increases require a new measured justification and review; they
  cannot hide a regression.
- **Resource lifecycle.** Attribute RSS and child count to the server+Chrome
  process tree. Track handles on Windows and file descriptors on POSIX; record
  unsupported platform counters explicitly rather than treating zero as a
  measurement. Capture baseline after controlled warm-up, peak during the fixed
  operation sequence, then close through MCP and poll to a fixed deadline. Pass
  requires all owned children gone, session/profile paths removable, handle/fd
  count returned within the reviewed delta, and RSS returned within the reviewed
  post-close delta. Always run cleanup in `finally` and emit diagnostics on breach.
- **Startup budget.** Reuse W1's launcher, handshake, stderr capture, and clock
  boundary. Measure from child creation immediately before `initialize` through
  successful `list_instances`; process discovery or fixture startup is not hidden
  outside the interval.
- **Specified large-DOM oracle.** Add one page that deterministically creates
  10,240 seeded record rows across 256 sections, with nested text, attributes,
  styles, and fixed boundary sentinels. It publishes a ready sentinel containing
  schema version, exact DOM node count, row count, and checksum. The test
  independently computes the expected selected-record count/content/checksum;
  it does not trust the page's checksum as its only oracle. Extraction/clone/file
  output must match that oracle, including explicit completeness or documented
  truncation semantics, within the frozen bounds.
- **Specified network-load oracle.** The same scenario issues exactly 1,024
  seeded loopback requests with predictable request ids, status classes, and body
  hashes. Wait for an exact completion sentinel, assert capture count/unique ids/
  boundary records/aggregate checksum, export and re-read the result, then clear
  capture and prove cleanup. Server-side counters are reset in `finally`; no
  request may escape loopback.
- **Perf-regression baseline lane (informational, in the W6 canary)** — compare
  the current local timing summary to the checked-in reviewed baseline and make
  the scheduled job red on a configured regression. Do not upload a profile or
  notify externally. It is an early warning, not a release qualification row.
- **Discipline**: any measured breach that is a *product* perf bug → a
  characterization pin + route (a new F-id), **never** a `src/` fix here (§1.2).
  Budgets are tuned once against green baselines; padding a budget to hide a
  regression is the §8.1 "score-chase" cheat and is banned.
- **Parity landing.** Append MQ-122…125 and their exact live evidence only in the
  W9 commit. Characterization of a breached budget keeps the MQ a known gap; it
  is not a passing performance claim.

### 2.10 W10 — resilience / fault-injection (the dynamic edge cases)

W8's negative-space steps are **static** (a disabled button, a readonly field).
The edge cases users actually hit are **dynamic**: the browser dies, a tab vanishes,
the network drops mid-operation. W10 injects those faults and asserts the tool
**fails in a typed, recoverable way** — never a hang, never a raw `−32000`, never a
silent wrong-`True`, and the server stays usable afterward. `tests/test_resilience.py`
(`integration` marker; faults injected through the real transport where possible,
harness/CDP where injection requires it).

- **Crash recovery.** Identify the owned Chrome process from W2's process
  evidence, terminate it mid-session, await confirmed exit, then call the next
  tool under the W1 outer bound. Assert the exact typed outcome the product
  contract specifies, then spawn/navigate/close a fresh instance. Pass also
  requires no orphan, unlocked singleton state, and removable prior profile.
- **Tab-closed-under-tool.** Use an event barrier so an operation has definitely
  started before closing its tab out of band. Assert one terminal response for
  the same request id, no silent success, then prove another tab operation and
  clean close work.
- **Controlled fixture routes.** Add token-addressed controllers with
  `entered`/`release` events, never `sleep` as synchronization:
  (a) a slow-success route that waits for release and then returns an exact body;
  (b) a hang-before-headers route; and (c) a hang-after-headers/partial-body route.
  Every wait has a fixture safety ceiling. Handler teardown treats
  `BrokenPipeError`/connection reset as expected, records disconnect, and exits.
  Fixture finalization sets every release event before server shutdown and asserts
  all handlers terminate, so even a failed test cannot wedge pytest.
- **Load-state coverage.** Exercise both `load` and `networkidle` behavior against
  the appropriate hang phase. The product timeout is strictly inside a larger
  test outer timeout. Assert exact error type/code and M6-pinned message bytes,
  then release the handler and prove a normal navigation succeeds. The
  slow-success control must complete when released before the product deadline,
  proving the timeout test is sensitive rather than universally broken.
- **Network drop mid-op** — via CDP `Network.emulateNetworkConditions` (offline) or
  route-abort, cut connectivity mid-navigate / mid-capture; assert a typed error,
  recoverable.
- **Recovery invariant** — after *every* injected fault the server must be usable
  again: close/respawn works and leaves no orphan (ties back to W9's ceiling). A
  fault that leaves the server wedged is the finding; if it's a product bug it is
  characterization-pinned + routed, not fixed here.
- **Characterization discipline.** If current behavior is a hang, raw `-32000`,
  or wrong success, the harness's outer bound must still terminate the test,
  record the F-id characterization, and narrow W5. It does not satisfy the MQ or
  W10 DoD.
- **Parity landing.** Append MQ-126…129 and their tests/evidence in this same
  commit; no route or MQ placeholder lands separately.

### 2.11 W11 — documentation-example tests (docs never lie)

A user's first five minutes are spent copy-pasting from the README. A broken
example is a worse first impression than any internal bug, and no internal green
catches it. `tests/test_doc_examples.py`:

- **Extract-and-run** — parse fenced code blocks in `README.md` and `docs/*.md`
  that use one exact reviewed runnable marker. Before execution, statically
  reject external URLs, undeclared file writes, secrets, interactive prompts, and
  shell metacharacter ambiguity; execute in a bounded throwaway directory against
  the W1 server/fixture. Record exact source file and fence ordinal as evidence.
- **Claims-sync** — assert the README's advertised install command matches the
  actual console-script name, and its advertised tool list is a subset of the live
  `tools/list` **and is release-qualified in W5's ledger**. Import W5's
  `gen_release_contract.py` API rather than scraping/generated-copying a second
  source of truth (ADDENDUM_LENSES). A served-unqualified tool such as
  `get_cookies` cannot be advertised as qualified merely because it is listed.
- **Scope**: only marked-runnable blocks; a doc example that hits a known-bug path
  is pinned + flagged exactly like an MQ known-gap, never quietly deleted.
- **Parity landing.** Append MQ-130, its exact runnable-example test, and W5-ledger
  reference in the same W11 commit.

### 2.12 W12 — security and trust-boundary verification

This workstream tests the existing boundary; it does not pretend an exec-capable
local automation server is a sandbox.

- **Threat contract first.** Generate a table for stdio and HTTP covering caller
  trust, bind exposure, authentication status, host-code execution, browser-code
  execution, filesystem reads/writes, uploads, the absence of an MCP download
  contract, and secrets. Assert HTTP's
  default bind is literal loopback and remote exposure requires explicit user
  action. The contract says plainly that an untrusted MCP client is out of scope.
- **Canonical policy in W5's API.** Extend `tools/release_evidence.py` and the W5
  contract generator—do not add a parallel policy file/parser—with one typed
  threat-boundary/redaction table. It classifies URL userinfo/query values,
  authorization/cookie headers, environment canaries, DOM/form values, script
  arguments, and sensitive path components; specifies preserve/drop/replace
  behavior and bounded replacement format; and is emitted into the contract.
  Negative tests prove each secret class is absent from policy-processed output,
  while required error type/code/correlation fields survive. All later diagnostic
  work imports this exact API without redefining a rule.
- **Exec-capable tools.** Use harmless canary values to prove whether JavaScript
  stays in browser execution contexts and whether Python bindings run with host
  privileges. Assert returned/error data follows W12's canonical rules. The test
  must not claim isolation that the implementation does not provide; observed
  host privilege is documented as a trust requirement.
- **One filesystem matrix.** Inventory every `*_to_file`, network import/export,
  clone-file, and upload path. In a throwaway tree, cover relative and
  absolute paths, `..`, mixed separators, drive-relative/UNC/device-like Windows
  forms, duplicate names, existing targets, symlinks on POSIX, and
  symlink/junction/reparse escapes on Windows. Assert exact bytes, exact resolved
  destination, overwrite policy, and cleanup. All escape probes target canary
  files inside the throwaway parent—never real user files.
- **No silent security pass.** A capability intentionally allowed by the trusted
  caller model is recorded as such; an unintended extra target, secret leak, or
  remote-default bind is a release blocker routed to a production-fix stream.
  Characterizing it is not enough to qualify the release.
- **MQ allocation.** Append only with live tests: MQ-131 transport/bind/threat
  contract; MQ-132 browser-JS and host-Python execution boundaries; MQ-133 the
  complete normal `*_to_file`/import/export matrix; MQ-134 traversal/absolute/
  platform path semantics; MQ-135 symlink/junction/reparse semantics; MQ-136
  overwrite/collision/cleanup; MQ-137 upload exact bytes/name plus the canonical
  secret-redaction matrix and generated no-download limitation.

### 2.13 W13 — wire concurrency, cancellation, and interoperability

- **Independent client.** Add the official `mcp` SDK to the `test` extra only and
  drive W1's absolute launcher without importing FastMCP server internals. It must
  independently initialize, list schemas, call a success and typed-error path,
  and close. FastMCP-client success remains W1 evidence; it cannot substitute for
  this interoperability result.
- **Deterministic barriers.** Reuse W7's event-controlled deterministic endpoints
  (already available transitively through W8) to place calls in-flight without
  sleeps. Cover concurrent calls on one instance and separate
  instances, intentionally reversed completion, duplicate-looking payloads, and
  verify each response/result/error remains attached to its request and instance.
- **Cancellation/disconnect.** Send protocol cancellation for a confirmed
  in-flight request, close a client mid-request, and close stdin with requests in
  flight. Each has an outer watchdog, exactly-one-terminal-outcome oracle, owned
  process cleanup, and a subsequent fresh-client recovery check. Unsupported
  cancellation is characterized and narrows the contract; a hang can never pass.
- **Framing/backpressure.** Exercise large bounded results, simultaneous stderr
  diagnostics, slow reader/backpressure, and malformed input. Assert stdout
  remains parseable protocol frames with no diagnostic bytes, stderr stays
  bounded, memory stays within W9's ceiling, and shutdown does not deadlock.
- **HTTP parity.** Where HTTP is contract-qualified, run the same concurrency and
  cancellation semantics against loopback HTTP and list any intentional
  transport difference. stdio evidence cannot be copied into the HTTP column.
- **MQ allocation.** MQ-138 official-client initialize/list/call; MQ-139
  same-instance concurrency plus multi-instance isolation; MQ-140 reversed
  completion/correlation; MQ-141 cancellation; MQ-142 client disconnect; MQ-143
  framing/stderr/backpressure/malformed input; MQ-144 shutdown with in-flight
  calls plus explicitly scoped HTTP parity. Append each only with its exact test
  and W5-ledger evidence.

### 2.14 W14 — literal N-1 upgrade, migration, and rollback

- **Human/admin-supplied immutable N-1.** Before implementation, the human/admin
  supplies the immediately preceding stable release tag, artifact filename/source
  URL, and immutable SHA-256. The executor verifies release ordering proves it is
  literal N-1 for the target release, tag/version/metadata agree, and bytes match
  the hash. An arbitrary older tag, same-version reinstall, prerelease, mutable
  URL, or local rebuild is rejected. Missing/ambiguous N-1 evidence is a STOP.
- **Isolated user state.** Run in throwaway HOME/config/cache/session roots and
  inventory every persisted surface before writing expectations. Install N-1,
  run a minimal journey, create only documented state, then install W3's exact
  current artifact into the same environment and run the canonical journey.
  Assert launcher/version/package-data resolve to current and no duplicate or
  stale entry point wins.
- **Live-backend boundary.** Exercise the documented upgrade procedure plus the
  N-1 singleton/version-mismatch case. Never replace a running executable by
  force. Assert bounded stop/restart, profile/session ownership, and no orphan.
- **Rollback.** Reinstall the hash-pinned N-1 artifact after current using a copied
  throwaway state tree. Either prove the documented rollback behavior or mark it
  unsupported without corrupting the original state. No test touches a real user
  profile.
- **Three-OS exact-artifact gate.** Clean install, in-place upgrade, and launcher
  checks run on all W2 cells. Network fetch is only for the hash-pinned
  N-1 artifact; current bits always come from W3's artifact.
- **MQ allocation.** MQ-145 verified N-1 identity/journey; MQ-146 in-place upgrade;
  MQ-147 singleton/persisted-state migration; MQ-148 rollback behavior; MQ-149
  current launcher/version/package-data and stale-entry cleanup. Append with live
  tests only.

### 2.15 W15 — observability on failure

- **Structured diagnostic oracle.** For representative validation, browser,
  timeout, cancellation, and filesystem failures, assert a stable error type/code,
  request or operation correlation where exposed, failed phase, actionable local
  next step, exact M6-pinned message bytes, and no protocol/stdout contamination.
  Missing correlation/recovery context is characterized and appears as a contract
  limitation; it is not invented in test prose.
- **Secret canaries.** Place unique values in URL credentials/query, headers,
  cookies, environment, form/DOM data, filesystem paths, and script arguments.
  Process every diagnostic through W12's canonical policy API, then search stderr,
  failure messages, local bundle, pytest output, and generated
  reproduction command byte-for-byte. Any unauthorized canary disclosure is a
  release blocker; do not bless it as a known-safe limitation.
- **Bounded capture.** Induce a large DOM/body/stderr failure and assert per-field
  and total diagnostic limits, explicit truncation markers/checksums, valid UTF-8
  or declared binary encoding, atomic local writes, and cleanup after replay.
- **Repro is local and single-source.** Reuse W6's throwaway destination/bundle
  writer and W12's canonical policy API; do not add another redactor, policy
  table, or contract generator. From a fresh directory, replay the sanitized
  transcript against the deterministic fixture and reproduce the same typed
  failure. Assert no DNS/public request, issue/comment/webhook, credential access,
  or write outside the destination.
- **MQ allocation.** MQ-150 structured/correlated errors; MQ-151 secret
  redaction; MQ-152 bounded/truncated diagnostics; MQ-153 environment/recovery
  guidance; MQ-154 local replay and no external mutation. Append with tests only.

### 2.16 W16 — stateful/PWA and internationalized site shapes

- **Dedicated worker fixture.** A versioned worker receives ids 1..3, returns an
  independently predicted transform/hash in order, emits a close sentinel, and
  is terminated; assert no later message or worker remains.
- **Shared worker fixture.** Two fixture tabs connect to one versioned shared
  worker, receive fixed port ids and shared counter sequence, close in controlled
  order, and trigger an exact zero-client/teardown sentinel. Cross-profile
  instances must not share its state.
- **PWA fixture.** Add a versioned service worker served from loopback with exact
  install/activate/controller/cache sentinels and a deterministic offline response.
  Test first load, controlled reload, cached/offline fetch, unregister, cache
  deletion, and server teardown. Never reuse a runner's ambient browser profile.
- **State stores.** Add separately seeded CacheStorage and IndexedDB datasets plus
  local/session storage and cookie records with independent counts/keys/hashes.
  Assert cache/offline bytes and IndexedDB transaction/index results separately;
  assert persistence
  only across the documented same-profile lifecycle and isolation across separate
  MCP instances/profiles; cleanup must remove all fixture state.
- **Internationalized oracle.** Use fixed NFC/NFD pairs, combining characters,
  emoji/ZWJ, non-BMP text, RTL and bidi-isolate strings, and mixed-direction
  attributes. Assert exact code points and DOM value/text/action-log round trips,
  not visual pixels or locale-dependent rendering.
- **Composition honesty.** Test the DOM composition/input event sequence the tool
  can deterministically synthesize. Native OS IME selection/candidate UI is not
  automatable on hosted headless runners and remains an explicit W5 limitation;
  synthetic events must not be advertised as native IME proof.
- **MQ allocation.** MQ-155 dedicated-worker lifecycle; MQ-156 shared-worker
  multi-tab/profile lifecycle; MQ-157 service-worker install/activate/controller/
  unregister lifecycle; MQ-158 CacheStorage/offline byte oracle; MQ-159 IndexedDB
  transaction/index oracle; MQ-160 storage/cookie persistence and profile
  isolation; MQ-161 Unicode/RTL exact round trip; MQ-162 composition sequence plus
  native-IME limitation. Append with tests and evidence in the same commit.

---

## 3. Sequencing — RELEASE-1 through RELEASE-16

> Before RELEASE-1, rerun Phase 0 at HEAD, record base branch/SHA, and prove the
> M4-Ph1+A1, M5b, and M14+A1 merge SHAs are ancestors. Every step is one stacked,
> independently reviewable PR/checkpoint atop the prior step. Run the appropriate
> gate after each; local Windows pytest uses
> `& ".venv\Scripts\python.exe" -m pytest ...`. `--no-verify` is banned and hooks
> must run. CI-only acceptance comes from that PR's current-SHA jobs. A changed
> anchor, required cheat, runtime dependency, production edit, or unsatisfied
> release-blocker means STOP and hand back; the executor never merges.

- **RELEASE-1 — W1 canonical transport harness.** Prove FastMCP 2.11.2 with the
  absolute installed launcher, then land the shared journey and transport tests.
  DoD: resolver negative controls reject bare/wrong-environment launchers;
  initialize/list/call/journey/shutdown pass twice; stderr is bounded; no child or
  fixture remains; unit gate unchanged. **Commit:** `RELEASE-1: canonical absolute-launcher stdio E2E (G-A)`.
- **RELEASE-2 — W2 three-OS gate.** Land the reusable workflow, per-OS coverage,
  lifecycle cases, real-Chrome identity, and aggregate. DoD: the PR shows green
  Ubuntu/X64, Windows/X64, and macOS/ARM64 cells with identity artifacts; a
  deliberate `quality` failure and a deliberate single matrix-cell failure each
  turn `release-gate` red; read-only ruleset evidence
  proves that stable check blocks merge. **Commit:** `RELEASE-2: required Ubuntu-x64 Windows-x64 macOS-arm64 release gate (G-B)`.
- **RELEASE-3 — W3 exact-artifact install/publish topology.** Build once, hash,
  smoke wheel+sdist on each OS, and wire publish to those files. DoD: all six PR
  smoke cells are current-SHA green; in-job copied-artifact package-data and
  hash-mismatch bite proofs
  are recorded; workflow inspection/dry-run proves no rebuild and no publish on a
  failed/skipped cell. **Commit:** `RELEASE-3: test exact built artifacts before publishing the same files (G-C)`.
- **RELEASE-4 — W4 stealth probe.** Land the closed schema/predicate table,
  ordered prerequisite CDP transcript, controls, process-flag evidence, offline
  gate, and informational online tier.
  DoD: product and failing controls prove sensitivity twice on every required OS;
  raw result artifacts validate; default/release selection contains no public URL
  and no score assertion. **Commit:** `RELEASE-4: acceptance-complete offline stealth probe plus informational detectors (G-D)`.
- **RELEASE-5 — W5 qualified contract/evidence source.** Land root contract and
  shared `release-evidence/v1` parser/generator/ledger and final evidence edge.
  DoD: regeneration is clean; every required child record and aggregate hash
  validates; all schema/parser negative controls fail; fake/stale tools fail;
  runner/architecture/Chrome exclusions are explicit; `get_cookies` has either a
  real transport success result or the generated count is 93 qualified plus one
  served-unqualified—never 94 by exemption. **Commit:** `RELEASE-5: generated qualified release contract and tool evidence ledger`.
- **RELEASE-6 — W6 scheduled informational observation.** Land scheduled/manual
  read-only observation and synthetic fixture-only local repro metadata. DoD:
  deterministic fixture failure
  makes the run red; live failures remain labelled informational; permission and
  network inspection proves no issue/comment/webhook/notification/upload and no
  sensitive/live capture. **Commit:** `RELEASE-6: read-only scheduled observation and local fixture repro metadata`.
- **RELEASE-7 — W7 deterministic site breadth.** Land every named fixture/oracle,
  exact table tests, MQ-114…121, seeded variations, live/HTTPS limitations, and
  same-Stable W4 differential reuse atomically. DoD: every table oracle passes;
  every page/route/controller is enumerated/released; W5 emits every public-
  surface limitation; no stale-handle/frame-target/redirect-chain/truncation/
  download/network-stream/shadow overclaim or other Chrome/TLS claim is generated.
  **Commit:** `RELEASE-7: atomic bounded public dynamic-site outcomes MQ-114 through MQ-121`.
- **RELEASE-8 — W8 exact MQ parity.** Resolve MQ-1…121 against current collection
  and workflow structure; land evidence plugin/parser, lifecycle/multi-instance
  tests, flake policy, and informational mutation lane. DoD: all tokens resolve;
  current-SHA required outcomes contain no skip/xfail/xpass/deselection;
  characterization appears only as `known-gap`, while `blocked`/`planned` remain
  unsatisfied; stale evidence node id, unmapped step,
  and skipped-node controls fail. **Commit:** `RELEASE-8: exact manual-QA evidence and parity gate`.
- **RELEASE-9 — W9 performance.** Land the frozen reviewed budget table, lifecycle
  accounting, 10,240-row/1,024-request oracles, canary timing summary, and
  MQ-122…125 with tests. DoD: two clean runs per claimed cell pass; deliberate
  slowdown and checksum/count corruption fail; cleanup returns to recorded
  deltas. **Commit:** `RELEASE-9: deterministic latency resource and large-payload budgets`.
- **RELEASE-10 — W10 resilience.** Land event-controlled slow/hang routes and
  crash/tab/network fault tests with MQ-126…129. DoD: load/networkidle timeout and
  slow-success controls are sensitive and bounded; every success-path fault has
  exact typed outcome, recovery, handler release, and zero orphan. A wrong current
  behavior is routed and cannot satisfy DoD. **Commit:** `RELEASE-10: bounded fault injection and recovery oracles`.
- **RELEASE-11 — W11 docs.** Land safe runnable-fence execution and claims sync by
  importing W5's generator; append MQ-130 and its test. DoD: every marked fence
  passes in a throwaway fixture environment; broken fence and served-unqualified
  advertised tool controls fail. **Commit:** `RELEASE-11: executable docs reuse the release evidence source`.
- **RELEASE-12 — W12 security.** Land the threat table, canonical W5-API
  redaction policy, and exec/filesystem/upload/import/export matrix with
  MQ-131…137. DoD: exact intended
  boundaries pass on all applicable OS cells; no unintended target, secret leak,
  or remote-default bind exists. Any such result is a release-blocker and stops
  the plan for an authorized fix. **Commit:** `RELEASE-12: security and filesystem trust-boundary gate`.
- **RELEASE-13 — W13 wire semantics.** Land independent official-client,
  concurrency/correlation/cancellation/disconnect/framing tests with MQ-138…144.
  DoD: all calls have exactly one correctly correlated terminal outcome, bounded
  shutdown, recovery, and clean framing; any unsupported behavior is explicitly
  narrowed and does not count as success. **Commit:** `RELEASE-13: independent MCP interoperability concurrency and cancellation`.
- **RELEASE-14 — W14 literal N-1 upgrade.** Record the human/admin-supplied
  immediately preceding stable tag and immutable artifact hash; verify ordering;
  land clean/in-place upgrade, backend/state, rollback, and
  stale-launcher tests with MQ-145…149. DoD: current exact artifact passes the
  three qualified runner cells from literal N-1; original throwaway state remains
  recoverable; no stale launcher/backend wins. **Commit:** `RELEASE-14: verified hash-pinned N-1 upgrade and rollback gate`.
- **RELEASE-15 — W15 observability.** Land structured diagnostic, canary-redaction,
  bounds, and local replay tests with MQ-150…154, reusing W6's writer and W12's
  policy. DoD: typed failures
  are actionable and protocol-clean; every secret canary is absent; bounds and
  truncation are explicit; replay makes no public request or external mutation.
  A leak blocks release. **Commit:** `RELEASE-15: redacted bounded actionable failure diagnostics`.
- **RELEASE-16 — W16 workers/state/PWA/i18n.** Land dedicated/shared/service-worker,
  storage/profile, and exact Unicode/RTL/composition fixtures with MQ-155…162.
  DoD: lifecycle,
  independent hashes, isolation, cleanup, and code-point/event oracles pass; the
  contract expressly excludes native IME UI proof. **Commit:** `RELEASE-16: deterministic PWA state and internationalized interaction coverage`.

---

## 4. Verification

- **Local (Windows)**: per step, `& ".venv\Scripts\python.exe" -m pytest <files> -q`;
  use `uv` only in a clean worktree after `uv sync --extra test --extra dev`.
  Never use `uv` in the OneDrive main checkout. Browser tiers use real headless
  Chrome only where the step calls for it. Run the non-integration suite after
  every checkpoint.
- **CI-only evidence**: Windows/macOS integration, required-check behavior, exact
  artifact smoke, image-provided Chrome Stable, and upgrade matrices are accepted only from
  the stacked PR's own current-SHA run. The completion note records workflow/run,
  commit, stable job/cell id, result, runner image/architecture, Python, resolved
  Chrome path/version, and artifact hash where applicable. A screenshot without
  machine-readable job evidence is supplementary only.
- **Determinism proof**: run every new deterministic gating file twice
  back-to-back with identical seed/oracle inputs. Zero flakes, skip, xfail, xpass,
  deselection, or conditional absence. Online detector/live-corpus observations
  are explicitly exempt because they are not gate evidence.
- **Gates**: ruff format/check, ty, vulture, suppression-owners, file-budgets all
  green; `git diff -- src` is empty; `server.py` budget is untouched; dependency
  diff contains no runtime addition; no embedded module imports `server`; hook
  logs show pre-commit/pre-push ran without bypass.
- **Per-OS coverage**: each qualified OS produces its own report and meets the
  same reviewed floor. Never combine reports before checking each floor. Review
  platform-targeted branch evidence separately; an unchanged aggregate percent
  is not proof that Windows/macOS lifecycle code ran.
- **Fixture and path backstops**: enumerate every fixture HTML/resource and every
  dynamic route/origin in the deterministic manifest; fail on an unlisted file,
  public URL, missing sentinel, or unreleased controller. Grep tests/workflows for
  `/tmp`, separator-built paths, unconditional `DISPLAY`, `localhost`, newline
  goldens, fixed ports, and unbounded waits; each hit must be platform-scoped or
  replaced with the portable shared mechanism.
- **Evidence/contract checks**: regenerate W5 output, run all negative
  `release-evidence/v1` parser/schema controls and W8 static/runtime parity, and
  assert `release-gate` directly needs successful `quality`, `unit-tests`,
  `coverage`, `integration`, `transport`, `offline-stealth`, `build-dist`,
  `package-verify`, `install-smoke`, and `release-evidence`. Verify each required
  job/cell JSON at the canonical SHA/job/cell path and aggregate child hashes.
  `get_cookies` and every characterized MQ remain visibly qualified/narrowed as
  specified; no completion note may overrule the generator.

## 5. Risks & mitigations

- **Launcher/PATH false proof** → W1 requires an absolute launcher inside the
  tested environment and negative controls for bare/wrong-environment commands.
- **Runner or Chrome label drift** → W2 fails on architecture/identity mismatch
  and records the image manifest. The response is a reviewed contract update,
  never silently accepting the new platform.
- **Platform branch false-green** → coverage is gated separately on each OS and
  explicit lock/process/port/profile tests prove the branch executed. Combined
  percentages are reporting only.
- **Required-check false confidence** → one stable aggregate, read-only ruleset
  evidence, plus separate deliberate `quality` and matrix-cell failures prove
  merge blocking. Matrix display names are not treated as repository policy.
- **Evidence self-attestation false-green** → the aggregate hashes every canonical
  child ledger, cross-checks workflow/SHA/JUnit/artifact data, and still directly
  needs every blocking job; a generated JSON claim cannot override a red child.
- **Artifact substitution** → build once, hash manifest, local-path install, and
  publish-from-downloaded-artifact make every substitution/rebuild a failure.
- **macOS cost** → accepted by the human; parallelize/split while retaining the
  ARM64 cell. Demoting or cancelling macOS invalidates the contract.
- **Online detector drift/ToS** → CreepJS and bot.incolumitas stay read-only,
  informational, score-free, and outside release evidence. Drop and record a site
  if access terms change; never replace it with a frozen "live" claim.
- **Controlled hang wedges pytest** → event-backed safety ceilings, release-all
  finalization before server shutdown, disconnect handling, and outer watchdogs
  are mandatory before the first hang assertion runs.
- **Performance flake or budget padding** → freeze reviewed per-cell values from
  two clean baselines, retain raw summaries, and require justification for every
  increase. A breach routes; it is not hidden with retries or a larger number.
- **Security test causes damage/leak** → all probes target unique canaries in a
  throwaway parent and use no real credentials/profiles. Unexpected external
  target or secret disclosure stops release and routes to an authorized fix.
- **N-1 artifact disappears/drifts** → W14 requires the human/admin-supplied
  immediately preceding stable tag+hash, independently verifies ordering and
  bytes, and fails closed; it never accepts arbitrary older/same/local input.
- **Scope/runtime growth** → reuse W1/W5/W6 mechanisms, parallelize independent
  cells, and measure duration. Runtime pressure cannot justify optionalizing a
  required cell, skipping an MQ, or adding a runtime dependency.

## 6. Findings interplay / honest framing

- **This plan gates and documents; M5b/M14 fix.** If E8-1…E8-4 / E7-1 / E7-6 /
  F-181 internal non-acceptance characterization / F-165 / close-flake are closed
  by the time this runs, the transport E2E
  asserts the *fixed* behavior and the contract lists them as resolved. If still
  open, they are **characterization-pinned here** and entered in the
  known-limitations register. Characterization makes the deviation visible; it
  does not make the affected MQ/tool/behavior successful. A release-blocker must
  be fixed in a separately authorized production stream before this plan resumes.
- **The guarantee delivered has two explicitly unequal tiers.**
  1. **Deterministic release-SHA evidence.** A green `release-gate` proves only
     the W5-generated release-qualified tool/behavior rows, with no unresolved
     release-blocker, on the exact GitHub-hosted Ubuntu x64, Windows x64, and
     macOS ARM64 image/Chrome Stable identities recorded for that run. It proves
     W1's representative stdio journey, W3's exact artifacts, W4's versioned
     probe, only W7's explicitly bounded public-tool outcomes and W16's named
     fixture shapes, W9's listed operations/budgets,
     W10/W13's listed fault/wire semantics, W12's tested trust boundaries, W14's
     verified hash-pinned literal N-1 path, W15's redaction/diagnostics, and
     W11's marked docs.
     It does **not** promote a served-unqualified tool, characterization, unlisted
     operation, untested architecture, future runner, or HTTP behavior into a
     success claim. Only the image-provided Stable identity is release-tested;
     any scheduled observation of another version/channel is informational only.
  2. **Informational open-web observation.** The live corpus and public detectors
     are read-only samples checked on a schedule. They may reveal drift; they do
     not guarantee arbitrary-site success, universal stealth, notification, or
     repair within a time bound, and they never substitute for a deterministic
     gate row.
  This scoped statement is the strongest current-SHA claim the evidence can
  support. §8 explains why extending it to every site, detector, platform, or
  future Chrome would require a refused cheat.

## 7. Human choices resolved; readiness still must be proved (2026-07-15)

The four substantive choices below are resolved across the July 15 ruling
commits. They do not waive fresh Phase-0 validation, prerequisite-ancestor proof,
ruleset evidence, or any RELEASE-n DoD. A failed anchor or release-blocker remains
a STOP even though no product-policy choice is open.

1. ~~**macOS CI now or fast-follow?**~~ **RESOLVED: macOS NOW, mandatory and
   gating alongside Ubuntu and Windows.** The tested contract is qualified to
   GitHub-hosted Ubuntu x64, Windows x64, and macOS ARM64 with each run's exact
   Chrome Stable identity. Unit/coverage, integration, transport, offline stealth,
   and install-smoke run with blocking semantics on every in-scope PR and tag.
2. ~~**Online stealth detector pages**~~ **RESOLVED: CreepJS + bot.incolumitas**,
   both research-oriented and automation-tolerant. Online tier is opt-in,
   non-gating, invariant-only, and locally reported; the offline probe gates.
   Either page may be dropped-and-recorded if its access terms change.
3. ~~**Release-gate composition**~~ **RESOLVED: `install-smoke` HARD-BLOCKS
   tag/publish** on the three qualified runner cells — no warn-and-ship path and
   no rebuild between smoke and upload.
4. ~~**Contract location**~~ **RESOLVED: `RELEASE_CONTRACT.md` at repo root**
   (marketing-grade, sits next to README).

---

## 8. The wall — why "works perfectly on any site forever" cannot be *promised* without cheating

This plan pushes as hard as engineering honestly allows toward that sentence (W6
scheduled observation for drift, W7/W16 deterministic structural breadth, and
W1–W5/W8–W15 exact release evidence). It deliberately stops one step
short of *asserting* the literal absolute, because that last step is not an
engineering gap — it is a logical one. Four independent walls, each irreducible:

1. **"forever" vs a moving substrate.** Detectors (Cloudflare/DataDome/Akamai) and
   Chrome-stable change continuously and adversarially. A suite green at SHA *X*
   is evidence about the world *at X*, and about nothing at *X+1*. No amount of
   testing today constrains an adversary's move tomorrow. Scheduled W6 observation
   can reveal some drift; without external notification or response automation it
   does not promise detection or repair within a fixed time.
2. **"any site" is an unbounded universal quantifier over an adversary.** You
   cannot finitely verify ∀ sites — including sites built *specifically* to break
   this tool, and sites that don't exist yet. W7's deterministic table and W6's
   scheduled informational samples cover a finite population they can never
   enumerate. Proving the
   universal would require the site distribution to be closed and known; it is
   neither.
3. **"perfectly / undetectable" is proving a negative in an open class.**
   "Detectable by *no* detector, including future ones" is not falsifiable-then-
   established by any finite test set — it is the anti-bot arms race, structurally
   open-ended. Differential testing (W4/W7) establishes only that the versioned
   probe's named controls distinguish vanilla while the product satisfies those
   named predicates. It says nothing about probes not run.
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
  — its role is drift observation, and the contract says so.)
- **Score-chase.** Assert a detector's numeric "human score ≥ N" — vendor-volatile,
  and passing it once ≠ passing it after their next model update. (W7 asserts
  *invariants and differentials*, never a vendor score.)
- **Silence the caveat.** Delete §8 / W5.4 and let the summary sentence stand
  alone. (This section exists precisely so that can't happen unnoticed.)
- **Count declarations as behavior.** Treat F-108 set equality, schema presence,
  an error path, an exemption, or a characterization as proof that a tool's real
  success path works. (`get_cookies` is the explicit anti-example.)
- **Misstate Chrome scope.** Use one image-provided Stable run to claim another
  version/channel, or omit proof of the executable/version actually launched.
- **Test one artifact, publish another.** Rebuild after smoke or install the PyPI
  name instead of the hashed local distribution, then claim the published bits
  were gated.
- **Relabel an upgrade baseline.** Call an arbitrary older, same-version,
  prerelease, or locally rebuilt package "N-1" without verifying stable-release
  ordering and the human/admin-supplied artifact hash.
- **Display jobs, omit the policy.** Let quality or any required test/coverage/
  browser/build/package/smoke/evidence child exist without directly feeding the
  fail-closed aggregate required by repository rules.
- **Use one OS's coverage for all OSes.** Call platform branches invariant and
  let Linux coverage hide unexecuted Windows/macOS lifecycle lines.
- **Smuggle absence into green.** Use skip, xfail, xpass, deselection,
  conditional non-collection, `continue-on-error`, retries, or a padded budget to
  preserve a claim whose evidence did not pass.

### 8.2 The wording the contract *will* carry (the non-cheating maximum)

> At release SHA `<sha>`, the generated evidence table qualifies `<N>` served MCP
> tools and the named behaviors on GitHub-hosted Ubuntu x64, Windows x64, and
> macOS ARM64 using the exact recorded Chrome Stable versions. The exact built
> wheel and sdist passed clean-install smoke on those cells. Qualification applies
> only to rows with current-SHA success evidence; served-unqualified tools,
> characterizations, architectures/channels not listed, and native IME UI are
> excluded and named below. The versioned offline stealth predicates passed their
> failing controls; this is not a promise of universal undetectability. Dynamic-
> site evidence is limited to fresh-selector SPA actions, direct-child iframe
> metadata, final redirect/CORS outcomes, completed response bodies, page-runtime
> SSE/WS sentinels, public tab switching, and light-DOM/script checks. Stale live
> handles, recursive/frame-targeted interaction, redirect chains/loops, typed
> truncation/loading failure, downloads, MCP-network SSE/WS detail, and generic/
> closed-shadow access are unsupported or unqualified. Public
> sites and detectors are read-only informational observations: the open web and
> Chrome change adversarially, so arbitrary-site or future behavior is not
> guaranteed. Upgrade evidence is limited to the ordering-verified, hash-pinned
> literal N-1 stable release named in this contract. Known limitations and trust
> boundaries follow.

That is the closest a truthful document can stand to "works perfectly on any site
forever." If the requirement is the *literal* sentence with no asterisk, the only
way there is one of §8.1 — and this plan will not take it. If instead the
requirement is *"make the real guarantee as strong as it can honestly be,"* that
is exactly what W1–W16 deliver, and this section is the proof of where the honest
ceiling sits and why.
