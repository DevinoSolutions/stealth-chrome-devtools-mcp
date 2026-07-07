"""Pinning tests for M1-4's CLI wiring: `status`/`doctor` report responsive/
wedged/down via `_format_backend_status` (-> `singleton._probe_backend_
status`) instead of `_find_running_server`'s binary reuse-or-not answer.

`_server()` is mocked out (returns a lightweight stub with just the methods
_cmd_status/_cmd_doctor call) to avoid the real, heavyweight `import server`
side effect (FastMCP tool registration) — the singleton wiring is what these
tests pin, not the rest of the CLI's server-derived fields.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stealth_chrome_devtools_mcp import cli


@pytest.fixture()
def fake_server(tmp_path):
    server = MagicMock()
    server._default_session_root.return_value = Path(tmp_path)
    server._clone_storage_cap_bytes.return_value = 1024**3
    server._session_storage_cap_bytes.return_value = 1024**3
    return server


class TestCliStatusBackendState:
    def test_wedged_backend_prints_unresponsive(self, fake_server, capsys):
        with patch.object(cli, "_server", return_value=fake_server):
            with patch(
                "singleton._probe_backend_status", return_value=("wedged", 19222)
            ):
                cli._cmd_status(None)
        out = capsys.readouterr().out
        assert "UNRESPONSIVE" in out
        assert "wedged" in out
        assert "19222" in out

    def test_responsive_backend_prints_responsive(self, fake_server, capsys):
        with patch.object(cli, "_server", return_value=fake_server):
            with patch(
                "singleton._probe_backend_status", return_value=("responsive", 19222)
            ):
                cli._cmd_status(None)
        out = capsys.readouterr().out
        assert "responsive" in out
        assert "UNRESPONSIVE" not in out
        assert "19222" in out

    def test_no_backend_prints_not_running(self, fake_server, capsys):
        with patch.object(cli, "_server", return_value=fake_server):
            with patch("singleton._probe_backend_status", return_value=("none", None)):
                cli._cmd_status(None)
        out = capsys.readouterr().out
        assert "not running" in out


class TestCliDoctorBackendState:
    def test_wedged_backend_prints_unresponsive(self, fake_server, capsys):
        with patch.object(cli, "_server", return_value=fake_server):
            with patch(
                "singleton._probe_backend_status", return_value=("wedged", 19222)
            ):
                with patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"):
                    cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "UNRESPONSIVE" in out
        assert "wedged" in out

    def test_responsive_backend_prints_responsive(self, fake_server, capsys):
        with patch.object(cli, "_server", return_value=fake_server):
            with patch(
                "singleton._probe_backend_status", return_value=("responsive", 19222)
            ):
                with patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"):
                    cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "responsive" in out
        assert "UNRESPONSIVE" not in out

    def test_no_backend_prints_not_running(self, fake_server, capsys):
        with patch.object(cli, "_server", return_value=fake_server):
            with patch("singleton._probe_backend_status", return_value=("none", None)):
                with patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"):
                    cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "not running" in out
