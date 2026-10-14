"""THE one home for running profile selection's BLOCKING work off the event loop
(F-967): a whole-profile copy, the seed freshen, and the process-table walk that
asks who holds a directory.

**The defect.** ``clone_storage.resolve_profile_selection`` is ``async``, and
every blocking call in its clone branch ran on the loop. A clone spawn copies a
~100 MB / 666-file seed (``profile_copy.replace_tree``: two passes and a pause),
so each one froze the whole backend for the length of the copy and N concurrent
spawns froze it N times over. On 2026-10-14 ten chats did that at once and
backend 38760 served nothing for ~106 s: no tool call, no probe, no heartbeat. Its
proxies' watchdogs condemned it, and a proxy's heal then terminated it
(``backend_eviction``'s F-967 paragraph) while it owned every chat's browser. The
process-table walks are the same defect at a smaller size: with 80-200
``chrome.exe`` a walk is one ``cmdline()`` read per process, on the loop, on every
unnamed spawn.

**What this owns** is the one way to hand such work to a worker thread, because
the work has three properties a bare ``await asyncio.to_thread(...)`` does not
give, and each is a way to lose a profile or a login:

* **A thread cannot be interrupted, so a cancelled spawn must outwait it.**
  ``await to_thread(copy)`` that is cancelled returns to its caller at once while
  the copy runs on and publishes a complete, marked clone that no spawn owns —
  and that ``_protect_clone_dir`` shields from the storage sweep for the life of
  the process. So a cancellation (or a failure) here waits the worker out and
  only then runs the caller's ``discard``, which removes what the worker built
  and releases what it reserved. ``replace_tree`` publishes in one rename, so the
  worker leaves either nothing or a whole tree and ``discard`` never meets a
  half-copy.
* **Copies are bounded** (:data:`MAX_CONCURRENT_COPIES`). Off the loop is not
  unbounded: ten spawns must not put ten 100 MB copies on one disk, nor take
  ten of the default executor's threads from ``close_instance``'s teardown, which
  shares that pool.
* **The context travels.** ``asyncio.to_thread`` copies the caller's
  ``contextvars``, which is what keeps a log line written from the worker joined
  to its tool call. It is a COPY: nothing the worker does to a context variable is
  seen by the caller, which is why ``directory_gate`` — whose reentrant holds are
  per-context — is never handed to a worker. The gate is taken and released by
  ``spawn_browser`` on the loop around this whole call.

A leaf: asyncio and nothing of ours. The work and what to discard arrive as
callables, so this module never learns what a profile or a reservation is.
"""

import asyncio
import weakref
from collections.abc import Awaitable, Callable
from typing import TypeVar

_T = TypeVar("_T")

#: How many profile operations may be on worker threads at once. Three keeps a
#: ten-spawn herd moving (the copies overlap on a disk that can take it) without
#: letting it become ten simultaneous 100 MB copies. Read when a loop first asks,
#: so a test can lower it before that.
MAX_CONCURRENT_COPIES = 3

# One semaphore per loop: an ``asyncio.Semaphore`` binds to the loop that first
# waits on it, and the suite (and ``asyncio.run`` callers) run many loops.
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(MAX_CONCURRENT_COPIES)
    return slot


async def _wait_out(awaitable: Awaitable[object]) -> bool:
    """Wait for *awaitable* to finish however many times this task is cancelled
    meanwhile, discard its outcome, and say whether a cancellation arrived.

    Cleanup that a second cancellation could abandon half-done is the failure
    this module exists to prevent, so it is not cancellable."""
    future = asyncio.ensure_future(awaitable)
    interrupted = False
    while not future.done():
        try:
            await asyncio.wait({future})
        except asyncio.CancelledError:
            interrupted = True
    if not future.cancelled():
        future.exception()  # retrieved: the caller is already being told something
    return interrupted


async def _in_thread(fn: Callable[..., _T], args: tuple[object, ...]) -> _T:
    worker = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(worker)
    except BaseException:
        # The thread cannot be stopped, so wait for it: whatever it built exists
        # by the time we return, which is what lets ``discard`` account for it.
        if await _wait_out(worker):
            raise asyncio.CancelledError from None
        raise


async def run(
    fn: Callable[..., _T],
    *args: object,
    discard: Callable[[], object] | None = None,
) -> _T:
    """Run ``fn(*args)`` on a worker thread, within the copy bound, and return
    its result — or raise what it raised.

    ``discard`` runs (also on a worker, uncancellably) when the result is NOT
    handed back: the call was cancelled or ``fn`` raised. It is how a caller
    undoes what ``fn`` did, and it runs only after ``fn`` has finished, so it sees
    everything ``fn`` did. It is not run on success: from then on the caller owns
    the result.
    """
    try:
        async with _slot():
            return await _in_thread(fn, args)
    except BaseException:
        if discard is not None and await _wait_out(asyncio.to_thread(discard)):
            raise asyncio.CancelledError from None
        raise
