"""F-993 -- the exit code of a browser that exited, as ``process_exit`` reads it.

On 2026-10-15 ``default`` and ``fleet`` exited together twice with no crash dump, no
WER event and no line in Chrome's log, and the backend could only say "browser process
is not running". The exit code is what separates an external ``TerminateProcess`` (1,
or whatever the killer passed) from a crash (``0xC0000005``...) from an init failure
(``0xC0000142``). A browser this backend LAUNCHED keeps it on its asyncio process
object; one it ADOPTED after a restart (F-888) has no such object, so on Windows a
handle held while it ran is what keeps the code readable after it is gone.
"""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from stealth_chrome_devtools_mcp.embedded import process_exit

WINDOWS = sys.platform == "win32"


class TestWhatTheCodeLooksLike:
    @pytest.mark.parametrize(
        ("code", "shown"),
        [
            (0, "exit_code=0"),
            (1, "exit_code=1"),
            (255, "exit_code=255"),
            (3221225794, "exit_code=3221225794 (0xC0000142)"),
            (3221225477, "exit_code=3221225477 (0xC0000005)"),
            (-9, "exit_code=-9 (signal 9)"),
            (-15, "exit_code=-15 (signal 15)"),
        ],
    )
    def test_a_launched_browser_reports_its_return_code(self, code, shown):
        browser = SimpleNamespace(_process=SimpleNamespace(returncode=code))

        assert process_exit.exit_status(browser, None) == shown

    def test_the_process_object_wins_over_a_handle(self):
        browser = SimpleNamespace(_process=SimpleNamespace(returncode=2))

        assert process_exit.exit_status(browser, 99, read=lambda _h: 7) == "exit_code=2"

    def test_a_handle_is_read_when_there_is_no_process_object(self):
        browser = SimpleNamespace(_process=None)

        assert process_exit.exit_status(browser, 99, read=lambda _h: 1) == "exit_code=1"

    def test_a_process_object_that_never_got_its_code_falls_to_the_handle(self):
        browser = SimpleNamespace(_process=SimpleNamespace(returncode=None))

        assert process_exit.exit_status(browser, 99, read=lambda _h: 5) == "exit_code=5"

    def test_nothing_to_read_says_why(self):
        line = process_exit.exit_status(SimpleNamespace(_process=None), None)

        assert line.startswith("exit_code=unavailable (")
        assert "no process object" in line

    def test_a_browser_we_know_nothing_about_is_unavailable_too(self):
        assert process_exit.exit_status(None, None).startswith(
            "exit_code=unavailable ("
        )

    def test_a_handle_whose_process_is_still_active_is_not_an_exit_code(self):
        line = process_exit.exit_status(
            SimpleNamespace(_process=None), 99, read=lambda _h: None
        )

        assert (
            line == "exit_code=unavailable (the process handle reports it still active)"
        )

    def test_a_handle_that_cannot_be_read_is_unavailable_not_an_exception(self):
        def broken(_handle: int) -> int | None:
            raise OSError("invalid handle")

        line = process_exit.exit_status(SimpleNamespace(_process=None), 99, read=broken)

        assert line == "exit_code=unavailable (OSError reading the process handle)"


class TestHoldingAHandle:
    def test_a_pid_that_is_not_there_gives_none(self):
        assert process_exit.hold_exit_handle(2**22 + 7) is None

    def test_releasing_nothing_is_fine(self):
        process_exit.release_exit_handle(0)

    @pytest.mark.skipif(not WINDOWS, reason="a process handle is a Windows idea")
    def test_a_handle_keeps_the_exit_code_readable_after_the_process_is_gone(self):
        child = subprocess.Popen(  # noqa: S603  PERMANENT(F-993: a throwaway child python)
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        handle = None
        try:
            handle = process_exit.hold_exit_handle(child.pid)
            assert isinstance(handle, int) and handle > 0
            # still running: the code is not an answer yet
            assert process_exit.exit_status(None, handle).startswith(
                "exit_code=unavailable"
            )
            child.terminate()  # an external kill, as the 10-15 exits looked
            child.wait(10)

            line = process_exit.exit_status(SimpleNamespace(_process=None), handle)
        finally:
            if handle is not None:
                process_exit.release_exit_handle(handle)
            child.kill()
            child.wait(10)

        assert line == "exit_code=1"  # TerminateProcess(1): what Popen.terminate does

    @pytest.mark.skipif(not WINDOWS, reason="a process handle is a Windows idea")
    def test_a_crash_code_survives_in_full(self):
        child = subprocess.Popen(  # noqa: S603  PERMANENT(F-993: a throwaway child python)
            [sys.executable, "-c", "import os; os._exit(-1073741819)"]
        )
        handle = None
        try:
            handle = process_exit.hold_exit_handle(child.pid)
            child.wait(30)

            line = process_exit.exit_status(None, handle)
        finally:
            if handle is not None:
                process_exit.release_exit_handle(handle)
            child.kill()

        assert line == "exit_code=3221225477 (0xC0000005)"

    @pytest.mark.skipif(WINDOWS, reason="the non-Windows answer")
    def test_there_is_no_handle_to_hold_off_windows(self):
        assert process_exit.hold_exit_handle(1) is None
