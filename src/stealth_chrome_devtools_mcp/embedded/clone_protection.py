"""THE one home for which clone directories are in flight or live, and so exempt
from the storage-cap sweep — moved VERBATIM out of ``clone_storage`` by F-967,
which needed a lock-held choose-and-reserve step beside it and that file's last
line of budget (it stood at exactly 1000/1000, a cap that ratchets DOWN only).

The storage-cap sweep picks reclaim targets from on-disk markers, but a clone
becomes a valid target the instant its marker is written — which happens BEFORE
its browser launches and is tracked. During that window the filesystem liveness
heuristic (``clone_storage._profile_has_running_browser``) reports "not running",
so a concurrent or startup sweep could delete a live-but-not-yet-attached clone
out from under the spawning browser (a silent, unlogged session loss). We
therefore register every clone dir the spawn flow is about to write — before the
marker exists — and clear it on close. The sweep skips any protected dir
regardless of what the filesystem heuristic reports. Guarded by a lock because
the sweep runs on a worker thread (``asyncio.to_thread``) and, since F-967, so
does every spawn's reservation.

``clone_storage`` re-binds these under the underscore names the rest of the tree
and the suite have always called; nothing here imports it. A leaf: ``profile_copy``
(itself a leaf), ``os`` and ``threading``.
"""

import os
import threading
from collections.abc import Callable
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import profile_copy

PROTECTED: set[str] = set()
_LOCK = threading.Lock()
# Makes "choose a directory nothing has reserved" and "reserve it" ONE step: F-834
# relied on there being no ``await`` between the two, and the choice now runs on a
# worker thread (F-967), where only a lock can say that.
_RESERVE_LOCK = threading.Lock()


def normalize(path: object) -> str:
    """Case/separator-normalized absolute path for protected-set membership."""
    # abspath, not resolve(): this is asked per clone by every sweep and must not
    # touch the filesystem or follow a link.
    return os.path.normcase(os.path.abspath(str(path)))  # noqa: PTH100  PERMANENT(a path comparison key, not a filesystem read)


def protect(path: object) -> None:
    """Shield a clone dir from the storage-cap sweep while it is in flight or
    live. Call BEFORE the clone's marker is written and keep it protected until
    the owning instance has closed."""
    with _LOCK:
        PROTECTED.add(normalize(path))


def release(path: object) -> None:
    """Drop sweep protection for a clone dir once its instance has closed."""
    with _LOCK:
        PROTECTED.discard(normalize(path))


def is_protected(path: object) -> bool:
    """True while a clone dir is registered as in-flight or live (sweep-exempt)."""
    with _LOCK:
        return normalize(path) in PROTECTED


def clear() -> None:
    """Drop all sweep protection. For test isolation and full-shutdown cleanup."""
    with _LOCK:
        PROTECTED.clear()


def reserve(choose: Callable[[], Path], held: list[Path]) -> Path:
    """The directory ``choose`` names, reserved before anyone else can choose it;
    ``held`` is told, for :func:`discard`."""
    with _RESERVE_LOCK:
        clone = choose()
        protect(clone)
        held.append(clone)
        return clone


def discard(made: list[Path], held: list[Path]) -> None:
    """What a cancelled or failed selection leaves behind (F-967): the
    directories it built, then the reservations it took."""
    for directory in made:
        profile_copy.rmtree_robust(directory)
    for directory in held:
        release(directory)
