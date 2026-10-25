"""F-939 — a clone never rotates Google's session cookies.

Pins what is decidable without Chrome: which URLs the patterns cover, the opt-out,
the CDP conversation of the guard against a scripted Chrome (every attached
session is blocked and auto-attached further BEFORE it is resumed, the block is
``Network.setBlockedURLs`` and never ``Fetch.enable`` (F-999), a dropped
connection ends the guard), and that a spawn arms the guard for a clone and for
nothing else. The commands are compared with the ones nodriver's own ``cdp``
constructors build, so a renamed field cannot pass. The behavior against a real
Chrome is ``tests/test_e2e_google_rotation_guard.py``.
"""

import asyncio
import json
from fnmatch import fnmatchcase
from types import SimpleNamespace

import pytest
import websockets.asyncio.server
from nodriver.cdp import network as cdp_network

from fakes import FakeBrowser
from stealth_chrome_devtools_mcp.embedded import google_rotation_guard
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.settings import get_settings


def _command(generator) -> dict:
    """The CDP message nodriver itself builds for a ``cdp`` command."""
    return next(generator)


_NETWORK_ENABLE = _command(
    cdp_network.enable(
        max_total_buffer_size=0, max_resource_buffer_size=0, max_post_data_size=0
    )
)


def _blocked(url: str) -> bool:
    return any(
        fnmatchcase(url, pattern)
        for pattern in google_rotation_guard.BLOCKED_URL_PATTERNS
    )


class TestWhatIsBlocked:
    @pytest.mark.parametrize(
        "url",
        [
            "https://accounts.google.com/RotateCookies",
            "https://accounts.google.com/RotateCookies?origin=https%3A%2F%2Fmail.google.com",
            "https://accounts.google.com/RotateCookiesPage?origin=https://www.google.com",
            "https://accounts.google.com/rotateCookies",
            "https://accounts.google.com/RotateBoundCookies",
            "https://accounts.youtube.com/RotateCookiesPage?origin=https://www.youtube.com",
            "https://accounts.youtube.com/RotateCookies",
            "http://accounts.google.com/RotateCookies",
        ],
    )
    def test_the_rotation_endpoints(self, url):
        assert _blocked(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://accounts.google.com/",
            "https://accounts.google.com/ServiceLogin",
            "https://accounts.google.com/ListAccounts?gpsia=1",
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://mail.google.com/mail/u/0/",
            "https://www.google.com/search?q=RotateCookies",
            "https://example.com/RotateCookies",
            "https://accounts.google.com.evil.example/RotateCookies",
        ],
    )
    def test_nothing_else(self, url):
        assert not _blocked(url)


async def _wire_patterns() -> list[str]:
    """The patterns the guard really sends in ``Network.setBlockedURLs``."""
    async with ScriptedChrome() as chrome:
        guard = google_rotation_guard.RotationGuard()
        await guard.start(chrome.url)
        await _settle(chrome, "Network.setBlockedURLs", "page-1")
        await guard.close()
    return next(p for s, m, p in chrome.log if m == "Network.setBlockedURLs")["urls"]


def _wire_blocks(patterns: list[str], url: str) -> bool:
    return any(fnmatchcase(url, pattern) for pattern in patterns)


class TestEveryUrlFormIsBlockedOnTheWire:
    """F-999: the same URL forms the ``Fetch`` patterns covered, now judged on the
    ``urls`` of the ``Network.setBlockedURLs`` command the guard SENDS (Chrome's
    wildcard is ``*`` for any run of characters, which ``fnmatch`` models for
    these patterns: none contains ``?`` or ``[``)."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://accounts.google.com/RotateCookies",
            "https://accounts.google.com/RotateCookies?origin=https%3A%2F%2Fmail.google.com",
            "https://accounts.google.com/RotateCookiesPage?origin=https://www.google.com",
            "https://accounts.google.com/rotateCookies",
            "https://accounts.google.com/rotateCookiesPage",
            "https://accounts.google.com/RotateBoundCookies",
            "https://accounts.google.com/rotateBoundCookies",
            "https://accounts.youtube.com/RotateCookiesPage?origin=https://www.youtube.com",
            "https://accounts.youtube.com/RotateCookies",
            "https://accounts.youtube.com/RotateBoundCookies",
            "https://accounts.youtube.com/rotateBoundCookies",
            "http://accounts.google.com/RotateCookies",
            "http://accounts.youtube.com/rotateCookies",
        ],
    )
    async def test_the_rotation_urls(self, url):
        assert _wire_blocks(await _wire_patterns(), url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://accounts.google.com/",
            "https://accounts.google.com/ServiceLogin",
            "https://accounts.google.com/ListAccounts?gpsia=1",
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://accounts.youtube.com/accounts/SetSID",
            "https://www.google.com/search?q=RotateCookies",
            "https://example.com/RotateCookies",
            "https://accounts.google.com.evil.example/RotateCookies",
        ],
    )
    async def test_nothing_else(self, url):
        assert not _wire_blocks(await _wire_patterns(), url)


class TestOptOut:
    def test_on_by_default(self):
        get_settings.cache_clear()
        assert google_rotation_guard.enabled()

    def test_the_env_var_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("STEALTH_MCP_ALLOW_CLONE_GOOGLE_ROTATION", "true")
        get_settings.cache_clear()
        try:
            assert not google_rotation_guard.enabled()
        finally:
            monkeypatch.undo()
            get_settings.cache_clear()


class ScriptedChrome:
    """A browser-level CDP endpoint that attaches one page, which has an iframe, and
    with ``workers`` a service worker and a shared worker beside it."""

    def __init__(self, workers=False):
        self.workers = workers
        self.log: list[tuple[str | None, str, dict]] = []
        self.paused_reply: list[str] = []
        self._server = None
        self.port = 0

    async def __aenter__(self):
        self._server = await websockets.asyncio.server.serve(
            self._handle, "127.0.0.1", 0
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/browser/x"

    async def send_raw(self, text):
        await self._ws.send(text)

    async def _handle(self, ws):
        self._ws = ws
        async for raw in ws:
            message = json.loads(raw)
            session = message.get("sessionId")
            self.log.append((session, message["method"], message.get("params", {})))
            reply = {"id": message["id"], "result": {}}
            if session:
                reply["sessionId"] = session
            await ws.send(json.dumps(reply))
            if message["method"] == "Target.setAutoAttach" and session is None:
                await self._attach(ws, "page-1", "page")
                if self.workers:
                    await self._attach(ws, "sw-1", "service_worker")
                    await self._attach(ws, "shw-1", "shared_worker")
            if message["method"] == "Target.setAutoAttach" and session == "page-1":
                await self._attach(ws, "frame-1", "iframe")
            if message["method"] == "Network.setBlockedURLs" and session == "frame-1":
                # What Network.enable makes Chrome send (compact, method first, as
                # Chrome writes it): the guard must drop these.
                await ws.send(
                    json.dumps(
                        {
                            "method": "Network.requestWillBeSent",
                            "params": {"requestId": "req-9"},
                            "sessionId": "frame-1",
                        },
                        separators=(",", ":"),
                    )
                )

    @staticmethod
    async def _attach(ws, session, kind):
        await ws.send(
            json.dumps(
                {
                    "method": "Target.attachedToTarget",
                    "params": {
                        "sessionId": session,
                        "targetInfo": {"type": kind},
                        "waitingForDebugger": True,
                    },
                }
            )
        )


async def _settle(chrome, method, session):
    for _ in range(100):
        if any(m == method and s == session for s, m, _ in chrome.log):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"never saw {method} on {session}: {chrome.log}")


class TestTheGuardConversation:
    async def test_every_attached_session_is_blocked_then_resumed(self):
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard(["*://x/RotateCookies*"])
            await guard.start(chrome.url)
            await _settle(chrome, "Runtime.runIfWaitingForDebugger", "frame-1")
            await guard.close()

        root = [(m, p) for s, m, p in chrome.log if s is None]
        assert root == [
            (
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
            )
        ]
        for session in ("page-1", "frame-1"):
            methods = [m for s, m, _ in chrome.log if s == session]
            assert methods[:4] == [
                "Network.enable",
                "Network.setBlockedURLs",
                "Target.setAutoAttach",
                "Runtime.runIfWaitingForDebugger",
            ], (session, methods)
        wire = {m: p for s, m, p in chrome.log if m.startswith("Network.")}
        assert {"method": "Network.enable", "params": wire["Network.enable"]} == (
            _NETWORK_ENABLE
        )
        assert {
            "method": "Network.setBlockedURLs",
            "params": wire["Network.setBlockedURLs"],
        } == _command(cdp_network.set_blocked_ur_ls(urls=["*://x/RotateCookies*"]))

    async def test_service_and_shared_workers_are_blocked_before_they_run(self):
        """Chrome attaches a service worker or a shared worker like a page, paused
        before it runs; its requests are not the page's, so it needs its own block
        and must be resumed only after it (the ordering is the whole guarantee)."""
        async with ScriptedChrome(workers=True) as chrome:
            guard = google_rotation_guard.RotationGuard()
            await guard.start(chrome.url)
            for worker in ("sw-1", "shw-1"):
                await _settle(chrome, "Runtime.runIfWaitingForDebugger", worker)
            await guard.close()
        for worker in ("sw-1", "shw-1"):
            methods = [m for s, m, _ in chrome.log if s == worker]
            assert methods[:2] == ["Network.enable", "Network.setBlockedURLs"], (
                worker,
                methods,
            )
            assert methods.index("Runtime.runIfWaitingForDebugger") > methods.index(
                "Network.setBlockedURLs"
            ), (worker, methods)

    async def test_the_interception_domain_is_never_used(self):
        """F-999: Fetch interception on every page of a clone is what aborted the
        page's CORS POST (macOS 25/216 armed runs, 0/216 without the guard). Not
        even a Document-only pattern is an exception: it paused nothing and still
        failed 16/216 (round 10), so any Fetch.* command is a defect."""
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard()
            await guard.start(chrome.url)
            await _settle(chrome, "Runtime.runIfWaitingForDebugger", "frame-1")
            await guard.close()
        assert [m for _, m, _ in chrome.log if m.startswith("Fetch.")] == []
        assert len([m for _, m, _ in chrome.log if m == "Network.setBlockedURLs"]) == 2

    async def test_the_network_events_enable_streams_are_dropped(self):
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard()
            await guard.start(chrome.url)
            await _settle(chrome, "Runtime.runIfWaitingForDebugger", "frame-1")
            await asyncio.sleep(0.2)
            assert guard in google_rotation_guard._LIVE
            await guard.close()
        # Every command in the whole conversation is one of the four the guard is
        # specified to send; a Network event must never provoke a fifth.
        assert {m for _, m, _ in chrome.log} == {
            "Target.setAutoAttach",
            "Network.enable",
            "Network.setBlockedURLs",
            "Runtime.runIfWaitingForDebugger",
        }

    async def test_a_network_event_frame_is_never_parsed(self, monkeypatch):
        """Network.enable streams every request event of every page to the guard,
        which reads none: a frame Chrome starts with ``{"method":"Network.`` is
        dropped before ``json.loads`` (F-1007's CPU class), so not even a malformed
        one is looked at, while the frame behind it is still handled."""
        warnings: list[str] = []
        monkeypatch.setattr(
            google_rotation_guard.debug_logger,
            "log_warning",
            lambda component, method, message: warnings.append(message),
        )
        handled: list[object] = []
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard()
            real = guard._handle

            async def recording(message):
                handled.append(message.get("method"))
                await real(message)

            monkeypatch.setattr(guard, "_handle", recording)
            await guard.start(chrome.url)
            await chrome.send_raw('{"method":"Network.requestWillBeSent","params":')
            await chrome.send_raw(
                json.dumps(
                    {"method": "Network.loadingFinished", "params": {}},
                    separators=(",", ":"),
                )
            )
            await chrome.send_raw(
                json.dumps(
                    {
                        "method": "Target.attachedToTarget",
                        "params": {
                            "sessionId": "late",
                            "targetInfo": {"type": "page"},
                            "waitingForDebugger": True,
                        },
                    },
                    separators=(",", ":"),
                )
            )
            await _settle(chrome, "Runtime.runIfWaitingForDebugger", "late")
            await guard.close()
        assert [m for m in handled if str(m).startswith("Network.")] == []
        assert "Target.attachedToTarget" in handled
        assert warnings == [], "a skipped frame must not even be a malformed one"

    async def test_the_default_patterns_are_the_ones_installed(self):
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard()
            await guard.start(chrome.url)
            await _settle(chrome, "Network.setBlockedURLs", "page-1")
            await guard.close()
        urls = next(p for s, m, p in chrome.log if m == "Network.setBlockedURLs")
        assert urls == {"urls": list(google_rotation_guard.BLOCKED_URL_PATTERNS)}

    async def test_a_dropped_connection_ends_the_guard(self):
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard()
            await guard.start(chrome.url)
            assert guard in google_rotation_guard._LIVE
        for _ in range(100):
            if guard not in google_rotation_guard._LIVE:
                break
            await asyncio.sleep(0.02)
        assert guard not in google_rotation_guard._LIVE


_SESSION_GONE = {"code": -32001, "message": "Session with given id not found."}


class TestARefusedBlock:
    """F-957 (F-999: now ``Network.enable`` and ``Network.setBlockedURLs``) — a
    block Chrome refuses warns ONLY for a target that is still alive. Measured on Chrome: a session closed before our command arrives
    is torn down with ``Target.detachedFromTarget`` FIRST, then the command is
    answered ``{"code": -32001, "message": "Session with given id not found."}``
    with no ``sessionId`` on the reply. That is a closed tab, not an unguarded one."""

    @pytest.fixture
    def warnings(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(
            google_rotation_guard.debug_logger,
            "log_warning",
            lambda component, method, message: seen.append(message),
        )
        return seen

    @staticmethod
    async def _attach(guard, session, method):
        """The id of *method*'s command for the session Chrome just attached."""
        sent: list[dict] = []

        class Wire:
            async def send(self, text):
                sent.append(json.loads(text))

        guard._ws = Wire()
        await guard._handle(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": session,
                    "targetInfo": {"type": "page"},
                    "waitingForDebugger": False,
                },
            }
        )
        return next(m["id"] for m in sent if m["method"] == method)

    @pytest.fixture(params=["Network.enable", "Network.setBlockedURLs"])
    def method(self, request):
        return request.param

    async def test_a_target_that_detached_first_does_not_warn(self, warnings, method):
        guard = google_rotation_guard.RotationGuard()
        refused = await self._attach(guard, "tab-1", method)
        await guard._handle(
            {"method": "Target.detachedFromTarget", "params": {"sessionId": "tab-1"}}
        )
        await guard._handle({"id": refused, "error": _SESSION_GONE})
        assert warnings == []
        assert guard._block_ids == set()

    async def test_a_target_still_attached_warns(self, warnings, method):
        guard = google_rotation_guard.RotationGuard()
        refused = await self._attach(guard, "tab-1", method)
        await guard._handle(
            {"id": refused, "sessionId": "tab-1", "error": {"code": -32000}}
        )
        assert warnings == [
            "Chrome refused the URL block on a target; it is not guarded"
        ]

    async def test_an_answered_command_does_not_warn(self, warnings, method):
        guard = google_rotation_guard.RotationGuard()
        answered = await self._attach(guard, "tab-1", method)
        await guard._handle({"id": answered, "sessionId": "tab-1", "result": {}})
        assert warnings == []
        assert guard._block_ids != set(), "the other block command is still pending"

    async def test_one_detached_target_does_not_hide_a_live_one(self, warnings, method):
        guard = google_rotation_guard.RotationGuard()
        gone = await self._attach(guard, "tab-1", method)
        live = await self._attach(guard, "tab-2", method)
        await guard._handle(
            {"method": "Target.detachedFromTarget", "params": {"sessionId": "tab-1"}}
        )
        await guard._handle({"id": gone, "error": _SESSION_GONE})
        await guard._handle({"id": live, "error": {"code": -32000}})
        assert len(warnings) == 1

    async def test_a_refused_auto_attach_is_not_a_block_refusal(self, warnings):
        """Only the two commands that block are accounted; a refused
        ``setAutoAttach`` is a different (unreported) failure."""
        guard = google_rotation_guard.RotationGuard()
        attach = await self._attach(guard, "tab-1", "Target.setAutoAttach")
        await guard._handle({"id": attach, "sessionId": "tab-1", "error": {}})
        assert warnings == []


class TestAMalformedFrame:
    async def test_a_reader_failure_closes_the_socket(self, monkeypatch):
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard()

            async def boom(message):
                raise RuntimeError("unexpected")

            monkeypatch.setattr(guard, "_handle", boom)
            await guard.start(chrome.url)
            for _ in range(100):
                if guard not in google_rotation_guard._LIVE:
                    break
                await asyncio.sleep(0.02)
            assert guard not in google_rotation_guard._LIVE
            await asyncio.wait_for(guard._ws.wait_closed(), 5)
            assert guard._ws.close_code is not None, "the socket must be closed"

    async def test_a_malformed_frame_does_not_end_the_reader(self):
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard()
            await guard.start(chrome.url)
            await chrome.send_raw("not json")
            await chrome.send_raw(
                json.dumps(
                    {"method": "Network.requestWillBeSent", "sessionId": "page-1"}
                )
            )
            await chrome.send_raw(
                json.dumps(
                    {
                        "method": "Target.attachedToTarget",
                        "params": {
                            "sessionId": "late",
                            "targetInfo": {"type": "page"},
                            "waitingForDebugger": True,
                        },
                    }
                )
            )
            await _settle(chrome, "Runtime.runIfWaitingForDebugger", "late")
            assert guard in google_rotation_guard._LIVE
            await guard.close()


class TestArm:
    async def test_arm_never_raises(self):
        browser = SimpleNamespace(websocket_url="ws://127.0.0.1:9/devtools/browser/x")
        assert await google_rotation_guard.arm(browser) is None

    async def test_arm_returns_a_live_guard(self):
        async with ScriptedChrome() as chrome:
            guard = await google_rotation_guard.arm(
                SimpleNamespace(websocket_url=chrome.url)
            )
            assert guard is not None
            await guard.close()


class TestTheSpawnArmsClonesOnly:
    @pytest.fixture
    def armed(self, monkeypatch):
        calls = []

        async def fake_arm(browser, *args):
            calls.append(browser)

        monkeypatch.setattr(google_rotation_guard, "arm", fake_arm)
        return calls

    async def test_a_clone_is_armed(self, armed, monkeypatch, tmp_path):
        options = BrowserOptions(user_data_dir=str(tmp_path), auto_clone=True)
        browser = await self._post_launch_with(options, monkeypatch)
        assert armed == [browser]

    async def test_a_master_or_named_session_is_not(self, armed, monkeypatch, tmp_path):
        options = BrowserOptions(user_data_dir=str(tmp_path), auto_clone=False)
        await self._post_launch_with(options, monkeypatch)
        assert armed == []

    async def test_the_opt_out_disarms_a_clone(self, armed, monkeypatch, tmp_path):
        monkeypatch.setenv("STEALTH_MCP_ALLOW_CLONE_GOOGLE_ROTATION", "1")
        get_settings.cache_clear()
        try:
            options = BrowserOptions(user_data_dir=str(tmp_path), auto_clone=True)
            await self._post_launch_with(options, monkeypatch)
            assert armed == []
        finally:
            monkeypatch.undo()
            get_settings.cache_clear()

    async def _post_launch_with(self, options, monkeypatch):
        import stealth_chrome_devtools_mcp.embedded.browser_manager as bm

        async def no_op(*args, **kwargs):
            return None

        async def measure(*args, **kwargs):
            return SimpleNamespace()

        monkeypatch.setattr(bm, "reconcile_launched_browser_version", no_op)
        monkeypatch.setattr(
            bm, "window_sizing", SimpleNamespace(apply_and_measure=measure)
        )
        manager = BrowserManager()
        monkeypatch.setattr(manager, "_apply_tab_overrides", no_op)
        monkeypatch.setattr(bm.login_persistence, "ensure_session_restore", no_op)
        monkeypatch.setattr(
            bm.process_cleanup, "track_browser_process", lambda *a, **k: None
        )
        browser = FakeBrowser(alive=None)  # F-1012 listens on it
        browser.config = None
        await manager._apply_post_launch(
            browser, SimpleNamespace(), options, "iid", options.user_data_dir, True, "c"
        )
        return browser
