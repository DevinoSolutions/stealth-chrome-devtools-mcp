"""F-866: the backend runs on the real interpreter, never on the venv redirector.

On 2026-09-13 at 13:32 the backend serving 24 Claude Code sessions (pid 95556,
port 52554, up 32 h) vanished between two 30 s hygiene ticks: no traceback, no
uvicorn "Shutting down", an empty fault log. Its replacement's orphan sweep then
killed the one browser open on it. The post-mortem found the cause in the SPAWN,
not in the backend:

* In a Windows venv ``sys.executable`` is ``Scripts\\python.exe`` — CPython's
  **venv redirector** (``PC/launcher.c``), not an interpreter. It re-spawns the
  real ``python.exe`` as a CHILD and puts that child in a Job Object with
  ``KILL_ON_JOB_CLOSE``: when the redirector dies, so does the backend.
* ``_start_server_process`` spawns the redirector ``DETACHED_PROCESS``, so the
  redirector has no console — and a console-subsystem child of a console-less
  parent gets a brand-new console from Windows. With Windows Terminal as the
  default terminal that console is a VISIBLE terminal window titled with the
  python path. Closing it sends ``CTRL_CLOSE_EVENT`` and the backend exits
  silently — F-839's ``SIG_IGN`` for SIGBREAK cannot catch it, and neither can
  the detach flags, which never reached the process that mattered.

The fix launches the base interpreter directly (``sys._base_executable``) with
``__PYVENV_LAUNCHER__`` set to the venv's python — the very variable the
redirector sets for its child, which ``getpath`` honours to locate ``pyvenv.cfg``
and then removes from the environment before any code runs. The detach flags
now apply to the process that serves; there is no redirector, no job and no
console. The pins below are, in order:

* the command pin — on Windows the command's interpreter is the base
  interpreter; elsewhere the command is unchanged;
* the environment pin — the child env carries ``__PYVENV_LAUNCHER__`` exactly
  when a redirector was bypassed, and never otherwise;
* the real-process pin (Windows only) — a process spawned through the real
  ``_start_server_process`` resolves the venv, has no redirector child, is in
  no Job Object (asserted only when the test process is itself job-free — a
  harness job is inherited by every child), and has no console. The check for
  a console runs in a helper process so this test's own console is never
  detached.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from unittest.mock import MagicMock

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import singleton

_REDIRECTOR = sys.platform == "win32" and os.path.normcase(
    getattr(sys, "_base_executable", sys.executable)
) != os.path.normcase(sys.executable)


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Divert every side effect of ``_start_server_process`` away from the
    real ``~/.stealth-mcp`` (a live user backend is recorded there)."""
    monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(singleton, "_ensure_state_dir", lambda: None)
    monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
    return tmp_path


# ---------------------------------------------------------------------------
# The command pin.
# ---------------------------------------------------------------------------


def test_the_command_runs_the_base_interpreter_not_the_redirector():
    cmd = singleton._server_process_cmd(4321)
    assert cmd[1:] == [
        "-m",
        "stealth_chrome_devtools_mcp",
        "--transport",
        "http",
        "--port",
        "4321",
        "--host",
        "127.0.0.1",
    ]
    if _REDIRECTOR:
        assert cmd[0] == sys._base_executable
        assert cmd[0] != sys.executable
    else:
        assert cmd[0] == sys.executable


# ---------------------------------------------------------------------------
# The environment pin.
# ---------------------------------------------------------------------------


def test_the_child_env_names_the_venv_exactly_when_the_redirector_is_bypassed(
    isolated_state, monkeypatch
):
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    popen_mock = MagicMock(return_value=fake_proc)
    monkeypatch.setattr(singleton.subprocess, "Popen", popen_mock)
    monkeypatch.setattr(singleton, "_write_server_state", lambda *a, **k: None)

    singleton._start_server_process(4321)

    args, kwargs = popen_mock.call_args
    env = kwargs["env"]
    if _REDIRECTOR:
        assert env["__PYVENV_LAUNCHER__"] == sys.executable
    else:
        assert "__PYVENV_LAUNCHER__" not in env
    # Unchanged from before F-866: still detached, still in its own group.
    if sys.platform == "win32":
        assert kwargs["creationflags"] == (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        assert kwargs["start_new_session"] is True


# ---------------------------------------------------------------------------
# The real-process pin. Windows only: the redirector, the Job Object and the
# auto-allocated console exist nowhere else, and neither does the defect.
# ---------------------------------------------------------------------------

_CHILD = """\
import pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(sys.prefix + "\\n" + sys.executable, encoding="utf-8")
time.sleep(120)
"""

# Runs in a helper process: FreeConsole/AttachConsole would otherwise detach
# THIS test's console. Prints "console" or "no-console" for the target pid.
_CONSOLE_PROBE = """\
import ctypes, sys
k = ctypes.windll.kernel32
k.FreeConsole()
print("console" if k.AttachConsole(int(sys.argv[1])) else "no-console")
"""


def _in_job(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    k = ctypes.windll.kernel32
    handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    assert handle, f"OpenProcess({pid}) failed: {k.GetLastError()}"
    try:
        result = wintypes.BOOL()
        assert k.IsProcessInJob(handle, None, ctypes.byref(result))
        return bool(result.value)
    finally:
        k.CloseHandle(handle)


@pytest.mark.skipif(not _REDIRECTOR, reason="needs a Windows venv redirector (F-866)")
def test_the_spawned_backend_has_no_redirector_no_job_and_no_console(
    isolated_state, monkeypatch
):
    marker = isolated_state / "child.txt"
    child_py = isolated_state / "child.py"
    child_py.write_text(_CHILD, encoding="utf-8")
    real_cmd = singleton._server_process_cmd(4321)
    # Keep the REAL interpreter choice; swap only the module for a sleeper.
    monkeypatch.setattr(
        singleton,
        "_server_process_cmd",
        lambda port: [real_cmd[0], str(child_py), str(marker)],
    )
    recorded: dict[str, int] = {}
    monkeypatch.setattr(
        singleton,
        "_write_server_state",
        lambda port, version, pid, fp: recorded.update(pid=pid),
    )

    singleton._start_server_process(4321)
    pid = recorded["pid"]
    proc = psutil.Process(pid)
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "child never started"
        prefix, executable = marker.read_text(encoding="utf-8").splitlines()

        # The recorded pid IS the interpreter that serves: no redirector child.
        assert proc.children() == []
        # The venv was resolved through __PYVENV_LAUNCHER__ ...
        assert os.path.normcase(prefix) == os.path.normcase(sys.prefix)
        assert os.path.normcase(executable) == os.path.normcase(sys.executable)
        # ... without the redirector's KILL_ON_JOB_CLOSE job. ``IsProcessInJob``
        # with a NULL job answers "in ANY job", and a harness that runs this test
        # inside a job of its own (Claude Code's tool shell does; a CI runner
        # may) makes every child a member by inheritance — so the check only
        # means something when the test itself is job-free. ``children() == []``
        # above already pins the absence of the redirector that creates the job.
        if not _in_job(os.getpid()):
            assert not _in_job(pid)
        # ... and without a console for a closing terminal to reach it through.
        probe = subprocess.run(
            [sys.executable, "-c", _CONSOLE_PROBE, str(pid)],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        assert probe.stdout.strip() == "no-console", probe.stdout + probe.stderr
    finally:
        for victim in [*proc.children(recursive=True), proc]:
            try:
                victim.kill()
            except psutil.Error:
                pass
