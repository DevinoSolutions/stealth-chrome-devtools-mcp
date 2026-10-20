"""THE one home for INTERPRETING Chrome's process-singleton artefacts — "is this
profile held by a live process, and who holds it" (F-871).

Interpreting, not owning the names: `profile_copy.REGENERABLE_NAMES`
also lists `SingletonLock`/`SingletonSocket`/`SingletonCookie` (and `lockfile`),
for the different question of what a clone must not copy and what a trim may
delete. That list is about bytes on disk; this module is about what they MEAN.

Two witnesses of Chrome's, in order, then one of OURS (F-1008):

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
3. **Our own launch marker**, ``LAUNCH_NAME``, which :func:`launching` writes
   in the profile for exactly as long as OUR launcher is starting a browser on
   it. Neither witness above can see a browser that is still starting: the scan
   matches a command line, and on Linux the launcher ``exec``s the real binary
   under the same pid, which reads an EMPTY argv meanwhile (F-998); Chrome
   writes ``SingletonLock`` 0.1-0.6 s after that. F-998's review measured 4
   FREE polls of this function inside one launch, so for that window a second
   launch, copy or trim could take a profile a browser was starting on. The
   marker is not a presence test either: it holds the same ``<hostname>-<pid>``
   Chrome's lock does and is read by the same code, naming the LAUNCHER (this
   backend), and it is dropped once it is older than a launch may last
   (``spawn_budget.LAUNCH_BUDGET_SECONDS``) — so a backend killed mid-launch
   leaves nothing that holds a profile for longer than one launch could.
   Only our own launches write it; a Chrome started by hand is still seen only
   once it is visible to the two witnesses above.

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
forever with no pid in it to check. Nor is it PROBED (F-1008): Chrome opens it
sharing READ only, so the one open that could tell a held ``lockfile`` from a
stale one must ask for an access Chrome refuses to share, and for as long as
that probe's handle is open a Chrome starting on the profile fails its own
``CreateFile`` and exits as "profile in use". What the missing second witness
DOES change is the direction an unreadable answer resolves in: see
``_browser_pids``. Our launch marker (3) is read on Windows as everywhere.

**Deliberately NOT here: clearing a stale lock.** Chromium unlinks an orphaned
lock itself on the next launch, so deleting one from our side would be a second
way to do something already done — and doing it from a failure handler would
race a browser that is still dying. Reading is enough; F-860's reaper
(``spawn_leak``) keeps killing pids and leaves the artefacts alone.

**One deliberate divergence from Chromium**: it additionally requires the lock's
pid to BE a browser (``IsChromeProcess``); we ask only that the pid is alive.
A recycled pid then costs a refusal or a walk (since F-914 a named session is
refused rather than walked), whereas calling a live browser's profile free
costs a corrupted profile. The error is taken in the survivable direction, and
for the same reason a lock that EXISTS but cannot be read is a hold, and so is
a pid ``/proc`` hides from us (``hidepid=2``) that the kernel still has
(F-1008). A pid too large for Chromium's ``StringToInt`` is parsed the way
Chromium parses it, as no pid and so an orphaned lock, instead of raising.

Never raises: every caller is deciding where to put a profile, and a failure to
read a lock is not a reason to fail a spawn.
"""

from __future__ import annotations

import contextlib
import os
import socket
import stat
import threading
import time
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path

import psutil

from stealth_chrome_devtools_mcp.embedded import browser_cmdline, spawn_budget
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

LOCK_NAME = "SingletonLock"
_DELIMITER = "-"

# Witness 3 (F-1008). The `.lock` suffix keeps it, and the `.tmp` it is written
# through, out of every profile copy (`profile_copy.ignore_names`).
LAUNCH_NAME = "stealth-launch.lock"
_LAUNCH_WHOSE = "the launch marker of a browser still starting"
# In-process launches per marker path: two spawns of ONE backend on one profile
# write the same `<hostname>-<pid>`, so the first to finish must not remove it.
_LAUNCHING: dict[str, int] = {}
_LAUNCHING_LOCK = threading.Lock()

# The largest pid Chromium's `StringToInt` parses; above it a lock names no pid.
_PID_MAX = 2**31 - 1

# Whether `os.kill(pid, 0)` is a QUESTION: on Windows any signal but the console
# ones terminates the process.
_SIGNAL_ASKS = os.name != "nt"

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

    ``members`` is the process set this answer was read FROM — empty when the
    answer came from the lock rather than the process table (F-931 M2). It
    exists so a consumer asking a DIFFERENT question of the same tree — which
    member is the browser, and is there exactly one
    (``browser_reattach.held_by``) — does not walk every process on the machine
    a second time for a set this module has just read. It is the input to that
    question and never its answer: who the browser is stays
    ``browser_cmdline``'s to decide.
    """

    pid: int | None
    reason: str
    members: tuple[int, ...] = ()


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
    return _lock_hold(profile_dir / LOCK_NAME) or _launch_hold(
        profile_dir / LAUNCH_NAME
    )


@contextlib.contextmanager
def launching(profile_dir: str | os.PathLike[str] | None) -> Iterator[None]:
    """Hold *profile_dir* for the browser launch inside this block (F-1008).

    Witness 3 of the module docstring: written before the block, removed after
    it whether the launch returned or raised. By then a started browser is seen
    by witnesses 1 and 2 — nodriver returns only once Chrome answers on its
    debugging port, which it opens after taking its own singleton. A directory
    that does not exist yet is skipped: there is nothing on disk to lose. Never
    raises: a marker that cannot be written costs this protection, not a spawn.
    """
    marker = _claim_launch(profile_dir)
    try:
        yield
    finally:
        _release_launch(marker)


def _launch_owner() -> str:
    return f"{_this_host()}{_DELIMITER}{os.getpid()}"


def _launch_key(marker: Path) -> str:
    return os.path.normcase(os.path.abspath(marker))  # noqa: PTH100  PERMANENT(a path comparison key, not a filesystem read)


def _claim_launch(profile_dir: str | os.PathLike[str] | None) -> Path | None:
    if not profile_dir or not Path(profile_dir).is_dir():
        return None
    marker = Path(profile_dir) / LAUNCH_NAME
    with _LAUNCHING_LOCK:
        count = _LAUNCHING.get(_launch_key(marker), 0)
        _LAUNCHING[_launch_key(marker)] = count + 1
        if count:
            return marker
        # Through a scratch file and one rename, so a reader never sees the
        # marker EMPTY, which reads as no marker at all.
        scratch = marker.with_name(f"{LAUNCH_NAME}.{os.getpid()}.tmp")
        try:
            scratch.write_text(_launch_owner(), encoding="utf-8")
            scratch.replace(marker)
        except OSError as error:
            debug_logger.log_warning(
                "profile_lock", "launching", f"Could not write {marker}: {error}"
            )
            with contextlib.suppress(OSError):
                scratch.unlink()
    return marker


def _release_launch(marker: Path | None) -> None:
    if marker is None:
        return
    with _LAUNCHING_LOCK:
        count = _LAUNCHING.pop(_launch_key(marker), 1) - 1
        if count > 0:
            _LAUNCHING[_launch_key(marker)] = count
            return
        # Only OUR marker: another backend's launch may have written its own since.
        if _lock_content(marker) != _launch_owner():
            return
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            debug_logger.log_warning(
                "profile_lock", "launching", f"Could not remove {marker}: {error}"
            )


def _launch_hold(marker: Path) -> Hold | None:
    """Witness 3: our own launch marker, read the way Chrome's lock is and
    dropped once it is older than a launch may last. Its pid is the LAUNCHER,
    never a browser, so the hold names none: a consumer that reads a hold's pid
    as a browser's (``browser_reattach.held_by``) must not be handed one."""
    held = _lock_hold(marker, _LAUNCH_WHOSE)
    if held is None:
        return None
    try:
        age = time.time() - marker.lstat().st_mtime
    except FileNotFoundError:
        return None  # the launch finished between the two reads
    except OSError:
        age = 0.0  # unanswerable, so HELD
    if age > spawn_budget.LAUNCH_BUDGET_SECONDS:
        return None
    return Hold(None, held.reason)


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

    **Each sentence names a pid it is true of** (F-931 M1): the unreadable
    branch reports one of the pids that could not be READ, never ``min(pids)``
    over the whole scan, which routinely named a member we had identified
    perfectly well as a child. That sentence is quoted verbatim by F-914's
    refusal, so a pid it is not about sends an operator after the wrong process.
    """
    members = browser_cmdline.browser_members(pids, str(profile_dir))
    # Ints only, `browser_members`' own filter: `Hold.members` is typed as pids.
    scanned = tuple(pid for pid in pids if isinstance(pid, int))
    if members.browsers:
        pid = min(members.browsers)
        return Hold(
            pid,
            f"a live browser process (pid {pid}) has this profile open",
            members=scanned,
        )
    if members.unreadable:
        pid = min(members.unreadable)
        return Hold(
            pid,
            f"a live process (pid {pid}) on this profile could not be read, so "
            "this profile cannot be shown free",
            members=scanned,
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


def _lock_hold(lock: Path, whose: str = f"Chrome's {LOCK_NAME}") -> Hold | None:
    """Chrome's singleton lock, read the way Chrome reads it. *whose* names the
    lock in the sentence, because witness 3 is read by this same code."""
    content = _lock_content(lock)
    if content is None:
        # It EXISTS and could not be read: unanswerable, so HELD (F-1008). It
        # used to read as no lock at all.
        return Hold(
            None, f"{whose} could not be read, so this profile cannot be shown free"
        )
    owner = _lock_owner(content)
    if owner is None:
        return None
    hostname, pid = owner
    if not hostname:
        # Chromium: "Invalid lockfile" -> UnlinkPath -> PROCESS_NONE.
        return None
    if hostname != _this_host():
        return Hold(
            pid if pid > 0 else None,
            f"{whose} names another host ({hostname}), so this "
            "profile cannot be shown free from here",
        )
    if pid > 0 and _pid_alive(pid):
        return Hold(pid, f"{whose} is held by live pid {pid}")
    # Chromium: "Orphaned lockfile" -> UnlinkPath -> PROCESS_NONE.
    return None


def _lock_owner(content: str) -> tuple[str, int] | None:
    """``(hostname, pid)`` from a lock's *content*, or ``None`` when it is empty.

    ``pid`` is ``-1`` when the content does not end in one, and *hostname* is
    empty when there is no delimiter at all — both exactly what Chromium's
    ``ParseProcessSingletonLock`` produces for the same bytes. Its
    ``StringToInt`` takes ASCII digits that fit an ``int``, so anything else is
    no pid either — including one too large for an ``int``, which reached
    ``psutil`` and raised ``OverflowError`` out of a function that never raises
    (F-1008).
    """
    if not content:
        return None
    hostname, delimiter, pid_text = content.rpartition(_DELIMITER)
    if not delimiter:
        return ("", -1)
    if not (pid_text.isascii() and pid_text.isdigit()) or int(pid_text) > _PID_MAX:
        return (hostname, -1)
    return (hostname, int(pid_text))


def _lock_content(lock: Path) -> str | None:
    """The lock's bytes, whether Chrome's symlink or a plain file: ``""`` when
    there is no lock, ``None`` when there IS one that could not be read.

    Chrome only ever writes a symlink; a plain file is read too because
    creating a symlink is privileged on Windows (so our own tests write one)
    and a `core.symlinks=false` checkout materializes symlinks as files. The
    bytes mean the same thing either way, and a real Chrome lock is unaffected.

    One ``lstat`` decides "absent", and only ``FileNotFoundError`` or
    ``NotADirectoryError`` says it (F-1008). ``Path.is_symlink`` asked before,
    and depending on the Python version it raised a ``PermissionError`` into
    the handler below or answered False for one; both read an unreadable lock
    as NO lock, and so the profile as free.
    """
    try:
        mode = lock.lstat().st_mode
        if stat.S_ISLNK(mode):
            return str(lock.readlink()).strip()
        if stat.S_ISREG(mode):
            return lock.read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, NotADirectoryError):
        return ""  # never there, or gone since: no lock
    except OSError as error:
        debug_logger.log_warning(
            "profile_lock",
            "read",
            f"Could not read {lock}: {error}",
        )
        return None
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
    the profile is then not SHOWN free, which is the survivable answer. A pid
    whose command line has GONE is not running (F-998), though it is still
    listed: it is the lock's owner in the window between a browser's death and
    its reaping, and reading it as a holder walked a relaunch of ``fleet`` to
    ``fleet-2``. A pid the process table does not list is still asked of the
    kernel (F-1008): see :func:`_kernel_has`."""
    try:
        if not psutil.pid_exists(pid):
            return _kernel_has(pid)
        return not browser_cmdline.command_line_is_gone(pid)
    except (psutil.Error, OSError, OverflowError) as error:
        debug_logger.log_warning(
            "profile_lock",
            "pid_alive",
            f"Could not tell whether pid {pid} is running: {error}",
        )
        return True


def _kernel_has(pid: int) -> bool:
    """Whether the kernel still has *pid* though ``psutil`` did not list it.

    ``psutil`` answers from ``/proc``, and ``/proc`` mounted ``hidepid=2`` lists
    only the caller's own processes, so another user's live browser read as
    gone and its lock as orphaned (F-1008). ``kill(pid, 0)`` delivers nothing
    and asks the kernel instead: ``EPERM`` is a process that exists and is not
    ours. POSIX only — on Windows ``os.kill`` terminates, and Chrome writes no
    lock there to ask about.
    """
    if not _SIGNAL_ASKS:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
