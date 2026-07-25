# Changelog

## 2.0.0

The first release since the foundational audit. It carries ~85 commits since 1.2.0 and
fixes four defects that were present in every prior release and invisible to the old
test suite, because none of them can be reproduced through the in-process test seam —
they only appear over the real stdio transport a client actually uses.

### ⚠️ Breaking

- **`STEALTH_MCP_SESSION_STORAGE_CAP_GB` is now `STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB`.**
- **`--session-cap-gb` is now `--browser-session-cap-gb`.**

  There is **no back-compat alias**. The old names are simply not read, so if you set
  either one it stops taking effect **silently** on upgrade — your storage cap reverts to
  the default. The rename removes a genuine ambiguity: "session" meant three different
  things across this codebase (an MCP protocol session, a Claude Code session, and a
  profile-backed browser session), and only the last one was ever meant here.

  Note the environment namespace is strict: an unrecognised `STEALTH_MCP_*` variable is
  rejected at startup rather than ignored, so a stale name fails loudly at the *next*
  restart even though the setting itself silently lapsed.

### Fixed

- **Browsers were destroyed every ~2 seconds over real stdio.** FastMCP runs the server
  lifespan once per *MCP session*, and the liveness watchdog's probe sessions each re-ran
  orphan recovery and its destructive teardown — killing every live browser instance
  belonging to the real session. Anyone driving this server the normal way (stdio proxy →
  detached backend) had instances disappear underneath them. The lifespan is now
  session-reentrant.
- **`list_tabs` raised a bare `TypeError` after any `close_tab`.** nodriver re-adds
  rediscovered targets as raw `Connection` objects, which are not awaitable, and the tool
  awaited each one. Once a tab had been closed the failure was permanent for that
  browser, not transient.
- **Every navigation after a `close_tab` silently switched tabs and leaked one.** The
  same root cause, but swallowed by a broad exception handler: the tracked tab was found
  correctly, the liveness check on it raised, and the handler concluded the tab was
  "missing or invalid" and replaced it — without closing the original. No error surfaced;
  navigation simply happened in a different tab each time, and the abandoned tabs
  accumulated.
- **`close_tab` returned `False` for a closeable tab**, and **`switch_to_tab` failed to
  activate**, for the same class of rediscovered target. Both now address the target by
  id through CDP, which works regardless of object type.
- **Headless mode advertised `HeadlessChrome` in its User-Agent.** That is the cheapest
  bot check that exists — one server-side substring test, before any JavaScript runs —
  and it contradicted the product's central claim. A default headless spawn now presents
  the same User-Agent the same binary presents headed, on the page, on the wire, and at
  the CDP level. An explicitly supplied `user_agent` still wins. See *Known limitations*
  for what this does **not** fix.
- **Every spawn enabled catch-all network interception, even with no hooks defined.**
  Chrome paused every request and waited for a resume that only the hook handler would
  send, so all traffic paid a pause plus a CDP round-trip for no benefit. Interception is
  now armed only when there is something to intercept — and, relatedly, a hook created
  *after* spawn now arms interception through the same path instead of relying on the
  catch-all's accidental coverage.
- **Selector resolution could hit stale-node `-32000` errors under DOM churn**, because
  nodriver's `select`/`find`/`query_selector` are not atomic. All selector resolution now
  routes through a single resolver that survives document-node invalidation.
- A Tier-A pass on silent-correctness and "lying success" defects — cases where a tool
  reported success without having done the thing (PR #41).

### Added

- A **three-OS release gate** (Ubuntu x64, Windows x64, macOS ARM64) that exercises the
  real stdio transport against real Chrome, asserts the exact Chrome binary identity, and
  gates on a single aggregate check. Previous releases were verified on Ubuntu only.
- **Build-once packaging.** The distribution is built exactly once per commit, hashed,
  verified, and installed from that same artifact in smoke tests; the publish step
  downloads those bytes, re-checks their SHA-256, and uploads them without rebuilding.
  What was tested is what ships.
- A **deterministic offline stealth suite** asserting anti-detection invariants against a
  vanilla-Chrome control, so a regression that reintroduces an automation tell fails the
  build.

### Known limitations

Stated explicitly rather than by omission.

- **macOS: navigation is unverified.** On GitHub-hosted macOS/ARM64 runners, Chrome
  launched by the detached backend completes no network navigation (reproducible 11/11);
  a connection to a *closed* port hangs rather than being refused, so the request never
  reaches the network stack. The cause is unknown and it has **never been reproduced on a
  real Mac** — hosted runners differ in ways that plausibly matter. The gate therefore
  excludes the macOS transport cell and runs macOS install-smoke without navigation. This
  release makes **no claim that macOS navigation works, and none that it is broken.**
  Linux x64 and Windows x64 are verified.
- **Headless is not "undetectable".** The User-Agent fix closes the cheapest and most
  widely deployed check, but supplying a User-Agent override makes Chrome blank its
  high-entropy client hints (`architecture`, `bitness`, `platformVersion`,
  `uaFullVersion`, `fullVersionList`). Low-entropy hints and every `sec-ch-ua*` header on
  the wire remain correct and coherent, so the residue is reachable only from JavaScript
  that explicitly calls `getHighEntropyValues()` — a strictly smaller and more expensive
  tell than the one it replaces, but a real one.
- **`switch_to_tab` can still store a rediscovered target** as an instance's main tab.
  Activation is fixed; the storage path is not. It fails loudly if it fires.
- **The HTTP transport is unauthenticated and loopback-default by design.** All
  verification here covers stdio; stdio evidence licenses no HTTP claim.
- **Not evidenced in this release:** scheduled drift observation, deterministic
  site-breadth corpus, manual-QA parity tripwire, performance and resource budgets,
  fault-injection/resilience, runnable-documentation checks, the security/trust-boundary
  matrix, wire concurrency/cancellation and independent-client interoperability,
  upgrade/migration smoke, failure-observability, and worker/PWA/internationalized site
  shapes. These are planned work that has **not** been performed — do not read their
  absence as a passing result.
- **Per-tool verification depth varies, and the release contract says so per tool.** All
  94 tools have end-to-end coverage driving real Chrome against a local fixture,
  enforced by a set-equality tripwire (94 covered, 0 exempt). But only `set_cookie`,
  `get_cookies` and `clear_cookies` are additionally verified over the **real stdio
  transport** your client actually speaks; the rest are exercised through an in-process
  test seam that bypasses the wire. That distinction is not academic — every one of the
  transport bugs fixed above was invisible to the seam. It is a gap in test placement
  rather than a known defect in those 91 tools, which is why the contract calls them
  *served* rather than *release-qualified*.
- A known flake exists in one packaging smoke cell (`install-smoke (sdist Linux/X64)`) on
  Chrome cold-spawn; it passes on re-run.

### Upgrading from 1.x

1. Rename `STEALTH_MCP_SESSION_STORAGE_CAP_GB` → `STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB`
   and `--session-cap-gb` → `--browser-session-cap-gb` wherever you set them.
2. Pin the new version, e.g. `uvx stealth-chrome-devtools-mcp==2.0.0`.
3. Restart the backend. Code changes apply via a fresh backend process — the singleton is
   version-gated, so an old backend is evicted rather than reused.

## 1.2.0 and earlier

Not tracked in this file; see the repository history.
