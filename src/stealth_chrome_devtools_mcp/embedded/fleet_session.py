"""The shared ``fleet`` session (F-952).

One signed-in browser that several agents and sessions work from, which must
outlive every one of them. It is NOT a new kind of profile: it is the named
session ``fleet`` (``spawn_browser(session="fleet")``), and a named session
already is persistent -- ``browser_pid_registry.on_persistent_profile`` keeps
it past ``close_instance``, past a backend restart (F-888 re-attaches it) and
away from every reap, GC and orphan sweep except ``kill-orphans --force``. A
reserved role would be a second way to say that (CLAUDE.md convention 4).

What this module adds is the three things a *shared* session needs that an
ordinary named one does not:

* **restored first** -- :func:`restore_first` orders the F-888 pass so a slow or
  wedged neighbour cannot delay it, and :func:`report_unreattached` makes a
  failed attach to it LEAVE the browser running instead of reaping it, because
  the login in it is the one thing nobody can re-create;
* **an honest answer when it is already running** -- :func:`reuse_answer`;
* **an opt-in default seed** -- :func:`default_seed`, the session a NEW clone or
  NEW named session is copied from (``STEALTH_MCP_SEED_SESSION``).

The name is ``fleet`` and not ``shared`` because ``shared`` already means the
``default`` session in this codebase (``Roots.shared``).

No import of ``clone_storage`` at module level: it imports this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import profile_seed, session_lease
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings

if TYPE_CHECKING:
    from stealth_chrome_devtools_mcp.embedded.browser_reattach import Adoptable

FLEET_SESSION = "fleet"

SEED_SETTING = "STEALTH_MCP_SEED_SESSION"


def _dir(name: str) -> Path:
    from stealth_chrome_devtools_mcp.embedded import clone_storage

    return Path(str(clone_storage.require_allowed_user_data_dir(None, name)))


def is_fleet(user_data_dir: str | Path | None) -> bool:
    """True when *user_data_dir* is the fleet session's directory."""
    return bool(user_data_dir) and profile_seed.same_dir(
        Path(str(user_data_dir)), _dir(FLEET_SESSION)
    )


def restore_first(
    adoptable: dict[str, Adoptable],
) -> list[tuple[str, Adoptable]]:
    """The F-888 pass's entries with the fleet session's first; every other
    entry keeps its recorded order."""
    items = list(adoptable.items())
    return sorted(items, key=lambda item: not is_fleet(item[1].user_data_dir))


def spare_on_failed_attach(
    instance_id: str, candidate: Adoptable, exc: BaseException
) -> bool:
    """True, after logging, when the browser that failed to re-attach is the
    fleet's: the caller then leaves it running and recorded. Every other browser
    answers False and takes the reap -- a wedged scratch browser is evidence
    about the browser, but the fleet's login is data nobody can re-create, and a
    browser that refused one attach may accept the next backend's."""
    if not is_fleet(candidate.user_data_dir):
        return False
    debug_logger.log_warning(
        "browser_reattach",
        "reattach",
        f"Could not re-attach the {FLEET_SESSION} session (instance {instance_id}, "
        f"port {candidate.port}, {type(exc).__name__}); leaving it running and "
        f"recorded rather than reaping its login.",
        error=exc,
    )
    return True


def reuse_answer(running_here: bool, user_data_dir: str | None) -> dict[str, object]:
    """What ``spawn_browser`` adds when the session asked for is already open in
    THIS backend: it is handed back, not walked to ``<name>-N`` and not
    someone else's, and the answer says so plus who holds the lock."""
    if not running_here or not user_data_dir:
        return {}
    name = Path(user_data_dir).name
    return {"already_running": True, "session_lock": session_lease.status(name)}


def headless_mismatch(requested: bool, actual: bool) -> dict[str, object]:
    """What a reused or re-attached browser adds when its headless state is not
    the one asked for. ``headless=False`` is the tool default, so a bare call
    cannot be told from an explicit one: the answer says so either way, because
    an invisible browser handed to a caller who wanted a window is the outcome
    nobody can see for themselves. Launch flags cannot be changed on a running
    browser; the remedy is to close it and spawn again."""
    if bool(requested) == bool(actual):
        return {}
    got, asked = ("headless", "headed") if actual else ("headed", "headless")
    return {
        "headless_mismatch": {
            "requested_headless": bool(requested),
            "actual_headless": bool(actual),
            "warning": f"You asked for a {asked} browser but this session's running "
            f"browser is {got}; a running browser cannot change that. Close it "
            "(close_instance) and spawn again to get the other.",
        }
    }


def default_seed(seed_from: str | None, landed: str | None) -> str | None:
    """The ``seed_from`` a copy should use: the caller's, else the configured
    ``STEALTH_MCP_SEED_SESSION``, else None (the snapshot).

    Asked only where a copy is about to be made, so a misconfigured setting
    cannot break an unnamed spawn that opens the shared profile itself. The
    configured session never seeds itself (*landed* is the directory being
    created): its first creation bootstraps from the snapshot like any other.
    """
    configured = get_settings().seed_session.strip()
    if seed_from or not configured:
        return seed_from
    name = profile_seed.require_name(
        SEED_SETTING, configured, path_hint="Name a session, not a directory."
    )
    if profile_seed.is_default_name(name):
        return None
    if landed and profile_seed.same_dir(Path(landed), _dir(name)):
        return None
    if not _dir(name).exists():
        raise ToolError(
            f"{SEED_SETTING}={name!r} names a session that does not exist yet. "
            f"Create it with spawn_browser(session={name!r}) or unset the setting."
        )
    return name
