# F-1022 — `close_instance` of a browser that crashed answered True or False depending on timing (T-5956, closes F-789)

**Severity:** Medium (a flaky answer from the very call the error message tells the caller to make). **Files:** `embedded/tool_errors.py` (`claim_exited`, `exited_selection`, `remember_departed(exited=)`), `embedded/clone_protection.py` (`release_selection`), `embedded/tool_sections/browser_management.py` (`close_instance`), `embedded/browser_manager.py` (`close_instance` miss branch, `_discard_instance_unlocked`; net zero lines, still at its 1438 cap, the +4 paid for by trimming three comment blocks), `tests/test_close_instance_offload.py`, `tests/test_resilience.py`, `tests/MANUAL_QA_PROTOCOL.md`, `.github/workflows/release-gate.yml` (comment), F-789 status. F-1022 was unclaimed (F-1014..F-1021 claimed elsewhere; every remote branch and `audit/stage2` censused).

## 1. Evidence

PR #264 run 39681365338, integration (macOS/ARM64): `test_crash_recovery_after_the_owned_chrome_is_killed` failed its `closed["closed"] is False` pin: `close_instance` returned True. The same cell passes elsewhere.

## 2. Cause

After the Chrome tree is killed, the next tool call (`navigate`) asks `get_instance`, and `get_instance`/`list_instances` DISCARD an instance whose process is not running (`_discard_instance_unlocked` pops it). `close_instance` then finds nothing and answers False ("no such instance"). If the failure surfaced without a discard (the call failed on the dead connection before any liveness check, as on the macOS run), `close_instance` itself claims the instance and answers True. Same browser, same crash, two answers, decided by which code path noticed first. F-789 had pinned the False of the first ordering as a known quirk.

## 3. Which answer is correct

True. The caller was told to "close the instance with close_instance"; the post-conditions (untracked, no process, profile reclaimable) are met on both paths, and a failure signal for an operation that succeeded cannot be told from a real cleanup failure (F-789's own argument). So the False ordering was the defect, not the True one.

## 4. Fix

`_discard_instance_unlocked` (a browser that died on its own) records the id with `tool_errors.remember_departed(id, data, exited=<the spawn's profile_selection dict>)`; `close_instance`'s miss branch returns `tool_errors.claim_exited(id)`: True exactly once for such an id, then False like any double close. A browser the caller closed is never recorded, an id never held is False, and the record is bounded (64, oldest out). Both orderings now answer `closed: True`.

**Correction (review of PR #267, M1).** The sentence this paragraph first ended with, that the tool layer's cleanup (clone release included) "runs on the discard path too", was wrong for the clone release, the seed refresh and the shared-session refusal: the discard popped `_spawn_diagnostics`, so the tool read `profile_selection == {}` and skipped all three. The fix, in one home each:

* **Clone release.** `_discard_instance_unlocked` calls `clone_protection.release_selection(selection)`, so any crash discard releases the clone dir (sweep-eligible), whether or not `close_instance` ever follows. `close_instance` calls the same function after a real close; it is idempotent. (This was a pre-existing leak on the old `False` path too.)
* **Selection kept.** `tool_errors._exited` now maps id -> the spawn's `profile_selection` (still bounded at 64, still claimed once); the tool reads it with `exited_selection(id)` (None when the id is not one) when the manager has no diagnostics left. Without it a second-arriving close cannot tell a clone from `fleet`/`default`. The tool does NOT call `get_instance` first: a lookup discards a dead browser, which `tests/test_unnamed_spawn_never_opens_default.py::TestCloseGuard` (F-966) forbids on purpose, because the shared-session guard must read the recorded selection without one. Close-first parity comes instead from `BrowserManager.close_instance` noting, as it claims the instance, that its process was already dead (`tool_errors.note_dead_on_close`, read once by the tool as `take_dead_on_close`).
* **No second release (review 2).** Clone dir names are deterministic per client session, so after the discard frees one a respawn can reserve it again; a later close of the OLD id releasing it again would drop the NEW spawn's protection. A close whose selection came from `exited_selection` therefore does not release; only a close that popped a live instance does.
* **Seed refresh: NOT done from a crashed `default` browser (review 2, reverses review 1).** The refresh guard (`_refresh_master_snapshot_if_safe`) tests "held", not "intact", and a crashed browser's profile may be torn; master-profile loss is top severity. The tool answers `seed_refreshed: false, seed_error: "default-crashed"` in both orderings and never calls the refresh; the existing good seed stays.
* **Shared-session refusal: kept.** An unnamed close of a crashed `fleet`/`default` id is refused as before; there is nothing live to protect, but the caller still must not be told the id was an ordinary browser, and a refused close does not consume the claim.

## 5. Pins

Hermetic: `test_closing_a_browser_that_crashed_answers_closed_either_way_round` (close first; `get_instance` discarded first; `list_instances` discarded first; the second close and a never-held id are False) and `test_a_close_never_claims_a_browser_that_was_closed_not_crashed`. RED on the pre-fix source: the first fails (`assert False is True` on the discard-first ordering); the second passes before and after (control). The real-Chrome pin in `test_resilience.py` is now `closed is True` and deterministic, since both orderings agree. This is the deliberate SOFT-golden update: its old False was the quirk, the justification is section 3.

## 5b. Review pins (M1)

Tool-layer, real manager, both orderings (`test_a_crashed_clone_is_released_to_the_sweep_whichever_call_noticed`, `test_a_crashed_default_browser_refreshes_the_seed_either_way_round`, `test_an_unnamed_close_of_a_crashed_fleet_browser_is_refused_either_way_round`). Review 2 adds `test_closing_a_crashed_id_never_unprotects_a_respawn_in_its_dir`, `test_a_crashed_non_clone_dir_is_never_unprotected` (named, fleet, default; both orderings) and `test_a_crashed_default_browser_never_refreshes_the_seed` (replacing the "refreshes either way round" pin). Mutated by runtime rebinding, out of tree: `release_selection` -> no-op (clone pins fail), the discard's reference only (discard-first clone pin fails), the tool releasing a crashed id again (respawn pin fails), `release_selection` releasing every role (6 non-clone pins fail), the tool believing nothing crashed (seed and refusal pins fail).

## 6. Residuals

* MQ-126 stays `planned`: promoting it means binding the node in the gate, which is a separate change; F-789 no longer blocks it.
* The macOS RED itself was not reproduced locally (Windows); the two orderings are pinned directly.
* Not changed here: the next clone's stale-seed refresh (`clone_storage._refresh_snapshot_if_stale`) is a separate, pre-existing path whose guard is also "held", not "intact"; after a crash of `default` it can still copy the unclean profile over the good seed. Recorded as a residual to measure and fix on its own.
* The claim is in-process memory, like `_departed`; a backend restart forgets it (then False, as for any unknown id).
