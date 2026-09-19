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

#: What :func:`capture` hands back: the client's ``(pid, create_time)``, or
#: ``None`` for "could not be asked", which :func:`present` reads as present.
ClientToken = tuple[int, float] | None

#: Launchers that RUN a command and wait for it. Named, not guessed: each one
#: lives exactly as long as the proxy under it, so naming one is naming a
#: process that cannot be seen to go away first.
_LAUNCHER_NAMES = frozenset({"uv", "uv.exe", "uvx", "uvx.exe"})

#: Our own console scripts (``pyproject.toml`` ``[project.scripts]``). On Windows
#: each is a redirector that re-execs the real interpreter — F-866's paragraph,
#: read from the other side.
_OUR_SCRIPTS = frozenset(
    {
        "stealth-chrome-devtools-mcp",
        "stealth-chrome-devtools-mcp.exe",
        "stealth-chrome-devtools",
        "stealth-chrome-devtools.exe",
    }
)

#: An import name is a better witness than an exe name for "this is a python of
#: ours": the interpreter can be called anything.
_PACKAGE_MARK = "stealth_chrome_devtools_mcp"

#: How many ancestors the walk may examine before giving up. Four is the longest
#: chain measured (trampoline, ``uv``, shell, client); eight is twice that and
#: still terminates promptly on a tree with a cycle or a very long shim stack.
WALK_LIMIT = 8


def _is_shim(proc: psutil.Process, ours: list[str]) -> bool:
    """True when ``proc`` is a launcher or an interpreter shim rather than the
    client — i.e. a process that exists only because we do.

    Four clauses, each a MEASURED shape and none of them a heuristic about what
    a client looks like:

    1. a named launcher (``uv``/``uvx``), which waits on its child by design;
    2. one of our own console-script redirectors;
    3. a command line BYTE-IDENTICAL to ours — the hallmark of the Windows venv
       ``python.exe`` trampoline, which spawns a second process with the same
       argv to do the work (measured: pids 148200 and 52904 differed in nothing
       else);
    4. any argv token carrying our package name — a python of ours spelled some
       other way.
    """
    name = (proc.name() or "").lower()
    if name in _LAUNCHER_NAMES or name in _OUR_SCRIPTS:
        return True
    cmdline = proc.cmdline()
    if cmdline == ours:
        return True
    return any(
        _PACKAGE_MARK in part
        or part.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower() in _OUR_SCRIPTS
        for part in cmdline
    )


def capture() -> ClientToken:
    """Identify the process that started this proxy, NOW.

    Called once, at proxy start, and never again: the answer must describe the
    client we were actually launched by. Asked later it would describe whatever
    we were reparented to, which on POSIX is init and is immortal — i.e. the
    check would silently stop working at exactly the moment it was needed.

    **The direct parent is usually not the client** (F-889 review N2). Measured
    on this machine, the chain above a proxy is ``python.exe`` (us) →
    ``python.exe`` (the venv trampoline, same argv) → ``uv.exe`` (waiting) →
    the shell → ``claude.exe``; on POSIX the trampoline is absent and ``uvx``
    still waits. Every one of those shims outlives us by construction, so a
    token naming one can never become "gone" while a session is stranded — which
    is precisely how the exit came to miss the 116 stale proxies it exists for.
    So the walk climbs past them to the first ancestor that is nobody's shim,
    and stops THERE: climbing further would name a terminal that outlives the
    client, which is the same miss by a longer route.

    **Walking too far is the safe direction and walking short is not.** Every
    ancestor lives at least as long as the one below it, so an over-eager skip
    delays the exit (the failure mode this module already tolerates) while a
    misidentified shim can only ever make it later, never earlier. That is why
    the ambiguous cases — a walk that does not settle inside :data:`WALK_LIMIT`,
    an ancestor psutil will not describe, no parent at all — all answer ``None``:
    presence unknown, so this proxy never exits on this ground.
    """
    try:
        me = psutil.Process(os.getpid())
        ours = me.cmdline()
        proc = me.parent()
        for _ in range(WALK_LIMIT):
            if proc is None:
                return None
            if not _is_shim(proc, ours):
                return (proc.pid, proc.create_time())
            proc = proc.parent()
    except (psutil.Error, OSError):
        _logger.debug("could not identify the client process", exc_info=True)
        return None
    _logger.debug(
        "gave up identifying the client after %d ancestors; this proxy will not "
        "exit on the client's account",
        WALK_LIMIT,
    )
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
