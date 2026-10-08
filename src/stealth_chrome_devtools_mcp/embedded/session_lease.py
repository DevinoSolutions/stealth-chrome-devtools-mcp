"""The advisory lease on a named session (F-952).

Several agents can share one signed-in browser, and nothing in the protocol says
whose turn it is. This module is the turn-taking: ``acquire`` a lease on a
session NAME under an owner label, ``release`` it, read ``status``. A lease
expires by itself, so an agent that died holding it cannot lock the session
forever.

**Advisory, on purpose.** A tool call carries no caller identity -- the owner is
a label the caller types -- so the lease cannot be ENFORCED against the other
tools without binding every one of them to an MCP session. What it gives is a
shared, truthful answer to "is somebody using this?": a refused ``acquire`` names
the holder and the expiry, and ``spawn_browser`` reports the lease beside the
running instance it hands back.

State is in memory and per backend. A lease is short (seconds to an hour), the
session it guards survives a restart but the lease does not, and a restart
clearing every lease is the safe direction. Time comes from ``monotonic`` for
the expiry (a wall-clock step cannot extend or cut a lease) and from ``time``
for the ``acquired_at`` / ``expires_at`` the caller reads.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

MIN_LEASE_SECONDS = 1
MAX_LEASE_SECONDS = 3600
DEFAULT_LEASE_SECONDS = 300
MAX_WAIT_SECONDS = 120
#: How often a bounded wait looks again. A lease is released by another tool
#: call on the same loop, so this is a poll and not a race.
POLL_SECONDS = 0.25


@dataclass
class _Lease:
    owner: str
    acquired_at: float
    expires_at: float
    expires_mono: float


def _mono() -> float:
    return time.monotonic()


def _wall() -> float:
    return time.time()


# session name -> its lease. An entry may be expired; `_live` is the one reader.
_leases: dict[str, _Lease] = {}


def _live(session: str) -> _Lease | None:
    lease = _leases.get(session)
    if lease is not None and lease.expires_mono <= _mono():
        del _leases[session]
        return None
    return lease


def _shape(session: str, lease: _Lease | None) -> dict[str, object]:
    if lease is None:
        return {"session": session, "locked": False}
    return {
        "session": session,
        "locked": True,
        "holder": lease.owner,
        "acquired_at": lease.acquired_at,
        "expires_at": lease.expires_at,
        "expires_in_seconds": round(max(lease.expires_mono - _mono(), 0), 1),
    }


def _require_owner(owner: str) -> str:
    name = owner.strip() if isinstance(owner, str) else ""
    if not name:
        raise ToolError("owner must name who is taking the lock (a non-empty label).")
    return name


def status(session: str) -> dict[str, object]:
    """The lease on *session* as it stands now: free, or its holder and times."""
    return _shape(session, _live(session))


def _take(session: str, owner: str, lease_seconds: int) -> dict[str, object]:
    now = _mono()
    wall = _wall()
    _leases[session] = _Lease(owner, wall, wall + lease_seconds, now + lease_seconds)
    return _shape(session, _leases[session])


async def acquire(
    session: str, owner: str, lease_seconds: int, wait_seconds: int
) -> dict[str, object]:
    """Take (or, for the same owner, renew) the lease, waiting at most
    *wait_seconds*; refuse naming the holder and the expiry when it is not free.

    Out-of-range numbers are REFUSED, not clamped: a caller who asked for a
    day-long lease and silently got an hour would plan around the wrong expiry.
    """
    name = _require_owner(owner)
    if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ToolError(
            f"lease_seconds must be {MIN_LEASE_SECONDS}-{MAX_LEASE_SECONDS}, "
            f"got {lease_seconds}."
        )
    if not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
        raise ToolError(
            f"wait_seconds must be 0-{MAX_WAIT_SECONDS}, got {wait_seconds}."
        )
    deadline = _mono() + wait_seconds
    while True:
        held = _live(session)
        if held is None or held.owner == name:
            return {**_take(session, name, lease_seconds), "acquired": True}
        remaining = deadline - _mono()
        if remaining <= 0:
            current = _shape(session, held)
            raise ToolError(
                f"Session {session!r} is locked by {held.owner!r} until "
                f"{held.expires_at:.0f} (epoch seconds; "
                f"{current['expires_in_seconds']}s from now)."
            )
        await asyncio.sleep(min(POLL_SECONDS, remaining))


def release(session: str, owner: str) -> dict[str, object]:
    """Give the lease back. Only its holder may: a release by anyone else, or of
    a session nobody holds, is refused, because both mean the caller's picture of
    who has the session is wrong."""
    name = _require_owner(owner)
    held = _live(session)
    if held is None:
        raise ToolError(f"Session {session!r} is not locked; nothing to release.")
    if held.owner != name:
        raise ToolError(
            f"Session {session!r} is locked by {held.owner!r}, not {name!r}; "
            f"only the holder can release it (it expires on its own)."
        )
    del _leases[session]
    return {"session": session, "locked": False, "released": True}


def reset() -> None:
    """Forget every lease. Test seam: the table is process-global."""
    _leases.clear()
