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

This makes the reaper uniform with the two places in the same tree that already
got it right — `spawn_leak._started_after` spares a pid whose start time it
cannot read, and `profile_lock._browser_pids` returns `None` for "could not be
asked" and resolves toward HELD.

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

RED at `a3d22b3`: the two unreadable cases.

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

14/14 in the new file; 528 passed across every non-integration importer of
`process_cleanup` / `browser_reattach` / `cdp_attach`; ruff format + check, ty,
vulture, suppression owners, pinned imports, file budgets and
`dump_tool_surface.py --check` all clean.

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

**Residual:** the guard is about the NAME only. A process that is genuinely
Chromium-family but belongs to someone else's browser — a real Chrome the
operator started, sharing a profile directory with a stale entry — passes this
check and is killed. That is F-917's surface, not this one's, and its own
residual (a browser with no record entry at all) is recorded there.
