"""F-943: the proxy and the backend agree on ONE MCP path.

fastmcp 2.11 served ``/mcp/`` and redirected ``/mcp``; 2.14 serves ``/mcp`` and
answers ``/mcp/`` with a 307. The proxy has always built ``…/mcp/``, and
``backend_probe`` does not follow redirects, so after the bump every readiness
probe saw a 307, never its 200, and every proxy reported "backend did not become
ready within 120s" about a backend that was serving. ``backend_probe.MCP_PATH``
is now handed to ``mcp.run(path=…)`` and used in the URL, so the library's
default no longer decides either end.
"""

from urllib.parse import urlsplit

import httpx
from fastmcp import FastMCP

from stealth_chrome_devtools_mcp.embedded import backend_probe, singleton
from test_clean_shutdown_noise import _http_mcp_run_call


def test_the_backend_serves_on_the_one_mcp_path():
    call = _http_mcp_run_call()
    path = next((kw.value for kw in call.keywords if kw.arg == "path"), None)
    assert path is not None, (
        "mcp.run(transport='http') must pass path= — fastmcp's default moved "
        "from /mcp/ (2.11) to /mcp (2.14) and the proxy's probe stopped "
        "seeing a 200 (F-943)"
    )
    assert getattr(path, "id", None) == "MCP_PATH"


def test_the_proxy_url_is_the_served_path():
    assert urlsplit(singleton._backend_http_url(1)).path == backend_probe.MCP_PATH


async def test_the_readiness_probe_gets_its_200_without_a_redirect(monkeypatch):
    """The real probe against the installed fastmcp, built the way the backend
    builds it, over httpx's own ASGI transport. Redirects stay httpx's default
    (not followed), so a 307 fails here exactly as it did live."""
    app = FastMCP("f943-pin").http_app(path=backend_probe.MCP_PATH)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.ASGITransport(app=app), **kw),
    )
    async with app.router.lifespan_context(app):
        assert await backend_probe.await_ready(singleton._backend_http_url(1), 2.0)
