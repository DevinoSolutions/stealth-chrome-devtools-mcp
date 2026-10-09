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
    """The lease key for a session NAME: one key per DIRECTORY (F-952)."""
    named = rt.profile_seed.require_name("session", session, path_hint=_NAME_HINT)
    return rt.fleet_session.session_key(named)


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
    again renews the lease. Waiters are served first come, first served: with a
    wait you join a queue and only the head of it can take the lock once it is
    released or expires, and a call that does not wait (wait_seconds=0) is
    refused while others are queued, so nobody jumps the line. An owner can wait
    in a queue once; asking again while waiting raises. A wait longer than your
    client's per-call timeout is cut off by the client, not by us, and a waiter
    that disconnects leaves the queue.

    Args:
        owner (str): A label for who is taking the lock (any non-empty string).
        session (str): The session NAME to lock (default: "fleet").
        lease_seconds (int): How long the lease lasts, 1-3600 (default: 300).
        wait_seconds (int): How long to wait in line for a current holder, 0-60
            (default: 0, fail at once).

    Returns:
        Dict[str, Any]: acquired, holder, acquired_at, expires_at and
        expires_in_seconds. Raises when the session is held by someone else or
        others are queued ahead of you, naming the holder, when the lease
        expires and the position you reached.
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


async def get_session_lock_status(
    session: str = FLEET_SESSION, owner: str | None = None
) -> dict[str, object]:
    """
    Read who holds the advisory lock on a shared session, if anyone, and who is
    waiting for it, in the order they will be served.

    Args:
        session (str): The session NAME (default: "fleet").
        owner (str): Optional. Your owner label, to get your_position back.

    Returns:
        Dict[str, Any]: locked, queue_length, waiting (a list of owner,
        position from 1 and waiting_seconds, in queue order), and when locked:
        holder, acquired_at, expires_at and expires_in_seconds. With owner,
        also your_position: 0 if you hold the lock, 1..N your place in the
        queue, null if neither.
    """
    return rt.session_lease.status(_name(session), owner)


TOOLS = (
    acquire_session_lock,
    release_session_lock,
    get_session_lock_status,
)
