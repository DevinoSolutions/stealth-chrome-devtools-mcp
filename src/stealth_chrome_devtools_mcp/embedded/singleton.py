"""Singleton server management for multi-session environments.

When multiple Claude Code sessions start simultaneously, this module ensures
only ONE HTTP backend process is spawned; all sessions connect to it as
lightweight stdio proxies. A file lock elects the one starter, and the losers
of that race poll (with backoff) until the backend is healthy.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import psutil

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# F-183: the proxy's cold-start orchestration used to swallow every failure
# silently. configure_logging("proxy") (in run_stdio_proxy) attaches the file
# handler to this name; until then it is a handler-less no-op (fail-open).
_logger = logging.getLogger("stealth.proxy")

STATE_DIR = Path.home() / ".stealth-mcp"
LOCK_FILE = STATE_DIR / "singleton.lock"
PORT_FILE = STATE_DIR / "server.port"
# Records {port, version, pid} for the backend we started, so discovery can
# confirm it is the SAME version before reusing it (issue #14).
SERVER_STATE_FILE = STATE_DIR / "server.json"
DEFAULT_PORT = 19222
# The installed package tree (the .../stealth_chrome_devtools_mcp dir this file
# lives under). _source_fingerprint() hashes every *.py below it so a backend on
# now-stale source is evicted exactly like a version mismatch (F-206/F-120/
# F-504): this editable install's version is frozen, so it cannot see an edit.
SOURCE_ROOT = Path(__file__).resolve().parent.parent
STARTUP_TIMEOUT = 30
SERVER_NAME = "stealth-chrome-devtools-mcp"
# How long the stdio proxy waits for the backend before later requests
# (tools/list, tool calls) fail. `initialize` is answered locally, never here.
BACKEND_READY_TIMEOUT = 120.0
# Human-resolved (plan_M1 appendix, 2026-07-02): keep the ~12s watchdog
# detection window (LIVENESS_PROBE_TIMEOUT=2.0, interval=2.0 x
# failures_before_teardown=3) - preserves the existing watchdog hysteresis
# tests. Not a decision to re-open in a later plan.
LIVENESS_PROBE_TIMEOUT = 2.0
# F-807: a lock-holder's grace for a SAME-identity backend (version AND source
# fingerprint both match) that is busy or mid-boot. Two ways one short probe
# verdict could kill a healthy backend: the winner used to release the lock at
# socket-bind while the reuse gate demands MCP-ready, so a thread acquiring
# inside that gap saw "not reusable" and evicted the newborn; and a backend
# absorbing a 40-session startup herd can miss a 2s probe while serving
# everyone. Sized to outlast a herd peak: waiting costs nothing (proxies sit
# in their own 120s _await_backend_http window), a genuinely wedged backend is
# still evicted well inside that window, and dead ones skip the wait entirely.
REUSE_PATIENCE_SECONDS = 60.0
# Per-attempt probe budget on the PATIENT path only, matching _await_backend_
# http's httpx timeout. The watchdog and the single-shot discovery probe keep
# the human-pinned LIVENESS_PROBE_TIMEOUT unchanged.
REUSE_PROBE_TIMEOUT = 10.0
# issue #56: written once, by the cold-start thread, when the backend it just
# spawned is PROVABLY dead (see _wait_for_server), and read by the proxy's own
# readiness wait - so an import-time crash surfaces in seconds WITH its cause
# instead of a silent 120s timeout. A slow-but-healthy boot never writes it.
_spawn_failure: dict[str, str] = {}


def _ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _exclusive_lock():
    """Try to acquire a file lock. Yields True if acquired, False otherwise."""
    _ensure_state_dir()
    fd = open(LOCK_FILE, "w")
    got = False
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        got = True
    except OSError:
        pass
    try:
        yield got
    finally:
        if got:
            try:
                if sys.platform == "win32":
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        fd.close()


def _server_is_healthy(port: int) -> bool:
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.close()
        return True
    except OSError:
        return False


def _read_server_state() -> dict | None:
    """Return the recorded ``{port, version, pid}`` for the backend we started.

    None if missing/corrupt. Written by :func:`_write_server_state`; a backend
    from an older release (<= 1.2.0) has no such file, so it is version-unknown.
    """
    try:
        state = json.loads(SERVER_STATE_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def _write_server_state(
    port: int, version: str, pid: int, source_fingerprint: str
) -> None:
    """Record the running backend's identity: port, the package version that
    started it, pid, and a fingerprint of the source it runs. Discovery reuses it
    only when BOTH version AND fingerprint still match (and it answers a live
    probe); the pid is what evicts a stale (version- or source-mismatched) one.
    """
    _ensure_state_dir()
    SERVER_STATE_FILE.write_text(
        json.dumps(
            {
                "port": port,
                "version": version,
                "pid": pid,
                "source_fingerprint": source_fingerprint,
            }
        )
    )


def _clear_server_state() -> None:
    """Remove the recorded backend identity (server.json) and the legacy
    write-only port file, best-effort. Used by `stop_backend()` so no stale
    record can make a later `_find_running_server` reuse a stopped backend.
    """
    for path in (SERVER_STATE_FILE, PORT_FILE):
        with suppress(OSError):
            path.unlink(missing_ok=True)


def _probe_backend_status() -> tuple[str, int | None]:
    """Report the recorded backend's actual state for display (CLI status/
    doctor), splitting the states `_find_running_server`'s binary answer
    collapses: absent, socket-dead, and wedged (the F-301 state a bare socket
    check cannot see). Read-only: never evicts, never spawns.

    Returns one of: ("none", None) - no recorded backend; ("down", port) -
    recorded but the socket itself is closed; ("wedged", port) - socket open
    but no real MCP initialize answer; ("responsive", port) - both.
    """
    state = _read_server_state()
    if state is None:
        return "none", None
    port = state.get("port")
    if not isinstance(port, int):
        return "none", None
    if not _server_is_healthy(port):
        return "down", port
    if not _backend_http_ready(port):
        return "wedged", port
    return "responsive", port


def _same_identity_backend_ready(port: int, patience: float | None = None) -> bool:
    """True iff ``server.json`` records OUR identity on ``port`` — package
    version AND source fingerprint both match (issue #14 / F-206: a stale,
    legacy, or edited-source backend is never reused, so upgrades take effect;
    an empty computed fingerprint fails closed) — and that backend answers a
    real ``initialize`` within the patience window (F-301/F-501: a wedged
    backend holds its socket open, so only the app-level probe counts).

    ``patience`` is F-807's anti-fratricide grace, used by the cold-start lock
    path: a backend absorbing a many-session startup herd can miss a single 2s
    probe while perfectly healthy — and a lock-holder that trusts that one miss
    "evicts" (kills) the backend everyone else is using, then double-spawns.
    Identity-gated on purpose: a version- or source-stale record gets NO
    patience and is evicted immediately. ``patience=0.0`` (discovery's
    single-shot probe) probes exactly once and never sleeps. ``None`` means
    ``REUSE_PATIENCE_SECONDS`` (read at call time, so tests can shrink it).
    """
    state = _read_server_state() or {}
    if state.get("port") != port or state.get("version") != _server_version():
        return False
    fp = _source_fingerprint()
    if not fp or state.get("source_fingerprint") != fp:
        return False
    patience = REUSE_PATIENCE_SECONDS if patience is None else patience
    # Busy backends answer slowly, so the patient path probes with the wider
    # per-attempt budget; the single-shot hot path keeps the pinned 2s.
    per_attempt = REUSE_PROBE_TIMEOUT if patience else LIVENESS_PROBE_TIMEOUT
    deadline = time.monotonic() + patience
    while not _backend_http_ready(port, timeout=per_attempt):
        if not _server_is_healthy(port) and not _is_our_backend(state.get("pid")):
            return False  # no socket and no live process: dead, not busy
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


def _find_running_server() -> int | None:
    """Return the port of a *reusable* backend, or None.

    The one reuse gate (`_clear_stale_backend`'s eviction and both cold-start
    callers route through it): same version, same fingerprint, and a live
    `initialize` answer — full contract in :func:`_same_identity_backend_ready`.
    Single-shot on purpose: this is a hot path and must never sleep.
    """
    state = _read_server_state()
    port = state.get("port") if state else None
    if not isinstance(port, int):
        return None
    return port if _same_identity_backend_ready(port, patience=0.0) else None


def _is_our_backend(pid) -> bool:
    """True only if ``pid`` is a process running OUR HTTP backend.

    Identity is the module name **plus** ``--transport`` in the command line, so
    it positively excludes the stdio proxy (same module, no ``--transport``),
    unrelated processes, and recycled pids — eviction relies on that.
    """
    if not isinstance(pid, int):
        return False
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        return False
    joined = " ".join(cmdline)
    return "stealth_chrome_devtools_mcp" in joined and "--transport" in joined


def _backend_pid_on_port(port: int) -> int | None:
    """Return the pid of OUR backend listening on ``port``, or None. A foreign
    process holding the port is deliberately ignored (never returned to kill).
    """
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.Error, OSError):
        return None
    for conn in conns:
        laddr = getattr(conn, "laddr", None)
        if (
            laddr
            and getattr(laddr, "port", None) == port
            and conn.status == psutil.CONN_LISTEN
            and conn.pid
            and _is_our_backend(conn.pid)
        ):
            return conn.pid
    return None


def _terminate_backend(port: int) -> bool:
    """Terminate OUR backend associated with ``port``, if one is identifiable.

    Resolves the pid by open port first, then falls back to ``server.json``'s
    recorded pid (guarded by ``_is_our_backend`` either way) — a pid not
    positively ours (e.g. recycled onto an unrelated process) is never touched.
    Best-effort, bounded, never raises. Returns whether one was terminated.
    """
    pid = _backend_pid_on_port(port)
    if pid is None:
        state = _read_server_state()
        recorded = state.get("pid") if state else None
        if _is_our_backend(recorded):
            pid = recorded
    if pid is None:
        return False

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            proc.kill()
    except (psutil.Error, OSError):
        pass

    # Give the OS a moment to release the port so a fresh backend can bind.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _server_is_healthy(port):
            return True
        time.sleep(0.1)
    return True


def _clear_stale_backend(port: int) -> None:
    """Terminate a stale/legacy backend of ours squatting ``port`` so a
    correctly-versioned one can bind. No-op when the port already holds a
    reusable same-version backend.
    """
    if _find_running_server() == port:
        return  # a reusable same-version backend is already there
    _terminate_backend(port)


def _server_process_cmd(port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "stealth_chrome_devtools_mcp",
        "--transport",
        "http",
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
    ]


def _start_server_process(port: int):
    cmd = _server_process_cmd(port)

    # F-303/F-503: stdout/stderr used to be DEVNULL, hiding every backend
    # crash. An embedded/server.py import-time crash dies before in-process
    # logging (configure_logging) exists, so only a raw stream redirect at Popen
    # captures it. stdin stays DEVNULL - the backend never reads it.
    from stealth_chrome_devtools_mcp.embedded.logging_setup import resolve_log_dir

    boot_log = None
    try:
        log_dir = resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        boot_log = open(log_dir / "backend-boot.log", "a", encoding="utf-8")
    except OSError:
        # Fail-open (plan_M3 §7: "M3's file setup is fail-open"): a log dir
        # that can't be created/opened must never block the backend from
        # spawning - fall back to the pre-M3 DEVNULL redirect instead.
        _logger.warning(
            "backend-boot.log unavailable; falling back to DEVNULL", exc_info=True
        )
        boot_log = None

    stdout_target = boot_log if boot_log is not None else subprocess.DEVNULL
    # A spawned backend must always own its lifecycle (reap its own orphaned
    # browsers on init) even when the CLI-invoking parent set this to skip
    # its own recovery-on-import (cli.py's os.environ.setdefault).
    child_env = dict(os.environ)
    child_env.pop("STEALTH_MCP_NO_AUTO_RECOVERY", None)
    kwargs: dict = {
        "stdout": stdout_target,
        "stderr": stdout_target,
        "stdin": subprocess.DEVNULL,
        "env": child_env,
    }

    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        if boot_log is not None:
            boot_log.close()

    _ensure_state_dir()
    PORT_FILE.write_text(str(port))
    _write_server_state(port, _server_version(), proc.pid, _source_fingerprint())
    return proc  # so the caller's wait can see this child EXIT (issue #56)


def _backend_failure_reason() -> str:
    """One-line cause for a backend that never came up: the bounded tail of what
    it printed, keyed by the pid we recorded. Never raises."""
    from stealth_chrome_devtools_mcp.embedded.logging_setup import backend_log_tail

    return backend_log_tail((_read_server_state() or {}).get("pid"))


def _wait_for_server(port: int, timeout: int = STARTUP_TIMEOUT, proc=None) -> bool:
    """Wait until ``port`` accepts a socket, ``timeout`` elapses, or — given the
    ``proc`` from :func:`_start_server_process` — that child is PROVABLY dead,
    ending the wait rather than burning the budget on a process that can never
    answer (issue #56). ``proc=None`` keeps the old contract.

    Provably dead = three readings agreeing: exited NONZERO, nothing listening
    (the loop's own check, one line above), MCP probe silent. A false "dead" is
    the F-807 failure class, so a clean exit or any sign of life keeps the full
    patience. The exit code is not trusted alone because of the uv trampoline:
    Windows ``.venv/Scripts/python.exe`` is a shim whose identically-named child
    does the work, so ``server.json``'s pid is the shim's. Measured 2026-08-01
    on this venv, that shim blocks for the child's whole life and forwards its
    code (0 clean / 1 crash, in 0.07s), so a non-None ``poll()`` does mean the
    interpreter is gone; socket+probe keep a slow boot alive under any launcher.
    """
    deadline = time.monotonic() + timeout
    interval = 0.25
    while time.monotonic() < deadline:
        if _server_is_healthy(port):
            return True
        if proc is not None and proc.poll() and not _backend_http_ready(port):
            _spawn_failure["reason"] = _backend_failure_reason()
            _logger.error("backend exited during startup: %s", _spawn_failure["reason"])
            return False
        time.sleep(interval)
        interval = min(interval * 1.5, 2.0)
    return False


def _backend_http_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp/"


def _backend_http_ready(port: int, *, timeout: float = LIVENESS_PROBE_TIMEOUT) -> bool:
    """Single-shot, synchronous app-level liveness probe: True iff the backend
    on ``port`` answers a real ``initialize`` with HTTP 200.

    The promoted, reusable form of what `_await_backend_http` proves at startup
    (initialize->200), as ONE attempt instead of a poll loop, so sync callers
    (discovery, CLI) can call it directly and the watchdog can drive it
    off-thread. Never raises: any failure (refused, timeout, malformed) reads as
    False, matching `_server_is_healthy`'s fail-closed contract.

    NOTE: this intentionally duplicates ~10 lines of `_await_backend_http`'s
    `initialize` request shape rather than sharing a helper (plan_M1 SS2.2
    rejected-alternative #4, cross-review ruling: M1/M3 singleton regions stay
    disjoint - `_await_backend_http` is M3's). `grep '"initialize"'` finds
    both twins; consolidating them is a future finding, not this plan's scope.
    """
    import httpx
    from mcp.types import DEFAULT_NEGOTIATED_VERSION

    probe = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": DEFAULT_NEGOTIATED_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "liveness-probe", "version": "0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    url = _backend_http_url(port)
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.post(url, json=probe, headers=headers)
            if resp.status_code != 200:
                return False
            session_id = resp.headers.get("mcp-session-id")
            if session_id:
                try:
                    client.delete(
                        url, headers={**headers, "mcp-session-id": session_id}
                    )
                except Exception:
                    # Best-effort cleanup of the throwaway liveness session;
                    # the probe itself already succeeded.
                    _logger.debug(
                        "liveness-probe session cleanup failed", exc_info=True
                    )
            return True
    except Exception as e:
        # Fail-closed: refused (down), hung/wedged (timeout), or any other
        # transport error all read as "not ready" - _server_is_healthy's
        # contract. DEBUG, not WARNING (M10a convention, cf.
        # _await_backend_http's identical catch): this fires routinely during a
        # normal cold start and whenever a backend is briefly busy - the
        # watchdog decides when a RUN of failures is WARNING-worthy, not this.
        _logger.debug("liveness probe attempt failed", exc_info=e)
        return False


def _server_version() -> str:
    try:
        from importlib.metadata import version

        return version(SERVER_NAME)
    except Exception:
        _logger.debug("could not resolve installed package version", exc_info=True)
        return "0.0.0"


def _source_fingerprint() -> str:
    """SHA-256 over the package's ``*.py`` source, so a backend built from
    now-stale source is not reused. COMPLETE (every importable module), STABLE
    (identical bytes -> identical digest, immune to mtime/OneDrive/git quirks),
    CHEAP (~1 MB read+hash per cold-start discovery). Best-effort: an OS read
    error yields ``""``, costing one respawn rather than crashing discovery -
    and the reuse gate treats ``""`` as a miss, never a false reuse.
    """
    import hashlib

    h = hashlib.sha256()
    try:
        for p in sorted(SOURCE_ROOT.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            h.update(p.relative_to(SOURCE_ROOT).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    except OSError:
        return ""
    return h.hexdigest()


def _start_backend_holding_lock(port: int) -> None:
    """Start the singleton backend exactly once, holding the lock until it is
    healthy so no other session double-starts it. Runs in a daemon thread so it
    never blocks the stdio handshake; the lock is held for the whole cold start,
    and a session that loses the race just proxies to the winner's backend.
    """
    try:
        with _exclusive_lock() as got_lock:
            if not got_lock:
                return  # another session owns startup; just proxy to it
            if _find_running_server() is not None:
                return  # already up (same version)
            if _same_identity_backend_ready(port):
                return  # ours, merely busy or mid-boot — never evict it (F-807)
            # M2-3: surface WHY a fresh backend is about to spawn when the
            # cause is a source change (version matches, fingerprint differs) -
            # otherwise silent. Logged once per spawn HERE, not in
            # _find_running_server (up to 3x per locked cold start); the state
            # re-read is a cheap diagnostic probe, deliberately NOT a second
            # reuse gate. Source-only: a version-change eviction (issue #14)
            # must not emit this line.
            state = _read_server_state()
            if (
                state is not None
                and state.get("version") == _server_version()
                and state.get("source_fingerprint") != _source_fingerprint()
            ):
                _logger.info("backend stale (source changed), evicting")
            # A stale/legacy backend may still hold the port; evict it under
            # the lock so our correctly versioned one can bind — otherwise the
            # proxy falls back to it and the upgrade silently never happens.
            _clear_stale_backend(port)
            # proc= so a backend that dies at import time ends the wait now.
            _wait_for_server(port, proc=_start_server_process(port))
            # Keep the lock past socket-bind, until the backend answers a real
            # initialize: release only when the reuse gate itself would pass,
            # so no thread can acquire inside the bind→ready gap (F-807).
            _same_identity_backend_ready(port)
    except Exception:
        # Best-effort; the proxy still answers initialize and retries. Before M3
        # this was silent (F-183's primary handler). Now it's on disk, control
        # flow unchanged (M10a's rule: add a log line, leave the sentinel).
        _logger.exception("backend cold start failed")


def stop_backend() -> tuple[str, int | None]:
    """Stop the shared backend (CLI `stop` verb): an operator-initiated action
    that terminates every live browser session on it — that is the verb's
    purpose, not a side effect to guard against.

    Consumes M1's `_probe_backend_status()` for the state read (binding ruling:
    no new liveness check anywhere) — only a responsive/wedged backend is
    targeted for termination; a stale `down` record is cleared with nothing left
    to kill; `none` is reported as-is. Lock contention (a concurrent cold
    start/stop/restart holding it) reports "busy" so the operator can retry.

    Returns ``(result, pid)``: ``result`` is one of "stopped" | "already
    stopped" | "not running" | "busy". ``pid`` is the terminated pid when
    ``result == "stopped"``, else None.
    """
    status, port = _probe_backend_status()
    if status == "none":
        return ("not running", None)

    with _exclusive_lock() as got:
        if not got:
            return ("busy", None)
        state = _read_server_state()
        recorded_pid = state.get("pid") if state else None
        terminated = _terminate_backend(port) if port is not None else False
        _clear_server_state()
        if terminated:
            return ("stopped", recorded_pid)
        return ("already stopped", None)


def restart_backend() -> tuple[str, int | None]:
    """Restart the shared backend (CLI `restart` verb): the manual escape hatch
    for a wedged (M1) or stale same-version (M2) backend — terminate whatever is
    on the target port, then run the exact cold-start spawn sequence under the
    same lock, with the SAME primitives (plan_M8 SS2.1-B: no second spawn path,
    no new kill logic). Unconditional by design, so a "down"/"none" backend also
    ends up running, not merely evicted. The terminate target is the port
    recorded in `server.json`, else `DEFAULT_PORT`; the spawn port then routes
    through `_select_backend_port()` (F-509 Amendment A1) so a squatter on the
    dead backend's port forces a fresh `_free_port()` pick instead of a repeat
    120s outage — and that fallback stays recorded across restarts (SSA1.5);
    `stop` clears `server.json`, the reset path to `DEFAULT_PORT`. Lock
    contention reports "busy" so the operator retries instead of racing. The
    post-restart state comes from `_probe_backend_status()` (binding ruling: ONE
    liveness vocabulary) — a restart that comes back wedged or down must be
    visible, not assumed "responsive".

    Returns ``(status, pid)``: `_probe_backend_status`'s status or "busy";
    ``pid`` is the freshly recorded pid once the lock is acquired, else None.
    """
    state = _read_server_state()
    recorded_port = state.get("port") if state else None
    port = recorded_port if isinstance(recorded_port, int) else DEFAULT_PORT

    with _exclusive_lock() as got:
        if not got:
            return ("busy", None)
        _terminate_backend(port)
        port = _select_backend_port(port)
        _wait_for_server(port, proc=_start_server_process(port))

    status, _ = _probe_backend_status()
    new_state = _read_server_state()
    new_pid = new_state.get("pid") if new_state else None
    return (status, new_pid)


def _port_is_foreign_held(port: int) -> bool:
    """True iff ``port``'s socket is open but NOT held by our backend.

    The canonical "foreign occupant" predicate (F-509, plan_M8 Amendment A1):
    ONE definition, consumed by the cold-start port fallback below and by
    doctor's diagnostic (cli.py's ``_doctor_port_occupant_line``).
    """
    return _server_is_healthy(port) and _backend_pid_on_port(port) is None


def _select_backend_port(preferred: int = DEFAULT_PORT) -> int:
    """Port to spawn the backend on (F-509 auto-fallback, plan_M8 Amendment A1).
    Prefers ``server.json``'s recorded port (so eviction/restart land where a
    prior backend ran), else ``preferred``, keeping that target when it is free
    or held by OUR OWN backend (eviction rebinds it there). Only a FOREIGN
    occupant forces an OS-assigned fallback via ``proxy_forwarder._free_port()``
    (the one existing port-picker), so a collision is recoverable instead of a
    silent 120s outage.
    """
    # lazy; no module-top cycle
    from stealth_chrome_devtools_mcp.embedded.proxy_forwarder import _free_port

    state = _read_server_state()
    recorded = state.get("port") if state else None
    target = recorded if isinstance(recorded, int) else preferred
    return _free_port() if _port_is_foreign_held(target) else target


def ensure_server_running(port: int = DEFAULT_PORT) -> int | None:
    """Ensure the singleton backend is up or coming up, WITHOUT blocking, and
    return the port to proxy to immediately: the proxy answers ``initialize``
    locally and only later requests wait for the backend. That decoupling is
    what keeps Claude Code's 30s connection timeout from firing under load.
    """
    existing = _find_running_server()
    if existing is not None:
        return existing

    # F-509 (Amendment A1): choose the port SYNCHRONOUSLY here, before the
    # daemon thread starts, so the one chosen value reaches both the spawn
    # arg below AND the return value (the proxy's connect target) in
    # lock-step — no polling server.json for a value the thread hasn't
    # written yet (SSA1.3 rejected alternative #2).
    port = _select_backend_port(port)

    threading.Thread(
        target=_start_backend_holding_lock, args=(port,), daemon=True
    ).start()
    return port


async def _await_backend_http(
    url: str, deadline_seconds: float = BACKEND_READY_TIMEOUT
) -> bool:
    """Poll the backend with a real ``initialize`` until it returns HTTP 200.

    Stronger than a socket probe *and* than "any HTTP response": a freshly bound
    uvicorn socket can answer 4xx while FastMCP's session manager is still
    starting, and forwarding to it then fails 400 (the old ``-32000`` race).
    Only a 200 to an ``initialize`` proves the MCP layer is genuinely ready.
    """
    import anyio
    import httpx
    from mcp.types import DEFAULT_NEGOTIATED_VERSION

    probe = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": DEFAULT_NEGOTIATED_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "readiness-probe", "version": "0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    deadline = time.monotonic() + deadline_seconds
    interval = 0.1
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        while time.monotonic() < deadline:
            if _spawn_failure:
                return False  # our cold start proved it dead; don't wait it out
            try:
                resp = await client.post(url, json=probe, headers=headers)
                if resp.status_code == 200:
                    # Terminate the throwaway readiness session so it does not
                    # linger on the backend (one per proxy start otherwise).
                    session_id = resp.headers.get("mcp-session-id")
                    if session_id:
                        try:
                            await client.delete(
                                url, headers={**headers, "mcp-session-id": session_id}
                            )
                        except Exception:
                            # Best-effort cleanup of the throwaway readiness
                            # session; the probe itself already succeeded.
                            _logger.debug(
                                "readiness-probe session cleanup failed",
                                exc_info=True,
                            )
                    return True
            except Exception:
                # Expected during cold start (connection refused before the
                # backend's socket is bound) - DEBUG, not a real problem
                # unless it persists until the deadline (see run_backend).
                _logger.debug("backend readiness probe attempt failed", exc_info=True)
            await anyio.sleep(interval)
            interval = min(interval * 1.5, 1.0)
    return False


async def _watch_backend_liveness(
    port: int,
    *,
    interval: float = 2.0,
    failures_before_teardown: int = 3,
    is_healthy=None,
    sleep=None,
) -> None:
    """Return once the backend on ``port`` has been unreachable for
    ``failures_before_teardown`` consecutive checks.

    Armed only after the backend was confirmed up; the caller tears the proxy
    down when this returns, turning a mid-session death into a clean client
    reconnect (which respawns a backend) instead of an unbounded hang. One
    healthy check resets the run. ``is_healthy``/``sleep`` inject for testing.

    F-501: the default check used to be a bare socket connect
    (``_server_is_healthy``), which a wedged backend (dispatch loop dead, socket
    still open) always passes - so the sole auto-recovery watchdog never armed
    against the exact failure it exists for. It now runs the app-level probe
    (``_backend_http_ready``) off-thread via ``anyio.to_thread.run_sync``
    (plan_M1 SS2.2 rejected alternative #3: a blocking httpx call run inline
    would freeze the stdio pump for up to ``LIVENESS_PROBE_TIMEOUT`` every
    ``interval``). The loop is await-aware (``inspect.isawaitable``) so this
    async default and every injected SYNC ``is_healthy`` both drive it.
    """
    import anyio

    def _default_check():
        return anyio.to_thread.run_sync(_backend_http_ready, port)

    check = is_healthy if is_healthy is not None else _default_check
    nap = sleep if sleep is not None else anyio.sleep
    consecutive = 0
    while True:
        await nap(interval)
        res = check()
        if inspect.isawaitable(res):
            res = await res
        if res:
            consecutive = 0
            continue
        consecutive += 1
        _logger.warning(
            "liveness probe failed for backend on port %d (%d/%d)",
            port,
            consecutive,
            failures_before_teardown,
        )
        if consecutive >= failures_before_teardown:
            return


async def _proxy_streams(client_read, client_write, port: int) -> None:
    """Answer ``initialize`` locally and instantly, then transparently proxy
    every other message to/from the singleton HTTP backend once it is ready. The
    transport plumbing (session-id capture, forwarding) is the same proven
    stdio↔streamable-HTTP pipe; the additions are the local ``initialize``
    answer and swallowing the backend's duplicate so the client sees only one.
    """
    import anyio
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.message import SessionMessage
    from mcp.types import (
        DEFAULT_NEGOTIATED_VERSION,
        ErrorData,
        JSONRPCError,
        JSONRPCMessage,
        JSONRPCRequest,
        JSONRPCResponse,
    )

    url = _backend_http_url(port)
    to_backend_tx, to_backend_rx = anyio.create_memory_object_stream(1024)
    init_request_id = {"value": None}
    init_swallowed = {"done": False}
    backend_initialized = anyio.Event()
    # Set once the backend has answered a real initialize (genuinely up); the
    # liveness monitor stays disarmed until then, so it never tears the proxy
    # down during a normal cold start.
    backend_ready = anyio.Event()

    async def pump_client():
        try:
            async for msg in client_read:
                if isinstance(msg, Exception):
                    continue
                inner = msg.message.root
                if isinstance(inner, JSONRPCRequest) and inner.method == "initialize":
                    params = inner.params or {}
                    proto = params.get("protocolVersion") or DEFAULT_NEGOTIATED_VERSION
                    result = {
                        "protocolVersion": proto,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": _server_version(),
                        },
                    }
                    response = JSONRPCResponse(
                        jsonrpc="2.0", id=inner.id, result=result
                    )
                    await client_write.send(
                        SessionMessage(message=JSONRPCMessage(response))
                    )
                    init_request_id["value"] = inner.id
                # Forward everything (including initialize) so the backend
                # session gets the client's real params; buffered until connect.
                await to_backend_tx.send(msg)
        finally:
            await to_backend_tx.aclose()

    async def fail_pending(reason: str):
        """Tell the CLIENT why, on its only remaining channel: a bare transport
        close is what Claude Code renders as the opaque "MCP error -32000:
        Connection closed" (issue #56), so answer every buffered request with the
        cause first. `initialize` was answered locally and is skipped; nothing
        else about the transport contract changes."""
        error = ErrorData(code=-32000, message=f"backend failed to start: {reason}")
        answered = init_request_id["value"]
        with suppress(anyio.WouldBlock, anyio.EndOfStream):
            while True:
                inner = to_backend_rx.receive_nowait().message.root
                if isinstance(inner, JSONRPCRequest) and inner.id != answered:
                    err = JSONRPCError(jsonrpc="2.0", id=inner.id, error=error)
                    await client_write.send(SessionMessage(message=JSONRPCMessage(err)))

    async def run_backend():
        if not await _await_backend_http(url):
            # Before M3 this returned silently (F-183); before issue #56 it
            # named only the timeout, leaving the real cause (an import-time
            # ValidationError, say) in backend-boot.log. Same sentinel, new cause.
            reason = _spawn_failure.get("reason") or _backend_failure_reason()
            _logger.error(
                "backend did not become ready within %.0fs: %s",
                BACKEND_READY_TIMEOUT,
                reason,
            )
            await fail_pending(reason)
            return

        backend_ready.set()  # arm the liveness monitor now that it is genuinely up
        async with streamablehttp_client(url) as (backend_read, backend_write, _):

            async def to_backend():
                # Forward the initialize first, then hold every later message
                # until the backend's response establishes the streamable-HTTP
                # session id: streamablehttp_client stamps each concurrent
                # request with the *current* id, so a tools/list sent before that
                # id exists yields a 400. A real client gets this sequencing by
                # waiting on the initialize response; we answered that locally,
                # so we reproduce the wait here.
                first = await to_backend_rx.receive()
                await backend_write.send(first)
                inner = first.message.root
                if isinstance(inner, JSONRPCRequest) and inner.method == "initialize":
                    await backend_initialized.wait()
                async for msg in to_backend_rx:
                    await backend_write.send(msg)

            async def from_backend():
                try:
                    async for msg in backend_read:
                        if isinstance(msg, Exception):
                            continue
                        inner = msg.message.root
                        if (
                            not init_swallowed["done"]
                            and init_request_id["value"] is not None
                            and isinstance(inner, JSONRPCResponse)
                            and inner.id == init_request_id["value"]
                        ):
                            init_swallowed["done"] = True
                            backend_initialized.set()
                            continue  # client already got a local initialize result
                        await client_write.send(msg)
                finally:
                    # Never leave to_backend blocked if the backend died before
                    # its initialize response arrived.
                    backend_initialized.set()

            async with anyio.create_task_group() as tg:
                tg.start_soon(to_backend)
                tg.start_soon(from_backend)

    async def run_backend_guarded():
        # A backend that dies mid-session surfaces as a read/connection error
        # out of run_backend. Swallow it and tear down, so the client sees a
        # clean disconnect and reconnects to a freshly spawned backend instead
        # of blocking forever on a request the dead one can never answer.
        try:
            await run_backend()
        except Exception:
            _logger.warning("backend connection lost", exc_info=True)
        finally:
            tg.cancel_scope.cancel()

    async def monitor_backend():
        # Armed only once the backend is confirmed up. Covers a backend that
        # vanishes while run_backend is parked forwarding (no error raised, so
        # run_backend_guarded alone would never fire).
        await backend_ready.wait()
        await _watch_backend_liveness(port)
        _logger.warning("backend became unreachable; tearing down for reconnect")
        tg.cancel_scope.cancel()

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_backend_guarded)
        tg.start_soon(monitor_backend)
        # Drive the client pump in the main task: when Claude Code disconnects,
        # stdin hits EOF, pump_client returns and we cancel everything. Without
        # that, from_backend stays parked on the still-open backend stream and
        # the proxy never exits — one stranded process per disconnect.
        await pump_client()
        tg.cancel_scope.cancel()


async def _bridge(port: int):
    """Bind real stdio and run the fast-handshake proxy."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (client_read, client_write):
        try:
            await _proxy_streams(client_read, client_write, port)
        finally:
            # The client disconnected. mcp's stdio_server holds its __aexit__
            # open until its stdout-writer task ends, which needs the write
            # stream closed; without this the process hangs after every
            # disconnect — one stranded entrypoint each time.
            await client_write.aclose()
            await client_read.aclose()


def run_stdio_proxy(port: int):
    """Run the stdio-to-HTTP proxy (blocking)."""
    import anyio

    # deferred: breaks the cycle
    from stealth_chrome_devtools_mcp.embedded.logging_setup import configure_logging

    configure_logging("proxy")
    anyio.run(_bridge, port)
