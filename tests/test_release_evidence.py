"""Negative controls for the `release-evidence/v1` ledger (plan_RELEASE W5).

The ledger's whole value is that it CANNOT be talked into a green aggregate. A
schema that only ever passes is decoration, so every rejection path the plan
names has a test here that proves it bites: the positive fixture builds a
complete, valid ledger for all required cells, and each test breaks exactly one
thing and asserts the aggregate refuses.

These tests are hermetic — synthetic records in a tmp dir, no CI, no network,
no Chrome.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import release_evidence as re_mod  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)

SHA = "a" * 40
OTHER_SHA = "b" * 40
RUN_ID = "12345"
RUN_ATTEMPT = "1"
EVENT = "pull_request"

JUNIT_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="2">
<testcase classname="tests.test_demo" name="test_alpha" file="tests/test_demo.py"
 line="1" time="0.01" />
<testcase classname="tests.test_demo.TestGroup" name="test_beta"
 file="tests/test_demo.py" line="9" time="0.01" />
</testsuite></testsuites>
"""

SKIPPED_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="1" tests="2">
<testcase classname="tests.test_demo" name="test_alpha" file="tests/test_demo.py"
 line="1" time="0.01">
<skipped type="pytest.skip" message="no chrome" />
</testcase>
<testcase classname="tests.test_demo" name="test_xf" file="tests/test_demo.py"
 line="4" time="0.01">
<skipped type="pytest.xfail" message="known bug" />
</testcase>
</testsuite></testsuites>
"""

FAILED_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1">
<testcase classname="tests.test_demo" name="test_alpha" file="tests/test_demo.py"
 line="1" time="0.01">
<failure message="boom">assert 0</failure>
</testcase>
</testsuite></testsuites>
"""


def _junit_for(nodes: list[str]) -> str:
    """A passing JUnit report for exactly these node ids.

    The positive control feeds the transport cells the nodes the SHIPPED claim
    ledger cites, so "a complete ledger aggregates clean" also proves the
    repository's own claims are the shape the verifier accepts.
    """
    cases = []
    for node in nodes:
        file_part, _, rest = node.partition("::")
        module = file_part[: -len(".py")].replace("/", ".")
        *classes, name = rest.split("::")
        classname = ".".join([module, *classes])
        cases.append(
            f'<testcase classname="{classname}" name="{name}" '
            f'file="{file_part}" line="1" time="0.01" />'
        )
    body = "\n".join(cases)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite name="pytest" errors="0" failures="0" '
        f'skipped="0" tests="{len(cases)}">\n{body}\n</testsuite></testsuites>\n'
    )


def _shipped_claim_nodes() -> list[str]:
    return sorted(
        {str(row.get("node_id", "")) for row in re_mod.claim_rows(re_mod.load_claims())}
    )


def _runner_identity(tmp_path: Path, spec: re_mod.CellSpec) -> Path:
    path = tmp_path / f"runner-{spec.job}-{spec.cell}.json"
    path.write_text(
        json.dumps(
            {
                "runner_os": spec.runner_os,
                "runner_arch": spec.runner_arch,
                "image_os": "ubuntu24",
                "image_version": "20260701.1",
                "python_version": spec.python_version or "3.12.4",
            }
        ),
        encoding="utf-8",
    )
    return path


def _chrome_identity(tmp_path: Path, spec: re_mod.CellSpec) -> Path:
    path = tmp_path / f"chrome-{spec.job}-{spec.cell}.json"
    path.write_text(
        json.dumps(
            {
                "os": spec.runner_os,
                "arch": spec.runner_arch,
                "path": "/usr/bin/google-chrome",
                "version": "141.0.7390.54",
                "product": "Chrome/141.0.7390.54",
            }
        ),
        encoding="utf-8",
    )
    return path


def _emit_cell(
    root: Path,
    work: Path,
    spec: re_mod.CellSpec,
    *,
    junit_body: str = JUNIT_TEMPLATE,
    outcome: str = "success",
    release_sha: str = SHA,
    run_id: str = RUN_ID,
    claim_nodes: bool = False,
) -> dict[str, object]:
    runner = _runner_identity(work, spec)
    artifacts: list[tuple[str, Path]] = [("runner-identity", runner)]
    chrome = None
    if spec.expects_chrome:
        chrome = _chrome_identity(work, spec)
        artifacts.append(("chrome-identity", chrome))
    junit = None
    if spec.expects_pytest:
        junit = work / f"junit-{spec.job}-{spec.cell}.xml"
        body = junit_body
        if claim_nodes and spec.job in re_mod.TRANSPORT_JOBS:
            # Opt-in: the transport lane is where the SHIPPED claims are
            # evidenced. Off by default so a test that wants a run which never
            # executed those nodes gets exactly that.
            body = _junit_for(
                sorted({*_shipped_claim_nodes(), "tests/test_demo.py::test_alpha"})
            )
        junit.write_text(body, encoding="utf-8")
        artifacts.append(("junit", junit))
    emit = re_mod.EmitSpec(
        out_root=root,
        release_sha=release_sha,
        workflow_name="release-gate",
        run_id=run_id,
        run_attempt=RUN_ATTEMPT,
        event=EVENT,
        job_id=spec.job,
        matrix_cell=spec.cell,
        terminal_outcome=outcome,
        runner_identity=runner,
        chrome_identity=chrome,
        chrome_launched=spec.expects_launched_chrome,
        junit=junit,
        artifacts=tuple(artifacts),
        mq_ids=(),
    )
    record = re_mod.build_record(emit)
    out = root / release_sha / spec.job / f"{spec.cell}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(re_mod.dumps(record), encoding="utf-8")
    return record


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """A complete, valid ledger for every required cell.

    Its transport cells execute the nodes the SHIPPED claim ledger cites, so the
    positive control also proves the repository's own claims are verifiable in
    the shape the aggregate demands.
    """
    root = tmp_path / "release-evidence"
    work = tmp_path / "work"
    work.mkdir()
    for spec in re_mod.REQUIRED_CELLS:
        _emit_cell(root, work, spec, claim_nodes=True)
    return root


def _no_claims(root: Path) -> Path:
    """A claims document with zero qualified rows.

    The structural tests are about the ledger, not about what the repo currently
    claims: pointing them at the SHIPPED ledger would make every one of them fail
    the day a real claim lands, for a reason that has nothing to do with the
    property under test. The shipped ledger gets its own tests below.
    """
    path = root.parent / "no-claims.json"
    if not path.exists():
        path.write_text(
            json.dumps(
                {
                    "schema": re_mod.CLAIMS_SCHEMA,
                    "note": [],
                    "qualified": [],
                    "default_note": {"tracking_id": "F-776", "user_impact": "x"},
                    "served_unqualified_notes": {},
                    "not_served": [],
                }
            ),
            encoding="utf-8",
        )
    return path


def _aggregate(root: Path, **overrides: str) -> tuple[dict[str, object], list[str]]:
    spec = re_mod.AggregateSpec(
        root=root,
        release_sha=overrides.get("release_sha", SHA),
        workflow_name="release-gate",
        run_id=overrides.get("run_id", RUN_ID),
        run_attempt=overrides.get("run_attempt", RUN_ATTEMPT),
        event=EVENT,
        claims_path=Path(overrides.get("claims", _no_claims(root))),
    )
    return re_mod.build_aggregate(spec)


def _record_path(root: Path, key: str) -> Path:
    return root / SHA / f"{key}.json"


def _mutate(root: Path, key: str, mutate) -> None:
    path = _record_path(root, key)
    record = json.loads(path.read_text(encoding="utf-8"))
    mutate(record)
    path.write_text(re_mod.dumps(record), encoding="utf-8")


# ── the positive control ────────────────────────────────────────────────────
def test_a_complete_current_ledger_aggregates_clean(ledger: Path):
    aggregate, problems = _aggregate(ledger)
    assert problems == []
    assert aggregate["job"] == {
        "id": "release-gate",
        "matrix_cell": "aggregate",
        "terminal_outcome": "success",
    }
    assert aggregate["required_cells"] == list(re_mod.REQUIRED_KEYS)
    children = aggregate["children"]
    assert [child["key"] for child in children] == list(re_mod.REQUIRED_KEYS)
    assert all(len(str(child["sha256"])) == 64 for child in children)


def test_the_required_cell_set_covers_all_three_qualified_runners(ledger: Path):
    labels = {spec.label for spec in re_mod.REQUIRED_CELLS}
    assert labels == {"Linux/X64", "Windows/X64", "macOS/ARM64"}
    transport = {
        spec.label for spec in re_mod.REQUIRED_CELLS if spec.job == "transport"
    }
    assert transport == {"Linux/X64", "Windows/X64"}, (
        "macOS transport is a DECLARED gap (F-773); if it comes back, the "
        "contract's transport paragraph must change in the same commit"
    )


# ── child-set rejections ────────────────────────────────────────────────────
def test_missing_child_is_rejected(ledger: Path):
    _record_path(ledger, "transport/Windows-X64").unlink()
    _, problems = _aggregate(ledger)
    assert any("missing child record" in p for p in problems)


def test_extra_child_for_an_undeclared_cell_is_rejected(ledger: Path):
    extra = ledger / SHA / "transport" / "macOS-ARM64.json"
    extra.write_text(
        _record_path(ledger, "transport/Linux-X64").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _, problems = _aggregate(ledger)
    assert any("undeclared cell" in p for p in problems)


def test_a_record_whose_body_disagrees_with_its_path_is_rejected(ledger: Path):
    _mutate(
        ledger,
        "coverage/Linux-X64",
        lambda r: r["job"].update({"matrix_cell": "Windows-X64"}),
    )
    _, problems = _aggregate(ledger)
    assert any("but its path says" in p for p in problems)


def test_one_cell_cannot_stand_in_for_another(ledger: Path):
    """Two cells emitting the SAME key can never both pass — on any filesystem.

    Every cell's artifact merges into one tree, so a copy/paste that gives two
    jobs the same ``--job-id``/``--matrix-cell`` means one record lands on the
    other. That must not be survivable: here `known-gaps` emits `quality`'s
    record, and the ledger rejects it because the body disagrees with the path it
    occupies — the deterministic form of the duplicate guard.
    """
    stand_in = ledger / SHA / "known-gaps" / "default.json"
    stand_in.write_text(
        _record_path(ledger, "quality/default").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _, problems = _aggregate(ledger)
    assert any("but its path says" in p for p in problems)


def test_case_variant_duplicate_is_rejected(ledger: Path, tmp_path: Path):
    """A case-variant twin hits the duplicate guard where the filesystem allows one.

    On a case-insensitive filesystem the write lands on the one existing file,
    so the guard is unreachable *because a second record cannot exist* — and
    that is what the test asserts there. No skip: both branches assert a real
    property, and the deterministic same-key case above is covered on every OS.
    """
    (tmp_path / "Probe").write_text("x", encoding="utf-8")
    case_insensitive = (tmp_path / "probe").exists()
    real = _record_path(ledger, "quality/default")
    quality_dir = ledger / SHA / "quality"
    (quality_dir / "Default.json").write_text(
        real.read_text(encoding="utf-8"), encoding="utf-8"
    )
    _, problems = _aggregate(ledger)
    if case_insensitive:
        assert len(list(quality_dir.glob("*.json"))) == 1
        assert problems == []
    else:
        assert any("duplicate" in p or "undeclared cell" in p for p in problems)


# ── identity rejections ─────────────────────────────────────────────────────
def test_stale_release_sha_is_rejected(ledger: Path):
    _, problems = _aggregate(ledger, release_sha=OTHER_SHA)
    assert any("no evidence directory" in p for p in problems)


def test_a_child_from_another_sha_is_rejected(tmp_path: Path):
    root = tmp_path / "release-evidence"
    work = tmp_path / "work"
    work.mkdir()
    for spec in re_mod.REQUIRED_CELLS:
        _emit_cell(root, work, spec)
    _mutate(root, "quality/default", lambda r: r.update({"release_sha": OTHER_SHA}))
    _, problems = _aggregate(root)
    assert any("stale evidence" in p for p in problems)


def test_a_child_from_another_workflow_run_is_rejected(ledger: Path):
    _mutate(
        ledger, "build-dist/default", lambda r: r["workflow"].update({"run_id": "9"})
    )
    _, problems = _aggregate(ledger)
    assert any("foreign evidence" in p for p in problems)


def test_a_child_from_another_run_attempt_is_rejected(ledger: Path):
    _mutate(
        ledger,
        "build-dist/default",
        lambda r: r["workflow"].update({"run_attempt": "7"}),
    )
    _, problems = _aggregate(ledger)
    assert any("run_attempt" in p for p in problems)


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped"])
def test_non_success_terminal_outcome_is_rejected(ledger: Path, outcome: str):
    _mutate(
        ledger,
        "integration/Linux-X64",
        lambda r: r["job"].update({"terminal_outcome": outcome}),
    )
    _, problems = _aggregate(ledger)
    assert any("non-success terminal outcome" in p for p in problems)


def test_an_invalid_outcome_value_is_rejected(ledger: Path):
    _mutate(
        ledger,
        "integration/Linux-X64",
        lambda r: r["job"].update({"terminal_outcome": "green"}),
    )
    _, problems = _aggregate(ledger)
    assert any("not a known outcome" in p for p in problems)


# ── schema rejections ───────────────────────────────────────────────────────
def test_unknown_field_is_rejected(ledger: Path):
    _mutate(ledger, "quality/default", lambda r: r.update({"vibes": "good"}))
    _, problems = _aggregate(ledger)
    assert any("unknown field" in p for p in problems)


def test_missing_field_is_rejected(ledger: Path):
    _mutate(ledger, "quality/default", lambda r: r.pop("mq_ids"))
    _, problems = _aggregate(ledger)
    assert any("missing field" in p for p in problems)


def test_wrong_schema_string_is_rejected(ledger: Path):
    _mutate(ledger, "quality/default", lambda r: r.update({"schema": "evidence/v2"}))
    _, problems = _aggregate(ledger)
    assert any("schema:" in p for p in problems)


def test_malformed_sha_is_rejected(ledger: Path):
    record = json.loads(
        _record_path(ledger, "quality/default").read_text(encoding="utf-8")
    )
    record["release_sha"] = "not-a-sha"
    problems = re_mod.validate_record(record, expect_key="quality/default")
    assert any("release_sha" in p for p in problems)


def test_malformed_run_id_is_rejected(ledger: Path):
    _mutate(
        ledger, "quality/default", lambda r: r["workflow"].update({"run_id": "abc"})
    )
    _, problems = _aggregate(ledger)
    assert any("not numeric" in p for p in problems)


def test_unknown_workflow_event_is_rejected(ledger: Path):
    _mutate(
        ledger, "quality/default", lambda r: r["workflow"].update({"event": "vibes"})
    )
    _, problems = _aggregate(ledger)
    assert any("not a known event" in p for p in problems)


def test_a_malformed_matrix_cell_is_rejected():
    problems = re_mod.validate_record({}, expect_key="transport/../etc")
    assert any("not a declared required cell" in p for p in problems)


def test_a_runner_without_a_github_image_identity_is_rejected(ledger: Path):
    """A self-hosted runner has no ImageOS — and is outside the qualified matrix."""
    _mutate(ledger, "quality/default", lambda r: r["runner"].update({"image_os": ""}))
    _, problems = _aggregate(ledger)
    assert any("image_os" in p for p in problems)


def test_a_runner_that_is_not_the_declared_cell_is_rejected(ledger: Path):
    _mutate(
        ledger, "transport/Linux-X64", lambda r: r["runner"].update({"os": "Windows"})
    )
    _, problems = _aggregate(ledger)
    assert any("!= declared cell" in p for p in problems)


# ── Chrome identity rejections ──────────────────────────────────────────────
def test_a_browser_cell_without_chrome_identity_is_rejected(ledger: Path):
    _mutate(ledger, "integration/macOS-ARM64", lambda r: r.update({"chrome": None}))
    _, problems = _aggregate(ledger)
    assert any("chrome: null on browser cell" in p for p in problems)


def test_a_browser_cell_that_never_launched_chrome_is_rejected(ledger: Path):
    _mutate(
        ledger,
        "transport/Linux-X64",
        lambda r: r["chrome"].update({"launched_major": None}),
    )
    _, problems = _aggregate(ledger)
    assert any("must prove a LAUNCHED browser" in p for p in problems)


def test_the_partial_macos_smoke_cell_may_not_claim_a_launch(ledger: Path):
    """F-773: the handshake-only cells install and serve; they navigate nothing."""
    _mutate(
        ledger,
        "install-smoke/wheel-macOS-ARM64",
        lambda r: r["chrome"].update({"launched_major": 141}),
    )
    _, problems = _aggregate(ledger)
    assert any("declared non-launching but recorded a launch" in p for p in problems)


def test_a_non_browser_cell_may_not_record_chrome(ledger: Path):
    _mutate(
        ledger,
        "quality/default",
        lambda r: r.update(
            {
                "chrome": {
                    "path": "/usr/bin/google-chrome",
                    "executable_version": "141.0.0.0",
                    "launched_major": None,
                }
            }
        ),
    )
    _, problems = _aggregate(ledger)
    assert any("recorded on non-browser cell" in p for p in problems)


# ── pytest-evidence rejections ──────────────────────────────────────────────
def test_a_cell_that_owes_pytest_evidence_may_not_omit_it(ledger: Path):
    _mutate(ledger, "unit-tests/Linux-X64-py3.12", lambda r: r.update({"pytest": None}))
    _, problems = _aggregate(ledger)
    assert any("owes pytest evidence" in p for p in problems)


def test_a_non_pytest_cell_may_not_invent_pytest_evidence(ledger: Path):
    _mutate(
        ledger,
        "known-gaps/default",
        lambda r: r.update(
            {
                "pytest": {
                    "junit_sha256": "0" * 64,
                    "executed_node_ids": [],
                    "skipped": [],
                    "xfail": [],
                    "failed": [],
                }
            }
        ),
    )
    _, problems = _aggregate(ledger)
    assert any("recorded on non-pytest cell" in p for p in problems)


def test_unsorted_or_duplicated_node_ids_are_rejected(ledger: Path):
    _mutate(
        ledger,
        "coverage/Linux-X64",
        lambda r: r["pytest"].update(
            {"executed_node_ids": ["tests/b.py::z", "tests/a.py::a", "tests/a.py::a"]}
        ),
    )
    _, problems = _aggregate(ledger)
    assert any("not deterministically sorted" in p for p in problems)
    assert any("contains duplicates" in p for p in problems)


def test_a_junit_hash_that_does_not_match_the_uploaded_report_is_rejected(
    ledger: Path,
):
    _mutate(
        ledger,
        "coverage/Windows-X64",
        lambda r: r["pytest"].update({"junit_sha256": "c" * 64}),
    )
    _, problems = _aggregate(ledger)
    assert any("JUnit hash mismatch" in p for p in problems)


def test_an_artifact_hash_that_does_not_match_the_bytes_is_rejected(ledger: Path):
    def corrupt(record: dict) -> None:
        record["artifacts"][0]["sha256"] = "d" * 64

    _mutate(ledger, "package-verify/default", corrupt)
    _, problems = _aggregate(ledger)
    assert any("artifact hash mismatch" in p for p in problems)


def test_an_artifact_that_was_never_uploaded_is_rejected(ledger: Path):
    record = json.loads(
        _record_path(ledger, "build-dist/default").read_text(encoding="utf-8")
    )
    (ledger / SHA / str(record["artifacts"][0]["path"])).unlink()
    _, problems = _aggregate(ledger)
    assert any("was not uploaded" in p for p in problems)


def test_an_unknown_artifact_kind_is_rejected(ledger: Path):
    def relabel(record: dict) -> None:
        record["artifacts"][0]["kind"] = "screenshot"

    _mutate(ledger, "build-dist/default", relabel)
    _, problems = _aggregate(ledger)
    assert any("kind:" in p for p in problems)


# ── JUnit parsing ───────────────────────────────────────────────────────────
def test_junit_parsing_rebuilds_exact_node_ids(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    junit.write_text(JUNIT_TEMPLATE, encoding="utf-8")
    parsed = re_mod.parse_junit(junit)
    assert parsed["executed_node_ids"] == [
        "tests/test_demo.py::TestGroup::test_beta",
        "tests/test_demo.py::test_alpha",
    ]
    assert parsed["skipped"] == []
    assert parsed["failed"] == []


def test_junit_parsing_separates_skip_from_xfail(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    junit.write_text(SKIPPED_JUNIT, encoding="utf-8")
    parsed = re_mod.parse_junit(junit)
    assert parsed["skipped"] == ["tests/test_demo.py::test_alpha"]
    assert parsed["xfail"] == ["tests/test_demo.py::test_xf"]
    assert "tests/test_demo.py::test_alpha" not in parsed["executed_node_ids"], (
        "a skipped node was collected, not executed — it may never read as success"
    )


def test_a_dotted_module_path_is_not_a_node_id(ledger: Path):
    """`tests.test_x.TestY::test_z` is NOT a node id and must be rejected.

    This is the first CI run's real failure, pinned. The dotted form cannot be
    resolved against `pytest --collect-only`, so a ledger that accepted it would
    hold ids nobody can verify — and W8's parity gate is built on resolving
    exactly these strings. The fix belongs in the producer (see the next test);
    the check stays strict.
    """
    _mutate(
        ledger,
        "coverage/Linux-X64",
        lambda r: r["pytest"].update(
            {"executed_node_ids": ["tests.test_x.TestY::test_z"]}
        ),
    )
    _, problems = _aggregate(ledger)
    assert any("not a fully-qualified node id" in p for p in problems)


def test_pytest_really_writes_the_file_attribute_the_parser_needs(tmp_path: Path):
    """The producer contract, pinned against a REAL pytest run.

    `junit_family=xunit2` (pytest's default) writes only `classname`/`name`; the
    ledger's node ids would then be guesses. The gate passes
    `-o junit_family=xunit1`, which writes the source `file`. If a pytest upgrade
    ever drops that attribute, this fails here instead of silently producing
    dotted ids that the validator rejects three CI hours later.
    """
    module = tmp_path / "test_probe_nodeids.py"
    module.write_text(
        "class TestGroup:\n"
        "    def test_inside(self):\n"
        "        assert True\n"
        "\n"
        "def test_outside():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    report = tmp_path / "junit.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(module),
            "-p",
            "no:cacheprovider",
            "-q",
            f"--junitxml={report}",
            "-o",
            "junit_family=xunit1",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    parsed = re_mod.parse_junit(report)
    assert parsed["executed_node_ids"] == [
        "test_probe_nodeids.py::TestGroup::test_inside",
        "test_probe_nodeids.py::test_outside",
    ], "the gate's JUnit settings no longer yield resolvable node ids"


def test_a_real_github_image_id_with_hyphens_is_accepted(ledger: Path):
    """`win25-vs2026` is a real hosted image id; the check is PRESENCE, not shape.

    The first CI run rejected it. What must stay rejected is an ABSENT image
    identity — that is the self-hosted signal, and the test below still proves it.
    """
    _mutate(
        ledger,
        "unit-tests/Windows-X64-py3.12",
        lambda r: r["runner"].update({"image_os": "win25-vs2026"}),
    )
    _, problems = _aggregate(ledger)
    assert problems == []


def test_junit_parsing_records_failures(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    junit.write_text(FAILED_JUNIT, encoding="utf-8")
    parsed = re_mod.parse_junit(junit)
    assert parsed["failed"] == ["tests/test_demo.py::test_alpha"]


# ── MQ ids ──────────────────────────────────────────────────────────────────
def test_a_malformed_mq_id_is_rejected(ledger: Path):
    _mutate(ledger, "quality/default", lambda r: r.update({"mq_ids": ["MQ53"]}))
    _, problems = _aggregate(ledger)
    assert any("mq_ids" in p and "malformed" in p for p in problems)


def test_a_duplicate_mq_id_inside_one_record_is_rejected(ledger: Path):
    _mutate(ledger, "quality/default", lambda r: r.update({"mq_ids": ["MQ-1", "MQ-1"]}))
    _, problems = _aggregate(ledger)
    assert any("duplicate id inside one record" in p for p in problems)


def test_a_required_mq_id_with_no_evidence_is_rejected(ledger: Path, monkeypatch):
    """W8 fills REQUIRED_MQ_IDS; the check that will police it works today."""
    monkeypatch.setattr(re_mod, "REQUIRED_MQ_IDS", frozenset({"MQ-1"}))
    _, problems = _aggregate(ledger)
    assert any("has no runtime evidence in this run" in p for p in problems)


def test_the_same_mq_id_on_several_cells_is_fine(ledger: Path):
    """One manual step verified on three OSes is three records, not a duplicate."""
    for key in ("coverage/Linux-X64", "coverage/Windows-X64", "coverage/macOS-ARM64"):
        _mutate(ledger, key, lambda r: r.update({"mq_ids": ["MQ-1"]}))
    _, problems = _aggregate(ledger)
    assert problems == []


# ── tool-claim rejections (the heart of "green is not claimable") ───────────
def _claims(tmp_path: Path, qualified: list[dict[str, object]]) -> Path:
    path = tmp_path / "claims.json"
    path.write_text(
        json.dumps(
            {
                "schema": re_mod.CLAIMS_SCHEMA,
                "note": [],
                "qualified": qualified,
                "default_note": {"tracking_id": "F-776", "user_impact": "x"},
                "served_unqualified_notes": {},
                "not_served": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _claim(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "tool": "get_cookies",
        "outcome": "reads back the exact cookie value it set",
        "transport": "stdio",
        "node_id": "tests/test_demo.py::test_alpha",
        "site_shape": "local fixture app",
        "required_cells": ["transport/Linux-X64", "transport/Windows-X64"],
    }
    row.update(overrides)
    return row


def test_a_claim_whose_node_really_passed_on_its_cells_is_accepted(
    ledger: Path, tmp_path: Path
):
    claims = _claims(tmp_path, [_claim()])
    _, problems = _aggregate(ledger, claims=str(claims))
    assert problems == []


def test_every_shipped_claim_is_one_the_ledger_can_actually_check(tmp_path: Path):
    """The SHIPPED claim ledger, verified against a run where its nodes passed.

    The positive control for what the repository actually claims today: the
    fixture gives the transport cells a pass for every cited node, and the
    aggregate goes clean. It proves the rows are checkable statements rather than
    decoration — and, with the rejection test below, that they are checked.

    It does NOT assert those nodes pass in reality. Only a real run says that;
    until one exists the ledger has no current-SHA evidence and the
    `release-evidence` job fails closed. Deliberately so.
    """
    if not re_mod.claim_rows(re_mod.load_claims()):
        pytest.skip("no qualified claims are shipped at this SHA")
    root = tmp_path / "release-evidence"
    work = tmp_path / "work"
    work.mkdir()
    for spec in re_mod.REQUIRED_CELLS:
        _emit_cell(root, work, spec, claim_nodes=True)
    _, problems = _aggregate(root, claims=str(re_mod.CLAIMS_PATH))
    assert problems == [], f"a shipped claim is not verifiable: {problems}"


def test_a_shipped_claim_fails_when_its_node_did_not_pass(tmp_path: Path):
    """The same shipped ledger against a run that never executed those nodes.

    The transport cells here report an unrelated node, so every shipped claim
    loses its evidence at once — the exact shape of "the test was renamed,
    deselected, or quietly dropped, and the contract kept claiming it".
    """
    if not re_mod.claim_rows(re_mod.load_claims()):
        pytest.skip("no qualified claims are shipped at this SHA")
    root = tmp_path / "release-evidence"
    work = tmp_path / "work"
    work.mkdir()
    unrelated = _junit_for(["tests/test_demo.py::test_alpha"])
    for spec in re_mod.REQUIRED_CELLS:
        _emit_cell(root, work, spec, junit_body=unrelated)
    _, problems = _aggregate(root, claims=str(re_mod.CLAIMS_PATH))
    assert any("did not execute and pass" in p for p in problems)


def test_a_claim_whose_node_never_ran_is_rejected(ledger: Path, tmp_path: Path):
    claims = _claims(tmp_path, [_claim(node_id="tests/test_demo.py::test_imaginary")])
    _, problems = _aggregate(ledger, claims=str(claims))
    assert any("did not execute and pass" in p for p in problems)


def test_a_claim_whose_node_was_skipped_is_rejected(tmp_path: Path):
    root = tmp_path / "release-evidence"
    work = tmp_path / "work"
    work.mkdir()
    for spec in re_mod.REQUIRED_CELLS:
        body = SKIPPED_JUNIT if spec.job == "transport" else JUNIT_TEMPLATE
        _emit_cell(root, work, spec, junit_body=body)
    claims = _claims(tmp_path, [_claim()])
    _, problems = _aggregate(root, claims=str(claims))
    assert any("did not execute and pass" in p for p in problems)


def test_a_claim_that_cites_the_representative_journey_is_rejected(
    ledger: Path, tmp_path: Path
):
    """plan_RELEASE §2.5: ONE journey node may never qualify an individual tool."""
    journey = sorted(re_mod.NON_PER_TOOL_NODES)[0]
    claims = _claims(tmp_path, [_claim(node_id=journey)])
    _, problems = _aggregate(ledger, claims=str(claims))
    assert any("representative journey" in p for p in problems)


def test_a_stdio_claim_evidenced_by_a_non_transport_job_is_rejected(
    ledger: Path, tmp_path: Path
):
    """An in-process `.fn` cell cannot license a transport claim."""
    claims = _claims(tmp_path, [_claim(required_cells=["integration/Linux-X64"])])
    _, problems = _aggregate(ledger, claims=str(claims))
    assert any("does not run the real-stdio lane" in p for p in problems)


def test_a_claim_naming_an_undeclared_cell_is_rejected(ledger: Path, tmp_path: Path):
    claims = _claims(tmp_path, [_claim(required_cells=["transport/macOS-ARM64"])])
    _, problems = _aggregate(ledger, claims=str(claims))
    assert any("undeclared cell" in p for p in problems)


def test_a_claim_with_no_cells_is_rejected(ledger: Path, tmp_path: Path):
    claims = _claims(tmp_path, [_claim(required_cells=[])])
    _, problems = _aggregate(ledger, claims=str(claims))
    assert any("names no required cells" in p for p in problems)


# ── the claim document itself ───────────────────────────────────────────────
def test_the_shipped_claim_ledger_is_valid():
    claims = re_mod.load_claims()
    problems = re_mod.validate_claims_document(claims, re_mod.registry_tool_names())
    assert problems == []


def test_a_claim_for_a_tool_the_registry_does_not_serve_is_rejected():
    claims = dict(re_mod.load_claims())
    claims["qualified"] = [_claim(tool="teleport_browser")]
    problems = re_mod.validate_claims_document(claims, re_mod.registry_tool_names())
    assert any("unknown tool" in p for p in problems)


def test_a_duplicate_tool_claim_is_rejected():
    claims = dict(re_mod.load_claims())
    claims["qualified"] = [_claim(), _claim()]
    problems = re_mod.validate_claims_document(claims, re_mod.registry_tool_names())
    assert any("duplicate qualified claim" in p for p in problems)


def test_a_claim_missing_a_required_field_is_rejected():
    claims = dict(re_mod.load_claims())
    row = _claim()
    row.pop("site_shape")
    claims["qualified"] = [row]
    problems = re_mod.validate_claims_document(claims, re_mod.registry_tool_names())
    assert any("is missing" in p for p in problems)


def test_a_not_served_entry_for_a_served_tool_is_rejected():
    claims = dict(re_mod.load_claims())
    claims["not_served"] = ["navigate"]
    problems = re_mod.validate_claims_document(claims, re_mod.registry_tool_names())
    assert any("is actually served" in p for p in problems)


def test_the_tool_count_is_derived_from_the_registry():
    from stealth_chrome_devtools_mcp.embedded import server as server_mod

    registry_total = sum(len(v) for v in server_mod.SECTION_TOOLS.values())
    assert len(re_mod.registry_tool_names()) == registry_total
