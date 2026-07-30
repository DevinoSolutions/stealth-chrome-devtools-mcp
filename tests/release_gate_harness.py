"""plan_RELEASE W1 — the ONE reusable real-stdio release-gate journey.

This module is the single home for the canonical transport E2E: it resolves the
**absolute installed console launcher**, proves ``fastmcp==2.11.2`` can spawn it
over stdio, and drives one real headless-Chrome journey ENTIRELY through
``tools/call`` against the local fixture app. ``tests/test_e2e_transport.py`` (W1)
and ``tools/install_smoke.py`` (W3) both import this unchanged — there is no
second smoke journey and no second fixture mechanism (conftest's
``fixture_app_server`` delegates to :func:`serve_fixture_app` here).

Exact ``fastmcp.Client`` stdio construction (the recorded foundation proof)::

    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=str(launcher),                    # absolute Scripts/…exe | bin/…
        args=["--singleton-port", str(port)],     # distinct port -> fresh backend
        env=child_env,                            # isolated HOME + session root
        keep_alive=False,                         # graceful child close on exit
    )
    async with Client(transport, init_timeout=INIT_TIMEOUT) as client:
        client.initialize_result           # serverInfo.name / .version, protocol
        await client.list_tools()          # 94 registry entries (baseline)
        await client.call_tool("list_instances", {})

Backend isolation (why this does not touch a live dev backend)
--------------------------------------------------------------
The launcher's ``server:main`` is a stdio→HTTP proxy backed by a source-
fingerprint singleton whose state (``server.json``, lock, port) lives under
``Path.home()/.stealth-mcp``. A dev backend on the default port is reused by any
session sharing that state dir. We therefore spawn an ISOLATED backend by, in the
child env only (never mutating the parent ``os.environ``):

* redirecting ``HOME``/``USERPROFILE`` to a throwaway dir, so the singleton's
  state dir is fresh and cannot reuse (or evict) the dev backend; and
* passing a distinct free ``--singleton-port``, so the fresh backend binds its own
  port instead of colliding with the dev backend's default port; and
* pinning ``STEALTH_MCP_BROWSER_SESSION_ROOT`` / clone / log dirs into the
  throwaway workspace, so Chrome profiles and artifacts never touch real state.

Teardown closes the browser instance and the fixture, lets the stdio child close
gracefully (``keep_alive=False``), terminates the detached backend recorded in the
isolated ``server.json``, and asserts no child process of this process remains.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import importlib.metadata
import inspect
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

import fixture_routes

_log = logging.getLogger("release_gate_harness")

# ── Contract constants ──────────────────────────────────────────────────────
# Which declared segment of the ONE gate mechanism `run_release_gate_journey`
# runs. These are extents/segments, never duplicated machinery: every value
# below shares the same launcher resolution, isolated env, fixture app, stdio
# client, and teardown. See that function's docstring for what each claims.
FULL_JOURNEY = "full"
HANDSHAKE_ONLY = "handshake"
COOKIE_ROUND_TRIP = "cookies"

SERVER_NAME = "stealth-chrome-devtools-mcp"
REGISTRY_TOOL_COUNT = 94  # remediation baseline (CLAUDE.md: derived == 94)
RESULT_SCHEMA_VERSION = 1
FIXTURE_APP_DIR = Path(__file__).resolve().parent / "fixture_app"

# ── Bounds (every await is wrapped; the pytest --timeout is the outer net) ──
INIT_TIMEOUT = 60.0  # initialize handshake (answered locally by the proxy)
LIST_TIMEOUT = 130.0  # first backend-bound call — covers backend cold start
SPAWN_TIMEOUT = 120.0  # first real Chrome launch
WARMUP_TIMEOUT = 150.0  # cold Chrome + master-profile bootstrap (best-effort)
WARMUP_ATTEMPTS = 4  # the cold launch intermittently fails outright, not just slowly
WARMUP_BACKOFF_SECONDS = 3.0  # multiplied by the attempt number; see _cold_start_warmup
NAV_TIMEOUT = 60.0
CALL_TIMEOUT = 45.0
CLOSE_TIMEOUT = 45.0
SETTLE_TIMEOUT = 20.0
TERMINATE_TIMEOUT = 15.0
CHILD_SETTLE_TIMEOUT = 15.0

# ── Diagnostic caps ─────────────────────────────────────────────────────────
_STDERR_CAP_BYTES = 64 * 1024
_STDERR_CAP_LINES = 200


# ---------------------------------------------------------------------------
# Fixture app — THE serving mechanism (conftest.fixture_app_server delegates
# here). Serves tests/fixture_app plus the plan_E2E §2.2 API routes over an
# ephemeral 127.0.0.1 port. Moved verbatim from conftest so there is one home.
# ---------------------------------------------------------------------------
# Diagnostics only: every request the fixture server actually served. When a
# navigation fails there is no other way to tell "Chrome never reached the
# server" (a network/launch problem) from "Chrome fetched the page but the load
# never completed" (a CDP/page problem) — the two have identical symptoms at the
# tool boundary. Bounded, and never asserted on.
_FIXTURE_HITS: list[str] = []
_FIXTURE_HITS_CAP = 50


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

    def handle_one_request(self):
        """Record the request line, then serve normally (stdlib override)."""
        super().handle_one_request()
        line = getattr(self, "raw_requestline", b"") or b""
        if line and len(_FIXTURE_HITS) < _FIXTURE_HITS_CAP:
            _FIXTURE_HITS.append(
                f"{self.client_address[0]} {line.decode('latin-1').strip()}"
            )

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


def _bind_origin(origin_state: dict) -> tuple[ThreadingHTTPServer, str]:
    """Bind one ephemeral literal-IPv4 loopback origin (not yet serving).

    Binding is separated from serving because a cross-linked PAIR cannot know
    either base URL until BOTH sockets have a port — so both bind, both learn
    the other's URL, and only then does either accept a request.
    """
    handler = functools.partial(
        _FixtureHandler, directory=str(FIXTURE_APP_DIR), origin_state=origin_state
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
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


# ---------------------------------------------------------------------------
# Launcher resolver (standalone — W3 reuses it for each installed environment).
# ---------------------------------------------------------------------------
def resolve_launcher(
    interpreter: str | os.PathLike[str] | None = None,
    *,
    name: str = "stealth-chrome-devtools-mcp",
) -> Path:
    """Resolve an absolute installed console launcher of this distribution.

    ``name`` selects which of the two declared ``[project.scripts]`` entry points
    to resolve; it defaults to the MCP server launcher this harness drives. W11's
    doc-example runner passes the ops CLI name so that both console scripts are
    resolved by this ONE resolver rather than a second copy of the rule.

    Given a target environment's interpreter (default: this process's), derive the
    console entry point from that environment's scripts directory —
    ``Scripts/<name>.exe`` on Windows, ``bin/<name>`` on POSIX — made absolute
    WITHOUT following
    symlinks (``Path.absolute()``, never ``Path.resolve()``: on POSIX a venv's
    ``bin/python`` is a symlink to the base interpreter, so resolving it escapes
    the venv into a scripts dir that has no entry points — exactly what the first
    Linux/macOS CI run hit). Requires an absolute, existing executable inside that
    environment. NEVER uses a bare command, mutates PATH, invokes ``python -m`` from
    the source tree, or falls back to another checkout.
    """
    interp = Path(
        interpreter if interpreter is not None else _this_executable()
    ).absolute()
    scripts_dir = interp.parent
    exe_name = f"{name}.exe" if os.name == "nt" else name
    launcher = scripts_dir / exe_name
    if not launcher.is_absolute():
        raise ValueError(f"resolved launcher is not absolute: {launcher}")
    if not launcher.exists() or not launcher.is_file():
        raise FileNotFoundError(
            f"console launcher {exe_name!r} not found in {scripts_dir} "
            f"(resolved from interpreter {interp}); expected an installed entry point"
        )
    if os.name != "nt" and not os.access(launcher, os.X_OK):
        raise PermissionError(f"resolved launcher is not executable: {launcher}")
    _log.info("resolved absolute launcher: %s", launcher)
    return launcher


def _this_executable() -> str:
    return sys.executable


# ---------------------------------------------------------------------------
# Isolation helpers.
# ---------------------------------------------------------------------------
def _pick_free_port() -> int:
    """An OS-assigned free loopback port, used as a distinct singleton port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _isolated_env(
    *, home_dir: Path, session_root: Path, log_dir: Path, clone_dir: Path
) -> dict[str, str]:
    """A copy of the parent env with the singleton state dir + all state paths
    redirected into the throwaway workspace. The parent ``os.environ`` is never
    mutated; every override is a known ``settings.py`` field or a home var.

    The redirect must be a COHERENT profile: Chrome 136+ on Windows refuses
    ``--remote-debugging-port`` ("DevTools remote debugging requires a
    non-default data directory") when ``USERPROFILE`` is redirected while
    ``LOCALAPPDATA`` still points into the real profile — even with an explicit
    non-default ``--user-data-dir``. Pointing ``LOCALAPPDATA``/``APPDATA`` at an
    ``AppData`` skeleton inside the new home restores debugging (empirically
    verified) and keeps Chrome's crashpad/GCM writes inside the throwaway."""
    env = dict(os.environ)
    # Redirect Path.home() so ~/.stealth-mcp (singleton state) is fresh & isolated.
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    local_appdata = home_dir / "AppData" / "Local"
    roaming_appdata = home_dir / "AppData" / "Roaming"
    local_appdata.mkdir(parents=True, exist_ok=True)
    roaming_appdata.mkdir(parents=True, exist_ok=True)
    env["LOCALAPPDATA"] = str(local_appdata)
    env["APPDATA"] = str(roaming_appdata)
    # Same coherence rule for the macOS home: Chrome derives Application
    # Support / Caches from HOME with no env var to point at them, so the
    # skeleton is what makes the redirect coherent there. Created on every OS
    # (cheap, inert off-macOS) so the isolated home has ONE shape everywhere.
    for mac_dir in ("Application Support", "Caches", "Preferences"):
        (home_dir / "Library" / mac_dir).mkdir(parents=True, exist_ok=True)
    # Known STEALTH_MCP_* settings fields (env has ONE home = settings.py).
    env["STEALTH_MCP_BROWSER_SESSION_ROOT"] = str(session_root)
    env["STEALTH_MCP_CLONE_OUTPUT_DIR"] = str(clone_dir)
    env["STEALTH_MCP_LOG_DIR"] = str(log_dir)
    return env


def _chrome_process_snapshot() -> list[str]:
    """The live Chrome process tree by ``--type=``, captured while it is stalled.

    Chrome runs its network stack in a separate ``--type=utility`` process
    (``network.mojom.NetworkService``). When a navigation hangs but
    ``about:blank`` returns instantly, whether that process EXISTS separates
    "Chrome cannot reach the network" from "Chrome never asked it to" — and
    nothing at the CDP boundary can tell those apart. Diagnostics only.
    """
    out: list[str] = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "chrom" not in name:
                continue
            cmdline = proc.info.get("cmdline") or []
            kinds = [
                arg
                for arg in cmdline
                if arg.startswith("--type=") or "utility-sub-type" in arg
            ]
            out.append(
                f"pid={proc.pid} {name} {' '.join(kinds) or '(browser process)'}"
            )
        except psutil.Error:
            continue
    return out


def gate_work_dir(fallback: Path) -> Path:
    """The throwaway workspace root for a gate journey (profiles, HOME, logs).

    Prefers the CI runner's own temp when ``RUNNER_TEMP`` is set, falling back to
    the caller's (usually pytest ``tmp_path``) everywhere else.

    Why not just use pytest's tmp_path on CI: on macOS that resolves into
    ``/private/var/folders/...``, the sandboxed per-user temp, and it is the one
    concrete difference between the macOS cases that pass and the one that does
    not — in-process integration runs its Chrome profile under ``RUNNER_TEMP``
    (``/Users/runner/work/_temp``) and navigates fine, while this journey ran
    under ``/private/var/folders`` and could not complete ANY network navigation
    (a connection to a CLOSED port hung too, with Fetch interception proven off,
    so nothing product-side was pausing it). Same OS, same Chrome, same code.

    The caller still owns the directory's lifetime; W3's install smoke gets this
    policy for free by calling the same helper.
    """
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp and Path(runner_temp).is_dir():
        return Path(tempfile.mkdtemp(prefix="gate-", dir=runner_temp))
    return fallback


def _backend_logs(*dirs: Path) -> str:
    """The isolated backend's own logs, for failures the exception text alone
    cannot explain.

    The journey runs the backend as a DETACHED child, so a tool failure arrives
    as a bare protocol error — the reason (Chrome launch args, CDP stalls, the
    orphan-recovery lines that root-caused B1) is only in the backend's log
    files, which die with the throwaway home. Cheap on the happy path: only
    read when the journey has already failed.
    """
    seen: set[Path] = set()
    chunks: list[str] = []
    for d in (*dirs, *(p / ".stealth-mcp" / "logs" for p in dirs)):
        if not d.is_dir():
            continue
        for log in sorted(d.glob("*.log"), key=lambda p: p.stat().st_mtime)[-2:]:
            if log in seen:
                continue
            seen.add(log)
            chunks.append(f"[{log.name}]\n{_read_capped(log)}")
    return "\n".join(chunks) if chunks else "(no backend log files found)"


def _backend_pid_from_state(home_dir: Path) -> int | None:
    """Read the isolated backend's recorded pid from its ``server.json``."""
    state_file = home_dir / ".stealth-mcp" / "server.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    pid = state.get("pid")
    return pid if isinstance(pid, int) else None


def _pid_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running()
    except psutil.Error:
        return False


def _terminate_process_tree(pid: int, timeout: float) -> bool:
    """Terminate ``pid`` and its descendants (bounded graceful, then kill)."""
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return False
    procs = []
    with contextlib.suppress(psutil.Error):
        procs = proc.children(recursive=True)
    procs.append(proc)
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.terminate()
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        with contextlib.suppress(psutil.Error):
            p.kill()
    return True


def _child_pids(parent: psutil.Process) -> set[int]:
    try:
        return {c.pid for c in parent.children(recursive=True)}
    except psutil.Error:
        return set()


def _await_children_settle(
    parent: psutil.Process, before: set[int], timeout: float
) -> set[int]:
    """Poll until children spawned during the run are gone; return any leftover."""
    deadline = time.monotonic() + timeout
    while True:
        new = _child_pids(parent) - before
        if not new or time.monotonic() >= deadline:
            return new
        time.sleep(0.25)


def _assert_ipv4_loopback(base_url: str) -> None:
    host = urlparse(base_url).hostname
    if host != "127.0.0.1":
        raise AssertionError(f"fixture URL must be literal IPv4 loopback, got {host!r}")


def _read_capped(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _cap_text(text)


def _cap_text(text: str) -> str:
    lines = text.splitlines()
    if len(lines) > _STDERR_CAP_LINES:
        lines = lines[-_STDERR_CAP_LINES:]
    capped = "\n".join(lines)
    if len(capped) > _STDERR_CAP_BYTES:
        capped = capped[-_STDERR_CAP_BYTES:]
    return capped


@contextlib.contextmanager
def _capture_stderr_fd():
    """Redirect OS fd 2 to a temp file for the child's lifetime; on exit expose the
    captured, capped text in ``holder['text']``.

    fastmcp's ``StdioTransport`` wires the child's stderr to ``sys.stderr`` (fd 2);
    stdout is a private JSON-RPC pipe untouched here. Redirecting fd 2 captures the
    child's diagnostics without polluting protocol stdout, in or out of pytest.
    Best-effort: any failure leaves stderr untouched and yields no capture.
    """
    holder = {"text": ""}
    import sys

    try:
        sys.stderr.flush()
        saved = os.dup(2)
    except (OSError, ValueError):
        yield holder
        return
    try:
        with tempfile.TemporaryFile(mode="w+b") as tmp:
            os.dup2(tmp.fileno(), 2)
            try:
                yield holder
            finally:
                with contextlib.suppress(OSError, ValueError):
                    sys.stderr.flush()
                with contextlib.suppress(OSError, ValueError):
                    os.dup2(saved, 2)
                with contextlib.suppress(OSError, ValueError):
                    tmp.seek(0)
                    holder["text"] = _cap_text(tmp.read().decode("utf-8", "replace"))
    finally:
        with contextlib.suppress(OSError):
            os.close(saved)


# ---------------------------------------------------------------------------
# tools/call helpers.
# ---------------------------------------------------------------------------
async def _call(
    client: Client,
    name: str,
    args: dict[str, Any],
    timeout: float,
    *,
    raise_on_error: bool = True,
    allow_fail: bool = False,
) -> Any:
    """Call a tool through the real protocol, bounded, returning the plain
    wire-shape result (raw ``structuredContent`` JSON, not fastmcp's pydantic
    ``Root`` reconstruction — the release gate asserts what a user's client
    actually receives)."""
    coro = client.call_tool(name, args, raise_on_error=raise_on_error)
    try:
        result = await asyncio.wait_for(coro, timeout)
    except Exception:
        if allow_fail:
            return None
        raise
    structured = result.structured_content
    if structured is not None:
        # FastMCP wraps non-object returns as {"result": X} on the wire.
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    return result.data


async def _eval(client: Client, iid: str, expression: str) -> Any:
    """Evaluate JS via ``execute_script`` and return its result (assert success)."""
    result = await _call(
        client,
        "execute_script",
        {"instance_id": iid, "script": expression},
        CALL_TIMEOUT,
    )
    assert isinstance(result, dict) and result.get("success") is True, result
    return result.get("result")


async def _settle_dom(client: Client, iid: str) -> None:
    """Bounded-poll ``query_elements('body')`` until the document is queryable.

    After navigation nodriver's cached document node is transiently stale, so the
    first DOM-node-path call can fail; ``query_elements`` is the safe probe (it
    returns ``[]`` rather than raising). Mirrors ``navigate_and_settle`` in
    ``e2e_helpers`` but over the real transport.
    """
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while True:
        body = await _call(
            client,
            "query_elements",
            {"instance_id": iid, "selector": "body"},
            CALL_TIMEOUT,
            raise_on_error=False,
            allow_fail=True,
        )
        if isinstance(body, list) and body:
            return
        if time.monotonic() >= deadline:
            return  # best-effort; a later assert surfaces the real failure
        await asyncio.sleep(0.25)


def _decode_screenshot(shot: Any) -> bytes:
    if isinstance(shot, str):
        return base64.b64decode(shot)
    if isinstance(shot, dict) and shot.get("file_path"):
        return Path(shot["file_path"]).read_bytes()
    raise AssertionError(f"unexpected take_screenshot result: {type(shot).__name__}")


# ---------------------------------------------------------------------------
# Foundation proof + schema/handshake + canonical journey.
# ---------------------------------------------------------------------------
async def _foundation_proof(client: Client, record: dict[str, Any]) -> None:
    """Protocol + server identity + 94-tool registry + a real tool call."""
    init = client.initialize_result
    assert init is not None, "no initialize result (handshake did not complete)"
    server_info = init.serverInfo
    assert server_info.name == SERVER_NAME, server_info.name
    installed = importlib.metadata.version(SERVER_NAME)
    assert server_info.version == installed, (server_info.version, installed)
    assert init.protocolVersion, "empty protocolVersion"
    record["server"] = {
        "name": server_info.name,
        "version": server_info.version,
        "protocol_version": init.protocolVersion,
    }

    tools = await asyncio.wait_for(client.list_tools(), LIST_TIMEOUT)
    names = [t.name for t in tools]
    assert len(names) == REGISTRY_TOOL_COUNT, (
        f"{len(names)} tools != {REGISTRY_TOOL_COUNT}"
    )
    assert len(set(names)) == len(names), "duplicate tool names in tools/list"
    for tool in tools:
        schema = tool.inputSchema
        assert isinstance(schema, dict) and schema.get("type") == "object", tool.name
    record["tool_count"] = len(names)

    instances = await _call(client, "list_instances", {}, CALL_TIMEOUT)
    assert isinstance(instances, list), instances
    record["list_instances_initial_count"] = len(instances)


async def _representative_parity(client: Client, record: dict[str, Any]) -> None:
    """One tools/call result is shape-equivalent to the in-process ``.fn`` seam.

    Uses ``list_cdp_commands`` (deterministic, browser-free): the protocol result
    and the ``get_fn`` seam (``e2e_helpers``) must carry the same set of names,
    without requiring byte-identical incidental formatting.
    """
    try:
        from e2e_helpers import get_fn
    except Exception:  # pragma: no cover - only when the src seam is unavailable
        record["representative_parity"] = {
            "tool": "list_cdp_commands",
            "seam_available": False,
        }
        return
    seam = get_fn("list_cdp_commands")()
    if inspect.isawaitable(seam):
        seam = await seam
    proto = await _call(client, "list_cdp_commands", {}, CALL_TIMEOUT)
    assert isinstance(proto, list) and isinstance(seam, list), (type(proto), type(seam))
    assert {str(x) for x in proto} == {str(x) for x in seam}, (
        "protocol vs seam mismatch"
    )
    record["representative_parity"] = {
        "tool": "list_cdp_commands",
        "protocol_len": len(proto),
        "seam_len": len(seam),
        "equal": True,
        "seam_available": True,
    }


def _headless_spawn_kwargs(**extra: Any) -> dict[str, Any]:
    """``{'headless': True}`` plus this environment's sandbox policy.

    No ``--use-mock-keychain`` here, deliberately: an earlier round passed it to
    rule out macOS keychain blocking, and the backend log showed the product's
    own stealth filter removing it ("Stripped 1 detectable arg(s):
    --use-mock-keychain stripped: Playwright default"). It never reached Chrome,
    so it tested nothing — keeping it would be an inert flag that also trips a
    stealth warning on every spawn.
    """
    kwargs: dict[str, Any] = {"headless": True, **extra}
    with contextlib.suppress(Exception):
        from e2e_helpers import sandbox_kwargs

        kwargs.update(sandbox_kwargs())
    return kwargs


async def _cold_start_warmup(
    client: Client, base_url: str, log_dir: Path, record: dict[str, Any]
) -> None:
    """Absorb the first-Chrome-launch cost BEFORE the measured journey.

    The backend's first spawn into a fresh browser-session root pays for both a
    Chrome cold start and the master-profile bootstrap that clone-on-spawn
    needs. On the macOS CI image that cold path is slow enough to exceed
    nodriver's internal connect patience (spawn: "Failed to connect to
    browser") or the product's CDP deadline on the first navigate — while the
    54 in-process integration tests pass on the same image, every one of them
    behind :func:`e2e_helpers.warmup_once` ("the first Chrome launch on CI is
    slow / flaky"). This is that same warmup at the wire level, for the same
    reason and with the same best-effort contract — not a second warmup
    convention: the journey drives a SEPARATE backend process, so it cannot
    share the in-process one.

    It warms BOTH cold paths, because they are separately expensive: the launch
    (Chrome binary + master-profile bootstrap) and the first page LOAD (network
    service, renderer, font cache). A spawn-only warmup left the journey's first
    navigate still cold — which is exactly where macOS kept failing.

    Bounded retry, because the cold launch is not merely slow but intermittently
    FAILS ("Failed to connect to browser" — seen on the Linux runner defeating
    even the in-process warmup). Retrying warmup costs nothing and asserts
    nothing.

    Best-effort by construction: the outcome is recorded and never fatal. It
    proves nothing and is asserted on by nobody — the canonical journey below
    remains the sole evidence and is still asserted in full.
    """
    warmup: dict[str, Any] = {"attempted": True, "ok": False, "attempts": 0}
    record["cold_start_warmup"] = warmup
    for attempt in range(1, WARMUP_ATTEMPTS + 1):
        warmup["attempts"] = attempt
        if attempt > 1:
            # BACK OFF before retrying. "Failed to connect to browser" is a fast
            # connect failure, not a timeout — a bigger WARMUP_TIMEOUT cannot fix
            # it, and an immediate retry meets exactly the resource contention
            # that just failed. Observed defeating both attempts on Linux/X64
            # runners simultaneously across two PRs, taking `transport` and
            # `install-smoke (wheel)` down with it. Same shape as the backoff
            # e2e_helpers.warmup_once already uses for the in-process warmup.
            await asyncio.sleep(WARMUP_BACKOFF_SECONDS * attempt)
        iid: str | None = None
        try:
            # Chrome's OWN log, into log_dir so the failure dump picks it up with
            # the backend logs. Neither flag is in the product's stealth-arg
            # blocklist, so unlike --use-mock-keychain these actually reach
            # Chrome. Warmup instance only: the canonical journey below stays a
            # clean default spawn.
            spawn = await _call(
                client,
                "spawn_browser",
                _headless_spawn_kwargs(
                    user_data_dir="release-gate-warmup",
                    browser_args=[
                        "--enable-logging",
                        f"--log-file={log_dir / 'chrome-warmup.log'}",
                    ],
                ),
                WARMUP_TIMEOUT,
            )
            iid = spawn.get("instance_id") if isinstance(spawn, dict) else None
            if not iid:
                continue
            # Diagnostic probe (recorded, never asserted): "about:blank" needs no
            # NETWORK request, so Chrome's Fetch interception — which the product
            # enables catch-all on every spawn even with zero hooks — has nothing
            # to pause. A real URL does. If blank succeeds where http hangs, the
            # stall is the paused-request path, not the tab, the CDP session, or
            # reachability. Cheap, and it turns the next red into an answer.
            # "refused" targets a port nothing listens on: the OS answers with
            # an immediate ECONNREFUSED, so Chrome renders an error page fast.
            # It separates the two remaining causes, which the fixture URL alone
            # cannot: if a REFUSED connection also hangs, the request never
            # reached the network stack at all (the paused-Fetch path — a
            # product bug); if it errors fast while the fixture port hangs, the
            # network stack is fine and something is dropping that connection
            # (a runner/environment problem).
            for label, url in (
                ("about_blank", "about:blank"),
                ("refused", "http://127.0.0.1:1/"),
                ("http", f"{base_url}/index.html"),
            ):
                started = time.monotonic()
                try:
                    await _call(
                        client,
                        "navigate",
                        {"instance_id": iid, "url": url},
                        NAV_TIMEOUT,
                    )
                    outcome = "ok"
                except BaseException as exc:  # noqa: BLE001  PERMANENT(probe records the failure shape; the journey below is what actually gates)
                    outcome = f"{type(exc).__name__}: {exc}"
                warmup.setdefault("nav_probe", {})[label] = {
                    "outcome": outcome,
                    "seconds": round(time.monotonic() - started, 1),
                }
                if outcome != "ok" and "chrome_processes" not in warmup:
                    # Snapshot WHILE the navigation is stalled and Chrome is
                    # still alive — after teardown the evidence is gone.
                    warmup["chrome_processes"] = _chrome_process_snapshot()
            warmup["ok"] = True
            warmup.pop("error", None)
            return
        except BaseException as exc:  # noqa: BLE001  PERMANENT(warmup is best-effort: record the reason, never fail the gate on it)
            warmup["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if iid:
                with contextlib.suppress(Exception):
                    await _call(
                        client, "close_instance", {"instance_id": iid}, CLOSE_TIMEOUT
                    )


async def _cookie_round_trip(
    client: Client, iid: str, page_url: str, journey: dict[str, Any]
) -> None:
    """plan_RELEASE W5 — the real success path for ``get_cookies``.

    W5's ``get_cookies`` hard block (§2.5) requires a real Chrome + real
    transport test that **sets a cookie, retrieves it, and asserts its value**;
    a schema check, a mock, a missing-instance error, or a characterization
    explicitly cannot satisfy it. This does the full round trip through
    ``tools/call`` only:

    ``set_cookie`` → ``get_cookies(urls=[page_url])`` → **assert the value** →
    ``get_cookies()`` (the no-argument default, a different CDP call) →
    ``document.cookie`` cross-check → ``clear_cookies`` → assert it is gone.

    The value is unique per run, so a stale cookie from an earlier run in a
    reused profile cannot forge the assertion, and the ``document.cookie``
    cross-check proves the cookie really reached the browser rather than
    ``get_cookies`` merely echoing what ``set_cookie`` was handed.

    ``get_cookies`` is declared ``-> list[dict[str, Any]]`` but returns nodriver
    ``cdp.network.Cookie`` **dataclasses**. pydantic serializes those to correct
    JSON objects, so ``structuredContent`` — what ``_call`` returns, and what a
    user's client receives — is a list of cookie dicts. Asserting through
    fastmcp's ``result.data`` instead would see an opaque ``[Root()]``; that is
    a property of the reconstruction, not a product defect.
    """
    name = "release_gate_cookie"
    value = f"w5-{uuid.uuid4().hex}"
    cookies: dict[str, Any] = {"name": name, "value": value}
    journey["cookies"] = cookies

    assert await _call(
        client,
        "set_cookie",
        {"instance_id": iid, "name": name, "value": value, "url": page_url},
        CALL_TIMEOUT,
    ), "set_cookie did not report success"

    # Retrieval #1 — scoped to the page URL (CDP Network.getCookies).
    scoped = await _call(
        client, "get_cookies", {"instance_id": iid, "urls": [page_url]}, CALL_TIMEOUT
    )
    assert isinstance(scoped, list) and scoped, (
        f"get_cookies(urls=…) returned {scoped!r}"
    )
    assert all(isinstance(c, dict) for c in scoped), (
        f"get_cookies did not serialize to dicts: {[type(c).__name__ for c in scoped]}"
    )
    scoped_hit = next((c for c in scoped if c.get("name") == name), None)
    assert scoped_hit is not None, f"{name!r} absent from get_cookies(urls=…): {scoped}"
    # THE assertion W5's hard block turns on.
    assert scoped_hit.get("value") == value, (scoped_hit.get("value"), value)
    cookies["scoped_value"] = scoped_hit["value"]
    cookies["scoped_count"] = len(scoped)
    cookies["field_names"] = sorted(scoped_hit)

    # Retrieval #2 — the no-argument default (CDP Network.getAllCookies).
    every = await _call(client, "get_cookies", {"instance_id": iid}, CALL_TIMEOUT)
    assert isinstance(every, list), f"get_cookies() returned {every!r}"
    all_hit = next(
        (c for c in every if isinstance(c, dict) and c.get("name") == name), None
    )
    assert all_hit is not None, f"{name!r} absent from get_cookies(): {every}"
    assert all_hit.get("value") == value, (all_hit.get("value"), value)
    cookies["all_value"] = all_hit["value"]

    # Ground truth: the cookie is really in the browser, not just in our reply.
    document_cookie = await _eval(client, iid, "document.cookie")
    assert f"{name}={value}" in str(document_cookie), document_cookie
    cookies["document_cookie_confirms"] = True

    # clear_cookies is part of the same real path — prove removal by re-reading,
    # not by trusting its return value.
    assert await _call(
        client, "clear_cookies", {"instance_id": iid, "url": page_url}, CALL_TIMEOUT
    ), "clear_cookies did not report success"
    after = await _call(
        client, "get_cookies", {"instance_id": iid, "urls": [page_url]}, CALL_TIMEOUT
    )
    assert isinstance(after, list), after
    assert not any(isinstance(c, dict) and c.get("name") == name for c in after), (
        f"{name!r} survived clear_cookies: {after}"
    )
    cookies["cleared"] = True


async def _cookie_journey(
    client: Client, base_url: str, record: dict[str, Any]
) -> None:
    """W5's segment: spawn → navigate → cookie round trip → close.

    Deliberately minimal — everything here exists to make the cookie assertions
    meaningful, so a failure reads as a cookie failure rather than as "the
    journey" failing. A real page (not ``about:blank``) is required because
    cookies need a real http:// origin, and the fixture app is that origin.
    """
    spawn = await _call(
        client, "spawn_browser", _headless_spawn_kwargs(), SPAWN_TIMEOUT
    )
    assert isinstance(spawn, dict) and spawn.get("instance_id"), spawn
    iid = spawn["instance_id"]
    journey: dict[str, Any] = {"instance_id": iid}
    record["journey"] = journey
    try:
        url = f"{base_url}/interact.html"
        await _call(client, "navigate", {"instance_id": iid, "url": url}, NAV_TIMEOUT)
        journey["navigated_url"] = url
        await _settle_dom(client, iid)

        await _cookie_round_trip(client, iid, url, journey)
    finally:
        await _call(
            client,
            "close_instance",
            {"instance_id": iid},
            CLOSE_TIMEOUT,
            raise_on_error=False,
            allow_fail=True,
        )


async def _canonical_journey(
    client: Client, base_url: str, record: dict[str, Any]
) -> None:
    """spawn → navigate → tabs → interact → assert oracle+ground truth →
    structural extraction → PNG screenshot → close, all via ``tools/call``."""
    spawn = await _call(
        client, "spawn_browser", _headless_spawn_kwargs(), SPAWN_TIMEOUT
    )
    assert isinstance(spawn, dict) and spawn.get("instance_id"), spawn
    iid = spawn["instance_id"]
    journey: dict[str, Any] = {"instance_id": iid, "spawn_state": spawn.get("state")}
    record["journey"] = journey
    try:
        url = f"{base_url}/interact.html"
        await _call(client, "navigate", {"instance_id": iid, "url": url}, NAV_TIMEOUT)
        journey["navigated_url"] = url
        await _settle_dom(client, iid)

        tabs = await _call(client, "list_tabs", {"instance_id": iid}, CALL_TIMEOUT)
        assert isinstance(tabs, list) and tabs and all("tab_id" in t for t in tabs), (
            tabs
        )
        journey["tabs_count"] = len(tabs)
        active = await _call(
            client, "get_active_tab", {"instance_id": iid}, CALL_TIMEOUT
        )
        assert isinstance(active, dict) and active.get("tab_id"), active

        assert await _call(
            client,
            "click_element",
            {"instance_id": iid, "selector": "#btn-counter"},
            CALL_TIMEOUT,
        )
        assert await _call(
            client,
            "type_text",
            {"instance_id": iid, "selector": "#text-input", "text": "hello"},
            CALL_TIMEOUT,
        )
        assert await _call(
            client,
            "select_option",
            {"instance_id": iid, "selector": "#select-single", "value": "beta"},
            CALL_TIMEOUT,
        )

        # Fixture action oracle (proof the RIGHT action fired) via execute_script.
        actions = json.loads(
            await _eval(client, iid, "JSON.stringify(window.__actions)")
        )
        assert "click:btn-counter" in actions, actions
        assert "change:select-single:beta" in actions, actions
        journey["actions"] = actions

        # Live-DOM ground truth.
        counter = await _eval(
            client, iid, "document.getElementById('counter-value').textContent"
        )
        assert counter == "1", counter
        journey["counter_value"] = counter
        text_value = await _eval(
            client, iid, "document.getElementById('text-input').value"
        )
        assert text_value == "hello", text_value
        journey["text_input_value"] = text_value
        select_value = await _eval(
            client, iid, "document.getElementById('select-single').value"
        )
        assert select_value == "beta", select_value
        journey["select_value"] = select_value
        title = await _eval(client, iid, "document.title")
        assert title == "fixture-interact-page", title
        journey["title"] = title

        content = await _call(
            client, "get_page_content", {"instance_id": iid}, CALL_TIMEOUT
        )
        assert "fixture-interact-page" in json.dumps(content, default=str)

        # One structural extraction.
        structure = await _call(
            client,
            "extract_element_structure",
            {"instance_id": iid, "selector": "#select-single"},
            CALL_TIMEOUT,
        )
        assert isinstance(structure, dict), structure
        journey["structure_tag"] = str(
            structure.get("tag_name") or structure.get("tagName") or ""
        ).lower()

        # PNG-magic screenshot (format='png' re-encodes through PIL to PNG).
        shot = await _call(
            client,
            "take_screenshot",
            {"instance_id": iid, "format": "png"},
            CALL_TIMEOUT,
        )
        png = _decode_screenshot(shot)
        assert png[:8] == b"\x89PNG\r\n\x1a\n", png[:8]
        journey["screenshot_bytes"] = len(png)
        journey["screenshot_format"] = "png"
    finally:
        await _call(
            client,
            "close_instance",
            {"instance_id": iid},
            CLOSE_TIMEOUT,
            raise_on_error=False,
            allow_fail=True,
        )


# ---------------------------------------------------------------------------
# The one public entry point (imported unchanged by W1's test and W3's smoke).
# ---------------------------------------------------------------------------
async def run_release_gate_journey(
    *,
    launcher: str | os.PathLike[str],
    work_dir: str | os.PathLike[str],
    singleton_port: int | None = None,
    stages: str = FULL_JOURNEY,
) -> dict[str, Any]:
    """Drive the canonical real-stdio journey against ``launcher`` and return a
    versioned, JSON-serializable result record (consumed unchanged by W3).

    ``work_dir`` is a throwaway directory (e.g. pytest ``tmp_path``) used for the
    isolated HOME (singleton state), session root, clone output, and logs.

    ``stages`` selects which declared segment runs on top of the ONE shared
    mechanism (launcher resolution, isolated env, fixture app, stdio client,
    teardown). It never duplicates that machinery:

    ``"full"`` (default)
        everything: handshake, registry, parity, cold-start warmup, and the
        navigating canonical journey. This is what W1's transport test and
        every non-macOS W3 smoke cell run.
    ``"handshake"``
        stops after the non-navigating prefix (initialize → ``tools/list`` →
        ``list_instances`` → the representative parity call). W3 uses it for
        the macOS/ARM64 install-smoke cells ONLY, because F-773 makes any
        navigation through the detached backend hang on hosted macOS runners.
        It is a genuinely reduced claim — "this artifact installs and serves"
        — and the result record says so in ``stages`` so no consumer can read
        it as the full journey. Not an xfail: nothing failing is being marked
        as expected-to-fail; a smaller thing is being run and labelled.
    ``"cookies"``
        the prefix plus warmup, then the focused ``set_cookie`` →
        ``get_cookies`` → ``clear_cookies`` round trip (``_cookie_journey``)
        instead of the canonical journey. plan_RELEASE §2.5 rules that a
        *representative journey* cannot carry a per-tool success claim, so W5's
        ``get_cookies`` evidence needs its own collected node
        (``tests/test_e2e_transport_cookies.py``); this segment is what that
        node drives. It navigates, so ``navigation_verified`` is true, but it
        makes no canonical-journey claim.
    """
    valid_stages = (FULL_JOURNEY, HANDSHAKE_ONLY, COOKIE_ROUND_TRIP)
    if stages not in valid_stages:
        raise ValueError(f"stages must be one of {valid_stages!r}, got {stages!r}")
    launcher = Path(launcher)
    work_dir = Path(work_dir)
    home_dir = work_dir / "home"
    session_root = work_dir / "session-root"
    log_dir = work_dir / "logs"
    clone_dir = work_dir / "clone-output"
    for directory in (home_dir, session_root, log_dir, clone_dir):
        directory.mkdir(parents=True, exist_ok=True)

    port = singleton_port or _pick_free_port()
    child_env = _isolated_env(
        home_dir=home_dir,
        session_root=session_root,
        log_dir=log_dir,
        clone_dir=clone_dir,
    )

    record: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "stages": stages,
        "navigation_verified": stages in (FULL_JOURNEY, COOKIE_ROUND_TRIP),
        "transport": "stdio",
        "launcher": str(launcher.resolve()),
        "singleton_port": port,
        # A successful JSON-RPC handshake + list + calls over the private stdout
        # pipe proves the server wrote only framing to stdout (a stray non-JSON
        # line would fail the client's line parser).
        "stdout_framing_only": True,
    }

    parent = psutil.Process()
    children_before = _child_pids(parent)
    child_stderr = ""
    err: BaseException | None = None
    backend_pid: int | None = None

    try:
        with serve_fixture_app() as base_url:
            _assert_ipv4_loopback(base_url)
            record["fixture_base_url"] = base_url
            transport = StdioTransport(
                command=str(launcher),
                args=["--singleton-port", str(port)],
                env=child_env,
                keep_alive=False,
            )
            with _capture_stderr_fd() as cap:
                try:
                    async with Client(transport, init_timeout=INIT_TIMEOUT) as client:
                        await _foundation_proof(client, record)
                        await _representative_parity(client, record)
                        if stages in (FULL_JOURNEY, COOKIE_ROUND_TRIP):
                            await _cold_start_warmup(client, base_url, log_dir, record)
                        if stages == FULL_JOURNEY:
                            await _canonical_journey(client, base_url, record)
                        elif stages == COOKIE_ROUND_TRIP:
                            await _cookie_journey(client, base_url, record)
                except BaseException as exc:  # noqa: BLE001  PERMANENT(augment with child stderr + boot log, then re-raise)
                    err = exc
            child_stderr = cap["text"]
    finally:
        # The detached backend usually dies with the proxy (keep_alive=False);
        # terminate is the bounded backstop. The release claim is "no backend
        # process remains", not "we were the ones to kill it".
        backend_pid = _backend_pid_from_state(home_dir)
        if backend_pid is not None and _pid_running(backend_pid):
            _terminate_process_tree(backend_pid, TERMINATE_TIMEOUT)
        leftover = _await_children_settle(parent, children_before, CHILD_SETTLE_TIMEOUT)
        for pid in leftover:
            _terminate_process_tree(pid, 5.0)

    record["backend_pid_recorded"] = backend_pid is not None
    record["backend_gone"] = backend_pid is None or not _pid_running(backend_pid)
    record["no_child_remaining"] = not leftover

    if err is not None:
        boot_log = _read_capped(home_dir / ".stealth-mcp" / "logs" / "backend-boot.log")
        raise RuntimeError(
            "release-gate journey failed: "
            f"{type(err).__name__}: {err}\n"
            f"--- warmup / nav probe ---\n{json.dumps(record.get('cold_start_warmup'))}\n"
            f"--- fixture server hits ({len(_FIXTURE_HITS)}) ---\n"
            f"{chr(10).join(_FIXTURE_HITS) or '(the fixture server served NOTHING)'}\n"
            f"--- backend logs (capped) ---\n{_backend_logs(log_dir, home_dir)}\n"
            f"--- child stderr (capped) ---\n{child_stderr}\n"
            f"--- backend boot log (capped) ---\n{boot_log}"
        ) from err
    if leftover:
        raise AssertionError(
            f"child process(es) remained after teardown: {sorted(leftover)}"
        )
    return record
