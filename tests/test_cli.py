"""The ops CLI must be a faithful, safe front-end to the storage sweep.

`cleanup` defaults to a dry run that mutates nothing; `--apply` reclaims using
the exact same selectors as the live sweep (so preview and apply agree), trims
named profiles down to their session state, and never deletes named profiles or
touches in-use ones. Pure filesystem tests (the `tmp_session_root` fixture points
every profile helper at a tmp dir); no browser.
"""

import json

import stealth_chrome_devtools_mcp.cli as cli


MARKER = ".stealth_chrome_devtools_mcp_clone.json"


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _named(sessions, name, *, model_mb):
    d = sessions / name
    d.mkdir(parents=True, exist_ok=True)
    (d / MARKER).write_text(
        json.dumps({"source_kind": "explicit-master", "auto_clean": False}), encoding="utf-8"
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
        json.dumps({"source_kind": "master-snapshot", "auto_clean": True}), encoding="utf-8"
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


class TestStatusProfiles:
    def test_status_runs(self, tmp_session_root, capsys):
        assert cli.main(["status"]) == 0
        assert "session root" in capsys.readouterr().out.lower()

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
        rc = cli.main(["cleanup", "--clone-cap-gb", "0.001", "--session-cap-gb", "0.001"])
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
            ["cleanup", "--apply", "--clone-cap-gb", "0.001", "--session-cap-gb", "0.001"]
        )
        out = capsys.readouterr().out.lower()

        assert rc == 0
        assert "applied" in out
        assert not auto.exists()                                  # auto-clone deleted
        assert not (named / "OptGuideOnDeviceModel").exists()     # named trimmed
        assert (named / "Default" / "Cookies").read_bytes() == b"COOKIES"  # logins kept

    def test_within_caps_reclaims_nothing(self, tmp_session_root, capsys):
        sessions = tmp_session_root["sessions"]
        _named(sessions, "github-session", model_mb=1)

        rc = cli.main(["cleanup"])  # default caps (10/20 GB) — way over the tiny data
        out = capsys.readouterr().out.lower()
        assert rc == 0
        assert "within caps" in out
