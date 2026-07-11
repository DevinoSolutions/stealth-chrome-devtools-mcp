"""Pinning tests for M1-2: `_find_running_server` gates reuse on the
app-level probe, not the bare socket check.

Before this change, a recorded same-version backend was "reusable" whenever
`_server_is_healthy` (bare TCP connect) returned True — a wedged backend
(dispatch loop dead, socket still open) passed that check and was reused
forever (F-301's reuse half). After: `_find_running_server` gates on
`_backend_http_ready` instead, so a wedged same-version backend is correctly
treated as NOT reusable — this un-blocks the existing eviction+respawn
machine (`_clear_stale_backend` -> `_start_server_process`), since the
eviction guard *is* `_find_running_server`.

Reuses the wedged_stub / responsive_stub fixture shapes from
test_backend_liveness_probe.py (M1-1) — duplicated locally per this
codebase's established per-file-fixture convention (see e.g.
test_debug_logger_file_bridge.py's captured_backend_records), not
centralized in conftest.py.
"""

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from stealth_chrome_devtools_mcp.embedded import singleton


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point singleton state at tmp_path so tests never touch ~/.stealth-mcp."""
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(
        singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
    )
    return tmp_path


@pytest.fixture()
def wedged_stub():
    """Accepts a TCP connection, then holds it open and never writes a byte."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stop = threading.Event()
    held_conns = []

    def _accept_and_hold():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                held_conns.append(conn)
            except TimeoutError:
                continue
            except OSError:
                return

    thread = threading.Thread(target=_accept_and_hold, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in held_conns:
            conn.close()
        listener.close()


@pytest.fixture()
def responsive_stub():
    """Answers POST with 200 + mcp-session-id."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802  stdlib override, PERMANENT(interface)
            body_len = int(self.headers.get("Content-Length", 0))
            self.rfile.read(body_len)
            self.send_response(200)
            self.send_header("mcp-session-id", "test-session-abc")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"jsonrpc":"2.0","id":0,"result":{}}')

        def do_DELETE(self):  # noqa: N802  stdlib override, PERMANENT(interface)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # silence stdlib's default stderr logging
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TestFindRunningServerAppProbe:
    def test_wedged_same_version_backend_is_not_reused(
        self, isolated_state, monkeypatch, wedged_stub
    ):
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        # M2: a MATCHING fingerprint so reuse reaches the HEALTH gate this test is
        # named for - the wedged backend must be rejected there, not
        # short-circuited at the new source-fingerprint gate.
        monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "fp-match")
        (isolated_state / "server.json").write_text(
            json.dumps(
                {
                    "port": wedged_stub,
                    "version": "1.2.1",
                    "pid": os.getpid(),
                    "source_fingerprint": "fp-match",
                }
            )
        )
        assert singleton._find_running_server() is None

    def test_responsive_same_version_backend_returns_port(
        self, isolated_state, monkeypatch, responsive_stub
    ):
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        # M2: a matching fingerprint so reuse composes through to the health gate
        # (the responsive stub answers, so the port is returned).
        monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "fp-match")
        (isolated_state / "server.json").write_text(
            json.dumps(
                {
                    "port": responsive_stub,
                    "version": "1.2.1",
                    "pid": os.getpid(),
                    "source_fingerprint": "fp-match",
                }
            )
        )
        assert singleton._find_running_server() == responsive_stub
