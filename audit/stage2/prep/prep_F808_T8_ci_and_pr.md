# prep-t8: CI placement + PR draft for plan_F808 Task 8

Produced 2026-08-02 by a read-only Opus research agent at worktree HEAD 58e7725.
This file is the dispatch spec for the Task 8 implementer. Line numbers are that
HEAD.

## Three plan-shaping findings

1. NO F-804 assertion is gated "pending F-808" — but the regate is real and
   INVERTED: `tests/test_window_sizing.py`'s `_HEADED_NEEDS_DESKTOP` (:43-46)
   gates on platform (linux), not capability. This branch's guard
   (server.py:388-397) will REDDEN the two headed F-804 nodes
   (`test_headed_spawn_honours_a_size_that_fits` :161,
   `test_headed_spawn_reports_a_clamped_size_truthfully` :181) on a Session-0
   Windows CI runner. Fix: make the skipif capability-derived —
   `not display_context.can_show_windows() or sys.platform.startswith("linux")`
   — keeping BOTH clauses with both reasons (Linux bare-Xvfb has no WM to
   clamp, per F-804; a non-capable context now refuses the spawn, per F-808).
   Headless node :208 untouched. This is a correctness fix, not a formality.
2. PIN DISCREPANCY: `~/.claude.json:1144` reads
   `stealth-chrome-devtools-mcp==1.0.0` (verified) — NOT ==2.0.2/2.0.3 as
   memory recorded. Yet Sentry shows release 2.0.3 running on server_name
   "amin", so multiple pin/config locations exist (~/.claude.json per-project
   MCP entries vs ~/.claude/.mcp.json). At ship time AUDIT ALL pin locations
   before declaring the user upgraded.
3. plan_F808.md:718-738 names the test file: extend
   `tests/test_browser_integration.py`.

## CI inventory (4 workflows)

- `test.yml` (name CI): push → main,dev ONLY; pull_request on ANY base. Body
  calls `release-gate.yml`. Pushing a branch runs ZERO checks; only a PR runs
  the gate.
- `release-gate.yml` (workflow_call only; 1114 lines, 12 jobs):
  - `quality` (ubuntu) — runs `tools/check_file_budgets.py` at line 75: the
    LOC-cap decision passes or fails HERE.
  - `unit-tests` :113-128 — 9 cells {ubuntu,windows,macos}×py{3.11-3.13},
    `-m "not integration"` (line 156).
  - `coverage` :185-219 — 3 cells, same selector.
  - `integration` :260-434 — 3 cells ubuntu/X64, windows/X64, macos/ARM64.
    Line 316-320 selector: macOS `integration and not transport`, else
    `integration`; `--timeout=180 --junitxml=junit.xml`. Linux gets
    DISPLAY=:99 bare Xvfb (:291-297); Windows/macOS DISPLAY=''.
  - `transport` :445-490 — linux+windows (macOS excluded, F-773).
  - `offline-stealth` :619-667; plus known-gaps, build-dist, package-verify,
    install-smoke (6 cells), release-evidence, and `release-gate` (aggregate =
    the required check).
- `publish.yml`: push tags v*; calls the gate with ref+release_tag; publishes
  the gate's dist artifact.
- `canary.yml`: schedule 06:17 UTC + workflow_dispatch.

## Placement

The new node lands in the EXISTING `integration` job's windows/X64 cell with
ZERO workflow edits (it is `@pytest.mark.integration`; Windows already has the
timeout and junit). Do NOT add a job — `tests/test_release_workflows.py` pins
the topology four ways (aggregate `needs` :113, result assert :128,
`tools/release_evidence.py` REQUIRED_CELLS :252, upload :303). Linux exclusion
lives in the TEST (branch A is `sys.platform == "win32"`-scoped; EnumWindows is
Win32-only) — the Linux cell simply collects nothing new. No `--mq` id: F-808
has no Manual-QA protocol step; the acceptance is manual-on-the-user's-machine,
and binding an MQ id the gate can't satisfy is the cheat the :360-369 comment
refuses.

## Integration idioms to reuse

`tests/test_browser_integration.py`: bootstrap :21-34 (importlib load of
embedded/server.py as `server`), `_unwrap` :37, module `pytestmark` :43-67
(integration + skip when no browser/module). Spawn via
`_get_fn("spawn_browser")` + `**_sandbox_kwargs()` (:255-260).

COPY VERBATIM the process-tree idiom `TestCloseKillsProcessTree` :249-298:
- root pid from
  `process_cleanup.process_cleanup.browser_processes[iid]["pid"]` (:266-267);
- `psutil.Process(root).children(recursive=True)` captured AFTER a navigate so
  renderers exist (:263-264, :271-275);
- bounded deadline poll (`time.monotonic() + 8.0`, `asyncio.sleep(0.2)`,
  :281-285) — never sleep-then-assert;
- `finally:` psutil kill net over the whole tree (:292-298) so a failed
  assertion never leaks Chrome (agent-fleets-exhaust-windows-chrome).

Mechanism home `tests/e2e_helpers.py`: CAN_RUN, get_fn, sandbox_kwargs(),
warmup_once() (:103-124, bounded 3-attempt retry), eval_js,
navigate_and_settle, wait_for_js; autouse `_warmup` fixture pattern at
test_window_sizing.py:154-158. Mechanism only, never test logic.

Branch B asserts ONLY `ToolError` + "cannot display a window" against the real
machine. The full 6-token message contract is owned by
`tests/test_spawn_headed_requires_display.py:95-105` — re-asserting all six
here would be the second way.

## window_sizing regate, precisely

Nothing currently-off turns on. Change `_HEADED_NEEDS_DESKTOP` (:43-46) to be
capability-derived (both clauses, both reasons — see finding 1). The two
decorated headed nodes (:161, :181) keep `@pytest.mark.integration` via the
class at :151; headless node :208 untouched. Task 8 owes verification: run the
file on Windows locally and confirm both headed nodes RUN (not skip); read the
Windows CI cell's junit to see whether they ran or skipped there — a skip is
the honest signal the GitHub Windows runner is Session 0, and branch B proves
it rather than leaving it inferred.

## EnumWindows helper

Home: `tests/e2e_helpers.py` (NOT src/ — display_context.py already owns the
production question via ProcessIdToSessionId; a second Win32 probe in the
package = second way). test_browser_integration.py can import just the helper
(same dir, on sys.path via conftest.py:29-31).

Sketch:
```python
def _visible_window_pids() -> set[int]:            # win32 only
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: set[int] = set()
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        rect = RECT()                              # reject zero-area helper windows
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.add(int(pid.value))
        return True
    user32.EnumWindows(CB(_cb), 0)
    return found

async def await_visible_window(root_pid: int, timeout: float = 15.0) -> int | None:
    """First pid in root_pid's tree owning a visible, non-zero-area top-level
    window, or None at the deadline. Only valid when the caller shares the
    window station with the spawned Chrome (in-process integration lane) —
    EnumWindows cannot see a detached backend's desktop."""
    deadline = time.monotonic() + timeout
    while True:
        tree = {root_pid} | {p.pid for p in psutil.Process(root_pid).children(recursive=True)}
        hit = tree & _visible_window_pids()
        if hit:
            return next(iter(hit))
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)
```

Flake handling: poll to deadline (15s under the cell's 180s timeout),
re-snapshot BOTH the tree and window-pid set each iteration (children appear
late), match ANY pid in the recursive tree, reject zero-area windows
(Chrome_MessageWindow false positives; tighten to class Chrome_WidgetWin_1 via
GetClassNameW if noisy), declare restype/argtypes explicitly (64-bit HWND
truncation silently finds nothing).

Evidence: `record_property("f808_branch", "A"|"B")` + the observed
display_context() token → junit.xml, which the integration cell already
produces (:320) and uploads (:428-434) and test_release_workflows.py:327 pins
as required. Ships with the gate artifact for free.

## Version bump + harness

- `pyproject.toml:7` (version = "2.0.3") is the ONE version home; the tag
  drives publish.yml; package_verify.py fails the gate on tag/metadata
  disagreement.
- CHANGELOG.md:3 gets the 2.0.4 section (Task 7's job).
- backend_registry.py:30/202/293 + test docstrings saying "<= 2.0.3" are
  historical statements about the pre-migration format — do NOT bump them.
- `tests/release_gate_harness.py` needs NOTHING from Task 8 (already parses
  v1+v2 at :489-512; RESULT_SCHEMA_VERSION :90 is the journey-result schema,
  unrelated to server.json).
- `tools/release_evidence.py` REQUIRED_CELLS unchanged (no new job).

## Drafted PR body

Title: `F-808: a headed spawn is visible wherever the desktop is`

(Adjust the doctor checklist line to match Task 6's landed output.)

---
Fixes STEALTH-CHROME-DEVTOOLS-MCP-K

### The defect

`spawn_browser(headless=False)` from an SSH or service session returned a
healthy instance and a real Chrome — onto a desktop nobody was looking at.
Chrome inherits its launcher's window station, so whether a headed browser can
be *seen* is decided by the process that spawns it, never by the caller and
never by the `headless` flag.

The asymmetry that let this ship: adoption. A Claude Code session started over
SSH found the recorded backend, adopted it, and served headed spawns through
whichever context that backend happened to be in — while a desktop session that
cold-started its own backend got a visible window. Same tool call, same
machine, two different outcomes, and no signal distinguishing them.
`server.json` recorded exactly one backend and had no field for the question.

### The fix, in three parts

**One home for the question.** `embedded/display_context.py` is the only place
that answers "can a window launched by THIS process be seen". It is
deliberately observational — it reports our own context and never tries to
enter someone else's session (on the machine F-808 was found on, the active
console session was *not* the session holding the user's desktop, so every
"find the interactive session" heuristic is wrong somewhere). Windows reads the
TS session id, Linux reads `WAYLAND_DISPLAY` then `DISPLAY`, macOS asks
`launchctl managername`. Every probe failure returns `unverified`, which is
treated as **capable**: a wrong "headless" would block headed browsing that
works today, which is a worse regression than the ghost it prevents.

**The record knows which desktop.** `embedded/backend_registry.py` (new,
extracted out of `singleton.py`) owns `server.json` as a schema-v2 map keyed by
display context, so one machine can hold a desktop backend and an SSH backend
at once and tell them apart. Discovery then routes: a client that cannot show
windows itself adopts a window-capable backend, and a proven-capable client
adopts only its own desktop rather than borrowing a session the user cannot
see.

**A loud guard, last.** If routing did not save us, `spawn_browser` refuses a
headed spawn from a context that cannot display a window — before it clones a
profile directory or reaches the browser manager, and outside the tool's
blanket handler so the user reads the diagnosis rather than a re-wrapped
"Failed to spawn browser". The message names the actual context token, offers
`headless=True`, and points at `stealth-chrome-devtools doctor`.

### Migration: v1 records keep working

Every release up to 2.0.3 wrote a flat record with no display context. Those
still read, as one backend classified `unverified` — which is capable, so a
healthy pre-upgrade backend is adopted rather than evicted. A v2 write
supersedes the v1 entry on its port; an unverified entry is never treated as a
port conflict; `stop` forgets one context's entry instead of unlinking the
whole file; and `restart`'s two halves agree on one port and terminate only the
port they are about to bind. Evicting a live backend during a session is the
failure class that made the last release cycle painful, so the first migration
test pins that a 2.0.3 record stays reusable.

### Test evidence

- tests/test_display_context.py — classification table + fail-toward-capable
  rule + one un-mocked real-probe node.
- tests/test_backend_registry.py — v1 read-through, v2 supersede, the adoption
  policy, "unverified is never a conflict".
- tests/test_singleton_display_routing.py — the headline symptom as a test: an
  SSH client routed to the desktop backend, and the <= 2.0.3 upgrade path.
- tests/test_spawn_headed_requires_display.py — the guard's message contract
  and ordering contract (tripwires prove no profile clone and no manager call
  happen first).
- tests/test_browser_integration.py — the integration twin, Windows only: on a
  window-capable context a headed spawn produces a real top-level window owned
  by a pid in the spawned Chrome's process tree (EnumWindows over the psutil
  tree); on a non-capable context the guard's ToolError fires. It never skips
  on Windows — whichever branch ran is recorded in the JUnit report the gate
  already hashes.
- tests/test_window_sizing.py — F-804's headed assertions now gate on display
  capability instead of platform, so the new guard cannot turn them red on a
  session-0 runner.

Lanes: full unit suite, the integration lane at file level,
check_file_budgets.py, and ruff check.

### Two decisions for the human

**1. Ratify the server.py LOC cap raise, 3389 -> 3401.**
tools/check_file_budgets.py currently carries the marker "CAP RAISE 3389->3401
PENDING HUMAN RATIFICATION in the F-808 PR". The twelve lines are the guard
itself plus its docstring correction; extracting them into a helper would save
~7 lines and buy nothing. Cap == actual ruff-clean LOC, no padding. On
ratification the comment switches to the completed-ruling idiom already used by
the rows below it ("per the human gate ruling <date>") — the marker must not
survive the merge.

**2. Confirm the 2.0.4 release and pin plan.**
pyproject.toml:7 is the one version home; the tag drives publish.yml and
package_verify.py fails the gate if tag and metadata disagree. Separately: the
client pin situation is inconsistent across config locations (~/.claude.json
holds ==1.0.0 at one site while Sentry shows 2.0.3 running) — audit every pin
location as part of shipping, or none of 2.0.x reliably reaches the reporting
machine.

### Manual acceptance (on the reporting machine — nothing else closes F-808)

Over magent/psmux SSH, against a backend cold-started from the desktop session:

- [ ] server.json shows the v2 map, and a v1 record present beforehand was
      superseded on its port rather than duplicated.
- [ ] A second Claude Code session adopts the running backend — no kill, no
      respawn.
- [ ] spawn_browser(headless=False) from the SSH session puts a visible window
      on the physical desktop.
- [ ] spawn_browser(headless=False) with no desktop backend available raises
      the guard's message instead of returning a ghost instance.
- [ ] stealth-chrome-devtools doctor names the display context of each
      recorded backend.
---

## Addendum 2026-08-02 — Sentry state at PR time (fold into the PR body)

Triage of 2026-08-02 (all in Sentry with per-issue comments):

- **STEALTH-CHROME-DEVTOOLS-MCP-K stays open** (74 events, still occurring on
  the real port 19222 under 2.0.3) — the PR body's "Fixes STEALTH-CHROME-DEVTOOLS-MCP-K"
  line closes it on merge, as already drafted.
- **NEW known-gap for the PR body — clean-shutdown ERROR noise on POSIX
  (issues -1J and -1H, leave open, do NOT claim fixed):** first seen 2026-08-02
  on a second teammate machine (user `fathi`, Linux, release 2.0.3, default
  port 19222). On SIGTERM, `process_cleanup.py:186`'s `_signal_handler` calls
  `sys.exit(0)` from inside the running asyncio loop; the SystemExit detonates
  inside `selector.poll`, cancels uvicorn's lifespan/SSE tasks, and uvicorn
  logs "Exception in ASGI application" + CancelledError at ERROR level — which
  observability ships to Sentry on every clean POSIX shutdown. Benign (process
  was exiting) but noisy and alarming. Deliberately NOT fixed in this PR:
  Task 10 is restructuring the same file, and the right fix (schedule a
  graceful stop instead of sys.exit in the raw handler) deserves its own
  finding. **Finding id assigned: F-809**; implementation-ready spec at
  `audit/stage2/prep/prep_F808_shutdown_noise_spec.md`. The spec found a
  SECOND independent noise source the PR body should name even if F-809 does
  not ride 2.0.4: FastMCP 2.11.2 hardcodes `timeout_graceful_shutdown: 0`
  into uvicorn's Config, and `wait_for(coro, timeout=0)` always raises on
  CPython 3.12 — so uvicorn logs "Cancel N running task(s)…" at ERROR on
  EVERY graceful HTTP shutdown regardless of our handler. Also: our
  `_signal_handler` REPLACES uvicorn's `handle_exit` (installed via
  `signal.signal` inside `capture_signals`), so uvicorn's graceful path is
  currently dead code in the backend. F-809 lands after Task 10, needs
  cap-increase rulings on process_cleanup.py + server.py (both cap==actual),
  and whether it rides the 2.0.4 PR or 2.0.5 is the human's scope call —
  surface that question in the PR body.
  NOTE for Task 10 reviewers: if the extraction moves `_signal_handler`, the
  `process_cleanup.py:186` anchor in the Sentry issues goes stale — the issue
  comments name the symbol, not just the line.
- **Noise flood closed out**: 14 test-string issues (kaboom, error 2991 @32k,
  unique error 536 @20k, soak selectors, hook-compile fixtures, etc.) ignored
  FOREVER with reasons; 8 campaign/generic issues (CDP timeouts on port 54110,
  generic Tracebacks, asyncio InvalidStateError/TypeError, script SyntaxError)
  resolved so any recurrence on the default port re-surfaces as a regression.
  -1K resolved as by-design (the "handled ToolErrors ship at full volume" gap
  already in the known-gaps list).
- **Flood source addressed on main**: the conftest Sentry mute (f808 commit
  29f02a0) was cherry-picked to main as **8674f6a** and pushed, so test runs
  from main-based checkouts stop shipping. The F-808 PR still contains the
  original 29f02a0 — identical patch, merges cleanly as a duplicate; do not
  "fix" that during the PR.
- Teammate-machine evidence now spans TWO users (AladdinDEV's -1E WebSocket
  404 + fathi's -1J/-1H) — worth one sentence in the PR body's rollout notes:
  2.0.3 is in real multi-machine use, so 2.0.4's registry-schema change must
  keep the v1-record adoption path (it does; tested).

## Addendum 2026-08-02 — the test half of this task is ALREADY DONE (merge it, don't redo it)

Implemented in worktree `.claude/worktrees/f808-t8tests`, branch
`fix/F808-t8-integration-tests` (base d84323e, commits **3fe6b37** regate +
**9c539d3** helper/twin; 3 files, +269/-7, no src/ or workflow edits). Merge
onto the f808 branch AFTER Task 10's commits; files are disjoint by
construction. Hermetic lane on that branch: 1264/1 (+4 gate rows). Integration
file: 19 passed twice. What Task 8 proper still owns: the CI-node wiring, the
release mechanics, and the acceptance runs.

Handoff facts (from the implementer, verified claims where noted):
- **The regate is PROVEN locally**: this machine's agent shells run in
  **Session 0** (ProcessIdToSessionId=0 vs active console 2), so under the OLD
  platform-only gate both headed F-804 nodes FAIL with the F-808 guard error;
  with the regate they skip honestly. The gate is now a named predicate
  `_headed_needs_desktop(platform, can_show_windows)` with 4 hermetic pin rows
  (deviation from inline-skipif, deliberate: inline expressions can't be
  asserted).
- **The integration twin's branch A (real window matched via EnumWindows) is
  UNEXERCISED** — impossible from any Session-0 shell. It needs one run from
  the user's interactive desktop session: `uv run python -m pytest
  tests/test_browser_integration.py -k HeadedSpawnIsSeenOrRefused -m
  integration` from a normal desktop terminal. ctypes wiring separately proven
  against GetDesktopWindow(). Windows CI cells (Session 0) will record branch
  B — that is the expected, honest signal.
- `record_property` emits a PytestWarning under junit_family=xunit2 but the
  properties DO land in the XML and release_evidence.py ignores them — do NOT
  change junit_family for cosmetics.
- test_browser_integration.py now has a MODULE-LEVEL `from e2e_helpers import
  await_visible_window`; this double-execs embedded/server.py when the file
  runs alone — verified harmless (19/19 solo) — do not "tidy" it into a lazy
  import without re-running the file solo.
- No backend was spawned; ~/.stealth-mcp untouched (integration lane calls the
  importlib-loaded server module's tools directly).
- Bonus fact for Task 7/F-804 docs: Session 0's default desktop measures
  exactly **1024x768** — the measured source of F-804's "RTX 3080 reports
  1024x768" mystery.

## Addendum 2026-08-02 — pin audit COMPLETE (ship-time step of this task)

Enumerated every config location on the user's machine (2026-08-02):

| Location | Entry | Pin |
|---|---|---|
| `~/.claude.json:1144` | `stealth-chrome-devtools-mcp` (the ONLY mcpServers entry there) | `==1.0.0` |
| `~/.claude/.mcp.json:5` | `stealth-chrome-devtools-mcp` | `==1.0.0` |
| `~/.claude/.mcp.json:9` | `stealth-chrome-devtools-mcp-pip` | `==1.0.0` |
| repo `.mcp.json` | `stealth-local` | disabled by design (documented eviction hazard) |
| magent `%APPDATA%/magent/config.json` | no MCP pin, only project paths | n/a |

VERIFIED SURPRISE: the uvx cache env the 40+ live stdio proxies run
(`uv\cache\archive-v0\1DS8NYD...`) reports version **1.0.0** — the user's
sessions are proxying through 1.0.0 while the recorded backend on 19222 is
2.0.3 (started by something else). The earlier memory's "one entry repointed
to ==2.0.3" is NOT true of any config file that exists today. Rollout step at
ship time therefore: update ALL THREE ==1.0.0 pins to ==2.0.4 (or unpin), and
expect uvx to fetch fresh; the magent/psmux acceptance test is only valid
AFTER the pins move, since today's proxies aren't even running 2.0.3.

## Addendum 2026-08-02b — review outcomes feeding this task

**Task 10 stage-1 (spec-t10): PASS, all six deviations accepted.** One
informational item for the PR body: step 10d replaced the inherited 4x50ms
lock budget with a 5.0s deadline at 10ms polls in `browser_pid_registry.py`.
Justified and measured (the old budget lost 120/180 entries under 3
concurrent writers), but a genuinely stuck lock holder now stalls a
`track_browser_process` call up to 5s before degrading to a logged skip
(previously 200ms). Bounded and self-repairing; worth knowing next to the
F-509 startup-herd history if a spawn ever appears to hang under a
multi-backend fleet.

**t8tests combined review (rev-t8tests): APPROVE** — 2 IMPORTANT + 3 MINOR
routed back to impl-t8tests for fixup commits on
`fix/F808-t8-integration-tests` (unguarded `await close` in the twin's
finally; vacuous `owner in tree or pid_exists(owner)` assertion; double-exec
safety comment at the import site; noting the deliberate assert-vs-skip
divergence). ALL FOUR LANDED in **7d08c5d** (branch is now
3fe6b37 -> 9c539d3 -> 7d08c5d; hermetic 1264/1, collect 19, ruff+budgets
green). The item-2 fix went further than asked, deliberately: the process-tree
snapshot feeding the kill net was taken BEFORE the 15s visibility poll, so
late-appearing renderers were outside the net — the fix re-snapshots via a new
`_chrome_tree_pids(root_pid)` helper and kills/asserts against the union of
both snapshots. Delta verified by rev-t8tests: APPROVE. One follow-up comment
correction landed as **08002f3** — FINAL branch tip, closed for merge
(3fe6b37 -> 9c539d3 -> 7d08c5d -> 08002f3; comment-only last commit,
collect 19 + ruff clean; the SECTION_TOOLS==94 measurement is now IN the
comment itself). Items Task 8 proper must carry:

1. **PR-body sentence**: the regate wiring pin
   (`test_the_marker_is_wired_to_the_gate`) is non-vacuous only where old and
   new gate conditions disagree — i.e. the WINDOWS cells. A green ubuntu/macOS
   unit cell proves nothing about the wiring; only Windows cells do.
2. **Confirm `record_property` lands in junit.xml on the first Windows CI
   integration cell** — the reviewer could not verify it locally (skipped the
   integration lane at 2557 processes; agent-fleets rule).
3. **Post-merge caveat**: once Task 10 is merged, branch A of the integration
   twin (like the whole integration lane) writes the real
   `~/.stealth-mcp/browser_pids.json`. Do NOT carry the "~/.stealth-mcp
   untouched" line from the earlier handoff verbatim into the PR body.
4. **On the merged branch re-run**: hermetic lane (expect ~1287+4 gate rows;
   re-measure, don't inherit), `tools/check_file_budgets.py`, and re-check the
   semantic coupling `process_cleanup.browser_processes[iid]["pid"]` (verified
   intact at 22529ad; re-verify only if 10a-10d get amended).
5. **Branch A remains unexercised** until the user's desktop run:
   `uv run python -m pytest tests/test_browser_integration.py -k
   HeadedSpawnIsSeenOrRefused -m integration` from a normal desktop terminal
   (NOT an agent shell — those are Session 0 on this machine).

Delta review of 7d08c5d: APPROVE (re-measured independently). Extra facts:

6. **PR-body citation for the double-exec safety**: after both exec_module
   passes over embedded/server.py, `sum(len(v) for v in
   SECTION_TOOLS.values())` == 94, not 188 — measured proof the per-section
   idempotent registration cannot accumulate (the defense against the runpy
   3x94 incident). One comment-sentence fix routed to impl-t8tests (the
   "no second load at all" claim was wrong — the second exec is
   unconditional in every lane); final branch tip = that fixup commit.
7. **Scope of the twin's kill net**: `contextlib.suppress(Exception)` around
   the close deliberately does NOT swallow BaseException — an
   asyncio.CancelledError during close still skips the net. Right call
   (swallowing cancellation is worse than the leak); the net is "survives a
   failing close", not "survives anything".
8. **Known residual race in branch A** (accepted, sub-millisecond): a pid
   that appeared during the poll, owned the window, then died before the
   post-poll snapshot would be in neither snapshot and fail `owner in tree`
   spuriously. If branch A ever flakes on the user's desktop, look here
   first — do not loosen the assert back to pid_exists.

