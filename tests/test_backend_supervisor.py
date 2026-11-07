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

import subprocess
import sys

import psutil
import pytest

from stealth_chrome_devtools_mcp import cli, cli_supervise
from stealth_chrome_devtools_mcp.embedded import (
    backend_liveness,
    backend_registry,
    backend_supervisor,
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
