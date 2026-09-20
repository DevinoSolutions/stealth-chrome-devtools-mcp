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

In `profile_seed.reserved_reason`, asked by `resolve_profile_selection` before
anything is created and **in front of the F-871 `<name>-2` walk**, so a
reserved name can never come back as `master-2`:

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

## 5. Residuals

* **The existing `sessions/master` and the mangled directory are not deleted.**
  This fix stops them being created; 0.81 GB of them is still on disk, and
  reclaiming a directory that may hold a login is an operator's decision, not a
  patch's. `stealth-chrome-devtools profiles` now shows both with their seed
  age (F-895).
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
