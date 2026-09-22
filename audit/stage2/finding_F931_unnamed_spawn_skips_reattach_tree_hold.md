# F-931 — an unnamed spawn could not re-attach, and a dead browser's children still held its profile

**Severity:** High (it refuses the default spawn against a profile nothing is
using, on the owner's own machine and on the release gate).
**Files:** `embedded/tool_sections/browser_management.py`,
`embedded/profile_lock.py`, `embedded/browser_cmdline.py`.
**Depends on:** F-888 (re-attach), F-914/F-915 (a held profile hands over or
refuses), F-910 (`close_instance` waits for the browser's own exit).

---

## 1. Symptom

`spawn_browser()` — no `session`, no `user_data_dir`, the call the owner makes
and every integration test makes — fails with:

```
ToolError: Failed to spawn browser: the 'default' session is open in a browser
this backend does not drive (a live browser process (pid 8084) has this profile
open)
```

about a profile whose browser **this same backend had just closed**, and which
`spawn_browser(session="default")` — the other spelling of the same directory —
would have re-attached to rather than refused.

## 2. Measurement

Release gate run **35689647688**, job `integration (Windows/X64)`
(`$TEMP\gate164_integration_win_attempt1.log`, lines ~430-434):
`tests/test_e2e_execute_script_async.py::test_a_promise_that_never_settles_is_killed_at_timeout_ms`
raised the message above, immediately after the preceding test in the same file
closed its own unnamed spawn on the shared profile.

Both halves are readable at source on 32ff753 (= origin/main, F-914/F-915 in):

**(a)** `browser_management.py:256` binds
`user_data_dir = rt.clone_storage.require_allowed_user_data_dir(...)`, which
answers `None` when nothing was named (`clone_storage.require_allowed_user_data_dir`:
`if not requested: return None`), and `:321` gated the F-888 re-attach on
`if user_data_dir:`. So an unnamed spawn went straight to
`resolve_profile_selection` → `profile_target.hand_over_or_refuse` → the
refusal, while the named spelling of the same directory reached
`browser_reattach.adopt_held_profile` and was adopted.

**(b)** `profile_lock.profile_hold` → `_browser_pids` →
`process_cleanup._get_browser_pids_for_profile`, which matches **every**
Chromium-family process carrying `--user-data-dir=<dir>` and applies no
`--type` filter, and then reported `min(pids)`. `close_instance` Phase 1 pops
`_instances` (so `cookie_handoff.driven_profiles` stops reporting the directory
as driven) and Phase 2b waits on `process_exit.browser_pid`, which **answers
`None` for any `--type=` child by design** (F-910: waiting on a renderer would
report "exited" while the browser was still flushing its cookie store);
`process_exit.terminate` then ends the browser alone. The logic is
platform-neutral; Windows only makes every tree member `chrome.exe`. So for a
window after every close, a surviving child is enough to make the profile read
HELD by a pid nothing drives.

The two compound: (b) manufactures a false holder, (a) removes the one path
that would have looked at it and found nothing to adopt.

### What the gate log does and does not establish

It establishes the REFUSAL, and that it landed on the shared profile one test
after a close of that profile. It does **not** say whether pid 8084 was the
browser or one of its children — no `reattach`, `adopt` or `reattach_declined`
line appears in the run at all. So the log is the *symptom*; both defects are
established by reading the code above, and the SHAPE is pinned hermetically
(`tests/test_profile_lock.py::TestWhichMemberOfTheTreeHolds`,
`test_orphaned_children_of_a_closed_browser_do_not_refuse`) rather than inferred
from it. The hermetic fixtures deliberately do **not** reuse 8084 as a child's
pid, because dressing a fixture in the incident's pid asserts exactly the thing
the log leaves open.

## 3. Root cause

Both are one shape — **a question asked in two places and answered differently.**

`browser_reattach.held_by` has asked `browser_cmdline.browser_process` since
F-888 for exactly this reason ("the pid it names is whichever member of the
holding process TREE its witness iterated first … a renderer or a utility five
times out of six"). `profile_lock.profile_hold` is the *other* consumer of that
same scan and never asked. And the re-attach itself was keyed on the string the
caller typed rather than on the directory the selection would land on, so the
one profile a caller can reach without typing anything was the one profile the
re-attach could not see.

## 4. Fix

**(a) `browser_management.spawn_browser`** asks the re-attach about
`user_data_dir or str(rt.clone_storage.master_profile_dir())` — the directory
the selection *will* land on, since an unnamed spawn selects the shared session
by design (F-834/F-896). `master_profile_dir()` is READ, not re-decided:
`clone_storage` stays the one home for where the shared session lives. The
`if user_data_dir:` gate is gone, so both spellings now take one path.

**(b) `browser_cmdline.browser_members`** (new) answers *which* of a profile's
pids are browsers — the structural rule `browser_process` already had, plus the
third outcome that function cannot report, because it answers one pid or
`None`: whether any member's argv could not be **read**. `browser_process` is
now a two-line reader of it, so there is still one rule.
`profile_lock._tree_hold` consumes it and gives `profile_hold` three answers
where it had one:

| tree | verdict |
|---|---|
| a browser member (or two) | HELD, naming the **browser** and never `min(pids)` |
| every member readable, all `--type=` children | not held — fall through to the lock exactly as an empty scan does |
| any member's argv unreadable | HELD, and the reason **says so** rather than claiming a browser we never saw |

The last row is the direction this codebase takes everywhere (`_pid_alive`,
`_browser_pids`' `None`, `reap_guard.UNDECIDED`, `backend_eviction`): what we
could not establish resolves toward not acting. A pid *missing* from the process
table is a different thing and contributes nothing — the scan and this read are
two moments, and a process that exited between them is an established negative.

### Why `profile_lock` and not `profile_target`

Triage's first option was to narrow `profile_target.hand_over_or_refuse` —
refuse only on a holder `browser_cmdline.browser_process` identifies as the
browser, the way `browser_reattach.held_by` already does. It is one level too
high, for three reasons, and the third is measured:

1. **One home per question.** "Is this directory held, and by whom" is a FACT and
   it is `profile_lock`'s; `profile_target`'s own row says it answers "what do we
   do about it", which is a POLICY. Correcting the fact inside the policy is the
   second-way defect that row warns about, and it leaves `profile_lock` still
   answering wrongly for everyone else.
2. **The pid in the message is composed in `profile_lock`.** `Hold.reason` is
   what F-914's refusal and `walk_reason` quote verbatim, so a fix in
   `profile_target` would still name a renderer as the holder.
3. **`profile_target` is not the only consumer, and the others matter.**
   `clone_storage._profile_has_running_browser` is the same answer as a bool and
   is asked at eight further sites. Two are load-bearing here:
   `_refresh_master_snapshot_if_safe` (`clone_storage.py`:448) answers
   `seed_error: "default-in-use"` and **refreshes nothing** while it reads held —
   so a lingering child would keep the SEED stale after the shared browser
   closed, which is F-914/F-915's own "five and a half hours behind" complaint
   arriving by a second route; and `cli.py`:126 prints `in_use` per profile in
   `stealthy profiles`, telling an operator a closed session is open. Fixing the
   witness fixes all ten sites at once; fixing `profile_target` fixes two.

### Why `profile_lock` and not `close_instance`

Waiting for the whole tree on close was considered and rejected:

1. It closes one door of several. A browser killed by anything other than our
   own close — a crash, `kill-orphans`, the user's Task Manager — leaves the
   same orphan children, and the refusal fires just the same. The witness fix
   closes all of them.
2. It contradicts F-910's own argument at the same file: `process_exit.browser_pid`
   deliberately refuses to wait on a `--type=` child, because a renderer's exit
   is not evidence about the cookie store.
3. It costs close latency (a per-child grace, up to `EXIT_GRACE_SECONDS`) to buy
   a narrower fix.
4. `profile_lock` is where the one home already is, and the fix makes it agree
   with `browser_reattach.held_by` by construction rather than by coincidence.

## 5. Tests

RED on 32ff753, GREEN after (both `uv run pytest` and `python -m pytest`):

`tests/test_profile_lock.py::TestWhichMemberOfTheTreeHolds` — a tree with no
browser member does not hold; the hold names the browser and not the lowest pid;
an unreadable member still reads as held *and says which*; a pid that has since
exited is not a hold; a browser on another profile does not hold this one.
`test_two_browsers_on_one_directory_still_hold_it` is green on both sides — it
guards against over-narrowing, since `browser_process` answers `None` for an
ambiguous tree and reusing that `None` here would call a two-browser directory
free.

`tests/test_held_profile_handoff.py::TestAnUnnamedSpawnAsksTheSameQuestion` —
both spellings ask the re-attach about the shared directory, and both adopt
instead of selecting (parametrized as a PAIR; the `default` cases were already
green, which is the finding).
`TestTheHeldSharedSession::test_orphaned_children_of_a_closed_browser_do_not_refuse`
— the gate's own shape at the surface it refused from.

**SOFT GOLDEN UPDATED**, same commit: `test_live_browser_process_is_a_hold`
handed the scan a bare pid nobody had established anything about and asserted a
hold; the scan's answer is a tree, so the fixture now says which member 4242 is.
It was also latently flaky — 4242 may be a live process on a busy machine.

`tests/test_extra_headers_cdp.py`'s `_CloneStorage` double gains
`master_profile_dir`, on that file's own stated rule: a double that offers less
than the real surface turns a renamed call into a red about something else.

`tests/goldens/tool_surface.json` is **not** regenerated: `spawn_browser`'s
return schema is unchanged. What changed is which of two equal spellings can
re-attach, and an adoption already returned the same five keys as a spawn
(`_adopted_instance_record`, F-888).

## 6. Residuals and cost

1. **One extra process-table walk on the unnamed spawn path** — `profile_hold`
   is now asked twice for it (once by the re-attach, once by the resolver),
   which is exactly what the named path has always paid. Named and unnamed
   spawns now cost the same.
2. **`browser_members` re-reads each matched pid's argv.** The scan that
   produced the pids already read them; this is a second read over the matched
   set only (eleven processes for one real Chrome, measured on Chrome 153), not
   over the process table.
3. **A profile whose browser is gone but whose orphan children linger is now
   reported FREE**, so a spawn will open a new Chrome on it while they are still
   there. That is the intended answer — the children hold no profile — and
   Chrome's own process singleton is the backstop either way: on POSIX the
   `SingletonLock` belongs to the browser and this code falls through to it, and
   on Windows the `lockfile` is the browser's `DELETE_ON_CLOSE` handle, which
   the kernel releases when the browser dies whatever its children do.
4. **A browser whose argv we can never read** (an elevated Chrome under a
   Windows `AccessDenied`) keeps its profile held forever from our side, and the
   refusal now says "could not be read" rather than naming a browser. That is
   the same trade `profile_lock._pid_alive` and `reap_guard` already make, and
   the remedy is the same: close it, or run the backend with the rights to see
   it.
5. **`profile_lock` is no longer a leaf on stdlib+psutil**: it imports
   `browser_cmdline`, itself a pure argv reader with no state and no orchestrator
   of its own. The leaf rule it was written under is about not importing
   `process_cleanup`, which is still true — the SCAN still arrives as an
   argument.
6. Not attempted here: `process_cleanup._get_browser_pids_for_profile` still
   answers with a whole tree, and its other callers (the reap paths) read it
   under `reap_guard`'s rules rather than this one. Unifying them is a separate
   question about killing, not about holding.
