"""F-1018 follow-up (T-6550): a 401 never condemns a healthy backend on the
cold-start or the self-heal path.

T-6367 made the heartbeat answer alive / unauthorized / dead, so a 401 is no
strike. Two other readers of the same probe still read a 401 as "not ready":

* ``singleton._same_identity_backend_ready`` (via ``backend_probe.ready``), so a
  proxy that cannot read the token got ``None`` from ``_find_running_server``
  and ``_clear_stale_backend`` TERMINATED an idle, healthy backend;
* ``proxy_selfheal._confirm_bridge_verdict``, which asks the same gate and so
  answered DEAD after a lost connection.

A 401 is the backend ANSWERING: alive, and refusing only THIS client. Nothing
here is mocked below the HTTP boundary: the backend is a real loopback server
that answers what a token-enforcing backend answers, and the verdicts are the
product's.

Review (T-6550): the 401 is reused only from OUR backend. A 401 in anyone else's
words, or from a process that is not our recorded backend listening on the port, is
a stranger (never adopted, never sent a bridged request); and a 401 that has lasted
past ``PERSISTENT_UNAUTHORIZED_SECONDS`` (a backend that kept an old token in
memory) is evicted - but only through ``clear_stale``, which spares a backend that
owns browsers.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from stealth_chrome_devtools_mcp import cli
from stealth_chrome_devtools_mcp.embedded import (
    backend_probe,
    backend_token,
    proxy_selfheal,
    singleton,
)

OUR_VERSION = "9.9.9"
OUR_FINGERPRINT = "fp-same"
#: No such process: never "our backend" by looking it up, only by what the seams say.
OUR_PID = 2**22 - 1


class _Backend:
    """A loopback server answering ``status`` (or per-path ``paths``) to anything.

    A 401 is the token middleware's own (``ours``) or another server's.
    """

    def __init__(
        self, status: int, paths: dict[str, int] | None = None, *, ours: bool = True
    ) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                outer.requests.append((self.command, self.path))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                code = (paths or {}).get(self.path, status)
                body = b""
                self.send_response(code)
                if code == 401:
                    body = (
                        backend_token.UNAUTHORIZED_BODY if ours else b'{"error":"no"}'
                    )
                    scheme = backend_token.SCHEME if ours else "Basic"
                    self.send_header("www-authenticate", scheme)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = _answer  # noqa: N815  PERMANENT(http.server dispatches on do_<METHOD>)

            def log_message(self, *args: object) -> None:
                return

        self.requests: list[tuple[str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{backend_probe.MCP_PATH}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def backend_401():
    backend = _Backend(401)
    yield backend
    backend.close()


@pytest.fixture
def foreign_401():
    """Another auth-protected server that answers 401 in its own words."""
    backend = _Backend(401, ours=False)
    yield backend
    backend.close()


@pytest.fixture
def dead_port() -> int:
    """A port with nothing listening: bound, read, closed - connection refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    """Our state in tmp_path, our identity pinned, no token read, no real kill."""
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "LOCK_FILE", tmp_path / "singleton.lock")
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", tmp_path / "server.json")
    monkeypatch.setattr(singleton, "_server_version", lambda: OUR_VERSION)
    monkeypatch.setattr(singleton, "_source_fingerprint", lambda: OUR_FINGERPRINT)
    monkeypatch.setattr(backend_probe.backend_token, "auth_headers", dict)
    # The once-per-run WARNING flag is process state: every test starts a new run.
    monkeypatch.setattr(backend_probe, "_unauthorized_said", threading.Event())
    # Owns no browsers: the F-886 rule is not what is under test here.
    monkeypatch.setattr(singleton, "_protecting_browsers", lambda *a, **k: [])
    # The process listening on the port IS our recorded backend, unless a test says
    # otherwise: the lookup is psutil's, and this process is not a backend.
    monkeypatch.setattr(singleton, "_backend_pid_on_port", lambda port: OUR_PID)
    monkeypatch.setattr(backend_probe, "_unauthorized_since", {})
    clock = _Clock()
    monkeypatch.setattr(backend_probe, "_clock", clock)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def terminated(monkeypatch) -> list[int]:
    """The terminate seam: records the port, kills nothing."""
    ports: list[int] = []
    monkeypatch.setattr(
        singleton, "_terminate_backend", lambda port: ports.append(port) or True
    )
    return ports


def _record(tmp_path, port: int) -> None:
    (tmp_path / "server.json").write_text(
        json.dumps(
            {
                "port": port,
                "version": OUR_VERSION,
                "pid": OUR_PID,
                "source_fingerprint": OUR_FINGERPRINT,
            }
        )
    )


# -- the probe: one home, one verdict vocabulary ----------------------------


def test_the_probe_names_a_401_unauthorized_and_ready_stays_false(backend_401):
    assert (
        backend_probe.probe(backend_401.url, 2.0) == backend_probe.VERDICT_UNAUTHORIZED
    )
    assert backend_probe.ready(backend_401.url, 2.0) is False


def test_the_probe_still_names_a_dead_backend_dead_and_a_good_one_alive(dead_port):
    assert (
        backend_probe.probe(f"http://127.0.0.1:{dead_port}/mcp/", 1.0)
        == backend_probe.VERDICT_DEAD
    )
    ok = _Backend(200)
    try:
        assert backend_probe.probe(ok.url, 2.0) == backend_probe.VERDICT_ALIVE
        assert backend_probe.ready(ok.url, 2.0) is True
    finally:
        ok.close()


def test_a_legacy_heartbeat_reads_a_401_initialize_as_unauthorized_not_dead():
    """No health route (404) falls back to ``initialize``, which 401s."""
    backend = _Backend(401, paths={backend_probe.HEALTH_PATH: 404})
    beat = backend_probe.Heartbeat(backend.url)
    try:
        assert beat.verdict(2.0) == backend_probe.VERDICT_UNAUTHORIZED
        assert beat.alive(2.0) is True
    finally:
        beat.close()
        backend.close()


async def test_the_cold_start_poll_logs_the_cause_at_warning_once(backend_401, caplog):
    with caplog.at_level(logging.DEBUG, logger="stealth.proxy"):
        assert await backend_probe.await_ready(backend_401.url, 0.6) is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "bearer token" in warnings[0].getMessage()


# -- the cold-start path: _clear_stale_backend ------------------------------


def test_clear_stale_never_terminates_an_idle_backend_answering_401(
    tmp_path, backend_401, terminated
):
    """THE cold-start pin. RED when a 401 reads as not-ready: ``reusable()`` is
    False, no browser protects the idle backend, and it is terminated."""
    _record(tmp_path, backend_401.port)
    assert singleton._clear_stale_backend(backend_401.port) == []
    assert terminated == []


def test_the_reuse_gate_finds_a_backend_answering_401(tmp_path, backend_401):
    """``_find_running_server`` is what hands the proxy a backend instead of a
    spawn; with a 401 it used to hand back None."""
    _record(tmp_path, backend_401.port)
    assert singleton._same_identity_backend_ready(backend_401.port, patience=0.0)


def test_the_cold_start_lock_does_not_spawn_over_a_backend_answering_401(
    tmp_path, backend_401, terminated, monkeypatch
):
    _record(tmp_path, backend_401.port)
    spawned: list[int] = []
    monkeypatch.setattr(singleton, "_start_server_process", spawned.append)
    singleton._start_backend_holding_lock(backend_401.port)
    assert (terminated, spawned) == ([], [])


def test_a_dead_backend_is_still_cleared(tmp_path, dead_port, terminated):
    _record(tmp_path, dead_port)
    assert singleton._clear_stale_backend(dead_port) == []
    assert terminated == [dead_port]


def test_a_stale_version_is_still_evicted_even_if_it_answers_401(
    tmp_path, backend_401, terminated
):
    """The unauthorized exemption is for backends we would REUSE. A stale
    version still has to go (issue #14), exactly as before."""
    (tmp_path / "server.json").write_text(
        json.dumps(
            {
                "port": backend_401.port,
                "version": "0.0.1",
                "pid": OUR_PID,
                "source_fingerprint": OUR_FINGERPRINT,
            }
        )
    )
    singleton._clear_stale_backend(backend_401.port)
    assert terminated == [backend_401.port]


# -- the self-heal path: _confirm_bridge_verdict ----------------------------


async def test_a_401_after_a_lost_connection_is_not_dead(tmp_path, backend_401):
    """THE self-heal pin, through the gate the proxy wires (``confirm_alive``)."""
    _record(tmp_path, backend_401.port)
    verdict = await proxy_selfheal._confirm_bridge_verdict(
        backend_401.port, confirm_alive=singleton._same_identity_backend_ready
    )
    assert verdict == proxy_selfheal.CONNECTION_RESET_CAUSE


async def test_a_refused_connection_after_a_lost_connection_is_still_dead(
    tmp_path, dead_port, monkeypatch
):
    monkeypatch.setattr(proxy_selfheal, "_report", lambda *a, **k: None)
    _record(tmp_path, dead_port)
    verdict = await proxy_selfheal._confirm_bridge_verdict(
        dead_port, confirm_alive=singleton._same_identity_backend_ready
    )
    assert verdict == proxy_selfheal.CONNECTION_LOST_CAUSE


# -- review: the 401 must be OUR backend's, from OUR process ----------------


def test_a_401_in_anyone_elses_words_is_not_unauthorized(foreign_401):
    """(b) another auth-protected server's 401 is a stranger, not our backend."""
    assert backend_probe.probe(foreign_401.url, 2.0) == backend_probe.VERDICT_DEAD


@pytest.mark.parametrize(
    ("body", "scheme"),
    [
        (b"", None),
        (b'{"error":"no"}', "Basic"),
        (backend_token.UNAUTHORIZED_BODY, "Basic"),
        (b'{"error":"no"}', backend_token.SCHEME),
    ],
    ids=["bare", "foreign-both", "our-body-foreign-scheme", "foreign-body-our-scheme"],
)
def test_a_401_needs_both_our_body_and_our_scheme(body, scheme):
    headers = {} if scheme is None else {"www-authenticate": scheme}
    assert backend_token.is_our_refusal(headers, body) is False
    assert backend_token.is_our_refusal(
        {"www-authenticate": backend_token.SCHEME}, backend_token.UNAUTHORIZED_BODY
    )


def test_a_foreign_401_responder_is_not_reused_and_the_port_is_reclaimed(
    tmp_path, foreign_401, terminated
):
    """(a) our backend died, the record still says ours, and another server bound
    the port and answers 401: the gate says no, and ``clear_stale`` handles it
    like any other non-reusable occupant (the real terminate never touches a
    process that is not ours)."""
    _record(tmp_path, foreign_401.port)
    assert not singleton._same_identity_backend_ready(foreign_401.port, patience=0.0)
    singleton._clear_stale_backend(foreign_401.port)
    assert terminated == [foreign_401.port]


def test_our_401_from_a_process_that_is_not_our_recorded_backend_is_not_reused(
    tmp_path, backend_401, monkeypatch
):
    """The right words are not enough: the process LISTENING on the port must be
    our recorded backend. A lookup that finds nobody, or somebody else, is a no."""
    _record(tmp_path, backend_401.port)
    for listener in (None, OUR_PID + 1):
        monkeypatch.setattr(
            singleton, "_backend_pid_on_port", lambda p, who=listener: who
        )
        assert not singleton._same_identity_backend_ready(
            backend_401.port, patience=0.0
        )


def test_a_gate_pass_costs_one_initialize(tmp_path, backend_401):
    """MINOR: the ready probe and the refusal check used to be two initializes."""
    _record(tmp_path, backend_401.port)
    assert singleton._same_identity_backend_ready(backend_401.port, patience=0.0)
    assert [c for c, _ in backend_401.requests] == ["POST"]


# -- review: a 401 that never ends is cured by a restart --------------------


def test_the_cure_is_in_the_one_sentence():
    assert "stealthy restart" in backend_token.UNAUTHORIZED


def test_a_persistent_401_on_an_idle_backend_is_evicted_after_the_bound(
    tmp_path, backend_401, terminated
):
    _record(tmp_path, backend_401.port)
    clock = backend_probe._clock
    assert singleton._clear_stale_backend(backend_401.port) == []
    assert terminated == []  # the first sighting: waited out, as before
    clock.now += backend_probe.PERSISTENT_UNAUTHORIZED_SECONDS - 1
    assert singleton._clear_stale_backend(backend_401.port) == []
    assert terminated == []  # still inside the bound
    clock.now += 2
    assert singleton._clear_stale_backend(backend_401.port) == []
    assert terminated == [backend_401.port]  # past it: only a restart cures this


def test_a_persistent_401_is_never_evicted_from_a_backend_that_owns_browsers(
    tmp_path, backend_401, terminated, monkeypatch
):
    monkeypatch.setattr(singleton, "_protecting_browsers", lambda *a, **k: [4321])
    _record(tmp_path, backend_401.port)
    singleton._clear_stale_backend(backend_401.port)
    backend_probe._clock.now += backend_probe.PERSISTENT_UNAUTHORIZED_SECONDS + 1
    assert singleton._clear_stale_backend(backend_401.port) == [4321]
    assert terminated == []


def test_an_answer_that_is_not_a_401_ends_the_run(tmp_path, backend_401):
    _record(tmp_path, backend_401.port)
    singleton._same_identity_backend_ready(backend_401.port, patience=0.0)
    ok = _Backend(200)
    try:
        assert backend_probe.ready(ok.url, 2.0)
        assert ok.url not in backend_probe._unauthorized_since
    finally:
        ok.close()
    assert backend_401.url in backend_probe._unauthorized_since
    backend_probe._note(backend_401.url, backend_probe.VERDICT_DEAD)
    assert backend_401.url not in backend_probe._unauthorized_since


# -- review: status says the token is refused -------------------------------


def test_the_status_ladder_says_refusing_for_our_401_and_wedged_for_the_rest(
    backend_401, foreign_401
):
    assert singleton._probe_port(backend_401.port) == "refusing"
    assert singleton._probe_port(foreign_401.port) == "wedged"
    broken = _Backend(503)
    try:
        assert singleton._probe_port(broken.port) == "wedged"
    finally:
        broken.close()


def test_a_refusing_backend_is_reported_as_running_and_names_the_cure():
    line = cli._format_backend_status("refusing", 4321)
    assert "REFUSING" in line
    assert "stealthy restart" in line
    assert "not running" not in line
