# prep-t7: anchored docs/CHANGELOG edit list for plan_F808 Task 7

Produced 2026-08-02 by a read-only Opus research agent at worktree HEAD 58e7725.
This file is the dispatch spec for the Task 7 implementer. Paths relative to the
worktree root; line numbers are that HEAD.

## Three shape-changing findings

1. DESIGN.md §2.2 is ALREADY updated (commits 7989dee/32b3185 landed schema v2,
   the F-808 note, the v1-still-reads sentence at DESIGN.md:99-117). Do not
   redraft. Still missing from DESIGN.md: the adoption section (asymmetry, new
   §2.7 below) and the invariant correction in §1.
2. server.py's viewport_width docstring is ALREADY corrected (server.py:352-356,
   commit d209b46). The remaining F-804 imprecision is in window_sizing.py:13-14
   and in the F-804 finding file (drafts below).
3. Task 6 has NOT landed at 58e7725 — every RUNBOOK/README line quoting a
   `doctor` string below is drafted from the Task 6 spec and MUST be re-verified
   against the strings impl-t6 actually lands before committing.

## 1. CLAUDE.md

### 1a. CLAUDE.md:60 — singleton.py nav row still claims server.json. REPLACE:

| `singleton.py` | **backend lifecycle + the stdio proxy** — liveness (`_backend_http_ready`, `_probe_backend_status`), port selection (`_select_backend_port`, `DEFAULT_PORT`), the one identity+readiness reuse gate (`_same_identity_backend_ready`, `_source_fingerprint`, `REUSE_PATIENCE_SECONDS`), cold-start lock (`_start_backend_holding_lock`), `run_stdio_proxy`. The `server.json` record moved to `backend_registry.py` (path names re-exported here for legacy callers) |

### 1b. CLAUDE.md:60 — insert two NEW rows immediately after:

| `backend_registry.py` | **THE one home for the `server.json` record** — schema v2 (`SCHEMA_VERSION`, one entry per display context), read/write/clear (`read_backends`, `record_backend`, `forget_backend`, `clear_record`), the adoption order (`adoption_candidates`, `window_capable_first`), per-context port lookup (`port_for_context`, `own_or_first_port`, `port_conflict`), plus `STATE_DIR`/`SERVER_STATE_FILE`/`PORT_FILE` |
| `display_context.py` | **THE one home for "can a window launched by THIS process be seen, and on which desktop"** — the opaque token (`display_context()`: `headless` / `unverified` / `win-session-N` / `wayland-…` / `x11-…` / `aqua-<uid>`) and `can_show_windows()`. Observational only: it never picks or enters another session (F-808) |

### 1c. CLAUDE.md:119 — glossary **backend** row (the deliberate invariant change):

| **backend** | the shared detached `python -m … --transport http` process running FastMCP + all 94 tools — **one per (source fingerprint, display context)**: in practice one on a headless box, at most two on a desktop box that is also SSH'd into (F-808) | the stdio proxy; "the server" (ambiguous — avoid); the pre-2.0.4 "exactly one process per machine" reading |

### 1d. CLAUDE.md:130 — append three glossary rows after **cloner engine**:

| **display context** | the opaque token from `display_context.display_context()` naming the desktop a window launched by THIS process would appear on (`win-session-N`, `wayland-…`, `x11-…`, `aqua-<uid>`), or `headless` (PROVEN invisible) / `unverified` (unclassifiable, treated as capable) | the `headless=` spawn argument (a caller's request); a browser session; the `DISPLAY` env var (one input to the Linux branch only) |
| **adoption** | a client's decision to reuse a recorded backend, ordered by `backend_registry.adoption_candidates`. Deliberately **asymmetric**: a client that cannot prove it has a desktop (`headless`/`unverified`) adopts any backend, window-capable first; a client that CAN prove one adopts only its own context's entry plus `unverified` ones | reuse *identity* (version + source fingerprint, at `singleton._same_identity_backend_ready`) — adoption picks WHICH record to test, identity decides whether it passes |
| **backend record (`server.json`)** | the schema-v2 file at `~/.stealth-mcp/server.json`: `{"schema": 2, "backends": {"<display context>": {port, version, pid, source_fingerprint, display_context}}}`, owned solely by `backend_registry.py`. A v1 flat record still reads, as one entry classified `unverified` | `server.port` (`PORT_FILE`) — write-only legacy with no reader in `src/`; `singleton.lock`; the in-memory storage |

### 1e. Harness follow-on — tests/test_doc_claims.py:92-113: add "display_context"
and "backend_registry" to TestNavMapModules.LIVE_EMBEDDED (both exist → stays
green). Required once 1b lands, or the harness is blind to the new rows.
(If Task 10's browser_pid_registry is also added to the nav map, add it there too.)

## 2. DESIGN.md

### 2a. DESIGN.md:21 → "There are **two front-ends** over a **shared backend
process**:". DESIGN.md:30-36 → replace the "Both talk to the **one** backend"
paragraph with:

Both talk to a shared ***backend***: a detached
`python -m stealth_chrome_devtools_mcp --transport http` process that hosts FastMCP
and all 94 tools. There is **one backend per (source fingerprint, display context)** —
in practice one process on a headless box, and at most two on a desktop box that is
also SSH'd into (§2.7). A Claude Code session connects through a short-lived
***stdio proxy*** that bridges stdio ↔ the backend's HTTP; the ops CLI talks to the
same backend over the same HTTP contract. Keeping the tool surface and the CLI as
thin front-ends over a shared backend is what lets N client sessions share one
browser fleet without N competing servers.

### 2b. DESIGN.md:99-117 (§2.2) already correct. OPTIONAL 3-line addition after
:117 for supersede-by-port in the why-doc:

Recording a backend also **supersedes by port**: any other entry claiming that port
is dropped, because only one process can hold a loopback listener, so a second entry
naming it is by construction a leftover (a v1 record, or a context token that changed
under a backend that did not — a Windows session id is reassigned across an RDP
reconnect). Entries on other ports, `unverified` included, survive.

### 2c. DESIGN.md:176 — insert NEW §2.7 between §2.6 (ends :175) and the ---
at :177 (full drafted text):

### 2.7 Display context: where a window launched here would be seen

Chrome inherits its parent's window station, so whether a headed browser is
**visible** is decided by the process that launches it — never by the caller and
never by the `headless` flag. On the machine F-808 was reported from, the shared
backend had been cold-started by an SSH client in Windows **Session 0** (isolated
since Vista; its desktop is never composited onto a user's screen), so every
desktop session on that box reused it and got a browser that was fully driveable
over CDP and permanently invisible. `spawn_browser` reported `state: "ready"`,
`headless: false`, `window_size.measured: true` — all true, none of them an
observation of a window.

`embedded/display_context.py` makes that property explicit and **observational**:
it reports OUR OWN context and never tries to pick or enter someone else's session.
`WTSGetActiveConsoleSessionId()` is deliberately not used — on the reporting machine
the active console session was 2 while the user's desktop lived in session 1, so any
"find the interactive session" heuristic is wrong on somebody's machine. The token is
`headless` (PROVEN invisible), `unverified` (unclassifiable — treated as capable, so
a broken probe can never block headed browsing), or a specific desktop:
`win-session-N`, `wayland-<display>`, `x11-<display>`, `aqua-<uid>`.

**Identity is a preference, not an equality test — in one direction only.** The
asymmetry lives in `backend_registry.adoption_candidates` and is the whole fix:

- A client that **cannot prove** it has a desktop (`headless` or `unverified`)
  adopts **any** recorded backend, window-capable entries first. This is what makes
  an SSH session's headed spawn visible: it converges on the desktop backend, and
  the window opens on the real desktop. For such a client display context is a
  *preference*, never a filter.
- A client that **can prove** it has a desktop adopts only its **own** context's
  entry plus `unverified` ones. Every other proven context is excluded — foreign
  desktops as much as `headless`. Identity cannot separate them (a sibling desktop
  runs the same install, so version and fingerprint both match), yet a browser
  spawned there renders on a window station this user cannot see. Refusing costs
  one cold start.

`unverified` is adoptable on **both** sides, because it is what every record written
up to 2.0.3 reads as. Refusing it would evict a healthy backend the moment a user
upgrades. **Only a PROVEN verdict moves anything** — that rule also governs
`port_conflict`, where treating `unverified` as a conflict would divert the spawn to
a random free port, send eviction at the wrong port, and leak the live 2.0.3 backend
and its Chrome processes for good.

Where no window-capable backend exists at all, `spawn_browser(headless=False)`
**raises** a `ToolError` naming the context and the two remedies (start a backend from
a desktop session; or pass `headless=True`). Silent headed→headless degradation was
rejected as the same defect wearing a different hat. Headless spawns from a `headless`
context are unaffected — CI depends on exactly that.

### 2d. DESIGN.md:418-429 (§10 known-debt ledger) IS the "known gaps for 2.0.5"
home. Append six rows after :429:

| **F-509 A2** | cold-start lock **losers** commit to their own `_select_backend_port` result; behind a foreign squatter the winner's `_free_port()` pick differs, so a loser polls a port nothing will bind for the full `BACKEND_READY_TIMEOUT` (120 s) before self-healing. Recorded in `TRIAGE_final-review_to_plan_RELEASE.md` A2 (singleton.py, MED). Fix shape: losers re-read the record after the lock resolves, or serialize port choice under the lock. OPEN. |
| **`_PROTECTED_CLONE_DIRS` lifetime** | `clone_storage._protect_clone_dir` shields an in-flight clone from the cap sweep, but protection is released only on a clean `close_instance`; a spawn that dies between `_protect_` and its instance record leaks a permanently sweep-exempt entry in a process-global set. Bounded and small, never audited. OPEN. |
| **`PORT_FILE` vestige** | `server.port` is written at `singleton.py:398` and cleared by `stop_backend`, with **no reader anywhere in `src/`**. Now that `backend_registry` owns the record, deleting it is a one-line removal plus the five test fixtures that redirect it. OPEN. |
| **Proxy-side `debug_logger` records reach no file** | `debug_logger` emits to the `stealth.backend` logger; `logging_setup.configure_logging(role)` attaches a handler only to `stealth.<role>` with `propagate = False`. In a stdio-proxy process only `stealth.proxy` is wired, so every `debug_logger` warning raised proxy-side is discarded. F-808 made this load-bearing: `display_context`'s "session probe refused / raised → unverified" warnings fire in the proxy, exactly where an operator would look to explain an invisible spawn. OPEN. |
| **Handled `ToolError`s ship to Sentry at full volume** | `debug_logger.log_error` emits `_backend_logger.error(...)` **unconditionally and un-deduped** (deliberate, F-182/F-204: the durable file must have every occurrence), and `observability.py:80` installs `LoggingIntegration(event_level=logging.ERROR)`. So an ordinary user-side error becomes a Sentry event per occurrence — Sentry issue `STEALTH-CHROME-DEVTOOLS-MCP-P` is **132 events from a single user-script `SyntaxError`**. The durable-file requirement and the Sentry volume want different gates; today there is one. OPEN. |
| **WebSocket 404 in `window_sizing.apply_and_measure`** | Post-launch measurement raised a WebSocket-404 on a teammate machine (Sentry `STEALTH-CHROME-DEVTOOLS-MCP-1E`, **one event**, not reproduced). Measurement is already guarded — it degrades to `measured: false` rather than failing the spawn — so the visible cost is a spawn that reports an unmeasured size. Root cause unknown; too thin to act on. OPEN. |

## 3. RUNBOOK.md

### 3a. RUNBOOK.md:25 — doctor verb row (VERIFY against Task 6's landed output):

| `doctor` | environment check: Python, platform, browser-session root, **one line per recorded backend with its display context and whether it can show a window**, port occupant, Chrome |

### 3b. RUNBOOK.md:45 — sample status block "version : 2.0.3" → 2.0.4.

### 3c. RUNBOOK.md:115-120 ("Port already in use") — replace the last sentence:

`stop` forgets the stopped backend's own display-context entry and clears
`server.json` only once nothing else is recorded, so the next start returns to
`19222` if it is free — and stopping one backend never makes another desktop's
live backend undiscoverable.

### 3d. RUNBOOK.md:149 — NEW recovery playbook after "Code edit didn't take
effect", before the --- at :150 (do NOT add a doc-example: runnable marker):

### Headed spawn fails: "cannot display a window"

`spawn_browser(headless=False)` raises a `ToolError` naming a display context
(`headless`, or a desktop token) when the backend serving your session runs
somewhere a window could never be seen — a Windows service session (Session 0), or
an SSH login with no `DISPLAY`/`WAYLAND_DISPLAY`. This is deliberate: before 2.0.4
the same spawn returned `state: "ready"`, `headless: false` and a browser that was
fully driveable over CDP and permanently invisible (F-808).

Run `doctor`. It lists one line per recorded backend with its display context and
whether that context can show a window. Two outcomes:

- **A window-capable backend is listed.** Your session should already be using it —
  discovery prefers a window-capable backend, and a client that cannot prove it has
  a desktop adopts any of them. If it is not, the entry is version- or
  source-stale; `restart` or let the next cold start evict it.
- **No backend can display a window.** `doctor` says so explicitly. Start one from
  a desktop session — open a Claude Code window on the physical desktop and let it
  cold-start a backend, or run `stealth-chrome-devtools serve --http` there. Every
  other session, SSH included, then converges on it and headed spawns become
  visible on the real desktop.

If you only need automation and not a visible window, pass `headless=True`; that
path is unaffected by display context and is what CI uses.

## 4. README.md

### 4a. README.md:146 — NEW subsection at the end of "How It Works", before
"## Usage Examples" at :147:

### Headed Browsing and Where the Window Opens

A headed browser appears on the desktop of whichever process **launched** it, not
of whichever session asked. Because sessions share a backend, a backend that was
first started from an SSH login or a Windows service session cannot show a window
to anyone — including the sessions running on the physical desktop.

So the backend is keyed by **display context**: one per desktop, plus one for a
headless context. Discovery prefers a backend that can show a window, which means
an SSH-driven `spawn_browser(headless=False)` automatically uses the desktop
backend and its window opens on the real screen. Where no such backend exists, the
spawn **raises** instead of handing back an invisible browser; run
`stealth-chrome-devtools doctor` to see which contexts have a backend. Headless
spawns work from anywhere.

### 4b. README.md:285-287 (Requirements) — optional one-liner:
- A desktop session for **headed** browsing (headless works from SSH, CI, and services)

### 4c. README.md:238 env table, :289-306 Error Reporting, :321-326 Documentation
— NO change (F-808 adds no env var; other "headless" mentions are an unrelated
code sample at :159-160).

## 5. CONTRIBUTING.md — no F-808 content (verified). RECOMMENDED addition after
:139 documenting the contract regen (currently only in RELEASE_CONTRACT.md's
generated header + the test failure message):

### The release contract is generated

`RELEASE_CONTRACT.md` is output, never hand-edited — every count in it derives from
the live tool registry, the claim ledger, and the evidence aggregate. Regenerate it
in the **same commit** as whatever changed those:

    PYTHONUTF8=1 uv run python tools/gen_release_contract.py --write

`tests/test_release_contract.py::test_the_contract_is_regenerated_not_edited` runs
`--check` in the unit gate on all three OSes, so drift is a red test.

## 6. window_sizing.py:13-14 — CONFIRMED imprecision. Replace lines 13-18 with:

Neither transport can *promise* the result. Headed Chrome clamps a window to the
work area of the desktop the **launching process** can draw on — which is the
user's monitor only when the backend runs there, and is Session 0's 1024x768
default desktop when it does not (F-808). A 1920x1080 request against such a
desktop lands at about 1044x788, while headless, having no window manager,
honours the request exactly. That asymmetry is F-804: before this module the
spawn result echoed the *request* back as though it had been applied, so a
clamped headed window reported a size it never had.

## 7. F-804 finding file
audit/stage2/finding_F804_headed_window_size_clamped_and_misreported.md:46-48 —
replace item 2 with:

2. **Headed Chrome clamps to the work area of the LAUNCHING process's desktop.**
   Anything larger came back at ~1044x788 — the same number for 1200x800 and for
   1920x1080, which is the signature of a clamp rather than of a dropped argument.
   *Corrected 2026-08-02:* this finding originally read that 1024x768 as an
   ordinary monitor limit, which implied a machine driving an RTX 3080 had a
   ~1024x768 screen. It was **Session 0's default desktop** — the backend had been
   cold-started from a non-interactive session, and that is also why the browser
   was invisible. See
   `finding_F808_headed_spawn_is_invisible_when_the_backend_was_cold_started_from_a_non_interactive_session.md`.
   The remedy below (report requested/actual/clamped truthfully) is unchanged and
   still correct; only the stated cause was wrong.

## 8. F-808 finding file — status flip
audit/stage2/finding_F808_headed_spawn_is_invisible_when_the_backend_was_cold_started_from_a_non_interactive_session.md
- :3 → **Status: FIXED** in 2.0.4 on branch `fix/F808-headed-visibility`.
  **Severity: HIGH. Regression from 1.0.0.** (keep :4-7)
- Retitle :96-101 "Workaround available today" → "## Workaround before 2.0.4".
- Append closing section after :101:

## The fix (2.0.4)

Both halves of "Proposed fix" landed, and the shape changed in one deliberate way:
display context did **not** join the reuse gate as an equality test, because that
would have refused the desktop backend to the very SSH client that needs it.

| Commit | What |
|---|---|
| `<T1 SHA>` | `embedded/display_context.py` — the observational token and `can_show_windows()` |
| `<T2 SHA>` | `embedded/backend_registry.py` — the `server.json` record moved out of `singleton.py` (pure refactor) |
| 7989dee, 32b3185, 62d4813 | schema v2: one backend per display context; v1 records still read as `unverified`; supersede-by-port |
| 85f7fe6, 09433a0, f02334c, 4e2ede3 | discovery prefers a window-capable backend; asymmetric adoption; `unverified` is never a port conflict; restart terminates only the port it is about to bind |
| 2b22fe1, d209b46, 58e7725 | `spawn_browser(headless=False)` raises in a non-capable context instead of returning a ghost; the F-804 docstring clamp correction |

The reported symptom is closed by the **adoption** half, not the refusal half: an
SSH client now converges on the desktop backend and its headed spawn opens a
visible window. The refusal is the floor under the case where no such backend
exists anywhere.

**Acceptance** (plan_F808 Task 8 step 5): on the reporting machine, with a backend
cold-started from the desktop session, an SSH-driven `spawn_browser(headless=False)`
puts a visible window on the physical desktop.

(Resolve `<T1 SHA>`/`<T2 SHA>` from git log — Task 1 commits a1b3075+8a78561,
Task 2 commits efed9d0+663da1a per the session record; verify with
`git log --oneline` before filling in.)

## 9. CHANGELOG.md — insert the whole 2.0.4 block at :2, above "## 2.0.3".
Format: `## <version>` newest-first; `### <Fixed|Changed|Added> — <sentence>`
subsections written as prose paragraphs. FULL DRAFTED TEXT:

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

### Fixed — the window-size clamp is attributed to the right desktop (corrects F-804)

2.0.1 reported window sizes truthfully but explained the clamp as headed Chrome
fitting "the desktop work area", read as an ordinary monitor limit. That reasoning
concluded a workstation driving an RTX 3080 had a ~1024x768 screen. The real clamp
was **Session 0's default desktop** — the same root cause as F-808. The remedy is
unchanged (`spawn_diagnostics.window_size` still reports `requested`, `actual`,
`inner_viewport` and `clamped`); the docstrings on `spawn_browser` and
`window_sizing` now say the clamp is to the **launching** context's desktop, which
is the user's monitor only when the backend runs on it.

### Fixed — test runs no longer ship injected failures to the real Sentry

Error reporting is on by default and `LoggingIntegration` forwards every
ERROR-level log, so a local test campaign — which deliberately injects failures —
pushed roughly 50,000 noise events into the live project in 15 hours. `conftest.py`
now sets `STEALTH_MCP_NO_ERROR_REPORTING=1` as a session-wide default alongside its
existing env guards, and because the singleton strips only
`STEALTH_MCP_NO_AUTO_RECOVERY` from a spawned backend's environment, real-Chrome
integration backends inherit the mute too. An explicitly-set value still wins, so a
CI cell that *wants* reporting keeps it.

Fixes `STEALTH-CHROME-DEVTOOLS-MCP-K` — 66 nodriver "Failed to connect to browser"
events on 2.0.3, all from headed spawns driven over the magent/psmux SSH path
against a backend that had no desktop to put a window on. That is F-808's signature
seen from the other end, and it is closed by the adoption fix above.

### Known gaps

Recorded, not fixed here; each is a row in the `DESIGN.md` §10 known-debt ledger.

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

## 10. Commands Task 7 must run

Version bump is NOT Task 7's — 2.0.1/2.0.2/2.0.3 each bumped pyproject.toml:7 in
a separate "Release X.Y.Z: <headline>" commit (7bb5bcf, 0812716, 67b7f28) that
also carried the CHANGELOG... BUT gen_release_contract.release_version() reads
pyproject and RELEASE_CONTRACT.md:4 currently says "version 2.0.3".
Recommendation: let Task 8's release commit bump pyproject; if Task 7 bumps it,
regenerate the contract in the SAME commit or test_release_contract.py goes red.

    PYTHONUTF8=1 uv run python tools/gen_release_contract.py --write
    uv run python -m pytest tests/test_doc_claims.py tests/test_doc_examples.py tests/test_release_contract.py -v
    PYTHONUTF8=1 uv run python tools/gen_release_contract.py --check   # exit 0
    uv run python -m pytest tests/ -q                                  # full unit lane
    uv run python tools/check_file_budgets.py && uv run ruff check .

On this checkout path, `uv run pytest` fails to canonicalize the console script —
use `uv run python -m pytest` (CONTRIBUTING.md:31-50).
tests/test_doc_examples.py executes fenced blocks marked
`<!-- doc-example: runnable -->` — none of these drafts add such a marker; keep
it that way for the RUNBOOK playbook (3d).

## 11. For the PR, not Task 7's diff

- tools/check_file_budgets.py:32-33 "CAP RAISE 3389->3401 PENDING HUMAN
  RATIFICATION" — must be requested explicitly in the PR body; Task 7 must NOT
  quietly resolve it.
- audit/STAGE3_RESUME_PROMPT.md:46 "M2's reuse key reads server.json" — still
  true, now indirectly via backend_registry. Historical resume prompt; leave it.

Verified clean, no edit needed: tests/release_gate_harness.py:492 (parses both
schemas); tests/MANUAL_QA_PROTOCOL.md:1703 (prose stays true); CONTRIBUTING.md
(no display/discovery claims); README.md "headless" at :159-160 (unrelated
sample).

## Addendum 2026-08-02 — doc items handed off by Task 10 (landed d84323e..22529ad)

Task 10 extracted `embedded/browser_pid_registry.py` (331 LOC leaf) out of
process_cleanup.py and added per-entry owner identity. New doc obligations:

1. **RUNBOOK.md:123-124 is now stale**: "`kill-orphans` reaps them (and clears
   the pid-tracking file)" — it no longer clears the file; it drops only the
   entries it reaped and leaves other backends' entries alone. The `--force`
   sentence stays true (step 10c preserved it) but add a clause: force also
   bypasses the OWNERSHIP check, not just the live-backend gate.
2. **CLAUDE.md nav map**: new row under "Lifecycle & transport" for
   `embedded/browser_pid_registry.py` — suggested owns-line: "the
   `browser_pids.json` tracking record — its schema, the owner identity on
   every entry, and the read-merge-write protocol every writer shares". Also a
   glossary pair disambiguating **backend registry** (server.json — which
   backend to talk to) from **browser-pid registry** (browser_pids.json —
   which browsers are tracked and by whom). test_doc_claims.py's LIVE_EMBEDDED
   does not currently include the new module — decide deliberately whether to
   add it (extending TestLoadBearingSymbols too, per the earlier addendum).
3. **New on-disk artifact**: `~/.stealth-mcp/browser_pids.json.lock` (empty,
   persistent sibling lock, same idea as singleton.lock) — add wherever the
   state dir's contents are listed (RUNBOOK).
4. **Record shape**: entries gained `owner_pid` + `owner_create_time`;
   deliberately NO schema-version bump — absence IS the legacy signal, which
   is what lets an upgrade reclaim 2.0.3's orphans. Docs must not invent a
   migration.
5. **F-804 doc correction now has a measured number**: Windows Session 0's
   default desktop is exactly 1024x768 (measured via GetDesktopWindow rect) —
   that is the source of the "RTX 3080 machine reported ~1024x768" mystery in
   the F-804 finding. Cite it as measured, not inferred.

## Addendum 2026-08-02b — spec-t10 stage-1 review confirmations (verdict: Pass, all 6 deviations accepted)

The reviewer independently verified and sharpened the Task-10 doc items above:

1. **RUNBOOK.md:124 is the ONLY stale "clears the pid-tracking file" claim** — a
   repo-wide grep (excluding audit/) returns that one line. Rewrite it: reaped
   entries are dropped by id; every other backend's entries are preserved; the
   empty record `{"browser_processes": {}}` stays on disk (deliberate — no
   unlink semantics anywhere).
2. **`--force` is a bigger hammer than "overrides the guard" conveys**: it now
   reaps entries whose OWNER backend is alive, not just wedged-gate bypass.
   Document its reach explicitly in the RUNBOOK `--force` sentence.
3. **Nav-map owns-line framing the reviewer suggests** for
   `browser_pid_registry.py`: "the one home for `browser_pids.json` — its
   schema, the owner stamp, and the read-merge-write protocol every writer
   shares". (The §1b rows for backend_registry/display_context are already
   drafted above — apply them; the reviewer confirmed all three modules are
   absent from the current map.)
4. **CHANGELOG gap**: the drafted 2.0.4 block above predates Task 10. Add a
   "Fixed" subsection for the fratricide fix: concurrent backends no longer
   erase each other's `browser_pids.json` entries (read-merge-write under a
   sibling lock `browser_pids.json.lock`); entries carry owner identity
   (`owner_pid`+`owner_create_time`); recovery reaps only browsers whose owner
   backend is DEAD; legacy 2.0.3 entries (no owner keys) are reclaimable
   orphans by construction — deliberately no schema bump, absence IS the
   legacy signal.
5. **DESIGN §10 ledger gets a seventh row (F-809)**: clean-shutdown ERROR noise
   on POSIX — our `_signal_handler` replaces uvicorn's `handle_exit` so
   uvicorn's graceful path is dead code, and FastMCP 2.11.2's hardcoded
   `timeout_graceful_shutdown: 0` makes `wait_for(coro, 0)` raise on every
   graceful HTTP stop (Sentry -1J/-1H). Spec:
   `prep_F808_shutdown_noise_spec.md`. Status OPEN; whether it rides 2.0.4 is
   the human's call at PR time — the ledger row and a CHANGELOG known-gap
   bullet record it either way (update if it lands first).
