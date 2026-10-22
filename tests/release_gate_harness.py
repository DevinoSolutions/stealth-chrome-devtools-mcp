"""plan_RELEASE W1 — the ONE reusable real-stdio release-gate journey.

This module is the single home for the canonical transport E2E: it resolves the
**absolute installed console launcher**, proves the pinned ``fastmcp`` can spawn it
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
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from release_gate_harness_consts import (
    _STDERR_CAP_BYTES,
    _STDERR_CAP_LINES,
    CALL_TIMEOUT,
    CHILD_SETTLE_TIMEOUT,
    CLOSE_TIMEOUT,
    COOKIE_ROUND_TRIP,
    FIXTURE_APP_DIR,
    FULL_JOURNEY,
    HANDSHAKE_ONLY,
    INIT_TIMEOUT,
    LIST_TIMEOUT,
    LOGIN_COOKIE,
    LOGIN_COOKIE_VALUE,
    NAV_TIMEOUT,
    REGISTRY_TOOL_COUNT,
    RESULT_SCHEMA_VERSION,
    SERVER_NAME,
    SETTLE_TIMEOUT,
    SOAK_BAD_HOST,
    SOAK_CYCLES,
    SOAK_MISSING_SELECTOR_WAIT_MS,
    SOAK_OP_TIMEOUT,
    SOAK_PAGES,
    SOAK_STABILITY,
    SPAWN_TIMEOUT,
    TERMINATE_TIMEOUT,
    WARMUP_ATTEMPTS,
    WARMUP_BACKOFF_SECONDS,
    WARMUP_TIMEOUT,
)
from release_gate_harness_env import (
    _PORT_PICK_ATTEMPTS,
    _PROXY_DIGEST_LINES,
    _WARNING_LINE_RE,
    DEFAULT_SINGLETON_PORT,
    _assert_ipv4_loopback,
    _await_children_settle,
    _backend_entries,
    _backend_logs,
    _backend_pid_from_state,
    _backend_pids_from_state,
    _cap_text,
    _capture_stderr_fd,
    _child_pids,
    _chrome_process_snapshot,
    _isolated_env,
    _log_dirs,
    _os_assigned_port,
    _pick_free_port,
    _pid_in_workspace,
    _pid_running,
    _proxy_warnings,
    _read_capped,
    _recorded_ports,
    _reserved_ports,
    _terminate_process_tree,
    _this_executable,
    gate_work_dir,
    resolve_launcher,
    use_isolated_token,
)
from release_gate_harness_fixture import (
    _FIXTURE_HITS,
    _FIXTURE_HITS_CAP,
    _HIT_RESET,
    _HIT_UNFINISHED,
    FIXTURE_ACCEPT_BACKLOG,
    _bind_origin,
    _FixtureHandler,
    _FixtureServer,
    _serving,
    serve_fixture_app,
    serve_fixture_origin_pair,
)
from release_gate_harness_journey import (
    _call,
    _canonical_journey,
    _cold_start_warmup,
    _cookie_journey,
    _cookie_round_trip,
    _decode_screenshot,
    _eval,
    _foundation_proof,
    _headless_spawn_kwargs,
    _representative_parity,
    _settle_dom,
    _wire_shape,
)
from release_gate_harness_soak import (
    _percentile,
    _reply_shape,
    _soak_cycle,
    _soak_journey,
    _soak_op,
)

if TYPE_CHECKING:
    import os

#: Every name that moved to a sibling module stays importable from here (T-6148).
__all__ = [
    "CALL_TIMEOUT",
    "CHILD_SETTLE_TIMEOUT",
    "CLOSE_TIMEOUT",
    "COOKIE_ROUND_TRIP",
    "DEFAULT_SINGLETON_PORT",
    "FIXTURE_ACCEPT_BACKLOG",
    "FIXTURE_APP_DIR",
    "FULL_JOURNEY",
    "HANDSHAKE_ONLY",
    "INIT_TIMEOUT",
    "LIST_TIMEOUT",
    "LOGIN_COOKIE",
    "LOGIN_COOKIE_VALUE",
    "NAV_TIMEOUT",
    "REGISTRY_TOOL_COUNT",
    "RESULT_SCHEMA_VERSION",
    "SERVER_NAME",
    "SETTLE_TIMEOUT",
    "SOAK_BAD_HOST",
    "SOAK_CYCLES",
    "SOAK_MISSING_SELECTOR_WAIT_MS",
    "SOAK_OP_TIMEOUT",
    "SOAK_PAGES",
    "SOAK_STABILITY",
    "SPAWN_TIMEOUT",
    "TERMINATE_TIMEOUT",
    "WARMUP_ATTEMPTS",
    "WARMUP_BACKOFF_SECONDS",
    "WARMUP_TIMEOUT",
    "_FIXTURE_HITS",
    "_FIXTURE_HITS_CAP",
    "_HIT_RESET",
    "_HIT_UNFINISHED",
    "_PORT_PICK_ATTEMPTS",
    "_PROXY_DIGEST_LINES",
    "_STDERR_CAP_BYTES",
    "_STDERR_CAP_LINES",
    "_WARNING_LINE_RE",
    "_FixtureHandler",
    "_FixtureServer",
    "_assert_ipv4_loopback",
    "_await_children_settle",
    "_backend_entries",
    "_backend_logs",
    "_backend_pid_from_state",
    "_backend_pids_from_state",
    "_bind_origin",
    "_call",
    "_canonical_journey",
    "_cap_text",
    "_capture_stderr_fd",
    "_child_pids",
    "_chrome_process_snapshot",
    "_cold_start_warmup",
    "_cookie_journey",
    "_cookie_round_trip",
    "_decode_screenshot",
    "_eval",
    "_foundation_proof",
    "_headless_spawn_kwargs",
    "_isolated_env",
    "_log_dirs",
    "_os_assigned_port",
    "_percentile",
    "_pick_free_port",
    "_pid_in_workspace",
    "_pid_running",
    "_proxy_warnings",
    "_read_capped",
    "_recorded_ports",
    "_reply_shape",
    "_representative_parity",
    "_reserved_ports",
    "_serving",
    "_settle_dom",
    "_soak_cycle",
    "_soak_journey",
    "_soak_op",
    "_terminate_process_tree",
    "_this_executable",
    "_wire_shape",
    "gate_work_dir",
    "resolve_launcher",
    "serve_fixture_app",
    "serve_fixture_origin_pair",
    "use_isolated_token",
]


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
    ``"soak"``
        the prefix plus warmup, then the long mixed-operation STABILITY run
        (:func:`_soak_journey`) instead of the canonical journey: ONE instance,
        ~50 operations, every one of them individually deadlined, with per-op
        latency recorded. Its claim is "the browser stays up and nothing
        hangs", which no single-pass journey can make — so it is its own
        segment driven by its own collected node
        (``tests/test_soak_stability.py``), on the same one mechanism.
    """
    valid_stages = (FULL_JOURNEY, HANDSHAKE_ONLY, COOKIE_ROUND_TRIP, SOAK_STABILITY)
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
        "navigation_verified": stages
        in (
            FULL_JOURNEY,
            COOKIE_ROUND_TRIP,
            SOAK_STABILITY,
        ),
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
                        if stages in (
                            FULL_JOURNEY,
                            COOKIE_ROUND_TRIP,
                            SOAK_STABILITY,
                        ):
                            await _cold_start_warmup(client, base_url, log_dir, record)
                        if stages == FULL_JOURNEY:
                            await _canonical_journey(client, base_url, record)
                        elif stages == COOKIE_ROUND_TRIP:
                            await _cookie_journey(client, base_url, record)
                        elif stages == SOAK_STABILITY:
                            # Warmup above absorbs the cold Chrome launch and the
                            # cold first page LOAD, so the soak's own per-op
                            # deadline can stay tight without measuring a cost
                            # that is paid once per machine, not per operation.
                            await _soak_journey(client, base_url, record)
                except BaseException as exc:  # noqa: BLE001  PERMANENT(augment with child stderr + boot log, then re-raise)
                    err = exc
            child_stderr = cap["text"]
    finally:
        # The detached backend usually dies with the proxy (keep_alive=False);
        # terminate is the bounded backstop. The release claim is "no backend
        # process remains", not "we were the ones to kill it".
        # EVERY recorded backend, not just the first (F-886): a workspace that
        # ran two source identities has two detached backends, and this block
        # promised to own all of them.
        for backend_pid in _backend_pids_from_state(home_dir):
            if _pid_running(backend_pid):
                _terminate_process_tree(backend_pid, TERMINATE_TIMEOUT)
        # Ownership is decided by cmdline, not by descent: the integration cell
        # spawns Chrome in-process, so a foreign renderer or crashpad helper is
        # also a new descendant of this pytest process and must not be killed
        # here nor counted against this journey's own no-child claim.
        leftover = {
            pid
            for pid in _await_children_settle(
                parent, children_before, CHILD_SETTLE_TIMEOUT
            )
            if _pid_in_workspace(pid, work_dir)
        }
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


# ---------------------------------------------------------------------------
# plan_RELEASE W13 — the isolated workspace and the raw JSON-RPC wire driver.
#
# Both live HERE rather than in a second module because they are the SAME
# mechanism this file already owns, exposed at a lower level:
#
# * :func:`gate_workspace` is the throwaway-HOME / free-port / backend-teardown
#   block from :func:`run_release_gate_journey` above, made reusable. W13 needs
#   several client sessions against ONE isolated backend, which the all-in-one
#   journey cannot express. It reuses ``_isolated_env``/``_pick_free_port``/
#   ``_backend_pid_from_state``/``_await_children_settle`` verbatim.
#
# * :class:`RawStdioWire` drives the same absolute installed launcher over the
#   same stdio pipes, but at the *frame* level. Neither ``fastmcp.Client`` nor
#   the official ``mcp`` SDK can express W13's questions — "was a MALFORMED
#   line answered without killing the session", "did any NON-frame byte reach
#   stdout", "did the response for request id 7 carry request 7's payload",
#   "what happens to an in-flight id when stdin closes" — because both SDKs
#   own the ids, hide the frames, and abort on a parse error. This is a
#   different altitude on the one transport, not a second transport.
# ---------------------------------------------------------------------------

# stdout StreamReader buffer for a wire. Large enough that a big bounded tool
# result is ONE readable line (the default 64 KiB is not), small enough that a
# deliberately paused reader still reaches real OS pipe backpressure.
WIRE_READ_LIMIT = 8 * 1024 * 1024
WIRE_STDERR_CAP_BYTES = 256 * 1024
WIRE_EXIT_TIMEOUT = 30.0
# The wire's own bound on a single response. Never a product deadline: if THIS
# fires the server did not answer, and the node fails by name instead of hanging.
WIRE_RESPONSE_TIMEOUT = 150.0


@contextlib.contextmanager
def gate_workspace(
    work_dir: str | os.PathLike[str], *, singleton_port: int | None = None
):
    """Yield an isolated backend workspace: throwaway HOME, state paths, port.

    The yielded mapping carries ``env`` (the child environment), ``port`` (a
    distinct free ``--singleton-port``), and the four state directories. Any
    number of clients may be started against it; they all share ONE isolated
    singleton backend, so its cold start is paid once.

    On exit the detached backend recorded in the isolated ``server.json`` is
    terminated and every child process this block spawned must be gone —
    the same owned-cleanup guarantee ``run_release_gate_journey`` makes, and
    the reason a W13 node can never leak a backend or a Chrome tree.
    """
    work_dir = Path(work_dir)
    home_dir = work_dir / "home"
    session_root = work_dir / "session-root"
    log_dir = work_dir / "logs"
    clone_dir = work_dir / "clone-output"
    for directory in (home_dir, session_root, log_dir, clone_dir):
        directory.mkdir(parents=True, exist_ok=True)

    space: dict[str, Any] = {
        "work_dir": work_dir,
        "home_dir": home_dir,
        "session_root": session_root,
        "log_dir": log_dir,
        "clone_dir": clone_dir,
        "port": singleton_port or _pick_free_port(),
    }
    space["env"] = _isolated_env(
        home_dir=home_dir,
        session_root=session_root,
        log_dir=log_dir,
        clone_dir=clone_dir,
    )

    parent = psutil.Process()
    children_before = _child_pids(parent)
    try:
        yield space
    finally:
        # EVERY recorded backend, not just the first (F-886): a workspace that
        # ran two source identities has two detached backends, and this block
        # promised to own all of them.
        for backend_pid in _backend_pids_from_state(home_dir):
            if _pid_running(backend_pid):
                _terminate_process_tree(backend_pid, TERMINATE_TIMEOUT)
        # Ownership is decided by cmdline, not by descent: the integration cell
        # spawns Chrome in-process, so a foreign renderer or crashpad helper is
        # also a new descendant of this pytest process and must not be killed
        # here nor counted against this workspace's own leftover claim.
        leftover = {
            pid
            for pid in _await_children_settle(
                parent, children_before, CHILD_SETTLE_TIMEOUT
            )
            if _pid_in_workspace(pid, work_dir)
        }
        for pid in leftover:
            _terminate_process_tree(pid, 5.0)
        space["leftover_children"] = sorted(leftover)


def workspace_backend_logs(space: dict[str, Any]) -> str:
    """The isolated backend's own logs for a failed W13 node (capped)."""
    return _backend_logs(space["log_dir"], space["home_dir"])


def workspace_proxy_warnings(space: dict[str, Any]) -> str:
    """Every proxy's WARNING-or-worse lines for a failed W13 node (capped)."""
    return _proxy_warnings(space["log_dir"], space["home_dir"])


class RawStdioWire:
    """A JSON-RPC-over-stdio driver for the absolute installed console launcher.

    The client owns every request id, every byte written to the child's stdin,
    and every byte read back — so a node can assert *frame* properties instead
    of SDK-mediated ones. Nothing here re-implements the transport: it is the
    same launcher, the same ``--singleton-port`` argument, and the same
    isolated child environment :func:`run_release_gate_journey` uses.

    Bounded by construction. stderr is accumulated behind a cap (and the
    overflow is *counted*, so "stderr stays bounded" is a measurement rather
    than an assumption), every response wait takes an explicit timeout, and
    :meth:`aclose` always terminates the process tree.
    """

    def __init__(
        self,
        *,
        launcher: str | os.PathLike[str],
        env: dict[str, str],
        port: int,
        read_limit: int = WIRE_READ_LIMIT,
        stderr_cap: int = WIRE_STDERR_CAP_BYTES,
    ) -> None:
        self.launcher = str(launcher)
        self.env = dict(env)
        self.port = int(port)
        self.read_limit = read_limit
        self.stderr_cap = stderr_cap
        self.proc: asyncio.subprocess.Process | None = None
        #: every parsed stdout JSON object, in arrival order
        self.frames: list[dict[str, Any]] = []
        #: any stdout line that was NOT a JSON object — must stay empty
        self.non_frame_stdout: list[str] = []
        self.stderr_total_bytes = 0
        self.stderr_truncated = False
        self._stderr_chunks: list[bytes] = []
        self._responses: dict[Any, dict[str, Any]] = {}
        self._events: dict[Any, asyncio.Event] = {}
        self._tasks: list[asyncio.Task] = []
        self._next_id = 0
        self._reader_gate = asyncio.Event()
        self._reader_gate.set()
        self.stdout_eof = False

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> RawStdioWire:
        self.proc = await asyncio.create_subprocess_exec(
            self.launcher,
            "--singleton-port",
            str(self.port),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            limit=self.read_limit,
        )
        self._tasks = [
            asyncio.create_task(self._pump_stdout()),
            asyncio.create_task(self._pump_stderr()),
        ]
        return self

    async def __aenter__(self) -> RawStdioWire:
        return await self.start()

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self, *, timeout: float = WIRE_EXIT_TIMEOUT) -> int | None:
        """Close stdin, wait bounded for exit, then terminate the tree."""
        if self.proc is None:
            return None
        self._reader_gate.set()  # a paused reader must never block teardown
        with contextlib.suppress(Exception):
            await self.close_stdin()
        code = await self.wait_exit(timeout)
        if code is None:
            _terminate_process_tree(self.proc.pid, TERMINATE_TIMEOUT)
            code = await self.wait_exit(10.0)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        return code

    async def close_stdin(self) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.close()
        with contextlib.suppress(Exception):
            await self.proc.stdin.wait_closed()

    async def wait_exit(self, timeout: float) -> int | None:
        """The child's exit code, or ``None`` if it outlived ``timeout``."""
        assert self.proc is not None
        try:
            return await asyncio.wait_for(self.proc.wait(), timeout)
        except TimeoutError:
            return None

    # ── pumps ──────────────────────────────────────────────────────────────
    async def _pump_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            await self._reader_gate.wait()  # the slow-reader / backpressure knob
            try:
                line = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                self.non_frame_stdout.append(f"<unreadable line: {exc}>")
                return
            if not line:
                self.stdout_eof = True
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                self.non_frame_stdout.append(text[:2000])
                continue
            if not isinstance(obj, dict):
                self.non_frame_stdout.append(text[:2000])
                continue
            self.frames.append(obj)
            rid = obj.get("id")
            if rid is not None and ("result" in obj or "error" in obj):
                self._responses[rid] = obj
                event = self._events.get(rid)
                if event is not None:
                    event.set()

    async def _pump_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            chunk = await self.proc.stderr.read(4096)
            if not chunk:
                return
            self.stderr_total_bytes += len(chunk)
            if sum(len(c) for c in self._stderr_chunks) < self.stderr_cap:
                self._stderr_chunks.append(chunk)
            else:
                self.stderr_truncated = True

    @property
    def stderr_text(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", "replace")

    def pause_reader(self) -> None:
        """Stop draining stdout. The child's pipe fills and the OS applies real
        backpressure once the StreamReader's ``read_limit`` buffer is full."""
        self._reader_gate.clear()

    def resume_reader(self) -> None:
        self._reader_gate.set()

    # ── frames ─────────────────────────────────────────────────────────────
    async def send_raw(self, payload: str) -> None:
        """Write one arbitrary line to the child's stdin — malformed included."""
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(payload.encode("utf-8") + b"\n")
        await self.proc.stdin.drain()

    async def request(
        self, method: str, params: Any = None, *, request_id: Any = None
    ) -> Any:
        """Send one JSON-RPC request; return the id it was sent under."""
        if request_id is None:
            self._next_id += 1
            request_id = self._next_id
        self._events[request_id] = asyncio.Event()
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        await self.send_raw(json.dumps(body))
        return request_id

    async def notify(self, method: str, params: Any = None) -> None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        await self.send_raw(json.dumps(body))

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kw):
        return await self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}, **kw
        )

    def answered(self, request_id: Any) -> bool:
        return request_id in self._responses

    def response_of(self, request_id: Any) -> dict[str, Any] | None:
        return self._responses.get(request_id)

    def frames_for(self, request_id: Any) -> list[dict[str, Any]]:
        """Every RESPONSE frame carrying this id — the exactly-one-outcome oracle.

        A frame with a ``method`` is a server→client *request* (this server sends
        ``roots/list``; see F-790) and carries the server's own id space, which
        can collide with ours. Excluding it is what keeps "exactly one response
        for request N" from silently counting an unrelated inbound request.
        """
        return [
            f
            for f in self.frames
            if f.get("id") == request_id
            and "method" not in f
            and ("result" in f or "error" in f)
        ]

    async def response(
        self, request_id: Any, timeout: float = WIRE_RESPONSE_TIMEOUT
    ) -> dict[str, Any]:
        event = self._events[request_id]
        await asyncio.wait_for(event.wait(), timeout)
        return self._responses[request_id]

    async def initialize(
        self,
        timeout: float = INIT_TIMEOUT,
        *,
        capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The MCP opening handshake, written by hand: ``initialize`` then the
        ``notifications/initialized`` notification.

        ``capabilities`` declares what this hand-written client claims to
        support. The default is ``{}`` — the minimum the protocol allows. Pass
        ``{"roots": {"listChanged": False}}`` to advertise MCP ``roots`` without
        ever answering ``roots/list``: the worst-case-but-conforming client that
        F-790 is about.
        """
        from mcp.types import LATEST_PROTOCOL_VERSION

        rid = await self.request(
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": capabilities if capabilities is not None else {},
                "clientInfo": {"name": "release-gate-raw-wire", "version": "1"},
            },
        )
        result = await self.response(rid, timeout)
        await self.notify("notifications/initialized")
        return result
