# F-842 — the python `mcp` SDK stdio client tree-kills the launcher on close, taking the DETACHED shared backend with it

**Severity: LOW for production (Claude Code is unaffected) / HIGH for anyone driving the proxy from the python SDK — and for every test harness we write with it**
**Found:** 2026-08-31, live battery against the installed 2.0.8 release (isolated gate workspace).
**Status:** OPEN — documented; fix direction proposed below. Filed alongside F-843, which it kept masking in harness runs.

## What happens

The official python `mcp` SDK's stdio client (and fastmcp's `Client` over
`StdioTransport`, which wraps it) terminates the launched server's **whole
process tree** when the client closes. Our stdio proxy launches the shared
backend `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` precisely so it outlives
any one session — but detachment does not remove the parent/child edge, and the
SDK's teardown kills by tree, not by process. So:

- when the session that **won the cold-start lock** closes, its proxy's tree
  includes the backend → the shared backend dies, orphaning every other
  session bridged to it;
- when any **non-winner** session closes, the backend (not in that proxy's
  tree) survives.

## Evidence (two-session parentage probe, installed 2.0.8, isolated workspace)

Client A opens first (wins the lock; backend's recorded ppid = A's proxy pid),
client B adopts. Close B → backend survives, A still served. Close A → backend
gone within seconds; probe verdict line: `backend KILLED by winner's session
close (client tree-kill)`. Reproduced deliberately after run-1 of the live
battery showed its telltales: mid-herd `httpx.ReadError` bursts, `-32603
"backend on port N died while 'tools/list' was in flight"` sacrifices, and
backends that died with **no shutdown log line and an empty fault log** — a
death by external kill, not by crash.

## Why production is unaffected

Claude Code does not tree-kill on disconnect: the operator's real backend
routinely survives the winning session's exit for days (observed continuously
on the real 19222/52554 backends). The defect lives in the python-SDK-client →
proxy interop, which is exactly the shape of every test harness, script, and
third-party python integration.

## Consequences for us

1. **Harness design:** any multi-session python-client test must expect the
   backend to die when its winner closes. The 2.0.8 live battery deliberately
   dropped its "exactly one backend booted across phases" assert for this
   reason; churn-style tests must treat winner-close as a backend-death
   injection (which is how it became the reproducer for F-843).
2. **Third-party exposure:** a python-SDK user running multiple sessions gets
   real orphan-and-die behavior we never see from Claude Code.

## Fix direction (proposed, not implemented)

Break the parent/child edge at spawn: have the proxy start the backend through
a **double-spawn** (an intermediate launcher process that spawns the backend
DETACHED and exits immediately), so the backend's parent is gone by the time
any client teardown walks a tree. Windows job-object semantics and the uv
trampoline chain (the spawned pid is a shim whose child re-execs the same
cmdline) need care: the intermediate must exit only after the real backend
process exists. Alternative considered and rejected: asking clients not to
tree-kill — not ours to control, and the SDK's behavior is reasonable for the
common single-server case.

## Related

- F-843 (found the same day): in-flight backend death bypasses the F-838
  self-heal — F-842 is how our own harnesses kept *injecting* that death.
- F-839: the SIGBREAK analogue of session teardown reaching the backend; the
  detachment flags stop console signals but not tree-kills.
- Installed-env coverage gap noted during the same investigation: the uv tool
  venv resolves fresh deps on Python 3.13 while the repo locks 3.12 — CI never
  tests what `uv tool install` actually materializes.
