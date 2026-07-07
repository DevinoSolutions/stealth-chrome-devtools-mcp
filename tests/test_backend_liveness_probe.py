"""Pinning tests for M1-1: the single-shot app-level liveness probe.

The system's only runtime liveness signal used to be a bare TCP connect
(``_server_is_healthy``) — a backend whose dispatch loop is wedged still
completes the kernel handshake, so that signal reads "healthy" forever
(F-301/F-501). ``_backend_http_ready`` promotes the *mechanism* that already
exists in ``_await_backend_http`` (a real ``initialize``->HTTP-200 round
trip) into a reusable, synchronous, single-shot check: one attempt, not a
poll loop, so both sync callers (discovery, CLI) and the watchdog (via
``anyio.to_thread.run_sync``) can use it directly.

Two hermetic, localhost-only stubs simulate the two real backend states this
whole finding is about (no Chrome, no real backend — stays in the
`not integration` suite):
- wedged stub: a raw socket that accepts a connection and then holds it
  without ever writing a byte. Socket-open, app-dead — exactly the state
  `_server_is_healthy` cannot distinguish from healthy.
- responsive stub: a stdlib ThreadingHTTPServer that answers POST with 200 +
  an ``mcp-session-id`` header and records DELETEs, so the probe's
  best-effort session-cleanup call is independently observable.
"""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import singleton


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
    """Answers POST with 200 + mcp-session-id; records DELETEs it receives."""
    delete_requests = []

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
            delete_requests.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # silence stdlib's default stderr logging
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, delete_requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _free_closed_port() -> int:
    """A port with nothing listening on it (bind, grab the number, close)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestBackendHttpReadyTruthTable:
    def test_responsive_backend_returns_true_and_receives_delete(self, responsive_stub):
        port, delete_requests = responsive_stub
        assert singleton._backend_http_ready(port) is True
        # Best-effort session cleanup: the throwaway readiness session must not
        # linger on the backend.
        deadline = time.monotonic() + 2
        while not delete_requests and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(delete_requests) == 1

    def test_wedged_backend_returns_false_within_timeout(self, wedged_stub):
        start = time.monotonic()
        result = singleton._backend_http_ready(wedged_stub, timeout=0.5)
        elapsed = time.monotonic() - start
        assert result is False
        # Bounded: must not hang past roughly the requested timeout.
        assert elapsed < 3.0

    def test_down_backend_returns_false_within_timeout(self):
        # "Fast" is platform-dependent: on Linux/macOS a closed port refuses
        # the connection immediately; on Windows the OS does not surface
        # ECONNREFUSED the same way and httpx's own connect-timeout governs
        # the wait instead. The load-bearing contract is bounded-and-False,
        # not a specific sub-second latency.
        port = _free_closed_port()
        start = time.monotonic()
        result = singleton._backend_http_ready(port, timeout=0.5)
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 3.0

    def test_never_raises_on_garbage_response(self):
        # A port with SOMETHING listening that is not HTTP at all (a bare
        # listening socket that closes the connection immediately) must still
        # resolve to False, never propagate an exception.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        stop = threading.Event()

        def _accept_and_close():
            listener.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                    conn.close()
                except TimeoutError:
                    continue
                except OSError:
                    return

        thread = threading.Thread(target=_accept_and_close, daemon=True)
        thread.start()
        try:
            result = singleton._backend_http_ready(port, timeout=1.0)
            assert result is False
        finally:
            stop.set()
            thread.join(timeout=2)
            listener.close()


class TestLivenessProbeTimeoutConstant:
    def test_default_timeout_is_two_seconds(self):
        # Human-resolved decision (plan_M1 appendix): keep LIVENESS_PROBE_TIMEOUT
        # at 2.0 to preserve the ~12s watchdog detection window and the existing
        # hysteresis tests. Not a design choice to re-litigate here.
        assert singleton.LIVENESS_PROBE_TIMEOUT == 2.0
