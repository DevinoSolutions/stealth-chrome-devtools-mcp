# F-921 — `kill-orphans --force` ends logged-in browsers and warns nobody

**Severity**: HIGH — the last open item in the master-profile audit, the family
behind the owner's "the master profile was erased / I have to set up the
credentials again" complaint. Re-entering a login costs manual 2FA/CAPTCHA work
no agent may automate.

**Status**: FIXED on `fix/F921-kill-orphans-force-warns-nobody`.

---

## 1. The mechanism

`kill-orphans` is the ops CLI's trigger for the orphan reaper. Without `--force`
it is safe by construction: `process_cleanup._recover_orphaned_processes` asks
`browser_reattach.adoptable_for(...).spare` first, and F-888's rule puts every
browser on a PERSISTENT profile in that set, so a human's logged-in Chrome is
left running and re-attached to later.

`--force` deletes that set:

```python
# src/stealth_chrome_devtools_mcp/embedded/process_cleanup.py:594-598
spare = (
    set()
    if force
    else browser_reattach.adoptable_for(self, saved_processes).spare
)
```

and then skips the per-entry ownership check too (`:615`), so **every** recorded
browser is reaped — including the ones whose whole contract is that their
directory, and the logins in it, outlive the browser. The `cli.py` docstring
said exactly that, in the source:

```python
# src/stealth_chrome_devtools_mcp/cli.py:699-707 (at 1e14006; :704-706 at a3d22b3)
    F-888's persistent-profile spare. That last one is why `--force` is the only
    verb left that can still end a human's logged-in browser: every other path
    now re-attaches to it instead.
```

The defect is that this sentence reached no operator. The `--help` text was:

```
  --force     override the live-backend guard and reap anyway
```

— `src/stealth_chrome_devtools_mcp/cli.py:861-865`, measured by printing
`build_parser()`'s `kill-orphans` subparser on 2026-09-21. Every word of it is
true and none of it is the harm. There was no pre-flight summary, no count, and
no dry run: the one flag in the tree that can destroy a logged-in profile read,
at the terminal, like an ordinary `--force`.

The gap is not cosmetic, because the two halves of the surface disagreed about
what the flag was FOR. `--help` framed it as a backend-guard override — the
thing the refusal message above it also names ("use restart to recover it, or
pass --force") — so the documented reason to reach for `--force` is a wedged
BACKEND, and the undocumented cost is a human's BROWSER.

## 2. Evidence — measured on this machine, 2026-09-21

Read-only, through the product's own readers
(`browser_pid_registry.read_entries` + `on_persistent_profile`, and
`clone_storage._profile_hold`). `kill-orphans` itself was never run.

```
record exists: True
entries recorded: 6
persistent (non-clone) entries: 3
  session: master              | pid: 148628 | cdp_port: 38080 | owner_pid: 47424
  session: nvidia-nim-signup   | pid: 171548 | cdp_port: 33346 | owner_pid: 124132
  session: master              | pid: 59672  | cdp_port: 65353 | owner_pid: 124132
auto-clone entries: 3
```

and the hold witness for the same six:

```
master                             persistent=True  hold=Hold(pid=33840, ...)
unotes-3e20cd061a41                persistent=False hold=None
openrouterfree-bd2402c3ad2b-47424-9 persistent=False hold=None
nvidia-nim-signup                  persistent=True  hold=Hold(pid=25628, ...)
master                             persistent=True  hold=Hold(pid=33840, ...)
amind-125477d2c47c                 persistent=False hold=Hold(pid=10996, ...)
```

So **at the moment of measurement, `kill-orphans --force` would have ended two
persistent profiles' browsers — `master` and `nvidia-nim-signup` — both of them
open, and `master` is the owner's logged-in shared session.** The word "master"
appeared nowhere in the verb's output or help.

Note the two `master` ENTRIES resolve to one DIRECTORY (one holder, pid 33840).
That is why the count below is by directory: the reap is directory-matched
(`process_cleanup._kill_processes_for_metadata:325` kills every browser on the
entry's `user_data_dir`), so two entries on one profile end one profile, and a
count of entries would have said "3" about two.

### Before

```
usage: stealthy kill-orphans [-h] [--force]

options:
  -h, --help  show this help message and exit
  --force     override the live-backend guard and reap anyway
```

```
$ stealthy kill-orphans --force
orphan recovery triggered: reaped any browsers left over from a dead backend.
```

### After

```
usage: stealthy kill-orphans [-h] [--force] [--dry-run]

options:
  -h, --help  show this help message and exit
  --force     override the live-backend guard AND F-888's persistent-profile
              spare: this can terminate a browser holding a logged-in profile,
              whose logins must then be re-entered by hand. Preview it with
              --dry-run
  --dry-run   print what would be reaped, then exit without killing anything
```

```
$ stealthy kill-orphans --force
profiles    : 2 persistent profile(s) tracked, 2 open now (master, nvidia-nim-signup)
              each keeps logins that must be re-entered BY HAND; --force ENDS these browsers
orphan recovery triggered: reaped any browsers left over from a dead backend.
```

```
$ stealthy kill-orphans          # no --force: F-888 spares them
profiles    : 2 persistent profile(s) tracked, 2 open now (master, nvidia-nim-signup)
              each keeps logins that must be re-entered BY HAND; only --force ends them
```

## 3. The fix

ONE home: `cli.py`'s `kill-orphans` parser and body. Two halves.

**The help text tells the truth.** `--force` now names both things it overrides
— the live-backend guard and F-888's persistent-profile spare — and states the
cost in the operator's vocabulary ("logins must then be re-entered by hand")
rather than the codebase's.

**A pre-flight line, printed before anything is killed.**
`_persistent_profile_preflight(force)` returns two lines: how many persistent
profiles are tracked, how many are OPEN right now, which ones those are, and
whether this invocation ends them. It is printed after the live-backend refusal
(so a run that does nothing does not warn about nothing) and before
`recover_orphans`, which is the only statement in the body that can kill.

**`--dry-run`** prints that pre-flight and returns 0 without calling the reaper,
so the set can be inspected. `--force --dry-run` is the preview of a forced
reap; `--force` is still required to get past the live-backend guard, because
that guard's meaning is unchanged by this finding.

Every reader is an existing one:

* **persistent vs clone** — `browser_pid_registry.on_persistent_profile`, which
  is THE one home for that question (F-888) and is literally the predicate
  `--force` skips. Using `clone_storage.clone_is_auto` instead would have been a
  second answer, read off the on-disk MARKER rather than the RECORD, and able to
  disagree with the reap it is warning about.
* **held or not** — `clone_storage._profile_hold`, the established two-line
  adapter over `profile_lock.profile_hold`, and never a presence test: F-871
  exists because `Path.exists()` over `Singleton*` read a reaped browser's
  residue as "busy" and could not see a dangling symlink at all.
* **the record** — `browser_pid_registry.read_entries`, read ONCE, through
  `process_cleanup.process_cleanup.pid_file` (the singleton's own handle, so a
  test that redirects it wins). No second record read and no probe pass of its
  own: the verb's single `singleton._probe_backend_status()` call is untouched,
  and `_survey_records` / `backend_liveness.survey` (F-880) are not consulted
  because they are about `server.json`, not `browser_pids.json`.

No message names a path. Counts, session names (directory basenames, exactly
what the `profiles` verb already prints) and nothing else — a path names the
operating user (F-869/F-877 discipline).

## 4. Blast radius

* `cli.py` only, +51 lines; **999 LOC against the 1000-LOC default budget** (was
  952). No cap was padded, and the file is not grandfathered. The helper's
  docstring is deliberately compact and this finding carries the long argument —
  that is the trade the remaining 48 lines allowed, and it is stated here rather
  than hidden.
* `--help` is a CLI surface. **No golden covers it.** Checked:
  `tests/goldens/` holds seven files and the only CLI-adjacent one is
  `tool_surface.json`, which is the MCP TOOL surface (`dump_tool_surface.py
  --check` reports *"tool surface IDENTICAL to the golden"* after this change);
  `tests/test_doc_claims.py::TestDocumentedCliVerbs` asserts verb NAMES and
  parser/dispatch agreement, not help strings; `tests/test_stealthy_cli.py` has
  no help assertion. So no golden moved, and none was bent.
* `tests/test_doc_examples.py` screens doc fences: `kill-orphans` is already in
  `DENIED_SUBCOMMANDS` and `--force` in `DENIED_FLAGS`, so `--dry-run` cannot
  smuggle the verb into a doc lane. `--dry-run` is deliberately NOT added to
  `INTERACTIVE_FLAGS` — it waits for nobody.
* Behaviour of the reap itself is unchanged: `recover_orphans(force=...)` is
  called with the same argument at the same point. The only new exit is the dry
  run's 0.
* One extra `browser_pids.json` read and one `profile_hold` per distinct
  persistent directory per invocation — for a human-typed ops verb, on a record
  that held six entries on the machine measured above.

## 5. Tests

`tests/test_cli.py::TestKillOrphansForceWarning`, nine nodes, hermetic: a fake
`browser_pids.json` under `tmp_path` bound through
`process_cleanup.process_cleanup.pid_file`, `tmp_session_root` setting
`STEALTH_MCP_BROWSER_SESSION_ROOT` EXPLICITLY (a `patch.dict`, not conftest's
`os.environ.setdefault` fence, which has already lost once), the process scan
stubbed to `()` so no real Chrome on the machine can answer, and
`recover_orphans` patched so nothing can be killed. `tests/fakes.py`'s
`held_profile` writes the singleton that makes one directory read as held.

**RED census on the pre-fix `cli.py`** (`git show HEAD:...cli.py` swapped in,
the new tests run, the file restored): **8 failed, 1 passed.**

| node | RED because |
|---|---|
| `test_force_help_names_the_logged_in_browser_risk` | help said "override the live-backend guard and reap anyway" |
| `test_dry_run_parses_and_defaults_false` | `error: unrecognized arguments: --dry-run` |
| `test_preflight_counts_persistent_profiles_before_the_reaper_runs` | nothing was printed before the reaper |
| `test_auto_clones_are_not_counted_as_persistent` | no count line at all |
| `test_two_entries_on_one_profile_count_once` | no count line at all |
| `test_a_tracked_profile_nothing_holds_is_counted_but_not_open` | no count line at all |
| `test_dry_run_prints_the_set_and_reaps_nothing` | the flag did not exist |
| `test_without_force_the_line_says_persistent_profiles_are_spared` | no count line at all |
| `test_preflight_names_no_path` | **green by construction** — an invariant pin, not a RED; it guards the fix against introducing a path |

The ordering node makes its assertion from INSIDE the patched reaper's
`side_effect`, so "printed before anything dies" is checked at the one moment it
means something rather than after the fact.

The held/not-held pair is what pins F-871's rule: the same directory, tracked
and EXISTING both times, is named in "open now" only when a singleton naming a
live pid is present, and is counted-but-unnamed when it is absent.

**Green**: `tests/test_cli.py` 33/33; and 719 passed across `test_cli*.py`,
`test_stealthy_cli*.py`, `test_process_cleanup*.py`,
`test_backend_liveness_probe.py`, `test_browser_reattach.py`,
`test_browser_pid_registry.py`, `test_doc_claims.py`, `test_doc_examples.py`
and every other importer of `cli` (`-m "not integration"`).

**Gates**: `ruff format --check`, `ruff check`, `ty check
--exit-zero-on-warning src/`, `vulture`, `check_suppression_owners.py`,
`check_file_budgets.py`, `check_pinned_imports.py`, `dump_tool_surface.py
--check` — all pass.

## 6. Residuals, and the two decisions that need defending

**6.1 — Why a printed line and not a prompt.** The obvious design for "about to
destroy a login" is a `y/N` confirmation. It is rejected, and not for
convenience. This CLI is driven by agents as well as humans: `stealthy` is the
verb surface the fleet in this repo uses, and a blocking `input()` on a non-tty
does not fail — it reads EOF or blocks, and an agent that cannot answer hangs
until something times out. A prompt would therefore convert a loud, visible
destruction into a silent stall, which is a worse failure of the same kind. And
the consent argument is already settled: `--force` is an explicit opt-in the
operator had to type, and `backend_eviction` makes exactly this argument for its
own ungated act ("an operator asking IS the authority the rule otherwise
supplies"). What was missing was never the consent. It was the DISCLOSURE —
the operator did not know what they were consenting to. A line plus `--dry-run`
delivers the disclosure without inventing a second, blocking authority.

**6.2 — The count is of what is RECORDED, not of what will die.** The reap kills
by directory, and a directory can hold a browser we never recorded (an operator's
own Chrome, started by hand on the same profile). Such a browser dies and is not
in the count. The count is bounded below, not exactly: "2 open now" is a floor.
Going further would mean a second process scan per directory looking for
unrecorded Chromes, which is `profile_lock`'s question asked a different way and
would put a second answer beside it.

**6.3 — A profile "tracked but not open" is still counted.** Its browser is
already gone, so `--force` ends nothing there. It is counted deliberately: the
tracked count is what `--force`'s scope IS, and the "open now" number beside it
is what it costs right now. Reporting only the open ones would hide a record
that is about to be swept.

**6.4 — `--dry-run` does not bypass the live-backend refusal.** Against a
responsive or wedged backend, plain `kill-orphans --dry-run` still refuses and
prints nothing, because the guard's meaning ("use restart") is not this
finding's to redefine. `--force --dry-run` is the inspection path and it works
in every backend state. The cost is one surprising combination; the benefit is
that the guard keeps exactly one meaning.

**6.5 — The helper docstring is compact by budget.** `cli.py` stood at 952 of
the 1000-LOC default. The change fits at 999 with the long-form argument here
rather than in the source. If `cli.py` needs another verb-level feature, the
next change should extract this question to a leaf (the `backend_liveness` /
`profile_lock` pattern) rather than pad the cap — caps ratchet DOWN only.

**6.6 — Not fixed here.** The audit's §5 siblings are untouched: F-916
(recovery reaps an entry it merely could not classify), F-917 (reap
directory-matched vs spare instance-id-matched), F-918 (an unverifiable pid is
terminated), F-919 (`spawn_leak`'s one-second fence). Each of those kills
without any operator typing anything, so a warning on `kill-orphans` does not
reach them — this finding closes the surface where a human DOES ask, and the
others close the paths where nobody did.
