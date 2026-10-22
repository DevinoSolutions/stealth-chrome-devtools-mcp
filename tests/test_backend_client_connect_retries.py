"""F-1026 part 2 — ``backend_client.http_client`` re-dials a connection that was
refused before any request byte went out, and only that.

The seam under test is the real one (``backend_client.http_client``), talking to
a real loopback socket on an OS-assigned port. The one thing made to fail is the
FIRST dial: ``anyio.connect_tcp`` — what httpcore calls to open the socket —
raises ``ConnectionRefusedError`` once and then behaves, which is what a backend
that is mid-restart looks like to a client. With ``retries=0`` that first
refusal is the end (``httpx.ConnectError``); with the transport's connection
retries the second dial reaches the listener.
"""

from __future__ import annotations

import asyncio

import anyio
import httpx
import pytest

from stealth_chrome_devtools_mcp.embedded import backend_client

_REPLY = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"


async def _serve():
    """A tiny HTTP responder; returns (server, port, requests_seen)."""
    seen: list[bytes] = []

    async def handle(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(_REPLY)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], seen


def _refuse_the_first_dial(monkeypatch) -> list[int]:
    real = anyio.connect_tcp
    dials: list[int] = []

    async def flaky(*args, **kwargs):
        dials.append(1)
        if len(dials) == 1:
            raise ConnectionRefusedError("the backend is restarting")
        return await real(*args, **kwargs)

    monkeypatch.setattr(anyio, "connect_tcp", flaky)
    return dials


async def test_a_refused_first_dial_is_retried_and_the_request_succeeds(monkeypatch):
    server, port, seen = await _serve()
    dials = _refuse_the_first_dial(monkeypatch)
    try:
        async with backend_client.http_client(5.0) as client:
            reply = await client.get(f"http://127.0.0.1:{port}/health")
    finally:
        server.close()
        await server.wait_closed()
    assert (reply.status_code, reply.text) == (200, "ok")
    assert len(dials) == 2
    # The refused dial never carried the request: the server saw exactly one.
    assert len(seen) == 1


async def test_without_connection_retries_the_same_refusal_is_fatal(monkeypatch):
    """The contrast that makes the pin above mean something: the same flaky first
    dial through a ``retries=0`` transport raises, so the success above is the
    transport's retry and nothing else."""
    server, port, seen = await _serve()
    _refuse_the_first_dial(monkeypatch)
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0)
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"http://127.0.0.1:{port}/health")
    finally:
        server.close()
        await server.wait_closed()
    assert seen == []


async def test_a_request_that_was_sent_is_never_replayed(monkeypatch):
    """The safety half: a server that takes the request and then drops the
    connection yields a read error, and the tool is NOT run a second time."""
    seen: list[bytes] = []

    async def handle(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with backend_client.http_client(5.0) as client:
            with pytest.raises(httpx.TransportError):
                await client.post(f"http://127.0.0.1:{port}/mcp", content=b"{}")
    finally:
        server.close()
        await server.wait_closed()
    assert len(seen) == 1
