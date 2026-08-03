"""``stealth-chrome-devtools`` — an ops CLI for the stealth browser MCP server.

A thin management layer over the *same* backend the MCP server uses: inspect the
singleton, list profiles, reclaim disk (the storage sweep), check the
environment, or start the server. It never reimplements browser logic — to drive
a browser, use the MCP server (or its HTTP backend) directly.

Two surfaces, one backend (F-700/F-109): this project ships two entry points —
``stealth-chrome-devtools-mcp`` (the MCP server: the tool surface an AI client
drives) and ``stealth-chrome-devtools`` (this ops CLI: the surface a human
inspects and operates). They are deliberately separate surfaces over the one
backend, not a single merged command; the registry side of this note lives in
``embedded/tool_registry.py``. Recorded for M14 (CONTRIBUTING/DESIGN).

Read-only commands (``status``, ``profiles``, ``cleanup`` without ``--apply``,
``doctor``) import the package with ``STEALTH_MCP_NO_AUTO_RECOVERY=1`` so merely
running the CLI never kills a running server's browsers or touches its profiles.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from stealth_chrome_devtools_mcp.observability import sentry_init


def _server():
    """Import the embedded server module, reusing its profile/storage helpers.

    Forces read-only import semantics: no orphan-process recovery, no atexit
    teardown handlers — so the CLI never disturbs a running backend.
    """
    os.environ.setdefault(  # noqa: TID251  PERMANENT(env write before import)
        "STEALTH_MCP_NO_AUTO_RECOVERY", "1"
    )
    from stealth_chrome_devtools_mcp.embedded import server

    return server


def _clone_storage():
    """Import the clone-storage subsystem (the profile/storage helpers),
    forcing the same read-only import semantics as :func:`_server`: the
    NO_AUTO_RECOVERY guard is set before the import so the CLI never disturbs
    a running backend."""
    os.environ.setdefault(  # noqa: TID251  PERMANENT(env write before import)
        "STEALTH_MCP_NO_AUTO_RECOVERY", "1"
    )
    from stealth_chrome_devtools_mcp.embedded import clone_storage

    return clone_storage


_BINARY_UNIT = 1024


def _human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < _BINARY_UNIT or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= _BINARY_UNIT
    return f"{value:.1f} TB"


def _role(cs, path: Path) -> str:
    if cs.clone_is_auto(path):
        return "auto-clone"
    if cs.clone_is_named(path):
        return "named"
    return "unmarked"


def _collect_profiles(cs) -> list[dict]:
    """Every profile under the session root with size, role, and in-use flag."""
    rows: list[dict] = []

    def _row(path: Path, role: str) -> dict:
        return {
            "name": path.name,
            "path": path,
            "role": role,
            "size": cs._dir_size_bytes(path),
            "in_use": cs._profile_has_running_browser(path),
        }

    master = cs.master_profile_dir()
    snapshot = cs.master_snapshot_dir()
    if master.exists():
        rows.append(_row(master, "master"))
    if snapshot.exists():
        rows.append(_row(snapshot, "snapshot"))

    clone_root = cs.clone_root_dir()
    if clone_root.exists():
        rows.extend(
            _row(child, _role(cs, child))
            for child in sorted(clone_root.iterdir())
            if child.is_dir()
        )
    return rows


def _gb_to_bytes(gb: float | None, fallback: int) -> int:
    if gb is None:
        return fallback
    return 0 if gb <= 0 else int(gb * (1024**3))


# ── commands ────────────────────────────────────────────────────────────────


def _format_backend_status() -> str:
    """Human-readable backend status for status/doctor, driven by
    `_probe_backend_status` (plan_M1 SS2.1-D) instead of `_find_running_
    server`'s binary reuse-or-not answer. Closes F-301's "status prints
    *running* through the whole outage" half: a wedged backend (socket open,
    dispatch loop dead) now reports UNRESPONSIVE instead of a plain
    "running" indistinguishable from a genuinely healthy one. Read-only -
    this performs a single initialize+DELETE probe, self-cleaning, same as
    every other consumer of `_backend_http_ready` (never evicts or spawns).
    """
    from stealth_chrome_devtools_mcp.embedded import singleton

    status, port = singleton._probe_backend_status()
    # "down" (a stale record but nothing actually listening) and "none" (no
    # record at all) both read as "not running" to an operator - there is no
    # live process to reconnect to either way; plan_M1 SS2.1-D's three
    # display strings map 1:1 to what matters operationally: not running /
    # responsive / wedged.
    if status in ("none", "down"):
        return "not running"
    if status == "wedged":
        return (
            f"running but UNRESPONSIVE on port {port} — wedged; "
            "a new session will evict and respawn it"
        )
    return f"running (responsive) on port {port}"


def _recorded_backend_pid() -> int | None:
    """The pid singleton last recorded for the backend (server.json), or None
    if there is no record. Independent of liveness — status/doctor combine
    this with `_format_backend_status()`'s liveness read separately (F-305).

    The FIRST recorded backend: the record can now hold one per display context
    (F-808), and naming them all belongs to `_doctor_backend_lines`, not to this
    one-value line."""
    from stealth_chrome_devtools_mcp.embedded import backend_registry, singleton

    entry = backend_registry.first_backend(singleton._read_server_state())
    return backend_registry.recorded_int(entry, "pid")


def _backend_log_location(pid: int | None) -> str:
    """Where to look for backend logs (F-503's log-path half: M3 delivered
    "there is now a log"; this delivers "here is where"). Names the exact
    per-pid file when a pid is recorded, else the shared boot log."""
    from stealth_chrome_devtools_mcp.embedded.logging_setup import resolve_log_dir

    filename = f"backend-{pid}.log" if pid is not None else "backend-boot.log"
    return str(resolve_log_dir() / filename)


def _probe_recorded_backend(port: int | None) -> str:
    """One recorded backend's liveness on a port the caller already holds, in
    `_probe_backend_status`'s exact vocabulary: down / wedged / responsive —
    plus "no port recorded" for an entry naming nothing usable as a port, a
    state that function cannot reach (it reports such a record as no backend
    at all, and so has no word for it).

    Same two primitives in the same order producing the same three words as
    `singleton._probe_backend_status` (ONE liveness vocabulary, plan_M8
    SS2.1-B) — all that differs is where the port comes from. That function
    reads the FIRST recorded backend, which cannot answer for a record holding
    one per display context (F-808), and singleton.py sits at its LOC budget,
    so the per-port form lives here rather than beside it. It reaches the
    primitives THROUGH the module (`singleton._server_is_healthy`), never by
    importing their names, so a test that patches singleton still reaches them.
    """
    from stealth_chrome_devtools_mcp.embedded import singleton

    if port is None:
        return "no port recorded"
    if not singleton._server_is_healthy(port):
        return "down"
    return "responsive" if singleton._backend_http_ready(port) else "wedged"


def _doctor_backend_lines() -> list[str]:
    """One line per RECORDED backend — context, port, pid, version, liveness,
    and whether a window launched there could be seen — plus the remedy when
    none of them can show one.

    The `backend :` line above answers "is the backend up"; this answers "which
    desktops have one", the operational question F-808 created. A headed spawn
    is refused when the backend serving it can neither display a window nor hand
    the launch to a logged-on desktop (F-810), and that refusal points the
    operator at this command, so it must be able to name every context — the
    summary line, which reports the first recorded backend only, cannot.
    Ordering is `backend_registry.window_capable_first`'s, so doctor presents
    the same preference discovery applies rather than re-deriving one.

    The remedy is suppressed only by a window-capable backend that is actually
    RESPONSIVE. A desktop backend recorded but dead — the desktop logged out —
    would otherwise hide the advice in precisely the state that needs it: an
    SSH session's headed spawn is served by no live capable backend, so
    "start one from a desktop session" is still worth saying. A wedged one is no
    better: it cannot serve the spawn either. The per-line "(can show windows)"
    note stays token-driven — it describes where that backend's windows WOULD
    appear, which is true whether or not it is currently answering.

    What that state MEANS changed with F-810, so the remedy has two forms. With
    a user logged on at the desktop, headed spawns self-heal (the OS places the
    launch there), and telling the operator they "will fail" would be a lie
    about their own machine — the advice degrades to an optimisation. Only with
    nobody logged on is the spawn genuinely refused.
    """
    from stealth_chrome_devtools_mcp.embedded import (
        backend_registry,
        desktop_launch,
        singleton,
    )
    from stealth_chrome_devtools_mcp.embedded.display_context import HEADLESS

    lines: list[str] = []
    serviceable = False
    for entry in backend_registry.window_capable_first(singleton.SERVER_STATE_FILE):
        context = str(entry.get("display_context"))
        port = backend_registry.recorded_int(entry, "port")
        pid = backend_registry.recorded_int(entry, "pid")
        version = entry.get("version")
        status = _probe_recorded_backend(port)
        serviceable = serviceable or (context != HEADLESS and status == "responsive")
        lines.append(
            f"backend  {context}  port {port if port is not None else '-'}  "
            f"pid {pid if pid is not None else '-'}  "
            f"version {version if isinstance(version, str) else '-'}  "
            f"{status}  "
            f"({'headless only' if context == HEADLESS else 'can show windows'})"
        )
    if not lines:
        # Not the headless-only diagnosis: with nothing recorded the next
        # session cold-starts a backend in whatever context it runs in, so
        # there is no remedy to give yet.
        return ["backend  (none recorded)"]
    if not serviceable:
        # "no LIVE backend": the lines above may well show a capable one that
        # is down, and a remedy contradicting the list it follows is worse than
        # no remedy. The instruction deliberately echoes the spawn refusal's
        # advice (embedded/server.py's headed-visibility guard) so an operator
        # who arrives here from that error recognises it — a close paraphrase,
        # not a shared constant; keep the two saying the same thing. Both forms
        # keep the leading phrase verbatim: it is the greppable diagnosis, and
        # only the CONSEQUENCE differs between them.
        if desktop_launch.available():
            lines.append(
                "no live backend can display a window, but a user is logged on "
                "at the desktop: headed spawns are launched there automatically "
                "(F-810) — start a backend from a desktop session to skip that hop"
            )
        else:
            lines.append(
                "no live backend can display a window and nobody is logged on at "
                "the desktop: headed spawns will fail — start one from a desktop "
                "session and any session will use it automatically"
            )
    return lines


def _doctor_port_occupant_line() -> str:
    """F-509 visibility: is the target port free, ours, or a NON-stealth
    process squatting it (which would otherwise silently block a backend
    from binding)? Uses only existing helpers — no new port logic."""
    from stealth_chrome_devtools_mcp.embedded import backend_registry, singleton

    entry = backend_registry.first_backend(singleton._read_server_state())
    recorded = entry.get("port") if entry else None
    port = recorded if isinstance(recorded, int) else singleton.DEFAULT_PORT
    our_pid = singleton._backend_pid_on_port(port)
    if our_pid is not None:
        return f"port {port} held by our backend (pid {our_pid})"
    if singleton._port_is_foreign_held(port):
        return f"port {port} held by a NON-stealth process — a backend cannot bind here"
    return f"port {port} free"


def _cmd_status(_args) -> int:
    cs = _clone_storage()
    from stealth_chrome_devtools_mcp.embedded import singleton

    root = cs.default_session_root()
    pid = _recorded_backend_pid()
    print(f"backend     : {_format_backend_status()}")
    print(f"pid         : {pid if pid is not None else '-'}")
    print(f"log         : {_backend_log_location(pid)}")
    print(f"version     : {singleton._server_version()}")
    print(f"browser-session root: {root}  (exists: {root.exists()})")
    print(
        f"clone cap   : {_human(cs.clone_storage_cap_bytes())}  "
        f"[STEALTH_MCP_CLONE_STORAGE_CAP_GB]"
    )
    print(
        f"browser-session cap : {_human(cs.browser_session_storage_cap_bytes())}  "
        f"[STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB]"
    )
    return 0


def _cmd_profiles(_args) -> int:
    rows = _collect_profiles(_clone_storage())
    if not rows:
        print("no profiles found.")
        return 0
    for row in sorted(rows, key=lambda r: r["size"], reverse=True):
        print(
            f"  {row['name'][:44]:44s} {row['role']:11s} "
            f"{_human(row['size']):>10s}  in_use={row['in_use']}"
        )
    print(f"  {'total':44s} {'':11s} {_human(sum(r['size'] for r in rows)):>10s}")
    return 0


def _cmd_cleanup(args) -> int:
    cs = _clone_storage()
    clone_root = cs.clone_root_dir()
    clone_cap = _gb_to_bytes(args.clone_cap_gb, cs.clone_storage_cap_bytes())
    session_cap = _gb_to_bytes(
        args.browser_session_cap_gb, cs.browser_session_storage_cap_bytes()
    )

    # Same selectors the live sweep uses — dry-run and apply can't disagree.
    to_delete = cs._idle_autoclones_over_cap(clone_root, clone_cap)
    to_trim = cs._named_profiles_over_session_cap(clone_root, session_cap)
    delete_bytes = sum(cs._dir_size_bytes(p) for p in to_delete)
    trim_bytes = sum(cs._regenerable_size(p) for p in to_trim)

    print(f"clone root  : {clone_root}")
    print(
        f"caps        : clone {_human(clone_cap)} | "
        f"browser-session {_human(session_cap)}"
    )
    if not to_delete and not to_trim:
        print("nothing to reclaim - storage is within caps.")
        return 0

    if to_delete:
        print(
            f"\ndelete {len(to_delete)} idle auto-clone(s) - frees "
            f"{_human(delete_bytes)}:"
        )
        for path in to_delete:
            print(f"   - {path.name[:50]:50s} {_human(cs._dir_size_bytes(path)):>10s}")
    if to_trim:
        print(
            f"\ntrim {len(to_trim)} idle named profile(s) - frees "
            f"~{_human(trim_bytes)} (logins kept):"
        )
        for path in to_trim:
            print(
                f"   - {path.name[:50]:50s} ~{_human(cs._regenerable_size(path)):>10s}"
            )
    print(f"\ntotal reclaimable: ~{_human(delete_bytes + trim_bytes)}")

    if not args.apply:
        print("\n(dry run - nothing deleted. Re-run with --apply to reclaim.)")
        return 0

    removed = cs._enforce_clone_storage_cap_in(clone_root, clone_cap, "cli")
    freed = cs._enforce_named_profile_trim_in(clone_root, session_cap, "cli")
    print(
        f"\napplied: deleted {removed} auto-clone(s); trimmed "
        f"{_human(freed)} from named profiles."
    )
    return 0


def _cmd_doctor(_args) -> int:
    import platform

    cs = _clone_storage()

    ok = True
    print(f"python      : {platform.python_version()}")
    print(f"platform    : {platform.platform()}")
    root = cs.default_session_root()
    print(f"browser-session root: {root}  (exists: {root.exists()})")
    pid = _recorded_backend_pid()
    print(f"backend     : {_format_backend_status()}")
    print(f"pid         : {pid if pid is not None else '-'}")
    print(f"log         : {_backend_log_location(pid)}")
    print(f"port        : {_doctor_port_occupant_line()}")
    print("contexts    :")
    for line in _doctor_backend_lines():
        print(f"  {line}")

    chrome = _find_chrome()
    print(f"chrome      : {chrome or 'NOT FOUND — install Google Chrome'}")
    if not chrome:
        ok = False
    return 0 if ok else 1


def _find_chrome() -> str | None:
    import shutil

    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def _cmd_stop(_args) -> int:
    """Thin front-end over `singleton.stop_backend()` — no matching/kill
    logic of its own (that lives in singleton.py, reused from eviction)."""
    _server()
    from stealth_chrome_devtools_mcp.embedded import singleton

    result, pid = singleton.stop_backend()
    if result == "stopped":
        print(f"stopped backend (pid {pid}).")
        return 0
    if result == "already stopped":
        print("backend already stopped (stale state cleared).")
        return 0
    if result == "not running":
        print("backend not running.")
        return 0
    print("busy: another session is starting/stopping the backend right now — retry.")
    return 1


def _cmd_restart(_args) -> int:
    """Thin front-end over `singleton.restart_backend()` — terminate then a
    fresh cold-start spawn under the same lock cold start uses; no lifecycle
    logic of its own (that lives in singleton.py)."""
    _server()
    from stealth_chrome_devtools_mcp.embedded import singleton

    status, pid = singleton.restart_backend()
    if status == "busy":
        print(
            "busy: another session is starting/stopping the backend right now — retry."
        )
        return 1
    if status == "responsive":
        print(f"backend restarted (responsive) (pid {pid}).")
        return 0
    if status == "wedged":
        print(
            f"backend restarted but is UNRESPONSIVE (wedged) (pid {pid}) — "
            "it came up but is not answering; a new session will evict and "
            "respawn it, or try `restart` again."
        )
        return 1
    # "down" (spawned but the socket never came up) or "none" (no state at
    # all afterward) - both mean the restart did not produce a running
    # backend. Report honestly rather than implying success.
    print(f"backend restart did not bring the backend up (state: {status}, pid {pid}).")
    return 1


def _cmd_kill_orphans(args) -> int:
    """Thin, gated trigger of the existing orphan reaper — a direct call on
    the already-constructed `process_cleanup` module singleton (import-time
    recovery was skipped because `_server()` sets
    `STEALTH_MCP_NO_AUTO_RECOVERY=1` before the import). No new matching
    logic; the canonical create_time+user-data-dir matcher stays in
    process_cleanup.py (plan_M8 SS2.1-C; M11a adds a public seam later, per
    state.json's recorded decision).

    Guarded off a LIVE backend: reaping would kill that backend's own browsers.
    `restart` is the verb for "backend alive but bad"; this verb is for
    "backend gone, browsers orphaned" — a clean behavioral partition.

    `--force` overrides the guard, and is passed THROUGH to the reaper rather
    than merely getting past the gate. Since plan_F808 Task 10 the reaper spares
    entries a live backend still owns, which would otherwise have made
    `--force` a no-op against exactly the wedged backend it exists for.
    """
    _server()
    from stealth_chrome_devtools_mcp.embedded import process_cleanup, singleton

    status, _ = singleton._probe_backend_status()
    if status in ("responsive", "wedged") and not args.force:
        pid = _recorded_backend_pid()
        print(
            f"a backend is running (pid {pid if pid is not None else '-'}); "
            "use restart to recover it, or pass --force."
        )
        return 1

    process_cleanup.process_cleanup.recover_orphans(force=args.force)
    print(
        "orphan recovery triggered: reaped any browsers left over from a dead backend."
    )
    return 0


def _cmd_serve(args) -> int:
    # Delegate to the same entrypoint as `stealth-chrome-devtools-mcp` so server
    # lifecycle (incl. orphan recovery) behaves exactly as normal.
    from stealth_chrome_devtools_mcp import server as shim

    if args.http:
        sys.argv = [
            "stealth-chrome-devtools-mcp",
            "--transport",
            "http",
            "--port",
            str(args.port),
            "--host",
            args.host,
        ]
    else:
        sys.argv = ["stealth-chrome-devtools-mcp", "--transport", "stdio"]
    shim.main()
    return 0


_DISPATCH = {
    "status": _cmd_status,
    "profiles": _cmd_profiles,
    "cleanup": _cmd_cleanup,
    "doctor": _cmd_doctor,
    "stop": _cmd_stop,
    "restart": _cmd_restart,
    "kill-orphans": _cmd_kill_orphans,
    "serve": _cmd_serve,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stealth-chrome-devtools",
        description="Ops CLI for the stealth Chrome DevTools MCP server "
        "(inspect, reclaim disk, start). For browser automation, use the MCP server.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "status", help="show backend state, browser-session root, and storage caps"
    )
    sub.add_parser("profiles", help="list profiles with size, role, and in-use flag")

    clean = sub.add_parser(
        "cleanup",
        help="reclaim disk: delete idle auto-clones over the clone cap and trim "
        "idle named profiles over the browser-session cap (dry run unless --apply)",
    )
    clean.add_argument(
        "--apply", action="store_true", help="actually reclaim (default: dry run)"
    )
    clean.add_argument(
        "--browser-session-cap-gb",
        type=float,
        default=None,
        dest="browser_session_cap_gb",
        help="override the named-profile trim cap for this run (GB; 0 disables)",
    )
    clean.add_argument(
        "--clone-cap-gb",
        type=float,
        default=None,
        dest="clone_cap_gb",
        help="override the auto-clone delete cap for this run (GB; 0 disables)",
    )

    sub.add_parser(
        "doctor",
        help="check Python, platform, browser-session root, backend, and Chrome",
    )

    sub.add_parser(
        "stop", help="terminate the shared backend (kills all live browser sessions)"
    )

    sub.add_parser(
        "restart",
        help="restart the shared backend (kills all live browser sessions)",
    )

    kill_orphans = sub.add_parser(
        "kill-orphans",
        help="reap orphaned browser processes left behind by a dead backend "
        "(refuses against a live backend unless --force)",
    )
    kill_orphans.add_argument(
        "--force",
        action="store_true",
        help="override the live-backend guard and reap anyway",
    )

    serve = sub.add_parser(
        "serve", help="start the MCP server (stdio by default, or --http)"
    )
    serve.add_argument(
        "--http", action="store_true", help="serve over HTTP instead of stdio"
    )
    from stealth_chrome_devtools_mcp.embedded.singleton import DEFAULT_PORT

    serve.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTP port (default {DEFAULT_PORT})",
    )
    serve.add_argument(
        "--host", default="127.0.0.1", help="HTTP host (default 127.0.0.1)"
    )
    return parser


def main(argv=None) -> int:
    # Ship errors to Sentry (on by default; no-op under
    # STEALTH_MCP_NO_ERROR_REPORTING, and never raises).
    sentry_init()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
