"""THE one home for RECOVERABLE eviction — an auto-clone the storage cap takes
away is moved aside, not deleted, and purged only after a retention window.

The argument is one sentence and it is the worst incident this project has had:
a wrong eviction deletes a browser session outright, and there is nothing to put
back. Moving the directory instead is a rename WITHIN the clone root, so it is
instant and same-volume, and it turns that class of bug from irreversible data
loss into something an operator can carry back by hand. The window is a knob
(``STEALTH_MCP_CLONE_TRASH_RETENTION_HOURS``) and a value <= 0 restores the old
delete-immediately behaviour for anyone who wants it.

It left ``clone_storage`` when F-914/F-915 put that file over a 1000-LOC budget
that ratchets DOWN only (``dom_handler`` -> ``text_entry``'s precedent,
``clone_storage`` -> ``profile_copy``'s twice over). The line between the two is
the question each answers: that module decides WHICH clones exceed a cap and
must go, this one decides what GOING means and for how long it can be undone.
Nothing here reads a marker, a role or a seed, with ONE exception that is a
refusal and never a selection: :func:`discard` — the end of a clone whose browser
is already gone (T-5968) — declines the seeds' own names and any directory a
marker does not call disposable, so a path that reaches it by mistake cannot
delete a login.

The trash directory is excluded from every clone-root scan, which is why
:data:`TRASH_DIRNAME` is public: a scan that re-selected, re-sized or re-swept
its contents would count evicted clones against the very cap their eviction was
meant to satisfy, and would eventually evict them a second time.

A leaf: ``profile_copy`` for the one robust delete, ``profile_seed`` for the
refusal, ``settings`` for the one knob, stdlib for the rest. Whether a directory
is BUSY arrives as a callable, so this module never learns how that is decided
(F-871's answer lives in ``profile_lock`` and reaches here through
``clone_storage``). Never raises: every failure is reported by answering ``None``
or by falling back to a best-effort delete, because the storage cap still has to
be honoured and a refusal to reclaim is strictly worse than the behaviour this
replaced.
"""

import os
import time
from collections.abc import Callable, Container
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import profile_copy, profile_seed
from stealth_chrome_devtools_mcp.settings import get_settings

#: The holding area, inside the clone root so the move is a same-volume rename.
#: Public because every clone-root scan has to skip it by name.
TRASH_DIRNAME = ".trash"


def trash_dir(clone_root: Path) -> Path:
    return clone_root / TRASH_DIRNAME


def retention_seconds() -> float:
    """How long an evicted clone stays recoverable before it is purged.

    Default 24h; override with ``STEALTH_MCP_CLONE_TRASH_RETENTION_HOURS``.
    """
    hours = get_settings().clone_trash_retention_hours
    return max(0.0, hours) * 3600.0


def trash(
    entry: Path,
    clone_root: Path,
    held: Callable[[Path], bool],
    *,
    delete_on_failure: bool = True,
) -> Path | None:
    """Move an evicted auto-clone aside so it stays recoverable.

    Answers the new path, or ``None`` when the move was refused or the entry had
    to be deleted instead. A running profile is never moved — selection already
    excludes live sessions, so this is belt-and-suspenders — and if the rename
    fails (a Windows lock, say) the cap must still be honoured, so it falls back
    to a best-effort delete, which is strictly no worse than the behaviour this
    replaced. A caller with no cap to honour (``clone_stale``) says
    ``delete_on_failure=False`` and gets ``None`` with the entry untouched: a
    refused rename is evidence of an open handle, not licence to delete.
    """
    if held(entry):
        return None
    holding = trash_dir(clone_root)
    try:
        holding.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    target = holding / entry.name
    counter = 1
    while target.exists():
        target = holding / f"{entry.name}-{counter}"
        counter += 1
    try:
        os.replace(str(entry), str(target))
    except OSError:
        if delete_on_failure:
            profile_copy.rmtree_robust(entry)
        return None
    try:
        # Stamp the trash time so retention is measured from EVICTION and not
        # from the clone's original creation (a rename preserves the old mtime).
        os.utime(target, None)
    except OSError:
        pass
    return target


def protected(entry: Path, spared: Container[str] = ()) -> bool:
    """A directory :func:`discard` must never delete: the seeds' own names
    (``master``, ``master-snapshot``, ``default``), any name in *spared* (the
    caller's own — ``fleet``, a configured seed directory), and anything carrying
    a marker that does not call it a disposable auto-clone: a named session, a
    legacy marker, one that could not be read. Unknown is protected.

    An UNMARKED directory is not protected: a half-deleted clone loses its marker
    first, and a nodriver temp profile never had one, so the caller's own record
    is what says those are disposable.
    """
    # The same fold `profile_seed` applies to a requested name: Windows strips
    # a trailing dot or space, so `master.` IS `master` on disk.
    folded = entry.name.casefold().rstrip(". ")
    if folded in profile_seed.FOLDED_NAMES or folded in spared:
        return True
    try:
        marked = (entry / profile_seed.MARKER_NAME).exists()
    except OSError:
        return True
    return marked and not profile_seed.is_auto(entry)


def discard(entry: Path, spared: Container[str] = ()) -> bool:
    """Delete a disposable clone whose browser is gone (T-5968). True when
    *entry* no longer exists at its own path.

    A link is unlinked and never followed, so a junction planted as the clone
    cannot reach the profile it points at. A locked file does not stop the rest
    going (the robust delete takes what it can), and a MARKED clone still there is
    renamed into ``.trash`` stamped as already expired, which frees the name,
    takes it out of every scan and hands the rest to the next
    :func:`purge_expired`. Only a marked clone is moved aside — the holding area
    is the clone root's, and an unmarked directory (a temp profile) has none.

    When a file is held so hard that even the rename is refused, the husk stays
    and False is answered for the caller to retry; its marker is put back if the
    delete took it first, because an unmarked husk is one no later sweep can
    recognise as a clone. A :func:`protected` directory is refused with False.

    The caller establishes that no browser holds it; this does not ask. Never
    raises.
    """
    try:
        if protected(entry, spared):
            return False
        if profile_copy.is_link(entry):
            try:
                entry.unlink()
            except OSError:
                entry.rmdir()  # a junction, or a directory symlink on Windows
            return not entry.exists()
        marker = entry / profile_seed.MARKER_NAME
        marked = profile_seed.is_auto(entry)
        text = marker.read_text(encoding="utf-8") if marked else ""
        profile_copy.rmtree_robust(entry)
        if entry.exists() and marked:
            moved = trash(entry, entry.parent, lambda _entry: False)
            if moved is not None:
                os.utime(moved, (0, 0))
            elif entry.exists() and not marker.exists():
                marker.write_text(text, encoding="utf-8")
        return not entry.exists()
    except OSError:
        return False


def purge_expired(clone_root: Path, max_age_seconds: float) -> int:
    """Delete trashed clones whose time-in-trash exceeds *max_age_seconds*.

    Answers the count purged. Never raises; missing or non-dir trash is a no-op.
    """
    holding = trash_dir(clone_root)
    if not holding.exists():
        return 0
    try:
        entries = list(holding.iterdir())
    except OSError:
        return 0
    cutoff = time.time() - max_age_seconds
    purged = 0
    for entry in entries:
        try:
            if not entry.is_dir() or entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        profile_copy.rmtree_robust(entry)
        if not entry.exists():
            purged += 1
    return purged
