"""The ops CLI must be a faithful, safe front-end to the storage sweep.

`cleanup` defaults to a dry run that mutates nothing; `--apply` reclaims using
the exact same selectors as the live sweep (so preview and apply agree), trims
named profiles down to their session state, and never deletes named profiles or
touches in-use ones. Pure filesystem tests (the `tmp_session_root` fixture points
every profile helper at a tmp dir); no browser.
"""

import json
from unittest.mock import patch

from stealth_chrome_devtools_mcp import cli

MARKER = ".stealth_chrome_devtools_mcp_clone.json"


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _named(sessions, name, *, model_mb):
    d = sessions / name
    d.mkdir(parents=True, exist_ok=True)
    (d / MARKER).write_text(
        json.dumps({"source_kind": "explicit-master", "auto_clean": False}),
        encoding="utf-8",
    )
    _write(d / "OptGuideOnDeviceModel" / "model.bin", b"x" * (model_mb * 1024 * 1024))
    _write(d / "Default" / "Cache" / "data", b"x" * 4096)
    _write(d / "Default" / "Cookies", b"COOKIES")
    _write(d / "Default" / "Login Data", b"LOGINS")
    return d


def _auto(sessions, name, *, mb):
    d = sessions / name
    d.mkdir(parents=True, exist_ok=True)
    (d / MARKER).write_text(
        json.dumps({"source_kind": "master-snapshot", "auto_clean": True}),
        encoding="utf-8",
    )
    _write(d / "Default" / "Cache" / "data", b"x" * (mb * 1024 * 1024))
    return d


class TestParser:
    def test_no_command_prints_help_and_returns_1(self, capsys):
        assert cli.main([]) == 1
        assert "usage" in capsys.readouterr().out.lower()

    def test_serve_args_parse(self):
        args = cli.build_parser().parse_args(["serve", "--http", "--port", "20001"])
        assert args.command == "serve" and args.http and args.port == 20001

    def test_stop_args_parse(self):
        args = cli.build_parser().parse_args(["stop"])
        assert args.command == "stop"

    def test_restart_args_parse(self):
        args = cli.build_parser().parse_args(["restart"])
        assert args.command == "restart"

    def test_kill_orphans_args_parse_defaults_force_false(self):
        args = cli.build_parser().parse_args(["kill-orphans"])
        assert args.command == "kill-orphans"
        assert args.force is False

    def test_kill_orphans_force_flag_parses_true(self):
        args = cli.build_parser().parse_args(["kill-orphans", "--force"])
        assert args.force is True


class TestStopVerb:
    """M8-4: `stop` is a thin front-end over singleton.stop_backend() - no
    matching/kill logic of its own in cli.py."""

    def test_stop_dispatches_and_prints_result(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with patch(
            "stealth_chrome_devtools_mcp.embedded.singleton.stop_backend",
            return_value=("stopped", 4242),
        ):
            assert cli.main(["stop"]) == 0
        assert "4242" in capsys.readouterr().out

    def test_stop_busy_returns_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with patch(
            "stealth_chrome_devtools_mcp.embedded.singleton.stop_backend",
            return_value=("busy", None),
        ):
            assert cli.main(["stop"]) == 1
        assert "busy" in capsys.readouterr().out.lower()

    def test_stop_not_running(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with patch(
            "stealth_chrome_devtools_mcp.embedded.singleton.stop_backend",
            return_value=("not running", None),
        ):
            assert cli.main(["stop"]) == 0
        assert "not running" in capsys.readouterr().out.lower()

    def test_stop_already_stopped(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with patch(
            "stealth_chrome_devtools_mcp.embedded.singleton.stop_backend",
            return_value=("already stopped", None),
        ):
            assert cli.main(["stop"]) == 0
        assert "already stopped" in capsys.readouterr().out.lower()


class TestRestartVerb:
    """M8-5: `restart` is a thin front-end over singleton.restart_backend() -
    no lifecycle logic of its own in cli.py. Exit code is 0 iff the final
    state is "responsive"; busy and any degraded post-restart state (wedged/
    down/none) are both non-zero, and the printed message must say so
    honestly rather than implying success."""

    def test_restart_responsive_returns_zero_with_pid(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with patch(
            "stealth_chrome_devtools_mcp.embedded.singleton.restart_backend",
            return_value=("responsive", 4242),
        ):
            assert cli.main(["restart"]) == 0
        assert "4242" in capsys.readouterr().out

    def test_restart_busy_returns_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with patch(
            "stealth_chrome_devtools_mcp.embedded.singleton.restart_backend",
            return_value=("busy", None),
        ):
            assert cli.main(["restart"]) == 1
        assert "busy" in capsys.readouterr().out.lower()

    def test_restart_down_returns_nonzero_with_honest_output(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with patch(
            "stealth_chrome_devtools_mcp.embedded.singleton.restart_backend",
            return_value=("down", None),
        ):
            assert cli.main(["restart"]) == 1
        assert "down" in capsys.readouterr().out.lower()


class TestKillOrphansVerb:
    """M8-6: `kill-orphans` is a thin, gated trigger over
    process_cleanup.process_cleanup._recover_orphaned_processes() - no
    matching logic of its own in cli.py. Gated off a live backend (reaping
    would kill a live backend's own browsers and wipe its pid tracking):
    responsive/wedged refuse unless --force; down/none proceed."""

    def test_responsive_refuses_and_does_not_call_reaper(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("responsive", 19222),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._read_server_state",
                return_value={"pid": 4242, "port": 19222, "version": "1.2.1"},
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.process_cleanup._recover_orphaned_processes"
            ) as reaper,
        ):
            rc = cli.main(["kill-orphans"])

        assert rc == 1
        reaper.assert_not_called()
        out = capsys.readouterr().out.lower()
        assert "restart" in out
        assert "--force" in out
        assert "4242" in out

    def test_wedged_refuses_and_does_not_call_reaper(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("wedged", 19222),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._read_server_state",
                return_value={"pid": 4242, "port": 19222, "version": "1.2.1"},
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.process_cleanup._recover_orphaned_processes"
            ) as reaper,
        ):
            rc = cli.main(["kill-orphans"])

        assert rc == 1
        reaper.assert_not_called()
        assert "restart" in capsys.readouterr().out.lower()

    def test_responsive_with_force_calls_reaper(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("responsive", 19222),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.process_cleanup._recover_orphaned_processes"
            ) as reaper,
        ):
            rc = cli.main(["kill-orphans", "--force"])

        assert rc == 0
        reaper.assert_called_once()

    def test_down_calls_reaper(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("down", 19222),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.process_cleanup._recover_orphaned_processes"
            ) as reaper,
        ):
            rc = cli.main(["kill-orphans"])

        assert rc == 0
        reaper.assert_called_once()

    def test_none_calls_reaper(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_server", lambda: None)
        with (
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("none", None),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup.process_cleanup._recover_orphaned_processes"
            ) as reaper,
        ):
            rc = cli.main(["kill-orphans"])

        assert rc == 0
        reaper.assert_called_once()


class TestStatusProfiles:
    def test_status_runs(self, tmp_session_root, capsys):
        assert cli.main(["status"]) == 0
        assert "browser-session root" in capsys.readouterr().out.lower()

    def test_status_labels_are_glossary_conformant(self, tmp_session_root, capsys):
        """F-741 pin: status/doctor use glossary-conformant 'browser-session'
        labels and the renamed cap env var; a reverted bare 'session root' or
        'session cap' leading label fails this test."""
        import re

        assert cli.main(["status"]) == 0
        cli.main(["doctor"])  # return code is Chrome-dependent; we scan its output
        out = capsys.readouterr().out
        assert "browser-session root" in out.lower()
        assert "STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB" in out
        for line in out.splitlines():
            assert not re.match(r"\s*session (root|cap)\b", line), line

    def test_profiles_lists_roles(self, tmp_session_root, capsys):
        sessions = tmp_session_root["sessions"]
        _named(sessions, "github-session", model_mb=2)
        _auto(sessions, "sess-auto", mb=1)

        assert cli.main(["profiles"]) == 0
        out = capsys.readouterr().out
        assert "github-session" in out and "named" in out
        assert "sess-auto" in out and "auto-clone" in out


class TestCleanup:
    def test_dry_run_reports_plan_and_mutates_nothing(self, tmp_session_root, capsys):
        sessions = tmp_session_root["sessions"]
        named = _named(sessions, "github-session", model_mb=4)
        auto = _auto(sessions, "sess-auto", mb=4)

        # Tiny caps so both the auto-clone and the named profile are over.
        rc = cli.main(
            ["cleanup", "--clone-cap-gb", "0.001", "--browser-session-cap-gb", "0.001"]
        )
        out = capsys.readouterr().out.lower()

        assert rc == 0
        assert "dry run" in out
        # nothing was touched
        assert (auto / "Default" / "Cache").exists()
        assert (named / "OptGuideOnDeviceModel").exists()
        assert (named / "Default" / "Cookies").read_bytes() == b"COOKIES"

    def test_apply_deletes_autoclones_and_trims_named(self, tmp_session_root, capsys):
        sessions = tmp_session_root["sessions"]
        named = _named(sessions, "github-session", model_mb=4)
        auto = _auto(sessions, "sess-auto", mb=4)

        rc = cli.main(
            [
                "cleanup",
                "--apply",
                "--clone-cap-gb",
                "0.001",
                "--browser-session-cap-gb",
                "0.001",
            ]
        )
        out = capsys.readouterr().out.lower()

        assert rc == 0
        assert "applied" in out
        assert not auto.exists()  # auto-clone deleted
        assert not (named / "OptGuideOnDeviceModel").exists()  # named trimmed
        assert (named / "Default" / "Cookies").read_bytes() == b"COOKIES"  # logins kept

    def test_within_caps_reclaims_nothing(self, tmp_session_root, capsys):
        sessions = tmp_session_root["sessions"]
        _named(sessions, "github-session", model_mb=1)

        rc = cli.main(["cleanup"])  # default caps (10/20 GB) — way over the tiny data
        out = capsys.readouterr().out.lower()
        assert rc == 0
        assert "within caps" in out
