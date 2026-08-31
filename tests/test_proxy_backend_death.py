"""The stdio proxy must not hang forever when the backend dies mid-session.

The proxy already tears down when the *client* disconnects. But there was no
symmetric handling for the *backend* dying: `from_backend`'s stream closed while
`to_backend` stayed parked, `run_backend` never returned, and `pump_client` kept
buffering client requests that could never be answered. A tool call issued after
the backend died therefore never got a response — the MCP client blocked with no
timeout (an unbounded loop for the AI driving it).

The fix arms a bounded backend-liveness monitor once the backend is confirmed up;
when the backend it proxies to vanishes, the proxy tears down so the client sees
a clean disconnect and reconnects (respawning a fresh backend) instead of hanging.

F-838 (SOFT golden update, same PR): "tear down and let the client reconnect"
turned out to rest on a premise that does not hold — MCP clients do not reliably
respawn a stdio server mid-session — so a confirmed death now HEALS first
(`proxy_selfheal.drive`) and only tears down when healing is impossible. What
this file pins is unchanged in substance: the proxy must reach a bounded end
instead of parking forever on a dead backend. The end-to-end case therefore
states its premise explicitly — the backend is unhealable — which also keeps a
test that kills a real backend from cold-starting a replacement against the
developer's own `~/.stealth-mcp` record. The heal path itself is pinned,
hermetically, in `test_proxy_selfheal.py`.

F-843 (this file's transport node) is the OTHER half of the same story, and the
half the shipped tests never reached: every node above drives a fake ``watch``,
so all of them exercise the SLOW witness. A backend killed while a client call
is in flight is noticed by the BRIDGE leg ~11s before the watchdog can conclude,
and that generation used to end unconfirmed — the proxy tore down ~1s after the
kill and the client was disconnected for good.
``TestBackendDeathWithACallInFlight`` reproduces exactly that, over the real
installed launcher, in an isolated gate workspace, and asserts the SAME client
comes back.

`TestWatchBackendLiveness` unit-tests the monitor's decision logic (fast, no
backend). `TestProxyExitsOnBackendDeath` reproduces the real hang end-to-end.
"""

import re
import socket
import time

import anyio
import anyio.lowlevel
import pytest

from e2e_helpers import CAN_RUN
from release_gate_harness import (
    INIT_TIMEOUT,
    REGISTRY_TOOL_COUNT,
    RawStdioWire,
    _backend_pid_from_state,
    _terminate_process_tree,
    gate_work_dir,
    gate_workspace,
    resolve_launcher,
    workspace_backend_logs,
)
from stealth_chrome_devtools_mcp.embedded import proxy_selfheal, singleton


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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
            "clientInfo": {"name": "backend-death-test", "version": "1"},
        },
    )
    return SessionMessage(message=JSONRPCMessage(req))


def _initialized_note():
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

    note = JSONRPCNotification(
        jsonrpc="2.0", method="notifications/initialized", params={}
    )
    return SessionMessage(message=JSONRPCMessage(note))


# ── F-843 bounds ───────────────────────────────────────────────────────────
# Harness bounds, never product deadlines: if one of these fires, the product
# did not answer, and the node fails by name instead of hanging the suite.
FIRST_CALL_BOUND = 130.0  # the first backend-bound call pays the cold start
INFLIGHT_BOUND = 60.0  # an unanswerable call must still be ANSWERED
# Deliberately outlasts the product's own allowance (HEAL_ATTEMPTS x
# HEAL_ATTEMPT_SECONDS = 90s): a node that cut the recovery off at 60s could not
# tell "never healed" from "healed slowly", which is the whole question here.
RECOVERY_BOUND = 100.0

# ``backend-<pid>.log`` — NOT ``backend-boot.log``, which every spawn appends to
# and which therefore says nothing about how many backends have existed.
_BACKEND_LOG = re.compile(r"^backend-\d+\.log$")
# Read from the product so the node cannot pin a code the contract has moved on
# from; the MESSAGE is asserted nowhere here, only that this is that error.
_BACKEND_DIED_CODE = proxy_selfheal._BACKEND_DIED_CODE


def _backend_log_names(log_dir) -> list[str]:
    return sorted(
        p.name for p in log_dir.glob("backend-*.log") if _BACKEND_LOG.match(p.name)
    )


def _proxy_log_text(log_dir) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(log_dir.glob("proxy-*.log"))
    )


def _tools_list_msg(req_id):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    req = JSONRPCRequest(jsonrpc="2.0", id=req_id, method="tools/list", params={})
    return SessionMessage(message=JSONRPCMessage(req))


class TestWatchBackendLiveness:
    @pytest.mark.asyncio
    async def test_returns_after_consecutive_unhealthy_checks(self):
        calls = {"n": 0}

        def health():
            calls["n"] += 1
            return False  # backend is gone on every check

        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        with anyio.fail_after(2):
            await singleton._watch_backend_liveness(
                port=1,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=health,
                sleep=tiny_sleep,
                # F-820: strikes alone no longer condemn — the gone backend of
                # this test's premise is stated explicitly, which also keeps
                # the case off the real server.json the default gate reads.
                confirm_probe=lambda: False,
            )

        assert calls["n"] == 3  # tore down after exactly 3 consecutive failures

    @pytest.mark.asyncio
    async def test_resets_counter_on_recovery(self):
        # A single healthy check between failures must reset the counter, so a
        # transient blip never tears down a still-live backend.
        results = [False, False, True, False, False, False]
        idx = {"i": 0}

        def health():
            v = results[idx["i"]]
            idx["i"] += 1
            return v

        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        with anyio.fail_after(2):
            await singleton._watch_backend_liveness(
                port=1,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=health,
                sleep=tiny_sleep,
                confirm_probe=lambda: False,  # F-820: gone, not merely busy
            )

        assert idx["i"] == 6  # needed all 6 checks: the True reset the run of failures

    @pytest.mark.asyncio
    async def test_does_not_return_while_healthy(self):
        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        done = anyio.Event()

        async with anyio.create_task_group() as tg:

            async def run():
                await singleton._watch_backend_liveness(
                    port=1,
                    interval=0.0,
                    failures_before_teardown=3,
                    is_healthy=lambda: True,
                    sleep=tiny_sleep,
                )
                done.set()

            tg.start_soon(run)
            await anyio.sleep(0.1)
            assert not done.is_set(), "monitor tore down a healthy backend"
            tg.cancel_scope.cancel()


@pytest.mark.integration
class TestProxyExitsOnBackendDeath:
    """End-to-end reproduction: the proxy MUST reach a bounded end (not hang)
    when the backend dies mid-session. On the un-fixed code the proxy parks
    forever on the dead backend and this times out — the exact unbounded hang.

    F-838: the bounded end is now "heal, else tear down", so this case pins the
    ELSE branch by stating its premise — no replacement is obtainable."""

    @pytest.mark.asyncio
    async def test_proxy_returns_when_backend_dies_and_cannot_be_healed(
        self, tmp_path, monkeypatch
    ):
        import os
        import subprocess
        import sys

        from stealth_chrome_devtools_mcp.embedded.singleton import _proxy_streams

        async def unhealable(_dead_port, **_kwargs):
            return None

        # Also keeps this test off the real cold-start path (and off the
        # developer's live ~/.stealth-mcp record) while it kills a real backend.
        monkeypatch.setattr(proxy_selfheal, "heal_backend", unhealable)

        port = _free_port()
        env = dict(os.environ)
        env["STEALTH_MCP_BROWSER_SESSION_ROOT"] = str(tmp_path / "sessions")
        env["STEALTH_BROWSER_DEBUG"] = "false"

        backend = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "stealth_chrome_devtools_mcp",
                "--transport",
                "http",
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        try:
            c2p_tx, c2p_rx = anyio.create_memory_object_stream(50)
            p2c_tx, p2c_rx = anyio.create_memory_object_stream(50)
            proxy_returned = anyio.Event()

            async with anyio.create_task_group() as tg:

                async def run_proxy():
                    await _proxy_streams(c2p_rx, p2c_tx, port)
                    proxy_returned.set()

                tg.start_soon(run_proxy)

                # Real handshake so the backend session is genuinely live and the
                # proxy is fully wired to it.
                await c2p_tx.send(_init_msg(1))
                await c2p_tx.send(_initialized_note())
                await c2p_tx.send(_tools_list_msg(2))
                with anyio.fail_after(90):
                    await p2c_rx.receive()  # local initialize answer
                    await p2c_rx.receive()  # tools/list from the real backend

                # The backend dies mid-session.
                backend.kill()
                backend.wait(timeout=10)

                # A request now can never be answered by the dead backend. The
                # proxy must notice the backend is gone and return within a bounded
                # time — on the old code it parks forever and this times out.
                await c2p_tx.send(_tools_list_msg(3))

                with anyio.fail_after(30):
                    await proxy_returned.wait()

                tg.cancel_scope.cancel()
        finally:
            if backend.poll() is None:
                backend.kill()
                try:
                    backend.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass


@pytest.mark.integration
@pytest.mark.transport
@pytest.mark.timeout(300)
@pytest.mark.skipif(not CAN_RUN, reason="Chrome not available or server failed to load")
class TestBackendDeathWithACallInFlight:
    """F-843, over the real transport: a backend killed WHILE a client call is
    in flight must not end the session.

    Why this node exists at all. The shipped self-heal suite drives a fake
    ``watch``, so every one of its cases is the watchdog's ~12s verdict — the
    SLOW witness. The death users actually reported is the fast one: an
    outstanding request breaks the proxy's HTTP leg in milliseconds, and before
    F-843 that ended the backend generation UNCONFIRMED, so the proxy tore down
    about a second after the kill and the client never came back (one -32603 for
    the in-flight call, then a closed stdio pipe forever). Only a real launcher
    over real stdio against a real backend reproduces that ordering; a fake
    transport cannot, which is exactly how the defect shipped.

    Isolation is total: ``gate_workspace`` gives a throwaway HOME (so the
    developer's own ``~/.stealth-mcp`` record and the real 19222 backend are
    untouched), its own free ``--singleton-port``, its own log dir, and it owns
    the teardown of whatever backend the heal leaves behind.
    """

    @pytest.mark.asyncio
    async def test_the_same_client_is_usable_again_after_an_in_flight_kill(
        self, tmp_path
    ):
        work_dir = gate_work_dir(tmp_path)
        with gate_workspace(work_dir) as space:
            env = dict(space["env"])
            # Never ship this node's deliberate kill to the real Sentry project.
            # conftest already sets it for the parent; restated because the child
            # inherits THIS mapping, not that one.
            env["STEALTH_MCP_NO_ERROR_REPORTING"] = "1"
            log_dir = space["log_dir"]

            wire = RawStdioWire(
                launcher=resolve_launcher(), env=env, port=space["port"]
            )
            await wire.start()
            try:
                await wire.initialize(INIT_TIMEOUT)
                first = await wire.request("tools/list")
                listed = await wire.response(first, FIRST_CALL_BOUND)
                assert len(listed["result"]["tools"]) == REGISTRY_TOOL_COUNT, (
                    "handshake did not reach a real backend\n"
                    f"{workspace_backend_logs(space)[-4000:]}"
                )

                backend_pid = _backend_pid_from_state(space["home_dir"])
                assert backend_pid is not None, "no backend was recorded to kill"
                before = _backend_log_names(log_dir)
                assert len(before) == 1, (
                    f"expected exactly one backend so far: {before}"
                )

                # THE reproduction. The request is written to the proxy's stdin
                # and deliberately NOT awaited, so the backend dies with it in
                # flight — the ordering the whole finding is about.
                inflight = await wire.request("tools/list")
                _terminate_process_tree(backend_pid, 15.0)

                # Contract 1: whatever was riding the dying connection is still
                # ANSWERED — the -32603 ``PendingCalls`` owes it, or a real
                # result if the backend got there first. Never stranded, which
                # is what the pre-F-838 hang used to do.
                await wire.response(inflight, INFLIGHT_BOUND)

                # Contract 2 — the F-843 fix itself. Exactly one call loses the
                # race with the kill, and which one is not deterministic (the
                # backend may answer ``inflight`` before it dies, leaving the
                # NEXT call to be tracked and failed). So the assertion is the
                # remedy the error message itself prescribes — "reissue it if it
                # is safe to repeat" — inside ONE bound: the SAME client, which
                # never disconnected, must come back with a real tool list.
                deadline = time.monotonic() + RECOVERY_BOUND
                recovered = None
                while recovered is None and time.monotonic() < deadline:
                    again = await wire.request("tools/list")
                    frame = await wire.response(
                        again, max(1.0, deadline - time.monotonic())
                    )
                    if "result" in frame:
                        recovered = frame
                        break
                    assert frame["error"]["code"] == _BACKEND_DIED_CODE, (
                        f"an unexpected error ended the session: {frame}\n"
                        f"--- proxy log ---\n{_proxy_log_text(log_dir)[-4000:]}"
                    )
                assert recovered is not None, (
                    "the client never became usable again\n"
                    f"--- proxy log ---\n{_proxy_log_text(log_dir)[-4000:]}"
                )
                assert len(recovered["result"]["tools"]) == REGISTRY_TOOL_COUNT

                # Contract 3: a SECOND backend actually exists — the recovery
                # spawned one rather than the client silently reusing a corpse.
                after = _backend_log_names(log_dir)
                assert len(after) >= 2, (
                    f"no replacement backend was ever started: {after}\n"
                    f"--- proxy log ---\n{_proxy_log_text(log_dir)[-4000:]}"
                )

                # Contract 4: it healed for the RIGHT reason. The bridge leg is
                # what saw this death; the pre-fix teardown line must be absent.
                proxy_log = _proxy_log_text(log_dir)
                assert "backend connection lost" in proxy_log, (
                    "the bridge leg did not witness the kill — this node no "
                    f"longer reproduces F-843\n{proxy_log[-4000:]}"
                )
                assert "backend healed" in proxy_log, (
                    f"recovery happened without a heal?\n{proxy_log[-4000:]}"
                )
                assert "tearing down for reconnect" not in proxy_log, (
                    f"the pre-F-843 disconnect is back\n{proxy_log[-4000:]}"
                )
            finally:
                await wire.aclose()
