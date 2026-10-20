"""F-1009 — the DevTools port a spawn connects to must be the one ITS OWN Chrome
opened, never one a sibling happens to hold.

nodriver picks the port by binding and releasing a socket (``util.free_port``)
and only then launches Chrome with ``--remote-debugging-port=<that port>``. In
the gap another process can take it — most often a sibling Chrome that a
concurrent spawn is starting — and our Chrome then fails to open its endpoint
while the sibling's answers ``/json/version``. nodriver cannot tell, so the
spawn registered the SIBLING's browser as its own and a later ``close_instance``
killed it: on the shared ``fleet`` or ``default`` session that is the logins.

The fix is not a bigger retry. Chrome is asked for port ``0`` and the real port
is read from ``DevToolsActivePort`` inside OUR profile directory; what answers
there must carry the websocket path Chrome wrote, or it is somebody else's.

Every node drives nodriver's REAL ``Browser.start`` through the REAL
``BrowserManager._launch_browser`` over a faked OS: a subprocess that "binds" the
port its argv names, a table of endpoints, a virtual clock and the process
table. No Chrome is launched, no socket is opened. The fake Chrome reads its
OWN argv, so a launcher that still passes a pre-picked port gets the squatter
and a launcher that passes ``0`` gets its own.
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
OUR_PORT = 45000
OUR_PATH = "/devtools/browser/ours-f1009"
SIBLING_PATH = "/devtools/browser/sibling-f1009"
STALE_PORT = 46000
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
        #: Every port NOT in ``endpoints`` answers as a sibling's browser — the
        #: port nodriver pre-picked and somebody else took.
        self.squatter_everywhere = True
        #: What a sibling's endpoint reports for OUR port (a second squatter).
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
        if self.requested_port() != "0" or OUR_PORT in self.endpoints:
            return
        self.endpoints[OUR_PORT] = self.endpoint_on_our_port
        (self.profile / PORT_FILE).write_text(f"{OUR_PORT}\n{OUR_PATH}\n")

    # -- /json/version -----------------------------------------------------
    async def request(self, api, endpoint_name, method="get", data=None):
        import urllib.error

        self.requests.append(api.port)
        path = self.endpoints.get(api.port)
        if path is None and self.squatter_everywhere and api.port != OUR_PORT:
            path = SIBLING_PATH
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
    """THE pin. The port nodriver picked is held by a sibling's browser; our
    Chrome, asked for whatever port it likes, opens its own and writes it into
    our profile. The spawn must end up on OUR endpoint, not the sibling's."""
    browser = await _launch(profile)

    assert browser.connection.url == f"ws://127.0.0.1:{OUR_PORT}{OUR_PATH}"
    assert world.connections == [browser.connection.url]
    # The port is the real one everywhere it is read afterwards — the registry
    # records it, and a later backend re-attaches through it (F-888).
    assert browser.config.port == OUR_PORT


async def test_chrome_is_not_handed_a_port_somebody_else_can_take(world, profile):
    await _launch(profile)

    assert world.requested_port() == "0"
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
    """A file a dead Chrome left behind names a port a squatter now holds. It is
    not ours, so it is never asked — we wait for the file THIS launch writes."""
    (profile / PORT_FILE).write_text(f"{STALE_PORT}\n{STALE_PATH}\n")
    world.endpoints[STALE_PORT] = SIBLING_PATH

    browser = await _launch(profile)

    assert STALE_PORT not in world.requests
    assert browser.connection.url == f"ws://127.0.0.1:{OUR_PORT}{OUR_PATH}"


async def test_a_stale_port_file_alone_is_not_an_endpoint(world, profile):
    (profile / PORT_FILE).write_text(f"{STALE_PORT}\n{STALE_PATH}\n")
    world.endpoints[STALE_PORT] = STALE_PATH
    world.chrome_opens = False

    with pytest.raises(ToolError, match=PORT_FILE):
        await _launch(profile)

    assert STALE_PORT not in world.requests
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
