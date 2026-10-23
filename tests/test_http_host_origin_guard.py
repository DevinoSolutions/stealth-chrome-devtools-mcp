"""F-1013 and F-1018: the HTTP backend's front door.

F-1013: it refuses DNS-rebound and foreign-origin requests. F-1018 (the last
classes in this file): it refuses every request that does not carry the per-user
bearer token, and every first-party client sends it.

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
import asyncio
import contextlib
import getpass
import re
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastmcp import FastMCP

from stealth_chrome_devtools_mcp.embedded import (
    backend_client,
    backend_probe,
    backend_registry,
    backend_token,
    server,
    session_hygiene,
    singleton,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

_SERVER_SOURCE = Path(server.__file__)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """F-1018: building the backend's app now creates the bearer-token file, and
    every first-party request carries it. Both must land in a throwaway state dir
    and never in the real ``~/.stealth-mcp``."""
    monkeypatch.setattr(backend_registry, "STATE_DIR", tmp_path / "state")


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """F-1018: building the backend's app now creates the bearer-token file, and
    every first-party request carries it. Both must land in a throwaway state dir
    and never in the real ``~/.stealth-mcp``."""
    monkeypatch.setattr(backend_registry, "STATE_DIR", tmp_path / "state")


PORT = 51873
# Accept + Content-Type are what every first-party client sends to /mcp.
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
_REBIND_HOSTS = ("evil.example", f"evil.example:{PORT}", "127.0.0.1.evil.example")
_LOOPBACK_HOSTS = (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}")


def _run_call_kwargs(host: str, allowed: tuple[str, ...] = ()) -> dict[str, object]:
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
    scope = {
        **vars(server),
        "args": SimpleNamespace(host=host, port=PORT, allowed_host=list(allowed)),
    }
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


async def _the_backends_app(host: str = "127.0.0.1", allowed: tuple[str, ...] = ()):
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
            show_banner=False, **{**_run_call_kwargs(host, allowed), "port": PORT}
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
                headers={**_MCP_HEADERS, **backend_token.auth_headers(), **headers},
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
            headers={"Host": f"evil.example:{PORT}", **backend_token.auth_headers()},
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

    async def test_a_rebound_page_that_also_sends_its_origin_is_refused_at_the_host(
        self,
    ):
        """Both checks would refuse it; the Host check runs first, so the answer
        is 421 and not 403. (This pins which refusal wins, not that both ran.)"""
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
                headers={
                    "Host": f"127.0.0.1:{PORT}",
                    **backend_token.auth_headers(),
                },
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


class TestAllowedHostIsAnExplicitOptIn:
    """T-5727: a name beyond loopback and ``--host`` is accepted only when the
    operator names it with ``--allowed-host``, and ``*`` can never be named."""

    async def test_an_unlisted_proxy_name_is_refused_by_default(self):
        app = await _the_backends_app()
        resp = await _initialize(app, {"Host": "proxy.example"})
        assert resp.status_code == 421

    async def test_a_listed_name_is_served_and_a_stranger_still_is_not(self):
        app = await _the_backends_app(allowed=("proxy.example",))
        accepted = await _initialize(app, {"Host": "proxy.example"})
        refused = await _initialize(app, {"Host": "evil.example"})
        assert (accepted.status_code, refused.status_code) == (200, 421)

    @pytest.mark.parametrize("name", ["*", "*.example", "", "   "])
    def test_a_wildcard_or_blank_name_is_refused_when_the_guard_is_built(self, name):
        with pytest.raises(ValueError, match="not a host name"):
            server.host_origin_guard("127.0.0.1", [name])

    def test_the_cli_option_reaches_the_guard(self):
        parsed = server.build_arg_parser().parse_args(
            ["--allowed-host", "a.example", "--allowed-host", "b.example"]
        )
        assert parsed.allowed_host == ["a.example", "b.example"]
        assert server.build_arg_parser().parse_args([]).allowed_host == []


# ----------------------------------------------------------------- F-1018


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.asynccontextmanager
async def _listening(app):
    """The backend's app behind a REAL uvicorn socket on a free loopback port:
    what the bearer-token pins drive, so a 401 is a status on the wire and not a
    return value of an in-process transport."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=_free_port(), log_level="warning", lifespan="on"
    )
    backend = uvicorn.Server(config)
    task = asyncio.create_task(backend.serve())
    try:
        for _ in range(200):
            if backend.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the real uvicorn never started")
        yield config.port
    finally:
        backend.should_exit = True
        await task


async def _post_initialize(port: int, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.post(
            f"http://127.0.0.1:{port}{backend_probe.MCP_PATH}",
            json=backend_probe._request("f1018-pin"),
            headers={**_MCP_HEADERS, **headers},
        )


class TestTheBackendDemandsItsToken:
    """No token, a wrong token and the right token, over a real socket."""

    async def test_no_token_is_401_and_no_session(self):
        async with _listening(await _the_backends_app()) as port:
            resp = await _post_initialize(port, {})
        assert resp.status_code == 401
        assert "mcp-session-id" not in resp.headers
        assert resp.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize(
        "header",
        [
            "Bearer not-the-token",
            "Bearer a",
            "Basic dXNlcjpwYXNz",
            "bearer {token}",  # the scheme is case-sensitive here, on purpose
            "{token}",  # the bare value with no scheme
        ],
    )
    async def test_a_wrong_token_is_401(self, header):
        app = await _the_backends_app()  # creates the token file
        token = backend_token.read()
        assert token
        async with _listening(app) as port:
            resp = await _post_initialize(
                port, {"Authorization": header.format(token=token)}
            )
        assert resp.status_code == 401

    async def test_the_right_token_is_200_and_opens_a_session(self):
        async with _listening(await _the_backends_app()) as port:
            resp = await _post_initialize(port, backend_token.auth_headers())
        assert resp.status_code == 200
        assert resp.headers.get("mcp-session-id")

    async def test_a_401_does_not_echo_the_token(self):
        app = await _the_backends_app()  # creates the token file
        token = backend_token.read()
        assert token
        async with _listening(app) as port:
            resp = await _post_initialize(port, {"Authorization": f"Bearer {token}x"})
        assert resp.status_code == 401
        assert token not in resp.text
        assert token not in str(resp.headers)

    async def test_the_health_route_is_behind_the_token_too(self):
        async with _listening(await _the_backends_app()) as port:
            url = f"http://127.0.0.1:{port}{backend_probe.HEALTH_PATH}"
            async with httpx.AsyncClient() as client:
                bare = await client.get(url)
                authed = await client.get(url, headers=backend_token.auth_headers())
        assert (bare.status_code, authed.status_code) == (401, 200)

    async def test_the_host_guard_answers_before_the_token_is_looked_at(self):
        """A rebound Host is 421 with no token at all: the middleware order the
        guard docstring promises, so the 401 never tells a web page anything."""
        async with _listening(await _the_backends_app()) as port:
            resp = await _post_initialize(port, {"Host": f"evil.example:{port}"})
        assert resp.status_code == 421

    async def test_the_token_is_not_accepted_in_the_query_string(self):
        app = await _the_backends_app()  # creates the token file
        token = backend_token.read()
        assert token
        async with _listening(app) as port:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}{backend_probe.MCP_PATH}?token={token}",
                    json=backend_probe._request("f1018-pin"),
                    headers=_MCP_HEADERS,
                )
        assert resp.status_code == 401

    def test_the_backend_will_not_serve_if_the_file_cannot_be_secured(
        self, monkeypatch
    ):
        """Fail closed: a token that cannot be made owner-only is not served."""

        def refuse(_path):
            raise ToolError("cannot restrict the token file")

        monkeypatch.setattr(backend_token, "_owner_only", refuse)
        with pytest.raises(ToolError, match="cannot restrict"):
            server.bearer_token_guard()
        assert not backend_token.token_path().exists()

    def test_building_the_guard_creates_the_token_before_any_port_is_bound(self):
        """So a client that finds a LISTENING backend always finds its token."""
        assert not backend_token.token_path().exists()
        (guard,) = server.bearer_token_guard()["middleware"]
        assert backend_token.read() == guard.kwargs["token"]


def _acl_lines(path: Path) -> list[str]:
    out = subprocess.run(  # noqa: S603  PERMANENT(reads the ACL the product just wrote, with the OS's own tool)
        ["icacls", str(path)],  # noqa: S607  PERMANENT(a pin reading an ACL; the product itself uses the absolute path)
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if re.search(r":\(", line)]


class TestTheTokenFileIsOwnerOnly:
    def test_a_new_token_is_43_urlsafe_characters_and_stable(self, tmp_path):
        path = tmp_path / "backend.token"
        first = backend_token.ensure(path)
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first)
        assert backend_token.ensure(path) == first == backend_token.read(path)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_posix_mode_is_0600(self, tmp_path):
        path = tmp_path / "backend.token"
        backend_token.ensure(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_posix_a_widened_file_is_narrowed_again(self, tmp_path):
        path = tmp_path / "backend.token"
        backend_token.ensure(path)
        path.chmod(0o666)
        backend_token.ensure(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
    def test_windows_acl_is_the_current_user_alone_with_nothing_inherited(
        self, tmp_path
    ):
        path = tmp_path / "backend.token"
        backend_token.ensure(path)
        (ace,) = _acl_lines(path)  # exactly one entry: no SYSTEM, no Administrators
        assert getpass.getuser().lower() in ace.lower()
        assert "(F)" in ace
        assert "(I)" not in ace  # nothing inherited from the state dir

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
    def test_windows_a_widened_acl_is_narrowed_again(self, tmp_path):
        path = tmp_path / "backend.token"
        backend_token.ensure(path)
        subprocess.run(  # noqa: S603  PERMANENT(a pin widening the ACL the product must then repair)
            ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],  # noqa: S607  PERMANENT(a pin)
            capture_output=True,
            check=True,
        )
        assert len(_acl_lines(path)) == 2
        backend_token.ensure(path)
        assert len(_acl_lines(path)) == 1

    @pytest.mark.parametrize("content", [b"", b"short", b"x" * 43 + b"\n!", b"\xff"])
    def test_a_file_that_is_not_a_token_is_replaced_not_trusted(
        self, tmp_path, content
    ):
        path = tmp_path / "backend.token"
        path.write_bytes(content)
        assert backend_token.read(path) is None
        fresh = backend_token.ensure(path)
        assert backend_token.read(path) == fresh

    def test_a_race_between_backends_converges_on_one_token(self, tmp_path):
        path = tmp_path / "backend.token"
        seen: list[str] = []
        gate = threading.Barrier(8)

        def start():
            gate.wait()
            seen.append(backend_token.ensure(path))

        threads = [threading.Thread(target=start) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(seen) == 8
        assert len(set(seen)) == 1
        assert [p.name for p in tmp_path.iterdir()] == ["backend.token"]

    def test_an_unusable_state_dir_raises_the_one_tool_error(self, tmp_path):
        blocker = tmp_path / "a-file"
        blocker.write_text("x")
        with pytest.raises(ToolError, match="cannot create the state dir"):
            backend_token.ensure(blocker / "backend.token")

    def test_a_missing_file_gives_no_header_and_never_raises(self, tmp_path):
        assert backend_token.read(tmp_path / "nope") is None
        assert backend_token.auth_headers(tmp_path / "nope") == {}


class TestEveryFirstPartyClientSendsTheToken:
    """The census: each caller of the HTTP backend, against a real socket."""

    async def test_the_readiness_and_identity_probe(self):
        async with _listening(await _the_backends_app()) as port:
            url = f"http://127.0.0.1:{port}{backend_probe.MCP_PATH}"
            assert await backend_probe.await_ready(url, 5.0)
            assert await asyncio.to_thread(backend_probe.ready, url, 5.0)

    async def test_the_watchdog_heartbeat(self):
        async with _listening(await _the_backends_app()) as port:
            beat = backend_probe.Heartbeat(
                f"http://127.0.0.1:{port}{backend_probe.MCP_PATH}"
            )
            try:
                assert await asyncio.to_thread(beat.alive, 5.0)
            finally:
                beat.close()

    async def test_the_cli_session_and_the_proxy_bridge_transport(self):
        async with _listening(await _the_backends_app()) as port:
            url = f"http://127.0.0.1:{port}{backend_probe.MCP_PATH}"
            assert await backend_client.list_tools(url) == []

    async def test_a_client_with_no_token_is_refused_by_every_path(
        self, tmp_path, monkeypatch
    ):
        """The same callers once the token file is not theirs to read: the probes
        say not-ready (fail-closed), the heartbeat says unauthorized, and the CLI's
        session raises the 401 that ``cli_call`` turns into one sentence."""
        from stealth_chrome_devtools_mcp import cli_call

        async with _listening(await _the_backends_app()) as port:
            url = f"http://127.0.0.1:{port}{backend_probe.MCP_PATH}"
            monkeypatch.setattr(backend_registry, "STATE_DIR", tmp_path / "other")
            assert not await backend_probe.await_ready(url, 0.5)
            assert not await asyncio.to_thread(backend_probe.ready, url, 5.0)
            beat = backend_probe.Heartbeat(url)
            try:
                # The heartbeat says "unauthorized", not "dead" (T-6367): the
                # backend answered, so the watchdog must not strike it.
                assert (
                    await asyncio.to_thread(beat.verdict, 5.0)
                    == backend_probe.VERDICT_UNAUTHORIZED
                )
            finally:
                beat.close()
            with pytest.raises(BaseException) as caught:  # noqa: PT011  PERMANENT(the SDK wraps the 401 in an ExceptionGroup; _verdict unwraps it, asserted next)
                await backend_client.list_tools(url)
        code, message = cli_call._verdict(caught.value)
        assert code == cli_call.EXIT_NO_BACKEND
        assert "bearer token" in message
