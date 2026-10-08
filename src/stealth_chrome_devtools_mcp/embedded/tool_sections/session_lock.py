"""The ``session-lock`` tools. See ``tool_sections/__init__.py`` for the contract.

F-952: an ADVISORY lease on a named session, so several agents sharing one
signed-in browser can take turns. The state and the argument for "advisory" are
in ``embedded/session_lease.py``; this module is only the tool surface.
"""

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.fleet_session import FLEET_SESSION

SECTION = "session-lock"

_NAME_HINT = "Pass the session NAME, as spawn_browser(session=...) takes it."


def _name(session: str) -> str:
    return rt.profile_seed.require_name("session", session, path_hint=_NAME_HINT)


async def acquire_session_lock(
    owner: str,
    session: str = FLEET_SESSION,
    lease_seconds: int = rt.session_lease.DEFAULT_LEASE_SECONDS,
    wait_seconds: int = 0,
) -> dict[str, object]:
    """
    Take the advisory lock on a shared session so other agents know it is in use.

    The lock is ADVISORY: it does not stop another agent's tool calls. It is the
    shared record of whose turn it is, and spawn_browser reports it beside a
    session that is already running. A lease expires on its own, so an agent that
    died holding it cannot lock the session forever. The same owner acquiring
    again renews the lease.

    Args:
        owner (str): A label for who is taking the lock (any non-empty string).
        session (str): The session NAME to lock (default: "fleet").
        lease_seconds (int): How long the lease lasts, 1-3600 (default: 300).
        wait_seconds (int): How long to wait for a current holder, 0-120
            (default: 0, fail at once).

    Returns:
        Dict[str, Any]: acquired, holder, acquired_at, expires_at and
        expires_in_seconds. Raises when the session is held by someone else,
        naming the holder and when the lease expires.
    """
    return await rt.session_lease.acquire(
        _name(session), owner, lease_seconds, wait_seconds
    )


async def release_session_lock(
    owner: str, session: str = FLEET_SESSION
) -> dict[str, object]:
    """
    Release the advisory lock on a shared session. Only the holder can.

    Args:
        owner (str): The label the lock was acquired under.
        session (str): The session NAME (default: "fleet").

    Returns:
        Dict[str, Any]: released. Raises when the session is not locked or is
        locked by a different owner.
    """
    return rt.session_lease.release(_name(session), owner)


async def get_session_lock_status(session: str = FLEET_SESSION) -> dict[str, object]:
    """
    Read who holds the advisory lock on a shared session, if anyone.

    Args:
        session (str): The session NAME (default: "fleet").

    Returns:
        Dict[str, Any]: locked, and when locked: holder, acquired_at, expires_at
        and expires_in_seconds.
    """
    return rt.session_lease.status(_name(session))


TOOLS = (
    acquire_session_lock,
    release_session_lock,
    get_session_lock_status,
)
