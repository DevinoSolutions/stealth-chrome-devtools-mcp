# F-923 — CHANGELOG placement is unguarded, and a green `test_doc_claims` has been read as proof it is not

**Status:** fixed (partially — see §6.1, which is the point of this finding)
**Date:** 2026-09-21
**Area:** `tests/test_doc_claims.py` (`TestChangelogIntegrity`) + `CONTRIBUTING.md`
**Filed by:** F-913's review. Observed while checking whether the 2.1.13 merge
procedure had an automated backstop; it does not.
**Measured against:** the tree at `b7701e5` (main `2c19448`), pyproject 2.1.12

---

## 1. The measured gap

`TestChangelogIntegrity` has existed since release 2.1.8 and asserts exactly two
things. Quoted from the source rather than paraphrased:

| node | what it asserts |
|---|---|
| `test_no_conflict_markers_in_root_docs` | no `^<<<<<<< `, `^=======$` or `^>>>>>>> ` in `CHANGELOG.md` or the five root docs |
| `test_changelog_states_each_section_once` | no `### ` heading appears twice |

Neither reads **placement**. Neither asks which `## ` heading a `### ` section
sits under, whether a queue exists, or where it is.

### 1.1 The gap is reachable, and it was reached — on `main`, for five PRs

Traced commit by commit over `v2.1.7..v2.1.8`, following `main`'s first-parent
line and reading which `## ` heading each `### ` section sits under:

| `main` commit | PR | sections wrongly under `## 2.1.7` |
|---|---|---|
| `b2a7fe88` | #116 (F-880) | — |
| `0c842e28` | #114 (F-876) | F-876 |
| `6339769e` | #118 (F-877) | F-876, F-877 |
| `3311be92` | #115 (F-879) | F-876, F-877, F-879 |
| `89cb0dd2` | #113 (F-875) | F-876, F-877, F-879, F-875 |
| `534d4944` | #117 (F-878) | F-876, F-877, F-879, F-875 |
| `101fdca3` | #119 (F-881) | — (corrected by hand) |
| `6ca0ae94` | #120 (release 2.1.8) | — |

At the worst point (`534d4944`) `## 2.1.7` held **six** `### ` sections. The
`v2.1.7` tag ships **two** — F-873 and F-874. So four entries described a
release that did not contain any of them, on `main`, across five merged PRs,
with the gate green the whole way.

The mechanism is a **clean merge, not a conflict**. The relocation is visible at
`5c939e61`, a `Merge remote-tracking branch 'origin/main'`: the release commit
renames `## Unreleased` to `## <version>`, and a branch that had appended its
section inside that block merges afterwards with the surrounding context
unchanged, so git places the section by context — under the RELEASE heading,
silently. The conflict resolver never runs, because nothing conflicted. The
correction was equally manual: `260ac306`, *"docs(changelog): move the six 2.1.8
fixes out of the …"*.

### 1.2 The absence has been mistaken for a presence — twice, in two directions

**A green gate read as a check.** The correction that prompted this finding was
a teammate reading a green `test_doc_claims` as placement being verified. It
never was. A gate that is green for a file it does not read is worse than no
gate, because it converts "nobody checked" into "something checked and it was
fine".

**And a healthy branch read as a catastrophe.** While writing this finding, a
report reached me that a sibling worktree had *clobbered 132 lines* of `main`'s
CHANGELOG — F-903/F-904/F-905's blocks overwritten. It is **not what happened**,
and the disproof matters more than the report:

* No commit in the repository deletes ~132 CHANGELOG lines. Scanned all 1401
  reflog entries across every worktree for a CHANGELOG diff with 100–200
  deletions against its own parent: three hits, all from 09-16/09-19, all
  balanced rewrites (`+117 −117`, `+157 −156`, `+293 −173`).
* The number is real, and it is `git diff origin/main <branch> --numstat --
  CHANGELOG.md` reporting **`… 132`** for `fix/F916-…`, `fix/F919-…` and
  `fix/F921-…`. The 132 deleted lines are exactly F-903's, F-904's and F-905's
  blocks.
* `git merge-base origin/main <branch>` is `a3d22b3` (PR #158) for all three,
  and each has **zero** merges of `main` on it. They have not merged `main`;
  `main` has moved 132 lines ahead of them. That is ordinary staleness.

`git diff origin/main` is not symmetric: deletions are lines `origin/main` has
that the branch lacks. **Before** a merge of `main`, a perfectly healthy branch
reports every entry `main` gained since the merge-base as a deletion. This is a
false-positive mode of the very procedure §5 prescribes, it fires on three live
branches right now, and it was read today as a data-loss incident. The procedure
is therefore stated with its precondition attached — *after* the merge — and
with the discriminator that tells the two apart (§5, §6.5).

A changelog that claims fixes under a version not containing them is a false
statement about a shipped artefact — the one class of claim this repo's doc
harness exists to prevent. So is a finding that reports an incident that did not
occur, which is why §1.2's second half is measured rather than repeated.

## 2. What the release process actually does — measured, because the fix depends on it

Three release commits, read off the tree:

| commit | `pyproject.toml` | first `## ` headings |
|---|---|---|
| `8ae59ce` release: 2.1.11 | `2.1.11` | `## 2.1.11`, `## 2.1.10`, … |
| `031318c` release: 2.1.12 | `2.1.12` | `## 2.1.12`, `## 2.1.11`, … |
| `9bb948c` release: 2.1.13 | `2.1.13` | `## 2.1.13`, `## 2.1.12`, … |

Two facts follow, and both are load-bearing:

1. **A release commit carries NO `## Unreleased` heading at all.** It renames
   the queue and adds no replacement. So a rule reading "the CHANGELOG has
   exactly one `## Unreleased`" is RED on every release commit — which is how a
   gate gets disabled within a week. The rule shipped here is **at most one**.
2. **The bump and the heading rename are ONE commit.** `9bb948c` changes one
   line of `CHANGELOG.md` and one line of `pyproject.toml`. There is no
   intermediate state where the version is bumped and the heading is not, which
   is what makes §5's third rule safe to assert unconditionally.

A third fact shapes the parser: the file carries a legacy terminal heading
`## 1.2.0 and earlier` alongside 23 `## X.Y.Z` headings. It is handled by
parsing the version from the START of the heading rather than by an exemption,
so the rule stays derived and a new release joins with no edit.

## 3. Root cause

The two existing nodes were written against a *content* failure — literal
conflict markers committed to `main` — and content is what they check. Placement
is a different question about the same file and nobody had asked it, because the
procedure that answers it ("diff the `### ` headings under the previous release
against `git show v<prev>:CHANGELOG.md`") lives in a runbook and in agents'
heads. A procedure five people have to remember is not a control; it is the
absence of one, with a person standing where the control should be.

---

## 4. The measurement

### 4.1 Each rule against each deliberately mis-shaped CHANGELOG

Fixtures are STRINGS — nothing writes the real `CHANGELOG.md`, because a test
that mutates it to prove it can fail is one interrupted run away from committing
the mis-shaped copy.

```
fixture                                                queue   order    sync
------------------------------------------------------------------------------
SHIPPED_HEADINGS            (legit release commit)         -       -       -
absorbed-into-shipped       (the 2.1.7 INCIDENT)           -       -       -
QUEUE_PLACED_BELOW_A_RELEASE                          CAUGHT       -       -
TWO_QUEUES                                            CAUGHT       -       -
DUPLICATED_RELEASE                                         -  CAUGHT       -
ASCENDING                                                  -  CAUGHT       -
INVENTED_SECTION                                           -  CAUGHT       -
bump without its heading                                   -       -  CAUGHT

REAL CHANGELOG  queue: None | order: None | sync: None    (pyproject 2.1.12)
```

Row 1 is the one that keeps the gate alive: a legitimate release commit is
accepted by all three. Row 2 is the one this finding refuses to hide — see §6.1.

### 4.2 Mutation matrix — because a pin nothing kills is not a pin

Eight mutations, each disabling ONE decision, each run against the whole file
with `__pycache__` cleared and `PYTHONDONTWRITEBYTECODE=1` (a same-second
mutate/revert otherwise executes stale bytecode). Baseline green, 25 passed;
target restored byte-identical afterwards.

```
mutation                                             verdict  node killed
--------------------------------------------------------------------------------
M1 queue-position check never fires                  KILLED   caught[queue-below-a-shipped-heading]
M2 second-queue check never fires                    KILLED   caught[two-unreleased-sections]
M3 ordering non-strict (<= becomes <)                KILLED   malformed[release-heading-stated-twice]
M4 ordering check never fires                        KILLED   malformed[release-heading-stated-twice, releases-out-of-order]
M5 a heading that is neither is tolerated            KILLED   malformed[a-heading-that-is-neither]
M6 version-sync check never fires                    KILLED   test_a_bump_without_its_heading_is_caught
M7 rule demands EXACTLY one Unreleased               KILLED   test_a_release_commit_is_accepted_with_no_unreleased_heading
                                                              test_the_incident_shape_is_not_caught_and_that_is_stated
M8 release regex drops the trailing-text arm         KILLED   test_the_legacy_catch_all_heading_is_not_a_special_case
                                                              test_the_real_changelog_releases_descend
```

**M7 is the one that had to be run.** The two nodes it kills are *limit* pins —
they assert the rules ACCEPT something — and a pin that asserts acceptance is
exactly the shape that is vacuous by accident. Making the rule demand a queue
kills both, so both are load-bearing.

## 5. The fix

Three nodes, in `TestChangelogIntegrity` — the existing home, not a second
doc-integrity file. Each rule is a pure function returning a PROBLEM STRING or
`None`, so the real-file node and the mis-shaped-fixture node assert the same
code rather than two spellings of it.

* **`_unreleased_problem`** — **at most one** `## Unreleased`, and if present it
  LEADS. "At most" is §2 fact 1. Catches a queue below a shipped heading (the
  mistake the post-release merge procedure can make) and a second queue being
  invented.
* **`_ordering_problem`** — every non-queue `## ` heading parses as a release and
  they **strictly descend**. Strict, so an equal pair — a section stated twice
  by a hand-resolved merge — is caught. Handles `## 1.2.0 and earlier` through
  the same regex, no exemption.
* **`_version_sync_problem`** — `pyproject.toml`'s version **IS** the top release
  heading. §2 fact 2 is why this needs no disjunction: the drafted
  "…or is absent from the CHANGELOG entirely" arm is deliberately NOT here,
  because this repo never produces that state and an alternative nothing can
  reach is a hole rather than tolerance — it would accept a release heading
  silently deleted. Measured true in all three states the repo does produce: a
  release commit, an ordinary commit after it, and a branch that merged `main`.

Plus two nodes that pin the *limits* rather than the behaviour:
`test_a_release_commit_is_accepted_with_no_unreleased_heading` (the cry-wolf
case) and `test_the_incident_shape_is_not_caught_and_that_is_stated` (§6.1).

**The catch-all is a procedure, not a node**, because it needs a base ref and
therefore cannot be hermetic. Added to `CONTRIBUTING.md` beside the release
steps: **after** every merge of `main`,

```
git diff origin/main --numstat -- CHANGELOG.md
```

must show insertions and **0 deletions**. An entry git relocated into a shipped
section shows up as deletions elsewhere in the file, so a nonzero right-hand
column is the signal whichever of the three shapes caused it.

The word *after* is load-bearing and is §1.2's second half: run **before** the
merge, the same command reports every entry `main` has gained since the
merge-base as a deletion, and a healthy branch looks like a clobber. The
discriminator is `git log --oneline origin/main..HEAD` — no merge of `main` on
the branch means the deletions are `main`'s lead, not your loss.

---

## 6. Residuals

### 6.1 The incident that motivated this finding is still NOT caught, and that is pinned

The 2.1.7 failure absorbed four entries **into** `## 2.1.7` and left no
`## Unreleased` behind. The resulting file has the same headings, in the same
order, agreeing with `pyproject.toml` — it is **byte-indistinguishable from a
legitimate release commit**, and all three rules accept it (§4.1, row 2).

No rule reading only this file could do otherwise. Knowing that a `### ` section
does not belong under `## 2.1.7` requires knowing what 2.1.7 shipped, which
lives in the tag, not the file.

This is stated three times on purpose — here, in the class docstring, and as
`test_the_incident_shape_is_not_caught_and_that_is_stated` — because the failure
mode of this finding is a future reader assuming F-923 closed the incident and
dropping the manual step. The node exists so that reader is contradicted by a
test rather than by a paragraph.

**What actually controls that shape** is the `--numstat` procedure in §5, and
the pre-bump heading diff against `git show v<prev>:CHANGELOG.md`.

### 6.2 A git-aware node was considered and declined

Comparing each release section against its tag would catch §6.1 exactly. It is
not done because it is not hermetic: it needs the tags present, which a shallow
CI clone does not guarantee, and it would be RED on the release commit itself
(the tag does not exist yet). A gate that is red on release day is a gate that
gets skipped on release day. If the tag fetch is ever made reliable in CI this
is the right node to add, and it belongs in the release lane rather than in
`test_doc_claims`.

### 6.3 `### ` sections are still unordered and uncounted within a block

Nothing asserts that a section under `## Unreleased` is well-formed, or that the
queue is non-empty. `test_changelog_states_each_section_once` catches the one
shape that mattered (a duplicate). Left because no failure of the others has
been observed and a rule with no evidence behind it is F-906's `uc` entry.

### 6.4 The rules read `## ` headings only

A `#`-level or `#### `-level heading inserted into the file is invisible to all
three. The file has exactly one `# ` heading today and no `#### ` ones; a rule
about them would be inventing a shape rather than guarding one.

### 6.5 The `--numstat` procedure is still a human step with a false-positive mode

§1.2 measured it firing on three healthy branches. It is kept — it is the only
control for §6.1's shape — but it is a procedure, so it cannot be made to
recognise its own precondition the way a node could. The mitigation is textual:
CONTRIBUTING states the precondition (*after* the merge) and the discriminator
in the same breath as the command. A reader who runs it before merging and acts
on the deletions will re-create the 2.1.7 incident from the opposite direction,
by "restoring" entries `main` already has.

## 7. Related

* `audit/stage2/finding_F913_validation_error_echoes_tool_result.md` — the
  finding whose review turned this up.
* `tests/test_doc_claims.py::TestChangelogIntegrity` — the one home; its
  docstring carries §6.1's limit.
* `CONTRIBUTING.md` — the `--numstat` procedure, which is the control for the
  shape §6.1 names.
