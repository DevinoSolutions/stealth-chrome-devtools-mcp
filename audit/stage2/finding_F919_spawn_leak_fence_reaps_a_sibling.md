# F-919 — `spawn_leak`'s one-second fence reaps a sibling spawn's browser

**Severity: top.** It terminates a browser holding a human's login, in precisely the
profile-singleton case `spawn_leak.py`'s own docstring promised to spare. Re-entering
those logins is manual 2FA/CAPTCHA work no agent can do.

Source read and measured at `origin/main` = `a3d22b3` ("Merge PR #158, F-910"), in the
worktree `.claude/worktrees/f919-spawn-leak-fence`. No Chrome was launched for any
measurement, no real process was killed, and nothing outside `%TEMP%` was written.

---

## 1. The defect, measured

### 1.1 The rule that shipped

`embedded/spawn_leak.py:45,70-73` (at `a3d22b3`):

```python
_CLOCK_TOLERANCE_SECONDS = 1.0
...
for pid in sorted(candidates):
    if not _started_after(pid, since - _CLOCK_TOLERANCE_SECONDS):
        continue
```

`candidates` is every Chromium-family process whose `--user-data-dir` equals the failed
attempt's (`process_cleanup._get_browser_pids_for_profile`). `since` is
`browser_manager.spawn_browser`'s `launch_started_at = time.time()`, stamped one line
before the launch. So the reap set is **"everything on this directory that started at or
after `since − 1.0 s`"** — an approximation of "this attempt launched it", and the only
thing standing between the reap and somebody else's browser.

The safety argument is stated in the module docstring (`:19-22`) and is the whole
justification for killing anything at all:

> only a process that started at or after the attempt began is ours to kill. An
> explicit `user_data_dir` may name a profile a REAL Chrome already holds (its singleton
> is then exactly why our launch failed); **that process predates the attempt and must
> survive**.

F-860 §1.4 spelled out why it believed master was safe by construction: *"master is only
selected when nothing holds it, and a concurrent caller that saw the just-launched Chrome
would have cloned, never opened master."*

### 1.2 Why the premise is false

Profile selection happens **before** either attempt launches, so neither concurrent
caller can see the other's Chrome. F-834 measured the consequence and it is by design:
concurrent unnamed spawns **all** select the shared profile (memory:
`concurrent-unnamed-spawns-all-pick-master`, "3-of-3 master is by design"). Two callers
therefore launch onto one `--user-data-dir`; one wins Chrome's process singleton and the
other fails *because of it* — the exact case §1.1 promises to spare — and the loser's
teardown then reaps by directory and start time.

### 1.3 How close the two launches actually land — measured

`M1` drives the REAL orchestrator: two concurrent `spawn_browser` calls with the launch
phase doubled to record `time.time()` (what `launch_started_at` is) and then raise, as
nodriver does on a connect failure. Nothing is launched. 10 rounds, this machine
(Windows 11, Python 3.13, `f919_measure.py`):

| measurement | n | min | median | max |
|---|---|---|---|---|
| **M1** separation of two concurrent spawns' `launch_started_at` | 10 | **9.9 ms** | 11.4 ms | **86.4 ms** |
| **M2** separation of two concurrent `asyncio.create_subprocess_exec` `create_time`s (nodriver's own launch call, `core/browser.py:397`) | 10 | 3.1 ms | 3.9 ms | 7.5 ms |

M1 raw (ms): `86.39, 11.11, 11.74, 10.88, 10.35, 12.20, 11.71, 10.57, 11.71, 9.90`.
M2 raw (ms): `7.51, 4.39, 3.56, 3.91, 3.21, 4.02, 4.43, 3.97, 3.39, 3.13`.

The loser's window reaches **1.0 s** behind its own stamp. The winner's Chrome sits at
worst **86.4 ms** behind it — i.e. **11×** inside the window, and 100× inside it at the
median.

### 1.4 The reap set does include the sibling — measured against the shipped code

`f919_red.py` loads `spawn_leak.py` **as it stands on `origin/main`** and calls the real
`reap_launched_browsers` over a fake process table holding two browsers on one directory:
the winner at `t`, and the loser's own at `t + sep`. Fake psutil only; nothing real dies.

| separation | reaped | sibling killed | winner `terminate()` calls |
|---|---|---|---|
| 9.9 ms (M1 min) | `[5001, 5002]` | **yes** | 1 |
| 11.4 ms (M1 median) | `[5001, 5002]` | **yes** | 1 |
| 86.4 ms (M1 max) | `[5001, 5002]` | **yes** | 1 |
| 500 ms | `[5001, 5002]` | **yes** | 1 |
| 999 ms | `[5001, 5002]` | **yes** | 1 |
| 1001 ms | `[5002]` | no | 0 |

The product's own durable line names the winner as a leak while doing it:

```
spawn_leak.reap: Failed spawn loser-instance left browser pid 5001 running on
  ...\master; killing it (F-860)
```

So the sibling is spared only past 1001 ms — **11× further out than the worst separation
two concurrent spawns were measured at.** Every measured separation kills it.

### 1.5 Why shrinking the tolerance is not a fix

The constant's own comment (`:41-44`) states what it is for:

> psutil's create_time and time.time() read the same wall clock, but the kernel rounds
> process start times (**10 ms on Linux, ~16 ms on Windows**).

To absorb that rounding the tolerance must be **≥ 10 ms**. To spare the sibling it must
be **< 9.9 ms** (M1's minimum). **No value satisfies both** — the two quantities are the
same size, which is not a coincidence: both are "how long a couple of process launches
take", asked from two directions. A smaller window is the same guess with a smaller blast
radius, and it trades the sibling's browser for F-860's leak at some other separation.

Supporting measurement (`f919_granularity.py`, 40 back-to-back launches): 40 distinct
`create_time` values, **zero ties**, smallest positive delta **2.39 ms**, total span
311.8 ms. That bounds the rounding floor from *above* only — it cannot prove the comment's
10/16 ms wrong — but the contradiction in the paragraph above holds on the constant's own
stated premise either way.

### 1.6 What it costs the operator

On the shared profile the reaped browser is the one the human is logged into (memory:
`master-profile-loss-is-top-severity`; the audit's §1.5 / D5 rows,
`$TEMP\master_profile_audit.md`). The loss is silent from the caller's side: the surviving
spawn's tool call has already returned, and the kill arrives from *another* call's failure
handler. `list_instances` then reports an instance whose browser is gone, and the next
spawn onto that profile is seeded from `master-snapshot`, i.e. logged out.

---

## 2. What is NOT claimed

* **Not measured on real Chrome.** Driving two real concurrent spawns onto the shared
  profile on this machine is the exact collision under test and the owner's logins are in
  it, so it was not run (the dispatch brief forbids it). M1 measures the product's own
  `launch_started_at` stamps through the real orchestrator; M2 measures nodriver's own
  launch call. What is *not* measured is whether real Chrome adds separation between
  `create_subprocess_exec` returning and the browser process appearing — but that would
  have to add **more than 900 ms** to move the verdict, against 3.1-7.5 ms measured for
  the launch itself.
* **No production incident is attributed to this.** The reap's WARNING is on the backend's
  durable channel, but the retained boot log (§0 of the audit) has rolled. This is a
  source-and-measurement finding, not a post-mortem.
* **F-860's leak is real and is not being reverted.** The reap still runs; only its fence
  changes.
* **Not a claim that the delegated (F-810) path leaked.** `desktop_launch.launch_and_attach`
  already kills a Chrome it started but could not attach to.

---

## 3. Root cause

`spawn_leak` had no identity for the process it was reaping — only the directory it was
launched on — so "did this attempt launch it" had to be inferred from a timestamp. The
information was never missing: nodriver sets `Browser._process` / `_process_pid` at
`core/browser.py:397-409` and registers the `Browser` at `:412`, both **before** it starts
polling `/json/version`, so the pid exists and is reachable at the moment `start()` raises.
It simply did not reach the teardown, because `_launch_browser` communicates only by
returning and a failed launch returns nothing.

---

## 4. The fix

**ONE home: `spawn_leak.reap_launched_browsers`.** The fence is the launched pid.

1. `spawn_leak.Attempt` — a one-field handle the orchestrator creates *before* the
   fallible launch and passes **into** `_launch_browser`, which stamps the `uc.Config`
   **object** it built onto it before awaiting `uc.start`. Passed in rather than returned
   because the moment it is needed is the moment that call raised (the same rule
   `extract-preserve-cleanup-ownership` states for cleanup-critical locals).
2. `spawn_leak.launched_pid(attempt)` walks nodriver's registry for the `Browser` whose
   `config` **is** that object — identity, never a field match: two concurrent spawns on
   one directory build configs equal in every field and distinct as objects. It reads the
   pid through **`process_exit.browser_pid`**, which already owns "which member of the
   tree is the browser" (`--type=`) and refuses a handle whose `returncode` is set.
3. `reap_launched_browsers` kills that pid and only that pid, and only when
   `process_cleanup._get_browser_pids_for_profile` still lists it on the directory we
   launched it on. `_CLOCK_TOLERANCE_SECONDS`, `_started_after` and `since` are deleted.

**The recycled-pid question is answered more strongly than by a stored pair.** The brief
suggested `browser_pid_registry`'s `(pid, create_time)` stamp. That pair exists because a
*record* is read long after it was written; here the handle is live, and while asyncio has
not collected the child the OS cannot hand its pid to anybody else — so there is no window
to compare across. `process_exit.browser_pid`'s `returncode` guard is exactly that
argument and already carries it in its docstring; reusing it was cheaper and tighter than
re-spelling the pair.

**An answer that cannot be established resolves toward NOT killing** — uniformly with
`profile_lock._browser_pids`, with the deleted `_started_after`, and with the direction
F-886 and F-888 both chose.

Files: `embedded/spawn_leak.py` (rewritten), `embedded/browser_manager.py`
(`_launch_browser` +`attempt`, `_teardown_failed_spawn` +`attempt`, `spawn_browser`'s
local), `tools/check_file_budgets.py` (ratchet 1452 → 1447), `tests/fakes.py`
(`nodriver_registry`, `LaunchedBrowser`), `tests/test_spawn_leak.py`, and the four test
files that double `_launch_browser`.

---

## 5. Verification

RED first: §1.4 is the shipped `reap_launched_browsers`, driven directly, killing the
sibling at all three measured separations.

GREEN, `tests/test_spawn_leak.py` (11 nodes, hermetic, fake psutil table, `pid_file` under
`tmp_path`, no Chrome):

* `test_the_sibling_that_won_the_race_survives` — **the F-919 pin.** Two concurrent
  `spawn_browser` calls onto ONE directory, the winner held *inside* its launch so both
  are genuinely in flight when the loser's teardown runs. Asserts the winner survives
  (`alive_on == [5001]`, `terminate_calls == 0`), the loser's own Chrome is reaped, and
  the winner's own teardown later reaps the winner's own. Nothing in it names a time —
  the separation stopped being a parameter of the answer.
* `test_a_real_failed_nodriver_start_is_resolved_to_its_pid` — drives nodriver's **real**
  `Browser.start` (its own `range(5)`, its own "Failed to connect to browser") over a
  faked subprocess, and asserts `launched_pid` finds pid 7331 through the config the real
  `_launch_browser` stamped. This is what pins `fakes.LaunchedBrowser` against the article
  rather than trusting it (memory: `mocked-fakes-can-encode-the-bug`), and what would
  catch nodriver ceasing to store the caller's config verbatim or ceasing to register
  before it polls.
* `test_a_browser_already_collected_is_not_killed` — a handle with `returncode` set is
  refused, i.e. the recycled-pid guard of §4.
* `test_a_launch_we_cannot_name_is_left_alone` — the cost of §6, pinned: a stranger on the
  directory survives and the process table is not walked.
* `test_a_browser_that_held_the_profile_before_the_attempt_is_spared` — kept, and
  **strengthened**: its `create_time` is now the same instant the attempt runs (it used to
  be `now − 3600`, which the old window could also get right). It is spared because no
  attempt of ours launched it.
* F-860's own nodes are unchanged in intent and still pass: the launched Chrome is killed,
  the launch error is what the caller sees, cancellation reaps, another profile is spared,
  no directory means no scan, a stubborn Chrome does not mask the error.

Suite: 269 passed across `test_spawn_leak`, `test_spawn_exhaustion_hint`,
`test_spawn_headed_requires_display`, `test_browser_connect`,
`test_concurrent_spawn_collision`, `test_extra_headers_cdp`, `test_process_cleanup`,
`test_process_cleanup_import_guard`, `test_browser_reattach`, `test_clone_storage`,
`test_clone_storage_cap`, `test_close_waits_for_chrome`, `test_close_instance_offload`.
Gates: ruff format/check, `ty --exit-zero-on-warning` (exit 0), vulture, suppression
owners, file budgets, pinned imports, `dump_tool_surface.py --check` all clean.

---

## 6. What this costs, and what is left open

**A leaked Chrome we cannot name is left RUNNING.** This is the deliberate direction and
it is not free. The reap declines when: the `Browser` is not in nodriver's registry (the
launch raised before `:412` — a missing executable, a `create_subprocess_exec` failure:
in all of those nothing was launched, so the cost is zero), the handle's `returncode` is
already set (Chrome has exited — again nothing to kill), the pid carries `--type=`, or the
launch was **delegated** (F-810), which leaves `Attempt.config` unstamped. Only the last
of those can leave a real process behind, and `desktop_launch.launch_and_attach` already
kills a Chrome it started but could not attach to, so no known path both leaks and is
unnameable today. If one appears, the leak survives until the next backend start's orphan
reap — which is the state F-860 found and fixed, reached here only for a process we cannot
prove is ours. The alternative is killing on a guess, which is this finding.

**A wrapper-process platform would under-reap.** The fence assumes the pid
`create_subprocess_exec` returns IS the browser process. That holds for `chrome.exe` on
Windows, for the macOS app binary, and for Linux's `google-chrome` wrapper (it `exec`s, so
the pid is preserved). A launcher that FORKED instead would leave `launched_pid` naming a
process that exits immediately; `_get_browser_pids_for_profile` would then not list it and
the reap would decline — under-reaping, never over-reaping. Not measured on a forking
launcher; none is known to exist for Chromium.

**`spawn_leak._started_after` is deleted**, and two documents cite it as the example of
"an unreadable witness must resolve toward NOT killing" — `finding_F870` §1.2 and the
master-profile audit's F-918 proposal. The *direction* is preserved and is now
`launched_pid` returning None; the symbol is not. A reader following those citations lands
on this file's docstring, which says so.

**The INFO line is new noise on a failed spawn.** Every failed spawn that reaps nothing
now writes one `spawn_leak.reap ... nothing was reaped (F-919)` line to the debug ring.
That is deliberate — a post-mortem that finds a stray Chrome on a directory should be able
to see that the attempt *declined to guess* rather than that the reap never ran — but it
is one more line per failure under a spawn storm, where F-870 measured five reaps per run.

**Not addressed here, and adjacent:** `process_cleanup._kill_process_by_pid` still
terminates a pid whose `.name()` could not be read (the audit's D3 / F-918) — the same
"unreadable resolves toward killing" shape, one layer below this fix, reached by every
caller including this one. And `_get_browser_pids_for_profile` is still a full
process-table walk per failed spawn; with the pid in hand it is now only a *witness*, so a
cheaper single-pid check would do — left alone because the walk is also what proves the
process is on our directory, and one witness deleted is one guess added.
