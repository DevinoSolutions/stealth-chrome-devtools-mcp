"""F-939 — a clone never rotates Google's session cookies.

Pins what is decidable without Chrome: which URLs the patterns cover, the opt-out,
the CDP conversation of the guard against a scripted Chrome (every attached
session is blocked and auto-attached further BEFORE it is resumed, a paused
request is failed, a dropped connection ends the guard), and that a spawn arms
the guard for a clone and for nothing else. The behavior against a real Chrome is
``tests/test_e2e_google_rotation_guard.py``.
"""

import asyncio
import json
from fnmatch import fnmatchcase
from types import SimpleNamespace

import pytest
import websockets.asyncio.server

from stealth_chrome_devtools_mcp.embedded import google_rotation_guard
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.settings import get_settings


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
    """A browser-level CDP endpoint that attaches one page, which has an iframe."""

    def __init__(self):
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

    async def _handle(self, ws):
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
            if message["method"] == "Target.setAutoAttach" and session == "page-1":
                await self._attach(ws, "frame-1", "iframe")
            if message["method"] == "Fetch.enable" and session == "frame-1":
                await ws.send(
                    json.dumps(
                        {
                            "method": "Fetch.requestPaused",
                            "sessionId": "frame-1",
                            "params": {"requestId": "req-9"},
                        }
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
            assert methods[:3] == [
                "Fetch.enable",
                "Target.setAutoAttach",
                "Runtime.runIfWaitingForDebugger",
            ], (session, methods)
        enable = next(p for s, m, p in chrome.log if m == "Fetch.enable")
        assert enable == {
            "patterns": [
                {"urlPattern": "*://x/RotateCookies*", "requestStage": "Request"}
            ]
        }

    async def test_a_paused_request_is_failed_as_blocked(self):
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard()
            await guard.start(chrome.url)
            await _settle(chrome, "Fetch.failRequest", "frame-1")
            await guard.close()
        params = next(p for s, m, p in chrome.log if m == "Fetch.failRequest")
        assert params == {"requestId": "req-9", "errorReason": "BlockedByClient"}

    async def test_the_default_patterns_are_the_ones_installed(self):
        async with ScriptedChrome() as chrome:
            guard = google_rotation_guard.RotationGuard()
            await guard.start(chrome.url)
            await guard.close()
        enable = next(p for s, m, p in chrome.log if m == "Fetch.enable")
        assert [p["urlPattern"] for p in enable["patterns"]] == list(
            google_rotation_guard.BLOCKED_URL_PATTERNS
        )

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
        browser = SimpleNamespace(_process=None, config=None)
        await manager._apply_post_launch(
            browser, SimpleNamespace(), options, "iid", options.user_data_dir, True, "c"
        )
        return browser
