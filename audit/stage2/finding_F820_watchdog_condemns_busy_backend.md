# F-820 — the liveness watchdog condemned a BUSY backend, disconnecting every session at once

**Status: RESOLVED** on `fix/f820-watchdog-busy-vs-dead`.
**Severity: HIGH (observed in production)** — this is the user-reported
"stealth randomly disconnects". Not random, and not one session: under fleet
load *every* Claude Code session lost the MCP server inside the same second,
repeatedly, while the backend they all share was healthy the entire time.

---

## Symptom

A stdio proxy tears itself down; Claude Code reports the `stealth-chrome-devtools-mcp`
server as disconnected and its 94 tools vanish mid-task. It recurs in waves,
minutes apart, and hits every concurrent session simultaneously — which is the
tell that it is not per-session flakiness but a shared cause read the same
wrong way by every proxy at once.

## Production evidence (2026-08-30)

Four waves, from the proxy logs, against **one** backend (pid 52396):

| Time  | Proxies torn down |
|-------|-------------------|
| 05:12 | 7                 |
| 05:19 | 30                |
| 05:28 | 3                 |
| 05:34 | 10                |

Two facts make the verdict wrong rather than merely unlucky:

1. **The backend kept serving.** Backend pid 52396 answered `navigate` and
   `screenshot` calls before, during, and after each wave, and outlived all
   four. Nothing died; nothing was restarted; the socket never closed.
2. **The probe counters reset.** Within the run-up to a wave the strike counter
   climbs and then goes back to `1/3` — i.e. probes *were* intermittently
   succeeding. That is the signature of a backend that is slow, not one that is
   gone. A dead backend never resets anything.

The slow spans lasted roughly 20–40 seconds — long enough to lose three
consecutive 2s probes, far short of "dead".

## Mechanism

`singleton._watch_backend_liveness` is armed once the backend answers a real
`initialize`. It then probed every `interval` (2.0s) with
`_backend_http_ready(port)` at `LIVENESS_PROBE_TIMEOUT` (2.0s), and returned —
which the caller (`_proxy_streams.monitor_backend`) treats as "backend gone,
tear the proxy down" — after `failures_before_teardown` (3) consecutive
failures. One success reset the run.

The probe is an app-level `initialize` round trip against the shared backend.
Under a multi-session fleet, that backend is one process serving every
session's tool calls, and a burst of real work makes it answer `initialize` in
more than 2s for a stretch. Three such ticks is ~6s of slowness. So the whole
watchdog reduced to: *6 seconds of slow = dead*.

Worse, it is correlated by construction. Every proxy probes the same backend,
so when it is busy they all miss together and all condemn it together — hence
7, 30, 3, 10 teardowns inside one second rather than a trickle.

The discrimination this needed already existed. **F-807** hit the identical
"busy is not dead" confusion on the cold-start path and resolved it: a
same-identity backend gets `REUSE_PATIENCE_SECONDS` (60s) of
`REUSE_PROBE_TIMEOUT` (10s) probes before a lock-holder may evict it, while a
socket-dead one fails on the first refused connection and buys no window at
all (`_same_identity_backend_ready`). The watchdog was simply never brought
onto that policy — it stayed on the pre-F-807 "one short probe verdict is
final" reading that F-807 removed everywhere else.

## Fix

The three strikes no longer condemn. They now open a **confirmation phase**,
and the verdict comes from the gate the cold-start lock already trusts:

```python
confirm = confirm_probe or (lambda: run(_same_identity_backend_ready, port))
...
if consecutive < failures_before_teardown:
    continue
if not await _ask(confirm):
    _logger.warning("backend on port %d confirmed unusable", port)
    return
_logger.info("backend on port %d was busy, not dead", port)
consecutive = 0
```

Deliberately **not** a second policy (CLAUDE.md convention 4: a second way to
do something already done is a defect). Reusing `_same_identity_backend_ready`
means F-820 introduces no new constant, no new tunable, and no second
definition of "dead" — and it inherits, already pinned by
`tests/test_singleton_cold_start_patience.py`:

* **busy** → answers inside the 60s window → the strike run resets and the
  proxy stays up. This is the whole fix.
* **dead** → no socket and no process of ours → fails on the *first* probe,
  buying none of the window. This is what preserves the human-pinned ~12s
  hard-down detection (plan_M1 appendix): 3 × 2s of strikes plus a
  connection-refused that resolves in milliseconds.
* **wedged** (F-501: dispatch loop dead, socket held open) → answers nothing
  for the whole window → still condemned, just confirmed first.

The gate is blocking, so it runs via `anyio.to_thread.run_sync` like the fast
probe — the stdio pump is never frozen.

The fast loop is untouched: same 2s interval, same 2s per-probe timeout, same
strike counter, same reset-on-success. Only the meaning of "three strikes"
changed, from *verdict* to *question*.

## Cost

Wedged detection now takes ~12s plus up to 60s of confirmation, instead of
~12s. That is the deliberate trade: the wedged case is rare and already has a
manual escape hatch (`stealth-chrome-devtools restart`), while the busy case
was firing in waves against a healthy backend several times an hour. Hard-down
detection is unchanged. A slow-but-healthy backend now costs two log lines
(`probe failed n/3`, then `was busy, not dead`) and nothing else.

## Residuals — NOT fixed here

1. **Why the backend has multi-second event-loop stalls under fleet load.**
   This finding makes the watchdog stop *misreading* those spans; it does not
   remove them. A shared backend that cannot answer `initialize` for 20–40s
   while serving is worth its own investigation (candidates: a blocking call
   on the event loop inside a tool body, CDP work not offloaded, GIL pressure
   from many concurrent sessions). Own finding.
2. **`backend-boot.log` is unrotated** — observed at **733 MB** on the
   reporting machine. `_start_server_process` appends every backend's raw
   stdout/stderr to one file forever (F-303/F-503 added the redirect precisely
   so crashes are not silent, and that part is working). Needs rotation or a
   size cap. Separate follow-up; deliberately untouched here.
3. **Wedged-verdict latency** is now ~72s worst case. If that proves too slow
   in practice the lever is `REUSE_PATIENCE_SECONDS`, which is shared with the
   cold-start gate — changing it is a policy decision for both consumers, not
   a watchdog-local tweak.

## Pins

`tests/test_watchdog_busy_vs_dead.py`:

* `test_a_passing_confirmation_resets_the_strike_run` — THE pin. Fast probe
  never answers, gate says alive: the watchdog must not return, and the strike
  run restarts (`[1, 2, 3, 1, 2, 3]`). RED before the fix with "DID NOT RAISE"
  — i.e. it tore down.
* `test_confirmation_is_not_consulted_before_the_strike_limit` — the patient
  gate is not paid for on every tick.
* `test_failed_confirmation_returns_on_the_third_strike` — teardown still
  lands on strike 3 with no extra tick, and logs a verdict distinguishable
  from the strikes. Guards the ~12s hard-down window.
* `test_default_seam_drives_same_identity_backend_ready_off_thread` — the
  one-policy pin: the default confirmation IS the cold-start gate, run
  off-thread.

The busy/dead/wedged behaviour *inside* the gate stays pinned where it already
was, in `tests/test_singleton_cold_start_patience.py` (untouched).

## Note on the LOC budget

`embedded/singleton.py` sat at exactly 1000/1000 (`tools/check_file_budgets.py`,
not grandfathered). The fix was written to its minimum honest size and the
remaining lines were paid for by densifying prose in the same
liveness/lifecycle region — the file lands back at **exactly 1000**. No cap was
raised or padded, and no rationale was dropped: every claim in the rewritten
comments survives, more tightly worded. Net `93 insertions / 93 deletions`.
