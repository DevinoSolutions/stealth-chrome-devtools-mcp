"""THE one home for the ``server.json`` backend record: its schema, its
read/write/clear I/O, and the canonical definition of the state dir it lives in.

Moved out of ``singleton.py`` (plan_F808 Task 2), which keeps lifecycle only —
probe, lock, spawn, evict, proxy — and re-exports the path names because
``settings.py``, ``logging_setup``, ``process_cleanup``, ``response_handler`` and
several tests reach for ``singleton.STATE_DIR`` / ``singleton._read_server_state``.

The I/O takes the record's path as an argument rather than reading the module
global. That is deliberate, not ceremony: the caller's binding is what selects
the file, so redirecting ``singleton.SERVER_STATE_FILE`` at runtime (which the
hermetic tests do, and which is the only thing keeping a test run from editing
the developer's live ``~/.stealth-mcp`` record) still reaches every read and
write. A function that closed over this module's own global would silently
ignore that redirection.

Corollary: no function here may take a default path. This module's own
``SERVER_STATE_FILE`` names the real file; a default would bind it at def-time
and bypass every caller's redirection. ``test_backend_registry.py`` pins this.

Schema (plan_F808 Task 3). v2 keys the record by **display context**, so one
machine can hold a headless backend and a desktop backend at once and discovery
can pick the one that can actually show a window::

    {"schema": 2, "backends": {"<display_context>": {port, version, pid,
                                                     source_fingerprint,
                                                     display_context}}}

A v1 record — the flat ``{port, version, pid, source_fingerprint}`` every
release up to 2.0.3 wrote — still reads, as ONE backend classified
``UNVERIFIED`` (treated as window-capable). Dropping it instead would evict a
perfectly healthy backend the moment a user upgrades.

There is no migration step; instead :func:`record_backend` **supersedes by
port**. The upgraded client writes under its REAL context (``win-session-1``,
``x11-:0``, …), which is a different key from the ``UNVERIFIED`` one the v1
record reads as — so without this rule the v1 entry would survive next to the
new one, sort first, and be handed to every reader forever. That is not a
cosmetic duplicate: the stale entry's version can never match the running
package, so the reuse gate would kill and respawn the shared backend on every
single proxy start. Hence: recording a backend also drops any ``UNVERIFIED``
entry on the SAME port. Sound because the v1 format is only ever written by
<= 2.0.3, so such an entry is always version-stale to the client doing the
write, and same-port means it is this very backend being re-recorded under its
real identity after the intended one-shot upgrade eviction. An ``UNVERIFIED``
entry on a DIFFERENT port is left alone — it may be a live backend on a
platform we genuinely cannot classify, or one whose probe failed.

A leaf module: stdlib plus ``display_context`` (itself a leaf). Never
``singleton``, never ``server``.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded.display_context import HEADLESS, UNVERIFIED

# THE definition of the state dir. settings.py recomputes this one path (as
# _STATE_DIR_ENV_FILE) because it is a leaf module that may not import the
# package; keep the two in step. Nothing else may fork it.
STATE_DIR = Path.home() / ".stealth-mcp"
PORT_FILE = STATE_DIR / "server.port"
# Records {port, version, pid} per display context for the backends we started,
# so discovery can confirm a running backend is the SAME version before reusing
# it. Without this an upgraded session silently reuses a stale old-version
# backend (issue #14).
SERVER_STATE_FILE = STATE_DIR / "server.json"

SCHEMA_VERSION = 2


def read_record(path: Path) -> dict | None:
    """Return the raw record as written, or None if absent/corrupt.

    The RAW shape — v1 flat or v2 — because ``singleton._read_server_state`` is
    a patch surface several test modules stub with v1-flat dicts. Callers that
    want backends read them through :func:`backends_in` / :func:`first_backend`
    / :func:`backend_on_port`, which normalize both shapes; nothing outside this
    module should branch on ``"schema"`` itself.
    """
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def backends_in(state: dict | None) -> list[dict]:
    """THE schema reader: a raw record (either version) → its backend entries.

    Every other accessor here is derived from this one, so "how a record is
    laid out" is answered in exactly one place. Each returned entry carries a
    ``display_context``: for v2 it comes from the key (the key is authoritative,
    so a hand-edited file cannot disagree with itself); for a v1 flat record it
    defaults to ``UNVERIFIED``, which :mod:`display_context` treats as
    window-capable. Anything unreadable — missing file, corrupt JSON, a
    non-object top level, a v2 file whose ``backends`` is not a mapping — is no
    backends, never an exception: this record is a cache, and discovery must not
    be able to fail on it.
    """
    if not isinstance(state, dict):
        return []
    if state.get("schema") == SCHEMA_VERSION:
        backends = state.get("backends")
        if not isinstance(backends, dict):
            return []
        return [
            {**entry, "display_context": ctx}
            for ctx, entry in backends.items()
            if isinstance(entry, dict)
        ]
    if isinstance(state.get("port"), int):
        return [{**state, "display_context": state.get("display_context", UNVERIFIED)}]
    return []


def first_backend(state: dict | None) -> dict | None:
    """The first recorded backend in a raw record, or None.

    "First" preserves the pre-v2 single-backend behaviour exactly (a v1 record
    reads as one entry). It carries no preference of its own — a caller that
    wants the backend most likely to be usable asks :func:`window_capable_first`.
    """
    return next(iter(backends_in(state)), None)


def backend_on_port(state: dict | None, port: int | None) -> dict | None:
    """The recorded backend listening on ``port``, or None.

    For the callers that already hold a port and want THAT backend's fields —
    the pid to terminate, the fingerprint that explains an eviction — rather
    than whichever entry happens to come first.
    """
    return next((e for e in backends_in(state) if e.get("port") == port), None)


def read_backends(path: Path) -> list[dict]:
    """Every backend recorded in the file at ``path``, in recorded order."""
    return backends_in(read_record(path))


def window_capable_first(path: Path) -> list[dict]:
    """Recorded backends, those that can show a window before those that cannot.

    The search order discovery follows, so an SSH or service-session client
    converges on the desktop backend already running instead of starting a
    second, blind one. Only a PROVEN-invisible context sorts last; ``UNVERIFIED``
    stays with the capable group, matching ``display_context.can_show_windows``.
    The sort is stable, so recorded order breaks ties.
    """
    return sorted(
        read_backends(path), key=lambda e: e.get("display_context") == HEADLESS
    )


def record_backend(  # noqa: PLR0913  PERMANENT(function interface)
    path: Path,
    *,
    port: int,
    version: str,
    pid: int,
    source_fingerprint: str,
    display_context: str,
) -> None:
    """Record one backend's identity under its display context, replacing any
    previous entry for that SAME context and leaving the others untouched.

    Identity is the port, the package version that started it, its pid, and a
    fingerprint of the source it is running: discovery reuses a backend only
    when BOTH the version AND the source fingerprint still match (and it answers
    a live probe); the pid is used to evict a stale one.

    Also supersedes by port: a v1 (<= 2.0.3) record on THIS port is the same
    backend being re-recorded under its real identity, so it is dropped rather
    than left to shadow this entry forever. The module docstring carries the
    full argument; an UNVERIFIED entry on a different port survives.
    """
    entries = {}
    for recorded in read_backends(path):
        ctx = recorded["display_context"]
        if ctx == UNVERIFIED and recorded.get("port") == port:
            continue
        entries[ctx] = recorded
    entries[display_context] = {
        "port": port,
        "version": version,
        "pid": pid,
        "source_fingerprint": source_fingerprint,
        "display_context": display_context,
    }
    _write(path, entries)


def forget_backend(path: Path, display_context: str) -> None:
    """Drop one context's entry, keeping the rest. Forgetting the last one
    leaves a readable empty record rather than removing the file — only
    :func:`clear_record` (the ``stop`` verb) deletes it.
    """
    entries = {
        e["display_context"]: e
        for e in read_backends(path)
        if e["display_context"] != display_context
    }
    _write(path, entries)


def _write(path: Path, entries: dict[str, dict]) -> None:
    """Write the v2 record atomically: stage into a sibling temp file, then
    ``Path.replace`` (i.e. ``os.replace``) — atomic on both Windows and POSIX,
    so a reader concurrent with a write sees the whole old record or the whole
    new one, never a truncated file, and a crash mid-write cannot leave the
    record unparseable.

    No locking here on purpose. Every production writer already runs under
    singleton's cold-start file lock, so what this module owes is atomicity of
    an individual write, not a merge protocol between racing writers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # pid-suffixed so two processes staging at once cannot collide on the temp
    # name; the same directory so the replace stays within one filesystem.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"schema": SCHEMA_VERSION, "backends": entries}))
        tmp.replace(path)
    except BaseException:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def clear_record(*paths: Path) -> None:
    """Best-effort unlink of the given files; a path that is absent or that the
    OS refuses is skipped, never raised.

    `stop_backend()` calls this with the server.json record and the legacy
    write-only port file, so a stale record can never make a later
    `_find_running_server` believe a stopped backend is still there to reuse —
    but that pair is the example, not the contract.
    """
    for path in paths:
        with suppress(OSError):
            path.unlink(missing_ok=True)
