"""THE one home for HOLDING the record's claim on a browser across an attach.

``browser_pid_registry.claim_browser`` owns the WRITE — the decide-and-stamp
inside one locked read-merge-write. What this module owns is the half that only
exists because the thing the claim guards is ``await``\\ ed: a claim is taken
before a single byte reaches Chrome, the attach that follows can fail, raise or
be CANCELLED by its enclosing budget, and on every one of those paths the record
must not be left naming this backend as the owner of a browser it does not hold.

That state is not a cosmetic leak, it is the exact failure F-888 exists to
abolish, reached from the other side: our own classification reads a LIVE owner,
startup recovery spares the entry, every other backend is refused, and nothing
heals it until this process exits.

So the lifecycle is one ``async with`` and the three things that make it correct
are here rather than spelled out at each call site:

* the claim is taken INSIDE the block, so the handler that releases it already
  exists — a claim taken before its ``try`` is a claim nothing releases;
* it is SHIELDED, because cancelling an ``await asyncio.to_thread(...)`` does not
  stop the worker thread: it still takes the record lock and still writes;
* and the TASK is kept, not just its result, so a teardown can hand back a claim
  that LANDED after the budget had already given up on it.

The two failures a claim can end in are named types, because their handling is
the opposite of every other failure's — see :class:`Refused`.

A leaf: ``browser_pid_registry`` for what a claim IS, ``tool_errors`` for the one
error convention, and nothing else. Both record writes arrive as CALLABLES, on
``backend_eviction.terminate``'s precedent, so the caller keeps the patchable
names its suite targets and this module never learns what an adoption candidate
is.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import browser_pid_registry, tool_errors

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Callable

# How long a teardown waits for a claim whose worker thread outlived the budget,
# so it can release what that thread actually wrote. One more than
# `browser_pid_registry._LOCK_TIMEOUT` (5.0): past its own deadline the write
# fails on its own and there is nothing left to release.
SETTLE_SECONDS = 6.0


class Refused(tool_errors.ToolError):
    """We did not take that browser, and it must be LEFT ALONE — never reaped.

    Its own type because the two entry points must report it differently from
    every other failure: "we could not reach it" sends a spawn to a different
    directory and is worth a line in the log, while THIS one is the rule working
    — the operator has a browser they can still get back, and what they need is
    the remedy, not a stack trace.

    **It is also the one thing standing between a lost race and a kill.** On the
    record path a failure falls back to the reap, and that is right only for a
    failure that is EVIDENCE ABOUT THE BROWSER — the attach was refused, it
    reports no tab, it is wedged. A claim another backend won is evidence about
    us, and reaping on it would kill the browser the winner has just adopted and
    drop the entry the winner has just re-stamped, which is strictly worse than
    the two-drivers race this class was introduced to end.
    """


class Undecided(Refused):
    """We could not DECIDE whether we may take it — which also means leave it.

    A subclass, not a sibling, because every caller's handling is identical and
    the whole point is that one ``except Refused`` covers both: a lost claim and
    a failure to reach a verdict are the same instruction. Raised when the claim
    itself fails — ``update_entries`` raises by design on a lock timeout or an
    ``OSError`` writing the record, and such an exception says nothing whatsoever
    about the browser. Treating it as "not reachable" converted a transient
    state-dir error into a human's logged-in Chrome being killed.
    """


@contextlib.asynccontextmanager
async def held(
    take: Callable[[], browser_pid_registry.Claimed | None],
    hand_back: Callable[[browser_pid_registry.Claimed], None],
    *,
    pid: int,
) -> AsyncIterator[browser_pid_registry.Claimed]:
    """Own the record's claim on the browser at *pid* for the body of the block.

    *take* and *hand_back* are the two RECORD writes, synchronous and run on a
    worker thread here. The block is entered only with a claim in hand; leaving
    it by ANY exception — including a cancellation from the enclosing attach
    budget — hands the claim back first. Leaving it normally keeps it, because
    the caller now owns the browser.

    Raises :class:`Refused` when a live backend of ours already owns it and
    :class:`Undecided` when the claim's own machinery failed, and those are the
    two answers a caller must not treat as evidence about the browser.
    """
    claiming: asyncio.Task | None = None
    claimed: browser_pid_registry.Claimed | None = None
    try:
        try:
            claiming = asyncio.ensure_future(asyncio.to_thread(take))
            claimed = await asyncio.shield(claiming)
        except (Refused, asyncio.CancelledError):
            raise
        except Exception as exc:
            # The claim's own machinery failed — a lock timeout, an OSError on
            # the state dir. That is a fact about the RECORD, never about the
            # browser, so it must not reach a handler whose remedy is a kill.
            raise Undecided(
                f"the record could not be written to claim the browser on pid "
                f"{pid} ({type(exc).__name__}), so it was left alone"
            ) from exc
        claimed = _decided(claimed, pid)
        yield claimed
    except BaseException:
        # What the claim ACTUALLY wrote, which is not always what we were handed:
        # a cancellation delivered while the claim's worker thread was mid-write
        # leaves `claimed` unbound and the record stamped anyway.
        landed = claimed if claimed is not None else await _landed(claiming)
        if landed is not None:
            # Shielded for the same reason the claim is — a second cancellation
            # arriving here must not leave the release half done, which is the
            # state that produces the unreachable browser.
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    asyncio.ensure_future(asyncio.to_thread(hand_back, landed))
                )
        raise


def _decided(
    claimed: browser_pid_registry.Claimed | None, pid: int
) -> browser_pid_registry.Claimed:
    """The claim, or the refusal that says a sibling backend won the race."""
    if claimed is None:
        raise Refused(
            f"a live backend of ours already owns the browser holding that "
            f"directory (pid {pid}); two backends driving one Chrome is the "
            f"defect F-886 fixed, so it was left alone. Stop that backend "
            f"first — see RUNBOOK, 'Recover a stranded login'"
        )
    return claimed


async def _landed(
    claiming: asyncio.Task | None,
) -> browser_pid_registry.Claimed | None:
    """The claim a cancelled ``to_thread`` completed anyway, or None.

    A worker thread cannot be cancelled, so when the budget expires mid-claim the
    write still happens while its result is never handed back — and a claim
    nothing releases is the one failure mode that produces an UNREACHABLE
    browser.

    So the teardown WAITS for it rather than guessing. Bounded, because this runs
    on a failure path that must not hang: the write itself is a millisecond-scale
    locked JSON round trip and ``browser_pid_registry``'s own lock deadline is
    5 s, so anything past that is a stuck holder whose write will fail on its
    own. Giving up costs one stale owner stamp, which is what we had before.
    """
    if claiming is None:
        return None
    with contextlib.suppress(Exception):
        await asyncio.wait({claiming}, timeout=SETTLE_SECONDS)
    if not claiming.done() or claiming.cancelled() or claiming.exception() is not None:
        return None
    return claiming.result()
