"""Singleton server management for multi-session environments.

When multiple Claude Code sessions start simultaneously, this module ensures
only ONE HTTP server process is spawned. All sessions connect to it as
lightweight stdio proxies.

Race condition handling:
  - File lock ensures exactly one process starts the server
  - Losers of the lock race poll until the server is healthy
  - Exponential backoff prevents thundering herd on health checks
  - Fallback to standalone stdio mode if server fails to start
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psutil

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

STATE_DIR = Path.home() / ".stealth-mcp"
LOCK_FILE = STATE_DIR / "singleton.lock"
PORT_FILE = STATE_DIR / "server.port"
# Records {port, version, pid} for the backend we started, so discovery can
# confirm a running backend is the SAME version before reusing it. Without this
# an upgraded session silently reuses a stale old-version backend (issue #14).
SERVER_STATE_FILE = STATE_DIR / "server.json"
DEFAULT_PORT = 19222
STARTUP_TIMEOUT = 30
SERVER_NAME = "stealth-chrome-devtools-mcp"
# How long the stdio proxy will wait for the backend before later requests
# (tools/list, tool calls) start failing. The `initialize` handshake itself is
# answered locally and never waits on this.
BACKEND_READY_TIMEOUT = 120.0


def _ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _exclusive_lock():
    """Try to acquire a file lock. Yields True if acquired, False otherwise."""
    _ensure_state_dir()
    fd = open(LOCK_FILE, "w")
    got = False
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        got = True
    except (OSError, IOError):
        pass
    try:
        yield got
    finally:
        if got:
            try:
                if sys.platform == "win32":
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        fd.close()


def _server_is_healthy(port: int) -> bool:
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.close()
        return True
    except (socket.error, OSError):
        return False


def _read_server_state() -> dict | None:
    """Return the recorded ``{port, version, pid}`` for the backend we started.

    None if there is no state file or it is missing/corrupt. This is the record
    written by :func:`_write_server_state`; a backend started by an older release
    (<= 1.2.0) has no such file and is therefore treated as version-unknown.
    """
    try:
        state = json.loads(SERVER_STATE_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def _write_server_state(port: int, version: str, pid: int) -> None:
    """Record the running backend's identity: its port, the package version that
    started it, and its pid. Discovery uses the version to confirm reuse is safe,
    and the pid to evict the backend if it is a stale (mismatched) version.
    """
    _ensure_state_dir()
    SERVER_STATE_FILE.write_text(
        json.dumps({"port": port, "version": version, "pid": pid})
    )


def _find_running_server() -> int | None:
    """Return the port of a *reusable* backend, or None.

    A backend is reusable only when we can confirm it is the SAME version as the
    running package: its recorded version must match and its port must be
    socket-healthy. A stale (older-version) or legacy (version-unknown) backend
    is deliberately NOT reused, so an upgrade actually takes effect instead of
    silently proxying to old backend code (issue #14).
    """
    state = _read_server_state()
    if state is None:
        return None
    port = state.get("port")
    if not isinstance(port, int):
        return None
    if state.get("version") != _server_version():
        return None
    if not _server_is_healthy(port):
        return None
    return port


def _is_our_backend(pid) -> bool:
    """True only if ``pid`` is a process running OUR HTTP backend.

    Identity is the module name **plus** ``--transport`` in the command line, so
    this positively excludes the stdio proxy (same module, no ``--transport``),
    unrelated processes, and recycled pids. Eviction relies on this to never
    terminate the wrong process.
    """
    if not isinstance(pid, int):
        return False
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        return False
    joined = " ".join(cmdline)
    return "stealth_chrome_devtools_mcp" in joined and "--transport" in joined


def _backend_pid_on_port(port: int) -> int | None:
    """Return the pid of OUR backend listening on ``port``, or None.

    A foreign process holding the port is deliberately ignored (never returned
    for termination).
    """
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.Error, OSError):
        return None
    for conn in conns:
        laddr = getattr(conn, "laddr", None)
        if (
            laddr
            and getattr(laddr, "port", None) == port
            and conn.status == psutil.CONN_LISTEN
            and conn.pid
            and _is_our_backend(conn.pid)
        ):
            return conn.pid
    return None


def _clear_stale_backend(port: int) -> None:
    """Terminate a stale/legacy backend of ours squatting ``port`` so a
    correctly-versioned backend can bind on it.

    No-op when the port already holds a reusable same-version backend. Targets
    only a process positively identified as our backend (by open port, then by
    recorded pid as a fallback). Best-effort and bounded — never raises.
    """
    if _find_running_server() == port:
        return  # a reusable same-version backend is already there

    pid = _backend_pid_on_port(port)
    if pid is None:
        state = _read_server_state()
        recorded = state.get("pid") if state else None
        if _is_our_backend(recorded):
            pid = recorded
    if pid is None:
        return

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            proc.kill()
    except (psutil.Error, OSError):
        pass

    # Give the OS a moment to release the port so the fresh backend can bind.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _server_is_healthy(port):
            return
        time.sleep(0.1)


def _start_server_process(port: int):
    cmd = [
        sys.executable,
        "-m",
        "stealth_chrome_devtools_mcp",
        "--transport",
        "http",
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
    ]

    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }

    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)

    _ensure_state_dir()
    PORT_FILE.write_text(str(port))
    _write_server_state(port, _server_version(), proc.pid)


def _wait_for_server(port: int, timeout: int = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    interval = 0.25
    while time.monotonic() < deadline:
        if _server_is_healthy(port):
            return True
        time.sleep(interval)
        interval = min(interval * 1.5, 2.0)
    return False


def _backend_http_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp/"


def _server_version() -> str:
    try:
        from importlib.metadata import version

        return version(SERVER_NAME)
    except Exception:
        return "0.0.0"


def _start_backend_holding_lock(port: int) -> None:
    """Start the singleton backend exactly once, holding the lock until it is
    healthy so no other session double-starts it.

    Runs in a daemon thread so it never blocks the stdio handshake. The lock is
    held for the whole backend cold start: any session that loses the lock race
    simply proxies to the backend the winner is bringing up.
    """
    try:
        with _exclusive_lock() as got_lock:
            if not got_lock:
                return  # another session owns startup; just proxy to it
            if _find_running_server() is not None:
                return  # already up (same version)
            # A stale/legacy backend (different or unknown version) may still be
            # holding the port; evict it under the lock so our fresh, correctly
            # versioned backend can bind — otherwise the proxy would fall back to
            # the old backend and the upgrade would silently not take effect.
            _clear_stale_backend(port)
            _start_server_process(port)
            _wait_for_server(port)  # keep the lock until the socket is bound
    except Exception:
        pass  # best-effort; the proxy still answers initialize and retries


def ensure_server_running(port: int = DEFAULT_PORT) -> int | None:
    """Ensure the singleton backend is up or coming up, WITHOUT blocking.

    Returns the port to proxy to immediately. Unlike a blocking wait, this never
    delays the stdio ``initialize`` handshake behind the backend's cold start —
    the proxy answers ``initialize`` locally and only later requests wait for the
    backend. That decoupling is what keeps Claude Code's 30s connection timeout
    from firing under load / on a cold cache.
    """
    existing = _find_running_server()
    if existing is not None:
        return existing

    threading.Thread(
        target=_start_backend_holding_lock, args=(port,), daemon=True
    ).start()
    return port


async def _await_backend_http(
    url: str, deadline_seconds: float = BACKEND_READY_TIMEOUT
) -> bool:
    """Poll the backend with a real ``initialize`` until it returns HTTP 200.

    Stronger than a socket probe *and* than "any HTTP response": a freshly bound
    uvicorn socket can answer (4xx) while FastMCP's MCP session manager is still
    starting — forwarding to it then fails with ``400`` (the same class of race
    as the old ``-32000``). Only a 200 to an ``initialize`` proves the MCP layer
    is genuinely ready to accept the client's session.
    """
    import anyio
    import httpx
    from mcp.types import DEFAULT_NEGOTIATED_VERSION

    probe = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": DEFAULT_NEGOTIATED_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "readiness-probe", "version": "0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    deadline = time.monotonic() + deadline_seconds
    interval = 0.1
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.post(url, json=probe, headers=headers)
                if resp.status_code == 200:
                    # Terminate the throwaway readiness session so it does not
                    # linger on the backend (one per proxy start otherwise).
                    session_id = resp.headers.get("mcp-session-id")
                    if session_id:
                        try:
                            await client.delete(
                                url, headers={**headers, "mcp-session-id": session_id}
                            )
                        except Exception:
                            pass
                    return True
            except Exception:
                pass
            await anyio.sleep(interval)
            interval = min(interval * 1.5, 1.0)
    return False


async def _watch_backend_liveness(
    port: int,
    *,
    interval: float = 2.0,
    failures_before_teardown: int = 3,
    is_healthy=None,
    sleep=None,
) -> None:
    """Return once the backend on ``port`` has been unreachable for
    ``failures_before_teardown`` consecutive checks.

    Armed only after the backend was confirmed up; the caller tears the proxy
    down when this returns. That converts a backend death mid-session into a
    clean client reconnect (which respawns a fresh backend) instead of an
    unbounded hang on requests a dead backend can never answer. A single healthy
    check resets the failure run, so a transient blip never tears down a live
    backend. ``is_healthy``/``sleep`` are injectable for testing.
    """
    import anyio

    check = is_healthy if is_healthy is not None else (lambda: _server_is_healthy(port))
    nap = sleep if sleep is not None else anyio.sleep
    consecutive = 0
    while True:
        await nap(interval)
        if check():
            consecutive = 0
            continue
        consecutive += 1
        if consecutive >= failures_before_teardown:
            return


async def _proxy_streams(client_read, client_write, port: int) -> None:
    """Answer ``initialize`` locally and instantly, then transparently proxy
    every other message to/from the singleton HTTP backend once it is ready.

    The transport plumbing (session-id capture, forwarding) is the same proven
    stdio↔streamable-HTTP pipe used previously; the only additions are the local
    ``initialize`` answer and swallowing the backend's duplicate ``initialize``
    response so the client never sees two.
    """
    import anyio
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.message import SessionMessage
    from mcp.types import (
        DEFAULT_NEGOTIATED_VERSION,
        JSONRPCMessage,
        JSONRPCRequest,
        JSONRPCResponse,
    )

    url = _backend_http_url(port)
    to_backend_tx, to_backend_rx = anyio.create_memory_object_stream(1024)
    init_request_id = {"value": None}
    init_swallowed = {"done": False}
    backend_initialized = anyio.Event()
    # Set once the backend has answered a real initialize (it is genuinely up).
    # The liveness monitor stays disarmed until then so it never tears the proxy
    # down during the backend's normal cold start.
    backend_ready = anyio.Event()

    async def pump_client():
        try:
            async for msg in client_read:
                if isinstance(msg, Exception):
                    continue
                inner = msg.message.root
                if isinstance(inner, JSONRPCRequest) and inner.method == "initialize":
                    params = inner.params or {}
                    proto = params.get("protocolVersion") or DEFAULT_NEGOTIATED_VERSION
                    result = {
                        "protocolVersion": proto,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": _server_version(),
                        },
                    }
                    response = JSONRPCResponse(
                        jsonrpc="2.0", id=inner.id, result=result
                    )
                    await client_write.send(
                        SessionMessage(message=JSONRPCMessage(response))
                    )
                    init_request_id["value"] = inner.id
                # Forward everything (including initialize) so the backend session
                # initializes with the client's real params. Buffered until the
                # backend connects.
                await to_backend_tx.send(msg)
        finally:
            await to_backend_tx.aclose()

    async def run_backend():
        if not await _await_backend_http(url):
            return  # backend never came up; later requests simply won't answer
        backend_ready.set()  # arm the liveness monitor now that it is genuinely up
        async with streamablehttp_client(url) as (backend_read, backend_write, _):

            async def to_backend():
                # Forward the initialize first, then hold every later message
                # until the backend's initialize response establishes the
                # streamable-HTTP session id. streamablehttp_client dispatches
                # requests concurrently and stamps each with the *current*
                # session id, so sending tools/list before that id exists yields
                # a 400. The real client gets this sequencing for free by waiting
                # on the initialize response; we answered it locally, so we must
                # reproduce the wait here.
                first = await to_backend_rx.receive()
                await backend_write.send(first)
                inner = first.message.root
                if isinstance(inner, JSONRPCRequest) and inner.method == "initialize":
                    await backend_initialized.wait()
                async for msg in to_backend_rx:
                    await backend_write.send(msg)

            async def from_backend():
                try:
                    async for msg in backend_read:
                        if isinstance(msg, Exception):
                            continue
                        inner = msg.message.root
                        if (
                            not init_swallowed["done"]
                            and init_request_id["value"] is not None
                            and isinstance(inner, JSONRPCResponse)
                            and inner.id == init_request_id["value"]
                        ):
                            init_swallowed["done"] = True
                            backend_initialized.set()
                            continue  # client already got a local initialize result
                        await client_write.send(msg)
                finally:
                    # Never leave to_backend blocked if the backend died before
                    # its initialize response arrived.
                    backend_initialized.set()

            async with anyio.create_task_group() as tg:
                tg.start_soon(to_backend)
                tg.start_soon(from_backend)

    async def run_backend_guarded():
        # A backend that dies mid-session surfaces as a read/connection error out
        # of run_backend. Don't let it crash (or hang) the proxy — swallow it and
        # tear down so the client sees a clean disconnect and reconnects to a
        # freshly spawned backend instead of blocking forever on a request the
        # dead backend can never answer.
        try:
            await run_backend()
        except Exception as exc:
            print(
                f"[stealth-mcp proxy] backend connection lost: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            tg.cancel_scope.cancel()

    async def monitor_backend():
        # Armed only after the backend is confirmed up. Covers the case where the
        # backend vanishes while run_backend is parked forwarding (no error is
        # raised, so run_backend_guarded alone would never fire).
        await backend_ready.wait()
        await _watch_backend_liveness(port)
        print(
            "[stealth-mcp proxy] backend became unreachable; tearing down for reconnect",
            file=sys.stderr,
            flush=True,
        )
        tg.cancel_scope.cancel()

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_backend_guarded)
        tg.start_soon(monitor_backend)
        # Drive the client pump in the main task. When the client (Claude Code)
        # disconnects, stdin hits EOF and pump_client returns — at which point we
        # cancel everything. Otherwise run_backend's from_backend loop stays
        # parked on the still-open backend stream forever and the proxy process
        # never exits, leaking one stranded process per disconnect.
        await pump_client()
        tg.cancel_scope.cancel()


async def _bridge(port: int):
    """Bind real stdio and run the fast-handshake proxy."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (client_read, client_write):
        try:
            await _proxy_streams(client_read, client_write, port)
        finally:
            # The client disconnected. mcp's stdio_server holds its __aexit__
            # open until its stdout-writer task finishes, and that task only
            # ends when the write stream is closed. Without this the process
            # hangs after every disconnect instead of exiting — one stranded
            # entrypoint per disconnect. Closing both streams lets stdio_server
            # tear down so the entrypoint returns and the process exits.
            await client_write.aclose()
            await client_read.aclose()


def run_stdio_proxy(port: int):
    """Run the stdio-to-HTTP proxy (blocking)."""
    import anyio

    anyio.run(_bridge, port)
