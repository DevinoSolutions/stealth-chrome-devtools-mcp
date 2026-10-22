"""Launcher resolution, isolated env, ports, logs, process-tree and stderr helpers.

Split out of :mod:`release_gate_harness` (T-6148); that module re-exports every name
here, so callers keep importing from ``release_gate_harness``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import psutil

from release_gate_harness_consts import _STDERR_CAP_BYTES, _STDERR_CAP_LINES

_log = logging.getLogger("release_gate_harness")


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
# The product's own default singleton port (``singleton.DEFAULT_PORT``), restated
# as a literal for the same reason ``REGISTRY_TOOL_COUNT`` is: this harness
# drives the installed artifact as a black box.
DEFAULT_SINGLETON_PORT = 19222
_PORT_PICK_ATTEMPTS = 32


def _os_assigned_port() -> int:
    """One OS-assigned free loopback port. The single seam ``_pick_free_port``
    retries through; a test can replace it to hand back a chosen sequence."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _reserved_ports(real_home: Path | None = None) -> frozenset[int]:
    """Ports an isolated workspace must never bind: the product's default
    singleton port plus every port the DEVELOPER'S real ``server.json`` records.

    An isolated backend on one of these could not evict a live real backend
    (bind fails on a port in use) — but with the real backend DOWN it would
    squat its recorded port, the developer's next real proxy would adopt the
    throwaway backend through ``adoption_candidates``, and that backend dies at
    workspace teardown: the exact ``CONNECTION_CLOSED`` the lifecycle suite
    exists to eliminate, handed to the developer by the suite itself. The
    Windows dynamic range measured here is 1025-65534 (``netsh int ipv4 show
    dynamicport tcp``: start 1025, 64510 ports), so an OS pick CAN land there.
    """
    home = Path.home() if real_home is None else real_home
    return frozenset({DEFAULT_SINGLETON_PORT, *_recorded_ports(home)})


def _pick_free_port(*, real_home: Path | None = None) -> int:
    """An OS-assigned free loopback port that is NOT one of
    :func:`_reserved_ports`, used as a distinct singleton port.

    Retries the OS pick rather than adjusting it: a port this harness cannot
    prove is safe is not one it may hand out, so exhaustion raises.
    """
    reserved = _reserved_ports(real_home)
    for _ in range(_PORT_PICK_ATTEMPTS):
        port = _os_assigned_port()
        if port not in reserved:
            return port
    raise RuntimeError(
        f"no free loopback port outside the reserved set {sorted(reserved)} in "
        f"{_PORT_PICK_ATTEMPTS} attempts"
    )


def use_isolated_token(monkeypatch, home_dir: Path) -> None:
    """Make THIS process's first-party clients send the isolated backend's bearer
    token (F-1018).

    ``backend_token.token_path`` is ``backend_registry.STATE_DIR / "backend.token"``
    and ``STATE_DIR`` is the TEST process's own home. A backend started under
    :func:`_isolated_env` wrote its token under ``home_dir`` instead, so an
    in-process probe of it (``singleton._backend_http_ready``,
    ``backend_probe.ready``) sent another token, or none, and read the 401 as
    "not serving". Subprocesses need nothing: their HOME already points there."""
    from stealth_chrome_devtools_mcp.embedded import backend_registry

    monkeypatch.setattr(backend_registry, "STATE_DIR", home_dir / ".stealth-mcp")


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
    # The workspace's logs are EVIDENCE — a failing node's whole post-mortem,
    # and for the lifecycle suite an oracle. `env = dict(os.environ)` above
    # inherits the developer's exported settings, so a `STEALTH_MCP_LOG_LEVEL`
    # of WARNING (a perfectly reasonable thing to export) would silently drop
    # every INFO line these workspaces read: `backend_watchdog`'s
    # `was busy, not dead` verdict is INFO while its strikes are WARNING, so a
    # run where F-820 did exactly the right thing would arrive as strikes with
    # no verdict — a false RED about the product, caused by the harness. The
    # level is therefore DECLARED here beside the directory, not inherited: the
    # harness chooses what the workspace records, as it already chooses where.
    env["STEALTH_MCP_LOG_LEVEL"] = "INFO"
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


def _log_dirs(dirs: tuple[Path, ...]) -> list[Path]:
    return [
        d for d in (*dirs, *(p / ".stealth-mcp" / "logs" for p in dirs)) if d.is_dir()
    ]


def _backend_logs(*dirs: Path) -> str:
    """The isolated backend's own logs, for failures the exception text alone
    cannot explain.

    The journey runs the backend as a DETACHED child, so a tool failure arrives
    as a bare protocol error — the reason (Chrome launch args, CDP stalls, the
    orphan-recovery lines that root-caused B1) is only in the backend's log
    files, which die with the throwaway home. Cheap on the happy path: only
    read when the journey has already failed.

    Every ``backend-*.log`` (the per-boot logs and the shared boot log) is
    included, THEN the two newest other logs. Selecting "the two newest logs"
    alone was right for a one-proxy journey and wrong for the startup herd:
    twelve proxies write after the backend does, so three wedge reports in a
    row (F-859 §9-§11) carried two proxy logs and no backend log, and the
    absence was read as a fact about the workspace. The proxy fleet's own view
    is :func:`_proxy_warnings`.
    """
    seen: set[Path] = set()
    chunks: list[str] = []
    for d in _log_dirs(dirs):
        by_age = sorted(d.glob("*.log"), key=lambda p: p.stat().st_mtime)
        backend = [p for p in by_age if p.name.startswith("backend-")]
        others = [p for p in by_age if not p.name.startswith("backend-")][-2:]
        for log in (*backend, *others):
            if log in seen:
                continue
            seen.add(log)
            chunks.append(f"[{log.name}]\n{_read_capped(log)}")
    return "\n".join(chunks) if chunks else "(no backend log files found)"


# The proxy digest keeps this many WARNING-or-worse lines, newest last: the end
# of a wedge is where every proxy says what it concluded.
_PROXY_DIGEST_LINES = 120
_WARNING_LINE_RE = re.compile(r"^\S+ \S+ (WARNING|ERROR|CRITICAL) ")


def _proxy_warnings(*dirs: Path) -> str:
    """Every proxy's WARNING-or-worse lines, by file, capped — the fleet's view.

    A herd's proxies write mostly DEBUG probe noise; what decides "one
    connection dropped" from "every proxy lost the backend in the same second"
    is which of them warned, and when. One digest instead of twelve raw logs.
    """
    entries: list[tuple[str, str]] = []  # (log file, warning line), oldest first
    for d in _log_dirs(dirs):
        for log in sorted(d.glob("proxy-*.log"), key=lambda p: p.stat().st_mtime):
            try:
                text = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            entries.extend(
                (log.name, ln) for ln in text.splitlines() if _WARNING_LINE_RE.match(ln)
            )
    if not entries:
        return "(no proxy warnings)"
    elided = len(entries) - _PROXY_DIGEST_LINES
    out: list[str] = []
    if elided > 0:
        entries = entries[-_PROXY_DIGEST_LINES:]
        out.append(f"(+{elided} earlier warning line(s) elided)")
    current = None
    for name, line in entries:
        if name != current:
            out.append(f"[{name}]")
            current = name
        out.append(line)
    return "\n".join(out)


def _backend_entries(home_dir: Path) -> list[dict[str, object]]:
    """Every backend entry ``<home_dir>/.stealth-mcp/server.json`` records.

    THE one parse of the record in this harness. Reads ALL THREE schemas — the
    flat v1 shape (one entry, the record itself), F-808's v2
    ``{"schema": 2, "backends": {ctx: entry}}`` and F-886's v3
    ``{"schema": 3, "backends": [entry, ...]}`` — because this harness drives
    the INSTALLED artifact as a black box and restates the contract rather than
    importing the package under test (the same reason ``REGISTRY_TOOL_COUNT``
    is a literal here). A missing, unreadable or malformed record is an empty
    list, never an error: for the developer's REAL home that means "nothing to
    avoid", for an isolated home "nothing recorded yet".
    """
    state_file = home_dir / ".stealth-mcp" / "server.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(state, dict):
        return []
    backends = state.get("backends")
    # v2 keys the entries by display context; v3 (F-886) is a list, so one
    # context can hold one backend per source IDENTITY. Both normalize to the
    # same list of entries here, and v1 is the record itself.
    if isinstance(backends, dict):
        backends = list(backends.values())
    if not isinstance(backends, list):
        return [state]
    return [e for e in backends if isinstance(e, dict)]


def _backend_pid_from_state(home_dir: Path) -> int | None:
    """The isolated backend's recorded pid — the FIRST entry's.

    An isolated HOME holds one backend per IDENTITY; every gate node runs one
    identity, so "some recorded entry" and "the one we spawned" are the same
    thing. The one node that runs two (``test_e2e_lifecycle_resilience``'s S5)
    reads the first as the incumbent it spawned first, which is exactly the pid
    that must not change."""
    entries = _backend_entries(home_dir)
    pid = entries[0].get("pid") if entries else None
    return pid if isinstance(pid, int) else None


def _backend_pids_from_state(home_dir: Path) -> list[int]:
    """EVERY backend pid the record names, in recorded order — what teardown
    consults (F-886).

    A workspace that ran two source identities has two detached backends, and
    terminating only the first would leak the second past the block that owns
    it. The singular reader above stays, because every ASSERTION in the suite
    is about THE backend a node ran on, which is the first."""
    return [
        pid
        for entry in _backend_entries(home_dir)
        if isinstance(pid := entry.get("pid"), int)
    ]


def _recorded_ports(home_dir: Path) -> frozenset[int]:
    """Every port ``home_dir``'s record names, across every display context."""
    return frozenset(
        port
        for entry in _backend_entries(home_dir)
        if isinstance(port := entry.get("port"), int)
    )


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


def _pid_in_workspace(pid: int, work_dir: Path) -> bool:
    """Whether *pid* is one this workspace created. A process we cannot inspect
    is reported as ours: a "no orphan" claim must fail closed."""
    try:
        return any(str(work_dir) in arg for arg in psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.Error:
        return True


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
