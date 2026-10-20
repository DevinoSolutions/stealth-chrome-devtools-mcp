"""F-1009 — the contracts of ``devtools_port`` that the launch doors share, each
stated once. The end-to-end pins live in ``test_devtools_port_identity.py`` (over
nodriver's real ``Browser.start``) and ``test_desktop_launch.py`` (the delegated
door)."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest
from nodriver import cdp

from stealth_chrome_devtools_mcp.embedded import devtools_port
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: Far above any pid a machine hands out, so ``psutil`` finds nothing below them.
LAUNCHED = 3_999_101
OTHER = 3_999_102


class _Connection:
    def __init__(self, processes) -> None:
        self.processes = processes
        self.disconnected = False
        self.sent: list[dict] = []

    async def send(self, command):
        self.sent.append(next(command))
        return self.processes

    async def disconnect(self):
        self.disconnected = True


def _browser(*processes: tuple[str, int]) -> SimpleNamespace:
    return SimpleNamespace(
        connection=_Connection(
            [cdp.system_info.ProcessInfo(t, i, 0.0) for t, i in processes]
        )
    )


class TestOwnedPids:
    def test_a_pid_with_nothing_below_it_owns_itself(self):
        assert devtools_port.owned_pids(LAUNCHED) == {LAUNCHED}

    def test_a_wrapper_owns_what_it_forked(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            assert child.pid in devtools_port.owned_pids(os.getpid())
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_a_process_psutil_cannot_read_is_just_itself(self, monkeypatch):
        def denied(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(psutil, "Process", denied)
        assert devtools_port.owned_pids(LAUNCHED) == {LAUNCHED}


@pytest.mark.asyncio
class TestConfirmOurs:
    async def test_the_launched_pid_passes_and_asks_the_read_only_query(self):
        browser = _browser(("gpu", 5), ("browser", LAUNCHED))
        await devtools_port.confirm_ours(browser, LAUNCHED)
        assert [c["method"] for c in browser.connection.sent] == [
            "SystemInfo.getProcessInfo"
        ]
        assert not browser.connection.disconnected

    async def test_another_browser_is_refused_naming_both_and_dropped(self):
        browser = _browser(("browser", OTHER))
        with pytest.raises(ToolError, match="F-1009") as err:
            await devtools_port.confirm_ours(browser, LAUNCHED)
        assert f"process {OTHER}" in str(err.value)
        assert f"pid {LAUNCHED}" in str(err.value)
        assert browser.connection.disconnected

    async def test_a_descendant_of_the_launched_pid_passes(self, monkeypatch):
        monkeypatch.setattr(devtools_port, "owned_pids", lambda pid: {pid, OTHER})
        browser = _browser(("browser", OTHER))
        await devtools_port.confirm_ours(browser, LAUNCHED)
        assert not browser.connection.disconnected

    async def test_no_browser_process_in_the_answer_is_unverifiable_not_foreign(self):
        browser = _browser(("gpu", 5), ("renderer", 6))
        await devtools_port.confirm_ours(browser, LAUNCHED)
        assert not browser.connection.disconnected

    async def test_no_launched_pid_means_nothing_to_compare(self):
        browser = _browser(("browser", OTHER))
        await devtools_port.confirm_ours(browser, None)
        assert browser.connection.sent == []
