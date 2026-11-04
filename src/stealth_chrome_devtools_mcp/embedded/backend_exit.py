"""THE one home for "the backend THIS process launched exited before it was ready".

(T-14185.)

After a reboot on 2026-11-04 every cold start of the shared backend died at import
time (``ImportError: MemoryStore requires py-key-value-aio[memory]``), 83 times. The
cause was in ``backend-boot.log`` and nowhere else: every stdio proxy waited out the
whole ``BACKEND_READY_TIMEOUT`` (120 s) for a process that had been dead since the
second after it was spawned, and logged only ``backend did not become ready``. What
the client saw was a call that never answered.

**One wait, one extra question, asked by both waits.** The waits are still
``backend_probe.await_ready`` and ``singleton._wait_for_server`` (the cold-start
lock's own, which holds the lock until the backend is up); what they gained is "…and
the process I spawned for this port has not exited". The process is one this module
started and nothing else: :func:`spawn` is the one spawn that records it, so a backend
ANOTHER proxy launched, or one we adopted, is not in the table and is waited on
exactly as before. A backend that is merely slow is alive, so it still gets the whole
deadline: nothing here ever times anything out.

**An exit stays recorded** until the next :func:`spawn` for the port replaces it or
:func:`ready` clears it. It is not consumed by being read: the lock-holder's wait, the
first poll and every heal attempt that follows all have to see it, because the one that
reads it first is not the one that can act on it (a heal that cannot take the lock
spawns nothing and would otherwise wait out a deadline for a process that is gone).

**The cause comes from the boot log, bounded and attributed.** ``backend-boot.log`` is
one file shared by every boot (it is rolled by size, not per backend), so the size it
had BEFORE the spawn is kept and only the bytes written after it are read, the last
:data:`TAIL_BYTES` of them: an old crash is not blamed for a new exit. The last
exception line in that window is the cause. Attribution is best-effort: sibling
proxies share the file, and a log that was rolled since the spawn (smaller than the
offset) is another boot's output, so it yields no cause. It is an exception MESSAGE
from the backend's own stderr, so it is capped at :data:`CAUSE_MAX_CHARS`, stripped of
control characters, and scrubbed of the shapes a secret takes in one (``key=value``
for the usual secret names, URL credentials, long opaque tokens).

**Telling the client.** :func:`latest` is what ``proxy_selfheal`` answers the client's
queued calls with while no backend exists, so the ImportError reaches the tool call
that failed and not just a file. It stands until :func:`ready` or the next
:func:`spawn`.

A leaf: ``psutil`` and (lazily, for the one spawn) ``backend_launch``. Imported lazily
by ``backend_probe`` and ``proxy_selfheal``, so the stdio proxy's cold start does not
pay for ``psutil`` before a wait needs it.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING, NamedTuple

import psutil

if TYPE_CHECKING:
    from pathlib import Path

    from stealth_chrome_devtools_mcp.embedded.backend_launch import Launched

_logger = logging.getLogger("stealth.proxy")

#: How much of the boot log is read for the cause: the tail of what the backend
#: wrote since it was spawned. A traceback is a few KB; this is a ceiling, not a size.
TAIL_BYTES = 64 * 1024
#: The cause is capped here, ellipsis included.
CAUSE_MAX_CHARS = 300

#: The last line of a Python traceback (``ImportError: ...``, ``fastmcp.exceptions.
#: ToolError: ...``), or the interpreter's own ``Fatal Python error``. Column 0 only:
#: a traceback's frames and source lines are indented, and the chaining sentences
#: ("During handling of the above exception...") name no exception class.
_EXCEPTION_LINE = re.compile(
    r"^(?:(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt|Warning)"
    r"(?::.*)?|Fatal Python error: .*)$"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: What a secret looks like inside an error message. Deliberately blunt: a cause
#: that loses a word is a smaller loss than one that prints a credential.
_SECRET_VALUE = re.compile(
    r"(?i)([A-Za-z0-9_]*?(?:token|secret|passw(?:or)?d|cookie|api[_-]?key|dsn"
    r"|authori[sz]ation))(\s*[=:]\s*)(?:bearer\s+)?\S+"
)
_URL_CREDENTIALS = re.compile(r"(?<=://)[^/\s@]+@")
_OPAQUE_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


class Failure(NamedTuple):
    """A launched backend that exited before it was ready."""

    exit_code: int | None
    cause: str | None
    boot_log: Path | None

    def describe(self) -> str:
        """The one sentence for this: the proxy log's ERROR and the client's error."""
        code = "unknown" if self.exit_code is None else str(self.exit_code)
        said = f": {self.cause}" if self.cause else " (no exception line in the log)"
        where = f" (boot log: {self.boot_log})" if self.boot_log else " (no boot log)"
        return (
            f"the backend this proxy launched exited with code {code} before "
            f"it was ready{said}{where}"
        )


class _Launch(NamedTuple):
    proc: psutil.Process | None  # None: it was already gone when we looked
    boot_log: Path | None
    offset: int  # the boot log's size before the spawn


_lock = threading.Lock()
#: port -> the backend this process spawned for it and has not yet seen ready or exit.
_watched: dict[int, _Launch] = {}
#: The exit the current wait ended on (one key, so no ``global`` statement).
_verdict: dict[str, Failure | None] = {"failure": None}


def spawn(
    port: int, cmd: list[str], env: dict[str, str], boot_log: Path | None
) -> Launched:
    """``backend_launch.spawn``, and the record that this process owns the result.

    The boot log's size is read BEFORE the spawn so the cause can be attributed to
    this backend alone. Everything about HOW the process is created stays
    ``backend_launch``'s; resolved at call time, so the suite's patch of
    ``backend_launch.spawn`` still steers it.
    """
    from stealth_chrome_devtools_mcp.embedded import backend_launch

    offset = _size(boot_log)
    launched = backend_launch.spawn(cmd, env, boot_log)
    try:
        proc: psutil.Process | None = psutil.Process(launched.pid)
    except psutil.NoSuchProcess:
        proc = None  # exited before we could look: the fastest crash there is
    except psutil.Error:
        return launched  # cannot watch it; it is waited on as it always was
    with _lock:
        _watched[port] = _Launch(proc, boot_log, offset)
        _verdict["failure"] = None  # a new launch is a new story
    return launched


def ready(port: int | None) -> None:
    """``port``'s backend answered: nothing is wrong, and nothing is watched."""
    with _lock:
        if port is not None:
            _watched.pop(port, None)
        _verdict["failure"] = None


def exited(port: int | None) -> bool:
    """Has the backend THIS process launched for ``port`` exited? A cheap peek: no
    log is read and nothing is recorded. False for any backend we did not launch."""
    with _lock:
        watched = _watched.get(port) if port is not None else None
    return watched is not None and _exit_of(watched.proc)[0]


def check(port: int | None) -> Failure | None:
    """The :class:`Failure` iff the backend THIS process launched for ``port`` has
    exited; ``None`` while it runs and for any backend we did not launch. Not
    consuming (see the module doc); it also becomes :func:`latest`."""
    with _lock:
        watched = _watched.get(port) if port is not None else None
    if watched is None:
        return None
    gone, code = _exit_of(watched.proc)
    if not gone:
        return None
    failure = Failure(code, _cause(watched.boot_log, watched.offset), watched.boot_log)
    with _lock:
        if _watched.get(port) is watched:
            _verdict["failure"] = failure
    return failure


def latest() -> Failure | None:
    """The exit the current wait ended on, for the client to be told about."""
    with _lock:
        return _verdict["failure"]


def _exit_of(proc: psutil.Process | None) -> tuple[bool, int | None]:
    """``(has it exited, its exit code if the OS still knows it)``."""
    if proc is None:
        return True, None
    try:
        return True, proc.wait(0)
    except psutil.NoSuchProcess:  # ZombieProcess is one too
        return True, None
    except psutil.Error:  # still running (TimeoutExpired), or not ours to know
        return False, None


def _size(path: Path | None) -> int:
    try:
        return 0 if path is None else path.stat().st_size
    except OSError:
        return 0


def _cause(boot_log: Path | None, offset: int) -> str | None:
    """The last exception line the backend wrote to ``boot_log`` after ``offset``."""
    if boot_log is None:
        return None
    try:
        with boot_log.open("rb") as handle:
            size = handle.seek(0, 2)
            if size < offset:
                return None  # rolled since the spawn: that file is another boot's
            start = max(offset, size - TAIL_BYTES)
            handle.seek(start)
            text = handle.read(TAIL_BYTES).decode("utf-8", errors="replace")
    except OSError:
        _logger.debug("could not read the boot log tail", exc_info=True)
        return None
    for line in reversed(text.splitlines()):
        if _EXCEPTION_LINE.match(line):
            return _tidy(line)
    return None


def _tidy(line: str) -> str:
    """One exception line, safe to print: no control characters, no secret shapes,
    at most :data:`CAUSE_MAX_CHARS`."""
    line = _CONTROL.sub(" ", line).strip()
    line = _SECRET_VALUE.sub(r"\1\2<redacted>", line)
    line = _URL_CREDENTIALS.sub("<redacted>@", line)
    line = _OPAQUE_TOKEN.sub("<redacted>", line)
    if len(line) > CAUSE_MAX_CHARS:
        line = line[: CAUSE_MAX_CHARS - 1] + "…"
    return line
