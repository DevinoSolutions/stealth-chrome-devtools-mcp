"""A REAL nodriver ``Connection`` and its REAL ``_listener`` over a fake websocket.

nodriver 0.50 made the ``Browser`` a ``Connection`` (``browser.py``:32), and gave
``Connection`` no URL constructor: ``Connection()`` then ``.websocket_url``, with
the socket opened by ``aopen`` and the session taken by ``attach``. Anything that
asks "what does this code do to a connection" is answered here by constructing
the library's own class, so a rename in the library fails the test that uses it
instead of passing against a double that kept the old name.

Only the two websocket methods ``Connection`` calls (``send``, ``recv``) are
ours. ``tests/test_cdp_reply_path.py`` pins the listener's own guarantees on this
harness; other tests use it when the behaviour under test is the connection's.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace

from nodriver.core.connection import Connection

#: Generous for a delivery that is a few local calls away, and short enough that a
#: REGRESSION reads as a failed node rather than a stalled lane.
BUDGET = 5.0


class FakeSocket:
    """The two methods of a websocket that ``Connection`` calls, and nothing else."""

    close_code = None

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.close_code = 1000

    def answer(self, request_id: int, result: dict) -> None:
        self.inbox.put_nowait(json.dumps({"id": request_id, "result": result}))

    def fail(self, request_id: int, message: str, code: int = -32000) -> None:
        error = {"code": code, "message": message}
        self.inbox.put_nowait(json.dumps({"id": request_id, "error": error}))


@contextlib.asynccontextmanager
async def wire() -> AsyncIterator[tuple[Connection, FakeSocket]]:
    """A real attached ``Connection`` whose real ``_listener`` reads a fake socket."""
    socket = FakeSocket()
    connection = Connection()
    connection.socket = socket
    connection.session_id = "session"
    connection.target = SimpleNamespace(target_id="target")
    connection._listener_task = asyncio.ensure_future(connection._listener())  # noqa: SLF001  PERMANENT(the real listener over a fake websocket is the thing under test)
    try:
        yield connection, socket
    finally:
        connection._listener_task.cancel()  # noqa: SLF001  PERMANENT(end the real listener)
        with contextlib.suppress(asyncio.CancelledError):
            await connection._listener_task  # noqa: SLF001  PERMANENT(end the real listener)


def listener_alive(connection: Connection) -> bool:
    return not connection._listener_task.done()  # noqa: SLF001  PERMANENT(is the listener alive is the question every wire node asks)


def registered(connection: Connection) -> int:
    """How many commands ``send`` still has registered (the F-883 witness)."""
    return len(connection._mapper)  # noqa: SLF001  PERMANENT(what send still has registered is the F-883 witness)


async def until(predicate: Callable[[], bool]) -> None:
    """Wait for *predicate*, bounded: a pin for a hang must fail, not hang."""

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), BUDGET)


class BrowserSocket(FakeSocket):
    """The BROWSER-level endpoint under a real ``Browser`` (``Browser.start`` ->
    ``attach``): it answers the handshake nodriver 0.50 performs on its own
    (``Target.attachToBrowserTarget``, with Chrome's ``attachedToTarget`` event
    AHEAD of the reply, which is how the connection learns its own target) and
    ``Target.getTargets`` from ``targets_now``. Everything else is answered ``{}``;
    a test overrides :meth:`respond` for the command it cares about.
    """

    def __init__(self, url: str | None = None, targets_now=None) -> None:
        super().__init__()
        self.url = url
        self.targets_now = targets_now or (list)
        #: Chrome scopes ``Target.setDiscoverTargets`` to the websocket it was sent on.
        self.discovering = False

    @property
    def methods(self) -> list[str]:
        return [frame["method"] for frame in self.sent]

    @property
    def disconnected(self) -> bool:
        return self.close_code is not None

    async def wait_closed(self) -> None:
        return None

    async def send(self, data: str) -> None:
        await super().send(data)
        frame = self.sent[-1]
        self.respond(frame["method"], frame["id"], frame.get("params", {}))

    def event(self, method: str, params: dict) -> None:
        self.inbox.put_nowait(json.dumps({"method": method, "params": params}))

    def respond(self, method: str, request_id: int, params: dict) -> None:
        if method == "Target.attachToBrowserTarget":
            info = {
                "targetId": "browser-target",
                "type": "browser",
                "title": "",
                "url": "",
                "attached": True,
                "canAccessOpener": False,
            }
            self.event(
                "Target.attachedToTarget",
                {
                    "sessionId": "session",
                    "targetInfo": info,
                    "waitingForDebugger": False,
                },
            )
            self.answer(request_id, {"sessionId": "session"})
        elif method == "Target.getTargets":
            infos = [target.to_json() for target in self.targets_now()]
            self.answer(request_id, {"targetInfos": infos})
        else:
            if method == "Target.setDiscoverTargets":
                self.discovering = True
            self.answer(request_id, {})
