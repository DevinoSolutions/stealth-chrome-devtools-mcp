# F-922 — recovery reaped by DIRECTORY on a profile a human owns

**Status:** fixed (`process_cleanup._kill_processes_for_metadata`)
**Found by:** F-917 §6, which named it as the residual it could not reach —
"a browser on that directory with **no record entry at all** … is still in the
directory scan's answer and is still killed". Escalated to the owner because it
needed a ruling, not a patch.
**Severity:** data loss, and the widest of this lane: the victim is a browser
the owner started BY HAND, so no record entry names it and no spare can reach
it.

---

## 1. The ruling

The owner ruled, and the ruling is the finding:

> Directory-wide reaping stays ONLY for disposable auto-clone directories. On a
> named/persistent profile, recovery may kill only pids the RECORD ACTUALLY
> NAMES. A Chrome the owner started by hand on one of their own session
> profiles must never be terminated.

Two alternatives were considered and explicitly NOT chosen: "only ever kill
recorded pids everywhere" (an orphaned clone browser the record lost would
accumulate on a directory nothing else will ever claim) and "keep directory-wide
reaping" (today's behaviour, which is the harm reported).

The reasoning is an ASYMMETRY, which is why one uniform rule was the wrong
shape. An auto-clone directory is ours BY CONSTRUCTION — a human would never
open one by hand — so anything running on it is ours to reap. A named profile is
exactly what a human DOES open by hand: that is what `session=` is for, and
since F-888 a persistent-profile browser is meant to outlive its backend. The
safe direction differs by profile KIND.

## 2. Measured, before the fix

`$TEMP\f922_measure.py`, against this branch at `50c63fc` (i.e. with F-916,
F-917 and F-918 already in). `5555` is the owner's hand-started Chrome and is in
**no record entry at all**; `7778` is a stale entry's own pid, gone.

| entry | on the directory | killed |
|---|---|---|
| stale entry, NAMED profile, recovery | `[5555]` | **`[5555]`** |
| same via the CLOSE path (`recovery=False`) | `[5555]` | **`[5555]`** |
| disposable auto-clone (control) | `[5555]` | `[5555]` |
| NAMED, the record's own pid alive | `[7777]` | `[7777]` |
| NAMED, own pid alive **and** owner's Chrome there | `[5555, 7777]` | **`[5555, 7777]`** |

The last row is the one that shows the shape is wrong rather than merely
over-eager: even a fully JUSTIFIED reap — the entry's own browser, correctly
identified — took a bystander with it, because both were on the directory.

**F-917 cannot reach this.** Its `protected_pids` subtracts the pids of SPARED
RECORD ENTRIES, and the owner's Chrome has no entry; there is nothing to spare
it by. Only narrowing the SCOPE reaches it.

## 3. The fix

One conditional, at the one place the kill set is BUILT:

```python
pids_to_kill: set[int] = (
    set()
    if browser_pid_registry.on_persistent_profile(metadata)
    else self._get_browser_pids_for_profile(metadata.get("user_data_dir"))
)
```

`on_persistent_profile` read the other way round — the SAME predicate F-888's
adoption rule asks and the same one `_cleanup_profile_for_metadata` asks before
refusing to delete a directory. No second notion of "ours" was introduced, and
none should be.

Nothing else needed to change, which is the argument that this is the right
seam: for a persistent entry `pids_to_kill` simply starts empty, and the
existing recorded-pid path — already identity-checked through the shared
`_fallback_pid_identity_ok`, and start-time fenced under `recovery` — supplies
the one pid the record names. What was the *fallback* for a directory scan that
found nothing is now the *only* route on a named profile.

**It applies to BOTH callers, deliberately.** `browser_reattach.reap_recorded`
arrives with `recovery=True` and `kill_browser_process` with `recovery=False`,
and a named profile is a named profile whichever one arrived. Keying the rule on
the CALLER would be a second answer to "may we kill by directory", and the close
path has the same harm: an agent closing its own `session=work` instance would
otherwise kill the owner's Chrome on `work` too. The ruling's wording says
"recovery"; its reasoning is about profile KIND, so the predicate is applied
where the kind is known. **This is the one place this finding goes beyond the
ruling's literal wording and it is flagged rather than folded in.**

### 3.1 What paid for the lines

`process_cleanup.py` was at 1007/1007 — this lane's own ratchet — so the change
paid for itself. The four sites that logged "Skipping … for …: <reason>" in four
spellings became one `_skip_note`, which is a real deduplication rather than a
compression: a reap that DECLINES is exactly what an operator goes looking for.
**1007 → 1006.**

An extraction of the two near-identical recorded-pid branches into a
`_recorded_kill_pid` helper was tried first and **reverted**: measured, it took
the file to 1036, because two signatures and two docstrings cost more than the
duplication they removed. Recorded here so it is not attempted again.

## 4. The pins

`tests/test_recovery_must_not_kill.py::TestPersistentProfileIsReapedByRecordOnly`,
six nodes: the hand-started Chrome survives on a named profile (recovery AND
close), a justified reap no longer takes a bystander, the record's own browser
is still reaped, a disposable auto-clone still reaps its whole directory, and an
entry carrying NEITHER key still reads as disposable.

RED at `50c63fc`: three of the six; the other three are the controls that keep
this a NARROWING rather than a stand-down.

`...::TestFailedAdoptionReapDoesNotCrossTheSpare`, two more — see §4.2.

### 4.1 A defect this fix created in F-917's own pins

F-917's two unit pins ran on a PERSISTENT entry and asserted that
`protected_pids` kept a pid off the kill set. After this change no directory
scan runs for such an entry, so they passed **vacuously** — measured, the answer
is `[]` with AND without `protected_pids`. One went RED and drew attention to
it; the other stayed green and would have rotted silently.

Both now run on a disposable auto-clone entry, where two entries sharing one
directory is still reachable, and each asserts BOTH directions — the same input
with an empty `protected_pids` must kill — so neither can go vacuous again. The
end-to-end F-917 pin is kept on the shared persistent profile and says in its
docstring that the scope rule now stops it one layer earlier: it is the harm as
REPORTED, and two guards on the owner's logged-in Chrome is the right number.

The general rule, which this lane paid for twice in one day: **a pin that still
passes after the behaviour it guards has been removed is not evidence; it is a
pin nobody has shown can fail.** Asserting both directions is what shows it can,
and it is cheap — one extra call with the guard emptied.

### 4.2 F-917's defect had a SECOND door, and this fix closes that too

Reported as B1 and confirmed by measurement. `browser_reattach.run` protects the
pids of its ADOPTABLE candidates (`candidate_pids`) when an adoption fails and
falls back to `reap_recorded`. An entry F-916 SPARED is by construction not one
of them — it is in `Classified.spare`, which `run` never reads — so its pid is
in neither set, and the directory scan over a profile the two share reached it.
F-917's filter cannot help: the pid it would need to carry is not in the
collection `run` derives that filter from.

Measured, driving `reap_recorded` with exactly the synthetic metadata `run`
builds, a spared sibling (6666) and the candidate (7777) on one directory:

| tree | killed |
|---|---|
| `50c63fc` (F-916/F-917/F-918) | **`[6666, 7777]`** — the spared browser dies |
| with this fix | `[7777]` — the candidate's own, and only that |

**What closes it is the scope rule, not a second subtraction**, and the reason
is worth stating because it is load-bearing rather than lucky: `run` hands that
reap a metadata dict hard-coding `uses_custom_data_dir: True` and
`auto_clone: False`. It has to — the profile-delete guard reads those two keys
and a persistent profile must not be deleted — so the entry is PERSISTENT by
construction and no directory scan is ever built for it.

Two nodes pin it, and the second exists so the first cannot go vacuous by §4.1's
own rule: flip that one key to `auto_clone: True` and the scan runs again and
reaches the spared pid, which is what shows the pins measure the rule rather
than an empty answer. RED at `50c63fc`: the first.

**This section originally closed here, and its conclusion was wrong.** It said
`candidate_pids` should keep carrying only adoptable pids, on the grounds that
widening it would be a second guard for a harm the scope rule already answers.
That reasoning does not survive review, and the lead overruled it:

* `run` already HAS a `protected_pids` argument. The question was never whether
  to add a guard, only **which value to pass into the one that exists** — and
  passing `.adoptable` where the tree's other caller passes `.spare` is not a
  second guard, it is two answers to one question, which is what convention 4
  actually forbids.
* The line is **untested**, measured: mutating it to `frozenset()` — `run`
  protecting nothing, F-917's plain defect restored on the adoption path —
  survived all 217 tests of the branch. A door held shut by a rule in another
  module, with nothing asserting either, is not closed.

So `run` now asks `reap_guard.spared_pids` over the SAME entries dict it
classified (one read, threaded through, so a record re-written in between cannot
make the two disagree), and two pins cover it — one on the OUTCOME and one on
the SET, because on this tree no outcome can witness the set: F-922's scope rule
means the protected value does not change what is killed, measured identical for
`frozenset()` and for the correct set. The set pin is what kills both mutations.

## 5. Verification

Same harness after the fix: rows 1, 2 and 5 answer `[]`, `[]` and `[7777]`; the
disposable control and the recorded-pid control are unchanged, and the
second-door table in §4.2 goes `[6666, 7777]` → `[7777]`. 22/22 in the pin file
and 3641 passed with 1 skipped across the whole non-integration suite; ruff
format + check, vulture, suppression owners and file budgets clean.

## 6. What this costs — the owner's accepted trade

**A leaked Chrome on a NAMED profile whose record entry was lost is never
reaped automatically.** Before this change the directory scan would eventually
find and end it; now nothing will. It stays visible in `stealthy profiles`, and
the owner clears it deliberately.

That is the trade the owner chose, and it is not to be engineered away. The
alternative is the harm this finding exists for: the same scan, unable to tell a
leaked browser of ours from the one the operator is logged into, ending both.

Two narrower consequences, stated rather than discovered later:

* **The close path is narrowed too** (§3), so `close_instance` on a named
  profile now ends only the instance's own recorded browser. If the recorded pid
  is stale while a browser of ours is still on that directory, that browser
  survives the close and becomes the leak above.
* **F-917's surface shrinks to disposable profiles.** Its subtraction is still
  the guard where two entries share one clone directory; on a named profile the
  scope rule gets there first. §4.1 has what that did to its pins.

**Residual, and it has since been FIXED elsewhere in this branch.** What decides
the KIND is `browser_pid_registry.on_persistent_profile`, which answers False for
an entry carrying NEITHER key — so a hand-edited or cross-version record still
gets the directory scan (the audit's A1), and the pin here still states that
shape. What changed is one layer up: F-916's named residual (its §7) makes such
an entry `UNDECIDED` during startup RECOVERY, so recovery no longer reaches this
scan for it at all. The scan itself is unchanged and the pin below is unchanged
with it — a direct call still scans the directory — which is the distinction
worth keeping: this finding narrowed the SCOPE of a scan, and that one narrowed
who is handed to it.
