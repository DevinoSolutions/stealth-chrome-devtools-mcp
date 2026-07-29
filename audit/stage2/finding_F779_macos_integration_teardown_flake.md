# F-779 — `integration (macOS/ARM64)` fails at teardown with `Event loop is closed`, then passes on an identical tree

**Status:** open (gate reliability, not a user-facing product defect)
**Opened by:** the W5 contract re-run, 2026-07-28
**Severity:** **HIGH.** Measured failure rate is roughly **one run in four** on the
macOS/ARM64 integration cell, and each failure reddens the aggregate
`release-gate` check — the one check a repository ruleset is meant to require.
A required check that fails a quarter of the time for reasons unrelated to the
change is not a gate; it is a coin flip with a retry button.

> **Revision note (2026-07-29):** this finding originally said "observed once then
> not reproduced." That was wrong within hours. It has now reproduced on a
> **documentation-only commit**, which settles the question of whether code is
> involved. The rate below is measured, not estimated.

## What was observed

Commit `d13997e` (W5, "regenerate the contract at 2.0.0") failed CI with three
reds:

| check | conclusion |
|---|---|
| `release-gate / integration (macOS/ARM64)` | failure |
| `release-gate / release-evidence` | failure (consequence) |
| `release-gate / release-gate` (aggregate) | failure (consequence) |

Only the first is a root cause. The other two are the fail-closed machinery
working exactly as designed: `release_evidence` reported
`integration/macOS-ARM64: non-success terminal outcome 'failure'` and refused to
certify the ledger, and the aggregate reported `one or more required edges were
not success`. **Neither is a separate defect.**

The macOS job's failure annotations were:

```
Process completed with exit code 1.        (.github:243 — "Run integration + Chrome-identity tests")
Event loop is closed                       (.github:545)
Event loop is closed                       (.github:545)
```

## Why it is a flake and not a regression

`6a8fa79` is an **empty commit on top of `d13997e`** — `git commit-tree` against
`d13997e^{tree}`, so the two commits have a **byte-identical tree**. The gate was
re-run against that identical tree and returned **32/32 success**, including
`integration (macOS/ARM64)`, `release-evidence`, and the aggregate.

Same code, same workflow, same runner image: one red, one green. That is the
definition of a flake.

For completeness, `d13997e`'s only delta against the previously fully-green
`5c2505a` was version metadata (`1.2.0` → `2.0.0`) and `[tool.hatch.build.targets.sdist]`
excludes — packaging-only changes that `d13997e`'s own **six green install-smoke
cells** already exercised. There was never a plausible mechanism by which that
diff could break a macOS integration teardown.

## Second observation — the one that settles it (2026-07-29)

Commit `7c65374` adds **exactly one file**: `finding_F780_...md`, a markdown
document. No code, no workflow, no test, no dependency. It failed with the
byte-identical signature:

```
Process completed with exit code 1.        (.github:241 — "Run integration + Chrome-identity tests")
Event loop is closed                       (.github:545)
Event loop is closed                       (.github:545)
```

A markdown file cannot break a macOS integration test. Combined with the
identical-tree re-run above, the code is exonerated twice over by two independent
methods.

### Measured rate on the W5 line

| commit | what changed | `integration (macOS/ARM64)` |
|---|---|---|
| `5028d66` | flake record | success |
| `5c2505a` | 2.0.0 merge | success |
| `d13997e` | contract regen | **failure** |
| `6a8fa79` | *empty commit, identical tree* | success |
| `a97c970` | F-779 doc + uv.lock one-liner | success |
| `7c65374` | **one markdown file** | **failure** |
| `7a3f546` | F-779 rate correction (docs) | success |
| `3be448b` | F-779 mechanism (docs) | success |

**2 failures in 8 consecutive runs (~25%), as of `3be448b`.**

Both failures carry the same `Event loop is closed` teardown signature. Successes
bracket each failure, so this is not a regression that landed and stayed.

> **On the number itself.** This tally is *live*, and it has a self-invalidating
> property worth naming: every commit that edits this file adds another sample to
> the denominator. It read 2/6 when first written, then 2/7, now 2/8 — not because
> anything changed, but because documenting it generates evidence about it. Do not
> keep re-editing the percentage; it will always be slightly behind.
>
> **The durable claim, which none of that drift touches:** the macOS/ARM64
> integration cell fails intermittently at an order of roughly **one run in four**,
> the failures are provably independent of the code under test, and each one takes
> the `release-gate` aggregate down with it. Anyone tempted to update the
> fraction should instead spend that effort on the mechanism below.

Note the compounding arithmetic: the aggregate needs *every* edge green, so a
~25% failure on one cell puts the headline `release-gate` check red roughly one
run in four no matter how healthy the other 31 jobs are — and that is a floor,
since the Linux cold-spawn flake can independently redden the same aggregate.

## Probable mechanism — HYPOTHESIS, not a confirmed diagnosis

**Confidence: moderate-to-high on the mechanism, zero direct confirmation.** No
macOS machine was available and job logs require admin rights, so nothing below
was observed — it is derived from reading the teardown path. Treat it as the
first place to look, not as the answer.

`browser_manager.close_instance` Phase 3 (`browser_manager.py:923-940`):

```python
stop_coro = await asyncio.wait_for(
    asyncio.to_thread(self._blocking_teardown, instance_id, browser),
    timeout=self.CLOSE_KILL_TIMEOUT,          # settings default: 5.0s
)
except TimeoutError:
    ... "worker thread continues in background, orphan will be reaped by process_cleanup"
```

The comment is correct and the code knows it: **`asyncio.wait_for` cancels the
awaitable, but it cannot cancel the worker thread.** `asyncio.to_thread` dispatches
to a `ThreadPoolExecutor`; on timeout the coroutine gives up while the thread keeps
running. That leaves a thread alive, holding `browser`, after `close_instance` has
returned and bookkeeping has moved on.

Two ways that thread produces exactly `Event loop is closed` once the loop is gone:

1. **Future resolution.** When the orphaned thread finishes, the executor resolves
   its future via `loop.call_soon_threadsafe(...)`. Against a closed loop that is
   `RuntimeError: Event loop is closed`, raised from the thread, outside anyone's
   `try`.
2. **`browser._process.terminate()` (`browser_manager.py:238`).** nodriver's
   `_process` is an **asyncio** subprocess bound to the loop that created it. The
   retry loop calls `.terminate()` / `.kill()` from the worker thread; once that
   loop is closed, the transport's `_check_closed()` raises
   `RuntimeError: Event loop is closed`. The surrounding `except Exception` then
   falls through to `.kill()`, which fails the same way — which is a plausible
   reading of why the annotation shows the message **twice**.

Why this fits F-779's fingerprint specifically:

- **Intermittent** — only bites when kill exceeds the 5s `CLOSE_KILL_TIMEOUT`.
- **Teardown-only** — nothing before teardown touches this path.
- **Code-independent** — any commit can lose the race, which is why a
  markdown-only commit hit it.
- **macOS-leaning** — macOS is already the anomalous cell here (F-773: Chrome under
  the detached backend completes no network navigation on that runner). A cell where
  Chrome/process behaviour is already known to differ is exactly where a 5s kill
  budget would be tightest.

### How to confirm it cheaply

Get one admin-authenticated job log and check whether the `RuntimeError` traceback
originates in a thread (`ThreadPoolExecutor-N_M`) rather than the main task. If it
does, the hypothesis holds. If the traceback is in the main task, this section is
wrong and should be deleted rather than argued for.

### Note for whoever fixes it

The tempting fix — widen `CLOSE_KILL_TIMEOUT` — only moves the race. The structural
issue is that a thread which cannot be cancelled is abandoned while still holding
loop-bound objects. Options worth weighing: join the orphaned thread at shutdown,
make `_blocking_teardown` touch only OS-level primitives (`os.kill`, psutil) and
never the asyncio `Process`, or keep a registry of abandoned threads that shutdown
drains. **This is a `src/` change and plan_RELEASE forbids `src/` edits, so it
belongs to a FIX plan, not here.**

## What is NOT yet known

The mechanism is not diagnosed. `Event loop is closed` at teardown is consistent
with an asyncio loop being closed while a task or transport still holds a
reference to it, but the specific owner was not identified, because:

- **Job logs require admin.** The unauthenticated REST API returns `403 Must have
  admin rights to Repository` for `/actions/jobs/<id>/logs`. Check-run
  **annotations** were the only window, and they carry the message but not the
  traceback.
- The failure was not reproduced locally (this is a macOS-only observation and no
  Mac is available in this environment).

So this finding records *that* it flakes and *that* the tree is exonerated. It
does **not** claim to know why.

## Relationship to neighbouring findings

- **Not F-773.** F-773 is "macOS/ARM64 Chrome under the detached backend completes
  no network navigation on hosted runners" — reproducible 11/11, and the reason
  the gate makes no macOS navigation claim. F-779 is a *teardown-time* error on a
  job that otherwise ran, and it is *not* reproducible.
- **Not the Linux cold-spawn flake.** That one is a cold-*start* race
  (`Failed to connect to browser`) on a different OS at a different phase. Same
  category (gate reliability), different mechanism.
- **Same category as F-775b's macOS close-flake observation** — an unreproducible
  macOS teardown/close anomaly. Worth checking whether they share a root cause;
  that has not been done.

## Why it matters

`plan_RELEASE` §0.2 makes flake-freedom one of the three properties behind
"green ⇒ blindly pushable". This is now the **second** distinct gate flake on
record (after the Linux cold-spawn one), and unlike that one it takes down the
**aggregate check itself**.

The practical hazard is cultural, not technical: a required check that
intermittently reddens for reasons unrelated to the change trains reviewers to
re-run until green, which is indistinguishable from training them to ignore it.
That is precisely the failure mode this campaign exists to prevent.

## Disposition

- Recorded as a limitation in the generated release contract (gate reliability).
- **Does not block the 2.0.0 contract on correctness** — the tree is green at
  `6a8fa79` and `a97c970`, and the failures are provably code-independent.
- **It does block the "green ⇒ blindly pushable" claim**, and that claim is the
  stated point of the whole campaign. A gate whose headline check is red ~1 run in
  3 for unrelated reasons cannot license a blind push, because the reviewer can no
  longer distinguish "my change is bad" from "the gate did the thing it does."
  Whether to tag 2.0.0 anyway is a human call; what is not available is calling
  the gate trustworthy while this is open.
- Correct owner is **W8** (flake quarantine), which has not run. W8 should treat
  "identical tree, different conclusion" as its acceptance criterion rather than
  a retry budget.
- If it recurs, capture the job log with an admin-authenticated `gh` (unavailable
  in the session that opened this) and attach the traceback here before
  attempting a fix. **Do not "fix" this by adding a retry** — a retry that hides
  a teardown bug is exactly the second-way-to-do-something defect the repo's
  conventions forbid.
