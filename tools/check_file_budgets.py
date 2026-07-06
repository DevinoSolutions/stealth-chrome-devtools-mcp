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
    "embedded/server.py": (4420, "plan_M4ph1"),
    "embedded/browser_manager.py": (1447, "DEBT(F-702)"),
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
