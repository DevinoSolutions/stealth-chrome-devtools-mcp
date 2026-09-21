# F-916 — startup recovery reaped a persistent browser it merely failed to classify

**Status:** fixed (`browser_reattach._adoptable_entry` / `adoptable`, new leaf
`embedded/reap_guard.py`)
**Found by:** `C:\…\Temp\master_profile_audit.md` §1.3 / §5, reading
`process_cleanup.py:594-631` against `browser_reattach.py:218-237`
**Severity:** data loss. It ends a browser holding a human's login, on the one
path whose whole purpose since F-888 is to spare exactly those, and it runs on
**every backend cold start** — `server.py` → `serve_startup.after_serving` →
`ProcessCleanup.recover_orphans`, not only on `kill-orphans`.

---

## 1. The defect in one sentence

`_adoptable_entry` answered a bare `None` for two different statements — "this
entry is not one to adopt" and "we could not establish whether it is" — and
`Classified.spare` is built from the entries that are adoptable plus the ones
that made the pass *raise*. An entry in neither set is handed to
`browser_reattach.reap_recorded` at `process_cleanup.py:631`. So a browser that
is alive, ours, and on a PERSISTENT profile was killed because we could not find
a door into it.

## 2. Measured, before the fix

Driven against `HEAD` (`a3d22b3`) source copied into a scratch tree, with the
record faked and both liveness witnesses injected
(`$TEMP\f916_measure.py`). `REAPED` means "outside `.spare`", which is exactly
what `recover_orphans` acts on:

| entry | verdict before |
|---|---|
| persistent, browser ALIVE, **no `cdp_port`** (the 2.1.8/2.1.9 record shape) | **REAPED** |
| persistent, browser alive, **no `create_time`** | **REAPED** |
| persistent, **`pid` not an int** | **REAPED** |
| disposable auto-clone (control — *should* be reaped) | REAPED |
| Chrome provably gone (control — *should* be reaped) | REAPED |

Five inputs, one answer. `unclassifiable` was `set()` in all five: the
classifier had no way to say "I do not know", so the three at the top were
indistinguishable from the two at the bottom.

The first row is not hypothetical. `browser_reattach`'s own module docstring
records that **2.1.8/2.1.9 recorded no `cdp_port` at all**, and the endpoint
ladder's two live fallbacks — the command line and `DevToolsActivePort` — are
both readable-or-not: a psutil `AccessDenied` on `cmdline()` closes the first,
and the file was measured **absent while the browser ran** on the stranded
Seller Central Chrome (pid 115652, port 9223). That population is precisely the
one the audit names as carrying today's stranded logins.

## 3. Why the old rule said the opposite, and why it was wrong

The pre-fix module docstring argued condition 4 deliberately:

> a candidate we cannot reach must be classified as un-adoptable BEFORE the
> reaper is told to skip it — otherwise a browser we can neither adopt nor reap
> leaks forever.

That is a real trade and the finding reverses it, not by taste but by what the
two errors cost. A leak is a Chrome that keeps running and keeps its entry; the
other error is a login that has to be re-made by hand, through 2FA or a CAPTCHA
no agent may automate. F-888 already made this trade once, in the same file, for
the same reason ("Toward a leak, never toward killing what we did not
understand"); condition 4 was the one place it had not been applied.

## 4. The fix

`reap_guard.UNDECIDED` — a distinct type with one instance, not a second `None`,
because one value standing for two statements *was* the defect. Three of
`_adoptable_entry`'s six returns now answer it, and all three are **after** the
persistence gate:

| condition | answer | why |
|---|---|---|
| a live backend of ours owns it | `None` | a DECISION (F-886's rule) |
| the profile is a disposable auto-clone | `None` | a DECISION |
| `pid`/`user_data_dir` unreadable | `UNDECIDED` | we know nothing about it |
| no `create_time` | `UNDECIDED` | cannot prove whose pid it is |
| `browser_alive` is False | `None` | ESTABLISHED: not the Chrome recorded |
| no CDP endpoint | `UNDECIDED` | alive, ours, and unreachable |

`adoptable()` maps `UNDECIDED` into `unclassifiable`, which `.spare` already
included — so the reaper skips it and the adopter never sees it, which is the
distinction `Classified` was built to keep.

**`browser_alive` deliberately stays a DECISION.** Sparing it too would mean an
entry whose Chrome is gone is spared forever, and `browser_pids.json` would grow
without bound with nothing able to clear it. That predicate does fold a psutil
`AccessDenied` into its `False` (`recorded_browser_alive:312`) — what catches
*that* one is F-918's guard at the kill itself, which refuses a pid it cannot
read. The two findings meet there on purpose: F-916 fixes the layer that knows
about ENTRIES, F-918 the layer that knows about PROCESSES.

### 4.1 What paid for the lines

`browser_reattach.py` was at **999 of its 1000-LOC default** and the fix needed a
third answer in it. Caps ratchet down only, so the cut came first: the **endpoint
ladder** (`endpoint`, `_port_from_profile`, `DEVTOOLS_PORT_FILE` and its
three-witness argument) moved whole into a new leaf, `embedded/cdp_endpoint.py`,
named for the question it answers — "where is the CDP endpoint of the browser
this RECORD ENTRY describes". **999 → 991**; the new leaf is 93 LOC.

**The first attempt put it in `cdp_attach` and that was wrong.** Reviewed and
reverted, and the decisive item is a fact rather than a preference: measured,
`cdp_attach` **never calls** `endpoint` — both call sites are
`browser_reattach`'s two entry points — so the move relocated a function away
from both of its callers into a module that does not use it. The justification
written at the time inverted itself in its own second clause ("both of
`browser_reattach`'s entry points ask for it"). Three more: it collapsed the
WHICH/HOW split the map states deliberately (`browser_reattach` owns WHEN to
knock, `cdp_attach` owns the door); the ladder's three witnesses are all about a
RECORD ENTRY while `cdp_attach`'s subject is a websocket; and — independently
blocking — the move made `cdp_attach` import `browser_cmdline` and
`browser_pid_registry`, so it was no longer a leaf, while the same commit's
CLAUDE.md row still ended "A leaf: `nodriver` … and `debug_logger`". A commit
that rewrites a row and leaves its last sentence false is the documentation
defect the map exists to prevent. `cdp_attach.py` is restored byte-for-byte and
its row with it.

## 5. The pins

`tests/test_recovery_must_not_kill.py::TestUnclassifiableEntryIsSpared`, six
nodes: the three could-not-establish conditions must be in `.spare`, the two
decisions must NOT be (without those two the fix would spare everything and
recovery would never reap again), and `test_recovery_does_not_kill_the_
unreachable_browser` drives the real `recover_orphans` and asserts on the pid
list the kill path received — the harm, not the classification.

RED at `a3d22b3`: 4 of the 6 fail — all four behaviour-RED — and the two that
pass are the controls. The lane total, and why the first number reported for it
was wrong, is `finding_F918_*.md` §4.1.

## 6. Verification, and what this costs

After the fix, same harness, same five inputs: the three could-not-establish
rows are `spared` with `unclassifiable == {'i'}`, the two controls are still
`REAPED`. 20/20 in the pin file — which carries F-922's six as well, that fix
having landed in this same branch — and 3639 passed, 1 skipped across the whole
non-integration suite.

**The cost, named rather than hidden: a browser we can neither adopt nor reap is
left running and left recorded.** That is the leak the old docstring warned
about, accepted deliberately, and it is BOUNDED in a way "forever" is not:

* the entry is re-classified on every later cold start;
* the moment its Chrome actually exits, `browser_alive` goes False, the entry
  becomes an established negative, it is reaped and it leaves the record;
* an operator who wants it gone sooner has `kill-orphans --force`, which skips
  the whole classification by design (`process_cleanup.py:594`).

So the unbounded case is a browser that runs forever — which is a browser the
user is using.

**Residual (not this finding's):** `browser_pid_registry.on_persistent_profile`
reads an entry MISSING both keys as *not* persistent, so a hand-edited or
cross-version record can still reach condition 2 and be reaped as disposable.
That is the audit's A1 and it is latent — no current write path produces that
shape (A2, refuted there) — but it is the one remaining way a persistent entry
answers `None` without a witness having been read.
