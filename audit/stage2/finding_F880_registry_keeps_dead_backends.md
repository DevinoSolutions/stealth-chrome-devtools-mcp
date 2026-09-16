# F-880 — nothing ever forgets a dead backend: `server.json` accumulates one entry per display context it has ever seen, forever

**Status:** fixed on `fix/F880-registry-dead-entries` (RED pinned, GREEN, hermetic)
**Opened by:** F-868 §6's first bullet, *"Pruning stale records is NOT done here,
deliberately… it needs an owner"*, plus the 2026-09-15 state of the maintainer's
own record (below), which still carries both entries F-868 observed on 2026-09-14
**Source at:** `main` = `b0ae010`
**Severity:** LOW-MEDIUM. Nothing is killed and nothing is lost — F-868 already
stopped a dead entry from being *reported* as the backend. What is left is a record
that only ever grows, a cold-start probe against ports nobody listens on, a `doctor`
listing that names two backends that have not existed for days, and the raw material
for the exact class of confusion F-868 had to fix: two entries, one live, one dead,
and every reader having to pick between them.

---

## 1. What the file holds today (shape)

`~/.stealth-mcp/server.json`, schema 2, read on 2026-09-15. Fingerprints elided;
ports and context tokens are the whole of what matters.

| recorded context | port | version | recorded pid | pid running now | listener on port |
|---|---|---|---|---|---|
| `win-session-2` | 7169 | 2.1.1 | 89892 | **no** | none |
| `headless` | 19222 | 2.1.3 | 67720 | **no** | none |
| `win-session-1` | 52554 | 2.1.6 | 136672 | yes | yes, serving |

Two of three entries describe backends that do not exist. Their versions — 2.1.1 and
2.1.3 — date them: neither process has run since those releases were current. F-868
observed the same two ports a day earlier under an older third entry (2.1.5, pid
53836); the live entry has been rewritten twice since, and the two dead ones have not
moved. That is the finding in one line: **the record has a writer for every arrival
and no writer for any departure.**

What that costs, precisely and no more:

- **Every cold start walks them.** `singleton._find_running_server` walks
  `backend_registry.adoption_candidates` and calls `_server_is_healthy(port)` on each,
  so an unproven-context client pays two refused loopback connects before reaching the
  live entry. Microseconds, not a hang — but it is work that exists only because
  nothing forgets.
- **`doctor` names them.** `_doctor_backend_lines` lists every recorded backend, so an
  operator reads three `backend …` lines, two of them `down`, with no statement
  anywhere that two of the three are permanent residue rather than something that
  might come back.
- **It is the raw material for F-868.** That finding's defect was a reader picking the
  wrong entry out of a multi-entry record. The multi-entry record is here because of
  this. Fixing the readers one at a time is strictly more surface than not keeping
  entries that describe nothing.

Deliberately **not** claimed: no eviction, no fratricide, no wrong backend adopted.
`adoption_candidates` is an ordering over candidates that are then identity-gated and
probed, so a dead entry is skipped, not trusted. This is a hygiene defect.

## 2. Root cause: the record has three writers and none of them is a departure

Every write path in the tree, at `b0ae010`:

| writer | when | what it removes |
|---|---|---|
| `backend_registry.record_backend` | a backend is spawned (`singleton._write_server_state`, under the cold-start lock) | only entries **claiming the same port** ("supersede by port") and the same context's previous entry |
| `backend_registry.forget_backend` | `singleton.stop_backend`, on the context it just stopped | exactly one context, the one the operator stopped |
| `backend_registry.clear_record` | `stop_backend`, and only once **nothing** is left recorded | the whole file |

`record_backend`'s supersede-by-port rule is the closest thing to a reaper, and its own
docstring says why it cannot be one: it drops an entry *because only one process can
hold a loopback listener*, i.e. it removes a leftover on **the port being claimed**. A
dead sibling on a different port is untouched by construction. `forget_backend` needs
an operator and a `stop`. `clear_record` needs every entry gone first — which is the
state this finding says never arrives.

So the answer to "what forgets a dead entry today" is: **nothing, on any path.** The
only way one leaves the file is a later backend happening to claim its exact port.

F-868 §6 saw this and named the two halves of the decision it could not make in that
PR, verbatim:

> - **Pruning stale records is NOT done here, deliberately.** It is a separate decision
>   with a real safety argument on both sides, and it needs an owner:
>   - *Who would prune?* The only process that can prune honestly is one that has just
>     probed. A CLI verb (`status`/`doctor`) must stay read-only by contract (module
>     docstring, `STEALTH_MCP_NO_AUTO_RECOVERY=1`), so it may not. `stop`
>     already forgets what it stopped. The natural candidate is the cold-start path under
>     `_exclusive_lock` — it is the only writer that holds the lock — but a backend whose
>     pid is gone is not necessarily a backend whose PORT is free, and dropping the entry
>     loses the record that would let a later spawn step around a squatter.
>   - *What is the cost of not pruning?* Bounded: one small JSON object per display context
>     per machine lifetime, and (after this fix) no reader is misled by it. The cost of
>     pruning wrong is a live sibling made undiscoverable, which F-808 already paid for
>     once.

Both halves are answered below. The `port`-is-not-free objection is answered by making
the test stronger than "pid is gone" — see §3.2.

## 3. The decision, and the one home it lives in

### 3.1 What "dead" means here — two witnesses, never one

An entry is **dead** iff BOTH:

1. `singleton._probe_port(port)` returns **`down`** — the first rung of the F-868
   ladder, which is `not _server_is_healthy(port)`: nothing is listening on the
   loopback port at all; **and**
2. `singleton._is_our_backend(pid)` is False — the recorded pid is not a running
   process whose command line carries `stealth_chrome_devtools_mcp` **and**
   `--transport`.

Neither witness alone is sufficient, and both failure modes are already documented in
this tree:

- **Socket alone is not enough.** `record_backend` is called at **Popen time**, before
  the child binds — `port_conflict`'s docstring says so explicitly ("a spawn records
  itself at Popen time — BEFORE the new backend is ready"). A sibling's backend is
  therefore `down` for the whole of its cold start while its process is alive and about
  to serve. Forgetting on the socket alone would race every cold start on the machine.
  This is the same discrimination `_same_identity_backend_ready` already makes, in the
  same words: `if not _server_is_healthy(port) and not _is_our_backend(entry.get("pid")):
  return False  # no socket and no live process: dead, not busy`.
- **Pid alone is not enough** — and this is exactly F-868 §6's objection. "A backend
  whose pid is gone is not necessarily a backend whose PORT is free." True, and it is
  why the pid is only half the test. Requiring `down` as well means the port is
  **observed to have no listener at this instant** — not merely no listener of ours.
  There is no squatter to step around, because there is nothing bound at all. The
  record that "would let a later spawn step around a squatter" describes a port that a
  socket probe has just shown to be empty; keeping it buys nothing and is what has
  been kept for two releases.

And the two states that must **never** be forgotten fall straight out of the rule:

- **`wedged`** (socket open, `initialize` unanswered) is not `down`, so it is never
  dead. A wedged backend holds its port and will be evicted and respawned by the next
  session (F-301/F-501); its record is what `_clear_stale_backend` and `_terminate_backend`
  use to find the pid to kill. Forgetting it would strand it.
- **`responsive`** is obviously not `down`.
- An entry whose `port` is not an `int` (hand-edited, or a schema a future version
  writes) is **not probed and never forgotten**. We have no liveness evidence about it,
  and `_other_records_note`'s docstring already carries the lesson that a `None` port
  must not be allowed to make an entry disappear from a summary. The survey reports it
  as `no port recorded` and leaves it on disk.

**Display context is not consulted.** The rule reads the whole record — `read_backends`,
not `adoption_candidates` — so a `down` entry belonging to another desktop is forgotten
exactly like our own. That is correct and it is not in tension with F-808: adoption's
asymmetry exists to stop a client **reusing** a foreign desktop's *live* backend. It
says nothing about a record whose process is gone and whose port is empty. Death is not
a property of a display context. (It is also the only way the maintainer's own record
gets clean: `win-session-2` and `headless` are both foreign to the shell that runs
`cleanup`.)

### 3.2 Where it lives, and what was rejected

Three modules could own this, and the split follows the roles each one already has:

- **`backend_liveness.survey` — THE one home for the VERDICT.** "Is the backend on this
  port alive" is this module's stated subject, and `down`/`wedged`/`responsive` is its
  closed vocabulary. It stays a leaf: both witnesses arrive as arguments (`probe`,
  `pid_is_ours`), exactly as `probe_port`'s do and exactly as `backend_watchdog` takes
  its probes, so it still never imports `singleton`. `survey` takes the ENTRIES as an
  argument rather than a path, which is what makes it a single probe pass that two
  different callers can order two different ways without either of them re-deciding
  anything: `doctor` hands it `window_capable_first`, the prune hands it
  `read_backends`. `dead_entries` reduces one survey to the entries that qualify.
- **`backend_registry.forget_entries` — THE one home for the WRITE.** The record's
  schema, its atomic `_write`, and the read-merge-write protocol are this module's and
  have been since F-808 Task 2. `forget_entries` is `forget_backend`'s sibling — "drop
  these entries" beside "drop this context" — and it re-reads the record itself rather
  than being handed a list of survivors, so the merge is real (§3.3).
- **`backend_liveness.forget_dead` — the one COMPOSITION**, so no caller ever writes
  "survey, filter, forget" a second time. Three callers consume it: the cold-start lock
  holder, `cleanup --apply`, and nothing else.

Rejected alternatives, with the reason each one fails a stated constraint:

1. **`backend_liveness.probe_recorded` returns the dead candidates it walked past, and
   `singleton` forgets them.** Rejected on three counts, any one of which is fatal.
   (a) `probe_recorded` **returns at the first responsive candidate**, so the entries
   after it are never probed — its dead set is partial by construction, and on the
   maintainer's record from a `win-session-1` shell it would be empty (the live entry
   sorts first among that client's candidates). (b) It walks **adoption candidates**,
   and for a PROVEN-capable client `adoption_candidates` *excludes every foreign proven
   context* — `win-session-2`'s dead entry is not in the walk at all, so the one entry
   the task requires be forgettable is the one this design cannot see. (c) Its two
   callers are `status`/`doctor`/`stop` and the CLI; turning a read-only reporter into
   a function with a write side-channel breaks the read-only contract those verbs are
   documented to keep.
2. **`backend_registry.forget_dead(path, probe)` — the whole decision in the registry.**
   Rejected because `backend_registry` is a leaf that imports stdlib plus
   `display_context` and deliberately owns no liveness at all; handing it a probe makes
   it the second place in the tree that knows what `down` means. Its docstring's own
   rule — "no function here may take a default path", and by extension no policy that
   is not about the file — points the other way. The registry owns *which bytes to
   write*; whether a backend is alive is `backend_liveness`'s sentence.
3. **A `cleanup`-only prune with no automatic path.** Rejected as a half-answer to
   F-868 §6: it leaves "who prunes" answered only by "a human who remembers to". The
   automatic caller costs five lines and fires on exactly the occasion §6 named.
4. **Pruning on every proxy start**, outside `_exclusive_lock`. Rejected: every
   production writer of this record runs under that lock, and a lock-free writer on the
   hottest path in the product (dozens of concurrent proxy starts — the herd) is how a
   lost update gets introduced. The merge in §3.3 would survive it; that is not a reason
   to go looking.

### 3.3 The write is a real merge, not a write-back

`forget_entries` does **not** write back the entries the survey looked at. Probing is
slow (a wedged sibling costs one `LIVENESS_PROBE_TIMEOUT`), so between the probe and
the write another process may have recorded a backend. It therefore:

1. re-reads the record with `read_backends(path)` — the same read-merge-write protocol
   `forget_backend` and `record_backend` use;
2. drops only entries still matching on **all three** of `display_context`, `port` and
   `pid`, so a context that has been **re-recorded** since the probe (new port or new
   pid) is kept — it is not the entry we found dead;
3. writes nothing at all when nothing matched, so the common case costs zero writes;
4. writes the survivors through the registry's existing atomic `_write`.

It **never** unlinks the file. Forgetting the last entry leaves a readable empty v2
record, exactly as `forget_backend` does; deleting the file stays `clear_record`'s job
and `PORT_FILE` is not touched on any path here.

## 4. The fix

**`embedded/backend_liveness.py`** (+ the survey half; still a leaf, still probe-by-argument):

- `Surveyed(entry, verdict, dead)` and `survey(entries, *, probe, pid_is_ours)` — ONE
  probe per entry, the verdict in the one closed vocabulary plus `NO_PORT` for an entry
  naming nothing usable as a port, and the two-witness `dead` flag of §3.1.
- `dead_entries(surveyed)` — the one reduction.
- `forget_dead(path, *, probe, pid_is_ours)` — survey the whole record, forget what
  qualifies, return the contexts forgotten.

The `NO_PORT` constant is where `cli._probe_recorded_backend`'s one extra word now
lives, and that adapter is **deleted**: with the survey answering per entry, keeping a
second function whose whole content was "the ladder, plus one word" would be the second
way to do something already done. `doctor`'s line still reads `no port recorded`.

**`embedded/backend_registry.py`**: `forget_entries(path, entries)` — §3.3, beside
`forget_backend`.

**`embedded/singleton.py`**: five lines at the top of `_start_backend_holding_lock`'s
locked block — the one automatic prune, on the one path that has just failed to find a
reusable backend, in the one process holding the lock. It runs **before** the two early
returns so a cold start that loses the race still pays its hygiene. 985 → 990 LOC
(default 1000; no grandfather row, none added).

**`cli.py`**:

- `_cmd_cleanup` gained a `backend records` section: the dry run **names** dead entries
  and reclaims nothing; `--apply` forgets them through the same `forget_dead`. It is
  the disk-hygiene verb and a dead record is disk residue, so this is the verb's
  existing job, not a new one.
- `_cmd_doctor`'s `contexts :` block now marks each dead line `(dead record)` and ends
  with the one-line summary and the remedy. It stays **read-only**: doctor reports,
  `cleanup --apply` writes. Both come from the SAME survey pass doctor already made per
  entry — no second sweep, and a wedged sibling is still probed exactly once per run.
- `status` is untouched: it is the one-line summary and F-868 fixed what it says.

What the operator now sees (shape, on a record like §1's):

```
$ stealth-chrome-devtools doctor
  backend  win-session-1  port 52554  pid 136672  version 2.1.6  responsive  (can show windows)
  backend  win-session-2  port 7169  pid 89892  version 2.1.1  down  (dead record)  (can show windows)
  backend  headless  port 19222  pid 67720  version 2.1.3  down  (dead record)  (headless only)
  2 dead record(s) (win-session-2, headless) — nothing is listening and the
  recorded pid is not a backend of ours; run `cleanup --apply` to forget them

$ stealth-chrome-devtools cleanup
backend records: 2 dead (win-session-2, headless) — re-run with --apply to forget
$ stealth-chrome-devtools cleanup --apply
backend records: forgot 2 dead (win-session-2, headless)
```

## 5. Pins

`tests/test_registry_dead_entries.py`, hermetic throughout: the record path is
monkeypatched to `tmp_path`, `singleton._probe_port` is patched exactly as the rest of
the suite patches it, no socket is opened and no process on this machine is probed or
signalled.

| pin | what it would catch |
|---|---|
| `TestDeadIsTwoWitnesses::test_down_with_a_dead_pid_is_dead` | the rule itself |
| `…::test_down_with_a_live_backend_pid_is_not_dead` | forgetting a sibling recorded at Popen time, mid cold start — RED against a socket-only test |
| `…::test_a_wedged_entry_is_never_dead` | forgetting a backend that holds its port and is about to be evicted |
| `…::test_a_responsive_entry_is_never_dead` | the obvious one |
| `…::test_an_entry_with_no_usable_port_is_reported_not_forgotten` | a hand-edited entry vanishing on evidence nobody has |
| `TestForgetEntries::test_forgets_only_the_named_entries` | the whole record being replaced |
| `…::test_a_context_re_recorded_since_the_probe_survives` | the lost update §3.3 exists to prevent |
| `…::test_forgetting_the_last_entry_leaves_an_empty_readable_record` | `clear_record`'s job being done here |
| `…::test_nothing_dead_writes_nothing` | a write on every cold start |
| `TestForgetDead::test_a_foreign_display_contexts_dead_entry_is_forgotten` | the F-808 over-reading that would leave the maintainer's record dirty forever |
| `…::test_the_live_entry_survives_its_dead_siblings` | the §1 record, verbatim, end to end |
| `TestColdStartPrunes::test_the_lock_holder_forgets_dead_entries` | the automatic caller being dropped |
| `TestCliDeadRecords::test_cleanup_dry_run_names_them_and_forgets_nothing` | `cleanup` writing without `--apply` |
| `TestCliDeadRecords::test_cleanup_apply_forgets_them` | the verb being wired to a second sweep |
| `TestCliDeadRecords::test_doctor_names_them_and_forgets_nothing` | doctor breaking its read-only contract |

RED was confirmed against `main`'s tree before the fix: `forget_entries` / `survey`
did not exist, so the registry and liveness pins error on the missing attribute, and
the two CLI pins fail on absent output.

## 6. Not claimed / follow-ups

- **F-868 §6's first bullet is CLOSED by this finding.** Its two questions are answered
  in §3.2 (who prunes: the cold-start lock holder and `cleanup --apply`, nobody else)
  and §3.1 (the port objection: the test is `down` **and** pid-not-ours, so the port is
  observed empty, not merely not-ours). A pointer has been added to that file.
- **The prune does not fire while a reusable backend exists.** `ensure_server_running`
  returns from `_find_running_server` without taking the lock, so a machine whose
  backend is healthy keeps its dead siblings until the next cold start (reboot, backend
  death, version upgrade) or until an operator runs `cleanup --apply`. This is
  deliberate — see rejected alternative 4 — and it is why the operator verb exists
  rather than being redundant with the automatic path. Making the prune unconditional
  on every proxy start would need a lock-free writer on the herd path and is NOT
  recommended without a measurement.
- **A recycled pid reads as not-ours, which is the safe direction here** but is worth
  stating: `_is_our_backend` is a command-line check, so a pid reused by an unrelated
  process makes the entry *more* likely to be forgotten — and correctly, since the
  backend that pid named is gone. The socket witness is what stops this mattering: a
  live backend is never `down`.
- **`psutil` is not consulted directly by any new code.** The pid witness is
  `singleton._is_our_backend`, the tree's existing one, handed in as an argument. No
  `os.kill(pid, 0)` and no second liveness helper were added, per the constraint.
- **Not measured: the cost of the survey on a pathological record.** `doctor` and
  `cleanup` are interactive verbs with no deadline, and the cold-start prune costs one
  refused loopback connect per dead entry. The only slow case is a *wedged* sibling —
  one `LIVENESS_PROBE_TIMEOUT` (2 s) — which is the same case F-868 §6 already accepted
  for `status`, and it is paid once per run, not once per line.
- **Not done: a retention bound on the record.** If a machine ever accumulates enough
  contexts that the prune is not enough (it would need hundreds of distinct display
  contexts), the answer is a cap, not a faster reaper. No evidence such a machine
  exists.
