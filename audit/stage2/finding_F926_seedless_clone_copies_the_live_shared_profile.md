# F-926 — a seedless clone copies the LIVE shared profile, which `profile_source` refuses for the same state

**Status:** FILED, not fixed
**Found by:** the F-925 audit of `clone_storage.resolve_profile_selection`
**Severity:** data loss as the caller experiences it — a session that looks
successful and carries zero cookies. No message, no diagnostic field.

---

## 1. The two answers

`src/stealth_chrome_devtools_mcp/embedded/clone_storage.py`, the clone branch of
`resolve_profile_selection` (`:936-952` on `fe5cef6`):

```python
if override is not None:
    seed = override
elif snapshot.exists():
    seed = profile_source.SeedSource(snapshot, "default-seed")
elif master.exists():
    # No seed yet (first run, seed deleted, or the seed copy failed). Fall
    # back to copying directly from the live shared profile.
    # profile_copy.copy_delta skips locked files (PermissionError/OSError),
    # and _copy_profile_tree does a double-pass — cookies and login data
    # transfer successfully even while Chrome has it open.
    seed = profile_source.SeedSource(master, "live-default-fallback")
else:
    raise RuntimeError(...)
```

This path composes a `SeedSource` **by hand** and never calls
`profile_source.seed_source`, so none of that module's rules apply to it. One
import away, `profile_source._default_source` (`:258-292`) is handed the
identical state — no seed, shared profile running — and **raises**:

```
the 'default' session has no copyable form yet and its browser is open, so
there is nothing safe to seed from: the only copy available would be of the
live profile directory itself, which carries no cookies at all — the jar is
held open and skipped, and nothing can say afterwards what was lost.
```

Two answers to one question, one import apart. Convention 4's defect in its
literal form.

## 2. The comment is contradicted by two modules

The claim "cookies and login data transfer successfully even while Chrome has
it open" is asserted nowhere else and denied twice:

* `profile_copy.py:26-37` — "A skipped file is a login silently missing from
  the copy, and this module cannot say which file mattered. So
  `profile_source.seed_source` refuses to seed a new session from a source a
  live browser holds, by name, rather than handing the caller a copy whose gaps
  nobody can enumerate."
* `profile_source.py:327-337` — "**measured, a file copy of a running source
  carries ZERO cookies**."

The double pass does not help: both passes read the same held jar, and
`copy_file` skips it both times. On Windows every file Chrome holds is refused
outright; everywhere else the cookie jar is a WAL-mode SQLite database
mid-transaction.

## 3. Reachability, and why it is not a corner

The branch is reached when there is no seed AND `master` exists AND control
reached the clone path at all. Control reaches the clone path (`:911`) when
`force_clone` is set **or** `_profile_has_running_browser(master)` is true — so
the live-master fallback fires in precisely the state `_default_source` case 3
refuses.

**Its trigger is what F-925 produced.** "Seed dir absent" was, until F-925 was
fixed, a state the product manufactured on its own: the refresh deleted the
seed and rebuilt it in place, so any death mid-copy left the seed empty — and
an empty directory still passes `snapshot.exists()`, which routes to
`SeedSource(snapshot, "default-seed")` and copies *nothing*. A seed directory
that was fully removed (a hand cleanup, an interrupted `rmtree`, a
first run) routes here instead. **The two findings compose**: F-925 creates the
precondition, F-926 turns it into a silently empty session. Fixing F-925 closes
the common producer of the precondition, not this branch.

## 4. What it should probably do

Not decided here, but the shape is narrow: this branch is the only caller of
`SeedSource` that does not go through `profile_source`, and the honest fix is to
ask `profile_source` the question it already answers — which includes the
`held`/`driven` split, the CDP cookie hand-off when we drive that browser
(F-898), and the by-name refusal with its one-action remedy when we do not. The
one thing that must be preserved is `_default_source`'s own last paragraph: a
shared profile nobody is running still copies the live directory when there is
no seed, because that is the first-run path and it is copying a directory at
rest.

## 5. Residual / notes

* `"live-default-fallback"` reaches the caller as `clone_source` in the
  selection dict and therefore as `spawn_diagnostics.profile_selection`, so the
  string is visible — but nothing says it means "this session may have no
  cookies", and no caller is known to read it.
* `_fallback_profile_selection` (`:982-984`) returns `None` when the seed is
  absent, so the retry path does not have this hole. Only the first selection
  does.
