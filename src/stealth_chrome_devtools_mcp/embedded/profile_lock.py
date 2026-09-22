"""THE one home for INTERPRETING Chrome's process-singleton artefacts — "is this
profile held by a live process, and who holds it" (F-871).

Interpreting, not owning the names: `profile_copy.REGENERABLE_NAMES`
also lists `SingletonLock`/`SingletonSocket`/`SingletonCookie` (and `lockfile`),
for the different question of what a clone must not copy and what a trim may
delete. That list is about bytes on disk; this module is about what they MEAN.

Two witnesses, in order, and no third:

1. **The process table.** A browser whose ``--user-data-dir`` is this directory
   holds it. ``process_cleanup`` owns that scan; it arrives here as an argument
   so this module never learns what an orchestrator is. What that scan answers
   with is a whole process TREE — it applies no ``--type`` filter — and only
   the BROWSER member of it holds the profile, which is
   ``browser_cmdline.browser_members``' question and the one thing this module
   imports (F-931). Chromium gives every child a ``--type=``, so a set of
   survivors that are all children says the browser has GONE: measured on the
   release gate, that window between ``close_instance`` killing the browser and
   its children following it refused the very next spawn onto the shared
   session, and ``min(pids)`` before it named a renderer as the holder in the
   message a caller reads. ``browser_reattach.held_by`` has asked the same
   question since F-888; this is that rule reached from the other side, so the
   two witnesses can no longer disagree.
2. **Chrome's own process singleton**, which is a FACT ABOUT A PID, not a file
   that exists. On POSIX ``ProcessSingleton::Create`` writes ``SingletonLock``
   as a SYMLINK whose target is the string ``<hostname>-<pid>``
   (``chrome/browser/process_singleton_posix.cc``; the delimiter is
   ``kProcessSingletonLockDelimiter = '-'`` and ``ParseProcessSingletonLock``
   splits on the LAST one, so a hostname may contain hyphens). Chrome treats a
   lock it cannot attribute to a live browser as ORPHANED — it unlinks it and
   starts (``NotifyOtherProcessWithTimeout`` -> ``ORPHANED_LOCK_FILE`` ->
   ``PROCESS_NONE``) — and an unparseable one as an INVALID lockfile, which it
   also unlinks. This module answers the same question the same way, so our
   idea of "busy" cannot disagree with the browser's.

**Why the lock and nothing else.** ``SingletonSocket`` and ``SingletonCookie``
are written AFTER the lock, so a live browser always has a lock; but the socket
is a symlink into a per-launch ``/tmp`` directory that a killed Chrome does not
get to clean up, so its target outlives the browser. Asking ``Path.exists()``
of the three names — what this check used to do — therefore read a reaped
browser's residue as "busy" and, for a NAMED profile, silently walked the caller
to ``<name>-2``: a different identity for a profile whose whole purpose is to
keep one. The same ``exists()`` could never see the lock at all, because a
dangling symlink is ``exists() == False``.

**On Windows there is only ONE witness.** Chrome writes no ``Singleton*``
artefact there at all — it takes a ``lockfile`` in the profile with
``FILE_FLAG_DELETE_ON_CLOSE``, which the kernel removes even when the process is
killed — so witness 2 is always vacuous and the process scan carries the answer
alone (as it did before this change; F-860 §3.6 measured 0 stale ``lockfile``s
across 80 idle profiles). Its mere PRESENCE is deliberately NOT taken as a third
witness: a presence test is the exact reasoning this finding condemns, and here
it is not even sound — a power loss or a hard reset leaves a ``lockfile`` behind
forever with no pid in it to check. What the missing second witness DOES change
is the direction an unreadable answer resolves in: see ``_browser_pids``.

**Deliberately NOT here: clearing a stale lock.** Chromium unlinks an orphaned
lock itself on the next launch, so deleting one from our side would be a second
way to do something already done — and doing it from a failure handler would
race a browser that is still dying. Reading is enough; F-860's reaper
(``spawn_leak``) keeps killing pids and leaves the artefacts alone.

**One deliberate divergence from Chromium**: it additionally requires the lock's
pid to BE a browser (``IsChromeProcess``); we ask only that the pid is alive.
A recycled pid then costs one walk, whereas calling a live browser's profile
free costs a corrupted profile. The error is taken in the survivable direction.

Never raises: every caller is deciding where to put a profile, and a failure to
read a lock is not a reason to fail a spawn.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psutil

from stealth_chrome_devtools_mcp.embedded import browser_cmdline
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from pathlib import Path

LOCK_NAME = "SingletonLock"
_DELIMITER = "-"

# Whether Chrome leaves a readable owner in the profile — i.e. whether witness 2
# exists on this platform at all (see the module docstring). False on Windows.
_LOCK_IS_A_WITNESS = os.name != "nt"

# The scan that answers "which live browsers run on this profile directory".
# ``None`` is accepted so a caller whose collaborator has been replaced falls
# back to the lock rather than raising.
PidScan = Callable[..., Collection[int] | None] | None


@dataclass(frozen=True)
class Hold:
    """Who holds a profile directory, and the sentence a caller can be told.

    ``pid`` is ``None`` when the holder is named but not addressable from here
    (a lock written on another host).
    """

    pid: int | None
    reason: str


def profile_hold(profile_dir: Path, live_pids: PidScan) -> Hold | None:
    """Return what holds *profile_dir*, or ``None`` when nothing does."""
    pids = _browser_pids(profile_dir, live_pids)
    if pids:
        held = _tree_hold(profile_dir, pids)
        if held is not None:
            return held
        # Every survivor carries a `--type=`, so the BROWSER has exited and what
        # is left are children outliving it. They hold no profile — Chrome's own
        # singleton is the browser's, not the tree's — so the question falls
        # through to the lock exactly as it does for an empty scan.
    elif pids is None and not _LOCK_IS_A_WITNESS:
        # The one witness this platform has did not answer. Every unanswerable
        # question here resolves toward HELD (see `_pid_alive`) and for the same
        # reason: one extra walk is survivable, two browsers on one profile is
        # not. Unreachable on POSIX — the lock still answers there.
        return Hold(
            None,
            "the process table could not be read and Chrome on this platform "
            "leaves no lock to read instead, so this profile cannot be shown free",
        )
    return _lock_hold(profile_dir / LOCK_NAME)


def _tree_hold(profile_dir: Path, pids: Collection[int]) -> Hold | None:
    """What the process TREE on *profile_dir* proves, or None for "nothing".

    Three answers from two facts (F-931). A browser member holds it and is the
    pid a caller is told about — the LOWEST when Chrome's singleton has failed
    and there are two, because a directory with two browsers is held whichever
    one is named. A member whose argv could not be READ leaves the question
    open, and an open question resolves toward HELD here exactly as it does in
    :func:`_pid_alive`: one extra walk is survivable, two browsers on one
    profile is not. Only "asked every member, none is a browser" is a negative,
    and it is the one the caller may act on.
    """
    members = browser_cmdline.browser_members(pids, str(profile_dir))
    if members.browsers:
        pid = min(members.browsers)
        return Hold(pid, f"a live browser process (pid {pid}) has this profile open")
    if members.unreadable:
        pid = min(pids)
        return Hold(
            pid,
            f"a live process (pid {pid}) on this profile could not be read, so "
            "this profile cannot be shown free",
        )
    return None


def _browser_pids(profile_dir: Path, live_pids: PidScan) -> Collection[int] | None:
    """The process table's answer about *profile_dir*.

    Three outcomes, and the caller needs all three: the pids found, ``()`` for
    "asked, and nothing is running", and ``None`` for "could NOT be asked" —
    which on Windows is the whole difference between free and unknowable,
    because there is no second witness there.

    A ``live_pids`` that is absent entirely is a replaced collaborator, not a
    runtime failure, and stays ``()``.
    """
    if not callable(live_pids):
        return ()
    answered = False
    rejected: TypeError | None = None
    for candidate in (str(profile_dir), profile_dir):
        try:
            found = live_pids(candidate)
        except TypeError as error:
            # The scan may take only one of str/Path. Remembered, not dropped: a
            # TypeError raised INSIDE a working scan would otherwise read as "no
            # browsers", so exhausting both forms is warned about below.
            rejected = error
            continue
        except (psutil.Error, OSError, AttributeError) as error:
            debug_logger.log_warning(
                "profile_lock",
                "pids",
                f"PID check failed for profile {profile_dir}: {error!r}",
            )
            return None
        answered = True
        if found:
            return found
    if not answered:
        debug_logger.log_warning(
            "profile_lock",
            "pids",
            f"PID check rejected both a str and a Path for {profile_dir}, so it "
            f"was never really asked: {rejected!r}",
        )
        return None
    return ()


def _lock_hold(lock: Path) -> Hold | None:
    """Chrome's singleton lock, read the way Chrome reads it."""
    owner = _lock_owner(lock)
    if owner is None:
        return None
    hostname, pid = owner
    if not hostname:
        # Chromium: "Invalid lockfile" -> UnlinkPath -> PROCESS_NONE.
        return None
    if hostname != _this_host():
        return Hold(
            pid if pid > 0 else None,
            f"Chrome's {LOCK_NAME} names another host ({hostname}), so this "
            "profile cannot be shown free from here",
        )
    if pid > 0 and _pid_alive(pid):
        return Hold(pid, f"Chrome's {LOCK_NAME} is held by live pid {pid}")
    # Chromium: "Orphaned lockfile" -> UnlinkPath -> PROCESS_NONE.
    return None


def _lock_owner(lock: Path) -> tuple[str, int] | None:
    """``(hostname, pid)`` from the lock, or ``None`` when there is no lock.

    ``pid`` is ``-1`` when the content does not end in one, and *hostname* is
    empty when there is no delimiter at all — both exactly what Chromium's
    ``ParseProcessSingletonLock`` produces for the same bytes.
    """
    content = _lock_content(lock)
    if not content:
        return None
    hostname, delimiter, pid_text = content.rpartition(_DELIMITER)
    if not delimiter:
        return ("", -1)
    try:
        return (hostname, int(pid_text))
    except ValueError:
        return (hostname, -1)


def _lock_content(lock: Path) -> str:
    """The lock's bytes, whether Chrome's symlink or a plain file.

    Chrome only ever writes a symlink; a plain file is read too because
    creating a symlink is privileged on Windows (so our own tests write one)
    and a `core.symlinks=false` checkout materializes symlinks as files. The
    bytes mean the same thing either way, and a real Chrome lock is unaffected.
    """
    try:
        if lock.is_symlink():
            return str(lock.readlink()).strip()
        if lock.is_file():
            return lock.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as error:
        debug_logger.log_warning(
            "profile_lock",
            "read",
            f"Could not read {lock}: {error}",
        )
    return ""


def _this_host() -> str:
    try:
        return socket.gethostname()
    except OSError as error:
        debug_logger.log_warning(
            "profile_lock",
            "hostname",
            f"Could not read this machine's hostname: {error}",
        )
        return ""


def _pid_alive(pid: int) -> bool:
    """Whether *pid* is running. A pid we cannot ask about counts as alive:
    the profile is then not SHOWN free, which is the survivable answer."""
    try:
        return psutil.pid_exists(pid)
    except (psutil.Error, OSError) as error:
        debug_logger.log_warning(
            "profile_lock",
            "pid_alive",
            f"Could not tell whether pid {pid} is running: {error}",
        )
        return True
