"""F-862 evidence tool: which workload grows the backend's RSS?

Boots ONE isolated backend through the release-gate harness (throwaway HOME,
its own port; never touches ``~/.stealth-mcp``), then drives it over real
streamable HTTP, sampling the backend interpreter's RSS.

  --mode sessions  three passes of N sessions each — terminated (DELETE),
                   ABANDONED (TCP dropped, no DELETE: a dead proxy or a probe
                   whose cleanup failed), terminated again — initialize-only,
                   the exact shape of the proxy's liveness probe. With
                   ``--linger S`` it then waits S seconds, sampling RSS each
                   minute, and prints the backend's own ``session hygiene``
                   log lines (the F-862 sweep at work).
  --mode all       abandoned initialize+tools/list sessions, then one headless
                   browser: navigation churn, then tool-call churn.

Usage::

    uv run python tools/probe_backend_memory.py --mode sessions --linger 420
    uv run python tools/probe_backend_memory.py --mode all --navs 20 --calls 20

Not a test: it takes minutes and opens real network pages in ``--mode all``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from release_gate_harness import (
    INIT_TIMEOUT,
    _backend_pid_from_state,
    gate_workspace,
    resolve_launcher,
)

PAGES = ["https://en.wikipedia.org/wiki/Main_Page", "https://www.python.org/"]
INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "f862-probe", "version": "0"},
}


def _backend_interpreter(pid: int) -> psutil.Process:
    """On Windows the recorded pid is a trampoline; the fattest process in its
    tree is the interpreter doing the work."""
    root = psutil.Process(pid)
    candidates = [root, *root.children(recursive=True)]
    return max(candidates, key=lambda p: p.memory_info().rss)


def rss_mb(proc: psutil.Process) -> float:
    return proc.memory_info().rss / (1024 * 1024)


class Http:
    """Minimal streamable-HTTP MCP client that can ABANDON a session."""

    def __init__(self, port: int) -> None:
        self.url = f"http://127.0.0.1:{port}/mcp/"
        self.client = httpx.AsyncClient(timeout=60)
        self.session_id: str | None = None
        self._id = 0

    async def _post(self, payload: dict[str, object]) -> dict[str, object] | None:
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        r = await self.client.post(self.url, json=payload, headers=headers)
        r.raise_for_status()
        if sid := r.headers.get("mcp-session-id"):
            self.session_id = sid
        if "text/event-stream" in r.headers.get("content-type", ""):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return None
        return r.json() if r.content else None

    async def call(
        self, method: str, params: dict[str, object] | None = None
    ) -> dict[str, object] | None:
        self._id += 1
        return await self._post(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        )

    async def notify(self, method: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": {}})

    async def initialize(self) -> None:
        await self.call("initialize", INITIALIZE_PARAMS)
        await self.notify("notifications/initialized")

    async def tool(self, name: str, **arguments: object) -> dict[str, object]:
        res = await self.call("tools/call", {"name": name, "arguments": arguments})
        result = (res or {}).get("result", {})
        if result.get("isError"):
            raise RuntimeError(result["content"][0]["text"][:400])
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    return {"text": block["text"]}
        return result

    async def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(httpx.HTTPError):
                await self.client.delete(
                    self.url, headers={"mcp-session-id": self.session_id}
                )
        await self.client.aclose()


async def session_churn(
    port: int, n: int, backend: psutil.Process, *, terminate: bool, list_tools: bool
) -> None:
    label = ("terminated (DELETE)" if terminate else "ABANDONED") + (
        " +tools/list" if list_tools else " initialize-only"
    )
    print(f"\n== session churn: {n} sessions {label}")
    t0 = time.monotonic()
    for i in range(1, n + 1):
        h = Http(port)
        await h.initialize()
        if list_tools:
            await h.call("tools/list")
        if terminate:
            await h.close()
        else:
            await h.client.aclose()  # drop the TCP side; NO DELETE
        if i % (n // 4 or 1) == 0:
            print(
                f"  after {i:4d} sessions: RSS {rss_mb(backend):8.1f} MB"
                f"  ({time.monotonic() - t0:5.1f}s)"
            )


async def nav_churn(
    h: Http, instance_id: str, rounds: int, backend: psutil.Process
) -> None:
    print(f"\n== navigation churn: {rounds} navigations alternating {PAGES}")
    for i in range(1, rounds + 1):
        await h.tool(
            "navigate", instance_id=instance_id, url=PAGES[i % 2], timeout=45000
        )
        if i % (rounds // 4 or 1) == 0:
            print(f"  after {i:3d} navs: RSS {rss_mb(backend):8.1f} MB")


async def tool_churn(
    h: Http, instance_id: str, k: int, backend: psutil.Process
) -> None:
    print(f"\n== tool-call churn: {k} x each")
    for name, kwargs in [
        ("take_screenshot", {}),
        ("get_page_content", {}),
        ("list_network_requests", {}),
        ("execute_script", {"script": "return document.title"}),
        ("get_instance_state", {}),
    ]:
        for _ in range(k):
            await h.tool(name, instance_id=instance_id, **kwargs)
        print(f"  after {k} x {name:22s}: RSS {rss_mb(backend):8.1f} MB")


def _hygiene_log_lines(log_dir: Path) -> list[str]:
    return [
        line[:220]
        for log in sorted(log_dir.glob("backend-*.log"))
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
        if "session hygiene" in line
    ]


async def linger(seconds: float, backend: psutil.Process, log_dir: Path) -> None:
    print(f"\n== lingering {seconds:.0f}s for the backend's own sweep")
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        await asyncio.sleep(60)
        print(f"  +{time.monotonic() - t0:4.0f}s: RSS {rss_mb(backend):8.1f} MB")
    for line in _hygiene_log_lines(log_dir):
        print("  LOG:", line)


async def main(args: argparse.Namespace) -> None:
    launcher = resolve_launcher()
    work_dir = Path(tempfile.mkdtemp(prefix="f862-"))
    with gate_workspace(work_dir) as space:
        # The backend under measurement must never ship to Sentry.
        space["env"]["STEALTH_MCP_NO_ERROR_REPORTING"] = "1"
        space["env"]["PYTHONUTF8"] = "1"
        transport = StdioTransport(
            command=str(launcher),
            args=["--singleton-port", str(space["port"])],
            env=space["env"],
            keep_alive=False,
        )
        async with Client(transport, init_timeout=INIT_TIMEOUT) as anchor:
            await anchor.list_tools()
            backend = _backend_interpreter(_backend_pid_from_state(space["home_dir"]))
            port = space["port"]
            print(f"backend pid {backend.pid} port {port}: {rss_mb(backend):.1f} MB")

            if args.mode == "sessions":
                for terminate in (True, False, True):
                    await session_churn(
                        port,
                        args.sessions,
                        backend,
                        terminate=terminate,
                        list_tools=False,
                    )
                if args.linger:
                    await linger(args.linger, backend, space["log_dir"])
                print(f"\nFINAL backend RSS {rss_mb(backend):.1f} MB")
                return

            await session_churn(
                port, args.sessions, backend, terminate=False, list_tools=True
            )
            h = Http(port)
            await h.initialize()
            spawned = await h.tool("spawn_browser", headless=True)
            instance_id = str(spawned["instance_id"])
            print(f"\nspawned {instance_id}; RSS {rss_mb(backend):.1f} MB")
            try:
                await nav_churn(h, instance_id, args.navs, backend)
                await tool_churn(h, instance_id, args.calls, backend)
            finally:
                await h.tool("close_instance", instance_id=instance_id)
                print(f"\nclosed instance; RSS {rss_mb(backend):.1f} MB")
                await h.close()
            print(f"\nFINAL backend RSS {rss_mb(backend):.1f} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=200)
    ap.add_argument("--navs", type=int, default=20)
    ap.add_argument("--calls", type=int, default=20)
    ap.add_argument("--mode", choices=["all", "sessions"], default="all")
    ap.add_argument("--linger", type=float, default=0.0)
    asyncio.run(main(ap.parse_args()))
