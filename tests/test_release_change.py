"""A pull request that cuts a release owes the full ledger (tools/release_change.py).

Four tags in a row (v2.1.63 to v2.1.66) failed their publish gate on a cell the PR
gate never ran. The release PR now runs the tag's ledger, decided by the one fact
that makes a release a release: the project version moved. These tests pin the
decision (against a real merge commit in a throwaway repository, because the
plumbing is `git rev-list --parents` and `git show <rev>:pyproject.toml`), the
fail-closed edges, and that the workflow asks the question in BOTH places that
derive the tier, so the cells a run executes and the cells it owes cannot differ.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import release_change  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)
import release_evidence  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)

RELEASE_GATE = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "release-gate.yml"
)

PYPROJECT = '[project]\nname = "x"\nversion = "{v}"\n\n[tool.other]\nk = 1\n'


# ── parsing ────────────────────────────────────────────────────────────────
def test_project_version_reads_the_project_table_only():
    assert release_change.project_version(PYPROJECT.format(v="2.1.67")) == "2.1.67"
    # A `version` in another table is not the package version.
    assert release_change.project_version('[tool.x]\nversion = "9"\n') is None
    assert release_change.project_version("[project]\nname = 'x'\n") is None
    assert release_change.project_version("[project\nversion = 1") is None
    assert release_change.project_version('[project]\nversion = ""\n') is None
    assert release_change.project_version("[project]\nversion = 3\n") is None


# ── the decision, against a real merge commit ──────────────────────────────
def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    done = subprocess.run(  # noqa: S603  PERMANENT(test helper; fixed program, arguments written in this file)
        ["git", *args],  # noqa: S607  PERMANENT(git is resolved from PATH, as in the workflow) cwd=repo, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout


def _write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8", newline="\n")


def _merged_pr(repo: Path, edit, main_edit=None) -> None:
    """main@v1 + a branch that applies ``edit`` + a base commit, merged --no-ff.

    ``main_edit`` is what main does AFTER the branch point (default: unrelated).
    """
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, "pyproject.toml", PYPROJECT.format(v="1.0.0"))
    _write(repo, "other.txt", "a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    edit(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the pr")
    _git(repo, "checkout", "-q", "main")
    if main_edit is None:
        _write(repo, "later.txt", "main moved on\n")  # the base advances meanwhile
    else:
        main_edit(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main moved")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge ref", "pr")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_version_bump_is_a_release(repo: Path):
    _merged_pr(repo, lambda r: _write(r, "pyproject.toml", PYPROJECT.format(v="1.0.1")))
    changed, why = release_change.cuts_release()
    assert changed is True
    assert "1.0.0 -> 1.0.1" in why


def test_an_ordinary_change_is_not_a_release(repo: Path):
    _merged_pr(repo, lambda r: _write(r, "other.txt", "b\n"))
    changed, why = release_change.cuts_release()
    assert changed is False
    assert "not a release" in why


def test_reformatting_pyproject_without_moving_the_version_is_not_a_release(
    repo: Path,
):
    """Parsed values are compared, not text: a comment or another table is inert."""
    edited = PYPROJECT.format(v="1.0.0") + "# a comment\n[tool.third]\nz = 2\n"
    _merged_pr(repo, lambda r: _write(r, "pyproject.toml", edited))
    assert release_change.cuts_release()[0] is False


def test_the_base_is_the_merge_commits_first_parent_not_the_branch_point(repo: Path):
    """Main bumped the version after the PR branched; the PR did not touch it.

    The merge result differs from the branch point but not from its own first
    parent (the tip GitHub merged into), so this PR is not the release.
    """
    _merged_pr(
        repo,
        lambda r: _write(r, "other.txt", "b\n"),
        main_edit=lambda r: _write(r, "pyproject.toml", PYPROJECT.format(v="1.0.1")),
    )
    assert release_change.cuts_release()[0] is False


def test_a_checkout_that_is_not_a_merge_commit_fails_closed(repo: Path):
    _merged_pr(repo, lambda r: _write(r, "other.txt", "b\n"))
    changed, why = release_change.cuts_release("pr")  # a plain one-parent commit
    assert changed is True
    assert "not a merge commit" in why


# ── the fail-closed edges, with the git seam faked ─────────────────────────
def _fake_git(parents: str, files: dict[str, str]):
    def run(args: list[str]) -> tuple[int, str]:
        if args[:2] == ["rev-list", "--parents"]:
            return 0, parents
        if args[0] == "show":
            rev = args[1].split(":")[0]
            return (0, files[rev]) if rev in files else (128, "")
        raise AssertionError(args)

    return run


def test_an_unreadable_side_fails_closed():
    ok = PYPROJECT.format(v="1")
    for files in ({"HEAD": ok}, {"b": ok}, {"b": "[bad", "HEAD": ok}):
        changed, why = release_change.cuts_release(git=_fake_git("m b p", files))
        assert changed is True, files
        assert "failing closed" in why
    assert release_change.cuts_release(
        git=_fake_git("m b p", {"b": ok, "HEAD": ok})
    ) == (False, "project.version is 1 on both sides: not a release")


def test_git_failing_to_list_the_parents_fails_closed():
    assert release_change.cuts_release(git=lambda _a: (128, ""))[0] is True


# ── the tier ───────────────────────────────────────────────────────────────
def test_a_release_change_can_only_raise_a_tier():
    pr, full = release_evidence.PR_TIER, release_evidence.FULL_TIER
    assert release_evidence.tier_for_event("pull_request") == pr
    assert release_evidence.tier_for_event("pull_request", False) == pr
    assert release_evidence.tier_for_event("pull_request", True) == full
    for event in ("push", "workflow_dispatch", "release", "", "x"):
        for flag in (False, True):
            assert release_evidence.tier_for_event(event, flag) == full, event


def _plan(monkeypatch, capsys, argv: list[str], changed: bool) -> tuple[str, str]:
    monkeypatch.setattr(release_evidence, "cuts_release", lambda: (changed, "because"))
    assert release_evidence.main(["plan", *argv]) == 0
    out = capsys.readouterr()
    return out.out, out.err


def test_the_plan_of_a_release_pr_is_the_tags_plan_and_an_ordinary_pr_is_not(
    monkeypatch, capsys
):
    tag, _ = _plan(monkeypatch, capsys, ["--event", "push"], False)
    release_pr, why = _plan(
        monkeypatch,
        capsys,
        ["--event", "pull_request", "--detect-release-change"],
        True,
    )
    ordinary, _ = _plan(
        monkeypatch,
        capsys,
        ["--event", "pull_request", "--detect-release-change"],
        False,
    )
    unasked, _ = _plan(monkeypatch, capsys, ["--event", "pull_request"], True)
    assert release_pr == tag and "tier=full" in release_pr
    assert "because" in why, "the reason reaches the log (stderr), not GITHUB_OUTPUT"
    assert ordinary == unasked and "tier=pr" in ordinary
    assert ordinary != release_pr


def test_the_detector_is_not_asked_about_events_that_are_already_full(
    monkeypatch, capsys
):
    def boom():
        raise AssertionError("a tag checkout has no merge base to ask about")

    monkeypatch.setattr(release_evidence, "cuts_release", boom)
    assert (
        release_evidence.main(["plan", "--event", "push", "--detect-release-change"])
        == 0
    )
    assert "tier=full" in capsys.readouterr().out


def test_a_release_pr_aggregate_owes_every_cell(tmp_path: Path):
    spec = {
        "root": tmp_path,
        "release_sha": "a" * 40,
        "workflow_name": "release-gate",
        "run_id": "1",
        "run_attempt": "1",
        "event": "pull_request",
        "claims_path": release_evidence.CLAIMS_PATH,
    }
    (tmp_path / spec["release_sha"]).mkdir()
    ordinary, _ = release_evidence.build_aggregate(
        release_evidence.AggregateSpec(**spec)
    )
    release, _ = release_evidence.build_aggregate(
        release_evidence.AggregateSpec(**spec, release_change=True)
    )
    assert ordinary["tier"] == "pr"
    assert release["tier"] == "full"
    assert release["required_cells"] == list(release_evidence.REQUIRED_KEYS)


# ── the workflow asks the question in both places ──────────────────────────
def _steps(job: str) -> list[dict]:
    return yaml.safe_load(RELEASE_GATE.read_text(encoding="utf-8"))["jobs"][job][
        "steps"
    ]


@pytest.mark.parametrize(
    ("job", "command"),
    [("plan", "release_evidence.py plan"), ("release-evidence", "aggregate")],
)
def test_plan_and_aggregate_both_detect_a_release_change(job: str, command: str):
    """If only one asked, a release PR would run cells its ledger did not owe (or
    owe cells it did not run): the aggregate would be red or credit too little."""
    steps = _steps(job)
    runs = [s["run"] for s in steps if command in s.get("run", "")]
    assert len(runs) == 1, runs
    assert "--detect-release-change" in runs[0]
    # The merge commit's parents must exist in the checkout.
    assert steps[0]["with"]["fetch-depth"] == 2
