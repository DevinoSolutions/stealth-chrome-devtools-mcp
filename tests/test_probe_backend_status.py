"""Pinning tests for M1-4's `_probe_backend_status` reporter (plan_M1 SS2.1-D).

Direct unit tests of the three-way branch itself (responsive/wedged/down/
none), separate from the CLI wiring (see test_cli_status_wedged.py for that
half). Reuses the wedged_stub/responsive_stub fixture shapes established in
M1-1/M1-2's test files, per this codebase's per-file-fixture convention.
"""

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import singleton


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(
        singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
    )
    return tmp_path


@pytest.fixture()
def wedged_stub():
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

        def log_message(self, *args):
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


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestProbeBackendStatus:
    def test_no_recorded_state_reports_none(self, isolated_state):
        status, port = singleton._probe_backend_status()
        assert status == "none"
        assert port is None

    def test_down_port_reports_down(self, isolated_state):
        port = _free_closed_port()
        (isolated_state / "server.json").write_text(
            json.dumps({"port": port, "version": "1.2.1", "pid": os.getpid()})
        )
        status, reported_port = singleton._probe_backend_status()
        assert status == "down"
        assert reported_port == port

    def test_wedged_backend_reports_wedged(self, isolated_state, wedged_stub):
        (isolated_state / "server.json").write_text(
            json.dumps({"port": wedged_stub, "version": "1.2.1", "pid": os.getpid()})
        )
        status, reported_port = singleton._probe_backend_status()
        assert status == "wedged"
        assert reported_port == wedged_stub

    def test_responsive_backend_reports_responsive(
        self, isolated_state, responsive_stub
    ):
        (isolated_state / "server.json").write_text(
            json.dumps(
                {"port": responsive_stub, "version": "1.2.1", "pid": os.getpid()}
            )
        )
        status, reported_port = singleton._probe_backend_status()
        assert status == "responsive"
        assert reported_port == responsive_stub
