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
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

_log = logging.getLogger("release_gate_harness")

# ── Contract constants ──────────────────────────────────────────────────────
SERVER_NAME = "stealth-chrome-devtools-mcp"
REGISTRY_TOOL_COUNT = 94  # remediation baseline (CLAUDE.md: derived == 94)
RESULT_SCHEMA_VERSION = 1
FIXTURE_APP_DIR = Path(__file__).resolve().parent / "fixture_app"

# ── Bounds (every await is wrapped; the pytest --timeout is the outer net) ──
INIT_TIMEOUT = 60.0  # initialize handshake (answered locally by the proxy)
LIST_TIMEOUT = 130.0  # first backend-bound call — covers backend cold start
SPAWN_TIMEOUT = 120.0  # first real Chrome launch
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
class _FixtureHandler(SimpleHTTPRequestHandler):
    """Static file server for tests/fixture_app + the plan_E2E §2.2 API routes."""

    def log_message(self, *args, **kwargs):
        """Silence per-request stderr logging (keeps test output clean)."""

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
        if self.path == "/api/echo":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            reflected = {key.lower(): value for key, value in self.headers.items()}
            self._send_json({"body": raw, "headers": reflected})
            return
        self.send_response(404)
        self.end_headers()


@contextlib.contextmanager
def serve_fixture_app():
    """Yield the ``http://127.0.0.1:<port>`` base URL of the fixture app server.

    Binds an ephemeral literal-IPv4 loopback port (so an IPv6-first ``localhost``
    can never cause a false failure), serves ``tests/fixture_app`` plus the §2.2
    routes on a daemon thread, and shuts the server down on exit. The caller owns
    the lifetime; the journey below owns its own instance so it can close it in a
    ``finally``.
    """
    handler = functools.partial(_FixtureHandler, directory=str(FIXTURE_APP_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Launcher resolver (standalone — W3 reuses it for each installed environment).
# ---------------------------------------------------------------------------
def resolve_launcher(interpreter: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the absolute installed ``stealth-chrome-devtools-mcp`` launcher.

    Given a target environment's interpreter (default: this process's), derive the
    console entry point from that environment's scripts directory —
    ``Scripts/stealth-chrome-devtools-mcp.exe`` on Windows,
    ``bin/stealth-chrome-devtools-mcp`` on POSIX — made absolute WITHOUT following
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
    name = (
        "stealth-chrome-devtools-mcp.exe"
        if os.name == "nt"
        else "stealth-chrome-devtools-mcp"
    )
    launcher = scripts_dir / name
    if not launcher.is_absolute():
        raise ValueError(f"resolved launcher is not absolute: {launcher}")
    if not launcher.exists() or not launcher.is_file():
        raise FileNotFoundError(
            f"console launcher {name!r} not found in {scripts_dir} "
            f"(resolved from interpreter {interp}); expected an installed entry point"
        )
    if os.name != "nt" and not os.access(launcher, os.X_OK):
        raise PermissionError(f"resolved launcher is not executable: {launcher}")
    _log.info("resolved absolute launcher: %s", launcher)
    return launcher


def _this_executable() -> str:
    import sys

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
    # Known STEALTH_MCP_* settings fields (env has ONE home = settings.py).
    env["STEALTH_MCP_BROWSER_SESSION_ROOT"] = str(session_root)
    env["STEALTH_MCP_CLONE_OUTPUT_DIR"] = str(clone_dir)
    env["STEALTH_MCP_LOG_DIR"] = str(log_dir)
    return env


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


async def _canonical_journey(
    client: Client, base_url: str, record: dict[str, Any]
) -> None:
    """spawn → navigate → tabs → interact → assert oracle+ground truth →
    structural extraction → PNG screenshot → close, all via ``tools/call``."""
    spawn_kwargs: dict[str, Any] = {"headless": True}
    with contextlib.suppress(Exception):
        from e2e_helpers import sandbox_kwargs

        spawn_kwargs.update(sandbox_kwargs())

    spawn = await _call(client, "spawn_browser", spawn_kwargs, SPAWN_TIMEOUT)
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
) -> dict[str, Any]:
    """Drive the canonical real-stdio journey against ``launcher`` and return a
    versioned, JSON-serializable result record (consumed unchanged by W3).

    ``work_dir`` is a throwaway directory (e.g. pytest ``tmp_path``) used for the
    isolated HOME (singleton state), session root, clone output, and logs.
    """
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
                        await _canonical_journey(client, base_url, record)
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
            f"--- child stderr (capped) ---\n{child_stderr}\n"
            f"--- backend boot log (capped) ---\n{boot_log}"
        ) from err
    if leftover:
        raise AssertionError(
            f"child process(es) remained after teardown: {sorted(leftover)}"
        )
    return record
