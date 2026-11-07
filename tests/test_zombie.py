"""The zombie question is never asked where there are no zombies (2026-11-07 hangs).

On Windows psutil's ``Process.status()`` is a snapshot of every process and thread
on the machine taken with the GIL held (``zombie``'s docstring has the account and
the measurements). ``process_exit`` asked it every 20 ms for up to 5 s of every
close, which starved the backend's event loop until no tool call came back. These
pins hold the call count, not a timing, so they are exact on any runner.
"""

from __future__ import annotations

import subprocess
import sys

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    devtools_port,
    process_exit,
    zombie,
)


class _Process:
    """A process whose ``status`` read is counted (and refused when *forbidden*)."""

    def __init__(self, status: str = psutil.STATUS_RUNNING, *, forbidden: bool = False):
        self._status = status
        self._forbidden = forbidden
        self.reads = 0

    def status(self) -> str:
        self.reads += 1
        if self._forbidden:
            raise AssertionError("status() read on a platform with no zombies")
        return self._status


@pytest.fixture
def status_reads(monkeypatch):
    """Every real ``psutil.Process.status`` call made while the test runs."""
    calls: list[int] = []
    real = psutil.Process.status

    def counted(self):
        calls.append(self.pid)
        return real(self)

    monkeypatch.setattr(psutil.Process, "status", counted)
    return calls


@pytest.fixture
def sleeper():
    """A real child that stays up for the length of a test, ended afterwards."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield child
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_windows_is_the_platform_without_zombies():
    assert zombie.ZOMBIES_EXIST is (sys.platform != "win32")


def test_no_zombies_means_no_status_read(monkeypatch):
    monkeypatch.setattr(zombie, "ZOMBIES_EXIST", False)
    process = _Process(psutil.STATUS_ZOMBIE, forbidden=True)

    assert zombie.is_zombie(process) is False
    assert process.reads == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [(psutil.STATUS_ZOMBIE, True), (psutil.STATUS_RUNNING, False)],
)
def test_where_zombies_exist_the_status_decides(monkeypatch, status, expected):
    monkeypatch.setattr(zombie, "ZOMBIES_EXIST", True)
    process = _Process(status)

    assert zombie.is_zombie(process) is expected
    assert process.reads == 1


def test_a_reaped_pid_raises_what_status_raises(monkeypatch):
    """Every caller already reads ``NoSuchProcess`` as gone; it is not swallowed."""
    monkeypatch.setattr(zombie, "ZOMBIES_EXIST", True)

    class _Reaped:
        def status(self):
            raise psutil.NoSuchProcess(9)

    with pytest.raises(psutil.NoSuchProcess):
        zombie.is_zombie(_Reaped())


def test_a_close_waiting_on_a_live_browser_never_snapshots_the_machine(
    monkeypatch, status_reads, sleeper
):
    """THE regression: the grace polls a browser that has not left yet. Before the
    fix every poll was a whole-machine snapshot with the GIL held (25-150 ms each
    on the fleet PC, so the 5 s grace held the GIL most of the time); without
    zombies it is none, and the wait still answers."""
    monkeypatch.setattr(zombie, "ZOMBIES_EXIST", False)

    waited = process_exit.wait_for_exit(sleeper.pid, timeout=0.3)

    assert waited.exited is False
    assert waited.reason == "still-running"
    assert status_reads == []


def test_the_grace_still_sees_the_browser_leave(monkeypatch, status_reads, sleeper):
    monkeypatch.setattr(zombie, "ZOMBIES_EXIST", False)
    sleeper.terminate()
    sleeper.wait(timeout=10)

    waited = process_exit.wait_for_exit(sleeper.pid, timeout=5.0)

    assert waited.exited is True
    assert status_reads == []


def test_where_zombies_exist_the_grace_still_asks(monkeypatch, status_reads, sleeper):
    """POSIX keeps its answer: a zombie is an exited browser (F-910)."""
    monkeypatch.setattr(zombie, "ZOMBIES_EXIST", True)

    process_exit.wait_for_exit(sleeper.pid, timeout=0.1)

    assert status_reads
    assert set(status_reads) == {sleeper.pid}


def test_the_other_liveness_readers_ask_the_same_question(
    monkeypatch, status_reads, sleeper
):
    """``browser_is_alive``, ``devtools_port._running`` and ``browser_cmdline``'s
    gone-pid witness go through the same one home."""
    monkeypatch.setattr(zombie, "ZOMBIES_EXIST", False)

    assert process_exit.browser_is_alive(
        type("_Browser", (), {"_process": None, "_process_pid": sleeper.pid})()
    )
    assert devtools_port._running(sleeper.pid) is True
    assert browser_cmdline.command_line_is_gone(sleeper.pid) is False
    assert status_reads == []


@pytest.mark.asyncio
async def test_a_browser_that_is_the_launched_pid_is_confirmed_without_a_walk(
    monkeypatch,
):
    """``owned_pids`` is a whole-machine ``children()`` snapshot taken ON the loop.
    A direct launch is the browser itself, so nothing needs walking."""
    from nodriver import cdp

    def no_walk(_pid):
        raise AssertionError("walked the process table for the launched pid itself")

    monkeypatch.setattr(devtools_port, "owned_pids", no_walk)

    class _Browser:
        async def send(self, command):
            assert next(command)["method"] == "SystemInfo.getProcessInfo"
            return [cdp.system_info.ProcessInfo("browser", 4242, 0.0)]

        async def aclose(self):
            raise AssertionError("dropped a browser that is ours")

    await devtools_port.confirm_ours(_Browser(), 4242)
