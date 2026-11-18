"""T-15386 — the out-of-process supervisor terminates a backend that has been
unusable for minutes, and nothing else.

Each of the 2026-11-07 hangs ended the same way: a person killed the backend and
the next proxy call cold-started a fresh one. ``backend_supervisor`` is that kill
with its rule written down, run once a minute from outside every process the
hang can reach. Pinned here: the verdict (GREEN / RED / SUSPECT), the clocks
across passes, the pid pin that keeps it from killing a successor, and the act
itself against a REAL child process. The verb's wiring is pinned through the
real parser. No test reaches a real backend: probes are handed in.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import psutil
import pytest

from stealth_chrome_devtools_mcp import cli, cli_supervise
from stealth_chrome_devtools_mcp.embedded import (
    backend_liveness,
    backend_registry,
    backend_supervisor,
    browser_pid_registry,
)

PORT = 51996
PID = 4444
STARTED = 1000.0
RED_LIMIT = backend_supervisor.RED_KILL_SECONDS
SUSPECT_LIMIT = backend_supervisor.SUSPECT_KILL_SECONDS


def _seen(*, healthy=False, silence=None, lag=None, pid=PID, started=STARTED):
    return backend_supervisor.Observation(
        port=PORT, pid=pid, started=started, healthy=healthy, silence=silence, lag=lag
    )


class TestTheVerdict:
    def test_an_answering_backend_is_green_whatever_its_stamp(self):
        assert backend_supervisor.judge(_seen(healthy=True)) == "green"
        assert backend_supervisor.judge(_seen(healthy=True, lag=9.0)) == "green"

    def test_no_answer_and_no_stamp_is_red(self):
        assert backend_supervisor.judge(_seen()) == "red"

    def test_no_answer_and_a_stale_stamp_is_red(self):
        stale = backend_liveness.HEARTBEAT_STALE_SECONDS + 1
        assert backend_supervisor.judge(_seen(silence=stale, lag=0.01)) == "red"

    def test_no_answer_and_a_lagging_loop_is_red(self):
        """The incident: stamping every few seconds, every await seconds late."""
        assert backend_supervisor.judge(_seen(silence=2.0, lag=2.5)) == "red"

    def test_no_answer_behind_a_free_loop_is_only_suspect(self):
        """The prober may be the starved one (F-889); give it longer."""
        assert backend_supervisor.judge(_seen(silence=2.0, lag=0.01)) == "suspect"
        assert backend_supervisor.judge(_seen(silence=2.0)) == "suspect"


class TestTheClocks:
    def _pass(self, seen, state, now):
        return backend_supervisor.decide([seen], state, now)

    def test_red_is_terminated_after_three_minutes_not_before(self):
        state = {}
        for now in (5000.0, 5060.0, 5120.0):
            [verdict], state = self._pass(_seen(), state, now)
            assert not verdict.kill
        [verdict], state = self._pass(_seen(), state, 5000.0 + RED_LIMIT)
        assert verdict.kill
        assert verdict.unusable_for == RED_LIMIT

    def test_suspect_waits_ten_minutes(self):
        seen = _seen(silence=1.0, lag=0.01)
        [verdict], state = self._pass(seen, {}, 5000.0)
        [verdict], _ = self._pass(seen, state, 5000.0 + RED_LIMIT)
        assert not verdict.kill
        [verdict], _ = self._pass(seen, state, 5000.0 + SUSPECT_LIMIT)
        assert verdict.kill

    def test_one_green_pass_restarts_the_clock(self):
        _, state = self._pass(_seen(), {}, 5000.0)
        [green], state = self._pass(_seen(healthy=True), state, 5060.0)
        assert green.state == "green"
        assert state == {}
        [verdict], _ = self._pass(_seen(), state, 5000.0 + RED_LIMIT)
        assert not verdict.kill

    def test_a_new_process_on_the_port_starts_a_fresh_clock(self):
        _, state = self._pass(_seen(), {}, 5000.0)
        successor = _seen(pid=PID + 1, started=5100.0)
        [verdict], _ = self._pass(successor, state, 5000.0 + RED_LIMIT)
        assert not verdict.kill

    def test_a_restarted_pid_is_a_new_process(self):
        """Same pid, different create time: a recycled pid is not the judged one."""
        _, state = self._pass(_seen(), {}, 5000.0)
        [verdict], _ = self._pass(_seen(started=5050.0), state, 5000.0 + RED_LIMIT)
        assert not verdict.kill

    def test_a_backend_is_never_in_trouble_for_longer_than_it_has_lived(self):
        """A slow cold start is not a red one."""
        young = _seen(started=5000.0)
        clock = {"pid": PID, "started": 5000.0, "since": 1.0}
        [verdict], _ = self._pass(young, {str(PORT): clock}, 5100.0)
        assert verdict.unusable_for == 100.0
        assert not verdict.kill


def _sleeper():
    proc = subprocess.Popen(  # noqa: S603  PERMANENT(test child, fixed argv)
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    return proc, psutil.Process(proc.pid).create_time()


class TestTheAct:
    def test_it_terminates_exactly_the_judged_process(self):
        proc, started = _sleeper()
        try:
            seen = _seen(pid=proc.pid, started=started)
            killed = backend_supervisor.kill(
                seen, is_ours=lambda pid: pid == proc.pid, is_healthy=lambda port: False
            )
            assert killed is True
            assert proc.wait(timeout=10) is not None
        finally:
            proc.kill()

    def test_it_never_terminates_a_process_it_did_not_judge(self):
        """The pin: a create time that no longer matches is someone else."""
        proc, started = _sleeper()
        try:
            seen = _seen(pid=proc.pid, started=started - 10)
            killed = backend_supervisor.kill(
                seen, is_ours=lambda pid: True, is_healthy=lambda port: False
            )
            assert killed is False
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_it_never_terminates_what_is_not_ours(self):
        proc, started = _sleeper()
        try:
            seen = _seen(pid=proc.pid, started=started)
            killed = backend_supervisor.kill(
                seen, is_ours=lambda pid: False, is_healthy=lambda port: False
            )
            assert killed is False
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture()
def recorded(tmp_path, monkeypatch):
    """A record naming one live process (this test's own child), judged ours."""
    proc, started = _sleeper()
    record = tmp_path / "server.json"
    backend_registry.record_backend(
        record,
        port=PORT,
        version="2.1.9",
        pid=proc.pid,
        source_fingerprint="a" * 64,
        display_context="win-session-1",
    )
    yield record, proc, started
    proc.kill()
    proc.wait(timeout=10)


def _run(record, proc, *, now, dry_run=False, healthy=False, troubles=None):
    return backend_supervisor.run_pass(
        record,
        is_ours=lambda pid: pid == proc.pid,
        healthy=lambda port: healthy,
        is_healthy=lambda port: False,
        dry_run=dry_run,
        now=lambda: now,
        on_trouble=None if troubles is None else lambda: troubles.append(1),
    )


class TestOnePass:
    def test_a_healthy_backend_is_left_alone_and_opens_no_log(self, recorded):
        record, proc, started = recorded
        troubles = []
        [verdict] = _run(
            record, proc, now=started + 999, healthy=True, troubles=troubles
        )
        assert verdict.state == "green"
        assert troubles == []
        assert proc.poll() is None

    def test_red_across_passes_is_terminated_and_remembered(self, recorded):
        record, proc, started = recorded
        troubles = []
        t0 = started + 10_000
        [first] = _run(record, proc, now=t0, troubles=troubles)
        assert (first.state, first.kill) == ("red", False)
        assert troubles == [1], "the log opens on the first pass with trouble"
        [due] = _run(record, proc, now=t0 + RED_LIMIT)
        assert due.kill
        assert proc.wait(timeout=10) is not None
        state = backend_supervisor.read_state(
            record.with_name(backend_supervisor.STATE_NAME)
        )
        [kill] = state["kills"]
        assert (kill["pid"], kill["port"], kill["killed"]) == (proc.pid, PORT, True)
        assert str(PORT) not in state, "a terminated backend's clock is dropped"

    def test_a_dry_run_keeps_the_clock_and_kills_nothing(self, recorded):
        record, proc, started = recorded
        t0 = started + 10_000
        _run(record, proc, now=t0, dry_run=True)
        [due] = _run(record, proc, now=t0 + RED_LIMIT, dry_run=True)
        assert due.kill, "the verdict says it is due"
        assert proc.poll() is None, "and nothing was terminated"

    def test_a_record_naming_nothing_of_ours_is_a_no_op(self, tmp_path):
        record = tmp_path / "server.json"
        backend_registry.record_backend(
            record,
            port=PORT,
            version="2.1.9",
            pid=PID,
            source_fingerprint="a" * 64,
            display_context="win-session-1",
        )
        assert (
            backend_supervisor.run_pass(
                record,
                is_ours=lambda pid: False,
                healthy=lambda port: pytest.fail("never probed"),
                is_healthy=lambda port: False,
            )
            == []
        )

    def test_an_unreadable_state_file_starts_every_clock_afresh(self, recorded):
        record, proc, started = recorded
        record.with_name(backend_supervisor.STATE_NAME).write_text("{", "utf-8")
        [verdict] = _run(record, proc, now=started + 10_000)
        assert verdict.unusable_for == 0.0


WINDOW = backend_supervisor.STAND_DOWN_WINDOW_SECONDS
LIMIT = backend_supervisor.STAND_DOWN_KILLS
LOGGER = "stealth.supervisor"


def _state_file(record):
    return record.with_name(backend_supervisor.STATE_NAME)


def _kills_at(*ages, now, killed=True):
    """History entries for kills that happened ``age`` seconds before ``now``."""
    return [
        {"at": now - age, "port": 1, "pid": 1, "state": "red", "killed": killed}
        for age in ages
    ]


def _two_backends(tmp_path):
    """Two live backends of ours in one record: two real sleeping children."""
    record = tmp_path / "server.json"
    children = []
    for port in (PORT, PORT + 1):
        proc, started = _sleeper()
        children.append((port, proc, started))
        backend_registry.record_backend(
            record,
            port=port,
            version="2.1.9",
            pid=proc.pid,
            source_fingerprint="a" * 64,
            display_context=f"win-session-{port}",  # one entry per display context
        )
    return record, children


@pytest.fixture()
def two_backends_due(tmp_path, monkeypatch):
    """Two backends, each red for exactly RED_KILL_SECONDS at ``t0``, and a
    ``kill`` that records the pid it is asked about instead of terminating it."""
    record, children = _two_backends(tmp_path)
    t0 = max(started for _, _, started in children) + 10_000
    clocks = {
        str(port): {"pid": proc.pid, "started": started, "since": t0 - RED_LIMIT}
        for port, proc, started in children
    }
    asked = []
    monkeypatch.setattr(
        backend_supervisor,
        "kill",
        lambda seen, **_: asked.append(seen.pid) or True,
    )
    yield record, children, t0, clocks, asked
    for _, proc, _ in children:
        proc.kill()
        proc.wait(timeout=10)


def _pass_over(record, children, now, *, dry_run=False):
    pids = {proc.pid for _, proc, _ in children}
    return backend_supervisor.run_pass(
        record,
        is_ours=lambda pid: pid in pids,
        healthy=lambda port: False,
        is_healthy=lambda port: False,
        dry_run=dry_run,
        now=lambda: now,
    )


def _seed_state(record, clocks, kills):
    _state_file(record).write_text(json.dumps({**clocks, "kills": kills}), "utf-8")


class TestStandDown:
    """T-15466 (a): after 3 kills in 30 minutes the supervisor kills no more, says
    why at ERROR once per pass, and resumes by itself when the window has passed."""

    def test_three_kills_in_the_window_stop_all_killing(self, two_backends_due, caplog):
        record, children, t0, clocks, asked = two_backends_due
        _seed_state(record, clocks, _kills_at(60, 600, 1700, now=t0))
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            verdicts = _pass_over(record, children, t0)
        assert asked == [], "nothing was terminated"
        assert [(v.kill, v.stood_down) for v in verdicts] == [(False, True)] * 2
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, "ERROR once per pass, not once per backend"
        text = errors[0].getMessage()
        assert "3 backend kills in the last 30 minutes (limit 3)" in text
        assert "resumes by itself in 100s" in text  # 1700 s old: 1800 - 1700
        warnings = [r.getMessage() for r in caplog.records if r.levelno == 30]
        assert sum("NOT terminating it: standing down" in w for w in warnings) == 2

    def test_below_the_limit_nothing_changes_and_this_passs_kills_count(
        self, two_backends_due
    ):
        """Two kills in the window leave one more: the first backend takes it,
        so the second, in the same pass, stands down."""
        record, children, t0, clocks, asked = two_backends_due
        _seed_state(record, clocks, _kills_at(60, 600, now=t0))
        verdicts = _pass_over(record, children, t0)
        assert len(asked) == 1
        assert [v.stood_down for v in verdicts] == [False, True]
        assert verdicts[0].kill

    def test_the_window_boundary_is_exact(self, two_backends_due):
        """A kill exactly WINDOW old has left the window; one a second younger has
        not."""
        record, children, t0, clocks, asked = two_backends_due
        _seed_state(record, clocks, _kills_at(60, 600, WINDOW, now=t0))
        _pass_over(record, children, t0)
        assert len(asked) == 1, "two in the window: one more kill, then the limit"
        asked.clear()
        _seed_state(record, clocks, _kills_at(60, 600, WINDOW - 1, now=t0))
        _pass_over(record, children, t0)
        assert asked == []

    def test_it_resumes_by_itself_and_keeps_the_clocks_meanwhile(
        self, two_backends_due
    ):
        record, children, t0, clocks, asked = two_backends_due
        _seed_state(record, clocks, _kills_at(60, 600, 1700, now=t0))
        _pass_over(record, children, t0)
        assert asked == []
        kept = backend_supervisor.read_state(_state_file(record))
        assert {str(PORT), str(PORT + 1)} <= set(kept), "the clocks survive"
        # 101 s later the oldest kill is 1801 s old and has left the window; two
        # remain, so exactly one more is allowed: no operator, no state edit.
        _pass_over(record, children, t0 + 101)
        assert len(asked) == 1
        # Once every one of the original three has left it, the rest go too.
        _pass_over(record, children, t0 + WINDOW + 700)
        assert len(asked) == 2

    def test_a_kill_that_terminated_nothing_does_not_count(self, two_backends_due):
        record, children, t0, clocks, asked = two_backends_due
        history = _kills_at(60, 600, 1000, now=t0, killed=False)
        history += ["junk", {"at": "yesterday", "killed": True}, {"killed": True}]
        _seed_state(record, clocks, history)
        _pass_over(record, children, t0)
        assert len(asked) == 2

    def test_a_dry_run_reports_that_it_would_stand_down(self, two_backends_due):
        record, children, t0, clocks, asked = two_backends_due
        _seed_state(record, clocks, _kills_at(60, 600, 1000, now=t0))
        verdicts = _pass_over(record, children, t0, dry_run=True)
        assert asked == []
        assert all(v.stood_down and not v.kill for v in verdicts)

    def test_the_verb_says_it_is_standing_down(self):
        verdict = backend_supervisor.Verdict(
            PORT, PID, "red", 190.0, kill=False, stood_down=True
        )
        assert "standing down" in cli_supervise._line(verdict, dry_run=False)


def _write_browsers(record, owner_pid, entries):
    """Browsers in the registry beside ``record``: ``(instance_id, pid, dir,
    custom, auto_clone, owner)`` each, owner ``None`` meaning ``owner_pid``."""
    stamped = {
        instance_id: browser_pid_registry.with_owner(
            browser_pid_registry.new_entry(
                pid,
                create_time=1.0,
                user_data_dir=directory,
                uses_custom_data_dir=custom,
                auto_clone=clone,
            ),
            owner_pid if owner is None else owner,
            1.0,
        )
        for instance_id, pid, directory, custom, clone, owner in entries
    }
    browser_pid_registry.update_entries(
        record.with_name(browser_pid_registry.RECORD_NAME),
        lambda current: {**current, **stamped},
    )


SECRET_DIR = "C:\\Users\\secret-person\\Chrome Sessions\\fleet"
CLONE_DIR = "C:\\Users\\secret-person\\clones\\clone-9f8e"
DEAD_PID = 987_654


@pytest.fixture()
def lost_setup(recorded, monkeypatch):
    record, proc, started = recorded
    monkeypatch.setattr(
        backend_supervisor.psutil, "pid_exists", lambda pid: pid != DEAD_PID
    )
    _write_browsers(
        record,
        proc.pid,
        [
            ("inst-clone", 101, CLONE_DIR, True, True, None),
            ("inst-fleet", 102, SECRET_DIR, True, False, None),
            ("inst-gone", DEAD_PID, CLONE_DIR, True, True, None),
            ("inst-theirs", 103, CLONE_DIR, True, True, proc.pid + 1),
        ],
    )
    return record, proc, started


def _due(record, proc, started, **kwargs):
    t0 = started + 10_000
    _run(record, proc, now=t0)
    return _run(record, proc, now=t0 + RED_LIMIT, **kwargs)


class TestLostBrowsers:
    """T-15466 (b): the kill names which browsers it just lost."""

    def test_one_warning_names_the_clones_and_the_sessions(self, lost_setup, caplog):
        record, proc, started = lost_setup
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _due(record, proc, started)
        assert proc.wait(timeout=10) is not None
        [line] = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "terminated;" in r.getMessage()
        ]
        assert f"backend pid {proc.pid} on port {PORT} terminated;" in line
        assert "1 disposable clone(s) now lost [inst-clone]" in line
        assert "1 named session(s) the next backend re-attaches" in line
        assert "inst-fleet (session fleet)" in line
        assert "inst-gone" not in line, "a browser that is already gone is not lost"
        assert "inst-theirs" not in line, "another backend's browser is not ours"

    def test_it_carries_no_path_from_the_record(self, lost_setup, caplog):
        record, proc, started = lost_setup
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _due(record, proc, started)
        everything = " ".join(r.getMessage() for r in caplog.records)
        for secret in ("secret-person", "Chrome Sessions", "clones", "clone-9f8e"):
            assert secret not in everything
        assert "Users" not in everything

    def test_the_shared_profiles_directory_is_called_default(self):
        entry = {"user_data_dir": "/home/me/.stealth/sessions/master"}
        assert backend_supervisor._session_name(entry) == "default"
        assert backend_supervisor._session_name({}) == "unnamed"
        long_name = {"user_data_dir": "/x/" + "n" * 500}
        assert len(backend_supervisor._session_name(long_name)) == 64

    def test_a_backend_that_owned_nothing_logs_no_such_line(self, recorded, caplog):
        record, proc, started = recorded
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _due(record, proc, started)
        assert not [r for r in caplog.records if "terminated;" in r.getMessage()]

    def test_a_dry_run_or_a_refused_kill_loses_nothing_so_says_nothing(
        self, lost_setup, caplog, monkeypatch
    ):
        record, proc, started = lost_setup
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _due(record, proc, started, dry_run=True)
            monkeypatch.setattr(backend_supervisor, "kill", lambda *a, **k: False)
            _due(record, proc, started)
        assert not [r for r in caplog.records if "terminated;" in r.getMessage()]
        assert proc.poll() is None


class TestTheVerb:
    def test_supervise_is_one_verb_in_the_one_parser(self):
        args = cli.build_parser().parse_args(["supervise", "--dry-run", "--json"])
        assert (args.command, args.dry_run, args.json) == ("supervise", True, True)
        assert cli_supervise.DISPATCH["supervise"] is cli_supervise.cmd_supervise

    def test_cli_main_dispatches_it(self, monkeypatch):
        seen = []
        monkeypatch.setitem(
            cli_supervise.DISPATCH, "supervise", lambda args: seen.append(args) or 0
        )
        monkeypatch.setattr(cli, "sentry_init", lambda: None)
        assert cli.main(["supervise"]) == 0
        assert len(seen) == 1

    def test_the_verb_prints_one_line_per_backend(self, monkeypatch, capsys):
        verdict = backend_supervisor.Verdict(PORT, PID, "red", 190.0, kill=True)
        monkeypatch.setattr(backend_supervisor, "run_pass", lambda *a, **k: [verdict])
        args = cli.build_parser().parse_args(["supervise"])
        assert cli_supervise.cmd_supervise(args) == 0
        assert capsys.readouterr().out.strip() == (
            f"port {PORT}  pid {PID}  red  unusable for 190s  -> terminating"
        )

    def test_the_verb_runs_with_no_stdout_as_under_pythonw(self, monkeypatch):
        """The scheduled task runs it under pythonw.exe, which has no stdout;
        ``cli.main`` flushes stdout after every verb, which raised there (a plain
        ``status`` under pythonw exits 1, measured)."""
        monkeypatch.setattr(backend_supervisor, "run_pass", lambda *a, **k: [])
        monkeypatch.setattr(cli, "sentry_init", lambda: None)
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        assert cli.main(["supervise"]) == 0
        assert sys.stdout is not None

    def test_the_verb_never_imports_the_server(self):
        """A pass must never run orphan recovery or register tools."""
        code = (
            "import sys; from stealth_chrome_devtools_mcp import cli_supervise; "
            "from stealth_chrome_devtools_mcp.embedded import backend_supervisor, "
            "singleton; "
            "print('stealth_chrome_devtools_mcp.embedded.server' in sys.modules)"
        )
        out = subprocess.run(  # noqa: S603  PERMANENT(test child, fixed argv)
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout.strip()
        assert out == "False"
