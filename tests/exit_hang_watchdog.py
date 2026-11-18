"""The exit-hang witness and watchdog: ONE home (T-15386, T-17254).

Registered by ``tests/conftest.py`` as a plugin; ``-p exit_hang_watchdog`` loads
the same module for the subprocess pins in ``tests/test_exit_hang_watchdog.py``.

The history. The suite passed 100% on macOS/ARM64 CI and then never finished: the
job hit its 15-minute limit and was CANCELLED, and a cancelled job's log is not
served (``BlobNotFound``), so nobody could read what held the process. #405 added a
witness that armed at ``pytest_unconfigure``. It never fired. The logs that DO
survive (the steps' ``/jobs/<id>/logs`` for runs 43895679866 / 44308489398) show
why: the hang is EARLIER than ``pytest_unconfigure``. The coverage cell's log ends
right after ``Coverage XML written`` (no ``Required test coverage`` line, no
``=== N passed ===``), and the unit cell's log ends one test before the last one's
verdict. Neither shows the witness' stderr line nor its 60 s stack dump, so the
interpreter never got as far as unconfigure.

So the watchdog is armed where the hang is, and it ends the process instead of
waiting for the runner's cancel to throw the evidence away:

* the LAST test's setup (covers its body, its teardown, the session-scoped
  fixtures' teardown) gets ``TEST_SECONDS``;
* ``pytest_sessionfinish`` (terminal summary, coverage report, junit) and then the
  interpreter's own exit (non-daemon threads are joined after ``pytest_unconfigure``)
  get ``FINISH_SECONDS``.

On expiry it writes, to stderr, every thread's stack, the non-daemon threads and
the child processes; prints a GitHub ``::warning::`` annotation (so the hang is
never silent); and ends the process with ``os._exit``. The status is pytest's OWN
once the session has concluded (``session.exitstatus`` is settled before
``pytest_sessionfinish`` starts), so a hang after a green run does not fail the
run, and a hang after a red one never turns it green. A hang BEFORE the session
concluded (the last test itself) exits non-zero: that run is not "every test
passed". A hang that holds the GIL, where no Python thread can run, is ended by
``faulthandler`` (exit status 1, with the stacks) ``GIL_GRACE_SECONDS`` later.

Not covered: a stall in a test that is not the last one (a mid-run stall at 95% was
seen once). A per-test deadline would kill a developer sitting in ``pdb``, and the
integration lanes already bound each test with ``--timeout``.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time

import pytest

# Overridable only so the subprocess pins can run in seconds, never in CI.
_OVERRIDE = os.environ.get("EXIT_HANG_WATCHDOG_SECONDS")
# On CI the job limit is 15 minutes and the suite takes up to 11 of them, so the
# last test gets 180 s there (it takes about 5 s): 600 s would lose to the runner's
# cancel, which is the very thing this module exists to beat. Locally it stays
# 600 s, for a developer sitting in ``pdb`` on the last test.
_LAST_TEST_DEFAULT = 180.0 if os.environ.get("CI") else 600.0
TEST_SECONDS = float(_OVERRIDE) if _OVERRIDE else _LAST_TEST_DEFAULT
FINISH_SECONDS = float(_OVERRIDE) if _OVERRIDE else 120.0
GIL_GRACE_SECONDS = 30.0

#: The status of a hang before the session concluded: pytest's TESTS_FAILED.
HUNG_BEFORE_THE_SESSION_CONCLUDED = 1

_state: dict = {"deadline": None, "session": None, "finishing": False}
#: The REAL stdout/stderr descriptors, duplicated while pytest's capture is
#: suspended (``pytest_configure``). During a test ``sys.stderr`` and fd 2 are
#: pytest's capture file, and a process ended with ``os._exit`` never flushes it:
#: everything the watchdog wrote there would be lost.
_fds: dict[str, int] = {}
_thread: threading.Thread | None = None
_wake = threading.Event()


def _non_daemon_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread is not threading.main_thread()
        and thread.is_alive()
        and not thread.daemon
    ]


def _children() -> list[str]:
    try:
        import psutil

        return [
            f"pid {child.pid} {child.name()}"
            for child in psutil.Process().children(recursive=True)
        ]
    except Exception as exc:
        return [f"unreadable ({type(exc).__name__}: {exc})"]


def _say(stream: str, text: str) -> None:
    os.write(_fds[stream], text.encode("utf-8", "replace"))


def _status() -> int:
    """pytest's own status once the session concluded, else a failure."""
    session = _state["session"]
    if _state["finishing"] and session is not None:
        return int(session.exitstatus)
    return HUNG_BEFORE_THE_SESSION_CONCLUDED


def _fire() -> None:
    status = _status()
    held = ", ".join(f"{t.name} (ident={t.ident})" for t in _non_daemon_threads())
    where = "after the session concluded" if _state["finishing"] else "in the last test"
    _say(
        "stderr",
        f"exit-hang witness: the process was still alive {where}; "
        f"non-daemon threads: {held or 'none'}; "
        f"children: {', '.join(_children()) or 'none'}; "
        f"ending it with status {status} (pytest's own where it had concluded)\n",
    )
    faulthandler.dump_traceback(file=_fds["stderr"], all_threads=True)
    _say(
        "stdout",
        f"::warning title=exit-hang watchdog::the pytest process hung {where} "
        f"and was ended with status {status}; the stacks are in this step's stderr\n",
    )
    os._exit(status)


def _watch() -> None:
    while True:
        deadline = _state["deadline"]
        if deadline is None:
            _wake.wait()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fire()
            _wake.wait(remaining)
        _wake.clear()


def arm(seconds: float) -> None:
    """(Re)start the countdown; the previous one is replaced, not added to."""
    global _thread
    _state["deadline"] = time.monotonic() + seconds
    faulthandler.dump_traceback_later(
        seconds + GIL_GRACE_SECONDS, repeat=False, file=_fds["stderr"], exit=True
    )
    if _thread is None:
        _thread = threading.Thread(
            target=_watch, name="exit-hang-watchdog", daemon=True
        )
        _thread.start()
    _wake.set()


def pytest_configure(config):
    # Registered from conftest's own ``pytest_configure`` this is still called:
    # pytest replays the historic hook to a plugin registered while configuring.
    if not _fds:
        _fds["stderr"] = os.dup(2)
        _fds["stdout"] = os.dup(1)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    # ``nextitem`` is not passed to setup: the last item is the one whose
    # protocol ends the run, which ``pytest_runtest_protocol`` below marks.
    if item is _state.get("last"):
        arm(TEST_SECONDS)
    yield


def pytest_collection_finish(session):
    _state["last"] = session.items[-1] if session.items else None


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session):
    _state["session"] = session
    _state["finishing"] = True
    arm(FINISH_SECONDS)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    held = _non_daemon_threads()
    if held:
        sys.stderr.write(
            "exit-hang witness: non-daemon threads alive at session end: "
            + ", ".join(f"{thread.name} (ident={thread.ident})" for thread in held)
            + "\n"
        )
