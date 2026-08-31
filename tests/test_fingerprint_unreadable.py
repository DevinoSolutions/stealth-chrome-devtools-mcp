"""F-829: an UNREADABLE source fingerprint must never read as "source changed".

``singleton._source_fingerprint`` used to collapse a transient OS read error
into ``""``, and ``""`` is what the reuse gate treats as a miss. So one locked
file — this tree syncs through OneDrive, where that is a routine event — made a
perfectly healthy shared backend look source-stale, and the cold-start lock
evicted (killed) it, mass-disconnecting every Claude Code session on it while
logging the misattribution "backend stale (source changed), evicting".

The fix separates three states: a computed digest, ``None`` ("unreadable" —
unknown, never a mismatch), and ``""`` (kept fail-closed for the legacy/stubbed
callers that mean "no digest"). These tests pin the separation from both ends:
unknown never evicts, a genuine mismatch still does.

Hermetic: no real backend, no Chrome, no ``~/.stealth-mcp`` writes (state paths
are redirected to ``tmp_path``, the same isolation idiom the other singleton
suites use), and a real listening socket stands in for a live backend.
"""

import json
import logging
import os
import socket
from contextlib import contextmanager
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import singleton


def _listening_socket():
    """A real socket accepting connections, so the socket health check passes."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    return s, s.getsockname()[1]


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point singleton state at tmp_path so tests never touch ~/.stealth-mcp."""
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(
        singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
    )
    return tmp_path


def _record(state_dir: Path, port: int, fingerprint) -> None:
    """Write a v1-flat record (the shape the other singleton suites write)."""
    (state_dir / "server.json").write_text(
        json.dumps(
            {
                "port": port,
                "version": "1.2.1",
                "pid": os.getpid(),
                "source_fingerprint": fingerprint,
            }
        )
    )


class TestUnreadableSourceReturnsSentinel:
    def test_persistent_oserror_yields_none_not_empty_string(
        self, tmp_path, monkeypatch
    ):
        # THE defect: "" is the same value the gate reads as "source changed".
        # A read failure must be its own third state, distinguishable from both
        # a digest and an intentional miss.
        (tmp_path / "a.py").write_text("x = 1\n")
        monkeypatch.setattr(singleton, "SOURCE_ROOT", tmp_path)
        monkeypatch.setattr(
            Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError("locked"))
        )
        assert singleton._source_fingerprint() is None

    def test_read_failure_is_retried_before_giving_up(self, tmp_path, monkeypatch):
        # A OneDrive sync lock is measured in milliseconds; one shot at the file
        # turns it into an eviction, so the read is retried before the sentinel.
        (tmp_path / "a.py").write_text("x = 1\n")
        monkeypatch.setattr(singleton, "SOURCE_ROOT", tmp_path)
        attempts = []

        def boom(self):
            attempts.append(self)
            raise OSError("locked")

        monkeypatch.setattr(Path, "read_bytes", boom)
        assert singleton._source_fingerprint() is None
        assert len(attempts) >= 2

    def test_transient_failure_that_heals_yields_a_real_digest(
        self, tmp_path, monkeypatch
    ):
        # The point of retrying: the common case is that the second read works,
        # so the backend is never even a candidate for eviction.
        (tmp_path / "a.py").write_text("x = 1\n")
        monkeypatch.setattr(singleton, "SOURCE_ROOT", tmp_path)
        real = Path.read_bytes
        calls = []

        def flaky(self):
            calls.append(self)
            if len(calls) == 1:
                raise OSError("locked")
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", flaky)
        digest = singleton._source_fingerprint()
        assert isinstance(digest, str) and len(digest) == 64

    def test_readable_source_still_yields_a_digest(self, tmp_path, monkeypatch):
        # Regression guard: the retry loop must not change the happy path.
        (tmp_path / "a.py").write_text("x = 1\n")
        monkeypatch.setattr(singleton, "SOURCE_ROOT", tmp_path)
        assert singleton._source_fingerprint() == singleton._source_fingerprint()
        assert len(singleton._source_fingerprint()) == 64


class TestUnknownFingerprintNeverEvicts:
    def test_unreadable_current_fingerprint_reuses_healthy_backend(
        self, isolated_state, monkeypatch
    ):
        # THE user-visible fix: the backend answers, its version matches, only
        # OUR read of the source failed. That is not evidence of a source
        # change, so the shared backend must be reused, not killed.
        sock, port = _listening_socket()
        try:
            monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
            monkeypatch.setattr(singleton, "_source_fingerprint", lambda: None)
            monkeypatch.setattr(
                singleton, "_backend_http_ready", lambda port, **kw: True
            )
            _record(isolated_state, port, "RECORDED")
            assert singleton._find_running_server() == port
        finally:
            sock.close()

    def test_recorded_sentinel_fingerprint_reuses_healthy_backend(
        self, isolated_state, monkeypatch
    ):
        # The other half of the record-time decision: a backend recorded while
        # the source was unreadable stamps the sentinel (JSON null). Comparing
        # sentinel-vs-anything is unknown too — it must not evict either, or the
        # defect simply moves from discovery time to record time.
        sock, port = _listening_socket()
        try:
            monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
            monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "CURRENT")
            monkeypatch.setattr(
                singleton, "_backend_http_ready", lambda port, **kw: True
            )
            _record(isolated_state, port, None)
            assert singleton._find_running_server() == port
        finally:
            sock.close()

    def test_version_mismatch_still_evicts_when_fingerprint_unknown(
        self, isolated_state, monkeypatch
    ):
        # "Unknown" relaxes exactly one of the two identity keys. An upgraded
        # package must still evict a stale backend (issue #14) even when the
        # source cannot be read.
        sock, port = _listening_socket()
        try:
            monkeypatch.setattr(singleton, "_server_version", lambda: "9.9.9")
            monkeypatch.setattr(singleton, "_source_fingerprint", lambda: None)
            monkeypatch.setattr(
                singleton, "_backend_http_ready", lambda port, **kw: True
            )
            _record(isolated_state, port, "RECORDED")
            assert singleton._find_running_server() is None
        finally:
            sock.close()

    def test_genuine_source_change_still_evicts(self, isolated_state, monkeypatch):
        # Regression guard for M2/F-206: a real digest that differs from the
        # recorded one is still a mismatch and still refuses reuse.
        sock, port = _listening_socket()
        try:
            monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
            monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "NEW")
            monkeypatch.setattr(
                singleton, "_backend_http_ready", lambda port, **kw: True
            )
            _record(isolated_state, port, "OLD")
            assert singleton._find_running_server() is None
        finally:
            sock.close()

    def test_legacy_record_without_a_fingerprint_still_evicts(
        self, isolated_state, monkeypatch
    ):
        # An ABSENT key is a pre-M2 backend, not a recorded sentinel: it fails
        # closed exactly as before. Only an explicitly recorded null is unknown.
        sock, port = _listening_socket()
        try:
            monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
            monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "CURRENT")
            monkeypatch.setattr(
                singleton, "_backend_http_ready", lambda port, **kw: True
            )
            (isolated_state / "server.json").write_text(
                json.dumps({"port": port, "version": "1.2.1", "pid": os.getpid()})
            )
            assert singleton._find_running_server() is None
        finally:
            sock.close()


class TestUnreadableIsLoggedAsItsOwnCause:
    def test_unreadable_warning_names_the_real_cause(
        self, tmp_path, monkeypatch, caplog
    ):
        (tmp_path / "a.py").write_text("x = 1\n")
        monkeypatch.setattr(singleton, "SOURCE_ROOT", tmp_path)
        monkeypatch.setattr(
            Path,
            "read_bytes",
            lambda self: (_ for _ in ()).throw(OSError("onedrive-locked")),
        )
        with caplog.at_level(logging.DEBUG, logger="stealth.proxy"):
            assert singleton._source_fingerprint() is None
        unreadable = [
            r
            for r in caplog.records
            if "fingerprint unreadable" in r.getMessage()
            or "fingerprint unreadable" in str(r.msg)
        ]
        assert unreadable, caplog.messages
        assert any(r.levelno == logging.WARNING for r in unreadable)
        assert "onedrive-locked" in " ".join(r.getMessage() for r in unreadable)
        # Distinct from the genuine-change line, which must not be borrowed.
        assert not any("source changed" in m for m in caplog.messages)

    def test_eviction_line_is_not_logged_when_the_fingerprint_is_unknown(
        self, isolated_state, monkeypatch, caplog
    ):
        # The misattribution itself: the cold-start lock used to print
        # "backend stale (source changed), evicting" for a read failure.
        @contextmanager
        def fake_lock():
            yield True

        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        monkeypatch.setattr(singleton, "_source_fingerprint", lambda: None)
        monkeypatch.setattr(singleton, "_find_running_server", lambda: None)
        monkeypatch.setattr(
            singleton, "_same_identity_backend_ready", lambda port, **kw: False
        )
        monkeypatch.setattr(singleton, "_clear_stale_backend", lambda port: None)
        monkeypatch.setattr(singleton, "_start_server_process", lambda port: None)
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)
        _record(isolated_state, 19222, "RECORDED")

        with caplog.at_level(logging.INFO, logger="stealth.proxy"):
            singleton._start_backend_holding_lock(19222)

        assert "backend stale (source changed), evicting" not in caplog.messages
