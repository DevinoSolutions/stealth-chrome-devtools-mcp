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
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psutil

from stealth_chrome_devtools_mcp.embedded import (
    backend_env,
    backend_eviction,
    backend_liveness,
    backend_probe,
    backend_registry,
    backend_watchdog,
    build_identity,
    client_presence,
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
# lives under). _source_fingerprint() hashes every *.py below it, so an in-place
# source edit is visible where a frozen editable-install version never is
# (F-206/F-120/F-504); build_identity.source_fingerprint carries the argument.
SOURCE_ROOT = Path(__file__).resolve().parent.parent
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


def _probe_port(port: int) -> str:
    """OUR binding of `backend_liveness.probe_port` — the ladder, with THIS
    module's two probes handed in. The reasoning lives with the leaf.

    A wrapper on purpose — THE statement of the convention every binding here
    follows: collaborators resolve HERE at call time, so a `monkeypatch.setattr`
    on this module still reaches the leaf, where importing the leaf's names
    directly would bind them at import time and never see the patch.
    """
    return backend_liveness.probe_port(
        port, is_healthy=_server_is_healthy, http_ready=_backend_http_ready
    )


def _probe_backend_status() -> tuple[str, int | None]:
    """OUR binding of `backend_liveness.probe_recorded`: this module's record
    path, this process's display context, and `_probe_port` above as the
    per-port probe (so a test that patches THAT still drives the walk).

    Every verb that must pick ONE recorded backend calls it through this name,
    so selection is fixed in one place; the order itself is the registry's.
    """
    return backend_liveness.probe_recorded(
        SERVER_STATE_FILE, display_context.display_context(), probe=_probe_port
    )


def _identity_matches(entry: backend_registry.BackendEntry | None) -> bool:
    """True iff ``entry`` records OUR version AND a source digest that does not
    CONTRADICT ours — the identity half of the reuse gate, with no probe.

    Extracted (F-886) because a second reader appeared — the eviction guard,
    which must know whether the backend it is about to terminate is a
    STRANGER's — and a third since, port SELECTION. Re-spelling it would let
    #14's version rule and F-829's three-state digest rule drift apart.

    It answers "is this entry MINE", and deliberately not "would I adopt it".
    F-889's first pass widened it to cover forward adoption and thereby inverted
    F-886 for the exact population (d) exists to protect: a NEWER stranger's
    backend read as ours, so `backend_eviction.protected`'s condition 2 returned
    no browsers and the kill site terminated a sibling still holding its user's
    logged-in Chrome (measured, F-889 review H1). Adoption is
    :func:`_adoptable_identity` — a separate name, consumed by the reuse gate
    alone — because the two questions have opposite answers for one entry and a
    predicate cannot hold both.
    """
    recorded = (entry or {}).get("version")
    return recorded == _server_version() and not backend_registry.fingerprint_mismatch(
        entry, _source_fingerprint()
    )


def _adoptable_identity(entry: backend_registry.BackendEntry | None) -> bool:
    """True iff we would REUSE ``entry``'s backend instead of spawning: ours, or
    a build strictly NEWER than ours (F-889 (d)).

    The forward half, and the one clause that ends the mixed-version eviction
    loop. ``fingerprint_mismatch`` answers "these two digests differ", never
    "mine is older", so two identities on one desktop each read the other as
    stale and each evicted the other on every proxy start — five waves in one
    measured session, every browser killed. A fleet MID-UPGRADE is precisely the
    population F-886's browser-protection rule does not cover, because neither
    side owns a browser yet. The comparison and its fail-closed guards are
    ``build_identity.newer``'s.

    Adopting forward means STEP ASIDE, never take the port, so this is read by
    :func:`_same_identity_backend_ready` and by nothing else. Port selection,
    the protection rule and the operator verbs all keep asking
    :func:`_identity_matches`: a newer backend that is not answering must stay
    protected while it owns a live browser, and ``restart``/``stop`` must go on
    targeting our OWN identity's backend exactly as they did before F-889.
    """
    return _identity_matches(entry) or build_identity.newer(
        (entry or {}).get("version"), _server_version()
    )


def _same_identity_backend_ready(port: int, patience: float | None = None) -> bool:
    """True iff ``server.json`` records OUR identity on ``port`` — the version
    matches and the fingerprint does not CONTRADICT it (issue #14/F-206 never
    reuse a stale, legacy or edited-source backend; F-829: an unreadable digest
    is unknown, not a contradiction — see ``fingerprint_mismatch``) — and it
    answers a real ``initialize`` in the patience window (F-301/F-501: a wedged
    backend holds its socket open, so only the app-level probe counts).

    THE one consumer of :func:`_adoptable_identity`, so "same identity" here has
    always meant "one we would reuse" — since F-889 (d) that includes a build
    strictly NEWER than ours. The name is kept because the suite patches it.

    ``patience`` is F-807's anti-fratricide grace for the cold-start lock path:
    a healthy backend absorbing a many-session startup herd can miss a single
    2s probe, and a lock-holder trusting that one miss "evicts" (kills) the
    backend everyone is using, then double-spawns. ``_watch_backend_liveness``
    applies the same discrimination mid-session (F-820). Identity-gated: a
    version- or source-stale record gets NO patience and is evicted at once.
    ``patience=0.0`` (discovery) probes once and never sleeps; ``None`` means
    ``REUSE_PATIENCE_SECONDS``, read at call time so tests can shrink it.

    F-856: the window is spent in FAIRLY SCHEDULED seconds, not wall seconds —
    a probe timeout is evidence about the backend only while this process is
    itself being scheduled. The measurement, its bound and the 2026-09-02
    incident behind it are ``scheduling_lag.FairWindow``'s, THE one home for
    that question; on an idle machine it is the wall-clock deadline it replaced.
    """
    # The entry recorded ON THIS PORT, not merely the first: under F-808's
    # per-context record another desktop's backend says nothing about `port`.
    entry = backend_registry.backend_on_port(_read_server_state(), port) or {}
    if not _adoptable_identity(entry):
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


# ── Eviction: the four bindings of `backend_eviction` ────────────────────────
# The rule ("a backend still serving live browsers is never evicted", F-886) and
# the act both live in that leaf, with the measurement and the argument. What
# stays here is the wiring that knows which record, which state dir and which
# probes are OURS — four more wrappers, for `_probe_port`'s reason.
def _backend_pid_on_port(port: int) -> int | None:
    """The pid of OUR backend listening on ``port``, or None."""
    return backend_eviction.pid_on_port(port, is_ours=_is_our_backend)


def _terminate_backend(port: int) -> bool:
    """Terminate OUR backend on ``port``; True iff one was found and killed."""
    entry = backend_registry.backend_on_port(_read_server_state(), port)
    return backend_eviction.terminate(
        port,
        pid_on_port=_backend_pid_on_port,
        recorded_pid=entry.get("pid") if entry else None,
        is_ours=_is_our_backend,
        is_healthy=_server_is_healthy,
    )


def _protecting_browsers(port: int) -> list[int]:
    """The live browsers that make the backend on ``port`` UNEVICTABLE (F-886),
    empty when it may be terminated and bound over."""
    return backend_eviction.protected(
        backend_registry.backend_on_port(_read_server_state(), port),
        state_dir=STATE_DIR,
        identity_matches=_identity_matches,
        is_ours=_is_our_backend,
        is_running=psutil.pid_exists,
    )


def _clear_stale_backend(port: int) -> list[int]:
    """Free ``port`` for a fresh backend of ours; the browsers that STOPPED it,
    empty when the caller may spawn (F-886).

    The reuse check is asked of the PORT, not of `_find_running_server`, which
    under F-808's adoption order may name another display context's backend, on
    another port.
    """
    return backend_eviction.clear_stale(
        port,
        reusable=lambda: _same_identity_backend_ready(port, patience=0.0),
        protecting=lambda: _protecting_browsers(port),
        terminate_backend=lambda: _terminate_backend(port),
    )


def _backend_interpreter() -> str:
    """The interpreter the backend runs on: the REAL one, never a Windows venv's
    redirector (F-866).

    In a Windows venv ``sys.executable`` is CPython's venv launcher, which
    re-spawns the real ``python.exe`` as a CHILD in a ``KILL_ON_JOB_CLOSE`` job
    and — console-less itself after our ``DETACHED_PROCESS`` — hands it a fresh
    console: a visible Windows Terminal window anyone can close. The detach flags
    and F-839's SIGBREAK immunity never reached the process that served (the
    2026-09-13 death: 32 h up, gone between two hygiene ticks, no trace).
    Launching ``sys._base_executable`` puts the flags on the serving process —
    no redirector, no job, no console; ``_start_server_process`` names the venv
    the way the launcher does. POSIX venvs have no redirector: unchanged.
    """
    base = getattr(sys, "_base_executable", None) or sys.executable
    if sys.platform == "win32" and os.path.normcase(base) != os.path.normcase(
        sys.executable
    ):
        return base
    return sys.executable


def _server_process_cmd(port: int) -> list[str]:
    return [
        _backend_interpreter(),
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
    # itself, so only a raw redirect of the child's handles captures it.
    from stealth_chrome_devtools_mcp.embedded import backend_launch, logging_setup

    boot_log = None
    try:
        log_dir = logging_setup.resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        # F-830: the launcher is the ONLY place this file can be rotated - once
        # a child inherits the fd, it pins it for life (see roll_boot_log).
        boot_log = logging_setup.roll_boot_log(log_dir)
    except OSError:
        # Fail-open (plan_M3 §7: "M3's file setup is fail-open"): a log dir
        # that can't be created must never block the backend from spawning -
        # fall back to the pre-M3 DEVNULL redirect instead.
        _logger.warning(
            "backend-boot.log unavailable; falling back to DEVNULL", exc_info=True
        )
        boot_log = None

    child_env = dict(os.environ)
    backend_env.scrub(child_env)  # F-890 + M8-2: what the backend must not inherit
    if cmd[0] != sys.executable:
        # F-866: the bypassed redirector's own venv hand-off. ``getpath`` reads it
        # to find ``pyvenv.cfg``, then CPython drops it from the environment before
        # any code runs, so the backend's Chrome children never inherit it.
        child_env["__PYVENV_LAUNCHER__"] = sys.executable

    # F-867: HOW the process is created is backend_launch's one job - the client
    # job this proxy sits in must not be inherited by the backend every other
    # session shares. The reuse gate, adoption order and cold-start lock stay
    # here; only the spawn moved.
    launched = backend_launch.spawn(cmd, child_env, boot_log)

    _ensure_state_dir()
    PORT_FILE.write_text(str(port))
    _write_server_state(port, _server_version(), launched.pid, _source_fingerprint())


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

    A binding, for the reason every binding in this file is one (see
    `_probe_port`): the suite patches THIS name, and what it knows that
    `backend_probe` must not is which URL a PORT of ours means. The probe
    itself - the `initialize` payload, the throwaway-session DELETE and the
    fail-closed contract - is `backend_probe.ready`'s, which is where the ~10
    lines this used to copy from `_await_backend_http` now live once (the
    plan_M1 SS2.2 #4 debt this docstring carried, paid by the F-889 review).
    """
    return backend_probe.ready(_backend_http_url(port), timeout)


# The two build-identity bindings. Wrappers for the reason every binding in
# this file is one (see `_probe_port`), and here doubly so: SERVER_NAME and
# SOURCE_ROOT are patched too, so both must resolve at CALL time.
def _server_version() -> str:
    return build_identity.version(SERVER_NAME)


def _source_fingerprint() -> str | None:
    return build_identity.source_fingerprint(SOURCE_ROOT)


def _report_eviction_decision(port: int, spared: list[int]) -> None:
    """Ship the source-change decision that was actually TAKEN (F-886 review,
    F4); it used to be shipped before the kill site decided, so a refusal
    reached Sentry and the durable log as an eviction. The evicted wording is
    byte-unchanged because ``tests/test_e2e_lifecycle_resilience.py`` greps it.

    Wire only on the refusal path — ``backend_eviction.clear_stale`` already
    writes that WARNING with the same count. (Restored verbatim: F-889's first
    pass compressed this sentence away to land the file at exactly 1000 LOC, and
    it was guidance, not prose — the headroom now comes from extracting
    ``backend_probe`` instead, which is what convention 4 asked for anyway.)
    """
    if spared:
        capture_lifecycle(
            "proxy: backend eviction refused (still serving)",
            port=port,
            browsers=len(spared),
        )
        return
    _logger.info("backend stale (source changed), evicting")
    capture_lifecycle("proxy: backend evicted (source changed)", port=port)


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
            # M2-3: read WHY this port's backend is about to be taken away when
            # the cause is a source change (version matches, digests differ) —
            # otherwise silent, in the log and (F-827) on the wire. Read HERE,
            # once per spawn, not in the thrice-called _find_running_server; a
            # cheap unconditional diagnostic probe, deliberately NOT a second
            # reuse gate. What it is USED for is decided below, once the kill
            # site has answered.
            entry = backend_registry.backend_on_port(_read_server_state(), port) or {}
            edited = backend_registry.fingerprint_mismatch(entry, _source_fingerprint())
            # A stale/legacy backend (different or unknown version) may still be
            # holding the port; evict it under the lock so our fresh, correctly
            # versioned backend can bind — otherwise the proxy would fall back to
            # the old backend and the upgrade would silently not take effect.
            # F-886: unless it is a stranger's backend still serving live
            # browsers, which is never evicted. Selection normally hands us a
            # free port in that case; if the record changed under us since, the
            # honest answer is to spawn nothing rather than kill it, and the
            # next proxy start re-selects.
            spared = _clear_stale_backend(port)
            # AFTER the decision, never before it (F-886 review, F4): this used
            # to sit above the call, so the refusal path — the one the comment
            # above exists for — shipped "backend evicted" to the durable log
            # and to Sentry about a backend still running. Source-only, as it
            # always was: not a version change (#14), not an unreadable digest
            # (F-829).
            if edited and entry.get("version") == _server_version():
                _report_eviction_decision(port, spared)
            if spared:
                return
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
        # F-886: by ENTRY, not by display context — a context can now hold two
        # clients' backends, and `stop` stopped exactly one port.
        if entry is not None:
            backend_registry.forget_entries(SERVER_STATE_FILE, [entry])
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
    that can diverge onto a sibling desktop's backend; a squatter on the dead
    backend's port therefore forces a fresh pick instead of a repeat 120s
    outage, and the fallback port stays recorded (SSA1.5). Lock contention
    reports "busy" so the operator retries instead of racing. The post-restart
    state is `_probe_port`'s verdict for THE PORT WE SPAWNED ON (binding ruling:
    ONE liveness vocabulary) — a restart that comes back wedged or down must be
    visible, not assumed "responsive".

    That port, never `_probe_backend_status()`'s (F-868): the adoption walk
    answers "is there a backend for ME", so on a multi-context record a
    RESPONSIVE sibling reported "responsive" for a restart whose own backend
    came up wedged. Both halves now read the one selected port.

    Returns ``(status, pid)``: `_probe_port`'s verdict or "busy"; ``pid`` is the
    freshly recorded pid once the lock is acquired, else None.
    """
    # A PREFERENCE only: selection re-derives our own context's port itself,
    # asking the SAME identity question, so the seed and the choice agree.
    own = display_context.display_context()
    seed = backend_registry.own_or_first_port(
        SERVER_STATE_FILE, own, matches=_identity_matches
    )
    port = seed or DEFAULT_PORT

    with _exclusive_lock() as got:
        if not got:
            return ("busy", None)
        port = _select_backend_port(port)
        _terminate_backend(port)
        _start_server_process(port)
        _wait_for_server(port)

    # Both halves read the port WE spawned on - the agree-on-one-port rule.
    fresh = backend_registry.backend_on_port(_read_server_state(), port)
    return (_probe_port(port), backend_registry.recorded_int(fresh, "pid"))


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

    F-886 adds one more such target: a port held by a STRANGER's backend that
    still owns live browsers, which may never be evicted. The clause is
    identity-gated by construction, because ``_protecting_browsers`` is — a
    backend of our OWN identity is never protected.

    That gate alone is not enough to keep ``restart_backend`` working, because
    the target must be CHOSEN on identity and not merely tested on it — hence
    ``matches=_identity_matches`` below. The measurement is in
    :func:`backend_registry.port_for_context`, which owns that rule.
    """
    # lazy; no module-top cycle
    from stealth_chrome_devtools_mcp.embedded.proxy_forwarder import bindable_port

    own = display_context.display_context()
    recorded = backend_registry.port_for_context(
        SERVER_STATE_FILE, own, matches=_identity_matches
    )
    target = preferred if recorded is None else recorded
    taken = backend_registry.port_conflict(SERVER_STATE_FILE, target, own)
    return bindable_port(
        target,
        force_new=taken
        or _port_is_foreign_held(target)
        or backend_eviction.stepping_aside(target, _protecting_browsers(target)),
    )


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

    The binding half of `backend_probe.await_ready` - it owns the DEADLINE this
    tree gives a cold start, and the suite patches this name. Why an
    ``initialize`` and not a socket connect or "any HTTP response" is argued
    once, in that module's docstring.
    """
    return await backend_probe.await_ready(url, deadline_seconds)


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
    # F-889's second witness. INLINE, not off-thread: a small local JSON read.
    witness = backend_liveness.self_report
    kwargs.setdefault("heartbeat", lambda: witness(SERVER_STATE_FILE, port))
    await backend_watchdog.watch_liveness(port, **kwargs)


async def _proxy_streams(client_read, client_write, port: int) -> None:
    """Answer ``initialize`` locally and instantly, then transparently proxy
    every other message to/from the singleton HTTP backend once it is ready.

    The transport plumbing (session-id capture, forwarding) is the same proven
    stdio↔streamable-HTTP pipe used previously; the only additions are the local
    ``initialize`` answer and swallowing the backend's duplicate ``initialize``
    response so the client never sees two.

    F-838/F-889: a CONFIRMED-dead backend no longer ends the proxy, and neither
    does a heal that fails — the client stays connected on stdio while the
    backend leg heals, backs off and re-bridges. That loop, its bounds and the
    herd live in ``proxy_selfheal``.
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

    # F-889 review M3: WHO launched us, captured now and never again — asked
    # later it would name whatever we were reparented to (init, on POSIX), i.e.
    # the check would stop working at exactly the moment it is needed. The rule
    # and its fail-open direction are `client_presence`'s; what to DO about the
    # answer is this function's, exactly as it is for `pump_client` returning.
    client = client_presence.capture()
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

    async def client_watch():
        # The SECOND exit, and deliberately not a third KIND of one: EOF and
        # this both mean "nobody is listening", and both end the session from
        # OUTSIDE the backend leg. F-889 (c) removed the proxy's ability to end
        # a session over the BACKEND; the same outage's cleanup found 116 stale
        # proxies, so "never exits on its own decision about the backend" has to
        # keep being different from "never exits". Declared before `backend_leg`
        # so the source pins that read that function's body cannot see it.
        await client_presence.await_gone(client)
        tg.cancel_scope.cancel()

    async def backend_leg():
        # F-838/F-843/F-889: the leg outlives any single backend. proxy_selfheal
        # owns the generation loop and heals via ensure_server_running (the SAME
        # startup path, so the same reuse gate and cold-start lock). It never
        # returns — the client's EOF below is this process's one exit.
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

    async with anyio.create_task_group() as tg:
        tg.start_soon(backend_leg)
        tg.start_soon(client_watch)
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
