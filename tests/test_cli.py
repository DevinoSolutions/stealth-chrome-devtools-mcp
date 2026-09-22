"""The ops CLI must be a faithful, safe front-end to the storage sweep.

`cleanup` defaults to a dry run that mutates nothing; `--apply` reclaims using
the exact same selectors as the live sweep (so preview and apply agree), trims
named profiles down to their session state, and never deletes named profiles or
touches in-use ones. Pure filesystem tests (the `tmp_session_root` fixture points
every profile helper at a tmp dir); no browser.
"""

import json
import os
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
    def test_no_command_prints_help_and_returns_the_usage_code(self, capsys):
        """It returned 1 until F-891 review M1. Nothing declared what 1 MEANT
        before that feature; now it means "the tool answered and said no", which
        is not what naming no verb is. 2 is what argparse's own refusals use."""
        from stealth_chrome_devtools_mcp import cli_call

        assert cli.main([]) == cli_call.EXIT_USAGE == 2
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


class TestKillOrphansForceWarning:
    """F-921: `--force` is the one verb left that can end a human's logged-in
    browser — it skips F-888's persistent-profile spare — and it said so only in
    `cli.py`'s source docstring, which no operator reads. `--help` must name the
    risk and a PRE-FLIGHT line must count what is about to die, printed before
    anything does.

    A printed line and never a prompt: this CLI is driven by agents as well as
    humans, a blocking `input()` on a non-tty would hang them, and `--force` is
    already the explicit opt-in — the consent existed, the disclosure did not.
    `--dry-run` is the inspection half.

    Hermetic: a fake `browser_pids.json` under tmp, `tmp_session_root` setting
    the browser-session root EXPLICITLY, the process scan stubbed so no real
    Chrome can answer, and the reaper patched so nothing is ever killed.
    """

    @staticmethod
    def _record(tmp_path, entries):
        path = tmp_path / "browser_pids.json"
        path.write_text(json.dumps({"browser_processes": entries}), encoding="utf-8")
        return path

    @staticmethod
    def _entry(directory, *, auto_clone=False, pid=4242):
        """One recorded browser. `uses_custom_data_dir=True` + `auto_clone=False`
        is exactly what `browser_pid_registry.on_persistent_profile` reads as
        "this directory outlives its browser"."""
        return {
            "pid": pid,
            "create_time": 1.0,
            "user_data_dir": str(directory),
            "uses_custom_data_dir": True,
            "auto_clone": auto_clone,
        }

    def _bind(self, monkeypatch, tmp_path, entries):
        """Point the record at tmp and stub the process scan.

        The scan answers `()` — "asked, and nothing is running" — so
        `profile_lock` falls through to Chrome's own singleton, which is what
        `fakes.held_profile` writes. Without the stub the real machine's process
        table decides and the test is not hermetic.

        **Why the stub lands at all**: `clone_storage.py` imports the process
        cleanup SINGLETON (`from ...process_cleanup import process_cleanup`),
        not the module, so `clone_storage._profile_hold`'s
        `getattr(process_cleanup, "_get_browser_pids_for_profile")` resolves on
        the very object patched here. Re-point that import at the module and
        this stub silently stops reaching `_profile_hold`, the real process
        table answers, and every node below becomes machine-dependent while
        staying green on a quiet machine — so do not "simplify" it.
        """
        from stealth_chrome_devtools_mcp.embedded import process_cleanup

        monkeypatch.setattr(cli, "_server", lambda: None)
        monkeypatch.setattr(
            process_cleanup.process_cleanup,
            "pid_file",
            self._record(tmp_path, entries),
        )
        monkeypatch.setattr(
            process_cleanup.process_cleanup,
            "_get_browser_pids_for_profile",
            lambda _directory: (),
        )

    @staticmethod
    def _no_backend_and_no_reap():
        """The two patches every body-level node here shares: nothing is
        listening (so the live-backend guard lets the verb through) and the
        reaper is a mock (so no process on this machine can be killed)."""
        return (
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("none", None),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.process_cleanup"
                ".process_cleanup.recover_orphans"
            ),
        )

    @staticmethod
    def _flag_help(flag: str) -> str:
        """One way to read a `kill-orphans` flag's help, lowercased."""
        action = next(
            action
            for action in cli.build_parser()
            ._subparsers._group_actions[0]
            .choices["kill-orphans"]
            ._actions
            if flag in action.option_strings
        )
        return (action.help or "").lower()

    def test_force_help_names_the_logged_in_browser_risk(self):
        """The whole defect in one assertion: "override the live-backend guard
        and reap anyway" is true, and says nothing about the harm."""
        help_text = self._flag_help("--force")
        assert "live-backend guard" in help_text
        assert "logged-in" in help_text or "logged in" in help_text
        assert "--dry-run" in help_text

    def test_dry_run_help_claims_only_what_the_flag_prints(self):
        """`--dry-run` prints the persistent pre-flight and nothing else, so
        "print what would be reaped" over-claimed: on the machine measured in
        the finding `--force` reaps SIX entries while the pre-flight names TWO
        directories. A preview that names a smaller set than the act is the
        same under-disclosure F-921 is about, one flag along."""
        help_text = self._flag_help("--dry-run")
        assert "persistent profiles at risk" in help_text
        assert "what would be reaped" not in help_text

    def test_dry_run_parses_and_defaults_false(self):
        assert cli.build_parser().parse_args(["kill-orphans"]).dry_run is False
        assert cli.build_parser().parse_args(["kill-orphans", "--dry-run"]).dry_run

    def test_preflight_counts_persistent_profiles_before_the_reaper_runs(
        self, tmp_session_root, tmp_path, monkeypatch, capsys
    ):
        """The count must reach the terminal BEFORE anything can die, so the
        assertion is made from inside the reaper itself."""
        from tests import fakes

        logged_in = _named(tmp_session_root["sessions"], "github-session", model_mb=1)
        fakes.held_profile(logged_in)
        self._bind(monkeypatch, tmp_path, {"a": self._entry(logged_in)})

        printed = {}
        probe, reap = self._no_backend_and_no_reap()
        with probe, reap as reaper:
            reaper.side_effect = lambda **_kwargs: printed.update(
                out=capsys.readouterr().out
            )
            assert cli.main(["kill-orphans", "--force"]) == 0

        reaper.assert_called_once()
        assert "1 persistent" in printed["out"]
        # Named because `profile_lock` read the singleton `fakes.held_profile`
        # wrote — the companion node below has the same directory, tracked and
        # existing, and is NOT named because nothing holds it.
        assert "1 open now (github-session)" in printed["out"]

    def test_preflight_names_no_path(
        self, tmp_session_root, tmp_path, monkeypatch, capsys
    ):
        """F-869/F-877 discipline: session names, counts and pids — never a
        path, which names the operating user.

        Two things make it able to fail, and it needed BOTH. The profile is
        HELD: without that nothing is open, the naming branch never runs and
        there is no name to bite on — which is what this node shipped as at
        `f35e2ca`, green and vacuous. And the comparison is made under the
        RECORD's own
        normalization: `browser_pid_registry.normalize_path` normcases, i.e.
        LOWERCASES on Windows, so the string a leak would actually print is the
        lowercased one and a raw `str(logged_in) not in out` misses it there.
        Mutation-proven both ways: `persistent_profile_risk`'s `path.name` ->
        `str(path)` turns this RED. A pin that is green in a RED census is not
        thereby an invariant; it is a pin nobody has shown can fail.
        """
        from tests import fakes

        from stealth_chrome_devtools_mcp.embedded.browser_pid_registry import (
            normalize_path,
        )

        sessions = tmp_session_root["sessions"]
        logged_in = _named(sessions, "github-session", model_mb=1)
        fakes.held_profile(logged_in)
        self._bind(monkeypatch, tmp_path, {"a": self._entry(logged_in)})

        probe, reap = self._no_backend_and_no_reap()
        with probe, reap:
            assert cli.main(["kill-orphans", "--force"]) == 0

        out = capsys.readouterr().out
        assert "github-session" in out  # the branch that could leak a path ran
        shown = os.path.normcase(out)
        assert normalize_path(str(logged_in)) not in shown
        assert normalize_path(str(sessions)) not in shown

    def test_auto_clones_are_not_counted_as_persistent(
        self, tmp_session_root, tmp_path, monkeypatch, capsys
    ):
        """The predicate is `on_persistent_profile`, the same one `--force`
        skips — so a disposable clone must not inflate the warning."""
        clone = _auto(tmp_session_root["sessions"], "sess-auto", mb=1)
        self._bind(monkeypatch, tmp_path, {"a": self._entry(clone, auto_clone=True)})

        probe, reap = self._no_backend_and_no_reap()
        with probe, reap:
            assert cli.main(["kill-orphans", "--force"]) == 0

        out = capsys.readouterr().out
        assert "sess-auto" not in out
        assert "none tracked on a persistent profile" in out
        assert "BY HAND" not in out

    def test_two_entries_on_one_profile_count_once(
        self, tmp_session_root, tmp_path, monkeypatch, capsys
    ):
        """The reap is DIRECTORY-matched (`_kill_processes_for_metadata`), so two
        entries on one profile end one profile, not two."""
        shared = _named(tmp_session_root["sessions"], "github-session", model_mb=1)
        self._bind(
            monkeypatch,
            tmp_path,
            {"a": self._entry(shared, pid=11), "b": self._entry(shared, pid=22)},
        )

        probe, reap = self._no_backend_and_no_reap()
        with probe, reap:
            assert cli.main(["kill-orphans", "--force"]) == 0

        assert "1 persistent" in capsys.readouterr().out

    def test_a_tracked_profile_nothing_holds_is_counted_but_not_open(
        self, tmp_session_root, tmp_path, monkeypatch, capsys
    ):
        """`profile_lock`, never a presence test (F-871): the directory exists
        and is recorded, and with no live holder it is still not open — so it
        is counted as tracked and left out of the named set."""
        idle = _named(tmp_session_root["sessions"], "github-session", model_mb=1)
        self._bind(monkeypatch, tmp_path, {"a": self._entry(idle)})

        probe, reap = self._no_backend_and_no_reap()
        with probe, reap:
            assert cli.main(["kill-orphans", "--force"]) == 0

        out = capsys.readouterr().out
        assert "1 persistent profile(s) in the record, none open" in out
        assert "github-session" not in out
        # The harm sentence is about losing a login. Nothing is running, so
        # this invocation ends none, and saying otherwise is the same defect
        # F-921 is about with the sign reversed.
        assert "BY HAND" not in out

    def test_dry_run_prints_the_set_and_reaps_nothing(
        self, tmp_session_root, tmp_path, monkeypatch, capsys
    ):
        logged_in = _named(tmp_session_root["sessions"], "github-session", model_mb=1)
        self._bind(monkeypatch, tmp_path, {"a": self._entry(logged_in)})

        probe, reap = self._no_backend_and_no_reap()
        with probe, reap as reaper:
            assert cli.main(["kill-orphans", "--force", "--dry-run"]) == 0

        reaper.assert_not_called()
        out = capsys.readouterr().out.lower()
        assert "1 persistent" in out
        assert "dry run" in out

    def test_an_empty_record_warns_about_nothing(
        self, tmp_session_root, tmp_path, monkeypatch, capsys
    ):
        """Review blocking 2: with nothing recorded the line still read "0
        persistent profile(s) tracked" and then told the operator that logins
        must be re-entered by hand. A warning printed when there is nothing to
        warn about is how a warning stops being read."""
        self._bind(monkeypatch, tmp_path, {})

        probe, reap = self._no_backend_and_no_reap()
        with probe, reap:
            assert cli.main(["kill-orphans", "--force"]) == 0

        out = capsys.readouterr().out
        assert "none tracked on a persistent profile" in out
        assert "BY HAND" not in out
        assert "open now" not in out

    def test_the_line_names_the_reaps_whole_scope_not_just_the_persistent_set(
        self, tmp_session_root, tmp_path, monkeypatch, capsys
    ):
        """`--force` ends EVERY tracked browser, not only the persistent ones,
        and the count is of what the RECORD names. Saying "2 tracked, --force
        ENDS these browsers" beside a record holding clones too implied the
        clones were safe and that the toll was exactly 2. Scope first, harm
        second."""
        from tests import fakes

        sessions = tmp_session_root["sessions"]
        logged_in = _named(sessions, "github-session", model_mb=1)
        fakes.held_profile(logged_in)
        clone = _auto(sessions, "sess-auto", mb=1)
        self._bind(
            monkeypatch,
            tmp_path,
            {
                "a": self._entry(logged_in),
                "b": self._entry(clone, auto_clone=True, pid=77),
            },
        )

        probe, reap = self._no_backend_and_no_reap()
        with probe, reap:
            assert cli.main(["kill-orphans", "--force"]) == 0

        out = capsys.readouterr().out
        assert "1 persistent profile(s) in the record, 1 open now" in out
        assert "--force ends EVERY tracked browser" in out
        assert "sess-auto" not in out  # a clone holds no login to name

    def test_without_force_the_line_says_persistent_profiles_are_spared(
        self, tmp_session_root, tmp_path, monkeypatch, capsys
    ):
        """Without `--force` F-888 spares them, so the same line must NOT claim
        they are about to end — a pre-flight that overstates is one nobody
        reads the second time. The profile is HELD deliberately: the wording
        branch under test only exists in the shape that has something to end."""
        from tests import fakes

        logged_in = _named(tmp_session_root["sessions"], "github-session", model_mb=1)
        fakes.held_profile(logged_in)
        self._bind(monkeypatch, tmp_path, {"a": self._entry(logged_in)})

        probe, reap = self._no_backend_and_no_reap()
        with probe, reap:
            assert cli.main(["kill-orphans"]) == 0

        out = capsys.readouterr().out
        assert "1 persistent" in out
        assert "only --force ends them" in out
        assert "ends EVERY tracked browser" not in out


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
