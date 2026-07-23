#!/usr/bin/env python3
"""Gate script: every ruff suppression must carry an owner tag.

Owner-tag grammar (from the 2.5-gates spec):
    plan_M<id>               -- an approved Stage-3 plan owns this code
    RELEASE-FIX-<tier>       -- an approved pre-release fix plan owns this code
                                (e.g. RELEASE-FIX-A; a non-M-series FIX plan)
    PERMANENT(<reason>)      -- by-design; will never be "fixed"
    FALSE-POSITIVE(<reason>) -- ruff is wrong here
    DEBT(F-<id>)             -- known debt tracked in audit/findings.json

Covers BOTH inline suppression comments (scanned across src/, tests/, tools/)
and ``[tool.ruff.lint.per-file-ignores]`` entries in pyproject.toml, including
multi-line array entries whose tag sits on the closing-bracket line.

Exit 0 if all suppressions are tagged; exit 1 and print violations otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

OWNER_RE = re.compile(
    r"(plan_M\w+|RELEASE-FIX-[A-Z]\w*|PERMANENT\(.+?\)|FALSE-POSITIVE\(.+?\)"
    r"|DEBT\(F-\d+\))"
)

# An inline ruff suppression marker (the "n-o-q-a" comment), with or without
# a trailing ``: CODES`` list. Written as a pattern so this very line does not
# read as a literal suppression when the scanner walks tools/.
_SUPPRESS_RE = re.compile(r"#\s*noqa\b", re.IGNORECASE)

# A quoted per-file-ignore key line, e.g.  "tests/**" = [
_ENTRY_KEY_RE = re.compile(r"""^\s*["'][^"']+["']\s*=""")

# A TOML table header, e.g.  [tool.ty.src]
_TABLE_HEADER_RE = re.compile(r"^\[[^\]]")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("src", "tests", "tools")
_PFI_SECTION = "[tool.ruff.lint.per-file-ignores]"


def inline_noqa_violations(text: str, rel: str) -> list[str]:
    """Report inline suppression lines in *text* that lack an owner tag."""
    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _SUPPRESS_RE.search(line) and not OWNER_RE.search(line):
            out.append(f"{rel}:{lineno}: inline suppression without owner tag")
    return out


def check_inline_noqas() -> list[str]:
    violations: list[str] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for py in sorted(base.rglob("*.py")):
            rel = py.relative_to(REPO_ROOT).as_posix()
            violations.extend(
                inline_noqa_violations(py.read_text(encoding="utf-8"), rel)
            )
    return violations


def per_file_ignore_violations(text: str) -> list[str]:
    """Report per-file-ignore entries that lack an owner tag.

    Handles single-line entries (tag on the key line) and multi-line array
    entries (tag on the closing ``]`` line). The original implementation only
    inspected lines that both contained ``=`` and ended in ``]`` -- which no
    real entry does -- so it validated zero entries (audit finding G-1).
    """
    out: list[str] = []
    lines = text.splitlines()
    in_section = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not in_section:
            if stripped == _PFI_SECTION:
                in_section = True
            i += 1
            continue
        if _TABLE_HEADER_RE.match(line):
            break  # a new table header ends the section
        if _ENTRY_KEY_RE.match(line):
            key = stripped.split("=", 1)[0].strip()
            after_eq = line.split("=", 1)[1]
            tag_line, tag_lineno = line, i + 1
            if "[" in after_eq and "]" not in after_eq:
                # multi-line array: the tag belongs on the closing-bracket line
                j = i + 1
                while j < len(lines) and not lines[j].lstrip().startswith("]"):
                    j += 1
                if j < len(lines):
                    tag_line, tag_lineno = lines[j], j + 1
                i = j
            comment = tag_line.split("#", 1)[1] if "#" in tag_line else ""
            if not OWNER_RE.search(comment):
                out.append(
                    f"pyproject.toml:{tag_lineno}: per-file-ignore "
                    f"{key} without owner tag"
                )
        i += 1
    return out


def check_pyproject_per_file_ignores() -> list[str]:
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return []
    return per_file_ignore_violations(pyproject.read_text(encoding="utf-8"))


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
