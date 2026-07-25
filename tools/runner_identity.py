#!/usr/bin/env python3
"""Assert the CI runner matches the qualified release contract (plan_RELEASE W2).

The release gate's cells are exactly three GitHub-hosted runners: Ubuntu x64,
Windows x64, and macOS ARM64. A label or architecture migration (for example
``macos-latest`` silently moving to a different arch) must be a RED gate that
forces contract review — never an invisible change. This tool runs early in each
OS-matrix job, before any test work: it fails the job unless the reported
``runner.os``/``runner.arch`` are one of the contract pairs AND match the pair
the matrix cell declared, and it records the runner/image identity as a JSON
artifact for the evidence ledger.

Values that GitHub exposes as expression contexts (``runner.os``, ``runner.arch``,
``runner.name``) are passed as arguments; the image identity (``ImageOS`` /
``ImageVersion``) is only available as runner environment, read here through one
tagged accessor. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path

# plan_RELEASE §2.2 — the only qualified (runner.os, runner.arch) cells.
CONTRACT: frozenset[tuple[str, str]] = frozenset(
    {
        ("Linux", "X64"),
        ("Windows", "X64"),
        ("macOS", "ARM64"),
    }
)


def _runner_env(name: str) -> str:
    """Read a GitHub-Actions runner-image env var (``ImageOS``/``ImageVersion``).

    This is CI build tooling, not the application: settings.py's "env has one
    home" rule governs runtime code, not the release harness's runner probe.
    """
    return os.environ.get(name, "")  # noqa: TID251  PERMANENT(CI-only runner-image probe; not application runtime env access)


def build_identity(
    *,
    runner_os: str,
    runner_arch: str,
    runner_name: str,
    python_version: str,
) -> dict[str, str]:
    return {
        "runner_os": runner_os,
        "runner_arch": runner_arch,
        "runner_name": runner_name,
        "runner_label": f"{runner_os}/{runner_arch}",
        "image_os": _runner_env("ImageOS"),
        "image_version": _runner_env("ImageVersion"),
        "python_version": python_version,
    }


def evaluate(identity: dict[str, str], expect_os: str, expect_arch: str) -> list[str]:
    """Return a list of contract violations (empty == qualified)."""
    problems: list[str] = []
    pair = (identity["runner_os"], identity["runner_arch"])
    if pair not in CONTRACT:
        problems.append(
            f"runner {pair[0]}/{pair[1]} is not a qualified release cell "
            f"(allowed: {sorted(CONTRACT)})"
        )
    if expect_os and identity["runner_os"] != expect_os:
        problems.append(
            f"runner.os {identity['runner_os']!r} != declared cell {expect_os!r}"
        )
    if expect_arch and identity["runner_arch"] != expect_arch:
        problems.append(
            f"runner.arch {identity['runner_arch']!r} != declared cell {expect_arch!r}"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert the runner matches the qualified release contract."
    )
    parser.add_argument("--runner-os", required=True)
    parser.add_argument("--runner-arch", required=True)
    parser.add_argument("--runner-name", default="")
    parser.add_argument("--expect-os", default="")
    parser.add_argument("--expect-arch", default="")
    parser.add_argument("--python", default=platform.python_version())
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the JSON identity to this path (in addition to stdout)",
    )
    args = parser.parse_args(argv)

    identity = build_identity(
        runner_os=args.runner_os,
        runner_arch=args.runner_arch,
        runner_name=args.runner_name,
        python_version=args.python,
    )
    problems = evaluate(identity, args.expect_os, args.expect_arch)
    identity["contract_ok"] = "true" if not problems else "false"

    text = json.dumps(identity, indent=2, sort_keys=True)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")

    if problems:
        for problem in problems:
            print(f"::error::runner_identity: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
