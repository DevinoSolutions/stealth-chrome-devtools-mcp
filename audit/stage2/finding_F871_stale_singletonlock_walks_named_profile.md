# F-871 — a named profile's busy check reads leftover files, not live processes: the caller is silently given a different profile

**Status:** fixed on `fix/F871-stale-singletonlock-profile-walk` (RED pinned, GREEN, hermetic)
**Opened by:** the F-870 write-up, from the release gate of 2.1.5
**Source at:** `main` = `267bac8`
**Severity:** MEDIUM-HIGH. A named profile exists to keep one identity — cookies, logins, consent. The walk to `<name>-2` hands the caller a DIFFERENT identity, freshly cloned from the master snapshot, and nothing in `spawn_browser`'s answer says a substitution happened. The mirror-image half is worse in kind though rarer: a profile a live browser holds could not be seen at all, so a second Chrome could be pointed at it.
**Evidence:** GitHub Actions run `34911829422`, job `104200794999` (`release-gate / integration (Linux/X64)`, the only red cell of that run).

---

## 1. What was observed

`tests/test_browser_integration.py:117-139` warms Chrome up before the first
real test, three bounded attempts, **passing `user_data_dir="ci-warmup"` on
every one of them**. The reap lines F-860 added name the directory each failed
attempt actually ran on (extracted from the job log):

| attempt | spawn id | reaped pids | `--user-data-dir` |
|---|---|---|---|
| 1 | `675b05f5…` | 2738 | `…/stealth-mcp-session-root/sessions/ci-warmup` |
| 2 | `11364fa0…` | 2754, 2768 | `…/sessions/ci-warmup` |
| 3 | `2c8534da…` | 2772 | `…/sessions/**ci-warmup-2**` |

The caller asked for `ci-warmup` three times and the third attempt ran on
`ci-warmup-2`. The same shape was reported in run `34924931290` (attempt 1).
Nothing in the answer carried the substitution: `spawn_diagnostics.
profile_selection.user_data_dir` shows the `-2` path and no field says what was
requested or why it was not honoured.

The walk is nondeterministic across attempts (attempt 2 did NOT walk) for the
reason §2 gives: it depends on whether the kill left a particular `/tmp`
directory behind, which depends on whether the browser got to run its own
teardown.

---

## 2. Root cause

Three code facts, in order.

**(a) "Busy" was a file-existence question.**
`embedded/clone_storage.py:79-99` (at `267bac8`):

```python
    return any(
        (profile_dir / marker).exists()
        for marker in ("SingletonLock", "SingletonSocket", "SingletonCookie")
    )
```

reached whenever the process-table scan
(`process_cleanup._get_browser_pids_for_profile`, `process_cleanup.py:264`)
comes back empty. `clone_storage.py:951` is the site that matters for a named
profile:

```python
        if _is_relative_to(explicit, clone_root) and _profile_has_running_browser(
            explicit
        ):
            explicit = _next_available_explicit_dir(explicit)
```

and `_next_available_explicit_dir` (`clone_storage.py:888`) walks `-2`, `-3`, …

**(b) None of those three names means what the check assumed.** Chrome's
process singleton is a claim about a PID, not a file. Measured against Chromium
`main`:

* `chrome/browser/process_singleton_posix.cc::ProcessSingleton::Create` writes
  `SingletonLock` as a **symlink** whose target is
  `base::StringPrintf("%s%c%u", net::GetHostName(), kProcessSingletonLockDelimiter, current_pid_)`
  — the string `<hostname>-<pid>`, which is not a path.
* `chrome/common/process_singleton_lock_posix.cc::ParseProcessSingletonLock`
  reads it with `ReadSymbolicLink` and splits on the **last** `-`.
* `NotifyOtherProcessWithTimeout` then decides: an empty target is "No lockfile
  exists" → `PROCESS_NONE`; an empty hostname is "Invalid lockfile" →
  `UnlinkPath` → `PROCESS_NONE`; `!IsChromeProcess(pid)` is
  **"Orphaned lockfile" → `UnlinkPath` → `PROCESS_NONE`**. Chrome takes a stale
  lock over and starts. Only a foreign hostname yields `PROFILE_IN_USE`.
* `SingletonSocket` is a symlink to `socket_dir_.GetPath()/SingletonSocket`,
  where `socket_dir_` is a per-launch `ScopedTempDir` under `/tmp`;
  `SingletonCookie` is a symlink to a random number.

Consequences for `Path.exists()`, which follows symlinks (measured on this
box, Python 3.13): a dangling `SingletonLock` gives
`exists() == False, is_symlink() == True, lexists() == True`. So

* the check **never saw `SingletonLock` at all** — the one artefact that
  actually names an owner was invisible to it; and
* the check **did** see `SingletonSocket`, because its `/tmp` target is a real
  file — and a browser that is killed rather than asked to quit never runs the
  `ScopedTempDir` destructor, so that target outlives it.

**(c) F-860's reaper kills the pid, and the residue is what the next caller
reads.** `embedded/spawn_leak.py:48-90` kills every browser on the attempt's
`--user-data-dir` that started after the launch began; it touches no files, by
design. So the CI sequence is: attempt N's Chrome is killed → its `/tmp` socket
directory survives → attempt N+1 asks "is `ci-warmup` busy?" → `SingletonSocket`
still resolves → "busy" → `ci-warmup-2`.

**In one sentence:** the busy check asked whether three files exist, but two of
them are residue that a killed browser leaves behind and the third — the only
one that names an owner — is a dangling symlink that `Path.exists()` reports as
absent, so a reaped browser's leftovers walked a named profile to `<name>-2`
while a live browser's lock was invisible.

---

## 3. Blast radius

`_profile_has_running_browser` is the one busy predicate for the whole profile
subsystem; every reader inherited both halves of the error.

| site | with a killed browser's residue (false BUSY) | with a live browser's lock only (false FREE) |
|---|---|---|
| `clone_storage.py:951` named-profile selection | the walk above — a different identity, unannounced | a named profile a live Chrome holds is handed to a second Chrome; the launch fails ("Failed to connect to browser") with nothing explaining it |
| `clone_storage.py:973` master selection | every later spawn clones instead of using `master`, and `_refresh_master_snapshot_if_safe` never runs — the F-860 §1.2 symptom, reachable without a leaked process | `master` opened while a Chrome we cannot see in the process table holds it |
| `clone_storage.py:270`/`:359`/`:513`/`:725` sweep, trim, trash | a dead profile is never reclaimed or trimmed | — |
| `clone_storage.py:874` `_dir_unavailable` (clone naming) | an extra `-<pid>-<n>` clone dir per spawn | — |
| `cli.py:87` `doctor`'s `in_use` column | reports a dead profile as in use | reports a held profile as free |

The false-FREE half was masked in practice by the process-table scan, which
sees any browser this machine's user can enumerate; it is the cases where that
scan cannot (another user, restricted access, a psutil failure that logs and
falls through) that were left with no second witness at all.

Windows is unaffected by the residue half: Chrome there writes `lockfile` with
`FILE_FLAG_DELETE_ON_CLOSE`, which the kernel removes even on a hard kill (F-860
§3.6 measured 0 stale `lockfile`s across 80 idle profiles), and this check never
consulted it.

---

## 4. The fix

One new leaf, `src/stealth_chrome_devtools_mcp/embedded/profile_lock.py` (196
LOC) — **THE one home for "is this Chrome profile held by a live process, and
who holds it"**. `profile_hold(profile_dir, live_pids) -> Hold | None` has two
witnesses and no third:

1. the process table, handed in as an argument so the module stays a leaf;
2. `SingletonLock`, read the way Chromium reads it — `<hostname>-<pid>` split on
   the last delimiter; a lock whose pid is dead is orphaned and holds nothing, a
   lock with no parseable hostname is invalid and holds nothing, a lock from
   another host holds the profile (we cannot show it free from here).

`SingletonSocket`/`SingletonCookie` are no longer consulted at all: Chromium
writes them AFTER the lock, so a live browser always has a lock, and consulting
them is precisely the bug.

`clone_storage` keeps the names its readers use — `_profile_hold` is a two-line
adapter and `_profile_has_running_browser` is that answer as a bool — so no
sweep, trim or CLI site changed.

**The walk now announces itself.** `resolve_profile_selection`'s explicit branch
adds three fields to the selection dict it already returns, and only when a walk
actually happened:

```python
                walk = {
                    "requested_user_data_dir": str(requested),
                    "walked_to": str(explicit),
                    "walk_reason": hold.reason,
                }
```

`_public_profile_selection` copies the dict whole, so they surface at
`spawn_diagnostics.profile_selection` with no second diagnostics home and no new
plumbing in `browser_manager.py` or `tool_sections/browser_management.py`.

### Two things deliberately NOT done

* **The reaper does not delete lock artefacts.** It was the other candidate fix
  and it is strictly weaker: it covers only the locks WE killed, leaving the ones
  a crash, an OOM kill or a reboot left, and Chromium already unlinks an orphaned
  lock on the next launch (§2b) — so deleting one would be a second way to do
  something already done, from a failure handler racing a browser that is still
  dying. Reading correctly covers every stale lock there will ever be.
* **Chromium's `IsChromeProcess` check is not reproduced.** We ask only that the
  lock's pid is alive. A recycled pid therefore costs one walk; the opposite
  error — declaring a live browser's profile free — costs the profile. The error
  is taken in the survivable direction, and the divergence is stated in the
  module docstring rather than left to be rediscovered.

---

## 5. What the tests pin

`tests/test_profile_lock.py` (13 tests, hermetic: no Chrome is spawned and no
process is inspected beyond the test's own pid).

* The premise: a real `SingletonLock` symlink is `exists() == False`.
* `profile_hold` across the whole decision table — live pid scan, live lock,
  stale lock, foreign-host lock, unparseable lock, residual socket+cookie with
  no lock, a pid scan that raises, a pid scan that is absent.
* Selection level, the two product claims, both RED before the fix:
  `test_residual_socket_reuses_the_named_profile`
  (`assert 'occupied-2' == 'occupied'` — the CI shape) and
  `test_a_live_lock_walks_and_the_answer_says_why`
  (`assert 'occupied' == 'occupied-2'`, plus the three diagnostic fields).

Chrome's artefacts are written by ONE helper pair in `tests/fakes.py`
(`write_singleton` / `held_profile`), as a symlink where the platform allows one
and a plain file otherwise — Windows symlink creation is privileged, and
`profile_lock` reads both forms because the bytes mean the same thing.

### SOFT goldens updated, deliberately

Four tests wrote `(dir / "SingletonLock").write_text("lock")` and called it
"simulates running browser". Under the fix those bytes are what Chromium calls an
INVALID lockfile — it unlinks them and starts — so they never meant "busy"; they
only passed because the old check asked `exists()`. Each now calls
`held_profile(dir)`, which names a live pid, with the reason inline:
`tests/test_profile_resolution.py::TestNextAvailableExplicitDir::test_skips_busy_variants`,
`::TestResolveProfileSelection::test_busy_profile_auto_suffixes`,
`::test_master_busy_clones`, and
`tests/test_clone_sweep_race.py::TestSpawnFlowProtectsClone::test_resolve_profile_selection_protects_the_clone`.

---

## 6. What remains

* **The warmup failures themselves are not this finding.** `ci-warmup` attempts
  1 and 2 failed with nodriver's "Failed to connect to browser" on a directory
  the tool had correctly chosen. F-860 §2 attributes that shape to machine load;
  whether the false-FREE half of §3 contributed on a Linux runner — a second
  attempt pointed at a profile the first attempt's Chrome still held, whose lock
  the old check could not see — is PLAUSIBLE and NOT established here. The fix
  removes that possibility going forward; it does not prove it was the cause.
* **The `-2` directories already on disk** (`ci-warmup-2` on the runners, and any
  `<name>-2` a user's machine accumulated) are not migrated. They are ordinary
  named profiles; nothing reclaims them, and merging them back into `<name>` is
  not a thing this tool can do safely.
* **The walk fields are not yet a `spawn_browser` WARNING.** They ride in
  `spawn_diagnostics`, where the existing named-profile `warning` also lives.
  Whether a substitution deserves louder treatment than a diagnostic field is a
  product call, not a defect.
* **The residue is still on disk after a reap.** By the §4 argument that is
  correct — Chromium cleans it up — but it means `ls` of a reaped profile still
  shows `SingletonSocket`. Nothing reads it any more.
