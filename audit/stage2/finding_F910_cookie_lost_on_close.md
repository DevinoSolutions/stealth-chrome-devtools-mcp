# F-910 — `close_instance` killed Chrome while Chrome was still saving

**Status:** fixed (product change in `close_instance`, new leaf
`embedded/process_exit.py`)
**Found by:** `tests/test_stateful_i18n.py::test_storage_and_cookies_survive_one_profile_and_no_other`
failing on the Windows integration cell of **two unrelated branches on one
day** — PR #150 attempt 2 and PR #151 (`fix/F906-…`, a logging-only change) —
with `AssertionError: assert '' == 'w16_persistent=w16-cookie-persistent-value'`
**Severity:** data loss. A login made shortly before a session is closed can be
gone when that session is re-opened.

---

## 1. What the failure actually said

Read the direction carefully: the pin's message is *"the session cookie must
not survive"*, but what failed was the opposite — the **max-age** cookie was
gone. And the two assertions above it PASSED:

| line | assertion | result |
|---|---|---|
| 791 | the respawn resolved the SAME profile directory | PASS |
| 797 | `localStorage` survived the restart | PASS |
| 799 | exactly the max-age cookie survives | **FAIL — `''`** |

Same directory, same profile, localStorage intact, cookie jar empty. That is
not a wrong profile and not a pin reading too early (a re-opened Chrome blocks
a `document.cookie` read until its store has loaded). It is a cookie store that
was never committed: `localStorage`'s leveldb flushes eagerly, cookies are
batched and written on a clean shutdown.

---

## 2. Not the fence, not Chrome, not the node's timing

**Not the fence.** Reproduced on `origin/main` `f397de4`, where F-903's
operator fence does not exist. `git diff --name-only origin/main` on the tree
that first reproduced it named only CHANGELOG, an audit file and three
`tests/` files — the product was byte-identical to main.

**Not a Chrome update.** All three runs ran Chrome **152.0.7977.83**: the two
failures AND the last green Windows integration run on main (run 35584911403,
09:43 UTC), where this node **PASSED** at 10:04:36. One build, one pass and two
failures inside ~50 minutes. A runner-side flip explains none of it; a race
explains all of it.

**Not the test harness either — asked and MEASURED after §7.6's placeholder
stores were found.** The question was sharp: `tests/conftest.py`'s
`tmp_session_root` writes 18-byte placeholders where Chrome keeps SQLite, and a
browser opened on one of those keeps its cookie jar in MEMORY, which is this
finding's symptom manufactured by a fixture. If the CI node ran on such a
profile, the attribution would be the harness and not the product.

| question | answer, measured |
|---|---|
| which session root does the node spawn into? | `%TEMP%\stealth-mcp-test-browser-sessions` — `tests/conftest.py`'s IMPORT-time `os.environ.setdefault("STEALTH_MCP_BROWSER_SESSION_ROOT", …)` (F-841's fence), or the gate's `runner.temp`. `test_stateful_i18n.py` mentions `tmp_session_root` **nowhere** and redirects nothing: the chain is the `browsers` fixture → `spawn_browser(user_data_dir="w16-persist-<hex>")` → role `explicit` → `<root>/sessions/w16-…`, seeded from `<root>/master-snapshot` |
| does that chain carry a placeholder store? | **NO.** Zero files under 100 B named `Cookies` / `Login Data` / `Web Data` anywhere under that root; `master` and `master-snapshot` each carry a real **20 480 B** cookie database |
| the node, 10× on this fix | **10 passed** |
| the node, 5× with the graceful close SUPPRESSED and the fix in place | **5 failed**, every one with the CI failure's own words: `AssertionError: exactly the max-age cookie survives; the session cookie must not` |

So the placeholder is absent from this node's chain and cannot be the CI cause;
and the node's cookie assertion is directly sensitive to a truncated Chrome
shutdown, which is what F-910 is. That last row is also the positive control
for §5's disagreement: the stalled arm reproduces the CI failure **with the fix
in place**, so it cannot be the fix's RED. The fixture is fixed anyway (§7.6).

**No timing signature in the node itself.** Durations, measured from the
previous result line: green PASS 6.0 s, #151 FAIL 17.3 s, #150 FAIL 3.4 s. The
failures abort at line 799 so they are not comparable to a pass, and they sit
on either side of the green one. Nothing to read.

---

## 3. The mechanism, measured

`close_instance` is three phases. Phase 2 sends `Target.closeTarget` per tab
(2.0 s each), `Browser.close` (2.0 s) and `connection.disconnect()` (2.0 s);
Phase 3 runs `kill_browser_process`, `browser.stop()`, then
`terminate()` → `kill()` → `os.kill(pid, 15)`. **Nothing waited for Chrome to
exit.** Chrome does not answer `Browser.close` — it drops the socket — so those
2.0 s budgets are never spent and the whole close returned in 0.13–0.32 s.

`Browser.close` is the START of Chrome's shutdown, not the end of it. The
shutdown writes `Default/Network/Cookies` and then the process exits.

**The decisive measurement.** One `psutil.Process(pid).status()` read added at
the top of Phase 3, nothing else changed:

> **20 closes out of 20 — Chrome's status at the kill site was `running`.**

So the terminate landed on a still-shutting-down browser every single time.
Whether the cookie survived was decided by whether the commit happened to win.

### 3.1 The four arms (all on `f397de4`)

| arm | change | survived |
|---|---|---|
| plain | none | 10/10 (`close_s` 0.128–0.168) |
| observe | +1 psutil status read | 20/20, **`chrome_at_kill=running` 20/20** |
| **stalled** | `Target.closeTarget` + `Browser.close` suppressed, so the terminate is the only ending | **0/5 — `after=''` every time** |
| **waited** | Phase 3 waits for Chrome's own exit first | **10/10**; Chrome exited unaided in **0.129–0.165 s** |

An earlier batch on a product-identical tree lost 1 in 30 spontaneously. So:
~1-in-30 on an idle local machine, 2-in-2 on a loaded Windows runner —
consistent, because a slower machine lengthens Chrome's shutdown against a
grace of effectively zero.

### 3.2 The fence cost table (retired, kept for the record)

60 real `browser_pid_registry.claim_browser` round trips with and without
`operator_fence.install(...)`, twice each: medians 3.806 / 4.235 ms **with**
the fence against 5.087 / 4.148 ms without. Inside the noise, twice faster with
it. `_designated_roots()` is cached and the hot guard does no syscall.

---

## 4. The fix

**One home for ending a browser's process** — `embedded/process_exit.py`:

- `browser_pid(process, fallback_pid)` — the pid of the **browser**, never one
  of its children. A Chrome profile is held by a whole tree (measured: eleven
  processes on one spawn) and only the member with no `--type` is the browser;
  a pid carrying `--type=` answers `None`, i.e. *do not wait*, because waiting
  on a renderer would report "exited" while the browser was still flushing —
  this finding's own defect wearing a fix's clothes. It also answers `None` for
  a process whose `returncode` is already set (**S1**, below).
- `wait_for_exit` / `wait_for_exit_async` — the bounded grace, off the loop,
  bounded twice (a worker thread cannot be cancelled, so the async wrapper
  carries its own deadline and the close always reaches the kill path). It
  **polls and never reaps** — §4.1.
- `settle` — the whole of Phase 2b as one call: the grace, the diagnostics
  line, and the kill a CANCELLED close would otherwise skip (**S2**, below).
- `terminate` — the terminate → kill → SIGTERM ladder, moved here whole. A
  rule in one file and the kill it gates in another is how F-886 came to be
  missing; the wait and the kill now sit together.
- `report` — the one close-diagnostics line, so a post-mortem can read whether
  Chrome left on its own and how long it took.

`close_instance` gains **Phase 2b**, between the graceful close and the kill:
one `process_exit.settle(...)`, then Phase 3 exactly as before.

**S1 — the waited pid is joined to an identity, two ways.** `browser_pid` hands
back a number and the grace is up to 5 s, so a recycled pid would be 5 s spent
on a stranger (a stall, never data loss — and impossible on Windows, where the
retained handle keeps the pid reserved). Both halves the review asked for are
in: the `psutil.Process` object is built ONCE before the loop and every poll
asks `is_running()`, which compares the `(pid, create_time)` PAIR through
`Process.__eq__` (`psutil/__init__.py`, `is_running` at 616-641) — so a
recycled pid reads as *gone* rather than restarting the wait; and a set
`returncode` means asyncio has already COLLECTED the child, from which instant
the pid is free, so that case declines to wait at all.

**S2 — a cancelled close still ends its browser.** Phase 2b sits inside the
`try` whose handler is `except Exception`, and `CancelledError` is not one, so
a client disconnecting mid-grace escaped without running Phase 3 — leaving the
browser to `process_cleanup`'s orphan reap. That window existed before F-910
(it was the 0.13 s of Phase 2) but the grace WIDENS it for a wedged browser, to
the whole ceiling. `settle` therefore catches `CancelledError`, runs the kill
synchronously, and re-raises: the caller's answer is unchanged and the browser
this close claimed is dealt with on every path out. F-899's move of the state
pop into Phase 1 is what had already limited the harm to a surviving process.

**S3 — what the grace costs a SHUTDOWN, and why the 2 s budget is not the
bound.** `app_lifespan` calls `close_all`, which closes instances
SEQUENTIALLY, so N wedged browsers add N × (5 s + 1 s slack) to a backend's
exit. The review named a collision with uvicorn's 2.0 s
`timeout_graceful_shutdown` (`logging_setup._GRACEFUL_SHUTDOWN_SECONDS`,
F-809) and it does not happen: read from source, that budget wraps
`_wait_tasks_to_complete()` **alone** (`uvicorn/server.py`:279-282) and
`await self.lifespan.shutdown()` runs afterwards, OUTSIDE the `wait_for`, with
no deadline at all (:291-293). So the lifespan shutdown was never bounded by
it — before this change or after — and no "timeout graceful shutdown exceeded"
can be attributed to the grace. The closes are left **serial deliberately**:
parallelising them would run N `_blocking_teardown` calls at once, each
mutating the one `ProcessCleanup.browser_processes` dict that
`_save_tracked_pids` iterates, trading a slow exit for a corrupted
`browser_pids.json` — the worse failure. And a merely SLOW browser costs
nothing extra, because the grace spends the time `kill_browser_process`'s own
`terminate()` + `wait(3)` would have spent anyway; that is why the measured
closes are indistinguishable (0.128-0.319 s plain, 0.129-0.165 s waited).

**`EXIT_GRACE_SECONDS` = 5.0**, and the number is argued rather than picked:
~30× the slowest unaided exit measured here (0.169 s), which is headroom for a
runner where the whole node ran 3× slower than locally, not a guess. It is a
CEILING — a browser that has already gone costs one `psutil` read — and the bad
case is stated: **a wedged Chrome makes one `close_instance` up to 5 s slower
and is then killed exactly as it was before F-910.** Phase 3 keeps its whole
`CLOSE_KILL_TIMEOUT`, because the wait is its own await rather than a charge
against that budget.

Universal: no knob, no platform branch, no settings field.

**LOC.** `browser_manager.py` was at exactly its 1485 cap, so the cut came
first: the kill ladder moved out whole and the file ratchets **1485 → 1452**.
Caps go down only. The review round stayed inside that number: Phase 2b became
one `settle` call, which is what paid for `close_instance`'s corrected
docstring (N2) — the file measures 1452/1452.

---

### 4.1 POSIX: the wait must not REAP, and Linux/macOS have never run this

The first draft used `psutil.Process(pid).wait(timeout=…)`. On Windows that is
a handle wait and harmless. On POSIX it is a second reaper of our own child,
and the reasoning is short:

- A browser we launched is a CHILD of the backend, and asyncio is **already**
  waiting on it. `ThreadedChildWatcher.add_child_handler` starts a daemon
  thread per child that blocks in `os.waitpid(pid, 0)`
  (CPython 3.13.11, `asyncio/unix_events.py`:1421-1443); on a Linux new enough
  for `pidfd_open` the default is `PidfdChildWatcher`, whose `_do_wait` reaps
  with the same call at :990 (`_init_watcher`, :1485-1491, picks between them).
- A child's exit status can be collected **once**. Both watchers answer losing
  it identically: `ChildProcessError` → `returncode = 255` plus a WARNING
  ("Unknown child process pid %d, will report returncode 255" at :1449-1451;
  "exit status already read: will report returncode 255" at :995-998).
- `psutil.Process.wait()` on POSIX is `_psposix.wait_pid`, which polls
  `os.waitpid(pid, os.WNOHANG)` and reaps whatever it finds
  (`psutil/_psposix.py`:62-155; `flags |= os.WNOHANG` at :96, the call at
  :110, and the not-our-child fallback to `pid_exists` polling at :113-122).

So the first draft could have made `Browser._process.returncode` read **255**
for a browser that exited cleanly with 0 — a field `terminate`'s own guard
reads — and put a spurious WARNING in the log of every close. The fix
OBSERVES instead: `_has_exited` asks `is_running()` and treats
`STATUS_ZOMBIE` as exited, so the status is never collected and asyncio keeps
its reap. A zombie IS an exited process (cookie store committed, files
closed), and it is precisely the state a browser is in between its own exit
and asyncio's `waitpid` — which is why that status is a success and not a
timeout, and why `tests/test_close_waits_for_chrome.py`'s kill-path pin accepts
`zombie` beside `gone`. Windows has no zombies and no child watcher (the
Proactor loop waits on the process HANDLE), so the poll answers on
`is_running()` alone and nothing there changes.

**This module was written and measured on Windows: the gate's Linux and macOS
cells are its first execution.** What they are checking is this section. The
cost of the poll is named rather than hidden: `_POLL_SECONDS` = 0.02, so the
grace can overshoot a real exit by up to 20 ms against a measured 0.100-0.169 s.

### 4.2 What the caller is now told (the lead's M3 ruling)

`tool_sections/browser_management.close_instance` answered `bool` and
**discarded** the dict `_refresh_master_snapshot_if_safe` returns. That is the
mechanism by which a seed refusal could not be seen from outside the process,
and the lead ruled it must be REPORTED rather than filed as a residual. The
tool now answers a record:

| key | meaning |
|---|---|
| `closed` | the boolean it used to return |
| `seed_refreshed` | `True` refreshed, `False` refused, **`None` not asked** — this close was not of the `default` session |
| `seed_error` | present only when `seed_refreshed` is `False`, in `clone_storage`'s own words (`default-in-use`, `SNAPSHOT_IN_USE`, or an exception's type and text) |

`None` rather than an absent key, on `profile_seed.seed_changed_since`'s
three-value precedent: one answer shape, and "nothing to report" cannot read
as "nothing reported".

`tests/goldens/tool_surface.json` DOES declare this tool's return schema, so
that SOFT golden moves in this commit — deliberately, which is convention 4's
whole requirement. The diff is 14 insertions / 12 deletions, all inside the
`close_instance` entry: the docstring, and `output_schema` losing FastMCP's
`_WrappedResult` (`x-fastmcp-wrap-result`) because a dict return is not
wrapped. **That is also the WIRE change**: a client used to read
`{"result": true}` and now reads the record itself. Callers updated:
`cli_call.cmd_close` (which would otherwise have printed "closed" for a close
that failed — a record is always truthy), the two E2E close helpers, and the
three assertions that read the bool directly.

---

## 5. The pins, and what each one's RED is worth

**`tests/test_close_waits_for_chrome.py`** — the close itself:

1. **`test_the_kill_path_never_runs_against_a_live_browser`** — the mechanism,
   and its RED is **deterministic**: on the shipped code it fails with
   `status 'running'`, measured 20/20 before the fix and reproduced on demand.
   It also asserts the close wrote its diagnostics line — see §5.1.
2. **`test_a_max_age_cookie_survives_close_and_respawn`** — the consequence.
   Its RED is **probabilistic** (1 in 30 locally, 2 of 2 on CI), and the file
   says so, because a node whose failure is rare must not be read as the proof.

**`tests/test_seed_refresh_after_close.py`** — the blast radius (§7 of the
first draft; built here). Both nodes run against a REDIRECTED session root with
a synthetic shared profile, and the fixture resolves all four directories
through the product's own accessors and refuses to run if any of them still
points outside the tmp tree — before a browser is spawned, because these nodes
rewrite the seed and on an unredirected machine that seed is the operator's own
logged-in profile.

3. **`test_the_seed_refresh_after_a_default_close_carries_the_login`** — a
   **CONTRACT pin, not a RED** (§5.2 has the numbers): the refresh ran before
   F-910 too. What it states is that closing the shared session refreshes its
   seed, that the CALLER is told so (§4.2), and that the seed carries the
   cookie set before the close — measured: the cookie's NAME is plain text in
   `Default/Network/Cookies` in both the profile and its seed copy.
4. **`test_the_seed_refresh_after_a_default_close_skips_no_file`** — the second
   exposure, which node 3 does not cover: `profile_copy.copy_file` answers a
   file Chrome still holds by logging `copy_skip` and carrying on, so a seed
   built over a live browser is silently incomplete rather than refused. Read
   out of the product's own debug ring as a DELTA across the close. Also a
   contract pin (§5.2).

**`tests/test_close_instance_offload.py`** — hermetic, no Chrome:

5. **`test_the_kill_ladder_logs_the_rung_that_ended_the_browser`** — see §5.1.
6. **`test_a_refused_seed_refresh_reaches_the_caller`** — the M3 contract
   through the REAL tool body with the singletons swapped: a refusal arrives as
   `seed_refreshed: False` + `seed_error: "default-in-use"`. RED against the
   pre-M3 body (`assert True == {...}`), which is the point — a pin on
   `clone_storage` or on `browser_manager` cannot see this defect at all,
   because the answer was computed correctly and then dropped.
7. **`test_a_close_that_owed_no_refresh_says_so_rather_than_nothing`** — a
   `clone` close answers `seed_refreshed: None` and never calls the refresh.
8. **`test_a_refusal_with_no_words_is_still_reported`** — `seed_error` becomes
   `"unreported"` rather than being omitted, so a future refusal that forgets
   to name itself is still visible.
9. **`test_the_wait_calls_nothing_that_would_reap_the_browser`** — an AST pin:
   no `.wait()` call anywhere in `process_exit`. §4.1 is unobservable from this
   host (Windows has no child watcher and no zombies), so a source pin is the
   only thing that can state it here at all. RED against the first draft,
   naming its line.
10. **`test_a_zombie_is_an_exited_browser`** / **`…never_leaves_is_handed_to_the_kill_path`**
    — the POSIX success and timeout shapes, driven through the module's one
    `psutil` name because this host cannot produce a zombie.
11. **`test_a_process_asyncio_has_already_collected_is_never_waited_on`** (S1)
    and **`test_a_cancelled_grace_still_kills_the_browser`** (S2).

**The stalled arm cannot be the RED harness for this fix, and this is the one
place the brief and the mechanism disagree.** With `Browser.close` suppressed,
nothing ever asks Chrome to leave — so the wait times out, the terminate lands
exactly as before, and the cookie is lost *after* the fix too. That arm proves
the CAUSE (0/5) and is recorded here rather than shipped as a pin the fix does
not satisfy. What makes node 1's RED deterministic instead is that Chrome needs
~0.13 s to leave and the shipped code gave it zero — which is true on every
close, not one in thirty.

### 5.2 A measured claim that did not reproduce, and the correction

The first draft of this finding, of the CHANGELOG entry and of
`test_seed_refresh_after_close.py`'s docstring said the seed refresh
**"refused every time, reporting `seed_error: default-in-use`"**, i.e. that
the refresh-on-close feature had been silently disabled for months. That was
reasoned from the code path and **never measured**. It is false.

- The reviewer measured the pre-F-910 refresh **4/4 GREEN**.
- Re-measured here, 2026-09-21, with the reviewer's own plugin
  (`%TEMP%\f910red\f910_noop_wait.py`, which replaces
  `process_exit.wait_for_exit_async` with a no-op and so restores the pre-fix
  ordering exactly): **10 runs, 20/20 node passes**, both nodes, every run.

The mechanism is one call upstream of the refresh and the first draft did not
follow it: the refresh runs *after* `browser_manager.close_instance` RETURNS,
and the last thing that call does is Phase 3's `_blocking_teardown`, whose
FIRST statement is `process_cleanup.kill_browser_process` — which for every
browser pid on the profile does `terminate()` and then a **blocking**
`process.wait(timeout=3)` (`process_cleanup.py`:869-895). The profile was
therefore already free when the refresh asked, and `_profile_has_running_browser`
answered False.

What F-910 changes for the seed is the **content** of the profile that gets
copied, not whether the copy happens: the grace is what lets Chrome's own
shutdown — and therefore its cookie commit — land BEFORE that terminate, so
the seed is a copy of a profile that finished writing rather than one cut
short. Both blast-radius nodes are contract pins on that property, and the
module docstring, this section and the CHANGELOG now say so. The rule this
broke is worth stating plainly: **a measured claim that a second measurer
cannot reproduce does not belong in a CHANGELOG**, and "the code path says so"
is not a measurement.

### 5.1 A defect the pins caught in the fix itself

`process_exit` shipped its first draft importing the debug_logger **module**
(`from …embedded import debug_logger`) where every other file in the tree
imports the **singleton** (`from …embedded.debug_logger import debug_logger`).
The module has no `log_info`, so every line the leaf wrote was an
`AttributeError`:

- in `report`, swallowed by that function's own never-raises contract — so the
  close diagnostics this finding promises **did not exist**, and nothing said
  so;
- in `_rung`, NOT swallowed — it would have escaped the ladder immediately
  after a successful `terminate()`, i.e. out of the kill path of a browser that
  had just been killed.

It survived the first round of pins because they all take the happy path, where
`terminate` returns at its `returncode is not None` guard and never logs. Both
holes are pinned now: node 1 asserts the diagnostics line exists and says
`exited_unaided=True`, and node 5 drives the ladder against a process that is
still running (RED against the old import: `AttributeError: module … has no
attribute 'log_info'`).

---

## 6. Verification

| Run | Result |
|---|---|
| `tests/test_close_waits_for_chrome.py` before the fix | 1 failed (`status 'running'`), 1 passed |
| same, after the fix | **2 passed**, Chrome exiting unaided in 0.115-0.142 s |
| same, after the review round (non-reaping wait + `settle`) | **2 passed** |
| `tests/test_seed_refresh_after_close.py` | **2 passed** (seed refreshed, reported, seed carries the cookie, 0 `copy_skip`) |
| same, with Phase 2b NEUTRALISED (the reviewer's plugin), 10 runs | **20/20 node passes** — these are contract pins, §5.2 |
| `tests/test_close_instance_offload.py` | **15 passed** (7 before the review round) |
| the three M3 nodes against the PRE-M3 tool body | **3 failed** (`assert True == {…}`) |
| the three M2/S-nodes against the reaping wait | **3 failed** (the AST pin naming its line) |
| the two `stealthy close` nodes against the shipped verb | **2 failed** |
| the CI node, 10× on the fix / 5× stalled | **10 passed / 5 failed** (§2) |
| the sweep's HERMETIC tier, ONE process — the 45 `tests/test_*.py` matching `close_instance\|process_exit\|seed_refreshed`, `-m "not integration"` | **406 passed, 0 failed** (273 integration deselected) |
| the sweep's INTEGRATION tier, real Chrome, ordered chunks — every one of those 273 nodes | **326 passed, 1 xfailed, 0 failed** (a chunk runs each file whole, so its non-integration nodes are counted again) |
| every `tmp_session_root` consumer after the fixture fix — 12 files | **342 passed** |
| `tools/dump_tool_surface.py --check` before / after `--write` | DRIFT / clean; the diff is the `close_instance` entry alone (§4.2) |
| every pre-commit gate (`ruff format`, `ruff check`, `ty`, `vulture`, suppression owners, budgets, pinned imports) | **all pass**; `browser_manager.py` 1452/1452 |
| every source line number §4.1 and §4.2 cite, re-read on this host | **one wrong of five**, corrected — §6.1 |

**N1 — the sweep is now ONE process per TIER, and the two failures the review
reported did not reproduce.** The review measured 380 passed / 2 failed in one
process, both in `test_observability.py`
(`TestStdoutPurity::test_a_burst_of_failures_writes_nothing_to_stdout` and
`TestSecretCanaries::test_no_canary_reaches_stdout_stderr_or_the_backend_log`),
caused by a pre-existing leak of `debug_logger._enabled` between files — and
F-907 (PR #153, head `11662ae`) fixes exactly that class upstream via
`tests/logging_state.py`. **No second restore is added here**, per the lead.
What this round measured instead: the file list in ONE process, hermetic tier,
is **406 passed, 0 failed**, with BOTH reported nodes collected and passing —
so the leak is order- and selection-dependent rather than present in this
branch's own file set. The two candidate leakers
(`test_exception_handling.py`, `test_debug_logger.py`, both of which call
`debug_logger.enable()` and neither of which is in this list) were each paired
with `test_observability.py` in one process and both pairs passed. **The file
list is stated as a RULE and not as a count**, because the first draft of this
row said "61 files, 801 passed" and the list that produced it could not be
reconstructed — which is §5.2's own lesson arriving in §5.2's own finding. The
rule is: every `tests/test_*.py` whose text matches `close_instance`,
`process_exit` or `seed_refreshed`; today that is 45 files. The tiers are two
processes because the integration tier is 273 real-Chrome nodes: a foreground
run cut off at a 10-minute cap dies mid-E2E and orphans Chrome trees, which is
a worse outcome than two honest processes.

One real regression was caught by that sweep and fixed:
`test_profile_seed_truth.py::TestLoginWitnesses::test_seed_literals_have_exactly_one_home[Default/Network/Cookies]`
— F-892 forbids any module under the package from spelling a witnessed path a
second time, **docstrings included**, and `process_exit`'s opening paragraph
did. It now names `profile_seed.LOGIN_WITNESSES` instead, which is the rule
working exactly as written.

### 6.1 The citations were re-read, and one of them was wrong

§4.1 and §4.2 rest on line numbers in three third-party sources, and §5.2 has
just finished saying that a claim a second measurer cannot reproduce does not
belong in a shipped document. So every one was re-read on this host
(Python 3.13.11, psutil 7.0.0, uvicorn 0.35.0) rather than carried forward:

| citation | verdict |
|---|---|
| `psutil/__init__.py` `is_running` at 616-641, zombie arm at 635-638 | **exact** — 635 is `except ZombieProcess:`, 638 its `return True` |
| `psutil/_psposix.py` `wait_pid` — `flags \|= os.WNOHANG` at :96, `os.waitpid(pid, flags)` at :110, the not-our-child `pid_exists` poll at :113-122 | **exact**, all three |
| the ENCLOSING span of that same function, quoted as `:62-127` | **WRONG — it is 62-155.** Corrected here, in `process_exit`'s module docstring and in the CLAUDE.md row. The three precise sub-citations were right, which is how a 28-line error in the span around them survived a review |
| `asyncio/unix_events.py` — `ThreadedChildWatcher.add_child_handler` :1421, its `os.waitpid(pid, 0)` :1443, the `returncode 255` WARNING :1449-1451; `PidfdChildWatcher._do_wait`'s `os.waitpid` :990 and its WARNING :995-998; `_init_watcher` choosing between them :1485-1491 | **exact**, every one, including both warning STRINGS. Windows cannot import this module but does ship its source, so the numbers are readable here even though §4.1's behaviour is not |
| `uvicorn/server.py` — `asyncio.wait_for(self._wait_tasks_to_complete(), …)` at :279-280 and `await self.lifespan.shutdown()` at :293, OUTSIDE it | **exact**, and it is S3's whole load-bearing claim: the graceful-shutdown budget does not bound the lifespan shutdown, so the grace cannot produce F-809's "timeout graceful shutdown exceeded" |

### 6.2 The F-903 fence data point, and why it is not attributed

The lead asked whether F-903's operator fence widens this race on CI: the node
`test_stateful_i18n.py::test_storage_and_cookies_survive_one_profile_and_no_other`
failed **3/3** on the F-903 fence branch against **1/4** elsewhere. The honest
answer is **unmeasured, and the difference is not evidence about the fence**,
for two reasons that are both already in this document:

- **The defect is fence-independent and that was measured, not argued.** §2
  reproduced it on `origin/main` `f397de4`, where the fence does not exist, and
  §3's `observe` arm found Chrome still `running` at the kill site **20 times
  out of 20** with no fence anywhere. A defect that fires on every close cannot
  be attributed to a branch that changes how often it is *seen*.
- **3/3 vs 1/4 is seven runs.** Under a race whose outcome is decided by
  whether Chrome's commit beats a terminate, that split is unremarkable: the
  same node is 2-of-2 on a loaded Windows runner and ~1-in-30 on an idle local
  machine (§3), i.e. the observed rate is a fact about machine load, and the
  fence branch's runs are not load-matched against the others.

What a real measurement would take is named rather than hinted at, so nobody
mistakes this for a closed question: the same sha, the same runner class, the
fence forced on and off, ≥20 runs per arm. Two mechanisms would be the
candidates — the session-root force (it redirects where the node's profile
lives, and a different filesystem path can change commit timing) and the
wrapped filesystem calls (they add work between Chrome's write and the kill).
Neither was measured here, and **neither needs to be for this fix**: F-910
removes the race itself, so both arms should go to 0 failures. If the node ever
fails again *after* this ships, that is when the fence becomes the next
suspect — and §3's `observe` arm, one `psutil.status()` read at the kill site,
is the instrument that would settle it.

---

## 7. Residuals

1. **FIXED, not a residual: a refused refresh was invisible to the caller.**
   The first draft filed this as a residual and the lead OVERRULED it: a silent
   refusal into a discarded dict is exactly how a defect stays hidden, so it is
   reported. `close_instance` answers `seed_refreshed` / `seed_error` now —
   §4.2 for the shape and the golden it moves, §5 nodes 6-8 for the pins.
   Nothing about the refresh itself changed; what changed is that its answer
   reaches the caller instead of the floor.
2. **`copy_skip` is pinned at zero for a real close, not proved impossible.**
   The window the wait closes is Chrome's own; anything else holding a profile
   file — an indexer, a scanner — still gets skipped, and the copier still
   cannot enumerate what it lost (`profile_copy`'s own docstring says so).
3. **One-off observed, not built for.** PR #151 also lost
   `tests/test_e2e_execute_script_async.py::test_a_returned_promise_answers_with_the_value_it_resolves_to`
   with `TypeError: Failed to fetch` from a page-side `fetch` — one occurrence,
   one cell, no second sighting. Recorded, not acted on.
4. **The harness reset is fixed here as a nit, not as a cause**:
   `release_gate_harness.handle_one_request` now returns on
   `ConnectionResetError` (WinError 10054 — a browser closing a keep-alive
   socket mid-`readline`). It was absent from #150's log where the same node
   failed the same way, so it never caused anything; it printed a traceback
   over the output a failure is read from.
5. **A grace is not a guarantee.** If a machine is so starved that Chrome needs
   more than 5 s to write its cookie store, this node fails again — but it now
   fails after giving Chrome thirty times its measured need, and the close
   diagnostics say so in one line.
6. **FIXED, not a residual: `tests/conftest.py`'s `tmp_session_root` built a
   profile no real browser could use.** Its `Default/Cookies`, `Login Data` and
   `Web Data` were 18-byte placeholders written for the path-resolution tests
   that share it — and Chrome >= 96 MIGRATES the legacy cookie store into the
   Network subdirectory at startup, finds a store it cannot read, and **keeps
   the cookie jar in MEMORY**: measured here as a cookie readable through
   `document.cookie`, absent from a 4096-byte (one empty page) on-disk
   database, and gone after a respawn — this finding's own symptom produced
   entirely by the harness, and an hour spent chasing the product for it. Six
   of the twelve modules sharing that fixture spawn a real browser onto it, so
   this was left loaded. The fixture now writes REAL empty SQLite databases
   (`_empty_sqlite`) and `tests/test_profile_seed_truth.py` pins every one of
   them as openable with `PRAGMA integrity_check`. The workaround
   `test_seed_refresh_after_close` briefly carried is DELETED — a shared
   fixture that manufactures data loss is fixed at its source, not routed
   around by the one module that noticed. All twelve consumers pass (342).
7. **POSIX runs this for the first time on the gate.** §4.1 is read from
   CPython's and psutil's sources and pinned by an AST rule plus two nodes
   driven through a fake `psutil`; what it is NOT is executed against a real
   Linux or macOS child, because this machine is Windows. The specific things
   the POSIX cells decide are: that a browser reaches `zombie` (not
   `NoSuchProcess`) inside the grace, that `is_running()` answers True for one,
   and that no `returncode 255` warning appears in a close's log.
8. **Two `test_observability.py` nodes fail for the REVIEWER when the sweep
   runs as one process, and did not fail here.** Not this branch's either way
   — a `debug_logger._enabled` leak between files, fixed upstream by F-907 —
   but the disagreement is itself the residual: the failure is a function of
   which files share a process and in what order, so "the sweep is green" is
   only ever a statement about one partition. Named because the first draft's
   "0 failed" was six batches and read as a green lane — see §6/N1.
9. **The WIRE shape changed, and only the callers in THIS repo were swept.**
   §4.2's record replaces a bare bool, so `{"result": true}` becomes the
   record itself. The sweep for consumers was mechanical and covered both
   trees — an AST pass over every `close_instance` call site, awaited or not,
   classifying a call as *discarded* only when nothing can read its value, so
   `asyncio.gather(*(close(i) for i in ids))` and `_terminal(close(i), …)`
   were caught where an `Await`-keyed grep would have missed them. It found
   one consumer in `src/` (`cli_call.cmd_close`, fixed) and eight in `tests/`
   (all fixed), plus one stale CLAIM with no assertion behind it —
   `test_stealthy_cli_e2e.py`'s docstring saying an unknown instance prints
   `false`, which is now a record whose `closed` is false. What the sweep
   cannot reach is a caller outside this repo: an agent or script reading
   `close_instance`'s answer as a boolean sees a dict, and a dict is always
   truthy, so a `if not closed:` branch goes quiet rather than loud. That is
   the cost of the lead's M3 ruling, and it is stated here rather than
   discovered later.
