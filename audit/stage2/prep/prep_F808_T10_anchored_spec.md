# prep-t10: anchored implementation spec for plan_F808 Task 10 (fratricide fix)

Produced 2026-08-02 by a read-only Opus research agent against worktree
`.claude/worktrees/f808` at HEAD 58e7725 (branch fix/F808-headed-visibility).
Line numbers are that HEAD. This file is the dispatch spec for the Task 10
implementer; verify anchors still hold if intervening commits touched
process_cleanup.py (Task 6 is CLI-only, so they should).

## Two findings that shape the plan

(A) `embedded/process_cleanup.py` measures EXACTLY 1054 LOC and its
grandfathered cap is EXACTLY 1054 (`tools/check_file_budgets.py:64`). Zero
headroom. The extraction to the new leaf is the ENABLING step — the budget
gate fails the first commit that adds owner fields before the move lands.
Sequence the work: move first, then add owner logic.

(B) `embedded/backend_registry.py` (441 LOC, this plan's Task 2) is the
precedent leaf to copy almost verbatim: module docstring declares the leaf
contract ("stdlib plus display_context. Never singleton, never server");
every public function takes the record path as a REQUIRED argument with no
default (docstring lines 17-19; `tests/test_backend_registry.py:27-58`
enforces by signature inspection); atomic writes via pid-suffixed sibling
temp + `Path.replace` with a Windows sharing-refusal retry loop (`_commit`
378-401, `_write` 403-427); `_write` does
`path.parent.mkdir(parents=True, exist_ok=True)` (:413) — which today's
`_save_tracked_pids` does NOT, a landmine for redirected test paths.

## 1. Every browser_pids.json site in process_cleanup.py

Path binding:
- `:23` `from ...embedded.singleton import STATE_DIR`
- `:35` `self.pid_file = STATE_DIR / "browser_pids.json"` — ONE binding, no
  per-context keying: the defect.

Read:
- `:218-237` `_load_tracked_pids` — read + `_file_lock` + json.load →
  `_normalize_process_metadata`. One caller: `:623`.

Write (the truncate-before-lock defect):
- `:239-258` `_save_tracked_pids` — `:251`
  `with self.pid_file.open("w") as fh, self._file_lock(fh):` — mode "w"
  truncates BEFORE the lock is entered; lock failure (OSError after 4
  retries, `:195-205`) is swallowed at `:253` with the file already empty.
  Also a full replace of `self.browser_processes` → last-writer-wins across
  backends. Callers: `:701` (track), `:739` (untrack), `:785`
  (kill_browser_process pid=None path), `:821` (finalize), `:995`
  (`_cleanup_all_tracked`).

Unlink — one impl, three call sites:
- `:999-1014` `_clear_pid_file` (unlink at `:1008`); called from `:647`
  (end of `_recover_orphaned_processes` — UNCONDITIONAL: where surviving
  entries get destroyed), `:741` (untrack when table empties), `:997`
  (`_cleanup_all_tracked` when table empties).

Lock primitive:
- `:188-189` `_LOCK_RETRIES = 4`, `_LOCK_RETRY_DELAY = 0.05`;
- `:191-216` `_file_lock` — msvcrt.locking(LK_NBLCK) / fcntl.flock(EX|NB),
  bounded retry then raise.

Schema normalizer — LANDMINE:
- `:107-150` `_normalize_process_metadata` builds a FIXED six-key dict
  (`:124-131` legacy-int form, `:136-143` dict form) and silently DROPS
  unrecognised keys. Adding owner fields in `track_browser_process` alone is
  insufficient — they vanish on next load. The normalizer must learn
  `owner_pid`/`owner_create_time` explicitly, and the legacy-int branch must
  leave them ABSENT so legacy entries classify dead.

The reaping (psutil scan + create_time comparison):
- `:260-290` `_get_active_browser_profile_dirs` — system-wide process_iter;
- `:292-328` `_get_browser_pids_for_profile` — system-wide scan matching
  `--user-data-dir`: what makes the kill system-wide;
- `:330-345` `_fallback_pid_identity_ok(fallback_pid, stored_create_time)`
  — the EXISTING 1.0s create_time tolerance, None-tolerant. Reuse it.
- `:347-432` `_kill_processes_for_metadata(..., recovery=False)`; recovery
  branch `:371-413` keeps pids with `create_time < self._init_time` (`:378`;
  `_init_time` = time.time() at `__init__` `:41`). Backend B's browsers all
  predate backend A's import → fratricide.
- `:616-648` `_recover_orphaned_processes` — load (`:623`), per-entry kill
  (`:628-630`) + `_cleanup_profile_for_metadata` (`:632`; irreversible
  rmtree of auto-clones via `:500-527` → `:434-498`, rmtree `:479`), then
  `_clear_pid_file()` (`:647`) and `_sweep_orphaned_temp_profiles()`
  (`:648`).

## 2. singleton._is_our_backend and the 1.0s tolerance

`src/.../embedded/singleton.py:242-257`: `def _is_our_backend(pid) -> bool`
— isinstance(int) guard (`:250-251`), `psutil.Process(pid).cmdline()` in
try/except (psutil.Error, OSError) → False (`:252-255`), True iff joined
cmdline contains BOTH "stealth_chrome_devtools_mcp" AND "--transport"
(`:256-257`). Excludes the stdio proxy. Never raises. Callers: `:213`,
`:277`, `:296`.

Gap: no create_time check → recycled pid running a DIFFERENT backend reads
True (conservative: skip-reap, never wrong-kill). The 1.0s tolerance closes
it and belongs in the CALLER:

```python
# in process_cleanup.py — the adapter injected into the leaf
def _owner_backend_alive(self, owner_pid, owner_create_time) -> bool:
    return singleton._is_our_backend(owner_pid) and self._fallback_pid_identity_ok(
        owner_pid, owner_create_time
    )
```

`_fallback_pid_identity_ok` IS the 1.0s tolerance (`abs(actual-stored) <
1.0`, True when stored is None, handles NoSuchProcess/AccessDenied/Zombie →
False). Do NOT add a second `1.0` literal ("second way is a defect").

None interaction: stored=None → True from the tolerance helper; that cannot
conflate with the legacy rule because LEGACY = no `owner_pid` KEY, decided
at the ENTRY level BEFORE the callable is consulted. Make that ordering
explicit in the leaf.

Import check: process_cleanup already imports singleton (`:23`); singleton
does NOT import process_cleanup (verified) — no cycle. Cross-module
underscore call precedent: `cli.py:390` calls
`singleton._probe_backend_status()`. The NEW leaf imports neither singleton
nor server (hence injection); with the adapter above the leaf needs no
psutil at all — it receives `(owner_pid, owner_create_time) -> bool`.

## 3. The seam

Two call sites only:
- `server.py:243` `process_cleanup.activate()` inside `app_lifespan`
  (`:227-249`) behind `_LIFESPAN_STARTED` (`:223`, `:236-237`). `activate()`
  (`process_cleanup.py:43-48`) returns early on
  `get_settings().no_auto_recovery`.
- `cli.py:399` `process_cleanup.process_cleanup.recover_orphans()` in
  `_cmd_kill_orphans` (`:371-403`) behind a live-backend guard
  (`:390-397`, `--force` overrides). `recover_orphans()` (`:50-52`) is
  UNGATED (no settings check).

Injection: do NOT thread the callable through `activate()`/
`recover_orphans()` signatures. Bind the adapter as a bound method on the
ProcessCleanup instance and pass it into leaf calls from inside
`_recover_orphaned_processes` / `_save_tracked_pids`. Keeps public seams
unchanged (`test_doc_claims.py:190-194` asserts activate/recover_orphans on
the class; `tests/test_cli.py` patches `_recover_orphaned_processes` by
string at `:170,:194,:211,:227,:243` — all keep working).

Also: `server.py:275` calls `process_cleanup._cleanup_all_tracked()` in the
shutdown path — hits `_save_tracked_pids`/`_clear_pid_file` at `:995/:997`
and must become merge-safe too (a backend shutting down must not wipe
another backend's entries). Known audit finding about private-member reach
is NOT Task 10's job; merge-safety of this path IS.

## 4. Current test coverage

`tests/test_process_cleanup.py` (589 lines):
- Update-deliberately / verify:
  - `:169-267` TestRecoveryFiltering — `test_old_process_killed_in_recovery`
    (`:182`) has NO owner fields and asserts killed → under legacy-dead rule
    stays GREEN unchanged. Verify, don't assume.
  - `:275-298` TestPidFilePersistence.test_save_and_load — extend with
    owner fields rather than replace.
  - `:448-493` TestAutoCloneMetadata — calls
    `ProcessCleanup._normalize_process_metadata` as CLASSMETHOD; keep a
    classmethod wrapper on ProcessCleanup post-move so `:58,:75,:83,:88,:97,
    :486,:491` stay green with no edits.
- Untouched: `:55-100`, `:108-134`, `:142-161`, `:306-406`, `:409-445`,
  `:501-588`.
- Hazard: `_make_cleanup` at `:175` and `:506` set pid_file to a REAL home
  path (`~/.stealth_browser_pids_test.json`) — harmless today (no writes);
  do NOT copy the idiom; new pins use tmp_path.

`tests/test_process_cleanup_import_guard.py` — all stays green.
`tests/test_cli.py:153-243` — green if `_recover_orphaned_processes`
survives by name.
`tests/test_doc_claims.py` — `LIVE_EMBEDDED` (`:91-115`) does not include
backend_registry/display_context, so the new module does not fail it; but if
Task 7 adds browser_pid_registry to CLAUDE.md's nav map, add it there too.
`TestLoadBearingSymbols` `:179` + `:190-194` must stay satisfied.
`tests/test_no_silent_excepts.py` — allowlist frozen empty; every swallowed
except in the leaf needs a debug_logger call or narrow except (see
backend_registry.py:420-427 `contextlib.suppress(OSError)` + ACCEPTED GAP
comment; debug_logger is the ONE internal import such leaves may use).

Monkeypatchers that never touch the file (no changes expected):
test_sweep_deferred_cleanup.py:44-74, test_lifespan_reentrancy.py:84/:116,
test_close_instance_offload.py:74-121, test_touch_activity_semantics.py:58-62,
test_profile_pid_check.py:18, test_clone_trash_recovery.py:148,
test_exception_handling.py:241-250, test_bug_prone_tools.py:194,
test_resilience.py:108/217/262.

No test asserts `_clear_pid_file` is called; none references `_file_lock` —
the unlink-on-recovery behavior is UNPINNED and can change without a golden
update.

## 5. LOC ratchet (pre-computed)

Measured as `check_file_budgets.py:91` does (`len(read_text().splitlines())`):
process_cleanup.py = 1054/1054. backend_registry.py = 441 (reference size).

Gross moving out ≈ 149: `_normalize_path` :54-67 (~15),
`_normalize_process_metadata` :107-150 (~45), `_LOCK_*`+`_file_lock`
:188-216 (~30), `_load_tracked_pids` :218-237 (~21), `_save_tracked_pids`
:239-258 (~21), `_clear_pid_file` :999-1014 (~17).
Coming back: ~6 thin delegating wrappers (~36) + imports (~2) + owner
skip/retain logic (~12) + owner fields in track (~3).
Net estimate: −90..−115 → ~940-965 LOC.

Do NOT hard-code a cap from the estimate. Procedure: land the move, `ruff
format`, run `python tools/check_file_budgets.py`, set the cap to the
PRINTED loc. Ratchet comment above check_file_budgets.py:64 in the C1 style
naming plan_F808 Task 10. New leaf lands under 1000 → NO grandfather entry.

## 6. The six hermetic pins (+ optional 7th)

Template: `tests/test_backend_registry.py` (plain module import, explicit
tmp_path-derived paths, no module-global monkeypatching). New file:
`tests/test_browser_pid_registry.py`. `call_tool`/`pretend_display_context`
not needed (no MCP tool driven).

Shared helper:
```python
def _entry(pid, *, owner_pid=None, owner_create_time=None, user_data_dir=None,
           auto_clone=False, uses_custom_data_dir=None):
    e = {"pid": pid, "create_time": None, "user_data_dir": user_data_dir,
         "uses_custom_data_dir": uses_custom_data_dir, "auto_clone": auto_clone,
         "timestamp": 0}
    if owner_pid is not None:
        e["owner_pid"] = owner_pid
        e["owner_create_time"] = owner_create_time
    return e
```

P1 `test_legacy_entry_without_owner_fields_is_reapable` — parametrize
legacy bare-int and dict-without-owner forms; callable `lambda pid, ct:
True` (deliberately generous); assert classified DEAD. Absence of owner
fields is decided BEFORE the callable runs. Guards the 2.0.3 upgrade path.

P2 `test_entry_owned_by_live_backend_is_neither_killed_nor_dropped` —
ProcessCleanup.__new__ + tmp_path pid_file (as test_process_cleanup.py:
312-319); entry "other" owner_pid=4242, auto_clone dir EXISTS on disk;
adapter → True for 4242; patch `_kill_process_by_pid` (record calls) and
`_get_browser_pids_for_profile` → {9999}. Run
`pc._recover_orphaned_processes()`. Assert: no kill, clone dir still
exists, AND reloaded file still contains "other" (part 3 is the
most-missed half). RED today on all three counts. The most important pin.

P3 `test_entry_owned_by_dead_backend_is_reaped_and_dropped` — same but
adapter → False. Assert kill + rmtree + entry gone. Proves recovery still
works.

P4 `test_save_merges_instead_of_replacing` — two ProcessCleanup instances
sharing one tmp_path pid_file; A tracks "inst-a", B tracks "inst-b"; A.save
then B.save; fresh load shows BOTH. RED today (full replace at :248).

P5 `test_failed_lock_does_not_destroy_existing_entries` — seed file;
monkeypatch the leaf's lock helper to raise OSError immediately; attempt
save; file still parses and holds the seeded entry. Deterministic,
single-threaded. RED today (truncate at :251 precedes the lock; except at
:253 swallows). THE pin proving truncation moved inside the lock.

P6 `test_owner_fields_round_trip_through_normalize` — raw record with owner
fields survives the normalizer with values intact; legacy bare-int form
yields NO owner_pid key (not None). Guards the drop-unknown-keys landmine.

Optional P7 — copy test_backend_registry.py:27-58 shape: no public function
in the leaf defaults its path param (nor any Path default) + non-vacuity
companion. The only guard keeping test runs off the real
~/.stealth-mcp/browser_pids.json.

## 7. STEALTH_MCP_NO_AUTO_RECOVERY interactions

- conftest.py:30 setdefaults it to "1" session-wide; settings.py:69;
  read at process_cleanup.py:45.
- conftest.py:46-53 autouse fixture clears get_settings cache around every
  test → monkeypatch.setenv IS visible to prod code.
- singleton.py:375-376 strips ONLY this key from child_env → real spawned
  backends DO run recovery (the production fratricide path).
- `recover_orphans()` is UNGATED → all pins can call
  `pc._recover_orphaned_processes()` directly, env-var-independent. Only an
  `activate()`-routed pin would no-op; if needed, follow
  test_process_cleanup_import_guard.py:41-52 (`patch.dict(os.environ, env,
  clear=True)`). None of P1-P6 should route through activate().
- conftest also sets STEALTH_MCP_NO_ERROR_REPORTING=1 — leave alone. Do not
  invent a new env knob (unknown STEALTH_MCP_* keys crash get_settings();
  universal-fix preference applies regardless).

## Landmine for the implementer

`_save_tracked_pids` has no mkdir(parents=True); production survives only
because singleton._ensure_state_dir (singleton.py:83) made STATE_DIR first.
The leaf must create the record's parent (match backend_registry.py:413) or
redirected test paths fail confusingly.
