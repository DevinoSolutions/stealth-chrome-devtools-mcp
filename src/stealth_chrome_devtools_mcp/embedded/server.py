"""Main MCP server for browser automation."""

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil
from browser_manager import BrowserManager
from cdp_element_cloner import CDPElementCloner
from cdp_function_executor import CDPFunctionExecutor
from comprehensive_element_cloner import comprehensive_element_cloner
from debug_logger import debug_logger
from dom_handler import DOMHandler
from dynamic_hook_ai_interface import dynamic_hook_ai
from dynamic_hook_system import dynamic_hook_system
from element_cloner import element_cloner
from fastmcp import FastMCP
from file_based_element_cloner import file_based_element_cloner
from logging_setup import bootstrap_backend_process_logging, with_correlation_id
from models import (
    BrowserOptions,
)
from network_interceptor import NetworkInterceptor
from persistent_storage import persistent_storage
from platform_utils import get_platform_info, validate_browser_environment
from process_cleanup import process_cleanup
from progressive_element_cloner import progressive_element_cloner
from response_handler import response_handler

from stealth_chrome_devtools_mcp.observability import sentry_init
from stealth_chrome_devtools_mcp.settings import get_settings

DISABLED_SECTIONS = set()
SECTION_TOOLS: dict[str, list[str]] = defaultdict(list)


CDP_OPERATION_TIMEOUT = get_settings().cdp_operation_timeout_seconds
MAX_TIMEOUT_MS = 60_000

# User-supplied JS (execute_script) gets a short, dedicated timeout so a blocking
# script fails fast instead of freezing the tab for the full CDP_OPERATION_TIMEOUT
# window and stalling every subsequent call.
EXECUTE_SCRIPT_TIMEOUT = get_settings().execute_script_timeout_seconds

# Reject user scripts larger than this. Huge inline payloads (e.g. base64-encoded
# files) overflow the transport and are almost always an upload hack — callers
# should use the upload_file tool instead.
MAX_USER_SCRIPT_BYTES = get_settings().max_user_script_bytes

# High-confidence denylist of patterns that block the renderer's main thread or
# overflow the page. This is NOT a JS sandbox — just a guard against the handful
# of foot-guns that wedge the browser for every later call.
_BLOCKING_SCRIPT_PATTERNS = [
    (
        re.compile(r"\.open\s*\([^)]*,\s*false\s*\)", re.IGNORECASE),
        "Synchronous XMLHttpRequest (xhr.open(url, false)) blocks the page's main "
        "thread and freezes every later call. Use 'await fetch(url)' instead.",
    ),
    (
        re.compile(r"while\s*\(\s*(?:true|1)\s*\)"),
        "Infinite 'while(true)' loop freezes the renderer. Use a bounded loop or an "
        "async delay: 'await new Promise(r => setTimeout(r, ms))'.",
    ),
    (
        re.compile(r"for\s*\(\s*;\s*;\s*\)"),
        "Infinite 'for(;;)' loop freezes the renderer. Use a bounded loop instead.",
    ),
    (
        re.compile(r"\b(?:alert|confirm|prompt)\s*\("),
        "Modal dialogs (alert/confirm/prompt) block the renderer and cannot be "
        "dismissed by automation. Remove them.",
    ),
]


def _script_rejection_reason(script: str) -> str | None:
    """Return a corrective message if a user script is unsafe to run, else None.

    Guards against the common foot-guns that freeze the tab or overflow the
    transport (sync XHR, infinite loops, blocking dialogs, oversized payloads).
    Intentionally small and high-confidence — not a JavaScript sandbox.
    """
    if not isinstance(script, str):
        return None
    size = len(script.encode("utf-8", errors="ignore"))
    if size > MAX_USER_SCRIPT_BYTES:
        return (
            f"Script too large ({size} bytes > {MAX_USER_SCRIPT_BYTES} limit). "
            "Inline payloads such as base64-encoded files overflow the transport — "
            "use the 'upload_file' tool for files, or a file-based approach."
        )
    for pattern, message in _BLOCKING_SCRIPT_PATTERNS:
        if pattern.search(script):
            return f"Rejected: {message}"
    return None


def _clamp_timeout(timeout_ms: int, default: int = 30_000) -> int:
    """Clamp a user-provided timeout (ms) to [1, MAX_TIMEOUT_MS]."""
    if isinstance(timeout_ms, str):
        timeout_ms = int(timeout_ms)
    return max(1, min(timeout_ms, MAX_TIMEOUT_MS))


async def _with_cdp_timeout(coro, timeout: float = 0, instance_id: str = ""):
    """Wrap a CDP coroutine with asyncio.wait_for to prevent infinite hangs.

    When a Chrome DevTools Protocol connection is stale or dead, awaiting a
    CDP operation blocks forever.  This wrapper raises a clear error after
    *timeout* seconds so the caller (and the MCP client) gets a response
    instead of hanging indefinitely.
    """
    t = timeout or CDP_OPERATION_TIMEOUT
    try:
        return await asyncio.wait_for(coro, timeout=t)
    except TimeoutError:
        tag = f" (instance {instance_id})" if instance_id else ""
        raise Exception(
            f"CDP operation timed out after {t:.0f}s{tag}. "
            "The browser may have crashed or the connection dropped. "
            "Try closing the instance with close_instance and spawning a new one."
        )


def _install_asyncio_close_noise_filter() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    marker = "_stealth_chrome_devtools_close_noise_filter"
    if getattr(loop, marker, False):
        return

    previous_handler = loop.get_exception_handler()

    def exception_handler(
        loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        exception = context.get("exception")
        if (
            exception is not None
            and exception.__class__.__name__
            in ("ConnectionClosedOK", "ConnectionClosedError", "ConnectionClosed")
            and str(exception.__class__.__module__).startswith("websockets.")
        ):
            # Swallow both clean (OK) and abnormal (Error) websocket closes. When
            # Chrome crashes or is killed, nodriver's background listener task
            # raises ConnectionClosedError; without this it surfaces loudly and
            # can escalate. The instance is already unusable and will be respawned.
            return

        if previous_handler is not None:
            previous_handler(loop, context)
            return

        loop.default_exception_handler(context)

    loop.set_exception_handler(exception_handler)
    setattr(loop, marker, True)


def _install_nodriver_cookie_compat() -> None:
    try:
        import nodriver.cdp.network as cdp_network
    except Exception as e:
        debug_logger.log_warning("server", "_install_nodriver_cookie_compat", str(e))
        return

    marker = "_stealth_chrome_devtools_cookie_compat"
    if getattr(cdp_network.Cookie, marker, False):
        return

    original_from_json = cdp_network.Cookie.from_json

    def from_json(json_obj: dict[str, Any]):
        if isinstance(json_obj, dict) and "sameParty" not in json_obj:
            json_obj = dict(json_obj)
            json_obj["sameParty"] = False
        return original_from_json(json_obj)

    cdp_network.Cookie.from_json = staticmethod(from_json)
    setattr(cdp_network.Cookie, marker, True)


DEBUG_LOGGING_ENABLED = get_settings().stealth_browser_debug or get_settings().debug


def _default_session_root() -> Path:
    root = get_settings().browser_session_root
    if root:
        return Path(root).expanduser()
    if os.name == "nt":
        return Path(r"C:\stealth-mcp-browser-sessions")
    return Path.home() / ".stealth-mcp-browser-sessions"


def _master_profile_dir() -> Path:
    configured = get_settings().browser_master_user_data_dir
    if configured:
        return Path(configured).expanduser()
    return _default_session_root() / "master"


def _clone_root_dir() -> Path:
    configured = get_settings().browser_profile_clone_root
    if configured:
        return Path(configured).expanduser()
    return _default_session_root() / "sessions"


def _master_snapshot_dir() -> Path:
    configured = get_settings().browser_master_snapshot_dir
    if configured:
        return Path(configured).expanduser()
    return _default_session_root() / "master-snapshot"


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


def _clone_storage_cap_bytes() -> int:
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


def _clone_is_auto(clone_dir: Path) -> bool:
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
            if not entry.is_dir() or not _clone_is_auto(entry):
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


def _session_storage_cap_bytes() -> int:
    """Cap (in bytes) on total clone-root storage before idle *named* profiles
    are trimmed of regenerable data. Default 20 GiB; override with
    ``STEALTH_MCP_SESSION_STORAGE_CAP_GB`` (a value <= 0 disables the trim).
    """
    gb = get_settings().session_storage_cap_gb
    if gb <= 0:
        return 0
    return int(gb * (1024**3))


def _clone_is_named(clone_dir: Path) -> bool:
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
        if not _clone_is_named(entry) or _profile_has_running_browser(entry):
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


def _enforce_session_storage(reason: str = "") -> None:
    """Bound clone-root storage: delete idle auto-clones over the clone cap,
    then trim regenerable data from the largest idle named profiles over the
    session cap. Best-effort; never raises into a spawn or startup."""
    try:
        clone_root = _clone_root_dir()
        _enforce_clone_storage_cap_in(clone_root, _clone_storage_cap_bytes(), reason)
        _enforce_named_profile_trim_in(clone_root, _session_storage_cap_bytes(), reason)
    except Exception as error:
        debug_logger.log_warning(
            "server", "session_storage_sweep", f"sweep failed: {error}"
        )


# Strong refs to in-flight housekeeping sweeps so the event loop cannot GC them
# mid-run; the done-callback drops each when it finishes.
_BACKGROUND_SWEEPS: set = set()


def _run_storage_sweep(
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


def _spawn_background_sweep(reason: str = "") -> None:
    """Kick the storage sweep off the event loop without blocking the caller.

    The clone root and caps are resolved now, at trigger time, and captured for
    the worker — so the sweep always targets the root that was active when it was
    triggered (this also keeps it hermetic when tests patch the env to a tmp
    dir). Deduped to one in-flight sweep, since sizing the clone root is the only
    real cost and running it concurrently with itself buys nothing.
    """
    if _BACKGROUND_SWEEPS:
        return
    clone_root = _clone_root_dir()
    clone_cap = _clone_storage_cap_bytes()
    session_cap = _session_storage_cap_bytes()

    task = asyncio.create_task(
        asyncio.to_thread(
            _run_storage_sweep, clone_root, clone_cap, session_cap, reason
        )
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
    master = _master_profile_dir()
    snapshot = _master_snapshot_dir()
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
            master, snapshot, _default_session_root(), f"master-snapshot-{reason}"
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
    master = _master_profile_dir()
    snapshot = _master_snapshot_dir()
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
        "master_snapshot_path": str(_master_snapshot_dir()),
    }
    _copy_profile_tree(source, clone, clone_root, source_kind)
    return selection


def _public_profile_selection(profile_selection: dict[str, Any]) -> dict[str, Any]:
    return dict(profile_selection)


async def _resolve_profile_selection(
    user_data_dir: str | None,
    *,
    force_clone: bool = False,
    source_override: Path | None = None,
    source_kind: str | None = None,
    clone_suffix: str | None = None,
) -> dict[str, Any]:
    master = _master_profile_dir()
    clone_root = _clone_root_dir()
    snapshot = _master_snapshot_dir()

    if user_data_dir:
        explicit = Path(user_data_dir).expanduser()
        if not explicit.is_absolute():
            # Resolve relative names so they land inside clone_root (sessions/).
            # First anchor against session_root; if the result is already inside
            # clone_root (e.g. "sessions/github-session"), keep it — otherwise
            # prepend clone_root so a bare name like "github-session" becomes
            # sessions/github-session.  This avoids the double-sessions path
            # sessions/sessions/github-session when the user includes the prefix.
            anchored = _default_session_root() / explicit
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
    _spawn_background_sweep("pre-clone")
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

    snapshot = _master_snapshot_dir()
    if attempt == 0:
        if snapshot.exists():
            return await _resolve_profile_selection(
                None,
                force_clone=True,
                source_override=snapshot,
                source_kind="master-snapshot-retry",
                clone_suffix="retry",
            )
        return None

    if snapshot.exists():
        return await _resolve_profile_selection(
            None,
            force_clone=True,
            source_override=snapshot,
            source_kind="master-snapshot-final",
            clone_suffix="snapshot",
        )

    return None


def is_section_enabled(section: str) -> bool:
    """Check if a tool section is enabled."""
    return section not in DISABLED_SECTIONS


def section_tool(section: str):
    """Decorator that registers tools and tracks section membership."""

    def decorator(func):
        SECTION_TOOLS[section].append(func.__name__)
        return mcp.tool(with_correlation_id(func))

    return decorator


def apply_disabled_sections() -> None:
    """Apply section disable rules by unregistering tools from FastMCP."""
    for section in sorted(DISABLED_SECTIONS):
        for tool_name in SECTION_TOOLS.get(section, []):
            try:
                mcp.remove_tool(tool_name)
            except Exception as e:
                # Tool may already be removed by another section policy.
                debug_logger.log_debug("server", "apply_disabled_sections", str(e))
                continue


@asynccontextmanager
async def app_lifespan(server):
    """
    Manage application lifecycle with proper cleanup.

    Args:
        server (Any): The server instance for which the lifespan is being managed.
    """
    _install_asyncio_close_noise_filter()
    _install_nodriver_cookie_compat()
    debug_logger.log_info(
        "server", "startup", "Starting Browser Automation MCP Server..."
    )
    try:
        await browser_manager.start_idle_reaper()
        # Reclaim leaked auto-clones and trim oversized idle named profiles left
        # by a previous run. Fire-and-forget so a large first sweep never delays
        # server readiness.
        _spawn_background_sweep("startup")
        yield
    finally:
        debug_logger.log_info(
            "server", "shutdown", "Shutting down Browser Automation MCP Server..."
        )
        try:
            await browser_manager.stop_idle_reaper()
        except Exception as e:
            debug_logger.log_error("server", "cleanup", e)
        try:
            await browser_manager.close_all()
            debug_logger.log_info("server", "cleanup", "All browser instances closed")
        except Exception as e:
            debug_logger.log_error("server", "cleanup", e)

        try:
            process_cleanup._cleanup_all_tracked()
            debug_logger.log_info("server", "cleanup", "Process cleanup complete")
        except Exception as e:
            debug_logger.log_error("server", "cleanup", f"Process cleanup failed: {e}")
        try:
            persistent_instances = persistent_storage.list_instances()
            if persistent_instances.get("instances"):
                debug_logger.log_info(
                    "server",
                    "storage_cleanup",
                    f"Clearing in-memory storage with {len(persistent_instances['instances'])} instances...",
                )
                persistent_storage.clear_all()
                debug_logger.log_info(
                    "server", "storage_cleanup", "In-memory storage cleared"
                )
        except Exception as e:
            debug_logger.log_error("server", "storage_cleanup", e)
        debug_logger.log_info(
            "server", "shutdown", "Browser Automation MCP Server shutdown complete"
        )


mcp = FastMCP(
    name="Browser Automation MCP",
    instructions="""
    This MCP server provides undetectable browser automation using nodriver (CDP-based).
    
    Key features:
    - Spawn and manage multiple browser instances
    - Navigate and interact with web pages
    - Query and manipulate DOM elements
    - Intercept and analyze network traffic
    - Execute JavaScript in page context
    - Manage cookies and storage
    
    All browser instances are undetectable by anti-bot systems.
    """,
    lifespan=app_lifespan,
)

browser_manager = BrowserManager()
network_interceptor = NetworkInterceptor()
dom_handler = DOMHandler()
cdp_function_executor = CDPFunctionExecutor()

if DEBUG_LOGGING_ENABLED:
    debug_logger.enable()


@section_tool("browser-management")
async def spawn_browser(
    headless: bool = False,
    user_agent: str | None = None,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    proxy: str | None = None,
    browser_args: list[str] = None,
    timezone_id: str | None = None,
    idle_timeout_seconds: int | None = None,
    block_resources: list[str] = None,
    extra_headers: dict[str, str] = None,
    user_data_dir: str | None = None,
    sandbox: Any | None = None,
) -> dict[str, Any]:
    """
    Spawn a new browser instance.

    Args:
        headless (bool): Run in headless mode.
        user_agent (Optional[str]): Custom user agent string.
        viewport_width (int): Viewport width in pixels.
        viewport_height (int): Viewport height in pixels.
        proxy (Optional[str]): Proxy server URL.
        browser_args (List[str]): Additional browser launch args.
        timezone_id (Optional[str]): IANA timezone ID applied via CDP timezone override.
        idle_timeout_seconds (Optional[int]): Idle timeout override in seconds for automatic instance cleanup.
        block_resources (List[str]): List of resource types to block (e.g., ['image', 'font', 'stylesheet']).
        extra_headers (Dict[str, str]): Additional HTTP headers.
        user_data_dir (Optional[str]): Leave UNSET for normal use. When unset, the server
            automatically clones a disposable session from the master profile and deletes it
            as soon as the browser closes — you never need to manage or clean up sessions.
            Only set this when the user has EXPLICITLY asked for a persistent/named profile:
            a named profile is NOT auto-cleaned and persists on disk indefinitely, so treat
            creating one as a deliberate, space-consuming action. Do not invent names.
        sandbox (Optional[Any]): Enable browser sandbox. Accepts bool, string ('true'/'false'), int (1/0), or None for auto-detect.

    Returns:
        Dict[str, Any]: Instance information including instance_id.
    """
    try:
        from platform_utils import is_running_as_root, is_running_in_container

        if sandbox is None:
            sandbox = not (is_running_as_root() or is_running_in_container())
        elif isinstance(sandbox, str):
            sandbox = sandbox.lower() in ("true", "1", "yes", "on", "enabled")
        elif isinstance(sandbox, int) or not isinstance(sandbox, bool):
            sandbox = bool(sandbox)

        profile_selection = await _resolve_profile_selection(user_data_dir)
        spawn_errors = []

        for spawn_attempt in range(3):
            selected_user_data_dir = profile_selection["user_data_dir"]
            options = BrowserOptions(
                headless=headless,
                user_agent=user_agent,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                proxy=proxy,
                browser_args=browser_args or [],
                timezone_id=timezone_id,
                idle_timeout_seconds=idle_timeout_seconds,
                block_resources=block_resources or [],
                extra_headers=extra_headers or {},
                user_data_dir=selected_user_data_dir,
                sandbox=sandbox,
                auto_clone=(profile_selection.get("profile_role") == "clone"),
            )
            try:
                instance = await browser_manager.spawn_browser(options)
                user_data_dir = selected_user_data_dir
                break
            except Exception as spawn_error:
                spawn_errors.append(f"{type(spawn_error).__name__}: {spawn_error}")
                # This attempt's clone never became a live instance — drop its
                # sweep protection so a failed clone can't stay protected (and thus
                # unreclaimable) for the rest of the process.
                if profile_selection.get("profile_role") == "clone":
                    _release_clone_dir(selected_user_data_dir)
                fallback_selection = await _fallback_profile_selection(
                    profile_selection, spawn_attempt
                )
                if fallback_selection is None:
                    raise
                profile_selection = fallback_selection
        else:
            raise Exception("; ".join(spawn_errors))

        tab = await browser_manager.get_tab(instance.instance_id)
        if tab:
            await network_interceptor.setup_interception(
                tab, instance.instance_id, block_resources
            )
        spawn_diagnostics = await browser_manager.get_spawn_diagnostics(
            instance.instance_id
        )
        if isinstance(spawn_diagnostics, dict):
            spawn_diagnostics["profile_selection"] = _public_profile_selection(
                profile_selection
            )
            if spawn_errors:
                spawn_diagnostics["profile_selection"]["spawn_retries"] = spawn_errors
            if profile_selection.get("profile_role") == "explicit":
                spawn_diagnostics["profile_selection"]["warning"] = (
                    "Named profile created — it is NOT auto-cleaned and persists on disk. "
                    "Only pass user_data_dir when the user explicitly asks for a persistent "
                    "profile; otherwise omit it so the session is auto-cloned and auto-deleted."
                )
        return {
            "instance_id": instance.instance_id,
            "state": instance.state,
            "headless": instance.headless,
            "viewport": instance.viewport,
            "spawn_diagnostics": spawn_diagnostics or {},
        }
    except Exception as e:
        raise Exception(f"Failed to spawn browser: {e!s}")


@section_tool("browser-management")
async def list_instances() -> list[dict[str, Any]]:
    """
    List all active browser instances.

    Returns:
        List[Dict[str, Any]]: List of browser instances with their current state.
    """
    memory_instances = await browser_manager.list_instances()
    storage_instances = persistent_storage.list_instances()
    result = []
    for inst in memory_instances:
        result.append(
            {
                "instance_id": inst.instance_id,
                "state": inst.state,
                "current_url": inst.current_url,
                "title": inst.title,
                "source": "active",
            }
        )
    memory_ids = {inst.instance_id for inst in memory_instances}
    for instance_id, inst_data in storage_instances.get("instances", {}).items():
        if instance_id not in memory_ids:
            result.append(
                {
                    "instance_id": inst_data["instance_id"],
                    "state": inst_data["state"] + " (stored)",
                    "current_url": inst_data["current_url"],
                    "title": inst_data["title"],
                    "source": "stored",
                }
            )
    return result


@section_tool("browser-management")
async def close_instance(instance_id: str) -> bool:
    """
    Close a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True if closed successfully.
    """
    spawn_diagnostics = await browser_manager.get_spawn_diagnostics(instance_id)
    profile_selection = {}
    if isinstance(spawn_diagnostics, dict):
        profile_selection = spawn_diagnostics.get("profile_selection") or {}
    should_refresh_snapshot = profile_selection.get("profile_role") == "master"

    success = await browser_manager.close_instance(instance_id)
    if success:
        await network_interceptor.clear_instance_data(instance_id)
        dynamic_hook_system.remove_instance(instance_id)
        # Instance is gone — lift sweep protection for its disposable clone so the
        # storage cap can reclaim it later if the on-close delete was deferred.
        if profile_selection.get("profile_role") == "clone" and profile_selection.get(
            "user_data_dir"
        ):
            _release_clone_dir(profile_selection["user_data_dir"])
        if should_refresh_snapshot:
            await asyncio.to_thread(
                _refresh_master_snapshot_if_safe, "after-master-close"
            )
    return success


@section_tool("browser-management")
async def get_instance_state(instance_id: str) -> dict[str, Any] | None:
    """
    Get detailed state of a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Optional[Dict[str, Any]]: Complete state information.
    """
    timeout_seconds = get_settings().browser_state_timeout_seconds
    try:
        state = await asyncio.wait_for(
            browser_manager.get_page_state(instance_id),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        for instance in await browser_manager.list_instances():
            if instance.instance_id == instance_id:
                return {
                    "instance_id": instance.instance_id,
                    "state": instance.state,
                    "current_url": instance.current_url,
                    "title": instance.title,
                    "source": "active",
                    "partial": True,
                    "detail_error": f"Timed out after {timeout_seconds:g}s while collecting full page state.",
                }
        return {
            "instance_id": instance_id,
            "state": "unknown",
            "partial": True,
            "detail_error": f"Timed out after {timeout_seconds:g}s while collecting full page state.",
        }
    except Exception as exc:
        for instance in await browser_manager.list_instances():
            if instance.instance_id == instance_id:
                return {
                    "instance_id": instance.instance_id,
                    "state": instance.state,
                    "current_url": instance.current_url,
                    "title": instance.title,
                    "source": "active",
                    "partial": True,
                    "detail_error": f"Failed to collect full page state: {type(exc).__name__}: {exc}",
                }
        return {
            "instance_id": instance_id,
            "state": "unknown",
            "partial": True,
            "detail_error": f"Failed to collect full page state: {type(exc).__name__}: {exc}",
        }
    if state:
        result = state.dict()
        result["partial"] = False
        return result
    return None


@section_tool("browser-management")
async def navigate(
    instance_id: str,
    url: str,
    wait_until: str = "load",
    timeout: int = 30000,
    referrer: str | None = None,
) -> dict[str, Any]:
    """
    Navigate to a URL.

    Args:
        instance_id (str): Browser instance ID.
        url (str): URL to navigate to.
        wait_until (str): Wait condition - 'load', 'domcontentloaded', or 'networkidle'.
        timeout (int): Navigation timeout in ms (default 30000, max 60000). Most pages load in under 10s — only increase if you have evidence the page is slow. Values above 60000 are capped.
        referrer (Optional[str]): Referrer URL.

    Returns:
        Dict[str, Any]: Navigation result with final URL and title.
    """
    timeout = _clamp_timeout(timeout, default=30_000)
    outer_timeout = max(timeout / 1000 + 5, CDP_OPERATION_TIMEOUT)
    return await _with_cdp_timeout(
        browser_manager.navigate(
            instance_id=instance_id,
            url=url,
            wait_until=wait_until,
            timeout=timeout,
            referrer=referrer,
        ),
        timeout=outer_timeout,
        instance_id=instance_id,
    )


@section_tool("browser-management")
async def go_back(instance_id: str) -> bool:
    """
    Navigate back in history.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True if navigation was successful.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    await _with_cdp_timeout(tab.back(), instance_id=instance_id)
    return True


@section_tool("browser-management")
async def go_forward(instance_id: str) -> bool:
    """
    Navigate forward in history.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        bool: True if navigation was successful.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    await _with_cdp_timeout(tab.forward(), instance_id=instance_id)
    return True


@section_tool("browser-management")
async def reload_page(instance_id: str, ignore_cache: bool = False) -> bool:
    """
    Reload the current page.

    Args:
        instance_id (str): Browser instance ID.
        ignore_cache (bool): Whether to ignore cache when reloading.

    Returns:
        bool: True if reload was successful.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    await _with_cdp_timeout(tab.reload(), instance_id=instance_id)
    return True


@section_tool("element-interaction")
async def query_elements(
    instance_id: str,
    selector: str,
    text_filter: str | None = None,
    visible_only: bool = True,
    limit: Any | None = None,
) -> list[dict[str, Any]]:
    """
    Query DOM elements.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath (starts with '//').
        text_filter (Optional[str]): Filter by text content.
        visible_only (bool): Only return visible elements.
        limit (Optional[Any]): Maximum number of elements to return.

    Returns:
        List[Dict[str, Any]]: List of matching elements with their properties.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    debug_logger.log_info(
        "Server",
        "query_elements",
        f"Received limit parameter: {limit} (type: {type(limit)})",
    )
    elements = await _with_cdp_timeout(
        dom_handler.query_elements(tab, selector, text_filter, visible_only, limit),
        instance_id=instance_id,
    )
    debug_logger.log_info(
        "Server", "query_elements", f"DOM handler returned {len(elements)} elements"
    )
    result = []
    for i, elem in enumerate(elements):
        try:
            if hasattr(elem, "model_dump"):
                elem_dict = elem.model_dump()
            else:
                elem_dict = elem.dict()
            result.append(elem_dict)
            debug_logger.log_info(
                "Server",
                "query_elements",
                f"Converted element {i + 1} to dict: {list(elem_dict.keys())}",
            )
        except Exception as e:
            debug_logger.log_error("Server", "query_elements", e, {"element_index": i})
    debug_logger.log_info(
        "Server", "query_elements", f"Returning {len(result)} results to MCP client"
    )
    return result or []


@section_tool("element-interaction")
async def click_element(
    instance_id: str,
    selector: str,
    text_match: str | None = None,
    timeout: int = 10000,
) -> bool:
    """
    Click an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        text_match (Optional[str]): Click element with matching text.
        timeout (int): Timeout in ms (default 10000, max 60000). Clicks rarely need more than 5s — only increase for dynamically loaded elements. Values above 60000 are capped.

    Returns:
        bool: True if clicked successfully.
    """
    timeout = _clamp_timeout(timeout, default=10_000)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        dom_handler.click_element(tab, selector, text_match, timeout),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def upload_file(
    instance_id: str,
    selector: str,
    file_paths: str | list[str],
    timeout: int = 10000,
) -> dict[str, Any]:
    """
    Upload local file(s) to a file input. USE THIS for file uploads.

    Sets the files directly on the <input type="file"> via CDP — reliable and
    non-blocking. Do NOT try to upload by fetching blobs / building base64 /
    DataTransfer inside execute_script: that hits mixed-content/CORS limits and
    can freeze the page. This tool is the supported path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath for the <input type="file"> element.
        file_paths (Union[str, List[str]]): Absolute path, or list of paths, to attach.
            For multiple files the input must have the `multiple` attribute.
        timeout (int): Element lookup timeout in ms (default 10000, max 60000).

    Returns:
        Dict[str, Any]: {"uploaded": [absolute paths], "count": int}.
    """
    timeout = _clamp_timeout(timeout, default=10_000)
    paths = [file_paths] if isinstance(file_paths, str) else list(file_paths)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        dom_handler.upload_file(tab, selector, paths, timeout),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def type_text(
    instance_id: str,
    selector: str,
    text: str,
    clear_first: bool = True,
    delay_ms: int = 50,
    parse_newlines: bool = False,
    shift_enter: bool = False,
) -> bool:
    """
    Type text into an input field.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        text (str): Text to type.
        clear_first (bool): Clear field before typing.
        delay_ms (int): Delay between keystrokes in milliseconds.
        parse_newlines (bool): If True, parse \n as Enter key presses.
        shift_enter (bool): If True, use Shift+Enter instead of Enter (for chat apps).

    Returns:
        bool: True if typed successfully.
    """
    if isinstance(delay_ms, str):
        delay_ms = int(delay_ms)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        dom_handler.type_text(
            tab, selector, text, clear_first, delay_ms, parse_newlines, shift_enter
        ),
        timeout=60,
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def paste_text(
    instance_id: str, selector: str, text: str, clear_first: bool = True
) -> bool:
    """
    Paste text instantly into an input field.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        text (str): Text to paste.
        clear_first (bool): Clear field before pasting.

    Returns:
        bool: True if pasted successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        dom_handler.paste_text(tab, selector, text, clear_first),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def select_option(
    instance_id: str,
    selector: str,
    value: str | None = None,
    text: str | None = None,
    index: Any | None = None,
) -> bool:
    """
    Select an option from a dropdown.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the select element.
        value (Optional[str]): Option value attribute.
        text (Optional[str]): Option text content.
        index (Optional[Any]): Option index (0-based). Can be string or int.

    Returns:
        bool: True if selected successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")

    converted_index = None
    if index is not None:
        try:
            converted_index = int(index)
        except (ValueError, TypeError):
            raise Exception(f"Invalid index value: {index}. Must be a number.")

    return await _with_cdp_timeout(
        dom_handler.select_option(tab, selector, value, text, converted_index),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def get_element_state(instance_id: str, selector: str) -> dict[str, Any]:
    """
    Get complete state of an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.

    Returns:
        Dict[str, Any]: Element state including attributes, style, position, etc.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        dom_handler.get_element_state(tab, selector), instance_id=instance_id
    )


@section_tool("element-interaction")
async def wait_for_element(
    instance_id: str,
    selector: str,
    timeout: int = 30000,
    visible: bool = True,
    text_content: str | None = None,
) -> bool:
    """
    Wait for an element to appear.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector or XPath.
        timeout (int): Timeout in ms (default 30000, max 60000). Most elements appear within 5-10s — only increase for very slow async content. Values above 60000 are capped.
        visible (bool): Wait for element to be visible.
        text_content (Optional[str]): Wait for specific text content.

    Returns:
        bool: True if element found.
    """
    timeout = _clamp_timeout(timeout, default=30_000)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        dom_handler.wait_for_element(tab, selector, timeout, visible, text_content),
        timeout=max(timeout / 1000 + 5, CDP_OPERATION_TIMEOUT),
        instance_id=instance_id,
    )


@section_tool("element-interaction")
async def scroll_page(
    instance_id: str, direction: str = "down", amount: int = 500, smooth: bool = True
) -> bool:
    """
    Scroll the page.

    Args:
        instance_id (str): Browser instance ID.
        direction (str): 'down', 'up', 'left', 'right', 'top', or 'bottom'.
        amount (int): Pixels to scroll (ignored for 'top' and 'bottom').
        smooth (bool): Use smooth scrolling.

    Returns:
        bool: True if scrolled successfully.
    """
    if isinstance(amount, str):
        amount = int(amount)
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        dom_handler.scroll_page(tab, direction, amount, smooth), instance_id=instance_id
    )


@section_tool("element-interaction")
async def execute_script(
    instance_id: str,
    script: str,
    args: list[Any] | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """
    Execute JavaScript in the page and return its value.

    ⚠️ Async, non-blocking code only. The script runs on the page's main thread,
    so anything that blocks it freezes the whole tab and makes every later call
    time out. Specifically:
      • NEVER use synchronous XHR — `xhr.open(url, false)`. Use `await fetch(url)`.
      • NEVER use infinite/blocking loops — `while(true)`, `for(;;)`, busy-waits.
      • NEVER call `alert()` / `confirm()` / `prompt()` — they block automation.
      • To UPLOAD FILES, use the `upload_file` tool — do NOT fetch blobs or build
        base64/DataTransfer here (mixed-content/CORS limits and can freeze the page).
      • Keep scripts small (< ~100KB); don't inline large payloads.

    Args:
        instance_id (str): Browser instance ID.
        script (str): JavaScript to execute. Must be non-blocking.
        args (Optional[List[Any]]): Arguments passed to the script body.
        timeout_ms (Optional[int]): Max run time in ms (default 10000, max 60000).
            A blocking script is killed at this limit instead of hanging the tab.

    Returns:
        Dict[str, Any]: {"success": bool, "result": Any, "error": Optional[str]}.
    """
    rejection = _script_rejection_reason(script)
    if rejection:
        return {"success": False, "result": None, "error": rejection}
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    if timeout_ms is not None:
        timeout_s = (
            _clamp_timeout(timeout_ms, default=int(EXECUTE_SCRIPT_TIMEOUT * 1000))
            / 1000
        )
    else:
        timeout_s = EXECUTE_SCRIPT_TIMEOUT
    try:
        result = await _with_cdp_timeout(
            dom_handler.execute_script(tab, script, args),
            timeout=timeout_s,
            instance_id=instance_id,
        )
        return {"success": True, "result": result, "error": None}
    except Exception as e:
        return {"success": False, "result": None, "error": str(e)}


@section_tool("element-interaction")
async def get_page_content(
    instance_id: str, include_frames: bool = False
) -> dict[str, Any]:
    """
    Get page HTML and text content.

    Args:
        instance_id (str): Browser instance ID.
        include_frames (bool): Include iframe information.

    Returns:
        Dict[str, Any]: Page content including HTML, text, and metadata.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    content = await _with_cdp_timeout(
        dom_handler.get_page_content(tab, include_frames), instance_id=instance_id
    )

    return response_handler.handle_response(
        content,
        "page_content",
        {"instance_id": instance_id, "include_frames": include_frames},
    )


@section_tool("element-interaction")
async def take_screenshot(
    instance_id: str,
    full_page: bool = False,
    format: str = "png",
    file_path: str | None = None,
) -> str | dict[str, Any]:
    """
    Take a screenshot of the page.

    Args:
        instance_id (str): Browser instance ID.
        full_page (bool): Capture full page (not just viewport).
        format (str): Image format ('png' or 'jpeg').
        file_path (Optional[str]): Optional file path to save screenshot to.

    Returns:
        Union[str, Dict]: File path if file_path provided, otherwise optimized base64 data or file info dict.
    """
    import io

    from PIL import Image

    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")

    if file_path:
        save_path = Path(file_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        await _with_cdp_timeout(tab.save_screenshot(save_path), instance_id=instance_id)
        return f"Screenshot saved. AI agents should use the Read tool to view this image: {save_path.absolute()!s}"

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        await _with_cdp_timeout(tab.save_screenshot(tmp_path), instance_id=instance_id)

        with Image.open(tmp_path) as img:
            if img.mode in ("RGBA", "LA", "P") and format.lower() == "jpeg":
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(
                    img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None
                )
                img = background

            output_buffer = io.BytesIO()

            if format.lower() == "jpeg":
                img.save(output_buffer, format="JPEG", quality=85, optimize=True)
            else:
                img.save(output_buffer, format="PNG", optimize=True)

            compressed_bytes = output_buffer.getvalue()

            base64_size = len(compressed_bytes) * 1.33
            estimated_tokens = int(base64_size / 4)

            if estimated_tokens > 20000:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_filename = (
                    f"screenshot_{timestamp}_{instance_id[:8]}.{format.lower()}"
                )
                screenshot_path = response_handler.clone_dir / screenshot_filename

                with open(screenshot_path, "wb") as f:
                    f.write(compressed_bytes)

                file_size_kb = len(compressed_bytes) / 1024
                return {
                    "file_path": str(screenshot_path),
                    "filename": screenshot_filename,
                    "file_size_kb": round(file_size_kb, 2),
                    "estimated_tokens": estimated_tokens,
                    "reason": "Screenshot too large, automatically saved to file",
                    "message": f"Screenshot saved. AI agents should use the Read tool to view this image: {screenshot_path!s}",
                }

            return base64.b64encode(compressed_bytes).decode("utf-8")

    finally:
        if tmp_path.exists():
            os.unlink(tmp_path)


@section_tool("network-debugging")
async def list_network_requests(
    instance_id: str, filter_type: str | None = None
) -> list[dict[str, Any]] | dict[str, Any]:
    """
    List captured network requests.

    Args:
        instance_id (str): Browser instance ID.
        filter_type (Optional[str]): Filter by resource type (e.g., 'image', 'script', 'xhr').

    Returns:
        Union[List[Dict[str, Any]], Dict[str, Any]]: List of network requests, or file metadata if response too large.
    """
    requests = await network_interceptor.list_requests(instance_id, filter_type)
    formatted_requests = [
        {
            "request_id": req.request_id,
            "url": req.url,
            "method": req.method,
            "resource_type": req.resource_type,
            "timestamp": req.timestamp.isoformat(),
        }
        for req in requests
    ]

    return response_handler.handle_response(formatted_requests, "network_requests")


@section_tool("network-debugging")
async def get_request_details(request_id: str) -> dict[str, Any] | None:
    """
    Get detailed information about a network request.

    Args:
        request_id (str): Network request ID.

    Returns:
        Optional[Dict[str, Any]]: Request details including headers, cookies, and body.
    """
    request = await network_interceptor.get_request(request_id)
    if request:
        return request.dict()
    return None


@section_tool("network-debugging")
async def get_response_details(request_id: str) -> dict[str, Any] | None:
    """
    Get response details for a network request.

    Args:
        request_id (str): Network request ID.

    Returns:
        Optional[Dict[str, Any]]: Response details including status, headers, and metadata.
    """
    response = await network_interceptor.get_response(request_id)
    if response:
        return response.dict()
    return None


@section_tool("network-debugging")
async def get_response_content(instance_id: str, request_id: str) -> str | None:
    """
    Get response body content.

    Args:
        instance_id (str): Browser instance ID.
        request_id (str): Network request ID.

    Returns:
        Optional[str]: Response body as text (base64 encoded for binary).
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    body = await _with_cdp_timeout(
        network_interceptor.get_response_body(tab, request_id), instance_id=instance_id
    )
    if body:
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            import base64

            return base64.b64encode(body).decode("utf-8")
    return None


@section_tool("network-debugging")
async def search_network_requests(
    instance_id: str,
    url_pattern: str | None = None,
    method: str | None = None,
    status_code: int | None = None,
    response_contains: str | None = None,
    payload_contains: str | None = None,
    resource_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Search network requests with advanced filters and pagination.

    Args:
        instance_id (str): Browser instance ID.
        url_pattern (Optional[str]): Filter by URL pattern (substring match).
        method (Optional[str]): Filter by HTTP method.
        status_code (Optional[int]): Filter by response status code.
        response_contains (Optional[str]): Search in response body.
        payload_contains (Optional[str]): Search in request payload.
        resource_type (Optional[str]): Filter by resource type.
        limit (int): Max results per page.
        offset (int): Starting index for pagination.

    Returns:
        Dict[str, Any]: Paginated results with metadata.
    """
    return await network_interceptor.search_requests(
        instance_id,
        url_pattern,
        method,
        status_code,
        response_contains,
        payload_contains,
        resource_type,
        limit,
        offset,
    )


@section_tool("network-debugging")
async def export_network_data(instance_id: str, filepath: str) -> bool:
    """
    Export network data to JSON file.

    Args:
        instance_id (str): Browser instance ID.
        filepath (str): Path to save JSON file.

    Returns:
        bool: True if successful.
    """
    return await network_interceptor.export_to_json(instance_id, filepath)


@section_tool("network-debugging")
async def import_network_data(instance_id: str, filepath: str) -> bool:
    """
    Import network data from JSON file.

    Args:
        instance_id (str): Browser instance ID.
        filepath (str): Path to JSON file.

    Returns:
        bool: True if successful.
    """
    return await network_interceptor.import_from_json(instance_id, filepath)


@section_tool("network-debugging")
async def set_network_capture_filters(
    instance_id: str,
    include_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
) -> bool:
    """
    Set resource type filters for network capture to reduce memory usage.

    Args:
        instance_id (str): Browser instance ID.
        include_types (Optional[List[str]]): Only capture these types (e.g., ['XHR', 'Fetch', 'Document']).
        exclude_types (Optional[List[str]]): Exclude these types (e.g., ['Image', 'Stylesheet', 'Font', 'Script']).

    Common resource types: Document, Stylesheet, Image, Media, Font, Script, XHR, Fetch, WebSocket, Manifest, Other

    Returns:
        bool: True if successful.
    """
    await network_interceptor.set_capture_filters(
        instance_id, include_types, exclude_types
    )
    return True


@section_tool("network-debugging")
async def get_network_capture_filters(instance_id: str) -> dict[str, list[str]]:
    """
    Get current network capture filters.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Dict[str, List[str]]: Current filters with 'include' and 'exclude' lists.
    """
    return await network_interceptor.get_capture_filters(instance_id)


@section_tool("network-debugging")
async def modify_headers(instance_id: str, headers: dict[str, str]) -> bool:
    """
    Modify request headers for future requests.

    Args:
        instance_id (str): Browser instance ID.
        headers (Dict[str, str]): Headers to add/modify.

    Returns:
        bool: True if modified successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        network_interceptor.modify_headers(tab, headers), instance_id=instance_id
    )


@section_tool("cookies-storage")
async def get_cookies(
    instance_id: str, urls: list[str] | None = None
) -> list[dict[str, Any]]:
    """
    Get cookies for current page or specific URLs.

    Args:
        instance_id (str): Browser instance ID.
        urls (Optional[List[str]]): Optional list of URLs to get cookies for.

    Returns:
        List[Dict[str, Any]]: List of cookies.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        network_interceptor.get_cookies(tab, urls), instance_id=instance_id
    )


@section_tool("cookies-storage")
async def set_cookie(
    instance_id: str,
    name: str,
    value: str,
    url: str | None = None,
    domain: str | None = None,
    path: str = "/",
    secure: bool = False,
    http_only: bool = False,
    same_site: str | None = None,
) -> bool:
    """
    Set a cookie.

    Args:
        instance_id (str): Browser instance ID.
        name (str): Cookie name.
        value (str): Cookie value.
        url (Optional[str]): The request-URI to associate with the cookie.
        domain (Optional[str]): Cookie domain.
        path (str): Cookie path.
        secure (bool): Secure flag.
        http_only (bool): HttpOnly flag.
        same_site (Optional[str]): SameSite attribute ('Strict', 'Lax', or 'None').

    Returns:
        bool: True if set successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")

    if not url and not domain:
        current_url = tab.url if hasattr(tab, "url") else None
        if current_url:
            url = current_url
        else:
            raise Exception("At least one of 'url' or 'domain' must be specified")

    cookie = {
        "name": name,
        "value": value,
        "path": path,
        "secure": secure,
        "http_only": http_only,
    }
    if url:
        cookie["url"] = url
    if domain:
        cookie["domain"] = domain
    if same_site:
        cookie["same_site"] = same_site
    return await _with_cdp_timeout(
        network_interceptor.set_cookie(tab, cookie), instance_id=instance_id
    )


@section_tool("cookies-storage")
async def clear_cookies(instance_id: str, url: str | None = None) -> bool:
    """
    Clear cookies.

    Args:
        instance_id (str): Browser instance ID.
        url (Optional[str]): Optional URL to clear cookies for (clears all if not specified).

    Returns:
        bool: True if cleared successfully.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        network_interceptor.clear_cookies(tab, url), instance_id=instance_id
    )


@mcp.resource("browser://{instance_id}/state")
async def get_browser_state_resource(instance_id: str) -> str:
    """
    Get current state of a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of the browser state or error message.
    """
    state = await browser_manager.get_page_state(instance_id)
    if state:
        return json.dumps(state.dict(), indent=2)
    return json.dumps({"error": "Instance not found"})


@mcp.resource("browser://{instance_id}/cookies")
async def get_cookies_resource(instance_id: str) -> str:
    """
    Get cookies for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of cookies or error message.
    """
    tab = await browser_manager.get_tab(instance_id)
    if tab:
        cookies = await network_interceptor.get_cookies(tab)
        return json.dumps(cookies, indent=2)
    return json.dumps({"error": "Instance not found"})


@mcp.resource("browser://{instance_id}/network")
async def get_network_resource(instance_id: str) -> str:
    """
    Get network requests for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of network requests.
    """
    requests = await network_interceptor.list_requests(instance_id)
    return json.dumps([req.dict() for req in requests], indent=2)


@mcp.resource("browser://{instance_id}/console")
async def get_console_resource(instance_id: str) -> str:
    """
    Get console logs for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        str: JSON string of console logs or error message.
    """
    state = await browser_manager.get_page_state(instance_id)
    if state:
        return json.dumps(state.console_logs, indent=2)
    return json.dumps({"error": "Instance not found"})


@section_tool("debugging")
async def get_debug_view(
    max_errors: int = 50,
    max_warnings: int = 50,
    max_info: int = 50,
    include_all: bool = False,
) -> dict[str, Any]:
    """
    Get comprehensive debug view with all logged errors and statistics.

    Args:
        max_errors (int): Maximum number of errors to include (default: 50).
        max_warnings (int): Maximum number of warnings to include (default: 50).
        max_info (int): Maximum number of info logs to include (default: 50).
        include_all (bool): Include all logs regardless of limits (default: False).

    Returns:
        Dict[str, Any]: Debug information including errors, warnings, and statistics.
    """
    debug_data = debug_logger.get_debug_view_paginated(
        max_errors=max_errors if not include_all else None,
        max_warnings=max_warnings if not include_all else None,
        max_info=max_info if not include_all else None,
    )
    return debug_data


@section_tool("debugging")
async def clear_debug_view() -> bool:
    """
    Clear all debug logs and statistics with timeout protection.

    Returns:
        bool: True if cleared successfully.
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(debug_logger.clear_debug_view_safe), timeout=10.0
        )
        return True
    except TimeoutError:
        return False


@section_tool("debugging")
async def export_debug_logs(
    filename: str = "debug_log.json",
    max_errors: int = 100,
    max_warnings: int = 100,
    max_info: int = 100,
    include_all: bool = False,
    format: str = "auto",
) -> str:
    """
    Export debug logs to a file using the fastest available method with timeout protection.

    Args:
        filename (str): Name of the file to export to.
        max_errors (int): Maximum number of errors to export (default: 100).
        max_warnings (int): Maximum number of warnings to export (default: 100).
        max_info (int): Maximum number of info logs to export (default: 100).
        include_all (bool): Include all logs regardless of limits (default: False).
        format (str): Export format: 'json', 'pickle', 'gzip-pickle', 'auto' (default: 'auto').
                     'auto' chooses fastest format based on data size:
                     - Small data (<100 items): JSON (human readable)
                     - Medium data (100-1000 items): Pickle (fast binary)
                     - Large data (>1000 items): Gzip-Pickle (fastest, compressed)

    Returns:
        str: Path to the exported file.
    """
    try:
        filepath = await asyncio.wait_for(
            asyncio.to_thread(
                debug_logger.export_to_file_paginated,
                filename,
                max_errors if not include_all else None,
                max_warnings if not include_all else None,
                max_info if not include_all else None,
                format,
            ),
            timeout=30.0,
        )
        return filepath
    except TimeoutError:
        return "Export timeout - file too large. Try with smaller limits or 'gzip-pickle' format."


@section_tool("debugging")
async def get_debug_lock_status() -> dict[str, Any]:
    """
    Get current debug logger lock status for debugging hanging exports.

    Returns:
        Dict[str, Any]: Lock status information.
    """
    try:
        return debug_logger.get_lock_status()
    except Exception as e:
        return {"error": str(e)}


@section_tool("tabs")
async def list_tabs(instance_id: str) -> list[dict[str, str]]:
    """
    List all tabs for a browser instance.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        List[Dict[str, str]]: List of tabs with their details.
    """
    return await _with_cdp_timeout(
        browser_manager.list_tabs(instance_id), instance_id=instance_id
    )


@section_tool("tabs")
async def switch_tab(instance_id: str, tab_id: str) -> bool:
    """
    Switch to a specific tab by bringing it to front.

    Args:
        instance_id (str): Browser instance ID.
        tab_id (str): Target tab ID to switch to.

    Returns:
        bool: True if switched successfully.
    """
    return await _with_cdp_timeout(
        browser_manager.switch_to_tab(instance_id, tab_id), instance_id=instance_id
    )


@section_tool("tabs")
async def close_tab(instance_id: str, tab_id: str) -> bool:
    """
    Close a specific tab.

    Args:
        instance_id (str): Browser instance ID.
        tab_id (str): Tab ID to close.

    Returns:
        bool: True if closed successfully.
    """
    return await _with_cdp_timeout(
        browser_manager.close_tab(instance_id, tab_id), instance_id=instance_id
    )


@section_tool("tabs")
async def get_active_tab(instance_id: str) -> dict[str, Any]:
    """
    Get information about the currently active tab.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        Dict[str, Any]: Active tab information.
    """
    tab = await _with_cdp_timeout(
        browser_manager.get_active_tab(instance_id), instance_id=instance_id
    )
    if not tab:
        return {"error": "No active tab found"}
    await _with_cdp_timeout(tab, instance_id=instance_id)
    return {
        "tab_id": str(tab.target.target_id),
        "url": getattr(tab, "url", "") or "",
        "title": getattr(tab.target, "title", "") or "Untitled",
        "type": getattr(tab.target, "type_", "page"),
    }


@section_tool("tabs")
async def new_tab(instance_id: str, url: str = "about:blank") -> dict[str, Any]:
    """
    Open a new tab in the browser instance.

    Args:
        instance_id (str): Browser instance ID.
        url (str): URL to open in the new tab.

    Returns:
        Dict[str, Any]: New tab information.
    """
    browser = await browser_manager.get_browser(instance_id)
    if not browser:
        raise Exception(f"Instance not found: {instance_id}")
    try:
        new_tab_obj = await _with_cdp_timeout(
            browser.get(url, new_tab=True), instance_id=instance_id
        )
        await _with_cdp_timeout(new_tab_obj, instance_id=instance_id)
        return {
            "tab_id": str(new_tab_obj.target.target_id),
            "url": getattr(new_tab_obj, "url", "") or url,
            "title": getattr(new_tab_obj.target, "title", "") or "New Tab",
            "type": getattr(new_tab_obj.target, "type_", "page"),
        }
    except Exception as e:
        raise Exception(f"Failed to create new tab: {e!s}")


@section_tool("element-extraction")
async def extract_element_styles(
    instance_id: str,
    selector: str,
    include_computed: bool = True,
    include_css_rules: bool = True,
    include_pseudo: bool = True,
    include_inheritance: bool = False,
) -> dict[str, Any]:
    """
    Extract complete styling information from an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_computed (bool): Include computed styles.
        include_css_rules (bool): Include matching CSS rules.
        include_pseudo (bool): Include pseudo-element styles (::before, ::after).
        include_inheritance (bool): Include style inheritance chain.

    Returns:
        Dict[str, Any]: Complete styling data including computed styles, CSS rules, pseudo-elements.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        element_cloner.extract_element_styles(
            tab,
            selector=selector,
            include_computed=include_computed,
            include_css_rules=include_css_rules,
            include_pseudo=include_pseudo,
            include_inheritance=include_inheritance,
        ),
        instance_id=instance_id,
    )


@section_tool("element-extraction")
async def extract_element_structure(
    instance_id: str,
    selector: str,
    include_children: bool = False,
    include_attributes: bool = True,
    include_data_attributes: bool = True,
    max_depth: int = 3,
) -> dict[str, Any]:
    """
    Extract complete HTML structure and DOM information.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Include child elements.
        include_attributes (bool): Include all attributes.
        include_data_attributes (bool): Include data-* attributes specifically.
        max_depth (int): Maximum depth for children extraction.

    Returns:
        Dict[str, Any]: HTML structure, attributes, position, and children data.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        element_cloner.extract_element_structure(
            tab,
            selector=selector,
            include_children=include_children,
            include_attributes=include_attributes,
            include_data_attributes=include_data_attributes,
            max_depth=max_depth,
        ),
        instance_id=instance_id,
    )


@section_tool("element-extraction")
async def extract_element_events(
    instance_id: str,
    selector: str,
    include_inline: bool = True,
    include_listeners: bool = True,
    include_framework: bool = True,
    analyze_handlers: bool = False,
) -> dict[str, Any]:
    """
    Extract complete event listener and JavaScript handler information.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_inline (bool): Include inline event handlers (onclick, etc.).
        include_listeners (bool): Include addEventListener attached handlers.
        include_framework (bool): Include framework-specific handlers (React, Vue, etc.).
        analyze_handlers (bool): Analyze handler functions for full details (can be large).

    Returns:
        Dict[str, Any]: Event listeners, inline handlers, framework handlers, detected frameworks.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        element_cloner.extract_element_events(
            tab,
            selector=selector,
            include_inline=include_inline,
            include_listeners=include_listeners,
            include_framework=include_framework,
            analyze_handlers=analyze_handlers,
        ),
        instance_id=instance_id,
    )


@section_tool("element-extraction")
async def extract_element_animations(
    instance_id: str,
    selector: str,
    include_css_animations: bool = True,
    include_transitions: bool = True,
    include_transforms: bool = True,
    analyze_keyframes: bool = True,
) -> dict[str, Any]:
    """
    Extract CSS animations, transitions, and transforms.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_css_animations (bool): Include CSS @keyframes animations.
        include_transitions (bool): Include CSS transitions.
        include_transforms (bool): Include CSS transforms.
        analyze_keyframes (bool): Analyze keyframe rules.

    Returns:
        Dict[str, Any]: Animation data, transition data, transform data, keyframe rules.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        element_cloner.extract_element_animations(
            tab,
            selector=selector,
            include_css_animations=include_css_animations,
            include_transitions=include_transitions,
            include_transforms=include_transforms,
            analyze_keyframes=analyze_keyframes,
        ),
        instance_id=instance_id,
    )


@section_tool("element-extraction")
async def extract_element_assets(
    instance_id: str,
    selector: str,
    include_images: bool = True,
    include_backgrounds: bool = True,
    include_fonts: bool = True,
    fetch_external: bool = False,
) -> dict[str, Any]:
    """
    Extract all assets related to an element (images, fonts, etc.).

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_images (bool): Include img src and related images.
        include_backgrounds (bool): Include background images.
        include_fonts (bool): Include font information.
        fetch_external (bool): Whether to fetch external assets for analysis.

    Returns:
        Dict[str, Any]: Images, background images, fonts, icons, videos, audio assets.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    result = await _with_cdp_timeout(
        element_cloner.extract_element_assets(
            tab,
            selector=selector,
            include_images=include_images,
            include_backgrounds=include_backgrounds,
            include_fonts=include_fonts,
            fetch_external=fetch_external,
        ),
        instance_id=instance_id,
    )
    # handle_response is synchronous — awaiting its dict return raises TypeError.
    return response_handler.handle_response(
        result, f"element_assets_{instance_id}_{selector.replace(' ', '_')}"
    )


@section_tool("element-extraction")
async def extract_element_styles_cdp(
    instance_id: str,
    selector: str,
    include_computed: bool = True,
    include_css_rules: bool = True,
    include_pseudo: bool = True,
    include_inheritance: bool = False,
) -> dict[str, Any]:
    """
    Extract element styles using direct CDP calls (no JavaScript evaluation).
    This prevents hanging issues by using nodriver's native CDP methods.

    Args:
        instance_id (str): Browser instance ID
        selector (str): CSS selector for the element
        include_computed (bool): Include computed styles
        include_css_rules (bool): Include matching CSS rules
        include_pseudo (bool): Include pseudo-element styles
        include_inheritance (bool): Include style inheritance chain

    Returns:
        Dict[str, Any]: Styling data extracted using CDP
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        element_cloner.extract_element_styles_cdp(
            tab,
            selector=selector,
            include_computed=include_computed,
            include_css_rules=include_css_rules,
            include_pseudo=include_pseudo,
            include_inheritance=include_inheritance,
        ),
        instance_id=instance_id,
    )


@section_tool("element-extraction")
async def extract_related_files(
    instance_id: str,
    analyze_css: bool = True,
    analyze_js: bool = True,
    follow_imports: bool = False,
    max_depth: int = 2,
) -> dict[str, Any]:
    """
    Discover and analyze related CSS/JS files for context.

    Args:
        instance_id (str): Browser instance ID.
        analyze_css (bool): Analyze linked CSS files.
        analyze_js (bool): Analyze linked JS files.
        follow_imports (bool): Follow @import and module imports (uses network).
        max_depth (int): Maximum depth for following imports.

    Returns:
        Dict[str, Any]: Stylesheets, scripts, imports, modules, framework detection.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    result = await _with_cdp_timeout(
        element_cloner.extract_related_files(
            tab,
            analyze_css=analyze_css,
            analyze_js=analyze_js,
            follow_imports=follow_imports,
            max_depth=max_depth,
        ),
        instance_id=instance_id,
    )
    # handle_response is synchronous — awaiting its dict return raises TypeError.
    return response_handler.handle_response(result, f"related_files_{instance_id}")


@section_tool("element-extraction")
async def clone_element_complete(
    instance_id: str, selector: str, extraction_options: str | None = None
) -> dict[str, Any]:
    """
    Master function that extracts ALL element data using specialized functions.

    This is the ultimate element cloning tool that combines all extraction methods.
    Use this when you want complete element fidelity for recreation or analysis.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        extraction_options (Optional[str]): Dict specifying what to extract and options for each.
            Example: {
                'styles': {'include_computed': True, 'include_pseudo': True},
                'structure': {'include_children': True, 'max_depth': 2},
                'events': {'include_framework': True, 'analyze_handlers': False},
                'animations': {'analyze_keyframes': True},
                'assets': {'fetch_external': False},
                'related_files': {'follow_imports': True, 'max_depth': 1}
            }

    Returns:
        Dict[str, Any]: Complete element clone with styles, structure, events, animations, assets, related files.
    """
    parsed_options = None
    if extraction_options:
        try:
            parsed_options = json.loads(extraction_options)
        except json.JSONDecodeError:
            raise Exception(f"Invalid JSON in extraction_options: {extraction_options}")
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    result = await _with_cdp_timeout(
        comprehensive_element_cloner.extract_complete_element(
            tab,
            selector=selector,
            include_children=parsed_options.get("structure", {}).get(
                "include_children", True
            )
            if parsed_options
            else True,
        ),
        instance_id=instance_id,
    )

    return response_handler.handle_response(
        result,
        fallback_filename_prefix="complete_clone",
        metadata={
            "selector": selector,
            "extraction_options": parsed_options,
            "url": getattr(tab, "url", "unknown"),
        },
    )


@section_tool("debugging")
async def validate_browser_environment_tool() -> dict[str, Any]:
    """
    Validate browser environment and diagnose potential issues.

    Returns:
        Dict[str, Any]: Environment validation results with platform info and recommendations
    """
    try:
        return validate_browser_environment()
    except Exception as e:
        return {
            "error": str(e),
            "platform_info": get_platform_info(),
            "is_ready": False,
            "issues": [f"Validation failed: {e!s}"],
            "warnings": [],
        }


@section_tool("progressive-cloning")
async def clone_element_progressive(
    instance_id: str, selector: str, include_children: bool = True
) -> dict[str, Any]:
    """
    Clone element progressively - returns lightweight base structure with element_id.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Whether to extract child elements.

    Returns:
        Dict[str, Any]: Base structure with element_id for progressive expansion.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        progressive_element_cloner.clone_element_progressive(
            tab, selector, include_children
        ),
        instance_id=instance_id,
    )


@section_tool("progressive-cloning")
async def expand_styles(
    element_id: str,
    categories: list[str] | None = None,
    properties: list[str] | None = None,
) -> dict[str, Any]:
    """
    Expand styles data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        categories (Optional[List[str]]): Style categories to include (layout, typography, colors, spacing, borders, backgrounds, effects, animation).
        properties (Optional[List[str]]): Specific CSS property names to include.

    Returns:
        Dict[str, Any]: Filtered styles data.
    """
    return progressive_element_cloner.expand_styles(element_id, categories, properties)


@section_tool("progressive-cloning")
async def expand_events(
    element_id: str, event_types: list[str] | None = None
) -> dict[str, Any]:
    """
    Expand event listeners data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        event_types (Optional[List[str]]): Event types or sources to include (click, react, inline, addEventListener).

    Returns:
        Dict[str, Any]: Filtered event listeners data.
    """
    return progressive_element_cloner.expand_events(element_id, event_types)


@section_tool("progressive-cloning")
async def expand_children(
    element_id: str, depth_range: list | None = None, max_count: Any | None = None
) -> dict[str, Any]:
    """
    Expand children data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        depth_range (Optional[List]): [min_depth, max_depth] range to include.
        max_count (Optional[Any]): Maximum number of children to return.

    Returns:
        Dict[str, Any]: Filtered children data.
    """
    if isinstance(max_count, str):
        try:
            max_count = int(max_count) if max_count else None
        except ValueError:
            return {"error": f"Invalid max_count value: {max_count}"}

    if isinstance(depth_range, list):
        try:
            depth_range = [int(x) if isinstance(x, str) else x for x in depth_range]
        except ValueError:
            return {"error": f"Invalid depth_range values: {depth_range}"}

    depth_tuple = tuple(depth_range) if depth_range else None

    result = progressive_element_cloner.expand_children(
        element_id, depth_tuple, max_count
    )
    return response_handler.handle_response(result, f"expand_children_{element_id}")


@section_tool("progressive-cloning")
async def expand_css_rules(
    element_id: str, source_types: list[str] | None = None
) -> dict[str, Any]:
    """
    Expand CSS rules data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        source_types (Optional[List[str]]): CSS rule sources to include (inline, external stylesheet URLs).

    Returns:
        Dict[str, Any]: Filtered CSS rules data.
    """
    return progressive_element_cloner.expand_css_rules(element_id, source_types)


@section_tool("progressive-cloning")
async def expand_pseudo_elements(element_id: str) -> dict[str, Any]:
    """
    Expand pseudo-elements data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().

    Returns:
        Dict[str, Any]: Pseudo-elements data (::before, ::after, etc.).
    """
    return progressive_element_cloner.expand_pseudo_elements(element_id)


@section_tool("progressive-cloning")
async def expand_animations(element_id: str) -> dict[str, Any]:
    """
    Expand animations and fonts data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().

    Returns:
        Dict[str, Any]: Animations, transitions, and fonts data.
    """
    return progressive_element_cloner.expand_animations(element_id)


@section_tool("progressive-cloning")
async def list_stored_elements() -> dict[str, Any]:
    """
    List all stored elements with their basic info.

    Returns:
        Dict[str, Any]: List of stored elements with metadata.
    """
    return progressive_element_cloner.list_stored_elements()


@section_tool("progressive-cloning")
async def clear_stored_element(element_id: str) -> dict[str, Any]:
    """
    Clear a specific stored element.

    Args:
        element_id (str): Element ID to clear.

    Returns:
        Dict[str, Any]: Success/error message.
    """
    return progressive_element_cloner.clear_stored_element(element_id)


@section_tool("progressive-cloning")
async def clear_all_elements() -> dict[str, Any]:
    """
    Clear all stored elements.

    Returns:
        Dict[str, Any]: Success message.
    """
    return progressive_element_cloner.clear_all_elements()


@section_tool("file-extraction")
async def clone_element_to_file(
    instance_id: str, selector: str, extraction_options: str | None = None
) -> dict[str, Any]:
    """
    Clone element completely and save to file, returning file path instead of full data.

    This is ideal when you want complete element data but don't want to overwhelm
    the response with large JSON objects. The data is saved to a JSON file that
    can be read later.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        extraction_options (Optional[str]): JSON string with extraction options.

    Returns:
        Dict[str, Any]: File path and summary information about the cloned element.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    parsed_options = None
    if extraction_options:
        try:
            parsed_options = json.loads(extraction_options)
        except json.JSONDecodeError:
            return {"error": "Invalid extraction_options JSON"}
    return await _with_cdp_timeout(
        file_based_element_cloner.clone_element_complete_to_file(
            tab, selector=selector, extraction_options=parsed_options
        ),
        instance_id=instance_id,
    )


@section_tool("file-extraction")
async def extract_complete_element_to_file(
    instance_id: str, selector: str, include_children: bool = True
) -> dict[str, Any]:
    """
    Extract complete element using working comprehensive cloner and save to file.

    This uses the proven comprehensive extraction logic that returns large amounts
    of data, but saves it to a file instead of overwhelming the response.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Whether to include child elements.

    Returns:
        Dict[str, Any]: File path and concise summary instead of massive data dump.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        file_based_element_cloner.extract_complete_element_to_file(
            tab, selector, include_children
        ),
        instance_id=instance_id,
    )


@section_tool("element-extraction")
async def extract_complete_element_cdp(
    instance_id: str, selector: str, include_children: bool = True
) -> dict[str, Any]:
    """
    Extract complete element using native CDP methods for 100% accuracy.

    This uses Chrome DevTools Protocol's native methods to extract:
    - Complete computed styles via CSS.getComputedStyleForNode
    - Matched CSS rules via CSS.getMatchedStylesForNode
    - Event listeners via DOMDebugger.getEventListeners
    - Complete DOM structure and attributes

    This provides the most accurate element cloning possible by bypassing
    JavaScript limitations and using CDP's direct browser access.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Whether to include child elements.

    Returns:
        Dict[str, Any]: Complete element data with 100% accuracy.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    cdp_cloner = CDPElementCloner()
    return await _with_cdp_timeout(
        cdp_cloner.extract_complete_element_cdp(tab, selector, include_children),
        instance_id=instance_id,
    )


@section_tool("file-extraction")
async def extract_element_styles_to_file(
    instance_id: str,
    selector: str,
    include_computed: bool = True,
    include_css_rules: bool = True,
    include_pseudo: bool = True,
    include_inheritance: bool = False,
) -> dict[str, Any]:
    """
    Extract element styles and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_computed (bool): Include computed styles.
        include_css_rules (bool): Include matching CSS rules.
        include_pseudo (bool): Include pseudo-element styles.
        include_inheritance (bool): Include style inheritance chain.

    Returns:
        Dict[str, Any]: File path and summary of extracted styles.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        file_based_element_cloner.extract_element_styles_to_file(
            tab,
            selector=selector,
            include_computed=include_computed,
            include_css_rules=include_css_rules,
            include_pseudo=include_pseudo,
            include_inheritance=include_inheritance,
        ),
        instance_id=instance_id,
    )


@section_tool("file-extraction")
async def extract_element_structure_to_file(
    instance_id: str,
    selector: str,
    include_children: bool = False,
    include_attributes: bool = True,
    include_data_attributes: bool = True,
    max_depth: int = 3,
) -> dict[str, Any]:
    """
    Extract element structure and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Include child elements.
        include_attributes (bool): Include all attributes.
        include_data_attributes (bool): Include data-* attributes.
        max_depth (int): Maximum depth for children extraction.

    Returns:
        Dict[str, Any]: File path and summary of extracted structure.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        file_based_element_cloner.extract_element_structure_to_file(
            tab,
            selector=selector,
            include_children=include_children,
            include_attributes=include_attributes,
            include_data_attributes=include_data_attributes,
            max_depth=max_depth,
        ),
        instance_id=instance_id,
    )


@section_tool("file-extraction")
async def extract_element_events_to_file(
    instance_id: str,
    selector: str,
    include_inline: bool = True,
    include_listeners: bool = True,
    include_framework: bool = True,
    analyze_handlers: bool = True,
) -> dict[str, Any]:
    """
    Extract element events and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_inline (bool): Include inline event handlers.
        include_listeners (bool): Include addEventListener handlers.
        include_framework (bool): Include framework-specific handlers.
        analyze_handlers (bool): Analyze handler functions.

    Returns:
        Dict[str, Any]: File path and summary of extracted events.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        file_based_element_cloner.extract_element_events_to_file(
            tab,
            selector=selector,
            include_inline=include_inline,
            include_listeners=include_listeners,
            include_framework=include_framework,
            analyze_handlers=analyze_handlers,
        ),
        instance_id=instance_id,
    )


@section_tool("file-extraction")
async def extract_element_animations_to_file(
    instance_id: str,
    selector: str,
    include_css_animations: bool = True,
    include_transitions: bool = True,
    include_transforms: bool = True,
    analyze_keyframes: bool = True,
) -> dict[str, Any]:
    """
    Extract element animations and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_css_animations (bool): Include CSS animations.
        include_transitions (bool): Include CSS transitions.
        include_transforms (bool): Include CSS transforms.
        analyze_keyframes (bool): Analyze keyframe rules.

    Returns:
        Dict[str, Any]: File path and summary of extracted animations.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        file_based_element_cloner.extract_element_animations_to_file(
            tab,
            selector=selector,
            include_css_animations=include_css_animations,
            include_transitions=include_transitions,
            include_transforms=include_transforms,
            analyze_keyframes=analyze_keyframes,
        ),
        instance_id=instance_id,
    )


@section_tool("file-extraction")
async def extract_element_assets_to_file(
    instance_id: str,
    selector: str,
    include_images: bool = True,
    include_backgrounds: bool = True,
    include_fonts: bool = True,
    fetch_external: bool = False,
) -> dict[str, Any]:
    """
    Extract element assets and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_images (bool): Include images.
        include_backgrounds (bool): Include background images.
        include_fonts (bool): Include font information.
        fetch_external (bool): Fetch external assets.

    Returns:
        Dict[str, Any]: File path and summary of extracted assets.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        raise Exception(f"Instance not found: {instance_id}")
    return await _with_cdp_timeout(
        file_based_element_cloner.extract_element_assets_to_file(
            tab,
            selector=selector,
            include_images=include_images,
            include_backgrounds=include_backgrounds,
            include_fonts=include_fonts,
            fetch_external=fetch_external,
        ),
        instance_id=instance_id,
    )


@section_tool("file-extraction")
async def list_clone_files() -> list[dict[str, Any]]:
    """
    List all element clone files saved to disk.

    Returns:
        List[Dict[str, Any]]: List of clone files with metadata and file information.
    """
    return file_based_element_cloner.list_clone_files()


@section_tool("file-extraction")
async def cleanup_clone_files(max_age_hours: int = 24) -> dict[str, int]:
    """
    Clean up old clone files to save disk space.

    Args:
        max_age_hours (int): Maximum age in hours for files to keep.

    Returns:
        Dict[str, int]: Number of files deleted.
    """
    deleted_count = file_based_element_cloner.cleanup_old_files(max_age_hours)
    return {"deleted_count": deleted_count}


@section_tool("cdp-functions")
async def list_cdp_commands() -> list[str]:
    """
    List all available CDP Runtime commands for function execution.

    Returns:
        List[str]: List of available CDP command names.
    """
    return await cdp_function_executor.list_cdp_commands()


@section_tool("cdp-functions")
async def execute_cdp_command(
    instance_id: str, command: str, params: dict[str, Any] = None
) -> dict[str, Any]:
    """
    Execute any CDP Runtime command with given parameters.

    Args:
        instance_id (str): Browser instance ID.
        command (str): CDP command name (e.g., 'evaluate', 'callFunctionOn').
        params (Dict[str, Any], optional): Command parameters as a dictionary.
                IMPORTANT: Use snake_case parameter names (e.g., 'return_by_value')
                NOT camelCase ('returnByValue'). The nodriver library expects
                Python-style parameter names.

    Returns:
        Dict[str, Any]: Command execution result.

    Example:
        # Correct - use snake_case
        params = {"expression": "document.title", "return_by_value": True}

        params = {"expression": "document.title", "returnByValue": True}
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await _with_cdp_timeout(
        cdp_function_executor.execute_cdp_command(tab, command, params or {}),
        instance_id=instance_id,
    )


@section_tool("cdp-functions")
async def get_execution_contexts(instance_id: str) -> list[dict[str, Any]]:
    """
    Get all available JavaScript execution contexts.

    Args:
        instance_id (str): Browser instance ID.

    Returns:
        List[Dict[str, Any]]: List of execution contexts with their details.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return []
    contexts = await _with_cdp_timeout(
        cdp_function_executor.get_execution_contexts(tab), instance_id=instance_id
    )
    return [
        {
            "id": ctx.id,
            "name": ctx.name,
            "origin": ctx.origin,
            "unique_id": ctx.unique_id,
            "aux_data": ctx.aux_data,
        }
        for ctx in contexts
    ]


@section_tool("cdp-functions")
async def discover_global_functions(
    instance_id: str, context_id: str = None
) -> list[dict[str, Any]]:
    """
    Discover all global JavaScript functions available in the page.

    Args:
        instance_id (str): Browser instance ID.
        context_id (str, optional): Optional execution context ID.

    Returns:
        List[Dict[str, Any]]: List of discovered functions with their details.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return []
    functions = await _with_cdp_timeout(
        cdp_function_executor.discover_global_functions(tab, context_id),
        instance_id=instance_id,
    )
    result = [
        {
            "name": func.name,
            "path": func.path,
            "signature": func.signature,
            "description": func.description,
        }
        for func in functions
    ]

    file_response = response_handler.handle_response(
        result,
        fallback_filename_prefix="global_functions",
        metadata={
            "context_id": context_id,
            "function_count": len(result),
            "url": getattr(tab, "url", "unknown"),
        },
    )

    if isinstance(file_response, dict) and "file_path" in file_response:
        return [
            {
                "name": "LARGE_RESPONSE_SAVED_TO_FILE",
                "path": "file_storage",
                "signature": "automatic_file_fallback",
                "description": f"Response too large ({file_response['estimated_tokens']} tokens), saved to: {file_response['filename']}",
            }
        ]

    return file_response


@section_tool("cdp-functions")
async def discover_object_methods(
    instance_id: str, object_path: str
) -> list[dict[str, Any]]:
    """
    Discover methods of a specific JavaScript object.

    Args:
        instance_id (str): Browser instance ID.
        object_path (str): Path to the object (e.g., 'document', 'window.localStorage').

    Returns:
        List[Dict[str, Any]]: List of discovered methods.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return []
    methods = await _with_cdp_timeout(
        cdp_function_executor.discover_object_methods(tab, object_path),
        instance_id=instance_id,
    )
    methods_data = [
        {
            "name": method.name,
            "path": method.path,
            "signature": method.signature,
            "description": method.description,
        }
        for method in methods
    ]

    # handle_response is synchronous — awaiting its dict return raises TypeError.
    return response_handler.handle_response(
        methods_data, f"object_methods_{object_path.replace('.', '_')}"
    )


@section_tool("cdp-functions")
async def call_javascript_function(
    instance_id: str, function_path: str, args: list[Any] = None
) -> dict[str, Any]:
    """
    Call a JavaScript function with arguments.

    Args:
        instance_id (str): Browser instance ID.
        function_path (str): Full path to the function (e.g., 'document.getElementById').
        args (List[Any], optional): List of arguments to pass to the function.

    Returns:
        Dict[str, Any]: Function call result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await _with_cdp_timeout(
        cdp_function_executor.call_discovered_function(tab, function_path, args or []),
        instance_id=instance_id,
    )


@section_tool("cdp-functions")
async def inspect_function_signature(
    instance_id: str, function_path: str
) -> dict[str, Any]:
    """
    Inspect a JavaScript function's signature and details.

    Args:
        instance_id (str): Browser instance ID.
        function_path (str): Full path to the function.

    Returns:
        Dict[str, Any]: Function signature and details.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await _with_cdp_timeout(
        cdp_function_executor.inspect_function_signature(tab, function_path),
        instance_id=instance_id,
    )


@section_tool("cdp-functions")
async def inject_and_execute_script(
    instance_id: str, script_code: str, context_id: str = None
) -> dict[str, Any]:
    """
    Inject and execute custom JavaScript code.

    Args:
        instance_id (str): Browser instance ID.
        script_code (str): JavaScript code to execute.
        context_id (str, optional): Optional execution context ID.

    Returns:
        Dict[str, Any]: Script execution result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await _with_cdp_timeout(
        cdp_function_executor.inject_and_execute_script(tab, script_code, context_id),
        instance_id=instance_id,
    )


@section_tool("cdp-functions")
async def create_persistent_function(
    instance_id: str, function_name: str, function_code: str
) -> dict[str, Any]:
    """
    Create a persistent JavaScript function that survives page reloads.

    Args:
        instance_id (str): Browser instance ID.
        function_name (str): Name for the function.
        function_code (str): JavaScript function code.

    Returns:
        Dict[str, Any]: Function creation result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await _with_cdp_timeout(
        cdp_function_executor.create_persistent_function(
            tab, function_name, function_code, instance_id
        ),
        instance_id=instance_id,
    )


@section_tool("cdp-functions")
async def execute_function_sequence(
    instance_id: str, function_calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Execute a sequence of JavaScript function calls.

    Args:
        instance_id (str): Browser instance ID.
        function_calls (List[Dict[str, Any]]): List of function calls, each with 'function_path', 'args', and optional 'context_id'.

    Returns:
        List[Dict[str, Any]]: List of function call results.
    """
    from cdp_function_executor import FunctionCall

    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return [{"success": False, "error": f"Instance not found: {instance_id}"}]
    calls = []
    for call_data in function_calls:
        calls.append(
            FunctionCall(
                function_path=call_data["function_path"],
                args=call_data.get("args", []),
                context_id=call_data.get("context_id"),
            )
        )
    return await _with_cdp_timeout(
        cdp_function_executor.execute_function_sequence(tab, calls),
        instance_id=instance_id,
    )


@section_tool("cdp-functions")
async def create_python_binding(
    instance_id: str, binding_name: str, python_code: str
) -> dict[str, Any]:
    """
    Create a binding that allows JavaScript to call Python functions.

    Args:
        instance_id (str): Browser instance ID.
        binding_name (str): Name for the binding.
        python_code (str): Python function code (as string).

    Returns:
        Dict[str, Any]: Binding creation result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    try:
        exec_globals = {}
        exec(python_code, exec_globals)
        python_function = None
        for name, obj in exec_globals.items():
            if callable(obj) and not name.startswith("_"):
                python_function = obj
                break
        if not python_function:
            return {"success": False, "error": "No function found in Python code"}
        return await _with_cdp_timeout(
            cdp_function_executor.create_python_binding(
                tab, binding_name, python_function
            ),
            instance_id=instance_id,
        )
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to create Python function: {e!s}",
        }


@section_tool("cdp-functions")
async def execute_python_in_browser(
    instance_id: str, python_code: str
) -> dict[str, Any]:
    """
    Execute Python code by translating it to JavaScript.

    Args:
        instance_id (str): Browser instance ID.
        python_code (str): Python code to translate and execute.

    Returns:
        Dict[str, Any]: Execution result.
    """
    tab = await browser_manager.get_tab(instance_id)
    if not tab:
        return {"success": False, "error": f"Instance not found: {instance_id}"}
    return await _with_cdp_timeout(
        cdp_function_executor.execute_python_in_browser(tab, python_code),
        instance_id=instance_id,
    )


@section_tool("cdp-functions")
async def get_function_executor_info(instance_id: str = None) -> dict[str, Any]:
    """
    Get information about the CDP function executor state.

    Args:
        instance_id (str, optional): Optional browser instance ID for specific info.

    Returns:
        Dict[str, Any]: Function executor state and capabilities.
    """
    return await _with_cdp_timeout(
        cdp_function_executor.get_function_executor_info(instance_id),
        instance_id=instance_id,
    )


@section_tool("dynamic-hooks")
async def create_dynamic_hook(
    name: str,
    requirements: dict[str, Any],
    function_code: str,
    instance_ids: list[str] | None = None,
    priority: int = 100,
) -> dict[str, Any]:
    """
    Create a new dynamic hook with AI-generated Python function.

    This is the new powerful hook system that allows AI to write custom Python functions
    that process network requests in real-time with no pending state.

    Args:
        name (str): Human-readable hook name
        requirements (Dict[str, Any]): Matching criteria (url_pattern, method, resource_type, custom_condition)
        function_code (str): Python function code that processes requests (must define process_request(request))
        instance_ids (Optional[List[str]]): Browser instances to apply hook to (all if None)
        priority (int): Hook priority (lower = higher priority)

    Returns:
        Dict[str, Any]: Hook creation result with hook_id

    Example function_code:
        ```python
        def process_request(request):
            if "example.com" in request["url"]:
                return HookAction(action="redirect", url="https://httpbin.org/get")
            return HookAction(action="continue")
        ```
    """
    return await dynamic_hook_ai.create_dynamic_hook(
        name=name,
        requirements=requirements,
        function_code=function_code,
        instance_ids=instance_ids,
        priority=priority,
    )


@section_tool("dynamic-hooks")
async def create_simple_dynamic_hook(
    name: str,
    url_pattern: str,
    action: str,
    target_url: str | None = None,
    custom_headers: dict[str, str] | None = None,
    instance_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create a simple dynamic hook using predefined templates (easier for AI).

    Args:
        name (str): Hook name
        url_pattern (str): URL pattern to match
        action (str): Action type - 'block', 'redirect', 'add_headers', or 'log'
        target_url (Optional[str]): Target URL for redirect action
        custom_headers (Optional[Dict[str, str]]): Headers to add for add_headers action
        instance_ids (Optional[List[str]]): Browser instances to apply hook to

    Returns:
        Dict[str, Any]: Hook creation result
    """
    return await dynamic_hook_ai.create_simple_hook(
        name=name,
        url_pattern=url_pattern,
        action=action,
        target_url=target_url,
        custom_headers=custom_headers,
        instance_ids=instance_ids,
    )


@section_tool("dynamic-hooks")
async def list_dynamic_hooks(instance_id: str | None = None) -> dict[str, Any]:
    """
    List all dynamic hooks.

    Args:
        instance_id (Optional[str]): Optional filter by browser instance

    Returns:
        Dict[str, Any]: List of hooks with details and statistics
    """
    return await dynamic_hook_ai.list_dynamic_hooks(instance_id=instance_id)


@section_tool("dynamic-hooks")
async def get_dynamic_hook_details(hook_id: str) -> dict[str, Any]:
    """
    Get detailed information about a specific dynamic hook.

    Args:
        hook_id (str): Hook identifier

    Returns:
        Dict[str, Any]: Detailed hook information including function code
    """
    return await dynamic_hook_ai.get_hook_details(hook_id=hook_id)


@section_tool("dynamic-hooks")
async def remove_dynamic_hook(hook_id: str) -> dict[str, Any]:
    """
    Remove a dynamic hook.

    Args:
        hook_id (str): Hook identifier to remove

    Returns:
        Dict[str, Any]: Removal status
    """
    return await dynamic_hook_ai.remove_dynamic_hook(hook_id=hook_id)


@section_tool("dynamic-hooks")
def get_hook_documentation() -> dict[str, Any]:
    """
    Get comprehensive documentation for creating hook functions (AI learning).

    Returns:
        Dict[str, Any]: Documentation of request object structure and HookAction types
    """
    return dynamic_hook_ai.get_request_documentation()


@section_tool("dynamic-hooks")
def get_hook_examples() -> dict[str, Any]:
    """
    Get example hook functions for AI learning.

    Returns:
        Dict[str, Any]: Collection of example hook functions with explanations
    """
    return dynamic_hook_ai.get_hook_examples()


@section_tool("dynamic-hooks")
def get_hook_requirements_documentation() -> dict[str, Any]:
    """
    Get documentation on hook requirements and matching criteria.

    Returns:
        Dict[str, Any]: Requirements documentation and best practices
    """
    return dynamic_hook_ai.get_requirements_documentation()


@section_tool("dynamic-hooks")
def get_hook_common_patterns() -> dict[str, Any]:
    """
    Get common hook patterns and use cases.

    Returns:
        Dict[str, Any]: Common patterns like ad blocking, API proxying, etc.
    """
    return dynamic_hook_ai.get_common_patterns()


@section_tool("dynamic-hooks")
def validate_hook_function(function_code: str) -> dict[str, Any]:
    """
    Validate hook function code for common issues before creating.

    Args:
        function_code (str): Python function code to validate

    Returns:
        Dict[str, Any]: Validation results with issues and warnings
    """
    return dynamic_hook_ai.validate_hook_function(function_code=function_code)


if get_settings().xpool_safe_mode:
    DISABLED_SECTIONS.add("cdp-functions")
    apply_disabled_sections()


def build_arg_parser():
    """Construct the CLI parser for the ``python -m ... server`` entrypoint.

    Extracted to module scope so argument defaults (notably the HTTP bind host)
    are unit-testable without executing the server.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Stealth Browser MCP Server with 90 tools"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=get_settings().port,
        help="Port for HTTP transport",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for HTTP transport. Defaults to loopback because "
        "the backend is unauthenticated and drives logged-in "
        "browser profiles; pass 0.0.0.0 only to deliberately "
        "expose it.",
    )

    parser.add_argument(
        "--disable-browser-management",
        action="store_true",
        help="Disable browser management tools (spawn, navigate, close, etc.)",
    )
    parser.add_argument(
        "--disable-element-interaction",
        action="store_true",
        help="Disable element interaction tools (click, type, scroll, etc.)",
    )
    parser.add_argument(
        "--disable-element-extraction",
        action="store_true",
        help="Disable element extraction tools (styles, structure, events, etc.)",
    )
    parser.add_argument(
        "--disable-file-extraction",
        action="store_true",
        help="Disable file-based extraction tools",
    )
    parser.add_argument(
        "--disable-network-debugging",
        action="store_true",
        help="Disable network debugging and interception tools",
    )
    parser.add_argument(
        "--disable-cdp-functions",
        action="store_true",
        help="Disable CDP function execution tools",
    )
    parser.add_argument(
        "--disable-progressive-cloning",
        action="store_true",
        help="Disable progressive element cloning tools",
    )
    parser.add_argument(
        "--disable-cookies-storage",
        action="store_true",
        help="Disable cookie and storage management tools",
    )
    parser.add_argument(
        "--disable-tabs", action="store_true", help="Disable tab management tools"
    )
    parser.add_argument(
        "--disable-debugging",
        action="store_true",
        help="Disable debug and system tools",
    )
    parser.add_argument(
        "--disable-dynamic-hooks",
        action="store_true",
        help="Disable dynamic network hook system",
    )

    parser.add_argument(
        "--minimal",
        action="store_true",
        help="Enable only core browser management and element interaction (disable everything else)",
    )
    parser.add_argument(
        "--list-sections",
        action="store_true",
        help="List all available tool sections and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=DEBUG_LOGGING_ENABLED,
        help="Enable debug logging to stderr",
    )
    parser.add_argument(
        "--xpool-safe",
        action="store_true",
        default=get_settings().xpool_safe_mode,
        help="Enable xpool-safe surface (disables cdp-functions tools that trigger Runtime.enable)",
    )

    return parser


if __name__ == "__main__":
    bootstrap_backend_process_logging()
    args = build_arg_parser().parse_args()

    if args.debug and not debug_logger._enabled:
        debug_logger.enable()

    if args.list_sections:
        print("Available tool sections:")
        print("  browser-management: Core browser operations (11 tools)")
        print(
            "  element-interaction: Page interaction and element manipulation (8 tools)"
        )
        print("  element-extraction: Element cloning and extraction (10 tools)")
        print("  file-extraction: File-based extraction tools (9 tools)")
        print("  network-debugging: Network monitoring and interception (10 tools)")
        print("  cdp-functions: Chrome DevTools Protocol function execution (15 tools)")
        print("  progressive-cloning: Advanced element cloning system (10 tools)")
        print("  cookies-storage: Cookie and storage management (3 tools)")
        print("  tabs: Tab management (5 tools)")
        print("  debugging: Debug and system tools (6 tools)")
        print("  dynamic-hooks: AI-powered network hook system (12 tools)")
        print("\nUse --disable-<section-name> to disable specific sections")
        print("Use --minimal to enable only core functionality")
        sys.exit(0)

    if args.minimal:
        DISABLED_SECTIONS.update(
            [
                "element-extraction",
                "file-extraction",
                "network-debugging",
                "cdp-functions",
                "progressive-cloning",
                "cookies-storage",
                "tabs",
                "debugging",
                "dynamic-hooks",
            ]
        )

    if args.disable_browser_management:
        DISABLED_SECTIONS.add("browser-management")
    if args.disable_element_interaction:
        DISABLED_SECTIONS.add("element-interaction")
    if args.disable_element_extraction:
        DISABLED_SECTIONS.add("element-extraction")
    if args.disable_file_extraction:
        DISABLED_SECTIONS.add("file-extraction")
    if args.disable_network_debugging:
        DISABLED_SECTIONS.add("network-debugging")
    if args.disable_cdp_functions:
        DISABLED_SECTIONS.add("cdp-functions")
    if args.disable_progressive_cloning:
        DISABLED_SECTIONS.add("progressive-cloning")
    if args.disable_cookies_storage:
        DISABLED_SECTIONS.add("cookies-storage")
    if args.disable_tabs:
        DISABLED_SECTIONS.add("tabs")
    if args.disable_debugging:
        DISABLED_SECTIONS.add("debugging")
    if args.disable_dynamic_hooks:
        DISABLED_SECTIONS.add("dynamic-hooks")

    if args.xpool_safe:
        DISABLED_SECTIONS.add("cdp-functions")

    apply_disabled_sections()

    if DISABLED_SECTIONS:
        debug_logger.log_info(
            "server",
            "startup",
            f"Disabled tool sections: {', '.join(sorted(DISABLED_SECTIONS))}",
        )

    # Ship errors to Sentry when SENTRY_DSN is set (no-op otherwise).
    sentry_init()

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
