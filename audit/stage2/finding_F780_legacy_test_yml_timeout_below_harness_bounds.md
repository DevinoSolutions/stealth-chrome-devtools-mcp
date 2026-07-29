# F-780 — the legacy `test.yml` browser-integration job cannot pass the W1 transport journey: its `--timeout=120` is *below* the harness's own inner bounds

**Status:** open, but **self-resolving** — the offending job is deleted by W2 (#44).
**Opened by:** merge-queue triage, 2026-07-28.
**Severity:** low as a defect, **high as a trap.** It makes three PRs in the release
stack look like they carry a product regression when they do not.

## Symptom

Three open PRs in the release stack are red on a check named
`Browser Integration Tests (Chrome + Xvfb)`:

| PR | branch | legacy job |
|---|---|---|
| #43 | `audit/release-fix-b` | **failure** |
| #45 | `audit/release-4-w4` | **failure** |
| #47 | `audit/release-fix-d` | **failure** |

The same check is **green** on `main` (~3 min) and on **#42** (`audit/release-1-w1`,
also ~3 min). So it looks exactly like "FIX-B broke the browser integration tests."

**It did not.**

## Root cause

`.github/workflows/test.yml` (the pre-W2 workflow) runs:

```yaml
uv run pytest -m integration -v --tb=short --timeout=120
```

`tests/release_gate_harness.py` declares its own per-step bounds, under a comment
that states the intent outright — *"every await is wrapped; the pytest --timeout is
the outer net"*:

```python
INIT_TIMEOUT    =  60.0
LIST_TIMEOUT    = 130.0   # first backend-bound call — covers backend cold start
SPAWN_TIMEOUT   = 120.0   # first real Chrome launch
WARMUP_TIMEOUT  = 150.0   # cold Chrome + master-profile bootstrap
WARMUP_ATTEMPTS =   4     # with 3s * attempt backoff
```

`LIST_TIMEOUT` (130s) and `WARMUP_TIMEOUT` (150s) are each **larger than the whole
job's 120s per-test budget**, and `BACKEND_READY_TIMEOUT = 120.0` in
`embedded/singleton.py` is exactly equal to it. A 120s outer net cannot contain a
single inner step budgeted at 130s or 150s. The job is **structurally incapable**
of running this test to completion — no timing luck involved.

## Why it started failing exactly at FIX-B

W1 landed the transport journey **already marked `xfail(strict=False)`**, because
B1 (per-MCP-session `app_lifespan` + the proxy's 2s watchdog) was a known open
defect at the time. An xfail costs nothing on the clock, so the legacy job stayed
green on #42.

FIX-B's C2 commit (`585ebf2`, "flip the W1 transport xfail — journey green") removed
that marker, which was correct: FIX-B fixed B1, so the test must really run. From
that commit onward the legacy job actually executes the journey, hits its 120s wall,
and fails.

#45 (W4) and #47 (FIX-D) branch off FIX-B, so they inherit both the un-xfailed test
and the under-budgeted workflow.

## Why the release gate is green on the same code

W2 (#44) replaced the legacy job's semantics with the reusable `release-gate`
workflow, which budgets realistically:

- `integration` job: `--timeout=180`
- dedicated `transport` job: `--timeout=300`

#46 (`audit/release-fix-c`) — which contains FIX-B's commits transitively — is
**green 23/23**, including `integration (Linux/X64)` running the *same*
`-m integration` selection. That is the controlled comparison: same code, same OS,
same marker, different budget, different outcome.

## Disposition

**Not fixed here, deliberately.** The job disappears when #44 merges, so patching
`test.yml` on three separate in-review branches would be churn against a file that
is about to be deleted — and it would mutate PRs while a human is reviewing them.

**What the human needs to know when working the merge queue:** merging in stack
order will show red at #43 → #45 → #47 until **#44 (W2)** lands, at which point the
legacy job no longer exists and the release gate takes over. Those reds are not a
signal about the code. The signal that matters is the tip of the stack, which was
green 32/32.

**Do not "fix" this by re-adding the xfail.** The xfail was correct only while B1
was open; restoring it to quiet a workflow that is being deleted would re-hide a
defect that is genuinely fixed — and would make the transport journey stop proving
the one thing it exists to prove.

## Wider lesson

An outer timeout that is *smaller* than the inner bounds it is supposed to contain
is not a conservative setting — it is a broken one, and it converts into a
false product-regression signal at the exact moment a test stops being skipped.
Any workflow that runs this harness must budget above `WARMUP_TIMEOUT` (150s) plus
the warmup retry envelope, not below it.
