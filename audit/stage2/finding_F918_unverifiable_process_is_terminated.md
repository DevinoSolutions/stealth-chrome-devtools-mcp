# F-918 — an UNVERIFIABLE process is terminated

**Status:** fixed (`process_cleanup._kill_process_by_pid`, `reap_guard.killable`)
**Found by:** `C:\…\Temp\master_profile_audit.md` D3 / §5, reading
`process_cleanup.py:860-867`
**Severity:** data loss. It is the last guard before the kill, and it was a
guard that logged and then did the thing anyway.

---

## 1. The defect in one sentence

```python
except psutil.NoSuchProcess:
    return True
except Exception as error:
    debug_logger.log_warning(..., f"Could not verify process {pid}: {error}")
    # <- no return; control falls through to terminate()
```

The name check exists so the reaper never kills a process that is not one of
ours. When the name could not be READ, the check was skipped and the kill went
ahead — on the strength of a record entry alone.

## 2. Measured, before the fix

`$TEMP\f916_measure.py` against `HEAD` = `a3d22b3`, `psutil.Process(pid).name()`
made to raise:

| raised by `.name()` | returned | `terminate()` called |
|---|---|---|
| `psutil.AccessDenied` | `True` | **yes** |
| `OSError` | `True` | **yes** |
| `psutil.ZombieProcess` | `True` | no |

`AccessDenied` is not exotic on Windows: it is what psutil answers for a process
this account may not open, and it is the same weakness `profile_lock`'s
docstring already names in the other direction ("on Windows there is only ONE
witness … an unreadable answer resolves toward HELD").

Two things the table also settles:

* the return value was **`True`** — "killed or already absent" — so the caller
  counted an unidentified pid as successfully reaped and the record entry was
  dropped. The browser and the only thing naming it went at once.
* `ZombieProcess` was **already correct** and is unchanged. It subclasses
  `NoSuchProcess` (measured: `issubclass(...) is True`), so it hit the rung
  above the blanket handler. It is pinned precisely because it looks like it
  should have changed.

## 3. The fix

The verdict moves to `reap_guard.killable(pid, instance_id, is_browser_name)`,
which answers one of four things instead of two:

| outcome | `_kill_process_by_pid` |
|---|---|
| gone (`NoSuchProcess`, incl. a zombie) | `return True` — nothing to do |
| a Chromium-family process | escalate |
| some other process | log, `return False` |
| **name unreadable** | log, **`return False`** |

The refusal wording lives in the `Verdict`, so the message and the decision have
one home; the Chromium-family test arrives as an ARGUMENT
(`self._is_browser_process_name`), so `reap_guard` adds no second answer to
"what counts as a browser we launch".

This makes the reaper uniform with the places in the same tree that already got
it right. `profile_lock._browser_pids` returns `None` for "could not be asked" —
deliberately distinct from `()` for "asked, nothing running" — and resolves
toward HELD; `backend_eviction` refuses to evict a backend it cannot prove is
idle; and `spawn_leak._started_after` spares a pid whose start time it cannot
read. **That last citation is deliberately the symbol main has today.** An
earlier draft of this section cited `spawn_leak.launched_pid` instead and
asserted that F-919 had deleted `_started_after` along with
`_CLOCK_TOLERANCE_SECONDS`. F-919 has not landed: verified against `origin/main`,
`_started_after` is present and `launched_pid` exists nowhere, so the citation
pointed at a symbol in neither this branch nor main. The swap belongs in the
later merge that happens once F-919 is actually in main, gated on a grep for
`launched_pid` in `origin/main`'s `spawn_leak.py` rather than on a plan — and in
that same commit, `CLAUDE.md`'s start-time-fence sentence and F-919's §6
cross-reference, so all three become true together.

### 3.1 What paid for the lines

`process_cleanup.py` is GRANDFATHERED at 1009 LOC and may not grow, so the fix
paid for itself inside the function it was fixing: the two escalation rungs were
near-identical 20-line blocks and are now ONE table (`_KILL_RUNGS`) and one
loop. **1009 → 1007**, and the grandfather row is ratcheted DOWN to 1007 to
match. The behaviour of the ladder is unchanged — terminate, wait 3 s, kill,
wait 2 s, then the same "did not die after force kill" warning — with one
deliberate difference: an exception from `kill()` itself used to log at ERROR
and return immediately, and now logs at WARNING and lets the loop report the
exhaustion, so a failed rung and an exhausted ladder are one story rather than
two.

`process_exit._rung` is the CLOSE path's twin of that table. The two stay apart
deliberately and that module's docstring says why: a reap has no shutdown in
flight to wait for, so a grace there buys a wedged orphan up to 5 s of a
startup for nothing.

## 4. The pins

`tests/test_recovery_must_not_kill.py::TestUnverifiableProcessIsNotKilled`, five
nodes: `AccessDenied` and `OSError` must answer `False` with neither
`terminate()` nor `kill()` called; a zombie must still answer `True` (the
unchanged case, pinned with its measurement); a verified `chrome.exe` must still
be terminated; a verified `explorer.exe` must still be refused.

RED at `a3d22b3`: the two unreadable cases, both behaviour-RED.

### 4.1 Re-measuring after correcting a pin moved the number

The lane's three pin classes are **14 nodes, 9 RED at `a3d22b3`**: 7
behaviour-RED (F-916's four, F-917's one, and the two above) and 2
signature-RED — F-917's pair, which fail on a `TypeError` for a keyword argument
the pre-fix signature does not have.

The commit that introduced them says **10**, and that number is wrong. It was
measured while the zombie node above asserted `killed is False` — the answer its
author expected before reading the pre-fix source, which in fact already reached
`except psutil.NoSuchProcess` (listed BEFORE the blanket handler) and answered
`True` without terminating. Correcting that assertion turned the node from a RED
into a control, and the count was never re-run.

Measured, not reasoned: the tree re-exported at `a3d22b3` with this file's pins
dropped in gives 9 failed / 5 passed; flip that one assertion back and it gives
exactly 10 failed / 4 passed, which is where the published number came from. The
export was confirmed to be the unfixed product by four witnesses — no
`reap_guard` and no `cdp_endpoint` module, `browser_reattach.endpoint` still
present at its pre-move home with `Classified.unclassifiable` instead of
`.spare`, and `_kill_processes_for_metadata` carrying no `protected_pids`.

**Re-measure a RED count after correcting any pin in the set.** A corrected pin
is a different experiment, and the number belongs to the experiment that was
falsified rather than to the one that shipped.

## 5. Verification

Same harness after the fix:

| raised by `.name()` | returned | `terminate()` |
|---|---|---|
| `psutil.AccessDenied` | `False` | no |
| `OSError` | `False` | no |
| `psutil.ZombieProcess` | `True` | no |

and the log line now states the decision rather than only the failure:

> `Could not verify process 1234 for i-unknown (AccessDenied: (pid=1234)); it
> was NOT killed, because a pid we cannot identify may be a browser holding a
> login`

22/22 in the pin file — which carries F-922's eight as well, that fix having
landed in this same branch — and 3641 passed with 1 skipped across the whole
non-integration suite; ruff format + check, ty, vulture, suppression owners,
pinned imports, file budgets and `dump_tool_surface.py --check` all clean.

## 6. What this costs

**An orphan we cannot identify is left running.** Its entry is also kept — the
caller now gets `False`, so `recover_orphans` does not count it as recovered —
which means the leak is visible in `browser_pids.json` rather than silent, and
`kill-orphans --force` still reaches it through the same path (the `--force`
override is about the CLASSIFICATION, not about this guard; a pid whose name
cannot be read is refused there too).

That is deliberate and it is the narrower risk of the two: a pid we cannot read
may be a browser holding a login, and it may equally be a process that has
nothing to do with this tool — a recycled pid belonging to something the
operator is running. The old code would have terminated that too.

**What this gives F-919 for free, and the cross-reference that has to move.**
F-919's reap calls this function — `process_cleanup._kill_process_by_pid` is the
third of the three witnesses its docstring lists, "the escalating kill,
unchanged" — so the two fixes interlock rather than overlap: **F-919's fence
decides WHICH process a failed spawn may end; this one decides whether a pid may
be ended at all.** Its identity fence hands a pid down, and that pid now passes
through a guard that refuses it if its `.name()` cannot be read.

F-919 has a sentence in its §6 that goes false on the day this lands — it
says `_kill_process_by_pid` "still terminates a pid whose `.name()` could not
be read". Correcting it is the MERGE's job, not this branch's, and only once
F-919 is actually in main; nothing here edits another lane's finding.

**Residual:** the guard is about the NAME only. A process that is genuinely
Chromium-family but belongs to someone else's browser — a real Chrome the
operator started, sharing a profile directory with a stale entry — passes this
check and is killed. That is F-917's surface, not this one's, and its own
residual (a browser with no record entry at all) is recorded there.

## 7. Residual — refused, but the entry is dropped anyway

`recover_orphans` adds the instance id to `reaped` BEFORE the kill is attempted
(`process_cleanup.py:645`), so a pid this guard REFUSES leaves the process
running and its record entry gone. The state is pre-existing and is not what
this finding changed — but **the population entering it does change meaning, and
that asymmetry is the point**:

* a process refused for having a NON-BROWSER name is known not to be ours, so
  losing its entry costs nothing;
* a process refused for being UNREADABLE **might be our Chrome holding a login**
  — which is the entire argument for the refusal — and dropping its entry
  removes the only thing that names it. `process_cleanup.py:634-638` argues
  exactly that case for F-916's spare, one function away.

Not fixed here: the two refusals share one return value (`False`), so telling
them apart at the drop site means either a second verdict field or moving the
`reaped.add` after the attempt, and the second changes what a partially
successful pass records. Named rather than folded in, on F-916's own precedent.
It is the same shape as F-928's "a live browser whose entry is gone can never be
found again", reached from the other side.

