# F-924 — `browser_pids.json` has no age prune, and some entries are now permanent

**Status:** open (filed by F-916's review; not fixed in that lane)
**Found by:** the F-916 review, arbitrating a boundedness claim that turned out
to be false for two of four UNDECIDED shapes.
**Severity:** low on its own — a growing JSON file, not a lost login — but it is
the reason those two shapes had to be accepted as permanent rather than bounded.

---

## 1. The finding

Two of this tree's record-keeping subsystems prune by age and this one does not:

| record | prune |
|---|---|
| log files | `logging_setup.prune_old_logs`, with a dead-backend post-mortem exemption (F-840) |
| clone trash | `clone_storage._purge_expired_trash` |
| `browser_pids.json` | **nothing** |

An entry leaves `browser_pids.json` only by being REAPED, i.e. only when
recovery reaches an established negative about it.

## 2. Why it stopped being theoretical

F-916 made four shapes answer `reap_guard.UNDECIDED`, and two of them are
decided BEFORE `browser_alive` is asked:

* an unreadable `pid` or `user_data_dir`;
* a missing `create_time`.

Neither can ever become an established negative, so those entries are permanent.
That is the owner's ruling read literally — we do not kill what we cannot
establish — and F-916 §6 now says so instead of claiming a bound it does not
have. The other two shapes (no recoverable endpoint; persistence the record never
stated) ARE bounded, because they are decided after the liveness witness.

The population is not hypothetical: `psutil.AccessDenied` makes `_create_time`
answer `None` on a current write path, which is the same Windows condition F-918
exists for.

## 3. Why a naive prune is WRONG

**Dropping an entry for a LIVE browser destroys the only thing that names it.**
That is F-888's harm exactly: the record is what a later backend reads to find a
browser its predecessor left running, and an unnamed live Chrome on a persistent
profile can never be re-attached to, never be closed by `close_instance`, and
never be reaped — it just holds its profile until someone finds it by hand.

So "delete entries older than N" is not the fix. Any prune has to establish that
the browser is GONE, which is precisely the question these entries could not
answer in the first place. That is what makes this a finding rather than a chore.

## 4. Shapes worth considering (none chosen)

* Prune only entries whose recorded pid does not exist AT ALL — weaker than
  `(pid, create_time)` and so subject to recycling, but a pid that is absent
  entirely cannot be a live browser of ours. Interacts with the bare-pid rule
  this tree otherwise refuses, so it needs the owner.
* Prune on the DIRECTORY instead: if the profile directory is gone, the browser
  is gone. Cheap and safe for auto-clones; says nothing about a named profile.
* Do not prune, and give the operator a verb — `stealthy` already lists
  profiles, and `kill-orphans --force` already skips the classification.

## 5. What is NOT this finding

The permanence itself is accepted, deliberately, by the owner's ruling. This
finding is about the record file growing without any prune AT ALL, which was
true before F-916 and is merely more visible after it.
