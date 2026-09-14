"""F-867: the three rungs of the backend spawn, and what each one hands over.

``tests/test_backend_escapes_client_job.py`` is the real-process pin — it builds
the MCP SDK's kill-on-close Job Object and proves a real backend outlives it.
This file is the hermetic half: no scheduled task is ever created (the ONE
``schtasks`` seam is faked), no backend is ever started (``subprocess.Popen`` is
faked), and ``sys.platform`` is monkeypatched so both OS branches are exercised
on every OS the gate runs.

The one exception is the last class, which RUNS the launcher script for real
with this interpreter: what the intermediary does with the spec is the part a
double cannot check, and F-303 (an import-time crash reaches
``backend-boot.log``) has to survive the hand-off through Task Scheduler.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_launch,
    backend_registry,
    desktop_launch,
)

_PYTHONW = "C:/base/pythonw.exe"


class FakePopen:
    """Records every spawn, and the teardown of any spawn that was discarded.

    ``_handle`` is deliberately absent: ``_proven_in_a_job`` can only prove
    membership through a real process handle, and "not proven" leaves the spawn
    standing — so an unpatched fake takes the breakaway rung, which is what the
    rung-order tests want to see.
    """

    def __init__(
        self,
        deny_first_winerror: int | None = None,
        message: str = "Access is denied",
        deny_errno: int = errno.EACCES,
    ):
        self.calls: list[dict] = []
        self._deny = deny_first_winerror
        self._message = message
        self._errno = deny_errno
        self.pid = 4242
        self.killed = 0
        self.waited: list[float] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        if self._deny is not None and len(self.calls) == 1:
            raise OSError(self._errno, self._message, None, self._deny)
        return self

    def kill(self):  # a discarded breakaway spawn
        self.killed += 1

    def wait(self, timeout=None):
        self.waited.append(timeout)
        return 1


class FakeSchtasks:
    """Plays the scheduler: on ``/Run`` it does what the launcher would do."""

    def __init__(self, *, create_rc: int = 0, run_rc: int = 0, effect: str = "pid"):
        self.calls: list[list[str]] = []
        self.command: str | None = None
        self._create_rc = create_rc
        self._run_rc = run_rc
        self._effect = effect
        self.pid = 7373

    @property
    def deleted(self) -> list[str]:
        return [
            call[call.index("/TN") + 1] for call in self.calls if call[0] == "/Delete"
        ]

    def _script(self) -> Path:
        assert self.command is not None
        return Path(self.command.split('" "')[1].rstrip('"'))

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        verb, rc = args[0], 0
        if verb == "/Create":
            self.command = args[args.index("/TR") + 1]
            rc = self._create_rc
        elif verb == "/Run":
            rc = self._run_rc
            if rc == 0 and self._effect == "pid":
                self._script().with_suffix(".pid").write_text(str(self.pid))
            elif rc == 0 and self._effect == "err":
                self._script().with_suffix(".err").write_text("boom in the launcher")
        return subprocess.CompletedProcess(args, rc, "", "schtasks says no")


@pytest.fixture()
def windows(tmp_path, monkeypatch):
    """A Windows spawn whose every seam is a double, on any host OS."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backend_registry, "STATE_DIR", tmp_path)
    monkeypatch.setattr(backend_launch, "_same_session_as_console", lambda: True)
    monkeypatch.setattr(
        backend_launch, "_intermediary_interpreter", lambda _cmd0: Path(_PYTHONW)
    )
    monkeypatch.setattr(backend_launch._logger, "propagate", True)
    return tmp_path


@pytest.fixture()
def in_a_job(monkeypatch):
    """The client's job holds on: the breakaway spawn is provably still inside
    a job, so rung 1 discards it (the measured nested-job case)."""
    monkeypatch.setattr(backend_launch, "_proven_in_a_job", lambda _proc: True)


def _spawn(popen, schtasks, monkeypatch, boot_log=None, cmd=None, env=None):
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(desktop_launch, "_schtasks", schtasks)
    return backend_launch.spawn(
        cmd or ["C:/base/python.exe", "-m", "pkg"], env or {"A": "1"}, boot_log
    )


# ---------------------------------------------------------------------------
# Rung order.
# ---------------------------------------------------------------------------


def test_the_breakaway_rung_serves_and_never_touches_the_scheduler(
    windows, monkeypatch
):
    popen, schtasks = FakePopen(), FakeSchtasks()
    launched = _spawn(popen, schtasks, monkeypatch)

    assert launched == backend_launch.Launched(4242, "breakaway")
    assert len(popen.calls) == 1
    assert popen.calls[0]["creationflags"] == (
        backend_launch._DETACHED_FLAGS | backend_launch._CREATE_BREAKAWAY_FROM_JOB
    )
    assert schtasks.calls == []


def test_a_refused_breakaway_falls_to_the_scheduler(windows, monkeypatch):
    popen = FakePopen(deny_first_winerror=backend_launch._ACCESS_DENIED)
    schtasks = FakeSchtasks()
    launched = _spawn(popen, schtasks, monkeypatch)

    assert launched == backend_launch.Launched(7373, "scheduler")
    # Exactly one Popen attempt: the refused one. The scheduler made the backend.
    assert len(popen.calls) == 1
    # The orphan sweep reads the launch dir, so a clean dir costs no schtasks
    # call at all — only this spawn's own three.
    assert [call[0] for call in schtasks.calls] == ["/Create", "/Run", "/Delete"]


def test_a_partial_breakaway_is_discarded_when_rung_2_can_do_better(
    windows, in_a_job, monkeypatch
):
    popen, schtasks = FakePopen(), FakeSchtasks()
    launched = _spawn(popen, schtasks, monkeypatch)

    assert launched.rung == "scheduler"
    # Killed AND waited for: the scheduler is about to start a backend on the
    # same port, so a discarded one still exiting would race it.
    assert popen.killed == 1
    assert popen.waited == [backend_launch.DISCARD_WAIT_SECONDS]


def test_a_partial_breakaway_is_kept_when_rung_2_could_not_do_better(
    windows, in_a_job, monkeypatch, caplog
):
    # Out of one job beats out of none: discarding it for a plain spawn would
    # replace it with something strictly worse.
    monkeypatch.setattr(backend_launch, "_same_session_as_console", lambda: False)
    popen, schtasks = FakePopen(), FakeSchtasks()
    with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
        launched = _spawn(popen, schtasks, monkeypatch)

    assert launched == backend_launch.Launched(4242, "breakaway-partial")
    assert popen.killed == 0
    assert len(popen.calls) == 1
    assert schtasks.calls == []
    assert "F-867" in caplog.text
    assert "escaped the innermost job only" in caplog.text


def test_the_plain_rung_keeps_a_breakaway_that_was_proven_permitted(
    windows, in_a_job, monkeypatch, caplog
):
    # Rung 1 did not raise, so the flag IS allowed here; rung 2 then failed at
    # runtime. Rung 3 must not spawn with fewer flags than we know work.
    popen, schtasks = FakePopen(), FakeSchtasks(create_rc=1)
    with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
        launched = _spawn(popen, schtasks, monkeypatch)

    assert launched.rung == "plain"
    assert popen.calls[-1]["creationflags"] == (
        backend_launch._DETACHED_FLAGS | backend_launch._CREATE_BREAKAWAY_FROM_JOB
    )


def test_the_plain_rung_drops_the_flag_that_was_refused(windows, monkeypatch):
    popen = FakePopen(deny_first_winerror=backend_launch._ACCESS_DENIED)
    launched = _spawn(popen, FakeSchtasks(create_rc=1), monkeypatch)

    assert launched.rung == "plain"
    assert popen.calls[-1]["creationflags"] == backend_launch._DETACHED_FLAGS


def test_any_other_oserror_is_a_real_spawn_failure(windows, monkeypatch):
    popen = FakePopen(
        deny_first_winerror=2,  # ERROR_FILE_NOT_FOUND
        message="The system cannot find the file specified",
        deny_errno=errno.ENOENT,
    )
    with pytest.raises(OSError, match="cannot find the file"):
        _spawn(popen, FakeSchtasks(), monkeypatch)


def test_the_posix_branch_is_the_one_it_always_was(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    popen = FakePopen()
    launched = _spawn(popen, FakeSchtasks(), monkeypatch)

    assert launched == backend_launch.Launched(4242, "posix")
    assert popen.calls[0]["start_new_session"] is True
    assert popen.calls[0]["creationflags"] == 0


# ---------------------------------------------------------------------------
# What crosses to the scheduler.
# ---------------------------------------------------------------------------


class TestHandOff:
    @pytest.fixture()
    def captured(self, windows, monkeypatch):
        """Run one scheduler spawn, keeping the spec the launcher would read."""
        seen: dict[str, object] = {}

        class Capturing(FakeSchtasks):
            def __call__(self, args):
                if args[0] == "/Run":
                    script = self._script()
                    seen["script"] = script.read_text(encoding="utf-8")
                    seen["spec"] = json.loads(
                        script.with_suffix(".json").read_text(encoding="utf-8")
                    )
                return super().__call__(args)

        schtasks = Capturing()
        popen = FakePopen(deny_first_winerror=backend_launch._ACCESS_DENIED)
        launched = _spawn(
            popen,
            schtasks,
            monkeypatch,
            boot_log=windows / "logs" / "backend-boot.log",
            cmd=["C:/base/python.exe", "-m", "pkg", "--port", "9"],
            env={"PATH": "C:/win", "__PYVENV_LAUNCHER__": "C:/v/python.exe"},
        )
        seen["launched"] = launched
        seen["schtasks"] = schtasks
        seen["dir"] = windows / backend_launch.LAUNCH_DIR_NAME
        return seen

    def test_the_whole_child_env_crosses_key_for_key(self, captured):
        assert captured["spec"]["env"] == {
            "PATH": "C:/win",
            "__PYVENV_LAUNCHER__": "C:/v/python.exe",
        }

    def test_the_argv_and_boot_log_cross_unchanged(self, captured, windows):
        assert captured["spec"]["cmd"] == [
            "C:/base/python.exe",
            "-m",
            "pkg",
            "--port",
            "9",
        ]
        assert captured["spec"]["boot_log"] == str(
            windows / "logs" / "backend-boot.log"
        )

    def test_the_working_directory_crosses_too(self, captured):
        # A scheduled task starts in system32; clone_storage's last-resort clone
        # seed reads the working directory, so it has to be the spawner's.
        assert captured["spec"]["cwd"] == os.getcwd()

    def test_the_launcher_is_the_shipped_script(self, captured):
        assert captured["script"] == backend_launch._LAUNCHER_SCRIPT

    def test_the_task_command_quotes_both_paths(self, captured):
        command = captured["schtasks"].command
        assert command.startswith(f'"{Path(_PYTHONW)}" "')
        assert command.endswith('.py"')

    def test_nothing_is_left_behind_on_success(self, captured):
        assert list(captured["dir"].iterdir()) == []
        assert "/Delete" in [call[0] for call in captured["schtasks"].calls]


def test_a_missing_boot_log_crosses_as_null(windows, monkeypatch):
    seen: dict[str, object] = {}

    class Capturing(FakeSchtasks):
        def __call__(self, args):
            if args[0] == "/Run":
                seen["spec"] = json.loads(
                    self._script().with_suffix(".json").read_text(encoding="utf-8")
                )
            return super().__call__(args)

    _spawn(
        FakePopen(deny_first_winerror=backend_launch._ACCESS_DENIED),
        Capturing(),
        monkeypatch,
        boot_log=None,
    )
    assert seen["spec"]["boot_log"] is None


# ---------------------------------------------------------------------------
# Every way the scheduler rung can fail lands on the plain rung, loudly.
# ---------------------------------------------------------------------------


class TestFallbacks:
    def _run(self, monkeypatch, schtasks, tmp_path):
        popen = FakePopen(deny_first_winerror=backend_launch._ACCESS_DENIED)
        launched = _spawn(popen, schtasks, monkeypatch)
        return launched, popen

    def test_a_create_failure_falls_back_and_cleans_up(
        self, windows, monkeypatch, caplog
    ):
        schtasks = FakeSchtasks(create_rc=1)
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            launched, popen = self._run(monkeypatch, schtasks, windows)

        assert launched.rung == "plain"
        assert popen.calls[-1]["creationflags"] == backend_launch._DETACHED_FLAGS
        assert "F-867" in caplog.text
        assert "schtasks says no" in caplog.text
        assert list((windows / backend_launch.LAUNCH_DIR_NAME).iterdir()) == []
        assert "/Delete" in [call[0] for call in schtasks.calls]

    def test_a_run_failure_falls_back(self, windows, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            launched, _ = self._run(monkeypatch, FakeSchtasks(run_rc=1), windows)

        assert launched.rung == "plain"
        assert "F-867" in caplog.text

    def test_a_pid_that_never_arrives_falls_back(self, windows, monkeypatch, caplog):
        monkeypatch.setattr(backend_launch, "PID_READY_TIMEOUT", 0.2)
        monkeypatch.setattr(backend_launch, "POLL_INTERVAL", 0.01)
        schtasks = FakeSchtasks(effect="nothing")
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            launched, _ = self._run(monkeypatch, schtasks, windows)

        assert launched.rung == "plain"
        assert "never handed back a pid" in caplog.text
        assert list((windows / backend_launch.LAUNCH_DIR_NAME).iterdir()) == []

    def test_a_launcher_traceback_falls_back_and_is_reported(
        self, windows, monkeypatch, caplog
    ):
        monkeypatch.setattr(backend_launch, "POLL_INTERVAL", 0.01)
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            launched, _ = self._run(monkeypatch, FakeSchtasks(effect="err"), windows)

        assert launched.rung == "plain"
        assert "boom in the launcher" in caplog.text

    def test_a_command_past_the_tr_cap_falls_back(self, windows, monkeypatch, caplog):
        # schtasks STORES only the first 253 characters of /TR and still exits 0;
        # a truncated command starts nothing at all, so the cap is a rung
        # boundary, not a warning.
        monkeypatch.setattr(backend_launch, "TR_MAX_CHARS", 10)
        schtasks = FakeSchtasks()
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            launched, _ = self._run(monkeypatch, schtasks, windows)

        assert launched.rung == "plain"
        assert schtasks.calls == []
        assert "schtasks truncates at" in caplog.text

    def test_another_session_means_no_scheduled_task_at_all(
        self, windows, monkeypatch, caplog
    ):
        monkeypatch.setattr(backend_launch, "_same_session_as_console", lambda: False)
        schtasks = FakeSchtasks()
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            launched, _ = self._run(monkeypatch, schtasks, windows)

        assert launched.rung == "plain"
        assert schtasks.calls == []

    def test_only_a_venv_redirector_pythonw_means_no_scheduled_task(
        self, windows, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            backend_launch, "_intermediary_interpreter", lambda _c: None
        )
        schtasks = FakeSchtasks()
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            launched, _ = self._run(monkeypatch, schtasks, windows)

        assert launched.rung == "plain"
        assert schtasks.calls == []
        assert "F-866" in caplog.text


def _launch_dir_for(command_length: int) -> Path:
    """A launch dir whose /TR command comes out exactly *command_length* long.

    Built by search rather than arithmetic so a miscounted quote shows up as a
    failure to construct the case, not as a test that quietly checks the wrong
    length.
    """
    interpreter = Path(_PYTHONW)
    for pad in range(1, 400):
        candidate = Path("C:/" + "d" * pad)
        token = "a" * backend_launch.TOKEN_CHARS
        if len(f'"{interpreter}" "{candidate / token}.py"') == command_length:
            return candidate
    raise AssertionError(f"no launch dir gives a {command_length}-char command")


class TestTheStoredCommandLengthCap:
    """253 is what schtasks STORES, measured on Windows 11 10.0.26200: a
    255-character command was accepted (/Create exit 0) and stored as its first
    253 characters, so the task ran pythonw against a truncated path and every
    herd session waited out the pid deadline."""

    def test_a_command_of_exactly_the_cap_is_accepted(self, windows, monkeypatch):
        monkeypatch.setattr(
            backend_launch,
            "_launch_dir",
            lambda: _launch_dir_for(backend_launch.TR_MAX_CHARS),
        )
        plan = backend_launch._scheduler_plan(_PYTHONW)

        assert plan is not None
        assert len(plan.command) == backend_launch.TR_MAX_CHARS

    def test_one_character_more_is_refused(self, windows, monkeypatch, caplog):
        over = backend_launch.TR_MAX_CHARS + 1
        monkeypatch.setattr(
            backend_launch, "_launch_dir", lambda: _launch_dir_for(over)
        )
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            assert backend_launch._scheduler_plan(_PYTHONW) is None

        assert str(over) in caplog.text
        assert str(backend_launch.TR_MAX_CHARS) in caplog.text

    def test_the_real_state_dir_leaves_room(self, windows, monkeypatch):
        # The shipped layout, not a constructed one: ~/.stealth-mcp with a
        # 12-character token has to fit, or the scheduler rung never runs.
        monkeypatch.setattr(backend_registry, "STATE_DIR", Path.home() / ".stealth-mcp")
        plan = backend_launch._scheduler_plan(_PYTHONW)

        assert plan is not None
        assert len(plan.command) <= backend_launch.TR_MAX_CHARS


class TestOrphanTaskSweep:
    """A spawner killed mid-launch leaves BOTH its task and its spec behind (the
    teardown deletes the task first), so age is the predicate, not absence."""

    def _seed(self, launch_dir: Path, token: str, age: float) -> list[Path]:
        launch_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for suffix in (".json", ".py", ".pid"):
            path = launch_dir / f"{token}{suffix}"
            path.write_text("{}", encoding="utf-8")
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
            written.append(path)
        return written

    def test_a_spec_older_than_the_pid_deadline_takes_its_task_with_it(
        self, windows, monkeypatch
    ):
        launch_dir = windows / backend_launch.LAUNCH_DIR_NAME
        stale = self._seed(
            launch_dir, "bbbbbbbb", backend_launch.STALE_SPEC_SECONDS + 30
        )
        schtasks = FakeSchtasks()
        popen = FakePopen(deny_first_winerror=backend_launch._ACCESS_DENIED)
        launched = _spawn(popen, schtasks, monkeypatch)

        assert launched.rung == "scheduler"
        assert f"{backend_launch.TASK_PREFIX}bbbbbbbb" in schtasks.deleted
        assert [path for path in stale if path.exists()] == []

    def test_a_fresh_spec_belongs_to_a_live_sibling_and_is_untouched(
        self, windows, monkeypatch
    ):
        launch_dir = windows / backend_launch.LAUNCH_DIR_NAME
        live = self._seed(launch_dir, "aaaaaaaa", 1.0)
        schtasks = FakeSchtasks()
        popen = FakePopen(deny_first_winerror=backend_launch._ACCESS_DENIED)
        _spawn(popen, schtasks, monkeypatch)

        assert f"{backend_launch.TASK_PREFIX}aaaaaaaa" not in schtasks.deleted
        assert all(path.exists() for path in live)

    def test_a_clean_launch_dir_costs_no_schtasks_call(self, windows, monkeypatch):
        # The normal case. A /Query here measured 0.85-0.89 s per cold start.
        schtasks = FakeSchtasks()
        popen = FakePopen(deny_first_winerror=backend_launch._ACCESS_DENIED)
        _spawn(popen, schtasks, monkeypatch)

        assert [call[0] for call in schtasks.calls] == ["/Create", "/Run", "/Delete"]

    def test_an_unreadable_launch_dir_never_stops_the_spawn(self, windows, monkeypatch):
        def explode(*_args, **_kwargs):
            raise OSError("no")

        monkeypatch.setattr(Path, "glob", explode)
        popen = FakePopen(deny_first_winerror=backend_launch._ACCESS_DENIED)
        launched = _spawn(popen, FakeSchtasks(), monkeypatch)

        assert launched.rung == "scheduler"


# ---------------------------------------------------------------------------
# The membership probe that decides whether rung 1 escaped.
# ---------------------------------------------------------------------------


class TestProvenInAJob:
    """The probe rung 1 trusts. The fakes elsewhere never reach it (they carry
    no process handle) and the real pin never reaches it either (its client job
    refuses breakaway), so it needs its own coverage against real handles."""

    def test_a_double_with_no_process_handle_is_not_proof(self, caplog):
        # Silent by design: a real Windows Popen always has ``_handle``, so this
        # branch means a test double, not a probe that failed.
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            assert backend_launch._proven_in_a_job(FakePopen()) is False
        assert caplog.text == ""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
    def test_a_real_process_in_our_own_job_reads_as_a_member(self, tmp_path):
        import ctypes
        from ctypes import wintypes

        from test_backend_escapes_client_job import _make_kill_on_close_job

        kernel = ctypes.windll.kernel32
        job = _make_kill_on_close_job()
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        try:
            assert kernel.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))
            )
            # True either way if the harness itself is inside a job, so what
            # this pins is that the query WORKS against a real handle and
            # answers "member" — not that it can tell our job from theirs.
            assert backend_launch._proven_in_a_job(proc) is True
        finally:
            kernel.CloseHandle(wintypes.HANDLE(job))
            proc.kill()
            proc.wait(timeout=10)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
    def test_a_refused_query_is_not_proof_and_says_so(self, caplog):
        class BadHandle(FakePopen):
            _handle = 0  # never a valid process handle

        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            assert backend_launch._proven_in_a_job(BadHandle()) is False
        assert "F-867" in caplog.text
        assert "unverified" in caplog.text


# ---------------------------------------------------------------------------
# The intermediary itself.
# ---------------------------------------------------------------------------


class TestIntermediaryInterpreter:
    def test_a_venv_scripts_pythonw_is_refused(self, tmp_path):
        venv = tmp_path / "venv"
        scripts = venv / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "pythonw.exe").write_text("")
        (venv / "pyvenv.cfg").write_text("home = C:/base")

        assert (
            backend_launch._intermediary_interpreter(str(scripts / "python.exe"))
            is None
        )

    def test_a_base_install_pythonw_is_accepted(self, tmp_path):
        base = tmp_path / "Python312"
        base.mkdir()
        (base / "pythonw.exe").write_text("")

        assert backend_launch._intermediary_interpreter(str(base / "python.exe")) == (
            base / "pythonw.exe"
        )

    def test_a_missing_pythonw_is_refused(self, tmp_path):
        assert (
            backend_launch._intermediary_interpreter(str(tmp_path / "python.exe"))
            is None
        )


@pytest.mark.skipif(
    sys.platform != "win32", reason="the launcher's creationflags are Windows-only"
)
class TestTheLauncherScriptItself:
    """Run the real ``_LAUNCHER_SCRIPT`` against a real spec, with this
    interpreter standing in for the scheduler's pythonw."""

    def _run_launcher(self, tmp_path, child_argv, cwd=None) -> tuple[Path, Path]:
        # A hex name, like the real token: a script dir goes on sys.path[0],
        # so a stdlib-shadowing name (token.py!) breaks the interpreter.
        base = tmp_path / "b3f0aa"
        script = base.with_suffix(".py")
        script.write_text(backend_launch._LAUNCHER_SCRIPT, encoding="utf-8")
        boot_log = tmp_path / "backend-boot.log"
        base.with_suffix(".json").write_text(
            json.dumps(
                {
                    "cmd": child_argv,
                    "env": {},
                    "boot_log": str(boot_log),
                    "cwd": str(cwd or tmp_path),
                }
            ),
            encoding="utf-8",
        )
        done = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return base.with_suffix(".pid"), boot_log

    def test_it_hands_back_a_pid_and_the_childs_output_lands_in_the_boot_log(
        self, tmp_path
    ):
        pid_file, boot_log = self._run_launcher(
            tmp_path,
            [sys.executable, "-c", "import sys; print('hi'); sys.stdout.flush()"],
        )
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert pid > 0
        _wait_for(boot_log, "hi")

    def test_the_child_starts_in_the_spec_s_working_directory(self, tmp_path):
        home = tmp_path / "seed dir"
        home.mkdir()
        _pid_file, boot_log = self._run_launcher(
            tmp_path,
            [
                sys.executable,
                "-c",
                "import os, sys; print(os.getcwd()); sys.stdout.flush()",
            ],
            cwd=home,
        )
        _wait_for(boot_log, str(home))

    def test_an_import_time_crash_lands_in_the_boot_log(self, tmp_path):
        # F-303's whole point, preserved across the scheduler hand-off.
        _pid_file, boot_log = self._run_launcher(
            tmp_path, [sys.executable, "-c", "import nonexistent_module_xyz"]
        )
        _wait_for(boot_log, "ModuleNotFoundError")


def _wait_for(path: Path, needle: str) -> None:
    import time

    deadline = time.monotonic() + 30
    text = ""
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            if needle in text:
                return
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} never reached {path}: {text!r}")
