"""Profile and clone-storage subsystem for the embedded browser backend.

Owns the disposable-session lifecycle extracted verbatim from ``server.py``
(F-201): where every session, clone and seed directory IS, refreshing the seed,
per-session profile copying, the storage-cap sweep (idle auto-clone eviction
plus named-profile regenerable trim), and profile-selection resolution.
Extracting it means a fault in storage GC can no longer disable the whole tool
surface. What a request MAY name and what the seed means is `profile_seed`'s;
WHICH SESSION a new one is copied from is `profile_source`'s; what happens when
the session a spawn ASKED FOR is already open is `profile_target`'s (F-914/
F-915); HOW the copy is made is `profile_copy`'s and what an EVICTION means is
`clone_trash`'s (F-897). This module is the only thing that knows where those
directories live, and hands them over as `profile_seed.Roots`; what stays here
is the POLICY around a copy: into which directory, refused when, reported how.

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
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stealth_chrome_devtools_mcp.embedded import (
    clone_trash,
    profile_copy,
    profile_lock,
    profile_seed,
    profile_source,
    profile_target,
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
        if entry.name == clone_trash.TRASH_DIRNAME:
            continue  # recoverable-eviction holding area — never a clone itself
        try:
            if not entry.is_dir() or not clone_is_auto(entry):
                continue
            size = profile_copy.dir_size_bytes(entry)
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

    Eviction is *recoverable*: victims are moved aside rather than deleted, and
    trash older than the retention window is purged first — so disk is reclaimed
    from expired trash before any live clone is touched. What "moved aside"
    means, and for how long it can be undone, is ``clone_trash``'s.
    """
    clone_trash.purge_expired(clone_root, clone_trash.retention_seconds())
    removed = 0
    for entry in _idle_autoclones_over_cap(clone_root, cap_bytes):
        if _clone_dir_is_protected(entry):
            # Protection acquired between selection and eviction (a spawn started
            # mid-sweep) — respect it rather than evict a now-in-flight clone.
            continue
        size = profile_copy.dir_size_bytes(entry)
        clone_trash.trash(entry, clone_root, _profile_has_running_browser)
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
        if entry.name == clone_trash.TRASH_DIRNAME:
            continue  # trashed clones are not named profiles and must not
            # inflate the session-cap total, or real profiles get over-trimmed
        try:
            if not entry.is_dir():
                continue
            size = profile_copy.dir_size_bytes(entry)
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
        total -= profile_copy.regenerable_size(
            entry
        )  # a trim frees ~the regenerable portion
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
        freed = profile_copy.trim_regenerable(entry)
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
        profile_copy.rmtree_robust(target)
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
    seed: profile_source.SeedSource, clone: Path, clone_root: Path
) -> dict[str, Any]:
    """The clone role's answer. It takes the whole ``SeedSource`` rather than a
    (path, kind) pair for ``resolve_profile_selection``'s ``override`` reason:
    the three facts are decided together, and splitting them lets a caller
    record a copy as having come from somewhere it did not."""
    selection: dict[str, Any] = {
        "user_data_dir": str(clone),
        "profile_role": "clone",
        "clone_source": seed.kind,
        "clone_source_path": str(seed.path),
        "seed_path": str(master_snapshot_dir()),
        **_live_seed_fields(seed),
    }
    _require_copied(_copy_profile_tree(seed.path, clone, clone_root, seed.kind), clone)
    return selection


def _roots() -> profile_seed.Roots:
    """The four directories a profile request is decided against. This module
    is the one thing that knows where they are, and ``profile_seed.Roots`` is
    how it hands them over (F-896); F-897 gave that hand-over a second caller,
    which is why it is a function rather than four arguments written twice."""
    return profile_seed.Roots(
        default_session_root(),
        clone_root_dir(),
        master_profile_dir(),
        master_snapshot_dir(),
    )


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
    return str(profile_seed.require_allowed(requested, _roots(), _is_relative_to))


def require_allowed_seed_from(
    seed_from: str | None,
    landed: str | None,
    *,
    check_source: bool = True,
    driven: Callable[[Path], bool] = profile_source.NOTHING_DRIVEN,
) -> str | None:
    """THE gate for a ``seed_from`` request (F-897): the name it may be, and
    that there is a NEW session for it to apply to. None when none was given.

    It takes *landed* — ``require_allowed_user_data_dir``'s answer, the
    DIRECTORY the caller's session request means — so "is this the shared
    session" and "does it already exist" are asked about the directory a
    request MEANS. Both are wrong for a relative spelling otherwise.

    Asked TWICE on ``require_allowed``'s precedent, and for a sharper reason:
    ``spawn_browser`` asks it in front of ``browser_reattach.adopt_held_profile``,
    because a session whose browser is still running is a session that EXISTS —
    so without it ``spawn --session work --from other`` would be silently
    ADOPTED onto the running ``work`` browser with nothing said about the flag.
    The resolver asks again because it is public and has its own callers.

    The SOURCE question is asked here too and its answer DISCARDED (review S1;
    finding §2.3): its three refusals are raised inside
    ``profile_source.seed_source``, which the resolver calls from INSIDE
    ``spawn_browser``'s ``try``, so they reached the caller re-labelled
    ``Failed to spawn browser: ...``, and an inner ``except ToolError: raise``
    does not fix that. Discarding is the point: "is this source open" is a fact
    with a LIFETIME, so the read that DECIDES stays the statement before the
    copy, with no ``await`` between.

    *check_source* is False for exactly one caller, the RESOLVER (memo review
    S): its ask already runs inside that ``try``, and ``_seed_source_for_copy``
    raises the same sentences one statement later with no ``await`` between, so
    a third walk of the process table decides nothing. A 1 s memo bought the
    same saving and is REPLACED by this flag — no process-global state, no
    clock, no reset hook, no answer that can go stale. What is left is one
    ``exists()`` and a psutil walk only where the answer is used.

    *driven* is F-898's witness — "does THIS backend hold a browser there" —
    passed to ``_seed_source``; its rule and its NO default are
    ``profile_source``'s."""
    requested = profile_source.seed_request(seed_from)
    if requested is None:
        return None
    target = None if landed is None else Path(landed)
    profile_source.require_new_session(
        requested,
        target,
        shared=target is not None
        and profile_seed.same_dir(target, master_profile_dir()),
        inside_root=target is not None and _is_relative_to(target, clone_root_dir()),
    )
    if check_source:
        _seed_source(requested, driven)
    return requested


def _seed_source(
    seed_from: str | None,
    driven: Callable[[Path], bool] = profile_source.NOTHING_DRIVEN,
) -> profile_source.SeedSource:
    """THE one binding of ``profile_source.seed_source`` to OUR four directories
    and OUR two witnesses. SIDE-EFFECT-FREE: it is asked TWICE (review S1) and
    only one ask is about to copy, so the freshen is ``_seed_source_for_copy``'s
    alone — taken twice it would copy a whole profile for a refused spawn.

    ``driven`` is F-898's second witness and this module's to BIND, not to
    answer: which browsers exist is ``browser_manager``'s, and importing it here
    would put the disk subsystem downstream of the browser one, so the snapshot
    arrives from ``spawn_browser`` as a plain predicate."""
    return profile_source.seed_source(
        seed_from,
        _roots(),
        _is_relative_to,
        held=_profile_has_running_browser,
        driven=driven,
    )


def _seed_source_for_copy(
    seed_from: str | None,
    driven: Callable[[Path], bool] = profile_source.NOTHING_DRIVEN,
) -> profile_source.SeedSource:
    """The AUTHORITATIVE read: the SAME question through the SAME binding
    (review N4 — one ``seed_source`` call site, not two that can drift), plus
    the freshen a copy from the SHARED session owes its seed first — the only
    source that HAS one. Authoritative by WHERE it sits: the statement before
    the copy, with no ``await`` between."""
    if seed_from is None or profile_seed.is_default_name(seed_from):
        _refresh_snapshot_if_stale()
    return _seed_source(seed_from, driven)


#: The selection key F-898's cookie hand-off is driven from, and the ONE key
#: :func:`_public_profile_selection` DROPS: it carries the live source's
#: directory, an instruction to this process rather than a field a client reads.
LIVE_SEED_KEY = "seed_live_source"

#: Its PUBLIC half (F-914/F-915): the NAME of the browser whose jar this
#: session was seeded from — a word the caller can pass back as ``session=``,
#: on ``seeded_from``'s precedent, where the path beside it is an instruction.
HANDED_OVER_KEY = "handed_over_from"


def _live_seed_fields(seed: profile_source.SeedSource) -> dict[str, Any]:
    """The two fields a seed with a LIVE source adds, or ``{}``. ONE home
    because THREE branches stamp them — F-897's ``seed_from`` and F-914/F-915's
    two held-target hand-overs — and a second spelling is how one of them would
    come to name a source another does not."""
    if seed.live is None:
        return {}
    return {
        LIVE_SEED_KEY: str(seed.live),
        HANDED_OVER_KEY: profile_seed.seed_name(
            seed.live, master_profile_dir(), master_snapshot_dir()
        ),
    }


def _public_profile_selection(profile_selection: dict[str, Any]) -> dict[str, Any]:
    """The selection as ``spawn_diagnostics.profile_selection`` reports it — the
    ONE site every role passes through, which is why F-895's seed provenance is
    stamped here. It is read from the marker on disk, so a directory that
    already existed reports the seed it was actually made from.

    It is also where an INTERNAL key stops being one (F-898): this function's
    whole job is the line between what the resolver decided and what a caller is
    told, so :data:`LIVE_SEED_KEY` is dropped HERE and nowhere else."""
    public = dict(profile_selection)
    public.pop(LIVE_SEED_KEY, None)
    selected = public.get("user_data_dir")
    if isinstance(selected, str) and selected:
        public.update(profile_seed.provenance(Path(selected)))
    return public


async def resolve_profile_selection(  # noqa: PLR0913  PERMANENT(one keyword per independent input to one selection)
    user_data_dir: str | None,
    *,
    seed_from: str | None = None,
    force_clone: bool = False,
    override: profile_source.SeedSource | None = None,
    clone_suffix: str | None = None,
    driven: Callable[[Path], bool] = profile_source.NOTHING_DRIVEN,
) -> dict[str, Any]:
    """Which directory this spawn drives, and how it got there.

    ``override`` is ``_fallback_profile_selection``'s — the (source, kind) pair
    a RETRY clones from. It was two parameters, ``source_override`` and
    ``source_kind``, and folding them into the ``SeedSource`` F-897 already
    needed is not tidying: a path and the word recorded for it are decided
    together, and two parameters let a caller record a copy as having come
    from somewhere it did not.

    ``driven`` is F-898's witness, passed through to ``_seed_source``. A seed
    with a LIVE source makes the answer carry :data:`LIVE_SEED_KEY` — set by the
    branch that actually COPIED, so a target that raced into existence between
    the pre-flight and here is never handed another session's jar.

    **Since F-914/F-915 it is also the witness for the TARGET.** Both held
    branches — a NAMED session and the shared one — ask
    ``profile_target.hand_over_or_refuse``, so there is one rule and two call
    sites rather than two rules. What differs is only which directory the copy
    is taken FROM: a named holder is its own source (no closed form of it
    exists), while the shared session keeps one — the seed — so its copy still
    comes from a directory nothing is writing to and only the jar comes live.
    """
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
    # F-897: refused BEFORE the walk, because `--from` is about the session the
    # caller NAMED. A target that is held is walked to `<name>-2`, which does
    # not exist — so asking afterwards would seed a substitute directory under
    # a flag the caller passed about theirs.
    # `driven` is INERT here — `check_source=False` gates its only reader — and
    # is passed for symmetry, so a future True cannot fail closed (review S5).
    seed_from = require_allowed_seed_from(
        seed_from, landed, check_source=False, driven=driven
    )
    explicit = (
        None
        if landed is None or profile_seed.same_dir(Path(landed), master)
        else Path(landed)
    )

    if explicit is not None:
        # The requested path (inside clone_root) is held by a running browser,
        # and what happens next is `profile_target`'s rule — the walk to
        # `<name>-N` is CONDITIONAL since F-915 and its module docstring is
        # where that is argued. What stays here is the walk's own REPORT
        # (F-871) and the holder as this copy's source.
        walk: dict[str, Any] = {}
        handed_over: profile_source.SeedSource | None = None
        if _is_relative_to(explicit, clone_root):
            hold = _profile_hold(explicit)
            if hold is not None:
                holder = profile_target.hand_over_or_refuse(
                    explicit, hold, _roots(), driven=driven
                )
                requested, explicit = explicit, _next_available_explicit_dir(explicit)
                walk = {
                    "requested_user_data_dir": str(requested),
                    "walked_to": str(explicit),
                    "walk_reason": hold.reason,
                }
                handed_over = profile_source.SeedSource(
                    holder, profile_source.LIVE_SESSION_KIND, holder
                )
        live_seed: dict[str, Any] = {}
        if not explicit.exists() and _is_relative_to(explicit, clone_root):
            # A hand-over REPLACES the seed for this copy and can never drop a
            # caller's `seed_from`: a held session is one that EXISTS, which is
            # `require_new_session`'s fourth refusal, raised before this line.
            # `_for_copy`, never `_seed_source`: that read owns the freshen and
            # is the AUTHORITATIVE hold check — the pre-flight skipped it (S) —
            # and a hand-over needs neither, copying from the holder instead.
            seed = handed_over or _seed_source_for_copy(seed_from, driven)
            _require_copied(
                _copy_profile_tree(seed.path, explicit, clone_root, seed.kind), explicit
            )
            # F-898: the copy could not reach a jar Chrome holds open.
            # `seed.live` and NOT `seed.kind` — for `default` the copy source
            # (the seed) and the live source (the shared profile) are different
            # directories, and reading the kind is how `--from default` got no
            # hand-off at all (review M1). Stamped inside the branch that
            # copied, the only place that knows a copy happened.
            live_seed = _live_seed_fields(seed)
        explicit.parent.mkdir(parents=True, exist_ok=True)
        return {
            "user_data_dir": str(explicit),
            "profile_role": "explicit",
            "clone_source": None,
            **walk,
            **live_seed,
        }

    master.parent.mkdir(parents=True, exist_ok=True)
    # ONE hold read where there used to be one liveness bool: F-914 needs the
    # REASON as well as the fact, and a second walk of the process table for it
    # would be a second answer to one question a line apart. The shared session
    # being OPEN is the same rule the named branch above asks, about a
    # different directory — `profile_target` argues it once for both.
    shared_hold = _profile_hold(master)
    if shared_hold is None:
        if not force_clone:
            snapshot_result = _refresh_master_snapshot_if_safe("before-default-open")
            return {
                "user_data_dir": str(master),
                "profile_role": profile_seed.DEFAULT_SESSION,
                "clone_source": None,
                **snapshot_result,
            }
        shared_live = None
    else:
        shared_live = profile_target.hand_over_or_refuse(
            master, shared_hold, _roots(), driven=driven
        )

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

    if override is not None:
        seed = override
    elif snapshot.exists():
        seed = profile_source.SeedSource(snapshot, "default-seed")
    elif master.exists():
        # No seed yet (first run, seed deleted, or the seed copy failed), so the
        # only copy available is of the shared profile itself. F-920: this
        # comment used to claim cookies "transfer successfully even while Chrome
        # has it open", which `profile_copy.copy_file`'s own docstring
        # contradicts — a held file is SKIPPED, twice for the double pass, and
        # the gap cannot be enumerated. What makes the branch honest is the
        # hold read above: OPEN here means `profile_target` allowed it and the
        # jar is about to arrive over CDP; CLOSED here is a copy of a directory
        # at rest, which is what 2.1.11's first run always was.
        seed = profile_source.SeedSource(master, "live-default-fallback")
    else:
        raise RuntimeError(
            "No shared profile directory found — nothing to copy from. Spawn a "
            "browser with no session first to create and populate the "
            f"{profile_seed.DEFAULT_SESSION!r} session."
        )
    if shared_live is not None:
        # The hand-off's source is the SHARED profile, the copy's is whichever
        # of the three above answered — different directories, which is why
        # `SeedSource` carries `live` as a third fact and not a flag on `kind`
        # (F-898 review M1). A live shared session decided HERE outranks the one
        # `override` may have carried forward from a previous attempt.
        seed = seed._replace(live=shared_live)

    # Shield this clone from the storage-cap sweep BEFORE its marker is written.
    # The marker (written inside the copy below) makes the clone a reclaim target,
    # yet its browser has not launched/attached yet — so without this the sweep
    # could delete it out from under the spawning browser. Released when the
    # instance closes (or when this spawn attempt fails).
    _protect_clone_dir(clone)
    return _copy_clone_from_source(seed, clone, clone_root)


async def _fallback_profile_selection(
    previous_selection: dict[str, Any],
    attempt: int,
    *,
    driven: Callable[[Path], bool] = profile_source.NOTHING_DRIVEN,
) -> dict[str, Any] | None:
    # What the NEXT attempt drives (F-834 stage 1). A ``clone`` re-clones below;
    # the two non-clone roles retry the SAME directory, which this attempt's
    # F-860 reap has just freed — a NAMED profile is the identity the caller
    # asked for and is never walked or swapped, and a shared profile no sibling
    # took is still the best profile here, while one a sibling DID take falls
    # through. The hold is asked about the directory this attempt DROVE, off the
    # selection, never config. No wait, no reservation: CLAUDE.md's row.
    #
    # `driven` is F-914/F-915's witness, threaded for TWO reasons. This is the
    # SECOND DOOR onto the held-shared-session rule — F-834 widened this
    # function to all three roles, so without it a spawn that failed once
    # answers a held shared session with exactly the snapshot clone the
    # resolver refuses one call earlier — and it is what decides whether a
    # hand-off the previous attempt was making survives this one.
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
        override=profile_source.SeedSource(
            snapshot,
            "default-seed-final" if final else "default-seed-retry",
            profile_target.still_driven_source(
                previous_selection.get(LIVE_SEED_KEY), driven
            ),
        ),
        clone_suffix="seed" if final else "retry",
        driven=driven,
    )
