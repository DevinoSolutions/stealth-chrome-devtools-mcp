# F-841 — a resilience test rmtree's whatever profile the instance used, including the operator's REAL master

**Severity: HIGH (test-safety; destroyed real user state)**
**Found:** 2026-08-31, first full-lane run of the 2.0.8 batch on a quiet machine.
**Status:** guard SHIPPED in the 2.0.8 batch (`tests/test_resilience.py::_is_per_instance_clone`); the structural fix (isolated session root for the whole e2e tier) remains OPEN.

## What happened

`test_crash_recovery_after_the_owned_chrome_is_killed` asserts, as part of the
MQ-126 recovery contract, that "the crashed instance's profile is removable" —
by calling `shutil.rmtree(profile_dir, ignore_errors=True)` on
`metadata["user_data_dir"]`.

On 2026-08-31 09:40 the machine was quiet (no live Chrome held the master), so
the test's spawn opened the SHARED MASTER profile directly
(`profile_role: "master"`), `user_data_dir` named
`C:\stealth-mcp-browser-sessions\master`, and the test **deleted most of the
operator's real master profile** (gutted to 11 component-cache files;
`ignore_errors=True` removes everything it can before "failing"). The lane
failure that exposed it was the assert firing only because a handful of files
resisted deletion.

**Recovery:** master restored from the product's own `master-snapshot`
(the "before-master-open" snapshot; 131 files incl. `Default/` and
`Local State`) via robocopy, same morning. No user action needed.

## Why it was never caught

The lane only ever ran on a busy machine: the operator's live Chrome held the
master, `_profile_has_running_browser` forced every test spawn onto a
per-instance clone, and the rmtree deleted a disposable clone. The test's
safety was an accident of environment. CI never sees it either — a runner's
master is fresh and worthless.

## Guard shipped (this batch)

The removability assertion now runs only when the instance actually got a
per-instance clone (path under the session root's `sessions/` directory).
The master — and anything else — is never this test's to delete. The MQ-126
contract loses nothing: "the crashed instance's profile is removable" was
always a claim about the disposable clone the spawn created.

## Structural fix (open, follow-up)

The whole e2e tier spawns against the operator's real
`STEALTH_MCP_BROWSER_SESSION_ROOT`. The right fix is a fixture that points the
session root at `tmp_path` for every real-Chrome test, so no test can touch
real profiles at all (and lane behavior stops depending on whether the
operator's browsers happen to be running). Costs: per-run master seeding, and
a decision about what stealth state the seeded master should carry. File with
the next test-infrastructure batch.

## Related

- F-834 (this batch): documented that uncontended spawns open the master
  directly — the precondition for this incident.
- The `probe` and `sessions` siblings under the session root were untouched.
