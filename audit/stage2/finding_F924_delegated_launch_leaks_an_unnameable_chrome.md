# F-924 — a delegated (F-810) headed launch can leave a live Chrome nothing can name

**Severity: moderate.** It strands a visible, untracked Chrome on the user's desktop until
the next backend start. No data is lost and no browser of the operator's is killed — the
harm F-919 was about is the opposite direction — but the stranded browser holds a profile,
so it can make a later named spawn refuse or walk, and nothing in any tool's answer says it
is there.

**Successor to F-919, and its §6 is the statement of the gap.** F-919 replaced
`spawn_leak`'s directory-plus-time fence with an identity fence and accepted, explicitly,
that the delegated path loses coverage. This finding is that loss written down as its own
question, with the fix that closes most of it.

**Read, not run.** Everything below is read off `desktop_launch.py`,
`browser_manager.py` and `spawn_leak.py` in the worktree
`.claude/worktrees/f919-spawn-leak-fence` at branch tip `f88caec`. No delegated launch was
driven, no `schtasks` task was created, no Chrome was launched and nothing was killed. The
line numbers were re-derived from the files, not copied from F-919.

---

## 1. The mechanism

A headed spawn on a backend that cannot show windows itself is handed to the logged-on
desktop through Task Scheduler (`desktop_launch.should_delegate`, `:156-161` — `not
headless and not display_context.can_show_windows() and available()`). `launch_and_attach`
writes a launcher script, creates and runs a one-shot task, then waits for the launcher to
drop a pid file and for DevTools to open.

Because that wait can leave by exception with a Chrome already on the desktop, the pid
travels **out-of-band** on a `_Delegated` carrier (`:332-346`), constructed at `:536` and
handed into `_run_task` (`:406-408`). `_run_task` stamps it at exactly one place:

```
:451   pid = await asyncio.to_thread(_read_pid, pid_file)
:452   if pid is not None:
:459       create_time = await asyncio.to_thread(_process_create_time, pid)
:460       if create_time is None:
:461           raise ToolError(...)                      # exits BEFORE the stamp
:466       delegated.pid, delegated.create_time = pid, create_time
```

and the `finally` kills only what was stamped (`:553-561`):

```
:560   if not attached and delegated.pid is not None:
:561       _kill_delegated(delegated.pid, delegated.create_time)
```

So the delegated path's cleanup is **conditional on having completed the stamp at `:466`**,
and `_kill_delegated` (`:349-388`) then applies its own identity check before killing. Two
gates, both of which can decline with a live Chrome on the desktop.

Meanwhile the F-919 fence cannot reach this launch at all. `spawn_leak.Attempt` is stamped
with the `uc.Config` object **nodriver** was given, and `launched_pid` finds the pid by
walking nodriver's registry for the `Browser` holding that exact object. The delegated
branch never asks nodriver to launch anything — it *attaches* (`cdp_attach.attach`,
`:550`) — so `Attempt.config` stays `None`, `launched_pid` returns `None`, and
`reap_launched_browsers` declines by design.

---

## 2. The three uncovered paths

All three reach `launch_and_attach`'s `finally` with a Chrome running and no kill.

**Path 1 — the deadline expires before the pid file is readable** (`:449-473`).
`PORT_READY_TIMEOUT` is 20.0 s (`:78`), polled every `POLL_INTERVAL` = 0.25 s (`:79`). If
no poll reads a pid, `:466` is never reached, the loop falls out to the raise at `:470`,
and `delegated.pid` is still `None`. A launcher that started Chrome but was slow to write
its pid file leaves that Chrome behind.

**Path 2 — the pid IS read but `_process_create_time` answers `None`** (`:459-465`). The
raise at `:461` happens *before* the stamp at `:466`, so the pid is known to the code and
never recorded. The branch is deliberate and correct for the case it was written for — a
Chrome that handed off to an already-running instance and exited, where the live browser on
that desktop is the USER'S and killing it would be us tidying up with their browser; the
comment at `:453-458` says exactly that.

What it also swallows is a different case. `_process_create_time` catches
**`psutil.Error`** wholesale (`:326-329`) and `AccessDenied` is a subclass, so "our
process, momentarily unreadable" takes the same exit as "gone". The helper's own docstring
narrows it further than its `except` does — "or ``None`` if there is no such process"
(`:320`).

**Path 3 — `_kill_delegated` runs and refuses** (`:369-388`): on a `create_time` mismatch
beyond `PID_IDENTITY_TOLERANCE` = 0.5 s (`:99`, checked at `:371-378`), or on any
`psutil.Error` (`:383-388`). Its third refusal, `create_time is None` (`:362-368`), is
unreachable from this caller, because `:466` assigns both fields together.

**In all three the Chrome is untracked.** `launch_and_attach` raised, so the spawn
pipeline's `_apply_post_launch` never ran and `track_browser_process` was never called
(F-919 §6). The process is in no registry, belongs to no instance, is invisible to
`list_instances`, and ends only at the next backend start's orphan sweep.

### 2.1 How reachable is this

**Not demonstrated, not excluded.** The common slow case is the *opposite* of path 1 — the
pid file lands quickly and it is DevTools that lags — and that case stamps at `:466` and is
covered. Path 2 needs a psutil refusal on our own process. Path 3 needs either a pid
recycled within the launch, or psutil failing at kill time.

The residual is bounded in three ways worth stating, because they are why this is a
follow-up and not a stop-ship: it is **Windows-only** (`should_delegate` requires
`available()` and a HEADED request, `:161`); it needs a launcher that actually started
Chrome *plus* one of the three conditions; and it **ends at the next backend start**
rather than persisting.

What it costs while it lasts is a visible browser the tool will not admit to, holding a
profile. That last part is the part worth watching: `profile_lock` reads a held profile as
held, so a later named spawn onto the same directory meets the held-profile rules rather
than a free directory.

---

## 3. Why this is F-810's question, not F-919's

F-919 §6 already ruled that *restoring* the old coverage is not on the table, and that
ruling stands. The old fence caught all three paths by **guessing**: it killed everything on
the attempt's `--user-data-dir` that started within a second of `launch_started_at`, and on
this path the aperture was far wider than on the direct one — a delegated launch is a
`schtasks` create, a `schtasks` run and then up to 20 s of polling, so every sibling Chrome
that started anywhere in those seconds was inside the window. Restoring that would restore
F-919's defect on the path where it is worst.

Two distinct questions are tangled here and only one of them is a reaping question:

1. *Why does the delegated launcher sometimes fail to leave a readable pid file inside 20 s
   at all?* That is an **F-810 reliability** question about the scheduler round trip, and it
   is not answered here or by the fix below. Path 1 lives entirely inside it.
2. *When the launch fails, can we name the process it started?* That is a fence question,
   it is the same question F-919 answered for the direct path, and paths 2 and 3 are inside
   it.

This finding proposes a fix for (2) only.

---

## 4. The proposed fix — stamp `Attempt` from the pair `desktop_launch` already computes

**Not implemented here.** This section is the design and its argument.

The information the fence needs already exists on this path: `:466` computes a `(pid,
create_time)` pair, and `:451` has the pid one branch earlier. `Attempt` is already
threaded into the delegated branch — `browser_manager._launch_browser` takes it (`:470`)
and the delegated branch is the first thing in its body (`:484-488`) — so nothing new has
to be plumbed through the orchestrator.

The shape:

* Give `Attempt` a way to carry a **directly stamped pid** alongside `config`, so
  `launched_pid` has a second source. `config` answers "which `Browser` did nodriver
  register for us"; the new field answers "which pid did we launch by hand". Both are the
  same question — *which process did THIS attempt start* — which is why they belong on the
  one handle and not on a second one.
* Stamp it **where `_Delegated` is stamped, and one branch earlier than that**: at `:451`'s
  non-`None` read, before the create-time gate, rather than only at `:466`. A stamp at
  `:466` alone reaches path 3 only.
* Leave `_kill_delegated` and its gates exactly as they are. This is a second, independent
  reaper reached from the spawn pipeline's own teardown, not a replacement for the
  `finally`'s best-effort kill — and where both fire, `reap_launched_browsers` finds the pid
  already gone and declines.

Which paths that reaches, stated without rounding up:

| path | pid known to the code | reachable by the stamp |
|---|---|---|
| 1 — deadline expired, no pid ever read | no | **no** |
| 2 — pid read, create_time `None` | yes (`:451`) | **yes**, with the stamp at `:451` |
| 3 — `_kill_delegated` refused | yes (`:466`) | **yes** |

Path 1 is closed by a guess or not at all, and a guess is what F-919 removed. It stays
open, and it is question (1) of §3.

### 4.1 Why reusing F-919's fence beats reinstating a time window

**The fence's second witness supplies the identity the delegated path could not read.**
`reap_launched_browsers` kills a pid only when
`process_cleanup._get_browser_pids_for_profile` still lists it as a Chromium-family process
on the directory we launched on. On path 2 that is a *better* answer than the one
`_process_create_time` failed to give, and it is better in the specific case the current
code gets wrong:

* A Chrome that **handed off and exited** — the case `:453-458` is written for — is not a
  running process at all, so it is not on the directory and the fence declines. The
  behaviour the tree already argues is correct is preserved, and preserved *by the rule*
  rather than by an `except` that cannot tell why it failed.
* A Chrome of ours that is **live but momentarily unreadable** IS on the directory, so the
  fence reaps it. That is the case `except psutil.Error` currently swallows.

So the ambiguity that makes path 2 a defect — `AccessDenied` and "no such process" leaving
by the same door — stops being load-bearing. It is not resolved by widening or narrowing
the `except`; it is resolved by asking a question that has an answer.

**And it is strictly better than what F-860 had**, for the reason F-919 §1 measured: a
window says "something started near when we did", which on a shared profile is also true of
somebody else's browser, and on this path the window is seconds wide. An identity says
"this is the process we started". The fence also keeps F-919's direction — an answer that
cannot be established resolves toward **not** killing — so a psutil failure inside the
witness costs a leak, never a stranger's browser.

**What it does not buy.** If psutil refuses on our process, it may well refuse for the
cmdline scan too, in which case the witness cannot confirm the directory and the fence
declines — a leak, the same as today, arrived at deliberately. This is a reduction in how
often the gap is reachable, not a proof that it is closed. Path 1 is untouched.

### 4.2 Alternatives considered and why they are worse

* **Narrow `_process_create_time`'s `except` to exclude `AccessDenied`.** Cheaper, and it
  does distinguish the two cases at the point of confusion — but it puts a second
  recycled-pid/liveness rule inside a helper whose docstring already describes a narrower
  job, and it still leaves the kill gated on the `finally` alone. It is a reasonable
  *addition* to the fix above; it is not a substitute, because it does not reach path 3.
* **Reinstate a time fence for the delegated branch only.** Rejected in §3: it is F-919's
  defect at a wider aperture.
* **Track the delegated Chrome before attaching**, so the ordinary orphan reaper owns it.
  This moves a failed launch's process into a registry that describes live instances, and
  the cleanup ordering that follows is a larger change than the gap justifies.

---

## 5. What would pin it

Hermetic, on the existing doubles — `desktop_launch`'s `schtasks` seam and psutil are
already faked in the suite, and nothing here needs Chrome:

* the stamp survives a `_process_create_time` of `None` (path 2), and the reap then kills
  that pid **only** when the fake process table lists it on the attempt's directory;
* a handed-off pid — stamped, then absent from the table — is **not** killed;
* a `_kill_delegated` refusal (path 3) still leaves a pid the reap can reach;
* path 1 (no pid ever read) reaps nothing and says so;
* a stranger's Chrome on the same directory is never touched in any of the above — the
  F-919 pin, extended onto this path.

---

## 6. Relationship to the other open items

* **F-919 §6** is where this gap is recorded as accepted; this file is the successor it
  names. The `spawn_leak.Attempt` docstring and `CLAUDE.md`'s `spawn_leak.py` row both point
  here.
* **F-918 / the master-profile audit's D3** — `process_cleanup._kill_process_by_pid` still
  terminates a pid whose `.name()` could not be read. That is one layer below this fix and
  is reached by every caller including the reap proposed here; it is not addressed by F-924.
* **F-810** owns question (1) of §3, the launcher's own reliability, which no stamp closes.
