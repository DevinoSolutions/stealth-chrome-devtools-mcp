"""Keeping a page out of Chrome's own DevTools endpoint (T-19366).

Unit tests: the launch flag, every spelling of the endpoint's address, the tool
refusal, and the guard's CDP conversation against a fake browser endpoint (which
checks ORDER: a page may run only after its interception is on). The real-Chrome
proof, with the reviewer's exact sequence, is
``tests/test_login_vault_debug_port.py``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import websockets

from fakes import FakeBrowserManager, call_tool
from stealth_chrome_devtools_mcp.embedded import debug_port_guard, platform_utils
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

PORT = 54321


@pytest.fixture
def guarded():
    debug_port_guard._PORTS.add(PORT)
    yield PORT
    debug_port_guard._PORTS.discard(PORT)


# ------------------------------------------------------------------ the flag


def test_a_launch_has_no_remote_allow_origins_flag():
    import nodriver as uc

    debug_port_guard.install()
    config = uc.Config(headless=True, user_data_dir="C:/profiles/p", sandbox=True)
    assert not [a for a in config() if a.startswith("--remote-allow-origins")]
    assert not [
        a for a in config.browser_args if a.startswith("--remote-allow-origins")
    ]
    # nodriver's other defaults are untouched
    assert "--no-first-run" in config()


def test_install_is_idempotent():
    import nodriver as uc

    debug_port_guard.install()
    first = uc.Config.__init__
    debug_port_guard.install()
    assert uc.Config.__init__ is first


@pytest.mark.parametrize(
    "arg",
    [
        "--remote-allow-origins=*",
        "--remote-allow-origins=http://evil.example",
        "--Remote-Allow-Origins=*",
        "--remote-debugging-address=0.0.0.0",
    ],
)
def test_a_caller_cannot_put_the_flag_back(arg):
    clean, warnings = platform_utils.filter_stealth_args([arg, "--lang=fr"])
    assert clean == ["--lang=fr"]
    assert len(warnings) == 1


# --------------------------------------------------------------- the address

SPELLINGS = [
    "http://127.0.0.1:{p}/json/version",
    "http://127.0.0.1:{p}",
    "https://127.0.0.1:{p}/",
    "HTTP://LOCALHOST:{p}/json",
    "http://localhost:{p}/",
    "http://localhost.:{p}/",
    "http://foo.localhost:{p}/",
    "http://[::1]:{p}/",
    "http://[::ffff:127.0.0.1]:{p}/",
    "http://0.0.0.0:{p}/",
    "http://127.1:{p}/",
    "http://127.0.0.2:{p}/",
    "http://2130706433:{p}/",
    "http://0x7f000001:{p}/",
    "http://0x7f.1:{p}/",
    "http://0177.0.0.1:{p}/",
    "http://127.0.0.1.:{p}/",
    "http://127.0.0.1:0{p}/",
    "ws://127.0.0.1:{p}/devtools/browser/abc",
    "wss://localhost:{p}/devtools/page/1",
    "  http://127.0.0.1:{p}/json/version  ",
    "http://user:pw@127.0.0.1:{p}/",
    "http://some-name-that-resolves-to-loopback.example:{p}/",
]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_every_spelling_of_the_endpoint_is_refused(guarded, spelling):
    url = spelling.format(p=PORT)
    assert debug_port_guard.refusal(url) == debug_port_guard.REFUSAL, url


@pytest.mark.parametrize(
    "url",
    [
        "about:blank",
        "https://example.com/",
        "https://example.com:8443/",
        "http://127.0.0.1/",
        "http://127.0.0.1:80/",
        f"http://127.0.0.1:{PORT + 1}/",
        f"http://127.0.0.1:{PORT - 1}/",
        f"data:text/html,127.0.0.1:{PORT}",
        f"file:///tmp/x:{PORT}",
        f"chrome://version/?p={PORT}",
        f"https://example.com/path:{PORT}",
        "http://127.0.0.1:99999/",
        "",
        None,
        12345,
    ],
)
def test_other_addresses_are_left_alone(guarded, url):
    assert debug_port_guard.refusal(url) is None


def test_nothing_is_refused_when_no_browser_is_guarded():
    assert debug_port_guard.protected_ports() == frozenset()
    assert debug_port_guard.refusal(f"http://127.0.0.1:{PORT}/") is None


@pytest.fixture
def server_mod(patched_server):
    return patched_server(browser_manager=FakeBrowserManager())


@pytest.mark.parametrize(
    ("tool", "argument"),
    [
        ("navigate", "url"),
        ("new_tab", "url"),
        ("create_simple_dynamic_hook", "target_url"),
    ],
)
async def test_a_url_tool_refuses_the_endpoint_before_doing_anything(
    server_mod, guarded, tool, argument
):
    arguments = {"instance_id": "inst-1", argument: f"http://localhost:{PORT}/json"}
    if tool == "create_simple_dynamic_hook":
        arguments = {
            "name": "h",
            "url_pattern": "*",
            "action": "redirect",
            argument: arguments[argument],
        }
    with pytest.raises(ToolError) as caught:
        await call_tool(server_mod, tool, **arguments)
    assert caught.value.args[0] == debug_port_guard.REFUSAL


async def test_the_refusal_binds_a_positional_call_too(guarded):
    from stealth_chrome_devtools_mcp.embedded.tool_registry import _refusing_sealed_tabs

    async def navigate(instance_id, url):
        return "navigated"

    wrapped = _refusing_sealed_tabs(navigate)
    with pytest.raises(ToolError):
        await wrapped("inst-1", f"http://127.0.0.1:{PORT}/")
    assert await wrapped("inst-1", "https://example.com/") == "navigated"


# ------------------------------------------------- the guard's conversation


class FakeEndpoint:
    """A browser DevTools endpoint that records what the guard sends it, in order."""

    def __init__(self, *, fail_fetch_enable=False, page_url="about:blank"):
        self.log: list[tuple[str, str | None]] = []
        self.commands: list[dict] = []
        self.fail_fetch_enable = fail_fetch_enable
        self.page_url = page_url
        self.socket = None
        self.server = None
        self.url = ""

    async def start(self):
        self.server = await websockets.serve(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/devtools/browser/fake"

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def emit(self, method, params, session=None):
        message = {"method": method, "params": params}
        if session:
            message["sessionId"] = session
        await self.socket.send(json.dumps(message))

    async def handle(self, socket):
        self.socket = socket
        async for raw in socket:
            message = json.loads(raw)
            self.commands.append(message)
            method, session = message["method"], message.get("sessionId")
            self.log.append((method, session))
            reply = {"id": message["id"], "result": {}}
            if method == "Target.getTargets":
                reply["result"] = {
                    "targetInfos": [
                        {"targetId": "T1", "type": "page", "url": self.page_url},
                        {"targetId": "UI", "type": "browser_ui", "url": ""},
                    ]
                }
            elif method == "Target.attachToTarget":
                reply["result"] = {"sessionId": f"S-{message['params']['targetId']}"}
                await self.emit(
                    "Target.attachedToTarget",
                    {
                        "sessionId": reply["result"]["sessionId"],
                        "targetInfo": {
                            "targetId": message["params"]["targetId"],
                            "type": "page",
                            "url": self.page_url,
                        },
                        "waitingForDebugger": False,
                    },
                )
            elif method == "Fetch.enable" and self.fail_fetch_enable:
                reply = {"id": message["id"], "error": {"message": "no"}}
            await socket.send(json.dumps(reply))

    def methods(self):
        return [m for m, _s in self.log]


async def _guard(endpoint, port=PORT):
    guard = debug_port_guard._Guard(endpoint.url, port)
    await guard.start()
    return guard


@pytest.fixture
async def endpoint():
    fake = FakeEndpoint()
    await fake.start()
    yield fake
    await fake.stop()


async def _settle(fake, method, count=1):
    for _ in range(200):
        if fake.methods().count(method) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{method} never arrived: {fake.methods()}")


async def test_the_guard_turns_on_auto_attach_that_waits_for_the_debugger(endpoint):
    guard = await _guard(endpoint)
    first = endpoint.commands[0]
    assert first["method"] == "Target.setAutoAttach"
    assert first["params"] == {
        "autoAttach": True,
        "waitForDebuggerOnStart": True,
        "flatten": True,
    }
    await guard.stop()


async def test_a_page_already_open_is_guarded_before_the_guard_returns(endpoint):
    guard = await _guard(endpoint)
    enables = [c for c in endpoint.commands if c["method"] == "Fetch.enable"]
    assert [c["sessionId"] for c in enables] == ["S-T1"]
    assert enables[0]["params"]["patterns"] == [
        {"urlPattern": f"*://*:{PORT}/*"},
        {"urlPattern": f"*://*:{PORT}"},
    ]
    # the browser_ui target is not a page: it is not given an interception
    assert endpoint.methods().count("Target.attachToTarget") == 1
    await guard.stop()


async def test_a_new_page_runs_only_after_its_interception_is_on(endpoint):
    guard = await _guard(endpoint)
    await endpoint.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-new",
            "targetInfo": {"targetId": "T2", "type": "page", "url": ""},
            "waitingForDebugger": True,
        },
    )
    await _settle(endpoint, "Runtime.runIfWaitingForDebugger")
    sessioned = [(m, s) for m, s in endpoint.log if s == "S-new"]
    assert sessioned == [
        ("Fetch.enable", "S-new"),
        ("Runtime.runIfWaitingForDebugger", "S-new"),
    ]
    await guard.stop()


async def test_a_page_that_cannot_be_guarded_is_closed_and_never_resumed():
    fake = FakeEndpoint()
    await fake.start()
    guard = await _guard(fake)
    fake.fail_fetch_enable = True
    await fake.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-bad",
            "targetInfo": {"targetId": "T3", "type": "page", "url": ""},
            "waitingForDebugger": True,
        },
    )
    await _settle(fake, "Target.closeTarget")
    await asyncio.sleep(0.1)
    assert ("Runtime.runIfWaitingForDebugger", "S-bad") not in fake.log
    closing = [c for c in fake.commands if c["method"] == "Target.closeTarget"]
    assert closing[-1]["params"] == {"targetId": "T3"}
    await guard.stop()
    await fake.stop()


async def test_a_non_page_target_is_resumed_without_an_interception(endpoint):
    guard = await _guard(endpoint)
    before = endpoint.methods().count("Fetch.enable")
    await endpoint.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-worker",
            "targetInfo": {"targetId": "W1", "type": "service_worker", "url": ""},
            "waitingForDebugger": True,
        },
    )
    await _settle(endpoint, "Runtime.runIfWaitingForDebugger")
    assert endpoint.methods().count("Fetch.enable") == before
    await guard.stop()


async def test_a_request_to_the_port_is_failed_on_the_session_that_paused_it(endpoint):
    guard = await _guard(endpoint)
    await endpoint.emit(
        "Fetch.requestPaused",
        {"requestId": "R1", "request": {"url": f"http://127.0.0.1:{PORT}/json"}},
        session="S-T1",
    )
    await _settle(endpoint, "Fetch.failRequest")
    failing = next(c for c in endpoint.commands if c["method"] == "Fetch.failRequest")
    assert failing["sessionId"] == "S-T1"
    assert failing["params"] == {"requestId": "R1", "errorReason": "BlockedByClient"}
    await guard.stop()


async def test_a_page_already_at_the_endpoint_origin_is_sent_to_about_blank():
    fake = FakeEndpoint(page_url=f"http://127.0.0.1:{PORT}/json/version")
    await fake.start()
    guard = await _guard(fake)
    navigations = [c for c in fake.commands if c["method"] == "Page.navigate"]
    assert [(c["sessionId"], c["params"]) for c in navigations] == [
        ("S-T1", {"url": "about:blank"})
    ]
    await guard.stop()
    await fake.stop()


async def test_protect_registers_the_port_and_refuses_when_it_cannot_connect():
    browser = SimpleNamespace(
        config=SimpleNamespace(port=PORT + 7),
        websocket_url="ws://127.0.0.1:1/devtools/browser/none",
    )
    with pytest.raises(ToolError) as caught:
        await debug_port_guard.protect(browser)
    assert "could not be guarded" in str(caught.value)
    assert PORT + 7 not in debug_port_guard.protected_ports()


async def test_protect_without_an_address_refuses():
    with pytest.raises(ToolError):
        await debug_port_guard.protect(SimpleNamespace(config=None))


async def test_protect_registers_the_port_and_drops_it_when_the_browser_goes(endpoint):
    browser = SimpleNamespace(
        config=SimpleNamespace(port=PORT), websocket_url=endpoint.url
    )
    await debug_port_guard.protect(browser)
    assert PORT in debug_port_guard.protected_ports()
    await endpoint.socket.close()
    for _ in range(200):
        if PORT not in debug_port_guard.protected_ports():
            break
        await asyncio.sleep(0.01)
    assert PORT not in debug_port_guard.protected_ports()
