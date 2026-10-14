"""The string a spawn's clone directory is NAMED from: what the CLIENT says its
workspace is (F-790's bounded ``roots/list`` round trip, then the local fallback
chain).

Moved VERBATIM out of ``clone_storage`` by F-967, which needed that file's last
lines of budget (it stood at exactly 1000/1000, a cap that ratchets DOWN only).
``clone_storage._client_session_seed`` stays as the one-line binding the suite and
``_clone_profile_dir_for_session`` call, so a patch on it still steers the name.

A leaf: ``debug_logger`` and ``settings``; ``fastmcp`` is imported at the call, as
it always was, so this module never needs a request context to load.
"""

import asyncio
import os
import urllib.parse
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.settings import get_settings


def _root_to_path(root: object) -> str | None:
    value = getattr(root, "uri", None) or root
    value = str(value)
    if value.startswith("file://"):
        parsed = urllib.parse.urlparse(value)
        return urllib.parse.unquote(
            parsed.path.lstrip("/") if os.name == "nt" else parsed.path
        )
    return value or None


async def session_seed() -> str:
    configured = (
        get_settings().stealth_chrome_profile_key or get_settings().browser_profile_key
    )
    if configured:
        return configured

    roots = []
    try:
        from fastmcp.server.dependencies import get_context

        # F-790: bound this OPTIONAL server->client round trip (see settings.py).
        bound = get_settings().client_roots_timeout_seconds
        listed = await asyncio.wait_for(get_context().list_roots(), bound)
        roots = [path for path in (_root_to_path(r) for r in listed) if path]
    except Exception as e:  # noqa: BLE001  PERMANENT(an optional client round trip must degrade to the local chain, never fail a spawn)
        message = str(e) or f"{type(e).__name__} awaiting roots/list"
        debug_logger.log_warning("server", "_client_session_seed", message)
        roots = []

    if roots:
        return "|".join(sorted(roots))

    return (
        get_settings().codex_workspace
        or get_settings().claude_project_dir
        or get_settings().pwd
        or str(Path.cwd())
    )
