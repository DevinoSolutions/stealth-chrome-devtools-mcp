"""F-1009 — the DevTools endpoint a spawn connects to must be the one ITS OWN
Chrome opened, never one a sibling happens to hold.

nodriver picks the port by binding and releasing a socket (``util.free_port``)
and only then launches Chrome with ``--remote-debugging-port=<that port>``. In
the gap another process can take it — most often a sibling Chrome that a
concurrent spawn is starting — and our Chrome then fails to open its endpoint
while the sibling's answers ``/json/version``. nodriver cannot tell, so the
spawn registered the SIBLING's browser as its own and a later ``close_instance``
killed it: on the shared ``fleet`` or ``default`` session that is the logins.

The fix keeps the argv (``--remote-debugging-port=0`` was tried and real Chrome
then reports ``navigator.webdriver == true``) and refuses to believe anything
that answers on the port until Chrome has written ``DevToolsActivePort`` in OUR
profile naming that port; what answers there must then carry the websocket path
Chrome wrote, or it is somebody else's.

Every node drives nodriver's REAL ``Browser.start`` through the REAL
``BrowserManager._launch_browser`` over a faked OS: a subprocess that "binds" the
port its argv names (unless a sibling already holds it), a table of endpoints, a
virtual clock and the process table. No Chrome is launched, no socket is opened.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import pytest
from nodriver.core.browser import Browser, HTTPApi
from nodriver.core.util import get_registered_instances

from fakes import ProcessTable, nodriver_registry
from stealth_chrome_devtools_mcp.embedded import (
    browser_connect,
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

PORT_FILE = "DevToolsActivePort"
#: The port ``util.free_port`` hands nodriver, so the one Chrome is asked for.
OUR_PORT = 45000
OUR_PATH = "/devtools/browser/ours-f1009"
SIBLING_PATH = "/devtools/browser/sibling-f1009"
STALE_PATH = "/devtools/browser/stale-f1009"
OUR_PID = 7331
SIBLING_PID = 9999


class World:
    """The OS as ``Browser.start`` meets it, on a virtual clock."""

    def __init__(self, profile: Path) -> None:
        self.profile = profile
        self.t = 0.0
        #: Seconds after launch at which Chrome has bound a port and written its file.
        self.opens_at = 1.0
        #: Does the launched Chrome ever get to write its file / open its port?
        self.chrome_opens = True
        #: A sibling bound the port nodriver picked before our Chrome could: it
        #: answers there, and our Chrome opens nothing and writes no file.
        self.sibling_takes_port = False
        #: The websocket path the browser answering on OUR port reports (a path
        #: other than the file's is a foreign browser).
        self.endpoint_on_our_port = OUR_PATH
        self.endpoints: dict[int, str] = {}
        self.argv: list[str] = []
        self.requests: list[int] = []
        self.connections: list[str] = []
        self.process = type("P", (), {"pid": OUR_PID, "returncode": None})()
        self._launched = False

    # -- time -------------------------------------------------------------
    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float = 0.1) -> None:
        self.t += seconds
        self._maybe_open()
        await asyncio.sleep(0)

    # -- the launched Chrome ----------------------------------------------
    async def spawn(self, executable, *args, **_kwargs):
        self.argv = list(args)
        self._launched = True
        return self.process

    def requested_port(self) -> str | None:
        for arg in self.argv:
            if arg.startswith("--remote-debugging-port="):
                return arg.split("=", 1)[1]
        return None

    def _maybe_open(self) -> None:
        if not (self._launched and self.chrome_opens and self.t >= self.opens_at):
            return
        if self.requested_port() != str(OUR_PORT) or OUR_PORT in self.endpoints:
            return
        if self.sibling_takes_port:
            self.endpoints[OUR_PORT] = SIBLING_PATH
            return
        self.endpoints[OUR_PORT] = self.endpoint_on_our_port
        (self.profile / PORT_FILE).write_text(f"{OUR_PORT}\n{OUR_PATH}\n")

    # -- /json/version -----------------------------------------------------
    async def request(self, api, endpoint_name, method="get", data=None):
        import urllib.error

        self.requests.append(api.port)
        path = self.endpoints.get(api.port)
        if path is None:
            raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        return {
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{api.port}{path}",
            "Browser": "Chrome/153.0.0.0",
        }


class FakeConnection:
    """nodriver's ``Connection``: records the url it was built for, opens nothing."""

    world: World

    def __init__(self, url, browser=None, **kwargs) -> None:
        type(self).world.connections.append(url)
        self.url = url
        self.handlers: dict = {}
        self.sent: list = []
        self.target = kwargs.get("target")

    def __getattr__(self, item):
        return getattr(self.target, item)

    async def send(self, command, **kwargs):
        self.sent.append(command)


@pytest.fixture
def profile(tmp_path) -> Path:
    directory = tmp_path / "profile"
    directory.mkdir()
    return directory


@pytest.fixture
def world(monkeypatch, profile):
    this = World(profile)
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
    our Chrome could bind it: the sibling answers ``/json/version``, ours writes
    no ``DevToolsActivePort``. The spawn must fail by name and attach to NOTHING
    (before F-1009 it registered the sibling's browser as its own)."""
    world.sibling_takes_port = True

    with pytest.raises(ToolError, match=PORT_FILE) as err:
        await _launch(profile)

    assert str(OUR_PORT) in str(err.value)
    assert world.connections == []
    assert world.t >= browser_connect.CONNECT_PATIENCE_SECONDS


async def test_our_own_chrome_on_the_port_is_attached(world, profile):
    """The other side of it: when the port is ours the file appears and the spawn
    attaches to exactly the websocket the file names."""
    browser = await _launch(profile)

    assert browser.connection.url == f"ws://127.0.0.1:{OUR_PORT}{OUR_PATH}"
    assert world.connections == [browser.connection.url]
    assert browser.config.port == OUR_PORT


async def test_chrome_keeps_the_concrete_port_in_its_argv(world, profile):
    """Pinned because the first version of this fix asked Chrome for port 0 and
    CI's offline-stealth gate then saw ``navigator.webdriver == true`` on every
    OS: the argv must keep the port nodriver chose."""
    await _launch(profile)

    assert world.requested_port() == str(OUR_PORT)
    assert sum(a.startswith("--remote-debugging-port") for a in world.argv) == 1


async def test_a_foreign_answer_on_our_port_is_refused_by_name(world, profile):
    """The second line of defence: Chrome wrote its port and path into OUR
    profile, but what answers on that port carries a different websocket path.
    The spawn must fail naming the conflict and must not build a connection."""
    world.endpoint_on_our_port = SIBLING_PATH

    with pytest.raises(ToolError, match="not the browser this spawn launched") as err:
        await _launch(profile)

    assert SIBLING_PATH in str(err.value)
    assert OUR_PATH in str(err.value)
    assert world.connections == []


async def test_a_stale_port_file_is_not_trusted(world, profile):
    """A file a dead Chrome left behind names the port a squatter now holds. It
    is not ours, so the port is never asked: we wait for the file THIS launch
    writes, which a Chrome that lost the race never does."""
    (profile / PORT_FILE).write_text(f"{OUR_PORT}\n{STALE_PATH}\n")
    world.sibling_takes_port = True

    with pytest.raises(ToolError, match="did not write"):
        await _launch(profile)

    assert world.requests == []
    assert world.connections == []


async def test_a_stale_file_that_matches_the_squatter_exactly_is_still_not_trusted(
    world, profile
):
    """The worst case for the identity check alone: the leftover file and the
    squatter agree with each other. Only deleting the file before the launch
    keeps that from being an attach to the squatter."""
    (profile / PORT_FILE).write_text(f"{OUR_PORT}\n{STALE_PATH}\n")
    world.endpoints[OUR_PORT] = STALE_PATH
    world.chrome_opens = False

    with pytest.raises(ToolError, match="did not write"):
        await _launch(profile)

    assert world.requests == []
    assert world.connections == []


async def test_a_chrome_that_never_writes_its_port_fails_by_name_inside_the_budget(
    world, profile
):
    world.chrome_opens = False

    with pytest.raises(ToolError, match=PORT_FILE) as err:
        await _launch(profile)

    assert str(profile) in str(err.value)
    # A ceiling and not a wait: the one patience constant plus nodriver's own
    # four remaining sleeps, and far inside the launch phase's budget.
    assert world.t >= browser_connect.CONNECT_PATIENCE_SECONDS
    assert world.t < browser_connect.CONNECT_PATIENCE_SECONDS + 5
    from stealth_chrome_devtools_mcp.embedded import spawn_budget

    assert world.t < spawn_budget.LAUNCH_BUDGET_SECONDS


async def test_a_launcher_that_exits_before_writing_its_port_is_named(world, profile):
    """Chrome handing off to a browser already on the profile exits at once and
    writes nothing; waiting out the ceiling on it would be the old 30 s lie."""
    world.chrome_opens = False
    world.process.returncode = 0

    with pytest.raises(ToolError, match="exited before"):
        await _launch(profile)

    assert world.t < browser_connect.CONNECT_PATIENCE_SECONDS


async def test_a_mismatch_never_touches_the_foreign_browser(
    monkeypatch, world, profile, tmp_path
):
    """``spawn_browser`` end to end over a process table: after the mismatch our
    own Chrome is reaped and the sibling's pid is never terminated. Nothing was
    registered, so ``close_instance`` has nothing to close either."""
    world.endpoint_on_our_port = SIBLING_PATH
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

    with pytest.raises(Exception, match="not the browser this spawn launched"):
        await manager.spawn_browser(_options(profile))

    assert table.procs[SIBLING_PID].terminate_calls == 0
    assert table.procs[SIBLING_PID].alive
    assert table.procs[OUR_PID].terminate_calls == 1
    assert world.connections == []
    assert manager._instances == {}
    assert await manager.close_instance("anything-the-caller-might-hold") is False
    assert table.procs[SIBLING_PID].terminate_calls == 0
