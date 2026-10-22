# F-1022 — `close_instance` of a browser that crashed answered True or False depending on timing (T-5956, closes F-789)

**Severity:** Medium (a flaky answer from the very call the error message tells the caller to make). **Files:** `embedded/tool_errors.py` (`claim_exited`, `remember_departed(exited=)`), `embedded/browser_manager.py` (`close_instance` miss branch, `_discard_instance_unlocked`; net zero lines, still at its 1438 cap), `tests/test_close_instance_offload.py`, `tests/test_resilience.py`, `tests/MANUAL_QA_PROTOCOL.md`, `.github/workflows/release-gate.yml` (comment), F-789 status. F-1022 was unclaimed (F-1014..F-1021 claimed elsewhere; every remote branch and `audit/stage2` censused).

## 1. Evidence

PR #264 run 39681365338, integration (macOS/ARM64): `test_crash_recovery_after_the_owned_chrome_is_killed` failed its `closed["closed"] is False` pin: `close_instance` returned True. The same cell passes elsewhere.

## 2. Cause

After the Chrome tree is killed, the next tool call (`navigate`) asks `get_instance`, and `get_instance`/`list_instances` DISCARD an instance whose process is not running (`_discard_instance_unlocked` pops it). `close_instance` then finds nothing and answers False ("no such instance"). If the failure surfaced without a discard (the call failed on the dead connection before any liveness check, as on the macOS run), `close_instance` itself claims the instance and answers True. Same browser, same crash, two answers, decided by which code path noticed first. F-789 had pinned the False of the first ordering as a known quirk.

## 3. Which answer is correct

True. The caller was told to "close the instance with close_instance"; the post-conditions (untracked, no process, profile reclaimable) are met on both paths, and a failure signal for an operation that succeeded cannot be told from a real cleanup failure (F-789's own argument). So the False ordering was the defect, not the True one.

## 4. Fix

`_discard_instance_unlocked` (a browser that died on its own) records the id with `tool_errors.remember_departed(..., exited=True)`; `close_instance`'s miss branch returns `tool_errors.claim_exited(id)`: True exactly once for such an id, then False like any double close. A browser the caller closed is never recorded, an id never held is False, and the record is bounded (64, oldest out). Both orderings now answer `closed: True`; the tool layer's cleanup (`if success:` network data, hooks, clone release) runs on the discard path too, which is what a close is for.

## 5. Pins

Hermetic: `test_closing_a_browser_that_crashed_answers_closed_either_way_round` (close first; `get_instance` discarded first; `list_instances` discarded first; the second close and a never-held id are False) and `test_a_close_never_claims_a_browser_that_was_closed_not_crashed`. RED on the pre-fix source: the first fails (`assert False is True` on the discard-first ordering); the second passes before and after (control). The real-Chrome pin in `test_resilience.py` is now `closed is True` and deterministic, since both orderings agree. This is the deliberate SOFT-golden update: its old False was the quirk, the justification is section 3.

## 6. Residuals

* MQ-126 stays `planned`: promoting it means binding the node in the gate, which is a separate change; F-789 no longer blocks it.
* The macOS RED itself was not reproduced locally (Windows); the two orderings are pinned directly.
* The claim is in-process memory, like `_departed`; a backend restart forgets it (then False, as for any unknown id).
