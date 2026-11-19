"""THE one atomic file write: stage into a sibling temp file, then replace.

A reader concurrent with a write sees the whole old file or the whole new one,
never a truncated one, and a crash mid-write cannot leave the target
unparseable. ``backend_registry`` (``server.json`` and its heartbeat sidecars),
``browser_pid_registry`` (``browser_pids.json``), ``backend_supervisor``
(``supervisor.json``) and ``profile_lock`` (the launch marker) all publish
through here, so a second spelling of the staging dance cannot appear beside it.

What every caller gets, and must not lose:

* a temp name suffixed with the pid, in the target's own directory: two
  processes staging at once cannot collide on the name, and the replace stays
  within one filesystem;
* a bounded retry of the replace on ``PermissionError``. ``Path.replace`` is
  atomic on both platforms, but on Windows it raises (WinError 5/32) while
  ANOTHER process has the target open, because Python's ``open()`` does not pass
  FILE_SHARE_DELETE, so a concurrent reader is enough to lose a race it would
  win on POSIX. The window is microseconds; a few short retries turn a spurious
  hard failure into a slightly later success. Do NOT delete the loop as
  redundant: on POSIX it costs one iteration and never sleeps, and the failure
  it prevents only appears under the concurrency a herd of proxy starts
  produces, never in a single-process test;
* cleanup of the temp file on ANY failure, ``BaseException`` included, then the
  original error.

ACCEPTED GAP: a process KILLED between the write and the replace leaves its
``.<pid>.tmp`` behind and nothing sweeps it. Bounded and harmless: one small
file per killed writer, in a directory the readers only ever address by exact
name.

NOT fsynced, as none of the sites it replaced was: what they relied on is
atomicity against a concurrent reader and a crashed process, not durability
across a power cut, and the heartbeat stamp rides this path every few seconds.
Adding an fsync is a decision for a change of its own.

A leaf: it imports nothing from this package, so any module may use it. It does
not swallow ``OSError``: what a failed write MEANS differs per caller (the
registries raise, the supervisor logs, the launch marker logs and carries on),
so each caller decides.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Windows refuses an atomic replace while another process holds the target
#: open (see :func:`commit`); a few short retries outlast that window.
COMMIT_ATTEMPTS = 5
COMMIT_RETRY_SECONDS = 0.02


def commit(tmp: Path, path: Path) -> None:
    """Move ``tmp`` onto ``path``, retrying briefly on a Windows sharing refusal.

    A refusal that outlasts :data:`COMMIT_ATTEMPTS` is raised, and the caller's
    ``tmp`` is left for it to remove (:func:`write_text` does).
    """
    for attempt in range(COMMIT_ATTEMPTS):
        try:
            tmp.replace(path)
        except PermissionError:
            if attempt == COMMIT_ATTEMPTS - 1:
                raise
            time.sleep(COMMIT_RETRY_SECONDS)
        else:
            return


def write_text(
    path: Path, text: str, *, encoding: str = "utf-8", make_parents: bool = True
) -> None:
    """Publish ``text`` at ``path`` atomically (see the module docstring).

    Creates the parent directory unless ``make_parents`` is False: a caller
    whose directory may be deleted under it (a profile being discarded) must
    fail rather than bring that directory back. Raises ``OSError`` when the
    write or the replace cannot be done; the previous file is then whole and no
    temp file is left behind.
    """
    if make_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        commit(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def write_json(
    path: Path,
    payload: object,
    *,
    indent: int | None = None,
    make_parents: bool = True,
) -> None:
    """:func:`write_text` of ``json.dumps(payload)``, serialised BEFORE anything
    is staged, so an unserialisable payload leaves nothing on disk."""
    write_text(path, json.dumps(payload, indent=indent), make_parents=make_parents)
