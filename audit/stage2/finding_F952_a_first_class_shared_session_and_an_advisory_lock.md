# F-952 — a first-class shared session (`fleet`) and an advisory lock for agents that share it

**Severity:** Medium (feature; it closes a data-loss path for the one login nobody can re-create).
**Files:** new `embedded/fleet_session.py`, `embedded/session_lease.py`,
`embedded/tool_sections/session_lock.py`; edits to `browser_reattach.py` (restore order, no reap,
`Held.running`), `clone_storage.py` (`_clone_seed`, the seed hook), `tool_sections/browser_management.py`
(the already-running marker), `tool_runtime.py`, `tool_sections/__init__.py`, `server.py`
(`--disable-session-lock`), `settings.py` (`seed_session`). Tests: `tests/test_fleet_session.py`
(28), `tests/test_session_lease.py` (21), plus 2 guards in `tests/test_cookie_handoff.py`.
**Branch:** `feat/f952-shared-session`, stacked on the unmerged F-950 branch (e5cd77b).

---

## 1. What was asked, and what was already true

The brief wanted a named, persistent shared session that survives `close_instance` and backend
restarts, is never reaped, is restored first, is returned (not walked) when asked for while
running, an explicit lock, clones seeded from it, and a way to adopt an already-running browser.

Most of requirement 1 already holds for ANY named session. Measured by reading the code, then
pinned (those pins pass on the baseline, by design):

| Property | Where it already lives |
|---|---|
| survives `close_instance` | `process_cleanup._cleanup_profile_for_metadata` refuses a persistent entry before looking at the path |
| survives a restart | F-888 `browser_reattach.run`, record kept by `browser_pid_registry.on_persistent_profile` |
| not reaped by GC/sweep/startup recovery/orphan sweep | marker `auto_clean: false` -> `is_named` and not `is_auto`; `kill-orphans` needs `--force` |
| `seed_from=<name>` with the live jar | F-897/F-898 (`profile_source.seed_source`, `cookie_handoff`) |
| a walked clone name as a source | it is a directory under the sessions root, i.e. a NAME |

So F-952 is not a new profile kind. It is the gaps around a *shared* one.

## 2. Decisions

### 2.1 The name is `fleet`, and it is not a reserved role

"Shared session" already means the `default` session here (`Roots.shared`, CLAUDE.md glossary).
Reusing the word would give it two meanings, so the lead's "e.g. `shared`" was NOT followed.
`fleet` is an ordinary name anchored to `<sessions>/fleet`. A reserved role (like `default`) would
be a second way to say "persistent named session" (convention 4) and would need new cases in the
marker, the reap guard and the close path. The constant lives in `fleet_session.FLEET_SESSION` and
is the only place the word is spelled in `src`.

### 2.2 Restored first; a failed attach does not reap it

`browser_reattach.run` iterates `fleet_session.restore_first(classified.adoptable)` — a stable sort
that moves the fleet entry to the front and leaves the rest in recorded order. The attach budget is
per entry, so a wedged neighbour already could not stall the fleet; the ordering makes it
deterministic and means the fleet's `instance_id` is back before a client re-initialises.

`run`'s blanket handler reaps on ANY non-`Refused` failure (a refused connect, no tab, a wedged
Chrome). For a scratch profile that is right. For the fleet it is the worst available outcome: the
browser holding the login is killed and its record dropped on one transient failure.
`fleet_session.spare_on_failed_attach` logs the WARNING and `continue`s before `failed.add`: the
browser stays running and recorded, and the next backend's pass tries again. Trade-off: a genuinely
dead fleet record is not cleaned by this path; the existing liveness checks in `adoptable` drop an
entry whose pid is gone, so only a live-but-unattachable Chrome is retained, and the operator
remedy is `kill-orphans --force` (documented in RUNBOOK). Contrast pinned:
`test_any_other_profile_still_takes_the_reap`.

### 2.3 Asking for it while it runs here

`adopt_held_profile` already returned the running instance for a named session driven by this
backend (`reuse_ours=bool(user_data_dir)`), so a walk to `fleet-2` was never reachable on that
path. What was missing was an EXPLICIT marker: the answer was indistinguishable from a fresh spawn.
`Held` gained `running: bool` (True only for a browser this backend was already driving, not for a
stranded one it just adopted), and `spawn_browser` merges
`fleet_session.reuse_answer(held.running, user_data_dir)` over `_adopted_instance_record`'s answer:
`already_running: true` and `session_lock` (the lease status of that session, so the caller learns
who holds it in the same reply). The merge sits at the call site rather than inside
`_adopted_instance_record` because existing tests fake that function with a fixed two-argument
signature. The marker applies to every named session, not only `fleet`: the property is general.

An unnamed spawn is still never handed it (`reuse_ours=False`; pinned with the fleet running).

### 2.4 The lock is advisory

A tool call carries no caller identity. `owner` is a label the caller types, so a lock could only be
ENFORCED if every other tool took and checked the same label, or if the lock bound to the MCP
session — a change to all 94 existing tools and to the proxy/session model, out of scope and
fragile (an agent crash would then need a recovery path). The lease is therefore the shared record
of whose turn it is, surfaced in three places: its own tools, the refusal text, and the
`session_lock` field on an already-running spawn. Revisit if two agents ever contend despite it.

Behaviour (all pinned, `test_session_lease.py`): acquire is non-blocking by default; `wait_seconds`
(0-120) polls every 250 ms; the same owner re-acquiring renews; a refusal names holder and expiry
(epoch seconds and seconds-from-now); a lease expires by itself; only the holder releases, and a
release of a free or expired session is refused (the caller's picture of who holds it is wrong);
`lease_seconds` (1-3600) and `wait_seconds` are REFUSED out of range, not clamped, since a caller
who asked for a day and silently got an hour plans around the wrong expiry. State is in memory,
per backend: a lease is short, the session survives a restart and the lease does not, and a
restart clearing every lease is the safe direction. Expiry uses `time.monotonic()` so a wall-clock
step cannot extend or cut a lease; `acquired_at`/`expires_at` are wall-clock for the caller.
Session names go through `profile_seed.require_name`, the same gate `session=` passes.

Tool names follow the acquire/release/get verb taxonomy: `acquire_session_lock`,
`release_session_lock`, `get_session_lock_status`. They live in a NEW section `session-lock`
(`tool_sections/session_lock.py`): `test_tool_sections_contract` requires a section to equal exactly
one module, so a new group cannot join an existing one. The count is 97 across 12 sections.

### 2.5 The default seed is opt-in

`STEALTH_MCP_SEED_SESSION=<name>` (settings field `seed_session`, default empty) makes that session
the source for every NEW clone and NEW named session, with its live jar handed over when this
backend drives it. It is OFF by default because it changes where every unnamed clone's cookies come
from, and the master-snapshot is the one source that is always safe to copy. The owner turns it on
once in `~/.stealth-mcp/.env`. (A universal fix over a knob was preferred where the default is
right; here no default is right for every user — a machine without a `fleet` has nothing to seed from.)

Mechanics: `clone_storage._clone_seed` (extracted from `resolve_profile_selection`, which was at
the statement and branch limits) returns `(seed, configured)`; `fleet_session.default_seed` is asked
only where a copy is about to be made, so a misconfigured value cannot break an unnamed spawn that
opens the free shared profile itself. The configured session never seeds itself (its first creation
bootstraps from the snapshot). A missing source is refused naming the setting and nothing is
created. A caller's `seed_from` always wins. When the seed is a configured session its own live jar
is used and the master's live jar (F-939/F-914) is NOT layered over it (`configured` guard).
The retry door (`_fallback_profile_selection`) honours it too: a clone that needed a retry is seeded
from the configured session, never silently from the snapshot (a second way to seed).

### 2.6 F-939 is preserved without new code

The Google rotation guard arms on `options.auto_clone`, set from `profile_role == "clone"`. The
fleet resolves to `explicit` (a source; it rotates), a clone seeded from it to `clone` (fenced).
Both role words are pinned in `test_fleet_session.py`; the arming itself is pinned by the existing
`TestTheSpawnArmsClonesOnly`.

### 2.7 Adopting a running browser, and walked clone names

`spawn_browser(session="fleet", seed_from="<running session name>")` is the supported path. It is
the existing F-897/F-898 route, so it needs this backend to DRIVE the source (live jar) and refuses
by name otherwise. A walked clone name such as `upup-b9be57135713-84488-27` IS accepted: it is a
bare name for a directory in the sessions dir, the one browser a human has typically logged in to,
and refusing it would leave that login unadoptable. The new session is a persistent `explicit-*`
profile, not a clone (`is_named`, not `is_auto`), and records `seeded_from` as the source's name.
Limits, stated in the docs: cookies only (no localStorage/IndexedDB from a running source), and a
source held by another process is refused.

## 3. Budget and ripple

`browser_reattach.py`, `clone_storage.py`, `browser_management.py` are all at their 1000-line cap;
every edit was paid for in comments (the F-856 payment mechanism). New code lives in three new leaf
modules. Updated: `tool_runtime.__all__` (+`fleet_session`, `session_lease`), `SECTION_MODULES`,
`server.py` flags/descriptions, `MIN_TOOL_SOURCE_FILES` 13 -> 14, the count 94 -> 97 in
CLAUDE/README/DESIGN/CONTRIBUTING/agent-setup docs and in `test_tool_registry`, `test_tool_dispatch`,
`test_tool_module_reload`, `test_doc_claims`, `test_e2e_functions_hooks` (the three tools are
`E2E_EXEMPT` with a reason: no browser involved), `release_gate_harness`; the SOFT golden
`tests/goldens/tool_surface.json` (three additions, plus `spawn_browser`'s description, which now
documents `already_running`) and the generated `RELEASE_CONTRACT.md`.

## 4. RED -> GREEN

Measured, not estimated. The base is `ec5a81e^` (F-950 is already in it), exported with `git archive`
(no shared worktree mutated); the FINAL test files were copied over it, with `fleet_session.py` and
`session_lease.py` present but empty so the files import. Four files, 112 tests:

| file | at base | after |
|---|---|---|
| `test_fleet_session.py` (30) | 25 failed, 5 passed | 30 passed |
| `test_session_lease.py` (21) | 21 errors (the module does not exist) | 21 passed |
| `test_cookie_handoff_rejected.py` | 1 failed (partial-jar warning) | all passed |
| `test_cookie_handoff.py` | 1 failed (top-level `seed_warning`) | all passed |

Total at base: 27 failed, 64 passed, 21 errors; after: 112 passed. The 5 `test_fleet_session.py`
tests that pass at base are the "verify and pin" claims that were already true: persistence,
`seed_from` live hand-off, refusal by name, and adoption from a running or walked-clone source.
The real-Chrome node `tests/test_e2e_fleet_session.py` (marked integration) is not in that table.

Follow-up (team-lead review): two more places a caller could be misled, each RED before GREEN
(2 failed, the 2 no-warning guards passing, against the unchanged source).
`fleet_session.headless_mismatch(requested, actual)` is merged into the reuse answer: the default
`headless=False` is compared like any value, so a bare call that lands on an adopted headless
browser gets `headless_mismatch` (both values and the remedy), not a silently invisible browser.
`_seed_cookies_over_cdp`'s failure return gains `seed_warning`, surfaced at the top level of the
`spawn_browser` answer; a hand-off that succeeded adds nothing. The warning reuses the already
sanitised `cookie_handoff.failure` reason, so no cookie name or value can reach it (pinned).
A PARTIAL hand-off (`cookies_rejected > 0`, F-950's bisection) gets its own, distinct
`seed_warning` from `Handoff.record()` naming only the count. A retry clone
(`_fallback_profile_selection`) now honours `STEALTH_MCP_SEED_SESSION`: `_clone_seed` tries the
configured session before a retry's `override`, and the fallback no longer needs a snapshot when
one is configured (RED: the `retry` and `final` attempts cloned the snapshot).

### 4.x Review fixes

- **One lock key per directory** (`fleet_session.lock_key`): the master is `default`, a direct
  child of the clone root is its name casefolded, anything else its normcased resolved path. The
  lock tools and `reuse_answer` both use it (pinned: a running `default`; `Fleet` and `fleet`; an
  absolute directory merely ending in `fleet`).
- **Bounds**: `wait_seconds` capped at 60 (it can still exceed a client's per-call timeout, and
  waiters are not served in order), owner 128 characters, key 255, expired leases swept on acquire.
- **Unusable seed session**: an UNNAMED clone falls back to the snapshot with a `seed_warning`
  (`fleet_session.clone_seed`); an explicit `seed_from` or a NEW named session still refuses by
  name. A retry's `default_seed` error never replaces the original spawn error.
- **F-939 for named sessions**: a NEW named session whose jar came from a LIVE hand-off shares its
  source's Google chain, so `BrowserOptions.guard_rotation` arms the guard at its creation launch,
  and `seeded_via` in the clone marker re-arms it on later launches.


## 5. Open items

- The lock is advisory (2.4). Enforcement would need caller identity.
- A fleet browser that is alive but permanently unattachable is retained rather than reaped; the
  operator removes it with `kill-orphans --force`.
- `hand-off` is cookies only; sites that keep their token in localStorage need a CLOSED source.
- The real-Chrome node uses headless Chrome on a tmp session root; it does not cover a HEADED fleet.
- A session the user later signs in to Google afresh is still guarded (the marker says it was
  seeded live); the guard also blocks that session's own legitimate rotation (escape:
  `STEALTH_MCP_ALLOW_CLONE_GOOGLE_ROTATION`). A browser re-attached after a restart is not re-armed.
