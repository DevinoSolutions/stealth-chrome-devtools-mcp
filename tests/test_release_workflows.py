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


# ---------------------------------------------------------------------------
# W6: the scheduled canary observes; it never gates and never reaches outward.
#
# Every property below is one the plan states in prose and nothing else would
# enforce. The dangerous direction of drift is uniform — a canary slowly
# acquiring gate authority, or a notification step, or a duplicated copy of the
# gate's job bodies — so each is pinned rather than trusted to a comment.
# ---------------------------------------------------------------------------
CANARY = WORKFLOWS / "canary.yml"

#: Anything that mutates state outside the run or notifies a human/service. The
#: plan's words: "no issue, PR-comment, webhook, email, chat, third-party upload,
#: or write-token step".
OUTWARD_TOKENS = (
    "gh issue",
    "gh pr",
    "gh release",
    "github-script",
    "create-or-update-comment",
    "create-issue",
    "softprops/action-gh-release",
    "slack",
    "discord",
    "webhook",
    "mailto",
    "curl -X POST",
    "curl --request POST",
    "pypi",
    "codecov",
    "git push",
)


@pytest.fixture(scope="module")
def canary_jobs() -> dict:
    return _jobs(CANARY)


def test_the_canary_is_scheduled_and_manually_dispatchable():
    doc = _load(CANARY)
    # `on:` parses as the boolean True in YAML 1.1; accept either spelling.
    triggers = doc.get("on", doc.get(True))
    assert "schedule" in triggers, "a canary with no schedule observes nothing"
    assert "workflow_dispatch" in triggers, (
        "a human must be able to run the observation on demand"
    )
    assert "pull_request" not in triggers and "push" not in triggers, (
        "the canary must not run on PRs or pushes — that is how an informational "
        "lane silently becomes a de-facto gate"
    )


def test_the_canary_reuses_the_gate_instead_of_restating_it(canary_jobs):
    """W1/W4 semantics live in release-gate.yml only — never a second copy."""
    callers = {
        name
        for name, job in canary_jobs.items()
        if str(job.get("uses", "")).endswith("release-gate.yml")
    }
    assert callers, (
        "the canary's deterministic half must CALL the reusable gate; a local "
        "re-implementation of W1/W4 would be a second source of truth"
    )
    for name, job in canary_jobs.items():
        script = "\n".join(_all_run_steps(job))
        for selector in ("-m transport", '-m "transport', "stealth and not online"):
            assert selector not in script, (
                f"canary job {name!r} re-selects a GATING lane ({selector!r}) in "
                f"its own steps; those bodies belong to release-gate.yml alone"
            )


def test_the_canary_has_no_write_permission_anywhere():
    doc = _load(CANARY)
    assert doc["permissions"] == {"contents": "read"}, (
        "the canary is read-only by construction, not by convention"
    )
    for name, job in doc["jobs"].items():
        perms = job.get("permissions")
        if perms is None:
            continue
        assert "write" not in str(perms), (
            f"canary job {name!r} requests write permission: {perms}"
        )


def test_the_canary_never_reaches_outside_the_run():
    text = CANARY.read_text(encoding="utf-8").lower()
    # The header comment names these tokens to say they are absent; strip comment
    # lines so the prose describing the rule cannot fail the rule.
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for token in OUTWARD_TOKENS:
        assert token not in body, (
            f"canary.yml contains {token!r} — W6 forbids external mutation and "
            f"notification of every kind, including a well-meant alert"
        )


def test_the_live_half_cannot_fail_the_run(canary_jobs):
    live = [
        (name, job)
        for name, job in canary_jobs.items()
        if "online" in "\n".join(_all_run_steps(job))
    ]
    assert live, "no job observes the live corpus — the informational half is missing"
    for name, job in live:
        assert job.get("continue-on-error") is True, (
            f"canary job {name!r} observes live pages without continue-on-error; "
            f"a vendor result change would then gate"
        )
        script = "\n".join(_all_run_steps(job))
        assert "exit 0" in script, (
            f"canary job {name!r} must capture the live exit code rather than "
            f"propagate it — continue-on-error alone leaves the cell red-looking"
        )
        assert "INFORMATIONAL" in script, (
            f"canary job {name!r} must LABEL its live result informational in the "
            f"log; an unlabelled result gets cited as evidence"
        )


def test_the_deterministic_half_is_not_marked_non_gating(canary_jobs):
    """Local fixture failures must stay red — that half is the honest signal."""
    for name, job in canary_jobs.items():
        if str(job.get("uses", "")).endswith("release-gate.yml"):
            assert not job.get("continue-on-error"), (
                f"canary job {name!r} calls the deterministic gate but absorbs its "
                f"failure; plan_RELEASE 2.6 requires local failures to be red"
            )


# ---------------------------------------------------------------------------
# F-819: every resolved identity is a frozen identity.
# ---------------------------------------------------------------------------
def test_every_chrome_identity_resolution_freezes_the_updater_first():
    """An unfrozen `resolve_chrome.py` reads an identity the runner may replace.

    GitHub's macOS images let Keystone upgrade Chrome Stable mid-run, so a job
    that resolves the identity and later launches the browser can compare two
    different binaries (PR #64: CDP said 151.x, the artifact said 150.x). The
    flag is what makes the reading durable for the rest of the job, so a job
    that resolves without it is resolving something it cannot rely on.
    """
    for path in (RELEASE_GATE, CANARY):
        for name, job in _jobs(path).items():
            for run in _all_run_steps(job):
                if "resolve_chrome.py" not in run:
                    continue
                assert "--freeze-updater" in run, (
                    f"{path.name} job {name!r} resolves the Chrome identity "
                    f"without freezing the updater first: {run.strip()!r}"
                )


def test_the_canary_is_not_wired_into_the_gate(gate_jobs):
    """Nothing in the required check may depend on an observation lane."""
    for name, job in gate_jobs.items():
        assert "canary" not in str(job.get("uses", "")), (
            f"gate job {name!r} calls the canary; an informational lane must "
            f"never sit inside the required check"
        )
        assert "canary" not in str(job.get("needs", "")), (
            f"gate job {name!r} needs the canary"
        )
