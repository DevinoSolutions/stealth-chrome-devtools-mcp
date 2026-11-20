"""tools/flake_sweep.py and the flake-sweep workflow, without CI or a browser.

The driver reaches pytest only through an injectable runner, so every behaviour a
sweep must have is pinned here against a fake: fresh process per run, never stop at
the first red, name the failing node ids, treat "no test selected" as neither a pass
nor a failure, and refuse to call an unmeasured or cut-short sweep green. The
workflow is pinned structurally, including that its setup steps are the release
gate's own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import flake_sweep as fs  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)
import release_schema  # noqa: E402  PERMANENT(tools/ is not an importable package; the sys.path line above must run first)

SWEEP = ROOT / ".github" / "workflows" / "flake-sweep.yml"
GATE = ROOT / ".github" / "workflows" / "release-gate.yml"

GREEN = fs.RunResult(0, "1 passed\n")


def _red(*ids: str, code: int = 1) -> fs.RunResult:
    return fs.RunResult(code, "".join(f"FAILED {i} - boom\n" for i in ids))


class FakeRunner:
    """Answers from a per-file script of results, and records every argv."""

    def __init__(self, script: dict[str, list[fs.RunResult]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> fs.RunResult:
        self.calls.append(argv)
        queue = self.script[argv[argv.index("pytest") + 1]]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _sweep(tmp_path: Path, files: list[str], runner, **kw):
    return fs.sweep(
        files,
        repeat=kw.pop("repeat", 4),
        marker="integration",
        out_dir=tmp_path,
        runner=runner,
        **kw,
    )


# ── one fresh process per run, and a red never stops the sweep ──────────────
def test_every_run_is_a_new_pytest_process_and_a_red_never_stops_the_sweep(tmp_path):
    runner = FakeRunner(
        {
            "tests/a.py": [
                GREEN,
                _red("tests/a.py::t1"),
                GREEN,
                _red("tests/a.py::t1"),
            ],
            "tests/b.py": [GREEN],
        }
    )
    a, b = _sweep(tmp_path, ["tests/a.py", "tests/b.py"], runner)
    assert len(runner.calls) == 8, "4 runs of each file, none skipped after a red"
    assert (a.runs, a.failures, dict(a.failing)) == (4, 2, {"tests/a.py::t1": 2})
    assert (b.runs, b.failures) == (4, 0)
    for argv in runner.calls:
        assert argv[:3] == [sys.executable, "-m", "pytest"], "a new interpreter"
        assert argv[argv.index("-m", 3) + 1] == "integration"
    junits = [a for argv in runner.calls for a in argv if a.startswith("--junitxml=")]
    assert len(set(junits)) == 8, "one junit file per run, none overwritten"
    assert (tmp_path / "tests__a.py" / "run-02.log").read_text().startswith("FAILED")
    assert (tmp_path / "tests__b.py" / "run-04.log").exists()


def test_a_run_that_selects_no_test_is_not_a_failure_and_is_not_repeated(tmp_path):
    runner = FakeRunner({"tests/a.py": [fs.RunResult(5, "no tests ran\n")]})
    (a,) = _sweep(tmp_path, ["tests/a.py"], runner)
    assert a.no_tests and (a.runs, a.failures, a.not_run) == (0, 0, 0)
    assert len(runner.calls) == 1
    assert fs.exit_status([a]) == 2, "a sweep that measured nothing is not green"


def test_a_timeout_is_a_failure_that_says_so(tmp_path):
    killed = fs.RunResult(-1, "partial output, no summary\n", timed_out=True)
    runner = FakeRunner({"tests/a.py": [killed]})
    (a,) = _sweep(tmp_path, ["tests/a.py"], runner, repeat=2, run_timeout=60)
    assert a.failures == 2
    assert list(a.failing) == ["<run exceeded 60s and was killed>"]


def test_the_deadline_stops_new_runs_and_the_sweep_is_not_a_pass(tmp_path):
    ticks = iter([0, 0, 100, 100, 100, 100])
    runner = FakeRunner({"tests/a.py": [GREEN], "tests/b.py": [GREEN]})
    a, b = _sweep(
        tmp_path,
        ["tests/a.py", "tests/b.py"],
        runner,
        repeat=2,
        deadline=50,
        clock=lambda: next(ticks),
    )
    assert len(runner.calls) == 2 and (a.runs, b.runs) == (2, 0)
    assert (a.not_run, b.not_run) == (0, 2)
    assert fs.exit_status([a, b]) == 1
    assert "| 0 of 2 (deadline) | 0 | - |" in fs.summary([a, b], title="t")


# ── what a red run names ───────────────────────────────────────────────────
def test_failing_ids_come_from_the_short_summary_exactly():
    out = (
        "tests/a.py::test_x FAILED\n"
        "FAILED tests/a.py::TestC::test_p[a - b] - AssertionError: 1 == 2\n"
        "ERROR tests/b.py - collection error\n"
        "FAILED tests/a.py::TestC::test_p[a - b] - again\n"
        "FAILED tests/c.py::test_noreason\n"
    )
    assert fs.failing_ids(fs.RunResult(1, out), 60) == [
        "tests/a.py::TestC::test_p[a",
        "tests/b.py",
        "tests/c.py::test_noreason",
    ], "documented limit: a ' - ' inside a parametrize id cuts the id there"
    assert fs.failing_ids(fs.RunResult(2, "Interrupted\n"), 60) == [
        "<pytest exit 2, no failed node id in the log>"
    ]


# ── patterns ───────────────────────────────────────────────────────────────
def test_patterns_expand_to_existing_files_sorted_and_deduplicated(
    tmp_path, monkeypatch
):
    (tmp_path / "tests").mkdir()
    for name in ("test_b.py", "test_a.py", "test_a_more.py", "notes.txt"):
        (tmp_path / "tests" / name).write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert fs.expand(["tests/test_a*.py", "tests/test_*.py"]) == [
        "tests/test_a.py",
        "tests/test_a_more.py",
        "tests/test_b.py",
    ]
    with pytest.raises(ValueError, match="matches no file"):
        fs.expand(["tests/test_a*.py", "tests/test_gone_*.py"])
    with pytest.raises(ValueError, match="matches no file"):
        fs.expand(["tests"])  # a directory is not a file


def test_the_default_patterns_still_match_files_in_this_repo(monkeypatch):
    """A rename that strands a default pattern fails here, not in a dispatched run."""
    default = yaml.safe_load(SWEEP.read_text(encoding="utf-8"))[True][
        "workflow_dispatch"
    ]
    patterns = default["inputs"]["files"]["default"].split()
    assert len(patterns) == 6
    monkeypatch.chdir(ROOT)
    for pattern in patterns:
        assert fs.expand([pattern]), pattern


# ── the table ──────────────────────────────────────────────────────────────
def test_the_summary_table_names_file_runs_failures_and_node_ids(tmp_path):
    runner = FakeRunner(
        {
            "tests/a.py": [_red("tests/a.py::t|1"), GREEN],
            "tests/b.py": [fs.RunResult(5, "")],
        }
    )
    outcomes = _sweep(tmp_path, ["tests/a.py", "tests/b.py"], runner, repeat=2)
    text = fs.summary(outcomes, title="Flake sweep Linux")
    assert "| file | runs | failures | failing node ids |" in text
    assert "| `tests/a.py` | 2 | 1 | `tests/a.py::t\\|1` (1/2) |" in text
    assert "| `tests/b.py` | 0 | 0 | no test selected by the marker |" in text
    assert "2 runs of 1 files, 1 red, 0 not run, 1 files with no test selected." in text


# ── the command line ───────────────────────────────────────────────────────
def _tree(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def test_run_writes_the_summary_and_result_files_and_exits_red(
    tmp_path, monkeypatch, capsys
):
    _tree(tmp_path, monkeypatch)
    runner = FakeRunner({"tests/test_a.py": [GREEN, _red("tests/test_a.py::t")]})
    monkeypatch.setattr(fs, "subprocess_runner", runner)
    summary_file = tmp_path / "step-summary.md"
    code = fs.main(
        [
            "run",
            "--files",
            "tests/test_*.py",
            "--repeat",
            "3",
            "--label",
            "Windows",
            "--out-dir",
            str(tmp_path / "out"),
            "--summary",
            str(summary_file),
        ]
    )
    assert code == 1
    assert "Flake sweep Windows: 3 runs per file" in summary_file.read_text("utf-8")
    assert "tests/test_a.py::t" in capsys.readouterr().out
    result = json.loads((tmp_path / "out" / "sweep-result.json").read_text("utf-8"))
    assert result[0]["runs"] == 3 and result[0]["failures"] == 2


def test_run_exits_green_only_when_every_run_passed(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fs, "subprocess_runner", FakeRunner({"tests/test_a.py": [GREEN]})
    )
    argv = ["run", "--files", "tests/test_a.py", "--out-dir", str(tmp_path / "o")]
    assert fs.main([*argv, "--repeat", "2"]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--files", "tests/test_gone.py"],
        ["--files", "tests/test_a.py", "--repeat", "0"],
        ["--files", "tests/test_a.py", "--repeat", "101"],
        ["--files", "   "],
    ],
)
def test_bad_input_is_a_usage_error_not_a_green_run(tmp_path, monkeypatch, argv):
    _tree(tmp_path, monkeypatch)
    monkeypatch.setattr(fs, "subprocess_runner", FakeRunner({}))
    assert fs.main(["run", *argv, "--out-dir", str(tmp_path / "o")]) == 2


def test_the_real_runner_reports_exit_code_output_and_timeouts():
    done = fs.subprocess_runner(
        [sys.executable, "-c", "print('hello'); raise SystemExit(3)"], 30
    )
    assert (done.returncode, done.timed_out) == (3, False) and "hello" in done.output
    slow = fs.subprocess_runner(
        [sys.executable, "-c", "import time; time.sleep(30)"], 1
    )
    assert slow.timed_out is True


# ── plan ───────────────────────────────────────────────────────────────────
def _matrix(capsys) -> list[dict]:
    line = capsys.readouterr().out.strip()
    assert line.startswith("matrix=")
    return json.loads(line[len("matrix=") :])["include"]


def test_plan_is_the_gates_integration_cells_filtered_by_os(capsys):
    gate = release_schema.plan_matrices("full")["integration"]["include"]
    assert fs.main(["plan"]) == 0
    assert _matrix(capsys) == gate
    assert {c["runner_os"] for c in gate} == {"Linux", "Windows", "macOS"}
    assert fs.main(["plan", "--oses", "Windows, macos"]) == 0
    assert {c["runner_os"] for c in _matrix(capsys)} == {"Windows", "macOS"}
    assert fs.main(["plan", "--oses", "linux"]) == 0
    assert [c["runner_os"] for c in _matrix(capsys)] == ["Linux"]


def test_plan_refuses_an_unknown_os(capsys):
    assert fs.main(["plan", "--oses", "linux,solaris"]) == 2
    assert "solaris" in capsys.readouterr().out


# ── the workflow ───────────────────────────────────────────────────────────
def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_sweep_is_dispatch_only_and_every_job_has_a_timeout():
    doc = _doc(SWEEP)
    assert list(doc[True]) == ["workflow_dispatch"], "never on a push, PR or schedule"
    assert set(doc[True]["workflow_dispatch"]["inputs"]) == {
        "files",
        "repeat",
        "oses",
        "marker",
        "deadline_minutes",
    }
    for name, job in doc["jobs"].items():
        assert 0 < job["timeout-minutes"] <= 240, name
        assert not job.get("continue-on-error"), name
        assert all(not s.get("continue-on-error") for s in job["steps"]), name
    sweep_job = doc["jobs"]["sweep"]
    assert sweep_job["strategy"]["fail-fast"] is False
    deadline = doc[True]["workflow_dispatch"]["inputs"]["deadline_minutes"]["default"]
    assert deadline < sweep_job["timeout-minutes"], "the deadline fires before GitHub"


def test_the_sweep_step_gets_its_inputs_through_env_and_its_artifact_always_uploads():
    steps = _doc(SWEEP)["jobs"]["sweep"]["steps"]
    run = next(s for s in steps if "flake_sweep.py run" in s.get("run", ""))
    for var in ("FILES", "REPEAT", "DEADLINE", "MARKER"):
        assert f'"${var}"' in run["run"], var
        assert "inputs." in run["env"][var], var
    assert "not transport" in run["env"]["MARKER"], "macOS keeps the gate's F-773 rule"
    upload = steps[-1]
    assert upload["if"] == "always()" and "flake-sweep" in upload["with"]["path"]


SETUP_STEPS = (
    "Install uv",
    "Set up Python 3.12",
    "Install dependencies",
    "Resolve image Chrome Stable identity",
    "Start Xvfb (Linux headed cases only)",
)


def test_the_setup_steps_are_the_gates_integration_steps():
    """The copy is the smallest defensible duplication; this keeps it a copy."""
    gate = {
        s["name"]: s for s in _doc(GATE)["jobs"]["integration"]["steps"] if "name" in s
    }
    sweep = {s["name"]: s for s in _doc(SWEEP)["jobs"]["sweep"]["steps"] if "name" in s}
    for name in SETUP_STEPS:
        assert sweep[name] == gate[name], f"{name!r} drifted from release-gate.yml"
    uploads = [s for s in _doc(SWEEP)["jobs"]["sweep"]["steps"] if "uses" in s]
    gate_upload = next(
        s["uses"]
        for s in _doc(GATE)["jobs"]["integration"]["steps"]
        if "artifact/upload" in s.get("uses", "")
    )
    assert uploads[-1]["uses"] == gate_upload, "same upload action, same pin"
