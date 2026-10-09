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

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import (
    google_rotation_guard,
    profile_seed,
    session_lease,
)
from stealth_chrome_devtools_mcp.embedded.cookie_handoff import VIA_CDP
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from stealth_chrome_devtools_mcp.embedded.browser_reattach import Adoptable
    from stealth_chrome_devtools_mcp.embedded.profile_source import SeedSource

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


def lock_key(user_data_dir: str | Path | None) -> str:
    """THE key a session's lease is held under, derived from the DIRECTORY it
    means and never from the spelling: the shared profile is ``default`` (not
    ``master``), a session under the session root is its casefolded name
    (``Fleet`` and ``fleet`` are one directory on Windows and macOS), and a
    directory outside the root is its whole normalised path, so an absolute dir
    that merely ends in ``fleet`` cannot alias the session."""
    from stealth_chrome_devtools_mcp.embedded import clone_storage

    if not user_data_dir:
        return profile_seed.DEFAULT_SESSION
    path = Path(str(user_data_dir))
    if profile_seed.same_dir(path, clone_storage.master_profile_dir()):
        return profile_seed.DEFAULT_SESSION
    if profile_seed.same_dir(path.parent, clone_storage.clone_root_dir()):
        return path.name.casefold()
    return os.path.normcase(path.resolve())


def session_key(session: str) -> str:
    """:func:`lock_key` for a session NAME, as ``spawn_browser`` takes it."""
    return lock_key(_dir_of(session))


def _dir_of(session: str) -> str | None:
    from stealth_chrome_devtools_mcp.embedded import clone_storage

    return clone_storage.require_allowed_user_data_dir(None, session)


def reuse_answer(running_here: bool, user_data_dir: str | None) -> dict[str, object]:
    """What ``spawn_browser`` adds when the session asked for is already open in
    THIS backend: it is handed back, not walked to ``<name>-N`` and not
    someone else's, and the answer says so plus who holds the lock."""
    if not running_here or not user_data_dir:
        return {}
    return {
        "already_running": True,
        "session_lock": session_lease.status(lock_key(user_data_dir)),
    }


def headless_mismatch(requested: bool, actual: bool | None) -> dict[str, object]:
    """What a reused or re-attached browser adds when its headless state is not
    the one asked for. ``headless=False`` is the tool default, so a bare call
    cannot be told from an explicit one: the answer says so either way, because
    an invisible browser handed to a caller who wanted a window is the outcome
    nobody can see for themselves. Launch flags cannot be changed on a running
    browser; the remedy is to close it and spawn again."""
    if actual is None or bool(requested) == bool(actual):
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


def clone_seed(
    source_for_copy: Callable[[str, Callable[[Path], bool]], SeedSource],
    driven: Callable[[Path], bool],
) -> tuple[SeedSource | None, str | None]:
    """The configured seed session as an UNNAMED clone's source, as
    ``(source, None)``; ``(None, None)`` when nothing is configured; and
    ``(None, warning)`` when it is configured but cannot be used right now
    (held by a browser this backend does not drive, or gone).

    An unnamed spawn asked for no particular profile, so a source that cannot
    be copied must not fail it: the caller falls back to the snapshot and the
    answer carries *warning*, which names why. The refusal stays BY NAME for an
    explicit ``seed_from`` and for a NEW named session created under the
    setting -- those callers asked for that profile specifically."""
    try:
        named = default_seed(None, None)
        return (source_for_copy(named, driven) if named else None), None
    except ToolError as exc:
        return None, (
            f"{SEED_SETTING} could not be used, so this clone was copied from "
            f"the snapshot instead and does NOT carry the {FLEET_SESSION} "
            f"session's logins: {exc}"
        )


def seed_warning(*selections: dict[str, object]) -> dict[str, object]:
    """The ``seed_warning`` fields of *selections* (the profile selection, the
    cookie hand-off's record) as one top-level answer field, or ``{}``."""
    found = [str(sel["seed_warning"]) for sel in selections if sel.get("seed_warning")]
    return {"seed_warning": " ".join(found)} if found else {}


#: The clone-marker key that remembers a named session's jar came from a LIVE
#: browser (its value is ``cookie_handoff.VIA_CDP``), so a relaunch re-arms the guard.
SEEDED_VIA_KEY = "seeded_via"


def guards_rotation(selection: dict[str, object]) -> bool:
    """True when the browser about to launch must have Google's cookie-rotation
    requests failed (F-939/F-952) although it is not a disposable clone.

    A clone always is (``auto_clone``). A NAMED session is too when its jar was
    handed over from a LIVE browser: it then holds a second copy of that
    browser's Google chain, and if both rotate Google reads it as theft and
    signs the source out. That is known at creation (the resolver marked a live
    source) and, for every later launch, from the marker
    :func:`record_live_seed` wrote. A session the owner later signs in to Google
    afresh is not recognised as different: it stays guarded (open item)."""
    from stealth_chrome_devtools_mcp.embedded import clone_storage

    if selection.get("profile_role") != "explicit":
        return False
    if selection.get(clone_storage.LIVE_SEED_KEY):
        return True
    directory = selection.get("user_data_dir")
    return bool(directory) and (
        profile_seed.read_marker(Path(str(directory))).get(SEEDED_VIA_KEY) is not None
    )


async def rearm_rotation_guard(browser: object, user_data_dir: str, role: str) -> None:
    """Re-arm the F-939 guard on a RE-ATTACHED named session whose marker says
    its jar came from a live hand-off: the restart dropped the guard with the
    process that held it. The ONE rule is :func:`guards_rotation`'s."""
    selection = {"profile_role": role, "user_data_dir": user_data_dir}
    if guards_rotation(selection) and google_rotation_guard.enabled():
        await google_rotation_guard.arm(browser)  # type: ignore[arg-type]


def record_live_seed(
    selection: dict[str, object], cookie_seed: dict[str, object]
) -> None:
    """After a hand-off that SUCCEEDED into a named session, remember in its
    marker that its jar came from a live browser (see :func:`guards_rotation`).
    Never raises: the session works either way, it would only relaunch unguarded."""
    if selection.get("profile_role") != "explicit":
        return
    if cookie_seed.get("seeded_via") != VIA_CDP:
        return
    directory = Path(str(selection["user_data_dir"]))
    try:
        marker = profile_seed.read_marker(directory)
        marker[SEEDED_VIA_KEY] = VIA_CDP
        (directory / profile_seed.MARKER_NAME).write_text(
            json.dumps(marker, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        debug_logger.log_warning(
            "fleet_session", "record_live_seed", f"could not stamp the marker: {exc!r}"
        )
