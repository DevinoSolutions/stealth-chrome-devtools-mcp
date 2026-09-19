"""THE one home for the ``browser_pids.json`` tracking record: its schema, the
owner identity stamped on every entry, and the read-merge-write protocol every
writer shares.

Moved out of ``process_cleanup.py`` (plan_F808 Task 10), which keeps the reaping
POLICY — which processes to kill, which profile directories to remove — and
passes this module the record's path.

The record is SHARED. One machine can run several backends at once (Task 3 keys
``server.json`` by display context precisely so that it can), and every one of
them tracks its browsers in this single file. Two rules follow, and F-808 is
what the file did without them.

**Every write merges.** A writer replaces only the entries it is writing and
leaves every other entry exactly as it found them; deletions name their instance
ids rather than happening by omission. The pre-F-808 writer serialised its whole
in-memory table over the file, so the last backend to track or untrack a browser
erased every other backend's tracking, and those browsers then leaked with
nothing left on disk to find them by.

**Ownership is recorded, not inferred.** Each entry carries the pid and
create_time of the process that wrote it, and startup recovery reaps an entry
only when no live owner still holds it. The old recovery instead spared
processes younger than its own import — which every already-running backend's
browsers necessarily fail — so a second backend starting killed the first one's
browsers, deleted their profiles, and wiped the record. An entry written before
this release carries no owner keys at all and reads as unowned: deliberate, and
it is what makes an upgrade from 2.0.3 reclaim its predecessor's orphans rather
than adopt them forever.

The path is a required argument on every public function, never a module global
with a default. The caller's binding is what selects the file, so the tests'
``pc.pid_file`` redirection — the only thing keeping a test run out of the
developer's live ``~/.stealth-mcp/browser_pids.json`` — reaches every read and
every write. Same corollary as :mod:`backend_registry`, pinned the same way in
``test_browser_pid_registry.py``.

A leaf module: stdlib plus ``debug_logger``. Never ``process_cleanup``, never
``singleton``, never ``server``.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from typing import TYPE_CHECKING, TextIO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

# One tracked browser, and the table of them keyed by instance id. `object`
# rather than a precise union, exactly as `backend_registry.BackendEntry` is:
# the values are deliberately heterogeneous, the record tolerates a legacy
# schema and a hand edit, and every consumer already re-checks the field it
# wants — which a validating model would replace with an exception at the one
# moment orphan recovery must not be able to fail.
Entry = dict[str, object]
Entries = dict[str, Entry]

OWNER_PID = "owner_pid"
OWNER_CREATE_TIME = "owner_create_time"

# The record's FILE NAME, and only the name: every public function still takes
# the whole path, so nothing here binds a state dir at import time and the
# tests' path redirection still reaches every read and write (see the module
# docstring). The name is here because two modules now join it to a state dir —
# `process_cleanup`, which owns the reaping policy, and `backend_eviction`,
# which asks this record whether a backend is still serving (F-886) — and a
# string literal spelled in both is a second place to rename it from.
RECORD_NAME = "browser_pids.json"

# Bounded non-blocking acquire, then raise (F-607): never yield as if the lock
# were held. Both callers already log and degrade, so raising turns a silent
# cross-process race into a logged skipped write.
#
# A DEADLINE rather than a fixed handful of tries, because a skipped write is
# not a cheap failure here: the browser it was recording goes untracked, which
# is the leak this module exists to prevent. The lock spans a read, a merge and
# an atomic replace — milliseconds — but several backends spawn and close
# browsers at once, so waiting out a normal collision has to be the common case
# and only a genuinely stuck holder may exhaust the budget. Measured, not
# guessed: the inherited 4 x 50ms was sized for a critical section that was one
# json.dump long, and three processes writing this record concurrently lost 120
# of 180 entries to it.
_LOCK_TIMEOUT = 5.0
_LOCK_RETRY_DELAY = 0.01
# Windows refuses an atomic replace while another process holds the target open;
# a few short retries outlast that window. See _replace.
_COMMIT_ATTEMPTS = 5
_COMMIT_RETRY_SECONDS = 0.02


def normalize_path(path: str | None) -> str | None:
    """Normalize a filesystem path for safe comparison, or None.

    Lives here because the recorded ``user_data_dir`` is normalized on the way
    in — the record's own schema — and profile matching elsewhere must ask the
    same question the same way.
    """
    if not path:
        return None
    return os.path.normcase(os.path.normpath(str(path)))


def new_entry(  # noqa: PLR0913  PERMANENT(one parameter per recorded field; folding them into a struct would put the schema in a second place)
    pid: int,
    *,
    create_time: float | None,
    user_data_dir: str | None,
    uses_custom_data_dir: bool | None,
    auto_clone: bool,
    cdp_port: int | None = None,
) -> Entry:
    """One tracked browser as it is first recorded.

    Building an entry lives here, next to :func:`normalize_entries` that reads
    one back, because the two are the same schema seen from either end and a
    disagreement between them is silent: the normalizer drops every key it does
    not know, so a field added at a tracking site would survive one process
    lifetime and vanish on the next load with nothing red.

    ``cdp_port`` is the browser's DevTools port (F-888). It is recorded because
    it is the one thing a LATER backend needs to reach a browser this one
    spawned, and nothing else on disk necessarily has it: it lives in
    ``browser.config.port``, in memory, in the process that dies. It defaults to
    None so a caller that cannot learn it records the absence rather than a lie —
    ``browser_reattach.endpoint`` then falls back to Chrome's own
    ``DevToolsActivePort`` and to the command line, which is also what serves
    every entry written before this release.

    The owner is deliberately NOT set here. :func:`with_owner` stamps it at
    write time, which is the only moment that knows which process is writing.
    """
    return {
        "pid": pid,
        "create_time": create_time,
        "user_data_dir": normalize_path(user_data_dir),
        "uses_custom_data_dir": uses_custom_data_dir,
        "auto_clone": bool(auto_clone),
        "cdp_port": cdp_port if isinstance(cdp_port, int) else None,
        "timestamp": time.time(),
    }


def normalize_entries(raw: object) -> Entries:
    """Normalize a raw recorded table — legacy or current — into today's shape.

    The key set is fixed and everything unrecognised is dropped, so a field only
    survives a round trip if this function knows it by name — which is why
    :func:`new_entry` is its neighbour, and why a pin asserts the two agree on
    the key set. That is also why the owner keys are copied explicitly: adding
    them at the write site alone would have them vanish on the very next load,
    and the owner check would then silently never fire.

    A legacy entry keeps NO owner keys rather than gaining null ones. The
    distinction is load-bearing: :func:`is_reapable` decides on the key's
    presence, while a recorded null ``owner_create_time`` is a legitimate value
    meaning psutil could not read it.
    """
    normalized: Entries = {}
    if not isinstance(raw, dict):
        return normalized

    for instance_id, value in raw.items():
        if isinstance(value, int):
            metadata: Entry = {
                "pid": value,
                "create_time": None,
                "user_data_dir": None,
                "uses_custom_data_dir": None,
                "auto_clone": False,
                "cdp_port": None,
                "timestamp": 0,
            }
        elif isinstance(value, dict):
            # Re-key to str up front: JSON object keys always are, and it is
            # what lets the rest of this branch read fields by name.
            recorded: Entry = {str(k): v for k, v in value.items()}
            pid = recorded.get("pid")
            if not isinstance(pid, int):
                continue
            recorded_dir = recorded.get("user_data_dir")
            recorded_cdp_port = recorded.get("cdp_port")
            metadata = {
                "pid": pid,
                "create_time": recorded.get("create_time"),
                "user_data_dir": normalize_path(
                    recorded_dir if isinstance(recorded_dir, str) else None
                ),
                "uses_custom_data_dir": recorded.get("uses_custom_data_dir"),
                "auto_clone": bool(recorded.get("auto_clone", False)),
                # Narrowed on the way in, unlike the owner keys below: an absent
                # port and an unusable one mean the same thing here — ask the
                # other two witnesses — so there is nothing for a caller to tell
                # apart and no reason to make every reader re-check a bool.
                "cdp_port": (
                    recorded_cdp_port
                    if isinstance(recorded_cdp_port, int)
                    and not isinstance(recorded_cdp_port, bool)
                    else None
                ),
                "timestamp": recorded.get("timestamp", 0),
            }
            for key in (OWNER_PID, OWNER_CREATE_TIME):
                if key in recorded:
                    metadata[key] = recorded[key]
        else:
            continue

        normalized[str(instance_id)] = metadata

    return normalized


def with_owner(entry: Entry, owner_pid: int, owner_create_time: float | None) -> Entry:
    """A copy of *entry* stamped with the process that is recording it.

    Stamped at write time rather than at track time so there is one place the
    owner can come from; an in-memory table that carried its own copy could
    drift from the process actually holding the browser.
    """
    return {**entry, OWNER_PID: owner_pid, OWNER_CREATE_TIME: owner_create_time}


def is_reapable(entry: Entry, owner_alive: Callable[[int, float | None], bool]) -> bool:
    """True when no live owner holds *entry*, so startup recovery may take it.

    The two questions are asked in this order deliberately. An entry with no
    recorded owner is unowned, full stop, and ``owner_alive`` is never consulted
    for it — otherwise a legacy entry's absent owner_create_time would reach a
    liveness check whose None-tolerance answers "alive", and every orphan left
    by 2.0.3 would be adopted rather than reaped.

    Only then does the recorded owner's liveness decide. ``owner_alive`` is
    injected rather than implemented here: answering it needs psutil and
    ``singleton``'s backend-identity check, and this module imports neither.
    """
    owner_pid = entry.get(OWNER_PID)
    if not isinstance(owner_pid, int):
        return True
    return not owner_alive(owner_pid, recorded_time(entry, OWNER_CREATE_TIME))


def on_persistent_profile(entry: Entry) -> bool:
    """True when *entry*'s profile directory OUTLIVES its browser.

    THE one home for that question (F-888). It was written out by hand in three
    places in ``process_cleanup`` — the delete guard, the untrack decision and,
    since F-888, the shutdown spare — and the three have to agree: a directory
    spared from deletion whose entry is dropped anyway is a browser nothing can
    find again, and a browser spared at shutdown whose directory is then deleted
    is the incident this finding is about with an extra step.

    Two conditions and both are needed. ``uses_custom_data_dir`` alone is True
    for every spawn since the resolver started handing back an explicit path, so
    it separates nothing on its own; ``auto_clone`` is what marks the disposable
    per-session clone that is MEANT to die with its browser. A legacy entry
    carrying neither key reads as NOT persistent, which is 2.0.3's behaviour and
    the safe direction here: the worst case is a temp profile reclaimed, where
    the other way round is a named profile deleted.
    """
    return bool(
        entry.get("uses_custom_data_dir") is True and not entry.get("auto_clone")
    )


# One past the highest TCP port. Chrome never binds 0 for DevTools — it resolves
# an ``=0`` request to a real port before reporting it — so 0 is "not bound yet"
# and is rejected with everything else out of range.
_PORT_CEILING = 65536


def valid_port(value: object) -> int | None:
    """*value* as a usable TCP port, or None (F-888).

    THE one home for that question, because three witnesses ask it about three
    different shapes — this record's ``cdp_port``, a line of Chrome's
    ``DevToolsActivePort`` and a ``--remote-debugging-port`` argument — and a
    second copy of the range is a second place it can drift.

    ``bool`` is excluded explicitly rather than incidentally: it is an ``int``
    subclass, so a hand-edited ``true`` would otherwise read as port 1.
    """
    if isinstance(value, bool):
        return None
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 0 < port < _PORT_CEILING else None


def recorded_port(entry: Entry) -> int | None:
    """The DevTools port *entry* carries, or None when it names none (F-888)."""
    return valid_port(entry.get("cdp_port"))


def recorded_time(entry: Entry, key: str) -> float | None:
    """One recorded timestamp off an entry, or None when it is absent or is not
    a number.

    The same narrowing ``backend_registry.recorded_int`` does, for the same
    reason: this record tolerates a legacy schema and a hand edit, so every
    field read must re-check its own type. None is a real answer here, not a
    failure — the identity tolerance reads it as "cannot tell", so it has to
    arrive as None rather than as whatever the file happened to say.
    """
    value = entry.get(key)
    return float(value) if isinstance(value, int | float) else None


def read_entries(path: Path) -> Entries:
    """Every tracked browser recorded at *path*, normalized; ``{}`` when the
    record is absent or unreadable.

    Takes no lock, and does not need one: :func:`update_entries` publishes by
    atomic replace, so a reader sees the whole previous record or the whole new
    one, never a half-written file. Never raises — an unreadable record must
    degrade to "nothing tracked" rather than fail a backend's startup.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        debug_logger.log_warning(
            "browser_pid_registry",
            "read_entries",
            f"Failed to read PID file {path}: {error}",
        )
        return {}

    try:
        data = json.loads(raw)
    except ValueError as error:
        debug_logger.log_warning(
            "browser_pid_registry",
            "read_entries",
            f"Failed to parse PID file {path}: {error}",
        )
        return {}

    if not isinstance(data, dict):
        return {}
    return normalize_entries(data.get("browser_processes", {}))


def update_entries(path: Path, mutate: Callable[[Entries], Entries]) -> None:
    """Apply *mutate* to the record's current contents and publish the result,
    holding the record's lock across the whole read-modify-write.

    The lock lives in a SIBLING file and the new contents land by atomic
    replace. Both halves are needed and neither is ceremony. Locking the record
    itself and then replacing it would leave the next writer locking an unlinked
    inode — no exclusion at all. Rewriting the record in place instead would
    mean truncating it, and a truncate necessarily precedes the write that
    refills it: that ordering is exactly how the pre-F-808 writer emptied the
    file whenever the lock it reached for AFTERWARDS could not be had.

    Raises rather than swallowing. Callers log and degrade to a skipped write,
    which is a state the next write repairs; a silent failure here is how the
    record drifts away from the browsers actually running.
    """
    with _hold_lock(path):
        _write(path, mutate(read_entries(path)))


def _lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


@contextlib.contextmanager
def _hold_lock(path: Path) -> Iterator[None]:
    """Hold the record's exclusive lock for the duration of the block, or raise."""
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    # "a" creates the file without truncating it. Nothing is ever written here
    # so the file stays empty and "w" would behave identically today; "a" is
    # simply the honest mode for a handle we open only to lock. (singleton's
    # cold-start lock uses "w" — same idea, no difference in effect.)
    with lock_file.open("a") as handle:
        # The lock is one byte at offset 0, so the position must be 0 whatever
        # append mode opened it at.
        handle.seek(0)
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                _acquire(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LOCK_RETRY_DELAY)
        try:
            yield
        finally:
            _release(handle)


def _acquire(handle: TextIO) -> None:
    if sys.platform == "win32":
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(handle: TextIO) -> None:
    try:
        if sys.platform == "win32":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError as error:
        # The handle closes either way, which drops the lock; a refused explicit
        # release is not actionable, only worth seeing.
        debug_logger.log_warning(
            "browser_pid_registry", "release_lock", f"Failed to release lock: {error}"
        )


def _write(path: Path, entries: Entries) -> None:
    """Write the record atomically: stage into a sibling temp file, then commit.

    So a reader concurrent with a write sees the whole old record or the whole
    new one, and a crash mid-write cannot leave the tracking table unparseable —
    which, for this record, would mean every backend's browsers untracked at
    once.

    DUPLICATION, stated rather than implied: this function and
    ``backend_registry._write`` are the same function apart from the payload
    they serialise, and :func:`_commit` and ``backend_registry._commit`` are the
    same function outright — same mkdir, same pid-suffixed temp name, same
    retrying replace, same BaseException cleanup, same two constants. What
    genuinely differs is only the caller's concurrency: this record is merged
    under :func:`_hold_lock` per write, that one is written whole under
    singleton's cold-start lock. Deliberately NOT extracted here — two
    occurrences is not yet a home, and the rule of three is the trigger.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # pid-suffixed so two processes staging at once cannot collide on the temp
    # name; the same directory so the replace stays within one filesystem.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps({"browser_processes": entries, "timestamp": time.time()}),
            encoding="utf-8",
        )
        _commit(tmp, path)
    except BaseException:
        # ACCEPTED GAP: cleans up a failed write, but a process killed between
        # write_text and the replace leaves a .tmp nothing sweeps. Bounded and
        # harmless — one small file per killed writer, in a directory read only
        # by exact name.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _commit(tmp: Path, path: Path) -> None:
    """Move *tmp* onto *path*, retrying briefly on a Windows sharing refusal.

    ``Path.replace`` is atomic on both platforms, but on Windows it raises
    PermissionError while ANOTHER process has the target open — a concurrent
    reader is enough, since Python's ``open()`` does not pass FILE_SHARE_DELETE.
    The window is microseconds, so a few short retries convert a spurious hard
    failure into a slightly later success. On POSIX this costs one iteration and
    never sleeps.

    Named to match ``backend_registry._commit``, which it duplicates line for
    line, so a grep for either lands on both. See :func:`_write` for why the
    pair is left unshared for now.
    """
    for attempt in range(_COMMIT_ATTEMPTS):
        try:
            tmp.replace(path)
        except PermissionError:
            if attempt == _COMMIT_ATTEMPTS - 1:
                raise
            time.sleep(_COMMIT_RETRY_SECONDS)
        else:
            return
