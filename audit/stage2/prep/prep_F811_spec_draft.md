# prep: F-811 implementation spec (draft, targeted at 2.0.5)

Read-only spec pass, 2026-08-03, measured against **main @ `5f387c1`**. Builds on
`prep_F811_exhaustion_diagnostics.md` (the evidence trail — Sentry issue
`STEALTH-CHROME-DEVTOOLS-MCP-K`, 82 events / 7 days, 68% from the maintainer's
machines). Everything numeric below was re-measured at HEAD; where it contradicts
the scoping doc, the number here is the measured one and the disagreement is called
out.

---

## 1. Defect statement

When Chrome cannot launch because the machine has run out of process capacity, the
tool surfaces nodriver's raw connect failure —

```
ToolError: Failed to spawn browser: --- Failed to connect to browser ---
```

— with **zero** indication that the machine is drowning in browser processes. This
is a product defect, not just hygiene: the CLI already ships the remedies
(`kill-orphans`, `cleanup`), `browser_pid_registry` already knows which browsers are
tracked, and psutil is already a dependency. Everything needed to make the error
actionable is present and simply not consulted. The user (usually an agent) reads an
opaque nodriver string and retries, which makes the exhaustion worse.

### User-visible contract after the fix

When `spawn_browser` fails **and** the machine shows an exhaustion signal, the
`ToolError` the caller receives keeps its current text and gains a trailing
diagnostic that names:

1. **the measured signals** — the live Chromium-family process count, and how many
   browsers are tracked in the shared `browser_pids.json` record;
2. **the exact remedy commands**, correct as typed:
   `stealth-chrome-devtools kill-orphans --force`, then
   `stealth-chrome-devtools cleanup --apply`;
3. **the honest limit** — untracked processes are not ours to reap.

Below the threshold the error is byte-identical to today's. Nothing is ever killed,
throttled, or retried differently; the change is purely additive text on a path that
has already failed.

> **`--force` is load-bearing, not decoration.** `cli.py:485` refuses `kill-orphans`
> outright while a backend is responsive or wedged, printing "a backend is running
> … use restart to recover it, or pass --force" and returning 1. A `spawn_browser`
> failure is by definition raised *by a live backend*, so a diagnostic that says
> plain `kill-orphans` points the user at a command that is **guaranteed to refuse**.
> The remedy string must carry `--force`, and the message must say why.

> **`cleanup` is a disk verb, not a process verb.** `cli.py:309 _cmd_cleanup`
> enforces the clone/browser-session storage caps; it kills nothing. It belongs in
> the message as the *second* step (reclaim the profile directories the reaped
> browsers left behind), and the wording must not imply it frees processes.

---

## 2. The exact sites

Grepped for the message string and every raise on the spawn path. Per the repo's
error-sweep lesson (a census scoped to except-blocks misses if-guards, and a missed
site is the classic failure), this enumerates **every** raise between `uc.start` and
the caller — including the ones that need no change, with the reason.

| # | Site | What it is | Change |
|---|---|---|---|
| **S1** | `embedded/browser_manager.py:769` — `raise Exception(f"Failed to spawn browser: {e!s}")  # noqa: B904  plan_M4ph1` | The terminal wrap of the whole spawn pipeline, in `spawn_browser`'s `except Exception as e:` (opens :738). Every failure from `_resolve_proxy` / `_resolve_launch_args` / `_launch_browser` / `_apply_post_launch` / `_setup_dynamic_hooks` funnels here. | **THE call site.** Append the hint. |
| **S2** | `embedded/browser_manager.py:550` — `return await uc.start(config=config)` in `_launch_browser` | Where nodriver's connect failure is actually born. Deliberately minimal (its docstring says so) so the orchestrator owns teardown. | **None.** Decorating here would duplicate S1 and split the diagnostic from the cleanup path. |
| **S3** | `embedded/browser_manager.py:659` — `raise Exception("Browser process exited immediately after launch")  # noqa: TRY301` | Inside the same `try`. Under exhaustion Chrome frequently *does* launch and die instantly, so this is an exhaustion-adjacent failure mode. | **None needed** — it flows through S1 and is decorated for free. This is a reason to decorate S1 rather than the `uc.start` await. |
| **S4** | `embedded/browser_manager.py:700` — `except asyncio.CancelledError: … raise` | Cancellation, not failure. | **None, and must stay undecorated.** A cancelled spawn is not evidence of exhaustion. |
| **S5** | `embedded/server.py:435-447` — the 3-attempt retry loop's `except Exception as spawn_error:` → bare `raise` at :446 | Re-raises S1's already-decorated message when no profile fallback exists (the common case). | **None.** Inherits. |
| **S6** | `embedded/server.py:449` — `raise Exception("; ".join(spawn_errors))` | All three attempts exhausted; joins the per-attempt messages, each already decorated. | **None.** Inherits. |
| **S7** | `embedded/server.py:479` — `raise ToolError(f"Failed to spawn browser: {e!s}")` | **The user-visible raise.** Converts to `ToolError` per convention 2. | **None.** Inherits S1's text verbatim. |
| **S8** | `embedded/server.py:391` — the F-808 headed-visibility `ToolError` | Fires *before* the try, outside the wrap, and is a different diagnosis. `tests/test_spawn_headed_requires_display.py:105` pins that it does **not** start with "Failed to spawn browser". | **None, and must not regress.** Re-run that file. |

**Why S1 and not S7.** S7 is where the `ToolError` the user sees is raised, and
decorating there would guarantee exactly one occurrence. It is rejected for two
reasons. First, semantics: S7's `except` also catches profile resolution, network
interception setup and diagnostics assembly — attributing "your machine is out of
process capacity" to a `clone_storage` failure is a false diagnosis. S1's blast
radius is the launch pipeline, which is the thing exhaustion actually breaks.
Second, budget: `server.py` is at **3401/3401** with zero headroom, and a cap raise
is a human-gate item (the 2026-07-12 `plan_M4ph1` C1 ruling). S1 needs no gate at
all (§6).

**Accepted consequence — up to 3 repetitions.** Because the hint is appended per
attempt at S1, a spawn that exhausts all three retries produces a joined message
(S6) carrying the hint up to three times. In practice `_fallback_profile_selection`
returns `None` on the first failure whenever no fallback profile exists, so the
common case is one. This is cosmetic, the counts may legitimately differ between
attempts, and de-duplicating it means editing `server.py` at cap. Documented, not
fixed.

**Convention note.** S1 raises a plain `Exception`, not a `ToolError` — correct and
unchanged. `browser_manager` is not a tool module; S7 is the one place the error
convention's `ToolError` is minted. Do not "fix" S1 to raise `ToolError` in this
change: it would alter what `server.py`'s retry loop catches and classifies.

---

## 3. The helper: home, name, API

### Home: a new leaf, `src/stealth_chrome_devtools_mcp/embedded/spawn_exhaustion.py`

Two candidates were considered; one is rejected on its own stated charter.

**Rejected — `browser_pid_registry.py`.** Its module docstring closes with "A leaf
module: stdlib plus `debug_logger`. Never `process_cleanup`, never `singleton`,
never `server`." The helper's whole job is to ask the **operating system** how many
browser processes exist, which needs `psutil` — a dependency that module has
deliberately never taken. Worse, the question is a category mismatch: the registry's
charter is "which browsers are tracked and by whom" (CLAUDE.md glossary), and
"how many browser processes does the OS see" is precisely the *other* number. Adding
it there would make the module's own docstring false. The helper *reads* the
registry (via the existing public `read_entries`) rather than living inside it.

**Rejected — `process_cleanup.py`.** At its exact 1023 cap; F-809/F-810 discipline
says leave it alone. Only its existing public surface is used.

**Chosen — a new leaf.** Its charter, to go in the module docstring: *THE one home
for "is this machine out of browser-process capacity, and what should the operator
do about it".* Imports `psutil`, `debug_logger`, and `browser_pid_registry` only —
never `process_cleanup`, never `singleton`, never `server` (convention 1).

> **Naming: do NOT call it `spawn_diagnostics.py`.** `browser_manager.spawn_browser`
> binds a **local variable** named `spawn_diagnostics` at :661, inside the very
> `try` whose `except` (S1, :769) is the call site. A module imported under that
> name would be shadowed at the call site by a local that is *unbound* on most
> failure paths — a `NameError`/`UnboundLocalError` raised from inside the error
> handler, i.e. the diagnostic destroying the error it decorates. `server.py` has a
> local of the same name at :456. `spawn_exhaustion` collides with nothing in either
> file.

### API — one public function

```python
def exhaustion_hint(pid_file: Path) -> str | None:
```

- **Returns `None`** below the threshold, or on any internal failure. The call site
  is therefore one line plus an `or ""`.
- **Returns a string that already carries its own separator** (leading `"\n\n"`) so
  the call site is a bare concatenation with no formatting logic.
- **`pid_file` is a required positional parameter with no default.** This is the
  house rule that `tests/fakes.py:61 assert_no_default_paths` enforces on the
  on-disk record modules, for the reason stated there: the caller's binding is what
  selects the file, and a defaulted path would bind a module global at def-time and
  silently ignore the tests' redirection — the only thing keeping a test run out of
  the developer's live `~/.stealth-mcp`. Reuse that assertion here (§5, T14).
- **It must never raise.** The whole body sits under one `try` /
  `except Exception as error:` that logs and returns `None`. A diagnostic that
  breaks the error it decorates is strictly worse than no diagnostic.
  - Use **`debug_logger.log_debug`**, not `log_error`. `log_error` ships to Sentry
    (`observability.py`), and a helper that fires only when the machine is already
    unhealthy would add volume to exactly the issue we are trying to close. The
    handler binds and references `error`, so `tests/test_no_silent_excepts.py`'s
    classifier sees a logging marker and a referenced binding — **no ALLOWLIST entry
    is needed, and none may be added.**

### What it measures

**Signal (gates the hint): live Chromium-family process count, machine-wide.**

```python
for proc in psutil.process_iter(["name"]):   # name ONLY — never cmdline
    if "chrom" in (proc.info.get("name") or "").lower():
        count += 1
```

- `process_iter(["name"])` only. Reading `cmdline` for the whole process table is
  the dominant cost on Windows (one PEB read per process) — the same reason
  `process_cleanup._get_active_browser_profile_dirs` (:218-221) documents pulling
  cmdline lazily. This helper never needs cmdline at all.
- The single substring **`"chrom"`** covers every platform's spelling in one test:
  Windows `chrome.exe` / `chrome_proxy.exe`, Linux `chrome` / `chromium` /
  `chromium-browser` / `chrome_crashpad_handler`, macOS `Google Chrome` /
  `Google Chrome Helper (Renderer)`. `tests/release_gate_harness.py:426` already
  uses exactly this idiom.
- **Deliberately narrower than `process_cleanup._is_browser_process_name`**, which
  also matches `msedge`/`edge`/`brave`. That predicate answers a *different*
  question — "am I allowed to kill this pid" — where breadth is a safety property.
  This one answers "how much of what WE spawn is running", and counting a user's
  Edge or Brave windows toward a hint that says "run kill-orphans" would be a false
  direction, since we never launch them. Different question, different predicate:
  this is not a second way to do one thing (convention 4), and the docstring says so
  explicitly so a future reader does not "unify" them.
- **Per-process errors are skipped, not fatal:** `except (psutil.NoSuchProcess,
  psutil.AccessDenied, psutil.ZombieProcess): continue` inside the loop. On Linux
  `/proc` entries vanish mid-iteration; on macOS other users' processes are
  AccessDenied. Both are normal, neither is a reason to lose the count.

**Context (reported, never gates): tracked-browser count.**
`len(browser_pid_registry.read_entries(pid_file))` — the public reader, which by its
own contract never raises and returns `{}` for an absent or unparseable record.

> **Why the tracked-vs-visible delta does NOT gate the hint.** The scoping doc and
> the task brief both floated `os-visible − tracked >= 50`. Measured against the
> false-positive requirement, that signal points the wrong way: a normal user's 40
> real tabs are *also* untracked, so a heavy human browsing session produces a large
> delta with zero orphans, which is precisely the "must NOT be told to kill-orphans"
> case. The delta is genuinely useful *information* — it is what tells the operator
> whether `kill-orphans` will help at all — so it is **printed**, and the absolute
> count is what **decides**.

### Threshold

```python
_EXHAUSTION_PROCESS_THRESHOLD = 120
```

Chrome is process-per-renderer plus a fixed retinue (browser, GPU, network utility,
storage, crashpad), so one browsing session with 40 real tabs lands around 50-70
processes, and one of our instances costs 5-12. **120 requires roughly double a
heavy human browsing session**, which is the false-positive margin the brief asks
for. The observed exhaustion event measured **204** (scoping doc §Evidence), so the
true-positive margin is comfortable too. 100 was considered and rejected as sitting
close enough to a heavy human session to invite the exact complaint named in the
brief.

The asymmetry of costs supports erring high-ish but not paranoid: a false positive
appends one extra paragraph to an error the user is **already reading and already
has to act on** — it never fires on a healthy machine, because it only runs after a
spawn has failed. A false negative restores today's opaque error.

A module constant, **not** a `STEALTH_MCP_*` setting: unknown env keys crash
`get_settings()`, and the house rule is universal defaults over config knobs.

### Message shape

```
\n\nSpawn diagnostics: 214 Chromium-family processes are live on this machine and
12 browser(s) are tracked in the shared record. This machine has most likely run out
of process capacity, which is the usual cause of a failed browser launch (F-811).
Reap the tracked browsers whose backend is gone:
  stealth-chrome-devtools kill-orphans --force
(--force is required because THIS backend is alive; without it the command refuses.)
Then reclaim the profile directories they left behind:
  stealth-chrome-devtools cleanup --apply
Processes not in the tracked count are not ours to reap — close them or reboot.
```

Both measured numbers, both remedy verbs, the `--force` reason, and the honest
limit. `F-811` is in the text for greppability, matching the F-808 message's
precedent (`test_spawn_headed_requires_display.py:102` pins that convention).

### Gating on the machine, not on the error text

The hint fires on the **signal alone**; it does not match nodriver's
"Failed to connect to browser" string. Matching an upstream library's message is the
brittleness the repo has been bitten by before, and it would silently stop firing on
a nodriver upgrade — a diagnostic that fails open into silence. And the framing
holds: on a machine with 214 live Chromium processes, *any* spawn-phase failure is
plausibly caused by exhaustion, and the message says "most likely", not "is".

---

## 4. Windows and POSIX

| Concern | Windows | POSIX (Linux / macOS) |
|---|---|---|
| Process names | `chrome.exe`, `chrome_proxy.exe` | `chrome`, `chromium`, `chromium-browser`, `chrome_crashpad_handler`, `Google Chrome Helper (Renderer)` |
| Matcher | one `"chrom" in name.lower()` test covers all of the above — no per-platform branch, no `sys.platform` check in this module | same |
| `process_iter` cost | one `NtQuerySystemInformation` snapshot for `name`; no per-process PEB read because cmdline is never requested | one `/proc` (or `sysctl`) walk |
| Per-process failure | rare `AccessDenied` on protected processes | `/proc` entry vanishing mid-walk (`NoSuchProcess`); other users' processes `AccessDenied` on macOS |
| Degradation | any of the above → skip that process; `process_iter` itself failing → whole helper returns `None` | same |

Nothing platform-conditional is needed, and nothing platform-conditional may be
added — the CI lesson that headed/geometry premises belong on Windows/macOS cells
does not apply here, because this helper reads only the process table, which every
lane has.

---

## 5. Tests — `tests/test_spawn_exhaustion_hint.py` (new)

Conventions, per `tests/fakes.py`'s header and the repo's hermetic rules:

- **No test spawns real Chrome.** No test writes to the real `~/.stealth-mcp`:
  `pid_file` is always under `tmp_path`.
- **`psutil.process_iter` is monkeypatched in every test but one** (T15, which is
  the deliberate real-contract check). The double is built to psutil's actual
  shape — objects exposing `.info` as a dict — because a hand-written double that
  copies our own assumption can keep a permanent defect green.
- The `FakeProc` / `fake_process_iter` helper stays **module-local for now**, with a
  comment naming the rule-of-three trigger for promoting it into `fakes.py`. This
  mirrors `browser_pid_registry._write`'s own recorded reasoning ("two occurrences
  is not yet a home"). If a second module needs process doubles, it moves to
  `fakes.py` — that file is THE harness home and a second hand-rolled copy would be
  the defect.

### Helper-level

| # | Test | Asserts |
|---|---|---|
| T1 | `test_below_threshold_returns_none` | `THRESHOLD - 1` chrome-named procs → `None` |
| T2 | `test_at_threshold_returns_a_hint` | exactly `THRESHOLD` procs → a `str` |
| T3 | `test_threshold_constant_stays_in_a_sane_band` | `50 <= _EXHAUSTION_PROCESS_THRESHOLD <= 500`. T1/T2 probe *relative* to the constant so retuning needs no golden update; this is what stops a retune to `5` or `100000` from sailing through green. |
| T4 | `test_non_browser_processes_are_not_counted` | 500 `python.exe` / `node.exe` + 3 chrome → `None` |
| T5 | `test_every_platform_spelling_is_counted` | `chrome.exe`, `chromium-browser`, `Google Chrome Helper (Renderer)`, `chrome_crashpad_handler` all count — Windows/Linux/macOS parity in one test, so a lane that never sees the other spellings still pins them |
| T6 | `test_process_iter_raising_degrades_to_none` | parametrized over `RuntimeError`, `psutil.Error`, and an iterator that raises *mid-iteration* → `None` every time, nothing propagates |
| T7 | `test_per_process_access_denied_is_skipped_not_fatal` | one proc whose `.info` raises `psutil.AccessDenied` among `THRESHOLD` healthy ones → still returns a hint (the loop skipped, not aborted) |
| T8 | `test_missing_record_still_produces_a_hint` | `pid_file` points at a nonexistent path under `tmp_path` → tracked count reads `0`, hint still returned |
| T9 | `test_message_names_both_measured_signals` | the live count and the tracked count both appear as digits in the message; seed the record with a known number of entries so the tracked figure is asserted against a real value, not against `0` |
| T10 | `test_message_names_both_remedy_verbs_and_force` | contains `kill-orphans`, `--force`, `cleanup`, and `F-811` |
| T11 | `test_the_hint_carries_its_own_separator` | starts with `"\n\n"` — the call site does no formatting, so this is where that contract lives |

### Call-site wiring

| # | Test | Asserts |
|---|---|---|
| T12 | `test_the_hint_lands_in_the_raised_error_when_the_seam_reads_high` | monkeypatch `spawn_exhaustion.exhaustion_hint` to return a sentinel, make `_launch_browser` raise, call `BrowserManager.spawn_browser` → the raised message starts with `Failed to spawn browser:` **and** contains the sentinel. This is the wiring proof: the seam is stubbed, not the process table. |
| T13 | `test_no_hint_when_the_seam_returns_none` | same wiring, seam returns `None` → the message is **exactly** today's text, `f"Failed to spawn browser: {inner}"`, with no trailing artifact. Pins the `or ""` and makes the below-threshold path a positive assertion rather than an absence. |
| T14 | `fakes.assert_no_default_paths(spawn_exhaustion)` | the `pid_file` parameter can never acquire a default — the structural guarantee that a future edit cannot route a test at the developer's live record |

### Real-contract check

| # | Test | Asserts |
|---|---|---|
| T15 | `test_psutil_process_iter_still_yields_info_dicts` | the ONE test that calls real `psutil.process_iter(["name"])`: take the first item, assert `.info` is a mapping containing `"name"`. Read-only, spawns nothing, costs milliseconds. Without it, a psutil API change would leave T1-T13 green over a helper that counts nothing — the "mocked fakes can encode the bug" failure mode. |

### Existing pins to re-run

- `tests/test_spawn_headed_requires_display.py` — S8 must keep NOT starting with
  "Failed to spawn browser" (:105). Nothing in this change touches it; a failure
  there means the edit leaked out of the `except` block.
- The **full** unit lane (`uv run python -m pytest`), never a narrow selector — a
  narrow selector loses Chrome warmup and fails to launch at all. Clean worktrees
  need `uv sync --extra test --extra dev` first.

---

## 6. LOC budget — measured, and it contradicts the brief

`python tools/check_file_budgets.py` at `5f387c1`:

| File | actual | cap | headroom |
|---|---|---|---|
| `embedded/browser_manager.py` | **1526** | 1532 | **+6** |
| `embedded/server.py` | 3401 | 3401 | 0 |
| `embedded/process_cleanup.py` | 1023 | 1023 | 0 |

> **Premise correction.** Both the scoping doc and the task brief state that
> `browser_manager.py` is "at its exact 1532 cap". It is not — it is at **1526**,
> with six lines of headroom. The 1532 entry was set by `plan_M4ph1` C4/M13 (the
> spawn_browser god-method split); the file has since shrunk. `server.py` and
> `process_cleanup.py` *are* exactly at cap, as stated.
>
> **ADDENDUM 2026-08-03 (post F-810 merge, main @ `32df5ca`): the correction above
> is itself stale.** PR #59 (F-810) landed after this spec was measured and its
> `browser_manager.py` changes consumed the six spare lines — the file is back at
> **exactly 1532/1532** (verified by `tools/check_file_budgets.py` at `32df5ca`).
> The "+2 net, pay nothing" plan below no longer holds: the implementer must find a
> **net-2 offset inside `browser_manager.py`** in the same commit (per the C1
> discipline: real collapsible lines, never padding, never a cap raise without the
> human gate). §8 open question 1 is therefore withdrawn — there is no free headroom
> to confirm. The rest of the accounting (leaf module under ordinary budget, tests
> uncapped, `server.py`/`process_cleanup.py` untouched) is unaffected. Re-measure at
> HEAD before implementing; F-809's merge will not change this file, but re-measure
> anyway.

### Accounting

**`embedded/browser_manager.py`: +2 lines net.**

1. **The import: +0 lines.** Line 16 already reads
   `from stealth_chrome_devtools_mcp.embedded import window_sizing`. Adding to that
   existing statement gives
   `from stealth_chrome_devtools_mcp.embedded import spawn_exhaustion, window_sizing`
   — 80 characters, inside the 88 limit, alphabetically ordered, and what
   `ruff format` + isort produce anyway. No new line.
2. **The call site: +2 lines.** At S1 (:768-769), one existing line becomes three:

   ```python
               instance.state = BrowserState.ERROR
               hint = spawn_exhaustion.exhaustion_hint(process_cleanup.pid_file) or ""
               message = f"Failed to spawn browser: {e!s}{hint}"
               raise Exception(message)  # noqa: B904  plan_M4ph1
   ```

   Longest of the three is 83 characters — clean under `line-length = 88`.

   > **Do not try to save the line by inlining.** The existing `raise` at :769 is
   > *already exactly 88 characters* (the `# noqa: B904  plan_M4ph1` trailer eats
   > most of the budget). Interpolating the hint into it directly gives 100
   > characters, and `E` is in ruff's `select` list, so that is an **E501 violation**
   > — `ruff format` would then split the call across three lines anyway, for the
   > same cost and a worse read. Absorbing the `or ""` into the `hint` local and
   > naming the message is the shape that stays clean. (`TRY003` is in ruff's
   > `ignore` list, so the named-message form raises no new lint.)

   `process_cleanup` here is the module **singleton** already imported at :31
   (`from …process_cleanup import process_cleanup`), so `.pid_file` is read at call
   time off the live object — which is exactly what makes the tests' `pc.pid_file`
   redirection reach it. Keeping `exhaustion_hint` as a named module attribute call
   (rather than a `from … import exhaustion_hint`) is also what lets the wiring
   tests (T12/T13) stub the seam with `monkeypatch.setattr`.

**Result: 1526 → 1528 of 1532, four lines of headroom left. No offset needed, no
cap change, no human gate.**

**Deliberately paying nothing.** With six verified lines of headroom, hunting for a
line to delete would be churn for its own sake, and the obvious candidate is the
wrong one: `_launch_browser`'s docstring (:536-541) explains *why* the method is
kept minimal — that the orchestrator must capture the browser handle immediately so
a later phase can tear it down. That is the "extract preserves cleanup ownership"
lesson written down at the exact place a future refactor would violate it. Do not
collapse it. **Do not ratchet the cap either way in this change** — the no-grow rule
already binds at 1532, and the C1 discipline is that caps track measured reality at
the moment they are set, not that every PR re-baselines them.

**`embedded/spawn_exhaustion.py` (new): ~100-120 LOC** including its charter
docstring. Ordinary 1000-LOC budget; **no `GRANDFATHER` entry, and none may be
added.**

**`tests/test_spawn_exhaustion_hint.py` (new):** the gate only scans
`src/stealth_chrome_devtools_mcp/`, so test files carry no budget.

**Untouched at cap:** `server.py` (3401), `process_cleanup.py` (1023),
`clone_storage.py` (1057), `cdp_element_cloner.py` (1013).

### Docs in the same PR

CLAUDE.md nav-map row for `spawn_exhaustion.py` under "Browser & interaction";
`DESIGN.md` §10 ledger row for F-811; a CHANGELOG entry under 2.0.5. **Tool count
stays 94** — no `@section_tool` is added or removed.

---

## 7. Out of scope

- **No auto-killing anything.** The hint tells the operator what to run; it never
  reaps, terminates, or throttles. Escalating a diagnostic into an action on a
  machine already in a bad state is how a fratricide bug is born (F-808).
- **No background monitoring**, no periodic sampling, no warning on the *success*
  path. The measurement runs exactly once, on a spawn that has already failed.
- **No changes to `process_cleanup.py`** (at its 1023 cap; F-809/F-810 discipline)
  or to `display_context.py` (observational by ruling). Only existing public
  surfaces are read.
- **No changes to `server.py`** — including the up-to-3 hint repetition (§2), which
  is documented and accepted rather than fixed at cap.
- **No new tool registration.** 94 stays 94.
- **No Sentry changes.** The helper logs at `log_debug`, which does not ship.
- **No new `STEALTH_MCP_*` knob.** The threshold is a module constant.
- **Not fixing the exhaustion itself.** Why parallel agent fleets leave hundreds of
  orphaned Chrome processes, and whether backend cold-start contention (45
  `backend-*.log` files in one day, 9 backends dying at boot inside 26 minutes) is a
  separate defect, are open questions the scoping doc raises and this spec does not
  answer. F-811 makes the symptom legible; it does not make it stop.

---

## 8. Open questions for the human

1. **The budget premise was wrong, in our favour.** `browser_manager.py` is
   1526/1532, not at cap. The spec spends two of the six spare lines and pays
   nothing, leaving four. Confirm that is preferred over manufacturing a net-zero
   offset — recommendation: yes, take the lines, delete nothing.
2. **Threshold 120 vs 100.** This is a judgement call about one specific machine
   class, and the maintainer's own machines produced 68% of the evidence. 120 is
   defended in §3 on false-positive margin; if the real-world Chrome footprint on
   the maintainer's normal (non-fleet) workday is routinely above ~80, say so and it
   should drop to 100.
3. **Should the same signal also surface on a *successful* spawn**, as a
   `spawn_diagnostics["exhaustion"]` field, so an agent sees the machine degrading
   *before* the failure? Genuinely arguable and cheap given the helper exists, but
   it is a different contract (a success-path measurement on every spawn has a cost
   the failure path does not), and it would touch `server.py` at cap. Filed here as
   a candidate follow-up finding, deliberately not specced.
