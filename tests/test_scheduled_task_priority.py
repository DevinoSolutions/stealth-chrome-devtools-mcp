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
