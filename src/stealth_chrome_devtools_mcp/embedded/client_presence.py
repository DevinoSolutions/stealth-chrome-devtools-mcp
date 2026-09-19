"""THE one home for "is the client that started this stdio proxy still there"
(F-889 review M3).

F-889 (c) deleted the proxy's own exit: it no longer ends a session because the
backend under it died, because that exit IS the outage — 114 sessions saw
"Connection closed" for a backend answering in 227 ms. But "never exits on its
own" and "never exits" are different sentences, and the same outage's cleanup
found **116 stale proxy processes** on the machine. A proxy that retries forever
with nobody on the other end is the previous defect wearing the opposite sign.

The ordinary exit is unchanged and is still the client's: stdin hits EOF,
``pump_client`` returns, and ``singleton._proxy_streams`` cancels everything.
This module answers the case where that does not happen — a client killed
without its pipe ever closing, a handle inherited by something that outlives it
— and it answers it with a FACT about the outside world rather than a decision
about the backend. That distinction is the whole point: noticing nobody is
listening is not the same act as concluding a server is dead.

**The identity is the PAIR, never the pid.** A pid alone is recycled, and a
proxy that exits because an unrelated process now owns its parent's number is
this module causing the disconnect it exists to prevent. ``psutil`` reports
``create_time()``, so the pair names one process for as long as that process
lives — the same discipline ``browser_pid_registry`` stamps on every browser it
tracks (``owner_pid`` + ``owner_create_time``).

**Fail-open, uniformly.** Every uncertainty resolves to PRESENT: no parent (we
were reparented to init, or the platform will not say), psutil refusing, a
partial answer. The cost of a false "gone" is a disconnected session; the cost of
a false "present" is one idle process that its client's next EOF will collect
anyway. Those are not comparable, so the direction is not a judgement call.

A leaf: ``psutil`` and stdlib only. It never cancels anything, never exits
anything and never reads the record — it answers a question and
``singleton._proxy_streams`` owns what to do about the answer, exactly as it
owns what to do about ``pump_client`` returning.
"""

from __future__ import annotations

import logging
import os

import psutil

_logger = logging.getLogger("stealth.proxy")

#: How often :func:`await_gone` asks. A dead client costs one extra minute of an
#: idle process at worst, and asking more often buys nothing: the ordinary exit
#: is EOF, which is immediate, so this poll only ever serves the case EOF missed.
#: One ``psutil.Process`` construction per minute per proxy is not a cost worth
#: tuning, which is why it is not a knob (F-853's rule).
CHECK_SECONDS = 60.0

#: What :func:`capture` hands back: the parent's ``(pid, create_time)``, or
#: ``None`` for "could not be asked", which :func:`present` reads as present.
ClientToken = tuple[int, float] | None


def capture() -> ClientToken:
    """Identify the process that started this proxy, NOW.

    Called once, at proxy start, and never again: the answer must describe the
    client we were actually launched by. Asked later it would describe whatever
    we were reparented to, which on POSIX is init and is immortal — i.e. the
    check would silently stop working at exactly the moment it was needed.
    """
    try:
        parent = psutil.Process(os.getpid()).parent()
        if parent is None:
            return None
        return (parent.pid, parent.create_time())
    except (psutil.Error, OSError):
        _logger.debug("could not identify the client process", exc_info=True)
        return None


def present(token: ClientToken) -> bool:
    """True iff the process ``token`` names is still running.

    Both halves must match. A pid whose ``create_time`` has moved is a RECYCLED
    pid — a different process wearing the same number — and reading it as our
    client would keep a stranded proxy alive forever; reading it as our client's
    DEATH would be worse, so it is the pair or nothing.
    """
    if token is None:
        return True
    pid, created = token
    try:
        return psutil.Process(pid).create_time() == created
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError):
        # Fail-open: "I could not ask" is not "it is gone".
        _logger.debug("could not check the client process", exc_info=True)
        return True


async def await_gone(token: ClientToken, *, interval: float = CHECK_SECONDS) -> None:
    """Return once the client named by ``token`` is gone. Never returns while it
    is there, and never returns at all when there is nobody to name."""
    import anyio

    if token is None:
        await anyio.sleep_forever()
        return
    pid = token[0]
    # A poll and not an event, because the OS offers no portable notification
    # for "my parent died" that anyio can wait on: POSIX reparents to init
    # silently and Windows has no equivalent signal at all. The cadence is a
    # minute, so the cost is one psutil lookup per proxy per minute.
    while present(token):  # noqa: ASYNC110  PERMANENT(there is no event to wait on; see above)
        await anyio.sleep(interval)
    _logger.warning(
        "the client process (pid %d) that started this proxy is gone; "
        "nobody is listening, so this proxy ends (F-889 review M3)",
        pid,
    )
