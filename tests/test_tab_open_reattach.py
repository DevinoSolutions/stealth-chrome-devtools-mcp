"""T-8501 -- a dropped BROWSER websocket is re-attached once and the command retried.

nodriver 0.50's ``Connection.send`` re-attaches only when ``not self.attached or not
self.socket`` (``connection.py``:397). A socket that dropped is not ``None``: it has
``close_code`` set, so without ``tab_open`` every later browser-level command raised
``ConnectionClosed`` for the life of the instance -- F-940's field failure, back. These
run a REAL ``Connection`` and its real ``_listener`` over a socket that can drop; the
real wire (a real Chrome, a real close) is ``tests/test_e2e_new_tab_after_reconnect.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import websockets
from nodriver import cdp
from nodriver.core.connection import Connection
from websockets.exceptions import ConnectionClosedError

from fakes_wire import BrowserSocket, until
from stealth_chrome_devtools_mcp.embedded import tab_identity, tab_open

pytestmark = pytest.mark.asyncio

_DROPPED = "DROPPED"
TARGET = cdp.target.TargetID("T-1")


class DroppableSocket(BrowserSocket):
    """A browser endpoint that can lose its link, as a real websocket does: the
    reader ends with ``ConnectionClosed``, writes fail, and ``close_code`` is set."""

    def drop(self) -> None:
        self.close_code = 1006
        self.inbox.put_nowait(_DROPPED)

    async def recv(self) -> str:
        item = await self.inbox.get()
        if item == _DROPPED:
            raise ConnectionClosedError(None, None)
        return item

    async def send(self, data: str) -> None:
        if self.close_code is not None:
            raise ConnectionClosedError(None, None)
        await super().send(data)


class _Browser(Connection):
    """The part of ``nodriver.Browser`` ``tab_open`` uses: a ``Connection`` with a
    URL, ``update_targets`` over ``Target.getTargets``, and a tab list."""

    tabs: list[object] = []  # noqa: RUF012  PERMANENT(a fixed empty list on a test double)

    def __init__(self) -> None:
        super().__init__()
        self.websocket_url = "ws://fake.test/browser"
        self.updates = 0

    async def update_targets(self) -> None:
        await self.send(cdp.target.get_targets())
        self.updates += 1


class Dialer:
    """``websockets.connect``: every dial hands out a new socket."""

    def __init__(self) -> None:
        self.sockets: list[DroppableSocket] = []

    async def __call__(self, *_args: object, **_kwargs: object) -> DroppableSocket:
        socket = DroppableSocket()
        self.sockets.append(socket)
        return socket

    @property
    def live(self) -> DroppableSocket:
        return self.sockets[-1]


@pytest.fixture
def dialer(monkeypatch: pytest.MonkeyPatch) -> Dialer:
    dial = Dialer()
    monkeypatch.setattr(websockets, "connect", dial)
    return dial


async def _attached(armed: list[str] | None = None) -> _Browser:
    browser = _Browser()
    await browser.attach()

    async def arm(*args: object) -> None:
        if armed is not None:
            armed.append(str(args[-1]))

    await tab_open.arm_popups(browser, arm, "instance")
    return browser


async def _end(browser: _Browser) -> None:
    tab_open.mark_closed(browser)
    await browser.aclose()


def _count(socket: DroppableSocket, method: str) -> int:
    return socket.methods.count(method)


async def test_a_dropped_socket_is_reattached_and_the_command_runs(dialer):
    browser = await _attached()
    first = dialer.live
    first.drop()
    await until(lambda: browser._listener_task.done())  # noqa: SLF001  PERMANENT(the reader ending is the drop's witness)

    await tab_open.update_targets(browser)

    assert len(dialer.sockets) == 2
    assert dialer.live is not first
    assert browser.updates == 1
    assert _count(dialer.live, "Target.attachToBrowserTarget") == 1
    await _end(browser)


async def test_the_popup_listener_and_discovery_survive_the_reattach(dialer):
    seen: list[str] = []
    browser = await _attached(seen)
    destroyed: list[object] = []
    browser.add_handler(cdp.target.TargetDestroyed, destroyed.append)
    dialer.live.drop()
    await until(lambda: browser._listener_task.done())  # noqa: SLF001  PERMANENT(the reader ending is the drop's witness)

    await tab_open.update_targets(browser)

    live = dialer.live
    assert live.discovering, "discovery is per websocket: the new one must ask again"
    assert len(browser.handlers[cdp.target.TargetCreated]) == 1
    assert destroyed.append in browser.handlers[cdp.target.TargetDestroyed]
    live.event(
        "Target.targetCreated",
        {
            "targetInfo": {
                "targetId": "popup",
                "type": "page",
                "title": "",
                "url": "",
                "attached": False,
                "canAccessOpener": True,
                "openerId": "opener",
            }
        },
    )
    await until(lambda: browser.updates >= 2)  # _arm_popup asked for the target
    assert seen == [], "no Tab was listed for it, so nothing was armed"
    await _end(browser)


async def test_a_write_that_finds_the_link_closed_is_retried_once(dialer):
    browser = await _attached()
    first = dialer.live
    first.close_code = 1006  # the write below is the first to notice

    await tab_open.send(browser, cdp.target.activate_target, TARGET)

    assert len(dialer.sockets) == 2
    assert _count(dialer.live, "Target.activateTarget") == 1
    await _end(browser)


async def test_the_retry_is_bounded_to_one(dialer, monkeypatch):
    browser = await _attached()
    dialer.live.drop()
    await until(lambda: browser._listener_task.done())  # noqa: SLF001  PERMANENT(the reader ending is the drop's witness)

    class Dies(DroppableSocket):
        async def send(self, data: str) -> None:
            raise ConnectionClosedError(None, None)

    async def dial_dead(*_args: object, **_kwargs: object) -> DroppableSocket:
        socket = Dies()
        dialer.sockets.append(socket)
        return socket

    monkeypatch.setattr(websockets, "connect", dial_dead)
    with pytest.raises((ConnectionClosedError, ConnectionError, RuntimeError)):
        await asyncio.wait_for(tab_open.update_targets(browser), 10)
    assert len(dialer.sockets) <= 3, "one re-attach, one retry, then the failure"
    await _end(browser)


async def test_concurrent_callers_share_one_reattach(dialer):
    browser = await _attached()
    dialer.live.drop()
    await until(lambda: browser._listener_task.done())  # noqa: SLF001  PERMANENT(the reader ending is the drop's witness)

    await asyncio.gather(*(tab_open.update_targets(browser) for _ in range(4)))

    assert len(dialer.sockets) == 2
    assert browser.updates == 4
    await _end(browser)


async def test_arm_popups_twice_leaves_one_listener(dialer):
    browser = await _attached()

    async def arm(*_args: object) -> None:
        return None

    await tab_open.arm_popups(browser, arm, "instance")

    assert len(browser.handlers[cdp.target.TargetCreated]) == 1
    await _end(browser)


async def test_a_closed_connection_is_never_reopened(dialer):
    browser = await _attached()
    await _end(browser)  # mark_closed, then aclose: socket is None
    dials = len(dialer.sockets)

    with pytest.raises(ConnectionError):
        await tab_open.update_targets(browser)
    with pytest.raises(ConnectionError):
        await tab_open.send(browser, cdp.target.activate_target, TARGET)

    assert len(dialer.sockets) == dials, "nothing may reopen a connection we closed"
    assert browser.socket is None


async def test_a_popup_announced_after_close_is_not_armed(dialer):
    seen: list[str] = []
    browser = await _attached(seen)
    handler = browser.handlers[cdp.target.TargetCreated][0]
    await _end(browser)
    info = cdp.target.TargetInfo.from_json(
        {
            "targetId": "popup",
            "type": "page",
            "title": "",
            "url": "",
            "attached": False,
            "canAccessOpener": True,
            "openerId": "opener",
        }
    )
    handler(cdp.target.TargetCreated(info))
    await asyncio.sleep(0.05)

    assert seen == []
    assert len(dialer.sockets) == 1, "the lingering task must not reconnect"


async def test_a_tab_refresh_right_after_a_drop_does_not_fail(dialer):
    """``tab_identity.refreshed`` runs right after navigate and tab operations."""
    browser = await _attached()
    dialer.live.drop()
    await until(lambda: browser._listener_task.done())  # noqa: SLF001  PERMANENT(the reader ending is the drop's witness)
    tab = SimpleNamespace(target=SimpleNamespace(target_id="T", title="t"), url="u")

    view = await tab_identity.refreshed(browser, tab)

    assert view["tab_id"] == "T"
    assert len(dialer.sockets) == 2
    await _end(browser)
