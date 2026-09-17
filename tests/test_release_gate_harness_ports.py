"""The port an isolated gate workspace is allowed to bind.

``release_gate_harness._pick_free_port`` used to return whatever loopback port
the OS assigned. On a developer machine the ephemeral range covers the
product's default singleton port and every port a live backend has recorded in
the REAL ``~/.stealth-mcp/server.json``, so a workspace that was supposed to be
isolated could land on one of them: with the real backend down, a throwaway
backend would squat its recorded port, be adopted by the developer's next real
proxy, and die at workspace teardown — the lifecycle suite handing out the very
``CONNECTION_CLOSED`` it exists to eliminate. The pick now refuses those ports
and retries; these pins hold that behaviour without a socket or a real home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import release_gate_harness as harness
from release_gate_harness import (
    _PORT_PICK_ATTEMPTS,
    DEFAULT_SINGLETON_PORT,
    _backend_entries,
    _backend_pid_from_state,
    _pick_free_port,
    _recorded_ports,
    _reserved_ports,
)


def _record(home: Path, payload: object) -> Path:
    state_dir = home / ".stealth-mcp"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "server.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _scripted_picks(monkeypatch, picks: list[int]) -> list[int]:
    """Replace the OS pick with a scripted sequence; returns the consumed log."""
    consumed: list[int] = []
    remaining = list(picks)

    def pick() -> int:
        port = remaining.pop(0)
        consumed.append(port)
        return port

    monkeypatch.setattr(harness, "_os_assigned_port", pick)
    return consumed


V2 = {
    "schema": 2,
    "backends": {
        "win-session-1": {"pid": 4242, "port": 52554},
        "headless": {"pid": 4343, "port": 7169},
        "unverified": {"pid": 4444, "port": "not-a-port"},
    },
}


class TestRecordedPorts:
    def test_v2_record_names_every_context_port(self, tmp_path):
        _record(tmp_path, V2)
        assert _recorded_ports(tmp_path) == frozenset({52554, 7169})

    def test_v1_record_names_its_one_port(self, tmp_path):
        _record(tmp_path, {"pid": 99, "port": 41000})
        assert _recorded_ports(tmp_path) == frozenset({41000})

    @pytest.mark.parametrize("payload", ["[]", "not json", '{"backends": 3}'])
    def test_missing_or_malformed_record_is_empty(self, tmp_path, payload):
        (tmp_path / ".stealth-mcp").mkdir()
        (tmp_path / ".stealth-mcp" / "server.json").write_text(
            payload, encoding="utf-8"
        )
        assert _recorded_ports(tmp_path) == frozenset()
        assert _backend_entries(tmp_path / "absent") == []

    def test_pid_reader_shares_the_parse(self, tmp_path):
        """One parse of the record: the pid reader reads through it too."""
        _record(tmp_path, V2)
        assert _backend_pid_from_state(tmp_path) == 4242
        _record(tmp_path, {"pid": 7, "port": 1})
        assert _backend_pid_from_state(tmp_path) == 7
        assert _backend_pid_from_state(tmp_path / "absent") is None


class TestReservedPorts:
    def test_default_port_is_always_reserved(self, tmp_path):
        assert DEFAULT_SINGLETON_PORT == 19222
        assert _reserved_ports(tmp_path) == frozenset({19222})

    def test_real_record_ports_join_the_default(self, tmp_path):
        _record(tmp_path, V2)
        assert _reserved_ports(tmp_path) == frozenset({19222, 52554, 7169})


class TestPickFreePort:
    def test_first_safe_pick_is_returned(self, tmp_path, monkeypatch):
        consumed = _scripted_picks(monkeypatch, [50001])
        assert _pick_free_port(real_home=tmp_path) == 50001
        assert consumed == [50001]

    def test_default_port_is_skipped(self, tmp_path, monkeypatch):
        consumed = _scripted_picks(monkeypatch, [19222, 50002])
        assert _pick_free_port(real_home=tmp_path) == 50002
        assert consumed == [19222, 50002]

    def test_every_port_the_real_record_names_is_skipped(self, tmp_path, monkeypatch):
        _record(tmp_path, V2)
        consumed = _scripted_picks(monkeypatch, [52554, 7169, 19222, 50003])
        assert _pick_free_port(real_home=tmp_path) == 50003
        assert consumed == [52554, 7169, 19222, 50003]

    def test_v1_recorded_port_is_skipped(self, tmp_path, monkeypatch):
        _record(tmp_path, {"pid": 1, "port": 41000})
        _scripted_picks(monkeypatch, [41000, 41001])
        assert _pick_free_port(real_home=tmp_path) == 41001

    def test_exhaustion_raises_rather_than_guessing(self, tmp_path, monkeypatch):
        consumed = _scripted_picks(monkeypatch, [19222] * (_PORT_PICK_ATTEMPTS + 5))
        with pytest.raises(RuntimeError, match="reserved set"):
            _pick_free_port(real_home=tmp_path)
        assert len(consumed) == _PORT_PICK_ATTEMPTS

    def test_default_home_is_the_real_one(self, tmp_path, monkeypatch):
        """No ``real_home`` means the developer's own record, not a test dir."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        _record(tmp_path, {"pid": 1, "port": 43210})
        _scripted_picks(monkeypatch, [43210, 43211])
        assert _pick_free_port() == 43211
