"""THE one home for auto-clones that nothing owns any more (T-5968).

The storage-cap sweep (``clone_storage``) reclaims only when the clone root is
OVER its cap, so a clone whose browser has gone and whose delete never finished
sits there until the cap is reached: a backend killed hard (Windows
``stop``/``restart`` is a ``TerminateProcess`` that runs no shutdown hook), a
recovery pass that drops its record after one locked delete
(``process_cleanup._recover_orphaned_processes``), a launch that failed after the
copy. With a 10 GiB cap that is ~100 dead profiles, and a cap is no hygiene. This
is the floor under it: a MARKED auto-clone older than :data:`STALE_CLONE_SECONDS`
that no browser holds and no spawn has reserved is moved aside through
``clone_trash`` (recoverable for its retention window, purged later), on the same
trigger as the cap sweep (``clone_storage.run_storage_sweep``: startup and before
each clone spawn).

It selects nothing the cap sweep could not already take: ``profile_seed.is_auto``
is the same fail-safe test (a legacy or unreadable marker is never disposable),
the busy witness is the same ``profile_lock`` answer, and eviction is the same
``clone_trash.trash``. What it adds is every way to read "unknown" as "keep":

* only a directory directly under the clone root, never a link, never scratch
  (``profile_copy.is_scratch``) or ``.trash``;
* never a seed's own name or one the caller spares (``fleet``), whatever its
  marker says (``clone_trash.protected``);
* the age comes from the marker's ``created_at`` alone, and an unreadable or
  future stamp is not stale;
* whether a browser holds it is asked AGAIN at the move, the answer that raises
  is "held", and a move the filesystem refuses is left alone and NEVER turned
  into a delete: a Windows rename that fails is more often a process with files
  open than a bad path, which is the one case this must not guess at.

The age is also what keeps it off a clone a spawn is still building (the marker
is written before the launch), and ``clone_trash`` is what makes a wrong guess a
carry-back and not data loss.

A leaf: ``clone_trash`` and ``profile_seed`` for the two rules, ``profile_copy``
for the scratch and link tests. Who is reserved and who is held arrive as
callables.
"""

import time
from collections.abc import Callable, Iterable
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import clone_trash, profile_copy, profile_seed
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

#: A spawn copies and launches within minutes (``spawn_budget``); a clone this
#: old that nothing holds is not one being built.
STALE_CLONE_SECONDS = 2 * 3600.0


def _folded(names: Iterable[str]) -> frozenset[str]:
    """*names* folded the way ``clone_trash.protected`` folds a directory's."""
    return frozenset(name.casefold().rstrip(". ") for name in names)


def _stale(
    entry: Path,
    cutoff: float,
    protected: Callable[[Path], bool],
    spared: frozenset[str],
) -> bool:
    """Whether *entry* is a marked, unreserved, unprotected auto-clone created
    before *cutoff*. Read-only; anything unreadable is False."""
    name = entry.name
    if name == clone_trash.TRASH_DIRNAME or profile_copy.is_scratch(name):
        return False
    try:
        created = profile_seed.moment(profile_seed.read_marker(entry).get("created_at"))
        return (
            created is not None
            and created < cutoff
            and not profile_copy.is_link(entry)
            and entry.is_dir()
            and profile_seed.is_auto(entry)
            and not clone_trash.protected(entry, spared)
            and not protected(entry)
        )
    except OSError:
        return False


def stale_clones(
    clone_root: Path,
    protected: Callable[[Path], bool],
    spared: Iterable[str] = (),
    older_than: float = STALE_CLONE_SECONDS,
) -> list[Path]:
    """Marked auto-clones directly under *clone_root* older than the window and
    not reserved by a spawn (*protected*). Read-only: whether a browser holds one
    is :func:`reap`'s question, asked at the moment of the move."""
    cutoff, names = time.time() - older_than, _folded(spared)
    try:
        entries = list(clone_root.iterdir())
    except OSError:
        return []
    return [e for e in entries if _stale(e, cutoff, protected, names)]


def reap(
    clone_root: Path,
    protected: Callable[[Path], bool],
    held: Callable[[Path], bool],
    spared: Iterable[str] = (),
    older_than: float = STALE_CLONE_SECONDS,
) -> int:
    """Move every stale clone aside; the count moved. Never raises. Also removes
    the staging copies a dead backend left (:func:`profile_copy.reclaim_stale_scratch`),
    which are the same leftovers found by the same sweep."""
    names = _folded(spared)

    def holds(entry: Path) -> bool:
        """What ``clone_trash.trash`` asks at the move: held by a browser, OR no
        longer stale. The second half is judged AFTER the first, because the
        process-table scan takes long enough for a sibling backend's spawn to
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

    # Staging copies a dead backend left (`profile_copy`'s question): the same
    # leftovers, found by the same sweep, and never the seeds' or a spared name's.
    profile_copy.reclaim_stale_scratch(
        clone_root, keep=names | profile_seed.FOLDED_NAMES
    )
    moved = 0
    for entry in stale_clones(clone_root, protected, names, older_than):
        try:
            if (
                clone_trash.trash(entry, clone_root, holds, delete_on_failure=False)
                is not None
            ):
                moved += 1
                debug_logger.log_info(
                    "server", "stale_clone_sweep", f"moved {entry.name} to trash"
                )
        except Exception as error:  # noqa: BLE001  PERMANENT(one clone's failure must not stop the sweep over the rest)
            debug_logger.log_warning(
                "server", "stale_clone_sweep", f"kept {entry.name}: {error!r}"
            )
    return moved
