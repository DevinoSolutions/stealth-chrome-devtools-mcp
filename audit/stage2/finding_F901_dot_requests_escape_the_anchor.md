# F-901 — a dot-shaped `user_data_dir` walks out of the anchor

**Status:** fixed on `fix/F901-dot-requests-escape-the-anchor`.
**Found by:** the F-896 closing review (`2953e8d`), as a pre-existing defect it
was right not to fold into that PR.
**Present in:** 2.1.11 and every release that has had `anchor`.

---

## 1. The mechanism

`profile_seed.anchor` decides where a relative profile request lands:

```python
anchored = roots.session / asked
return anchored if inside(anchored, roots.clones) else roots.clones / asked
```

That `inside` call reads like a containment guard and is not one. It is a
**disambiguation** — "did the caller already write the `sessions/` prefix, so
that anchoring under the session root has already put them in the clone root?"
— and whichever branch it picks, the composed path is returned **unchecked**.
Nothing downstream re-asked the question either: `reserved_reason` refused a
reserved NAME, the seed PATH, a drive-qualified relative path, a folded name
and a second `default`, all of which key on `Path(requested).name` — and
`Path("..").name` is `""`, which is in no rule's set.

So a request made of dots walked straight out. **Measured** against a temp
session root, on `2953e8d` and identically on 2.1.11:

| request | landed on | |
|---|---|---|
| `user_data_dir="."` | `<root>/sessions` | **the clone root** |
| `user_data_dir="./"` | `<root>/sessions` | **the clone root** |
| `user_data_dir=".\"` | `<root>/sessions` | the clone root (Windows folds the component) |
| `user_data_dir="..."` | `<root>/sessions/...` | **resolves to** the clone root |
| `user_data_dir=".."` | `<root>/sessions/..` | **the browser-session root** |
| `user_data_dir="../.."` | `<root>/sessions/../..` | above both, outside the tree |
| `user_data_dir=<abs clone root>` | itself | the clone root, by the other door |
| `user_data_dir=<abs session root>` | itself | the browser-session root |

Chrome is then handed the directory that holds **every session** — or the one
that holds every session *and* the shared profile *and* its seed — as its own
`--user-data-dir`, and writes its profile files in among them. It is the same
class of harm F-896 refused `"   "` for (that one reaches the clone root by the
filesystem folding a whitespace component away), reached by a shorter road.

It was also the two spellings disagreeing: `session=` refused every shape in
that table, because a session is a NAME and all of these are paths.

## 2. The decisions

**2.1 The containment check is asked of the NORMALISED landing, and the
normalisation is LEXICAL.** `anchor` now returns `os.path.normpath` of the
composed relative landing. Lexical and not `resolve()` because it must fold
`..` for a directory that does not exist yet, must NOT follow a symlink (a
symlinked session root is one the operator configured; resolving it here would
record a path they never wrote, and F-888's re-attach matches on that string),
and must cost no filesystem call on the spawn path. What `normpath` cannot see
— a component the OS itself folds away, `...` on Windows — is caught one call
later by `same_dir` / `inside`, which resolve **both sides**, so a symlinked
root still compares equal to itself.

Only the RELATIVE branch is normalised. An absolute path is the caller's own
string and F-896 promises it back byte-for-byte (`…/work/trailing ` keeps its
space); normalising it would break that promise for nothing.

**2.2 A relative request must land STRICTLY INSIDE the clone root, and that
question is asked LEXICALLY.** That is the seventh refusal in
`reserved_reason`. The shared profile is exempt by where it LANDS rather than
by how it was spelled, because `anchor` answers `roots.shared` for the bare
word `default`.

The first shape of this refusal asked `clone_storage._is_relative_to`, which
resolves both sides — and that was a **regression against 2.1.11**, caught by
the review (S1) and measured: a session directory that is a symlink or a
Windows junction to storage elsewhere resolves OUTSIDE the clone root, so
`acme` was read as a walk out of the tree and refused through both spellings,
with a message saying it "walks out of the session storage and lands at
`<clones>\acme`" — a path visibly inside the root it claims to have left. That
configuration is one an operator deliberately makes (a big session on another
disk) and 2.1.11 honoured it, because the pre-F-901 refusals all keyed on
`Path(requested).name`, which is just `acme`.

So `_inside_lexically` asks the strings alone. It closes the finding unchanged,
because **every escape F-901 is about is folded by `normpath` BEFORE the check**
— `..`, `../..`, `sub/..` are already the directory they mean by the time the
refusal sees them. What a lexical test cannot see is caught one clause above by
the ROOT pair, which keeps resolving: `...` on Windows (measured to land on the
clone root) and a junction pointing AT a root are both refused there.

Two predicates, and deliberately not a second way to do one thing: "which root
did the caller already name" is about the directories these paths BECOME, and
"did this request walk out" is about the path they composed. The cost is named
rather than hidden — a link INSIDE the clone root pointing outside it is the
caller's own business again, which is what 2.1.11 said and what an operator who
made that link meant. `_inside_lexically` is spelled on `os.path` rather than
this module's `Path` name, because both arguments are concrete directories on
this host while the flavour screening that matters is on the caller's STRING,
one line above.

**2.2b A refusal names the directory it really opens.** `_landing_words` adds
the resolved target when, and only when, it differs from the lexical landing.
Without it the root refusal reached through a junction read `'self' names a
directory profiles are KEPT in (<clones>\self)` — the one sentence explaining
that a path IS the clone root, naming a path inside the clone root.

**2.3 The two roots are refused through EITHER door**, which is the sixth
refusal. An absolute path reaches the clone root exactly as `"."` did, and the
lead's lean was to refuse; the argument for it is that these two directories
are not profiles — they are where profiles are kept — so a browser opened on
one writes its own `Default/`, `Local State` and lock files in among every
session's, and the storage sweep then iterates its own profile. It is **two
equalities and nothing wider**: a directory that merely lives near them, or
contains them, is untouched, which keeps F-896's "an absolute path is the
caller's own business" true.

**2.4 `sub/../acme` is ALLOWED and lands on `sessions/acme`.** Decided, not
overlooked. After normalisation it IS `acme` — the same directory, the same
marker, nothing second created — so the product answers with the canonical form
and there is one directory with one recorded answer, which is what convention 4
asks for. Refusing it would buy nothing and would cost a caller who composed a
path out of parts. `sessions/../sessions/acme` lands there too, measured.

**2.5 `...` is refused on Windows and allowed on POSIX, and the RULE is still
uniform.** The rule is "strictly inside the clone root"; on POSIX `...` is an
ordinary directory name that satisfies it, and on Windows it folds onto the
clone root and does not. The divergence is the filesystem's, not the rule's,
and the hazard is closed on the host where the hazard exists. That is the one
place this finding differs from F-896's fold rule, which refuses on every host
— there the folded target was a word the PRODUCT owns and a caller could type
by accident; here the containment rule already answers it, on the host where it
matters, and a lexical dots-only rule would refuse a legal POSIX directory name
for no hazard there.

## 3. Pins

`tests/test_profile_anchor_containment.py`, 38 nodes across two RED rounds.

**Round 1 — the finding itself: 28 failed, 3 passed** — every dot shape landed
on or above a root, both roots were honoured by absolute path, and
`sub/../acme` did not equal `acme`. The 3 that passed are the guards (an
ordinary name, the `sessions/` prefix, an ordinary absolute path), and they are
labelled as such.

**Round 2 — the review's S1: 4 failed, 34 passed**, each for its own reason: a
linked session directory refused through the alias, refused through `session=`,
refused when nested, and — the fourth — the refusal naming the landing exactly
once, which is the link and never the directory it opens. The link is a
`mklink /J` junction on Windows (no administrator, no Developer Mode, which
`os.symlink` on a directory needs) and a symlink on POSIX; a host that will
make neither SKIPS with that as its reason rather than asserting about a link
that is not there. Two further nodes guard the clause that stays resolving: a
junction pointing AT the clone root, and `...` on Windows.

* **The invariant**, parametrized over all eleven shapes: a request is REFUSED
  or lands strictly inside the clone root. Either answer is acceptable because
  which spellings a filesystem folds away is the host's business; the third
  answer — on or above a directory that holds profiles — is what may never
  happen, and it is asserted through `same_dir` and `_is_relative_to`, which
  resolve, so the assertion holds under a symlinked root too.
* **The refusal in words**, over the six shapes that are a path walk under BOTH
  `PurePath` flavours, so the refusal itself and not merely the containment is
  the same answer on all seven CI cells.
* **Both spellings answer the same way** for those six.
* **Both flavours agree `..` is relative** — what gates the rule is
  `Path.is_absolute()`, the flavour `anchor` anchors with.
* **The two roots by absolute path**, and an ordinary absolute path still
  answered byte-for-byte beside them.
* **`sub/../acme` == `acme`**, and the `sessions/` prefix still means one
  directory rather than `sessions/sessions/acme`.
* **`sub\..\acme`** joined the invariant's list (review N4): the one member
  carrying a NAME as well as dots, so it means `sessions/acme` on Windows and a
  literal directory called `sub\..\acme` on POSIX — both inside, which is what
  makes the invariant's "refused OR inside" phrasing the right one.

## 4. What did NOT change

* **Which directory an ordinary request lands on.** `acme`, `sessions/acme`,
  `default`, an absolute path, and F-896's `" /tmp/x"` all answer exactly as
  they did — measured before and after.
* **A linked session directory.** A symlink or junction under the clone root
  opens exactly as it did in 2.1.11, through either spelling (§2.2).
* **`anchor`'s disambiguation.** The `sessions/` prefix still collapses.
* **The absolute-path promise.** Byte-for-byte, whitespace included.
* **`reserved_reason`'s five existing refusals**, their order, and its
  SIGNATURE — the containment question is asked of the landing alone, so the
  resolving predicate stays `anchor`'s argument and nothing downstream had to
  learn a new call shape.
* **The wording of a refusal about a bare name.** Two of them now name the
  string the caller typed when it differs from the basename they key on
  (`'../master' names 'master', which is a reserved profile name` — review N2),
  and are byte-identical for the common case.

## 5. Residuals

1. **An absolute path that CONTAINS the roots is not refused** — `C:\` or a
   home directory. Refusing ancestors would be a much wider rule (every
   plausible parent of a configured session root), the harm is not the same one
   (Chrome writes a profile beside the storage, not inside it), and a caller who
   types their home directory as a profile has made a different mistake. Named
   rather than silently out of scope.
2. **`...` on POSIX** — §2.5.
3. **The normalisation is not applied to an absolute path**, so
   `<abs>/sessions/..` reaches the browser-session root under a spelling
   `same_dir` DOES catch (it resolves) but `inside` never sees. Covered today
   by the sixth refusal for exactly the two roots; a third directory reached
   that way is the caller's own absolute path, which is §5.1's question.
4. **POSIX is not executed here.** Every rule is flavour-screened by
   construction and the two-flavour nodes assert it, but only Windows was run.
   CI's six POSIX cells remain the check.
5. **A link INSIDE the clone root pointing outside it is the caller's own
   business** — the price of §2.2's lexical walk test, and the behaviour
   2.1.11 had. Refusing it would take away the configuration the review
   measured (a session on another disk) to close nothing this finding is about.
6. **A trailing-space walk component makes the recorded directory and the real
   one disagree** (review N3). `user_data_dir="acme/.. "` is accepted — it is
   strictly inside the clone root and there is no escape — and answers
   `…\sessions\acme\.. `, while Windows lands a browser on `…\sessions\acme`
   and `browser_pid_registry.normalize_path`, the F-888 re-attach key, reads it
   as `…\sessions`. Three readings of one string. Nobody types it and the
   invariant is untouched; it is the same class as §5.3 one door along, and a
   per-component `rstrip(". ")` before `normpath` would be right on Windows and
   wrong on POSIX, which is a platform branch this file deliberately does not
   carry.
7. **A refusal can still quote a string the caller did not type**, in the one
   place the transformation is `profile_request`'s rather than
   `reserved_reason`'s: `user_data_dir=". "` is stripped to `"."` by F-896's
   bare-name rule before any refusal sees it, so the message quotes `'.'`.
   Review N2's fix covers the two rules that key on a basename; carrying the
   pre-strip string down for this one would mean threading it through the
   request normaliser for a message.
