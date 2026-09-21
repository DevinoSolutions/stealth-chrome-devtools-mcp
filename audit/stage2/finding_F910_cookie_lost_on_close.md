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
  this finding's own defect wearing a fix's clothes.
- `wait_for_exit` / `wait_for_exit_async` — the bounded grace, off the loop,
  bounded twice (a worker thread cannot be cancelled, so the async wrapper
  carries its own deadline and the close always reaches the kill path).
- `terminate` — the terminate → kill → SIGTERM ladder, moved here whole. A
  rule in one file and the kill it gates in another is how F-886 came to be
  missing; the wait and the kill now sit together.
- `report` — the one close-diagnostics line, so a post-mortem can read whether
  Chrome left on its own and how long it took.

`close_instance` gains **Phase 2b**, between the graceful close and the kill:
wait for the browser to go, then report, then Phase 3 exactly as before.

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
Caps go down only.

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

3. **`test_the_seed_refresh_after_a_default_close_carries_the_login`** — and
   its RED is **deterministic**, by a route the first draft did not see:
   `_refresh_master_snapshot_if_safe` asks `_profile_has_running_browser` first,
   so on the shipped code it found the browser we had just asked to leave STILL
   RUNNING and refused with `seed_error: default-in-use`. **The whole
   refresh-on-close feature did nothing at all, silently, on every close** —
   not "sometimes copied a truncated profile". After the fix the refresh runs
   and the seed carries the cookie (measured: the cookie's NAME is plain text
   in `Default/Network/Cookies` in both the profile and its seed copy).
4. **`test_the_seed_refresh_after_a_default_close_skips_no_file`** — the second
   exposure, which node 3 does not cover: `profile_copy.copy_file` answers a
   file Chrome still holds by logging `copy_skip` and carrying on, so a seed
   built over a live browser is silently incomplete rather than refused. Read
   out of the product's own debug ring as a DELTA across the close.

**`tests/test_close_instance_offload.py`** — hermetic, no Chrome:

5. **`test_the_kill_ladder_logs_the_rung_that_ended_the_browser`** — see §5.1.

**The stalled arm cannot be the RED harness for this fix, and this is the one
place the brief and the mechanism disagree.** With `Browser.close` suppressed,
nothing ever asks Chrome to leave — so the wait times out, the terminate lands
exactly as before, and the cookie is lost *after* the fix too. The same is true
of both blast-radius nodes: with the browser still alive the refresh is refused
and any copy skips, after the fix exactly as before it. That arm proves the
CAUSE (0/5) and is recorded here rather than shipped as three pins the fix does
not satisfy. What makes these REDs deterministic instead is that Chrome needs
~0.13 s to leave and the shipped code gave it zero — which is true on every
close, not one in thirty.

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
| `tests/test_seed_refresh_after_close.py` | **2 passed** (seed refreshed, seed carries the cookie, 0 `copy_skip`) |
| `tests/test_close_instance_offload.py` | 7 passed; node 5 RED against the old import |
| the CI node, 10× on the fix / 5× stalled | **10 passed / 5 failed** (§2) |
| every test file that drives `close_instance` + F-899 + doc-claims — 47 files, six FOREGROUND batches | **685 passed, 1 xfailed, 0 failed** |
| every `tmp_session_root` consumer after the fixture fix — 12 files | **342 passed** |
| `tools/check_file_budgets.py` | all within budget, `browser_manager.py` 1452/1452 |
| `ruff check src/ tests/ tools/` | All checks passed |

One real regression was caught by that sweep and fixed:
`test_profile_seed_truth.py::TestLoginWitnesses::test_seed_literals_have_exactly_one_home[Default/Network/Cookies]`
— F-892 forbids any module under the package from spelling a witnessed path a
second time, **docstrings included**, and `process_exit`'s opening paragraph
did. It now names `profile_seed.LOGIN_WITNESSES` instead, which is the rule
working exactly as written.

---

## 7. Residuals

1. **A refused refresh is invisible to the caller.** `close_instance` awaits
   `_refresh_master_snapshot_if_safe` and **discards the dict it answers**, so
   the `seed_error` that shipped for months (§5, node 3) could only ever be
   seen by a test that wrapped the function. Not changed here — it is a
   reporting decision in another module's tool body — but it is why that defect
   lasted: the one thing that knew was thrown away one line after it was built.
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
