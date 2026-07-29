"""Structural pins for the release topology (plan_RELEASE W3, gap G-C).

The W3 claims that are easiest to lose are the ones nothing executes on a normal
PR: *publish never rebuilds*, *the aggregate never forgets an edge*, and *the
macOS smoke cells are partial on purpose*. A tag build happens rarely and cannot
be dry-run here, so those properties are pinned by reading the workflow YAML
instead of by hoping a reviewer notices.

These are deliberately structural, not stylistic: they assert the shape the plan
requires and say why, so a future workstream that adds a job (W5's
``release-evidence``) is told to wire its edge rather than discovering months
later that the required check was green while a lane never ran.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
RELEASE_GATE = WORKFLOWS / "release-gate.yml"
PUBLISH = WORKFLOWS / "publish.yml"
TEST_CALLER = WORKFLOWS / "test.yml"

AGGREGATE = "release-gate"
BUILD_COMMANDS = ("uv build", "python -m build", "hatchling build", "pip wheel")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _jobs(path: Path) -> dict:
    return _load(path)["jobs"]


def _all_run_steps(job: dict) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


@pytest.fixture(scope="module")
def gate_jobs() -> dict:
    return _jobs(RELEASE_GATE)


# ---------------------------------------------------------------------------
# Build once.
# ---------------------------------------------------------------------------
def test_exactly_one_job_builds_the_distribution(gate_jobs):
    """`uv build` may appear in build-dist and nowhere else in the gate."""
    builders = {
        name
        for name, job in gate_jobs.items()
        if any(cmd in run for run in _all_run_steps(job) for cmd in BUILD_COMMANDS)
    }
    assert builders == {"build-dist"}, (
        f"the distribution must be built exactly once, in build-dist; "
        f"these jobs build: {sorted(builders)}"
    )


def test_publish_never_builds_and_never_downloads_from_pypi():
    """The publish job uploads the gated files; it does not make new ones."""
    for name, job in _jobs(PUBLISH).items():
        for run in _all_run_steps(job):
            for cmd in BUILD_COMMANDS:
                assert cmd not in run, (
                    f"publish.yml job {name!r} runs {cmd!r} — publishing must "
                    f"upload the artifact the gate tested, never a rebuild"
                )
            assert "pip install stealth-chrome-devtools-mcp" not in run, (
                f"publish.yml job {name!r} re-downloads the package from PyPI"
            )


def test_publish_calls_the_reusable_gate_rather_than_reimplementing_it():
    jobs = _jobs(PUBLISH)
    assert "gate" in jobs, "publish.yml must qualify the tag through the gate"
    assert jobs["gate"]["uses"].endswith("release-gate.yml"), (
        "publish.yml must CALL the one reusable gate, not duplicate job semantics"
    )
    # The tag SHA and the tag itself are both handed to the gate, so the gate
    # tests the commit it will publish and fails on a tag/version disagreement.
    assert set(jobs["gate"]["with"]) == {"ref", "release_tag"}


def test_publish_requires_the_green_gate_and_downloads_that_runs_artifact():
    publish = _jobs(PUBLISH)["publish"]
    needs = publish["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "gate" in needs, (
        "publish must need the gate: a failed, skipped, or cancelled cell has to "
        "prevent PyPI and the GitHub release"
    )
    downloads = [
        step
        for step in publish["steps"]
        if "download-artifact" in str(step.get("uses", ""))
    ]
    assert len(downloads) == 1, "publish must download the one gated dist artifact"
    assert downloads[0]["with"]["name"] == "dist"
    # …and re-check it before uploading anything.
    assert any("package_verify.py verify" in run for run in _all_run_steps(publish)), (
        "publish must re-verify the downloaded artifact before uploading it"
    )


# ---------------------------------------------------------------------------
# The aggregate never forgets an edge.
# ---------------------------------------------------------------------------
def test_aggregate_directly_needs_every_other_job(gate_jobs):
    """The one public required check must depend on every lane in the file.

    A lane that exists but is not in `needs` is invisible to the required check:
    it can fail while `release-gate` reports green. Adding a job to this
    workflow therefore MUST add its edge here (W5's `release-evidence` next).
    """
    expected = set(gate_jobs) - {AGGREGATE}
    actual = set(gate_jobs[AGGREGATE]["needs"])
    assert actual == expected, (
        f"release-gate is missing edges for {sorted(expected - actual)} and has "
        f"stale edges for {sorted(actual - expected)}"
    )


def test_aggregate_checks_the_result_of_every_edge(gate_jobs):
    """Listing a job in `needs` is not enough — its result must be asserted."""
    script = "\n".join(_all_run_steps(gate_jobs[AGGREGATE]))
    for edge in gate_jobs[AGGREGATE]["needs"]:
        assert f"needs.{edge}.result" in script, (
            f"the aggregate lists {edge!r} in needs but never checks its result"
        )


def test_aggregate_runs_even_when_a_dependency_fails(gate_jobs):
    """Without always(), a failed dependency SKIPS the aggregate instead of
    failing it — and a skipped required check does not block merge."""
    assert gate_jobs[AGGREGATE]["if"] == "always()"


def test_w3_edges_are_present(gate_jobs):
    for job in ("build-dist", "package-verify", "install-smoke"):
        assert job in gate_jobs, f"W3 edge {job!r} is missing from the gate"


# ---------------------------------------------------------------------------
# The declared macOS gap stays declared.
# ---------------------------------------------------------------------------
def test_install_smoke_covers_both_artifacts_on_all_three_cells(gate_jobs):
    matrix = gate_jobs["install-smoke"]["strategy"]["matrix"]
    assert set(matrix["kind"]) == {"wheel", "sdist"}
    labels = {f"{c['runner_os']}/{c['runner_arch']}" for c in matrix["cell"]}
    assert labels == {"Linux/X64", "Windows/X64", "macOS/ARM64"}


def test_macos_smoke_is_partial_and_the_others_are_full(gate_jobs):
    """F-773: macOS cells run `handshake` (no navigation) and MUST say so.

    If F-773 closes, flip these cells to `full` and update this pin. If someone
    quietly flips them to `full` while F-773 is open, the cells will simply hang
    — this pin is what makes the intent explicit either way.
    """
    stages = {
        f"{c['runner_os']}/{c['runner_arch']}": c["stages"]
        for c in gate_jobs["install-smoke"]["strategy"]["matrix"]["cell"]
    }
    assert stages == {
        "Linux/X64": "full",
        "Windows/X64": "full",
        "macOS/ARM64": "handshake",
    }


def test_partial_cells_are_labelled_in_the_check_name(gate_jobs):
    """A reduced cell must be readable as reduced from the check list alone."""
    name = gate_jobs["install-smoke"]["name"]
    assert "NO-NAVIGATION partial" in name


def test_the_gap_declaration_job_exists_and_is_required(gate_jobs):
    assert "known-gaps" in gate_jobs, (
        "gap declarations have ONE home; it must be a job so a reviewer reads it "
        "in the check list rather than in a YAML comment"
    )
    assert "known-gaps" in gate_jobs[AGGREGATE]["needs"]
    script = "\n".join(_all_run_steps(gate_jobs["known-gaps"]))
    for lane in ("transport", "install-smoke"):
        assert lane in script, f"known-gaps does not mention the {lane!r} gap"


def test_no_gate_job_hides_failure(gate_jobs):
    """continue-on-error anywhere would let a red lane report green."""
    for name, job in gate_jobs.items():
        assert not job.get("continue-on-error"), (
            f"job {name!r} sets continue-on-error — a required gate may not "
            f"absorb a failure"
        )
        for step in job.get("steps", []):
            assert not step.get("continue-on-error"), (
                f"job {name!r} has a continue-on-error step: {step.get('name')}"
            )


def test_pr_caller_runs_the_same_reusable_gate():
    """Every PR gets the full gate — no dispatch-only or path-filtered substitute."""
    caller = _load(TEST_CALLER)
    assert caller["jobs"]["release-gate"]["uses"].endswith("release-gate.yml")
    # `on:` parses as the boolean True in YAML 1.1; accept either spelling.
    triggers = caller.get("on", caller.get(True))
    assert "pull_request" in triggers
    assert "paths" not in triggers["pull_request"], (
        "path filtering may not omit packaging-relevant changes"
    )


# ---------------------------------------------------------------------------
# W5: the evidence ledger is wired to the real jobs, not to a parallel list.
# ---------------------------------------------------------------------------
def test_w5_evidence_edge_is_present_and_required(gate_jobs):
    assert "release-evidence" in gate_jobs, (
        "W5's ledger job is missing; the aggregate would be green with no "
        "acceptance evidence behind it"
    )
    assert "release-evidence" in gate_jobs[AGGREGATE]["needs"]
    assert gate_jobs["release-evidence"]["if"] == "always()", (
        "without always(), a failed edge SKIPS the ledger instead of failing it"
    )


def test_the_ledger_job_needs_every_evidence_producing_job(gate_jobs):
    """It must validate every lane it aggregates — not a hand-picked subset."""
    producers = {
        name
        for name, job in gate_jobs.items()
        if name not in {AGGREGATE, "release-evidence"}
    }
    assert set(gate_jobs["release-evidence"]["needs"]) == producers


def test_the_ledger_does_not_replace_a_single_direct_edge(gate_jobs):
    """§2.5: `release-gate` needs the aggregate AND every child it validates."""
    gate_needs = set(gate_jobs[AGGREGATE]["needs"])
    for child in gate_jobs["release-evidence"]["needs"]:
        assert child in gate_needs, (
            f"{child!r} is only reachable through release-evidence; the gate must "
            f"keep its own direct edge so the ledger can never soften a red job"
        )


def test_every_declared_required_cell_emits_its_record(gate_jobs):
    """The ledger's required cells and the workflow's matrix cells are ONE set.

    A cell that runs but emits nothing would make the aggregate fail (missing
    child); a declared cell the workflow no longer has would too. This pins the
    two together so the failure surfaces here, in a unit test, instead of in CI.
    """
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import release_evidence

    emitting_jobs = {
        name
        for name, job in gate_jobs.items()
        for step in job.get("steps", [])
        if "release_evidence.py emit" in step.get("run", "")
    }
    declared_jobs = {spec.job for spec in release_evidence.REQUIRED_CELLS}
    assert emitting_jobs == declared_jobs, (
        f"jobs that emit but are not declared: {sorted(emitting_jobs - declared_jobs)}; "
        f"declared but never emit: {sorted(declared_jobs - emitting_jobs)}"
    )

    yaml_cells = {
        f"unit-tests/{c['runner_os']}-{c['runner_arch']}-py{c['python-version']}"
        for c in gate_jobs["unit-tests"]["strategy"]["matrix"]["include"]
    }
    for job in ("coverage", "integration", "transport", "offline-stealth"):
        yaml_cells |= {
            f"{job}/{c['runner_os']}-{c['runner_arch']}"
            for c in gate_jobs[job]["strategy"]["matrix"]["include"]
        }
    smoke = gate_jobs["install-smoke"]["strategy"]["matrix"]
    yaml_cells |= {
        f"install-smoke/{kind}-{c['runner_os']}-{c['runner_arch']}"
        for kind in smoke["kind"]
        for c in smoke["cell"]
    }
    yaml_cells |= {
        f"{job}/default"
        for job in ("quality", "known-gaps", "build-dist", "package-verify")
    }
    assert yaml_cells == set(release_evidence.REQUIRED_KEYS), (
        f"workflow cells not in the ledger: "
        f"{sorted(yaml_cells - set(release_evidence.REQUIRED_KEYS))}; "
        f"ledger cells not in the workflow: "
        f"{sorted(set(release_evidence.REQUIRED_KEYS) - yaml_cells)}"
    )


def test_every_emitting_job_uploads_what_it_emitted(gate_jobs):
    for name, job in gate_jobs.items():
        steps = job.get("steps", [])
        if not any("release_evidence.py emit" in s.get("run", "") for s in steps):
            continue
        uploads = [
            s
            for s in steps
            if str(s.get("uses", "")).startswith("actions/upload-artifact")
            and s.get("with", {}).get("path") == "release-evidence"
        ]
        assert uploads, f"job {name!r} emits a record but never uploads it"
        assert uploads[0]["with"]["if-no-files-found"] == "error", (
            f"job {name!r} would upload nothing silently"
        )


def test_the_contract_drift_check_runs_in_the_gate(gate_jobs):
    script = "\n".join(_all_run_steps(gate_jobs["quality"]))
    assert "gen_release_contract.py --check" in script, (
        "a generated contract that nothing re-renders is a stale promise"
    )


def test_pytest_lanes_write_the_junit_the_ledger_hashes(gate_jobs):
    for job in (
        "unit-tests",
        "coverage",
        "integration",
        "transport",
        "offline-stealth",
    ):
        script = "\n".join(_all_run_steps(gate_jobs[job]))
        assert "--junitxml=junit.xml" in script, (
            f"{job!r} records pytest evidence but produces no JUnit report to hash"
        )
        assert "junit_family=xunit1" in script, (
            f"{job!r} would write a report with no `file` attribute, so its node "
            f"ids could only be guessed — the ledger rejects guesses"
        )
