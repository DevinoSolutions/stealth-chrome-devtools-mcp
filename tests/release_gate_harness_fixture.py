"""The fixture app: THE serving mechanism (handler, server, origin pair).

Split out of :mod:`release_gate_harness` (T-6148); that module re-exports every name
here, so callers keep importing from ``release_gate_harness``.
"""

from __future__ import annotations

import contextlib
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import fixture_routes
from release_gate_harness_consts import (
    FIXTURE_APP_DIR,
    LOGIN_COOKIE,
    LOGIN_COOKIE_VALUE,
)

# ---------------------------------------------------------------------------
# Fixture app — THE serving mechanism (conftest.fixture_app_server delegates
# here). Serves tests/fixture_app plus the plan_E2E §2.2 API routes over an
# ephemeral 127.0.0.1 port. Moved verbatim from conftest so there is one home.
# ---------------------------------------------------------------------------
# Diagnostics only: every request the fixture server received, and how it ended.
# When a navigation fails there is no other way to tell "Chrome never reached the
# server" (a network/launch problem) from "Chrome fetched the page but the load
# never completed" (a CDP/page problem) — the two have identical symptoms at the
# tool boundary. An entry is written when the request line is parsed, marked
# ``[unfinished]``, and rewritten when the handler ends: plain when the response
# was served, ``[reset]`` when the client went away mid-request. So a request
# Chrome sent and then cancelled, or one still being served, is visible instead
# of absent (T-4709: "no GET /app.js" used to mean "none served to completion").
# Bounded, and never asserted on.
_FIXTURE_HITS: list[str] = []
_FIXTURE_HITS_CAP = 50
_HIT_UNFINISHED = " [unfinished]"
_HIT_RESET = " [reset]"


class _FixtureHandler(SimpleHTTPRequestHandler):
    """Static file server for tests/fixture_app + the plan_E2E §2.2 API routes
    + W7's dynamic routes (``tests/fixture_routes.py``).

    The W7 shapes need pages that name their *peer* origin, exact response
    headers, redirects, chunked and event-stream bodies, and a WebSocket
    upgrade — none of which a file on disk can be. They dispatch through
    :func:`fixture_routes.dispatch` FIRST and fall through to the static tree
    when the path is not one of theirs, so there is still exactly one handler,
    one server, and one fixture mechanism.
    """

    def __init__(self, *args, origin_state=None, **kwargs):
        # Set before super().__init__, which handles the whole request inline.
        self.origin_state = origin_state or fixture_routes.new_origin_state("a")
        super().__init__(*args, **kwargs)

    def log_message(self, *args, **kwargs):
        """Silence per-request stderr logging (keeps test output clean)."""

    def parse_request(self):
        """Record the request the moment its line is parsed (stdlib override)."""
        parsed = super().parse_request()
        line = f"{self.client_address[0]} {self.requestline}"
        if len(_FIXTURE_HITS) < _FIXTURE_HITS_CAP:
            _FIXTURE_HITS.append(line + _HIT_UNFINISHED)
            self._hit = (len(_FIXTURE_HITS) - 1, line)
        return parsed

    def _end_hit(self, suffix: str) -> None:
        """Rewrite this request's entry to its outcome; no-op when it was never
        recorded or the list was cleared for another server since."""
        hit, self._hit = self._hit, None
        if hit is None:
            return
        index, line = hit
        if (
            index < len(_FIXTURE_HITS)
            and _FIXTURE_HITS[index] == line + _HIT_UNFINISHED
        ):
            _FIXTURE_HITS[index] = line + suffix

    def handle_one_request(self):
        """Serve one request and record how it ended (stdlib override).

        A browser closing a socket while this thread reads or writes raises a
        ``ConnectionError`` (``ConnectionResetError`` / WinError 10054, or the
        aborted/broken-pipe pair) out of the stdlib handler, and pytest prints the
        whole traceback into the node's captured output — where it masks the
        output a failure is read from (it did exactly that on PR #151's
        cookie-node failure). The client going away is nothing to report: return.
        A reset with no request line parsed (a speculative socket) records
        nothing, because there was no request; one after it records ``[reset]``.
        """
        self._hit = None
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True
            self._end_hit(_HIT_RESET)
            return
        self._end_hit("")

    def _send_json(self, payload, status=200, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802  stdlib override, PERMANENT(interface)
        if fixture_routes.dispatch(self, "GET"):
            return
        if self.path == "/api/json":
            self._send_json({"ok": True, "value": 42, "source": "fixture"})
            return
        if self.path == "/api/set-cookie":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Set-Cookie", "fixture_cookie=server-set; Path=/")
            self.end_headers()
            self.wfile.write(b"cookie set")
            return
        if self.path == "/api/login":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header(
                "Set-Cookie",
                f"{LOGIN_COOKIE}={LOGIN_COOKIE_VALUE}; Path=/; HttpOnly; Max-Age=3600",
            )
            self.end_headers()
            self.wfile.write(b"signed in")
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/api/json")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802  stdlib override, PERMANENT(interface)
        if fixture_routes.dispatch(self, "POST"):
            return
        if self.path == "/api/echo":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            reflected = {key.lower(): value for key, value in self.headers.items()}
            self._send_json({"body": raw, "headers": reflected})
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):  # noqa: N802  stdlib override, PERMANENT(interface)
        """CORS preflight (W7 MQ-118). Only dynamic routes answer OPTIONS."""
        if fixture_routes.dispatch(self, "OPTIONS"):
            return
        self.send_response(404)
        self.end_headers()


#: How many connections the fixture server's listening socket queues before
#: its accept loop takes them (F-934). The stdlib default is 5. A six-browser
#: fleet opens up to 36 at once (Chrome's six per host, per browser), and when
#: a busy runner stalls the accept loop the sixth is refused: Windows retries
#: the SYN for ~2 s and then fails the connect, which Chrome reports as its
#: error page. 128 is the largest backlog every CI kernel honours as asked
#: (macOS clamps to ``kern.ipc.somaxconn``, 128 by default).
FIXTURE_ACCEPT_BACKLOG = 128


class _FixtureServer(ThreadingHTTPServer):
    """The fixture origin's server: stdlib's, with a fleet-sized backlog."""

    request_queue_size = FIXTURE_ACCEPT_BACKLOG


def _bind_origin(origin_state: dict) -> tuple[ThreadingHTTPServer, str]:
    """Bind one ephemeral literal-IPv4 loopback origin (not yet serving).

    Binding is separated from serving because a cross-linked PAIR cannot know
    either base URL until BOTH sockets have a port — so both bind, both learn
    the other's URL, and only then does either accept a request.
    """
    handler = functools.partial(
        _FixtureHandler, directory=str(FIXTURE_APP_DIR), origin_state=origin_state
    )
    httpd = _FixtureServer(("127.0.0.1", 0), handler)
    host, port = httpd.server_address
    return httpd, f"http://{host}:{port}"


@contextlib.contextmanager
def _serving(servers: list[ThreadingHTTPServer], states: list[dict]):
    """Run each bound server on a daemon thread; shut all of them down on exit.

    Finalization releases every W10 fault controller FIRST. ``shutdown()`` only
    stops the accept loop — it never interrupts an in-flight request — so a test
    that failed while a fault route was parked would otherwise leave a handler
    thread holding a connection past ``server_close()``. Releasing first, then
    asserting every entered handler actually left, is what makes a failed test
    unable to wedge pytest.
    """
    threads = [
        threading.Thread(target=httpd.serve_forever, daemon=True) for httpd in servers
    ]
    for thread in threads:
        thread.start()
    try:
        yield
    finally:
        stuck = [
            token
            for state in states
            for token in fixture_routes.release_all_faults(state)
        ]
        for httpd in servers:
            httpd.shutdown()
            httpd.server_close()
        for thread in threads:
            thread.join(timeout=5)
        if stuck:
            raise AssertionError(
                f"fixture fault handler(s) never terminated after release: {stuck}"
            )


@contextlib.contextmanager
def serve_fixture_app():
    """Yield the ``http://127.0.0.1:<port>`` base URL of the fixture app server.

    Binds an ephemeral literal-IPv4 loopback port (so an IPv6-first ``localhost``
    can never cause a false failure), serves ``tests/fixture_app`` plus the §2.2
    routes on a daemon thread, and shuts the server down on exit. The caller owns
    the lifetime; the journey below owns its own instance so it can close it in a
    ``finally``.
    """
    _FIXTURE_HITS.clear()
    state = fixture_routes.new_origin_state("a")
    httpd, base_url = _bind_origin(state)
    state["self_url"] = base_url
    with _serving([httpd], [state]):
        yield base_url


@contextlib.contextmanager
def serve_fixture_origin_pair():
    """Yield ``(origin_a, origin_b)`` — two INDEPENDENT ``127.0.0.1:0`` servers.

    plan_RELEASE W7 needs a genuine second origin (cross-origin frames, a
    cross-origin CSP ``connect-src`` violation, a redirect that leaves A, and a
    real CORS preflight); two ephemeral loopback ports differ in port and so are
    distinct web origins. Each server learns its peer's base URL after both
    sockets bind and before either accepts, so no request can observe a
    half-linked pair. Same handler, same fixture tree, same shutdown discipline
    as the single-origin form above.
    """
    _FIXTURE_HITS.clear()
    states = [
        fixture_routes.new_origin_state("a"),
        fixture_routes.new_origin_state("b"),
    ]
    bound = [_bind_origin(state) for state in states]
    urls = [url for _, url in bound]
    for index, state in enumerate(states):
        state["self_url"] = urls[index]
        state["peer_url"] = urls[1 - index]
    with _serving([httpd for httpd, _ in bound], states):
        yield urls[0], urls[1]
