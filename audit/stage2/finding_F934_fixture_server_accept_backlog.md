# F-934 — the fixture server queued five connections, and a six-browser fleet opens up to thirty-six

**Severity:** None for users, since no product code changes. Medium for this
repo's CI: the release gate's `integration (Windows/X64)` cell went red on 3 of
the last 6 runs, always in the same node, and passed on rerun.
**Files:** `tests/release_gate_harness.py` (the fix),
`tests/test_fixture_dynamic_routes.py` (the pin).
**Depends on:** F-933, which makes the next failure name Chrome's own
`net::ERR_*` code.

---

## 1. Symptom

`tests/test_e2e_fleet.py::test_a_fleet_of_six_browsers_answers_truthfully_about_every_page`
points six browsers at one fixture origin. A `navigate` in that node landed on
Chrome's error page: `/cov/form.html` in run 36268702476 attempt 1 (PR #169's
gate) and `/cov/slow_load.html?ms=1200` in run 36280896091 attempt 1 (the
v2.1.15 publish). Byte-identical trees passed on rerun. The URL was loopback,
which rules out DNS and TLS, but the message before F-933 could not say which
network error Chrome had hit.

## 2. Measurement

All of this was measured on this host (Windows 11 10.0.26200), with stdlib
`ThreadingHTTPServer` subclasses differing only in `request_queue_size`.

- **Nothing accepting.** The server is bound and listening, and its accept
  loop never runs. At backlog 5, connects 1–5 queue, and the 6th fails with
  `ConnectionRefusedError` after **2.03 s**. At backlog 128, 40 of 40 queue.
- **Accept loop running, idle host.** 36 concurrent connects: no refusal in 20
  trials at either backlog. When the loop runs, it keeps up.
- **Accept loop stalled, then started.** 36 concurrent connects arrive while
  the loop has not run yet:

  | stall | refused at backlog 5 | refused at backlog 128 |
  |---|---|---|
  | 0.3 s | 0 | 0 |
  | 1.0 s | 0 | 0 |
  | 2.5 s | **30 of 36** | 0 |

  The threshold is the client's SYN retry window. Windows retries a refused
  SYN for about 2 s. If the queue has drained by then the connect succeeds,
  and if it has not, the connect fails.

## 3. Root cause

`_bind_origin` built a plain `ThreadingHTTPServer`. Its `request_queue_size`
is the stdlib default of 5, and `server_activate` calls `listen(5)`. The fleet
runs six browsers against one origin, each with up to six connections to a
host (Chrome's per-host limit), so the burst can reach 36. When the accept
loop falls about 2 s behind, every connection past the fifth is refused.
Chrome then commits its error page (`net::ERR_CONNECTION_REFUSED`), and the
node reports a failed navigation. Six Chromes starting at once on a 2-vCPU
runner can hold the accept loop back that long.

**What is proven and what is not.** Proven: this failure takes nothing more
than an accept loop about 2 s behind, and at backlog 128 the same stall
refuses nothing. Not proven: that the CI loop actually stalled, because the
failing runs predate F-933 and do not carry Chrome's code. If the fleet goes
red again after this fix and F-933 names `net::ERR_CONNECTION_REFUSED`, the
backlog was not the cause, or not the only one.

## 4. Fix

- `FIXTURE_ACCEPT_BACKLOG = 128`, and `_FixtureServer(ThreadingHTTPServer)`
  carries it as `request_queue_size`. `_bind_origin` builds a `_FixtureServer`.
  `serve_fixture_app` and `serve_fixture_origin_pair` both bind through
  `_bind_origin`, so every fixture origin gets the new backlog, and there is
  still one serving mechanism.
- The value is a class attribute, not set on the instance, because
  `ThreadingHTTPServer`'s constructor binds and listens. The size has to be in
  place before construction.
- Why 128: it is well above 36, and it is the largest backlog every CI kernel
  honours as asked. macOS clamps `listen()` to `kern.ipc.somaxconn` (128 by
  default), Linux clamps to `net.core.somaxconn` (4096 since kernel 5.4), and
  Windows takes it as given (the 40-of-40 row above).

## 5. Tests

`tests/test_fixture_dynamic_routes.py::test_a_stalled_origin_still_queues_the_whole_fleets_connections`
is in the unit lane and uses no Chrome. The pin binds an origin through
`_bind_origin` and never serves it, which is an accept loop stalled for as long
as the test runs, so it needs no load and no timing. It then opens
`FLEET_SIZE × 6` = 36 connections, each with a 3 s budget, and names the first
one that does not queue. `FLEET_SIZE` is imported from `test_e2e_fleet`, so a
larger fleet raises the demand the pin checks.

- **RED at 13bfc45.** The run used main's harness in an out-of-tree
  `git archive` export, with only the new test file copied in. Result:
  `connection 6 of the fleet's 36 was not queued: ConnectionRefusedError(10061, …)`
  in 8.71 s. On Linux, RED looks different: the kernel drops a SYN that
  arrives while the accept queue is full, so the connect hangs until its
  budget. I measured this on WSL2's Linux 6.18 (`net.core.somaxconn` 4096)
  with the same stdlib server and 3 s budget. At backlog 5, six connects queue
  and the 7th fails with `TimeoutError` at 3.0 s. At backlog 128, all 36
  queue. macOS was not measured.
- **GREEN with the fix:** `31 passed` over the whole of
  `test_fixture_dynamic_routes.py`, which is the 30 existing nodes plus the
  pin.
- The fleet node itself runs in the PR's integration cells.

## 6. Residuals and cost

1. The cause of the CI failures is unproven (§3). F-933 is what will tell.
2. Three other test stubs build a `ThreadingHTTPServer` with backlog 5:
   `test_backend_liveness_probe.py`, `test_find_running_server_app_probe.py`
   and `test_probe_backend_status.py`. Each answers one client's probes, not a
   browser fleet, so they are unchanged.
3. Chrome's six-per-host limit is for HTTP/1.1 connections per profile.
   Speculative preconnects can add a few more, and 128 leaves room for them.
4. Cost: a listen queue of 128 entries on each fixture origin. There is no
   runtime cost in the product.
