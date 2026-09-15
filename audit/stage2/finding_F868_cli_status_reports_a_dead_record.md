# F-868 — `status` reports the FIRST recorded backend, so an operator with a healthy backend is told "not running" and handed a dead pid

**Status:** FIXED in this PR (product defect; reproduced hermetically from the exact
observed record, root cause confirmed in source)
**Opened by:** the 2026-09-14 ~23:20 local ops-CLI observation on the maintainer's
workstation (PyPI 2.1.5, Windows 11, `~/.stealth-mcp/server.json` schema 2, three entries)
**Source at:** `main` = `267bac8`
**Severity:** MEDIUM. No data loss and no backend is harmed, but it is a truthfulness
defect in the ONE surface an operator consults to decide whether the product is broken —
and the answer it gives ("not running") is the one that invites a `restart`, a
`kill-orphans`, or a bug report, in the exact state where nothing is wrong.

---

## 1. What was observed

`~/.stealth-mcp/server.json`, schema 2, three entries, in this recorded order:

| context | port | version | pid | actual state at 23:20 |
|---|---|---|---|---|
| `win-session-2` | 7169 | 2.1.1 | 89892 | pid DEAD, nothing listening |
| `headless` | 19222 | 2.1.3 | 67720 | pid DEAD, nothing listening |
| `win-session-1` | 52554 | 2.1.5 | 53836 | pid ALIVE, listening on 127.0.0.1:52554, healthy, serving 56 proxies |

The shell running the CLI was in Windows session 1 — the active console session — so
`display_context.display_context()` answers `win-session-1` for it, and
`singleton._find_running_server` adopts and proxies to 52554 from that same shell.

`stealth-chrome-devtools status` printed:

```
backend     : not running
pid         : 89892
log         : C:\Users\amind\.stealth-mcp\logs\backend-89892.log
version     : 2.1.5
```

Three separate untruths in four lines: the backend this shell would be served by was
responsive; the pid named a process that had not existed for hours; and the log path
pointed at a dead backend's file instead of the live one's.

**Reproduced hermetically** (`tests/test_cli_status_wedged.py::TestCliStatusReportsTheBackendThisClientWouldUse`,
the record above verbatim, both liveness primitives stubbed so no socket is opened):
RED output before the fix was byte-identical to the observation —
`'backend     : not running\npid         : 89892\n…'`.

## 2. Root cause

**One function picked the record, and it picked by position rather than by relevance:**
`singleton._probe_backend_status` (`src/stealth_chrome_devtools_mcp/embedded/singleton.py:160`
at `267bac8`) read

```python
entry = backend_registry.first_backend(_read_server_state())   # singleton.py:171
```

`first_backend` (`embedded/backend_registry.py:135`) is honest about what it is and says
so in its own docstring: *"'First' preserves the pre-v2 single-backend behaviour exactly…
It carries no preference of its own — a caller that wants the backend most likely to be
usable asks `window_capable_first`."* Under schema v2 "first" is dict insertion order,
i.e. **whichever context happened to record itself earliest and has not been superseded
by port since** — here `win-session-2`, a backend from 2.1.1 that had been dead for two
releases. The probe therefore reported that entry's port (7169) as `down`, and
`cli._format_backend_status` maps `down` and `none` alike to `"not running"`.

**Why the own-context record was not preferred.** The ordering that answers "which
backend would THIS client actually use" already exists and is already the one home for
that policy: `backend_registry.adoption_candidates(path, own_context)`
(`backend_registry.py:178`), asymmetric by design — a client that can PROVE it has a
desktop adopts only its own context's entry plus `UNVERIFIED` ones; a client that cannot
adopts anything, window-capable first. `singleton._find_running_server`
(`singleton.py:231`) walks exactly that list at `singleton.py:242`, which is why the
proxies in that same shell were all correctly on 52554. `_probe_backend_status` simply
never consulted it. For `own_context == "win-session-1"` the candidate list is a single
entry — the live one — so the right answer was one call away.

**The pid line was a SECOND, independent selection**, which is why it could disagree with
the status line rather than merely inherit its error: `cli._recorded_backend_pid`
(`cli.py:144`) did its own `backend_registry.first_backend(singleton._read_server_state())`
at `cli.py:154`. `cli._doctor_port_occupant_line` (`cli.py:274`) made a third at
`cli.py:280`. Three reads of "the backend", none of them agreeing by construction.
(`singleton.stop_backend:607` and `restart_backend:660` had already learned this lesson
and read `backend_on_port(…, port)` — "the entry recorded ON THIS PORT, not merely the
first" — so the pattern the CLI needed was already in the tree.)

## 3. What is and is not affected

**Affected — all through the ONE shared `_probe_backend_status`:**

| verb | consumed at | symptom on the observed record |
|---|---|---|
| `status` | `cli.py:128` (via `_format_backend_status`) | "not running" + dead pid + dead log path |
| `doctor`'s `backend :` / `pid :` / `log :` / `port :` summary lines | `cli.py:381`+ | same four lines, same wrongness |
| `stop` | `singleton.py:600` | targets `win-session-2`'s record: terminates nothing, reports "already stopped", and FORGETS the dead sibling's entry while the live own-context backend keeps running. Not destructive, but it is not the backend the operator asked to stop |
| `kill-orphans` | `cli.py:502` (gate), `cli.py:504` (the pid it names) | the "a backend is running — use restart, or pass `--force`" guard reads `down` where the true answer is `responsive`, so the verb runs its reaper beside a live backend and then prints "reaped any browsers left over from a dead backend" while that backend is serving. The live backend's OWN browsers are still spared — `_recover_orphaned_processes` re-checks recorded ownership through `_owner_backend_alive` (`process_cleanup.py:319`, the F-808 fix) and only `--force` bypasses that — so this is a defeated outer gate, not a fleet kill. The residual is real but narrower: anything recorded under an owner that is NOT alive is reaped, and the operator is told the backend is dead |

**Not affected:**

- **Discovery / the proxies.** `_find_running_server` already walks
  `adoption_candidates`; every one of the 56 proxies was correctly on 52554. This was
  never a routing bug — only a reporting one.
- **`doctor`'s `contexts :` block.** `_doctor_backend_lines` (`cli.py:193`) walks
  `window_capable_first` over the WHOLE record and probes each entry on its own port
  (`cli.py:231`), so it listed all three backends with per-port liveness correctly. The
  information was on screen four lines below the lie.
- **`restart`.** It selects its own port through `_select_backend_port` /
  `own_or_first_port` (own-context first) and terminates exactly the port it selected, so
  it never aimed at the sibling. Only its post-restart REPORT came through
  `_probe_backend_status`.
- **The record itself.** No writer is wrong; `record_backend`'s supersede-by-port rule is
  working as designed. What is wrong is only who READS it for a single-value answer.

**Stale records are pruned by nobody.** A dead backend's entry leaves `server.json` in
exactly two ways: `stop_backend` → `forget_backend` for the context it stopped
(`singleton.py:617`), and `record_backend`'s supersede-by-port when some LATER backend
claims the same port (`backend_registry.py:381-384`). A backend that is killed, crashes,
or whose desktop logs out leaves its entry behind forever — nothing sweeps on pid
liveness, and `2.1.1` / `2.1.3` entries surviving under a `2.1.5` install is the proof.
That is deliberate to a point (a record is a never-raise cache, and "pid gone" is not
"port free"), but it means the dict grows monotonically per display context and the
FIRST entry becomes steadily less likely to be the relevant one over a machine's
lifetime. **Deliberately NOT changed here — see §6.**

## 4. The fix

**The liveness ladder now has one home.** The socket → `initialize` → `down` / `wedged` /
`responsive` ladder was four lines duplicated in `cli._probe_recorded_backend`, justified
there by a note saying `_probe_backend_status` "reads the FIRST recorded backend" and so
could not answer per-entry. That justification dies with this fix, and a duplicated
liveness ladder was a second way to answer one question regardless. It now has three
callers: the candidate walk, `restart_backend`, and the CLI form, which keeps only the one
word the ladder cannot reach ("no port recorded"). It lives in
`embedded/backend_liveness.py` as `probe_port`, beside the adoption walk
(`probe_recorded`), reached through `singleton._probe_port` / `_probe_backend_status` —
see §6 for why those wrappers exist and why the leaf takes its probes as arguments.

**One home, no second way.** `_probe_backend_status` now walks
`backend_registry.adoption_candidates(SERVER_STATE_FILE, display_context.display_context())`
— the same list, from the same function, in the same order that `_find_running_server`
uses — and reports the first candidate that ANSWERS. With none responsive it reports the
most informative verdict it saw, `wedged` over `down`, because a wedged backend holds a
port and will be evicted while a `down` record names nothing running at all. No selection
policy is introduced: this function still only says which of the offered candidates is
up, and `adoption_candidates` remains the sole home for WHICH candidates those are.
Fixing it here fixes `status`, `doctor`, `stop` and `kill-orphans` at once, because all
four already consumed this one function.

**The CLI status block now selects once and passes the answer down**, so its lines cannot
disagree with each other:

- `_format_backend_status(status, port)` is pure formatting and performs no I/O.
- `_recorded_backend_pid(port)` reads `backend_on_port` — the entry on the port just
  reported — instead of `first_backend`. This deletes a second selection rather than
  adding a parallel one, and adopts the rule `stop_backend`/`restart_backend` already use.
- `_doctor_port_occupant_line(port)` takes that same port (falling back to
  `DEFAULT_PORT` when nothing is reported, exactly as before), deleting the third.

**`restart`'s report is pinned to the port it spawned on.** `restart_backend` took its
`status` from `_probe_backend_status()` while its `pid` came from `backend_on_port(…,
port)`, so on a multi-context record a responsive SIBLING could report "responsive" beside
the pid of a backend that had just come up wedged — the two halves of one return
describing two processes, and a direct contradiction of the docstring's promise that "a
restart that comes back wedged or down must be visible". Both halves now read the one
selected port via `_probe_port(port)`, so they agree by construction. The adoption walk
answers "is there a backend for me", which is the right question for `status` and the
wrong one here.

**`status` now says what it is not speaking about.** `_other_records_note(port)` adds one
line when — and only when — the record holds entries besides the reported one:

```
backend     : running (responsive) on port 52554
pid         : 53836
log         : C:\Users\amind\.stealth-mcp\logs\backend-53836.log
others      : 2 backends recorded (win-session-2, headless) — run `doctor` for each one's state
version     : 2.1.5
```

It never re-decides which backend to report; it names what the reported one is not and
points at the verb that probes them all. A single-entry record prints no such line.
"Other" is decided on DISPLAY CONTEXT, not on port: `backends_in` stamps a context on
every entry, so the comparison is always against a real value, whereas a hand-edited entry
whose `port` is a string reads as `None` and would have matched a `None` reported port —
hiding itself in precisely the "nothing is running" case that needs it most.

No new `STEALTH_MCP_*` knob, no new env read, no `typing.Any`, no LOC-budget change:
`cli.py` 645 → 691, `singleton.py` 979 → 999 → **985** once the `backend_liveness`
extraction landed (§6), and the new leaf is 91.

## 5. Verification

- `tests/test_cli_status_wedged.py::TestCliStatusReportsTheBackendThisClientWouldUse`
  (4 tests, hermetic — `SERVER_STATE_FILE` redirected to `tmp_path`, both liveness
  primitives patched so no socket is opened and no real process is touched): the observed
  three-entry record reports 52554/53836 and its log path for a `win-session-1` client;
  the same record reports the live backend for a `headless` client (whose adoption list
  tries the dead capable entry FIRST); the other-records line appears with its contexts
  and the `doctor` pointer; a single-entry record prints no such line.
- `tests/test_probe_backend_status.py::TestProbeWalksAdoptionOrder` (3 tests, hermetic,
  real listeners on OS-assigned ports via the file's existing `responsive_stub` /
  `wedged_stub` fixtures): a dead first entry does not hide the live own-context one; a
  dead window-capable entry does not end an unproven client's search; a wedged candidate
  outranks a dead one.
- `tests/test_singleton_stop_restart.py::TestRestartReportsTheSpawnedPort` (1 test): a
  responsive SIBLING must not answer for a restart whose own fresh backend came up
  wedged. RED against the pre-fix reporter with `('responsive', 4242) == ('wedged', 4242)`
  — the sibling speaking for the wrong process — and it asserts BOTH halves, so it cannot
  pass by the sibling merely being invisible: `_probe_backend_status()` is separately
  shown to say `("responsive", sibling_port)` at the same moment.
- `tests/test_singleton_stop_restart.py::TestStopIsPerDisplayContext` (1 test): a record
  holding only a FOREIGN proven context, own context a different proven desktop →
  `stop_backend()` is `("not running", None)`, `_terminate_backend` is never called, and
  the foreign entry is still recorded afterward. **Not RED-first, and deliberately so** —
  it is green on this branch by construction, because the adoption walk IS the fix. It is
  pinned because it is a BEHAVIOUR CHANGE riding on a bug fix (§6), and an unpinned
  behaviour change is the kind a later "simplification" reverts without noticing.
- RED first: **6 of the 7 status tests failed before the product change** (plus the
  restart pin above, 7 of 8 overall), the CLI ones with output identical to the
  observation (§1). The seventh,
  `test_a_single_recorded_backend_gets_no_other_records_line`, is green on BOTH sides by
  construction and is not claimed as a RED: with a single-entry record `first_backend` and
  the adoption walk select the same entry, and the assertion is a negative one about a
  line that did not exist before. It earns its place as the boundary of the new note, not
  as evidence of the defect. GREEN after.
- Regression scope run: `test_cli.py`, `test_cli_status_wedged.py`,
  `test_probe_backend_status.py`, `test_backend_registry.py`,
  `test_singleton_stop_restart.py`, `test_singleton_display_routing.py`,
  `test_singleton_version_aware.py`, `test_singleton_port_fallback.py`,
  `test_singleton_fast_handshake.py`, `test_singleton_backend_logging.py`,
  `test_singleton_cold_start_logging.py`, `test_singleton_starvation_patience.py`,
  `test_find_running_server_app_probe.py`, `test_proxy_selfheal.py`,
  `test_no_silent_excepts.py`, `test_silent_excepts_log.py`, `test_doc_claims.py`,
  `test_doc_examples.py`, `test_release_contract.py` — all green.
  `ruff check` / `ruff format --check` / `tools/check_file_budgets.py` clean.

## 6. Not claimed / follow-ups

- **Pruning stale records is NOT done here, deliberately.** It is a separate decision
  with a real safety argument on both sides, and it needs an owner:
  - *Who would prune?* The only process that can prune honestly is one that has just
    probed. A CLI verb (`status`/`doctor`) must stay read-only by contract (module
    docstring, `STEALTH_MCP_NO_AUTO_RECOVERY=1`), so it may not. `stop`
    already forgets what it stopped. The natural candidate is the cold-start path under
    `_exclusive_lock` — it is the only writer that holds the lock — but a backend whose
    pid is gone is not necessarily a backend whose PORT is free, and dropping the entry
    loses the record that would let a later spawn step around a squatter.
  - *What is the cost of not pruning?* Bounded: one small JSON object per display context
    per machine lifetime, and (after this fix) no reader is misled by it. The cost of
    pruning wrong is a live sibling made undiscoverable, which F-808 already paid for
    once.
- **`stop` on a record with no adoptable entry** now reports `not running` where it
  previously reported (and forgot) a foreign proven context's entry. That is the intended
  consequence of the same rule — an operator's `stop` should not reach across into
  another desktop's backend — but it is a behaviour change, not merely a bug fix, and it
  is stated here rather than left to be discovered.
- **`restart`'s post-restart report: CLOSED here** (it was scoped out of the first draft
  and put back on review). `_probe_port` made it a one-line change rather than a contract
  change — see §4 and the `TestRestartReportsTheSpawnedPort` pin in §5.
- **`stop`'s narrowing is a behaviour change, and is now pinned** rather than only
  described: `TestStopIsPerDisplayContext` (§5). On a record whose only entries are
  foreign PROVEN contexts, `stop` reports `not running` and leaves them alone, where it
  previously reported one, terminated nothing, and then forgot the record — quietly making
  a possibly-live sibling undiscoverable. That pin is green on both sides of the fix by
  construction; it exists to stop the change being reverted by accident, not as evidence.
- **The probe now costs up to one connect attempt per adoptable candidate** instead of
  exactly one. Refused loopback connects are immediate; the only slow case is a candidate
  whose socket is open but silent (one `LIVENESS_PROBE_TIMEOUT`, 2 s), and the walk stops
  at the first responsive one. Not measured on a pathological record — `status` is an
  interactive verb with no deadline.
- **`singleton.py`'s LOC: DONE, in its own commit.** The fix left that file at 999 of its
  1000-LOC default — one line of headroom, which is a gate that passes and a file nobody
  can edit. On review this was escalated and the extraction was done as a SEPARATE commit,
  so "the fix" and "the move" read independently: `embedded/backend_liveness.py` is now THE
  one home for the ladder (`probe_port`) and the adoption walk (`probe_recorded`), on
  `backend_watchdog`'s proven leaf pattern — the two primitives arrive as ARGUMENTS and the
  record as a PATH, so it never imports `singleton`. `singleton` keeps thin
  `_probe_port` / `_probe_backend_status` wrappers that bind OUR probes, OUR record path
  and OUR display context, which is what keeps every existing
  `monkeypatch.setattr(singleton, …)` in the suite reaching the code (a direct import would
  bind at import time and silently stop seeing the patch — the standing lesson from the
  "moving module globals breaks monkeypatch" incident). `singleton.py` 999 → **985**;
  the new leaf is 91 lines. Two lines of the original growth were also paid for honestly:
  the F-856 paragraph in `_same_identity_backend_ready` was retelling what
  `scheduling_lag.FairWindow` is THE one home for, and now points at it instead.
