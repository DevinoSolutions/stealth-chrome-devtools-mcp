#!/usr/bin/env python3
"""Gate script: every ruff noqa / per-file-ignore must carry an owner tag.

Owner-tag grammar (from the 2.5-gates spec):
    plan_M<id>               -- an approved Stage-3 plan owns this code
    PERMANENT(<reason>)      -- by-design; will never be "fixed"
    FALSE-POSITIVE(<reason>) -- ruff is wrong here
    DEBT(F-<id>)             -- known debt tracked in audit/findings.json

Exit 0 if all suppressions are tagged; exit 1 and print violations otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

OWNER_RE = re.compile(
    r"(plan_M\w+|PERMANENT\(.+?\)|FALSE-POSITIVE\(.+?\)|DEBT\(F-\d+\))"
)

NOQA_RE = re.compile(r"#\s*noqa\s*:", re.IGNORECASE)

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


def check_inline_noqas() -> list[str]:
    violations: list[str] = []
    for py in sorted(SRC_ROOT.rglob("*.py")):
        for lineno, line in enumerate(
            py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if NOQA_RE.search(line) and not OWNER_RE.search(line):
                rel = py.relative_to(SRC_ROOT.parent)
                violations.append(f"{rel}:{lineno}: bare noqa without owner tag")
    return violations


def check_pyproject_per_file_ignores() -> list[str]:
    pyproject = SRC_ROOT.parent / "pyproject.toml"
    if not pyproject.exists():
        return []
    violations: list[str] = []
    in_section = False
    for lineno, line in enumerate(
        pyproject.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if stripped == "[tool.ruff.lint.per-file-ignores]":
            in_section = True
            continue
        if in_section and stripped.startswith("["):
            break
        if in_section and "=" in line and not stripped.startswith("#"):
            if not stripped.endswith("]"):
                continue
            comment = line.split("#", 1)[1] if "#" in line else ""
            if not OWNER_RE.search(comment):
                violations.append(
                    f"pyproject.toml:{lineno}: per-file-ignore without owner tag"
                )
    return violations


def main() -> int:
    violations = check_inline_noqas() + check_pyproject_per_file_ignores()
    if violations:
        print(f"Found {len(violations)} untagged suppression(s):")
        for v in violations:
            print(f"  {v}")
        return 1
    print("All suppressions have owner tags.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
