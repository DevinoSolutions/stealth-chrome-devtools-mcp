# F-925 — the seed refresh destroys the seed before rebuilding it

**Status:** fixed (`clone_storage._copy_profile_tree` now delegates the
replacement to a new `profile_copy.replace_tree`)
**Found by:** the lead's read of `clone_storage.py:491-499` on `origin/main`
`fe5cef6`, during the hunt for the sixth mechanism behind the owner's reported
login loss
**Severity:** data loss. The directory every new session is copied from can be
left empty or half-written, permanently, and every session created afterwards
comes up logged out.

---

## 1. The code

`src/stealth_chrome_devtools_mcp/embedded/clone_storage.py`, `_copy_profile_tree`:

```python
if target.exists():
    if _profile_has_running_browser(target):
        return TARGET_IN_USE
    profile_copy.rmtree_robust(target)      # the seed is DELETED
target.mkdir(parents=True, exist_ok=True)
profile_copy.copy_delta(source, target)     # ~101 MB, seconds
time.sleep(0.2)
profile_copy.copy_delta(source, target)
profile_seed.write_marker(...)              # marker written LAST
```

Destroy-then-rebuild, in place, non-atomically. The target here is
`<session root>/master-snapshot` — the **seed**, the directory
`resolve_profile_selection` copies every new session from. For the whole of
that copy the seed is absent, then partial, and it carries no marker until the
last statement. A process death anywhere inside the window — a crash,
`stealthy stop`, an F-886 eviction, a reboot, a machine losing power — leaves
it in whatever state it had reached.

## 2. Why a window becomes permanent loss

Three facts compose, and the third is the one that matters:

1. **The window is entered constantly.** `_refresh_master_snapshot_if_safe` is
   called on every OPEN of the `default` session (`clone_storage.py:912`,
   `before-default-open`), on every CLOSE of it
   (`tool_sections/browser_management.py:777-781`, F-910's `seed_refreshed`
   report), and before a clone whenever the seed is stale
   (`_refresh_snapshot_if_stale`). The live seed's marker on the owner's
   machine is dated today.
2. **The repair is refused exactly when it is needed.** A gutted seed is
   repaired only by another refresh, and `_refresh_master_snapshot_if_safe`
   returns `default-in-use` without copying anything while the shared browser
   is open — which is the normal state of the machine this product is for.
3. **An empty directory passes every consumer's test.** Both readers ask
   `snapshot.exists()` (`clone_storage.py:938` selecting the seed as a copy
   source, and `:983` in `_fallback_profile_selection`). An empty directory
   exists. So the seed is not detected as broken, is not rebuilt, and is
   copied from — silently — for every session created from then on.

`profile_seed.needs_refresh` does not rescue it either: it returns True for a
seed with no marker, but that only matters if something asks, and the ask
(`_refresh_snapshot_if_stale`) runs on the clone path and then hits the same
`default-in-use` refusal.

## 3. The RED, measured

`tests/test_seed_refresh_atomicity.py` interrupts the copy at the real seam —
`profile_copy.copy_delta`, patched at the module attribute, which is the same
object whether it is called from `clone_storage` (as it was) or from inside
`profile_copy.replace_tree` (as it is now). The probe records what a process
that vanished at that instant would have left behind. On `fe5cef6`:

| pin | result on the unfixed code |
|---|---|
| the seed still holds its logins at the instant of death | `assert None == b'seed-login-jar-the-old-generation'` — the directory exists, the jar is already gone |
| the seed is intact after the failed refresh | `assert None == b'seed-login-...'` — permanent |
| a later session still gets the logins | `assert None == b'seed-login-...'` — **the session spawned afterwards has no cookie jar at all** |
| every copy pass sees a complete seed | `[None, b'master-login-jar-the-new-generation']` — the FIRST pass observes an empty seed |

The last row is worth reading twice: the second pass sees the *new* jar, which
is what proves the hole is real and bounded by the copy rather than by the
whole call.

**An exception is not a process death**, and the distinction is load-bearing
here because every `finally` still runs for one. `TestARealProcessDeath` runs a
refresh in a CHILD interpreter and `os._exit(9)`s it at the first copy pass —
no `finally`, no `atexit`, no handler, which is what a crash, `stealthy stop`,
an eviction or a power cut actually do. On the unfixed code it fails with the
same `assert None == b'seed-login-...'`: a killed interpreter leaves the seed
gutted. It asserts a tombstone (the child really reached the copy), the exit
code (it really died un-unwound) and that a staging copy was left behind —
which is the positive proof that no cleanup ran, and is also the one leftover
`_discard_stale_staging` exists for.

## 4. The fix, and the shape that was rejected

`profile_copy.replace_tree` builds the copy in a **sibling staging directory**,
stamps the marker **into it**, moves the old tree aside, and publishes the new
one with a single rename. The target is therefore only ever the old tree or the
new one.

**Why build-beside and not a trash hop.** The two candidates were (1) remove
the window and (2) make the loss recoverable by replacing `rmtree_robust` with
the existing `_trash_clone`. They are not equivalent: (2) leaves the seed
absent or partial for the entire copy exactly as before, so every session
created during that window is still silently logged out — it only lets an
operator who knows to look carry the old copy back by hand afterwards. (1)
removes the window. (1) was taken.

**Why the displaced seed is kept as ONE generation and not put in `.trash`.**
The lead asked for the trash to be considered as belt and braces; it was, and
it is not the right holding area here, for three reasons found by measurement
rather than taste:

* **The purge never reaches it.** `_purge_expired_trash` is only ever called
  with `clone_root_dir()` (`_enforce_clone_storage_cap_in:302`), and the seed
  is not in the clone root — it is `<session root>/master-snapshot`, a
  *sibling* of `<session root>/sessions`. A trash hop would land it in
  `<session root>/.trash`, which nothing sweeps: ~101 MB leaked per refresh,
  for ever.
* **Redirecting it into the clone root's trash bounds it only in theory.**
  That trash is purged from `run_storage_sweep`, which is kicked by
  `spawn_background_sweep("pre-clone")` — a call that lives on the CLONE branch
  only (`clone_storage.py:932`). Someone who only ever opens `default` never
  clones, so never sweeps, and accumulates one seed per open *and* per close.
* **A displaced seed is never the only copy.** That asymmetry is the real
  argument. `.trash` exists because an evicted clone is the only copy of that
  session. A displaced seed is a *staler* copy of the shared profile, which is
  still on disk and *newer*. What it genuinely insures against is narrow — a
  copy that skipped a file Chrome held open (`profile_copy.copy_file` answers a
  locked file by skipping it, and cannot say which) — and the newest previous
  generation covers exactly that.

So one generation, `master-snapshot.stealth-previous`, replaced on each
successful refresh. The cost is flat and stated: **~2× the seed steady-state
(~202 MB here) and ~3× transiently (~303 MB) at the moment the old previous
generation is removed.** That is not counted against
`STEALTH_MCP_CLONE_STORAGE_CAP_GB`, because the seed is outside the clone root
— bounded by construction rather than by policy.

**Why the marker rides into the staging copy.** Stamping after the swap would
leave a complete profile with no marker if the process died between the two.
An unmarked seed reads as provenance `unknown` for ever and `needs_refresh`
True — this finding's own shape, one step smaller. Pinned
(`test_a_death_between_the_copy_and_the_marker_publishes_nothing`).

**Why the scratch siblings are invisible to the clone-root scans.** A staged
copy carries a marker, so `clone_is_auto` is true of it. Without the skip a
storage sweep could select a half-built staging copy as an eviction victim
*while it is being written*, and a displaced generation would inflate the
session-cap total until real profiles were over-trimmed. Both scans skip them
by name exactly as they skip `.trash`, and a pin asserts the skip is narrow
enough that an ordinary session is still selected.

## 5. The pins, and what each one's RED is worth

22 pins in `tests/test_seed_refresh_atomicity.py`, all hermetic — no Chrome, no
socket, no process. A mutation probe restored eight halves of the defect in the
production source, clearing every `__pycache__` verifiably between runs
(`assert left == 0`, since a same-length edit is invisible to `.pyc`
validation). The first pass caught **7 of 8**; the miss is recorded here
because it is the useful part:

> **M8 — a failed `_displace` reported as success** left every pin green. The
> "the old tree could not be moved out of the way" branch was argued in two
> docstrings and had no behavioural consequence anything observed. It is a real
> gap: the mutated code publishes the new copy over a target it failed to
> displace, which is this finding again by another route.

Closed by `TestAFailedDisplaceChangesNothing`, driven through the real path —
a previous generation that is still present and cannot be removed (the Windows
lock `rmtree_robust` exists to tolerate) makes the rename onto it fail, which
is exactly how that branch is reached in production. All 8 mutations are RED
now.

## 6. What this finding does NOT close

* **The one-rename gap, and it composes with F-926.** Between
  `target.replace(previous)` and `staging.replace(target)` the seed PATH does
  not exist. It is two renames apart — microseconds — and nothing is lost (both
  complete trees are on disk, as `*.stealth-previous` and the staging copy),
  but a reader in that instant sees no seed and falls through to
  `resolve_profile_selection`'s `elif master.exists()` branch, which copies the
  LIVE shared profile and carries zero cookies. That branch is **F-926**, filed
  alongside this one. A single atomic directory swap does not exist on either
  platform, so closing this fully means an exchange primitive or a lock, not a
  reordering.
* **There is still no lock around a seed refresh.** Two can overlap — a close
  runs one on a worker thread while a spawn runs one on the event loop — and
  that was true before this change. Each is now individually atomic and each
  publishes a valid tree, so the outcome is a valid seed either way; what can
  be wrong is the *previous generation*, which may be the other refresh's copy
  rather than the true predecessor. Not closed.
* **`_discard_stale_staging` is keyed on age.** A build that genuinely took
  longer than `STALE_STAGING_SECONDS` (1 h, ~1000× a measured ~101 MB copy)
  could be reclaimed by a concurrent refresh. Chosen over ownership because the
  pid in the name belongs to a process that is gone.
* **A staging-name collision is safe for the bytes and NOT safe for the
  leftovers.** The name is `<target>.stealth-staging-<pid>-<seq>`, and neither
  half is unique across processes: a pid is reused, and `_STAGING_SEQ` is
  module-level, so a fresh process's first refresh of a given target reaches
  for exactly the name a dead process's first refresh of that target used. If
  that process was killed after `staging.mkdir`, its tree is still there
  (`replace_tree`'s `finally` does not run through `os._exit` or a kill) and
  younger than `_discard_stale_staging`'s hour, so `mkdir(exist_ok=True)`
  succeeds onto it and `copy_delta` copies INTO a dead process's partial work.
  Written down because a reader will ask and the reassuring answer — "pids do
  not collide" — is not the true one.
  **The bytes are fine, by construction.** `copy_file` is `shutil.copy2`, which
  writes content and only then stamps the mtime, so a file the dead process was
  killed inside is SHORTER than its source and `copy_delta`'s size test
  re-copies it; a file it finished but had not stamped is byte-correct already,
  so skipping it loses nothing whatever its mtime says. Each of the two places
  it can die is covered by one of the two tests, and no wrong byte survives
  either.
  **What is NOT covered is the other direction**: `copy_delta` walks the SOURCE,
  so a file present in the stale staging tree and absent from today's source is
  never removed and is published by the rename. A shared profile gains and
  rewrites files far more often than it drops one, so what this can carry is a
  stale artefact and not a wrong login — but it is a real residual and pruning
  the staging tree against the source is what would close it.
* **`stealthy profiles` lists the clone root unfiltered**, so a transient
  staging directory can appear in it. Pre-existing shape — `.trash` already
  does — and cosmetic; not touched here.
* **A copy is still not verified.** `copy_file` skips a locked file with a
  warning and `replace_tree` publishes whatever it built. This finding makes
  the *previous* generation survivable; it does not make the new one
  checkable. That is the same gap `profile_source.seed_source` refuses a live
  source over, and it is the reason the previous generation is kept at all.
* **F-927**, filed alongside: `_clone_dir_is_protected` is an in-process set
  that `process_cleanup._cleanup_profile_dir` never consults, so a deferred
  delete can `rmtree` a directory a new spawn is copying into. Adjacent to this
  one — same subsystem, same class of harm — and untouched here.

## 7. Verification

* `tests/test_seed_refresh_atomicity.py` — 22 passed.
* Adjacent suites, unchanged and green (329 passed): `test_profile_resolution`,
  `test_profile_seed_truth`, `test_clone_trash_recovery`, `test_clone_storage`,
  `test_clone_sweep_race`, `test_clone_storage_cap`, `test_close_instance_offload`,
  `test_seed_refresh_after_close`, `test_operator_fence`,
  `test_concurrent_spawn_collision`, `test_seed_from_session`,
  `test_cookie_handoff`, `test_clone_legacy_marker_classification`.
* `ruff check` / `ruff format --check` clean; `ty check src` 151 diagnostics
  before and after (none added); `tools/check_file_budgets.py` clean.
* LOC: `clone_storage.py` 993 → 998 (two signature-echo docstring blocks
  collapsed — the plan_F856 payment mechanism — rather than a raised cap, which
  ratchets DOWN only); `profile_copy.py` 309 → 451.
* No new `STEALTH_MCP_*` knob, no `typing.Any`, no `os.environ` read.
