"""Profile-aware MCP proxy for upstream stealth-browser-mcp."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import psutil
from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_context
from fastmcp.server.proxy import FastMCPProxy, ProxyToolManager, StatefulProxyClient
from fastmcp.tools.tool import ToolResult
from fastmcp.client.transports import StdioTransport


VOLATILE_PROFILE_NAMES = {
    "BrowserMetrics",
    "CertificateRevocation",
    "Crashpad",
    "DawnCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    "GPUCache",
    "OptimizationHints",
    "Safe Browsing",
    "ShaderCache",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
}


@dataclass(frozen=True)
class Settings:
    master_profile: Path
    sessions_dir: Path
    refresh_days: int
    upstream_root: Path
    upstream_python: Path
    upstream_server: Path
    browser_idle_timeout: str


def default_master_profile() -> Path:
    if os.name == "nt":
        return Path(r"C:\browser-sessions\master")
    return Path.home() / ".browser-sessions" / "master"


def default_sessions_dir() -> Path:
    if os.name == "nt":
        return Path(r"C:\browser-sessions\sessions")
    return Path.home() / ".browser-sessions" / "sessions"


def default_upstream_root() -> Path:
    if os.name == "nt":
        return Path(r"C:\Users\amind\stealth-browser-mcp")
    return Path.home() / "stealth-browser-mcp"


def default_upstream_python(upstream_root: Path) -> Path:
    if os.name == "nt":
        return upstream_root / "venv" / "Scripts" / "python.exe"
    return upstream_root / "venv" / "bin" / "python"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="stealth-chrome-devtools-mcp",
        description="Profile-aware stealth Chrome DevTools MCP proxy.",
    )
    parser.add_argument("--master-profile", default=os.getenv("BROWSER_MASTER_USER_DATA_DIR"))
    parser.add_argument("--sessions-dir", default=os.getenv("BROWSER_PROFILE_CLONE_ROOT"))
    parser.add_argument("--refresh-days", type=int, default=int(os.getenv("BROWSER_PROFILE_REFRESH_DAYS", "7")))
    parser.add_argument("--upstream-root", default=os.getenv("STEALTH_BROWSER_MCP_ROOT"))
    parser.add_argument("--upstream-python", default=os.getenv("STEALTH_BROWSER_MCP_PYTHON"))
    parser.add_argument("--upstream-server", default=os.getenv("STEALTH_BROWSER_MCP_SERVER"))
    parser.add_argument("--browser-idle-timeout", default=os.getenv("BROWSER_IDLE_TIMEOUT", "0"))
    return parser.parse_args(argv)


def settings_from_args(args: argparse.Namespace) -> Settings:
    upstream_root = Path(args.upstream_root) if args.upstream_root else default_upstream_root()
    upstream_python = Path(args.upstream_python) if args.upstream_python else default_upstream_python(upstream_root)
    upstream_server = Path(args.upstream_server) if args.upstream_server else upstream_root / "src" / "server.py"
    refresh_days = args.refresh_days if args.refresh_days > 0 else 7

    return Settings(
        master_profile=Path(args.master_profile) if args.master_profile else default_master_profile(),
        sessions_dir=Path(args.sessions_dir) if args.sessions_dir else default_sessions_dir(),
        refresh_days=refresh_days,
        upstream_root=upstream_root,
        upstream_python=upstream_python,
        upstream_server=upstream_server,
        browser_idle_timeout=str(args.browser_idle_timeout),
    )


def normalize_path(value: str | Path | None) -> str | None:
    if not value:
        return None
    return os.path.normcase(os.path.normpath(str(value)))


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def profile_from_arg(arg: str) -> str | None:
    if arg.startswith("--user-data-dir="):
        return normalize_path(arg.split("=", 1)[1].strip('"'))
    return None


def chrome_pids_for_profile(profile_dir: Path) -> set[int]:
    target = normalize_path(profile_dir)
    if target is None:
        return set()

    pids: set[int] = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "chrome" not in name and "chromium" not in name and "msedge" not in name:
                continue
            for arg in proc.info.get("cmdline") or []:
                if profile_from_arg(arg) == target:
                    pids.add(int(proc.info["pid"]))
                    break
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return pids


def is_initialized_profile(profile_dir: Path) -> bool:
    if not profile_dir.exists() or not profile_dir.is_dir():
        return False
    try:
        next(profile_dir.iterdir())
    except StopIteration:
        return False
    return True


def should_skip_profile_item(name: str) -> bool:
    if name in VOLATILE_PROFILE_NAMES:
        return True
    lowered = name.lower()
    return lowered.endswith((".tmp", ".lock", ".log.tmp"))


def copy_profile_best_effort(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        target_root = target / relative_root
        target_root.mkdir(parents=True, exist_ok=True)

        dirs[:] = [name for name in dirs if not should_skip_profile_item(name)]
        for filename in files:
            if should_skip_profile_item(filename):
                continue
            try:
                shutil.copy2(root_path / filename, target_root / filename)
            except (FileNotFoundError, PermissionError, OSError):
                continue


def session_id(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return "".join(f"{byte:02x}" for byte in digest[:6])


def path_from_file_uri(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    if parsed.netloc:
        return unquote(f"//{parsed.netloc}{parsed.path}")
    return unquote(parsed.path.lstrip("/") if os.name == "nt" else parsed.path)


async def session_seed_from_context() -> str:
    try:
        ctx: Context = get_context()
        roots = await ctx.list_roots()
        if roots:
            root_path = path_from_file_uri(str(roots[0].uri))
            if root_path:
                return root_path
    except Exception:
        pass

    return (
        os.getenv("BROWSER_SESSION_CWD")
        or os.getenv("CODEX_WORKSPACE")
        or os.getenv("PWD")
        or os.getcwd()
    )


def is_stale(profile_dir: Path, settings: Settings) -> bool:
    if not profile_dir.exists():
        return True
    age_seconds = (datetime.now() - datetime.fromtimestamp(profile_dir.stat().st_mtime)).total_seconds()
    return age_seconds > settings.refresh_days * 86400


async def resolve_profile_for_spawn(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    resolved = dict(arguments or {})

    explicit_user_data_dir = resolved.get("user_data_dir")
    if explicit_user_data_dir:
        explicit_path = Path(str(explicit_user_data_dir))
        if (
            is_relative_to(explicit_path, settings.sessions_dir)
            and not explicit_path.exists()
            and is_initialized_profile(settings.master_profile)
        ):
            copy_profile_best_effort(settings.master_profile, explicit_path)
        return resolved

    if not is_initialized_profile(settings.master_profile):
        raise RuntimeError(
            "Master browser profile not initialized. Spawn browser with "
            f"user_data_dir='{settings.master_profile}' so you can log in. "
            "After closing, all future sessions inherit those logins."
        )

    if not chrome_pids_for_profile(settings.master_profile):
        resolved["user_data_dir"] = str(settings.master_profile)
        return resolved

    seed = await session_seed_from_context()
    session_dir = settings.sessions_dir / session_id(seed)
    if is_stale(session_dir, settings):
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        copy_profile_best_effort(settings.master_profile, session_dir)
    resolved["user_data_dir"] = str(session_dir)
    return resolved


class SessionProfileToolManager(ProxyToolManager):
    def __init__(self, *, settings: Settings, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.settings = settings

    async def call_tool(self, key: str, arguments: dict[str, Any]) -> ToolResult:
        if key == "spawn_browser":
            arguments = await resolve_profile_for_spawn(arguments, self.settings)
        return await super().call_tool(key, arguments)


def build_proxy(settings: Settings) -> FastMCPProxy:
    upstream_env = os.environ.copy()
    upstream_env["BROWSER_IDLE_TIMEOUT"] = settings.browser_idle_timeout

    upstream_transport = StdioTransport(
        command=str(settings.upstream_python),
        args=[str(settings.upstream_server), "--transport", "stdio"],
        env=upstream_env,
        cwd=str(settings.upstream_root),
    )
    stateful_client = StatefulProxyClient(upstream_transport)

    proxy = FastMCPProxy(
        client_factory=stateful_client.new_stateful,
        name="Stealth Chrome DevTools MCP",
        instructions=(
            "Proxy for stealth-browser-mcp that injects master/session Chrome "
            "profiles into spawn_browser."
        ),
    )
    proxy._tool_manager = SessionProfileToolManager(
        client_factory=stateful_client.new_stateful,
        transformations=proxy._tool_manager.transformations,
        settings=settings,
    )
    return proxy


def validate_settings(settings: Settings) -> None:
    if not settings.upstream_python.exists():
        raise FileNotFoundError(f"Missing upstream Python: {settings.upstream_python}")
    if not settings.upstream_server.exists():
        raise FileNotFoundError(f"Missing upstream server: {settings.upstream_server}")


def main(argv: list[str] | None = None) -> None:
    settings = settings_from_args(parse_args(argv))
    try:
        validate_settings(settings)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    build_proxy(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
