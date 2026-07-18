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
    # plan_M4ph1 C1 (F-201): extracted the 50-def clone-storage subsystem into
    # clone_storage.py, shrinking server.py from its 4425 grandfathered cap to
    # its actual 3389 LOC (measured after ruff format). Ratcheted DOWN per the
    # no-grow discipline; the prior M3/M10a except-surface bumps are folded into
    # this post-extraction baseline. Owner string unchanged.
    "embedded/server.py": (3389, "plan_M4ph1 + plan_M3 + plan_M10a"),
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
    # + 27 (plan_M7 step M7-2: _fallback_pid_identity_ok shared predicate +
    # non-recovery fallback identity check + recovery branch refactored).
    "embedded/process_cleanup.py": (1054, "plan_M11a_M15 + plan_M7"),
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
    # assets/related_files=JS-eval per the 2026-07-18 ruling). M5b-1 is ADDITIVE
    # (nothing deleted yet), so this is the transitional PEAK — element_cloner.py
    # (833) + comprehensive_element_cloner.py (400) are deleted in M5b-3/M5b-5,
    # and this engine's own now-superseded nested extract_complete_element_cdp +
    # _get_* helpers ratchet the cap DOWN then. GRANDFATHERED at actual ruff-clean
    # LOC (cap == actual, no padding) per the C1/C4 gate-ruling discipline.
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
