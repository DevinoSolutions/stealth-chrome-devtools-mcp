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

## Structural fix (LANDED — `test/e2e-coverage-F873-F881`)

The whole e2e tier used to spawn against the operator's real
`STEALTH_MCP_BROWSER_SESSION_ROOT`. It no longer does — unless the operator's
own environment names it: `tests/conftest.py` redirects that variable at
**module import time** (before collection, before any fixture) to a fixed
directory under the system temp dir, alongside the
`STEALTH_MCP_CLONE_OUTPUT_DIR` line that already used the idiom, using
`setdefault` so the release gate's `runner.temp` value still wins. The cost of
`setdefault` is exactly that: a shell that exports
`STEALTH_MCP_BROWSER_SESSION_ROOT=C:\stealth-mcp-browser-sessions` gets the old
behaviour back, deliberately, because overriding an explicit environment would
also override the gate's.

A second residual comes from the path being FIXED: the root is shared across
worktrees and concurrent runs. Named collisions walk correctly and nothing
deletes another process's live profile, but `master` carries no reservation and
a disk assertion must be scoped to what the test itself was given. The argument
is written out beside the `setdefault` line.

Two things were learned building it, and they are why the fix is not the
fixture this section originally proposed:

* **A per-test fixture cannot do it.** `get_settings()` is `@lru_cache`d and
  `conftest._reset_settings_cache` clears it at each test's SETUP, so the root
  the product reads is whatever `os.environ` said at that moment. Every E2E
  module declares an autouse `_warmup` that spawns a browser, and pytest orders
  it BEFORE a function-scoped root fixture — so `tmp_empty_root`'s `patch.dict`
  arrives after the root has already been resolved from the real environment.
  Measured: a six-browser fleet node that declared `tmp_empty_root` wrote six
  108 MB named profiles into `C:\stealth-mcp-browser-sessions\sessions`, and a
  control run with the variable set in the PARENT environment put them in the
  probe root instead. `tmp_empty_root` on an E2E node is therefore decorative;
  the nodes added with this fix do not declare it, and say so.
* **A fixed temp path beats a fresh one per session.** The costs this section
  worried about — "per-run master seeding" — are paid once per machine rather
  than once per run, and a seeded master that persists is also what keeps a
  six-browser fleet's spawn phase at 2.6 s instead of 9.3 s.

What remains open is only the second half of the original note: nothing decides
what stealth state the seeded master should carry. Today it is whatever the
first spawn creates.

## Related

- F-834 (this batch): documented that uncontended spawns open the master
  directly — the precondition for this incident.
- The `probe` and `sessions` siblings under the session root were untouched.
