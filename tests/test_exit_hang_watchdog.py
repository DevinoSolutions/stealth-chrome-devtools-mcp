"""The exit-hang watchdog ends a stuck pytest process, loudly, with the right status.

Each case is a REAL ``python -m pytest`` subprocess with the watchdog loaded the way
conftest loads it (``-p exit_hang_watchdog``), a 3 s deadline, and a stuck
NON-daemon thread: the exact shape that held the macOS CI cells. Nothing here can
hang the suite: every inner process is run under ``subprocess.run(timeout=...)``,
which kills the child this test started, and the stuck thread itself sleeps a
bounded 120 s.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
DEADLINE = 3
BOUND = 45  # seconds: the subprocess must be over long before this


def _run(tmp_path: Path, body: str) -> tuple[subprocess.CompletedProcess, float]:
    (tmp_path / "test_inner.py").write_text(textwrap.dedent(body), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(TESTS),
        "EXIT_HANG_WATCHDOG_SECONDS": str(DEADLINE),
    }
    env.pop("PYTEST_ADDOPTS", None)
    started = time.monotonic()
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "exit_hang_watchdog",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            str(tmp_path / "test_inner.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=BOUND,
    )
    return done, time.monotonic() - started


_STUCK_THREAD = """
    import threading, time

    def _hold():
        time.sleep(120)

    threading.Thread(target=_hold, name="stuck-non-daemon", daemon=False).start()
"""


def _loud(done: subprocess.CompletedProcess) -> None:
    assert "exit-hang witness" in done.stderr, done.stderr
    assert "stuck-non-daemon" in done.stderr, done.stderr
    assert "most recent call first" in done.stderr, done.stderr
    assert "::warning title=exit-hang watchdog::" in done.stdout, done.stdout


def test_a_green_run_held_by_a_non_daemon_thread_ends_green(tmp_path):
    done, took = _run(
        tmp_path,
        _STUCK_THREAD
        + """
    def test_passes():
        assert True
    """,
    )
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    assert "1 passed" in done.stdout
    _loud(done)
    assert took < BOUND


def test_a_failing_run_held_by_a_non_daemon_thread_stays_red(tmp_path):
    done, took = _run(
        tmp_path,
        _STUCK_THREAD
        + """
    def test_fails():
        assert False
    """,
    )
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert "1 failed" in done.stdout
    _loud(done)
    assert took < BOUND


def test_a_last_test_that_never_returns_ends_the_run_red(tmp_path):
    """The hang is in the last test itself: the session never concluded, so the
    run is not "every test passed" and the status must say so."""
    done, took = _run(
        tmp_path,
        """
    import threading

    def test_first():
        assert True

    def test_last():
        threading.Event().wait(120)
    """,
    )
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert "exit-hang witness" in done.stderr
    assert "in the last test" in done.stderr
    assert "most recent call first" in done.stderr
    assert "::warning title=exit-hang watchdog::" in done.stdout
    assert took < BOUND


def test_a_normal_run_prints_nothing_and_is_not_slowed(tmp_path):
    done, took = _run(
        tmp_path,
        """
    def test_passes():
        assert True
    """,
    )
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    assert "exit-hang witness" not in done.stderr
    assert "::warning" not in done.stdout
    assert took < DEADLINE + 20


@pytest.mark.parametrize("name", ["TEST_SECONDS", "FINISH_SECONDS"])
def test_the_ci_defaults_are_the_documented_ones(name):
    """The override is for these pins only: unset, the deadlines are long enough
    that no honest test or terminal summary reaches them."""
    import exit_hang_watchdog

    if exit_hang_watchdog._OVERRIDE is None:
        assert getattr(exit_hang_watchdog, name) >= 120
