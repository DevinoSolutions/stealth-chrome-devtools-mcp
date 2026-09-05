#!/usr/bin/env python3
"""Gate script: no src/**/*.py file may exceed 1000 LOC unless grandfathered.

Grandfathered files may never GROW beyond their recorded LOC.
Exit 0 if all files are within budget; exit 1 and print violations otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
LOC_BUDGET = 1000

GRANDFATHER: dict[str, tuple[int, str]] = {
    # plan_SERVERSPLIT slice 12 (closing) DELETES the ``embedded/server.py``
    # row that stood here through plan_M4ph1 / M3 / M10a / F808 / F809 and the
    # twelve ratchets of the split itself (4425 -> 3411 -> 524). The file is
    # governed by the 1000-LOC default now, and the row is removed rather than
    # merely satisfied: a grandfathered cap is a standing permission to be over
    # budget, and leaving one on a 523-line file would say this file is allowed
    # 523 lines when what is true is that it is allowed 1000 like every other.
    # Slice 10 already took it under the default (986); slices 11 and 12 are why
    # deleting it is honest rather than lucky. Do NOT re-add a row for a file
    # that fits the default (plan_SERVERSPLIT §6.2).
    #
    # plan_M4ph1 C1 (F-201): the verbatim 50-def clone-storage move is an
    # irreducibly ~1024-line contiguous block, landing this module over the
    # 1000-LOC budget. GRANDFATHERED at its actual post-ruff-format LOC per the
    # human gate ruling 2026-07-12 (cap == actual, no padding; the two-module
    # split and the partial move were both explicitly declined). No-grow applies.
    "embedded/clone_storage.py": (1057, "plan_M4ph1"),
    # 1447 (DEBT(F-702)) + 2 (plan_M10a step 7a: switch_to_tab/close_tab's two
    # truly-silent `except Exception: return False` handlers now each add one
    # debug_logger.log_warning(...) line closing F-181 rows 1-2; same minimal-
    # bump rationale as server.py above, cross-review-confirmed there).
    # + 3 (plan_M7 step M7-1: close_instance restructured into 4 phases with
    # _blocking_teardown extracted + _close_proxy_forwarder_ref helper).
    # + 80 (plan_M4ph1 C4 / M13 / F-208: spawn_browser's ~230-line god-method
    # extracted IN PLACE into 5 testable sub-methods (_build_instance,
    # _resolve_proxy, _resolve_launch_args, _launch_browser, _apply_post_launch)
    # plus the orchestrator. This is method-boundary overhead (signatures,
    # returns, ruff-mandated separators), NOT logic growth: the exact
    # modularity/testability gain the budget gate exists to encourage, so it
    # cannot fit the prior no-grow headroom. Unlike C1's server.py ratchet-DOWN
    # (code physically left the file), this is an in-file split with no offset,
    # so it is a net increase. GRANDFATHERED at the actual ruff-clean LOC (cap
    # == actual, no padding) per the human gate ruling 2026-07-17. No-grow
    # applies from this commit forward.
    "embedded/browser_manager.py": (
        1532,
        "DEBT(F-702) + plan_M10a + plan_M7 + plan_M4ph1",
    ),
    # plan_F808 Task 10 (F-808 fratricide), in two ratchets against one file:
    # 1054 -> 966 (step 10a) when the browser_pids.json schema, its lock and its
    # read-merge-write protocol moved out to the new browser_pid_registry.py
    # leaf, which is the record's one home; this file keeps the reaping policy
    # and passes the path in. 966 -> 1017 (step 10b) for the owner identity that
    # fix needs: _owner_identity, the _owner_backend_alive adapter composing the
    # two EXISTING predicates, the one _rewrite_record write path, and the
    # recovery gate that spares a live owner's browsers. The move was sequenced
    # first precisely because 1054/1054 left zero headroom for any of it. Then
    # 1017 -> 1023 (step 10c) to thread `force` from `kill-orphans --force`
    # through to the reaper, which that gate would otherwise have turned into a
    # no-op against the very wedged backend the flag exists for. Net -31 from
    # the pre-task cap; every number is the actual post-ruff-format LOC
    # (cap == actual, no padding), per the C1 discipline. Cap raise 966->1023
    # RATIFIED per the human gate ruling 2026-08-02 (PR #57 merge). No-grow
    # applies from here.
    # plan_F809 / F-809 spends the whole remaining balance and not a line more:
    # the signal hand-off paid its own way (-3, the two boilerplate
    # Args:/Returns: docstring blocks the spec's §4 payment plan named), and the
    # re-install guard spends +3 (one loop line, two docstring lines for why a
    # second install must not record our own handler). 1023 stays the actual
    # post-ruff-format LOC, so this is a ratchet to actual, NOT a raise.
    # plan_F856 RATCHETS DOWN 1023 -> 1017. `activate()` grew +6 (the reap moved
    # to serve_startup.after_serving, and the WHY of an ordering change has to
    # sit with it); that was paid for twice over by collapsing two boilerplate
    # Args:/Returns: blocks that only restated their own signatures
    # (_extract_profile_dir_from_cmdline, untrack_browser_process) — the same
    # payment mechanism the plan_F809 row above already records. Cap == actual.
    "embedded/process_cleanup.py": (
        1017,
        "plan_M11a_M15 + plan_M7 + plan_F808 + plan_F809 + plan_F856",
    ),
    # 1004 (pre-M7) + 7 (plan_M7 step M7-4: best-effort terminate_execution
    # + honest message + debug_logger.log_info on failure) + 1 (plan_M4ph1
    # STEP 0: isort emits a first-party group-separator blank line once
    # debug_logger's import is the absolute
    # stealth_chrome_devtools_mcp.embedded.debug_logger form).
    "embedded/cdp_function_executor.py": (1012, "plan_M7 + plan_M4ph1"),
    # plan_M5b-1 (F-140/F-203/F-601 5->1 cloner consolidation): CDPElementCloner
    # is the canonical extraction engine the five cloner modules converge onto,
    # so it absorbs the six per-aspect methods + the composing extract_complete_
    # element + node-id/JS helpers (styles=CDP; structure/events/animations/
    # assets/related_files=JS-eval per the 2026-07-18 ruling). element_cloner.py
    # (833) + comprehensive_element_cloner.py (400) are now deleted (M5b-4a/M5b-5);
    # the engine retains the CDP-native extract_complete_element_cdp + _get_*
    # helpers (they back the live extract_complete_element_cdp tool, not dead), so
    # the cap holds at 1013. GRANDFATHERED at actual ruff-clean LOC (cap == actual,
    # no padding) per the C1/C4 gate-ruling discipline.
    "embedded/cdp_element_cloner.py": (1013, "plan_M5b"),
}


def main() -> int:
    pkg = SRC_ROOT / "stealth_chrome_devtools_mcp"
    violations: list[str] = []

    for py in sorted(pkg.rglob("*.py")):
        rel = str(py.relative_to(pkg)).replace("\\", "/")
        loc = len(py.read_text(encoding="utf-8").splitlines())

        if rel in GRANDFATHER:
            cap, owner = GRANDFATHER[rel]
            if loc > cap:
                violations.append(f"{rel}: {loc} LOC > grandfathered {cap} ({owner})")
        elif loc > LOC_BUDGET:
            violations.append(
                f"{rel}: {loc} LOC > budget {LOC_BUDGET} (not grandfathered)"
            )

    if violations:
        print(f"File budget violations ({len(violations)}):")
        for v in violations:
            print(f"  {v}")
        return 1

    print(f"All files within {LOC_BUDGET}-LOC budget.")
    print("Grandfathered files (may not grow):")
    for rel, (cap, owner) in sorted(GRANDFATHER.items()):
        fp = pkg / rel
        if fp.exists():
            loc = len(fp.read_text(encoding="utf-8").splitlines())
            print(f"  {rel}: {loc}/{cap} LOC ({owner})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
