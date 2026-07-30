# F-801 — on Linux a named profile never reads as free after `close_instance`, so the documented same-profile restart silently resolves a different profile

**Status: OPEN.** Opened by the RELEASE integration-gate arbitration, 2026-07-30.
**Severity: HIGH** — the product's own "is this profile in use?" predicate stays
`True` indefinitely after the browser that held it was closed. Every caller that
respawns a named session on Linux therefore gets a **numbered variant profile
freshly cloned from master** instead of the profile it named, and nothing in any
return value says so. The documented same-profile persistence lifecycle — the
whole point of `session_name` — does not hold on Linux.

---

## What is proven

`integration (Linux/X64)` is the only red cell of the release gate, and it is red
for exactly one node:

```
tests/test_stateful_i18n.py::test_storage_and_cookies_survive_one_profile_and_no_other
tests/test_stateful_i18n.py:757: assert await browsers.await_profile_free(kept) is True
E   assert False is True
```

**It is deterministic, not a flake.** Two gate runs on **byte-identical trees**
produced the identical failure:

| run | commit | node | assertion | node time |
|---|---|---|---|---|
| 30512817900 | `c95b16c` | same | `assert False is True` | 62.12s |
| 30513555594 | `031decf` (empty arbitration commit) | same | `assert False is True` | 62.07s |

`031decf` is an empty commit on top of `c95b16c`, so the two runs certify the
same tree. Both junit files name **one** failure and it is the same one. This is
the inverse of the F-779 pattern (identical tree, *different* conclusion): here
the identical tree yields the identical conclusion twice, which is what
determinism looks like.

Also proven by the same two runs:

* **Linux-only.** `integration (Windows/X64)` and `integration (macOS/ARM64)`
  ran this node and **passed** it on both runs. F-779 did not fire on either
  run, so the macOS green is a real pass and not a lucky one.
* **The other 127 integration nodes passed on Linux**, including all 15
  wire-semantics nodes. Nothing else in the cell is unhealthy.
* **The predicate returns `False` for a full 30 seconds.** `await_profile_free`
  polls at 0.25s until `PROFILE_RELEASE_TIMEOUT = 30.0`, then returns the
  predicate one last time. The node's ~62s runtime is consistent with the seed
  phase plus that entire budget elapsing. The barrier did not lose a race by a
  hair; the profile never became free at all.

The failing call is the **product's own** predicate, not a test-side
approximation:

```python
# tests/test_stateful_i18n.py — the barrier
clone_storage._profile_has_running_browser(directory)
```

## Why this is a product defect and not a test bug

The barrier exists because of a documented product behaviour, recorded in the
test's own comment and in `await_profile_free`'s docstring: `spawn_browser` on a
named profile that is **still held** does not fail — it resolves a numbered
variant (`…-2`) cloned fresh from master. That is a reasonable thing for the
product to do when a profile really is busy. It is a silent disaster when the
predicate is *wrong* about busy.

So the Linux consequence is not "a test times out". It is:

> On Linux, closing a named session and respawning it returns a **different,
> empty profile**, and the caller is never told. Local storage, IndexedDB,
> CacheStorage and persistent cookies all appear to have vanished, when in truth
> they are intact in a profile directory the product declined to reuse.

The node is the messenger. Its assertion is the correct contract and is left
byte-for-byte unchanged; only the marker scopes the known-red platform.

## Mechanism — PARTLY INFERRED, not confirmed

Job logs require admin rights (the unauthenticated REST API returns 403), and no
Linux machine was available in this session, so **nothing below was observed**.
What follows is derived from reading the predicate. Treat it as where to look.

`clone_storage._profile_has_running_browser` is two checks in sequence
(`clone_storage.py:78-97`):

1. `process_cleanup._get_browser_pids_for_profile(directory)` — non-empty ⇒ busy.
2. Otherwise, a **marker-file fallback**: busy if any of `SingletonLock`,
   `SingletonSocket`, `SingletonCookie` exists in the profile directory.

Either branch alone is sufficient to keep the profile reading busy, and the
evidence available cannot distinguish which one fired. Both are Linux-plausible:

**Candidate A — a surviving process still matches.**
`_get_browser_pids_for_profile` (`process_cleanup.py:292-328`) scans
`psutil.process_iter`, keeps processes whose **name** contains any of
`chrome`/`chromium`/`msedge`/`edge`/`brave`, then requires the cmdline to carry
`--user-data-dir=<profile>` (the `=` form, or `--user-data-dir` followed by the
path — nothing looser). On Linux every Chrome helper and
`chrome_crashpad_handler` is named `chrome*`, so any one of them that outlives
`close_instance` while carrying the profile in its cmdline pins the predicate to
busy for as long as it lives.

**Candidate B — the POSIX singleton files survive.** Chromium's POSIX
`ProcessSingleton` creates `SingletonLock`, `SingletonSocket` and
`SingletonCookie` inside the user-data-dir and removes them on a clean exit. An
unclean teardown leaves them, and the fallback then reports busy with **zero**
live processes. This branch cannot fire on Windows at all — the Windows
`ProcessSingleton` uses a named mutex and creates none of those files — which is
one clean explanation of the Windows green.

### A correction worth recording, so the next reader does not re-derive it

An intuitive reading of the cross-platform split is "macOS helpers are named
`Google Chrome Helper`, so the name filter excludes them." **That reading is
wrong.** The filter is a lowercased substring test, and
`"google chrome helper (renderer)"` contains `"chrome"`. macOS helpers pass the
name filter exactly like Linux ones do. Whatever distinguishes macOS from Linux
here, it is *not* the name check — it is either which processes actually survive
teardown on each platform, which of them carry `--user-data-dir=`, or whether
the singleton files get cleaned up. That question is open.

The honest summary: **Linux-only is proven; which of the two branches makes it
true is not.**

### How to confirm it cheaply

On any Linux box, after a `close_instance` on a named session, poll for 30s:

* `psutil` for processes matching the filter — prints Candidate A's answer,
  and names the surviving process if there is one;
* `ls` the profile dir for `Singleton*` — prints Candidate B's answer.

One run separates them. Do that before writing any fix; the two candidates have
entirely different repairs.

## Evidence in the tree

`tests/test_stateful_i18n.py::test_storage_and_cookies_survive_one_profile_and_no_other`
is marked `@pytest.mark.xfail(sys.platform.startswith("linux"), strict=True)`.
`strict=True` is chosen **because determinism is proven** by the two
byte-identical runs above: the moment F-801 is fixed the node passes on Linux,
the strict xfail turns that pass into a failure, and the gate forces this finding
to be closed and the marker removed in the same commit. A non-strict xfail would
let the fix land silently and leave the marker rotting in the tree.

The Linux integration cell no longer emits `--mq "MQ-160"`
(`.github/workflows/release-gate.yml`). `tools/release_evidence.py::verify_claims`
requires a claimed node to have executed **and passed** on every cell claiming
it, and an xfail is not a pass — so a Linux cell that kept emitting the id would
be claiming evidence it does not have. Per-cell `mq_ids` sets already differ by
design (W13 emits the transport ids from the transport cell only), and
`_check_mq_ids` merely unions them against `REQUIRED_MQ_IDS`, so dropping one id
from one cell is structurally fine.

`tests/MANUAL_QA_PROTOCOL.md` MQ-160 is qualified to Windows/macOS accordingly.

## What closing it requires

A `src/` change in the `close_instance` ⇄ `_get_browser_pids_for_profile`
interaction, once the confirmation run above says which branch is at fault:

* **If Candidate A** — `close_instance` must not return until the processes that
  hold the profile are actually gone, or the predicate must stop counting a
  process that is exiting. Note the shape here is adjacent to F-779's: a
  teardown that reports done while something it spawned is still alive.
* **If Candidate B** — the teardown must remove the singleton files it is
  responsible for, or the fallback must not treat a stale lock as authoritative.
  A stale-lock fallback that can never be cleared is a liveness bug regardless.

**Do not "fix" this by widening `PROFILE_RELEASE_TIMEOUT`.** Thirty seconds of
`False` is not a slow release; nothing indicates the predicate would ever flip.
A longer timeout would only make the node take longer to report the same defect,
and would leave the real user-facing behaviour — a silent respawn onto the wrong
profile — completely untouched.

**This is a `src/` change and plan_RELEASE forbids `src/` edits, so it belongs to
a FIX plan, not here.**

## Relationship to neighbouring findings

- **F-789** (`close_instance` returns `False` for a browser that has already
  died) is the closest relative and worth fixing alongside: both are cases where
  `close_instance`'s *report* disagrees with the state of the world. F-789 is a
  false negative on success; F-801 is a false positive on "still in use" from the
  predicate that observes the same teardown. A fix that makes teardown-completion
  observable would plausibly settle both.
- **Not F-779.** F-779 is a macOS/ARM64 teardown flake — identical tree, *different*
  conclusion, ~1 run in 4. F-801 is Linux, deterministic, and reproduces on an
  identical tree. Same subsystem (teardown), opposite reliability signature.
- **Not F-773.** F-773 is why macOS makes no navigation claim; it does not touch
  the profile lifecycle.

## Disposition

- The gate is honest at HEAD: Linux xfails the node and claims no MQ-160.
- **The `session_name` persistence lifecycle is unclaimed on Linux** until this
  closes. Anyone reading the release contract should read it that way rather
  than as "storage persistence works everywhere."
- Correct owner is a FIX plan. The first task is the confirmation run above, not
  a patch.
