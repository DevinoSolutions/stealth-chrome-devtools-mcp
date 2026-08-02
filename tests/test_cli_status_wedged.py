"""Pinning tests for M1-4's CLI wiring: `status`/`doctor` report responsive/
wedged/down via `_format_backend_status` (-> `singleton._probe_backend_
status`) instead of `_find_running_server`'s binary reuse-or-not answer.

`_server()` is mocked out (returns a lightweight stub with just the methods
_cmd_status/_cmd_doctor call) to avoid the real, heavyweight `import server`
side effect (FastMCP tool registration) — the singleton wiring is what these
tests pin, not the rest of the CLI's server-derived fields.

The last class extends that to F-808's question: `_format_backend_status` still
answers "is THE backend up", but the record now holds one backend per display
context, so doctor must also name every recorded one and say which of them can
put a window on a screen.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stealth_chrome_devtools_mcp import cli
from stealth_chrome_devtools_mcp.embedded import singleton


@pytest.fixture()
def fake_server(tmp_path):
    server = MagicMock()
    server.default_session_root.return_value = Path(tmp_path)
    server.clone_storage_cap_bytes.return_value = 1024**3
    server.browser_session_storage_cap_bytes.return_value = 1024**3
    return server


class TestCliStatusBackendState:
    def test_wedged_backend_prints_unresponsive(self, fake_server, capsys):
        with patch.object(cli, "_clone_storage", return_value=fake_server):
            with patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("wedged", 19222),
            ):
                cli._cmd_status(None)
        out = capsys.readouterr().out
        assert "UNRESPONSIVE" in out
        assert "wedged" in out
        assert "19222" in out

    def test_responsive_backend_prints_responsive(self, fake_server, capsys):
        with patch.object(cli, "_clone_storage", return_value=fake_server):
            with patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("responsive", 19222),
            ):
                cli._cmd_status(None)
        out = capsys.readouterr().out
        assert "responsive" in out
        assert "UNRESPONSIVE" not in out
        assert "19222" in out

    def test_no_backend_prints_not_running(self, fake_server, capsys):
        with patch.object(cli, "_clone_storage", return_value=fake_server):
            with patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("none", None),
            ):
                cli._cmd_status(None)
        out = capsys.readouterr().out
        assert "not running" in out


class TestCliDoctorBackendState:
    def test_wedged_backend_prints_unresponsive(self, fake_server, capsys):
        with patch.object(cli, "_clone_storage", return_value=fake_server):
            with patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("wedged", 19222),
            ):
                with patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"):
                    cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "UNRESPONSIVE" in out
        assert "wedged" in out

    def test_responsive_backend_prints_responsive(self, fake_server, capsys):
        with patch.object(cli, "_clone_storage", return_value=fake_server):
            with patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("responsive", 19222),
            ):
                with patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"):
                    cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "responsive" in out
        assert "UNRESPONSIVE" not in out

    def test_no_backend_prints_not_running(self, fake_server, capsys):
        with patch.object(cli, "_clone_storage", return_value=fake_server):
            with patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("none", None),
            ):
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
            patch.object(cli, "_clone_storage", return_value=fake_server),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("responsive", 19222),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._read_server_state",
                return_value={"port": 19222, "version": "1.2.1", "pid": 4242},
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.logging_setup.resolve_log_dir",
                return_value=tmp_path,
            ),
        ):
            cli._cmd_status(None)
        out = capsys.readouterr().out
        assert "4242" in out
        assert str(tmp_path) in out
        assert "backend-4242.log" in out

    def test_status_prints_boot_log_when_no_record(self, fake_server, tmp_path, capsys):
        with (
            patch.object(cli, "_clone_storage", return_value=fake_server),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("none", None),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._read_server_state",
                return_value=None,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.logging_setup.resolve_log_dir",
                return_value=tmp_path,
            ),
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
            patch.object(cli, "_clone_storage", return_value=fake_server),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("down", None),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._read_server_state",
                return_value=None,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.logging_setup.resolve_log_dir",
                return_value=tmp_path,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._backend_pid_on_port",
                return_value=None,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._server_is_healthy",
                return_value=True,
            ),
            patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"),
        ):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "NON-stealth" in out
        assert str(singleton.DEFAULT_PORT) in out

    def test_doctor_reports_our_backend_occupant(self, fake_server, tmp_path, capsys):
        with (
            patch.object(cli, "_clone_storage", return_value=fake_server),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("responsive", 19222),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._read_server_state",
                return_value={"port": 19222, "version": "1.2.1", "pid": 4242},
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.logging_setup.resolve_log_dir",
                return_value=tmp_path,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._backend_pid_on_port",
                return_value=4242,
            ),
            patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"),
        ):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "held by our backend" in out
        assert "4242" in out

    def test_doctor_reports_free_port(self, fake_server, tmp_path, capsys):
        with (
            patch.object(cli, "_clone_storage", return_value=fake_server),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("none", None),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._read_server_state",
                return_value=None,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.logging_setup.resolve_log_dir",
                return_value=tmp_path,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._backend_pid_on_port",
                return_value=None,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._server_is_healthy",
                return_value=False,
            ),
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
            patch.object(cli, "_clone_storage", return_value=fake_server),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
                return_value=("down", None),
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._read_server_state",
                return_value=None,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.logging_setup.resolve_log_dir",
                return_value=tmp_path,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._backend_pid_on_port",
                return_value=None,
            ),
            patch(
                "stealth_chrome_devtools_mcp.embedded.singleton._port_is_foreign_held",
                return_value=True,
            ),
            patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"),
        ):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out
        assert "NON-stealth" in out


@pytest.fixture()
def recorded_backends(tmp_path, monkeypatch):
    """Redirect the backend record at a tmp file and hand back a writer.

    `singleton.SERVER_STATE_FILE` is the one binding every reader resolves
    through, so patching it — never a registry global — is what keeps a test
    run out of the developer's live `~/.stealth-mcp/server.json`.
    """
    path = tmp_path / "server.json"
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", path)

    def _record(state: dict | None) -> None:
        if state is not None:
            path.write_text(json.dumps(state), encoding="utf-8")

    return _record


def _v2(**backends) -> dict:
    """A schema-v2 record from `context=entry` kwargs (`_` reads as `-`)."""
    return {
        "schema": 2,
        "backends": {ctx.replace("_", "-"): entry for ctx, entry in backends.items()},
    }


@contextmanager
def _doctor_probes(fake_server, *, healthy=True, ready=True):
    """Run doctor with every live probe stubbed, so the ONLY input that varies
    is the recorded state file.

    `healthy`/`ready` take a bool or a port predicate — that pair is what
    `_probe_backend_status`'s vocabulary is built from (is the socket open? and
    does it answer a real initialize?), so per-port liveness is expressible here
    without a second set of stubs.
    """

    def _by_port(answer):
        return lambda port, **_kwargs: answer(port) if callable(answer) else answer

    with (
        patch.object(cli, "_clone_storage", return_value=fake_server),
        patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"),
        patch(
            "stealth_chrome_devtools_mcp.embedded.singleton._probe_backend_status",
            return_value=("none", None),
        ),
        patch(
            "stealth_chrome_devtools_mcp.embedded.singleton._backend_pid_on_port",
            return_value=None,
        ),
        patch(
            "stealth_chrome_devtools_mcp.embedded.singleton._port_is_foreign_held",
            return_value=False,
        ),
        patch(
            "stealth_chrome_devtools_mcp.embedded.singleton._server_is_healthy",
            side_effect=_by_port(healthy),
        ),
        patch(
            "stealth_chrome_devtools_mcp.embedded.singleton._backend_http_ready",
            side_effect=_by_port(ready),
        ),
    ):
        yield


class TestCliDoctorDisplayContexts:
    """plan_F808 Task 6: doctor names EVERY recorded backend with its display
    context, so "which contexts have a backend" — the question `spawn_browser`'s
    headed-visibility refusal sends the operator here to answer — is answerable
    at all. The single `backend :` summary line above it reports the first
    recorded backend only, and cannot.
    """

    def test_every_recorded_backend_is_listed_with_its_context(
        self, fake_server, recorded_backends, capsys
    ):
        recorded_backends(
            _v2(
                win_session_1={"port": 55296, "pid": 4242, "version": "2.0.4"},
                headless={"port": 19222, "pid": 909, "version": "2.0.4"},
            )
        )
        with _doctor_probes(fake_server):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        assert "backend  win-session-1  port 55296  pid 4242  version 2.0.4" in out
        assert "backend  headless  port 19222  pid 909  version 2.0.4" in out
        assert "(can show windows)" in out
        assert "(headless only)" in out

    def test_window_capable_backends_are_listed_first(
        self, fake_server, recorded_backends, capsys
    ):
        """Recorded headless-first, printed capable-first: the order is
        `backend_registry.window_capable_first`'s, not the file's."""
        recorded_backends(
            _v2(
                headless={"port": 19222, "pid": 909, "version": "2.0.4"},
                win_session_1={"port": 55296, "pid": 4242, "version": "2.0.4"},
            )
        )
        with _doctor_probes(fake_server):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        assert out.index("backend  win-session-1") < out.index("backend  headless")

    def test_each_backend_is_probed_on_its_own_port(
        self, fake_server, recorded_backends, capsys
    ):
        """Three recorded backends, three different liveness answers — a
        once-per-record probe would report one verdict for all of them."""
        recorded_backends(
            _v2(
                win_session_1={"port": 55296, "pid": 4242, "version": "2.0.4"},
                win_session_2={"port": 55297, "pid": 4243, "version": "2.0.4"},
                headless={"port": 19222, "pid": 909, "version": "2.0.4"},
            )
        )
        with _doctor_probes(
            fake_server,
            healthy=lambda port: port != 19222,  # the headless one is gone
            ready=lambda port: port == 55296,  # 55297 holds its socket only
        ):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        assert "port 55296  pid 4242  version 2.0.4  responsive" in out
        assert "port 55297  pid 4243  version 2.0.4  wedged" in out
        assert "port 19222  pid 909  version 2.0.4  down" in out

    def test_remedy_line_when_no_backend_can_show_a_window(
        self, fake_server, recorded_backends, capsys
    ):
        """The actionable half: a headless-only machine must be told what to do,
        in the same words `spawn_browser`'s refusal uses."""
        recorded_backends(_v2(headless={"port": 19222, "pid": 909, "version": "2.0.4"}))
        with _doctor_probes(fake_server):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        assert "no live backend can display a window" in out
        assert "headed spawns will fail" in out
        assert "desktop session" in out
        assert "automatically" in out

    def test_no_remedy_line_when_a_window_capable_backend_is_responsive(
        self, fake_server, recorded_backends, capsys
    ):
        recorded_backends(
            _v2(
                headless={"port": 19222, "pid": 909, "version": "2.0.4"},
                win_session_1={"port": 55296, "pid": 4242, "version": "2.0.4"},
            )
        )
        # Explicitly responsive: it is the LIVENESS, not the recorded token,
        # that is allowed to suppress the remedy.
        with _doctor_probes(fake_server, healthy=True, ready=True):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        # Both halves: the capable backend IS listed, and its presence is what
        # suppresses the remedy (asserting only the absence would pass against
        # a doctor that prints nothing at all).
        assert "backend  win-session-1" in out
        assert "responsive  (can show windows)" in out
        assert "no live backend can display a window" not in out

    def test_a_dead_window_capable_backend_does_not_suppress_the_remedy(
        self, fake_server, recorded_backends, capsys
    ):
        """The desktop logged out. The entry survives, so a token-only judgment
        would hide the advice in exactly the state that needs it — the SSH
        operator's headed spawn is still refused, because discovery finds no
        LIVE capable backend to adopt."""
        recorded_backends(
            _v2(win_session_1={"port": 55296, "pid": 4242, "version": "2.0.4"})
        )
        with _doctor_probes(fake_server, healthy=False):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        # The note stays token-driven: that is still where its windows WOULD go.
        assert "down  (can show windows)" in out
        assert "no live backend can display a window" in out

    def test_a_wedged_window_capable_backend_does_not_suppress_the_remedy(
        self, fake_server, recorded_backends, capsys
    ):
        """Same ruling's other half: a wedged backend holds its socket open but
        cannot serve a spawn either."""
        recorded_backends(
            _v2(win_session_1={"port": 55296, "pid": 4242, "version": "2.0.4"})
        )
        with _doctor_probes(fake_server, healthy=True, ready=False):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        assert "wedged  (can show windows)" in out
        assert "no live backend can display a window" in out

    def test_an_entry_with_no_usable_port_says_so(
        self, fake_server, recorded_backends, capsys
    ):
        """The record tolerates hand-editing, so `port` can be a string. There
        is nothing to probe, and claiming "down" would assert a liveness we
        never tested."""
        recorded_backends(
            _v2(win_session_1={"port": "55296", "pid": 4242, "version": "2.0.4"})
        )
        with _doctor_probes(fake_server):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        assert "backend  win-session-1  port -  pid 4242" in out
        assert "no port recorded  (can show windows)" in out
        assert "no live backend can display a window" in out

    def test_a_v1_record_reads_as_one_unverified_window_capable_backend(
        self, fake_server, recorded_backends, capsys
    ):
        """Every release up to 2.0.3 wrote the flat record. It must list, and it
        must NOT provoke the remedy — UNVERIFIED is treated as capable."""
        recorded_backends({"port": 19222, "pid": 909, "version": "2.0.3"})
        with _doctor_probes(fake_server):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        assert "backend  unverified  port 19222  pid 909  version 2.0.3" in out
        assert "(can show windows)" in out
        assert "no backend can display a window" not in out

    def test_no_record_says_none_recorded_without_a_remedy(
        self, fake_server, recorded_backends, capsys
    ):
        """Nothing recorded is not the headless-only diagnosis: the next spawn
        cold-starts a backend in whatever context it happens to run in."""
        recorded_backends(None)  # the file is never written
        with _doctor_probes(fake_server):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        assert "backend  (none recorded)" in out
        assert "no backend can display a window" not in out
