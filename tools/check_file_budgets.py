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
    # 4420 (plan_M4ph1) + 2 (plan_M3 step 2: single bootstrap_backend_process_
    # logging() import + call in __main__ — the M3/M10a except-surface work
    # plan_M4ph1's own tag already anticipated) + 3 (plan_M10a step 7d: the
    # final 3 of the 17 truly-silent excepts - _install_nodriver_cookie_compat,
    # _client_session_seed, apply_disabled_sections - each add one
    # debug_logger.log_warning/log_debug(...) line closing F-181 rows 15-17;
    # same minimal-bump rationale as browser_manager.py below.
    "embedded/server.py": (4425, "plan_M4ph1 + plan_M3 + plan_M10a"),
    # 1447 (DEBT(F-702)) + 2 (plan_M10a step 7a: switch_to_tab/close_tab's two
    # truly-silent `except Exception: return False` handlers now each add one
    # debug_logger.log_warning(...) line closing F-181 rows 1-2; same minimal-
    # bump rationale as server.py above, cross-review-confirmed there).
    # + 3 (plan_M7 step M7-1: close_instance restructured into 4 phases with
    # _blocking_teardown extracted + _close_proxy_forwarder_ref helper).
    "embedded/browser_manager.py": (1452, "DEBT(F-702) + plan_M10a + plan_M7"),
    "embedded/process_cleanup.py": (1022, "plan_M11a"),
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
