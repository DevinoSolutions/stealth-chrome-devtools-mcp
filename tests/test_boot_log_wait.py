"""T-9675 review: ``boot_log_when_settled`` watches the child it was told about.

``spawn`` hands back only a pid. On Windows a pid is reused, so a child that exited
before the first ``psutil.Process(pid)`` could leave the wait watching a stranger: a
broken redirect then failed after the full deadline with a false "still running", or
``psutil.AccessDenied`` errored the test. The identity check (the child's command line
must carry a string only it has) turns every such case into "gone", and a child that
really is stuck is killed before the failure.
"""

from __future__ import annotations

import os
import subprocess
import sys

import psutil
import pytest

from fakes import boot_log_when_settled


def test_a_pid_that_is_not_our_child_counts_as_gone(tmp_path):
    log = tmp_path / "boot.log"
    log.write_text("partial", encoding="utf-8")

    text = boot_log_when_settled(log, os.getpid(), "never", identity="not-in-argv-xyz")

    assert text == "partial"


def test_a_pid_with_no_process_counts_as_gone(tmp_path):
    log = tmp_path / "boot.log"
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()

    assert boot_log_when_settled(log, done.pid, "never", identity="-c") == ""


def test_the_needle_ends_the_wait_while_the_child_is_still_running(tmp_path):
    log = tmp_path / "boot.log"
    log.write_text("hello needle", encoding="utf-8")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        text = boot_log_when_settled(log, child.pid, "needle", identity="time.sleep")
        assert "needle" in text and child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_a_child_that_never_settles_is_killed_before_the_failure(tmp_path):
    log = tmp_path / "boot.log"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with pytest.raises(pytest.fail.Exception, match="still running"):
            boot_log_when_settled(
                log, child.pid, "never", identity="time.sleep", deadline=0.5
            )
        child.wait(10)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_a_child_that_settles_as_the_deadline_kill_lands_is_an_answer(
    tmp_path, monkeypatch
):
    """It wrote its traceback and exited between the last wait and the kill: the
    kill finds no process, and the log already holds the needle."""
    log = tmp_path / "boot.log"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def exited_just_now(self):
        log.write_text("Traceback ... needle", encoding="utf-8")
        raise psutil.NoSuchProcess(self.pid)

    monkeypatch.setattr(psutil.Process, "kill", exited_just_now)
    try:
        text = boot_log_when_settled(
            log, child.pid, "needle", identity="time.sleep", deadline=0.3
        )
        assert "needle" in text
    finally:
        monkeypatch.undo()
        if child.poll() is None:
            child.kill()


def test_a_stranger_is_never_killed_at_the_deadline(tmp_path):
    log = tmp_path / "boot.log"
    # Our own pid passes the "alive" test, fails identity: returns, no kill, no fail.
    assert (
        boot_log_when_settled(log, os.getpid(), "n", identity="nope-nope", deadline=0.2)
        == ""
    )
    assert psutil.Process(os.getpid()).is_running()


def test_a_transient_permission_error_on_read_is_retried(tmp_path, monkeypatch):
    log = tmp_path / "boot.log"
    log.write_text("ok needle", encoding="utf-8")
    real = type(log).read_text
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("an indexer holds the file")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(type(log), "read_text", flaky)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        text = boot_log_when_settled(log, child.pid, "needle", identity="time.sleep")
    finally:
        child.kill()
        child.wait()

    assert "needle" in text and calls["n"] == 3
