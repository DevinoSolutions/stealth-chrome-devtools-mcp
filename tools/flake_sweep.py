#!/usr/bin/env python3
"""Run chosen test files N times each as fresh pytest processes, and count the reds.

The release gate runs each cell's tests once, so an intermittent failure shows up
as one red cell on one tag. This is the measuring instrument for it: the
``flake-sweep`` workflow runs this on Linux, Windows and macOS and reads the table.

* ``plan``: print the ``matrix=`` line for ``$GITHUB_OUTPUT``. The cells are the
  release gate's own integration cells (``release_schema.plan_matrices``), so a
  sweep can only ever run where the gate runs, on the same runner labels.
* ``run``: for every file matching the patterns, start ``--repeat`` NEW pytest
  processes (never one process looping, so import-time and fixture state is
  fresh each time), keep every log and junit file, and NEVER stop at the first
  failure. Exit 0 only when every run of every file passed.

A file with no test selected by ``--marker`` is reported as such and not run again;
it is not a failure, but a sweep that selected nothing anywhere is (exit 2), because
a green run that measured nothing is the worst answer. When ``--deadline-minutes``
passes, no further run starts, the unrun ones are listed, and the exit is 1: an
incomplete sweep is not a pass. Stdlib only, and pytest is reached through the
injectable ``Runner`` so ``tests/test_flake_sweep.py`` is hermetic.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

NO_TESTS_SELECTED = 5  # pytest's exit status when nothing matched ``-m``
MAX_REPEAT = 100
#: ``FAILED tests/x.py::t[a - b] - msg`` / ``ERROR tests/x.py - msg`` (``-rfE``).
_SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - |$)", re.MULTILINE)
_OS_BY_TOKEN = {"linux": "Linux", "windows": "Windows", "macos": "macOS"}


@dataclass(frozen=True)
class RunResult:
    returncode: int
    output: str
    timed_out: bool = False


#: ``(argv, timeout_seconds) -> RunResult``: the seam tests replace.
Runner = Callable[[list[str], float], RunResult]


def subprocess_runner(argv: list[str], timeout: float) -> RunResult:
    try:
        done = subprocess.run(  # noqa: S603  PERMANENT(CI tool; argv is built here from a validated file path and flags, no shell)
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return RunResult(-1, partial, timed_out=True)
    return RunResult(done.returncode, done.stdout + done.stderr)


@dataclass
class FileOutcome:
    file: str
    planned: int
    runs: int = 0
    failures: int = 0
    no_tests: bool = False
    failing: Counter[str] = field(default_factory=Counter)

    @property
    def not_run(self) -> int:
        return 0 if self.no_tests else self.planned - self.runs


def failing_ids(result: RunResult, timeout: float) -> list[str]:
    """The node ids one red run names, or one label saying why it has none."""
    ids = list(dict.fromkeys(_SUMMARY_LINE.findall(result.output)))
    if ids:
        return ids
    if result.timed_out:
        return [f"<run exceeded {timeout:g}s and was killed>"]
    return [f"<pytest exit {result.returncode}, no failed node id in the log>"]


def expand(patterns: list[str]) -> list[str]:
    """Existing files matching ``patterns``, sorted; a pattern matching none raises."""
    found: list[str] = []
    for pattern in patterns:
        try:
            hits = sorted(p for p in Path().glob(pattern) if p.is_file())
        except (NotImplementedError, ValueError) as exc:  # absolute or empty pattern
            raise ValueError(f"pattern {pattern!r} is not a relative glob") from exc
        if not hits:
            raise ValueError(f"pattern {pattern!r} matches no file")
        found.extend(hit.as_posix() for hit in hits)
    return list(dict.fromkeys(found))


def pytest_argv(
    file: str, marker: str, junit: Path, test_timeout: int, python: str = sys.executable
) -> list[str]:
    return [
        python,
        "-m",
        "pytest",
        file,
        "-m",
        marker,
        "-v",
        "--tb=short",
        "-rfE",
        "--color=no",
        f"--timeout={test_timeout}",
        "-o",
        "faulthandler_timeout=240",
        f"--junitxml={junit}",
        "-o",
        "junit_family=xunit1",
        "-p",
        "no:cacheprovider",
    ]


def sweep(  # noqa: PLR0913  PERMANENT(each argument is one knob of the sweep; the seams for the runner and clock are what make it hermetic)
    files: list[str],
    *,
    repeat: int,
    marker: str,
    out_dir: Path,
    runner: Runner = subprocess_runner,
    run_timeout: float = 1500,
    test_timeout: int = 300,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[FileOutcome]:
    outcomes: list[FileOutcome] = []
    for file in files:
        outcome = FileOutcome(file, repeat)
        outcomes.append(outcome)
        slug = out_dir / file.replace("/", "__")
        slug.mkdir(parents=True, exist_ok=True)
        for number in range(1, repeat + 1):
            if deadline is not None and clock() >= deadline:
                break
            junit = slug / f"run-{number:02d}.xml"
            result = runner(pytest_argv(file, marker, junit, test_timeout), run_timeout)
            (slug / f"run-{number:02d}.log").write_text(
                result.output, encoding="utf-8", errors="replace"
            )
            if result.returncode == NO_TESTS_SELECTED and not result.timed_out:
                outcome.no_tests = True
                break
            outcome.runs += 1
            if result.returncode != 0 or result.timed_out:
                outcome.failures += 1
                outcome.failing.update(failing_ids(result, run_timeout))
    return outcomes


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def summary(outcomes: list[FileOutcome], *, title: str) -> str:
    lines = [
        f"### {title}",
        "",
        "| file | runs | failures | failing node ids |",
        "|---|---|---|---|",
    ]
    for o in outcomes:
        if o.no_tests:
            lines.append(f"| `{o.file}` | 0 | 0 | no test selected by the marker |")
            continue
        runs = str(o.runs) if not o.not_run else f"{o.runs} of {o.planned} (deadline)"
        ids = "<br>".join(
            f"`{_cell(node)}` ({n}/{o.runs})" for node, n in o.failing.most_common()
        )
        lines.append(f"| `{o.file}` | {runs} | {o.failures} | {ids or '-'} |")
    ran = [o for o in outcomes if not o.no_tests]
    lines += [
        "",
        f"{sum(o.runs for o in ran)} runs of {len(ran)} files, "
        f"{sum(o.failures for o in ran)} red, "
        f"{sum(o.not_run for o in ran)} not run, "
        f"{len(outcomes) - len(ran)} files with no test selected.",
        "",
    ]
    return "\n".join(lines)


def exit_status(outcomes: list[FileOutcome]) -> int:
    """0 all green; 1 any red or unrun; 2 nothing was measured at all."""
    ran = [o for o in outcomes if not o.no_tests]
    if not ran:
        return 2
    return 1 if any(o.failures or o.not_run for o in ran) else 0


def cmd_plan(args: argparse.Namespace) -> int:
    from release_schema import plan_matrices

    tokens = [t for t in re.split(r"[\s,]+", args.oses.lower()) if t]
    unknown = sorted(set(tokens) - set(_OS_BY_TOKEN) - {"all"})
    if unknown:
        print(f"flake_sweep: unknown os {unknown}; use linux, windows, macos or all")
        return 2
    wanted = (
        set(_OS_BY_TOKEN.values())
        if not tokens or "all" in tokens
        else {_OS_BY_TOKEN[t] for t in tokens}
    )
    cells = [
        c
        for c in plan_matrices("full")["integration"]["include"]
        if c["runner_os"] in wanted
    ]
    print("matrix=" + json.dumps({"include": cells}, separators=(",", ":")))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not 1 <= args.repeat <= MAX_REPEAT:
        print(f"flake_sweep: --repeat must be 1..{MAX_REPEAT}")
        return 2
    try:
        files = expand(args.files.split())
    except ValueError as exc:
        print(f"flake_sweep: {exc}")
        return 2
    if not files:
        print("flake_sweep: no file patterns given")
        return 2
    started = time.monotonic()
    deadline = started + args.deadline_minutes * 60 if args.deadline_minutes else None
    outcomes = sweep(
        files,
        repeat=args.repeat,
        marker=args.marker,
        out_dir=args.out_dir,
        runner=subprocess_runner,
        run_timeout=args.run_timeout,
        test_timeout=args.test_timeout,
        deadline=deadline,
    )
    title = (
        f"Flake sweep {args.label}: {args.repeat} runs per file, "
        f"-m `{args.marker}`, {(time.monotonic() - started) / 60:.0f} min"
    )
    text = summary(outcomes, title=title)
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(text)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "sweep-result.json").write_text(
        json.dumps(
            [
                {
                    "file": o.file,
                    "planned": o.planned,
                    "runs": o.runs,
                    "failures": o.failures,
                    "no_tests": o.no_tests,
                    "failing": dict(o.failing),
                }
                for o in outcomes
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return exit_status(outcomes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print the matrix= line for $GITHUB_OUTPUT")
    plan.add_argument("--oses", default="all", help="linux, windows, macos or all")
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="run the sweep on this machine")
    run.add_argument("--files", required=True, help="whitespace-separated globs")
    run.add_argument("--repeat", type=int, default=10)
    run.add_argument("--marker", default="integration")
    run.add_argument("--out-dir", type=Path, default=Path("flake-sweep"))
    run.add_argument("--summary", type=Path, default=None, help="append markdown here")
    run.add_argument("--label", default="", help="shown in the summary title")
    run.add_argument("--run-timeout", type=float, default=1500, help="seconds per run")
    run.add_argument("--test-timeout", type=int, default=300, help="seconds per test")
    run.add_argument("--deadline-minutes", type=float, default=0, help="0 = none")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
