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

Waiters are served in the order they asked (F-956). Each session has a FIFO
queue; a bounded wait joins it, and only the head may take the lease once it is
free or expired. A newcomer cannot jump the queue, not even with
``wait_seconds=0``. The head is woken the moment the holder releases (or the
head ahead of it gives up), and by a timer at the lease's expiry when nobody
releases, so there is no polling. A waiter leaves the queue on success, on its
deadline and on cancellation, so a caller that disconnected cannot block the
line. A wait is capped (``MAX_WAIT_SECONDS``) because a client's own per-call
timeout can end the call before the wait does.

The key is the canonical one from ``fleet_session.lock_key`` (the session's
resolved directory, not the spelling), so two spellings of one profile share a
lease.

State is in memory and per backend. A lease is short (seconds to an hour), the
session it guards survives a restart but the lease does not, and a restart
clearing every lease is the safe direction. Time comes from ``monotonic`` for
the expiry (a wall-clock step cannot extend or cut a lease) and from ``time``
for the ``acquired_at`` / ``expires_at`` the caller reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

MIN_LEASE_SECONDS = 1
MAX_LEASE_SECONDS = 3600
DEFAULT_LEASE_SECONDS = 300
#: Below a typical MCP client's per-call timeout, which a longer wait would outlive.
MAX_WAIT_SECONDS = 60
MAX_OWNER_LENGTH = 128
MAX_KEY_LENGTH = 255


@dataclass
class _Lease:
    owner: str
    acquired_at: float
    expires_at: float
    expires_mono: float


@dataclass
class _Waiter:
    owner: str
    enqueued_mono: float
    #: Set to make the waiter look again: it became head, or state was reset.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    dropped: bool = False


def _mono() -> float:
    return time.monotonic()


def _wall() -> float:
    return time.time()


# session name -> its lease. An entry may be expired; `_live` is the one reader.
_leases: dict[str, _Lease] = {}


# session name -> its waiters, oldest first. All tool calls run on one loop, so
# this needs no lock; an entry exists only while somebody is waiting.
_queues: dict[str, list[_Waiter]] = {}


def _live(session: str) -> _Lease | None:
    lease = _leases.get(session)
    if lease is not None and lease.expires_mono <= _mono():
        del _leases[session]
        return None
    return lease


def _waiting(session: str) -> list[dict[str, object]]:
    now = _mono()
    return [
        {
            "owner": w.owner,
            "position": place,
            "waiting_seconds": round(max(now - w.enqueued_mono, 0), 1),
        }
        for place, w in enumerate(_queues.get(session, ()), start=1)
    ]


def _shape(session: str, lease: _Lease | None) -> dict[str, object]:
    queue: dict[str, object] = {
        "queue_length": len(_queues.get(session, ())),
        "waiting": _waiting(session),
    }
    if lease is None:
        return {"session": session, "locked": False, **queue}
    return {
        "session": session,
        "locked": True,
        "holder": lease.owner,
        "acquired_at": lease.acquired_at,
        "expires_at": lease.expires_at,
        "expires_in_seconds": round(max(lease.expires_mono - _mono(), 0), 1),
        **queue,
    }


def _require_owner(owner: str) -> str:
    name = owner.strip() if isinstance(owner, str) else ""
    if not name:
        raise ToolError("owner must name who is taking the lock (a non-empty label).")
    if len(name) > MAX_OWNER_LENGTH:
        raise ToolError(f"owner is at most {MAX_OWNER_LENGTH} characters.")
    return name


def _position_of(session: str, owner: str) -> int | None:
    for place, w in enumerate(_queues.get(session, ()), start=1):
        if w.owner == owner:
            return place
    return None


def status(session: str, owner: str | None = None) -> dict[str, object]:
    """The lease on *session* as it stands now: free, or its holder and times,
    plus who is waiting in order. With *owner*, ``your_position`` is 0 when that
    owner holds the lease, 1..N for its place in the queue, None for neither."""
    held = _live(session)
    shaped = _shape(session, held)
    if owner is not None:
        name = _require_owner(owner)
        if held is not None and held.owner == name:
            shaped["your_position"] = 0
        else:
            shaped["your_position"] = _position_of(session, name)
    return shaped


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
    if len(session) > MAX_KEY_LENGTH:
        raise ToolError(f"session is at most {MAX_KEY_LENGTH} characters.")
    for key in list(_leases):
        _live(key)  # sweeps an expired lease on a key nobody asks about again
    if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ToolError(
            f"lease_seconds must be {MIN_LEASE_SECONDS}-{MAX_LEASE_SECONDS}, "
            f"got {lease_seconds}."
        )
    if not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
        raise ToolError(
            f"wait_seconds must be 0-{MAX_WAIT_SECONDS}, got {wait_seconds}."
        )
    held = _live(session)
    if held is not None and held.owner == name:
        return {**_take(session, name, lease_seconds), "acquired": True}
    queue = _queues.get(session, [])
    already = _position_of(session, name)
    if already is not None:
        raise ToolError(
            f"{name!r} is already waiting for session {session!r} at position "
            f"{already}; one owner holds one place in the line."
        )
    if not queue and held is None:
        return {**_take(session, name, lease_seconds), "acquired": True}
    if wait_seconds == 0:
        raise ToolError(_refusal(session, held, len(queue) + 1))
    return await _wait_in_line(session, name, lease_seconds, wait_seconds)


def _refusal(session: str, held: _Lease | None, position: int) -> str:
    ahead = f"{position - 1} waiting ahead of you" if position > 1 else "nobody ahead"
    if held is None:
        return (
            f"Session {session!r} is free but {position - 1} other(s) are queued "
            f"for it; you would be at position {position} ({ahead})."
        )
    return (
        f"Session {session!r} is locked by {held.owner!r} until "
        f"{held.expires_at:.0f} (epoch seconds; "
        f"{round(max(held.expires_mono - _mono(), 0), 1)}s from now); "
        f"you would be at position {position} ({ahead})."
    )


def _wake_head(session: str) -> None:
    queue = _queues.get(session)
    if queue:
        queue[0].wake.set()


def _may_take(queue: list[_Waiter], me: _Waiter, held: _Lease | None) -> bool:
    """Only the head of the line may take a lease that is free or expired."""
    return queue[0] is me and held is None


async def _wait_in_line(
    session: str, name: str, lease_seconds: int, wait_seconds: int
) -> dict[str, object]:
    me = _Waiter(name, _mono())
    queue = _queues.setdefault(session, [])
    queue.append(me)
    deadline = me.enqueued_mono + wait_seconds
    took = False
    try:
        while True:
            if me.dropped:
                raise ToolError(
                    f"Session {session!r}'s lock state was reset while "
                    f"{name!r} was waiting; ask again."
                )
            held = _live(session)
            if _may_take(queue, me, held):
                took = True
                return {**_take(session, name, lease_seconds), "acquired": True}
            remaining = deadline - _mono()
            if remaining <= 0:
                raise ToolError(_refusal(session, held, queue.index(me) + 1))
            # The head also wakes when an unreleased lease runs out; the rest
            # only when they become head or their own deadline passes.
            timeout = remaining
            if queue[0] is me and held is not None:
                timeout = min(timeout, max(held.expires_mono - _mono(), 0))
            me.wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(me.wake.wait(), timeout)
    finally:
        if me in queue:
            queue.remove(me)
        if not queue and _queues.get(session) is queue:
            del _queues[session]
        if not took:
            _wake_head(session)  # the next in line may be able to take it now


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
    _wake_head(session)  # hand it over now, not at the next timer
    return {"session": session, "locked": False, "released": True}


def reset() -> None:
    """Forget every lease and queue. Test seam: the tables are process-global.
    Waiters are woken and told, so none is left waiting on a queue that is gone."""
    _leases.clear()
    for queue in _queues.values():
        for waiter in queue:
            waiter.dropped = True
            waiter.wake.set()
    _queues.clear()
