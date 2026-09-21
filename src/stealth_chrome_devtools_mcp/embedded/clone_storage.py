"""Profile and clone-storage subsystem for the embedded browser backend.

Owns the disposable-session lifecycle extracted verbatim from ``server.py``
(F-201): where every session, clone and seed directory IS, refreshing the seed,
per-session profile copying, the storage-cap sweep (idle auto-clone eviction
plus named-profile regenerable trim), the trash/retention mechanism, and
profile-selection resolution. Extracting it means a fault in storage GC can no
longer disable the whole tool surface. What a request MAY name, and what the
seed means, is ``profile_seed``'s; this module is the only thing that knows
where those directories live, and hands them over as ``profile_seed.Roots``.
HOW a profile directory is copied — what such a copy leaves behind and what it
does about a file Chrome holds open — is ``profile_copy``'s (F-897); what
stays here is the POLICY around a copy: which source, into which directory,
refused when, and reported how.

``server.py`` (the browser tools) and ``cli.py`` (the ops CLI) import this module
and call its public functions; ``spawn_browser`` delegates profile selection to
:func:`resolve_profile_selection`. Public functions drop the leading underscore;
internal-only helpers keep theirs.
"""

import asyncio
import hashlib
import itertools
import os
import re
import shutil
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stealth_chrome_devtools_mcp.embedded import (
    profile_copy,
    profile_lock,
    profile_seed,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings


def default_session_root() -> Path:
    root = get_settings().browser_session_root
    if root:
        return Path(root).expanduser()
    if os.name == "nt":
        return Path(r"C:\stealth-mcp-browser-sessions")
    return Path.home() / ".stealth-mcp-browser-sessions"


def master_profile_dir() -> Path:
    configured = get_settings().browser_master_user_data_dir
    if configured:
        return Path(configured).expanduser()
    return default_session_root() / "master"


def clone_root_dir() -> Path:
    configured = get_settings().browser_profile_clone_root
    if configured:
        return Path(configured).expanduser()
    return default_session_root() / "sessions"


def master_snapshot_dir() -> Path:
    configured = get_settings().browser_master_snapshot_dir
    if configured:
        return Path(configured).expanduser()
    return default_session_root() / "master-snapshot"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _profile_hold(profile_dir: Path) -> profile_lock.Hold | None:
    """What holds *profile_dir*, per the one home for that question (F-871)."""
    return profile_lock.profile_hold(
        profile_dir, getattr(process_cleanup, "_get_browser_pids_for_profile", None)
    )


def _profile_has_running_browser(profile_dir: Path) -> bool:
    return _profile_hold(profile_dir) is not None


def clone_storage_cap_bytes() -> int:
    """Cap (in bytes) on total auto-clone storage under the clone root.

    Default 10 GiB; override with ``STEALTH_MCP_CLONE_STORAGE_CAP_GB``. A value
    <= 0 disables the cap entirely. Only disposable auto-clones count against
    this — user-named/explicit profiles are never measured or reclaimed.
    """
    gb = get_settings().clone_storage_cap_gb
    if gb <= 0:
        return 0
    return int(gb * (1024**3))


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def clone_is_auto(clone_dir: Path) -> bool:
    """The disposable auto-clones — the marker's question, ``profile_seed``'s
    (its docstring carries the fail-safe-on-a-legacy-marker argument). A
    wrapper: every sweep, trim and CLI site reads this name."""
    return profile_seed.is_auto(clone_dir)


# ── Authoritative in-flight / live clone protection ──────────────────────────
# The storage-cap sweep picks reclaim targets from on-disk markers, but a clone
# becomes a valid target the instant its marker is written — which happens BEFORE
# its browser launches and is tracked. During that window the filesystem liveness
# heuristic (`_profile_has_running_browser`) reports "not running", so a
# concurrent or startup sweep could delete a live-but-not-yet-attached clone out
# from under the spawning browser (a silent, unlogged session loss). We therefore
# register every clone dir the spawn flow is about to write — before the marker
# exists — and clear it on close. The sweep skips any protected dir regardless of
# what the filesystem heuristic reports. Guarded by a lock because the sweep runs
# on a worker thread (`asyncio.to_thread`) while spawns run on the event loop.
_PROTECTED_CLONE_DIRS: set = set()
_PROTECTED_CLONE_DIRS_LOCK = threading.Lock()


def _normalize_clone_path(path) -> str:
    """Case/separator-normalized absolute path for protected-set membership."""
    return os.path.normcase(os.path.abspath(str(path)))


def _protect_clone_dir(path) -> None:
    """Shield a clone dir from the storage-cap sweep while it is in flight or
    live. Call BEFORE the clone's marker is written and keep it protected until
    the owning instance has closed."""
    with _PROTECTED_CLONE_DIRS_LOCK:
        _PROTECTED_CLONE_DIRS.add(_normalize_clone_path(path))


def _release_clone_dir(path) -> None:
    """Drop sweep protection for a clone dir once its instance has closed."""
    with _PROTECTED_CLONE_DIRS_LOCK:
        _PROTECTED_CLONE_DIRS.discard(_normalize_clone_path(path))


def _clone_dir_is_protected(path) -> bool:
    """True while a clone dir is registered as in-flight or live (sweep-exempt)."""
    with _PROTECTED_CLONE_DIRS_LOCK:
        return _normalize_clone_path(path) in _PROTECTED_CLONE_DIRS


def _clear_protected_clone_dirs() -> None:
    """Drop all sweep protection. For test isolation and full-shutdown cleanup."""
    with _PROTECTED_CLONE_DIRS_LOCK:
        _PROTECTED_CLONE_DIRS.clear()


# Evicted auto-clones are moved here — a rename within the clone root, so it is
# instant and same-volume — instead of being deleted outright, then purged only
# after a retention window. This turns a wrong eviction (the worst incident this
# project has had) into a recoverable event rather than irreversible data loss.
# The dir is excluded from every clone-root scan below so its contents are never
# re-selected, re-sized, or re-swept.
_CLONE_TRASH_DIRNAME = ".trash"


def _clone_trash_dir(clone_root: Path) -> Path:
    return clone_root / _CLONE_TRASH_DIRNAME


def _clone_trash_retention_seconds() -> float:
    """How long an evicted clone stays recoverable in ``.trash`` before purge.

    Default 24h; override with ``STEALTH_MCP_CLONE_TRASH_RETENTION_HOURS``. A
    value <= 0 purges on the next sweep, restoring the old delete-immediately
    behavior for anyone who wants it.
    """
    hours = get_settings().clone_trash_retention_hours
    return max(0.0, hours) * 3600.0


def _trash_clone(entry: Path, clone_root: Path):
    """Move an evicted auto-clone into ``.trash`` so it stays recoverable.

    Returns the new path on success, or ``None`` if the move was refused or the
    entry had to be deleted instead. A running profile is never moved (selection
    already excludes live sessions; this is belt-and-suspenders). If the rename
    fails (e.g. a Windows lock) the storage cap must still be honored, so we fall
    back to a best-effort delete — strictly no worse than the old behavior.
    """
    if _profile_has_running_browser(entry):
        return None
    trash = _clone_trash_dir(clone_root)
    try:
        trash.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    target = trash / entry.name
    counter = 1
    while target.exists():
        target = trash / f"{entry.name}-{counter}"
        counter += 1
    try:
        os.replace(str(entry), str(target))
    except OSError:
        _rmtree_robust(entry)
        return None
    try:
        # Stamp the trash time so retention is measured from eviction, not from
        # the clone's original creation (rename preserves the old mtime).
        os.utime(target, None)
    except OSError:
        pass
    return target


def _purge_expired_trash(clone_root: Path, max_age_seconds: float) -> int:
    """Delete trashed clones whose time-in-trash exceeds ``max_age_seconds``.

    Returns the count purged. Never raises; missing or non-dir trash is a no-op.
    """
    trash = _clone_trash_dir(clone_root)
    if not trash.exists():
        return 0
    try:
        entries = list(trash.iterdir())
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
        _rmtree_robust(entry)
        if not entry.exists():
            purged += 1
    return purged


def _idle_autoclones_over_cap(clone_root: Path, cap_bytes: int) -> list[Path]:
    """Oldest-first idle auto-clones whose removal brings total auto-clone
    storage within ``cap_bytes``. Read-only — selection only, no deletion.

    Named/explicit profiles, unmarked dirs, and clones a live browser is using
    are never selected. ``cap_bytes <= 0`` selects nothing. Shared by the live
    sweep and the CLI's dry-run so the two can never disagree.
    """
    if cap_bytes <= 0 or not clone_root.exists():
        return []

    autos = []  # (mtime, size, path)
    total = 0
    try:
        entries = list(clone_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name == _CLONE_TRASH_DIRNAME:
            continue  # recoverable-eviction holding area — never a clone itself
        try:
            if not entry.is_dir() or not clone_is_auto(entry):
                continue
            size = _dir_size_bytes(entry)
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        total += size
        autos.append((mtime, size, entry))

    if total <= cap_bytes:
        return []

    victims: list[Path] = []
    for _mtime, size, entry in sorted(autos, key=lambda item: item[0]):
        if total <= cap_bytes:
            break
        if _clone_dir_is_protected(entry) or _profile_has_running_browser(entry):
            continue  # never evict a live or in-flight session to satisfy the cap
        victims.append(entry)
        total -= size
    return victims


def _enforce_clone_storage_cap_in(
    clone_root: Path, cap_bytes: int, reason: str = ""
) -> int:
    """Evict the oldest idle auto-clones until total auto-clone storage under
    ``clone_root`` is within ``cap_bytes``. Returns the number of dirs evicted.
    Selection (and its safety invariants) lives in ``_idle_autoclones_over_cap``.

    Eviction is *recoverable*: victims are moved into ``.trash`` (see
    ``_trash_clone``) rather than deleted, and trash older than the retention
    window is purged first — so disk is reclaimed from expired trash before any
    live clone is touched.
    """
    _purge_expired_trash(clone_root, _clone_trash_retention_seconds())
    removed = 0
    for entry in _idle_autoclones_over_cap(clone_root, cap_bytes):
        if _clone_dir_is_protected(entry):
            # Protection acquired between selection and eviction (a spawn started
            # mid-sweep) — respect it rather than evict a now-in-flight clone.
            continue
        size = _dir_size_bytes(entry)
        _trash_clone(entry, clone_root)
        if not entry.exists():
            removed += 1
            debug_logger.log_info(
                "server",
                "clone_cap_sweep",
                f"evicted auto-clone {entry.name} ({size} bytes) to trash reason={reason}",
            )
    return removed


def browser_session_storage_cap_bytes() -> int:
    """Cap (bytes) on clone-root storage before idle *named* profiles are trimmed.
    Default 20 GiB; ``STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB`` (<= 0 disables)."""
    gb = get_settings().browser_session_storage_cap_gb
    if gb <= 0:
        return 0
    return int(gb * (1024**3))


def clone_is_named(clone_dir: Path) -> bool:
    """The persistent profiles — the marker's question, ``profile_seed``'s."""
    return profile_seed.is_named(clone_dir)


def _regenerable_dirs_in_profile(profile_dir: Path) -> list[Path]:
    """Regenerable cache/model directories in a profile — those named in
    ``profile_copy.REGENERABLE_NAMES``, at the profile root and one level down
    (``Default/``, ``Profile N/``), which is where Chrome keeps its caches and
    on-device model stores. Never recurses deeper, so session-state dirs such as
    ``Local Storage`` and ``IndexedDB`` are never included."""
    found: list[Path] = []

    def _scan(directory: Path) -> None:
        try:
            children = list(directory.iterdir())
        except OSError:
            return
        for child in children:
            try:
                if child.is_dir() and child.name in profile_copy.REGENERABLE_NAMES:
                    found.append(child)
            except OSError:
                continue

    _scan(profile_dir)
    try:
        subdirs = [
            c
            for c in profile_dir.iterdir()
            if c.is_dir() and c.name not in profile_copy.REGENERABLE_NAMES
        ]
    except OSError:
        subdirs = []
    for sub in subdirs:
        _scan(sub)
    return found


def _regenerable_size(profile_dir: Path) -> int:
    """Bytes a trim of ``profile_dir`` would reclaim (read-only)."""
    return sum(_dir_size_bytes(d) for d in _regenerable_dirs_in_profile(profile_dir))


def _trim_profile_regenerable(profile_dir: Path) -> int:
    """Delete the regenerable cache/model dirs from a profile (see
    ``_regenerable_dirs_in_profile``) while preserving every session-state file
    (cookies, logins, Web Data, Local Storage, Preferences). Returns bytes freed.
    """
    freed = 0
    for directory in _regenerable_dirs_in_profile(profile_dir):
        size = _dir_size_bytes(directory)
        _rmtree_robust(directory)
        if not directory.exists():
            freed += size
    return freed


def _named_profiles_over_session_cap(clone_root: Path, cap_bytes: int) -> list[Path]:
    """Largest-first idle named profiles whose trim brings total clone-root
    storage within ``cap_bytes``. Read-only — selection only.

    Auto-clones (the clone-cap sweep's job), unmarked dirs, and in-use profiles
    are never selected. ``cap_bytes <= 0`` selects nothing. Shared by the live
    sweep and the CLI's dry-run.
    """
    if cap_bytes <= 0 or not clone_root.exists():
        return []

    sized = []  # (size, path)
    total = 0
    try:
        entries = list(clone_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name == _CLONE_TRASH_DIRNAME:
            continue  # trashed clones are not named profiles and must not
            # inflate the session-cap total, or real profiles get over-trimmed
        try:
            if not entry.is_dir():
                continue
            size = _dir_size_bytes(entry)
        except OSError:
            continue
        total += size
        sized.append((size, entry))

    if total <= cap_bytes:
        return []

    victims: list[Path] = []
    for _size, entry in sorted(
        sized, key=lambda item: item[0], reverse=True
    ):  # largest first
        if total <= cap_bytes:
            break
        if not clone_is_named(entry) or _profile_has_running_browser(entry):
            continue  # autos -> clone-cap sweep; unmarked/in-use -> leave alone
        victims.append(entry)
        total -= _regenerable_size(entry)  # a trim frees ~the regenerable portion
    return victims


def _enforce_named_profile_trim_in(
    clone_root: Path, cap_bytes: int, reason: str = ""
) -> int:
    """Trim regenerable data from the largest idle named profiles until total
    clone-root storage is within ``cap_bytes``. Returns bytes freed. Selection
    (and its safety invariants) lives in ``_named_profiles_over_session_cap``.
    """
    freed_total = 0
    for entry in _named_profiles_over_session_cap(clone_root, cap_bytes):
        freed = _trim_profile_regenerable(entry)
        if freed:
            freed_total += freed
            debug_logger.log_info(
                "server",
                "profile_trim",
                f"trimmed {freed} bytes of regenerable data from {entry.name} reason={reason}",
            )
    return freed_total


def enforce_session_storage(reason: str = "") -> None:
    """Bound clone-root storage: delete idle auto-clones over the clone cap,
    then trim regenerable data from the largest idle named profiles over the
    session cap. Best-effort; never raises into a spawn or startup."""
    try:
        clone_root = clone_root_dir()
        _enforce_clone_storage_cap_in(clone_root, clone_storage_cap_bytes(), reason)
        _enforce_named_profile_trim_in(
            clone_root, browser_session_storage_cap_bytes(), reason
        )
    except Exception as error:
        debug_logger.log_warning(
            "server", "session_storage_sweep", f"sweep failed: {error}"
        )


# Strong refs to in-flight housekeeping sweeps so the event loop cannot GC them
# mid-run; the done-callback drops each when it finishes.
_BACKGROUND_SWEEPS: set = set()


def run_storage_sweep(
    clone_root: Path, clone_cap: int, session_cap: int, reason: str = ""
) -> None:
    """One reclaim pass over the clone root. Runs on a worker thread; never raises.

    Three steps: delete idle auto-clones over the clone cap, trim regenerable data
    from oversized idle named profiles, and finalize any *deferred* clone
    deletions. That last step matters: when a close cannot delete its clone in
    time (Windows still holding a file), the entry stays tracked until something
    drives the retry — and nothing else does between spawns. Driving it here keeps
    leaked clones from accumulating and holding the cap perpetually exceeded.

    ``cleanup_deferred_profiles`` only ever finalizes entries whose browser
    process is already gone, so it can never disturb a live or in-flight clone.
    """
    try:
        _enforce_clone_storage_cap_in(clone_root, clone_cap, reason)
        _enforce_named_profile_trim_in(clone_root, session_cap, reason)
        process_cleanup.cleanup_deferred_profiles()
    except Exception as error:
        debug_logger.log_warning(
            "server", "session_storage_sweep", f"sweep failed: {error}"
        )


def spawn_background_sweep(reason: str = "") -> None:
    """Kick the storage sweep off the event loop without blocking the caller.

    The clone root and caps are resolved now, at trigger time, and captured for
    the worker — so the sweep always targets the root that was active when it was
    triggered (this also keeps it hermetic when tests patch the env to a tmp
    dir). Deduped to one in-flight sweep, since sizing the clone root is the only
    real cost and running it concurrently with itself buys nothing.
    """
    if _BACKGROUND_SWEEPS:
        return
    clone_root = clone_root_dir()
    clone_cap = clone_storage_cap_bytes()
    session_cap = browser_session_storage_cap_bytes()

    task = asyncio.create_task(
        asyncio.to_thread(run_storage_sweep, clone_root, clone_cap, session_cap, reason)
    )
    _BACKGROUND_SWEEPS.add(task)
    task.add_done_callback(_BACKGROUND_SWEEPS.discard)


def _rmtree_robust(path: Path, retries: int = 3) -> None:
    """Remove a directory tree, handling Windows file-lock race conditions.

    Chrome profile dirs may have cache files vanishing mid-traversal
    (Chrome cleanup) or directories still locked by background processes.
    Retries with backoff and falls back to best-effort removal so the
    subsequent profile copy can proceed via overwrite.
    """

    def _on_rm_error(_func, fpath, exc_info):
        exc = exc_info[1]
        if isinstance(exc, FileNotFoundError):
            return  # file already gone — Chrome or OS cleaned it
        if isinstance(exc, PermissionError):
            try:
                os.chmod(fpath, 0o700)
                _func(fpath)
            except (OSError, FileNotFoundError):
                pass
            return
        # OSError (e.g. directory not empty) — let rmtree continue
        if isinstance(exc, OSError):
            return

    for attempt in range(retries):
        try:
            if not path.exists():
                return
            shutil.rmtree(path, onerror=_on_rm_error)
            return
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.5)
                continue
            # Final attempt: remove whatever is possible
            shutil.rmtree(path, ignore_errors=True)
            if path.exists():
                debug_logger.log_warning(
                    "server",
                    "_rmtree_robust",
                    f"Could not fully remove {path} after {retries} retries, "
                    f"proceeding with overwrite",
                )


# F-893: why a copy did not run — the copier is the one place that knows.
TARGET_IN_USE = "target-in-use"
SEED_IN_USE = "seed-in-use"


def _copy_profile_tree(
    source: Path, target: Path, clone_root: Path, source_kind: str = "profile"
) -> str | None:
    """Copy *source* over *target*, and report whether the copy actually RAN:
    None when it did, ``TARGET_IN_USE`` when a live browser holds the target.

    Refusing is right — rewriting a directory a Chrome is writing to would be
    the harm — but it is not success, and the bare ``return`` it used to be let
    the refresh report a refreshed snapshot with not one byte moved (F-893).
    The other two callers hand the answer to ``_require_copied``."""
    if not source.exists():
        target.mkdir(parents=True, exist_ok=True)
        return None
    if not _is_relative_to(target, clone_root):
        raise ValueError(f"Refusing to refresh clone outside clone root: {target}")
    if target.exists():
        if _profile_has_running_browser(target):
            return TARGET_IN_USE
        _rmtree_robust(target)
    target.mkdir(parents=True, exist_ok=True)
    profile_copy.copy_delta(source, target)
    time.sleep(0.2)
    profile_copy.copy_delta(source, target)
    profile_seed.write_marker(
        target,
        source=source,
        source_kind=source_kind,
        seeded_from=profile_seed.seed_name(
            source, master_profile_dir(), master_snapshot_dir()
        ),
    )
    return None


def _require_copied(refusal: str | None, target: Path) -> None:
    """A copy these callers cannot have refused (F-893 review m4): both copy
    into a directory they have just found free, with no ``await`` in between.
    One inserted ``await`` makes it reachable, and what it would hand back is an
    empty directory the caller is told is their profile — so it raises."""
    if refusal is not None:
        raise ToolError(f"profile copy into {target} was refused: {refusal}")


def _refresh_master_snapshot_if_safe(reason: str) -> dict[str, Any]:
    """Freshen the SEED, reporting in a caller's vocabulary (F-896): ``seed_*``
    keys, and refusals that name the shared SESSION rather than its directory."""
    master = master_profile_dir()
    snapshot = master_snapshot_dir()
    result = {
        "seed_dir": str(snapshot),
        "seed_refreshed": False,
        "seed_reason": reason,
    }

    if _profile_has_running_browser(master):
        result["seed_error"] = "default-in-use"
        return result

    try:
        refused = _copy_profile_tree(
            master, snapshot, default_session_root(), f"default-seed-{reason}"
        )
        # F-893: a copy the seed's own live browser refused is not a refresh.
        if refused is None:
            result["seed_refreshed"] = True
        else:
            result["seed_error"] = SEED_IN_USE
    except Exception as exc:
        result["seed_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _refresh_snapshot_if_stale() -> None:
    """Freshen the seed before a copy when the shared profile has newer logins
    and is free. Both copy paths asked this the same two-line way (F-892)."""
    if _snapshot_needs_refresh():
        _refresh_master_snapshot_if_safe("pre-clone-stale")


def _snapshot_needs_refresh() -> bool:
    """The seed is stale — the shared profile holds logins newer than it. The
    rule and the witnesses are ``profile_seed``'s (F-892); this binds them to
    OUR two directories and keeps the name every other site calls it by."""
    return profile_seed.needs_refresh(master_profile_dir(), master_snapshot_dir())


def _root_to_path(root: Any) -> str | None:
    value = getattr(root, "uri", None) or root
    value = str(value)
    if value.startswith("file://"):
        parsed = urllib.parse.urlparse(value)
        return urllib.parse.unquote(
            parsed.path.lstrip("/") if os.name == "nt" else parsed.path
        )
    return value or None


async def _client_session_seed() -> str:
    configured = (
        get_settings().stealth_chrome_profile_key or get_settings().browser_profile_key
    )
    if configured:
        return configured

    roots = []
    try:
        from fastmcp.server.dependencies import get_context

        # F-790: bound this OPTIONAL server->client round trip (see settings.py).
        bound = get_settings().client_roots_timeout_seconds
        listed = await asyncio.wait_for(get_context().list_roots(), bound)
        roots = [path for path in (_root_to_path(r) for r in listed) if path]
    except Exception as e:
        message = str(e) or f"{type(e).__name__} awaiting roots/list"
        debug_logger.log_warning("server", "_client_session_seed", message)
        roots = []

    if roots:
        return "|".join(sorted(roots))

    return (
        get_settings().codex_workspace
        or get_settings().claude_project_dir
        or get_settings().pwd
        or os.getcwd()
    )


async def _clone_profile_dir_for_session(clone_root: Path) -> Path:
    seed = await _client_session_seed()
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(seed).name or "session").strip(".-")
    return clone_root / f"{label[:48] or 'session'}-{digest}"


# Per-ATTEMPT uniqueness for a spawn's clone dir (F-834). ``os.getpid()`` alone
# is the BACKEND's pid — one string for every concurrent spawn — so N retrying
# spawns copied into and launched Chrome from ONE directory, and a failed
# sibling's cleanup then deleted the winner's live profile. The counter fixes it.
_CLONE_ATTEMPT_SEQ = itertools.count(1)


def _attempt_token() -> str:
    """A suffix unique to this spawn attempt within this backend process."""
    return f"{os.getpid()}-{next(_CLONE_ATTEMPT_SEQ)}"


def _dir_unavailable(candidate: Path) -> bool:
    """Busy for selection: a live browser holds it, or an in-flight spawn has
    already reserved it via ``_protect_clone_dir``. ``_profile_has_running_browser``
    is a LIVENESS check, NOT a reservation — every concurrent spawn is pre-launch
    when it asks, so liveness alone told them all one name was free (F-834).
    Selection and ``_protect_clone_dir`` have no await between them, so the
    reserve is atomic on the event loop."""
    return _clone_dir_is_protected(candidate) or _profile_has_running_browser(candidate)


def _unique_clone_dir(base_clone: Path, suffix: str) -> Path:
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "-", suffix).strip(".-") or "retry"
    return base_clone.with_name(f"{base_clone.name}-{_attempt_token()}-{safe_suffix}")


def _available_clone_dir(base_clone: Path) -> Path:
    if not _dir_unavailable(base_clone):
        return base_clone
    return base_clone.with_name(f"{base_clone.name}-{_attempt_token()}")


def _next_available_explicit_dir(requested: Path) -> Path:
    """Return the next free variant of a user-supplied profile path.

    When ``sessions/github-session`` is busy, tries ``sessions/github-session-2``,
    ``sessions/github-session-3``, … up to -99, then falls back to a timestamp
    suffix.  Uses clean numeric suffixes (no PID) because these are user-visible.
    """
    for index in range(2, 100):
        candidate = requested.with_name(f"{requested.name}-{index}")
        if not _dir_unavailable(candidate):
            return candidate
    return requested.with_name(
        f"{requested.name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    )


def _copy_clone_from_source(
    source: Path, clone: Path, clone_root: Path, source_kind: str
) -> dict[str, Any]:
    selection: dict[str, Any] = {
        "user_data_dir": str(clone),
        "profile_role": "clone",
        "clone_source": source_kind,
        "clone_source_path": str(source),
        "seed_path": str(master_snapshot_dir()),
    }
    _require_copied(_copy_profile_tree(source, clone, clone_root, source_kind), clone)
    return selection


def require_allowed_user_data_dir(
    user_data_dir: str | None, session: str | None = None
) -> str | None:
    """The ONE thing `spawn_browser` does with what its caller typed: the two
    spellings read as one request, refused if it may not be opened, and the
    DIRECTORY it means — None when nothing was named. It ANSWERS the anchored
    path because `adopt_held_profile`, which runs in front of selection (F-894
    review M1), matches a DIRECTORY against live browsers: the raw string left
    ``session="default"`` matched as a literal name nothing holds, and the
    spawn fell through to a fresh copy."""
    requested = profile_seed.profile_request(session, user_data_dir)
    if not requested:
        return None
    roots = profile_seed.Roots(
        default_session_root(),
        clone_root_dir(),
        master_profile_dir(),
        master_snapshot_dir(),
    )
    return str(profile_seed.require_allowed(requested, roots, _is_relative_to))


def _public_profile_selection(profile_selection: dict[str, Any]) -> dict[str, Any]:
    """The selection as ``spawn_diagnostics.profile_selection`` reports it — the
    ONE site every role passes through, which is why F-895's seed provenance is
    stamped here. It is read from the marker on disk, so a directory that
    already existed reports the seed it was actually made from."""
    public = dict(profile_selection)
    selected = public.get("user_data_dir")
    if isinstance(selected, str) and selected:
        public.update(profile_seed.provenance(Path(selected)))
    return public


async def resolve_profile_selection(
    user_data_dir: str | None,
    *,
    force_clone: bool = False,
    source_override: Path | None = None,
    source_kind: str | None = None,
    clone_suffix: str | None = None,
) -> dict[str, Any]:
    master = master_profile_dir()
    clone_root = clone_root_dir()
    snapshot = master_snapshot_dir()

    # F-894: refused before anything is created, in front of the walk, through
    # the same gate the SPAWN asks — "where does this land" has ONE answer, not
    # two agreeing ones. F-896: the shared profile (by PATH or by the name
    # `default`) is itself and not a clone of itself, so it gets the ROLE that
    # makes `close_instance` refresh the seed — read off the ANCHORED path,
    # because only anchoring turns a name into a directory.
    landed = require_allowed_user_data_dir(user_data_dir)
    explicit = (
        None
        if landed is None or profile_seed.same_dir(Path(landed), master)
        else Path(landed)
    )

    if explicit is not None:
        # If the requested path (inside clone_root) is already held by a running
        # browser, find the next free numbered variant rather than crashing.
        # For a NAMED profile that walk is an identity change — a different set
        # of cookies and logins than the caller asked for — so the answer has to
        # carry what was asked for and what held it (F-871).
        walk: dict[str, Any] = {}
        if _is_relative_to(explicit, clone_root):
            hold = _profile_hold(explicit)
            if hold is not None:
                requested, explicit = explicit, _next_available_explicit_dir(explicit)
                walk = {
                    "requested_user_data_dir": str(requested),
                    "walked_to": str(explicit),
                    "walk_reason": hold.reason,
                }
        if not explicit.exists() and _is_relative_to(explicit, clone_root):
            _refresh_snapshot_if_stale()
            source = snapshot if snapshot.exists() else master
            source_kind = (
                "explicit-default-seed" if source == snapshot else "explicit-default"
            )
            _require_copied(
                _copy_profile_tree(source, explicit, clone_root, source_kind), explicit
            )
        explicit.parent.mkdir(parents=True, exist_ok=True)
        return {
            "user_data_dir": str(explicit),
            "profile_role": "explicit",
            "clone_source": None,
            **walk,
        }

    master.parent.mkdir(parents=True, exist_ok=True)
    if not force_clone and not _profile_has_running_browser(master):
        snapshot_result = _refresh_master_snapshot_if_safe("before-default-open")
        return {
            "user_data_dir": str(master),
            "profile_role": profile_seed.DEFAULT_SESSION,
            "clone_source": None,
            **snapshot_result,
        }

    base_clone = await _clone_profile_dir_for_session(clone_root)
    clone = (
        _unique_clone_dir(base_clone, clone_suffix)
        if clone_suffix
        else _available_clone_dir(base_clone)
    )
    clone_root.mkdir(parents=True, exist_ok=True)
    # Backstop against unbounded session bloat: kick a background sweep (delete
    # idle auto-clones over the clone cap; trim idle named profiles over the
    # session cap) before adding another clone. Non-blocking so spawns stay
    # fast; the clone we are about to write has no marker yet, so it is never a
    # sweep target.
    spawn_background_sweep("pre-clone")

    _refresh_snapshot_if_stale()

    if source_override is not None:
        source = source_override
        resolved_source_kind = source_kind or "default-seed"
    elif snapshot.exists():
        source = snapshot
        resolved_source_kind = source_kind or "default-seed"
    elif master.exists():
        # No seed yet (first run, seed deleted, or the seed copy failed). Fall
        # back to copying directly from the live shared profile.
        # profile_copy.copy_delta skips locked files (PermissionError/OSError),
        # and _copy_profile_tree does a double-pass — cookies and login data
        # transfer successfully even while Chrome has it open.
        source = master
        resolved_source_kind = source_kind or "live-default-fallback"
    else:
        raise RuntimeError(
            "No shared profile directory found — nothing to copy from. Spawn a "
            "browser with no session first to create and populate the "
            f"{profile_seed.DEFAULT_SESSION!r} session."
        )

    # Shield this clone from the storage-cap sweep BEFORE its marker is written.
    # The marker (written inside the copy below) makes the clone a reclaim target,
    # yet its browser has not launched/attached yet — so without this the sweep
    # could delete it out from under the spawning browser. Released when the
    # instance closes (or when this spawn attempt fails).
    _protect_clone_dir(clone)
    return _copy_clone_from_source(source, clone, clone_root, resolved_source_kind)


async def _fallback_profile_selection(
    previous_selection: dict[str, Any],
    attempt: int,
) -> dict[str, Any] | None:
    # What the NEXT attempt drives (F-834 stage 1). A ``clone`` re-clones below;
    # the two non-clone roles retry the SAME directory, which this attempt's
    # F-860 reap has just freed — a NAMED profile is the identity the caller
    # asked for and is never walked or swapped, and a shared profile no sibling
    # took is still the best profile here, while one a sibling DID take falls
    # through. The hold is asked about the directory this attempt DROVE, off the
    # selection, never config. No wait, no reservation: CLAUDE.md's row.
    shared = profile_seed.DEFAULT_SESSION
    role = previous_selection.get("profile_role")
    same = previous_selection.get("user_data_dir")
    if role == "explicit" or (role == shared and _profile_hold(Path(same)) is None):
        return dict(previous_selection)
    if role not in ("clone", shared):
        return None

    snapshot = master_snapshot_dir()
    if not snapshot.exists():
        return None
    final = attempt > 0
    return await resolve_profile_selection(
        None,
        force_clone=True,
        source_override=snapshot,
        source_kind="default-seed-final" if final else "default-seed-retry",
        clone_suffix="seed" if final else "retry",
    )
