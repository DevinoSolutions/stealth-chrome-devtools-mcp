# F-896 — a session has a name, and the name is never "master"

**Status:** fixed on `feat/F896-session-vocabulary`.
**Phase:** 2 of the session-UX plan (option A — vocabulary, zero mechanism change),
after F-892/F-893/F-894/F-895 shipped in 2.1.11.
**Owner decision it implements:** the four recommended defaults of
`design_session_ux.md` §5 (Q1–Q4), accepted 2026-09-20.

---

## 1. The finding

The owner's question was *"could the snapshot trick be improved so that a user of
the session management system wouldn't really need to understand the master
concept?"* The design study's answer is that the concept cannot be removed while
the product's own words are the mechanism's: a caller opened a profile with
`user_data_dir`, got back `profile_role: "master"`, `clone_source:
"master-snapshot"`, `snapshot_error: "master-in-use"` and `seeded_from:
"master-snapshot"`, and read a tool docstring that said *"clones a disposable
session from the master profile"*. Every one of those is a word about the
implementation, and not one of them is a word the caller can type back.

Three specific harms, all in the same shape:

1. **The documented parameter did not match the documented noun.** `CLAUDE.md`'s
   glossary promised `spawn_browser(session_name=…)`; the real parameter was
   `user_data_dir`, and the docstring said so in capitals ("THIS PARAMETER IS THE
   PERSISTENT-PROFILE OPTION — there is no separate `profile=`"). The glossary
   taught agents a keyword that raised (fixed in F-894; the mismatch it recorded
   is what this finding closes).
2. **`master` was a word with no door.** An unnamed spawn reaches the shared
   profile only while it is FREE (`clone_storage:997`); there was no way to ASK
   for it. `user_data_dir="master"` — the documented name — anchored under the
   clone root and silently opened `sessions/master`, a different profile (F-894,
   MEASURED: that directory exists, 0.46 GB).
3. **The answer a caller reads named directories, not sessions.** `seeded_from:
   "master-snapshot"` tells a user their session was copied from a thing they
   cannot open, cannot name and cannot act on.

---

## 2. What changed

### 2.1 One request, two spellings

`spawn_browser` gains `session: str | None`. `user_data_dir` stays as a
deprecated alias and **resolves to the same request** through one function,
`profile_seed.profile_request(session, user_data_dir) -> str | None`, which is
the only place either spelling is read. `clone_storage.require_allowed_user_data_dir`
calls it, so the tool body has one line and everything below — the F-888
re-attach, the resolver, the diagnostics — sees a single value.

Two rules, each because its absence is a silence:

* **Both given with different values RAISES.** A precedence would pick one and
  say nothing, and the caller who typed two profiles cannot tell which they got.
  Both given with the same value is honoured — refusing someone who said one
  thing twice buys nothing.
* **`session` takes a NAME and refuses a path.** "A session named
  `C:\Users\me\profile`" is not a sentence. The refusal names the path door:
  `user_data_dir`, or `stealthy call spawn_browser --arg user_data_dir=<path>`.
  The drive test is `PureWindowsPath`'s, for `reserved_reason`'s measured reason
  (`PurePosixPath("C:foo").drive` is `""`, so reading the host's flavour makes a
  refusal fire on one platform only); both separators are tested literally,
  because a backslash is a separator on Windows and a legal filename character
  on POSIX, and a name that means two things on two platforms is not a name.

### 2.2 `default` is a session you can open

F-894 reserved `default` as a refusal and said in its own source comment that it
was reserved *"ahead of the vocabulary that will use it"*. This is that
vocabulary, so the word now MEANS the shared profile. `profile_seed.anchor`
answers the shared directory for the bare name, and the resolver's existing fold
— the one an absolute path to it already went through — gives it the shared
ROLE, which is what makes `close_instance` refresh the seed afterwards.

**Nothing about which directory a spawn selects moved.** An unnamed spawn is
byte-identical to 2.1.11: shared profile when free, disposable copy when held
(F-834 stage 1's fallback is untouched, only its role WORD changed). What
`session="default"` adds is a door onto a path that already existed.

It stays reserved in the sense that matters. `master` and `master-snapshot` are
refused outright; `default` names exactly one directory, so any RELATIVE
spelling that would CREATE a second one under that word (`sessions/default`) is
refused. Without that clause F-894's trap would have come back one separator
away from the word the vocabulary teaches.

Two boundaries on that clause, both from review round 1 and both pinned:

* **It is gated on the request being relative**, exactly as the reserved-name
  refusal beside it is. `./default` normalises to the bare name and reaches the
  shared profile — it creates nothing, so there is nothing to refuse — and an
  ABSOLUTE path whose basename is `default` is the caller's own directory
  (Chrome's per-profile folder is literally `Default`). Refusing those was a new
  refusal of an input 2.1.11 honoured, offering an escape that named a different
  profile, and it made RUNBOOK's recovery paragraph false as written (M2).
* **A name the filesystem FOLDS onto one of those words is refused** (S1).
  Windows strips a trailing dot or space from a path component, so
  `session="default."` reached `sessions/default` — a second directory under the
  word, through the one door this clause exists to close, one character away
  (measured; `Path.resolve(strict=False)` leaves the dot on a path that does not
  exist yet, so `same_dir` cannot catch it either). The fold is applied on every
  platform, because a name that means one directory here and another there is
  not a name.

### 2.3 The words, retired

| was | is |
|---|---|
| `profile_role: "master"` | `profile_role: "default"` |
| `snapshot_dir` / `snapshot_refreshed` / `snapshot_reason` / `snapshot_error` | `seed_dir` / `seed_refreshed` / `seed_reason` / `seed_error` |
| `master_snapshot_path` | `seed_path` |
| `"master-in-use"` / `"snapshot-in-use"` | `"default-in-use"` / `"seed-in-use"` |
| `"master-snapshot"` / `"live-master-fallback"` (`clone_source`) | `"default-seed"` / `"live-default-fallback"` |
| `"explicit-master-snapshot"` / `"explicit-master"` | `"explicit-default-seed"` / `"explicit-default"` |
| `"master-snapshot-final"` / `"-retry"` | `"default-seed-final"` / `"default-seed-retry"` |
| `"before-master-open"` / `"after-master-close"` | `"before-default-open"` / `"after-default-close"` |
| `seed_name()` → `"master-snapshot"` or `"master"` | → `"default"`, for both |
| `profiles` roles `master` / `snapshot` | `default` / `default-seed` |
| `ToolError` prefix `user_data_dir rejected:` | `profile request rejected:` |

`seed_name` collapsing two answers into one is a deliberate loss and it is the
right one: the shared profile and its seed are ONE session to a user, and which
of the two directories a copy was physically taken from is a mechanism detail
that exists only because a live profile cannot always be copied. Staleness does
not go with it — `provenance` reads `seed_changed_since` off the marker's
recorded `source` PATH, so the two stay distinguishable exactly where the
distinction is load-bearing.

**The old keys are NOT kept readable beside the new ones.** `spawn_diagnostics`
is rebuilt on every spawn, is never persisted, and has no cross-version
consumer; every reader in the tree is in this PR. A duplicate key would be two
spellings of one fact bought for nothing, which is convention 4 exactly.

### 2.4 The CLI

`stealthy spawn --session NAME`. `--profile` is accepted for one release,
`argparse.SUPPRESS`ed so `--help` does not list it, and prints one stderr line
naming `--session` and `stealthy call`. Both flags at once are passed on and the
BACKEND refuses, so the conflict rule keeps one home.

This deliberately overrules F-891 §6, which promised `--profile` would stay as
the escape hatch. The escape hatch already exists and is the CLI's whole design
(`stealthy call` reaches any tool argument); a second sugar flag for one tool
argument is the defect convention 4 names; and the two would diverge the moment
`--session` reserves names and records provenance while `--profile` creates an
unmanaged directory.

### 2.5 Two payments, and where they came from

Both files touched were at their caps and **caps ratchet down only**.

* `clone_storage.py` (grandfathered 1054, ended at 1054): `_snapshot_needs_refresh`'s
  body moved to `profile_seed.needs_refresh` — every term in it was already that
  module's (the witness list, the marker, what counts as a login) — leaving a
  one-line binding to OUR two directories. And `resolve_profile_selection` now
  calls `require_allowed_user_data_dir` instead of re-composing
  `profile_seed.require_allowed` itself, so "where does this request land" has
  ONE answer rather than two agreeing ones.
* `cli_call.py` (1000, ended at 1000): the seven-line `**There is no --master**`
  paragraph is deleted, because the flag it argued against is no longer the
  question — `--session` ships.

`profile_seed.require_allowed` grew past ruff's `PLR0913` at six parameters,
which is what produced `profile_seed.Roots`: the four directories always travel
together and mean nothing apart, so one tuple is the shape, and `clone_storage`
stays the only thing that knows where they are.

---

## 3. Pins

`tests/test_session_vocabulary.py`, **49 nodes**. They arrived in three batches
and each was RED first. The first batch at `b620ee3` was 31 of them: **30
failed, 1 passed** before the fix, each for its own reason —
`require_allowed_user_data_dir() takes 1 positional argument but 2 were given`
(the parameter did not exist), `spawn_browser.description says 'master'`,
`{} == {'session': 'acme'}` (the CLI sent nothing), `--profile`'s help was not
`SUPPRESS`, `StopIteration` (no `--session` action), `KeyError: 'session'`.

The one that passed is the parser-tree sweep: the CLI's help strings were
already clean, so that node is a guard against the next flag rather than a
change driver, and it is stated as one.

The second batch is the three tool-level nodes at `780f6f0` (§3, last bullet).
The third is review round 1's fourteen, of which **8 failed, 6 passed**, for
three reasons: the two absolute-path nodes raised `profile request rejected:
'default' is the shared session …` about a directory outside the tree; the five
fold nodes did not raise at all; and the whitespace node put ` acme ` in a
directory name. The six that passed on arrival are guards, and are stated as
such: four spellings that normalise to the bare `default` (the behaviour a
docstring had claimed was refused — S3) and two flavour assertions.

* **One request** — a name resolves identically through either spelling; both
  given and different raises; both given and equal is honoured; neither is no
  request.
* **A session is a NAME** — eight path shapes refused, parametrized; the
  drive refusal asserted under BOTH `PurePath` flavours; and a path is still
  reachable through the alias, which is what makes this a narrowing of one
  spelling rather than a loss of reach.
* **`default`** — selects the shared profile; is the same answer as naming no
  session; creates no `sessions/default`; `sessions/default` is refused, under
  BOTH `PurePath` flavours, because what gates that refusal is
  `Path.is_absolute()`; four spellings that normalise to the bare word
  (`./default`, `default/`, `Default`, `DEFAULT`) reach the shared profile and
  make nothing; an ABSOLUTE path ending in `default` is the caller's own, both
  outside the tree and as the pre-F-896 `sessions/default` RUNBOOK promises is
  still openable; five names the filesystem would fold onto a reserved word are
  refused; whitespace around a name is not part of it, through either spelling;
  `master`/`master-snapshot` stay refused; `default` is out of `RESERVED_NAMES`.
* **No user-facing string says the old words**, and the string set is DERIVED,
  never listed: the live tool registry (every description and every parameter
  description, off `dump_tool_surface._surface()`), the live argparse tree
  (walked recursively, `SUPPRESS`ed actions skipped), and a REAL
  `profile_selection` payload for each of the three roles — `default`,
  `explicit` and `clone`, asserted to be three and not two, because the sweep
  first shipped with `default` in it twice and so never read `clone_source`,
  the one key whose values this PR renames (review S2) — produced by the
  resolver. Plus two scoped nodes: the reserved-name refusal is checked with the
  caller's own echoed word removed, because quoting what you typed is not the
  product teaching a vocabulary; and `seeded_from` is checked WITHOUT the
  path exemption and asserted to be a name you can pass back to `session=`.
* **The CLI** — `--session` sends `session`; `--profile` still works and says it
  is deprecated; `--profile` is `SUPPRESS`ed and `--session` is not; both flags
  are passed through for the backend to refuse.

### Goldens moved

`tests/goldens/tool_surface.json` — **HARD**, moved deliberately in this PR with
justification per `CONTRIBUTING.md`. 13 insertions, 1 deletion, all inside
`spawn_browser`: the `description` (the new `session` parameter documented, the
`user_data_dir` entry rewritten as the deprecated alias) and one new
`session` property in `input_schema.properties`. No other tool's bytes moved,
which is the thing the golden exists to prove about a change like this.

No SOFT golden file moved. The diagnostics-string rename lands as Q4 directs —
in this PR — but it is pinned by assertions in test files rather than by a
golden artifact, so what moved is **seven** test files' literals, each one an
assertion about a value this PR renames (`test_browser_integration`,
`test_concurrent_spawn_collision`, `test_correlation_id`,
`test_extra_headers_cdp`, `test_profile_lock`, `test_profile_resolution`,
`test_profile_seed_truth`). The pin file itself and the HARD golden are
counted separately above, and the integration tier's own sweep is its own
commit — nine and six were two ways of miscounting that set.

---

## 4. What did NOT change

* **Which directory an unnamed spawn selects.** Unchanged, and pinned by
  `test_concurrent_spawn_collision.py`'s 3-of-3 node.
* **The held-shared-profile → disposable-copy fallback** (F-834 stage 1).
  Unchanged; only the role word it reads moved.
* **The directories on disk.** Still `master` and `master-snapshot`.
* **The env vars.** Still `BROWSER_MASTER_USER_DATA_DIR` /
  `BROWSER_MASTER_SNAPSHOT_DIR`. `Settings` is `extra="forbid"`, so renaming
  them either breaks every existing `.env` or needs an alias — a second spelling
  of one knob, which is the defect this finding is about.
* **Clone markers already on disk.** A marker written by 2.1.11 carries
  `source_kind: "explicit-master-snapshot"`; `is_auto`/`is_named` read the
  `explicit` prefix, which both old and new values keep, so an existing named
  profile is still read as named. Pinned by leaving the legacy literals in the
  fixtures of `test_clone_storage_cap.py`, `test_clone_legacy_marker_classification.py`
  and `test_profile_trim.py` exactly as they were.

---

## 5. One behaviour change beyond vocabulary, named rather than hidden

`require_allowed_user_data_dir` now ANSWERS the anchored directory, and
`spawn_browser` binds that answer back onto `user_data_dir` before the F-888
re-attach. This is required — `session="default"` must reach the re-attach as
the shared DIRECTORY, or `adopt_held_profile` matches the literal name
`default`, finds nothing holding it, and the spawn falls through to a fresh
copy: the exact silent substitution this vocabulary exists to end.

It has a side effect on the alias path that is a **bug fix**, and it should be
read as one rather than as scope creep. Before this change a BARE name reached
`adopt_held_profile` unanchored, so `Path("acme").resolve()` was
`<cwd>/acme` — a directory that does not exist — and the re-attach could never
fire for a session named by name, although `spawn_browser`'s own docstring
promises it does ("spawning with a user_data_dir a live browser still holds
re-attaches to THAT browser"). Only an absolute path worked. Now both do.

**A tilde path gains the same thing, for the same reason** (review N4): `anchor`
calls `.expanduser()`, so `~/profiles/acme` now reaches `adopt_held_profile` as
the directory it means, where before `Path("~/x").resolve()` was `<cwd>/~/x` on
Windows — a directory nothing holds. Same class of fix, same one line.

---

## 6. Residuals

1. **An operator can still SEE "master" in a path.** `user_data_dir` and
   `seed_dir` carry `<root>/master` and `<root>/master-snapshot`, and the pin
   exempts absolute-path values for that reason, stated in the pin's own
   docstring. What an operator can no longer be TOLD is a role, a reason, an
   error or a seed named after the mechanism. Moving the directories would
   migrate every existing installation's profiles for a word nobody types;
   if it is ever done it belongs in a release with a migration step, not here.
2. **No explicit `ephemeral` door.** The study's Q1 suggests one for "give me a
   disposable copy even though `default` is free". It is NOT added: today that
   is expressible only as a silence (spawn unnamed and hope `default` is busy),
   so adding it would be a new capability rather than a second spelling — which
   makes it a design question with its own trade-off (a caller who asks for
   disposable and gets a copy of a seed two days stale is a different
   conversation), not a vocabulary one. Named here as the follow-up it is.
3. **`--profile` is still accepted.** One release, undocumented, warning on
   stderr. Its removal is the next release's job and is stated in the CHANGELOG
   rather than left to be discovered.
4. **`user_data_dir` is still the only path door.** That is deliberate (§2.1),
   but it means the deprecated parameter cannot be removed until something else
   takes a path — or until the position is taken that `stealthy call` and the
   raw tool argument are enough, which is what F-891's `call` was built for.
5. **F-897 will change what `seeded_from` can say.** Today it is `default` or a
   directory name; with `--from <session>` it becomes any session's name, which
   is why `seed_name`'s fallback is `source.name` and why the pin asserts the
   value is something `session=` accepts.
6. **A login still does not propagate between existing sessions.** Unchanged and
   deliberate — the study's option D (write-back by default) silently merges
   identities. F-897 (`--from`) and F-898 (C2 cookie hand-off) are where that is
   addressed, and neither is in this PR.
