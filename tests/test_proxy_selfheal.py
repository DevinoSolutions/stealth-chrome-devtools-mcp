"""F-838 — a CONFIRMED-dead backend must be healed, not answered with an exit.

Before this, the stdio proxy's only reaction to the liveness watchdog's
confirmed-dead verdict (F-820's three strikes plus the identity+readiness
confirmation) was to log "backend became unreachable; tearing down for
reconnect" and return. That premise — the MCP client respawns a stdio server
mid-session — does not hold, so every real backend death (2026-08-30: an OOM
crash at 15:46, a console CTRL_BREAK at 18:43) surfaced as a dead MCP server
until the user ran `/mcp` by hand.

What this file pins:

  * ``heal_backend`` gets a replacement through the SAME startup path the proxy
    used at boot (``singleton.ensure_server_running``), off-thread, and is
    BOUNDED — a fixed number of attempts, never a tight retry loop.
  * ``drive`` runs one backend GENERATION at a time: a confirmed death heals and
    starts the next generation, an unhealable one returns so the caller runs the
    pre-F-838 teardown, and a BUSY backend (the watchdog simply never returns)
    heals nothing at all.
  * F-843: the WATCHDOG is not the only witness. Every node here used to drive a
    fake ``watch`` — the slow-death path — while the death users actually felt
    (a backend killed with a client call in flight) is noticed by the BRIDGE leg
    first, ~11s before the watchdog can conclude. Those generations must run the
    same dead-vs-busy confirmation and heal on either verdict, while a
    never-armed generation and an outer cancellation still exit untouched.
  * ``_proxy_streams`` really re-bridges: a second transport is opened against
    the NEW port, a fresh ``initialize`` handshake is replayed on it (that is
    what mints the new ``mcp-session-id``), and a tool call issued afterwards is
    served — while the client's stdio side never noticed.
  * The herd serializes on the cold-start lock the startup path already holds:
    two proxies healing at once produce exactly ONE spawn.

Everything is hermetic: the "backend" is a pair of memory streams, the cold-start
lock is redirected into ``tmp_path``, and no real backend, port or ``~/.stealth-mcp``
record is touched.
"""

from __future__ import annotations

import contextlib
import threading

import anyio
import pytest

from stealth_chrome_devtools_mcp.embedded import proxy_selfheal, singleton

PORT_A = 41001
PORT_B = 41002


# --------------------------------------------------------------------------
# message helpers (the same shapes test_proxy_backend_death.py uses)
# --------------------------------------------------------------------------
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
            "clientInfo": {"name": "selfheal-test", "version": "1"},
        },
    )
    return SessionMessage(message=JSONRPCMessage(req))


def _request(req_id, method):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    req = JSONRPCRequest(jsonrpc="2.0", id=req_id, method=method, params={})
    return SessionMessage(message=JSONRPCMessage(req))


def _response(req_id, result):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCResponse

    resp = JSONRPCResponse(jsonrpc="2.0", id=req_id, result=result)
    return SessionMessage(message=JSONRPCMessage(resp))


class _Sink:
    """A client_write stand-in that just records what the proxy sends back."""

    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


# --------------------------------------------------------------------------
# heal_backend — bounded, and routed through the startup path
# --------------------------------------------------------------------------
class TestHealBackend:
    async def test_returns_the_replacement_port_on_the_first_success(self):
        async def ready(_url, *_a):
            return True

        port = await proxy_selfheal.heal_backend(
            PORT_A,
            ensure_running=lambda _preferred: PORT_B,
            await_ready=ready,
            url_for=lambda p: f"http://127.0.0.1:{p}/mcp/",
        )

        assert port == PORT_B

    async def test_prefers_the_dead_port_so_the_herd_converges(self):
        """Every proxy on the dead backend asks for the SAME port, which is what
        makes the cold-start lock's winner/adopter split work at all."""
        asked = []

        async def ready(_url, *_a):
            return True

        await proxy_selfheal.heal_backend(
            PORT_A,
            ensure_running=lambda preferred: asked.append(preferred) or PORT_A,
            await_ready=ready,
            url_for=lambda p: str(p),
        )

        assert asked == [PORT_A]

    async def test_is_bounded_and_gives_up_instead_of_retrying_forever(
        self, monkeypatch
    ):
        calls = []

        async def never_ready(_url, *_a):
            return False

        monkeypatch.setattr(proxy_selfheal, "HEAL_BACKOFF_SECONDS", 0.0)
        healed = await proxy_selfheal.heal_backend(
            PORT_A,
            ensure_running=lambda p: calls.append(p) or PORT_B,
            await_ready=never_ready,
            url_for=str,
        )

        assert healed is None
        assert len(calls) == proxy_selfheal.HEAL_ATTEMPTS

    async def test_a_raising_startup_path_is_survived_not_propagated(self, monkeypatch):
        def boom(_preferred):
            raise OSError("no fd for you")

        async def ready(_url, *_a):
            return True

        monkeypatch.setattr(proxy_selfheal, "HEAL_BACKOFF_SECONDS", 0.0)
        assert (
            await proxy_selfheal.heal_backend(
                PORT_A, ensure_running=boom, await_ready=ready, url_for=str
            )
            is None
        )

    async def test_the_blocking_startup_path_runs_off_thread(self, monkeypatch):
        """``ensure_server_running`` probes sockets and can hold for a cold
        start; run inline it would freeze the stdio pump."""
        seen = []

        async def fake_run_sync(fn, *args):
            seen.append((fn, args))
            return PORT_B

        async def ready(_url, *_a):
            return True

        monkeypatch.setattr(anyio.to_thread, "run_sync", fake_run_sync)
        marker = object()

        await proxy_selfheal.heal_backend(
            PORT_A,
            ensure_running=marker,
            await_ready=ready,
            url_for=str,
        )

        assert seen == [(marker, (PORT_A,))]


# --------------------------------------------------------------------------
# drive — the generation loop
# --------------------------------------------------------------------------
def _drive_kwargs(**overrides):
    async def connect(_url, _replay, armed):
        armed.set()
        await anyio.sleep_forever()

    async def watch(_port):
        await anyio.sleep_forever()

    kwargs = {
        "port": PORT_A,
        "url_for": lambda p: f"http://127.0.0.1:{p}/mcp/",
        "connect": connect,
        "watch": watch,
        # F-843: the dead-vs-busy confirmation a bridge-first death now runs.
        # Default "dead" — the premise of every death case below, and stating it
        # keeps the real ~/.stealth-mcp record off the hermetic lane entirely.
        "confirm_alive": lambda _port: False,
        "replay": lambda: None,
        "pending": proxy_selfheal.PendingCalls(),
        "client_write": _Sink(),
        "ensure_running": lambda p: p,
        "await_ready": None,
    }
    kwargs.update(overrides)
    return kwargs


class TestDrive:
    async def test_a_confirmed_death_heals_and_opens_the_next_generation(
        self, monkeypatch
    ):
        generations = []

        async def connect(url, _replay, armed):
            generations.append(url)
            armed.set()
            await anyio.sleep_forever()

        deaths = [True, False]

        async def watch(_port):
            if deaths.pop(0):
                return  # confirmed dead
            await anyio.sleep_forever()

        async def fake_heal(dead_port, **_kw):
            assert dead_port == PORT_A
            return PORT_B

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.move_on_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(connect=connect, watch=watch))

        assert len(generations) == 2
        assert str(PORT_A) in generations[0]
        assert str(PORT_B) in generations[1]

    async def test_a_bridge_failure_confirmed_dead_heals_the_next_generation(
        self, monkeypatch
    ):
        """F-843 REGRESSION PIN. When the backend dies with a client call in
        flight the BRIDGE leg notices first — the watchdog is still inside its
        first strike, so ``watch`` never concludes. Before the fix that
        generation ended unconfirmed and the proxy tore down ~1s after the kill:
        a permanent client disconnect. It must instead run the SAME dead-vs-busy
        confirmation the watchdog uses and, on a DEAD verdict, heal."""
        generations = []
        breaks = [True, False]

        async def connect(url, _replay, armed):
            generations.append(url)
            armed.set()
            if breaks.pop(0):
                raise ConnectionResetError("backend went away mid-call")
            await anyio.sleep_forever()

        async def watch(_port):
            await anyio.sleep_forever()  # mid-first-strike: no verdict yet

        async def fake_heal(dead_port, **_kw):
            assert dead_port == PORT_A
            return PORT_B

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.move_on_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(connect=connect, watch=watch))

        assert len(generations) == 2, "the bridge-first death must reach the heal"
        assert str(PORT_B) in generations[1], "must re-bridge onto the NEW port"

    async def test_a_bridge_failure_over_a_live_backend_still_converges(
        self, monkeypatch
    ):
        """The other verdict. A broken leg over a backend the gate still calls
        alive is a transient break, not a death — and it recovers through the
        SAME path, because ``ensure_running`` reuses a live same-identity backend
        rather than spawning beside it. One recovery, two causes."""
        generations = []
        breaks = [True, False]
        asked = []

        async def connect(url, _replay, armed):
            generations.append(url)
            armed.set()
            if breaks.pop(0):
                raise ConnectionResetError("the leg broke, the backend did not")
            await anyio.sleep_forever()

        async def watch(_port):
            await anyio.sleep_forever()

        async def fake_heal(dead_port, **_kw):
            asked.append(dead_port)
            return dead_port  # the one gate hands back the SAME live backend

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.move_on_after(5):
            await proxy_selfheal.drive(
                **_drive_kwargs(
                    connect=connect, watch=watch, confirm_alive=lambda _p: True
                )
            )

        assert asked == [PORT_A], "recovery must still route through the one gate"
        assert len(generations) == 2, "the client must be re-bridged, not dropped"
        assert str(PORT_A) in generations[1], "onto the backend that is still there"

    async def test_a_bridge_failure_before_readiness_is_not_an_incident(
        self, monkeypatch
    ):
        """``armed`` is the discriminator's other half: a backend that never
        answered an initialize was never ours to lose, so the pre-F-838 answer
        stands — return at once instead of spending a recovery budget on a cold
        start that already had its own 120s."""
        heals = []
        confirmations = []

        async def connect(_url, _replay, _armed):
            return  # readiness never came; armed stays unset

        async def fake_heal(dead_port, **_kw):
            heals.append(dead_port)
            return PORT_B

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.fail_after(5):
            await proxy_selfheal.drive(
                **_drive_kwargs(
                    connect=connect,
                    confirm_alive=lambda p: confirmations.append(p) or False,
                )
            )

        assert heals == []
        assert confirmations == [], "nothing was serving us; nothing to confirm"

    async def test_the_client_going_away_still_exits_without_healing(self, monkeypatch):
        """The client EOF reaches ``drive`` as a cancellation from OUTSIDE (the
        proxy's outer task group cancels it when ``pump_client`` returns). That
        must unwind straight through — never be caught by the confirmation and
        never heal a backend nobody is left to talk to."""
        heals = []
        confirmations = []

        async def connect(_url, _replay, armed):
            armed.set()
            await anyio.sleep_forever()

        async def watch(_port):
            await anyio.sleep_forever()

        async def fake_heal(dead_port, **_kw):
            heals.append(dead_port)
            return PORT_B

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        async def run():
            await proxy_selfheal.drive(
                **_drive_kwargs(
                    connect=connect,
                    watch=watch,
                    confirm_alive=lambda p: confirmations.append(p) or False,
                )
            )

        async with anyio.create_task_group() as outer:
            outer.start_soon(run)
            await anyio.sleep(0.05)
            outer.cancel_scope.cancel()  # what pump_client's return does

        assert heals == [], "a departed client must not be healed for"
        assert confirmations == [], "cancellation is not a verdict to confirm"

    async def test_an_unhealable_death_returns_for_the_legacy_teardown(
        self, monkeypatch
    ):
        async def watch(_port):
            return  # confirmed dead, immediately

        async def fake_heal(_dead_port, **_kw):
            return None

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.fail_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(watch=watch))

    async def test_a_busy_backend_never_heals(self, monkeypatch):
        """F-820's distinction survives: 'busy' is the watchdog simply not
        returning, so no generation ever ends and heal is never reached."""
        heals = []

        async def fake_heal(dead_port, **_kw):
            heals.append(dead_port)

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.move_on_after(0.5):
            await proxy_selfheal.drive(**_drive_kwargs())

        assert heals == []

    async def test_a_flapping_backend_stops_being_healed(self, monkeypatch):
        """Every replacement dies at once: healing forever would be the tight
        retry loop in slow motion, so the streak is capped and the caller tears
        down instead."""
        heals = []

        async def watch(_port):
            return  # every generation is confirmed dead immediately

        async def fake_heal(dead_port, **_kw):
            heals.append(dead_port)
            return dead_port + 1

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.fail_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(watch=watch))

        assert len(heals) == proxy_selfheal.MAX_CONSECUTIVE_HEALS

    async def test_a_generation_that_lived_earns_the_heal_budget_back(
        self, monkeypatch
    ):
        """A proxy up for hours must keep being healed; only back-to-back
        deaths exhaust the budget."""
        monkeypatch.setattr(proxy_selfheal, "HEAL_STREAK_RESET_SECONDS", 0.0)
        heals = []

        async def watch(_port):
            return

        async def fake_heal(dead_port, **_kw):
            heals.append(dead_port)
            return None if len(heals) > proxy_selfheal.MAX_CONSECUTIVE_HEALS else 1

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.fail_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(watch=watch))

        assert len(heals) == proxy_selfheal.MAX_CONSECUTIVE_HEALS + 1

    async def test_inflight_calls_are_failed_not_replayed(self, monkeypatch):
        sink = _Sink()
        pending = proxy_selfheal.PendingCalls()
        pending.track(_request(7, "tools/call").message.root)

        async def watch(_port):
            return

        async def fake_heal(_dead_port, **_kw):
            return None

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.fail_after(5):
            await proxy_selfheal.drive(
                **_drive_kwargs(watch=watch, pending=pending, client_write=sink)
            )

        assert len(sink.sent) == 1
        err = sink.sent[0].message.root
        assert err.id == 7
        assert "tools/call" in err.error.message
        assert "NOT retried" in err.error.message


class TestPendingCalls:
    async def test_an_answered_call_is_no_longer_in_flight(self):
        sink = _Sink()
        pending = proxy_selfheal.PendingCalls()
        pending.track(_request(1, "tools/list").message.root)
        pending.settle(_response(1, {}).message.root)

        await pending.fail_all(sink, PORT_A)

        assert sink.sent == []

    async def test_failures_are_reported_once(self):
        sink = _Sink()
        pending = proxy_selfheal.PendingCalls()
        pending.track(_request(1, "tools/list").message.root)

        await pending.fail_all(sink, PORT_A)
        await pending.fail_all(sink, PORT_A)

        assert len(sink.sent) == 1


# --------------------------------------------------------------------------
# _proxy_streams — the real re-bridge, over a fake streamable-HTTP transport
# --------------------------------------------------------------------------
class _Link:
    """One backend generation's wire: the streams the proxy sees plus the far
    ends the test drives as the backend itself."""

    def __init__(self, url):
        self.url = url
        down_tx, self.proxy_read = anyio.create_memory_object_stream(50)
        self.proxy_write, up_rx = anyio.create_memory_object_stream(50)
        self._down_tx = down_tx
        self._up_rx = up_rx

    async def recv(self):
        return await self._up_rx.receive()

    async def send(self, msg):
        await self._down_tx.send(msg)


def _fake_streamable_client(links):
    @contextlib.asynccontextmanager
    async def _open(link):
        yield (link.proxy_read, link.proxy_write, lambda: None)

    def factory(url, *_a, **_kw):
        link = _Link(url)
        links.append(link)
        return _open(link)

    return factory


async def _await_link(links, index):
    with anyio.fail_after(5):
        while len(links) <= index:
            await anyio.sleep(0.01)
    return links[index]


@pytest.fixture()
def wired_proxy(monkeypatch):
    """`_proxy_streams` with its HTTP transport and readiness probe faked."""
    from mcp.client import streamable_http

    links = []
    monkeypatch.setattr(
        streamable_http, "streamablehttp_client", _fake_streamable_client(links)
    )

    async def always_ready(_url, *_a, **_kw):
        return True

    monkeypatch.setattr(singleton, "_await_backend_http", always_ready)

    death = anyio.Event()
    verdicts = {"n": 0}

    async def watch(_port, **_kw):
        # The FIRST backend dies once; the replacement is healthy, so the
        # watchdog watching it simply never returns (F-820's "busy/alive" shape).
        await death.wait()
        verdicts["n"] += 1
        if verdicts["n"] > 1:
            await anyio.sleep_forever()

    monkeypatch.setattr(singleton, "_watch_backend_liveness", watch)
    return {"links": links, "death": death, "monkeypatch": monkeypatch}


class TestProxyStreamsRebridges:
    async def test_rebridges_to_a_new_port_and_serves_a_later_request(
        self, wired_proxy
    ):
        links, death = wired_proxy["links"], wired_proxy["death"]
        healed = []

        async def fake_heal(dead_port, **_kw):
            healed.append(dead_port)
            return PORT_B

        wired_proxy["monkeypatch"].setattr(proxy_selfheal, "heal_backend", fake_heal)

        c2p_tx, c2p_rx = anyio.create_memory_object_stream(50)
        p2c_tx, p2c_rx = anyio.create_memory_object_stream(50)

        async with anyio.create_task_group() as tg:
            tg.start_soon(singleton._proxy_streams, c2p_rx, p2c_tx, PORT_A)

            # --- generation 1: real handshake, one served call --------------
            await c2p_tx.send(_init_msg(1))
            with anyio.fail_after(5):
                await p2c_rx.receive()  # the locally-answered initialize
            gen1 = await _await_link(links, 0)
            assert str(PORT_A) in gen1.url
            with anyio.fail_after(5):
                first = await gen1.recv()
                assert first.message.root.method == "initialize"
                await gen1.send(_response(1, {"protocolVersion": "2025-03-26"}))

                await c2p_tx.send(_request(2, "tools/list"))
                assert (await gen1.recv()).message.root.method == "tools/list"
                await gen1.send(_response(2, {"tools": []}))
                assert (await p2c_rx.receive()).message.root.id == 2

            # --- the backend dies; the proxy must NOT exit ------------------
            death.set()

            gen2 = await _await_link(links, 1)
            assert healed == [PORT_A], "the confirmed death must reach the heal"
            assert str(PORT_B) in gen2.url, "must re-bridge onto the NEW port"

            with anyio.fail_after(5):
                # a FRESH initialize handshake — this is what mints the new
                # mcp-session-id on the replacement backend
                replayed = await gen2.recv()
                assert replayed.message.root.method == "initialize"
                await gen2.send(_response(1, {"protocolVersion": "2025-03-26"}))

                # ...and the client, which never disconnected, is served again
                await c2p_tx.send(_request(3, "tools/list"))
                assert (await gen2.recv()).message.root.method == "tools/list"
                await gen2.send(_response(3, {"tools": []}))
                assert (await p2c_rx.receive()).message.root.id == 3

            tg.cancel_scope.cancel()

    async def test_the_replayed_initialize_answer_is_not_shown_to_the_client(
        self, wired_proxy
    ):
        """The client already has its initialize result; a second one would be a
        protocol violation."""
        links, death = wired_proxy["links"], wired_proxy["death"]

        async def fake_heal(_dead_port, **_kw):
            return PORT_B

        wired_proxy["monkeypatch"].setattr(proxy_selfheal, "heal_backend", fake_heal)

        c2p_tx, c2p_rx = anyio.create_memory_object_stream(50)
        p2c_tx, p2c_rx = anyio.create_memory_object_stream(50)

        async with anyio.create_task_group() as tg:
            tg.start_soon(singleton._proxy_streams, c2p_rx, p2c_tx, PORT_A)

            await c2p_tx.send(_init_msg(1))
            with anyio.fail_after(5):
                await p2c_rx.receive()
            gen1 = await _await_link(links, 0)
            with anyio.fail_after(5):
                await gen1.recv()
                await gen1.send(_response(1, {"protocolVersion": "2025-03-26"}))

            death.set()
            gen2 = await _await_link(links, 1)
            with anyio.fail_after(5):
                await gen2.recv()
                await gen2.send(_response(1, {"protocolVersion": "2025-03-26"}))
                # only the reply to the LATER call may reach the client
                await c2p_tx.send(_request(9, "tools/list"))
                assert (await gen2.recv()).message.root.method == "tools/list"
                await gen2.send(_response(9, {"tools": []}))
                assert (await p2c_rx.receive()).message.root.id == 9

            tg.cancel_scope.cancel()

    async def test_an_unhealable_death_still_tears_the_proxy_down(self, wired_proxy):
        """The pre-F-838 fallback: healing is a backstop, not a promise."""
        links, death = wired_proxy["links"], wired_proxy["death"]

        async def fake_heal(_dead_port, **_kw):
            return None

        wired_proxy["monkeypatch"].setattr(proxy_selfheal, "heal_backend", fake_heal)

        c2p_tx, c2p_rx = anyio.create_memory_object_stream(50)
        p2c_tx, p2c_rx = anyio.create_memory_object_stream(50)
        returned = anyio.Event()

        async def run():
            await singleton._proxy_streams(c2p_rx, p2c_tx, PORT_A)
            returned.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            await c2p_tx.send(_init_msg(1))
            with anyio.fail_after(5):
                await p2c_rx.receive()
            gen1 = await _await_link(links, 0)
            with anyio.fail_after(5):
                await gen1.recv()
                await gen1.send(_response(1, {"protocolVersion": "2025-03-26"}))

            death.set()
            with anyio.fail_after(10):
                await returned.wait()
            tg.cancel_scope.cancel()

        assert len(links) == 1, "an unhealable death must not open a generation 2"


class TestHealUsesTheOneStartupPath:
    async def test_proxy_streams_hands_ensure_server_running_to_the_heal(
        self, wired_proxy
    ):
        """No second cold-start path: the heal must call the very function
        ``run_stdio_proxy`` uses at boot, which is what puts it behind the
        cold-start lock and the one identity+readiness reuse gate."""
        links, death = wired_proxy["links"], wired_proxy["death"]
        seen = {}

        async def fake_heal(_dead_port, **kw):
            seen.update(kw)

        wired_proxy["monkeypatch"].setattr(proxy_selfheal, "heal_backend", fake_heal)

        c2p_tx, c2p_rx = anyio.create_memory_object_stream(50)
        p2c_tx, p2c_rx = anyio.create_memory_object_stream(50)

        async with anyio.create_task_group() as tg:
            tg.start_soon(singleton._proxy_streams, c2p_rx, p2c_tx, PORT_A)
            await c2p_tx.send(_init_msg(1))
            with anyio.fail_after(5):
                await p2c_rx.receive()
            gen1 = await _await_link(links, 0)
            with anyio.fail_after(5):
                await gen1.recv()
                await gen1.send(_response(1, {"protocolVersion": "2025-03-26"}))
            death.set()
            with anyio.fail_after(10):
                while "ensure_running" not in seen:
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

        assert seen["ensure_running"] is singleton.ensure_server_running
        assert seen["await_ready"] is singleton._await_backend_http


# --------------------------------------------------------------------------
# the herd: many proxies confirm the same death at once
# --------------------------------------------------------------------------
class TestTheHerdSerializesOnTheColdStartLock:
    def test_two_concurrent_heals_produce_exactly_one_cold_start(
        self, tmp_path, monkeypatch
    ):
        """The heal calls ``ensure_server_running``, whose spawn runs under the
        SAME ``singleton.lock`` the startup herd already serializes on: the
        winner spawns, the rival finds the lock held and simply proxies to what
        the winner is bringing up. Redirected into tmp_path — the real
        ``~/.stealth-mcp`` is never touched."""
        port = 41999
        monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
        monkeypatch.setattr(singleton, "LOCK_FILE", tmp_path / "singleton.lock")
        monkeypatch.setattr(singleton, "_read_server_state", lambda: None)
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(
            singleton, "_same_identity_backend_ready", lambda *a, **k: False
        )
        monkeypatch.setattr(singleton, "_clear_stale_backend", lambda p: None)
        monkeypatch.setattr(singleton, "_wait_for_server", lambda p, **k: True)

        spawns = []
        lock_held = threading.Event()
        rival_done = threading.Event()

        def fake_spawn(p):
            spawns.append(p)
            lock_held.set()
            rival_done.wait(timeout=10)

        monkeypatch.setattr(singleton, "_start_server_process", fake_spawn)

        winner = threading.Thread(
            target=singleton._start_backend_holding_lock, args=(port,)
        )
        winner.start()
        try:
            assert lock_held.wait(timeout=10), "the winner never took the lock"
            # The rival heals while the lock is demonstrably held.
            singleton._start_backend_holding_lock(port)
        finally:
            rival_done.set()
            winner.join(timeout=15)

        assert spawns == [port], "the herd must cold-start exactly once"
