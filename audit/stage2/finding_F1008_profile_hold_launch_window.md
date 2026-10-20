# F-1008 — `profile_hold` reads a profile FREE while our own browser is starting on it, and three lock reads resolve toward FREE

**Severity:** High (data-loss class: master-profile loss is the owner's top severity). Pre-existing on main; found
during F-998's data-loss review (PR #236), tracked as GetItDone T-3022.
**Files:** `embedded/profile_lock.py` (witness 3 `launching`/`_launch_hold`, `_lock_content`, `_lock_owner`,
`_pid_alive`, `_kernel_has`, and from review `launch_reaped`/`_remove_marker`), `embedded/browser_manager.py`
(`_launch_browser` runs the start inside `profile_lock.launching`), `embedded/spawn_leak.py` (the reap ends a failed
launch's marker), `embedded/browser_cmdline.py` and `embedded/profile_copy.py` (docstrings),
`tools/check_file_budgets.py` (`browser_manager.py` holds at 1438, paid), `NAVMAP.md`, `CHANGELOG.md`. Tests: `tests/test_profile_hold_launch_window.py` (new).
**Branch:** `fix/F1008-profile-hold-launch-window`, from `origin/main` (b767977).

---

## 1. The defect

`profile_lock.profile_hold` is the one answer to "is this profile held". It had two witnesses, both Chrome's: the
process scan (a browser whose argv names this `--user-data-dir`) and `SingletonLock` (`<hostname>-<pid>`, read as
Chrome reads it). In the first part of a launch neither exists:

* the scan matches a command line, and on Linux `/usr/bin/chromium` and Google's launcher are `#!/bin/sh` scripts
  that `exec` the real binary under the same pid; across that `execve` the pid reads an EMPTY argv (F-998 measured
  this in 11 of 40 launches under CPU load);
* Chrome writes `SingletonLock` 0.11-0.57 s after that.

So the profile read FREE. F-998's review measured 4 FREE polls of production `profile_hold` inside one launch. Every
consumer that acts on FREE can then act on a profile a browser is starting on: a second spawn of the same session (two
browsers, one profile), `_copy_profile_tree` onto it, the seed refresh copying a `default` that is mid-launch into the
snapshot every clone comes from, the named-profile trim (`_named_profiles_over_session_cap` has no in-flight
exemption at all, unlike the auto-clone sweep's `clone_protection`), and `remove_saved_tabs`.

## 2. The fix: witness 3, our own launch marker

`profile_lock.launching(profile_dir)` is a context manager. `BrowserManager._launch_browser` runs both starts — nodriver's
and the delegated headed launch (F-810) — inside it, after `remove_saved_tabs` (which must still see the profile free).
It writes `stealth-launch.lock` in the profile holding `<hostname>-<backend pid>`, and removes it when the launch
returns. A launch that raised keeps it until its Chrome is reaped (§6). `profile_hold` reads it after `SingletonLock`, with the same
`_lock_hold` code, so there is still one reader of "whose lock is this and is that pid alive".

Why it is not a second way to answer "is this profile held": the answer still comes from `profile_hold` alone. The
marker is one more witness inside that function, read the same way as Chrome's own lock. `clone_protection` is a
different question (which clone dirs the storage sweep must skip, for the whole life of an instance, in this process
only) and is unchanged.

Properties, each pinned:

* **Visible across processes.** It is a file, so a sweep or spawn in another backend (another display context or
  identity sharing the session root) or the CLI sees it too. An in-memory registry would not.
* **Names no pid.** Its pid is the launcher, a backend, never a browser. `Hold.pid` is `None` so
  `browser_reattach.held_by` (which adopts the pid it is given), `profile_target.live_master_port` and
  `browser_health.holder_of` are never handed a backend's pid as a browser's.
* **Never readable as empty.** It is written to `stealth-launch.lock.<pid>.tmp` and renamed into place. An empty
  marker would read as no marker.
* **Never copied.** The `.lock` and `.tmp` suffixes are already in `profile_copy.ignore_names`. A copy taken from a
  profile mid-relaunch therefore cannot hand the new session a marker that holds it.
* **Refcounted in-process.** Two spawns in one backend write the same content, so the first to finish does not remove
  the marker under the second.
* **Only ours is removed.** If another backend overwrote it meanwhile, it is left for that backend.
* **Cannot outlive a launch.** A marker whose backend has exited reads as orphaned, as Chrome's lock does. One older
  than `spawn_budget.LAUNCH_BUDGET_SECONDS` (120 s, the bound `_launch_browser` already runs under) is dropped, so a
  backend killed mid-launch whose pid is then recycled holds the profile for at most one launch's length.
* **A directory that does not exist yet is skipped**, because there is nothing on disk to lose and creating it early
  would change what `_clone_seed` reads as a brand-new install.
* **Never raises.** A marker that cannot be written costs this protection and logs a warning, but it does not fail
  the spawn.

When the block ends the browser is visible to Chrome's own witnesses. nodriver returns only once Chrome answers on
its debugging port, which Chrome opens after it has taken its process singleton, and by then its argv is readable.

## 3. The lock-read edges (F-998 review)

| Edge | Before | Now | Direction |
|---|---|---|---|
| `SingletonLock` exists but `lstat`/`readlink` raises (`EACCES`) | `Path.is_symlink` raised into the handler (3.11/3.12) or answered False (3.13), and both returned `""`, read as **no lock: FREE** | one `lstat`; only `FileNotFoundError`/`NotADirectoryError` mean absent; any other `OSError` is **HELD**, pid `None`, "could not be read" | toward HELD |
| Lock owner hidden by `/proc` `hidepid=2` (another user's Chrome) | `psutil.pid_exists` reads `/proc` and answered False, so the lock read as **orphaned: FREE** | a pid psutil does not list is asked of the kernel with `kill(pid, 0)`: `EPERM` means alive, `ESRCH` means gone (POSIX only, because `os.kill` terminates on Windows) | toward HELD |
| Lock pid too large for an `int` | `int()` took it and `psutil` **raised `OverflowError`** out of `profile_hold`, which never raises, so the spawn failed | parsed as Chromium's `StringToInt` does: ASCII digits up to `2**31-1`, anything else is no pid, so an orphaned lock (Chromium unlinks it and starts); `_pid_alive` also catches `OverflowError` as unanswerable, which means HELD | no raise; same verdict as Chrome |
| Lock pid recycled by a non-browser | HELD | HELD, unchanged and documented. Chromium's `IsChromeProcess` check is not copied. The docstring's "costs one walk" is corrected: since F-914 it costs a refusal. A process create-time check against the lock's mtime was considered and rejected, because on Linux psutil derives `create_time` from the CURRENT wall clock and `btime`, so a clock step after launch would make a live holder look newer than its lock, which reads FREE | toward HELD (kept) |
| Dangling `SingletonLock` symlink | read via `is_symlink` + `readlink` | read via `lstat` + `readlink`, unchanged verdict | same |
| Windows `lockfile` | not a witness; presence deliberately ignored | still not a witness, now argued: Chrome opens it `FILE_SHARE_READ` + `DELETE_ON_CLOSE`, so the only open that can tell held from stale must request an access Chrome does not share. For as long as that probe's handle is open, a Chrome starting on the profile fails its own `CreateFile` and exits as "profile in use". Witness 3 covers our own launches on Windows too | unchanged |

## 4. RED proof

`tests/test_profile_hold_launch_window.py` was run against an out-of-tree `git archive` export of main (b767977) with this
branch's test file copied in (`PYTHONPATH=<export>/src;<export>/tests`, `PYTHONDONTWRITEBYTECODE=1`, cwd inside the
export, and `profile_lock.__file__` asserted to resolve inside the export). Mutations of the fix were done by **runtime
attribute rebinding** from a `-p` plugin's `pytest_configure` against an export of this branch, one fresh subprocess
per mutation and no disk writes to any worktree. Counts are in the PR body.

## 5. Residuals (not fixed here, named)

1. **A Chrome started by hand**, or by anything but our launcher, is still invisible during its own launch window.
   Only our launches write the marker.
2. **Selection → launch.** `clone_storage` decides FREE before `_launch_browser` starts, so two spawns of one session
   can both pass selection before either writes the marker. Chrome's own singleton then arbitrates (one browser exits)
   as it did before. The marker narrows the window to "both selected before either launched".
3. **A failed launch's Chrome** that nothing can name (no pid: a delegated launch, a `Browser` missing from nodriver's
   registry) is covered only by `FAILED_LAUNCH_GRACE_SECONDS` (15 s), not by its reap. One named by its pid is covered
   until the reap ends it (§6).
4. **Check-then-act in the sweeps.** A trim that decided FREE before a launch began keeps going. That is inherent in any
   check-then-act and was true of Chrome's own witnesses too.
5. **`default` mid-launch refuses an unnamed spawn's live-jar seed.** While this backend launches `default`, the
   shared profile reads held by a browser it does not yet drive, with no port, so `_resolve_shared`'s
   `shared_live_source` refuses for those seconds, exactly as it does for any `default` held by a browser this
   backend does not drive. That is the wrong-HELD direction, which costs a refusal rather than data.
6. **`process_cleanup._get_browser_pids_for_profile` skips an `AccessDenied` process** (an elevated Chrome on
   Windows) instead of reporting it unreadable. On Windows, where the scan is the only witness, such a browser reads
   FREE. Changing that scan changes what the reapers kill, so it is out of scope here.

## 6. Review round (PR #247): every minor finding fixed here

| # | Finding | Fix | Direction |
|---|---|---|---|
| 1 | `_launch_hold` read an unanswerable marker `lstat` as age 0, so such a marker held the profile for ever | dated by the profile directory's `lstat`, which every claim touches, so it is never older than the newest launch's start; both failing drops it (unreachable: Chrome's lock in the same directory was just read) | bounded HELD |
| 2 | on Windows `unlink` fails while a `profile_hold` reader has the marker open, leaving it to hold until the age bound | `_remove_marker` retries 3 times, 25 ms apart; a marker still left holds until the age bound, and the launch succeeds | toward HELD |
| 3 | a second launch in one backend on one profile did not re-date the marker, so it was dropped at the FIRST launch's start + 120 s, under the second | the count>0 branch of `_claim_launch` re-dates the marker and the directory (`os.utime`, `OSError` suppressed) | was wrong-FREE |
| 4 | stale docs: NAVMAP "two witnesses and no third", `browser_cmdline` "Windows never reaches `_pid_alive`", `profile_copy` "names three" | corrected | docs |
| 5 | a failed or cancelled launch's `finally` removed the marker BEFORE `spawn_browser`'s teardown reaped the Chrome it may have started | a failed launch leaves the marker, dated to expire after `FAILED_LAUNCH_GRACE_SECONDS` (15 s, longer than the reap's 3 + 2 s kill rungs); `spawn_leak.reap_launched_browsers` ends it through `profile_lock.launch_reaped` once the launched pid is killed or is not on the profile. A kill that failed keeps it. `browser_manager.py` is unchanged, cap unchanged | was wrong-FREE |
| 6 | tests: a cancelled launch, a re-dating claim, and the hidepid pin patching the global `os.kill` | added; the hidepid and Windows pins patch the module's own `_signal` alias and name a pid no process has | tests |

Why the reap ends the marker and not the grace alone: `_fallback_profile_selection` re-asks `profile_hold` about a failed
`default` launch AFTER the teardown has reaped it and, if held, turns the retry into a clone. A grace alone would have
made our own dead launch read as a sibling's for 15 s.

**RED and mutations, same method as §4** (out-of-tree `git archive` export of 55448bf with the new test file, runtime
attribute rebinding from a `-p` plugin, no disk writes to any worktree):

* 55448bf: 10 failed, 18 passed. 8 are behavioural REDs of the new pins: the failed-launch pin raised and cancelled, the
  failed-reap pin and the grace pin ("a failed launch's Chrome read as FREE"), both re-dating pins, the unanswerable-age
  pin ("held it for ever") and the unlink-retry pin (`assert 1 == 2`, one try). The other 2 are the hidepid params:
  55448bf has no `_signal` alias, so they are a harness change, not a RED. The pid is one no process has, so the real
  `os.kill` that 55448bf calls there hurt nothing. The unlink-refused-every-time pin passes on 55448bf, as a guard.
* Fixed tip: 28 passed.
* 8 of 8 mutations killed: `age_unanswerable_zero` (1), `single_unlink` (2), `no_redate` (3), `unlink_on_failure`,
  `reap_never_releases`, `grace_is_whole_budget`, `failure_not_dated` and `reap_releases_unkilled` (5).
