"""Profile and clone-storage subsystem for the embedded browser backend.

Owns the disposable-session lifecycle extracted verbatim from ``server.py``
(F-201): default/master/clone/snapshot path resolution, master-snapshot refresh,
per-session profile cloning, the storage-cap sweep (idle auto-clone eviction plus
named-profile regenerable trim), the trash/retention mechanism, and
profile-selection resolution. Extracting it means a fault in storage GC can no
longer disable the whole tool surface.

``server.py`` (the browser tools) and ``cli.py`` (the ops CLI) import this module
and call its public functions; ``spawn_browser`` delegates profile selection to
:func:`resolve_profile_selection`. Public functions drop the leading underscore;
internal-only helpers keep theirs.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
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


def _profile_refresh_days() -> int:
    return get_settings().browser_profile_refresh_days


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _profile_has_running_browser(profile_dir: Path) -> bool:
    try:
        get_pids = getattr(process_cleanup, "_get_browser_pids_for_profile", None)
        if callable(get_pids):
            for candidate in (str(profile_dir), profile_dir):
                try:
                    if get_pids(candidate):
                        return True
                except TypeError:
                    continue
    except (psutil.Error, OSError, AttributeError) as e:
        debug_logger.log_warning(
            "server",
            "_profile_has_running_browser",
            f"PID check failed for profile {profile_dir}: {e}",
        )
    return any(
        (profile_dir / marker).exists()
        for marker in ("SingletonLock", "SingletonSocket", "SingletonCookie")
    )


# Regenerable Chrome profile subdirectories — caches and on-device model stores
# that Chrome rebuilds on next launch. Single source of truth: these are both
# excluded when cloning a profile (_profile_ignore_names) and trimmed from idle
# profiles under storage pressure (_trim_profile_regenerable), so the clone path
# and the trim path can never drift apart.
_REGENERABLE_PROFILE_NAMES = frozenset(
    {
        "BrowserMetrics",
        "CertificateRevocation",
        "Crashpad",
        "Crash Reports",
        "DawnCache",
        "GPUCache",
        "GrShaderCache",
        "GraphiteDawnCache",
        "LOCK",
        "lockfile",
        "Safe Browsing",
        "ShaderCache",
        "SingletonCookie",
        "SingletonLock",
        "SingletonSocket",
        "component_crx_cache",
        # Heavy, regenerable caches and on-device AI models — typically ~98% of a
        # Chrome profile by size (the on-device model alone can be ~4 GB). Excluding
        # or trimming them leaves only real session state: cookies, logins, Web
        # Data, Local Storage, Preferences. Chrome rebuilds them all on next launch.
        "Cache",
        "Code Cache",
        "Service Worker",
        "blob_storage",
        "Download Service",
        "extensions_crx_cache",
        "optimization_guide_model_store",
        "optimization_guide_hint_cache_store",
        "OptGuideOnDeviceModel",
        "OptGuideOnDeviceClassifierModel",
    }
)


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
    """True only for server-created disposable auto-clones.

    Never true for user-named/explicit profiles (they persist by design) or for
    directories the server did not create (no clone marker). Disposability is
    carried by an explicit ``auto_clean`` flag written at clone time.

    Fail-safe on legacy markers: a marker that predates the ``auto_clean`` flag
    is NEVER treated as disposable. The old source-kind fallback
    (``not source_kind.startswith("explicit")``) misjudged user-named profiles
    cloned from a plain ``master-snapshot`` as auto and let the storage-cap sweep
    permanently delete a logged-in business session — a silent, unrecoverable
    loss. Wrongly keeping a stale auto-clone only costs bounded disk, so the
    ambiguity resolves to "keep".
    """
    marker = clone_dir / ".stealth_chrome_devtools_mcp_clone.json"
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data.get("auto_clean", False))


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


def session_storage_cap_bytes() -> int:
    """Cap (in bytes) on total clone-root storage before idle *named* profiles
    are trimmed of regenerable data. Default 20 GiB; override with
    ``STEALTH_MCP_SESSION_STORAGE_CAP_GB`` (a value <= 0 disables the trim).
    """
    gb = get_settings().session_storage_cap_gb
    if gb <= 0:
        return 0
    return int(gb * (1024**3))


def clone_is_named(clone_dir: Path) -> bool:
    """True for user-named/explicit profiles (the persistent ones). They are
    never deleted, but they *can* be trimmed of regenerable data when idle."""
    marker = clone_dir / ".stealth_chrome_devtools_mcp_clone.json"
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if "auto_clean" in data:
        return not bool(data["auto_clean"])
    return str(data.get("source_kind", "")).startswith("explicit")


def _regenerable_dirs_in_profile(profile_dir: Path) -> list[Path]:
    """Regenerable cache/model directories in a profile — those named in
    ``_REGENERABLE_PROFILE_NAMES``, at the profile root and one level down
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
                if child.is_dir() and child.name in _REGENERABLE_PROFILE_NAMES:
                    found.append(child)
            except OSError:
                continue

    _scan(profile_dir)
    try:
        subdirs = [
            c
            for c in profile_dir.iterdir()
            if c.is_dir() and c.name not in _REGENERABLE_PROFILE_NAMES
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
        _enforce_named_profile_trim_in(clone_root, session_storage_cap_bytes(), reason)
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
    session_cap = session_storage_cap_bytes()

    task = asyncio.create_task(
        asyncio.to_thread(run_storage_sweep, clone_root, clone_cap, session_cap, reason)
    )
    _BACKGROUND_SWEEPS.add(task)
    task.add_done_callback(_BACKGROUND_SWEEPS.discard)


def _profile_ignore_names(directory: str, names: list[str]) -> set:
    ignored = set()
    for name in names:
        lower = name.lower()
        if (
            name in _REGENERABLE_PROFILE_NAMES
            or name.startswith("Singleton")
            or lower.endswith(".tmp")
            or lower.endswith(".lock")
            or lower in {"lock", "lockfile"}
        ):
            ignored.add(name)
    return ignored


def _copy_profile_file(source: str, target: str) -> str:
    last_error = None
    for attempt in range(3):
        try:
            shutil.copy2(source, target)
            return target
        except (PermissionError, OSError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        log_warning = getattr(debug_logger, "log_warning", None)
        if callable(log_warning):
            log_warning(
                "profile",
                "copy_skip",
                f"Skipping locked profile file {source}: {last_error}",
            )
    return target


def _copy_profile_delta(source: Path, target: Path) -> None:
    for directory, dirnames, filenames in os.walk(source, onerror=lambda exc: None):
        ignored_dirs = _profile_ignore_names(directory, dirnames)
        dirnames[:] = [name for name in dirnames if name not in ignored_dirs]

        source_dir = Path(directory)
        target_dir = target / source_dir.relative_to(source)
        target_dir.mkdir(parents=True, exist_ok=True)

        ignored_files = _profile_ignore_names(directory, filenames)
        for filename in filenames:
            if filename in ignored_files:
                continue
            source_file = source_dir / filename
            target_file = target_dir / filename
            try:
                if (
                    not target_file.exists()
                    or source_file.stat().st_size != target_file.stat().st_size
                    or int(source_file.stat().st_mtime)
                    != int(target_file.stat().st_mtime)
                ):
                    _copy_profile_file(str(source_file), str(target_file))
            except (PermissionError, OSError):
                continue


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


def _copy_profile_tree(
    source: Path, target: Path, clone_root: Path, source_kind: str = "profile"
) -> None:
    if not source.exists():
        target.mkdir(parents=True, exist_ok=True)
        return
    if not _is_relative_to(target, clone_root):
        raise ValueError(f"Refusing to refresh clone outside clone root: {target}")
    if target.exists():
        if _profile_has_running_browser(target):
            return
        _rmtree_robust(target)
    target.mkdir(parents=True, exist_ok=True)
    _copy_profile_delta(source, target)
    time.sleep(0.2)
    _copy_profile_delta(source, target)
    marker = {
        "source": str(source),
        "source_kind": source_kind,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Disposable auto-clones may be reclaimed by the storage-cap sweep;
        # explicit/named profiles (explicit-* source kinds) never are.
        "auto_clean": not str(source_kind).startswith("explicit"),
    }
    (target / ".stealth_chrome_devtools_mcp_clone.json").write_text(
        json.dumps(marker, indent=2),
        encoding="utf-8",
    )


def _clone_needs_refresh(target: Path) -> bool:
    if not target.exists():
        return True
    refresh_days = _profile_refresh_days()
    if refresh_days <= 0:
        return False
    marker = target / ".stealth_chrome_devtools_mcp_clone.json"
    if not marker.exists():
        return False
    cutoff = datetime.now() - timedelta(days=refresh_days)
    return datetime.fromtimestamp(marker.stat().st_mtime) < cutoff


def _refresh_master_snapshot_if_safe(reason: str) -> dict[str, Any]:
    master = master_profile_dir()
    snapshot = master_snapshot_dir()
    result = {
        "snapshot_dir": str(snapshot),
        "snapshot_refreshed": False,
        "snapshot_reason": reason,
    }

    if _profile_has_running_browser(master):
        result["snapshot_error"] = "master-in-use"
        return result

    try:
        _copy_profile_tree(
            master, snapshot, default_session_root(), f"master-snapshot-{reason}"
        )
        result["snapshot_refreshed"] = True
    except Exception as exc:
        result["snapshot_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _snapshot_needs_refresh() -> bool:
    """Return True when master has auth-relevant files newer than the last snapshot.

    Checks a small set of key Chrome profile files whose mtime changes on
    login/logout.  Fast (stat-only) and safe to call before every clone.
    """
    master = master_profile_dir()
    snapshot = master_snapshot_dir()
    if not snapshot.exists():
        return False  # no snapshot yet; creation is handled elsewhere
    marker = snapshot / ".stealth_chrome_devtools_mcp_clone.json"
    if not marker.exists():
        return True
    try:
        snapshot_time = marker.stat().st_mtime
        for rel in ("Default/Cookies", "Default/Login Data", "Default/Web Data"):
            src = master / rel
            if src.exists() and src.stat().st_mtime > snapshot_time:
                return True
    except OSError:
        pass
    return False


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

        context = get_context()
        for root in await context.list_roots():
            path = _root_to_path(root)
            if path:
                roots.append(path)
    except Exception as e:
        debug_logger.log_warning("server", "_client_session_seed", str(e))
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


def _pid_suffixed_clone_dir(base_clone: Path) -> Path:
    return base_clone.with_name(f"{base_clone.name}-{os.getpid()}")


def _unique_clone_dir(base_clone: Path, suffix: str) -> Path:
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "-", suffix).strip(".-") or "retry"
    candidate = base_clone.with_name(f"{base_clone.name}-{os.getpid()}-{safe_suffix}")
    if not _profile_has_running_browser(candidate):
        return candidate
    return base_clone.with_name(
        f"{base_clone.name}-{os.getpid()}-{safe_suffix}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
    )


def _available_clone_dir(base_clone: Path) -> Path:
    if not _profile_has_running_browser(base_clone):
        return base_clone

    pid_clone = _pid_suffixed_clone_dir(base_clone)
    if not _profile_has_running_browser(pid_clone):
        return pid_clone

    for index in range(2, 100):
        candidate = base_clone.with_name(f"{base_clone.name}-{os.getpid()}-{index}")
        if not _profile_has_running_browser(candidate):
            return candidate

    return base_clone.with_name(
        f"{base_clone.name}-{os.getpid()}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    )


def _next_available_explicit_dir(requested: Path) -> Path:
    """Return the next free variant of a user-supplied profile path.

    When ``sessions/github-session`` is busy, tries ``sessions/github-session-2``,
    ``sessions/github-session-3``, … up to -99, then falls back to a timestamp
    suffix.  Uses clean numeric suffixes (no PID) because these are user-visible.
    """
    for index in range(2, 100):
        candidate = requested.with_name(f"{requested.name}-{index}")
        if not _profile_has_running_browser(candidate):
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
        "master_snapshot_path": str(master_snapshot_dir()),
    }
    _copy_profile_tree(source, clone, clone_root, source_kind)
    return selection


def _public_profile_selection(profile_selection: dict[str, Any]) -> dict[str, Any]:
    return dict(profile_selection)


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

    if user_data_dir:
        explicit = Path(user_data_dir).expanduser()
        if not explicit.is_absolute():
            # Resolve relative names so they land inside clone_root (sessions/).
            # First anchor against session_root; if the result is already inside
            # clone_root (e.g. "sessions/github-session"), keep it — otherwise
            # prepend clone_root so a bare name like "github-session" becomes
            # sessions/github-session.  This avoids the double-sessions path
            # sessions/sessions/github-session when the user includes the prefix.
            anchored = default_session_root() / explicit
            explicit = (
                anchored
                if _is_relative_to(anchored, clone_root)
                else clone_root / explicit
            )
        # If the requested path (inside clone_root) is already held by a running
        # browser, find the next free numbered variant rather than crashing.
        if _is_relative_to(explicit, clone_root) and _profile_has_running_browser(
            explicit
        ):
            explicit = _next_available_explicit_dir(explicit)
        if not explicit.exists() and _is_relative_to(explicit, clone_root):
            # Refresh stale snapshot before copying so the clone carries the
            # latest logins (only runs when master is not in use).
            if _snapshot_needs_refresh():
                _refresh_master_snapshot_if_safe("pre-clone-stale")
            source = snapshot if snapshot.exists() else master
            source_kind = (
                "explicit-master-snapshot" if source == snapshot else "explicit-master"
            )
            _copy_profile_tree(source, explicit, clone_root, source_kind)
        explicit.parent.mkdir(parents=True, exist_ok=True)
        return {
            "user_data_dir": str(explicit),
            "profile_role": "explicit",
            "clone_source": None,
        }

    master.parent.mkdir(parents=True, exist_ok=True)
    if not force_clone and not _profile_has_running_browser(master):
        snapshot_result = _refresh_master_snapshot_if_safe("before-master-open")
        return {
            "user_data_dir": str(master),
            "profile_role": "master",
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
    if _profile_has_running_browser(clone):
        clone = _unique_clone_dir(base_clone, clone_suffix or "busy")

    # Freshen snapshot before cloning if master has newer auth data and is not in use.
    if _snapshot_needs_refresh():
        _refresh_master_snapshot_if_safe("pre-clone-stale")

    if source_override is not None:
        source = source_override
        resolved_source_kind = source_kind or "master-snapshot"
    elif snapshot.exists():
        source = snapshot
        resolved_source_kind = source_kind or "master-snapshot"
    elif master.exists():
        # No snapshot yet (first run, snapshot deleted, or snapshot copy failed).
        # Fall back to cloning directly from the live master directory.
        # _copy_profile_delta skips locked files (PermissionError/OSError),
        # and _copy_profile_tree does a double-pass — cookies and login data
        # transfer successfully even while Chrome has master open.
        source = master
        resolved_source_kind = source_kind or "live-master-fallback"
    else:
        raise RuntimeError(
            "No master profile directory found — nothing to clone from. "
            "Spawn a browser without user_data_dir first to create and populate the master profile."
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
    if previous_selection.get("profile_role") != "clone":
        return None

    snapshot = master_snapshot_dir()
    if attempt == 0:
        if snapshot.exists():
            return await resolve_profile_selection(
                None,
                force_clone=True,
                source_override=snapshot,
                source_kind="master-snapshot-retry",
                clone_suffix="retry",
            )
        return None

    if snapshot.exists():
        return await resolve_profile_selection(
            None,
            force_clone=True,
            source_override=snapshot,
            source_kind="master-snapshot-final",
            clone_suffix="snapshot",
        )

    return None
