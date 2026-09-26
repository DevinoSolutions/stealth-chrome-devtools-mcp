# F-932 — a Task Scheduler launch ran its child at BelowNormal priority

**Severity:** Medium. Under CPU load it drops the backend off the F-867
`scheduler` rung and back inside the MCP client's job. It also failed the 2.1.15
pre-push lane twice.
**Files:** `embedded/backend_launch.py`, `embedded/desktop_launch.py`.
**Depends on:** F-867 (the backend spawn ladder), F-810 (delegated headed
launch), F-879 (the 253-char `/TR` budget).

---

## 1. Symptom

The pre-push lane for release 2.1.15 failed twice on
`test_backend_escapes_client_job`:

- r1: 2 failed.
- r2: 1 failed, 3881 passed.

The machine was at about 82 % CPU both times. The test needs the backend to come
up on the `breakaway` or `scheduler` rung. Instead, the scheduled task did not
publish its pid inside the 20 s deadline, and the backend was served by `plain`.
That rung leaves the backend inside the client's job, which is exactly what
F-867 exists to prevent.

## 2. Measurement

- **Before the fix** (2026-09-24, Windows 11 10.0.26200). A task created
  without `<Priority>` reports priority 7. The process it started read
  `PriorityClass = BelowNormal`.
- **After the fix** (2026-09-26, same host). A real `schtasks /Create` +
  `/Run` of a `pythonw.exe` probe spawned two children and read their priority
  with `GetPriorityClass`:

  ```
  {"launcher": "BelowNormal", "child_old_flags": "BelowNormal", "child_f932_flags": "Normal"}
  ```

  The child created with the shipped flags (`DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP`) inherited BelowNormal. The child created with the
  same flags plus `NORMAL_PRIORITY_CLASS` ran at Normal. The PowerShell half is
  measured the same way in §5.

## 3. Root cause

Both launches that go through `schtasks` call `/Create` without a priority:

- `backend_launch`'s scheduler rung.
- `desktop_launch.launch_and_attach` (the F-810 delegated headed Chrome).

`/Create` has no priority switch; only `/XML` with a `<Priority>` element does.
The default task priority is 7, so the task's processes start at BelowNormal.
Children inherit that class unless they ask for another one. Under load a
BelowNormal launcher, and the BelowNormal Python or Chrome it starts, loses the
CPU to everything else running. That is enough to miss the 20 s pid deadline.

## 4. Fix

The CHILD asks for Normal itself. The task definition is unchanged.

- **`backend_launch._LAUNCHER_SCRIPT`** now ORs `subprocess.NORMAL_PRIORITY_CLASS`
  into the backend's `creationflags`. The launcher script stays stdlib-only.
- **`desktop_launch._launcher_script`** now sets
  `$p.PriorityClass = 'Normal'` right after `Start-Process … -PassThru` and
  before `Set-Content` publishes the pid. Whoever attaches to that pid therefore
  attaches to a Normal-priority Chrome. The raise is wrapped in
  `try { … } catch {}`: the script runs under `$ErrorActionPreference = 'Stop'`,
  and a Chrome that exits at once (it handed its arguments to an already running
  browser) would make the setter throw before `Set-Content`. That would turn a
  reportable launch into a 20 s pid timeout, so the raise is best-effort and can
  never cost the pid.
- **One home for the value:** `desktop_launch.TASK_CHILD_PRIORITY_CLASS`
  (`0x20`) and `TASK_CHILD_PRIORITY_NAME` (`"Normal"`), beside the rest of the
  `schtasks` seam. The backend launcher is a string evaluated in another
  interpreter, so it cannot import the constant. It names the same OS value, and
  a pin checks the constant against `subprocess.NORMAL_PRIORITY_CLASS`.

### Why not `/XML` with `<Priority>`

It was considered and rejected for two reasons:

- Every `/Create` fake in `tests/test_desktop_launch.py` and
  `tests/test_backend_launch.py` asserts the `/TR` argv shape. A task XML would
  replace that shape with a second one.
- The `/TR` budget (F-879) is measured against the `/Create /TR` form.
  Switching forms would need that budget re-measured.

Asking from the child reaches the same result without touching either.

## 5. Tests

`tests/test_scheduled_task_priority.py` reads the launcher text, because that is
what runs under `schtasks`; a fake's argv would not show the priority. Before the
fix it ran RED: 2 failed, 3 passed. It pins:

- The constant's value, and that it equals the OS value (Windows only).
- The PowerShell raise line is best-effort (`try { … } catch {}`) and sits after
  `-PassThru` and before `Set-Content`.
- `NORMAL_PRIORITY_CLASS` is inside the backend launcher's `creationflags` (an
  AST read), next to the two detach flags that were already there.
- The backend launcher still imports only stdlib modules.

After the fix, that file plus `tests/test_backend_launch.py` and
`tests/test_desktop_launch.py`: 87 passed.

**The PowerShell half, measured live (2026-09-26).** Windows PowerShell 5.1
was started at BelowNormal with `start /belownormal`, which reproduces the
inheritance condition a task creates. It ran two `Start-Process … -PassThru`
children and raised only the second with the `$p.PriorityClass = 'Normal'`
setter that the shipped line wraps in `try { … } catch {}`:

```
{"launcher": "BelowNormal", "child_unraised": "BelowNormal", "child_raised": "Normal"}
```

The same script launched through a real `schtasks /Run` never got as far as
starting a child. The PowerShell process sat at 1.1 s of CPU for more than
11 minutes on the loaded host and was killed. The same thing happened to the
`powershell -Command` calls the first `pythonw` probe made under a task. That
hang happens before `Start-Process`, so this fix neither caused it nor cures it.
It is recorded as residual 4.

## 6. Residuals and cost

1. **The launcher itself still runs at BelowNormal.** It is short-lived: it
   reads a spec, starts one process and writes one pid file. The time it spends
   BelowNormal is bounded by that work, and it does not include the backend's or
   Chrome's life.
2. **It does not remove the deadline.** Under extreme starvation the scheduler
   rung can still miss its 20 s deadline and drop to `plain`, which logs its
   F-867 line as before. What changed is that the processes doing the work are
   no longer deprioritised on top of the load.
3. **A running process is not re-prioritised.** Backends and delegated Chrome
   windows started before the upgrade keep BelowNormal until they restart.
4. **Windows PowerShell started by a task can stall on a loaded host before it
   runs a line of the script** (§5). The delegated headed launch is a
   PowerShell launcher, so under that load it can still miss its deadline. The
   cause is not established; this finding does not claim to address it.
