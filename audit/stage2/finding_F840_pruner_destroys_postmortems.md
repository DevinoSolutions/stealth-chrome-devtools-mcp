# F-840 — `prune_old_logs` deletes the evidence a post-mortem needs

**Severity:** HIGH (diagnosability). **Status:** FIXED on
`fix/F830-F840-log-hygiene`.
**File:** `src/stealth_chrome_devtools_mcp/embedded/logging_setup.py`.
**Tests:** `tests/test_log_hygiene.py`, `tests/test_logging_setup.py`
(one deliberate golden update, §5).

---

## 1. Observation

On 2026-08-30 a backend worker was OOM-killed. By the next morning its
`backend-<pid>.log` and `backend-<pid>-fault.log` were **gone** — the death
investigation started blind, with only the shared `backend-boot.log` (itself
794 MB and unreadable in practice, see F-830) to work from.

## 2. Root cause

`prune_old_logs` swept purely on recency, with two rules and no exceptions:

```python
cutoff = time.time() - keep_days * 86400
for index, path in enumerate(files):              # newest first
    if index >= keep_files or path.stat().st_mtime < cutoff:
        path.unlink()
```

Both rules are actively hostile to post-mortems:

* **A dead process's log stops being touched the moment it dies.** A *live*
  backend refreshes its mtime constantly, so it always sorts newest and always
  beats the cutoff. A *crashed* one never does. The sweep therefore
  preferentially deletes exactly the logs belonging to processes that died —
  the only ones anyone ever needs to read.
* **The age at which a crash log becomes interesting is the age at which it
  gets deleted.** Nobody investigates an unattended overnight OOM within
  minutes; they investigate it the next morning, or the next week. Age is a
  proxy for "nobody has looked at this yet", not for "this is worthless".
* **`keep_files` is index-based, so a busy machine evicts fast.** Every stdio
  proxy leaves a `proxy-<pid>.log`; a fleet of sessions can push a dead
  backend's set past a 50-file budget in a day regardless of the age rule.
* **`*-fault.log` was treated like any other file.** That file is written by
  `faulthandler` at the C level for hard/native crashes that never reach
  Python's exception machinery — i.e. precisely the class of death (segfault,
  OOM kill, stack overflow) that leaves *no other trace at all*.

`prune_old_logs` runs from `configure_logging`, so every new backend and every
new stdio proxy triggers a sweep. The deletion window is hours, not days.

## 3. Fix

`prune_old_logs` now consults `_post_mortem_exempt(files)` and skips whatever
it returns. Two narrow rules:

| Constant | Value | Rule and rationale |
|---|---|---|
| `_KEEP_BACKEND_SETS` | 3 | Keep the newest three backend **log sets** (`backend-<pid>.log`, its `.N` rotations and `backend-<pid>-fault.log`, grouped by pid) **regardless of age**. Three covers "the backend that just died, plus the one before it, plus one" — enough to compare a crash against its healthy predecessors, small enough that the bound is obvious. Age never applies, because age is the thing that was destroying the evidence. |
| `_FAULT_LOG_KEEP_DAYS` | 14 | Never prune *any* `*-fault.log` younger than a fortnight. Two weeks spans a holiday or a vacation, which is the realistic worst case for "nobody looked yet". Fault logs are opened empty and stay empty unless a hard crash actually wrote one, so the retention costs bytes, not megabytes. |

The grouping key is `_BACKEND_LOG_RE = ^backend-(\d+)(?:-fault)?\.log`.
`backend-boot.log` deliberately does **not** match (`boot` is not a pid): it is
shared across every backend rather than one backend's post-mortem, so it stays
fully sweepable and F-840 cannot re-open F-830. A test pins that.

Everything else is unchanged: proxy logs, non-matching files, and surplus
backend sets past the newest three still go by age and count exactly as
before. The exemption is a whitelist, not a policy change.

### Bonus — the boot line now names the launch context

`bootstrap_backend_process_logging`'s affirmative startup line gained `argv`:

```
backend process starting (pid=…, log=…, argv=['…', '--transport', 'http', '--port', …])
```

A backend can be born two ways — a console-attached `serve --http` run by the
operator, or the detached `singleton._start_server_process` spawn — and they
fail differently (signal handling, console attachment, inherited environment).
Nothing in the log distinguished them; argv does, in one line, at zero
recurring cost. It is written to the local per-pid file only and is never
shipped to Sentry.

## 4. Tests (`tests/test_log_hygiene.py::TestPrunerKeepsPostMortems`)

* the three newest dead-backend sets survive `keep_days=1, keep_files=1` even
  at 30–34 days old (the 2026-08-30 regression, verbatim);
* 20 fault logs aged 10 days all survive the same brutal sweep;
* **regression guard:** proxy logs and the seven *oldest* backend sets (400+
  days) are still deleted — the exemption must not turn the sweep into a
  no-op;
* `backend-boot.log.1`/`.2` are pruned normally, i.e. not post-mortem-exempt.

RED was demonstrated by mutation as well as by absence: forcing
`exempt = set()` fails the first three of the above and passes the rest.

## 5. Golden update

`tests/test_logging_setup.py::TestPruneOldLogs::test_prune_caps_file_count`
built its five fixture files as `backend-<n>.log` — which are now exempt by
construction, so the test no longer demonstrated the count cap it was written
for. It now uses `proxy-<n>.log`, which is what `prune_old_logs`'s own
docstring says the cap targets ("one per proxy session"), and carries a
comment pointing at the exemption's coverage. Deliberate, in the same commit
as the behaviour change, per CONTRIBUTING's SOFT-golden discipline.

## 6. Residual risk

* **Unbounded fault-log count within the 14-day window.** Every backend boot
  creates a `backend-<pid>-fault.log`, usually 0 bytes, and all of them within
  the window are now exempt from both rules. A machine that spawns hundreds of
  backends a fortnight accumulates hundreds of near-empty files. Inode count,
  not disk space; accepted as the cheap side of the trade. If it ever matters,
  the right narrowing is "exempt young fault logs *that are non-empty*", which
  keeps every fault log that ever recorded anything.
* The exemption is keyed on the `backend-<pid>` filename convention. Renaming
  the per-role log files without updating `_BACKEND_LOG_RE` would silently
  restore the old behaviour; the regex and `configure_logging`'s
  `f"{role}-{os.getpid()}.log"` are ~230 lines apart in the same file.
