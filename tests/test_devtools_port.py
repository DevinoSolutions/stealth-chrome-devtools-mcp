"""F-1009 — the pure helpers behind the port-0 launch (``devtools_port``).

The end-to-end pins live in ``test_devtools_port_identity.py`` (over nodriver's
real ``Browser.start``) and ``test_desktop_launch.py`` (the delegated door); these
are the contracts the two share, each stated once.
"""

from __future__ import annotations

import pytest

from stealth_chrome_devtools_mcp.embedded import browser_cmdline, devtools_port
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

OURS = devtools_port.ActivePort(45000, "/devtools/browser/ours")


class TestWithPortZero:
    def test_replaces_any_port_the_args_already_carry(self):
        args = ["--a", "--remote-debugging-port=9222", "--b"]
        assert devtools_port.with_port_zero(args) == [
            "--a",
            "--b",
            "--remote-debugging-port=0",
        ]

    def test_adds_the_flag_when_there_is_none(self):
        assert devtools_port.with_port_zero(["--a"]) == [
            "--a",
            "--remote-debugging-port=0",
        ]


class TestRead:
    def test_reads_port_and_browser_path(self, tmp_path):
        (tmp_path / devtools_port.PORT_FILE).write_text("45000\n/devtools/browser/x\n")
        assert devtools_port.read(tmp_path) == (45000, "/devtools/browser/x")

    def test_a_port_with_no_path_is_still_a_port_but_not_an_own_endpoint(
        self, tmp_path
    ):
        (tmp_path / devtools_port.PORT_FILE).write_text("45000\n")
        assert devtools_port.read(tmp_path) == (45000, None)
        assert devtools_port.own_endpoint(tmp_path) is None

    @pytest.mark.parametrize("text", ["", "0\n/p", "70000\n/p", "abc\n/p"])
    def test_an_unusable_port_is_none(self, tmp_path, text):
        (tmp_path / devtools_port.PORT_FILE).write_text(text)
        assert devtools_port.read(tmp_path) is None

    def test_no_file_is_none(self, tmp_path):
        assert devtools_port.read(tmp_path) is None


class TestVerify:
    def test_the_matching_websocket_passes(self):
        devtools_port.verify("ws://127.0.0.1:45000/devtools/browser/ours", OURS)

    @pytest.mark.parametrize(
        "reported",
        [
            "ws://127.0.0.1:45000/devtools/browser/sibling",
            "ws://127.0.0.1:45001/devtools/browser/ours",
            "",
        ],
    )
    def test_anything_else_is_refused_by_name(self, reported):
        with pytest.raises(ToolError, match="not the browser this spawn launched"):
            devtools_port.verify(reported, OURS)


class TestClearStale:
    def test_removes_a_leftover_file(self, tmp_path):
        stale = tmp_path / devtools_port.PORT_FILE
        stale.write_text("46000\n/devtools/browser/stale\n")
        devtools_port.clear_stale(tmp_path)
        assert not stale.exists()

    def test_a_profile_without_one_is_fine(self, tmp_path):
        devtools_port.clear_stale(tmp_path)

    def test_a_file_that_cannot_be_removed_fails_closed(self, tmp_path, monkeypatch):
        stale = tmp_path / devtools_port.PORT_FILE
        stale.write_text("46000\n/devtools/browser/stale\n")

        def refuse(self, *a, **k):
            raise PermissionError("held")

        monkeypatch.setattr(type(stale), "unlink", refuse)
        with pytest.raises(ToolError, match="F-1009"):
            devtools_port.clear_stale(tmp_path)


class TestDebugPortFindsAPortZeroBrowser:
    """A browser launched with ``=0`` carries no port on its command line, so the
    live-master door would see none; the file in ITS profile is where it is."""

    def _argv(self, monkeypatch, profile, port):
        monkeypatch.setattr(
            browser_cmdline,
            "arguments",
            lambda pid: [
                "chrome",
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}",
            ],
        )

    def test_falls_back_to_the_profiles_file_for_port_zero(self, tmp_path, monkeypatch):
        (tmp_path / devtools_port.PORT_FILE).write_text("45000\n/devtools/browser/x\n")
        self._argv(monkeypatch, tmp_path, 0)
        assert browser_cmdline.debug_port(1, str(tmp_path)) == 45000

    def test_no_profile_means_no_file_is_consulted(self, tmp_path, monkeypatch):
        (tmp_path / devtools_port.PORT_FILE).write_text("45000\n/devtools/browser/x\n")
        self._argv(monkeypatch, tmp_path, 0)
        assert browser_cmdline.debug_port(1) is None

    def test_an_explicit_port_never_consults_the_file(self, tmp_path, monkeypatch):
        (tmp_path / devtools_port.PORT_FILE).write_text("45000\n/devtools/browser/x\n")
        self._argv(monkeypatch, tmp_path, 9222)
        assert browser_cmdline.debug_port(1, str(tmp_path)) == 9222
