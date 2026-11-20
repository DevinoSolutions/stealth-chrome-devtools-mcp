#!/usr/bin/env python3
"""Does the change under test cut a release? The one answer the gate's tier asks.

A pull request normally runs the PR tier (``release_schema.PR_TIER``: Linux, the
wheel smoke, one advisory Windows cell). A tag runs every cell, and a tag is never
moved or deleted, so a cell that is red only off-Linux used to burn a version
number. The fix is to run the tag's ledger BEFORE the tag: on the pull request
that cuts the release.

"Cuts a release" is decided by the one fact that makes a release a release: the
``[project] version`` in ``pyproject.toml`` differs between the pull request's
base and the merge result the gate checks out. Not the title, not the branch name
and not the commit subject: a person can forget those, and nothing forgets that the
version moved. The two values are parsed, not diffed as text, so reformatting the
file or touching another table never raises the tier, and a version bump always
does.

Only a pull request's checkout (the merge commit GitHub builds from the head and
the base's tip, so its FIRST parent is the base) can be asked. The answer fails
CLOSED: anything this module cannot establish (a checkout that is not a merge
commit, a revision git cannot show, a file that does not parse) is "yes, this cuts
a release", which costs runner minutes and never a missed cell. Stdlib only.
"""

from __future__ import annotations

import subprocess
import tomllib
from collections.abc import Callable

PYPROJECT = "pyproject.toml"
_MERGE_PARENTS = 2  # a merge commit: the base, then the pull request's head

#: ``git <args>`` -> ``(returncode, stdout)``. The seam tests replace; production
#: shells out to the real ``git``.
GitRunner = Callable[[list[str]], tuple[int, str]]


def _git(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603  PERMANENT(fixed program; the arguments are revisions this module builds, no shell)
        ["git", *args],  # noqa: S607  PERMANENT(git is resolved from PATH exactly as the workflow steps resolve it)
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return proc.returncode, proc.stdout


def project_version(text: str) -> str | None:
    """``[project] version`` of a ``pyproject.toml`` body, or None if absent/bad."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    return version if isinstance(version, str) and version else None


def _version_at(rev: str, git: GitRunner) -> str | None:
    code, out = git(["show", f"{rev}:{PYPROJECT}"])
    return project_version(out) if code == 0 else None


def cuts_release(head: str = "HEAD", *, git: GitRunner = _git) -> tuple[bool, str]:
    """``(changed, reason)``: does merge commit ``head`` move the project version?

    ``reason`` is one line for the run log, because a tier that silently changed
    is a gate nobody can audit.
    """
    code, out = git(["rev-list", "--parents", "-n", "1", head])
    parents = out.split()[1:] if code == 0 else []
    if len(parents) != _MERGE_PARENTS:
        return True, (
            f"{head} is not a merge commit (parents: {len(parents)}), so the "
            f"base cannot be named; failing closed to the full tier"
        )
    base = parents[0]
    before = _version_at(base, git)
    after = _version_at(head, git)
    if before is None or after is None:
        return True, (
            f"{PYPROJECT} has no readable [project] version at "
            f"{'the base' if before is None else head}; failing closed to the full tier"
        )
    if before != after:
        return True, f"project.version moves {before} -> {after}: a release"
    return False, f"project.version is {after} on both sides: not a release"
