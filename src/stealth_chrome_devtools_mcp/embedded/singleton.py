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
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

STATE_DIR = Path.home() / ".stealth-mcp"
LOCK_FILE = STATE_DIR / "singleton.lock"
PORT_FILE = STATE_DIR / "server.port"
DEFAULT_PORT = 19222
STARTUP_TIMEOUT = 30


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


def _find_running_server() -> int | None:
    try:
        if PORT_FILE.exists():
            port = int(PORT_FILE.read_text().strip())
            if _server_is_healthy(port):
                return port
    except (ValueError, OSError):
        pass
    return None


def _start_server_process(port: int):
    cmd = [
        sys.executable,
        "-m",
        "stealth_chrome_devtools_mcp",
        "--transport", "http",
        "--port", str(port),
        "--host", "127.0.0.1",
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

    subprocess.Popen(cmd, **kwargs)

    _ensure_state_dir()
    PORT_FILE.write_text(str(port))


def _wait_for_server(port: int, timeout: int = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    interval = 0.25
    while time.monotonic() < deadline:
        if _server_is_healthy(port):
            return True
        time.sleep(interval)
        interval = min(interval * 1.5, 2.0)
    return False


def ensure_server_running(port: int = DEFAULT_PORT) -> int | None:
    """Ensure a singleton HTTP server is running. Returns port or None."""
    existing = _find_running_server()
    if existing is not None:
        return existing

    with _exclusive_lock() as got_lock:
        if got_lock:
            existing = _find_running_server()
            if existing is not None:
                return existing
            _start_server_process(port)
            if _wait_for_server(port):
                return port
            return None
        else:
            if _wait_for_server(port):
                return _find_running_server() or port
            return None


async def _bridge(port: int):
    """Bridge MCP stdio ↔ HTTP using the mcp library's transports."""
    import anyio
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.server.stdio import stdio_server

    url = f"http://127.0.0.1:{port}/mcp/"

    async with stdio_server() as (stdio_read, stdio_write):
        async with streamablehttp_client(url) as (http_read, http_write, _):

            async def forward_requests():
                async for msg in stdio_read:
                    await http_write.send(msg)

            async def forward_responses():
                async for msg in http_read:
                    await stdio_write.send(msg)

            async with anyio.create_task_group() as tg:
                tg.start_soon(forward_requests)
                tg.start_soon(forward_responses)


def run_stdio_proxy(port: int):
    """Run the stdio-to-HTTP proxy (blocking)."""
    anyio = __import__("anyio")
    anyio.run(_bridge, port)
