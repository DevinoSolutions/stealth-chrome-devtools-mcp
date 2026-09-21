# F-897 — a new session can start from an existing one, and never from a live profile

**Status:** implemented on `feat/F897-seed-from-session` (branched from the
F-896 PR tip `bd68d79`).
**Phase:** 3 of the session-UX plan (option B — `--from` generalisation), after
F-892/F-893/F-894/F-895 shipped in 2.1.11 and F-896 (the `session` vocabulary).
**Owner decision it implements:** `design_session_ux.md` §3(B) + §4's phased
plan, on the four recommended defaults of §5 accepted 2026-09-20.

MEASURED = observed on this machine / in this suite. REASONED = derived from
source. Every claim below that a copy loses data is REASONED from
`profile_copy`'s own code and from F-893's measured precondition; none of it is
a fresh measurement of a live Chrome, and §6 says so.

---

## 1. The finding

F-896 gave sessions a name. It did not give a login a way to reach a second
session. The design study's §2.6 states the gap as MEASURED: 27 named profiles
on the owner's machine, all `source_kind: explicit-master-snapshot`, the oldest
frozen since 2026-08-04. Seeding happens once, at creation
(`clone_storage:952-961`, `if not explicit.exists()`), and there is no verb that
asks for another.

So a user who has logged into `work` by hand and wants a second browser with
that login has exactly two options today, and both are wrong:

1. **Spawn `work` again.** Since F-888 that RE-ATTACHES to the running browser —
   the same window, not a second one. Correct behaviour, wrong answer to the
   question.
2. **Log in again in a new session.** Which is the thing the session system
   exists to avoid.

The seam for the fix already existed and had one caller:
`resolve_profile_selection(source_override=…, source_kind=…)`, used only by
`_fallback_profile_selection`. Exposing it is the whole mechanism. The finding
is therefore not "this is broken" but "this is reachable and is not offered",
and the interesting work is in what must be REFUSED rather than in what must be
built.

---

## 2. What changed

### 2.1 The parameter

`spawn_browser` gains `seed_from: str | None = None`; the CLI gains
`stealthy spawn --session NAME --from SOURCE`. When `session` names a session
that does **not exist yet**, it is created as a copy of `seed_from`.

**Unset means `default`, and the two are ONE path.** `profile_source.seed_source`
treats `None` and the bare word `default` identically in its first branch, so
"seeding from the shared session" cannot develop behaviour that differs from
"seeding from nothing named". Pinned
(`test_seed_from_default_is_the_same_as_omitting_it`), because two branches that
agree today are the defect convention 4 names.

### 2.2 It is a NAME, through the gate `session` already passes

`profile_source.seed_request` → `require_name` → (at the copy)
`require_allowed`. No second resolver, no path door.

`require_name` is F-896's name rule with the FIELD as data. The two messages
must name the parameter the caller actually typed — a caller told about
`session` when they wrote `seed_from` goes and edits the wrong argument — but
the ESCAPE differs and cannot come from the field: `session` has a path door
(`user_data_dir`) and `seed_from` has none, because the provenance a seed writes
is a NAME the caller can pass back to `session=`, and an arbitrary directory has
no such word. So each caller hands in its own PATH hint and the rule stays one
function.

That also means `master`, `master-snapshot` and F-896's fold shapes (`default.`,
`master.`) are refused through the seed door exactly as through the session
door, because it is the same `reserved_reason`.

**The empty-request rules came from F-896's own delta and are shared, not
copied** (merge of `500afb6` + `2953e8d`). There are two of them and they do not
overlap:

* **`seed_from=""` is NOT GIVEN** and seeds from `default`, exactly as an unset
  `--from` does. `seed_request` tests falsy rather than `is None`, the same
  decision `profile_request` makes for `session` and for the measured reason:
  an MCP client is a language model and `""` for an optional string is one of
  its commonest shapes. Here it is not merely harmless but right — `""` says
  nothing about where to copy from, and the answer for saying nothing already
  exists.
* **A NON-empty value that is empty once stripped RAISES** — and with the
  *same sentence*, because `require_name`'s empty branch is now
  `_names_nothing(field, value)` rather than a hint this function composes.
  That is the whole shape of the merge: F-896's delta introduced one sentence
  for two spellings of one request, and a third field that takes a name would
  have been a third wording of it. The last clause ("Omit it entirely to use
  the `'default'` session") is true of all three — omitting `session` opens
  the shared session, omitting `seed_from` copies it — so nothing is
  parameterised except the field name, which is the part that tells a caller
  which argument to edit. Only the PATH refusal still takes a hint.

The hazard the second rule exists for cannot be reached through `seed_from` the
way it is reached through `user_data_dir` — a `seed_from` naming nothing is a
source that cannot exist, not a directory that resolves to the session root —
but the refusal is shared anyway, because the alternative is one field quietly
disagreeing with the other two about what a name is, which is the finding
F-896 closed.

`" C:profile"` is refused as a path, because `require_name` strips BEFORE
`is_bare_name` reads the drive. That was already true and is pinned as a guard
rather than claimed as a fix — `reserved_reason` needed its own `.strip()`
added for exactly this shape in `500afb6`, which is what a missing guard here
would eventually cost.

### 2.3 CREATION only — an existing target RAISES

`profile_source.require_new_session` refuses FOUR shapes, which are one sentence
read four ways: there has to be a session (`seed_from` with no `session`), it
has to be the caller's own (`session="default"`), it has to be a session at all
(a `user_data_dir` outside the clone root), and it must not already exist.

**The third was a SILENT DROP and it is the review's M1** (measured, not
reasoned). `resolve_profile_selection` only seeds a directory it is about to
CREATE under the session root — `if not explicit.exists() and
_is_relative_to(explicit, clone_root)` — so for a `user_data_dir` landing
anywhere else the seed request passed every gate and was then never used:

```
=== out-of-root user_data_dir + seed_from=work ===
no refusal raised      : True
dir exists             : False
carries work cookie jar: False
seeded_from reported   : unknown
```

`spawn_browser(user_data_dir="<abs path outside the clone root>",
seed_from="work")` succeeded, the browser got an empty profile, and `--from
work` did nothing. That is this feature's own stated commitment — "`--from` is
never silently dropped" — inverted, reached through the path door one argument
to the left: `session=` cannot produce it (it refuses paths), but the documented
escape `stealthy call spawn_browser --arg user_data_dir=<path>` and the
still-accepted `stealthy spawn --profile` both can. The deprecated spelling
lowers the frequency, not the shape.

It is refused in `require_new_session` rather than in the resolver because the
rule is that function's — *is there a NEW SESSION for this seed to apply to* —
and `clone_storage` supplies the one fact it cannot know (`inside_root`)
exactly as it already supplies `shared`. Making the resolver honour `seed_from`
outside the root was the other option and was rejected: what `--from` copies has
to be a session, because the name it records is a word the caller can pass back
to `session=`, and an arbitrary directory has no such word. `inside_root` is a
REQUIRED keyword, so a second caller must decide it rather than inherit a
default.

The fourth had a real choice in it and the two alternatives were both rejected:

* **A silent no-op** tells a caller their session came from `work` when it came
  from wherever it was seeded weeks ago. That is the class of silence F-894 and
  F-896 exist to close, reached one release later.
* **A re-seed** overwrites a profile whose entire purpose is to hold a login a
  human typed by hand. A flag must not be able to do that by accident.

Refusing is the only answer that is neither a lie nor a loss, and it can afford
to be loud because the remedy is one word: open the session (a spawn without
`seed_from`), or pick a free name. The message NAMES where the existing session
was actually seeded from, so a caller expecting a fresh copy learns what they
have instead.

**It is asked BEFORE the F-888 re-attach, and that placement is load-bearing.**
A session whose browser is still running is a session that EXISTS. Without the
guard in front, `spawn --session work --from other` would be adopted onto the
running `work` browser and the caller told nothing at all about the flag they
passed — `--from` silently dropped in precisely the case they most want to hear
about. `require_allowed_seed_from` therefore sits in `spawn_browser`'s pre-flight
beside `require_allowed_user_data_dir`, and takes that call's ANSWER (the
anchored directory) rather than the raw string, so "is this the shared session"
and "does it already exist" are asked about the directory a request MEANS. The
resolver asks again because it is public with its own callers; the cost is one
`exists()`.

**And the SOURCE question is asked in the pre-flight too, for the same class of
reason** (review S1). Two of the five refusals were already there and reached
the caller clean; the other three — the source is open, the source does not
exist, the source names a reserved word — are raised inside
`profile_source.seed_source`, which runs under the resolver INSIDE
`spawn_browser`'s `try`, so they arrived re-labelled:

```
Failed to spawn browser: seed_from='work' is open in a browser right now. …
```

A spawn that never started, reported as a spawn that failed. That is verbatim
what the comment fourteen lines above the guard forbids, and it is F-894 review
M1's whole argument reached by the new door. The obvious fix does NOT work: an
inner `try: … except ToolError: raise` around the resolver call still unwinds
into the enclosing `except Exception`. So `require_allowed_seed_from` calls
`_seed_source(requested)` and DISCARDS the answer, on `require_allowed`'s own
"asked twice on purpose" precedent.

**Discarding it is the point, not a shortcut.** "Is this source open" is a fact
with a LIFETIME, so the authoritative read stays the resolver's — the statement
before the copy, with no `await` between the two. The pre-flight ask is
ADVISORY: it turns the common case into a clean refusal and narrows the window
to microseconds rather than to zero. A source opened inside that window still
refuses, one layer down and one label differently, which is strictly better
than what it replaces.

**What it cost, measured, and what that cost is now** (delta review N2). The
gate is itself asked twice — the tool's pre-flight, then the resolver's own —
so a seeded spawn walked the process table THREE times for one source where an
unseeded named spawn walks it once: counted at `clone_storage._profile_hold`,
`['work', 'work', 'beta', 'work']`. `_profile_hold` is a full psutil cmdline
scan, and on a machine that has been running a Chrome fleet that is hundreds of
processes. The two ADVISORY asks now share ONE walk through
`profile_source.advisory` / `ADVISORY_HOLD_SECONDS` (1.0 s), and the read
before the copy is never served from it — `_seed_source_for_copy` takes the
witness itself. So the count is `['work', 'beta', 'work']`, pinned as a count
rather than described.

Reusing an advisory answer is a WEAKER claim than the one the advisory ask
already makes: that answer is discarded. Inside one spawn a remembered answer
can only ever be NEGATIVE — a positive raises at the first ask, so nothing
reaches the second. Across spawns a stale negative lands in exactly the
check-to-copy window above and the authoritative read refuses it; a stale
positive refuses a source closed less than a second ago, with the right
sentence and the right remedy. Both are bounded by the window and by nothing
else, which is why it is a second and not a minute. Cost now: one
`Path.resolve`, one `exists()` and one `profile_hold` for a spawn that passes
`seed_from`, and nothing at all for one that does not — which is what
`require_allowed_seed_from`'s docstring says, where it used to say "one
`exists()`".

The freshen had to be split out to make that safe: `_seed_source` is now
side-effect-free and `_seed_source_for_copy` is the one that refreshes a stale
shared seed first. Taken twice, that freshen would copy a whole profile for a
spawn about to be refused; left inside the shared function, a gate whose job is
to ask questions would write to disk. The two can answer DIFFERENTLY on a first
run — the pre-flight sees no seed yet and reads the live shared directory, the
copy creates the seed and reads that — which is harmless precisely because the
pre-flight's answer is thrown away.

It is also asked BEFORE the F-871 walk to `<name>-2`, for the same shape of
reason: a held target is walked to a directory that does not exist, so a
`seed_from` asked afterwards would seed a SUBSTITUTE directory under a flag the
caller passed about theirs.

### 2.4 A RUNNING source is refused BY NAME — the safety argument

This is the decision the whole feature rests on.

`profile_copy.copy_file` gives a file `COPY_ATTEMPTS` (3) tries and then SKIPS
it with a WARNING; `copy_delta` swallows `PermissionError`/`OSError` per file
and continues. That tolerance is correct — one unreadable cache entry must not
fail a whole profile copy — and its price is exact: **a skipped file is a login
silently missing from the copy, and nothing in the product can say which file
mattered.** On Windows every file Chrome holds open is refused outright;
everywhere, a WAL-mode SQLite cookie jar may be mid-transaction (study §3.C1).

So the source of a copy must be closed, and a source that is not is refused by
NAME with the remedy:

```
seed_from='work' is open in a browser right now. Copying a profile Chrome is
writing to silently drops whatever it has locked — which is where the logins
are — and nothing can say afterwards what was lost. Close the 'work' session
first (`stealthy close <instance>`), or seed from 'default', which the product
keeps a separate copyable form of.
```

Refusing by NAME rather than by path is not cosmetic: `stealthy ls` and
`stealthy close` take a name and an instance, and a message quoting
`C:\stealth-mcp-browser-sessions\sessions\work` is not something a caller can
act on directly.

**Nothing is created on disk when it refuses.** `_seed_source` raises before
`_copy_profile_tree`, which is the only thing that makes the target directory. A
half-made session the caller now has to clean up would be a second harm stacked
on the refusal. Pinned.

#### `default` is the one exception, for a mechanical reason

Seeding from `default` works whether or not `default` is open, because the
product maintains a separate, CLOSED, copyable form of it — the seed
(`master-snapshot`), refreshed on three triggers whenever the shared profile is
free (F-892/F-893). A copy taken from it is a copy of a directory nothing is
writing to. That is a fact about the mechanism, not a privilege of the word, and
the module docstring says so — which is what stops the next reader from
generalising the exception.

What it costs is stated rather than hidden: **that copy can be as stale as the
last time `default` was closed.** F-888 made this worse without intending to
(study §1.6: the shared profile's browser now outlives its backend, so
`"default-in-use"` can hold indefinitely), and F-895's `seed_changed_since` is
exactly the report for it — `stealthy profiles` prints `SEED CHANGED SINCE` when
the shared profile has taken a login write since the seed was copied. The
feature does not fix that staleness; it reports it.

**The live-directory fallback is KEPT.** When no seed exists yet (a fresh
installation), `seed_source` falls back to copying the live shared directory,
which is 2.1.11's behaviour byte for byte. Widening the refusal to cover it was
considered and rejected: that path is what CREATES the first seed, and refusing
it would leave a fresh installation unable to make its first named session. The
cost is `profile_copy`'s, is named there, and applies to exactly one spawn in
the life of an installation.

### 2.5 Per-session seeds: deliberately NOT built

The study mentions them as the natural generalisation. They are not built, and
the reason is not only the ~0.47 GB each:

A seed is not a directory, it is a LIFECYCLE. `master-snapshot` needs three
refresh triggers, a staleness witness (`LOGIN_WITNESSES` — which F-892 found
naming a file Chrome stopped writing in v96), a rule for when a refresh is safe
(F-893: a refresh whose target was held shipped as a success), and a reservation
keeping callers from driving a browser on it (F-894). Two findings were spent
getting that right for ONE seed. N of them is N copies of all of it, bought to
avoid a refusal whose remedy is closing a window.

Copying the CLOSED session directory directly needs none of it: a closed profile
is consistent on disk, there is nothing to keep fresh, and the refusal above is
what makes "closed" true. That is the simplest mechanism that never copies a
live profile, which is the property that was actually being bought.

### 2.6 Provenance

`profile_seed.seed_name`'s existing fallback is `source.name`, so a session
seeded from `work` records `seeded_from: "work"` with no change — F-896 §6.5
predicted exactly this and pinned that the value is something `session=` accepts.
That pin is extended here rather than replaced: the reported word is now any
session's name, and the test re-opens the session it names.

`seed_changed_since` reads `LOGIN_WITNESSES` off the marker's recorded `source`
PATH. For a session source that path is the session's own directory, which is a
Chrome profile, so it works unchanged — VERIFIED by a pin that touches the
source's cookie jar and asserts the flag flips.

`source_kind` for this path is `explicit-session`. The `explicit` prefix is
load-bearing and is asserted: `profile_seed.is_named` reads it, so a session
seeded from another session is still a NAMED profile and is never swept.

`profile_seed.seed_sentence` is new and is the ONE phrasing of `provenance`'s
three fields, because two surfaces now say it out loud — `stealthy profiles`
(F-895) and `stealthy spawn` (this finding, so `--from` is checkable at the
shell). It is PHRASING only; whether the question arises is the caller's, and
the two callers answer that differently ON PURPOSE. `profiles` lists
DIRECTORIES, so an unmarked one is a finding worth printing as `unknown`; a
spawn reports a spawn, so it omits the line rather than claim an unknown seed
for the shared session, which is nobody's copy (F-895 review m6, reached from
the other side).

**`seed_changed_since` has three values and each gets its own ending** (review
N1). It was two: `SEED CHANGED SINCE` for True and silence for everything else,
which made `None` — "no login witness could be read in the recorded source" —
render BYTE-IDENTICALLY to False. Measured:

```
source present:         'work'  changed=False | seeded from work at <t>
source DELETED:         'work'  changed=None  | seeded from work at <t>
name reused (new dir):  'work'  changed=False | seeded from work at <t>
```

A vanished source read as a fresh one. Pre-existing in F-895's shape and made
ORDINARY by F-897: until now the recorded source was always the product's own
seed, and it is now a directory a caller can delete or rename at will. `None`
prints `  (source unreadable)`.

Lowercase and parenthetical, deliberately NOT a second shouted phrase.
`SEED CHANGED SINCE` is an ALERT — your copy is behind, act on it — while this
is a caveat about what could be READ; giving them one register teaches a reader
to ignore both. It does not say "deleted", because `seed_sentence` sees three
fields and not a path, and a source that exists but holds no login produces the
same `None`. Distinguishing those two would need a fourth field on
`provenance`, i.e. a payload change, for a difference nobody can act on
differently — named as residual 8 rather than built.

The NAME is still reported for a deleted source, and that is correct rather
than an oversight: which session a copy came from is a fact about the COPY and
does not stop being true when the source is removed. What changes is only
whether we claim to know the source's current state.

### 2.7 The CLI holds no second opinion

`--from` is `seed_from=<what you typed>`, passed through untouched, pinned with
a value the backend will refuse (`a/b`). Whether the source exists, is open, is
the shared one, or names a target that already exists are four facts only the
backend can see, and a CLI-side pre-check would be a second answer that goes
stale between the check and the spawn. Same discipline `--session` already has,
with a sharper reason.

---

## 3. Three payments, and what they bought

Every file that needed a line was at its cap, and **caps ratchet down only.**

* **`clone_storage.py`** (grandfathered 1054). `embedded/profile_copy.py` is the
  new home for the filesystem mechanics of a Chrome profile directory —
  `REGENERABLE_NAMES`, `ignore_names`, `copy_file`, `copy_delta`,
  `rmtree_robust`. The justification is this finding's own: F-897 makes the
  SOURCE of a copy a caller's choice, and §2.4's refusal is correct only because
  of what that module's tolerance costs, so putting the tolerance and the
  exclusion list in one named home is what lets `clone_storage` state the policy
  without the mechanics in the middle of it (`page_storage` leaving
  `browser_manager`, same shape). 1054 → 958 before the feature, **980 after**,
  so the `GRANDFATHER` row is **DELETED** rather than ratcheted, on
  `embedded/server.py`'s precedent: a grandfathered cap is a standing permission
  to be over budget and leaving one on a file that fits says something untrue.
* **`cli_call.py`** (1000/1000, not grandfathered). `cli_render.py` is the new
  home for the three renderings — `instance_rows`, `spawn_lines`, `tool_lines`,
  the clip rule, the widths, `LAST_KNOWN_MARK`, `UNKNOWN_SECTION`. The line
  between the two files is CONSEQUENCE: an exit code and the JSON-or-text
  decision (`wants_json`) are things a script depends on and stay in
  `cli_call`; the shape of a table is explicitly not a contract, which is why
  `wants_json` sends JSON the moment stdout is not a terminal. 1000 → **928**.
* **`profile_seed.py`** (917/1000), and this one was owed to the MERGE rather
  than to the feature. F-901 grew the landing rule by 185 lines while F-897 had
  grown the same file by 288, and the merged module stood at **1112**.
  `embedded/profile_source.py` is the new home for `seed_from` itself —
  `seed_request`, `require_new_session`, `SeedSource`, `seed_source` — and the
  cut is this question's surface rather than a raised cap. The line between the
  two is WHEN the question is asked: `profile_seed` is consulted on every spawn
  and answers what a seed IS and what a caller may SAY; nothing in the new file
  runs unless a caller wrote `seed_from`. The seam was already visible before
  the merge made it load-bearing — `profile_seed`'s own module docstring still
  enumerated four questions and never named this one. They meet at exactly
  three points, each of which stays single-homed: `require_name` (one name rule
  for `session` and `seed_from`), `require_allowed` (a source is anchored and
  reserved by the same gate a target is) and `seed_sentence`/`provenance` (the
  already-exists refusal names a seed in the words `stealthy profiles` uses).
  It is deliberately NOT folded into `profile_copy`, which owns the MECHANICS
  of a copy and knows nothing about sessions. The invariant `profile_seed`
  carries is inherited whole rather than weakened: the four directories arrive
  as a `Roots`, the containment predicate and the liveness witness as
  callables, so `clone_storage` is still the only thing that knows where a
  session root is. 1112 → **917**; `clone_storage` paid the one line the new
  import cost out of its own module docstring, 1001 → **1000**.

Two behaviour-preserving cleanups came with the first move, because a new leaf
lands under the default lint set rather than inheriting `clone_storage`'s
verbatim-from-`server.py` ignore list: `ignore_names` dropped its unused
`directory` parameter (a `shutil.copytree(ignore=…)` vestige — a parameter
nothing reads claims the answer depends on where you ask), and `copy_file`'s
retry count is `COPY_ATTEMPTS` rather than a bare 3 and 2.

One more fold, and it is not tidying: `resolve_profile_selection`'s
`source_override` + `source_kind` became ONE `profile_source.SeedSource`
(`override`). A path and the word recorded for it are decided together, and two
parameters let a caller record a copy as having come from somewhere it did not.
It also kept the signature inside `PLR0913`.

| File | before | after | cap |
|---|---|---|---|
| `embedded/clone_storage.py` | 1054 | 1000 | 1000 (GRANDFATHER row DELETED) |
| `embedded/profile_copy.py` | — | 231 | 1000 |
| `embedded/profile_seed.py` | 599 | 917 | 1000 |
| `embedded/profile_source.py` | — | 241 | 1000 |
| `cli_call.py` | 1000 | 928 | 1000 |
| `cli_render.py` | — | 146 | 1000 |
| `cli.py` | 953 | 950 | 1000 |
| `tool_sections/browser_management.py` | 751 | 786 | 1000 |

---

## 4. Pins

`tests/test_seed_from_session.py`, **66 nodes**, measured in four rounds
because the file was written in four.

1. The original 43 against the tree with the two extractions committed and the
   feature reverted — **40 failed, 3 passed**.
2. The five added at the F-896 merge, against that merge with only their two
   product lines un-extended (`seed_request`'s falsy test, and `require_name`'s
   empty branch raising its own sentence instead of `_names_nothing`'s) —
   **5 failed, 1 passed**.
3. The fourteen added for this review, against `c5accae` — **9 failed,
   4 passed** (the remaining node, `test_every_refusal_here_names_both
   _spellings`, was written after the M1 refusal existed and is stated below).
4. The four added for the DELTA review's N2 (`TestTheAdvisoryAskCostsOneScan`),
   against the merge commit `3da8572` — **2 failed, 2 passed**. The two REDs
   are the count itself: `AssertionError: one advisory scan shared by both
   pre-flight asks, plus the authoritative read before the copy — got
   ['work', 'work', 'beta', 'work']`, and `advisory asks share one walk:
   ['work', 'work']`. The two GREENs are guards and say so — an unseeded named
   spawn must not change (`['beta']`), and a source opened after the memo must
   still be refused by the pre-copy read, which is the memo's one named cost
   rather than a new hole. They were run with the memo's own reset removed, so
   the REDs are the behaviour and not an `AttributeError` on a helper that did
   not exist yet.

Round 3's REDs, by claim:

* **S1** — `test_a_running_source_…`, `test_a_missing_source_…` and
  `test_a_reserved_source_…[master, master-snapshot]` all failed on
  `AssertionError: a caller-input refusal was re-labelled as a failed spawn:
  'Failed to spawn browser: …'`. Each drives the REAL tool through the REAL
  resolver, which is the whole point: the existing tool-level nodes patch
  `resolve_profile_selection` away, and that double is exactly the blind spot
  the review found.
* **M1** — `test_an_out_of_root_target_is_refused_at_the_tool` failed with
  `DID NOT RAISE`, i.e. the silent drop reproduced at the tool;
  `test_the_gate_refuses_it_and_not_only_the_tool` likewise at the gate.
* **N4** — `test_the_no_session_refusal_names_both_spellings` failed on the
  missing `--session`.
* **N1** — `test_none_is_not_silence` and
  `test_a_deleted_source_reaches_the_sentence_that_way` failed on
  `assert 'seeded from work at …' != 'seeded from work at …'`, which is the
  defect stated as an equality.

The four that passed in round 3 are guards and say so:
`test_an_existing_target_…` and `test_a_path_shaped_source_…` (already in the
pre-flight, pinned beside the other three so a later move of one is visible
against the rest), `test_a_target_inside_the_root_still_passes` (the M1
refusal must not catch an ordinary named session) and
`test_changed_since_is_none_once_the_source_is_gone` (`changed_since` was
already answering `None` honestly — the defect was the COMPOSITION above it,
which is why the end-to-end node is the one that failed).

The four that passed on arrival are guards and are stated as such in their own
docstrings: `test_no_from_sends_no_seed_from` and
`test_no_seed_from_reaches_the_resolver_as_none` (no `--from` sends no
argument, at the CLI and at the tool — true before and after, and there so a
later default cannot start sending one),
`test_the_drive_refusal_reads_one_flavour_on_both_platforms` (a pure `PurePath`
assertion about `C:profile`, F-894's lesson reached a third time) and
`test_a_drive_hidden_behind_a_space_is_still_refused` (the strip already ran
before the drive read; pinned because the two halves live in two functions).

The five merge REDs, verbatim:

```
test_a_value_that_names_nothing_is_refused_with_the_one_sentence["   ", "\t", " \n "]
  E  ToolError: seed_from must be a name; it was empty.     (≠ the one sentence)
test_empty_is_not_given_and_seeds_from_the_default
test_an_empty_from_still_makes_the_session_from_the_default
  -> seed_request("") raised instead of answering None
5 failed, 1 passed
```

The 40 REDs are of four distinct shapes, not one:

* `AttributeError: module … has no attribute 'require_allowed_seed_from'` — the
  gate did not exist (every node that composes a selection).
* `TypeError`/`AttributeError` on `profile_source.seed_request` — the reader did
  not exist (the eight path shapes, the empty case, the `None` case).
* `assert {} == {'seed_from': 'a/b'}` — the CLI sent nothing.
* `StopIteration` — the parser had no `--from` action.

By claim:

* **Seeding from a closed session** — the new session holds the SOURCE's cookie
  jar (a byte string no fixture writes, read back out of the new directory, not
  a role word); the marker records the source by NAME, its PATH, an `explicit-`
  prefixed kind and `auto_clean: False`; omitting `seed_from` is unchanged; and
  `None` is the same answer as `"default"` field for field.
* **A running source** — refused naming the session, saying it is open, and
  offering the remedy; NOTHING created on disk; a nonexistent source refused
  naming it; and a running `default` explicitly NOT refused, with the mechanism
  reason in the node's own docstring.
* **Creation only** — an existing target raises, names its real seed, and the
  existing directory is not touched; no `session` raises; `session="default"`
  raises.
* **A NAME** — eight path shapes parametrized, each asserted to say
  `seed_from`; the empty case; four reserved/folded words refused with nothing
  created; whitespace stripped (and `"default "` asserted NOT to be the fold
  case, which is what `"default."` is for); `None` stays `None`.
* **Provenance** — diagnostics carry the source name; the reported word is
  something `session=` accepts AND re-opens the right directory;
  `seed_changed_since` flips when the SOURCE takes a later login write; the
  spawn block prints the sentence; it prints NOTHING for a profile that is
  nobody's copy; and the sentence is byte-identical to `cli._seed_line`'s.
* **The CLI** — `--from` sends `seed_from`; a value the backend will refuse is
  passed through verbatim; the flag is documented (unlike `--profile`).
* **The tool BODY**, which is the layer every node above sits one below — a
  gate that answers correctly and a body that never calls it would leave all of
  them green. Four nodes drive the REAL `spawn_browser` and watch what the
  resolver is handed: the normalised name reaches it, an existing session is
  refused AT THE TOOL (which is what proves the guard sits in front of the
  F-888 re-attach rather than only in the resolver behind it), and a
  path-shaped `seed_from` is refused there too.
* **The vocabulary** — all six F-897 refusals swept for `master`/`snapshot`,
  with F-896's own exemption for a caller's echoed word. F-896's derived sweep
  (tool registry + parser tree + a real `profile_selection`) is unchanged and
  green with the new parameter and its docstring.

### Goldens moved

`tests/goldens/tool_surface.json` — **HARD**, moved deliberately in this PR with
justification per `CONTRIBUTING.md`. **13 insertions, 1 deletion, all inside
`spawn_browser`**: the `description` (the new `seed_from` parameter documented)
and one new `seed_from` property in `input_schema.properties`. No other tool's
bytes moved, which is the thing the golden exists to prove about a change of
this shape — the same shape and the same size as F-896's move one commit
earlier.

`tests/test_correlation_id.py`'s inline schema snapshot gains the same ONE
property, for the same reason, with the reason in the comment above it.

No SOFT golden file moved.

---

## 5. What did NOT change

* **What an unnamed spawn selects.** Untouched, and still pinned by
  `test_concurrent_spawn_collision.py`'s 3-of-3 node.
* **What a session with no `seed_from` is copied from.** The shared seed, with
  the same `source_kind`, on the same code path.
* **The held-target walk to `<name>-2`** (F-871). Unchanged — and unreachable
  when `seed_from` is passed, because a held target exists and is refused in
  front of it.
* **`_fallback_profile_selection`.** Its two arguments became one tuple; what it
  decides is identical. `seed_from` is deliberately NOT threaded into a retry:
  the retry re-opens a directory this attempt already created.
* **Directories on disk, env vars, and existing clone markers.** As F-896 left
  them.
* **What `stealthy spawn` prints — except that it gained a line** (review N2).
  `seeded : seeded from default at <t>` now appears for EVERY spawn that
  resolves to a marked directory, including an ordinary disposable clone
  nobody passed a flag for: the gate is `seeded_from not in (None, "",
  UNKNOWN_SEED)` and a clone's marker says `default`. It is listed here
  because §5 otherwise reads as a complete account of what stayed still, and
  it is KEPT rather than gated on `seeded_from != DEFAULT_SESSION`. A
  throwaway profile's seed is the one thing about it that is not throwaway: it
  answers "why am I logged in / not logged in in this browser", which is the
  first question a disposable spawn raises, and the timestamp is the only
  thing that distinguishes a clone made from a fresh seed from one made from a
  seed last refreshed in August. Suppressing it would hide F-895's whole
  answer from the commonest spawn there is.

---

## 6. Residuals

1. **The live-source refusal is REASONED, not measured.** That a file copy of a
   running Chrome profile loses locked data is read off `profile_copy`'s own
   `except` clauses plus the study's §3.C1 analysis of WAL-mode SQLite; no
   experiment in this PR copies a live profile and enumerates what went
   missing. The designed measurement exists (study §3.C, the 3×2 storage
   matrix) and is F-898's gate, not this one's. The refusal is the
   conservative direction either way: the cost of being wrong is a caller
   closing a window they did not have to.
2. **F-898 is the answer for a RUNNING source** — a CDP cookie hand-off
   (`Storage.getCookies`/`setCookies`) rather than a file copy — and it is
   being measured separately. It carries cookies and NOT `localStorage`,
   IndexedDB, service workers or the autofill stores, so it is a different
   promise, not a strictly better one. Named here; not built here.
3. **No `promote` / write-back verb.** `--from` with the arrows reversed is
   defensible and cheap now that this exists, but default write-back silently
   merges identities (study §3.D: two sessions logged into two Google accounts
   both promote and the seed keeps whichever closed last). An explicit verb is
   its own finding.
4. **No `ephemeral` door.** F-896 §6.2 deferred an explicit "give me a
   disposable copy even though `default` is free", and F-897 does not need it:
   `seed_from` is about a session that persists, and a disposable copy seeded
   from a named session is a different capability with its own trade-off (a
   caller who asks for disposable and gets a copy of a session someone else is
   about to change). It stays a residual, now with a second reason to exist —
   `seed_from` with no `session` is refused, and `ephemeral` is the request
   that refusal would otherwise have covered.
5. **`seed_changed_since` is about the SOURCE, not about the copy.** It says
   the source has moved on; it does not say the copy is missing anything
   specific, and nothing back-fills a session when its source changes. That is
   deliberate — see residual 3.
6. **A caller's own two names can still be one directory.** F-896 §6.6's
   boundary is unchanged and now applies to `seed_from` too: `--from acme.` and
   `--from acme` are two requests and, on Windows, one directory. The fold
   refusal exists to stop a name folding onto a word the PRODUCT owns; two of a
   caller's own names colliding is their own collision.
7. **`profile_copy` is a new module with no test file of its own.** Its
   behaviour is covered where it always was — `test_profile_clone_excludes_cache.py`
   and `test_profile_resolution.py`, whose imports moved with the names. A
   dedicated file would be a third place to look for the same assertions; if
   the module grows a decision of its own it should get one. Its one
   non-move is recorded rather than folded into the "pure move" claim (review
   N3): `ignore_names` dropped the `directory` parameter `_profile_ignore_names`
   had — an unused `shutil.copytree(ignore=…)` vestige, and a parameter nothing
   reads claims the answer depends on where you ask. Both call sites and both
   test files moved with it.
8. **`(source unreadable)` cannot say WHICH unreadable it is.** `None` covers
   both "the recorded source directory is gone" and "it is there and holds no
   login witness", because `seed_sentence` is handed three fields and not a
   path. Distinguishing them means a fourth field on `provenance` — a payload
   change, and a golden move — for a difference a caller cannot act on
   differently: either way the honest statement is "I cannot tell you whether
   that source has moved on". Named rather than built.
9. **A REUSED source name still reads as `False`.** Delete `work`, create a
   new unrelated `work`, and the copy's provenance reports a source that has
   not moved on — about a directory sharing nothing with the original but its
   spelling. The marker records a PATH as well as a name, and the path is the
   same path, so nothing in the three fields can see the substitution. Closing
   it needs an identity for a session that survives deletion (a uuid in the
   marker), which is a schema decision this finding did not need to make.
   F-896's "the seed a session reports is a session you can open" still holds
   — it is simply a different session than the one that seeded it.
10. **A JUNCTIONED existing session plus `--from` is refused with the WRONG
    SENTENCE** (delta review N1, measured). With `<clones>\linked` a junction
    to storage elsewhere, `session="linked", seed_from="work"` raises
    *"`seed_from='work'` copies one SESSION into another, and
    `'<clones>\linked'` is a directory named by path rather than a session"* —
    to a caller who typed a bare session NAME. It still REFUSES (a `--from`
    onto an existing session is refused either way, and nothing is created),
    so what is wrong is only which of the two refusals speaks. The cause is
    ORDER: `require_new_session` tests `inside_root` before `target.exists()`,
    and `inside_root` is `clone_storage._is_relative_to`, which RESOLVES — so
    the junction lands outside the clone root.

    **Named rather than fixed, because both obvious fixes are worse.** Swapping
    the two clauses hands an out-of-root target that happens to EXIST the
    advice "pass `session='p'` on its own to open it", which is wrong for a
    path — that request has no session to open. Switching `inside_root` to
    F-901's `_inside_lexically` re-opens a hairline M1: a DANGLING junction
    inside the clone root reads as non-existent AND lexically inside, passes
    every gate, and then the resolver's own `_is_relative_to` says outside and
    seeds nothing, silently — which is the exact defect M1 closed. The
    resolving predicate is the right one HERE precisely because it has to
    mirror the resolver's seeding condition (`clone_storage.py`'s
    `_is_relative_to(explicit, clone_root)`) rather than F-901's containment
    question; two predicates that sound alike answer two different questions,
    and the gap between them is where the silence lives. Closing this properly
    means the gate knowing the target is a session *by name* independently of
    where it resolves, which is a `profile_request` change and not a
    `seed_from` one.
