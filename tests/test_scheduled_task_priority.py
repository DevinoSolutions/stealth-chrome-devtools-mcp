"""F-932: a process started under ``schtasks`` asks for Normal priority itself.

Measured on Windows 11 10.0.26200 (2026-09-24): a task created without
``<Priority>`` runs at priority 7, and the process it launched read
``PriorityClass = BelowNormal``. Under CPU load that starved the F-867 scheduler
rung past its 20 s pid deadline, the backend fell to the ``plain`` rung and was
left inside the MCP client's job. Both launchers that run under ``schtasks`` —
``backend_launch._LAUNCHER_SCRIPT`` and ``desktop_launch._launcher_script`` —
must hand the CHILD they create Normal priority. The ``/Create`` argv is
deliberately unchanged (only ``/XML`` takes a priority, and that would move the
253-char ``/TR`` budget), so these pins read the launcher text, which is what
actually runs, rather than a fake's argv.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import backend_launch, desktop_launch


def test_the_priority_class_has_one_home_and_is_normal() -> None:
    assert desktop_launch.TASK_CHILD_PRIORITY_CLASS == 0x00000020
    assert desktop_launch.TASK_CHILD_PRIORITY_NAME == "Normal"


@pytest.mark.skipif(
    sys.platform != "win32", reason="subprocess exposes it on Windows only"
)
def test_the_constant_is_the_os_value() -> None:
    assert desktop_launch.TASK_CHILD_PRIORITY_CLASS == subprocess.NORMAL_PRIORITY_CLASS


def test_the_delegated_browser_launcher_raises_its_child_to_normal() -> None:
    script = desktop_launch._launcher_script(
        "C:/x/chrome.exe", ["--a"], Path("C:/tmp/p.pid")
    )
    lines = script.splitlines()
    start = next(i for i, line in enumerate(lines) if "-PassThru" in line)
    record = next(i for i, line in enumerate(lines) if line.startswith("Set-Content"))
    raise_line = "try { $p.PriorityClass = 'Normal' } catch {}"
    assert raise_line in lines, script
    # After ``$p`` exists and before the pid is published, so the process the
    # caller attaches to is already at Normal when it learns the pid.
    assert start < lines.index(raise_line) < record, script


def _creationflags_names(source: str) -> set[str]:
    """Every ``subprocess.<NAME>`` inside a ``creationflags=`` keyword."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "creationflags":
                continue
            for inner in ast.walk(keyword.value):
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "subprocess"
                ):
                    names.add(inner.attr)
    return names


def test_the_backend_launcher_creates_the_backend_at_normal_priority() -> None:
    names = _creationflags_names(backend_launch._LAUNCHER_SCRIPT)
    assert "NORMAL_PRIORITY_CLASS" in names, names
    # The detach flags it already had are still there.
    assert {"DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"} <= names, names


def test_the_backend_launcher_stays_stdlib_only() -> None:
    tree = ast.parse(backend_launch._LAUNCHER_SCRIPT)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported <= set(sys.stdlib_module_names), imported


# --- F-1004: the I/O and memory priority, which the class flag never touched ---
#
# Measured 2026-10-18 (Windows 11 10.0.26300): the scheduler-rung backend and
# every Chrome it launched read I/O priority Low (1) and memory priority 2. With
# a chkdsk scan holding the disk, a fresh-profile Chrome at those levels answered
# /json/version after 111 s; at Normal, 0.54 s. Every spawn missed the 30 s
# connect ceiling, the master profile included.


class _FakeProc:
    def __init__(self, io: int, *, pid: int = 4242) -> None:
        self.pid = pid
        self.io = io
        self.set_calls: list[int] = []

    def ionice(self, value: int | None = None) -> int:
        if value is None:
            return self.io
        self.set_calls.append(value)
        self.io = value
        return value


@pytest.fixture()
def win32(monkeypatch):
    monkeypatch.setattr(desktop_launch.sys, "platform", "win32")


def test_the_io_and_memory_floors_are_normal() -> None:
    assert desktop_launch.TASK_CHILD_IO_PRIORITY == 2
    assert desktop_launch.TASK_CHILD_MEMORY_PRIORITY == 5


@pytest.mark.skipif(sys.platform != "win32", reason="psutil exposes it on Windows only")
def test_the_io_floor_is_psutils_normal() -> None:
    import psutil

    assert int(psutil.IOPRIO_NORMAL) == desktop_launch.TASK_CHILD_IO_PRIORITY


def test_the_raise_is_a_no_op_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(desktop_launch.sys, "platform", "linux")

    def _never(*_a, **_k):
        raise AssertionError("no process may be touched off Windows")

    monkeypatch.setattr(desktop_launch.psutil, "Process", _never)
    assert desktop_launch.raise_inherited_priority(4242) == {}


def test_a_low_process_is_raised_to_normal(win32, monkeypatch) -> None:
    proc = _FakeProc(io=1)
    monkeypatch.setattr(desktop_launch.psutil, "Process", lambda pid=None: proc)
    monkeypatch.setattr(
        desktop_launch, "_raise_memory_priority", lambda pid, floor: (2, floor)
    )
    assert desktop_launch.raise_inherited_priority(4242) == {
        "io": (1, 2),
        "memory": (2, 5),
    }
    assert proc.set_calls == [2]


def test_a_higher_priority_is_never_lowered(win32, monkeypatch) -> None:
    proc = _FakeProc(io=3)  # High: somebody asked for it on purpose
    monkeypatch.setattr(desktop_launch.psutil, "Process", lambda pid=None: proc)
    monkeypatch.setattr(
        desktop_launch, "_raise_memory_priority", lambda pid, floor: None
    )
    assert desktop_launch.raise_inherited_priority(4242) == {}
    assert proc.set_calls == []


def test_a_refusal_is_logged_never_raised(win32, monkeypatch) -> None:
    import psutil

    def _denied(pid=None):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(desktop_launch.psutil, "Process", _denied)
    assert desktop_launch.raise_inherited_priority(4242) == {}


def _lower(pid: int, io: int, memory: int) -> None:
    import ctypes
    from ctypes import wintypes

    import psutil

    psutil.Process(pid).ionice(io)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.SetProcessInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x0200, False, pid)
    assert handle, ctypes.get_last_error()
    try:
        value = wintypes.ULONG(memory)
        assert kernel32.SetProcessInformation(handle, 0, ctypes.byref(value), 4)
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows priorities")
def test_a_real_low_process_is_raised_and_reads_back_normal() -> None:
    """Over the real OS, not a fake: a child started at the levels a scheduled
    task hands out reads back Normal on both after the raise, and a second raise
    changes nothing."""
    import psutil

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _lower(child.pid, io=1, memory=2)
        assert int(psutil.Process(child.pid).ionice()) == 1
        changed = desktop_launch.raise_inherited_priority(child.pid)
        assert changed == {"io": (1, 2), "memory": (2, 5)}
        assert int(psutil.Process(child.pid).ionice()) == 2
        assert desktop_launch._raise_memory_priority(child.pid, 5) is None  # already 5
        assert desktop_launch.raise_inherited_priority(child.pid) == {}
    finally:
        child.kill()
        child.wait(timeout=10)


def test_the_backend_raises_itself_before_it_launches_or_adopts_a_browser() -> None:
    """Every Chrome inherits the backend's priority, so the raise must come
    before the first thing in the lifespan that can launch or reach a browser."""
    from stealth_chrome_devtools_mcp.embedded import server

    source = Path(server.__file__).read_text(encoding="utf-8")
    body = source[source.index("async def app_lifespan(") :]
    call = body.index("rt.desktop_launch.raise_inherited_priority()")
    for later in (
        "rt.browser_manager.start_idle_reaper()",
        "rt.browser_health.start(",
        "rt.browser_reattach.start(",
        "\n    try:\n        yield",
    ):
        assert call < body.index(later), later
