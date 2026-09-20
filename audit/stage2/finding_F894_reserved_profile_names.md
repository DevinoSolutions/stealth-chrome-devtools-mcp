# F-894 — `user_data_dir="master"` silently meant a different profile

**Severity** HIGH (silent). **Ref** `origin/main` = 56596e8 (2.1.10).
**Fixed in** `fix/F892-F895-snapshot-truth`.

## 1. What shipped

A relative `user_data_dir` is anchored under the clone root (56596e8
`clone_storage.py:922-936`). So `spawn_browser(user_data_dir="master")`
resolved to `<root>/sessions/master`, **not** `<root>/master`, and — the
directory not existing — was CREATED as a fresh clone of the snapshot.

`MEASURED 2026-09-20`: `C:\stealth-mcp-browser-sessions\sessions\master`
exists, 0.46 GB, marker `source_kind: explicit-master-snapshot`,
`created_at: 2026-09-11 21:54:06`. Someone asked for the master profile by the
name the docs give it and got a nine-day-old copy of a snapshot, with no
warning anywhere in the return.

There was also nothing stopping a caller passing the SNAPSHOT directory as a
`user_data_dir`, which is how F-893's live precondition arose — a browser
writing into the seed every new session copies from.

## 2. The mangled directory, explained and reproduced

`design_session_ux.md` §2.4 noted an adjacent artefact as "cosmetic":

```
sessions/stealth-mcp-browser-sessionssessionsstealth-chrome-devtools-mcp-f876e3d7f2ec
```

It is not cosmetic and it is not a separate bug — it is this one, reached
through a second door. `MEASURED` (reproduced exactly, `probe_driverel.py`):

```
Path("C:stealth-mcp-browser-sessionssessionsstealth-chrome-devtools-mcp-f876e3d7f2ec")
  .is_absolute()  -> False        # drive-RELATIVE: drive 'C:', no root
resolver picks    -> C:\stealth-mcp-browser-sessions\sessions\
                     stealth-mcp-browser-sessionssessionsstealth-chrome-devtools-mcp-f876e3d7f2ec
```

which is byte-for-byte the directory on disk. A Windows absolute path whose
backslashes have been eaten by a lenient string-escape layer (`\s` is nobody's
escape sequence) becomes a drive-relative path; `Path.is_absolute()` is False
for one; the resolver therefore treated a fully drive-qualified path as a bare
session NAME and anchored it. 0.35 GB of profile was written under a name that
was an accident.

## 3. The fix

### 3a. Where the question is asked (review M1)

**Asking it only in the resolver was not enough, and the first pass did exactly
that.** `browser_reattach.adopt_held_profile` runs in `spawn_browser` *before*
`resolve_profile_selection` (`tool_sections/browser_management.py`, the F-888
branch) and matches the requested directory against live browsers on exact
normalized path equality. So for an absolute snapshot path with a browser on
it, the spawn RE-ATTACHED and the resolver never saw the request — and that is
not a hypothetical, it is the state F-893 measured. The resolver-only pin
passed only because its fixture had no browser on the snapshot; it pinned the
resolver, not the product.

So `profile_seed.require_allowed` is the raise-shaped gate and **two sites ask
it**: `spawn_browser`, beside its headed-visibility guard and outside its `try`,
and `resolve_profile_selection`, which is public and has its own callers. One
rule, one home, two callers — it is a pure path decision (no I/O beyond
`Path.resolve`), so asking twice is free, and one home is worth more than one
call. Being outside the `try` also stops the refusal being re-labelled
`Failed to spawn browser: …` (review m9), which is the wrong word for a request
we declined to act on at all.

The MASTER by absolute path deliberately still reaches the re-attach: there,
adopting the running browser is the desired outcome.

### 3b. What is refused

In `profile_seed.reserved_reason`, before anything is created and **in front of
the F-871 `<name>-2` walk**, so a reserved name can never come back as
`master-2`:

1. a bare relative name in `RESERVED_NAMES` = {`master`, `master-snapshot`,
   `default`}, case-folded → `ToolError` naming the word and pointing at the
   unnamed spawn;
2. a path resolving to the SNAPSHOT dir → `ToolError`;
3. a drive-qualified path that is not absolute → `ToolError` naming it.

`default` is reserved ahead of the session vocabulary that will use it: a
friendlier word for the same trap is still the trap.

**The master by absolute PATH is deliberately allowed**, and now selects the
MASTER role rather than `explicit`. Driving the master directly is how a human
logs in, and only the master role makes `close_instance` refresh the snapshot
afterwards — which is the whole propagation path. Today's behaviour (role
`explicit`, no refresh on close) was a quieter version of the same defect: the
right directory with the wrong consequences. The chosen behaviour is pinned.

Anchoring moved to `profile_seed.anchor` so `reserved_reason` can be asked
about the directory a request MEANS rather than the string it was written as;
the two differ for every relative path.

## 4. The pins

`tests/test_profile_seed_truth.py::TestReservedProfileNames` — each reserved
name raises and creates nothing; case-insensitivity; the snapshot path; the
master path selecting `profile_role == "master"`; the drive-relative path
raising with an empty `sessions/` afterwards; an ordinary name unaffected; and
a reserved name never walking to `master-2` even when `sessions/master` exists
and is held. Mutation-checked (emptying `RESERVED_NAMES` turns the class RED).

Four more from the review round:

* `test_a_held_snapshot_is_refused_before_any_re_attach` — the headline. A live
  holder on the snapshot directory (`fakes.held_profile` plus a patched
  `profile_lock.profile_hold`) and an `adopt_held_profile` that would otherwise
  succeed; it asserts BOTH the `ToolError` and that no adoption was ATTEMPTED,
  because without the second half it passes again for the reason the
  resolver-only pin did. Mutation-checked: deleting the `spawn_browser` call
  turns it RED with `Failed to spawn browser: adopt_held_profile must not be
  reached`, i.e. the re-attach really is in front.
* `test_the_drive_refusal_does_not_depend_on_the_host_flavour` — §6 above.
* `test_an_existing_reserved_dir_is_told_how_to_reach_it` and
  `test_a_reserved_name_with_no_directory_says_nothing_about_one` — the escape
  is named when, and only when, there is something to escape to.
* `test_the_profiles_verb_still_lists_a_refused_name` — refusing the NAME must
  not hide the DIRECTORY; the message, CHANGELOG and RUNBOOK all promise the
  listing, so it is pinned as the claim it is. Mutation-checked by making
  `_collect_profiles` skip the name.

## 5. Residuals

* **The existing `sessions/master` and the mangled directory are not deleted.**
  This fix stops them being created; 0.81 GB of them is still on disk, and
  reclaiming a directory that may hold a login is an operator's decision, not a
  patch's. `stealth-chrome-devtools profiles` still LISTS both (the reservation
  is on what a caller may ask for, not on what the CLI reports) with their seed
  age (F-895), and both stay reachable by absolute path. The refusal message
  names that escape when the anchored directory exists, and RUNBOOK's "Disk
  filling up" carries the one-time job (review M3) — "pick another name" alone
  was advice for a NEW session and useless to someone who already has one.
* **The reservation is on the NAME, not on intent.** A caller who genuinely
  wants a session called `master` cannot have one. That is the trade: the word
  is the product's, and the message says which.
* **Only the snapshot path is refused, not every product-owned path.** The
  clone root itself, `.trash`, and an arbitrary session belonging to another
  project are all still nameable. They are not seeds, so naming one is a
  legitimate (if unwise) request; the seeds are the ones where a mistake is
  silent and shared.
* **A drive-relative path is refused, not repaired.** We do not guess that
  `C:foosessionsbar` meant `C:\foo\sessions\bar` — the separators are gone and
  any reconstruction would be a second guess on top of the first.
* **That refusal was a Windows-only rule and is now universal — deliberately, in
  production rather than in the pin.** `test_drive_relative_path_is_refused`
  went RED on all six Linux/macOS cells of PR #139 run 35528415335 and green on
  all Windows cells, because a drive is a Windows concept: MEASURED,
  `PureWindowsPath("C:foo").drive` is `"C:"` and `PurePosixPath("C:foo").drive`
  is `""`, so on POSIX `C:foo` was an ordinary relative name and the refusal
  never fired. The two available fixes were splitting the pin per flavour
  (F-888's `c0d201a` precedent) and screening the input under both. **Split
  rejected**: it would have pinned the divergence rather than closed it, and the
  harm is not host-shaped — the string arrives from a CALLER, a model writing a
  Windows-looking path to a backend on any OS, and on POSIX it is anchored into
  the clone root exactly as it was on Windows. So `reserved_reason` reads the
  DRIVE through `PureWindowsPath` (the only flavour that has drives; on Windows
  it *is* the host flavour, so nothing there changes) and keeps "would this be
  anchored rather than opened" on plain `Path`, the flavour the resolver
  actually anchors with. **No second home for anchoring**: `anchor` is untouched
  and still native. For `C:foo` both flavours answer "not absolute", so the
  refusal is now the same on every platform — pinned by
  `test_the_drive_refusal_does_not_depend_on_the_host_flavour`, which sets
  `profile_seed.Path` to `PurePosixPath` (what a POSIX host IS for this
  function) and RED-failed before the change. **The residual is one asymmetry we
  chose to keep**: a full Windows absolute path (`C:\Users\x\p`) is allowed on
  Windows and REFUSED on POSIX — correctly, because on POSIX that whole string
  becomes one anchored session name, which is this finding's harm; it is
  platform-dependent by nature and is therefore NOT pinned cross-platform. The
  price is a POSIX user with a directory genuinely named `C:something`, who is
  refused and told to pass a fully qualified path. **POSIX is verified here by
  flavour simulation and by reasoning only** — no Linux or macOS run was made
  from this machine; CI is the witness.
