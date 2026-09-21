"""F-900 — the stdio proxy's bridge rides the SDK's CURRENT client, through the
ONE transport seam, under a read policy a standing event stream can survive.

Two things were wrong with ``async with streamablehttp_client(url)``.

The visible one: at the pinned ``mcp`` 1.27.1 that function is
``@deprecated("Use `streamable_http_client` instead.")``, so every proxy start
raised a ``DeprecationWarning`` from our own call site. It is a MIGRATION and not
a bug fix — measured, the deprecated function does honour its two timeout
numbers (it builds ``httpx.Timeout(timeout, read=sse_read_timeout)`` and hands
the client down to the replacement); what it does not do is let us configure the
transport, because the replacement takes the ``httpx.AsyncClient`` itself.

The one that cost something: called with NO arguments the bridge inherited the
SDK's DEFAULTS — ``Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)`` —
and 300 s of ``read`` is a deadline on an IDLE standing GET event stream. The
backend sends nothing on that stream while a session is quiet and emits no SSE
keepalive, so the stream read-times-out, and ``handle_get_stream`` retries
``MAX_RECONNECTION_ATTEMPTS`` (2) times before returning for good — at DEBUG,
with nothing told to the client. :func:`test_a_bounded_read_permanently_abandons_
an_idle_event_stream` measures exactly that against a real loopback socket.

That is F-862's discriminator destroyed from the client side: its sweep spares a
session that holds a standing GET stream and reaps one that has none and has
been silent for ``ABANDONED_AFTER_SECONDS``, and its docstring promises "a live
proxy — even one idle for hours — is never touched". With the stream abandoned
after ~601 s, a live and perfectly healthy proxy IS eventually touched.

So the bridge's read clock is UNBOUNDED (``backend_client.BRIDGE_READ_TIMEOUT is
None``) and the whole of what bounds a bridge is left where it already lives: the
F-820 watchdog decides the backend is dead, ``proxy_selfheal`` heals, and a tool
call's own budget is ``tool_runtime._clamp_timeout`` + ``_with_cdp_timeout`` at
the tool body. A transport read timeout would be a SECOND answer to "is the
backend still there", and it answers wrong — an idle session is not a dead
backend.

Hermetic: the SDK is real and only the socket under it is ours (an
``httpx.MockTransport``, the pattern ``tests/test_stealthy_cli.py``'s
``TestSessionHygiene`` uses), except the one node that measures a read timeout,
which needs a real loopback socket on an OS-assigned port because a
``MockTransport`` has no clock to time out against. No real backend, port or
``~/.stealth-mcp`` record is touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import warnings
from pathlib import Path

import anyio
import pytest

import operator_fence
from stealth_chrome_devtools_mcp.embedded import backend_client, singleton

PORT = 41900
SESSION_ID = "bridge-session-under-test"
SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


# --------------------------------------------------------------------------
# a streamable-HTTP backend, answered at the HTTP layer
# --------------------------------------------------------------------------
class FakeBackend:
    """The far end of the bridge, faked at the SOCKET and nowhere above it.

    Nothing of the SDK's and nothing of ours is doubled: the real
    ``streamable_http_client`` negotiates a real session over a real
    ``httpx.AsyncClient``. ``tests/test_stealthy_cli.py``'s ``FakeBackend`` is
    the same shape for the CLI's side of the same seam.
    """

    def __init__(self) -> None:
        self.methods: list[str] = []
        self.rpc: list[str] = []
        self.deleted_session: str | None = None

    def transport(self):
        import httpx

        return httpx.MockTransport(self._handle)

    def _handle(self, request):
        import httpx

        self.methods.append(request.method)
        if request.method == "DELETE":
            self.deleted_session = request.headers.get("mcp-session-id")
            return httpx.Response(200)
        if request.method == "GET":
            # The fake DECLINES the standing stream. This fixture is about the
            # handshake; the stream itself is measured against a real socket
            # below. Deliberately not described as what our backend does — it
            # answers a valid GET with 200 + SSE and registers GET_STREAM_KEY
            # (mcp/server/streamable_http.py:659-728), which is this file's
            # whole subject — nor as something the SDK reads as "no stream":
            # `handle_get_stream`'s `raise_for_status` raises on a 405 and
            # burns both reconnection attempts. The branch is not reached today
            # (the fixture sends no `notifications/initialized`, so the SDK
            # never calls `start_get_stream`); it answers at all so that a
            # change which DOES open the stream fails loudly rather than
            # hanging on an unanswered request.
            return httpx.Response(405)
        message = json.loads(request.content)
        method = message.get("method", "")
        self.rpc.append(method)
        if "id" not in message:
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "fake-backend", "version": "0"},
                },
            },
            headers={
                "content-type": "application/json",
                "mcp-session-id": SESSION_ID,
            },
        )


def _init_msg(req_id):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    req = JSONRPCRequest(
        jsonrpc="2.0",
        id=req_id,
        method="initialize",
        params={
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "bridge-transport-test", "version": "1"},
        },
    )
    return SessionMessage(message=JSONRPCMessage(req))


# The tripwire is `operator_fence`'s, not this file's (F-903). It was declared
# here by F-900, with the `BaseException` argument that module's own guards are
# built on — two classes, one sentence, one finding apart. The reasoning moved
# with the symbol; what stays here is the ARMING, because a spawn cannot be
# refused suite-wide without deciding for the integration tier and the herd test.
# :meth:`TestTheFence.test_the_tripwire_ends_the_run_when_the_heal_path_is_reached`
# still proves this one is not swallowed.
_RealStartupReached = operator_fence.RealStartupReached


@pytest.fixture()
def bridged(monkeypatch, tmp_path):
    """Drive the REAL :func:`singleton._proxy_streams` against a fake socket.

    What is substituted is the transport and the things that would make the node
    reach the machine — readiness, the watchdog, and the whole heal path. The
    SDK client, the handshake and this module's own bridge code are all real.

    **The heal path is fenced off, and that fence is not paranoia.** A bridge
    that cannot connect is exactly what a RED run of this file looks like, and
    ``proxy_selfheal.drive`` answers a broken bridge by healing through
    ``singleton.ensure_server_running`` — the real startup path, which on the
    first RED of this file COLD-STARTED A REAL BACKEND on the developer's
    machine (pid 55240, port 21770, recorded in the real ``~/.stealth-mcp``).
    F-886 spared the two live siblings, so nothing was evicted; that was the
    rule working, not the test being safe. A hermetic proxy test must make the
    real startup path unreachable rather than merely unlikely.

    Two things make it unreachable, and they are not redundant: the
    ``heal_backend`` stub means the path is never walked, and
    :class:`_RealStartupReached` means that if a later change re-points that
    stub the run ENDS instead of quietly cold-starting a backend.
    """
    import httpx

    from stealth_chrome_devtools_mcp.embedded import proxy_selfheal

    backend = FakeBackend()
    asked: list[float | None] = []
    real_heal = proxy_selfheal.heal_backend

    def seam(read_seconds):
        asked.append(read_seconds)
        return httpx.AsyncClient(transport=backend.transport(), follow_redirects=True)

    monkeypatch.setattr(backend_client, "http_client", seam)

    async def always_ready(_url, *_a, **_kw):
        return True

    async def never_returns(_port, **_kw):
        await anyio.sleep_forever()

    monkeypatch.setattr(singleton, "_await_backend_http", always_ready)
    monkeypatch.setattr(singleton, "_watch_backend_liveness", never_returns)

    def _must_not_start(*_a, **_kw):
        raise _RealStartupReached(
            "a hermetic bridge test must never reach the real startup path"
        )

    async def _no_heal(_port, **_kw):
        return None

    monkeypatch.setattr(singleton, "ensure_server_running", _must_not_start)
    monkeypatch.setattr(singleton, "_same_identity_backend_ready", lambda _p: False)
    monkeypatch.setattr(proxy_selfheal, "heal_backend", _no_heal)
    monkeypatch.setattr(proxy_selfheal, "RETRY_BASE_SECONDS", 0.05)
    monkeypatch.setattr(proxy_selfheal, "RETRY_MAX_SECONDS", 0.05)
    # Belt and braces: nothing below may read or write the real record.
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")

    async def run() -> None:
        """One generation: handshake through the bridge, then tear it down."""
        c2p_tx, c2p_rx = anyio.create_memory_object_stream(50)
        p2c_tx, p2c_rx = anyio.create_memory_object_stream(50)
        async with anyio.create_task_group() as tg:
            tg.start_soon(singleton._proxy_streams, c2p_rx, p2c_tx, PORT)
            await c2p_tx.send(_init_msg(1))
            with anyio.fail_after(10):
                await p2c_rx.receive()  # the locally-answered initialize
                while "initialize" not in backend.rpc:
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

    return {
        "backend": backend,
        "asked": asked,
        "run": run,
        "monkeypatch": monkeypatch,
        "real_heal": real_heal,
        "proxy_selfheal": proxy_selfheal,
    }


def _flatten(error: BaseException) -> list[BaseException]:
    """Every exception in a (possibly nested) group, the group included."""
    found = [error]
    for child in getattr(error, "exceptions", ()):
        found.extend(_flatten(child))
    return found


# --------------------------------------------------------------------------
# the bridge calls the replacement
# --------------------------------------------------------------------------
class TestTheBridgeUsesTheCurrentClient:
    async def test_the_bridge_warns_about_nothing_from_its_own_call_site(self, bridged):
        """A proxy start must not raise a ``DeprecationWarning`` of our own
        making. Keyed on the SDK's own wording rather than on the category, so
        an unrelated third-party deprecation somewhere under the handshake does
        not make this node about something else."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            # A bridge on the deprecated client cannot reach the fake socket at
            # all (the seam is what carries it there), so it warns and THEN
            # times out. The warning is asserted first so that reversion is
            # reported as what it is rather than as a broken handshake.
            with contextlib.suppress(TimeoutError):
                await bridged["run"]()

        ours = [
            str(w.message)
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "streamable_http_client" in str(w.message)
        ]
        assert ours == [], f"the bridge is still on the deprecated client: {ours}"
        # ... and the node is not passing because nothing happened.
        assert "initialize" in bridged["backend"].rpc

    def test_nothing_under_src_imports_or_calls_the_deprecated_name(self):
        """One client, reached one way. A second call site would warn again from
        wherever it was added, and the SDK will eventually delete the name.

        By AST and deliberately NOT by grep: a grep makes this pin about the
        WORD, and the word is load-bearing in prose — ``backend_client``'s
        docstring names the deprecated function to explain why it is not used,
        and a reader who cannot grep for it cannot find that explanation. What
        may not appear is an import, a bare reference or an attribute access;
        what may is a sentence about it. (``profile_seed.LOGIN_WITNESSES`` is
        pinned the other way round, by grep including docstrings — there the
        second SPELLING was the defect. Here it is the second CALL.)
        """
        import ast

        deprecated = "streamablehttp_client"
        offenders: list[str] = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                used = (
                    (
                        isinstance(node, ast.ImportFrom)
                        and any(alias.name == deprecated for alias in node.names)
                    )
                    or (isinstance(node, ast.Name) and node.id == deprecated)
                    or (isinstance(node, ast.Attribute) and node.attr == deprecated)
                )
                if used:
                    offenders.append(f"{path.relative_to(SRC_ROOT)}:{node.lineno}")
        assert offenders == [], (
            f"`{deprecated}` is @deprecated at mcp 1.27.1; reach the SDK's "
            f"current client through backend_client instead: {offenders}"
        )

    def test_the_sdk_still_takes_a_client_and_terminates_on_close(self):
        """The two parameters the bridge depends on. A bump that renames either
        is a decision to make deliberately rather than discover in a log —
        ``tests/test_stealthy_cli.py`` asks the same question for the CLI's half
        of this seam, and F-900 gave it a second consumer."""
        import inspect

        import mcp.client.streamable_http as sdk

        params = inspect.signature(sdk.streamable_http_client).parameters
        assert "http_client" in params
        assert "terminate_on_close" in params
        assert getattr(sdk.streamablehttp_client, "__deprecated__", None), (
            "the deprecated alias lost its marker; re-read why the bridge moved"
        )


# --------------------------------------------------------------------------
# the fence itself
# --------------------------------------------------------------------------
class TestTheFence:
    @pytest.mark.timeout(60)
    async def test_the_tripwire_ends_the_run_when_the_heal_path_is_reached(
        self, bridged
    ):
        """The fence is load-bearing, so it is PROVEN rather than asserted.

        This node removes the ``heal_backend`` stub — the thing that actually
        keeps the real startup path out of reach today — and breaks the bridge,
        which is exactly what a RED run of this file looks like. What must
        happen is that :class:`_RealStartupReached` ENDS the run.

        Before review M2 it did not. The tripwire raised ``AssertionError``, so
        ``heal_backend``'s ``except Exception`` backstop (``proxy_selfheal.py``
        :325) swallowed it, logging ``heal attempt <n>/HEAL_ATTEMPTS failed``
        once per attempt of every heal round, and let the node pass — after
        ``ensure_server_running`` had already been entered. On 2026-09-21 that path cold-started pid 55240 on port 21770
        into the real ``~/.stealth-mcp``. Nothing here reaches a real backend:
        the tripwire replaces ``ensure_server_running`` itself, so it raises
        before that function's first statement.
        """
        import httpx

        mp = bridged["monkeypatch"]
        proxy_selfheal = bridged["proxy_selfheal"]

        def dead_socket(_request):
            raise httpx.ConnectError("there is no backend on this port")

        def broken_seam(_read_seconds):
            return httpx.AsyncClient(
                transport=httpx.MockTransport(dead_socket), follow_redirects=True
            )

        mp.setattr(backend_client, "http_client", broken_seam)
        mp.setattr(proxy_selfheal, "heal_backend", bridged["real_heal"])

        with pytest.raises(BaseException) as excinfo:  # noqa: B017, PT011  PERMANENT(F-900: the group's SHAPE is anyio's, the member is the contract)
            await bridged["run"]()

        reached = [
            error
            for error in _flatten(excinfo.value)
            if isinstance(error, _RealStartupReached)
        ]
        assert reached, (
            "the heal path reached the real startup path and the run did NOT "
            f"end — the tripwire is being swallowed again: {excinfo.value!r}"
        )


# --------------------------------------------------------------------------
# the transport is built through the ONE seam
# --------------------------------------------------------------------------
class TestTheBridgeTransport:
    async def test_the_bridge_asks_the_one_seam_for_its_client(self, bridged):
        """`backend_client.http_client` is THE one transport seam (F-891); the
        bridge is its second consumer rather than a second spelling of it."""
        await bridged["run"]()
        assert bridged["asked"] == [backend_client.BRIDGE_READ_TIMEOUT]

    async def test_the_two_clocks_reach_the_httpx_client(self):
        """Drives the REAL seam with the REAL argument the bridge passes. The
        fixture above deliberately does not assert on a client it built itself
        — that would pin the double."""
        client = backend_client.http_client(backend_client.BRIDGE_READ_TIMEOUT)
        try:
            assert client.timeout.read is None, (
                "a bridge read timeout is a second answer to 'is the backend "
                "there', and it answers wrong for an idle session"
            )
            assert client.timeout.connect == backend_client.CONNECT_TIMEOUT_SECONDS
            assert client.follow_redirects is True
        finally:
            await client.aclose()

    async def test_the_cli_budget_is_still_bounded_on_the_same_seam(self):
        """The seam grew a second consumer; the first one's contract is that a
        tool call waits under ``read`` for exactly the budget it was given."""
        client = backend_client.http_client(7.0)
        try:
            assert client.timeout.read == 7.0
            assert client.timeout.connect == backend_client.CONNECT_TIMEOUT_SECONDS
        finally:
            await client.aclose()


# --------------------------------------------------------------------------
# the read policy, and the measurement behind it
# --------------------------------------------------------------------------
class TestTheReadPolicy:
    def test_the_bridge_read_clock_is_unbounded_and_the_constant_says_why(self):
        """The VALUE is the decision, so it is pinned as a value. What bounds a
        bridge is the watchdog (F-820/F-838) and a tool's own CDP budget."""
        assert backend_client.BRIDGE_READ_TIMEOUT is None

    def test_the_sdk_gives_an_abandoned_event_stream_up_for_good(self):
        """The reason the policy cannot be a large number instead of ``None``.

        ``handle_get_stream`` retries a broken stream ``MAX_RECONNECTION_
        ATTEMPTS`` times and then RETURNS — it does not keep trying — so any
        finite read timeout eventually costs the standing stream permanently,
        just later. The bound is the SDK's; it is read here rather than
        restated."""
        import mcp.client.streamable_http as sdk

        assert sdk.MAX_RECONNECTION_ATTEMPTS == 2

    @pytest.mark.timeout(60)
    async def test_a_bounded_read_permanently_abandons_an_idle_event_stream(self):
        """MEASURED, against a real loopback socket on an OS-assigned port.

        The server answers ``initialize`` and then holds the standing GET stream
        open sending NOTHING — an idle MCP session exactly as our backend serves
        one (no SSE keepalive anywhere in ``mcp.server.streamable_http``). Under
        a BOUNDED read clock the stream is opened, times out, is retried once,
        times out again and is then abandoned for good; under the bridge's
        policy it is opened once and held.

        The clock is scaled down (0.3 s stands in for the SDK default's 300 s);
        what is measured is the SDK's BEHAVIOUR, not the number.
        """
        bounded = await _count_stream_opens(read_seconds=0.3, idle_seconds=2.6)
        unbounded = await _count_stream_opens(
            read_seconds=backend_client.BRIDGE_READ_TIMEOUT, idle_seconds=2.6
        )
        assert bounded == 2, (
            "a bounded read is expected to burn both reconnection attempts and "
            f"then give up; saw {bounded} opens"
        )
        assert unbounded == 1, (
            "the bridge's policy must hold ONE standing stream for the whole "
            f"idle window; saw {unbounded} opens"
        )


async def _count_stream_opens(*, read_seconds: float | None, idle_seconds: float):
    """Open a real session against a real socket and count how many times the
    SDK has to (re-)open the standing GET event stream while it sits idle."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    opens: list[float] = []
    started = time.monotonic()

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except Exception:  # noqa: BLE001  PERMANENT(F-900: a torn-down client is this node's happy path)
            return
        text = head.decode("latin-1")
        method = text.split(" ", 1)[0]
        if method == "GET":
            opens.append(time.monotonic() - started)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\nTransfer-Encoding: chunked\r\n\r\n"
            )
            await writer.drain()
            await asyncio.sleep(3600)  # idle forever: nothing to push, no keepalive
            return
        if method == "DELETE":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        length = 0
        for line in text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        body = await reader.readexactly(length) if length else b""
        request = json.loads(body or b"{}")
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "idle-backend", "version": "0"},
                },
            }
        )
        frame = f"event: message\ndata: {payload}\n\n".encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"mcp-session-id: idle-session\r\nCache-Control: no-cache\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        writer.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(b"0\r\n\r\n")
        await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(5.0, read=read_seconds)
        )
        async with client:
            async with streamable_http_client(
                f"http://127.0.0.1:{port}/mcp",
                http_client=client,
                terminate_on_close=False,  # the DELETE is not what is measured
            ) as (read, write, _get_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await asyncio.sleep(idle_seconds)
    finally:
        server.close()
    return len(opens)
