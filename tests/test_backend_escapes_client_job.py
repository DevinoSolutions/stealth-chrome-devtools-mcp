"""F-867: the backend outlives the Job Object the MCP client wraps our proxy in.

The reference MCP Python SDK (``mcp/os/win32/utilities.py::create_windows_process``)
puts EVERY stdio server it starts into a Windows Job Object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and no ``BREAKAWAY_OK``. Our per-session
stdio proxy is that server. When the proxy wins the cold-start lock and spawns
the shared HTTP backend, the backend is created INSIDE that job — job membership
is inherited by children, and ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` say
nothing about jobs. The moment that ONE client session ends, either
``TerminateJobObject`` or simply the last ``CloseHandle`` on the job kills every
member, and the backend serving every OTHER session dies with no traceback, no
uvicorn shutdown and an empty fault log (the F-859 §11 shape, seen in CI: v2.1.4
publish run 34797141480 attempt 1).

**Why the membership query uses OUR OWN job handle.** ``IsProcessInJob(h, NULL)``
answers "is this process in ANY job", and Claude Code's tool shell — like a CI
runner's — is itself inside a job, so every process spawned from a test is a
member by inheritance and the NULL query is always True. That is exactly why
F-866's pin could only make a relative assertion. Here the job is one we created,
so ``IsProcessInJob(backend, job)`` is a precise question about the job that would
do the killing. Membership is fixed at creation and cannot change afterwards, so
the pin records it BEFORE closing the handle and asserts liveness after.

The pin is a real spawn through the real ``singleton._start_server_process`` (the
module swapped for a sleeper, the interpreter kept), driven from a helper process
that we assign to the job first — the helper waits on a line of stdin before it
spawns, so there is no spawn-before-assign race. Everything the spawn would write
to ``~/.stealth-mcp`` is diverted inside the helper, because a live user backend
is recorded there.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import warnings
from ctypes import wintypes
from typing import TYPE_CHECKING

import psutil
import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Job Objects (F-867)"
)

# JOBOBJECTINFOCLASS.JobObjectExtendedLimitInformation
_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_CREATE_NO_WINDOW = 0x08000000

_HELPER = '''\
"""Spawns the backend through the REAL launcher, from inside the caller's job."""

import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="LOG %(message)s")

from stealth_chrome_devtools_mcp.embedded import backend_registry, singleton

# Read the rung off the line a post-mortem would read, on the one logger a
# proxy's file handler is installed on. Reading the log rather than the return
# value also pins that the line exists at all: without it, a CI cell could not
# say which rung served there.
rungs = []


MARKER = "backend spawned via the "


class RungHandler(logging.Handler):
    def emit(self, record):
        text = record.getMessage()
        if MARKER in text:
            rungs.append(text.split(MARKER, 1)[1].split(" rung", 1)[0])


logging.getLogger("stealth.proxy").addHandler(RungHandler())
logging.getLogger("stealth.proxy").setLevel(logging.INFO)

state = Path(sys.argv[1])

backend_registry.STATE_DIR = state
singleton.PORT_FILE = state / "server.port"
singleton._ensure_state_dir = lambda: None
singleton._server_version = lambda: "1.2.1"

recorded = {{}}
singleton._write_server_state = lambda port, version, pid, fp: recorded.update(pid=pid)

# Keep the REAL interpreter choice (F-866); swap only the module for a sleeper.
real_cmd = singleton._server_process_cmd(4321)
singleton._server_process_cmd = lambda port: [
    real_cmd[0],
    "-c",
    "import time; time.sleep({sleep})",
]

# Wait to be assigned to the job before spawning anything.
sys.stdin.readline()

singleton._start_server_process(4321)

print("RUNG", rungs[-1] if rungs else "unlogged", flush=True)
print("PID", recorded["pid"], flush=True)
import time

time.sleep({sleep})
'''


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


def _kernel32():
    return ctypes.windll.kernel32


def _make_kill_on_close_job():
    """A job built exactly the way the MCP SDK builds the one around our proxy."""
    kernel = _kernel32()
    job = kernel.CreateJobObjectW(None, None)
    assert job, f"CreateJobObjectW failed: {kernel.GetLastError()}"
    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert kernel.SetInformationJobObject(
        wintypes.HANDLE(job),
        _EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ), f"SetInformationJobObject failed: {kernel.GetLastError()}"
    return job


def _in_job(process_handle, job) -> bool:
    result = wintypes.BOOL()
    kernel = _kernel32()
    assert kernel.IsProcessInJob(
        wintypes.HANDLE(process_handle), wintypes.HANDLE(job), ctypes.byref(result)
    ), f"IsProcessInJob failed: {kernel.GetLastError()}"
    return bool(result.value)


def _still_running(process_handle) -> bool:
    code = wintypes.DWORD()
    kernel = _kernel32()
    assert kernel.GetExitCodeProcess(
        wintypes.HANDLE(process_handle), ctypes.byref(code)
    ), f"GetExitCodeProcess failed: {kernel.GetLastError()}"
    return code.value == _STILL_ACTIVE


def _read_until_pid(helper, sink: list[str]) -> None:
    """Drain the helper's merged stdout/stderr until it announces the pid."""
    for line in helper.stdout:
        sink.append(line.rstrip())
        if line.startswith("PID "):
            return


@pytest.mark.parametrize("session_end", ["close_handle", "terminate_job"])
def test_the_backend_survives_the_clients_job_ending(tmp_path, session_end):
    helper_py = tmp_path / "helper.py"
    helper_py.write_text(_HELPER.format(sleep=120), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()

    env = _child_env(tmp_path)
    job = _make_kill_on_close_job()
    kernel = _kernel32()
    helper = subprocess.Popen(
        [sys.executable, str(helper_py), str(state)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        creationflags=_CREATE_NO_WINDOW,
    )
    lines: list[str] = []
    backend_handle = None
    backend_pid = None
    try:
        assert kernel.AssignProcessToJobObject(
            wintypes.HANDLE(job), wintypes.HANDLE(int(helper._handle))
        ), f"AssignProcessToJobObject failed: {kernel.GetLastError()}"
        # Sanity: the job is real and the helper is in it.
        assert _in_job(int(helper._handle), job)

        helper.stdin.write("go\n")
        helper.stdin.flush()
        reader = threading.Thread(target=_read_until_pid, args=(helper, lines))
        reader.start()
        reader.join(90)
        transcript = "\n".join(lines)
        assert not reader.is_alive(), f"helper never announced a pid:\n{transcript}"
        pid_lines = [line for line in lines if line.startswith("PID ")]
        assert pid_lines, f"helper never announced a pid:\n{transcript}"
        backend_pid = int(pid_lines[-1].split()[1])
        rung = next(
            (line.split()[1] for line in lines if line.startswith("RUNG ")), "unlogged"
        )
        # Named first, because it is the reason for everything below: only a rung
        # that leaves the job can pass the escape assertions, and a cell that has
        # no scheduler has to say so rather than just failing.
        assert rung in {"breakaway", "scheduler"}, (
            f"F-867: rung {rung!r} served — the scheduler was unavailable on this "
            f"runner (F-867 §6), so the backend stayed inside the client's "
            f"job:\n{transcript}"
        )

        backend_handle = kernel.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, backend_pid
        )
        assert backend_handle, (
            f"the backend (pid {backend_pid}) was gone before the job even "
            f"closed:\n{transcript}"
        )
        # Membership is fixed at creation, so read it while the handle is open.
        member = _in_job(backend_handle, job)

        if session_end == "terminate_job":
            kernel.TerminateJobObject(wintypes.HANDLE(job), 1)
        kernel.CloseHandle(wintypes.HANDLE(job))
        job = None

        _wait_for_exit(helper, 15.0)
        assert helper.poll() is not None, (
            f"the client's proxy outlived its own job ({session_end}); the pin "
            f"cannot say anything about the backend:\n{transcript}"
        )
        assert not member, (
            f"F-867: the backend (pid {backend_pid}) was created INSIDE the "
            f"client's job, so ending that one session kills it:\n{transcript}"
        )
        assert _still_running(backend_handle), (
            f"F-867: the backend (pid {backend_pid}) died when the client's job "
            f"ended ({session_end}):\n{transcript}"
        )
        # pytest prints the warnings summary for PASSING tests too, so this is
        # how a CI cell's own log says which rung protected the backend there.
        warnings.warn(
            f"F-867 pin: backend escaped the client job via rung {rung!r} "
            f"({session_end})",
            stacklevel=1,
        )
    finally:
        if backend_handle:
            kernel.CloseHandle(wintypes.HANDLE(backend_handle))
        if job:
            kernel.CloseHandle(wintypes.HANDLE(job))
        if backend_pid is not None:
            _kill(backend_pid)
        _kill(helper.pid)


def _child_env(tmp_path: Path) -> dict[str, str]:
    """The helper's environment: the real one, with every state path diverted."""
    env = dict(os.environ)
    env["STEALTH_MCP_LOG_DIR"] = str(tmp_path / "logs")
    env["STEALTH_MCP_NO_ERROR_REPORTING"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _wait_for_exit(helper: subprocess.Popen, timeout: float) -> None:
    try:
        helper.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _kill(pid: int) -> None:
    try:
        process = psutil.Process(pid)
        for child in process.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        process.kill()
    except psutil.Error:
        pass
