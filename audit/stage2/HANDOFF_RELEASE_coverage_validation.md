# HANDOFF — revalidate plan_RELEASE (E2E-9) coverage before implementation

**To:** the independent validation agent.
**From:** the orchestrator (Fable), revised 2026-07-15 after the first Phase-0
validation.
**Your job:** adversarially revalidate the release-gate plan at the exact HEAD you
are given. Answer the human's two questions: (1) does the design honestly gate
the supported Linux, Windows, and macOS cells, and (2) is the deterministic
fixture surface a sufficient stand-in for the user-facing claims?

## Status — the previous validation and cross-review were NO-GO

The first Phase-0 validation did **not** clear implementation. It found factual
anchor failures and material coverage gaps. A subsequent independent
cross-document review also returned **NO-GO** for false evidence classifications,
MQ ownership, and dependency/specification defects. This handoff and the
associated plan and protocol have since been revised, but a document revision is
not evidence that the gaps are closed. **Run Phase 0 again from HEAD. Do not
self-declare GO from the presence of the new text.**

Your default posture remains skepticism: separate what **exists and passes now**
from what the plan merely **proposes to build**. If an anchor is false, a required
claim cannot be delivered, or a test would only appear to cover the behavior,
return **NO-GO** and stop before implementation.

---

## 0. Read and verify these anchors in order

| Artifact | What to verify |
|---|---|
| `audit/stage2/plan_RELEASE.md` | The revised E2E-9 design: W1–W16, sequencing, DoDs, supported-platform wording, and §8/§8.1 refused cheats. Read the whole file. |
| `tests/MANUAL_QA_PROTOCOL.md` | The current **design-time draft** has the MQ-1…113 baseline plus forward ownership through MQ-162. Reserved IDs are not executable coverage. Read the whole file, verify the exact W7/W9–W16 ranges, and independently resolve every cited test node ID and route. |
| `audit/stage2/plan_E2E.md` | The prior fixture-app design and its determinism rules. Recheck §1.2, §2.4, and §2.6 against the files that actually landed. |
| `.github/workflows/test.yml`, `.github/workflows/publish.yml` | Current CI and publish topology. Unless HEAD proves otherwise, treat non-Linux CI, exact-artifact smoke, and required checks as unbuilt. |
| `git log --oneline -8` | The four resolved release choices below and the actual branch ancestry. A prose status line is not ancestry evidence. |
| `src/`, `tests/`, `pyproject.toml` | Tool registry, platform branches, fixture routes/pages, dependency pins, markers, and the real collected tests. Grep and execute; do not inherit counts from either document. |

The **four** resolved release choices are spread across **two ruling-bearing
commits**, not “three human rulings”:

1. `e1fa3b6` — macOS is mandatory now, alongside Linux and Windows.
2. `4cc0e26` — the online informational detector tier uses CreepJS and
   bot.incolumitas.
3. `4cc0e26` — three-OS install smoke hard-blocks publish.
4. `4cc0e26` — the release contract lives at repository-root
   `RELEASE_CONTRACT.md`.

Recheck those SHAs at HEAD; do not rely on this summary if history differs.

### Existing versus planned

- **Existing:** the E2E-7/E2E-8 `.fn`-tier suite, the current loopback fixture
  app, an Ubuntu-only workflow, an Ubuntu-only publish workflow, the 94-tool
  registry, and a draft manual protocol.
- **Planned:** real installed-console stdio E2E, required three-platform gates,
  build-once/exact-artifact smoke and publish, the versioned
  `release-evidence/v1` ledger, runtime stealth verification, expanded
  deterministic site shapes, parity enforcement, performance, resilience,
  docs, security, wire concurrency/cancellation/interoperability,
  upgrade/migration, failure observability, and worker/stateful/PWA/
  international-text coverage.

Do not describe a planned test, workflow, fixture, tripwire, or contract as
existing evidence.

---

## 1. Prior NO-GO findings that must be re-proved, not assumed fixed

The earlier validation established the following at `06b0a06`. Re-run each
check at the revalidation HEAD and report changed and unchanged evidence:

1. Current CI and publish were Ubuntu-only. No Windows/macOS release gate existed.
2. `tests/test_manual_qa_parity.py` did not exist. Only 25 of the then-documented
   122 “Automated by” names resolved exactly; the advertised parity was not
   executable.
3. MQ-42's cited test did not remove a node and therefore did not exercise a
   stale/removed element.
4. MQ-53 exempted `get_cookies`, while the claimed hermetic coverage exercised
   schema/error paths rather than a successful cookie retrieval. This blocked
   the claim that all 94 advertised tools work end to end.
5. MQ-101 claimed nested iframes, but the fixture had no genuinely nested frame
   chain and no true second-origin frame.
6. Production coverage was platform-sensitive (`msvcrt`/`fcntl`, process flags
   and signals, session roots, browser discovery, Linux detection). A Linux-only
   coverage floor could not prove the other platform paths.
7. `fastmcp==2.11.2` successfully initialized and drove the real console server
   over stdio **when given the absolute Windows launcher**, including a 94-tool
   `tools/list`; the bare `stealth-chrome-devtools-mcp` command failed with
   `WinError 2` in the mandated non-activated venv invocation.
8. Current GitHub-hosted runner evidence was Ubuntu x64, Windows x64, and macOS
   ARM64 with image-provided Chrome Stable—not three x86-64 cells.
9. The plan contradicted itself about PR versus tag install-smoke gating and did
   not prove that the exact artifact tested would be the artifact published.
10. The proposed stealth probe, large-DOM page, and hanging route lacked
    acceptance-complete schemas, correctness oracles, and teardown rules.
11. Security/trust boundaries, wire concurrency/cancellation, upgrade/migration,
    failure observability, and stateful/PWA/international-text behavior were not
    part of the gate.
12. Fixture-page determinism used a hand-maintained page list, so an unregistered
    HTML fixture could escape the backstops.
13. MQ-1 claimed a clean install from PyPI even though the pre-publish gate can
    prove only the exact candidate wheel/sdist built for that release SHA.
14. MQ-4, MQ-8, MQ-56, MQ-57, MQ-103…106, and MQ-108 were marked `satisfied`
    by tests that asserted only parser defaults, container shape, helper
    selection, forced cleanup, or a partial lifecycle—not the stated manual
    acceptance outcome. MQ-107's bounded `_proxy_streams` initialize response is
    valid only for its narrow local fast-handshake wording; MQ-2 separately owns
    installed-console stdio transport.
15. W7 specified new gating site shapes without assigning their manual parity
    ownership, while W9 and later workstreams had already claimed MQ-114 onward.
16. A `known-gap` label was not consistently backed by both a collected
    `@pytest.mark.characterization` node and its matching F-id route. MQ-26 was
    the clearest false pin: its cited permissive test accepted either image
    encoding and carried neither marker nor F-id.
17. The dependency graph omitted W2→W4 and W9→W10/W13, W12 referred forward to
    W15 for redaction, and W12's proposed threat table did not explicitly extend
    W5's sole evidence generator.
18. W4 required prerequisite CDP activity in prose but did not specify an exact,
    ordered CDP sequence whose success precedes stealth leak collection.

A characterization pin is evidence that a gap is **known and regression-pinned**;
it is not evidence that the affected user journey succeeds. A release claim must
remain narrowed or blocked for that behavior until the intended behavior has a
green success-path test.

---

## 2. Question 1 — does the design truly gate all supported OS cells?

### 2.1 Audit what exists

Confirm the current workflow facts rather than copying this expected baseline:

- `test.yml` was Ubuntu-only: Python 3.11–3.13 unit jobs, one Chrome/Xvfb
  integration job, `/tmp`, and `DISPLAY=:99`.
- `publish.yml` was Ubuntu-only, ran unit tests, rebuilt the package, and had no
  clean-install smoke before publish.

List every job, runner label, architecture, Python, Chrome executable/version,
test selection, skip/xfail/`continue-on-error` condition, and dependency edge.
Distinguish “job runs” from “job is a required merge/publish check.” Repository
ruleset or branch-protection evidence is required for the latter.

### 2.2 Validate the revised W1–W3 design

The maximum honest platform contract is scoped to the cells actually executed at
the release SHA. The intended wording is equivalent to:

> Verified on GitHub-hosted Ubuntu x64, Windows x64, and macOS ARM64, with the
> recorded runner image, a CPython 3.11–3.13 unit matrix including at least one
> coverage cell per OS, and the recorded image-provided Google Chrome Stable
> executable/version.

Re-verify the current runner architectures and installed Chrome manifests from
official GitHub runner-image sources. The job itself must print and archive the
resolved runner image, OS/architecture, Python version, Chrome executable, and
launched Chrome version. Do not generalize that evidence to other Linux
distributions, Windows ARM64, Intel macOS, self-hosted runners, non-stable Chrome
channels, old OS releases, or IPv6-only environments.

Require all of the following in the design and later in the PR evidence:

1. **Stable required gate:** one aggregate release-gate check depends on
   `quality` and every blocking unit, integration, transport, coverage, stealth,
   install-smoke, and other release child. It uses fail-closed terminal-result
   inspection, is required by repository rules, and no blocking child is absent,
   skipped, xfailed, cancelled, or `continue-on-error`.
2. **Same PR and tag topology:** PR and tag/publish paths invoke the same reusable
   gate, and each evaluates the exact SHA that triggered it. A green PR at an
   older SHA does not license a later tag.
3. **Build once, test exactly, publish exactly:** build one wheel/sdist set once;
   upload it; smoke-test those exact bytes on Ubuntu, Windows, and macOS in clean
   environments; make publish depend on every smoke cell; then publish the same
   artifact without rebuilding. Record checksums before smoke and publish. Prove
   the dependency can bite through a local/workflow-graph test or the authorized
   stacked PR; do not create a temporary diagnostic branch or external run.
4. **Real install isolation:** the smoke environment must not import the source
   checkout or inherit the development venv/PYTHONPATH. It must invoke the
   installed console launcher and drive the canonical journey against Chrome.
5. **Portable W1 launcher:** re-prove the FastMCP 2.11.2 stdio API. On Windows,
   resolve the installed `.exe`/script path explicitly rather than assuming an
   activated-shell `PATH`; prove the analogous POSIX launchers. A fallback client
   may be test-only, never a new runtime dependency.
6. **Per-OS coverage:** enforce the coverage floor on each supported OS because
   covered production lines are platform-branched. A Linux aggregate cannot stand
   in for Windows/macOS coverage. Preserve artifacts so uncovered branches can be
   inspected rather than hidden by a combined percentage.
7. **Chrome claim ceiling:** the blocking matrix uses only each hosted image's
   image-provided Google Chrome Stable. There is no alternate-Chrome or Chrome
   for Testing release-qualification matrix. Any observation against another
   browser/channel/version is separately labelled informational, excluded from
   release acceptance, and cannot broaden the supported-browser claim.
8. **Canonical evidence ledger:** every blocking child writes exactly one
   `release-evidence/<release_sha>/<job_id>/<matrix_cell>.json` record with schema
   `release-evidence/v1`. Its exact field groups are `schema`, `release_sha`,
   `workflow{name,run_id,run_attempt,event}`,
   `job{id,matrix_cell,terminal_outcome}`,
   `runner{os,arch,image_os,image_version}`, `python_version`,
   `chrome{path,executable_version,launched_major}`,
   `pytest{junit_sha256,executed_node_ids,skipped,xfail,failed}`,
   `artifacts[{name,path,kind,sha256}]`, and `mq_ids`. The aggregate hashes the
   child ledgers into
   `release-evidence/<release_sha>/release-gate/aggregate.json` and rejects a
   missing or duplicate record, non-success terminal outcome, any
   skip/xfail/failure, or an artifact/JUnit/hash mismatch.

### 2.3 Portability sweep — rank predicted failures

Grep `src/`, `tests/`, scripts, and workflows for at least:

- `/tmp`, forward-slash string assembly, `DISPLAY`/`:99`, shell-only syntax;
- `pathlib`/`os.path` interactions, long paths, spaces, UNC paths, permissions,
  symlinks versus Windows junctions, and executable suffixes;
- `\n` versus `\r\n` assumptions in protocol output, snapshots, and asserts;
- `fcntl`/`msvcrt` locking, singleton PID/port ownership, simultaneous startup,
  stale locks, and fallback ports;
- process groups/sessions, `SIGBREAK`, detached flags, child cleanup, Chrome
  profile locks, and close-path races;
- IPv4/IPv6 resolution and `ThreadingHTTPServer(("127.0.0.1", 0), ...)` behavior.

Run safe platform-local probes where possible. A `127.0.0.1` fixture is an IPv4
contract; do not silently substitute `localhost` when it may resolve to `::1`.
Return a ranked list with file/line anchors, likelihood, impact, and whether the
issue is observed or predicted.

### Q1 verdict

State whether the built design can back only the exact supported-cell contract
above, and whether current HEAD actually does. If required-check configuration,
hosted macOS/Windows execution, exact-artifact provenance, or a platform path is
not evidenced, under-claim and raise it. Do not mention an alternate Chrome/CfT
matrix in the release claim: only the recorded image-provided Stable binary in
each executed hosted cell is release-qualifying.

---

## 3. Question 2 — is the deterministic fixture a complete enough stand-in?

### 3.1 Inventory what exists

Enumerate `tests/fixture_app/` and the server handler at HEAD; do not reuse a
static table. The earlier inventory included MPA navigation/forms, adversarial
interaction and hit-testing, specialty inputs, upload controls, extraction,
open/closed shadow roots, slots, one same-origin iframe, `srcdoc`, an opaque
sandboxed iframe, contenteditable, SVG/canvas, hooks, same-origin fetch/POST,
cookies/localStorage, redirect, and exact API bodies.

Verify all determinism backstops and add the missing design check to your verdict:
the backstop must discover **every** `tests/fixture_app/*.html` page and fail if a
page is absent from its sentinel/no-external-URL/encoding checks. A hand-maintained
allowlist without a glob/set-equality tripwire is incomplete.

### 3.2 Validate the expanded deterministic taxonomy

Map real-site shapes to a deterministic gating fixture, a supplementary live
corpus, or an explicit product limitation. The revised W7 should make at least
the following hermetic and gating:

- SPA `pushState`/`replaceState`/`popstate`, hydration/rerender, and node
  replacement followed by fresh public-selector re-query; no stale actionable
  handle is claimed;
- a true second loopback origin and A→B→A nested topology, bounded to direct-child
  iframe metadata discovery; recursive traversal, frame switching/targeting, and
  nested-frame content extraction are unsupported;
- IntersectionObserver lazy load and a finite virtualized/infinite list that
  recycles nodes under a deterministic scroll oracle;
- CSP delivered as response headers, including allowed and blocked actions with
  typed expected outcomes;
- custom-element lifecycle, templates, nested light-DOM slots, popup tab
  list/switch/inspect/close, and only the explicitly supported script escape
  hatch for open shadow roots;
- browser-visible auth, final redirect, and final CORS outcomes without claiming
  redirect-chain/loop or preflight-event observability;
- completed metadata/text/base64-binary/assembled-chunked/4xx/5xx network
  outcomes, with truncation/loading-failure and download contracts explicitly
  unsupported;
- finite page-runtime SSE/WebSocket connectivity through an `execute_script`
  sentinel, not MCP network-debugging event/frame/message visibility.

W7's route/oracle ownership is closed and must match the plan, protocol, server,
and collected tests exactly. Revalidate these eight rows rather than accepting a
generic fixture-family claim:

| MQ | Deterministic pages/routes | Required acceptance oracle | Exact planned test node(s) |
|---|---|---|---|
| **MQ-114** | A:`/spa_history.html` performs `pushState` → `replaceState` → `back`/`popstate`; every transition replaces `#route-root` and increments a generation token. | After every transition the test issues a **fresh public selector query and action**, then asserts exact route/action/generation logs and the newly selected element's content. It does not retain or exercise an old backend node. | `tests/test_e2e_dynamic_sites.py::test_spa_history_route_swap_and_requery` |
| **MQ-115** | A:`/frames/a_outer.html` → B:`/frames/b_middle.html` → A:`/frames/a_inner.html`, with fixed frame metadata tokens. | `get_page_content(include_frames=True)` returns the top-level page plus metadata for its **direct B child**, and the A-B-A page causes no hang/crash. No recursive inner-A content, frame control targeting, or cross-frame interaction is asserted. | `tests/test_e2e_dynamic_sites.py::test_cross_origin_a_b_a_direct_metadata_and_limit` |
| **MQ-116** | A:`/lazy_virtual_infinite.html`; one IntersectionObserver target; a 1,000-logical-row/20-node recycled pool; A:`/api/feed?page=0..3` returns exactly four ordered 25-row pages and page 4 returns the declared terminal sentinel. | Lazy token appears only after controlled intersection; virtual node identities recycle while logical ids/text remain exact; finite-infinite load ends at 100 unique ordered rows with four requests and no page-4 append. | `tests/test_e2e_dynamic_sites.py::test_intersection_observer_lazy_load`; `tests/test_e2e_dynamic_sites.py::test_virtualized_and_finite_infinite_lists` |
| **MQ-117** | A:`/csp/strict` returns exact `Content-Security-Policy: default-src 'self'; script-src 'nonce-e2e9'; connect-src 'self'; object-src 'none'; base-uri 'none'`; page attempts nonce script, inline script/eval, self fetch, and B fetch. | Header bytes match; nonce script and self fetch succeed; inline script/eval and B fetch are blocked; ordered `securitypolicyviolation` entries name the expected effective directives and blocked origins. | `tests/test_e2e_dynamic_sites.py::test_strict_csp_surface` |
| **MQ-118** | A:`/auth/basic` requires an exact Authorization header and returns a fixed final body/status/header set; A:`/redirect/start` resolves to `/redirect/final`; A:`/redirect/to-b` resolves to B:`/redirect/final`; B:`/cors/echo` handles OPTIONS/POST and `/cors/blocked` omits ACAO. | Assert the sent auth header, final loaded page token, final response status/headers, and allowed/blocked CORS final outcomes. Redirect hop ids/order/count and loop diagnosis are not asserted. | `tests/test_e2e_dynamic_sites.py::test_auth_redirect_cors_preflight` |
| **MQ-119** | A:`/payload/text` returns fixed text; `/payload/binary` returns 4,096 seeded bytes; `/payload/chunked` returns three chunks that complete normally; `/status/418` and `/status/503` return fixed final headers/bodies. | Public network tools return ordinary request metadata, exact final status/headers, completed text, completed binary body in declared base64 form, completed assembled chunked body, and preserved 4xx/5xx bodies/hashes. No loading-failure, truncation, or download behavior is asserted. | `tests/test_e2e_dynamic_sites.py::test_completed_text_base64_binary_chunked_and_http_errors` |
| **MQ-120** | A:`/events/sse` emits ids 1..3 then EOF; A:`/events/ws` sends `alpha,beta,gamma` then close 1000. The page records both streams in a bounded sentinel. | Through public `execute_script`, wait for and assert the page sentinel's finite SSE/WS connectivity, order, data, and close state. No MCP network-debugging SSE-event or WebSocket handshake/frame/message visibility is asserted. | `tests/test_e2e_dynamic_sites.py::test_sse_and_websocket_lifecycle` |
| **MQ-121** | A:`/popup_components.html` has a tokenized `target="_blank"` link to `/popup_target.html`; a `fixture-card` custom element, cloned template, nested light-DOM slots, and an open-shadow escape-hatch sentinel expose bounded lifecycle logs. | Popup flow is exactly `list_tabs` → `switch_tab` → assert URL/content → `close_tab`; generic selector/script asserts custom-element light DOM, lifecycle, template, and slot logs. Open shadow is inspected only through explicit `execute_script`; no generic or closed-shadow support is asserted. | `tests/test_e2e_dynamic_sites.py::test_custom_elements_slots_and_popup_lifecycle` |

All eight MQ entries, their exact fixture routes/oracles, collected tests, and
ledger records land atomically in RELEASE-7. Missing one row shifts nothing into
an exemption: it leaves W7 and the release claim unsatisfied.

The release contract must generate the same public-surface limits: selectors are
re-resolved and expose no stale actionable handle; iframe support is direct-child
metadata only; redirect chains/loops and preflight events are not observable;
loading failure/truncated-body and MCP download contracts do not exist; SSE/WS
wire detail is not exposed through network debugging; and generic shadow support
is limited to light DOM, with open shadow reachable only through explicit script.

W16 should separately gate stateful/PWA and international-text behavior: service
workers and controlled cache update, IndexedDB/CacheStorage/persisted state,
dedicated/shared worker lifecycle and messaging, plus Unicode/RTL/combining-
character/emoji input and extraction. Native IME behavior that CI cannot
reproduce must be named as a limitation, not implied by ASCII key tests.

The live real-site corpus and the CreepJS/bot.incolumitas detector runs are
**supplementary, non-gating, and informational**. They may detect drift and
produce artifacts, but they cannot replace any deterministic claim or be
advertised as release-gate proof.

HTTPS/TLS behavior is an explicit limitation of this release gate. Loopback HTTP
fixtures and informational visits to public HTTPS sites do not deterministically
gate certificate validation, HSTS, mixed-content policy, mTLS, proxy interception,
or TLS handshake/version behavior. The contract and docs must say so rather than
generalizing the HTTP fixture evidence.

### 3.3 Acceptance-completeness of planned fixtures

Do not accept a filename as a specification. Check that each fixture has a
versioned result schema, ready/completion sentinel, exact correctness oracle,
bounded waits, cleanup, and a failing-control proof where appropriate:

- **W4 `stealth_probe.html`:** exact signals and predicates, exhaustive leak-name
  policy, controlled per-platform allowances, vanilla baseline, an exact ordered
  prerequisite CDP sequence—`Runtime.enable` → `Page.enable` →
  `Network.enable` → `DOM.getDocument` → `Runtime.evaluate` of a nonce sentinel
  → `Page.captureScreenshot`, followed by the final `Runtime.evaluate` that
  releases an armed probe—whose successful results and ordered transcript are
  asserted before leak collection, process-argument inspection, and
  machine-readable results. Today `test_stealth_args.py` proves argument
  sanitization only; it is not runtime stealth coverage.
- **W9 large DOM/network payload:** seeded structure and breadth/depth/text/style
  distribution, exact node and request counts, checksum/content oracle, selected
  extraction roots, body-size corpus, truncation expectations, generous budgets,
  and attributable memory/handle cleanup. Ten thousand empty siblings are not
  adequate stress coverage.
- **W10 slow/hanging routes:** separate bounded slow-success and released-never-
  completes controls, exact stall phase, event-backed release in `finally`, outer
  and tool timeouts, disconnect/BrokenPipe handling, fixture teardown that cannot
  hang, and a successful post-timeout recovery oracle.
- **Expanded W7/W16 pages/routes:** second-origin server lifecycle, deterministic
  clocks/data/seeds, finite network streams, service-worker unregister/cache
  cleanup, and no dependency on public network access.

### 3.4 Tool-surface and manual-parity honesty

Recount the live registry. Earlier evidence found 94 unique tools and an F-108
set tripwire with 93 `E2E_COVERED` names plus one `E2E_EXEMPT` name. That proves
declared set equality—not meaningful success-path behavior per tool.

`get_cookies` is release-blocking for an “all 94 advertised tools work” claim
until a bounded real-Chrome success path retrieves and asserts a known cookie.
Schema, missing-instance errors, a fake protocol response, or reading
`document.cookie` through another tool do not cover `get_cookies`. If the product
bug remains, narrow/remove the advertised claim and record the limitation; do not
count MQ-53 or the F-108 exemption as success.

Treat `tests/MANUAL_QA_PROTOCOL.md` accurately:

- At this revision it is a **design-time draft with an MQ-1…113 baseline and
  forward ownership through MQ-162**, not an executable parity guarantee. W8
  still has to build the machine-checkable manifest and
  `test_manual_qa_parity.py`; reserved future IDs remain absent from executable
  acceptance evidence until their owning checkpoint lands.
- MQ-1 is the clean install of the exact candidate release artifact, identified
  by filename and hash. It is not a pre-publish claim that an unshipped version
  was installed from PyPI. Any post-publish index observation is separate and
  cannot retroactively authorize publication.
- Generate all coverage counts from the live registry, the machine-readable
  manifest, and `pytest --collect-only` exact node IDs. Never trust typed totals
  such as “91 existing,” and never let prose names stand in for collected tests.
- Keep acceptance evidence separate from supporting evidence in the canonical
  `release-evidence/v1` model. Registry/set equality, schema checks, parser
  defaults, helper-level assertions, and characterization pins may support an
  entry, but none can satisfy its behavioral acceptance outcome.
- The corrected current draft parses to **22 `satisfied`, 89 `planned`, 2
  `blocked`, and zero `known-gap`** entries. Unrouted characterization nodes
  appear only as `Current support (non-acceptance)` while the acceptance target
  remains `planned` or `blocked`; they are not silently promoted into the
  evidence state. Recompute every state count from the live manifest rather than
  trusting this cross-check.
- Recheck the earlier false-satisfied entries MQ-4, MQ-8, MQ-56, MQ-57,
  MQ-103…106, and MQ-108. Each must remain downgraded unless a collected test now
  asserts the complete stated manual outcome. MQ-107 may remain satisfied only
  for its bounded local `_proxy_streams` initialize-response outcome, never as
  installed-console transport evidence.
- Explicitly recheck MQ-42, MQ-53 (`get_cookies` success was absent), and MQ-101.
  MQ-42's public acceptance is fresh selector re-resolution after SPA
  replacement and reuses W7 MQ-114's
  `tests/test_e2e_dynamic_sites.py::test_spa_history_route_swap_and_requery`;
  stale live-handle/F-181 behavior is internal support-only and unsupported,
  never a public acceptance contract. MQ-101's intended acceptance is only
  direct iframe-element metadata for existing variants; nested/cross-origin
  interaction and frame targeting are explicit limitations, not missing success
  claims. Each remaining intended outcome is a gap until its exact assertion
  exists.
- Accept `known-gap` only when the cited collected node is marked
  `@pytest.mark.characterization` and the identical exact `route:<F-id>` token
  appears in its docstring, this manifest, and W5's ledger. A permissive support
  test, bare bug name, or workstream name is not a route; MQ-26 must not remain
  `known-gap` on its former evidence.
- A parity test must fail for an unknown MQ ID, duplicate ID, missing node ID,
  uncollected test, skipped/xfail test, or characterization presented as success.
  CI-job/script evidence needs a machine-verifiable representation rather than a
  fabricated pytest node.
- When later workstreams add MQ steps, each step and its live test/evidence record
  land in the same checkpoint commit so the parity gate never has a false-green
  interval.

### Q2 verdict

Return a ranked list of still-uncovered real-site shapes. For each, say **gating
fixture**, **informational live corpus**, or **documented limitation**, and explain
why. State separately whether the existing fixture is complete and whether the
planned design would be complete enough once built.

---

## 4. Stress-test the coverage model itself

The earlier works / fast / fails-safe / docs decomposition was incomplete. Verify
that the revised plan gives the following dimensions an independently gating
workstream with meaningful negative and success controls:

| Workstream | Required dimension |
|---|---|
| **W12** | Security and trust-boundary gates for arbitrary execution, imports/uploads, `*_to_file`, path confinement, traversal, absolute/UNC paths, symlinks/junctions, overwrite behavior, output/secret redaction, unauthenticated HTTP exposure, and explicit documentation that the MCP has no download contract. W12 owns the one redaction oracle and extends W5's existing generator; a threat-model paragraph or second table/source is not a gate. |
| **W13** | Wire concurrency, cancellation, backpressure, client disconnect, request correlation, stdout framing/stderr saturation, shutdown in flight, and independent official-MCP-client interoperability. FastMCP driving FastMCP alone is not independent conformance. |
| **W14** | Literal N-1-to-current clean upgrade/migration using the exact current candidate artifacts and the immutable artifact for the immediately preceding stable release. A human/admin supplies that stable tag plus artifact filename/version/source and SHA-256; the executor verifies release ordering, tag/metadata identity, and the hash. An arbitrary older release, same-version reinstall, local checkout/build, or executor-selected convenience baseline is not N-1 evidence. Cover console-script replacement, config/profile/session compatibility, stale-state handling, and rollback/clear failure behavior. |
| **W15** | Failure observability and local-repro integrity: actionable diagnostics, correlation IDs, bounded artifacts, recovery guidance, and consumption of W12's single redaction oracle, with proof that secrets are not emitted while useful evidence remains. |
| **W16** | Dedicated/shared worker behavior plus stateful/PWA and international-text coverage: worker lifecycle/message/error/cleanup, service-worker/cache/IndexedDB lifecycle, and Unicode/RTL/combining/emoji behavior, with native IME limitations explicit. |

W7 owns MQ-114…121; W9 owns MQ-122…125; W10 MQ-126…129; W11 MQ-130;
W12 MQ-131…137; W13 MQ-138…144; W14 MQ-145…149; W15 MQ-150…154; and
W16 MQ-155…162. Reserved IDs are not current coverage and must not appear in the
executable protocol until their evidence lands in the same checkpoint. W7's
eight steps and exact deterministic fixture/oracle tests must land atomically,
just like every later range.

Verify the corrected ownership graph explicitly: W2→W4, W9→W10, and W9→W13
must be declared; W12 owns the canonical redaction oracle and extends W5's sole
`release-evidence/v1` generator; W15 consumes that oracle rather than defining a
second one.

For each workstream, distinguish tests that prove intended behavior from
characterizations that merely pin a defect. A gap cannot pass silently as “out of
scope”; recommend a narrower release contract if it cannot be gated without a
production change.

---

## 5. Prerequisite and branch ancestry gate

E2E-9 remains sequenced **after** the audit FIX pipeline:

`M4-Ph1+A1 → M5b → M14+A1 → E2E-9`

Do not infer that those prerequisites landed from a plan status line, branch
name, PR state, or commit-message mention. Obtain each actual merge/landing SHA
and run `git merge-base --is-ancestor <sha> HEAD`; record every command and result.
If a required SHA is missing or is not an ancestor, raise the ordering and stop
unless the human explicitly chooses the documented “branch now and pin current
bugs” path.

For review topology, this remediation work is intentionally on
`agent/e2e9-plan-remediation`, stacked on the most recently worked plan branch
`agent/e2e9-release-plan`. Record the exact parent and child SHAs when validating.
That layering is only review ancestry; it does **not** prove the audit FIX
prerequisites landed and does not waive the human merge gate.

---

## 6. Refused cheats and immediate STOP conditions

Re-read plan_RELEASE §8 and §8.1 immediately before the verdict. Stop and return
a written finding if any of these is present:

- redefining “works,” “all tools,” “all OSes,” “live web,” or “supported Chrome”
  after the test is written;
- freezing/snapshotting an adversary and presenting it as current live-detector
  evidence;
- `assert True`, existence-only assertions, catch-and-pass, padded budgets, or a
  fixture control that cannot fail;
- gating on the fixture and advertising the informational live tier as proven;
- vendor-score chasing or deleting/silencing the caveat;
- skip/xfail/`continue-on-error` smuggled into a required gate;
- counting a known-bug characterization, exemption, schema test, or alternate
  tool path as successful user behavior;
- smoke-testing a different build from the artifact published, or importing the
  checkout from a supposedly clean install;
- creating a separate diagnostic branch/run to demonstrate a gate bite instead
  of using a local graph test or the already authorized stacked PR;
- merging percentages across OSes so an uncovered platform branch is hidden;
- claiming branch protection, prerequisite landing, or CI-only evidence without
  the corresponding ruleset, ancestor command, or green run.

An anchor mismatch, a claimed coverage that does not exist, an acceptance test
that cannot go red, or a pillar that can be delivered only through one of these
cheats is a **NO-GO**. Do not implement around it and do not edit the plan during
validation; return recommended plan edits to the orchestrator.

---

## 7. Phase-0 report and implementation gate

Hand back one markdown report containing:

1. exact HEAD, worktree state, prerequisite ancestor proofs, and skipped/exempt
   checks;
2. Q1 verdict, current-versus-planned CI table, exact supported-cell contract,
   FastMCP launcher proof, exact-artifact topology assessment, and ranked
   portability risks;
3. Q2 verdict, real fixture/page/route inventory, determinism-backstop assessment,
   ranked missing-site-shape dispositions, W4/W9/W10 acceptance-spec assessment,
   and tool/MQ parity findings;
4. W12–W16 coverage-model verdicts;
5. an overall **GO**, **GO-WITH-CHANGES**, or **NO-GO**, with every caveat and
   evidence gap stated explicitly.

No implementation begins until that written verdict exists **and the human clears
Phase 1**. If the result is NO-GO or contains a blocking gap, stop there. If it is
GO-WITH-CHANGES, distinguish changes that must land before RELEASE-1 from
non-blocking follow-ups; never use the label to soften a release-blocking defect.
CI-only acceptance evidence remains the future implementation PR's own green run,
and the human retains every merge gate.
