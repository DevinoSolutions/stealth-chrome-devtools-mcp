# F-888 — a backend that dies takes its browsers' logins with it

**Severity**: HIGH — a human's logged-in session is destroyed by a backend
restart, a heal, or a crash, with no warning and no way back. Observed three
times in two days on one machine.

**Status**: FIXED on `fix/F888-persistent-profile-reattach`.

---

## 1. The mechanism

A browser lives in the backend process, but the thing a human cares about lives
on disk and in that browser's memory: a logged-in session. When the backend goes
away, four separate pieces of code decide what happens to the Chrome it owned,
and every one of them decided "kill it".

**04:13–04:20 UTC, 2026-09-18.** The sole backend (pid 64836) went unresponsive
under 60 concurrent sessions. A replacement cold-started on the same port. Its
mandatory startup orphan sweep — `process_cleanup.recover_orphans`, handed to
`serve_startup.after_serving` by `activate()` — walked `browser_pids.json`, found
every entry whose recorded owner was the now-dead 64836, and reaped them. Among
them was instance `b52e7f37`, an Amazon Seller Central session a human had logged
into by hand. `process_cleanup.kill_process` names it in the log. Nine hours
later the client saw `Connection closed`, then a fresh backend holding an
unrelated instance.

The sweep was not wrong about ownership. `browser_pid_registry.is_reapable` is
F-808's fix and it is correct: an entry whose owner is not a live backend of ours
is unowned. What was missing is that "unowned" had exactly one consequence, and
the tool had no way to express the other one — *adopt it*.

The same day, two more logins were stranded by the second shape of the same gap.
Backend 173824 is still alive and still owns them — instance `4301d842` (a
RevenueCat login on `C:\stealth-mcp-browser-sessions\sessions\MASTER_CHAT-…`) and
a Seller Central login on a client-supplied absolute dir
(`…\amazon-buy-bot\seller-central-profile`) — but every proxy that could reach
that backend has died. The browsers are running, the record names them, and
nothing can talk to them: `stop` and `restart` both terminate the backend, and
the backend's own shutdown path (`_cleanup_all_tracked`) kills every browser it
tracks on the way out. There was no verb that ended with those logins alive.

F-886 (2.1.9) narrowed one door: a cold start no longer EVICTS a backend that
owns live browsers. It says nothing about a backend that dies for any other
reason, which is every other way this happens.

### The four decisions, before

| Where | What it did to a browser whose backend is gone |
|---|---|
| `process_cleanup._recover_orphaned_processes` | killed it, then asked whether to delete its profile |
| `process_cleanup._cleanup_all_tracked` (atexit / SIGTERM) | killed it |
| `singleton.stop_backend` / `restart_backend` | killed the backend, which ran the above |
| nothing at all | there was no adoption path, and no port recorded to build one on |

### The missing fact

Even with the will to adopt, the tool could not: **nothing persisted the CDP
port.** nodriver assigns it inside `uc.start` (`browser.py:374-375`), keeps it in
`browser.config.port`, and that object dies with the process. `browser_pids.json`
carried the pid, the profile dir and the clone flag — everything except the one
number needed to speak to the browser again. Measured: zero hits for `cdp_port`,
`websocket_url` or `DevToolsActivePort` anywhere under `src/` before this change.

---

## 2. What already existed, and what did not

Ask (1) of this finding was "make a named profile persistent". The first job was
to find out how much of that was already true. It was almost all of it.

`spawn_browser(user_data_dir=<name or absolute path>)` resolves to
`clone_storage.resolve_profile_selection`'s `explicit` role, which sets
`uses_custom_data_dir=True` and `auto_clone=False`. Four guarantees follow, and
all four already held:

| Guarantee | Held in 2.1.9? | Where |
|---|---|---|
| (a) not deleted on `close_instance` | **yes** | `_cleanup_profile_for_metadata` refuses a custom non-clone dir |
| (b) not reclaimed by the clone GC / storage cap | **yes** | `clone_is_auto` needs an explicit `auto_clean: true` marker a named profile never gets |
| (c) not removed by `cleanup --apply` | **yes** | the delete list is gated on `clone_is_auto`; a named profile can only be TRIMMED of regenerable caches, and an absolute path outside the clone root is never even enumerated |
| (b)/(c) for a session dir with **no marker at all** | **yes** | measured below |
| (d) not removed by `kill-orphans` | **yes** | the same guard as (a); the verb kills the browser, which is its purpose, and leaves the directory |

So ask (1) is satisfied by NAMING, DOCUMENTING and PINNING it — there is no
`profile=` alias, no new parameter and no new layout, because adding one would be
a second way to say `user_data_dir`.

**Measured answer for a session dir whose browser is already gone.** The second
stranded login (`C:\stealth-mcp-browser-sessions\sessions\MASTER_CHAT-299040179676`)
has no Chrome left; the directory is all that remains, so "never auto-deleted"
carries the whole guarantee on its own. Every delete in `clone_storage` was read:
there are five `_rmtree_robust` call sites and exactly ONE of them can reach a
session directory — `_trash_clone`, whose targets come from a single selection
gate, `if not entry.is_dir() or not clone_is_auto(entry): continue`. The other
four are the trash purge (only inside the trash dir), the regenerable-cache trim
(subdirectories only, session state preserved), and `_copy_profile_tree`'s clone
refresh (guarded by `_is_relative_to(target, clone_root)` *and*
`_profile_has_running_browser`). So the answer is `clone_is_auto`'s, and it fails
safe in **both** shapes a named session dir can have: with no marker at all it
returns False at `if not marker.exists()`, and with the marker the server writes
for a named profile it returns `bool(data.get("auto_clean", False))` = False.
Pinned both ways in `TestNamedSessionDirIsNeverReclaimed`. A named directory can
be TRIMMED of regenerable caches; it cannot be removed.

What the guarantee did NOT have was a name. The condition
`uses_custom_data_dir is True and not auto_clone` was written out by hand in two
places in `process_cleanup` (the delete guard and the untrack decision) and was
about to be needed in a third. Three copies of one condition is how they come to
disagree, and the disagreement is silent in both directions: a directory spared
from deletion whose entry is dropped anyway is a live Chrome nothing on disk can
find, and a browser spared at shutdown whose directory is then deleted is this
incident with an extra step. It is now
`browser_pid_registry.on_persistent_profile`, and a pin fails if the literal
comes back.

---

## 3. The rule chosen, and the two rejected

### Chosen — **a browser on a persistent profile is handed over, not killed**

One predicate, applied at the two places a browser dies without a client asking:

* **Shutdown** (`_cleanup_all_tracked`): a persistent browser is left running and
  left TRACKED. `stop` and `restart` therefore end with the login alive.
* **Startup recovery** (`_recover_orphaned_processes`): a persistent browser
  whose owner is gone is skipped rather than reaped, and picked up by the
  adopter.

And one adoption, `browser_reattach.run`, driven fire-and-forget from
`app_lifespan` on `clone_storage.spawn_background_sweep`'s precedent, because it
reaches a browser over CDP and nothing about readiness depends on it (F-856's
argument, at the exact code path F-856 was about).

**Where the code lives, and why not in the two obvious files.** The pass takes
the `BrowserManager` and the `ProcessCleanup` as ARGUMENTS and lives beside the
rule it applies, on `spawn_leak.reap_launched_browsers`'s precedent (which takes
its `ProcessCleanup` the same way and reaches the same private helpers). Two
reasons, and the second is not aesthetic. The first is convention 4: the rule,
the endpoint ladder, the door and the pass are one subject, and a pass in another
file is a second place the rule gets asked from. The second is the LOC gate —
`browser_manager.py` and `process_cleanup.py` both sit on grandfathered caps that
ratchet DOWN only, and `tools/check_file_budgets.py`'s remedy for a change that
would grow one is exactly this: extract a leaf. `browser_manager` keeps only the
one thing that cannot move (recording the port `uc.start` assigned — the single
moment that number exists outside this process's memory), and `process_cleanup`
keeps only its two decisions, three lines and an argument. Both caps hold;
`browser_manager`'s ratchets 1493 -> 1492.

**The adoption rule needs four conditions and each one alone refuses**
(`browser_reattach.adoptable`):

1. **The owner is not a live backend of ours** — `is_reapable`, the one ownership
   rule, asked with the same injected witness recovery uses. Two backends driving
   one Chrome is F-886's harm reached from the other side.
2. **The profile is persistent** — `on_persistent_profile`. A disposable
   auto-clone is never adopted; its whole contract is that it dies with its
   browser, and adopting one would keep a throwaway profile alive forever.
3. **The recorded pid is still that Chrome** — alive, create_time within the
   existing recycled-pid tolerance, and a Chromium-family process name.
   Composed from `process_cleanup`'s two existing predicates rather than a third.
4. **A CDP endpoint is recoverable.** Asked HERE, before the reaper is told to
   skip anything: a candidate we can neither adopt nor reap leaks forever.

### The second entry point, and why the first is not enough

A first draft of this fix adopted from the RECORD only, at backend startup, and
argued in §6 that a spawn-time surface would be a second trigger for one rule.
**Live observation of the machine disproved that**, and the reversal is the most
important correction in this finding.

The stranded Seller Central Chrome — pid 115652, on
`…\amazon-buy-bot\seller-central-profile`, `--remote-debugging-port=9223`, owner
backend 173824 gone — **has no entry in `browser_pids.json` at all.** The
successor backend (46740) rewrote the record without it. That is not an exotic
case; it is the ordinary consequence of the thing this finding is about: a
backend dies, a replacement starts, and the replacement's record write is the
last word. `run` iterates entries, so for that browser it iterates nothing and
walks past it forever. The startup pass could never have recovered the very login
that prompted the work.

It is also why that Chrome SURVIVED the successor's orphan sweep — the sweep
walks the record too, and it was not in it. That accident is not relied on: the
spare rule (persistent + alive) is pinned for the case where the entry IS
present.

So there is a second entry point, `held_by` / `adopt_held_profile`, and the thing
that makes it one rule rather than two is what it asks: the refusal is
`is_reapable`, the identical call `run` makes, and the attach is the identical
`_adopt_one`. What differs is only the WITNESS that finds the browser — the
directory a caller just named, through `profile_lock.profile_hold`, instead of a
record entry — and one policy: **a failure here never reaps.** `run` is startup
recovery, where an unreachable orphan has to end somewhere and the fallback is
2.1.9's reap. Here a client asked for that browser, and killing it because we
could not attach would be this finding's own harm committed by the fix.

**Which process we enter is asked before any of that, and it is not the witness's
answer.** `profile_lock.profile_hold` answers "is this directory held, and by
whom", from `process_cleanup`'s cmdline scan — a SET — so the pid it reports is
whichever member of the holding process TREE iterated first. Measured, one real
Chrome 153 spawn, eleven processes on one profile:

| `--type` | count | carries `--remote-debugging-port` |
|---|---|---|
| *(none — the browser)* | 1 | yes |
| `renderer` | 6 | yes |
| `utility` | 2 | no |
| `gpu-process` | 1 | no |
| `crashpad-handler` | 1 | no |

So adopting the named member is a coin flip with two different wrong faces. When
the witness named a `utility`, the endpoint ladder found no port and the adoption
declined — and declined SILENTLY, because "no port" and "nothing holds this
directory" were the same `None`. When it named a `renderer`, the port is there
and the adoption would have SUCCEEDED onto the wrong process: that pid is stamped
onto `Browser._process_pid`, so `BrowserManager._browser_process_is_alive`
discards the instance the moment that renderer recycles, and `close_instance`
kills a renderer while the browser keeps running. Both faces were observed in the
real-Chrome nodes, as an intermittent failure that looked exactly like machine
capacity and was not.

The rule is `browser_cmdline.browser_process`: among the live processes on that
directory, the browser is **the one with no `--type`**. Nothing else is
consulted — not the parent pid (the browser's own parent is a trampoline that has
already exited) and not the port (a renderer has it too). Where no member
qualifies, the caller is told rather than left with silence.

**The endpoint has three witnesses**, most trusted first
(`cdp_endpoint.endpoint` — `browser_reattach.endpoint` when this was written;
F-916 moved the ladder to its own leaf):

| Witness | Why it is where it is |
|---|---|
| the recorded `cdp_port` | written at track time since this release; it is about THIS instance |
| `--remote-debugging-port` on the pid's command line | what nodriver passed it (`Config.__call__` appends it from `config.port`); definitionally the LIVE process's |
| `<user_data_dir>/DevToolsActivePort`, first line | Chrome's own record of the port it bound — but a file, which outlives its writer |

The last two exist for entries and processes that name no port themselves. 2.1.8
and 2.1.9 recorded none at all, and those are precisely the browsers carrying the
stranded logins, so an adoption path that only read the new field would have
fixed the next incident and not this one.

**The order of the last two is measured, not assumed.** The stranded Seller
Central Chrome has **no `DevToolsActivePort` file in its profile** while running,
so a ladder that asked the file first would have found nothing to attach to for
the one browser this finding exists to recover. The command line cannot be stale
in that way: it belongs to the process we just proved is holding the directory.
The file keeps its rung for the one case the command line cannot answer — a
caller passing `--remote-debugging-port=0`, where the argv says `0` (which
`valid_port` rejects as "not bound yet") and the file holds the port Chrome
actually resolved it to.

**The door is one door.** Setting BOTH `host` and `port` on a nodriver `Config`
is what makes `uc.start` connect instead of spawn (`browser.py:371-375`:
`connect_existing = True`, and `create_subprocess_exec` is behind
`if not connect_existing`). `desktop_launch.launch_and_attach` was already
standing in that door for F-810. Rather than write a second one, the two lines
moved to the new `cdp_attach` leaf and `desktop_launch` became its second
consumer. A pin reads BOTH consumers' source and fails if `config.host =` or
`uc.start(` comes back into either.

### The claim: what makes any of this safe between PROCESSES

The rule refuses a browser a live backend of ours owns. That refusal was, at
first, a CLASSIFICATION followed much later by an ownership stamp — the
`track_browser_process` write after a successful attach. Between those two moments
the record still names the dead owner, so every other backend on the machine reads
it, reaches the identical verdict, and attaches too. An `asyncio` lock cannot help:
the racers are PROCESSES. Two backends driving one Chrome is exactly F-886's harm,
reached from the other side and by a fix meant to prevent it.

So the check and the stamp happen together, in ONE `update_entries` mutate, which
holds the record's own file lock across its whole read-modify-write
(`browser_pid_registry.claim_browser`). Both entry points take it, BEFORE a single
byte reaches Chrome, which is also the answer to "two concurrent spawns onto one
held directory": the first claim lands, the second reads a LIVE owner — us — and
is `Refused`. A failed attach hands the claim back (`release_claim`), because a
record naming us as the owner of a browser we do not hold is worse than the state
we found: the next backend would read a live owner and refuse to adopt a browser
nobody is driving.

Two decisions inside it are deliberate, and both are the opposite of the obvious:

* **Keyed on the PID, never on the instance id.** The id is not stable across the
  two entry points — a browser with no record entry is proposed under a freshly
  minted one — so two backends racing for one Chrome would claim two different ids
  and both succeed. The pid is the browser.
* **The decision made inside the mutate is RETURNED, not re-read afterwards.**
  "Stamp, re-read, proceed only if the stamp that stuck is ours" is the intuitive
  shape and it is strictly weaker: once the lock is released a third backend may
  legitimately claim something ELSE and rewrite the file, and our own successful
  claim would then read as a failure. The authoritative moment is inside the lock,
  so that is the moment the answer comes from.

In-process, a per-DIRECTORY `asyncio.Lock` keeps the common case cheap — two
spawns naming one directory do not both walk the psutil scan and the door — but it
is not what makes this safe, and it is per directory rather than module-wide so an
unrelated spawn never waits out a wedged browser's whole `ATTACH_BUDGET_SECONDS`.

### Rejected — **a `--keep-browsers` flag on `stop` / `restart`**

The obvious shape, and it is a second shutdown policy. Two flags' worth of
behaviour where one predicate already answers the question, an operator who has
to know which verb to type to not lose a login, and a default that is still
wrong. The persistence of a profile is a property the CALLER already declared
when they passed `user_data_dir`; asking them to re-declare it at shutdown is
asking the same question twice and accepting two answers.

### Rejected — **keep reaping, and restore the login from the profile on disk**

Tempting, because the profile genuinely survives (§2 (a)-(d)) and a later
`spawn_browser(user_data_dir=…)` would reopen it. It does not hold: a Chrome
killed with `TerminateProcess` / `SIGKILL` has not necessarily flushed its
session, session cookies are by definition not on disk, and the human's open tabs
and in-page state are gone regardless. The profile surviving is what makes the
FALLBACK acceptable (§6), not what makes the reap acceptable.

---

## 4. Blast radius

**What changed for a disposable clone: nothing.** Every condition that spares a
browser requires `on_persistent_profile`, which an auto-clone fails. It is still
killed at shutdown, still reaped at startup, and its directory is still deleted.

**What changed for `kill-orphans --force`: nothing.** `force` skips the
classification entirely. An operator asking IS the authority the adoption rule
otherwise supplies — the same argument `backend_eviction` makes for its ungated
act.

**What changed for `close_instance`: nothing.** The spare is about SHUTDOWN, not
about a close. A client that asks for a browser to close still gets it closed.

**What a client sees after a restart.** The proxy heals to a new backend, which
means a new MCP session: the client re-initializes, as it already did. Its
`instance_id` is unchanged — adoption registers under the RECORDED id — so a
tool call carrying an id from before the restart reaches the same browser.
`list_instances` reports the adopted instance like any other, with its LIVE url
and title read through `tab_identity` (F-874), never the cached
`last_navigated_*` pair, which for an adopted instance would be empty.

**Cost of a wedged browser.** One `ATTACH_BUDGET_SECONDS` (15 s), once, on a
background task, after which that entry is reaped exactly as 2.1.9 would have
reaped it. It cannot delay a serve and it cannot cost another instance anything:
the adoption pass takes its OWN lock, not `BrowserManager._lock`, which every
tool body takes for a dict read.

**Record schema.** `cdp_port` joins `browser_pids.json`'s entry, built by
`new_entry` and copied by `normalize_entries` in the same commit — the two halves
the module docstring says must never drift. An entry without the key reads as
`None`, which is "ask the other two witnesses", not a lie.

---

## 5. Tests

`tests/test_browser_reattach.py`, 91 pins, hermetic — the record is a `tmp_path`
file on every one, both liveness witnesses are injected, the `ProcessCleanup` the
pass writes through is a double, and the CDP door is patched. The one thing NOT
stubbed out is the claim: it writes to that real `tmp_path` record, because a
claim whose file lock is faked proves nothing about two processes.

| Class | What it pins |
|---|---|
| `TestPersistenceGuarantees` | the four guarantees of §2, each at the function that could break it, plus the other side of (a): a clone IS still deleted |
| `TestOnePersistencePredicate` | one condition, one home; a legacy entry is not persistent; the literal is gone from `process_cleanup` |
| `TestAdoptionRule` | each of the four conditions refuses alone; a malformed entry does not lose the others |
| `TestEndpointLadder` | recorded port wins; the two legacy fallbacks; `0` / out-of-range / a hand-edited `true` are all "no port"; both command-line spellings |
| `TestRecoverySparesAdoptable` | adoptable is neither killed nor forgotten; a plain orphan is still reaped; both verdicts in one pass; `force` takes everything |
| `TestShutdownHandsOver` | a persistent browser survives shutdown WITH its entry; a clone does not |
| `TestManagerAdoption` | the client keeps its instance id and gets the live page; ownership moves through the one write; a failed attach falls back to the reap with the directory spared; no tab is refused; an already-registered instance is left alone; a wedged attach is bounded; `app_lifespan` hands the pass BOTH collaborators |
| `TestHeldProfileAdoption` | **a holder with NO entry at all is adoptable** (the incident's exact shape); the port comes from the holder's command line; **the BROWSER is adopted when the witness names a child**; a dead owner's entry donates its instance id; a live backend's browser is never taken; nothing holding the dir is a silent None, while a tree with no browser process and a holder with no recoverable port each decline with a NAMED reason |
| `TestTheCrossProcessClaim` | the claim is taken before the door and released on failure; keyed on the pid; a second backend reading a live owner is `Refused`; the decision is the one made inside the mutate; a claim never flips a disposable entry to persistent; the budget expiring AFTER the claim and WHILE it is still landing both still release it |
| `TestALostClaimIsNeverAReap` | two backends racing one dead-owner browser: exactly one adopts, the other SPARES and the entry survives with the winner's stamp; a record write that raises spares too; a browser-level failure still reaps; `Undecided` is one class with `Refused`, so one handler covers both |
| `TestIgnoredArgsNamesOnlyWhatTheCallerPassed` | `ignored_spawn_args` reports only arguments whose value differs from the tool's own default — a `sandbox` the caller really passed IS named, the one this handler resolves for them is not |
| `TestWhatTheCommandLineSays` | the port is joined to the profile (the REFUSAL in both path flavors on every platform, the MATCH in the running platform's own); **the owner witness is blind to which BUILD the owner runs**, so F-889's `_adoptable_identity` can never be folded into `_is_our_backend` and make a newer live sibling's browsers adoptable; headless is measured; a dead loopback proxy is reported and a live one is not |
| `TestAnAdoptedInstanceTellsTheTruth` | measured headless and window size, `not_restored`, `ignored_spawn_args`, hooks and interception on the adopted tab |
| `TestHeldAdoptionNeverReaps` | a failed attach on the spawn path spawns instead and **kills nothing** — no reap, no record drop; a successful one answers with the instance id and stamps `reattached` |
| `TestNamedSessionDirIsNeverReclaimed` | fact (b)/(c) for both shapes a named session dir has on disk: no marker, and the server's own `auto_clean: false` marker |
| `TestOneDoor` | the config carries host, port and the profile; `desktop_launch` uses that one door |

`tests/fakes.py` grows `FakeBrowser(main_tab=…)` — nodriver's `Browser.main_tab`,
what an attach hands back. It is a PROPERTY that raises `IndexError` when
unseeded, because nodriver's is `sorted(self.targets, …)[0]` and never returns
None; a fake that answered None would pin a shape production cannot produce.
`FakeTab` grows `disconnect()` and a `disconnected` flag, deliberately NOT named
`aclose` — see §3.

**And three real-Chrome nodes**, `tests/test_e2e_persistent_profile_reattach.py`
(marked `integration`), because every pin above patches the CDP door and so none
of them can prove the one thing the incident was about: that a second `uc.start`
against a port the first backend's Chrome is listening on CONNECTS to that
renderer rather than launching a new browser.

1. **The record path.** Spawns a real Chrome on a real named profile, writes
   `window.__f888` into the live page, drops the manager's handle WITHOUT killing
   the process (what a `TerminateProcess`d backend leaves behind), hands a fresh
   manager the record, and asserts the adopted instance keeps its id and
   `window.__f888` is still set. That assertion is a claim about the RENDERER,
   not the profile directory — a re-spawn onto the same `user_data_dir` would
   pass a cookie check and fail this one. Also pins `reattached: true`, the
   recorded port, the live `current_url` from `tab_identity` (F-874), and that a
   second pass adopts nothing because ownership moved.
2. **The holder path, with an EMPTY record** — the real stranded shape. Same
   setup, but the record is emptied rather than seeded, and the recovery is a
   plain `spawn_browser(user_data_dir=…)` through the tool. The port is
   recovered from the live Chrome's real command line through the real ladder.
   It asserts `reattached: true`, that `window.__f888_held` is still readable
   (same renderer), and that **F-871's `<name>-2` sibling directory was never
   created** — i.e. the spawn reached the browser instead of walking away from
   it. It also asserts the adopted pid is the BROWSER process (no `--type`).
3. **Two managers coexisting**, the second CONSTRUCTED BEFORE the first drops
   its handle — what F-886 made the ordinary case, where no restart happens at
   all and backend A simply dies while B is already up. It proves the
   cross-process claim end to end: the record starts empty and ends naming this
   process as the owner of that browser's pid, and a THIRD take-over of the same
   browser is refused.

Each node waits for its own Chrome to actually EXIT before the next one launches
(`_released`). Without that barrier the file raced itself — `close_instance`
offloads its teardown, so three real Chromes launched over the top of three dying
ones, and nodriver's fixed ≈2.75 s connect deadline lost. That failure presented
as "Failed to connect to browser" plus a retry onto a different directory, i.e.
as an adoption that was never attempted, and it is exactly the shape a genuinely
overloaded machine produces — which is why the process table was probed rather
than the diagnosis assumed.

**Measured, not asserted**: all three nodes PASS on the development machine
(Windows 11, Chrome 153, 2026-09-19), three consecutive whole-file runs, with the
Chrome process count returning to its ~130 baseline afterwards. The owner in node 1's record is this process's own pid
behind a patched `is_reapable` rather than a fabricated dead pid, because a
fabricated one can be recycled onto a live process between the write and the read
and the resulting flake would look exactly like a genuine adoption refusal.

---

## 6. Residuals

**Profile matching is case-SENSITIVE on macOS, and that is pre-existing.** Joining
a port to a profile compares through `browser_pid_registry.normalize_path`, which
is `os.path.normcase(os.path.normpath(...))` — the RUNNING platform's flavor,
which is right, because both sides of every real comparison come from one machine:
the record this backend wrote and the argv of a process running beside it. But
`posixpath.normcase` is the identity, so on macOS — where APFS is case-insensitive
by default — `/Users/x/Profile` and `/Users/x/profile` are one directory that this
comparator calls two. Not introduced here (the function normalises the record on
the way IN and predates F-888) and not fixed here for the same reason: changing it
changes the stored shape of every entry on that platform. Making it flavor-aware
by SHAPE was considered when PR #135's gate went red on all five POSIX cells and
rejected outright — a backslash is a legal character in a POSIX filename and
`C:foo` is a legal POSIX relative path, so sniffing would corrupt real entries to
serve a cross-flavor case that cannot occur on one machine.

**A persistent browser we cannot attach to is still killed.** The fallback for a
failed adoption is `reap_recorded`, i.e. exactly what 2.1.9 did to that entry, so
this is not a regression — but it is not the ideal either: the alternative is an
unbounded orphan leak, and a Chrome nothing can reach is indistinguishable from
one nothing will ever reach. What makes it acceptable is §2: the reap is handed
the two keys that spare the DIRECTORY, so the profile and its on-disk cookies are
still there for the next `spawn_browser(user_data_dir=…)`. What it costs is the
session cookies and the open tabs.

**Recovering the two stranded logins needs the operator to stop backend 173824
first.** The adoption rule refuses a browser whose owner is a LIVE backend of
ours, and it must — that is F-886's harm. The recipe is in RUNBOOK ("recover a
stranded login"): stop that backend, then start a session, and the new backend
adopts them. On Windows this already works with the 2.1.9 backend that is running
today, because `psutil.terminate()` is `TerminateProcess`, which runs no handler
— so 2.1.9's `_cleanup_all_tracked` never fires and the browsers survive the stop
by accident. On POSIX a 2.1.9 backend still kills them on the way out; the fix
must be INSTALLED before the stop for that platform to behave.

**There is no `profile=` parameter.** `user_data_dir` already IS the
persistent-profile option, and a second spelling of it would be a second way to
say one thing. It is DOCUMENTED as that option instead.

**The record pass is driven from `app_lifespan`, which is once per process
(`_LIFESPAN_STARTED`), not once per heal — and the spawn path only half closes
that.** A backend that adopts nothing at startup, because the browsers' owner was
still alive then, never re-runs the record walk. Since the spawn-time entry point
exists, a caller who NAMES the profile still reaches that browser; a caller who
does not — one addressing it by the `instance_id` it held before the restart —
does not, and gets `InstanceNotFoundError` until some later backend's startup pass
picks it up. A periodic re-check would close it. Deliberately not added: the two
triggers there are both answer a question a caller actually asked (a backend
starting, a client naming a directory), and a timer asking on nobody's behalf
would attach to browsers no session wants.

**The spared population is unbounded, and the idle reaper is the place that could
mirror this defect.** Nothing now reaps a persistent-profile browser whose backend
is gone, so a machine that spawns named profiles and loses backends accumulates
Chromes until `kill-orphans --force` or a human closes them. That is the intended
trade — they are logins — but it is a real cost, and the same shape would return
by the other door if `BrowserManager`'s idle timeout ever closed an ADOPTED
instance: the close path deletes nothing for a named profile, so the login would
survive on disk, but the live session would not. Not touched here; named so the
next change to the reaper knows the question exists.

**An adopted browser whose egress proxy died is adopted anyway, and stamped.**
An authenticated `proxy=` spawn points Chrome at a forwarder living INSIDE the
backend, so the forwarder dies with it while the launch arg lives on: every page
load then fails at a closed loopback port. The alternative — refusing adoption —
was rejected because on the record path a refusal routes to the reap, which turns
a recoverable login into a kill, i.e. this finding's own harm reached by the fix.
So `browser_cmdline.dead_local_proxy` connect-probes it and the diagnostics carry
`dead_egress_proxy` plus a WARNING. The browser is reachable, closable and
re-spawnable with the same `proxy=`; what it is not is usable as it stands.

**Per-instance state that lived in the dead backend is not restored, and says
so.** `extra_headers`, `timezone_id`, `user_agent` and `proxy` were applied at
launch or over CDP by a process that has exited, and none can be read back off a
running browser. They are listed in `spawn_diagnostics["not_restored"]` rather
than silently re-asserted as the caller's spawn arguments, which is what the
adopted instance's `headless`/`viewport` used to do — those two are now MEASURED
(the holder's command line, and `window_sizing.measure`, which reads without
resizing a human's window). `block_resources` and the dynamic-hook interception
ARE re-established, exactly as a spawn does.

**Adoption is asymmetric with backend adoption on display context.** A backend
only adopts a recorded BACKEND whose display context it could use
(`adoption_candidates`); a recorded BROWSER is adopted regardless of which desktop
it was launched on. That is deliberate — a browser is reached over a loopback
socket, not through a window server, and the incident's browser belongs to
whichever backend is alive — but it means a headed browser on `win-session-2`
can be driven by a backend on `win-session-1`, which can see its tabs and not its
window. No verb here creates that situation; only a session change can.

**`uses_custom_data_dir` is the load-bearing half of the persistence predicate
and it comes from nodriver's config readback.** An entry written by 2.0.3 as a
bare int, or hand-edited to drop the key, reads as NOT persistent — which is the
safe direction here (a temp profile reclaimed, rather than a named profile kept
alive by an adoption that should never have happened) and is pinned as such. It
does mean a genuinely named profile recorded by 2.0.3 is not adoptable; those
records predate the port field anyway, so there is nothing to adopt them with.

**Standalone stdio still closes everything.** `app_lifespan`'s non-HTTP teardown
calls `browser_manager.close_all()`, which is a CLOSE per instance and so is not
covered by the shutdown spare. That path is the 1.x single-process contract and
the backend does not run on it; left alone deliberately rather than given a
fourth opinion about what a shutdown means.
