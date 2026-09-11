"""Singleton server management for multi-session environments.

When multiple Claude Code sessions start simultaneously, this module ensures
only ONE HTTP server process is spawned. All sessions connect to it as
lightweight stdio proxies.

Race condition handling:
  - File lock ensures exactly one process starts the server
  - Losers of the lock race poll until the server is healthy
  - Exponential backoff prevents thundering herd on health checks
  - Fallback to standalone stdio mode if server fails to start
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psutil

from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    backend_watchdog,
    display_context,
    scheduling_lag,
)
from stealth_chrome_devtools_mcp.embedded.backend_registry import (
    PORT_FILE,
    SERVER_STATE_FILE,
    STATE_DIR,
)
from stealth_chrome_devtools_mcp.observability import capture_lifecycle

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# F-183: the proxy's cold-start orchestration used to swallow every failure
# silently. configure_logging("proxy") (in run_stdio_proxy) attaches the file
# handler to this same logger name; until then this is a normal Logger with
# no handlers - a safe no-op, same fail-open contract as logging_setup itself.
_logger = logging.getLogger("stealth.proxy")

LOCK_FILE = STATE_DIR / "singleton.lock"
DEFAULT_PORT = 19222
# The installed package tree (the .../stealth_chrome_devtools_mcp dir this file
# lives under). _source_fingerprint() hashes every *.py below it so a backend
# running now-stale source is evicted and respawned exactly like a version
# mismatch (F-206/F-120/F-504): on this editable install the package version is
# frozen at 1.2.0, so the version key alone can never see an in-place source edit.
SOURCE_ROOT = Path(__file__).resolve().parent.parent
_FINGERPRINT_ATTEMPTS = 3  # F-829: outlast a transient read failure
_FINGERPRINT_RETRY_SECONDS = 0.05
STARTUP_TIMEOUT = 30
SERVER_NAME = "stealth-chrome-devtools-mcp"
# How long the stdio proxy will wait for the backend before later requests
# (tools/list, tool calls) start failing. The `initialize` handshake itself is
# answered locally and never waits on this.
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
# Per-attempt probe budget on the PATIENT path only, matching the httpx
# timeout _await_backend_http already uses. The watchdog and the single-shot
# discovery probe keep the human-pinned LIVENESS_PROBE_TIMEOUT unchanged.
REUSE_PROBE_TIMEOUT = 10.0


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


# The record itself lives in backend_registry; these three name the paths this
# module owns and pass them in. Keeping the names here keeps the one call and
# patch surface the rest of the tree (cli.py, the tests) already targets.
def _read_server_state() -> dict | None:
    """The RAW record — v1 flat or v2 per-context (F-808) — not a backend.
    Kept raw because it is a patch surface (test_cli / test_cli_status_wedged
    stub it with v1-flat dicts), so every consumer reads backends out of it
    through backend_registry's normalizers (`first_backend` /
    `backend_on_port`), which accept both shapes, rather than indexing it.
    """
    return backend_registry.read_record(SERVER_STATE_FILE)


def _write_server_state(
    port: int, version: str, pid: int, source_fingerprint: str | None
) -> None:
    backend_registry.record_backend(
        SERVER_STATE_FILE,
        port=port,
        version=version,
        pid=pid,
        source_fingerprint=source_fingerprint,
        display_context=display_context.display_context(),
    )


def _clear_server_state() -> None:
    backend_registry.clear_record(SERVER_STATE_FILE, PORT_FILE)


def _probe_backend_status() -> tuple[str, int | None]:
    """Report the recorded backend's actual state for display (CLI status/
    doctor), distinguishing what `_find_running_server`'s binary answer
    collapses: not running, socket-dead, and wedged (the F-301 state a bare
    socket check cannot see). Read-only: never evicts, never spawns. Doctor
    runs this same ladder per-entry in `cli._probe_recorded_backend`.

    Returns ("none", None) no recorded backend | ("down", port) socket closed |
    ("wedged", port) socket open, no real MCP initialize answer |
    ("responsive", port) socket open AND initialize answers 200.
    """
    entry = backend_registry.first_backend(_read_server_state())
    if entry is None:
        return "none", None
    port = entry.get("port")
    if not isinstance(port, int):
        return "none", None
    if not _server_is_healthy(port):
        return "down", port
    if not _backend_http_ready(port):
        return "wedged", port
    return "responsive", port


def _same_identity_backend_ready(port: int, patience: float | None = None) -> bool:
    """True iff ``server.json`` records OUR identity on ``port`` — the version
    matches and the fingerprint does not CONTRADICT it (issue #14/F-206 never
    reuse a stale, legacy or edited-source backend; F-829: an unreadable digest
    is unknown, not a contradiction — see ``fingerprint_mismatch``) — and it
    answers a real ``initialize`` in the patience window (F-301/F-501: a wedged
    backend holds its socket open, so only the app-level probe counts).

    ``patience`` is F-807's anti-fratricide grace for the cold-start lock path:
    a healthy backend absorbing a many-session startup herd can miss a single
    2s probe, and a lock-holder trusting that one miss "evicts" (kills) the
    backend everyone is using, then double-spawns. ``_watch_backend_liveness``
    applies the same discrimination mid-session (F-820). Identity-gated: a
    version- or source-stale record gets NO patience and is evicted at once.
    ``patience=0.0`` (discovery) probes once and never sleeps; ``None`` means
    ``REUSE_PATIENCE_SECONDS``, read at call time so tests can shrink it.

    F-856: the window is spent in FAIRLY SCHEDULED seconds, not wall seconds.
    A probe timeout is evidence about the backend only while this process is
    itself being scheduled, and on a machine at 100% CPU it is not — the
    2026-09-02 incident condemned a backend that had answered its six previous
    confirmations. ``scheduling_lag.FairWindow`` measures that lateness from
    the loop's own naps and discounts the budget by it, bounded at
    ``MAX_STRETCH``. On an idle machine the measurement is 1.0 and this is
    exactly the wall-clock deadline it replaced.
    """
    # The entry recorded ON THIS PORT, not merely the first: under F-808's
    # per-context record another desktop's backend says nothing about `port`.
    entry = backend_registry.backend_on_port(_read_server_state(), port) or {}
    if entry.get("version") != _server_version():
        return False
    if backend_registry.fingerprint_mismatch(entry, _source_fingerprint()):
        return False
    patience = REUSE_PATIENCE_SECONDS if patience is None else patience
    # Busy backends answer slowly, so the patient path probes with the wider
    # per-attempt budget; the single-shot hot path keeps the pinned 2s.
    per_attempt = REUSE_PROBE_TIMEOUT if patience else LIVENESS_PROBE_TIMEOUT
    window = scheduling_lag.FairWindow(patience)
    while not _backend_http_ready(port, timeout=per_attempt):
        if not _server_is_healthy(port) and not _is_our_backend(entry.get("pid")):
            return False  # no socket and no live process: dead, not busy
        if window.expired():
            return False
        window.nap(0.25)
    return True


def _find_running_server() -> int | None:
    """Return the port of a *reusable* backend, or None.

    The one reuse gate (both cold-start callers route through it): same
    version, same source fingerprint, a live `initialize` — full contract in
    :func:`_same_identity_backend_ready`. Candidates come in adoption order
    (F-808; the policy and its asymmetry live in
    :func:`backend_registry.adoption_candidates`), but IDENTITY, never display
    context, is the gate. Single-shot per candidate on the proxy's hot path,
    behind a socket pre-filter: a dead record costs ms, not a 2s timeout."""
    own = display_context.display_context()
    for entry in backend_registry.adoption_candidates(SERVER_STATE_FILE, own):
        port = entry.get("port")
        if not isinstance(port, int) or not _server_is_healthy(port):
            continue
        if _same_identity_backend_ready(port, patience=0.0):
            return port
    return None


def _is_our_backend(pid) -> bool:
    """True only if ``pid`` is a process running OUR HTTP backend.

    Identity is the module name **plus** ``--transport`` in the command line, so
    this positively excludes the stdio proxy (same module, no ``--transport``),
    unrelated processes, and recycled pids. Eviction relies on this to never
    terminate the wrong process.
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
    """Return the pid of OUR backend listening on ``port``, or None.

    A foreign process holding the port is deliberately ignored (never returned
    for termination).
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

    Resolves the pid by open port first, then falls back to the recorded pid
    in ``server.json`` (guarded by ``_is_our_backend`` either way) — a pid
    that is not positively identified as our backend (e.g. a recycled pid now
    running an unrelated process) is never touched. Best-effort and bounded —
    never raises. Returns whether a backend of ours was found and terminated.
    """
    pid = _backend_pid_on_port(port)
    if pid is None:
        entry = backend_registry.backend_on_port(_read_server_state(), port)
        recorded = entry.get("pid") if entry else None
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
    correctly-versioned backend can bind on it.

    No-op when THIS port already holds a reusable same-identity backend —
    asked of the port, not of `_find_running_server`, which under F-808's
    adoption order may name another display context's backend, on another port.
    """
    if _same_identity_backend_ready(port, patience=0.0):
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
    # crash - an import-time crash dies before configure_logging installs
    # itself, so only a raw Popen redirect captures it. stdin stays DEVNULL.
    from stealth_chrome_devtools_mcp.embedded import logging_setup

    boot_log = None
    try:
        log_dir = logging_setup.resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        # F-830: the launcher is the ONLY place this file can be rotated - once
        # Popen inherits the fd, the child pins it for life (see roll_boot_log).
        boot_log = logging_setup.roll_boot_log(log_dir).open("a", encoding="utf-8")
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


def _wait_for_server(port: int, timeout: int = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    interval = 0.25
    while time.monotonic() < deadline:
        if _server_is_healthy(port):
            return True
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
    off-thread. Never raises: any failure (connection refused, timeout,
    malformed response) resolves to False - `_server_is_healthy`'s fail-closed
    contract, where a probe error reads as "not ready" and never propagates.
    The ~10 duplicated lines of that twin's `initialize` shape are deliberate
    (plan_M1 SS2.2 rejected-alternative #4, cross-review ruling: M1/M3
    singleton regions stay disjoint); consolidating is a future finding.
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
        # Fail-closed: connection refused (down), a hung/wedged backend that
        # never answers (timeout), or any other transport error all read as
        # "not ready" - matching _server_is_healthy's contract. DEBUG, not
        # WARNING (M10a convention, cf. _await_backend_http's identical catch):
        # this fires routinely during a normal cold start and on every watchdog
        # tick while a backend is briefly busy - the caller decides when
        # repeated failures are WARNING-worthy, not this single-attempt probe.
        _logger.debug("liveness probe attempt failed", exc_info=e)
        return False


def _server_version() -> str:
    try:
        from importlib.metadata import version

        return version(SERVER_NAME)
    except Exception:
        _logger.debug("could not resolve installed package version", exc_info=True)
        return "0.0.0"


def _source_fingerprint() -> str | None:
    """SHA-256 over the package's ``*.py`` source, so a backend built from
    now-stale source is not reused. COMPLETE (every module the backend can
    import), STABLE (identical bytes -> identical digest, immune to mtime/git
    quirks), CHEAP (~1 MB read+hash per cold-start discovery). Never raises: a
    read that keeps failing across ``_FINGERPRINT_ATTEMPTS`` yields ``None`` —
    UNREADABLE, which ``backend_registry.fingerprint_mismatch`` reads as
    unknown, never as "source changed" (F-829: this returned ``""``, which the
    gate could not tell from a real mismatch, so one OneDrive sync lock evicted
    the healthy backend every session was sharing).
    """
    import hashlib

    for _ in range(_FINGERPRINT_ATTEMPTS):
        h = hashlib.sha256()
        try:
            for p in sorted(SOURCE_ROOT.rglob("*.py")):
                if "__pycache__" not in p.parts:
                    h.update(p.relative_to(SOURCE_ROOT).as_posix().encode())
                    h.update(b"\0" + p.read_bytes() + b"\0")
            return h.hexdigest()
        except OSError as e:
            err = e
            time.sleep(_FINGERPRINT_RETRY_SECONDS)
    _logger.warning("source fingerprint unreadable: %s", err)
    return None


def _start_backend_holding_lock(port: int) -> None:
    """Start the singleton backend exactly once, holding the lock until it is
    healthy so no other session double-starts it.

    Runs in a daemon thread so it never blocks the stdio handshake. The lock is
    held for the whole backend cold start: any session that loses the lock race
    simply proxies to the backend the winner is bringing up.
    """
    try:
        with _exclusive_lock() as got_lock:
            if not got_lock:
                return  # another session owns startup; just proxy to it
            if _find_running_server() == port:
                return  # already up (same version) ON THE PORT WE WERE HANDED
            if _same_identity_backend_ready(port):
                return  # ours, merely busy or mid-boot — never evict it (F-807)
            # M2-3: surface WHY a fresh backend is about to spawn when the cause is a
            # source change (version matches, digests differ) — it is otherwise silent,
            # in the log and now (F-827) on the wire. Once per spawn HERE, not in the
            # thrice-called _find_running_server; the state+digest re-read is a cheap
            # unconditional diagnostic probe, deliberately NOT a second reuse gate.
            # Source-only: not a version change (#14), not an unreadable digest (F-829).
            entry = backend_registry.backend_on_port(_read_server_state(), port) or {}
            edited = backend_registry.fingerprint_mismatch(entry, _source_fingerprint())
            if edited and entry.get("version") == _server_version():
                _logger.info("backend stale (source changed), evicting")
                capture_lifecycle("proxy: backend evicted (source changed)", port=port)
            # A stale/legacy backend (different or unknown version) may still be
            # holding the port; evict it under the lock so our fresh, correctly
            # versioned backend can bind — otherwise the proxy would fall back to
            # the old backend and the upgrade would silently not take effect.
            _clear_stale_backend(port)
            _start_server_process(port)
            _wait_for_server(port)
            # Keep the lock past socket-bind, until the backend answers a real
            # initialize: release only when the reuse gate itself would pass,
            # so no thread can acquire inside the bind→ready gap (F-807).
            _same_identity_backend_ready(port)
    except Exception:
        # Best-effort; the proxy still answers initialize and retries. Before
        # M3 this was silent (F-183's primary handler) - a cold-start failure
        # left no trace anywhere. Now it's on disk even though control flow
        # is unchanged (M10a's rule: add a log line, leave the sentinel).
        _logger.exception("backend cold start failed")


def stop_backend() -> tuple[str, int | None]:
    """Stop the shared backend (CLI `stop` verb): an operator-initiated action
    that terminates every live browser session on it — that is the verb's
    purpose, not a side effect to guard against.

    Consumes M1's `_probe_backend_status()` for the state read (binding ruling:
    no new liveness check anywhere) — only a responsive/wedged backend is
    actually targeted for termination; a stale `down` record is cleared with
    nothing left to kill; `none` is reported as-is. Lock contention (a
    concurrent cold start/stop/restart already holding it) reports "busy" so
    the operator can retry instead of racing it.

    Returns ``(result, pid)``: "stopped" | "already stopped" | "not running" |
    "busy", with ``pid`` the terminated pid only when ``result == "stopped"``.
    """
    status, port = _probe_backend_status()
    if status == "none":
        return ("not running", None)

    with _exclusive_lock() as got:
        if not got:
            return ("busy", None)
        entry = backend_registry.backend_on_port(_read_server_state(), port)
        recorded_pid = backend_registry.recorded_int(entry, "pid")
        terminated = _terminate_backend(port) if port is not None else False
        # F-808: forget ONLY the backend we stopped. Unlinking the whole record
        # (what this used to do) would make a live backend on another display
        # context undiscoverable, and the next proxy start would spawn a second
        # one beside it. Clear the file only once nothing is left recorded, so
        # the single-backend case still ends with no record on disk at all.
        ctx = entry.get("display_context") if entry else None
        if ctx is not None:
            backend_registry.forget_backend(SERVER_STATE_FILE, str(ctx))
        if not backend_registry.read_backends(SERVER_STATE_FILE):
            _clear_server_state()
        if terminated:
            return ("stopped", recorded_pid)
        return ("already stopped", None)


def restart_backend() -> tuple[str, int | None]:
    """Restart the shared backend (CLI `restart` verb): the manual escape hatch
    for a wedged (M1) or stale same-version (M2) backend — terminate whatever
    is on the target port, then run the exact cold-start spawn sequence under
    the same lock, with the SAME primitives (plan_M8 SS2.1-B: no second spawn
    path, no new kill logic). Unconditional by design, so a "down"/"none"
    backend also ends up running, not merely evicted. The spawn port is chosen
    FIRST — `_select_backend_port()` (F-509 A1) — and terminate then targets
    exactly it, so both halves agree BY CONSTRUCTION (F-808), not via two reads
    that can diverge onto a sibling desktop's backend. Selection means a
    squatter on the dead backend's port forces a fresh `_free_port()` pick
    instead of a repeat 120s outage — the fallback port stays recorded (SSA1.5);
    `stop` clears `server.json`, the reset path to `DEFAULT_PORT`. Lock
    contention reports "busy" so the operator retries instead of racing. The
    post-restart state is reported via `_probe_backend_status()` (binding
    ruling: ONE liveness vocabulary) — a restart that comes back wedged or down
    must be visible, not assumed "responsive".

    Returns ``(status, pid)``: `_probe_backend_status`'s status or "busy";
    ``pid`` is the freshly recorded pid once the lock is acquired, else None.
    """
    # A PREFERENCE only: selection re-derives our own context's port itself.
    own = display_context.display_context()
    port = backend_registry.own_or_first_port(SERVER_STATE_FILE, own) or DEFAULT_PORT

    with _exclusive_lock() as got:
        if not got:
            return ("busy", None)
        port = _select_backend_port(port)
        _terminate_backend(port)
        _start_server_process(port)
        _wait_for_server(port)

    status, _ = _probe_backend_status()
    # The port WE spawned on, not first_backend's - same agree-on-one-port rule.
    fresh = backend_registry.backend_on_port(_read_server_state(), port)
    return (status, backend_registry.recorded_int(fresh, "pid"))


def _port_is_foreign_held(port: int) -> bool:
    """True iff ``port``'s socket is open but NOT held by our backend.

    The canonical "foreign occupant" predicate (F-509, plan_M8 Amendment
    A1): the one definition of "foreign," consumed both by the cold-start
    port fallback below and by doctor's foreign-occupant diagnostic
    (cli.py's ``_doctor_port_occupant_line``) — a single home instead of two
    places re-deriving the same condition.
    """
    return _server_is_healthy(port) and _backend_pid_on_port(port) is None


def _select_backend_port(preferred: int = DEFAULT_PORT) -> int:
    """Port to spawn the backend on (F-509 auto-fallback, plan_M8 Amendment
    A1). Prefers the port recorded for OUR OWN display context (so
    eviction/restart land where a prior backend ran), else ``preferred``. Keeps
    that target when free or held by OUR OWN backend; a FOREIGN occupant — or
    one another display context recorded, whose entry our spawn's own record
    would supersede-evict (F-808), or a target the OS FORBIDS us outright
    (F-509's field residual) — each forces an OS-assigned fallback via the one
    picker, ``proxy_forwarder.bindable_port``: recoverable, not a 120s outage.
    """
    # lazy; no module-top cycle
    from stealth_chrome_devtools_mcp.embedded.proxy_forwarder import bindable_port

    own = display_context.display_context()
    recorded = backend_registry.port_for_context(SERVER_STATE_FILE, own)
    target = preferred if recorded is None else recorded
    taken = backend_registry.port_conflict(SERVER_STATE_FILE, target, own)
    return bindable_port(target, force_new=taken or _port_is_foreign_held(target))


def ensure_server_running(port: int = DEFAULT_PORT) -> int | None:
    """Ensure the singleton backend is up or coming up, WITHOUT blocking.

    Returns the port to proxy to immediately. Unlike a blocking wait, this never
    delays the stdio ``initialize`` handshake behind the backend's cold start —
    the proxy answers ``initialize`` locally and only later requests wait for the
    backend. That decoupling is what keeps Claude Code's 30s connection timeout
    from firing under load / on a cold cache.
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
    uvicorn socket can answer (4xx) while FastMCP's MCP session manager is still
    starting — forwarding to it then fails with ``400`` (the same class of race
    as the old ``-32000``). Only a 200 to an ``initialize`` proves the MCP layer
    is genuinely ready to accept the client's session.
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


async def _watch_backend_liveness(port: int, **kwargs: object) -> None:
    """The F-820 watchdog, wired to THIS module's probes.

    The loop moved to ``backend_watchdog`` (F-856, which needed the lines); what
    stays here is the only part of it that has to know which probes are ours —
    the fast app-level check and the patient dead-vs-busy verdict, each driven
    off-thread because both block (plan_M1 SS2.2 rejected alternative #3: run
    inline they would freeze the stdio pump for up to ``LIVENESS_PROBE_TIMEOUT``
    every tick). Bound at call time, so patching either probe still steers the
    watchdog and an injected ``is_healthy``/``confirm_probe`` still wins.
    """
    import anyio

    run = anyio.to_thread.run_sync
    kwargs.setdefault("is_healthy", lambda: run(_backend_http_ready, port))
    kwargs.setdefault("confirm_probe", lambda: run(_same_identity_backend_ready, port))
    await backend_watchdog.watch_liveness(port, **kwargs)


async def _proxy_streams(client_read, client_write, port: int) -> None:
    """Answer ``initialize`` locally and instantly, then transparently proxy
    every other message to/from the singleton HTTP backend once it is ready.

    The transport plumbing (session-id capture, forwarding) is the same proven
    stdio↔streamable-HTTP pipe used previously; the only additions are the local
    ``initialize`` answer and swallowing the backend's duplicate ``initialize``
    response so the client never sees two.

    F-838: a CONFIRMED-dead backend no longer ends the proxy — the client stays
    connected on stdio while the backend leg heals and re-bridges onto a
    replacement. That loop, its bound and the herd live in ``proxy_selfheal``.
    """
    import anyio
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.message import SessionMessage
    from mcp.types import (
        DEFAULT_NEGOTIATED_VERSION,
        JSONRPCMessage,
        JSONRPCRequest,
        JSONRPCResponse,
    )

    from stealth_chrome_devtools_mcp.embedded import proxy_selfheal

    to_backend_tx, to_backend_rx = anyio.create_memory_object_stream(1024)
    init_request_id = {"value": None}
    init_message = {"value": None}
    pending = proxy_selfheal.PendingCalls()

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
                    init_message["value"] = msg  # F-838 replays it on a re-bridge
                # Forward everything (including initialize) so the backend session
                # initializes with the client's real params. Buffered until the
                # backend connects.
                await to_backend_tx.send(msg)
        finally:
            await to_backend_tx.aclose()

    async def run_backend(url, replay, armed):
        if not await _await_backend_http(url):
            # F-183: this used to return silently, leaving no cause on disk.
            _logger.error(
                "backend did not become ready within %.0fs", BACKEND_READY_TIMEOUT
            )
            return

        armed.set()  # arm the liveness monitor now that it is genuinely up
        init_swallowed = {"done": False}  # per generation: each backend answers
        backend_initialized = anyio.Event()  # our initialize exactly once
        async with streamablehttp_client(url) as (backend_read, backend_write, _):

            async def to_backend():
                # Forward the initialize first, then hold every later message
                # until the backend's initialize response establishes the
                # streamable-HTTP session id: streamablehttp_client stamps each
                # concurrent request with the CURRENT id, so a tools/list sent
                # before it exists yields 400. A real client gets that
                # sequencing by awaiting the initialize response; we answered
                # locally, so we reproduce the wait. ``replay`` is F-838's
                # re-bridge: generation 2+ re-sends the client's own initialize.
                first = replay or await to_backend_rx.receive()
                await backend_write.send(first)
                inner = first.message.root
                if isinstance(inner, JSONRPCRequest) and inner.method == "initialize":
                    await backend_initialized.wait()
                async for msg in to_backend_rx:
                    pending.track(msg.message.root)
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
                        pending.settle(inner)
                        await client_write.send(msg)
                finally:
                    # never leave to_backend blocked on a never-answered init
                    backend_initialized.set()

            async with anyio.create_task_group() as tg:
                tg.start_soon(to_backend)
                tg.start_soon(from_backend)

    async def backend_leg():
        # F-838/F-843: the leg outlives any single backend. proxy_selfheal owns
        # the generation loop — connect, watch, confirm — and on any confirmed
        # incident heals via ensure_server_running (the SAME startup path, so
        # the same reuse gate and cold-start lock) before re-bridging. It
        # returns only when nothing is left to heal; the teardown below runs.
        await proxy_selfheal.drive(
            port=port,
            url_for=_backend_http_url,
            connect=run_backend,
            watch=_watch_backend_liveness,
            confirm_alive=_same_identity_backend_ready,  # F-843's discriminator
            replay=lambda: init_message["value"],
            pending=pending,
            client_write=client_write,
            ensure_running=ensure_server_running,
            await_ready=_await_backend_http,
        )
        _logger.warning("backend became unreachable; tearing down for reconnect")
        tg.cancel_scope.cancel()

    async with anyio.create_task_group() as tg:
        tg.start_soon(backend_leg)
        # Drive the client pump in the main task. When the client (Claude Code)
        # disconnects, stdin hits EOF and pump_client returns — at which point we
        # cancel everything. Otherwise run_backend's from_backend loop stays
        # parked on the still-open backend stream forever and the proxy process
        # never exits, leaking one stranded process per disconnect.
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
            # open until its stdout-writer task finishes, and that task only
            # ends when the write stream is closed. Without this the process
            # hangs after every disconnect instead of exiting — one stranded
            # entrypoint per disconnect. Closing both streams lets stdio_server
            # tear down so the entrypoint returns and the process exits.
            await client_write.aclose()
            await client_read.aclose()


def run_stdio_proxy(port: int):
    """Run the stdio-to-HTTP proxy (blocking)."""
    import anyio

    # deferred: breaks the cycle
    from stealth_chrome_devtools_mcp.embedded.logging_setup import configure_logging

    configure_logging("proxy")
    anyio.run(_bridge, port)
