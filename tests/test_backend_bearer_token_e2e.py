"""F-1018: a real MCP handshake through the real stdio proxy, against a real backend.

The hermetic pins in ``test_http_host_origin_guard.py`` drive the backend's app
and each client against a real socket, one at a time. This is the whole chain a
user's Claude Code session runs: the installed launcher is spawned over stdio,
it starts the backend (detached, in an isolated home, so no shared browser, no
``default``/``fleet`` session and no real ``~/.stealth-mcp`` is touched), the
readiness poll and identity gate probe it WITH the token, the bridge opens its
MCP session WITH the token, and ``tools/list`` plus a tool call cross it. While
that session is alive the same backend is asked, over a raw socket, the three
questions that matter: no token, a wrong token, the token the proxy used.

No Chrome is started: the tools called are ``list_instances`` and the registry.
Marked ``integration`` (a real backend subprocess), like the stdio gate nodes.
"""

from __future__ import annotations

import re
import stat
import subprocess
import sys

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from release_gate_harness import (
    INIT_TIMEOUT,
    REGISTRY_TOOL_COUNT,
    _backend_entries,
    gate_work_dir,
    gate_workspace,
    resolve_launcher,
)
from stealth_chrome_devtools_mcp.embedded import backend_probe, backend_token

pytestmark = [pytest.mark.integration, pytest.mark.transport]

_ACCEPT = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _initialize(port: int, headers: dict[str, str]) -> httpx.Response:
    return httpx.post(
        f"http://127.0.0.1:{port}{backend_probe.MCP_PATH}",
        json=backend_probe._request("f1018-e2e"),
        headers={**_ACCEPT, **headers},
        timeout=30,
    )


async def test_the_stdio_proxy_handshake_and_a_tool_call_cross_the_token(tmp_path):
    launcher = resolve_launcher()
    work_dir = gate_work_dir(tmp_path)
    with gate_workspace(work_dir) as space:
        space["env"]["STEALTH_MCP_NO_ERROR_REPORTING"] = "1"
        space["env"]["STEALTH_MCP_NO_AUTO_RECOVERY"] = "1"
        space["env"]["PYTHONUTF8"] = "1"
        transport = StdioTransport(
            command=str(launcher),
            args=["--singleton-port", str(space["port"])],
            env=space["env"],
            keep_alive=False,
        )
        async with Client(transport, init_timeout=INIT_TIMEOUT) as client:
            # The handshake the user's client does, then both directions of the
            # bridge: a list that only the backend can answer and a tool call.
            assert client.initialize_result is not None
            tools = await client.list_tools()
            assert len(tools) == REGISTRY_TOOL_COUNT
            listed = await client.call_tool("list_instances", {})
            assert listed.is_error is False
            assert listed.data == []

            # The backend the proxy started, in the isolated home.
            (entry,) = _backend_entries(space["home_dir"])
            port = entry["port"]
            assert isinstance(port, int)
            token_file = (
                space["home_dir"] / ".stealth-mcp" / backend_token.TOKEN_FILE_NAME
            )
            token = backend_token.read(token_file)
            assert token, "the backend never wrote its token file"

            # Over the wire, to that same backend: 401, 401, 200.
            assert _initialize(port, {}).status_code == 401
            assert (
                _initialize(port, {"Authorization": "Bearer wrong"}).status_code == 401
            )
            ok = _initialize(port, backend_token.auth_headers(token_file))
            assert ok.status_code == 200
            assert ok.headers.get("mcp-session-id")

            _assert_owner_only(token_file)


def _assert_owner_only(path):
    if sys.platform == "win32":
        shown = subprocess.run(  # noqa: S603  PERMANENT(reads the ACL the backend wrote, with the OS's own tool)
            ["icacls", str(path)],  # noqa: S607  PERMANENT(a pin reading an ACL)
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        aces = [line for line in shown.splitlines() if re.search(r":\(", line)]
        assert len(aces) == 1, shown
        assert "(I)" not in aces[0]
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
