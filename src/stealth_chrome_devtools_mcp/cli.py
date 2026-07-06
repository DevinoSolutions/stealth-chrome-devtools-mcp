"""``stealth-chrome-devtools`` — an ops CLI for the stealth browser MCP server.

A thin management layer over the *same* backend the MCP server uses: inspect the
singleton, list profiles, reclaim disk (the storage sweep), check the
environment, or start the server. It never reimplements browser logic — to drive
a browser, use the MCP server (or its HTTP backend) directly.

Read-only commands (``status``, ``profiles``, ``cleanup`` without ``--apply``,
``doctor``) import the package with ``STEALTH_MCP_NO_AUTO_RECOVERY=1`` so merely
running the CLI never kills a running server's browsers or touches its profiles.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

EMBEDDED_DIR = Path(__file__).with_name("embedded")


def _ensure_embedded_on_path() -> None:
    path = str(EMBEDDED_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


def _server():
    """Import the embedded server module, reusing its profile/storage helpers.

    Forces read-only import semantics: no orphan-process recovery, no atexit
    teardown handlers — so the CLI never disturbs a running backend.
    """
    os.environ.setdefault(  # noqa: TID251  PERMANENT(env write before import)
        "STEALTH_MCP_NO_AUTO_RECOVERY", "1"
    )
    _ensure_embedded_on_path()
    import server

    return server


def _human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _role(server, path: Path) -> str:
    if server._clone_is_auto(path):
        return "auto-clone"
    if server._clone_is_named(path):
        return "named"
    return "unmarked"


def _collect_profiles(server) -> list[dict]:
    """Every profile under the session root with size, role, and in-use flag."""
    rows: list[dict] = []

    def _row(path: Path, role: str) -> dict:
        return {
            "name": path.name,
            "path": path,
            "role": role,
            "size": server._dir_size_bytes(path),
            "in_use": server._profile_has_running_browser(path),
        }

    master = server._master_profile_dir()
    snapshot = server._master_snapshot_dir()
    if master.exists():
        rows.append(_row(master, "master"))
    if snapshot.exists():
        rows.append(_row(snapshot, "snapshot"))

    clone_root = server._clone_root_dir()
    if clone_root.exists():
        for child in sorted(clone_root.iterdir()):
            if child.is_dir():
                rows.append(_row(child, _role(server, child)))
    return rows


def _gb_to_bytes(gb: float | None, fallback: int) -> int:
    if gb is None:
        return fallback
    return 0 if gb <= 0 else int(gb * (1024**3))


# ── commands ────────────────────────────────────────────────────────────────


def _cmd_status(_args) -> int:
    server = _server()
    import singleton

    port = singleton._find_running_server()
    root = server._default_session_root()
    print(f"backend     : {'running on port ' + str(port) if port else 'not running'}")
    print(f"version     : {singleton._server_version()}")
    print(f"session root: {root}  (exists: {root.exists()})")
    print(
        f"clone cap   : {_human(server._clone_storage_cap_bytes())}  [STEALTH_MCP_CLONE_STORAGE_CAP_GB]"
    )
    print(
        f"session cap : {_human(server._session_storage_cap_bytes())}  [STEALTH_MCP_SESSION_STORAGE_CAP_GB]"
    )
    return 0


def _cmd_profiles(_args) -> int:
    server = _server()
    rows = _collect_profiles(server)
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
    server = _server()
    clone_root = server._clone_root_dir()
    clone_cap = _gb_to_bytes(args.clone_cap_gb, server._clone_storage_cap_bytes())
    session_cap = _gb_to_bytes(args.session_cap_gb, server._session_storage_cap_bytes())

    # Same selectors the live sweep uses — dry-run and apply can't disagree.
    to_delete = server._idle_autoclones_over_cap(clone_root, clone_cap)
    to_trim = server._named_profiles_over_session_cap(clone_root, session_cap)
    delete_bytes = sum(server._dir_size_bytes(p) for p in to_delete)
    trim_bytes = sum(server._regenerable_size(p) for p in to_trim)

    print(f"clone root  : {clone_root}")
    print(f"caps        : clone {_human(clone_cap)} | session {_human(session_cap)}")
    if not to_delete and not to_trim:
        print("nothing to reclaim - storage is within caps.")
        return 0

    if to_delete:
        print(
            f"\ndelete {len(to_delete)} idle auto-clone(s) - frees {_human(delete_bytes)}:"
        )
        for path in to_delete:
            print(
                f"   - {path.name[:50]:50s} {_human(server._dir_size_bytes(path)):>10s}"
            )
    if to_trim:
        print(
            f"\ntrim {len(to_trim)} idle named profile(s) - frees ~{_human(trim_bytes)} (logins kept):"
        )
        for path in to_trim:
            print(
                f"   - {path.name[:50]:50s} ~{_human(server._regenerable_size(path)):>10s}"
            )
    print(f"\ntotal reclaimable: ~{_human(delete_bytes + trim_bytes)}")

    if not args.apply:
        print("\n(dry run - nothing deleted. Re-run with --apply to reclaim.)")
        return 0

    removed = server._enforce_clone_storage_cap_in(clone_root, clone_cap, "cli")
    freed = server._enforce_named_profile_trim_in(clone_root, session_cap, "cli")
    print(
        f"\napplied: deleted {removed} auto-clone(s); trimmed {_human(freed)} from named profiles."
    )
    return 0


def _cmd_doctor(_args) -> int:
    import platform

    server = _server()
    import singleton

    ok = True
    print(f"python      : {platform.python_version()}")
    print(f"platform    : {platform.platform()}")
    root = server._default_session_root()
    print(f"session root: {root}  (exists: {root.exists()})")
    port = singleton._find_running_server()
    print(f"backend     : {'running on port ' + str(port) if port else 'not running'}")

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
    "serve": _cmd_serve,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stealth-chrome-devtools",
        description="Ops CLI for the stealth Chrome DevTools MCP server "
        "(inspect, reclaim disk, start). For browser automation, use the MCP server.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show backend state, session root, and storage caps")
    sub.add_parser("profiles", help="list profiles with size, role, and in-use flag")

    clean = sub.add_parser(
        "cleanup",
        help="reclaim disk: delete idle auto-clones over the clone cap and trim "
        "idle named profiles over the session cap (dry run unless --apply)",
    )
    clean.add_argument(
        "--apply", action="store_true", help="actually reclaim (default: dry run)"
    )
    clean.add_argument(
        "--session-cap-gb",
        type=float,
        default=None,
        dest="session_cap_gb",
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
        "doctor", help="check Python, platform, session root, backend, and Chrome"
    )

    serve = sub.add_parser(
        "serve", help="start the MCP server (stdio by default, or --http)"
    )
    serve.add_argument(
        "--http", action="store_true", help="serve over HTTP instead of stdio"
    )
    serve.add_argument(
        "--port", type=int, default=19222, help="HTTP port (default 19222)"
    )
    serve.add_argument(
        "--host", default="127.0.0.1", help="HTTP host (default 127.0.0.1)"
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
