"""F-886 — a backend that is still serving live browsers is never evicted.

Hermetic pins for the rule (``backend_eviction``) and for the two places
``singleton`` asks it: the BIND site (``_select_backend_port`` steps around a
protected port) and the KILL site (``_clear_stale_backend`` refuses, and
``_start_backend_holding_lock`` then spawns nothing). The real-fleet proof is
``tests/test_e2e_lifecycle_resilience.py``'s S5 pair; these are what CI runs
without a fleet, and what names the exact clause when one regresses.

Every probe is injected, exactly as the leaf takes them, so no test here touches
psutil's process table, a socket, or the developer's ``~/.stealth-mcp``.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_eviction,
    backend_registry,
    browser_pid_registry,
    singleton,
)

OUR_VERSION = "2.1.9"
OUR_DIGEST = "digest-ours"
STRANGER_DIGEST = "digest-theirs"
STRANGER_PID = 4242
BROWSER_PID = 9001
PORT = 40123


def _entry(pid=STRANGER_PID, digest=STRANGER_DIGEST, version=OUR_VERSION):
    return {
        "port": PORT,
        "version": version,
        "pid": pid,
        "source_fingerprint": digest,
        "display_context": "win-session-1",
    }


def _write_browsers(state_dir, *, owner_pid=STRANGER_PID, pids=(BROWSER_PID,)):
    """A ``browser_pids.json`` whose every entry is owned by ``owner_pid``."""
    entries = {
        f"inst-{pid}": browser_pid_registry.with_owner(
            browser_pid_registry.new_entry(
                pid,
                create_time=1.0,
                user_data_dir=None,
                uses_custom_data_dir=None,
                auto_clone=False,
            ),
            owner_pid,
            1.0,
        )
        for pid in pids
    }
    path = state_dir / browser_pid_registry.RECORD_NAME
    path.write_text(json.dumps({"browser_processes": entries}), encoding="utf-8")


def _is_ours(pid):
    return pid == STRANGER_PID


def _identity_matches(entry):
    return (entry or {}).get("source_fingerprint") == OUR_DIGEST


class TestOwnedBrowsers:
    def test_lists_only_this_owners_running_browsers(self, tmp_path):
        _write_browsers(tmp_path, pids=(1, 2, 3))
        _write_browsers_more = {
            "other": browser_pid_registry.with_owner(
                browser_pid_registry.new_entry(
                    7,
                    create_time=1.0,
                    user_data_dir=None,
                    uses_custom_data_dir=None,
                    auto_clone=False,
                ),
                999,
                1.0,
            )
        }
        path = tmp_path / browser_pid_registry.RECORD_NAME
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["browser_processes"].update(_write_browsers_more)
        path.write_text(json.dumps(raw), encoding="utf-8")

        got = backend_eviction.owned_browsers(
            tmp_path, STRANGER_PID, is_running=lambda pid: pid != 2
        )
        assert sorted(got) == [1, 3]

    def test_a_non_int_owner_owns_nothing(self, tmp_path):
        _write_browsers(tmp_path)
        assert (
            backend_eviction.owned_browsers(tmp_path, None, is_running=lambda p: True)
            == []
        )
        assert (
            backend_eviction.owned_browsers(tmp_path, "42", is_running=lambda p: True)
            == []
        )

    def test_an_absent_record_is_no_browsers(self, tmp_path):
        assert (
            backend_eviction.owned_browsers(
                tmp_path, STRANGER_PID, is_running=lambda p: True
            )
            == []
        )

    def test_a_legacy_unowned_entry_protects_nobody(self, tmp_path):
        """A 2.0.3 entry carries no owner keys; it belongs to no live backend."""
        path = tmp_path / browser_pid_registry.RECORD_NAME
        path.write_text(json.dumps({"browser_processes": {"x": {"pid": 5}}}))
        assert (
            backend_eviction.owned_browsers(
                tmp_path, STRANGER_PID, is_running=lambda p: True
            )
            == []
        )


class TestProtected:
    """The four conditions, each the ONLY thing that flips the verdict."""

    def _protected(self, tmp_path, entry, **overrides):
        kwargs = {
            "state_dir": tmp_path,
            "identity_matches": _identity_matches,
            "is_ours": _is_ours,
            "is_running": lambda pid: True,
        }
        kwargs.update(overrides)
        return backend_eviction.protected(entry, **kwargs)

    def test_a_strangers_live_backend_with_a_live_browser_is_protected(self, tmp_path):
        _write_browsers(tmp_path)
        assert self._protected(tmp_path, _entry()) == [BROWSER_PID]

    def test_nothing_recorded_protects_nothing(self, tmp_path):
        _write_browsers(tmp_path)
        assert self._protected(tmp_path, None) == []

    def test_our_own_identity_is_never_protected(self, tmp_path):
        """A wedged backend of OUR OWN must stay evictable — the restart verb
        and the cold-start lock both exist to replace it."""
        _write_browsers(tmp_path)
        assert self._protected(tmp_path, _entry(digest=OUR_DIGEST)) == []

    def test_a_dead_or_recycled_pid_protects_nothing(self, tmp_path):
        _write_browsers(tmp_path)
        assert self._protected(tmp_path, _entry(), is_ours=lambda pid: False) == []

    def test_a_non_int_pid_protects_nothing(self, tmp_path):
        _write_browsers(tmp_path)
        assert self._protected(tmp_path, _entry(pid="4242")) == []

    def test_a_live_backend_with_no_live_browser_is_evictable(self, tmp_path):
        """The upgrade flow (#14): an idle stale backend is still replaced."""
        _write_browsers(tmp_path)
        assert self._protected(tmp_path, _entry(), is_running=lambda pid: False) == []

    def test_a_live_backend_with_no_recorded_browser_is_evictable(self, tmp_path):
        assert self._protected(tmp_path, _entry()) == []


class TestClearStale:
    def test_reusable_clears_nothing_and_permits(self):
        killed = []
        spared = backend_eviction.clear_stale(
            PORT,
            reusable=lambda: True,
            protecting=lambda: [BROWSER_PID],
            terminate_backend=lambda: killed.append(PORT),
        )
        assert spared == [] and killed == []

    def test_protected_refuses_and_logs_the_count(self, caplog):
        killed = []
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            spared = backend_eviction.clear_stale(
                PORT,
                reusable=lambda: False,
                protecting=lambda: [1, 2],
                terminate_backend=lambda: killed.append(PORT),
            )
        assert spared == [1, 2]
        assert killed == []
        [rec] = [r for r in caplog.records if "refusing to evict" in r.getMessage()]
        assert rec.levelno == logging.WARNING
        assert f"port {PORT}" in rec.getMessage()
        assert "2 live browser(s)" in rec.getMessage()

    def test_otherwise_terminates_and_permits(self):
        killed = []
        spared = backend_eviction.clear_stale(
            PORT,
            reusable=lambda: False,
            protecting=list,
            terminate_backend=lambda: killed.append(PORT),
        )
        assert spared == [] and killed == [PORT]


class TestSteppingAside:
    def test_nothing_spared_is_no_step_and_no_line(self, caplog):
        with caplog.at_level(logging.INFO, logger="stealth.proxy"):
            assert backend_eviction.stepping_aside(PORT, []) is False
        assert not [r for r in caplog.records if "F-886" in r.getMessage()]

    def test_spared_is_a_step_with_the_explaining_line(self, caplog):
        with caplog.at_level(logging.INFO, logger="stealth.proxy"):
            assert backend_eviction.stepping_aside(PORT, [1]) is True
        [rec] = [r for r in caplog.records if "F-886" in r.getMessage()]
        assert rec.levelno == logging.INFO
        assert "spawning ours beside it" in rec.getMessage()


class TestTerminateHonoursTheInjectedResolver:
    def test_the_callers_pid_on_port_decides_what_dies(self, monkeypatch):
        """``singleton._backend_pid_on_port`` is a patch surface; the leaf must
        ask the caller's binding, never its own ``pid_on_port``."""
        from unittest.mock import MagicMock

        proc = MagicMock()
        monkeypatch.setattr(
            backend_eviction.psutil, "Process", MagicMock(return_value=proc)
        )
        got = backend_eviction.terminate(
            PORT,
            pid_on_port=lambda port: 777,
            recorded_pid=None,
            is_ours=lambda pid: False,
            is_healthy=lambda port: False,
        )
        assert got is True
        backend_eviction.psutil.Process.assert_called_once_with(777)
        proc.terminate.assert_called_once()

    def test_nothing_identifiable_kills_nothing(self, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr(backend_eviction.psutil, "Process", MagicMock())
        got = backend_eviction.terminate(
            PORT,
            pid_on_port=lambda port: None,
            recorded_pid=555,
            is_ours=lambda pid: False,
            is_healthy=lambda port: False,
        )
        assert got is False
        backend_eviction.psutil.Process.assert_not_called()


# ── The two singleton sites ──────────────────────────────────────────────────
@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(
        singleton, "LOCK_FILE", tmp_path / "singleton.lock", raising=False
    )
    monkeypatch.setattr(singleton, "_server_version", lambda: OUR_VERSION)
    monkeypatch.setattr(singleton, "_source_fingerprint", lambda: OUR_DIGEST)
    monkeypatch.setattr(
        singleton.display_context, "display_context", lambda: "win-session-1"
    )
    return tmp_path


def _record_stranger(tmp_path, *, digest=STRANGER_DIGEST, version=OUR_VERSION):
    backend_registry.record_backend(
        tmp_path / "server.json",
        port=PORT,
        version=version,
        pid=STRANGER_PID,
        source_fingerprint=digest,
        display_context="win-session-1",
    )


class TestBindSite:
    """``_select_backend_port`` steps around a protected port."""

    @pytest.fixture(autouse=True)
    def _probes(self, monkeypatch):
        monkeypatch.setattr(singleton, "_is_our_backend", _is_ours)
        monkeypatch.setattr(singleton.psutil, "pid_exists", lambda pid: True)
        # The port is OUR backend's, not foreign-held; no real socket is asked.
        monkeypatch.setattr(singleton, "_port_is_foreign_held", lambda port: False)

    def test_a_strangers_serving_backend_forces_a_fresh_port(
        self, isolated_state, monkeypatch, caplog
    ):
        _record_stranger(isolated_state)
        _write_browsers(isolated_state)
        picked = {}
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.proxy_forwarder.bindable_port",
            lambda target, force_new: (
                picked.update(target=target, force_new=force_new)
                or (target if not force_new else target + 1)
            ),
        )
        with caplog.at_level(logging.INFO, logger="stealth.proxy"):
            got = singleton._select_backend_port(PORT)
        assert picked == {"target": PORT, "force_new": True}
        assert got == PORT + 1
        assert any("F-886" in r.getMessage() for r in caplog.records)

    def test_a_strangers_idle_backend_keeps_the_port_to_be_evicted(
        self, isolated_state, monkeypatch
    ):
        _record_stranger(isolated_state)  # no browsers recorded
        picked = {}
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.proxy_forwarder.bindable_port",
            lambda target, force_new: picked.update(force_new=force_new) or target,
        )
        assert singleton._select_backend_port(PORT) == PORT
        assert picked == {"force_new": False}

    def test_our_own_serving_backend_keeps_the_port_so_restart_can_replace_it(
        self, isolated_state, monkeypatch
    ):
        """Identity-gated by construction: `restart_backend` seeds selection
        and terminates exactly what it gets back."""
        _record_stranger(isolated_state, digest=OUR_DIGEST)
        _write_browsers(isolated_state)
        picked = {}
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.proxy_forwarder.bindable_port",
            lambda target, force_new: picked.update(force_new=force_new) or target,
        )
        assert singleton._select_backend_port(PORT) == PORT
        assert picked == {"force_new": False}


class TestKillSite:
    """``_clear_stale_backend`` refuses, and the lock path spawns nothing."""

    @pytest.fixture(autouse=True)
    def _probes(self, monkeypatch):
        monkeypatch.setattr(singleton, "_is_our_backend", _is_ours)
        monkeypatch.setattr(singleton.psutil, "pid_exists", lambda pid: True)
        monkeypatch.setattr(
            singleton, "_same_identity_backend_ready", lambda port, **kw: False
        )

    def test_refuses_to_terminate_a_serving_stranger(self, isolated_state, monkeypatch):
        _record_stranger(isolated_state)
        _write_browsers(isolated_state)
        killed = []
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: killed.append(port)
        )
        assert singleton._clear_stale_backend(PORT) == [BROWSER_PID]
        assert killed == []

    def test_terminates_an_idle_stranger(self, isolated_state, monkeypatch):
        _record_stranger(isolated_state)
        killed = []
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: killed.append(port)
        )
        assert singleton._clear_stale_backend(PORT) == []
        assert killed == [PORT]

    def test_the_lock_path_spawns_nothing_when_refused(
        self, isolated_state, monkeypatch
    ):
        @contextmanager
        def fake_lock():
            yield True

        _record_stranger(isolated_state)
        _write_browsers(isolated_state)
        calls = []
        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: calls.append(("kill", port))
        )
        monkeypatch.setattr(
            singleton,
            "_start_server_process",
            lambda port: calls.append(("start", port)),
        )
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)

        singleton._start_backend_holding_lock(PORT)

        assert calls == []

    def test_the_lock_path_still_evicts_an_idle_stranger(
        self, isolated_state, monkeypatch
    ):
        @contextmanager
        def fake_lock():
            yield True

        _record_stranger(isolated_state)
        calls = []
        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: calls.append(("kill", port))
        )
        monkeypatch.setattr(
            singleton,
            "_start_server_process",
            lambda port: calls.append(("start", port)),
        )
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)

        singleton._start_backend_holding_lock(PORT)

        assert calls == [("kill", PORT), ("start", PORT)]


class TestStopForgetsOneEntry:
    """``stop_backend`` forgets the ENTRY it stopped, not the whole context —
    a sibling identity on the same desktop stays recorded."""

    def test_the_sibling_survives_a_stop(self, isolated_state, monkeypatch):
        @contextmanager
        def fake_lock():
            yield True

        record = isolated_state / "server.json"
        backend_registry.record_backend(
            record,
            port=PORT,
            version=OUR_VERSION,
            pid=1,
            source_fingerprint="a",
            display_context="win-session-1",
        )
        backend_registry.record_backend(
            record,
            port=PORT + 1,
            version=OUR_VERSION,
            pid=2,
            source_fingerprint="b",
            display_context="win-session-1",
        )
        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(
            singleton, "_probe_backend_status", lambda: ("responsive", PORT)
        )
        monkeypatch.setattr(singleton, "_terminate_backend", lambda port: True)

        assert singleton.stop_backend() == ("stopped", 1)
        assert [
            (e["port"], e["pid"]) for e in backend_registry.read_backends(record)
        ] == [(PORT + 1, 2)]


OUR_PORT = 40200
OUR_PID = 4343


def _record_ours(tmp_path):
    """OUR backend, recorded SECOND under the SAME display context.

    Second is the point: ``read_backends`` preserves insertion order, so on this
    machine "the first entry for this context" is the stranger's by construction
    — we stepped aside from it, which is why it was there first.
    """
    backend_registry.record_backend(
        tmp_path / "server.json",
        port=OUR_PORT,
        version=OUR_VERSION,
        pid=OUR_PID,
        source_fingerprint=OUR_DIGEST,
        display_context="win-session-1",
    )


def _is_ours_either(pid):
    """Both recorded backends are processes of OURS in the ``_is_our_backend``
    sense — that predicate asks "did this tool start it", not "is it my build".
    """
    return pid in (STRANGER_PID, OUR_PID)


class TestRestartOnATwoIdentityDesktop:
    """The desktop F-886 deliberately creates: a stranger's backend we stepped
    aside from, and ours beside it, under ONE display context.

    ``restart`` has to keep reaching OUR backend there. It selects the port it
    will terminate, so a selection that answers the stranger's port makes the
    verb terminate nothing of ours and spawn a THIRD backend on an OS-assigned
    one — and ``record_backend``'s supersede-by-(context, identity) rule then
    drops our wedged backend's entry, so ``doctor``/``status``/``cleanup`` stop
    seeing it too. Nothing would be left that reaches it.

    The stranger is given a live browser in every case here, because that is
    what makes it protected and is therefore the state that used to divert
    selection away from us.
    """

    @pytest.fixture(autouse=True)
    def _probes(self, monkeypatch):
        monkeypatch.setattr(singleton, "_is_our_backend", _is_ours_either)
        monkeypatch.setattr(singleton.psutil, "pid_exists", lambda pid: True)
        monkeypatch.setattr(singleton, "_port_is_foreign_held", lambda port: False)

    def test_selection_prefers_our_own_entry_over_the_strangers_first_one(
        self, isolated_state, monkeypatch
    ):
        _record_stranger(isolated_state)
        _record_ours(isolated_state)
        _write_browsers(isolated_state)
        picked = {}
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.proxy_forwarder.bindable_port",
            lambda target, force_new: (
                picked.update(target=target, force_new=force_new) or target
            ),
        )
        assert singleton._select_backend_port(PORT) == OUR_PORT
        assert picked == {"target": OUR_PORT, "force_new": False}

    def test_restart_replaces_our_backend_and_leaves_the_stranger_alone(
        self, isolated_state, monkeypatch
    ):
        @contextmanager
        def fake_lock():
            yield True

        _record_stranger(isolated_state)
        _record_ours(isolated_state)
        _write_browsers(isolated_state)
        calls = []
        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.proxy_forwarder.bindable_port",
            lambda target, force_new: target + 1 if force_new else target,
        )
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: calls.append(("kill", port))
        )
        monkeypatch.setattr(
            singleton,
            "_start_server_process",
            lambda port: calls.append(("start", port)),
        )
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)
        monkeypatch.setattr(singleton, "_probe_port", lambda port: "responsive")

        status, _pid = singleton.restart_backend()

        assert calls == [("kill", OUR_PORT), ("start", OUR_PORT)]
        assert status == "responsive"

    def test_a_context_holding_only_a_stranger_still_steps_aside(
        self, isolated_state, monkeypatch
    ):
        """The half that must NOT change: with no entry of ours to prefer, the
        seed falls back to the first entry and the protection still diverts us.
        """
        _record_stranger(isolated_state)
        _write_browsers(isolated_state)
        picked = {}
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.proxy_forwarder.bindable_port",
            lambda target, force_new: (
                picked.update(target=target, force_new=force_new) or target + 1
            ),
        )
        assert singleton._select_backend_port(PORT) == PORT + 1
        assert picked == {"target": PORT, "force_new": True}


class TestTheReportNamesTheDecisionTaken:
    """F-886 review, F4: the F-827 eviction report used to be shipped BEFORE
    ``_clear_stale_backend`` decided, so the refusal path — the one the whole
    fix exists for — told the durable log and Sentry that a backend still
    running had been evicted.

    Both cases here are a SOURCE CHANGE (same version, different digest), which
    is the only condition that reports at all; what differs between them is
    whether the stranger owns a live browser.
    """

    @pytest.fixture(autouse=True)
    def _probes(self, monkeypatch):
        monkeypatch.setattr(singleton, "_is_our_backend", _is_ours)
        monkeypatch.setattr(singleton.psutil, "pid_exists", lambda pid: True)
        monkeypatch.setattr(
            singleton, "_same_identity_backend_ready", lambda port, **kw: False
        )
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(singleton, "_terminate_backend", lambda port: True)
        monkeypatch.setattr(singleton, "_start_server_process", lambda port: None)
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)

    @staticmethod
    @contextmanager
    def _lock():
        yield True

    def _run(self, monkeypatch):
        shipped = []
        monkeypatch.setattr(singleton, "_exclusive_lock", self._lock)
        monkeypatch.setattr(
            singleton,
            "capture_lifecycle",
            lambda message, **fields: shipped.append((message, fields)) or True,
        )
        singleton._start_backend_holding_lock(PORT)
        return shipped

    def test_a_refusal_is_never_reported_as_an_eviction(
        self, isolated_state, monkeypatch, caplog
    ):
        _record_stranger(isolated_state)
        _write_browsers(isolated_state)
        with caplog.at_level(logging.INFO, logger="stealth.proxy"):
            shipped = self._run(monkeypatch)

        assert shipped == [
            (
                "proxy: backend eviction refused (still serving)",
                {"port": PORT, "browsers": 1},
            )
        ]
        # The vocabulary the lifecycle E2E greps for must not appear either:
        # a durable log line saying "evicting" about a backend that was spared
        # is the same lie in the other stream.
        assert not any("evicting" in r.getMessage() for r in caplog.records)

    def test_a_real_eviction_still_reports_exactly_what_it_always_did(
        self, isolated_state, monkeypatch, caplog
    ):
        _record_stranger(isolated_state)  # no browsers recorded -> evictable
        with caplog.at_level(logging.INFO, logger="stealth.proxy"):
            shipped = self._run(monkeypatch)

        assert shipped == [("proxy: backend evicted (source changed)", {"port": PORT})]
        assert any(
            "backend stale (source changed), evicting" in r.getMessage()
            for r in caplog.records
        )


# ── F-967: a backend that is slow is not a backend that is dead ──────────────
class TestAStarvedBackendThatOwnsBrowsersIsNotEvicted:
    """2026-10-14, backend 38760 on 2.1.32. Ten chats spawned at once and each
    spawn froze the backend's event loop for the whole of a profile copy
    (F-967), so for ~106 s it answered no probe. Its proxies' watchdogs
    condemned it ("confirmed unusable" x32), every proxy healed through
    ``ensure_server_running``, and the lock winner — after ``REUSE_PATIENCE``'s
    60 s of unanswered probes — reached ``_clear_stale_backend``, whose
    ``protected`` rule is IDENTITY-gated: a backend of OUR OWN identity is
    "adoptable or wedged", and a wedged one is terminated. So a backend that was
    working flat out, last heard from 53 s earlier, and owning the shared and
    fleet browsers plus every chat's clone was killed by a proxy. Its successor,
    72908, was killed the same way 51 s later.

    The kill site now asks the one witness a starved prober cannot fake — the
    backend's own loop stamp — and a process of ours that is alive, owns live
    browsers and was heard from within ``STARVED_CEILING_SECONDS`` is slow, not
    wedged. Everything the old rule did for a TRULY wedged backend is unchanged:
    no stamp, a stamp older than the ceiling, or no live browser, and it is
    terminated exactly as before.
    """

    @pytest.fixture(autouse=True)
    def _probes(self, monkeypatch):
        monkeypatch.setattr(singleton, "_is_our_backend", _is_ours)
        monkeypatch.setattr(singleton.psutil, "pid_exists", lambda pid: True)
        monkeypatch.setattr(
            singleton, "_same_identity_backend_ready", lambda port, **kw: False
        )

    @staticmethod
    def _stamp(state_dir, *, age, pid=STRANGER_PID):
        backend_registry.stamp_heartbeat(
            state_dir / "server.json", port=PORT, pid=pid, at=time.time() - age
        )

    def _kill_recorder(self, monkeypatch):
        killed = []
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: killed.append(port)
        )
        return killed

    def test_a_silent_but_recent_backend_with_browsers_is_spared(
        self, isolated_state, monkeypatch
    ):
        _record_stranger(isolated_state, digest=OUR_DIGEST)
        _write_browsers(isolated_state)
        self._stamp(isolated_state, age=53.0)  # the incident's own number
        killed = self._kill_recorder(monkeypatch)

        assert singleton._clear_stale_backend(PORT) == [BROWSER_PID]
        assert killed == [], "a starved backend that owns live browsers was killed"

    def test_the_cold_start_lock_spawns_nothing_beside_it(
        self, isolated_state, monkeypatch, caplog
    ):
        @contextmanager
        def fake_lock():
            yield True

        _record_stranger(isolated_state, digest=OUR_DIGEST)
        _write_browsers(isolated_state)
        self._stamp(isolated_state, age=53.0)
        calls = []
        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: calls.append(("kill", port))
        )
        monkeypatch.setattr(
            singleton,
            "_start_server_process",
            lambda port: calls.append(("start", port)),
        )
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)

        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            singleton._start_backend_holding_lock(PORT)

        assert calls == []
        [rec] = [r for r in caplog.records if "refusing to evict" in r.getMessage()]
        assert "1 live browser(s)" in rec.getMessage()

    def test_a_backend_silent_past_the_ceiling_is_a_wedge_and_is_evicted(
        self, isolated_state, monkeypatch
    ):
        _record_stranger(isolated_state, digest=OUR_DIGEST)
        _write_browsers(isolated_state)
        self._stamp(isolated_state, age=backend_eviction.STARVED_CEILING_SECONDS + 60.0)
        killed = self._kill_recorder(monkeypatch)

        assert singleton._clear_stale_backend(PORT) == []
        assert killed == [PORT]

    def test_a_backend_that_never_stamped_is_evicted_as_before(
        self, isolated_state, monkeypatch
    ):
        """A 2.1.9 backend writes no stamp: no evidence is today's behaviour."""
        _record_stranger(isolated_state, digest=OUR_DIGEST)
        _write_browsers(isolated_state)
        killed = self._kill_recorder(monkeypatch)

        assert singleton._clear_stale_backend(PORT) == []
        assert killed == [PORT]

    def test_a_stamp_by_another_pid_is_no_evidence(self, isolated_state, monkeypatch):
        """A leftover from a predecessor on that port speaks for nobody."""
        _record_stranger(isolated_state, digest=OUR_DIGEST)
        _write_browsers(isolated_state)
        self._stamp(isolated_state, age=5.0, pid=STRANGER_PID + 1)
        killed = self._kill_recorder(monkeypatch)

        assert singleton._clear_stale_backend(PORT) == []
        assert killed == [PORT]

    def test_a_starved_backend_that_owns_no_browser_is_still_replaced(
        self, isolated_state, monkeypatch
    ):
        """The browsers are the only state a backend holds that cannot be
        rebuilt by reconnecting; an idle one has nothing to lose."""
        _record_stranger(isolated_state, digest=OUR_DIGEST)
        self._stamp(isolated_state, age=53.0)
        killed = self._kill_recorder(monkeypatch)

        assert singleton._clear_stale_backend(PORT) == []
        assert killed == [PORT]

    def test_a_dead_pid_is_not_spared_by_its_old_stamp(
        self, isolated_state, monkeypatch
    ):
        _record_stranger(isolated_state, digest=OUR_DIGEST)
        _write_browsers(isolated_state)
        self._stamp(isolated_state, age=5.0)
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: False)
        killed = self._kill_recorder(monkeypatch)

        assert singleton._clear_stale_backend(PORT) == []
        assert killed == [PORT]

    def test_the_bind_site_does_not_step_aside_from_a_starved_backend(
        self, isolated_state, monkeypatch
    ):
        """The refusal belongs at the KILL site only. At the bind site it would
        make every proxy spawn a SECOND backend on a fresh port beside the
        starved one — the herd, and a split of the fleet's browsers."""
        _record_stranger(isolated_state, digest=OUR_DIGEST)
        _write_browsers(isolated_state)
        self._stamp(isolated_state, age=53.0)
        monkeypatch.setattr(singleton, "_port_is_foreign_held", lambda port: False)
        picked = {}
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.proxy_forwarder.bindable_port",
            lambda target, force_new: picked.update(force_new=force_new) or target,
        )

        assert singleton._select_backend_port(PORT) == PORT
        assert picked == {"force_new": False}

    def test_the_operator_verbs_stay_unconditional(self, isolated_state, monkeypatch):
        """`restart` terminates whatever is on the port it selected; the
        operator asking IS the authority the rule otherwise supplies."""

        @contextmanager
        def fake_lock():
            yield True

        _record_stranger(isolated_state, digest=OUR_DIGEST)
        _write_browsers(isolated_state)
        self._stamp(isolated_state, age=5.0)
        calls = []
        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_port_is_foreign_held", lambda port: False)
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.proxy_forwarder.bindable_port",
            lambda target, force_new: target,
        )
        monkeypatch.setattr(
            singleton, "_terminate_backend", lambda port: calls.append(("kill", port))
        )
        monkeypatch.setattr(
            singleton,
            "_start_server_process",
            lambda port: calls.append(("start", port)),
        )
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)
        monkeypatch.setattr(singleton, "_probe_port", lambda port: "responsive")

        singleton.restart_backend()

        assert calls == [("kill", PORT), ("start", PORT)]


class TestProtectedWithAWitness:
    """The rule's own contract: the witness only ever ADDS protection, and only
    to an identity-matching backend (a foreign one is protected regardless)."""

    def _protected(self, tmp_path, entry, **overrides):
        kwargs = {
            "state_dir": tmp_path,
            "identity_matches": _identity_matches,
            "is_ours": _is_ours,
            "is_running": lambda pid: True,
            "silence": lambda: 53.0,
        }
        kwargs.update(overrides)
        return backend_eviction.protected(entry, **kwargs)

    def test_ours_within_the_ceiling_with_a_browser_is_protected(self, tmp_path):
        _write_browsers(tmp_path)
        assert self._protected(tmp_path, _entry(digest=OUR_DIGEST)) == [BROWSER_PID]

    def test_the_ceiling_is_inclusive_of_its_own_value(self, tmp_path):
        _write_browsers(tmp_path)
        at = backend_eviction.STARVED_CEILING_SECONDS
        assert self._protected(
            tmp_path, _entry(digest=OUR_DIGEST), silence=lambda: at
        ) == [BROWSER_PID]
        assert (
            self._protected(
                tmp_path, _entry(digest=OUR_DIGEST), silence=lambda: at + 0.1
            )
            == []
        )

    def test_no_evidence_is_no_protection(self, tmp_path):
        _write_browsers(tmp_path)
        assert (
            self._protected(tmp_path, _entry(digest=OUR_DIGEST), silence=lambda: None)
            == []
        )

    def test_a_stranger_stays_protected_without_any_witness(self, tmp_path):
        _write_browsers(tmp_path)
        assert self._protected(tmp_path, _entry(), silence=None) == [BROWSER_PID]
        assert self._protected(tmp_path, _entry(), silence=lambda: None) == [
            BROWSER_PID
        ]


class TestTheKillNamesItself:
    """The incident's gap in evidence: ``terminate`` wrote nothing, so a backend
    that vanished with no shutdown line could not be attributed. Every kill now
    says who it killed, on which port, in the proxy log."""

    def test_terminate_logs_the_pid_and_the_port(self, monkeypatch, caplog):
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            backend_eviction.psutil, "Process", MagicMock(return_value=MagicMock())
        )
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            backend_eviction.terminate(
                PORT,
                pid_on_port=lambda port: 777,
                recorded_pid=None,
                is_ours=lambda pid: False,
                is_healthy=lambda port: False,
            )
        [rec] = [r for r in caplog.records if "terminating" in r.getMessage()]
        assert "777" in rec.getMessage() and str(PORT) in rec.getMessage()

    def test_an_eviction_says_why_it_went_ahead(self, caplog):
        with caplog.at_level(logging.WARNING, logger="stealth.proxy"):
            backend_eviction.clear_stale(
                PORT,
                reusable=lambda: False,
                protecting=list,
                terminate_backend=lambda: None,
            )
        [rec] = [r for r in caplog.records if "evicting" in r.getMessage()]
        assert f"port {PORT}" in rec.getMessage()
        assert "no live browser" in rec.getMessage()
