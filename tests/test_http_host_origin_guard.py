"""F-1013: the HTTP backend refuses DNS-rebound and foreign-origin requests.

The backend is unauthenticated and drives logged-in browsers, and its tools
include ``create_python_binding`` (``exec`` in the backend process) and the
cookie readers. It binds 127.0.0.1, which keeps other MACHINES out and does
nothing about a web page: a page whose domain is DNS-rebound to 127.0.0.1 is
same-origin with ``/mcp`` and can ``initialize`` and call any tool. fastmcp's
Host/Origin guard is the defence and is OFF by default (settings
``http_host_origin_protection=False``, and ``backend_env`` scrubs ``FASTMCP_*``
so an operator cannot turn it on), so ``mcp.run`` must ask for it.

``mcp.run(...)`` lives under ``__main__`` and is unreachable by import, so these
pins take the keyword arguments from ITS SOURCE (an AST evaluation, the way
``test_clean_shutdown_noise`` reads the same call), hand them to the real
``FastMCP.run`` with only ``uvicorn.Server.serve`` swapped for a capture of the
app it was given, and drive that app over httpx's ASGI transport. What is
exercised is the app the backend would build, not a second construction of it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastmcp import FastMCP

from stealth_chrome_devtools_mcp.embedded import (
    backend_client,
    backend_probe,
    server,
    session_hygiene,
    singleton,
)

_SERVER_SOURCE = Path(server.__file__)
PORT = 51873
# Accept + Content-Type are what every first-party client sends to /mcp.
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
_REBIND_HOSTS = ("evil.example", f"evil.example:{PORT}", "127.0.0.1.evil.example")
_LOOPBACK_HOSTS = (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}")


def _run_call_kwargs(host: str) -> dict[str, object]:
    """The keywords of ``server.py``'s ``mcp.run(transport="http", ...)``,
    evaluated as the backend would with ``--host host``. ``uvicorn_config`` is
    left out: it is F-809/F-830's and constructing a uvicorn config with it
    would reconfigure this process's logging."""
    tree = ast.parse(_SERVER_SOURCE.read_text(encoding="utf-8"))
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and any(
            kw.arg == "transport"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "http"
            for kw in node.keywords
        )
    )
    scope = {**vars(server), "args": SimpleNamespace(host=host, port=PORT)}
    kwargs: dict[str, object] = {}
    for kw in call.keywords:
        if kw.arg == "uvicorn_config":
            continue
        value = eval(  # noqa: S307  PERMANENT(evaluates one expression from this repo's own server.py, so the pin reads the real call and never a copy of it)
            compile(ast.Expression(kw.value), str(_SERVER_SOURCE), "eval"), scope
        )
        if kw.arg is None:  # a ``**helper(...)`` spread
            kwargs.update(value)
        else:
            kwargs[kw.arg] = value
    return kwargs


async def _the_backends_app(host: str = "127.0.0.1"):
    """The ASGI app ``FastMCP.run`` builds from ``server.py``'s own call
    (``run`` is ``anyio.run(run_async)``; a running test loop awaits the latter)."""
    built: list[object] = []

    async def capture(self, *args, **kwargs):
        built.append(self.config.app)

    original = uvicorn.Server.serve
    uvicorn.Server.serve = capture
    try:
        mcp = FastMCP("f1013-pin")
        session_hygiene.install(mcp)  # as server.py's http branch does before run()
        await mcp.run_async(
            show_banner=False, **{**_run_call_kwargs(host), "port": PORT}
        )
    finally:
        uvicorn.Server.serve = original
    (app,) = built
    return app


async def _initialize(app, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.post(
                f"http://127.0.0.1:{PORT}{backend_probe.MCP_PATH}",
                json=backend_probe._request("f1013-pin"),
                headers={**_MCP_HEADERS, **headers},
            )


class TestForeignHostIsRefused:
    @pytest.mark.parametrize("host", _REBIND_HOSTS)
    async def test_a_rebound_host_gets_421_and_no_session(self, host):
        resp = await _initialize(await _the_backends_app(), {"Host": host})
        assert resp.status_code == 421
        assert "mcp-session-id" not in resp.headers

    async def test_a_rebound_host_cannot_reach_the_health_route_either(self):
        app = await _the_backends_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"Host": f"evil.example:{PORT}"},
        ) as client:
            resp = await client.get(
                f"http://127.0.0.1:{PORT}{backend_probe.HEALTH_PATH}"
            )
        assert resp.status_code == 421


class TestForeignOriginIsRefused:
    @pytest.mark.parametrize(
        "origin",
        ["http://evil.example", f"http://evil.example:{PORT}", "https://evil.example"],
    )
    async def test_a_web_origin_gets_403_even_on_a_loopback_host(self, origin):
        """The DNS-rebinding page's own Origin, and a cross-site page that reaches
        the loopback address directly. Either way a browser is the caller."""
        resp = await _initialize(
            await _the_backends_app(),
            {"Host": f"127.0.0.1:{PORT}", "Origin": origin},
        )
        assert resp.status_code == 403
        assert "mcp-session-id" not in resp.headers

    async def test_a_rebound_page_is_refused_on_both_checks(self):
        resp = await _initialize(
            await _the_backends_app(),
            {"Host": f"evil.example:{PORT}", "Origin": f"http://evil.example:{PORT}"},
        )
        assert resp.status_code == 421


class TestEveryFirstPartyRequestShapeStillConnects:
    @pytest.mark.parametrize("host", _LOOPBACK_HOSTS)
    async def test_a_loopback_host_with_no_origin_is_accepted(self, host):
        """No Origin is how a non-browser client (stdio proxy, ``stealthy`` CLI,
        a ``uvx`` user's url transport) looks. Each spelling of loopback."""
        resp = await _initialize(await _the_backends_app(), {"Host": host})
        assert resp.status_code == 200
        assert resp.headers.get("mcp-session-id")

    async def test_the_health_route_answers_a_loopback_host(self):
        """The watchdog heartbeat's route, which the guard also sits in front of."""
        app = await _the_backends_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"Host": f"127.0.0.1:{PORT}"},
            ) as client:
                resp = await client.get(
                    f"http://127.0.0.1:{PORT}{backend_probe.HEALTH_PATH}"
                )
        assert resp.status_code == 200

    async def test_the_readiness_probe_still_gets_its_200(self, monkeypatch):
        """``backend_probe`` is what the cold-start poll, the identity gate,
        ``doctor`` and ``status`` ask with, against the URL ``singleton`` builds."""
        app = await _the_backends_app()
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: real_client(transport=httpx.ASGITransport(app=app), **kw),
        )
        async with app.router.lifespan_context(app):
            assert await backend_probe.await_ready(
                singleton._backend_http_url(PORT), 2.0
            )

    async def test_the_stdio_proxys_bridge_and_the_cli_session_connect(
        self, monkeypatch
    ):
        """The proxy bridge and every CLI verb open an MCP session through
        ``backend_client.http_client`` (the one transport seam) at the URL
        ``singleton`` builds. Same client, same headers, only the socket swapped."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        app = await _the_backends_app()
        real = backend_client.http_client

        def over_asgi(read_seconds):
            client = real(read_seconds)
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers=client.headers,
                timeout=client.timeout,
                follow_redirects=True,
            )

        monkeypatch.setattr(backend_client, "http_client", over_asgi)
        url = singleton._backend_http_url(PORT)
        async with app.router.lifespan_context(app):
            async with (
                backend_client.http_client(None) as http,
                streamable_http_client(url, http_client=http) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
            # ``opened`` is the CLI's way in: the same seam, a second session.
            assert await backend_client.list_tools(url) == []


class TestTheGuardFollowsTheBoundHost:
    """``--host`` can be changed (the help text says how). A non-loopback bind
    must keep serving its own address and still refuse everyone else's name."""

    async def test_a_deliberate_wide_bind_still_refuses_a_foreign_name(self):
        app = await _the_backends_app(host="0.0.0.0")  # noqa: S104  PERMANENT(the pin IS the wide bind: the guard must still refuse a foreign Host when --host 0.0.0.0 is passed)
        resp = await _initialize(app, {"Host": f"evil.example:{PORT}"})
        assert resp.status_code == 421

    async def test_a_named_bind_serves_its_own_name(self):
        app = await _the_backends_app(host="backend.lan")
        accepted = await _initialize(app, {"Host": f"backend.lan:{PORT}"})
        refused = await _initialize(app, {"Host": f"evil.example:{PORT}"})
        assert (accepted.status_code, refused.status_code) == (200, 421)

    async def test_loopback_names_stay_accepted_on_a_named_bind(self):
        app = await _the_backends_app(host="backend.lan")
        resp = await _initialize(app, {"Host": f"127.0.0.1:{PORT}"})
        assert resp.status_code == 200
