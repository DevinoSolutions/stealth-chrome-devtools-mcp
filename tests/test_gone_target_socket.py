"""T-8944: a websocket Chrome refuses for a page target that is gone is not a fault.

Sentry -37, -BK, -BM, -BY (and -DW on the hook path) all read ``InvalidStatus:
server rejected WebSocket connection: HTTP 500``. nodriver's ``Connection.send``
re-opens a tab's own websocket when it is down; for a closed page target Chrome
answers that handshake with HTTP 500 (measured in F-963). Nothing is wrong with
the command, so ``tab_close.report_gone_aware`` files it as a WARNING at the three
tab-send sites the groups showed (and only there), and ``_replace_main_tab`` no longer lets it fail a navigation.

The CH half: ``schtasks`` writes the OEM code page, and a strict text decode
killed ``subprocess``'s reader thread (``UnicodeDecodeError`` in ``_readerthread``).
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest
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
from stealth_chrome_devtools_mcp.embedded import desktop_launch, tab_close
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import (
    CDPFunctionExecutor,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import DebugLogger
from stealth_chrome_devtools_mcp.embedded.network_interceptor import NetworkInterceptor


def _status(code):
    return InvalidStatus(Response(code, "x", Headers(), b""))


GONE = _status(500)
NOT_GONE = {
    "403": _status(403),
    "404": _status(404),
    "502": _status(502),
    "refused": ConnectionRefusedError(1225, "refused"),
    "timeout": TimeoutError("timed out"),
    "invalid-message": InvalidMessage("did not receive a valid HTTP response"),
    "protocol-500-text": ProtocolException(
        {"code": -32000, "message": "HTTP 500 server rejected"}
    ),
    "plain": RuntimeError("server rejected WebSocket connection: HTTP 500"),
}


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


class FailingTab(FakeTab):
    """A tab whose websocket cannot be re-opened: every command raises."""

    def __init__(self, error):
        super().__init__()
        self._error = error

    async def send(self, cdp_obj, *args, **kwargs):
        await super().send(cdp_obj, *args, **kwargs)
        raise self._error


class TestTargetSocketGone:
    def test_only_http_500_is_a_gone_target(self):
        assert tab_close.target_socket_gone(GONE)

    @pytest.mark.parametrize("error", NOT_GONE.values(), ids=NOT_GONE.keys())
    def test_everything_else_is_not(self, error):
        assert not tab_close.target_socket_gone(error)


class TestReportGoneAware:
    def test_a_gone_target_is_a_warning(self, backend_log):
        tab_close.report_gone_aware("comp", "meth", GONE)

        assert _levels(backend_log, logging.ERROR) == []
        (warned,) = _levels(backend_log, logging.WARNING)
        assert "InvalidStatus" in warned and "HTTP 500" in warned

    @pytest.mark.parametrize("error", NOT_GONE.values(), ids=NOT_GONE.keys())
    def test_any_other_error_is_still_an_error(self, error, backend_log):
        tab_close.report_gone_aware("comp", "meth", error)

        assert len(_levels(backend_log, logging.ERROR)) == 1
        assert _levels(backend_log, logging.WARNING) == []


class TestLogErrorStaysBlind:
    """A websockets response carries no URL, so ``log_error`` cannot tell a closed
    page from a failing browser-level endpoint; it must keep filing a 500 as an
    ERROR (``browser_health``'s relaunch and heal paths rely on it)."""

    def test_a_500_through_log_error_is_an_error(self, backend_log):
        log = DebugLogger()

        log.log_error("comp", "meth", GONE)

        assert len(_levels(backend_log, logging.ERROR)) == 1
        assert _levels(backend_log, logging.WARNING) == []
        assert log.get_debug_view()["summary"]["total_errors"] == 1


class TestTheSentrySites:
    """-BM, -BK and -BY, through the real call sites."""

    async def test_enable_runtime_returns_false_without_an_error_record(
        self, backend_log
    ):
        assert await CDPFunctionExecutor().enable_runtime(FailingTab(GONE)) is False

        assert _levels(backend_log, logging.ERROR) == []
        assert len(_levels(backend_log, logging.WARNING)) == 1

    async def test_execute_cdp_command_still_answers_the_failure(self, backend_log):
        answer = await CDPFunctionExecutor().execute_cdp_command(
            FailingTab(GONE), "evaluate", {"expression": "1"}
        )

        assert answer["success"] is False
        assert "HTTP 500" in answer["error"]
        assert _levels(backend_log, logging.ERROR) == []

    async def test_setup_interception_still_raises_but_logs_a_warning(
        self, backend_log
    ):
        with pytest.raises(Exception, match="HTTP 500"):
            await NetworkInterceptor().setup_interception(FailingTab(GONE), "i1")

        assert _levels(backend_log, logging.ERROR) == []
        assert len(_levels(backend_log, logging.WARNING)) == 1

    async def test_a_real_failure_at_the_same_site_is_still_an_error(self, backend_log):
        with pytest.raises(Exception, match="boom"):
            await NetworkInterceptor().setup_interception(
                FailingTab(RuntimeError("boom")), "i1"
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
    raised out of ``_replace_main_tab``."""

    @staticmethod
    def _closing_raises(tab, error):
        async def close():
            raise error

        tab.close = close

    async def test_a_gone_previous_tab_does_not_fail_the_navigation(
        self, manager_and_tabs, backend_log
    ):
        manager, tracked = manager_and_tabs
        self._closing_raises(tracked, GONE)

        new_tab = await manager._replace_main_tab("i1", reason="test")

        assert manager._instances["i1"]["tab"] is new_tab
        assert new_tab is not tracked
        assert _levels(backend_log, logging.ERROR) == []
        (warned,) = _levels(backend_log, logging.WARNING)
        assert "HTTP 500" in warned

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
