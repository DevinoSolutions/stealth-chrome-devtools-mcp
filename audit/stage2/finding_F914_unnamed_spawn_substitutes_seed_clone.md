# F-914 — an unnamed spawn silently substitutes a seed clone when the shared session is held

**Severity: top.** This is the whole of the owner's "the master profile was
erased / I have to set up the credentials again". Re-entering the logins it
costs is manual 2FA and CAPTCHA work no agent may automate.

## 1. The defect

`clone_storage.resolve_profile_selection` (2.1.12, `clone_storage.py:911`):

```python
master.parent.mkdir(parents=True, exist_ok=True)
if not force_clone and not _profile_has_running_browser(master):
    snapshot_result = _refresh_master_snapshot_if_safe("before-default-open")
    return {"user_data_dir": str(master),
            "profile_role": profile_seed.DEFAULT_SESSION, ...}
...
elif snapshot.exists():
    seed = profile_source.SeedSource(snapshot, "default-seed")
...
return _copy_clone_from_source(seed.path, clone, clone_root, seed.kind)
```

If **anything** holds the shared profile — the owner's own logged-in Chrome, a
sibling agent's spawn, a stale Windows `lockfile` — the `default` branch is
skipped and the caller is handed a **new clone of `master-snapshot`** under a
generated name. Nothing raises. `profile_role: "clone"` is the only tell, and it
is a field a caller has to go looking for.

## 2. Why the substitute is LOGGED OUT and not merely different

The seed is refreshed only while the shared session is CLOSED.
`_refresh_master_snapshot_if_safe` answers `seed_error: "default-in-use"`
(`clone_storage.py:530-532`) whenever a browser holds it, and the
`before-default-open` refresh at `:912` sits INSIDE the not-running branch. The
owner keeps a shared Chrome open, so the seed freezes at the last clean close
and every clone minted afterwards carries that vintage.

Measured on the owner's machine, 2026-09-21:

| Path | Size | LastWriteTime |
|---|---|---|
| `master\Default\Network\Cookies` | 524,288 | **1:17:46 PM** |
| `master-snapshot\Default\Network\Cookies` | 524,288 | **7:42:41 AM** |

Five and a half hours. `shutil.copy2` preserves mtime, so the snapshot
faithfully reflects the jar as it stood at its last refresh — which is exactly
the point: a spawn at 1:18 PM got 7:42 AM's logins and was told it had
succeeded.

## 3. The hold witness can be wrong in the direction that causes this

`_profile_has_running_browser` -> `profile_lock.profile_hold`. On Windows there
is only ONE witness and that module's own docstring states an unreadable answer
"resolves toward HELD" (F-871's deliberate, correct choice for its own
question). A psutil `AccessDenied` on the scan therefore reads as "the shared
session is busy" and diverts the spawn to a clone. That direction is right for
"may I take this directory"; it was silently also deciding "may I substitute a
different one", which is a different question with the opposite safe direction.

## 4. The rule (owner's ruling, 2026-09-21, binding)

When the requested profile is already held by a running Chrome: **seed the new
browser from the HOLDER's LIVE cookie jar over CDP if we can reach the holder;
otherwise REFUSE by name.** Never silently substitute a fresh clone. "Substitute
but report it" was offered and explicitly NOT chosen — the substitute is a
logged-out stranger, and `profile_role` is not a place a caller looks before
trusting a login.

## 5. The fix

`embedded/profile_target.py` is THE one home for the rule, and it is the
TARGET-side twin of `profile_source.seed_source`: a running profile is usable
exactly when we drive it. `resolve_profile_selection`'s shared branch reads the
`Hold` once (it needed the REASON as well as the fact, and a second walk of the
process table would be a second answer to one question) and calls
`profile_target.hand_over_or_refuse`:

* **driven** -> the copy still comes from the closed SEED, because F-893's
  argument is untouched — a file copy must come from a directory nothing is
  writing to — and the live jar is handed over afterwards through F-898's
  `cookie_handoff`. A stale seed plus a current jar is strictly better than a
  stale seed, and the ORDERING is what makes it true: the hand-off writes last.
  The answer carries `handed_over_from: "default"`.
* **not driven** -> `ToolError`, naming the session and the holder's pid and no
  PATH (F-869/F-877: a profile path names the operating user and this message
  reaches the client, the durable log and Sentry at once). Nothing is created.

`_fallback_profile_selection` asks the same rule, because F-834 widened it to
all three roles and it is therefore the SECOND DOOR onto the same substitution —
left alone it answers a held shared session with exactly the clone the resolver
now refuses, one spawn failure later. It also carries a hand-off the previous
attempt was making onto the retry (`profile_target.still_driven_source`) and
DROPS it when the source is no longer ours, because a retry sits on the far side
of a whole browser launch.

**F-920 is folded in here** because it is the same branch: the
`live-default-fallback` arm's comment claimed cookies "transfer successfully
even while Chrome has it open", which `profile_copy.copy_file`'s own docstring
contradicts — a held file is SKIPPED, twice for the double pass, and the gap
cannot be enumerated. The branch is now reachable with the shared session open
only after `hand_over_or_refuse` has allowed it, so the jar arrives over CDP;
with it closed it is a copy of a directory at rest, which is what 2.1.11's first
run always was. The comment says that instead.

## 6. What this costs, named rather than hidden

**A concurrent unnamed spawn that loses the profile singleton to a Chrome we do
not drive now FAILS.** F-834 stage 1 gave that caller a disposable clone; it now
gets a `ToolError` naming the holder and the two remedies. That is the ruling
applied literally, and the trade is deliberate: the clone it replaces is the
logged-out browser this finding is about. A fleet that wants parallel browsers
should name sessions (`session=<name>`), which is unaffected. The stage-1 case
where the holder IS one of ours — the common one, since a sibling spawn of this
backend is what wins that race — still gets its clone, now with the jar handed
over. `tests/test_concurrent_spawn_collision.py` pins both halves.

**An absolute `user_data_dir` outside the clone root is untouched.** The rule
sits exactly where the walk sat, inside `_is_relative_to(explicit, clone_root)`,
so a caller naming a path outside it still opens that path as-is. There is no
substitution there to prevent — the caller gets the directory they named — and
widening the rule would refuse a working configuration.

**The seed is still not refreshed while the shared session runs.** F-914 closes
the substitution, not the staleness; `seed_changed_since` already reports it and
`close_instance` refreshes on close (F-910). A per-session seed was considered
and declined in F-898 §10.1 for reasons that have not changed.

**`profile_target` and `clone_trash` are new files and both are forced moves.**
`clone_storage.py` stood at 993 of a 1000-LOC budget that ratchets DOWN only, so
the rule went to its own home (which it deserved: it is a policy, where
`profile_lock` is a fact) and the recoverable-eviction mechanism followed it out
(`clone_trash.py`) because the rule alone did not pay for itself. No behaviour
moved with either.
