# F-917 — the reap is DIRECTORY-matched while the spare is INSTANCE-ID-matched

**Status:** fixed (`process_cleanup._kill_processes_for_metadata`,
`reap_guard.spared_pids`)
**Found by:** `C:\…\Temp\master_profile_audit.md` §1.3 note / D2 / §5, reading
`process_cleanup.py:325` against `:624`
**Severity:** data loss, and it is the one that reaches the SHARED profile —
i.e. the master, the profile the owner's own logged-in Chrome holds.

---

## 1. The defect in one sentence

Startup recovery decides what to SKIP by `instance_id`
(`process_cleanup.py:624`, `if instance_id in spare: continue`) and the reap it
then performs decides what to KILL by `user_data_dir`
(`:325`, `_get_browser_pids_for_profile(metadata.get("user_data_dir"))`). Two
entries may legitimately share one directory — that is what a shared profile
*is* — so one stale entry's reap kills the browser another entry has just
been spared for.

## 2. Measured, before the fix

Two entries on one directory, driven through the real `recover_orphans`
(`$TEMP\f916_measure.py`, HEAD = `a3d22b3`):

* `i-live` — pid 7777, persistent, `cdp_port` present, Chrome alive → classified
  **adoptable**, therefore in `.spare`;
* `i-stale` — pid 7778 on the SAME directory, Chrome gone → reaped.

The directory scan answers `{7777}`. Result:

```
i-live is adoptable and was SPARED by instance id; kills issued: [('i-stale', 7777)]
record after: ['i-live']
```

`_kill_process_by_pid` was called **with the spared browser's own pid**, charged
to the entry that did not own it. And the record afterwards still lists
`i-live` — so the record says that browser is alive and adoptable while its
process has just been ended. The next backend's pass will try to attach to it,
fail, and reap it: the entry is cleaned up one generation after the login is
already gone.

Note what does NOT save it. The recovery start-time fence at `:329-347` only
spares processes that started *after* `self._init_time` — a browser the operator
has had open since before the backend started is exactly the case it lets
through, and it is exactly the case that holds a login.

## 3. Why not fix it the other way

Making the spare directory-matched instead would be wrong, not merely different:
two entries sharing a directory is legitimate (a re-tracked instance, the master
held by more than one recorded browser across versions), so a directory-wide
spare would refuse to reap entries that genuinely should go, and the record
would never shrink. The asymmetry has to be resolved by carrying the spare's
PIDS to the kill set, not by widening the spare.

Scoping the kill to the recorded pid alone was the other option the audit
offered. It was not taken: the directory scan exists because a recorded pid can
be stale while a browser on that profile is not, and removing it would trade
this defect for a leak on every entry whose pid was recycled.

## 4. The fix

`reap_guard.spared_pids(entries, spare)` — the bridge between the two matchings,
in the leaf that owns the rule rather than in either of the two files that were
disagreeing. `_kill_processes_for_metadata` grows a `protected_pids` parameter
and subtracts it at **the one place the kill set is finally spent**, just before
the loop:

```python
pids_to_kill = set(pids_to_kill) - protected_pids
```

One subtraction rather than one per source, so none of the three ways a pid
enters that set (the directory scan, the recovery fallback pid, the non-recovery
fallback pid) can grow a second answer to "is this one protected".

Two callers fill it:

* `recover_orphans` passes `reap_guard.spared_pids(saved_processes, spare)` —
  the same `spare` the loop consults one line earlier, from the same single
  classification pass, so the reaper and the spare cannot drift;
* `browser_reattach.run`'s **failed-adoption reap** passes the other candidates'
  pids (`candidate_pids - {candidate.pid}`). That path has the same shape from
  the other door: `run` reaps an entry it could not attach to, by directory, and
  a sibling candidate on that same profile was in the set it was iterating. The
  comment at `run`'s `Refused` branch already described this race for the
  cross-BACKEND case; this is the intra-pass case.

## 5. The pins

`tests/test_recovery_must_not_kill.py::TestReapDoesNotCrossTheSpare`, three
nodes:

* `test_stale_entry_does_not_kill_a_spared_siblings_browser` — the harm, through
  the real `recover_orphans`. It deliberately lets the **start-time fence pass**
  (the patched `create_time` predates `_init_time`), because that fence is what
  hides this on a machine where the pid happens to be absent, and a pin it
  satisfies would read green without the fix;
* `test_protected_pids_are_filtered_from_every_kill_path` — the subtraction;
* `test_an_unprotected_pid_on_the_directory_is_still_killed` — that it is a
  subtraction and not a switch.

RED at `a3d22b3`: all three — but only the FIRST is behaviour-RED. The other two
fail with `TypeError: ProcessCleanup._kill_processes_for_metadata() got an
unexpected keyword argument 'protected_pids'`, which is evidence that the
parameter does not exist yet: a statement about an API, not about a kill. So the
harm is pinned RED exactly once, by
`test_stale_entry_does_not_kill_a_spared_siblings_browser`, and that is the node
to re-run if this fix is ever questioned. The lane total is `finding_F918_*.md`
§4.1.

## 6. Verification, and what this costs

Same harness after the fix: `kills issued: []`, `i-live` still recorded and
still running, `i-stale` dropped. 22/22 in the pin file, and 3641 passed with 1
skipped across the whole non-integration suite.

**The cost:** a browser that a spared entry names is now never reaped by a
sibling entry's pass, even when that browser really is an orphan of the sibling.
The population is exactly "pids a spared entry records", so the leak is bounded
by the spare itself — and every member of it is, by construction, a browser we
either intend to adopt or could not classify. `kill-orphans --force` sets
`spare` to the empty set and therefore `protected_pids` to empty too, so the
operator's override is unaffected.

F-922 also closes this defect through a SECOND door that the filter above
cannot reach — `browser_reattach.run`'s failed-adoption reap, whose
`candidate_pids` names only ADOPTABLE entries and so never names one F-916
spared. The measurement and why the scope rule rather than a second subtraction
is what closes it are in `finding_F922_*.md` §4.2.

**The residual this finding named is now RESOLVED — by F-922, in this same
branch.** It read: a browser on that directory with no record entry at all — the
owner's own Chrome, started by hand — is still in the directory scan's answer
and is still killed, and nothing here can spare it because nothing in the record
names it. That needed the owner's ruling rather than a patch, and the ruling
came: the directory scan is for DISPOSABLE profiles only; on a named/persistent
profile a reap may end only what the RECORD names. See
`finding_F922_named_profile_reaped_by_directory.md`.

**What that did to THIS finding's surface, and to two of its pins.** The
subtraction now guards only the disposable case — where two entries can still
share one clone directory — because on a named profile the scope rule gets there
first. The two unit pins were left VACUOUS by it: they ran on a persistent entry
and, measured, answered `[]` with AND without `protected_pids`. Both moved to a
disposable entry and each now asserts BOTH directions, so neither can rot that
way again; F-922 §4.1 carries the detail. The end-to-end pin stays on the shared
persistent profile — it is the harm as reported, and two guards on the owner's
logged-in Chrome is the right number.
