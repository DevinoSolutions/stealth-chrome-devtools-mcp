"""THE one classifier of the two shared sessions' browsers (F-964, F-966), and the one
witness of who holds a profile directory.

Moved VERBATIM out of :mod:`browser_health` (F-1007), which stood at exactly its
1000-line budget: the relaunch asks whether a browser is one of ``default`` /
``fleet`` (and builds its options, :func:`relaunch_options`), ``close_instance``
asks which, and neither is about probing a browser.
"Shared" here means those two sessions and nothing else (CLAUDE.md glossary).

A leaf: it imports neither ``browser_health`` nor ``server``.
"""

from collections.abc import Mapping
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import (
    fleet_session,
    profile_lock,
    profile_seed,
)
from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance, BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


def shared_session_name(user_data_dir: str | None, *, auto_clone: bool) -> str | None:
    """``default`` or ``fleet`` when a browser on *user_data_dir* is the browser
    of one of the two shared sessions, else ``None``: a directory the caller
    named (never an auto-clone) that ``fleet_session.lock_key`` -- the one mapping
    from a directory to a session -- keys as one of the two.

    THE classifier (F-964, and F-966's ``close_instance`` guard): the relaunch asks
    whether it is one, the close asks which. Plain values, because the two read
    them from different records of the same browser -- the options it launched
    with, and the profile selection its spawn diagnostics carry (which survives a
    re-attach, F-888) -- and the close must not look an instance up in a way that
    discards a dead one."""
    if not user_data_dir or auto_clone:
        return None
    key = fleet_session.lock_key(user_data_dir)
    if key in (profile_seed.DEFAULT_SESSION, fleet_session.FLEET_SESSION):
        return key
    return None


def require_shared_session_named(
    instance_id: str, profile_selection: Mapping[str, object], session: str | None
) -> None:
    """Refuse to close the ``default`` or ``fleet`` browser unless the caller named
    that session (F-966).

    Those two are browsers other chats work in, and a chat that was handed one
    by mistake (2.1.27 gave ``spawn_browser()`` the ``default`` browser) closed it
    for all of them. Naming the session is the confirmation: it says the caller
    knows what it is closing. The selection is the one the spawn recorded, which
    ``close_instance`` already reads and which does not discard a dead browser the
    way an instance lookup does. ``session`` is ignored for every other instance:
    nothing there needs confirming, and rejecting it would break a caller that
    always sends one."""
    directory = profile_selection.get("user_data_dir")
    shared = shared_session_name(
        directory if isinstance(directory, str) else None,
        auto_clone=profile_selection.get("profile_role") == "clone",
    )
    if shared is None or (session and fleet_session.session_key(session) == shared):
        return
    raise ToolError(
        f"Instance {instance_id} is the {shared!r} session's browser, and other "
        "chats depend on it, so it was NOT closed. If closing it is what you "
        "mean, say so by naming the session: "
        f'close_instance(instance_id="{instance_id}", session="{shared}"). '
        "If you only wanted a browser of your own, spawn_browser() with no "
        "session gives you a disposable one to close freely."
    )


def is_shared_session(options: BrowserOptions) -> bool:
    """The ``default`` or ``fleet`` session's browser (F-964)."""
    return (
        shared_session_name(options.user_data_dir, auto_clone=options.auto_clone)
        is not None
    )


def holder_of(user_data_dir: str) -> str | None:
    """Who holds *user_data_dir* right now, as a short phrase, or ``None``: the
    one witness ``spawn_browser(session=...)`` asks before it adopts or spawns."""
    hold = profile_lock.profile_hold(
        Path(user_data_dir),
        getattr(process_cleanup, "_get_browser_pids_for_profile", None),
    )
    if hold is None:
        return None
    return f"pid {hold.pid}" if isinstance(hold.pid, int) else "an unaddressable holder"


def relaunch_options(
    options: BrowserOptions, instance: BrowserInstance, previous: dict[str, object]
) -> BrowserOptions:
    """What a relaunch of *instance* spawns with: the same directory, headless and
    viewport it had, and rotation-guarded when the spawn path's own rule says so."""
    selection = previous.get("profile_selection")
    role = selection.get("profile_role") if isinstance(selection, dict) else None
    # The ONE rule for "this relaunch must be guarded" is the spawn path's.
    guard = options.guard_rotation or fleet_session.guards_rotation(
        {
            "profile_role": role,
            "user_data_dir": options.user_data_dir,
        }
    )
    return options.model_copy(
        update={
            "headless": instance.headless,
            "viewport_width": instance.viewport.get("width", options.viewport_width),
            "viewport_height": instance.viewport.get("height", options.viewport_height),
            "guard_rotation": guard,
        }
    )
