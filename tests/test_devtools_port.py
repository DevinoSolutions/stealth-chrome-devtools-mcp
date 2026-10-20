"""F-1009 — the contracts of ``devtools_port`` that the launch doors share, each
stated once. The end-to-end pins live in ``test_devtools_port_identity.py`` (over
nodriver's real ``Browser.start``) and ``test_desktop_launch.py`` (the delegated
door)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest
from nodriver import cdp
from nodriver.core.connection import ProtocolException

from fakes import forbid_real_process_kills
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
        #: ``"hang"`` never answers; an exception instance is raised instead.
        self.behaviour: object = None

    async def send(self, command):
        self.sent.append(next(command))
        if self.behaviour == "hang":
            await asyncio.Event().wait()
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.processes

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture(autouse=True)
def no_real_process_is_touched(monkeypatch):
    """F-1009 safety net: nothing here may enumerate or end a REAL process. The
    one test that needs a real ``Process.children`` reads, it never ends one."""
    attempts = forbid_real_process_kills(monkeypatch)
    yield
    assert attempts == []


@pytest.fixture
def logged(monkeypatch):
    """Every ``log_warning`` the module emits, as ``(function, message)``."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        devtools_port,
        "debug_logger",
        SimpleNamespace(log_warning=lambda _c, fn, msg: seen.append((fn, msg))),
    )
    return seen


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

    def test_a_process_psutil_cannot_read_is_just_itself_and_says_so(
        self, monkeypatch, logged
    ):
        def denied(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(psutil, "Process", denied)
        assert devtools_port.owned_pids(LAUNCHED) == {LAUNCHED}
        assert [fn for fn, _ in logged] == ["owned_pids"]
        assert "AccessDenied" in logged[0][1]


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

    async def test_no_browser_process_in_the_answer_is_allowed_but_logged(self, logged):
        browser = _browser(("gpu", 5), ("renderer", 6))
        await devtools_port.confirm_ours(browser, LAUNCHED)
        assert not browser.connection.disconnected
        assert [fn for fn, _ in logged] == ["confirm_ours"]
        assert "no `browser` process" in logged[0][1]

    async def test_no_launched_pid_means_nothing_to_compare_and_is_logged(self, logged):
        browser = _browser(("browser", OTHER))
        await devtools_port.confirm_ours(browser, None)
        assert browser.connection.sent == []
        assert [fn for fn, _ in logged] == ["confirm_ours"]
        assert "no launched pid" in logged[0][1]

    async def test_a_browser_without_the_query_is_refused_and_dropped(self):
        """An Electron or CEF app squatting on the port has no SystemInfo domain;
        real Chrome always answers. Not being able to answer is not being ours."""
        browser = _browser()
        browser.connection.behaviour = ProtocolException(
            {"code": -32601, "message": "'SystemInfo.getProcessInfo' wasn't found"}
        )
        with pytest.raises(ToolError, match="cannot identify its process"):
            await devtools_port.confirm_ours(browser, LAUNCHED)
        assert browser.connection.disconnected

    async def test_the_refusal_names_a_separate_pid_namespace(self):
        browser = _browser(("browser", OTHER))
        with pytest.raises(ToolError, match="flatpak"):
            await devtools_port.confirm_ours(browser, LAUNCHED)

    async def test_a_cancelled_wait_still_drops_the_foreign_socket(self):
        """Cancellation is how a spawn budget or a caller ends the wait; it must
        not leave a websocket open to somebody else's browser."""
        browser = _browser()
        browser.connection.behaviour = "hang"
        task = asyncio.ensure_future(devtools_port.confirm_ours(browser, LAUNCHED))
        while not browser.connection.sent:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert browser.connection.disconnected
        assert [c["method"] for c in browser.connection.sent] == [
            "SystemInfo.getProcessInfo"
        ]
