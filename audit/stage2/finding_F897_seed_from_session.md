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

**Unset means `default`, and the two are ONE path.** `profile_seed.seed_source`
treats `None` and the bare word `default` identically in its first branch, so
"seeding from the shared session" cannot develop behaviour that differs from
"seeding from nothing named". Pinned
(`test_seed_from_default_is_the_same_as_omitting_it`), because two branches that
agree today are the defect convention 4 names.

### 2.2 It is a NAME, through the gate `session` already passes

`profile_seed.seed_request` → `require_name` → (at the copy)
`require_allowed`. No second resolver, no path door.

`require_name` is F-896's name rule with the FIELD as data. The two messages
must name the parameter the caller actually typed — a caller told about
`session` when they wrote `seed_from` goes and edits the wrong argument — but
the ESCAPE differs and cannot come from the field: `session` has a path door
(`user_data_dir`) and `seed_from` has none, because the provenance a seed writes
is a NAME the caller can pass back to `session=`, and an arbitrary directory has
no such word. So each caller hands in its own two hints and the rule stays one
function.

That also means `master`, `master-snapshot` and F-896's fold shapes (`default.`,
`master.`) are refused through the seed door exactly as through the session
door, because it is the same `reserved_reason`.

### 2.3 CREATION only — an existing target RAISES

`profile_seed.require_new_session` refuses three shapes, which are one sentence
read three ways: there has to be a session (`seed_from` with no `session`), it
has to be the caller's own (`session="default"`), and it must not already exist.

The third had a real choice in it and the two alternatives were both rejected:

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

### 2.7 The CLI holds no second opinion

`--from` is `seed_from=<what you typed>`, passed through untouched, pinned with
a value the backend will refuse (`a/b`). Whether the source exists, is open, is
the shared one, or names a target that already exists are four facts only the
backend can see, and a CLI-side pre-check would be a second answer that goes
stale between the check and the spawn. Same discipline `--session` already has,
with a sharper reason.

---

## 3. Two payments, and what they bought

Both files that needed a line were at their caps, and **caps ratchet down only.**

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

Two behaviour-preserving cleanups came with the first move, because a new leaf
lands under the default lint set rather than inheriting `clone_storage`'s
verbatim-from-`server.py` ignore list: `ignore_names` dropped its unused
`directory` parameter (a `shutil.copytree(ignore=…)` vestige — a parameter
nothing reads claims the answer depends on where you ask), and `copy_file`'s
retry count is `COPY_ATTEMPTS` rather than a bare 3 and 2.

One more fold, and it is not tidying: `resolve_profile_selection`'s
`source_override` + `source_kind` became ONE `profile_seed.SeedSource`
(`override`). A path and the word recorded for it are decided together, and two
parameters let a caller record a copy as having come from somewhere it did not.
It also kept the signature inside `PLR0913`.

| File | before | after | cap |
|---|---|---|---|
| `embedded/clone_storage.py` | 1054 | 980 | 1000 (GRANDFATHER row DELETED) |
| `embedded/profile_copy.py` | — | 227 | 1000 |
| `embedded/profile_seed.py` | 599 | 803 | 1000 |
| `cli_call.py` | 1000 | 928 | 1000 |
| `cli_render.py` | — | 146 | 1000 |
| `cli.py` | 953 | 950 | 1000 |
| `tool_sections/browser_management.py` | 751 | 786 | 1000 |

---

## 4. Pins

`tests/test_seed_from_session.py`, **43 nodes**. RED evidence: run against the
tree with the two extractions committed and the feature reverted,
**40 failed, 3 passed**.

The three that passed on arrival are guards and are stated as such in their own
docstrings: `test_no_from_sends_no_seed_from` and
`test_no_seed_from_reaches_the_resolver_as_none` (no `--from` sends no
argument, at the CLI and at the tool — true before and after, and there so a
later default cannot start sending one) and
`test_the_drive_refusal_reads_one_flavour_on_both_platforms` (a pure `PurePath`
assertion about `C:profile`, F-894's lesson reached a third time).

The 40 REDs are of four distinct shapes, not one:

* `AttributeError: module … has no attribute 'require_allowed_seed_from'` — the
  gate did not exist (every node that composes a selection).
* `TypeError`/`AttributeError` on `profile_seed.seed_request` — the reader did
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
   the module grows a decision of its own it should get one.
