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
from nodriver import cdp

from fakes import FakeBrowser, FakeBrowserManager, call_tool
from stealth_chrome_devtools_mcp.embedded import (
    debug_port_guard,
    login_seal,
    platform_utils,
)
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
        # every spelling Chrome's own parser accepts (re-review N3)
        "-remote-allow-origins=*",
        "/remote-allow-origins=*",
        "-Remote-Allow-Origins=http://evil.example",
        "/REMOTE-ALLOW-ORIGINS=*",
        "-remote-debugging-address=0.0.0.0",
        # a DevTools window on every tab is an inspector the agent could reach (N4)
        "--auto-open-devtools-for-tabs",
        "-auto-open-devtools-for-tabs",
        "/Auto-Open-DevTools-For-Tabs",
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
        self.refuse_pin = False
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
            elif (method == "Fetch.enable" and self.fail_fetch_enable) or (
                method == "Browser.setDownloadBehavior" and self.refuse_pin
            ):
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
    assert endpoint.commands[0]["method"] == "Target.setDiscoverTargets"
    first = endpoint.commands[1]
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
    # interception first, then the same for the children it starts, then it runs
    assert sessioned == [
        ("Fetch.enable", "S-new"),
        ("Target.setAutoAttach", "S-new"),
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


async def test_a_target_that_cannot_make_requests_is_resumed_without_one(endpoint):
    guard = await _guard(endpoint)
    before = endpoint.methods().count("Fetch.enable")
    await endpoint.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-ui",
            "targetInfo": {"targetId": "W1", "type": "browser_ui", "url": ""},
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


# ------------------------------------- every target that can make requests (N2)


@pytest.mark.parametrize(
    "kind", ["iframe", "worker", "service_worker", "shared_worker"]
)
async def test_every_target_that_can_make_requests_is_guarded_before_it_runs(
    endpoint, kind
):
    """A cross-site iframe is its own process and its own target: the page's
    interception does not cover what it fetches or the frames it navigates."""
    guard = await _guard(endpoint)
    await endpoint.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-child",
            "targetInfo": {"targetId": "C1", "type": kind, "url": "http://x.test/"},
            "waitingForDebugger": True,
        },
        session="S-T1",
    )
    await _settle(endpoint, "Runtime.runIfWaitingForDebugger")
    sessioned = [m for m, s in endpoint.log if s == "S-child"]
    assert sessioned[0] == "Fetch.enable"
    assert sessioned[-1] == "Runtime.runIfWaitingForDebugger"
    patterns = next(
        c["params"]["patterns"]
        for c in endpoint.commands
        if c["method"] == "Fetch.enable" and c.get("sessionId") == "S-child"
    )
    assert patterns[0] == {"urlPattern": f"*://*:{PORT}/*"}
    await guard.stop()


@pytest.mark.parametrize("kind", ["page", "iframe", "worker"])
async def test_a_target_that_can_start_children_is_watched_for_them(endpoint, kind):
    guard = await _guard(endpoint)
    await endpoint.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-p",
            "targetInfo": {"targetId": "P1", "type": kind, "url": ""},
            "waitingForDebugger": True,
        },
    )
    await _settle(endpoint, "Runtime.runIfWaitingForDebugger")
    auto = [
        c
        for c in endpoint.commands
        if c["method"] == "Target.setAutoAttach" and c.get("sessionId") == "S-p"
    ]
    assert [c["params"] for c in auto] == [
        {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
    ]
    await guard.stop()


async def test_a_frame_that_committed_at_the_endpoint_blanks_the_page_that_holds_it(
    endpoint,
):
    """Belt and braces: whatever the interception missed, a document at the
    endpoint's own origin does not stay in a page. The iframe's session is a child
    of the page's, and it is the PAGE that is sent to ``about:blank``."""
    guard = await _guard(endpoint)
    await endpoint.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-frame",
            "targetInfo": {"targetId": "F1", "type": "iframe", "url": "about:blank"},
            "waitingForDebugger": False,
        },
        session="S-T1",
    )
    await _settle(endpoint, "Fetch.enable", count=2)
    await endpoint.emit(
        "Target.targetInfoChanged",
        {
            "targetInfo": {
                "targetId": "F1",
                "type": "iframe",
                "url": f"http://127.0.0.1:{PORT}/devtools/inspector.html",
            }
        },
    )
    await _settle(endpoint, "Page.navigate")
    navigations = [c for c in endpoint.commands if c["method"] == "Page.navigate"]
    assert [(c["sessionId"], c["params"]) for c in navigations] == [
        ("S-T1", {"url": "about:blank"})
    ]
    await guard.stop()


async def test_the_logins_own_page_and_its_popups_are_not_watched_for_children(
    endpoint,
):
    """A paused popup of a page that is watched for children holds its opener inside
    ``window.open`` (a key handler on the login page) until the login's popup
    handling has closed it: the key press never returned. Those pages are the
    site's, which the vault types into anyway. An agent's page keeps the watch."""
    guard = await _guard(endpoint)
    login_seal.begin("inst")
    login_seal.seal_tab("inst", "LOGIN")
    try:
        for target, session, opener in (
            ("LOGIN", "S-login", None),
            ("POPUP", "S-popup", "LOGIN"),
            ("AGENT", "S-agent", "T1"),
        ):
            info = {"targetId": target, "type": "page", "url": "about:blank"}
            if opener:
                info["openerId"] = opener
            await endpoint.emit(
                "Target.attachedToTarget",
                {"sessionId": session, "targetInfo": info, "waitingForDebugger": True},
            )
        await _settle(endpoint, "Runtime.runIfWaitingForDebugger", count=3)
    finally:
        login_seal.end("inst")
    watched = {
        c["sessionId"]
        for c in endpoint.commands
        if c["method"] == "Target.setAutoAttach" and c.get("sessionId")
    }
    enabled = {
        c["sessionId"] for c in endpoint.commands if c["method"] == "Fetch.enable"
    }
    assert watched >= {"S-agent"}
    assert not watched & {"S-login", "S-popup"}
    assert {"S-login", "S-popup", "S-agent"} <= enabled  # all three are intercepted
    await guard.stop()


async def test_a_frame_at_the_endpoint_origin_is_held_paused_and_never_resumed(
    endpoint,
):
    """A frame that commits at the endpoint's own origin (the one Origin Chrome
    accepts) is left waiting for the debugger: none of its script runs while its
    page is sent to ``about:blank``."""
    guard = await _guard(endpoint)
    await endpoint.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-frame",
            "targetInfo": {
                "targetId": "F1",
                "type": "iframe",
                "url": f"http://127.0.0.1:{PORT}/devtools/inspector.html",
            },
            "waitingForDebugger": True,
        },
        session="S-T1",
    )
    await _settle(endpoint, "Page.navigate")
    await asyncio.sleep(0.1)
    assert [m for m, s in endpoint.log if s == "S-frame"] == []
    assert [
        (c["sessionId"], c["params"])
        for c in endpoint.commands
        if c["method"] == "Page.navigate"
    ] == [("S-T1", {"url": "about:blank"})]
    await guard.stop()


async def test_a_frame_that_cannot_be_guarded_sends_its_page_to_about_blank(endpoint):
    guard = await _guard(endpoint)
    endpoint.fail_fetch_enable = True
    await endpoint.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-frame",
            "targetInfo": {"targetId": "F1", "type": "iframe", "url": "http://x.test/"},
            "waitingForDebugger": True,
        },
        session="S-T1",
    )
    await _settle(endpoint, "Page.navigate")
    assert [
        (c["sessionId"], c["params"])
        for c in endpoint.commands
        if c["method"] == "Page.navigate"
    ] == [("S-T1", {"url": "about:blank"})]
    await guard.stop()


async def test_a_worker_that_cannot_be_guarded_still_runs_and_its_page_stays(endpoint):
    """Chrome refuses ``Fetch.enable`` on a dedicated worker. That is no reason to
    close the page that made it; the worker is resumed (the page's own interception
    and the endpoint's Origin check are what stand for it)."""
    guard = await _guard(endpoint)
    endpoint.fail_fetch_enable = True
    await endpoint.emit(
        "Target.attachedToTarget",
        {
            "sessionId": "S-w",
            "targetInfo": {
                "targetId": "W1",
                "type": "worker",
                "url": "blob:http://x/1",
            },
            "waitingForDebugger": True,
        },
        session="S-T1",
    )
    await _settle(endpoint, "Runtime.runIfWaitingForDebugger")
    assert "Target.closeTarget" not in endpoint.methods()
    assert "Page.navigate" not in endpoint.methods()
    await guard.stop()


async def test_idle_returns_with_a_finished_task_still_in_the_set(endpoint):
    """A task that is done but whose done-callback has not run is still in
    ``tasks``. Waiting on it with ``gather`` never yields, so the callback that
    would remove it never runs: a real spawn hung for minutes on exactly this."""
    guard = await _guard(endpoint)
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    guard.tasks.add(finished)  # as if its callback had not run yet
    async with asyncio.timeout(2):
        await guard.idle()
    await guard.stop()


# -------------------------------------------------- inspectors (re-review N1)

INSPECTOR_URLS = [
    "chrome://inspect",
    "chrome://inspect/",
    "chrome://inspect/#pages",
    "chrome://inspect/#devices",
    "CHROME://INSPECT/#pages",
    "chrome://Inspect",
    "chrome:inspect",
    "chrome:///inspect",
    "about:inspect",
    "about://inspect",
    "ABOUT:Inspect",
    "chrome://serviceworker-internals",
    "chrome://serviceworker-internals/?devtools=1",
    "chrome://extensions",
    "chrome://extensions/?id=abc",
    "devtools://devtools/bundled/inspector.html",
    "devtools://devtools/bundled/devtools_app.html?ws=127.0.0.1:1/devtools/page/x",
    "DEVTOOLS://devtools/bundled/inspector.html",
    "chrome-devtools://devtools/bundled/inspector.html",
    "  chrome://inspect/#pages  ",
    "\tchrome://inspect",
    "chrome://insp\nect",
    "chro\tme://inspect",
    "\x00chrome://inspect",
    "view-source:chrome://inspect",
    "chrome://inspect.",
    # re-review F2: Chrome decodes and canonicalises the host
    "chrome://%69nspect/#pages",
    "chrome://%49nspect",
    "chrome://%65xtensions/",
    "chrome://serviceworker%2Dinternals/",
    "chrome://%2569nspect",  # decoded twice by a careless reader: refused too
    "chrome://inspect:80/",
    "chrome://user:secret@inspect/",
    "chrome://inspect./#pages",
    "chrome://\uff49nspect/",  # full-width i
    "chrome://\uff29\uff2e\uff33\uff30\uff25\uff23\uff34/",
    "about:%69nspect",
    "about://%49NSPECT/",
    "view-source:chrome://%69nspect",
    "  chrome://%69nspect  ",
    # fourth review F2: Chrome maps the host with UTS 46, which drops and maps more
    "chrome://i\u00adnspect/",  # soft hyphen
    "chrome://inspect\u3002/",  # ideographic full stop
    "chrome://inspect%E3%80%82",  # the same, percent-encoded
    "chrome://inspect\uff61/",  # half-width ideographic full stop
    "chrome://ins\u200bpect/",  # zero-width space
    "chrome://insp\ufe0fect/",  # variation selector
    "chrome://i\u034fnspect/",  # combining grapheme joiner
    "chrome://%E2%80%8Binspect/",  # encoded zero-width space
    "about:ins\u00adpect",
    "view-source:chrome://i\u00adnspect",
    "chrome://inspect\\#pages",
]


@pytest.mark.parametrize("url", INSPECTOR_URLS)
def test_every_spelling_of_an_inspector_is_one(url):
    assert debug_port_guard.inspector_url(url), repr(url)
    assert debug_port_guard.refusal(url) == debug_port_guard.INSPECTOR_REFUSAL


@pytest.mark.parametrize(
    "url",
    [
        "about:blank",
        "https://example.com/inspect",
        "https://example.com/?u=chrome://inspect",
        "chrome://version",
        "chrome://settings",
        "chrome://flags",
        "chrome://gpu",
        "chrome://net-internals",
        "chrome://inspector",
        "chrome://myinspect",
        "chrome-untrusted://print/",
        "http://inspect/",
        "data:text/html,devtools://x",
        "",
        None,
        7,
    ],
)
def test_ordinary_pages_are_not_inspectors(url):
    assert not debug_port_guard.inspector_url(url), repr(url)


def test_a_devtools_frontend_is_told_from_a_chrome_inspector_page():
    assert debug_port_guard.is_frontend("devtools://devtools/bundled/inspector.html")
    assert debug_port_guard.is_frontend("chrome-devtools://devtools/x")
    assert not debug_port_guard.is_frontend("chrome://inspect")
    assert not debug_port_guard.is_frontend(None)


@pytest.mark.parametrize(
    ("tool", "argument"),
    [
        ("navigate", "url"),
        ("new_tab", "url"),
        ("create_simple_dynamic_hook", "target_url"),
    ],
)
@pytest.mark.parametrize(
    "url",
    [
        "chrome://inspect/#pages",
        "devtools://devtools/bundled/inspector.html",
        "about:inspect",
    ],
)
async def test_a_url_tool_refuses_an_inspector_with_no_browser_guarded(
    server_mod, tool, argument, url
):
    """The refusal needs no guarded browser: it is about the URL."""
    assert debug_port_guard.protected_ports() == frozenset()
    arguments = {"instance_id": "inst-1", argument: url}
    if tool == "create_simple_dynamic_hook":
        arguments = {
            "name": "h",
            "url_pattern": "*",
            "action": "redirect",
            argument: url,
        }
    with pytest.raises(ToolError) as caught:
        await call_tool(server_mod, tool, **arguments)
    assert caught.value.args[0] == debug_port_guard.INSPECTOR_REFUSAL


def _target(target, url, kind="page"):
    return {"targetInfo": {"targetId": target, "type": kind, "url": url}}


async def _wait_until(predicate, what):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(what)


FRONTEND = "devtools://devtools/bundled/devtools_app.html?ws=127.0.0.1:1/x"


async def test_a_frontend_is_barred_from_the_tools_the_moment_it_is_announced(
    endpoint,
):
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetCreated", _target("D1", FRONTEND))
    await _wait_until(lambda: login_seal.is_sealed("any", "D1"), "not barred")
    with pytest.raises(ToolError):
        login_seal.refuse_if_sealed(
            "execute_script", {"instance_id": "any", "tab_id": "D1"}
        )
    # no login runs: a person's own DevTools window is hidden, not closed
    await asyncio.sleep(0.1)
    assert "Target.closeTarget" not in endpoint.methods()
    # and it is not barred forever: it goes when the browser closes it
    await endpoint.emit("Target.targetDestroyed", {"targetId": "D1"})
    await _wait_until(lambda: not login_seal.is_sealed("any", "D1"), "still barred")
    await guard.stop()


async def test_a_frontend_that_appears_while_a_login_runs_is_closed(endpoint):
    guard = await _guard(endpoint)
    login_seal.begin("inst")
    try:
        await endpoint.emit("Target.targetCreated", _target("D2", FRONTEND))
        await _settle(endpoint, "Target.closeTarget")
    finally:
        login_seal.end("inst")
    closing = [c for c in endpoint.commands if c["method"] == "Target.closeTarget"]
    assert closing[-1]["params"] == {"targetId": "D2"}
    assert login_seal.is_sealed("any", "D2")
    await guard.stop()


async def test_a_tab_sent_to_chrome_inspect_is_blanked_at_once(endpoint):
    """The agent's own tab (the one a tool with no ``tab_id`` acts on) navigates to
    ``chrome://inspect``: it is barred AND sent back, whether or not a login runs."""
    guard = await _guard(endpoint)  # T1 is attached: session S-T1
    await endpoint.emit(
        "Target.targetInfoChanged", _target("T1", "chrome://inspect/#pages")
    )
    await _settle(endpoint, "Page.navigate")
    navigations = [c for c in endpoint.commands if c["method"] == "Page.navigate"]
    assert [(c["sessionId"], c["params"]) for c in navigations] == [
        ("S-T1", {"url": "about:blank"})
    ]
    assert login_seal.is_sealed("any", "T1")
    # once it reads about:blank again the tab is the agent's again
    await endpoint.emit("Target.targetInfoChanged", _target("T1", "about:blank"))
    await _wait_until(lambda: not login_seal.is_sealed("any", "T1"), "still barred")
    await guard.stop()


async def test_a_page_that_navigates_to_a_frontend_is_blanked_not_closed(endpoint):
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetInfoChanged", _target("T1", FRONTEND))
    await _settle(endpoint, "Page.navigate")
    assert "Target.closeTarget" not in endpoint.methods()
    await guard.stop()


async def test_inspectors_already_open_when_the_guard_starts_are_barred():
    fake = FakeEndpoint(page_url="chrome://inspect/#pages")
    await fake.start()
    guard = await _guard(fake)
    assert login_seal.is_sealed("any", "T1")
    await guard.stop()
    await fake.stop()


async def test_the_bars_stay_when_the_guard_ends(endpoint):
    """Re-review F4: a guard that is gone is when a DevTools window must stay out
    of reach, so its bars are not lifted with it. Only Chrome saying the target is
    destroyed lifts one, and only a live guard hears that."""
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetCreated", _target("D3", FRONTEND))
    await _wait_until(lambda: login_seal.is_sealed("any", "D3"), "not barred")
    task = asyncio.create_task(guard.run())
    await endpoint.socket.close()
    await asyncio.wait_for(task, 5)
    assert login_seal.is_sealed("any", "D3")
    assert PORT not in debug_port_guard.protected_ports()


async def test_a_login_under_watch_is_cancelled_and_told_when_the_guard_ends(
    endpoint,
):
    guard = await _guard(endpoint)
    browser = SimpleNamespace(config=SimpleNamespace(port=PORT))
    debug_port_guard._GUARDS[PORT] = guard
    task = asyncio.create_task(guard.run())
    cleaned = []

    async def login():
        try:
            async with debug_port_guard.watch(browser):
                await asyncio.sleep(30)
        finally:
            cleaned.append(True)  # the login's own cleanup ran

    running = asyncio.create_task(login())
    await asyncio.sleep(0.05)
    await endpoint.socket.close()
    with pytest.raises(ToolError) as caught:
        await asyncio.wait_for(running, 5)
    assert str(caught.value) == debug_port_guard.GUARD_DOWN
    assert cleaned == [True]
    await asyncio.wait_for(task, 5)


async def test_watch_with_no_guard_refuses_at_once():
    browser = SimpleNamespace(config=SimpleNamespace(port=PORT + 3))
    with pytest.raises(ToolError):
        async with debug_port_guard.watch(browser):
            pass


async def test_an_outer_cancel_is_not_turned_into_a_guard_error(endpoint):
    guard = await _guard(endpoint)
    browser = SimpleNamespace(config=SimpleNamespace(port=PORT))
    debug_port_guard._GUARDS[PORT] = guard

    async def login():
        async with debug_port_guard.watch(browser):
            await asyncio.sleep(30)

    running = asyncio.create_task(login())
    await asyncio.sleep(0.05)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    debug_port_guard._GUARDS.pop(PORT, None)
    await guard.stop()


# ------------------------------------------ the class rule while a login runs (F3)

INTERNAL = [
    "chrome://version",
    "chrome://history/",
    "chrome://settings",
    "chrome://net-export",
    "chrome://accessibility",
    "chrome://%76ersion",
    "CHROME://VERSION",
    "chrome-untrusted://print/",
    "chrome-extension://abcdefghijklmnop/page.html",
    "chrome-search://most-visited/title.html",
    "devtools://devtools/bundled/inspector.html",
    "chrome-devtools://devtools/bundled/inspector.html",
    "view-source:chrome://version",
]
ORDINARY = [
    "about:blank",
    "https://example.com/chrome://version",
    "http://127.0.0.1:1/",
    "data:text/html,chrome://version",
    "chrome-error://chromewebdata/",
    "chrome://newtab/",
    "chrome://new-tab-page/",
    "chrome-untrusted://new-tab-page/",
    "chrome-search://local-ntp/",
    "",
    None,
    7,
]


@pytest.mark.parametrize("url", INTERNAL)
def test_a_browser_internal_page_is_of_the_class(url):
    assert debug_port_guard.internal_url(url), repr(url)


@pytest.mark.parametrize("url", ORDINARY)
def test_other_pages_and_the_new_tab_page_are_not(url):
    assert not debug_port_guard.internal_url(url), repr(url)


@pytest.mark.parametrize("url", INTERNAL)
def test_while_a_login_runs_the_url_tools_refuse_the_class(url):
    assert debug_port_guard.refusal(url) is None or debug_port_guard.inspector_url(url)
    login_seal.begin("inst")
    try:
        assert debug_port_guard.refusal(url) is not None
        with pytest.raises(ToolError):
            debug_port_guard.refuse_urls("navigate", {"url": url})
        with pytest.raises(ToolError):
            debug_port_guard.refuse_urls("new_tab", {"url": url})
    finally:
        login_seal.end("inst")


@pytest.mark.parametrize(
    "url", [u for u in INTERNAL if "inspect" not in u and "devtools" not in u]
)
def test_outside_a_login_only_the_inspector_pages_stay_refused(url):
    assert debug_port_guard.refusal(url) is None, url


@pytest.mark.parametrize("url", ORDINARY)
def test_during_a_login_about_blank_and_the_new_tab_page_still_load(url):
    login_seal.begin("inst")
    try:
        assert debug_port_guard.refusal(url) is None, repr(url)
    finally:
        login_seal.end("inst")


async def test_an_internal_page_is_hidden_only_while_a_login_runs(endpoint):
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetCreated", _target("V1", "chrome://version/"))
    await endpoint.emit("Target.targetCreated", _target("N1", "chrome://newtab/"))
    await endpoint.emit("Target.targetCreated", _target("W1", "https://example.com/"))
    await _wait_until(lambda: "V1" in login_seal._internal, "not noted")
    assert not login_seal.is_sealed("inst", "V1")  # no login: ordinary use
    login_seal.begin("inst")
    try:
        assert login_seal.is_sealed("inst", "V1")
        assert not login_seal.is_sealed("inst", "N1")
        assert not login_seal.is_sealed("inst", "W1")
        with pytest.raises(ToolError) as caught:
            login_seal.refuse_if_sealed(
                "execute_script", {"instance_id": "inst", "tab_id": "V1"}
            )
        assert str(caught.value) == login_seal.REFUSAL
    finally:
        login_seal.end("inst")
    assert not login_seal.is_sealed("inst", "V1")  # and shown again afterwards
    await endpoint.emit("Target.targetDestroyed", {"targetId": "V1"})
    await _wait_until(lambda: "V1" not in login_seal._internal, "not forgotten")
    await guard.stop()


async def test_a_page_that_navigates_to_an_internal_page_is_hidden_and_back_again(
    endpoint,
):
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetCreated", _target("P1", "https://example.com/"))
    await endpoint.emit("Target.targetInfoChanged", _target("P1", "chrome://version/"))
    await _wait_until(lambda: "P1" in login_seal._internal, "not noted")
    await endpoint.emit(
        "Target.targetInfoChanged", _target("P1", "https://example.com/")
    )
    await _wait_until(lambda: "P1" not in login_seal._internal, "not cleared")
    await guard.stop()


# ----------------------------------------- honest refusals and the window count (F5)


def test_a_barred_window_with_no_login_is_not_called_a_login():
    login_seal.bar("D9")
    arguments = {"instance_id": "inst", "tab_id": "D9"}
    with pytest.raises(ToolError) as caught:
        login_seal.refuse_if_sealed("execute_script", arguments)
    assert str(caught.value) == login_seal.INSPECTOR_REFUSAL
    assert "login" not in str(caught.value)
    login_seal.begin("inst")
    try:
        with pytest.raises(ToolError) as caught:
            login_seal.refuse_if_sealed("execute_script", arguments)
        assert str(caught.value) == login_seal.REFUSAL
    finally:
        login_seal.end("inst")


async def test_the_windows_a_login_closes_are_counted_once_each(guarded):
    browser = ListedBrowser(
        [
            _info("D1", FRONTEND),
            _info("I1", "chrome://inspect"),
            _info("T1", "about:blank"),
        ]
    )
    before = debug_port_guard.closed_windows(browser)
    await debug_port_guard.verify_clean(browser)
    assert debug_port_guard.closed_windows(browser) - before == 1  # not the page
    again = ListedBrowser([_info("D1", FRONTEND)])
    await debug_port_guard.verify_clean(again)
    assert debug_port_guard.closed_windows(again) - before == 1  # the same id: once


async def test_another_browsers_closed_windows_are_not_this_logins(guarded):
    """F11: the count is per browser (debug port). A window closed in one browser
    must never appear in the result of a login in another."""
    here = ListedBrowser([_info("D1", FRONTEND)])
    elsewhere = ListedBrowser([_info("D2", FRONTEND)])
    elsewhere.config.port = PORT + 1
    debug_port_guard._PORTS.add(PORT + 1)
    try:
        await debug_port_guard.verify_clean(elsewhere)
    finally:
        debug_port_guard._PORTS.discard(PORT + 1)
    assert debug_port_guard.closed_windows(elsewhere) == 1
    assert debug_port_guard.closed_windows(here) == 0


async def test_the_guards_own_close_of_a_frontend_is_counted(endpoint):
    browser = SimpleNamespace(config=SimpleNamespace(port=PORT))
    before = debug_port_guard.closed_windows(browser)
    guard = await _guard(endpoint)
    login_seal.begin("inst", PORT)
    try:
        await endpoint.emit("Target.targetCreated", _target("D7", FRONTEND))
        await _settle(endpoint, "Target.closeTarget")
        await _wait_until(
            lambda: debug_port_guard.closed_windows(browser) > before, "not counted"
        )
    finally:
        login_seal.end("inst")
    await guard.stop()


async def test_a_login_in_another_browser_does_not_close_this_browsers_window(
    endpoint,
):
    """F11: the guard closes a DevTools window only for a login in ITS browser. A
    person's F12 window in a browser where nothing logs in is hidden, not destroyed,
    however busy the others are."""
    guard = await _guard(endpoint)
    login_seal.begin("other-instance", PORT + 7)
    try:
        await endpoint.emit("Target.targetCreated", _target("D8", FRONTEND))
        await _wait_until(lambda: login_seal.is_sealed("any", "D8"), "not barred")
        await asyncio.sleep(0.2)
        assert "Target.closeTarget" not in endpoint.methods()
    finally:
        login_seal.end("other-instance")
    await guard.stop()


def test_a_login_in_one_instance_does_not_hide_or_refuse_pages_in_another():
    """F11: the internal-page rule is per instance. Another instance's login
    refuses neither a ``chrome://`` URL for this instance nor its tabs."""
    login_seal.mark_internal("V9", True)
    login_seal.begin("busy", PORT)
    try:
        assert login_seal.is_sealed("busy", "V9")
        assert not login_seal.is_sealed("idle", "V9")
        assert debug_port_guard.refusal("chrome://version", "busy") is not None
        assert debug_port_guard.refusal("chrome://version", "idle") is None
        # a call that names no instance takes the cautious reading
        assert debug_port_guard.refusal("chrome://version") is not None
    finally:
        login_seal.end("busy")
        login_seal.mark_internal("V9", False)


# ------------------------------------------ the check before a login types


def _info(target, url):
    return cdp.target.TargetInfo(
        target_id=cdp.target.TargetID(target),
        type_="page",
        title="",
        url=url,
        attached=False,
        can_access_opener=False,
    )


class ListedBrowser(FakeBrowser):
    """A browser whose target list is ``listed``; ``closes`` says whether a close
    takes the target out of it, as Chrome's does."""

    def __init__(self, listed, *, closes=True):
        super().__init__()
        self.config = SimpleNamespace(host="127.0.0.1", port=PORT)
        self.listed = list(listed)
        self.closed_targets: list[str] = []
        self._cdp_responses["get_targets"] = lambda _name: list(self.listed)

        def close(_name):
            target = self.cdp_frames[-1]["params"]["targetId"]
            self.closed_targets.append(target)
            if closes:
                self.listed = [i for i in self.listed if str(i.target_id) != target]
            return True

        self._cdp_responses["close_target"] = close


async def test_a_browser_with_no_inspector_and_a_live_guard_is_clean(guarded):
    browser = ListedBrowser([_info("T1", "about:blank")])
    await debug_port_guard.verify_clean(browser)
    assert browser.closed_targets == []


async def test_an_inspector_that_was_already_open_is_closed_before_the_login(guarded):
    browser = ListedBrowser(
        [
            _info("T1", "about:blank"),
            _info("D1", FRONTEND),
            _info("I1", "chrome://inspect"),
        ]
    )
    await debug_port_guard.verify_clean(browser)
    assert sorted(browser.closed_targets) == ["D1", "I1"]
    assert [str(i.target_id) for i in browser.listed] == ["T1"]


async def test_a_window_that_will_not_close_refuses_the_login(guarded, monkeypatch):
    monkeypatch.setattr(debug_port_guard, "_CLEAN_RETRY_S", 0.0)
    browser = ListedBrowser([_info("D1", FRONTEND)], closes=False)
    with pytest.raises(ToolError) as caught:
        await debug_port_guard.verify_clean(browser)
    assert caught.value.args[0] == debug_port_guard.INSPECTOR_OPEN
    assert browser.closed_targets == ["D1"] * 3  # tried three times, then refused
    assert login_seal.is_sealed("any", "D1")


async def test_a_browser_whose_guard_has_ended_refuses_the_login(monkeypatch):
    """The guard's connection dropped while the browser lives: the port is no
    longer protected, and a login must not carry on as if it were."""
    browser = ListedBrowser([_info("T1", "about:blank")])
    assert PORT not in debug_port_guard.protected_ports()
    with pytest.raises(ToolError) as caught:
        await debug_port_guard.verify_clean(browser)
    assert caught.value.args[0] == debug_port_guard.GUARD_DOWN
    assert browser.closed_targets == []


async def test_a_browser_with_no_known_port_refuses_the_login():
    with pytest.raises(ToolError):
        await debug_port_guard.verify_clean(SimpleNamespace(config=None))


# ------------------------------------------------------- vault mode (T-19366)


@pytest.fixture
def mode_on(tmp_path, monkeypatch):
    """A vault file exists: vault mode is on, and its download folder is the test's."""
    from stealth_chrome_devtools_mcp.embedded import vault_mode, vault_store

    vault = tmp_path / "vault.json"
    vault.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vault_store, "VAULT_PATH", vault)
    monkeypatch.setattr(vault_mode, "download_dir", lambda: tmp_path / "downloads")
    return vault


def _downloads(endpoint):
    return [
        c["params"]
        for c in endpoint.commands
        if c["method"] == "Browser.setDownloadBehavior"
    ]


async def test_with_no_vault_the_guard_sends_no_download_command_at_all(endpoint):
    guard = await _guard(endpoint)
    assert not _downloads(endpoint)
    assert guard.keeper is not None
    await guard.stop()


async def test_a_browser_is_pinned_before_it_is_handed_on_while_a_vault_exists(
    endpoint, mode_on, tmp_path
):
    guard = await _guard(endpoint)
    pins = _downloads(endpoint)
    assert len(pins) == 1
    assert pins[0]["behavior"] == "allowAndName"
    assert pins[0]["eventsEnabled"] is False
    assert (tmp_path / "downloads").samefile(pins[0]["downloadPath"])
    # sent at the BROWSER (no session), and after the guard's own set-up
    sent = [
        c for c in endpoint.commands if c["method"] == "Browser.setDownloadBehavior"
    ]
    assert "sessionId" not in sent[0]
    assert endpoint.methods().index(
        "Browser.setDownloadBehavior"
    ) > endpoint.methods().index("Target.setAutoAttach")
    await guard.stop()


async def test_protect_pins_a_re_attached_browser_the_same_way(endpoint, mode_on):
    """Spawn and re-attach both end in ``protect``: the pin is made there, so a
    browser an older backend started is pinned too, before anyone is handed it."""
    browser = SimpleNamespace(
        config=SimpleNamespace(port=PORT), websocket_url=endpoint.url
    )
    await debug_port_guard.protect(browser)
    assert len(_downloads(endpoint)) == 1
    await debug_port_guard._GUARDS[PORT].stop()


async def test_a_pin_that_cannot_be_made_refuses_the_browser(mode_on):
    fake = FakeEndpoint()

    async def refusing(socket):
        fake.socket = socket
        async for raw in socket:
            message = json.loads(raw)
            fake.commands.append(message)
            fake.log.append((message["method"], message.get("sessionId")))
            reply = {"id": message["id"], "result": {}}
            if message["method"] == "Browser.setDownloadBehavior":
                reply = {"id": message["id"], "error": {"message": "no"}}
            await socket.send(json.dumps(reply))

    fake.handle = refusing
    await fake.start()
    browser = SimpleNamespace(config=SimpleNamespace(port=PORT), websocket_url=fake.url)
    with pytest.raises(ToolError, match="could not be guarded"):
        await debug_port_guard.protect(browser)
    assert PORT not in debug_port_guard.protected_ports()
    await fake.stop()


async def test_a_vault_made_after_the_browser_pins_it_within_one_pass(
    endpoint, tmp_path, monkeypatch
):
    from stealth_chrome_devtools_mcp.embedded import vault_mode, vault_store

    monkeypatch.setattr(vault_mode, "download_dir", lambda: tmp_path / "downloads")
    monkeypatch.setattr(debug_port_guard, "KEEP_SECONDS", 0.05)
    guard = await _guard(endpoint)
    assert not _downloads(endpoint)
    vault = tmp_path / "vault.json"
    vault.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vault_store, "VAULT_PATH", vault)
    await _wait_until(lambda: _downloads(endpoint), "never pinned")
    assert _downloads(endpoint)[-1]["behavior"] == "allowAndName"
    # it is re-asserted, not said once: a folder set in between is taken back
    count = len(_downloads(endpoint))
    await _wait_until(lambda: len(_downloads(endpoint)) > count, "not re-asserted")
    # and the mode ending hands the browser's own default back, once
    vault.unlink()
    await _wait_until(
        lambda: _downloads(endpoint)[-1] == {"behavior": "default"}, "not lifted"
    )
    settled = len(_downloads(endpoint))
    await asyncio.sleep(0.2)
    assert len(_downloads(endpoint)) == settled
    await guard.stop()


FILE_PAGE = "file:///C:/Users/x/.stealth-mcp/vault.json"


async def test_a_file_page_is_barred_and_blanked_like_an_inspector(endpoint, mode_on):
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetInfoChanged", _target("T1", FILE_PAGE))
    await _settle(endpoint, "Page.navigate")
    navigations = [c for c in endpoint.commands if c["method"] == "Page.navigate"]
    assert [(c["sessionId"], c["params"]) for c in navigations] == [
        ("S-T1", {"url": "about:blank"})
    ]
    assert login_seal.is_sealed("any", "T1")  # hidden from every tool at once
    await endpoint.emit("Target.targetInfoChanged", _target("T1", "about:blank"))
    await _wait_until(lambda: not login_seal.is_sealed("any", "T1"), "still barred")
    await guard.stop()


async def test_a_file_page_is_left_alone_with_no_vault(endpoint):
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetInfoChanged", _target("T1", FILE_PAGE))
    await asyncio.sleep(0.1)
    assert "Page.navigate" not in endpoint.methods()
    assert not login_seal.is_sealed("any", "T1")
    await guard.stop()


async def test_a_file_page_already_open_when_the_vault_appears_is_blanked(
    tmp_path, monkeypatch
):
    from stealth_chrome_devtools_mcp.embedded import vault_mode, vault_store

    fake = FakeEndpoint(page_url=FILE_PAGE)
    await fake.start()
    monkeypatch.setattr(vault_mode, "download_dir", lambda: tmp_path / "downloads")
    monkeypatch.setattr(debug_port_guard, "KEEP_SECONDS", 0.05)
    guard = await _guard(fake)
    assert "Page.navigate" not in fake.methods()  # no vault: it was left alone
    vault = tmp_path / "vault.json"
    vault.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(vault_store, "VAULT_PATH", vault)
    await _settle(fake, "Page.navigate")
    assert login_seal.is_sealed("any", "T1")
    await guard.stop()
    await fake.stop()


# ------------------------------------------- WebUI pages and a failed pin (audit 6)

WEBUI = [
    "chrome://settings",
    "chrome://settings/downloads",
    "chrome://net-export/",
    "CHROME://flags",
    "chrome-untrusted://terminal/",
    "devtools://devtools/bundled/inspector.html",
    "view-source:chrome://version",
    " chrome://policy",
    "chrome://\tsettings",
]
NOT_WEBUI = [
    "about:blank",
    "chrome://newtab/",
    "chrome://new-tab-page/",
    "https://example.com/chrome://settings",
    "http://127.0.0.1:1/",
    "chrome-extension://abc/page.html",
    "",
]


@pytest.mark.parametrize("url", WEBUI)
def test_a_webui_page_is_read_as_one_in_every_spelling(url):
    assert debug_port_guard.webui_url(url)


@pytest.mark.parametrize("url", NOT_WEBUI)
def test_other_pages_are_not_webui(url):
    assert not debug_port_guard.webui_url(url)


@pytest.mark.parametrize("url", WEBUI)
def test_the_url_tools_refuse_a_webui_page_only_while_a_vault_exists(url, mode_on):
    from stealth_chrome_devtools_mcp.embedded import vault_mode

    reason = debug_port_guard.refusal(url)
    assert reason is not None
    assert "stealthy login destroy" in reason or reason == (
        debug_port_guard.INSPECTOR_REFUSAL
    )
    assert vault_mode.active()


@pytest.mark.parametrize("url", WEBUI)
def test_with_no_vault_a_webui_page_is_loadable_as_before(url):
    from stealth_chrome_devtools_mcp.embedded import vault_mode

    assert not vault_mode.active()
    reason = debug_port_guard.refusal(url)
    assert reason in (None, debug_port_guard.INSPECTOR_REFUSAL)  # inspectors only


@pytest.mark.parametrize(
    "url", ["about:blank", "chrome://newtab/", "https://example.com/"]
)
def test_blank_and_new_tab_still_load_while_a_vault_exists(url, mode_on):
    assert debug_port_guard.refusal(url) is None


async def test_a_webui_target_is_hidden_and_refused_with_the_vault_sentence(
    endpoint, mode_on
):
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetInfoChanged", _target("T1", "chrome://settings"))
    await _wait_until(lambda: login_seal.is_sealed("any", "T1"), "not hidden")
    with pytest.raises(ToolError) as caught:
        login_seal.refuse_if_sealed(
            "switch_tab", {"instance_id": "any", "tab_id": "T1"}
        )
    assert "stealthy login destroy" in str(caught.value)
    assert "login is running" not in str(caught.value)
    # hidden, NOT blanked: the product's own session restore opens chrome://settings
    await asyncio.sleep(0.1)
    assert "Page.navigate" not in endpoint.methods()
    await endpoint.emit("Target.targetInfoChanged", _target("T1", "about:blank"))
    await _wait_until(lambda: not login_seal.is_sealed("any", "T1"), "still hidden")
    await guard.stop()


async def test_a_webui_target_is_left_alone_with_no_vault(endpoint):
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetInfoChanged", _target("T1", "chrome://settings"))
    await asyncio.sleep(0.1)
    assert not login_seal.is_sealed("any", "T1")
    await guard.stop()


async def test_a_webui_target_is_not_marked_with_no_vault_but_is_by_the_next_pass_after_one(
    endpoint, request
):
    """F5: no vault, no mark (so ``any_sealing`` stays false and no tool call binds
    its arguments); a vault made while the page is open marks it on the next pass."""
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetInfoChanged", _target("T1", "chrome://settings"))
    await asyncio.sleep(0.2)
    assert "T1" not in login_seal._webui
    assert not login_seal.any_sealing()
    request.getfixturevalue("mode_on")
    endpoint.page_url = "chrome://settings"
    await guard.keep()
    assert login_seal.is_sealed("any", "T1")
    assert login_seal.any_sealing()
    await guard.stop()


async def test_the_new_tab_page_is_not_hidden_while_a_vault_exists(endpoint, mode_on):
    guard = await _guard(endpoint)
    await endpoint.emit("Target.targetInfoChanged", _target("T1", "chrome://newtab/"))
    await asyncio.sleep(0.1)
    assert not login_seal.is_sealed("any", "T1")
    await guard.stop()


async def test_a_pass_that_cannot_pin_marks_the_browser_and_logins_refuse(
    mode_on, monkeypatch, caplog
):
    """N4: spawn and attach fail closed, a LATER failed pass must not leave a browser
    driven and unpinned silently: it is marked, logged once at ERROR, and a login in
    it is refused until a pass succeeds."""
    monkeypatch.setattr(debug_port_guard, "KEEP_SECONDS", 0.03)
    fake = FakeEndpoint()
    await fake.start()
    guard = await _guard(fake)
    debug_port_guard._GUARDS[PORT] = guard
    debug_port_guard._PORTS.add(PORT)
    browser = SimpleNamespace(config=SimpleNamespace(port=PORT))
    try:
        assert not guard.pin_failed
        fake.refuse_pin = True
        with caplog.at_level("ERROR", logger=debug_port_guard._logger.name):
            await _wait_until(lambda: guard.pin_failed, "the failed pass was not seen")
            await asyncio.sleep(0.2)  # several more failed passes
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1, [r.getMessage() for r in errors]
        with pytest.raises(ToolError) as caught:
            await debug_port_guard.verify_clean(browser)
        assert str(caught.value) == debug_port_guard.PIN_DOWN
        fake.refuse_pin = False
        await _wait_until(lambda: not guard.pin_failed, "the pin was never retried")
        assert not guard.pin_failed
    finally:
        await guard.stop()
        debug_port_guard._GUARDS.pop(PORT, None)
        debug_port_guard._PORTS.discard(PORT)
        await fake.stop()


async def test_a_failed_pass_with_no_vault_marks_nothing(monkeypatch):
    monkeypatch.setattr(debug_port_guard, "KEEP_SECONDS", 0.03)
    fake = FakeEndpoint()
    await fake.start()
    guard = await _guard(fake)
    fake.refuse_pin = True
    await asyncio.sleep(0.15)
    assert not guard.pin_failed
    await guard.stop()
    await fake.stop()


def test_a_browser_with_no_port_has_closed_no_windows_and_no_guard():
    """N3 (audit 5, NIT): ``0`` is the whole truth there, because the same browser is
    refused by ``verify_clean`` before any login counts."""
    assert debug_port_guard.closed_windows(SimpleNamespace(config=None)) == 0
    assert (
        debug_port_guard.closed_windows(SimpleNamespace(config=SimpleNamespace(port=0)))
        == 0
    )
