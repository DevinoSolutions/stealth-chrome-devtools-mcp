"""T-15387 — a Proactor transport closes its socket even when ``shutdown()`` raises.

The stock ``_ProactorBasePipeTransport._call_connection_lost`` calls
``sock.shutdown()`` and only then ``sock.close()`` in one ``finally``. A peer that
has reset makes ``shutdown`` raise ``ConnectionResetError`` (918 times in 2.3 h on
the shared backend), the rest of the ``finally`` is skipped, and the connection
sits in ``CLOSE_WAIT`` for the life of the process.

Two levels, both held to the same assertion (the socket is closed):

* a stand-in transport whose ``shutdown`` raises, which pins the exact state the
  stock body leaves behind (socket, server detach, flag) and that the exception
  still propagates unchanged;
* a REAL Proactor server, real clients that reset with ``SO_LINGER(1, 0)``, and
  ``shutdown`` made to raise ``ConnectionResetError`` on the server's sockets.
  The injection is a stand-in for the OS: on this Windows build a plain
  ``shutdown`` after an RST succeeds in every state tried, so the failure the
  production hosts show cannot be provoked from here without it (see the report
  on the task). Everything else is the real thing.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import sys
import threading
from asyncio import proactor_events
from types import SimpleNamespace

import pytest

from stealth_chrome_devtools_mcp.embedded import proactor_teardown

CLASS = proactor_events._ProactorBasePipeTransport
STOCK = CLASS._call_connection_lost
assert not getattr(STOCK, proactor_teardown._MARK, False), "patched before collection"

windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the Proactor loop and its install() are Windows-only",
)


@pytest.fixture()
def stock(monkeypatch):
    """The stock method, restored after the test whatever it installed."""
    monkeypatch.setattr(CLASS, "_call_connection_lost", STOCK)


@pytest.fixture()
def patched(stock):
    assert proactor_teardown.install() is True


class _Sock:
    """A socket whose ``shutdown`` raises, as a reset peer makes it."""

    def __init__(self, shutdown_error=None):
        self.shutdown_error = shutdown_error
        self.closed = False

    def fileno(self):
        return -1 if self.closed else 7

    def shutdown(self, how):
        if self.shutdown_error is not None:
            raise self.shutdown_error

    def close(self):
        self.closed = True


class _Server:
    def __init__(self):
        self.detached = []

    def _detach(self, *transport):
        self.detached.append(transport)


def _transport(sock, *, lost=None):
    def connection_lost(exc):
        if lost is not None:
            raise lost

    return SimpleNamespace(
        _called_connection_lost=False,
        _protocol=SimpleNamespace(connection_lost=connection_lost),
        _sock=sock,
        _server=_Server(),
    )


RESET = ConnectionResetError(10054, "reset by peer", None, 10054)


class TestTheStockBodyIsRecognised:
    def test_this_pythons_stock_body_matches_a_reference(self):
        """Every CI Python (3.11-3.13 here, 3.14 compared) is one of the two."""
        assert proactor_teardown._detach_takes_transport(STOCK) is (
            sys.version_info >= (3, 13)
        )

    def test_a_body_that_is_neither_is_refused_whole(self):
        def other(self, exc):
            self._sock.close()

        assert proactor_teardown._detach_takes_transport(other) is None

    def test_a_function_without_source_is_refused(self):
        assert proactor_teardown._detach_takes_transport(len) is None


@windows_only
class TestInstall:
    def test_install_is_idempotent(self, stock):
        assert proactor_teardown.install() is True
        assert proactor_teardown.install() is False

    def test_an_unknown_body_is_not_patched_and_warns(self, stock, monkeypatch, caplog):
        def other(self, exc):
            self._sock.close()

        monkeypatch.setattr(CLASS, "_call_connection_lost", other)
        with caplog.at_level(logging.WARNING, logger="stealth.backend"):
            assert proactor_teardown.install() is False
        assert CLASS._call_connection_lost is other
        assert [r.levelname for r in caplog.records] == ["WARNING"]
        assert "T-15387" in caplog.records[0].getMessage()

    def test_off_windows_it_does_nothing(self, stock, monkeypatch):
        monkeypatch.setattr(proactor_teardown.sys, "platform", "linux")
        assert proactor_teardown.install() is False
        assert CLASS._call_connection_lost is STOCK

    def test_the_backend_installs_it_beside_the_others(self):
        from pathlib import Path

        source = (
            Path(proactor_teardown.__file__)
            .with_name("server.py")
            .read_text(encoding="utf-8")
        )
        spawn = source.index("proactor_spawn.install()")
        teardown = source.index("proactor_teardown.install()")
        assert 0 < teardown - spawn < 200, "all three run in the http branch, together"


@windows_only
class TestWhatShutdownLeavesBehind:
    @pytest.mark.parametrize("takes_transport", [False, True])
    def test_the_socket_is_closed_the_server_detached_and_the_error_unchanged(
        self, takes_transport
    ):
        sock = _Sock(RESET)
        transport = _transport(sock)
        server = transport._server
        replacement = proactor_teardown._replacement(takes_transport)
        with pytest.raises(ConnectionResetError) as raised:
            replacement(transport, None)
        assert raised.value is RESET
        assert sock.closed
        assert transport._sock is None
        assert transport._server is None
        assert transport._called_connection_lost is True
        assert server.detached == [(transport,) if takes_transport else ()]

    def test_the_stock_body_leaves_all_of_it_undone(self, stock):
        """The defect, in the same terms (the RED half of the test above)."""
        sock = _Sock(RESET)
        transport = _transport(sock)
        with pytest.raises(ConnectionResetError):
            STOCK(transport, None)
        assert not sock.closed
        assert transport._server.detached == []
        assert transport._called_connection_lost is False

    def test_a_clean_shutdown_behaves_as_it_always_did(self, patched):
        sock = _Sock()
        transport = _transport(sock)
        server = transport._server
        CLASS._call_connection_lost(transport, None)
        assert sock.closed and transport._sock is None
        assert transport._called_connection_lost is True
        assert len(server.detached) == 1
        CLASS._call_connection_lost(transport, None)  # a second call is a no-op
        assert len(server.detached) == 1

    def test_a_protocol_that_raises_still_gets_its_socket_closed(self, patched):
        boom = RuntimeError("connection_lost failed")
        sock = _Sock()
        transport = _transport(sock, lost=boom)
        with pytest.raises(RuntimeError) as raised:
            CLASS._call_connection_lost(transport, None)
        assert raised.value is boom
        assert sock.closed and transport._called_connection_lost is True

    def test_both_raising_surfaces_shutdowns_error_chained_to_the_protocols(
        self, patched
    ):
        boom = RuntimeError("connection_lost failed")
        sock = _Sock(RESET)
        with pytest.raises(ConnectionResetError) as raised:
            CLASS._call_connection_lost(_transport(sock, lost=boom), None)
        assert raised.value.__context__ is boom
        assert sock.closed


CONNECTIONS = 25
DEADLINE_SECONDS = 10


class _Echo(asyncio.Protocol):
    sockets: list

    def connection_made(self, transport):
        type(self).sockets.append(transport.get_extra_info("socket"))
        transport.write(b"hi")


def _reset_one(port):
    """Connect, wait to be accepted (never reset a connection still pending in
    AcceptEx: that is T-6363), then reset it."""
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.recv(2)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()


@pytest.fixture()
def shutdown_fails_on_the_servers_sockets(monkeypatch):
    """``socket.shutdown`` raises ``ConnectionResetError`` for sockets bound to the
    server's port, as it does on the hosts that leaked; counts how often."""
    import _socket

    real = _socket.socket.shutdown
    state = SimpleNamespace(port=None, hits=0, lock=threading.Lock())

    def shutdown(self, how):
        try:
            ours = self.getsockname()[1] == state.port
        except OSError:
            ours = False
        if ours:
            with state.lock:
                state.hits += 1
            raise ConnectionResetError(10054, "reset by peer", None, 10054)
        return real(self, how)

    monkeypatch.setattr(socket.socket, "shutdown", shutdown)
    return state


async def _reset_clients_against_a_real_server(state):
    loop = asyncio.get_running_loop()
    assert isinstance(loop, asyncio.ProactorEventLoop)
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    _Echo.sockets = []
    server = await loop.create_server(_Echo, "127.0.0.1", 0)
    state.port = server.sockets[0].getsockname()[1]
    try:
        for _ in range(CONNECTIONS):
            await asyncio.to_thread(_reset_one, state.port)
        deadline = loop.time() + DEADLINE_SECONDS
        while loop.time() < deadline:
            if len(_Echo.sockets) == CONNECTIONS and state.hits == CONNECTIONS:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)  # let the last callbacks finish either way
        return [s.fileno() for s in _Echo.sockets], errors
    finally:
        server.close()


@windows_only
class TestRealResetsAgainstARealServer:
    def test_the_stock_loop_leaves_every_reset_connection_open(
        self, stock, shutdown_fails_on_the_servers_sockets
    ):
        """The defect, measured by the harness the fix is held to."""
        state = shutdown_fails_on_the_servers_sockets
        filenos, errors = asyncio.run(_reset_clients_against_a_real_server(state))
        assert state.hits == CONNECTIONS, "the failure was not provoked"
        assert len(filenos) == CONNECTIONS
        # All 25 on 3.13; 3.11-3.12 and 3.14 close some of them later by another
        # route (measured: 16, 8 and 13 of 25 stay open), so the defect is "some".
        assert any(fileno != -1 for fileno in filenos)
        assert len(errors) == CONNECTIONS

    def test_the_patched_loop_closes_every_one(
        self, patched, shutdown_fails_on_the_servers_sockets
    ):
        state = shutdown_fails_on_the_servers_sockets
        filenos, errors = asyncio.run(_reset_clients_against_a_real_server(state))
        assert state.hits == CONNECTIONS, "the failure was not provoked"
        assert len(filenos) == CONNECTIONS
        assert filenos == [-1] * CONNECTIONS, "a reset connection kept its socket"
        # The error behaviour is unchanged: each still reaches the loop's handler.
        assert len(errors) == CONNECTIONS
        assert all(isinstance(e["exception"], ConnectionResetError) for e in errors)
        assert all("_call_connection_lost" in repr(e["handle"]) for e in errors)
