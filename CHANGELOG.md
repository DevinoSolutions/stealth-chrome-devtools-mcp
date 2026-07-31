# Changelog

## 2.0.1

### Fixed — two tools no longer report success for an operation that failed

Both defects had the same shape: the operation failed *at the browser* while every
Python-side step around it succeeded, so the tool assembled a payload whose `success`
said it worked.

- **`navigate` raises instead of reporting a Chrome error page as a success (F-802).**
  Navigating to a host that does not resolve (or refuses the connection, or fails the
  TLS handshake) used to return `{"url": "chrome-error://chromewebdata/", "success":
  true}`. It now raises a `ToolError` naming the requested URL and the error page. A
  page that merely answered 404/500, a redirect to a different final URL, `about:blank`
  and `data:` URLs are **not** failures and are unaffected.
- **`execute_script` raises when the script throws (F-795).** `nodriver`'s
  `Tab.evaluate` returns the CDP `ExceptionDetails` record *in the value's place*
  instead of raising, so a throwing script came back as `{"success": true, "error":
  null}` with the exception nested inside `result`. It now raises a `ToolError`
  carrying the exception text. The success envelope is unchanged.

Callers that branched on `result["success"]` will now see a raised tool error where they
previously saw a success they could not act on.

### Fixed — spawn can no longer hang forever on a silent client (F-790)

The default (unnamed) `spawn_browser` path sends a `roots/list` request to the MCP
client and awaited the answer with no deadline. MCP roots is an *optional* client
capability, so a conforming client that never answers parked the tool call forever.
The round trip is now bounded by `STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS`
(default 5 s, `0` = never ask); on expiry the spawn falls back to the same local
seed chain an unsupported client already used. Clients that answer are unaffected.

### Fixed — network capture rows are typed, filterable, and free of browser noise (F-803)

- `resource_type` was `null` on every captured request in every prior release:
  nodriver's CDP dataclasses spell the field `type_`, and the interceptor read
  `event.type` behind a `hasattr` guard that turned the permanent miss into `None`.
  It is now populated (Document, XHR, Fetch, Script, …).
- Consequently `list_network_requests(filter_type=…)` could never match anything;
  it now works, case-insensitively.
- Browser-internal traffic (`chrome://`, `chrome-extension://`, `devtools://`,
  `chrome-error://`, `about:`) is no longer captured by default — it drowned real
  requests 24-to-1 on an ordinary page load. Opt back in per instance via
  `set_network_capture_filters(capture_internal_urls=True)` or process-wide via
  `STEALTH_MCP_NETWORK_CAPTURE_INTERNAL_URLS`.

### Fixed — window size is reported truthfully (F-804)

Headed Chrome clamps its window to the desktop work area; the spawn result echoed
the *requested* size as if applied (1920x1080 requested, ~1028x617 delivered).
Spawn diagnostics now report `requested`, measured `actual`, the real inner
viewport, and a `clamped` flag; `instance.viewport` is the measured size.
Headless remains unclamped and exact.

### Added — real-transport soak coverage

A 62-operation soak journey (`tests/test_soak_stability.py`) drives one instance
over real stdio with a hard per-call deadline: navigations (including deliberately
unresolvable hosts), throwing scripts, tab churn, screenshots, cookies. Any overdue
reply fails the suite by name; the journey ends with a clean close and a
no-leftover-Chrome-children assertion. It also characterized **F-805** (a selector
that never resolves costs nodriver's default 10 s regardless of the caller's
timeout) as a strict xfail pending its fix.

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
- The **source distribution shrank from 15 MB to 592 KB.** It had been shipping 12.8 MB
  of demo media and 2.8 MB of internal audit documents. The wheel — what `pip` and `uvx`
  actually install — is unchanged at 192 KB; this only affects installing from source.

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
