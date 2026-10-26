"""T-8944: a command that failed because its page target is gone is not a fault.

Sentry -37, -BK, -BM, -BY (and -DW on the hook path) read ``InvalidStatus: server
rejected WebSocket connection: HTTP 500``, but every one of those events is from
a 2.1.27 / 2.1.39 backend, i.e. nodriver 0.47, where a tab opened its OWN page
websocket and Chrome answered the handshake for a closed page with HTTP 500. On
nodriver 0.50 a ``Tab`` takes its parent's ``websocket_url`` (the BROWSER
endpoint) and addresses its page through a flattened session, so a closed page
answers ``-32001 Session with given id not found`` (a command on the live
session) or ``No target with given id`` (a re-attach), and never a 500. So the
three tab-send sites (``execute_cdp_command``, ``enable_runtime``,
``setup_interception``) report through ``tab_close.report_gone_aware``: a WARNING
only when the error says the target is gone AND Chrome's own target listing no
longer has it (a ``-32001`` on a target that is still listed stays an ERROR).

``_replace_main_tab`` no longer lets a failed close of the previous tab fail a
navigation.

The CH half: ``schtasks`` writes the OEM code page, and a strict text decode
killed ``subprocess``'s reader thread (``UnicodeDecodeError`` in ``_readerthread``).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest
from nodriver import cdp
from nodriver.core.browser import Browser as NodriverBrowser
from nodriver.core.connection import ProtocolException
from websockets.datastructures import Headers
from websockets.exceptions import InvalidMessage, InvalidStatus
from websockets.http11 import Response

from fakes import (
    FakeAttachedTab,
    FakeBrowser,
    FakeDiscoveredTarget,
    FakeTab,
    fake_instance,
    fake_target,
)
from stealth_chrome_devtools_mcp.embedded import desktop_launch, tab_close, tab_open
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import (
    CDPFunctionExecutor,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import DebugLogger
from stealth_chrome_devtools_mcp.embedded.network_interceptor import NetworkInterceptor


def _status(code):
    return InvalidStatus(Response(code, "x", Headers(), b""))


# What Chrome sends for a closed page, as nodriver 0.50 raises it.
SESSION_GONE = ProtocolException(
    {"code": -32001, "message": "Session with given id not found"}
)
NO_TARGET = ProtocolException(
    {"code": -32602, "message": "No target with given id found"}
)
# Errors that must stay ERRORs wherever the target is.
OTHER = {
    "http-500": _status(500),
    "http-403": _status(403),
    "refused": ConnectionRefusedError(1225, "refused"),
    "timeout": TimeoutError("timed out"),
    "invalid-message": InvalidMessage("did not receive a valid HTTP response"),
    "invalid-parameters": ProtocolException(
        {"code": -32602, "message": "Invalid parameters"}
    ),
    "plain": RuntimeError("Session with given id not found"),
}
TARGET_ID = "T-closed"


@pytest.fixture
def backend_log(caplog):
    caplog.set_level(logging.DEBUG, logger="stealth.backend")
    return caplog


def _levels(caplog, level):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "stealth.backend" and r.levelno == level
    ]


def _info(target_id):
    return cdp.target.TargetInfo(
        target_id=cdp.target.TargetID(target_id),
        type_="page",
        title="t",
        url="https://x.test/",
        attached=False,
        can_access_opener=False,
    )


class ChromeBrowser(FakeBrowser):
    """The browser connection of a Chrome that lists ``listed`` target ids.

    ``update_targets`` is nodriver's OWN (``Browser.update_targets``), run over
    a stand-in that answers ``Target.getTargets`` from ``listed``, so what a
    vanished target does to ``tabs`` is the library's rule, not ours.
    """

    def __init__(self, *known, listed=(), list_error=None, stall=False):
        super().__init__()
        self._stall = stall
        self._stand_in = SimpleNamespace(
            _targets=[FakeAttachedTab(_info(t)) for t in known],
            listed=[_info(t) for t in listed],
        )

        async def send(_command):
            if list_error is not None:
                raise list_error
            return self._stand_in.listed

        self._stand_in.send = send
        self.tabs = list(self._stand_in._targets)

    async def update_targets(self) -> None:
        self.update_targets_calls += 1
        if self._stall:
            await asyncio.Event().wait()
        await NodriverBrowser.update_targets(self._stand_in)
        self.tabs = list(self._stand_in._targets)


class FailingTab(FakeTab):
    """A tab on ``browser`` whose commands all raise ``error``."""

    def __init__(self, error, browser=None, target_id=TARGET_ID):
        super().__init__(target_id=target_id)
        self._error = error
        self.parent = browser

    async def send(self, cdp_obj, *args, **kwargs):
        await super().send(cdp_obj, *args, **kwargs)
        raise self._error


def _closed_tab(error):
    """A tab whose page Chrome has closed: the browser no longer lists it."""
    return FailingTab(error, ChromeBrowser(TARGET_ID, "T-other", listed=["T-other"]))


def _live_tab(error):
    """The same error on a target Chrome still lists."""
    return FailingTab(
        error, ChromeBrowser(TARGET_ID, "T-other", listed=[TARGET_ID, "T-other"])
    )


class TestTargetUnlisted:
    async def test_a_target_the_listing_dropped_is_unlisted(self):
        assert await tab_close.target_unlisted(_closed_tab(SESSION_GONE))

    async def test_a_target_the_listing_has_is_not(self):
        assert not await tab_close.target_unlisted(_live_tab(SESSION_GONE))

    async def test_a_tab_with_no_browser_is_not_shown_to_be_gone(self):
        assert not await tab_close.target_unlisted(FailingTab(SESSION_GONE))

    async def test_a_listing_that_fails_is_not_shown_to_be_gone(self):
        browser = ChromeBrowser(TARGET_ID, listed=[], list_error=RuntimeError("down"))

        assert not await tab_close.target_unlisted(FailingTab(SESSION_GONE, browser))

    async def test_a_listing_that_never_answers_is_not_shown_to_be_gone(
        self, monkeypatch
    ):
        monkeypatch.setattr(tab_close, "_LISTING_TIMEOUT", 0.05)
        browser = ChromeBrowser(TARGET_ID, listed=[], stall=True)

        # The outer bound keeps a regression RED instead of hanging the lane.
        unlisted = await asyncio.wait_for(
            tab_close.target_unlisted(FailingTab(SESSION_GONE, browser)), 2
        )

        assert unlisted is False

    async def test_a_browser_this_backend_closed_needs_no_listing(self):
        tab = _live_tab(SESSION_GONE)  # Chrome would still list it
        tab_open.mark_closed(tab.parent)

        assert await tab_close.target_unlisted(tab) is True
        assert tab.parent.update_targets_calls == 0

    async def test_a_target_that_is_not_a_page_is_not_judged(self):
        tab = _closed_tab(SESSION_GONE)
        tab.target.type_ = "iframe"

        assert not await tab_close.target_unlisted(tab)


class TestReportGoneAware:
    @pytest.mark.parametrize(
        "error", [SESSION_GONE, NO_TARGET], ids=["session-gone", "no-target"]
    )
    async def test_a_gone_error_on_an_unlisted_target_is_a_warning(
        self, error, backend_log
    ):
        await tab_close.report_gone_aware("comp", "meth", error, _closed_tab(error))

        assert _levels(backend_log, logging.ERROR) == []
        (warned,) = _levels(backend_log, logging.WARNING)
        assert "ProtocolException" in warned

    @pytest.mark.parametrize(
        "error", [SESSION_GONE, NO_TARGET], ids=["session-gone", "no-target"]
    )
    async def test_the_same_error_on_a_listed_target_stays_an_error(
        self, error, backend_log
    ):
        await tab_close.report_gone_aware("comp", "meth", error, _live_tab(error))

        assert len(_levels(backend_log, logging.ERROR)) == 1
        assert _levels(backend_log, logging.WARNING) == []

    @pytest.mark.parametrize("error", OTHER.values(), ids=OTHER.keys())
    async def test_any_other_error_stays_an_error_even_on_an_unlisted_target(
        self, error, backend_log
    ):
        await tab_close.report_gone_aware("comp", "meth", error, _closed_tab(error))

        assert len(_levels(backend_log, logging.ERROR)) == 1
        assert _levels(backend_log, logging.WARNING) == []

    async def test_a_stalled_listing_keeps_the_error(self, backend_log, monkeypatch):
        monkeypatch.setattr(tab_close, "_LISTING_TIMEOUT", 0.05)
        browser = ChromeBrowser(TARGET_ID, listed=[], stall=True)

        await asyncio.wait_for(
            tab_close.report_gone_aware(
                "comp", "meth", SESSION_GONE, FailingTab(SESSION_GONE, browser)
            ),
            2,
        )

        assert len(_levels(backend_log, logging.ERROR)) == 1
        assert _levels(backend_log, logging.WARNING) == []

    async def test_a_command_that_raced_close_instance_is_a_warning(self, backend_log):
        tab = _live_tab(SESSION_GONE)
        tab_open.mark_closed(tab.parent)

        await tab_close.report_gone_aware("comp", "meth", SESSION_GONE, tab)

        assert _levels(backend_log, logging.ERROR) == []
        assert len(_levels(backend_log, logging.WARNING)) == 1
        assert tab.parent.update_targets_calls == 0

    async def test_the_warning_names_the_target(self, backend_log):
        long_id = "0123456789ABCDEF0123456789ABCDEF"
        tab = FailingTab(
            SESSION_GONE, ChromeBrowser(long_id, "T-other", listed=["T-other"]), long_id
        )

        await tab_close.report_gone_aware("comp", "meth", SESSION_GONE, tab)

        (warned,) = _levels(backend_log, logging.WARNING)
        assert "target 01234567 is gone" in warned
        assert long_id not in warned
        assert "Session with given id not found" in warned

    async def test_a_listing_failure_keeps_the_error(self, backend_log):
        browser = ChromeBrowser(TARGET_ID, list_error=RuntimeError("down"))

        await tab_close.report_gone_aware(
            "comp", "meth", SESSION_GONE, FailingTab(SESSION_GONE, browser)
        )

        assert len(_levels(backend_log, logging.ERROR)) == 1


class TestLogErrorStaysBlind:
    """``log_error`` itself decides nothing about gone targets (``browser_health``'s
    relaunch and heal paths log through it, and a 500 there is a fault)."""

    @pytest.mark.parametrize(
        "error", [SESSION_GONE, _status(500)], ids=["session-gone", "http-500"]
    )
    def test_it_files_an_error(self, error, backend_log):
        log = DebugLogger()

        log.log_error("comp", "meth", error)

        assert len(_levels(backend_log, logging.ERROR)) == 1
        assert _levels(backend_log, logging.WARNING) == []
        assert log.get_debug_view()["summary"]["total_errors"] == 1


class TestTheSentrySites:
    """The three tab-send sites, through the real call sites, on the error a closed
    page really gives on nodriver 0.50."""

    async def test_enable_runtime_returns_false_without_an_error_record(
        self, backend_log
    ):
        tab = _closed_tab(SESSION_GONE)

        assert await CDPFunctionExecutor().enable_runtime(tab) is False

        assert _levels(backend_log, logging.ERROR) == []
        assert len(_levels(backend_log, logging.WARNING)) == 1

    async def test_execute_cdp_command_still_answers_the_failure(self, backend_log):
        answer = await CDPFunctionExecutor().execute_cdp_command(
            _closed_tab(SESSION_GONE), "evaluate", {"expression": "1"}
        )

        assert answer["success"] is False
        assert "Session with given id not found" in answer["error"]
        assert _levels(backend_log, logging.ERROR) == []
        # enable_runtime and the command itself each fail on the closed page.
        assert len(_levels(backend_log, logging.WARNING)) == 2

    async def test_setup_interception_still_raises_but_logs_a_warning(
        self, backend_log
    ):
        with pytest.raises(Exception, match="Session with given id not found"):
            await NetworkInterceptor().setup_interception(
                _closed_tab(SESSION_GONE), "i1"
            )

        assert _levels(backend_log, logging.ERROR) == []
        assert len(_levels(backend_log, logging.WARNING)) == 1

    async def test_a_session_error_on_a_listed_target_is_still_an_error(
        self, backend_log
    ):
        answer = await CDPFunctionExecutor().execute_cdp_command(
            _live_tab(SESSION_GONE), "evaluate", {"expression": "1"}
        )

        assert answer["success"] is False
        assert len(_levels(backend_log, logging.ERROR)) == 2
        assert _levels(backend_log, logging.WARNING) == []

    async def test_a_real_failure_at_the_same_site_is_still_an_error(self, backend_log):
        with pytest.raises(Exception, match="boom"):
            await NetworkInterceptor().setup_interception(
                _closed_tab(RuntimeError("boom")), "i1"
            )

        assert len(_levels(backend_log, logging.ERROR)) == 1


@pytest.fixture
def manager_and_tabs():
    tracked = FakeAttachedTab(fake_target(target_id="T-main", url="https://x.test/"))
    browser = FakeBrowser(tabs=[FakeDiscoveredTarget(fake_target(target_id="T-main"))])
    manager = BrowserManager()
    manager._instances["i1"] = {
        "browser": browser,
        "tab": tracked,
        "instance": fake_instance("i1"),
        "navigation_count": 0,
    }
    return manager, tracked


class TestReplacingADeadMainTab:
    """-37: ``navigate`` failed because closing the previous, already-gone tab
    raised out of ``_replace_main_tab`` (on nodriver 0.50 a closed page answers the
    close ``-32001``)."""

    @staticmethod
    def _closing_raises(tab, error):
        async def close():
            raise error

        tab.close = close

    async def test_a_gone_previous_tab_does_not_fail_the_navigation(
        self, manager_and_tabs, backend_log
    ):
        manager, tracked = manager_and_tabs
        self._closing_raises(tracked, SESSION_GONE)

        new_tab = await manager._replace_main_tab("i1", reason="test")

        assert manager._instances["i1"]["tab"] is new_tab
        assert new_tab is not tracked
        assert _levels(backend_log, logging.ERROR) == []
        (warned,) = _levels(backend_log, logging.WARNING)
        assert "Session with given id not found" in warned

    async def test_a_target_chrome_already_dropped_is_fine_too(
        self, manager_and_tabs, backend_log
    ):
        manager, tracked = manager_and_tabs
        self._closing_raises(
            tracked,
            ProtocolException({"code": -32602, "message": "No target with given id"}),
        )

        new_tab = await manager._replace_main_tab("i1", reason="test")

        assert manager._instances["i1"]["tab"] is new_tab
        assert _levels(backend_log, logging.WARNING) == []

    async def test_the_failure_is_still_logged_when_it_is_not_a_gone_target(
        self, manager_and_tabs, backend_log
    ):
        manager, tracked = manager_and_tabs
        self._closing_raises(tracked, RuntimeError("close broke"))

        await manager._replace_main_tab("i1", reason="test")

        (warned,) = _levels(backend_log, logging.WARNING)
        assert "close broke" in warned


class TestSchtasksOutputIsDecodedTolerantly:
    """-CH: bytes that are neither UTF-8 nor the ANSI code page used to kill the
    reader thread, which left the output empty and the error unhandled.

    Two pins, because no single test can hold both on every platform: the
    arguments handed to ``subprocess.run`` (the OEM code page decodes all 256
    bytes, so on Windows the behaviour alone cannot tell ``errors="replace"``
    from its absence) and, through a real child, that the output survives."""

    SCRIPT = (
        "import sys;"
        "sys.stdout.buffer.write(b'out:\\x81\\x8d\\x90\\x9d\\xff:end');"
        "sys.stderr.buffer.write(b'err:\\x94\\x81\\xff:end')"
    )

    @pytest.mark.parametrize(
        ("platform", "encoding"),
        [("win32", "oem"), ("linux", "utf-8"), ("darwin", "utf-8")],
    )
    def test_it_asks_for_a_tolerant_decode(self, monkeypatch, platform, encoding):
        seen = {}

        def run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(desktop_launch.subprocess, "run", run)
        monkeypatch.setattr(desktop_launch.sys, "platform", platform)

        desktop_launch._schtasks(["/Query"])

        assert seen["encoding"] == encoding
        assert seen["errors"] == "replace"
        assert seen["text"] is True

    def test_undecodable_output_is_replaced_not_fatal(self, monkeypatch):
        monkeypatch.setattr(
            desktop_launch, "_system_binary", lambda _name: sys.executable
        )
        raised = []
        monkeypatch.setattr(
            "threading.excepthook", lambda args: raised.append(args.exc_value)
        )

        done = desktop_launch._schtasks(["-c", self.SCRIPT])

        assert raised == []
        assert done.returncode == 0
        assert done.stdout.startswith("out:") and done.stdout.endswith(":end")
        assert done.stderr.startswith("err:") and done.stderr.endswith(":end")
