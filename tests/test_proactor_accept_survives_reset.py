"""T-6363 part 3: a client reset during accept must not close the listener.

asyncio's Proactor accept loop closes the LISTENING socket on any accept
``OSError`` and a client that resets while its connection is pending in
``AcceptEx`` completes the accept with ``WinError 64``. The backend (uvicorn on
Windows) then refuses every later connection while established ones stay up;
the 12-session startup herd died of it (CI run 39833151221).
``proactor_accept.install`` is the fix; these pins use the REAL
``ProactorEventLoop`` and real sockets, because a fake accept would encode the
bug (cf. the mocked-fakes rule).
"""

from __future__ import annotations

import ast
import asyncio
import socket
import struct
import sys
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import proactor_accept

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the Proactor loop only exists on Windows"
)

RESETS = 200


@pytest.fixture
def installed(monkeypatch):
    """The patch in force for ONE test; the stdlib method is restored after."""
    from asyncio import windows_events

    monkeypatch.setattr(
        windows_events.IocpProactor, "accept", windows_events.IocpProactor.accept
    )
    proactor_accept.install()
    return windows_events.IocpProactor


def _reset_storm(port: int) -> None:
    """Connect and close with SO_LINGER(1, 0): the close sends an RST."""
    for _ in range(RESETS):
        s = socket.socket()
        s.settimeout(1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        try:
            s.connect(("127.0.0.1", port))
        except TimeoutError:
            s.close()
            return  # a dead listener leaves connects queued: the storm has done its harm
        except OSError:
            pass
        s.close()


async def _serve_and_storm():
    async def handle(reader, writer):
        await reader.read(1)
        writer.write(b"ok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _reset_storm, port)
    await asyncio.sleep(0.5)  # let every reset complete its accept
    return server, port


async def _fresh_connection_is_served(port: int) -> bytes:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), 3
    )
    writer.write(b"x")
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(2), 3)
    writer.close()
    return answer


def test_the_listener_survives_a_storm_of_resets(installed):
    async def scenario():
        server, port = await _serve_and_storm()
        try:
            assert all(sock.fileno() != -1 for sock in server.sockets), (
                "a reset client closed the listening socket"
            )
            assert await _fresh_connection_is_served(port) == b"ok"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario(), loop_factory=asyncio.ProactorEventLoop)


def test_close_still_ends_the_accept_loop_without_spinning(installed):
    async def scenario():
        server, port = await _serve_and_storm()
        loop = asyncio.get_running_loop()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 3)
        await asyncio.sleep(0.2)
        assert not loop._accept_futures, "an accept future outlived server.close()"
        others = asyncio.all_tasks() - {asyncio.current_task()}
        assert not others, f"tasks leaked past close: {others}"
        with pytest.raises(OSError):
            await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 3)

    asyncio.run(scenario(), loop_factory=asyncio.ProactorEventLoop)


def _reset_error() -> OSError:
    return OSError(None, "The specified network name is no longer available", None, 64)


class _FakeProactor:
    """The two attributes the wrapper reads, and a scripted inner accept."""

    def __init__(self, loop, script):
        self._loop = loop
        self._script = list(script)
        self.issued = 0

    def original_accept(self, listener):
        self.issued += 1
        future = self._loop.create_future()
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            future.set_exception(outcome)
        elif outcome is not None:
            future.set_result(outcome)
        return future  # None: stays pending


class _Listener:
    def fileno(self) -> int:
        return 7


def _run(script, *, cancel=False):
    async def scenario():
        fake = _FakeProactor(asyncio.get_running_loop(), script)
        accept = proactor_accept._wrap(
            lambda self, listener: self.original_accept(listener)
        )
        outer = accept(fake, _Listener())
        if cancel:
            await asyncio.sleep(0)
            outer.cancel()
            await asyncio.sleep(0)
            return fake, outer
        await asyncio.wait({outer}, timeout=2)
        return fake, outer

    return asyncio.run(scenario())


def test_a_reset_is_retried_and_the_next_connection_is_returned():
    fake, outer = _run([_reset_error(), _reset_error(), ("conn", "addr")])
    assert outer.result() == ("conn", "addr")
    assert fake.issued == 3


def test_any_other_error_surfaces_unchanged():
    boom = OSError(None, "not a reset", None, 10038)
    fake, outer = _run([boom])
    assert outer.exception() is boom
    assert fake.issued == 1


def test_a_listener_that_resets_every_accept_is_surfaced_not_spun_on(monkeypatch):
    monkeypatch.setattr(proactor_accept, "MAX_CONSECUTIVE_RESETS", 3)
    fake, outer = _run([_reset_error() for _ in range(10)])
    assert isinstance(outer.exception(), OSError)
    assert fake.issued == 4  # the first accept plus three retries


def test_cancelling_the_loops_future_cancels_the_pending_accept():
    fake, outer = _run([None], cancel=True)
    assert outer.cancelled()
    assert fake.issued == 1


def test_install_is_idempotent(installed):
    first = installed.accept
    assert proactor_accept.install() is False
    assert installed.accept is first


def test_the_backend_installs_it_and_the_proxy_does_not():
    """The fix only protects a listener if the process that listens installs it.

    ``server.py`` is the backend's entry (and runs up to three times under
    runpy, hence the idempotent install); the stdio proxy never listens.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "stealth_chrome_devtools_mcp"

    def installs(path: Path) -> bool:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "install"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "proactor_accept"
            for node in ast.walk(tree)
        )

    assert installs(root / "embedded" / "server.py")
    assert not installs(root / "embedded" / "singleton.py")
