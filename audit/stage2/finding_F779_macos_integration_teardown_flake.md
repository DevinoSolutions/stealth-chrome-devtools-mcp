# F-779 — `integration (macOS/ARM64)` fails at teardown with `Event loop is closed`, then passes on an identical tree

**Status:** open (gate reliability, not a user-facing product defect)
**Opened by:** the W5 contract re-run, 2026-07-28
**Severity:** medium — it reddens the aggregate `release-gate` check, which is the
one check a repository ruleset is meant to require.

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
- **Does not block the 2.0.0 contract**, whose tree is green at `6a8fa79`.
- Correct owner is **W8** (flake quarantine), which has not run. W8 should treat
  "identical tree, different conclusion" as its acceptance criterion rather than
  a retry budget.
- If it recurs, capture the job log with an admin-authenticated `gh` (unavailable
  in the session that opened this) and attach the traceback here before
  attempting a fix. **Do not "fix" this by adding a retry** — a retry that hides
  a teardown bug is exactly the second-way-to-do-something defect the repo's
  conventions forbid.
