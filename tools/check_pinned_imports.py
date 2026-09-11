#!/usr/bin/env python3
"""Gate script: every third-party module the source imports is pinned to the
version ``uv.lock`` carries (F-865).

Why a lockfile is not enough. ``uv.lock`` is a UNIVERSAL resolution: one
version per package that satisfies every Python the project supports, so a
transitive dependency that needs a newer pin on a newer Python (mcp >= 1.28
wants pydantic >= 2.12 on 3.14) is held back for ALL Pythons in the lock. A
user's installer resolves the wheel's metadata again for ONE Python and takes
the newest version that fits: on 2026-09-11 the gate ran mcp 1.27.1 on every
cell while ``uv tool install stealth-chrome-devtools-mcp==2.1.2`` on 3.12 got
mcp 1.30.0. The lock therefore describes what the gate tested, not what ships,
unless the wheel's metadata pins it.

The rule. For every distribution a module under ``src/`` imports (stdlib and
first-party excluded), ``[project.dependencies]`` must carry an EXACT ``==`` pin
and that pin must equal the version in ``uv.lock``. Distributions that belong
to an ``[project.optional-dependencies]`` extra are the declared exceptions
(``py2js``). The direct dependencies were already exact pins; this check makes
the imported TRANSITIVE ones (mcp, anyio, starlette, httpx) pins too, and keeps
the two files from drifting apart afterwards.

Exit 0 if every import is pinned at the locked version; exit 1 and print the
violations otherwise.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "stealth_chrome_devtools_mcp"
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCK = REPO_ROOT / "uv.lock"

# PEP 508 requirement head: name, optional [extras], then the specifier.
_REQUIREMENT_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$"
)
_EXACT_PIN_RE = re.compile(r"^==\s*([A-Za-z0-9._+!*-]+)\s*$")
# How many importing files a violation names before eliding the rest.
_SHOWN_FILES = 3


def normalize(name: str) -> str:
    """PEP 503 normalization: the one spelling a distribution name has."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pyproject_text() -> str:
    return PYPROJECT.read_text(encoding="utf-8")


def lock_text() -> str:
    return LOCK.read_text(encoding="utf-8")


def third_party_imports(src_root: Path) -> dict[str, list[str]]:
    """Top-level third-party module -> the source files that import it."""
    package = src_root.name
    stdlib = set(sys.stdlib_module_names)
    found: dict[str, set[str]] = {}
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for dotted in names:
                top = dotted.split(".", 1)[0]
                if top in stdlib or top == package:
                    continue
                rel = path.relative_to(src_root).as_posix()
                found.setdefault(top, set()).add(rel)
    return {module: sorted(files) for module, files in sorted(found.items())}


def distribution_for(module: str) -> str:
    """The normalized distribution that provides *module* (its own name when
    the environment cannot say, e.g. an optional extra that is not installed)."""
    provided = packages_distributions().get(module)
    return normalize(provided[0]) if provided else normalize(module)


def _requirement_parts(spec: str) -> tuple[str, str]:
    match = _REQUIREMENT_RE.match(spec)
    if match is None:
        return normalize(spec), ""
    return normalize(match.group(1)), match.group(3).strip()


def exact_pins(pyproject: str) -> dict[str, str | None]:
    """Runtime dependency -> its exact pinned version, or None for a range."""
    project = tomllib.loads(pyproject)["project"]
    pins: dict[str, str | None] = {}
    for spec in project.get("dependencies", []):
        name, tail = _requirement_parts(spec)
        exact = _EXACT_PIN_RE.match(tail)
        pins[name] = exact.group(1) if exact else None
    return pins


def optional_distributions(pyproject: str) -> set[str]:
    project = tomllib.loads(pyproject)["project"]
    extras = project.get("optional-dependencies", {})
    return {_requirement_parts(spec)[0] for specs in extras.values() for spec in specs}


def locked_versions(lock: str) -> dict[str, str]:
    return {
        normalize(entry["name"]): str(entry["version"])
        for entry in tomllib.loads(lock).get("package", [])
        if "version" in entry
    }


def violations(src_root: Path, pyproject: str, lock: str) -> list[str]:
    pins = exact_pins(pyproject)
    optional = optional_distributions(pyproject)
    locked = locked_versions(lock)
    out: list[str] = []
    for module, files in third_party_imports(src_root).items():
        dist = distribution_for(module)
        if dist in optional:
            continue
        shown, elided = files[:_SHOWN_FILES], len(files) > _SHOWN_FILES
        where = ", ".join(shown) + (", ..." if elided else "")
        if dist not in pins or pins[dist] is None:
            out.append(
                f"{module} (distribution {dist!r}), imported by {where}, is not "
                "pinned '==<version>' in [project.dependencies]"
            )
            continue
        pinned = pins[dist]
        if dist in locked and locked[dist] != pinned:
            out.append(
                f"{dist} is pinned =={pinned} in pyproject.toml but uv.lock has "
                f"{locked[dist]}: re-run `uv lock` or move the pin"
            )
    return out


def main() -> int:
    problems = violations(SRC_ROOT, pyproject_text(), lock_text())
    for line in problems:
        print(line)
    if problems:
        print(
            f"\n{len(problems)} floating import(s): the gate tests uv.lock, users "
            "install the wheel's metadata; pin what the source imports (F-865)."
        )
        return 1
    print(
        "check_pinned_imports: every third-party import is pinned at the locked version"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
