# F-921 — `kill-orphans --force` ends logged-in browsers and warns nobody

**Severity**: HIGH — the last open item in the master-profile audit, the family
behind the owner's "the master profile was erased / I have to set up the
credentials again" complaint. Re-entering a login costs manual 2FA/CAPTCHA work
no agent may automate.

**Status**: FIXED on `fix/F921-kill-orphans-force-warns-nobody`; revised
after the first Opus review returned CHANGES REQUIRED
(§3.1, §6.2, §6.4, §6.6).

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
  --dry-run   print the persistent profiles at risk, then exit without reaping
```

```
$ stealthy kill-orphans --force
profiles    : 2 persistent profile(s) in the record, 2 open now (master, nvidia-nim-signup)
              --force ends EVERY tracked browser; these keep logins that must be re-entered BY HAND
orphan recovery triggered: reaped any browsers left over from a dead backend.
```

```
$ stealthy kill-orphans          # no --force: F-888 spares them
profiles    : 2 persistent profile(s) in the record, 2 open now (master, nvidia-nim-signup)
              only --force ends them; these keep logins that must be re-entered BY HAND
```

Two more shapes, both added at review (§6.2, §6.4). Nothing recorded:

```
profiles    : none tracked on a persistent profile
```

and profiles recorded but none of them open:

```
profiles    : 2 persistent profile(s) in the record, none open — this reap ends no logged-in browser
```

## 3. The fix

Two homes, split at review: `cli.py`'s `kill-orphans` parser and body keep the
flag surface and the PHRASING, and a new leaf owns the ANSWER (§3.1).

**The help text tells the truth.** `--force` now names both things it overrides
— the live-backend guard and F-888's persistent-profile spare — and states the
cost in the operator's vocabulary ("logins must then be re-entered by hand")
rather than the codebase's.

**A pre-flight line, printed before anything is killed.**
`_persistent_profile_preflight(force)` returns ONE line in the two shapes that
have nothing to warn about, and TWO when something is open: how many persistent
profiles the record names, how many are OPEN right now, which ones those are,
and whether this invocation ends them. It is printed after the live-backend
refusal (so a run that does nothing does not warn about nothing) and before
`recover_orphans`, which is the only statement in the body that can kill.

**`--dry-run`** prints that pre-flight and returns 0 without calling the reaper,
so the set can be inspected. `--force --dry-run` is the preview of a forced
reap; `--force` is still required to get past the live-backend guard, because
that guard's meaning is unchanged by this finding.

Its own help says what it actually does — *"print the persistent profiles at
risk, then exit without reaping"*. The first revision said *"print what would be
reaped"*, which over-claims in the very direction this finding is about: the
flag prints the persistent pre-flight and nothing else, and on the machine
measured in §2 `--force` reaps **six** recorded entries while the pre-flight
names **two** directories. A preview advertised as the whole set, which is a
subset of it, is under-disclosure one flag along.

### 3.1 The extraction — `embedded/persistent_profile_risk.py`

The first revision put the counting in `cli.py` and landed the file at 999 of
the 1000-LOC default. Review ordered the extraction, and the decisive argument
was not the number: **the budget was already deciding the code.** The
reviewer's own two-line fix for an empty record (§6.2) takes the file to 1001,
so the correct behaviour was literally unwritable in that file. Supporting:
`cli.py` is touched by 49 of the last 60 commits on `origin/main`, went 839 →
956 → 981 → 890 → 952 in two days with a near-miss at 981, and F-916/F-917/
F-918/F-919 are in flight on the same reap subsystem. The repo's own precedent
is `dom_handler.py` at **997** producing `script_evaluation`; 999 is past it.

`persistent_profile_risk.assess(entries, *, is_open)` returns
`AtRisk(tracked, open_names)` — the persistent DIRECTORIES the record names,
and the basenames of the ones a browser holds. It is a leaf on
`backend_liveness`'s pattern: the entries arrive as a value and the hold
predicate as an ARGUMENT, so it imports `browser_pid_registry` and stdlib and
nothing else (pinned by an AST check over its own imports). `cli.py` keeps the
three printed shapes.

Deliberately not `process_cleanup` (grandfathered at 1009, may not grow) and
deliberately not `browser_reattach`: its `Classified.spare` answers "what would
recovery protect", while `--force` skips the classification entirely, so the
at-risk set is EVERY persistent entry and not just the unadoptable ones.

**The self-criticism this earns.** The first revision's §6.5 said "the next
change should extract this" — a rule written where the person who needs it will
not read it, which is F-921's own defect one level up: the danger was recorded
in a docstring instead of at the surface where it would be acted on. The
argument now lives in the extracted module's own docstring, beside the code.

The honest number: `cli.py` is **998**, not the ~975 the review estimated. The
counting moved out, but the two shapes §6.2 and §6.4 add back most of what it
freed. What the extraction actually bought is the testable home — the counts
now have eight direct pins that build no parser and capture no stdout — and a
fresh 1000-line budget for the next change to this question.

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

* `cli.py` **998 LOC** against the 1000-LOC default (was 952), plus the new
  99-line `embedded/persistent_profile_risk.py`. No cap was padded and neither
  file is grandfathered. Measured with `splitlines()`, per
  `tools/check_file_budgets.py:215`'s own rule.
* `--help` is a CLI surface. **No golden covers it**, re-verified at this
  revision. `tests/goldens/` holds seven files; six are cloner payloads and the
  seventh is `tool_surface.json`, the MCP TOOL surface. Its ONE hit for
  `kill-orphans` is inside `spawn_browser`'s DESCRIPTION, not a help string, and
  `dump_tool_surface.py --check` reports *"tool surface IDENTICAL to the
  golden"* after this change. No test in the tree asserts on `format_help` or
  `print_help` (grepped); the two help pins here read `action.help` directly.
  `tests/test_doc_claims.py::TestDocumentedCliVerbs` asserts verb NAMES and
  parser/dispatch agreement, not help strings; `tests/test_stealthy_cli.py` has
  no help assertion. So no golden moved, and none was bent.
* That golden hit is worth one sentence, because it sits exactly on F-921's
  confusion: `spawn_browser` tells callers a named session "is never deleted by
  … `kill-orphans`", and that remains TRUE — the reap kills PROCESSES and
  deletes no directory. What it never said is that the browser holding the
  profile can still be ended, which is this finding. The two claims are about
  different objects and both now have a surface that says which.
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

Two files since the extraction. **`tests/test_persistent_profile_risk.py`**,
eight nodes, PURE — entries are dicts fed through
`browser_pid_registry.normalize_entries` (so the shape is the one `read_entries`
actually produces, never a hand-made one), the hold predicate is injected,
nothing is read from disk and nothing is probed. It pins the empty record, the
auto-clone exclusion, the by-directory de-duplication, sorted basenames with no
path, that the answer follows the injected predicate and nothing else (both
directories EXIST, so a presence test would answer the same either way — F-871),
a legacy bare-pid entry naming no directory, and the leaf property itself as an
AST check over the module's own imports.

**`tests/test_cli.py::TestKillOrphansForceWarning`**, twelve nodes, hermetic: a fake
`browser_pids.json` under `tmp_path` bound through
`process_cleanup.process_cleanup.pid_file`, `tmp_session_root` setting
`STEALTH_MCP_BROWSER_SESSION_ROOT` EXPLICITLY (a `patch.dict`, not conftest's
`os.environ.setdefault` fence, which has already lost once), the process scan
stubbed to `()` so no real Chrome on the machine can answer, and
`recover_orphans` patched so nothing can be killed. `tests/fakes.py`'s
`held_profile` writes the singleton that makes one directory read as held.

**Why the process-scan stub lands at all**, which is not obvious and is one
import away from silently stopping: `clone_storage.py:41` imports the process
cleanup **SINGLETON** (`from ...process_cleanup import process_cleanup`), not
the module, so `clone_storage._profile_hold`'s
`getattr(process_cleanup, "_get_browser_pids_for_profile", None)` resolves on
the very object the suite's `monkeypatch.setattr` targets. Re-point that import
at the MODULE and the stub stops reaching `_profile_hold`: the real machine's
process table answers, every node here becomes machine-dependent, and they all
stay green on a quiet machine. The note is in `_bind`'s docstring too, where
the next person will meet it.

Both censuses below were re-measured against the CURRENT test file, with the
historical `cli.py` restored from its own commit and the working copy put back
byte-for-byte (sha256 checked; `git restore` deliberately unused — under
autocrlf it rewrites line endings and would change the file it is meant to
return).

**RED census on the pre-fix `cli.py`** (`a3d22b3`, the parent of this branch's
first commit): **12 failed, 0 passed** — every node in the class.

| node | RED because |
|---|---|
| `test_force_help_names_the_logged_in_browser_risk` | help said "override the live-backend guard and reap anyway" |
| `test_dry_run_help_claims_only_what_the_flag_prints` | the flag did not exist |
| `test_dry_run_parses_and_defaults_false` | `error: unrecognized arguments: --dry-run` |
| `test_dry_run_prints_the_set_and_reaps_nothing` | the flag did not exist |
| `test_preflight_counts_persistent_profiles_before_the_reaper_runs` | nothing was printed before the reaper |
| `test_preflight_names_no_path` | nothing was printed at all, so there was no name for the path rule to be about (§6.6 — it took a held profile to get here) |
| `test_auto_clones_are_not_counted_as_persistent` | no count line at all |
| `test_two_entries_on_one_profile_count_once` | no count line at all |
| `test_a_tracked_profile_nothing_holds_is_counted_but_not_open` | no count line at all |
| `test_an_empty_record_warns_about_nothing` | no count line at all |
| `test_the_line_names_the_reaps_whole_scope_not_just_the_persistent_set` | no count line at all |
| `test_without_force_the_line_says_persistent_profiles_are_spared` | no count line at all |

**Second RED census, against the committed first revision** (`f35e2ca`), for
the behaviour the review round ordered: **5 failed, 7 passed** — the seven that
pass are the ones the first revision already got right, which is what makes
this census about the review's items and not a re-run of the one above.

| node | RED because |
|---|---|
| `test_an_empty_record_warns_about_nothing` | printed "0 persistent profile(s) tracked" and then the BY HAND harm sentence |
| `test_a_tracked_profile_nothing_holds_is_counted_but_not_open` | same harm sentence with nothing open to end |
| `test_auto_clones_are_not_counted_as_persistent` | the zero case had no wording of its own |
| `test_the_line_names_the_reaps_whole_scope_not_just_the_persistent_set` | "--force ENDS these browsers" named only the persistent set, implying the clones were safe and the toll exact |
| `test_dry_run_help_claims_only_what_the_flag_prints` | the help said "print what would be reaped" about a flag that prints the persistent pre-flight only |

The ordering node makes its assertion from INSIDE the patched reaper's
`side_effect`, so "printed before anything dies" is checked at the one moment it
means something rather than after the fact.

The held/not-held pair is what pins F-871's rule: the same directory, tracked
and EXISTING both times, is named in "open now" only when a singleton naming a
live pid is present, and is counted-but-unnamed when it is absent.

**Mutation proof of the two no-path pins.** `path.name` -> `str(path)` in
`persistent_profile_risk.assess` — the one expression standing between the
printed line and an absolute path — turns BOTH
`test_cli.py::…::test_preflight_names_no_path` and
`test_persistent_profile_risk.py::…::test_open_names_are_sorted_basenames_only`
RED, and restoring turns them green, with the file's sha256 unchanged across
the round trip. Two hazards had to be handled for that to mean anything, and
both bit on the first attempt:

* Both spellings are **9 bytes**, so CPython's default mtime+size `.pyc`
  validation cannot see the edit. Every `__pycache__` under `src/` is removed
  before each run and the removal is asserted, or the stale bytecode runs and
  the mutant "passes".
* At the first attempt the CLI pin **survived the mutation** — it was still
  vacuous, for a second reason nobody had looked for. The record normalizes
  `user_data_dir` through `browser_pid_registry.normalize_path`, which is
  `os.path.normcase(os.path.normpath(...))`, and on Windows **normcase
  lowercases**. So the string a leak would print is the lowercased path, and a
  literal `str(logged_in) not in out` can never match it on this platform. The
  assertion is now made under the record's own normalization, which is what
  made the mutation land.

**Green**: `tests/test_cli.py` + `tests/test_persistent_profile_risk.py`
**44/44**, and the WHOLE non-integration suite at this revision —
**3824 passed, 1 skipped, 290 deselected**
(`-m "not integration"`, 482.88 s, exit 0). The whole suite rather than a named
subset because this revision edits a doc fence as well as code, and
`test_doc_examples.py` screens those: a subset chosen by the author of the
change is a subset that can miss the file the change broke.

Measured at `7da54a5`, which is this branch AFTER the 2.1.13 release merge
(`fe5cef6`) and after the five queued lanes ahead of it — F-919, F-916/F-917/
F-918 with F-922, F-913, and F-914/F-915 — not before either. A suite count
taken before a merge describes a revision nobody will ever run.

The count was PREDICTED before the lane ran, from this tree's own
`--collect-only`: **3825 selected** (3824 passed + 1 skipped;
selected is passed plus skipped, and conflating them reads a correct forecast as
a miss). It is the lane ahead's 3805 plus exactly this finding's 20
nodes — 8 in `tests/test_persistent_profile_risk.py` and 12 in the F-921 class
of `tests/test_cli.py` — so a green lane also confirms the merge delivered the
tests it claimed to rather than merely passing the ones that survived.

The merges also re-checked the claims these artefacts MAKE — every product
symbol named by the module docstring, §3.1 and the two CLAUDE.md rows still
resolves, `process_cleanup.py:349-355` is still the recorded-pid fallback §6.7
cites, and `clone_storage` still imports the process-cleanup SINGLETON, which is
the one import §5 says the hermeticity of every `test_cli.py` node here depends
on.

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

**6.2 — The zero case used to warn about nothing (review blocking 2).** The
second printed line was unconditional, so an empty record produced "0 persistent
profile(s) tracked" followed by "each keeps logins that must be re-entered BY
HAND". A warning printed when there is nothing to warn about is how a warning
stops being read — the same failure mode as the silence this finding fixes, with
the sign reversed. There are now three shapes, and the harm sentence appears
only when something is actually open.

**6.3 — The count is the RECORD's scope, and it is a FLOOR.** A directory the
record calls DISPOSABLE has every browser on it ended — including one a human
started there by hand, which is absent from this count because the record
classifies by how WE made a profile, not by what is inside it. The printed line
no longer implies otherwise: it says "in the record" and, under `--force`, "ends
EVERY tracked browser", so the operator learns the reap is wider than the named
set rather than inferring a death toll from it.

**F-922 has since LANDED** (owner ruling, assigned to the F-916 lane; it is an
ancestor of this branch and `process_cleanup._kill_processes_for_metadata` now
gates its directory scan on `on_persistent_profile`), which narrows the gap
rather than closing it: a PERSISTENT profile's reap ends only the pids the
record names, while a disposable clone directory is still swept directory-wide.
So the floor survives on the disposable side, and the wording above is what is
true after F-922 as well as before it.

**6.4 — "Tracked but none open" gets its own wording, deliberately.** With
profiles in the record and nothing holding any of them, this invocation ends no
logged-in browser, so the line reads "2 persistent profile(s) in the record,
none open — this reap ends no logged-in browser" and the harm sentence is
withheld. The alternative — reusing the general shape with "0 open now" — was
rejected for the §6.2 reason: it names a harm this run cannot cause. The count
is still printed rather than suppressed, because the record naming two profiles
is a fact about the reap's scope that survives them being closed, and a silent
line would read as "nothing here" about a record that is about to be swept.
Residual: `_profile_hold` can answer None for a directory whose holder Chrome is
mid-launch, so "none open" is a snapshot, not a lease.

**6.5 — `--dry-run` does not bypass the live-backend refusal.** Against a
responsive or wedged backend, plain `kill-orphans --dry-run` still refuses and
prints nothing, because the guard's meaning ("use restart") is not this
finding's to redefine. `--force --dry-run` is the inspection path and it works
in every backend state. The cost is one surprising combination; the benefit is
that the guard keeps exactly one meaning.

**6.6 — A pin that is green in a RED census is not thereby an invariant
(review should-fix 3).** `test_preflight_names_no_path` shipped at `f35e2ca`
as the last row of a table of eight genuine REDs, labelled *"green by
construction — an invariant pin"*, and it guarded nothing: it built the profile
WITHOUT `fakes.held_profile`, so nothing was open, the naming branch never ran,
`shown` was `""` and `assert str(logged_in) not in out` had no name to bite on.
The census it sat in is what made it look safe — everything around it had been
shown to fail, so it inherited that credibility without earning any of it.
**A pin that is green in a RED census is not thereby an invariant — it is a pin
nobody has shown can fail.**

Two things were needed and the first alone was not enough. Holding the profile
makes the naming branch run, which is what turns the node into a genuine RED
against `a3d22b3` — the first census is 12 of 12 now, with no "green by
construction" row left in it. But held and otherwise unchanged it STILL
survived the mutation, because the comparison was made against the raw path
while the record normcases (§5) — so the second reading is that a mutation is
the only thing that settles the question, and reading the node will not. The
rule generalises past this file: a node that is green at the moment it is
written owes a mutation, and the mutation belongs in the finding beside the
census.

**6.7 — An entry the pre-flight skips can still be killed.**
`persistent_profile_risk.assess` keys on `user_data_dir` and skips an entry
whose value is absent or not a `str` (`isinstance(recorded, str)`) — it has no
directory to count, and no name to print. The REAP does not skip it. Under
`--force` the classification and `is_reapable` are both bypassed
(`process_cleanup.py:594-618`), so the entry reaches
`_kill_processes_for_metadata`, whose cmdline scan finds nothing without a
directory and falls through to the RECORDED PID at
`process_cleanup.py:349-355`. That fallback is not itself force-gated — it
still requires `_fallback_pid_identity_ok` and a process predating backend init
— so the honest statement is that such an entry is *considered* by a reap the
pre-flight said nothing about, and dies when those two guards pass.

Producing one needs a hand-edited or partially-written `browser_pids.json`;
`browser_pid_registry.new_entry` cannot. But a pre-flight exists precisely for
the record that is not what we expect, so it is named rather than assumed away.
The direction is the safe one — the line under-counts, and already says the
reap is wider than the set it names (§6.3). Fixing it properly means the
pre-flight iterating the REAP's own per-entry decision rather than the record's
directories. That is F-922's shape and not this finding's — but F-922 has landed
and did NOT do it: it narrowed which pids a reap may take, leaving this
pre-flight still reading the record's directories, so the residual stands.

**6.8 — The count is a snapshot, not a lease.** The pre-flight reads the record
at T0; `recover_orphans` re-reads it at T1, after the line is printed. A browser
started, closed or recorded in between is counted wrongly, and no lock is taken
across the window because holding the record's lock through a printed line would
block every spawn on a human reading their terminal. Both directions are
possible and only one is dangerous, so the printed wording is already the
conservative one: "more may die than counted" is what "in the record" and "ends
EVERY tracked browser" say. Same class as §6.4's `_profile_hold` snapshot.

**6.9 — `cli.py` is still close to its budget, and the next extraction is a
different one.** At 998 of 1000 the headroom is two lines. The counting has left
the file; what remains near the cap is the `_doctor_*` / `_format_backend_status`
/ `_other_records_note` block, which is a separate question with its own
reasoning and is outside this finding's brief. Recording it HERE rather than in
a docstring is the §3.1 lesson applied: a rule is only useful where the next
person will meet it.

**6.10 — Not fixed here.** The audit's §5 siblings are untouched: F-916
(recovery reaps an entry it merely could not classify), F-917 (reap
directory-matched vs spare instance-id-matched), F-918 (an unverifiable pid is
terminated), F-919 (`spawn_leak`'s one-second fence). Each of those kills
without any operator typing anything, so a warning on `kill-orphans` does not
reach them — this finding closes the surface where a human DOES ask, and the
others close the paths where nobody did.
