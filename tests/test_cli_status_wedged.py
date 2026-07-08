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
import singleton

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


class TestCliStatusPidAndLog:
    """M8-3 (F-305/F-503-half): status/doctor must surface the recorded pid
    and the exact log file to check - stacked on top of M1's responsive/
    wedged/down vocabulary, not replacing it."""

    def test_status_prints_pid_and_log_location(self, fake_server, tmp_path, capsys):
        with (
            patch.object(cli, "_server", return_value=fake_server),
            patch(
                "singleton._probe_backend_status", return_value=("responsive", 19222)
            ),
            patch(
                "singleton._read_server_state",
                return_value={"port": 19222, "version": "1.2.1", "pid": 4242},
            ),
            patch("logging_setup.resolve_log_dir", return_value=tmp_path),
        ):
            cli._cmd_status(None)
        out = capsys.readouterr().out
        assert "4242" in out
        assert str(tmp_path) in out
        assert "backend-4242.log" in out

    def test_status_prints_boot_log_when_no_record(self, fake_server, tmp_path, capsys):
        with (
            patch.object(cli, "_server", return_value=fake_server),
            patch("singleton._probe_backend_status", return_value=("none", None)),
            patch("singleton._read_server_state", return_value=None),
            patch("logging_setup.resolve_log_dir", return_value=tmp_path),
        ):
            cli._cmd_status(None)
        out = capsys.readouterr().out
        assert "backend-boot.log" in out


class TestCliDoctorPortOccupant:
    """M8-3 (F-509 visibility half): doctor names a squatter on the target
    port so a bind collision is diagnosable, distinct from our own backend
    or a genuinely free port."""

    def test_doctor_reports_foreign_occupant(self, fake_server, tmp_path, capsys):
        with (
            patch.object(cli, "_server", return_value=fake_server),
            patch("singleton._probe_backend_status", return_value=("down", None)),
            patch("singleton._read_server_state", return_value=None),
            patch("logging_setup.resolve_log_dir", return_value=tmp_path),
            patch("singleton._backend_pid_on_port", return_value=None),
            patch("singleton._server_is_healthy", return_value=True),
            patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"),
        ):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "NON-stealth" in out
        assert str(singleton.DEFAULT_PORT) in out

    def test_doctor_reports_our_backend_occupant(self, fake_server, tmp_path, capsys):
        with (
            patch.object(cli, "_server", return_value=fake_server),
            patch(
                "singleton._probe_backend_status", return_value=("responsive", 19222)
            ),
            patch(
                "singleton._read_server_state",
                return_value={"port": 19222, "version": "1.2.1", "pid": 4242},
            ),
            patch("logging_setup.resolve_log_dir", return_value=tmp_path),
            patch("singleton._backend_pid_on_port", return_value=4242),
            patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"),
        ):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "held by our backend" in out
        assert "4242" in out

    def test_doctor_reports_free_port(self, fake_server, tmp_path, capsys):
        with (
            patch.object(cli, "_server", return_value=fake_server),
            patch("singleton._probe_backend_status", return_value=("none", None)),
            patch("singleton._read_server_state", return_value=None),
            patch("logging_setup.resolve_log_dir", return_value=tmp_path),
            patch("singleton._backend_pid_on_port", return_value=None),
            patch("singleton._server_is_healthy", return_value=False),
            patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"),
        ):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out
        # Exact phrase, not a bare "free" substring - tmp_path's own generated
        # directory name can coincidentally contain "free" (e.g. when it is
        # derived from this test's name), which would false-pass a weaker
        # assertion.
        assert f"port {singleton.DEFAULT_PORT} free" in out

    def test_doctor_foreign_line_delegates_to_port_is_foreign_held(
        self, fake_server, tmp_path, capsys
    ):
        """M8-7/A1 repoint pin: the foreign-occupant branch is now driven by
        the one shared predicate (singleton._port_is_foreign_held), not a
        re-derived _server_is_healthy check - so patching the predicate
        directly (rather than its two underlying probes) is sufficient to
        flip the branch, proving doctor delegates to it instead of
        recomputing "foreign" inline."""
        with (
            patch.object(cli, "_server", return_value=fake_server),
            patch("singleton._probe_backend_status", return_value=("down", None)),
            patch("singleton._read_server_state", return_value=None),
            patch("logging_setup.resolve_log_dir", return_value=tmp_path),
            patch("singleton._backend_pid_on_port", return_value=None),
            patch("singleton._port_is_foreign_held", return_value=True),
            patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"),
        ):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "NON-stealth" in out
