# F-909 — two pins raced a wall clock, and a slow Windows runner won

**Status:** fixed (tests only; no `src/` change)
**Found by:** the release gate on PR #150, two Windows cells only, both green on
PR #149's gate an hour earlier
**Severity:** low for the product, high for the gate — both failures were
*true assertions about things that never happened*, which is the shape that
costs the most time to read

---

## 1. What failed

| Cell | Node | Reported |
|---|---|---|
| `unit-tests (Windows/X64 py3.12)` | `tests/test_browser_reattach.py::TestOneDoor::test_the_adoption_path_goes_through_the_reclaiming_attach` | `AssertionError: the adoption timed out mid-attach and left its connection open` |
| `integration (Windows/X64)` | `tests/test_stateful_i18n.py::test_service_worker_installs_activates_controls_and_unregisters` | `AssertionError: assert 'uncontrolled' == 'controlled'` |

Neither is a product defect and neither is caused by F-903's operator fence,
which was the only delta between the two gates. Both are races **in the pins**,
whose windows a loaded Windows runner opened.

---

## 2. Not the fence — measured, not argued

The suspicion was reasonable: #150 adds `tests/operator_fence.py`, which wraps
every filesystem primitive the product uses and guards `psutil`'s kill
primitives. Three measurements retire it.

**2.1 The fence costs a locked record write nothing.** 60 real
`browser_pid_registry.claim_browser` round trips against a tmp record, in a
process with `operator_fence.install(...)` and in one without, twice each:

| | median | mean | p90 | max |
|---|---|---|---|---|
| no fence, run 1 | 5.087 ms | 5.195 ms | 6.256 ms | 7.671 ms |
| **with fence, run 1** | **3.806 ms** | 3.909 ms | 4.865 ms | 5.671 ms |
| no fence, run 2 | 4.148 ms | 4.246 ms | 5.332 ms | 6.410 ms |
| **with fence, run 2** | **4.235 ms** | 4.306 ms | 5.320 ms | 7.418 ms |

Inside the noise, and twice *faster* with the fence. The reason is in the code:
`operator_fence._designated_roots()` is cached on `_TABLE_CACHE`, so
`_root_spellings`' `realpath` + `GetShortPathNameW` + UNC/`\\?\` expansion runs
once per table rebuild, never per call; the hot guard `_designated` is a
`casefold`, a substring test and at most one `normpath`, with no syscall.

**2.2 The fence is not in the reattach node's path.** Time from
`adopt_held_profile` entry to the attach door, five samples each, against the
node's own patched 250 ms budget:

| tree | samples (ms) |
|---|---|
| fence branch `2d3bab6` | 3.5 / 3.2 / 2.9 / 2.9 / 3.8 |
| main `f397de4` | 4.2 / 2.7 / 2.4 / 2.5 / 3.6 |

An ~80× margin, identical on both. The kill guard wraps only
`psutil.Process.terminate`/`kill`/`send_signal` and `os.kill`, and only ever
*raises* for a protected pid; `cdp_attach`'s reclaiming closer calls
`Connection.disconnect`, which is none of those.

**2.3 The fence cannot reach the service worker.** Its session-root read guard
is armed on `REAL_SESSION_ROOTS`, captured *before* the redirect, so the forced
tmp session root the test's profile lives under is never a designated root —
and a hit raises `RealSessionRootAccess`, a `BaseException`, which is a loud
error and never the string `'uncontrolled'`. Chrome is a subprocess, which the
fence's own docstring lists among what it deliberately does not wrap.

**2.4 Neither node reproduces on either tree.** Same machine, same env:
reattach 5/5 and 5/5 (whole file) on both; service worker 5/5 and 5/5 on both.

---

## 3. The reattach node: the budget was spent before the door

The CI traceback is the whole diagnosis:

```
  File ".../embedded/browser_reattach.py", line 859, in _adopt_one
    async with browser_claim.held(
  File ".../embedded/browser_claim.py", line 109, in held
    claimed = await asyncio.shield(claiming)
asyncio.exceptions.CancelledError
```

`asyncio.wait_for(_adopt_one(...), timeout=ATTACH_BUDGET_SECONDS)` starts its
clock **before** the claim, and the claim is a real locked `browser_pids.json`
read-merge-write on a worker thread. The node patched the budget to 0.25 s and
made the door sleep 0.6 s, so it was racing two wall clocks at once. On the
runner the claim won: `_adopt_one` was cancelled at the claim, no `Browser` was
ever produced, nothing was opened — and the node then waited 5 s for a
connection that had never existed and reported it as *left open*. It cost 8 s
and named the wrong thing.

**Reproduced deterministically** by stalling the claim's worker-thread hop past
the budget (`patch.object(browser_reattach, "claim", …)` + `time.sleep(0.4)`)
and running the SHIPPED node unchanged — identical assertion, identical
`browser_claim.py:109` frame, identical `TimeoutError` WARNING.

### 3.1 The fix: every edge is an event

- The claim is stubbed to the one thing this node is about — it was **taken**
  and it was **handed back**, both now asserted, where before they were assumed
  and never checked. No disk in the measured window. The real claim keeps its
  own pins in `TestTheCrossProcessClaim`.
- The door **parks on an `asyncio.Event`** instead of sleeping, so the budget is
  *provably* spent inside the attach rather than probably.
- The default thread pool is warmed with one `await asyncio.to_thread(int)`
  before the call, so the first-`to_thread` worker spin-up is not inside it.
- **Reaching the door is its own assertion**, with its own message, so the
  pre-door stall can never again present as the post-door leak.

The budget stays 0.25 s. Widening it would have hidden the race instead of
removing it, and the node is about a budget that fires *mid-attach* — a bigger
number makes that less true, not more.

### 3.2 It still catches what it exists for

Mutation check: with `cdp_attach.attach_reclaiming(` replaced by
`cdp_attach.attach(` in `browser_reattach.py`, the new node fails with its own
message (`1 failed, 3 passed`). Source and `__pycache__` restored afterwards and
re-verified. And it passes **under the same 0.4 s claim stall that breaks the
old one** — the race is gone, not hidden.

---

## 4. The service worker: the fixture gated on the wrong promise

`w16Register` set `state = 'ready'` off `await navigator.serviceWorker.ready`,
and the node polled that state and then read `navigator.serviceWorker.controller`
in the same round trip. **`ready` resolves on an ACTIVE REGISTRATION and says
nothing about this document.** `clients.claim()` reaches a page as its own task —
and this worker's `activate` handler awaits a real network round trip to the
fixture ledger *before* it calls claim at all:

```js
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    await fetch(CFG.report + '?phase=activate&sentinel=' + …);
    await self.clients.claim();
  })());
});
```

So the node read `controller` in the gap between the two. On a fast machine the
gap is invisible; on the runner it was not.

**Reproduced causally** by injecting 1500 ms in front of `self.clients.claim()`
(`monkeypatch.setattr(fixture_routes, "_SERVICE_WORKER_JS", …)`) and changing
nothing else — **identical output on the fence branch and on main**:

```
READY_STATE=ready CONTROLLER_AT_READY=uncontrolled
E   AssertionError: assert 'uncontrolled' == 'controlled'
```

`READY_STATE=ready` beside `CONTROLLER_AT_READY=uncontrolled` is the finding in
one line.

### 4.1 The fix: a distinct `controlled` state, and no sleeping

`w16Register` keeps `ready` for "the registration is active" and adds
`controlled`, reached by awaiting `controllerchange` — **re-checking
`navigator.serviceWorker.controller` after arming the listener**, because a
claim landing between the check and the `addEventListener` would otherwise
leave the page waiting forever for an event that had already fired.
`_register_service_worker` polls for `controlled`/`error` and asserts
`controlled`.

Nothing sleeps and nothing is platform-branched. Under the *same* 1500 ms
injection the node now reports `STATE=controlled CONTROLLER=controlled` and
passes.

A worker that activates and never claims now stops at `ready`, and the poll's
own failure reports that state — which names the problem instead of answering
with a wrong value. That is deliberately better than the previous behaviour,
where such a worker produced `controlled: 'uncontrolled'` and read as this bug.

All three callers want a controlled page, including F-800's control step
(`navigated["controllerAtLoad"] == "controlled"`), which was itself relying on
the first registration having taken control before the second navigation — so
that node is more reliable too, and its inverted `reload_page` assertions are
untouched.

---

## 5. Verification

| Run | Result |
|---|---|
| `tests/test_browser_reattach.py` whole, ×5 | 91 passed each (4.08–4.21 s) |
| the reattach node under the 0.4 s claim stall | passes (it fails the stall on main) |
| reattach mutation `attach_reclaiming` → `attach` | fails with its own message |
| `tests/test_stateful_i18n.py` whole, ×19 | 18 green; **one run failed and its name was lost** — see §6 |
| the service-worker node alone, ×15 | 15/15 green |
| the service-worker node under the 1500 ms claim injection | passes (fails it on main) |
| fixture consumers + registry + cleanup batch (5 files) | 207 passed |

Live state untouched: `server.json` SHA-256
`1B24991EBA294507195B2BAC386A56BA78B0300B6B26837FF7360314F6D2ADAB` before and
after; backend pids 47424 (port 3881) and 167540 (port 35273) alive throughout
and the only listeners among the six protected ports; `chrome.exe` count 89
before and 89 after every real-Chrome batch, none of them started or ended here.

---

## 6. Residuals

1. **One unattributed failure.** In the first batch of five whole-file
   `test_stateful_i18n.py` runs, run 4 reported `1 failed, 10 passed` and the
   node's NAME was lost, because that batch filtered pytest's output to its
   summary line. Every run since has captured names: 14 more whole-file runs,
   all 11/11 green, plus 15/15 on the node this finding changed. So it is not
   attributed, and it is not attributable to either fix by evidence — it is
   recorded here rather than rounded away. The machine was carrying 89
   pre-existing `chrome.exe` processes from other agents throughout, which is
   the documented local capacity-flake surface.
2. **The reattach node no longer exercises a real claim.** That is the trade
   this finding makes deliberately: `TestTheCrossProcessClaim` is where the real
   locked write is pinned, and a pin that needs *both* a real file write and a
   250 ms deadline is asking a wall clock to arbitrate a correctness question.
3. **The `to_thread` hop remains inside the budget.** `browser_claim.held` runs
   the claim on a worker thread by design, so a warmed-pool hop (tens of
   microseconds) is still in the measured window. If an event loop is denied
   250 ms of scheduling, this node will fail — but so will most of the suite,
   and it now fails saying *the door was never reached* rather than lying about
   a connection.
4. **`controllerchange` is not a guarantee of anything but control.** The new
   state says this document is controlled; it does not say which worker version
   controls it. No node here asks that, and MQ-157's version sentinels are read
   from the SERVER ledger, not from the controller.
