"""THE one home for auto-clones that nothing owns any more (T-5968).

The storage-cap sweep (``clone_storage``) reclaims only when the clone root is
OVER its cap, so a clone whose browser has gone and whose delete never finished
sits there until the cap is reached: a backend killed hard (Windows
``stop``/``restart`` is a ``TerminateProcess`` that runs no shutdown hook), a
recovery pass that drops its record after one locked delete
(``process_cleanup._recover_orphaned_processes``), a launch that failed after the
copy. With a 10 GiB cap that is ~100 dead profiles, and a cap is no hygiene. This
is the floor under it: a MARKED auto-clone not used for :data:`STALE_CLONE_SECONDS`
that no browser may hold and no spawn has reserved is moved aside through
``clone_trash`` (recoverable for its retention window, purged later), on the same
trigger as the cap sweep (``clone_storage.run_storage_sweep``: startup and before
each clone spawn).

Which directory may be taken is ``clone_trash.disposable``'s answer, the cap
sweep's too, so the two sweeps cannot disagree about a seed, a link or a named
session. What this adds is every way to read "unknown" as "keep":

* only a directory directly under the clone root, never scratch
  (``profile_copy.is_scratch``) or ``.trash``;
* "last used" is the NEWEST of the marker's ``created_at``, the directory's own
  mtime and the files Chrome rewrites whenever it runs (:data:`USE_WITNESSES`),
  so a clone a caller re-opened by path or name is not stale because it was
  created long ago; with no readable ``created_at`` it is kept;
* whether a browser may hold it is asked AGAIN after the process-table scan,
  which is slow enough for a sibling backend's spawn to publish a fresh copy
  under the same name, and an answer that raises is "held";
* a move the filesystem refuses is left alone (``clone_trash.trash`` never turns
  it into a delete).

The first sweep after an upgrade can therefore move every clone older than the
window at once, so it says so first: one WARNING with the count, the bytes and
the names, before anything moves.

Staging copies a dead backend left (``profile_copy.stale_scratch``: only ones
carrying the ``STAGING_OWNER`` proof) are removed through ``clone_trash.discard``,
the one guarded delete.

A leaf: ``clone_trash``, ``profile_copy`` and ``profile_seed``; reserved and held
arrive as callables.
"""

import time
from collections.abc import Callable, Iterable
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import clone_trash, profile_copy, profile_seed
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

#: A spawn copies and launches within minutes (``spawn_budget``); a clone this
#: long unused that nothing holds is not one being built.
STALE_CLONE_SECONDS = 2 * 3600.0

#: Files Chrome rewrites when it runs or closes, newest first by use: any of them
#: newer than the cutoff is evidence the clone was opened since.
USE_WITNESSES = (
    profile_seed.PROFILE_STATE_FILE,
    "Default/Preferences",
    *profile_seed.LOGIN_WITNESSES,
)


def _folded(names: Iterable[str]) -> frozenset[str]:
    """*names* folded the way ``clone_trash`` folds a directory's."""
    return frozenset(name.casefold().rstrip(". ") for name in names)


def last_used(entry: Path) -> float | None:
    """When *entry* was last known to be in use, epoch seconds: the newest of its
    marker's ``created_at``, its own mtime and :data:`USE_WITNESSES`. None when
    the marker gives no readable creation time (nothing trustworthy to age by)."""
    created = profile_seed.moment(profile_seed.read_marker(entry).get("created_at"))
    if created is None:
        return None
    newest = created
    for rel in ("", *USE_WITNESSES):
        try:
            newest = max(newest, (entry / rel if rel else entry).stat().st_mtime)
        except OSError:
            continue
    return newest


def _stale(
    entry: Path,
    cutoff: float,
    protected: Callable[[Path], bool],
    spared: frozenset[str],
) -> bool:
    """Whether *entry* is a disposable clone, unreserved, unused since *cutoff*.
    Read-only; anything unreadable is False."""
    name = entry.name
    if name == clone_trash.TRASH_DIRNAME or profile_copy.is_scratch(name):
        return False
    used = last_used(entry)
    return (
        used is not None
        and used < cutoff
        and clone_trash.disposable(entry, spared)
        and not protected(entry)
    )


def stale_clones(
    clone_root: Path,
    protected: Callable[[Path], bool],
    spared: Iterable[str] = (),
    older_than: float = STALE_CLONE_SECONDS,
) -> list[Path]:
    """Disposable clones directly under *clone_root* unused for *older_than* and
    not reserved by a spawn (*protected*). Read-only: whether a browser may hold
    one is :func:`reap`'s question, asked at the moment of the move."""
    cutoff, names = time.time() - older_than, _folded(spared)
    try:
        entries = list(clone_root.iterdir())
    except OSError:
        return []
    return [e for e in entries if _stale(e, cutoff, protected, names)]


def holds_browser(held: Callable[[Path], bool]) -> Callable[[Path], bool]:
    """*held* with an answer that raises read as "held"."""

    def safe(entry: Path) -> bool:
        try:
            return bool(held(entry))
        except Exception as error:  # noqa: BLE001  PERMANENT(an answer that cannot be had is HELD)
            debug_logger.log_warning(
                "server",
                "stale_clone_sweep",
                f"kept {entry.name}: cannot tell if a browser has it: {error!r}",
            )
            return True

    return safe


def reap(
    clone_root: Path,
    protected: Callable[[Path], bool],
    held: Callable[[Path], bool],
    spared: Iterable[str] = (),
    older_than: float = STALE_CLONE_SECONDS,
) -> int:
    """Move every stale clone aside and remove the staging copies a dead backend
    left; the count of clones moved. Never raises."""
    names = _folded(spared)

    def holds(entry: Path) -> bool:
        """What ``clone_trash.trash`` asks at the move: a browser may hold it, OR
        it is no longer stale. The second half is judged AFTER the first, because
        the process-table scan takes long enough for a sibling backend's spawn to
        publish a fresh copy under this very name since the listing."""
        try:
            if held(entry):
                return True
        except Exception as error:  # noqa: BLE001  PERMANENT(an answer that cannot be had is HELD: one extra day on disk is survivable, a live browser's profile moved is not)
            debug_logger.log_warning(
                "server",
                "stale_clone_sweep",
                f"kept {entry.name}: cannot tell if a browser has it: {error!r}",
            )
            return True
        return not _stale(entry, time.time() - older_than, protected, names)

    try:
        for entry in profile_copy.stale_scratch(
            clone_root, keep=names | profile_seed.FOLDED_NAMES
        ):
            clone_trash.discard(entry, names, holds_browser(held))
    except Exception as error:  # noqa: BLE001  PERMANENT(scratch is best-effort and must not stop the sweep over the clones)
        debug_logger.log_warning(
            "server", "stale_clone_sweep", f"staging reclaim failed: {error!r}"
        )
    candidates = stale_clones(clone_root, protected, names, older_than)
    if candidates:
        # Say what is about to happen BEFORE it does: the first sweep after an
        # upgrade may take every clone older than the window at once.
        size = sum(profile_copy.dir_size_bytes(entry) for entry in candidates)
        debug_logger.log_warning(
            "server",
            "stale_clone_sweep",
            f"moving {len(candidates)} clone(s) unused for {older_than / 3600:g} h "
            f"({size} bytes) to .trash: {', '.join(e.name for e in candidates)}",
        )
    moved = 0
    for entry in candidates:
        try:
            if clone_trash.trash(entry, clone_root, holds) is not None:
                moved += 1
        except Exception as error:  # noqa: BLE001  PERMANENT(one clone's failure must not stop the sweep over the rest)
            debug_logger.log_warning(
                "server", "stale_clone_sweep", f"kept {entry.name}: {error!r}"
            )
    return moved
