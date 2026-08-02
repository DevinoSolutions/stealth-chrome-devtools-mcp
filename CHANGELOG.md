# Changelog

## 2.0.4

### Fixed — a headed spawn opens a browser you can actually see, or says why not (F-808)

`spawn_browser(headless=False)` could return `state: "ready"`, `headless: false`
and `window_size.measured: true` while producing a browser that was **permanently
invisible**. Chrome inherits its parent's window station, so visibility is decided
by whoever launched the backend — not by the `headless` flag and not by the caller.
One session cold-starting the shared backend from an SSH login or a Windows service
session (Session 0, isolated since Vista) poisoned headed browsing for **every**
session on the machine, including the ones running on the physical desktop. Every
signal the server had said success, because none of them observed a window: CDP
attaches to the process, and `take_screenshot` captures the compositor surface
whether or not it is displayed. This was a regression from 1.0.0, where each client
ran the server in-process and Chrome was always a descendant of the session that
asked.

The fix has two halves, and the first is the one that closes the report.

- **The backend a session adopts now depends on where windows can be shown.**
  `server.json` records one backend per **display context** — an observed token
  naming the desktop a process could put a window on (`win-session-N`,
  `wayland-…`, `x11-…`, `aqua-<uid>`, or `headless` / `unverified`). Discovery
  prefers a window-capable backend, and adoption is deliberately asymmetric: a
  client that cannot prove it has a desktop adopts **any** backend, window-capable
  first — which is exactly what makes an SSH session's headed spawn land on the
  desktop backend and open on the real screen — while a client that can prove one
  adopts only its own context's backend. Nothing tries to *find* the interactive
  session: on the reporting machine the active console session was 2 while the
  user's desktop was session 1, so every "pick the interactive session" heuristic
  is wrong on somebody's machine. The cost is one extra backend process on a
  desktop box that is also SSH'd into, which is the correct trade against invisible
  browsing.
- **Where no window-capable backend exists, the spawn raises.** A headed spawn in a
  context that cannot display a window now fails with a `ToolError` naming the
  context and both remedies — start a backend from a desktop session, or pass
  `headless=True` — instead of handing back a browser nobody can see. It refuses
  before cloning a profile directory, so a doomed spawn costs no disk. There is no
  silent headed→headless degradation; that is the same defect wearing a different
  hat. **Headless spawns are unaffected from any context**, which is what CI
  depends on.

`stealth-chrome-devtools doctor` now prints one line per recorded backend with its
display context and whether that context can show a window, plus an explicit remedy
line when none of them can.

Fixes `STEALTH-CHROME-DEVTOOLS-MCP-K` — 66 nodriver "Failed to connect to browser"
events on 2.0.3, all from headed spawns driven over the magent/psmux SSH path
against a backend that had no desktop to put a window on. That is F-808's signature
seen from the other end, and it is closed by the adoption fix above.

### Changed — `server.json` is schema v2, and your existing record is not evicted

The record grew from a flat `{port, version, pid, source_fingerprint}` to
`{"schema": 2, "backends": {"<display context>": {…}}}`, so one machine can hold a
headless backend and a desktop backend at once. **Records written by 2.0.3 and
earlier still read**, as one backend classified `unverified` — which every client
treats as adoptable — so upgrading does not evict the backend you are currently
using. The v1 entry is superseded in place the first time a 2.0.4 backend records
itself on that port: recording supersedes any other entry claiming the same port,
because only one process can hold a loopback listener, so a second entry naming it
is by construction a leftover. Without that rule the stale entry would sort first
forever and force a kill-and-respawn of the shared backend on every proxy start.
Reading the record belongs to `embedded/backend_registry.py`; nothing outside it
branches on the schema.

### Fixed — concurrent backends stop erasing each other's tracked browsers

`browser_pids.json` was read and rewritten whole by every writer, so two backends
running at once — which schema v2 now makes an ordinary state — clobbered each
other's entries, and the loser's browsers became untrackable orphans nothing would
ever reap. Every write now read-merge-writes under a sibling lock
(`browser_pids.json.lock`), with the record's schema, its owner stamp, and that
protocol living in one new module, `embedded/browser_pid_registry.py`.

Entries also carry the identity of the backend that started them (`owner_pid` and
`owner_create_time`), and recovery reaps only browsers whose owner backend is
**dead** — the distinction the old `create_time` guard could not draw, since every
already-running backend's browsers predate a starting backend's import. `kill-orphans`
now drops only the entries it actually reaped, leaving other backends' entries
alone, and `--force` bypasses the ownership check as well as the live-backend
refusal, so it still does what an operator reaches for it to do. There is
deliberately **no schema bump**: an entry with no owner keys is a 2.0.3 entry, and
that absence is exactly what makes it a reclaimable orphan after an upgrade.

### Fixed — the window-size clamp is attributed to the right desktop (corrects F-804)

2.0.1 reported window sizes truthfully but explained the clamp as headed Chrome
fitting "the desktop work area", read as an ordinary monitor limit. That reasoning
concluded a workstation driving an RTX 3080 had a ~1024x768 screen. The real clamp
was **Session 0's small default desktop** — the same root cause as F-808. The
remedy is unchanged (`spawn_diagnostics.window_size` still reports `requested`,
`actual`, `inner_viewport` and `clamped`); the docstrings on `spawn_browser` and
`window_sizing` now say the clamp is to the **launching** context's desktop,
which is the user's monitor only when the backend runs on it.

### Fixed — test runs no longer ship injected failures to the real Sentry

Error reporting is on by default and `LoggingIntegration` forwards every
ERROR-level log, so a local test campaign — which deliberately injects failures —
pushed roughly 50,000 noise events into the live project in 15 hours. `conftest.py`
now sets `STEALTH_MCP_NO_ERROR_REPORTING=1` as a session-wide default alongside its
existing env guards, and because the singleton strips only
`STEALTH_MCP_NO_AUTO_RECOVERY` from a spawned backend's environment, real-Chrome
integration backends inherit the mute too. An explicitly-set value still wins, so a
CI cell that *wants* reporting keeps it.

### Changed — error reports no longer carry your username or machine name

Error reporting is on by default, and this release is the one that stopped
pretending it only ever runs here. Events arriving from third-party installs of
2.0.3 carried **their** Windows usernames — in stacktrace frame paths, in the
recorded command line, and inside exception messages such as
`No such file or directory: 'C:\Users\<name>\…'` — plus **their** machine names, as
Sentry's `server_name`.

The reporting stays: it is how two real bugs on machines nobody here owns were
found. What changes is that every event now passes through a scrubber before it
leaves your machine. `server_name` is dropped, and the home-directory segment of
every path is replaced with `~` — `C:\Users\~\…`, `/home/~/…`, `/Users/~/…`, plus
UNC shares and the `/var/home` layouts — regardless of which OS produced the
event, since a Windows maintainer receives Linux users' reports and the reverse.
Account names containing spaces are handled too.

Separately, **local variables are no longer captured**. The SDK records every
frame's locals by default, and in this product a local can hold a proxy
password, an `Authorization` or `Cookie` header, or a script you passed in —
values that are secret in themselves, which no amount of path scrubbing would
have fixed. The project's own canary suite already treats those classes as
release blockers on every other surface; error reports now match.

What a maintainer actually debugs from is deliberately untouched: the release,
the environment, the exception type and mechanism, the failing source line, and
the module path *after* the home segment.

This is universal — there is no maintainer-only exemption and no way to opt back
into sending the identifying fields. The README now discloses what a report
contains and how to switch it off (`STEALTH_MCP_NO_ERROR_REPORTING=true`).

### Known gaps

Recorded, not fixed here; each is a row in the `DESIGN.md` §10 known-debt ledger.

- A clean shutdown on Linux and macOS still logs at ERROR, so every graceful stop
  ships Sentry events (`STEALTH-CHROME-DEVTOOLS-MCP-1J`, `-1H`). Two independent
  causes: our signal handler replaces the HTTP server's rather than handing control
  back to it, and FastMCP pins a zero-second graceful-shutdown budget, which always
  times out. Windows is unaffected. (F-809)
- A cold-start lock **loser** can poll a port the winner never bound for up to 120 s
  before self-healing, when a foreign process squats the preferred port (F-509 A2).
- Handled tool errors reach Sentry at full volume: the durable debug log is
  deliberately un-deduped, and the same records feed error reporting. One user-script
  `SyntaxError` produced 132 events (`STEALTH-CHROME-DEVTOOLS-MCP-P`).
- `debug_logger` records emitted inside a **stdio proxy** reach no log file — only the
  backend role has a handler for them.
- A single unreproduced WebSocket 404 during post-launch window measurement
  (`STEALTH-CHROME-DEVTOOLS-MCP-1E`); measurement is already guarded, so the visible
  cost is `measured: false`.
- `~/.stealth-mcp/server.port` is still written and still has no reader.
- A clone directory shielded from the storage sweep stays shielded if its spawn dies
  before the instance exists.

## 2.0.3

### Fixed — the shared backend no longer absorbs the host project's `.env` (#56)

`Settings` read `.env` from the **current working directory**, and MCP clients
launch this server with cwd set to whatever project folder the user opened. The
backend therefore configured itself from that project's application config. Under
the model's `extra="forbid"` schema this was fatal, not merely wrong: a folder
whose `.env` held nothing but `DATABASE_URL` and `NEXT_PUBLIC_*` killed the
backend at startup with a `ValidationError`, for **every** session connected to
it — an ordinary Next.js repo was enough. The same read silently adopted a host
`PORT=3000` or `DEBUG=true` as this server's own.

The `.env` file is now read from `~/.stealth-mcp/.env` — the state dir that
already holds the logs, the port file and `server.json`. A project-local `.env`
is never read. `extra="forbid"` is kept deliberately: with the file scoped to our
own state dir, strictness protects the operator from typos in a file they wrote
instead of punishing them for one they did not. Operators who had put keys in a
project `.env` must move them to `~/.stealth-mcp/.env` (see `.env.example`).

### Changed — error reporting is on by default and reads no `SENTRY_DSN` (#55)

`SENTRY_DSN` is the single most common key in a product repo's `.env`, so the
opt-in knob for *our* error reporting was in practice a switch the host project
flipped: the backend adopted the app's DSN and shipped this tool's crashes into
someone else's project. There is no `sentry_dsn` setting any more.

Reporting now goes to this project's own hardcoded DSN — the one previously
published in the README, and public by design, since a DSN is an ingest address
and not a credential. `sentry-sdk` moved from the `[sentry]` extra into the
package's dependencies (a default-on feature that only works if you remembered to
install something is not on), and the extra is kept, empty, so existing
`pip install stealth-chrome-devtools-mcp[sentry]` command lines keep resolving.
`sentry_init()` can no longer raise: a missing SDK degrades to a logged warning,
where it used to abort startup with a `RuntimeError`. Opt out with
`STEALTH_MCP_NO_ERROR_REPORTING=true`.

## 2.0.2

### Fixed — multi-session cold start can no longer evict the backend it is racing (F-807)

The singleton's cold-start lock used to be released when the backend's socket
bound, while the reuse gate demands an answered MCP `initialize` on a single 2s
probe. A session acquiring the freed lock inside that gap — or while the backend
was busy absorbing a fleet of simultaneous startups — concluded "not reusable",
**terminated** the healthy backend everyone else was using, and double-spawned.
The winner now holds the lock until the backend is genuinely MCP-ready, and a
lock-holder gives a same-identity backend (version AND source fingerprint both
match) up to 60s of retried probes before it may evict. A stale record still
evicts immediately (upgrades take effect now), and a dead one (no socket, no
live process) skips the wait entirely, so crash-recovery cold starts stay fast.

### Added — startup-herd scale test

`tests/test_startup_herd.py` starts **40 real stdio launcher processes at
once** against a cold isolated workspace and requires every session to finish
`initialize` + `tools/list` within 30s, with exactly one logical backend
spawned, plus a warm-join bound for a 41st session. Measured on a Windows
workstation: full 40-session cold herd usable in **7.9s**, warm join **1.0s**.

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
