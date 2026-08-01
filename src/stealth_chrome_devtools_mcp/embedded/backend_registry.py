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

A leaf module: stdlib only, no back edge to ``singleton``.
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path

# THE definition of the state dir. settings.py recomputes this one path (as
# _STATE_DIR_ENV_FILE) because it is a leaf module that may not import the
# package; keep the two in step. Nothing else may fork it.
STATE_DIR = Path.home() / ".stealth-mcp"
PORT_FILE = STATE_DIR / "server.port"
# Records {port, version, pid} for the backend we started, so discovery can
# confirm a running backend is the SAME version before reusing it. Without this
# an upgraded session silently reuses a stale old-version backend (issue #14).
SERVER_STATE_FILE = STATE_DIR / "server.json"


def read_record(path: Path) -> dict | None:
    """Return the recorded ``{port, version, pid}`` for the backend we started.

    None if there is no state file or it is missing/corrupt. This is the record
    written by :func:`write_record`; a backend started by an older release
    (<= 1.2.0) has no such file and is therefore treated as version-unknown.
    """
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    return state if isinstance(state, dict) else None


def write_record(
    path: Path, port: int, version: str, pid: int, source_fingerprint: str
) -> None:
    """Record the running backend's identity: its port, the package version that
    started it, its pid, and a fingerprint of the source it is running. Discovery
    reuses the backend only when BOTH the version AND the source fingerprint still
    match (and it answers a live probe); the pid is used to evict a stale backend
    (mismatched version or source).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "port": port,
                "version": version,
                "pid": pid,
                "source_fingerprint": source_fingerprint,
            }
        )
    )


def clear_record(*paths: Path) -> None:
    """Remove the recorded backend identity (server.json) and the legacy
    write-only port file, best-effort. Used by `stop_backend()` so a stale
    record can never make a later `_find_running_server` believe a stopped
    backend is still there to reuse.
    """
    for path in paths:
        with suppress(OSError):
            path.unlink(missing_ok=True)
