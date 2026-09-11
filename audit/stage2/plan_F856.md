# plan_F856 — a death verdict reached by an unscheduled prober is not evidence

**Finding**: F-856. **Incident**: 2026-09-02, machine at 3,445 processes, CPU
pegged at 100%. **Branch**: `fix/F856-starvation-aware-patience`.

---

## 1. What the product's own logs say

`proxy-176920.log`, one backend on port 52554, shared by every Claude Code
session on the machine:

| time | line | reading |
|---|---|---|
| 12:21–12:31 | `probe failed 1/3..3/3 on port 52554` ×6 runs, each followed by `backend on port 52554 was busy, not dead` | F-820's confirmation gate held off SIX times across ~11 minutes. It was right every time. |
| 12:32:27 | `backend on port 52554 confirmed unusable` | the SEVENTH confirmation ran out its 60s window and flipped. |
| 12:33–12:34 | `heal attempt 1/2 produced no ready backend`, `heal attempt 2/2 …`, `backend unhealable after 2 attempts`, `backend became unreachable; tearing down for reconnect` | 2 × 45s of readiness budget, spent against a cold start that could not finish in it. |
| after | `Failed to reconnect to stealth-chrome-devtools-mcp: CONNECTION_CLOSED` on every session sharing that backend | the one user-visible outage F-838 exists to prevent. |
| 12:37:47 → ~12:39:41 | new backend process tree born, **not serving for ~114s**; `backend-132420.log` shows ~90s inside `process_cleanup.recovery` — orphan reaping, temp-profile removal, several 5s force-kill waits per orphan — **before the first tool was served** | the heal deadline was unmeetable by construction. |

Two defects, coupled. Neither alone produces the outage; together they are a
machine for turning a load spike into a fleet-wide disconnect.

### 1.1 Defect one — a timeout is only evidence if the prober ran on time

Every liveness verdict in this tree bottoms out in a wall-clock deadline:
`_same_identity_backend_ready` gives a same-identity backend
`REUSE_PATIENCE_SECONDS` (60s) of `REUSE_PROBE_TIMEOUT` (10s) probes and
condemns it if none lands. That is sound arithmetic about the BACKEND only if
the prober itself was scheduled fairly. At 100% CPU with thousands of runnable
threads, the proxy's own `time.sleep(0.25)` returns late, its httpx client is
descheduled mid-request, and its 60 wall-clock seconds contain far less than 60
seconds of the attention the window was sized for. The window keeps counting
wall time; the evidence it is counting evaporates.

The busy-not-dead gate does not fix this — it only postpones it. Repetition
wins: each strike run re-rolls the same dice, and the seventh roll came up
"dead" for a backend that was demonstrably alive (it had answered six previous
confirmations, and the machine was merely saturated).

### 1.2 Defect two — a heal deadline the same starvation guarantees will miss

`HEAL_ATTEMPT_SECONDS` is 45s, twice. `proxy_selfheal`'s own comment sizes it
"generous against a cold start (STARTUP_TIMEOUT is 30s for the socket alone)".
It was sized against the SOCKET. But the proxy's readiness probe is
`_await_backend_http` — a real `initialize` answered 200 — and on the HTTP
transport that answer is gated by `app_lifespan`, which FastMCP runs on the
first MCP session. `app_lifespan` calls `process_cleanup.activate()`
**synchronously**, and under this load that call took ~90 seconds: it reaps
every browser a dead backend left behind, and each stubborn one costs a 5s
force-kill wait.

So a cold-started backend binds its socket in seconds and answers nothing for
~114s. 45s cannot see it. Neither can 2×45s, because attempt 2 finds the
cold-start lock still held by attempt 1's spawn and returns immediately.

The two halves compound: starvation manufactures a condemnation, and the same
starvation makes the recovery that follows the condemnation impossible.

---

## 2. Decision

**Adopt (a) and (b). Reject (c) as a verdict veto; keep the one thing it was
already doing right.**

### 2.1 (a) ACCEPTED — patience is spent in FAIRLY SCHEDULED seconds

New leaf `embedded/scheduling_lag.py`: **THE one home for "was this process
scheduled fairly, and what does a time budget owe it when it was not."** It
owns `FairWindow`, a patience window whose budget is charged in fair seconds
rather than wall seconds.

The measurement is self-contained and free: the reuse gate's wait loop already
sleeps between probes. That sleep is the calibration probe. Ask for 0.25s, wake
at 0.25s + δ; the ratio `actual / requested` is this process's own scheduling
lag, measured with a monotonic clock, needing no psutil, no system-wide CPU
poll, and no second thread. When the ratio is 1.0 — a healthy machine — the
window behaves EXACTLY as it does today, to the line. It cannot misfire when
the machine is idle, because the signal it reads is definitionally zero there.

Charging model, per loop iteration: elapsed wall time is divided by the
currently observed lag factor before it is deducted from the budget. "One
second observed while the scheduler was delivering a third of our requested
timing counts as a third of a second of patience." A hard wall stop at
`patience × MAX_STRETCH` guarantees termination.

Applied at exactly ONE site: `singleton._same_identity_backend_ready`. That is
the single home of "how long may a same-identity backend stay silent before it
is not ours any more", and it already has THREE consumers, all of which
inherit the fix without a second policy:

1. `_watch_backend_liveness`'s confirmation phase — the watchdog verdict
   (F-820), the one that fired in this incident;
2. `proxy_selfheal._confirm_bridge_verdict` via the injected `confirm_alive` —
   the bridge-first verdict (F-843);
3. `_start_backend_holding_lock`'s anti-fratricide grace (F-807) — the
   cold-start herd, which is *also* a starvation scenario.

`proxy_selfheal.drive` is untouched. The cause is still reported and never
branched on; there is still exactly one heal path. Starvation modulates how
long a silence must last before it becomes a verdict — nothing downstream of
the verdict changes.

**Bound**: `MAX_STRETCH = 4.0`. Justified from this incident: the confirmation
windows that SUCCEEDED did so repeatedly over 11 minutes, so the backend was
answering intermittently at a cadence a modestly wider window covers; the one
that failed needed more than 60s and, at the observed load, plausibly less than
240s. 4× also keeps the worst case honest against the surrounding budgets —
the proxy's own `BACKEND_READY_TIMEOUT` is 120s and in-flight calls simply wait
during the stretch, whereas the alternative outcome (condemn → heal fails →
teardown) is a hard `CONNECTION_CLOSED` for every session on the backend. A
starved wait is strictly better than a wrong funeral.

**Lifecycle report**: yes, it earns its place (F-827). A stretched window is a
DECISION — "I declined to believe this timeout" — not a crash, which is exactly
what `capture_lifecycle` is for, and without it the CONDEMNED/HEALED/TEARDOWN
series has no denominator for "how often did starvation nearly cause one". Emit
at most once per window, and only when the measured lag is material
(`REPORT_FACTOR`), so a healthy machine ships nothing. `capture_lifecycle`
promises never to raise and is THE one non-exception report home, so it is
called directly — no second never-raise wrapper.

### 2.2 (b) ACCEPTED — readiness before reaping

New leaf `embedded/serve_startup.py`: **THE one home for "startup work that
must not delay the backend's first serve."** `process_cleanup.activate()` keeps
installing its atexit/signal handlers synchronously — they are cheap, and they
must be armed before any browser can exist — and hands the reap to
`serve_startup.after_serving`, which runs it on a daemon thread.

**What recovery protects against, checked before moving it** (the ordering
question the brief demanded):

| protection | still holds when reaping runs concurrently? |
|---|---|
| kill browsers a DEAD backend left behind | yes. `browser_pid_registry.is_reapable` spares every entry whose owner backend is alive (F-808). This process is alive, so browsers spawned during the reap are never candidates. |
| registry consistency | yes. The reap ends in `_drop_recorded(reaped)`, which removes the reaped ids **by name** through the registry's read-merge-write protocol; entries written concurrently by `track_browser_process` are merged, never clobbered by omission. |
| delete an orphan's profile dir | yes. `_cleanup_profile_for_metadata` refuses to touch a custom (named-session) dir that is not an auto-clone, and auto-clone dirs are per-instance, so a fresh spawn can never be pointed at a dir the reap is deleting. |
| sweep stale `uc_*` temp profiles | yes, doubly. `_sweep_orphaned_temp_profiles` skips any dir belonging to a LIVE browser process and any dir younger than `browser_orphan_profile_max_age` — a just-spawned profile fails both tests. |
| racing an OLD backend's browsers | unchanged. Recovery never had a synchronous relationship with another backend; ownership, not timing, is what makes it safe (that is the whole F-808 fix). |

One accepted degradation, named: `clone_storage.spawn_background_sweep`
("startup"), already fire-and-forget at the same boundary, may now run *before*
the reap and therefore spare a not-yet-reaped orphan's clone dir. That is a
leak retried on the next sweep, never a wrong deletion — the conservative
direction, and the direction that subsystem already fails in.

`clone_storage.spawn_background_sweep` is deliberately NOT folded into
`serve_startup`: it is an asyncio task with an in-flight dedupe and trigger-time
root capture, called from four sites, only one of which is startup. Different
lifetime, different dedupe, different callers — a fold-in would be a widening,
not a consolidation.

### 2.3 (c) REJECTED as a verdict veto — and it is already doing its correct job

"The recorded pid is alive, therefore not dead" cannot be allowed to veto a
condemnation, because a WEDGED backend — dispatch loop dead, socket still open,
process still resident — passes it forever. F-301/F-501 exist precisely because
a socket-level check could not see that state; a pid-level veto is the same
mistake one layer down, and it would make the watchdog structurally incapable
of condemning the exact failure it was built for.

Note also that `_same_identity_backend_ready` **already consults exactly this
signal**, in the only place it is sound: `_is_our_backend(entry.get("pid"))`
guards the FAST-FAIL. No socket AND no process of ours ⇒ dead immediately, skip
the wait. Liveness of the pid is used to withhold an instant death verdict, and
never to grant an eternal reprieve. Adding it as a veto would be a second way to
answer a question that already has a home.

(The uv-trampoline caveat is real but does not change the ruling: the recorded
pid can be a shim whose identical-cmdline child does the work. `_is_our_backend`
matches on the command line, and shim and child carry the same one, so the
predicate answers the same for both — and the two die together in practice. It
is fine where it is used and would be no better as a veto.)

### 2.4 Rejected: just raise the numbers

`REUSE_PATIENCE_SECONDS = 180`, `HEAL_ATTEMPT_SECONDS = 120`. Rejected: a fixed
number cannot distinguish "the backend is slow" from "we are slow", so every
raise buys starvation tolerance by making a genuine hard-down slower to detect
for everyone, on every machine, forever. The whole point of (a) is that the
window widens only in the conditions that make the narrow window wrong.

### 2.5 Rejected: a `STEALTH_MCP_*` knob

Out of scope by construction — unknown keys crash `get_settings()`, and the
owner's standing rule is universal defaults, not operator knobs. Both constants
are module-level with measurement-justified comments, like
`REUSE_PATIENCE_SECONDS` before them.

---

## 3. The LOC problem, and the extraction it forced

`singleton.py` sits at 1000/1000 — the ungrandfathered budget — so the change
above cannot be written there until something leaves. Per the repo's own
precedent (`proxy_selfheal.py`, `backend_registry.py`), the answer is an
extraction, never a cap raise.

**Extracted: `_watch_backend_liveness` → new `embedded/backend_watchdog.py`.**
It is the largest single function in the file (63 lines), it is a self-contained
algorithm that already takes every collaborator through an injectable seam, and
it is one of the two mechanisms this finding changes the meaning of — the LOC
gate forced the question, but the answer was already the right one. The new
module is a leaf: it takes the two probes as ARGUMENTS, so it never imports
`singleton` and the reuse gate stays single-homed.

`singleton._watch_backend_liveness` survives as the wiring: it binds the
production probes at call time and forwards. That keeps `singleton` the place
that knows which probes are ours, keeps `drive(watch=…)`'s call site unchanged,
and keeps every existing `monkeypatch.setattr(singleton, "_backend_http_ready",
…)` steering the watchdog exactly as it does today.

`process_cleanup.py` (1021/1023) pays for its own two-line change by collapsing
one boilerplate `Args:`/`Returns:` docstring block — the same payment mechanism
the F-809 row in `check_file_budgets.py` already records. **No cap is raised.**

---

## 4. Implementation

| # | file | change |
|---|---|---|
| 1 | `embedded/scheduling_lag.py` (new) | `FairWindow`, `MAX_STRETCH`, `REPORT_FACTOR`, the one lifecycle report |
| 2 | `embedded/backend_watchdog.py` (new) | `watch_liveness` — F-820's strikes + confirmation, verbatim, probes injected |
| 3 | `embedded/serve_startup.py` (new) | `after_serving` — run one startup job off the first-serve path |
| 4 | `embedded/singleton.py` | reuse gate spends a `FairWindow`; `_watch_backend_liveness` becomes the wiring |
| 5 | `embedded/process_cleanup.py` | `activate()` defers the reap; docstring payment |
| 6 | `CLAUDE.md`, `CHANGELOG.md` | three new homes, one moved home |

## 5. Tests (each must be shown RED first)

| test | mechanism it pins | red without the fix |
|---|---|---|
| `test_scheduling_lag.py::a healthy window is charged wall time` | fake clock, zero lag ⇒ identical behaviour to today | module does not exist |
| `…::a starved window outlives its wall patience` | fake clock+sleep, 4× lag ⇒ survives past `patience` | module does not exist |
| `…::the stretch is bounded` | lag 100× ⇒ still expires by `patience × MAX_STRETCH` | module does not exist |
| `…::a material stretch is reported once` | `capture_lifecycle` spy | module does not exist |
| `…::a healthy window reports nothing` | spy sees zero calls | module does not exist |
| `test_singleton_starvation_patience.py::a starved prober does not condemn a silent backend` | patch `_backend_http_ready` False + a lagging `time.sleep` ⇒ gate keeps probing past 60 wall-seconds | RED: today's fixed deadline returns False at 60s |
| `…::an unstarved prober still condemns on schedule` | zero lag ⇒ the F-807 eviction still lands | guards against over-fixing |
| `test_serve_startup.py::the job runs off the caller's thread` | records thread ids | module does not exist |
| `…::a raising job never reaches the caller` | job raises ⇒ `after_serving` returns | module does not exist |
| `test_process_cleanup_deferred_reap.py::activate returns before the reap finishes` | reap blocks on an event ⇒ `activate()` returns anyway | RED: today `activate()` blocks |
| `…::handlers are installed before the reap is handed off` | order recorded | pins the one thing that must stay synchronous |

`tests/test_singleton_cold_start_patience.py` is NOT modified — its
`REUSE_PATIENCE_SECONDS = 0.0` cases and its no-op `time.sleep` produce zero
measured lag, so a `FairWindow` behaves identically to the deadline it replaces.
Verified, not assumed.

## 6. Verification

Full unit lane, `tests/test_startup_herd.py` (the guard for any singleton
port/lock/patience change), `tools/check_file_budgets.py`, `ruff format
--check`, `ruff check`, `ty`, `vulture`, `tools/check_suppression_owners.py`.
