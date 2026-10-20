"""F-1009 — the browser a spawn connects to must be the Chrome ITS OWN launch
started, never one a sibling happens to hold on the port.

nodriver picks the port by binding and releasing a socket (``util.free_port``)
and only then launches Chrome with ``--remote-debugging-port=<that port>``. In
the gap another process can take it — most often a sibling Chrome that a
concurrent spawn is starting — and our Chrome then fails to open its endpoint
while the sibling's answers ``/json/version``. nodriver cannot tell, so the
spawn registered the SIBLING's browser as its own and a later ``close_instance``
killed it: on the shared ``fleet`` or ``default`` session that is the logins.

The fix keeps the argv (``--remote-debugging-port=0`` was tried and real Chrome
then reports ``navigator.webdriver == true``; and Chrome writes no
``DevToolsActivePort`` for a concrete port, so that file cannot witness anything)
and asks the connected browser which PROCESS it is (``SystemInfo.getProcessInfo``,
read-only): its pid must be the one we launched or below it.

Every node drives nodriver's REAL ``Browser.start`` through the REAL
``BrowserManager._launch_browser`` over a faked OS: a table of endpoints, a
virtual clock, a connection that runs the REAL ``cdp.system_info`` generator, and
the process table. No Chrome is launched, no socket is opened. The pids are far
above any a machine hands out, so ``psutil`` finds no descendants of them.
"""

from __future__ import annotations

import asyncio
import sys
import urllib.error
from typing import TYPE_CHECKING

import pytest
from nodriver import cdp
from nodriver.core.browser import Browser, HTTPApi
from nodriver.core.connection import ProtocolException
from nodriver.core.util import get_registered_instances

from fakes import ProcessTable, forbid_real_process_kills, nodriver_registry
from stealth_chrome_devtools_mcp.embedded import (
    browser_connect,
    devtools_port,
    spawn_budget,
    spawn_contention,
    spawn_exhaustion,
    spawn_leak,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

#: The port ``util.free_port`` hands nodriver, so the one Chrome is asked for.
OUR_PORT = 45000
OUR_PID = 3_999_001
SIBLING_PID = 3_999_002


class World:
    """The OS as ``Browser.start`` meets it, on a virtual clock."""

    def __init__(self) -> None:
        self.t = 0.0
        #: Seconds after launch at which the Chrome holding the port answers.
        self.opens_at = 1.0
        #: Who holds OUR_PORT: the pid its browser process answers to
        #: ``SystemInfo.getProcessInfo`` with.
        self.holder_pid = OUR_PID
        #: What the holder does with the identity question.
        self.identity = "answers"
        self.argv: list[str] = []
        self.connections: list[FakeConnection] = []
        self.process = type("P", (), {"pid": OUR_PID, "returncode": None})()
        self._launched = False

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float = 0.1) -> None:
        self.t += seconds
        await asyncio.sleep(0)

    async def spawn(self, executable, *args, **_kwargs):
        self.argv = list(args)
        self._launched = True
        return self.process

    def requested_port(self) -> str | None:
        for arg in self.argv:
            if arg.startswith("--remote-debugging-port="):
                return arg.split("=", 1)[1]
        return None

    async def request(self, api, endpoint_name, method="get", data=None):
        answering = self._launched and api.port == OUR_PORT and self.t >= self.opens_at
        if not answering:
            raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        return {
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{api.port}/devtools/browser/x",
            "Browser": "Chrome/153.0.0.0",
        }


class FakeConnection:
    """nodriver's ``Connection``: opens nothing, and runs the REAL cdp generator
    for each command, answering ``SystemInfo.getProcessInfo`` as the holder of
    the port would."""

    world: World

    def __init__(self, url, browser=None, **kwargs) -> None:
        self.url = url
        self.handlers: dict = {}
        self.sent: list[str] = []
        self.disconnected = False
        self.target = kwargs.get("target")
        type(self).world.connections.append(self)

    def __getattr__(self, item):
        return getattr(self.target, item)

    async def send(self, command, **kwargs):
        method = next(command)["method"]
        self.sent.append(method)
        world = type(self).world
        reply: dict = {}
        if method == "SystemInfo.getProcessInfo":
            if world.identity == "silent":
                await asyncio.Event().wait()
            if world.identity == "unsupported":
                raise ProtocolException({"code": -32601, "message": "not found"})
            reply = {
                "processInfo": [
                    {"type": "gpu", "id": 1, "cpuTime": 0.0},
                    {"type": "browser", "id": world.holder_pid, "cpuTime": 0.0},
                ]
            }
        try:
            command.send(reply)
        except StopIteration as done:
            return done.value
        return None

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture(autouse=True)
def no_real_process_is_touched(monkeypatch):
    """F-1009 safety net: nothing here may end a REAL process or see the real process table. The
    one test that wants a process table installs ``ProcessTable`` over this."""
    attempts = forbid_real_process_kills(monkeypatch)
    yield
    assert attempts == []


@pytest.fixture
def profile(tmp_path) -> Path:
    directory = tmp_path / "profile"
    directory.mkdir()
    return directory


@pytest.fixture
def world(monkeypatch):
    this = World()
    FakeConnection.world = this
    monkeypatch.setattr(asyncio, "create_subprocess_exec", this.spawn)
    monkeypatch.setattr("nodriver.core.util.free_port", lambda: OUR_PORT)
    monkeypatch.setattr("nodriver.core.browser.Connection", FakeConnection)

    async def _request(api, endpoint_name, method="get", data=None):
        return await this.request(api, endpoint_name, method, data)

    monkeypatch.setattr(HTTPApi, "_request", _request)
    monkeypatch.setattr(Browser, "sleep", lambda self, t=0.1: this.sleep(t))

    async def _no_targets(self):
        return None

    monkeypatch.setattr(Browser, "update_targets", _no_targets)
    monkeypatch.setattr(browser_connect, "_now", this.now)
    monkeypatch.setattr(browser_connect, "_sleep", this.sleep)
    monkeypatch.setattr(devtools_port, "IDENTITY_TIMEOUT_SECONDS", 0.05)
    browser_connect.install()
    with nodriver_registry():
        yield this


def _options(profile: Path) -> BrowserOptions:
    return BrowserOptions(headless=True, user_data_dir=str(profile))


async def _launch(profile: Path) -> Browser:
    return await BrowserManager()._launch_browser(
        _options(profile), sys.executable, [], spawn_leak.Attempt()
    )


async def test_a_stolen_port_cannot_attach_us_to_a_sibling(world, profile):
    """THE pin. The port nodriver picked was taken by a sibling's browser before
    our Chrome could bind it: the sibling answers ``/json/version`` and reports
    ITS OWN browser pid. The spawn must fail by name naming both pids, and hand
    back nothing (before F-1009 it registered the sibling's browser as its own)."""
    world.holder_pid = SIBLING_PID

    with pytest.raises(ToolError, match="F-1009") as err:
        await _launch(profile)

    assert str(SIBLING_PID) in str(err.value)
    assert str(OUR_PID) in str(err.value)
    assert "not the Chrome this spawn launched" in str(err.value)


async def test_our_own_chrome_on_the_port_is_attached(world, profile):
    """The other side of it: when the port is ours the spawn attaches."""
    browser = await _launch(profile)

    assert browser.connection is world.connections[0]
    assert browser.config.port == OUR_PORT
    assert not browser.connection.disconnected
    assert "SystemInfo.getProcessInfo" in browser.connection.sent


async def test_chrome_keeps_the_concrete_port_in_its_argv(world, profile):
    """Pinned because the first version of this fix asked Chrome for port 0 and
    CI's offline-stealth gate then saw ``navigator.webdriver == true`` on every
    OS: the argv must keep the port nodriver chose."""
    await _launch(profile)

    assert world.requested_port() == str(OUR_PORT)
    assert sum(a.startswith("--remote-debugging-port") for a in world.argv) == 1


async def test_a_refused_foreign_connection_is_dropped_not_closed(world, profile):
    """Disconnecting is a websocket close; ``Browser.close`` is a COMMAND that
    would shut the sibling's browser down. Only the former may happen."""
    world.holder_pid = SIBLING_PID

    with pytest.raises(ToolError):
        await _launch(profile)

    (foreign,) = world.connections
    assert foreign.disconnected
    assert not [m for m in foreign.sent if m.startswith("Browser.")]
    assert "Target.closeTarget" not in foreign.sent


async def test_a_stale_port_file_is_not_trusted(world, profile):
    """A ``DevToolsActivePort`` a dead Chrome left behind names the port a
    squatter now holds. A check that believed it would have passed the sibling;
    the identity of the answering PROCESS is what decides."""
    (profile / "DevToolsActivePort").write_text(
        f"{OUR_PORT}\n/devtools/browser/x\n", encoding="utf-8"
    )
    world.holder_pid = SIBLING_PID

    with pytest.raises(ToolError, match="not the Chrome this spawn launched"):
        await _launch(profile)


async def test_a_browser_that_never_says_who_it_is_times_out_by_name(world, profile):
    world.identity = "silent"

    with pytest.raises(ToolError, match="did not say which process it is") as err:
        await _launch(profile)

    assert "F-1009" in str(err.value)
    (foreign,) = world.connections
    assert foreign.disconnected
    assert "Browser.close" not in foreign.sent


async def test_the_identity_wait_fits_inside_the_launch_phase():
    assert (
        0 < devtools_port.IDENTITY_TIMEOUT_SECONDS < spawn_budget.LAUNCH_BUDGET_SECONDS
    )


async def test_a_browser_without_the_query_is_refused_not_trusted(world, profile):
    """An Electron or CEF app squatting on the port has no ``SystemInfo`` domain,
    and real Chrome always answers: a browser that cannot say who it is is not
    ours, and the connection to it is dropped."""
    world.identity = "unsupported"

    with pytest.raises(ToolError, match="cannot identify its process"):
        await _launch(profile)

    (foreign,) = world.connections
    assert foreign.disconnected
    assert not [m for m in foreign.sent if m.startswith("Browser.")]


async def test_a_launched_pid_nodriver_did_not_stamp_is_allowed(world, profile):
    world.process.pid = None

    # ``Browser.start`` stamps ``_process_pid`` from the process it spawned; with
    # none to compare against nothing can be refused.
    world.holder_pid = SIBLING_PID
    browser = await _launch(profile)

    assert not browser.connection.disconnected


async def test_a_mismatch_never_touches_the_foreign_browser(
    monkeypatch, world, profile, tmp_path
):
    """``spawn_browser`` end to end over a process table: after the mismatch our
    own Chrome is reaped and the sibling's pid is never terminated. Nothing was
    registered, so ``close_instance`` has nothing to close either."""
    world.holder_pid = SIBLING_PID
    table = ProcessTable(monkeypatch, get_registered_instances())
    table.launch(OUR_PID, str(profile))
    table.launch(SIBLING_PID, str(tmp_path / "fleet"))
    monkeypatch.setattr(process_cleanup, "pid_file", tmp_path / "browser_pids.json")
    monkeypatch.setattr(spawn_exhaustion, "exhaustion_hint", lambda path: None)
    monkeypatch.setattr(spawn_contention, "contention_hint", lambda n: None)
    monkeypatch.setattr(
        BrowserManager,
        "_resolve_launch_args",
        lambda self, options, proxy, platform_info: ([], sys.executable, []),
    )
    manager = BrowserManager()

    with pytest.raises(Exception, match="not the Chrome this spawn launched"):
        await manager.spawn_browser(_options(profile))

    assert table.procs[SIBLING_PID].terminate_calls == 0
    assert table.procs[SIBLING_PID].alive
    assert table.procs[OUR_PID].terminate_calls == 1
    (foreign,) = world.connections
    assert foreign.disconnected
    assert manager._instances == {}
    assert await manager.close_instance("anything-the-caller-might-hold") is False
    assert table.procs[SIBLING_PID].terminate_calls == 0


async def test_the_query_the_check_sends_is_the_real_cdp_command():
    """The fake answers by driving the library's own generator; this keeps that
    honest by naming the method the production code sends."""
    assert next(cdp.system_info.get_process_info())["method"] == (
        "SystemInfo.getProcessInfo"
    )
