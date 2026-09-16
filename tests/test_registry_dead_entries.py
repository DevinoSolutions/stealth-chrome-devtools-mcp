"""F-880 pins: a recorded backend that is DEAD is forgotten, and nothing else is.

Hermetic throughout. The record path is redirected at ``singleton.SERVER_STATE_
FILE`` — the one binding every reader resolves through — and the two witnesses
(``singleton._probe_port``, ``singleton._is_our_backend``) are patched by the
names the rest of the suite already patches, so no socket is opened, no port on
the developer's machine is contacted and no process is inspected or signalled.

The rule under test has TWO witnesses and the pins exist mostly to stop either
one being dropped: an entry is dead only when its port has no listener
(``down``) AND its recorded pid is not a backend of ours. A sibling recorded at
Popen time is ``down`` for the whole of its cold start, which is why the socket
alone may never decide this.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from fakes import v2_record
from stealth_chrome_devtools_mcp import cli
from stealth_chrome_devtools_mcp.embedded import (
    backend_liveness,
    backend_registry,
    singleton,
)

DEAD_PORT = 7169
OTHER_DEAD_PORT = 19222
LIVE_PORT = 52554


@pytest.fixture()
def record(tmp_path, monkeypatch):
    """Redirect the record at a tmp file and hand back a writer + reader pair."""
    path = tmp_path / "server.json"
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", path)

    def _write(state: dict) -> None:
        path.write_text(json.dumps(state), encoding="utf-8")

    _write.path = path  # type: ignore[attr-defined]
    return _write


def _entry(context: str, port, pid: int, version: str = "2.1.1") -> dict:
    return {
        "port": port,
        "version": version,
        "pid": pid,
        "source_fingerprint": "fp",
        "display_context": context,
    }


def _survey(entries, verdicts: dict, ours: set):
    return backend_liveness.survey(
        entries,
        probe=lambda port: verdicts[port],
        pid_is_ours=lambda pid: pid in ours,
    )


class TestDeadIsTwoWitnesses:
    """The rule itself, on a single entry, with both witnesses expressible."""

    def test_down_with_a_dead_pid_is_dead(self):
        surveyed = _survey(
            [_entry("win-session-2", DEAD_PORT, 89892)], {DEAD_PORT: "down"}, set()
        )
        assert [s.verdict for s in surveyed] == ["down"]
        assert [s.dead for s in surveyed] == [True]
        assert backend_liveness.dead_entries(surveyed) == [
            _entry("win-session-2", DEAD_PORT, 89892)
        ]

    def test_down_with_a_live_backend_pid_is_not_dead(self):
        """A backend is recorded at Popen time, BEFORE it binds its socket, so a
        sibling mid cold start reads `down` while its process is alive and about
        to serve. Forgetting it would race every cold start on the machine."""
        surveyed = _survey(
            [_entry("win-session-2", DEAD_PORT, 89892)], {DEAD_PORT: "down"}, {89892}
        )
        assert [s.dead for s in surveyed] == [False]
        assert backend_liveness.dead_entries(surveyed) == []

    def test_a_wedged_entry_is_never_dead(self):
        """A wedged backend holds its port and will be evicted and respawned;
        its record is how `_terminate_backend` finds the pid to kill."""
        surveyed = _survey(
            [_entry("headless", OTHER_DEAD_PORT, 67720)],
            {OTHER_DEAD_PORT: "wedged"},
            set(),
        )
        assert [s.verdict for s in surveyed] == ["wedged"]
        assert [s.dead for s in surveyed] == [False]

    def test_a_responsive_entry_is_never_dead(self):
        surveyed = _survey(
            [_entry("win-session-1", LIVE_PORT, 136672)],
            {LIVE_PORT: "responsive"},
            set(),
        )
        assert [s.dead for s in surveyed] == [False]

    def test_an_entry_with_no_usable_port_is_reported_not_forgotten(self):
        """The record tolerates hand-editing, so `port` can be a string. There
        is nothing to probe, so there is no evidence of death either."""
        surveyed = _survey([_entry("win-session-1", "52554", 136672)], {}, set())
        assert [s.verdict for s in surveyed] == [backend_liveness.NO_PORT]
        assert [s.dead for s in surveyed] == [False]


class TestForgetEntries:
    """The WRITE half: `backend_registry.forget_entries`, the read-merge-write."""

    def test_forgets_only_the_named_entries(self, record):
        record(
            v2_record(
                win_session_2=_entry("win-session-2", DEAD_PORT, 89892),
                win_session_1=_entry("win-session-1", LIVE_PORT, 136672, "2.1.6"),
            )
        )
        forgotten = backend_registry.forget_entries(
            record.path, [_entry("win-session-2", DEAD_PORT, 89892)]
        )
        assert forgotten == ["win-session-2"]
        assert [
            e["display_context"] for e in backend_registry.read_backends(record.path)
        ] == ["win-session-1"]

    def test_a_context_re_recorded_since_the_probe_survives(self, record):
        """The lost update this merge exists to prevent: we probed one entry and
        found it dead, but by the time we write, that context has been recorded
        again on a NEW port with a NEW pid. It is not the entry we condemned."""
        record(v2_record(win_session_2=_entry("win-session-2", DEAD_PORT, 89892)))
        dead = [_entry("win-session-2", DEAD_PORT, 89892)]
        # Another proxy re-records the same context between the probe and write.
        backend_registry.record_backend(
            record.path,
            port=40404,
            version="2.1.6",
            pid=1234,
            source_fingerprint="fp",
            display_context="win-session-2",
        )

        assert backend_registry.forget_entries(record.path, dead) == []
        [survivor] = backend_registry.read_backends(record.path)
        assert (survivor["port"], survivor["pid"]) == (40404, 1234)

    def test_forgetting_the_last_entry_leaves_an_empty_readable_record(self, record):
        record(v2_record(win_session_2=_entry("win-session-2", DEAD_PORT, 89892)))

        backend_registry.forget_entries(
            record.path, [_entry("win-session-2", DEAD_PORT, 89892)]
        )

        assert record.path.exists(), "unlinking the file is clear_record's job"
        assert backend_registry.read_backends(record.path) == []

    def test_nothing_dead_writes_nothing(self, record):
        record(v2_record(win_session_1=_entry("win-session-1", LIVE_PORT, 136672)))
        before = record.path.stat().st_mtime_ns

        assert backend_registry.forget_entries(record.path, []) == []
        assert record.path.stat().st_mtime_ns == before


class TestForgetDead:
    """The composition, end to end, over the record F-880 §1 observed."""

    def _observed(self, record):
        record(
            v2_record(
                win_session_2=_entry("win-session-2", DEAD_PORT, 89892, "2.1.1"),
                headless=_entry("headless", OTHER_DEAD_PORT, 67720, "2.1.3"),
                win_session_1=_entry("win-session-1", LIVE_PORT, 136672, "2.1.6"),
            )
        )

    def _forget(self, path, verdicts, ours=frozenset()):
        return backend_liveness.forget_dead(
            path,
            probe=lambda port: verdicts[port],
            pid_is_ours=lambda pid: pid in ours,
        )

    def test_a_foreign_display_contexts_dead_entry_is_forgotten(self, record):
        """Death is not a property of a display context. Adoption's asymmetry
        protects a foreign desktop's LIVE backend; it says nothing about one
        whose process is gone and whose port has no listener."""
        self._observed(record)

        forgotten = self._forget(
            record.path,
            {DEAD_PORT: "down", OTHER_DEAD_PORT: "down", LIVE_PORT: "responsive"},
        )

        assert sorted(forgotten) == ["headless", "win-session-2"]

    def test_the_live_entry_survives_its_dead_siblings(self, record):
        self._observed(record)

        self._forget(
            record.path,
            {DEAD_PORT: "down", OTHER_DEAD_PORT: "down", LIVE_PORT: "responsive"},
        )

        [survivor] = backend_registry.read_backends(record.path)
        assert survivor["display_context"] == "win-session-1"
        assert survivor["port"] == LIVE_PORT

    def test_a_wedged_sibling_is_left_alone(self, record):
        self._observed(record)

        forgotten = self._forget(
            record.path,
            {DEAD_PORT: "down", OTHER_DEAD_PORT: "wedged", LIVE_PORT: "responsive"},
        )

        assert forgotten == ["win-session-2"]
        assert sorted(
            e["display_context"] for e in backend_registry.read_backends(record.path)
        ) == ["headless", "win-session-1"]


class TestColdStartPrunes:
    """The ONE automatic caller: the cold-start lock holder, the one writer of
    this record that holds `_exclusive_lock`."""

    def test_the_lock_holder_forgets_dead_entries(self, record, monkeypatch):
        record(
            v2_record(
                win_session_2=_entry("win-session-2", DEAD_PORT, 89892),
                win_session_1=_entry("win-session-1", LIVE_PORT, 136672, "2.1.6"),
            )
        )
        monkeypatch.setattr(
            singleton,
            "_probe_port",
            lambda port: "responsive" if port == LIVE_PORT else "down",
        )
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: False)
        # The cold start itself is not under test: stop it after the prune by
        # taking the "already up on the port we were handed" early return.
        monkeypatch.setattr(singleton, "_find_running_server", lambda: LIVE_PORT)

        singleton._start_backend_holding_lock(LIVE_PORT)

        assert [
            e["display_context"] for e in backend_registry.read_backends(record.path)
        ] == ["win-session-1"]

    def test_a_lost_lock_race_never_writes(self, record, monkeypatch):
        """Another process owns startup, so this one has no right to the file."""
        state = v2_record(win_session_2=_entry("win-session-2", DEAD_PORT, 89892))
        record(state)
        monkeypatch.setattr(singleton, "_probe_port", lambda port: "down")
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: False)
        monkeypatch.setattr(singleton, "_exclusive_lock", lambda: _no_lock())

        singleton._start_backend_holding_lock(DEAD_PORT)

        assert json.loads(record.path.read_text()) == state


def _no_lock():
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        yield False

    return _cm()


@pytest.fixture()
def fake_clone_storage(tmp_path):
    cs = MagicMock()
    cs.default_session_root.return_value = tmp_path
    cs.clone_root_dir.return_value = tmp_path / "clones"
    cs.clone_storage_cap_bytes.return_value = 1024**3
    cs.browser_session_storage_cap_bytes.return_value = 1024**3
    cs._idle_autoclones_over_cap.return_value = []
    cs._named_profiles_over_session_cap.return_value = []
    return cs


class _CleanupArgs:
    apply = False
    clone_cap_gb = None
    browser_session_cap_gb = None


class TestCliDeadRecords:
    """`cleanup` writes (with --apply), `doctor` only reports — and both reach
    the same home rather than sweeping the record a second way."""

    @pytest.fixture(autouse=True)
    def _probes(self, monkeypatch):
        monkeypatch.setattr(
            singleton,
            "_probe_port",
            lambda port: "responsive" if port == LIVE_PORT else "down",
        )
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: False)

    @pytest.fixture()
    def observed(self, record):
        record(
            v2_record(
                win_session_2=_entry("win-session-2", DEAD_PORT, 89892, "2.1.1"),
                win_session_1=_entry("win-session-1", LIVE_PORT, 136672, "2.1.6"),
            )
        )
        return record

    def test_cleanup_dry_run_names_them_and_forgets_nothing(
        self, observed, fake_clone_storage, capsys
    ):
        args = _CleanupArgs()
        with patch.object(cli, "_clone_storage", return_value=fake_clone_storage):
            cli._cmd_cleanup(args)
        out = capsys.readouterr().out

        assert "backend records: 2 recorded, 1 dead (win-session-2)" in out
        assert len(backend_registry.read_backends(observed.path)) == 2

    def test_cleanup_apply_forgets_them(self, observed, fake_clone_storage, capsys):
        args = _CleanupArgs()
        args.apply = True
        with patch.object(cli, "_clone_storage", return_value=fake_clone_storage):
            cli._cmd_cleanup(args)
        out = capsys.readouterr().out

        assert "backend records: forgot 1 dead (win-session-2)" in out
        assert [
            e["display_context"] for e in backend_registry.read_backends(observed.path)
        ] == ["win-session-1"]

    def test_cleanup_says_nothing_when_no_record_is_dead(
        self, record, fake_clone_storage, capsys
    ):
        record(v2_record(win_session_1=_entry("win-session-1", LIVE_PORT, 136672)))
        with patch.object(cli, "_clone_storage", return_value=fake_clone_storage):
            cli._cmd_cleanup(_CleanupArgs())
        out = capsys.readouterr().out

        assert "backend records: 1 recorded, 0 dead" in out
        assert "forgot" not in out

    def test_doctor_names_them_and_forgets_nothing(
        self, observed, fake_clone_storage, capsys
    ):
        with (
            patch.object(cli, "_clone_storage", return_value=fake_clone_storage),
            patch.object(cli, "_find_chrome", return_value="/usr/bin/chrome"),
            patch.object(
                singleton,
                "_probe_backend_status",
                return_value=("responsive", LIVE_PORT),
            ),
            patch.object(singleton, "_backend_pid_on_port", return_value=None),
            patch.object(singleton, "_port_is_foreign_held", return_value=False),
        ):
            cli._cmd_doctor(None)
        out = capsys.readouterr().out

        assert "down  (can show windows)  (dead record)" in out
        assert "1 dead record(s) (win-session-2)" in out
        assert "cleanup --apply" in out
        assert len(backend_registry.read_backends(observed.path)) == 2
