# HANDOFF — validate plan_RELEASE (E2E-9) coverage: all-OS + fixture-webapp completeness

**To:** the validation agent.
**From:** the orchestrator (Fable), 2026-07-15.
**Your job:** independently judge whether the release-gate plan actually delivers
what it claims, focused on the two things the human explicitly wants checked:

1. **Does it truly cover all three OSes properly** (Linux, Windows, macOS) — is the
   "works on Linux, Windows and macOS" claim backed by real gating CI, and are the
   tests actually OS-portable, or will they silently pass on Linux and break on the
   others?
2. **Do we have a dummy webapp that properly covers everything** — is the fixture
   app (existing + the pages the plan still has to add) a complete enough stand-in
   that "green suite ⇒ a real user's site will work"? What can a real user hit that
   the fixture cannot exercise?

This is an **adversarial validation**, not a rubber-stamp. Assume the plan is
over-claiming until the anchors prove otherwise. Report gaps, not reassurance.

---

## 0. Read these first (in order)

| Artifact | What it is | Anchor |
|---|---|---|
| `audit/stage2/plan_RELEASE.md` | The plan under review (E2E-9). 11 workstreams W1–W11, sequenced RELEASE-1…11. | whole file |
| `tests/MANUAL_QA_PROTOCOL.md` | The blind-push manifest: 122 steps MQ-1…122, each mapped to an automated test. | whole file |
| `audit/stage2/plan_E2E.md` | The prior E2E-7/E2E-8 plan that BUILT the fixture app + determinism rules. Context for what already exists. | §1.2, §2.4, §2.6 |
| Recent commits | `git log --oneline -6` — the E2E-9 drafting + the three human rulings (2026-07-15). | — |

**Critical framing before you start:** plan_RELEASE is a **PLAN**. Almost nothing
in W1–W11 is implemented yet. The fixture app and the E2E `.fn`-tier suite **do**
exist (built by E2E-7/E2E-8). So your validation splits cleanly into:

- **What EXISTS** — audit it for real (fixture pages, existing tests, current CI).
- **What is PLANNED** — audit the *design* for whether it will cover the claim
  once built (the OS matrix, the stealth/perf/resilience fixtures, the tripwires).

Do not conflate the two. A gap in "planned" is a plan defect (tell us to fix the
plan). A gap in "exists" that the plan doesn't address is the more serious finding.

---

## 1. Coverage strategy in one screen (so you know what "complete" is being claimed)

The plan's thesis: **green gate == the entire manual QA pass**, across three pillars.

| Pillar | "Green means…" | Workstreams | Status |
|---|---|---|---|
| **It works** | every advertised MCP tool driven end-to-end over the real stdio transport, against real Chrome, from a clean install, on all 3 OSes; every interaction/extraction invariant holds as a *property*; stealth holds *differentially* | W1 transport, W2 cross-OS CI, W3 install-smoke, W4 stealth, W5 contract, W6 canary, W7 breadth/fuzz, W8 parity capstone | **planned** (fixture + `.fn` suite exist) |
| **It's fast & lean** | every tool returns within a measured latency budget with a bounded memory footprint | W9 perf/resource budgets | **planned** |
| **It fails safe** | every injected fault (crash, tab-close, timeout, network-drop) yields a *typed, recoverable* error | W10 resilience/fault-injection | **planned** |
| (+ docs) | every runnable README/docs example executes; advertised tools are real | W11 doc-examples | **planned** |

The manifest tripwire (`test_manual_qa_parity.py`, W8) is the mechanism that keeps
these honest: every MQ step must map to a live (collected, non-skip) test, and no
test-less step is allowed. W9/W10/W11 each append their MQ steps + tests together.

**Your first sanity check:** does that three-pillar decomposition actually cover
"a user will have a great experience"? What dimension is missing from *works /
fast / fails-safe / docs*? (Candidates to weigh: security/trust-boundary of the
exec + to-file tools; concurrency/cancellation on the wire; upgrade/migration;
observability when it breaks. The plan mentions some as documented limitations —
judge whether "documented" is enough or whether it needs a test.)

---

## 2. OS-coverage validation (question 1)

### What the plan claims
- **W2** (`§1.1` table; `§2.2`; `RELEASE-2` in `§3`): three-OS unit matrix
  (`ubuntu`, `windows`, `macos`) × py3.11–3.13, **plus** a Windows integration+
  transport job **and** a macOS integration+transport job — all three OSes
  **mandatory and gating** (human ruling 2026-07-15, `§7.1`).
- **W3** (`§2.3`): install-smoke on all three OSes, **hard-blocks publish**.
- The `RELEASE_CONTRACT.md` (W5) opens with "works on Linux, Windows and macOS"
  and cites those four gating jobs as its evidence.

### What EXISTS today (verify — don't trust me)
- `.github/workflows/test.yml` — **`ubuntu-latest` only**. Unit matrix py3.11–3.13
  (`-m "not integration"`, `--cov-fail-under=55`), a `quality` job, and one
  integration job (google-chrome-stable + Xvfb, `DISPLAY=:99`,
  `STEALTH_MCP_BROWSER_SESSION_ROOT=/tmp/stealth-mcp-test`, `--timeout=120`).
- `.github/workflows/publish.yml` — **`ubuntu-latest` only**, unit-only, no
  install-smoke. Advertises `uv tool install` / `uvx`.
- So **zero non-Linux CI exists today.** The entire OS claim is unbuilt.

### Validate the PLAN's OS design against these questions
1. **Does the matrix design actually gate, or just run?** Confirm the plan wires
   the Windows + macOS jobs as required `needs:`/branch-protection gates, not
   `continue-on-error`. A job that runs green but isn't required proves nothing.
2. **Chrome on each runner.** Plan says Chrome is preinstalled on the GH Windows
   image and uses a pinned `setup-chrome` as fallback; macOS similar. **Verify
   this is actually true for current GH runner images** (this is the single most
   likely place the plan is wrong — runner images change). Does `nodriver` find
   Chrome on Windows/macOS without help? Is there a headless-vs-headed assumption?
   (Linux uses Xvfb; Windows/macOS have a session display — plan says run headless.
   Confirm the server/tests don't hard-depend on Xvfb/`DISPLAY`.)
3. **Path & env portability.** The Linux job sets
   `STEALTH_MCP_BROWSER_SESSION_ROOT=/tmp/...`. The plan says the Windows job uses
   a `RUNNER_TEMP` equivalent. **Grep the whole test suite + `src` for POSIX-only
   assumptions**: hard-coded `/tmp`, forward-slash path building, `:99` DISPLAY,
   `os.path` vs `pathlib`, `\n` vs `\r\n` in any golden/characterization assert,
   file-locking/port-binding that behaves differently on Windows. The plan's
   `§2.2` "path-portability sweep" is a *promise* — check the current code to
   estimate how much will actually break. The singleton port-fallback logic
   (`test_singleton_port_fallback.py`) and process handling are the highest-risk
   OS-sensitive areas.
4. **The install-path claim.** W3 says install-smoke must catch missing
   `embedded/js` package data on every OS. Does `uvx`/`uv tool install` behave the
   same on Windows/macOS? Console-script shims differ per-OS — is that exercised?
5. **What's NOT claimed.** The plan explicitly does not add ARM/alt-arch, older
   Chrome-on-old-OS, or non-glibc Linux. Judge whether "Linux, Windows and macOS"
   as a *marketing claim* is honest given it's x86-64 GH runners with current
   Chrome only. Should the contract say "on x86-64 with Chrome stable"?

**Deliverable for Q1:** a verdict — *will the built plan back the three-OS claim,
or is the claim broader than the CI proves?* — plus the concrete portability
breakages you predict from reading the current code.

---

## 3. Fixture-webapp completeness validation (question 2)

### What EXISTS today (`tests/fixture_app/`) — audit it
Served hermetically on loopback by a conftest fixture (`fixture_app_server`);
determinism is enforced by `test_fixture_app_server.py` (no external URLs,
ASCII-only, every page 200 + sentinel). The action-log convention
(`window.__actions` / `logAction` / `<pre id="action-log">`) is the ground-truth
oracle every interaction test reads.

| Page | Covers |
|---|---|
| `index.html` | landing / sentinel / basic nav target |
| `interact.html` | basic interaction surface |
| `interactions.html` (5.9K — the workhorse) | overlay hit-testing traps (real coordinate click vs synthetic `.click()`), `pointer-events` pass-through, **`:hover`-gated submenu (unreachable census)**, disabled/readonly/label-for, validation form (`invalid`/`submit`/`reset`), Enter-implicit-submit, keydown/up/press + `isTrusted` fidelity, value-typed inputs (**range/number/date/color**), `<select>` by value/index/text, modal `<dialog>`, native `popover`, anchor/same-page/new-tab links, **dblclick + contextmenu (unreachable census)**, **HTML5 drag-and-drop (unreachable census)**, offscreen auto-scroll |
| `extract.html` | nested structure (level-1/2/3), `data-*` attributes, `data:`-URI image, deep text node |
| `hard_dom.html` (2.8K) | **open + closed shadow roots**, `<slot>`, iframe via `src`, `srcdoc` iframe, **sandboxed opaque-origin iframe**, contenteditable, multi-select, inline SVG, canvas, `<details>` |
| `iframe_child.html` | same-origin child-frame document |
| `hooks.html` | JS-function-hook targets (`calcTotal`, `appAPI` in app.js) |
| `network.html` | fetch JSON / POST echo triggers |
| `cookies.html` | client cookie + localStorage triggers |
| server API routes | `/api/json` (exact body), `/api/echo` (POST, header reflection, lowercased), `/api/set-cookie`, `/redirect` (302) |

**This is a strong, deliberately adversarial fixture.** It already encodes the
known-bug classes (disabled/readonly/select/specialty-input, stale-node,
occlusion) as first-class probes. Judge it on its merits.

### Fixture assets the plan STILL HAS TO ADD (not present today)
- **`stealth_probe.html`** (W4) — in-page bot-detection probes (`navigator.webdriver`,
  CDP-leak globals `cdc_`/`$cdc_`, `navigator.plugins`, `navigator.languages`,
  `window.chrome` shape, UA vs `userAgentData`, `Function.prototype.toString`
  native-code integrity). **Today only `test_stealth_args.py` exists — it tests
  the argument sanitizer, NOT runtime stealth. There is zero runtime stealth
  assertion in the repo.** This is the biggest existing coverage hole.
- **Large-DOM page (≥10k nodes)** (W9) — for the large-payload perf/memory stress.
  Does not exist.
- **A hanging / never-completing server route** (W10) — for the timeout/resilience
  test. The current server has no such route.

### Validate against these questions
1. **Does the fixture span the structural taxonomy a real site presents?** Map the
   fixture pages against the categories that break automation in the wild:
   shadow DOM ✅, iframes (incl. opaque-origin) ✅, canvas/SVG ✅, contenteditable ✅,
   occlusion/hit-testing ✅, specialty inputs ✅, native top-layer (dialog/popover) ✅.
   **What's missing?** Candidates to judge: SPA route-swaps that detach nodes
   (History API), lazy-load / IntersectionObserver, virtualized/infinite lists,
   CSP headers, web components beyond shadow DOM, `<template>`, workers/service
   workers, cross-origin (truly different origin, not just sandboxed), file
   upload/download, WebSocket/SSE network, auth/redirect chains. The plan pushes
   *some* of these into W7's property-fuzz + real-site corpus rather than the
   fixture — judge whether that split is right, or whether they belong in the
   deterministic fixture so they actually **gate**.
2. **Fixture (gating, deterministic) vs corpus (informational, live).** The plan's
   philosophy: anything that must *gate* has to be in the hermetic fixture; live
   sites are informational-only (can't gate on a third party). Verify nothing the
   user depends on is only covered by the non-gating live corpus.
3. **Negative-space fidelity.** The fixture encodes known bugs as probes
   (disabled-click, readonly, stale-node). Confirm the plan's MQ steps
   (MQ-38…46) *pin current behavior* and flag the known bugs, rather than
   asserting the buggy behavior is correct (the anti-cheat in `§8.1`).
4. **Does "everything" mean every tool?** Cross-check: the tool surface is ~94
   tools. `test_e2e_functions_hooks::test_e2e_coverage_manifest` (F-108 tripwire)
   claims every tool has ≥1 test. The `get_cookies` tool is E2E-**exempt** (hangs
   against real Chrome — CDP `Network.getCookies` never returns). **Verify that
   exemption is real and that it's the ONLY one**, and that MQ-53's exemption is
   honestly flagged, not hidden.

**Deliverable for Q2:** a verdict — *is the fixture (existing + the three planned
additions) a faithful stand-in, or are there site shapes a real user will hit that
nothing deterministic covers?* — with a concrete list of fixture pages/routes you'd
add and which MQ steps they'd back.

---

## 4. Validation rubric (what a PASS looks like)

Produce a report with a verdict on each:

- [ ] **OS claim is honest.** The three-OS gating design, once built, genuinely
      backs "works on Linux, Windows and macOS" — or you've named exactly where
      the claim outruns the CI (arch, Chrome discovery, portability breakages).
- [ ] **OS portability risks enumerated.** A concrete list of current-code
      POSIX-isms that will break on Windows/macOS, ranked, so RELEASE-2 doesn't
      discover them one CI-red at a time.
- [ ] **Fixture completeness judged.** The fixture's structural taxonomy is mapped;
      missing site-shapes are listed with a gate-vs-corpus recommendation for each.
- [ ] **The three unbuilt fixtures are speced well enough to build** (stealth
      probe, large-DOM, hanging route) — or you've flagged what's underspecified.
- [ ] **The parity mechanism actually bites.** Confirm the tripwire design fails
      on an unmapped MQ step / a skip-downgraded test (read the W8 design; it's the
      keystone — if it's weak, "green == manual pass" collapses).
- [ ] **Coverage-maximization gaps.** Anything the three-pillar model misses that a
      user would care about (security boundary, concurrency, upgrade, observability)
      — with a recommendation: add a workstream, or document-as-limitation.
- [ ] **No cheats.** Re-read `§8`/`§8.1`. Confirm nothing added since (W9/W10/W11)
      smuggled in a redefinition, a snapshot-the-adversary, an `assert True`, a
      gate-on-fixture-advertise-live, a score-chase, or a silenced caveat.

## 5. Assumptions to challenge (I may be wrong about these)
- That `fastmcp==2.11.2`'s `Client` can drive a stdio subprocess of the console
  script (W1's whole foundation). If it can't, the transport tier — and every
  claim built on it — needs the `mcp` SDK fallback. **Verify the API exists.**
- That GH's Windows/macOS runner images ship a Chrome `nodriver` can find headless.
- That coverage numbers are OS-invariant (plan runs `--cov-fail-under` on Linux
  only). Is any covered line platform-branched such that Linux-only coverage lies?
- That the fixture's hermetic loopback server behaves identically on Windows
  (localhost binding, port allocation, `127.0.0.1` vs `::1`).

## 6. What to hand back
A markdown report: verdict on Q1 (OS) and Q2 (fixture), the two ranked gap lists
(portability breakages; missing fixture shapes), and a go / go-with-changes /
no-go on whether the plan as written will deliver "green ⇒ user works well" once
executed. Do not edit the plan — recommend edits; the orchestrator applies them
under the routing ruling (plan edits are orchestrator work; code/test edits go to
Opus executors). This plan is still **queued behind the audit FIX pipeline**
(M4-Ph1+A1 → M5b → M14+A1); your validation gates whether it's ready to enter that
queue as written.
