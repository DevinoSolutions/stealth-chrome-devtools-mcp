"""F-939 — a clone seeded while the master runs gets the master's LIVE cookies.

2.1.20 handed a live jar over only when THIS backend drove the master. The
master is usually another backend's, so every clone fell back to the
``master-snapshot`` (hours stale, every ``__Secure-*PSIDTS`` already rotated away
by the master). The shared session's browser now has a second door: the loopback
``--remote-debugging-port`` on its own command line. Pinned here without Chrome:
the port discovery and its join to the profile, the opt-out, the resolver
carrying the port, the raw jar read and its translation, and the fallbacks.
The behavior against a real Chrome is ``tests/test_e2e_google_rotation_guard.py``.
"""

import json
from pathlib import Path

import pytest
import websockets.asyncio.server

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    clone_storage,
    cookie_handoff,
    profile_lock,
    profile_target,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings

MASTER = Path("C:/m/master")


@pytest.fixture
def argv(monkeypatch):
    def _set(cmdline):
        monkeypatch.setattr(browser_cmdline, "arguments", lambda pid: list(cmdline))

    return _set


class TestLiveMasterPort:
    def test_the_port_of_the_holder_on_that_profile(self, argv):
        argv([f"--user-data-dir={MASTER}", "--remote-debugging-port=51234"])
        hold = profile_lock.Hold(10, "held")
        assert profile_target.live_master_port(MASTER, hold) == 51234

    def test_a_process_on_another_profile_is_no_source(self, argv):
        argv(["--user-data-dir=C:/other", "--remote-debugging-port=51234"])
        hold = profile_lock.Hold(10, "h")
        assert profile_target.live_master_port(MASTER, hold) is None

    def test_a_holder_without_a_port_or_a_pid_is_none(self, argv):
        argv([f"--user-data-dir={MASTER}"])
        assert (
            profile_target.live_master_port(MASTER, profile_lock.Hold(10, "h")) is None
        )
        assert (
            profile_target.live_master_port(MASTER, profile_lock.Hold(None, "h"))
            is None
        )

    def test_the_opt_out(self, argv, monkeypatch):
        argv([f"--user-data-dir={MASTER}", "--remote-debugging-port=51234"])
        monkeypatch.setenv("STEALTH_MCP_NO_LIVE_MASTER_SEED", "1")
        get_settings.cache_clear()
        try:
            hold = profile_lock.Hold(10, "h")
            assert profile_target.live_master_port(MASTER, hold) is None
        finally:
            monkeypatch.undo()
            get_settings.cache_clear()


def _hold_master(monkeypatch):
    master = clone_storage.master_profile_dir()
    monkeypatch.setattr(
        clone_storage,
        "_profile_hold",
        lambda d: profile_lock.Hold(10, "held") if Path(d) == master else None,
    )
    return master


class TestTheResolver:
    async def test_a_held_master_we_do_not_drive_with_a_port_seeds_live(
        self, tmp_session_root, monkeypatch
    ):
        master = _hold_master(monkeypatch)
        monkeypatch.setattr(profile_target, "live_master_port", lambda m, h: 4242)
        selection = await clone_storage.resolve_profile_selection(None)
        assert selection["profile_role"] == "clone"
        assert selection[clone_storage.LIVE_SEED_KEY] == str(master)
        assert selection[profile_target.LIVE_PORT_KEY] == 4242
        public = clone_storage._public_profile_selection(selection)
        assert profile_target.LIVE_PORT_KEY not in public
        assert clone_storage.LIVE_SEED_KEY not in public

    async def test_without_a_port_it_still_refuses(self, tmp_session_root, monkeypatch):
        _hold_master(monkeypatch)
        monkeypatch.setattr(profile_target, "live_master_port", lambda m, h: None)
        with pytest.raises(ToolError, match="does not drive"):
            await clone_storage.resolve_profile_selection(None)

    async def test_a_master_we_drive_never_carries_a_port(
        self, tmp_session_root, monkeypatch
    ):
        master = _hold_master(monkeypatch)
        selection = await clone_storage.resolve_profile_selection(
            None, driven=lambda p: Path(p) == master
        )
        assert profile_target.LIVE_PORT_KEY not in selection
        assert selection[clone_storage.LIVE_SEED_KEY] == str(master)

    async def test_a_free_master_is_opened_and_no_clone_is_made(self, tmp_session_root):
        selection = await clone_storage.resolve_profile_selection(None)
        assert selection["profile_role"] == "default"
        assert profile_target.LIVE_PORT_KEY not in selection


WIRE_JAR = [
    {
        "name": "SID",
        "value": "v1",
        "domain": ".google.com",
        "path": "/",
        "expires": 1900000000.5,
        "size": 5,
        "httpOnly": True,
        "secure": True,
        "session": False,
        "sameSite": "None",
        "priority": "High",
        "sourceScheme": "Secure",
        "sourcePort": 443,
    },
    {
        "name": "sess",
        "value": "v2",
        "domain": "example.com",
        "path": "/",
        "expires": -1,
        "size": 5,
        "httpOnly": False,
        "secure": False,
        "session": True,
        "priority": "Medium",
        "sourceScheme": "Secure",
        "sourcePort": 443,
    },
]


class FakeDebugPort:
    """A browser-level CDP endpoint answering ``Storage.getCookies``."""

    def __init__(self, reply):
        self.reply = reply
        self.methods = []

    async def _handle(self, ws):
        async for raw in ws:
            message = json.loads(raw)
            self.methods.append(message["method"])
            await ws.send(json.dumps({"id": message["id"], **self.reply}))

    async def __aenter__(self):
        self.server = await websockets.asyncio.server.serve(
            self._handle, "127.0.0.1", 0
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
def ws_by_port(monkeypatch):
    monkeypatch.setattr(
        cookie_handoff,
        "_port_ws_url",
        lambda port: f"ws://127.0.0.1:{port}/devtools/browser/x",
    )


class TestRawJarOverAPort:
    async def test_it_reads_one_command_and_nothing_else(self, ws_by_port):
        async with FakeDebugPort({"result": {"cookies": WIRE_JAR}}) as chrome:
            jar = await cookie_handoff._read_raw_jar_over_port(chrome.port)
        assert jar == WIRE_JAR
        assert chrome.methods == ["Storage.getCookies"]

    async def test_a_refusal_is_a_shape_only_error(self, ws_by_port):
        async with FakeDebugPort({"error": {"message": "SECRET-VALUE"}}) as chrome:
            with pytest.raises(cookie_handoff.HandoffError) as excinfo:
                await cookie_handoff._read_raw_jar_over_port(chrome.port)
        assert "SECRET-VALUE" not in str(excinfo.value)

    def test_the_translation_keeps_a_session_cookie_a_session_cookie(self):
        persistent, session = (p.to_json() for p in cookie_handoff.raw_params(WIRE_JAR))
        assert persistent["expires"] == 1900000000.5
        assert "expires" not in session
        for wire in (persistent, session):
            assert "sameParty" not in wire
            assert "size" not in wire
        assert persistent["name"] == "SID"
        assert persistent["httpOnly"] is True

    def test_one_malformed_cookie_is_skipped_not_fatal(self):
        bad = {"name": "x", "value": "y", "domain": "d", "path": "/", "sameSite": 7}
        params = cookie_handoff.raw_params([bad, *WIRE_JAR])
        assert [p.name for p in params] == ["SID", "sess"]


class TestSeedingOverThePort:
    async def test_a_port_in_the_selection_reads_the_jar_over_it(self, monkeypatch):
        from types import SimpleNamespace

        from stealth_chrome_devtools_mcp.embedded.tool_sections import (
            browser_management,
        )

        calls = []

        async def hand_off_from_port(port, target):
            calls.append((port, target))
            return cookie_handoff.Handoff(1, 1, 1, 0, 0)

        async def driven_profiles(manager):
            return SimpleNamespace(instance=lambda path: None)

        async def get_browser(instance_id):
            return "TARGET"

        rt = browser_management.rt
        monkeypatch.setattr(rt.cookie_handoff, "hand_off_from_port", hand_off_from_port)
        monkeypatch.setattr(rt.cookie_handoff, "driven_profiles", driven_profiles)
        monkeypatch.setattr(rt.browser_manager, "get_browser", get_browser)
        selection = {
            clone_storage.LIVE_SEED_KEY: str(MASTER),
            profile_target.LIVE_PORT_KEY: 4242,
        }
        await browser_management._seed_cookies_over_cdp(
            selection, SimpleNamespace(instance_id="iid")
        )
        assert calls == [(4242, "TARGET")]
