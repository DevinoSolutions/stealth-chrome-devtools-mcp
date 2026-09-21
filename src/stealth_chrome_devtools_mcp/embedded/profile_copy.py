"""THE one home for copying a Chrome profile directory: what such a copy must
NOT carry, and what it does about a file Chrome is holding open (F-897).

One subject, two halves that only mean anything together.

**What a copy leaves behind** — ``REGENERABLE_NAMES``, ``ignore_names``, and
since F-898 the DELETING half as well (``regenerable_dirs``,
``regenerable_size``, ``trim_regenerable``, ``dir_size_bytes``). Caches and
on-device model stores are typically ~98 % of a Chrome profile by size (the
on-device model alone can be ~4 GB) and Chrome rebuilds every one of them on
the next launch, so a copy that carried them would be slower, larger and no
more useful. Two paths read that list in OPPOSITE directions — the copy below
excludes these names, a trim under storage pressure deletes them — and they
lived in two modules, which is one import apart from drifting. They are one
module now: what a copy may leave behind and what a trim may take away are the
same sentence read twice, and the finding that moved them is the one that
needed ``clone_storage``'s last line of budget (that file stood at exactly
1000/1000, a cap which ratchets DOWN only, so the answer is an extraction and
never a raise). The POLICY stays where it was — WHEN a profile is trimmed, and
which profiles are idle enough to trim, are ``clone_storage``'s storage-cap
question and are not about bytes in a directory. ``profile_lock`` names three
of these entries too, for the DIFFERENT question of what Chrome's process
singleton MEANS; that module's docstring says so, and this list stays about
bytes on disk.

**What a copy does about a locked file** — ``copy_file``'s three attempts and
``copy_delta``'s two ``except`` clauses. A profile being copied may be a
profile Chrome has open: on Windows an open file is refused outright, and
everywhere a WAL-mode SQLite cookie jar may be mid-transaction. The answer here
is to RETRY briefly and then SKIP with a warning, never to fail the copy — and
the cost of that answer is exactly why the SOURCE has to be chosen carefully
one layer up. A skipped file is a login silently missing from the copy, and
this module cannot say which file mattered. So ``profile_source.seed_source``
refuses to seed a new session from a source a live browser holds, by name,
rather than handing the caller a copy whose gaps nobody can enumerate; the one
source that stays copyable while its own browser runs is the shared session,
and only because the product keeps a separate, closed, copyable form of it.

Extracted from ``clone_storage`` by F-897, which is the finding that made the
SOURCE of a copy a caller's choice rather than always the one seed. Putting the
tolerance and the exclusion list in a home of their own is what lets that
module state the policy — which source, and when it is refused — without the
mechanics sitting in the middle of it. The same shape ``page_storage`` made
when it left ``browser_manager``.

A leaf: stdlib plus ``debug_logger``. It knows nothing about session roots,
seeds or roles — every path arrives as an argument.
"""

import os
import shutil
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

# Regenerable Chrome profile subdirectories — caches and on-device model stores
# that Chrome rebuilds on next launch. Single source of truth: these are both
# excluded when copying a profile (``ignore_names``) and deleted from an idle
# profile under storage pressure (``trim_regenerable``), so the copy path and
# the trim path can never drift apart.
REGENERABLE_NAMES = frozenset(
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


#: Attempts ``copy_file`` gives one file before it gives up and skips it. Named
#: rather than inline because the LAST attempt is the one that decides a file is
#: lost, and the retry exists for a lock Chrome holds for milliseconds.
COPY_ATTEMPTS = 3


def ignore_names(names: Sequence[str]) -> set[str]:
    """Which of *names* a profile copy skips, by the list above plus the lock
    and scratch shapes Chrome writes under names it invents.

    It takes the names alone. It used to take the containing directory too, an
    unused vestige of ``shutil.copytree(ignore=…)``'s signature — and a
    parameter nothing reads is a claim that the answer depends on where you
    ask, which it does not.
    """
    ignored = set()
    for name in names:
        lower = name.lower()
        if (
            name in REGENERABLE_NAMES
            or name.startswith("Singleton")
            or lower.endswith((".tmp", ".lock"))
            or lower in {"lock", "lockfile"}
        ):
            ignored.add(name)
    return ignored


def copy_file(source: str, target: str) -> str:
    """Copy one profile file, tolerating a brief lock: ``COPY_ATTEMPTS`` tries,
    then a WARNING and a SKIP. It never raises, because one unreadable cache
    entry must not fail a whole profile copy — and the price of that is that a
    copy cannot enumerate what it lost, which is the argument in this module's
    docstring for refusing a live SOURCE one layer up."""
    last_error = None
    for attempt in range(COPY_ATTEMPTS):
        try:
            shutil.copy2(source, target)
        except (PermissionError, OSError) as exc:
            last_error = exc
            if attempt < COPY_ATTEMPTS - 1:
                time.sleep(0.05 * (attempt + 1))
        else:
            return target
    if last_error is not None:
        log_warning = getattr(debug_logger, "log_warning", None)
        if callable(log_warning):
            log_warning(
                "profile",
                "copy_skip",
                f"Skipping locked profile file {source}: {last_error}",
            )
    return target


def rmtree_robust(path: Path, retries: int = 3) -> None:
    """Remove a directory tree, handling Windows file-lock race conditions.

    Chrome profile dirs may have cache files vanishing mid-traversal
    (Chrome cleanup) or directories still locked by background processes.
    Retries with backoff and falls back to best-effort removal so the
    subsequent profile copy can proceed via overwrite.

    The same tolerance :func:`copy_file` has, in the opposite direction, which
    is why it lives here: both answer "a file Chrome is holding" by making
    progress anyway, and both therefore cannot say afterwards what they could
    not touch. Its callers are ``clone_storage``'s — the copy that overwrites
    a stale seed, the trash purge and the regenerable trim.
    """

    def _on_rm_error(
        func: Callable[[str], object], fpath: str, exc_info: tuple
    ) -> None:
        exc = exc_info[1]
        if isinstance(exc, FileNotFoundError):
            return  # file already gone — Chrome or OS cleaned it
        if isinstance(exc, PermissionError):
            with suppress(OSError, FileNotFoundError):
                Path(fpath).chmod(0o700)
                func(fpath)
            return
        # OSError (e.g. directory not empty) — let rmtree continue
        if isinstance(exc, OSError):
            return

    for attempt in range(retries):
        try:
            if not path.exists():
                return
            shutil.rmtree(path, onerror=_on_rm_error)
        # The final attempt falls back to ignore_errors and LOGS what it could
        # not remove, so nothing is swallowed silently; a narrower except would
        # let one exotic error fail a whole profile copy, which is the thing
        # this function exists to prevent.
        except Exception:  # noqa: BLE001 — PERMANENT(best-effort, logged below)
            if attempt < retries - 1:
                time.sleep(0.5)
                continue
            # Final attempt: remove whatever is possible
            shutil.rmtree(path, ignore_errors=True)
            if path.exists():
                debug_logger.log_warning(
                    "server",
                    "rmtree_robust",
                    f"Could not fully remove {path} after {retries} retries, "
                    f"proceeding with overwrite",
                )
        else:
            return


def copy_delta(source: Path, target: Path) -> None:
    """Copy every non-regenerable file under *source* into *target* that is not
    already there at the same size and mtime. A delta rather than a fresh tree
    because the second of ``clone_storage._copy_profile_tree``'s two passes
    exists to pick up what the first found locked."""
    for directory, dirnames, filenames in os.walk(source, onerror=lambda _exc: None):
        ignored_dirs = ignore_names(dirnames)
        dirnames[:] = [name for name in dirnames if name not in ignored_dirs]

        source_dir = Path(directory)
        target_dir = target / source_dir.relative_to(source)
        target_dir.mkdir(parents=True, exist_ok=True)

        ignored_files = ignore_names(filenames)
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
                    copy_file(str(source_file), str(target_file))
            except (PermissionError, OSError):
                continue


def dir_size_bytes(path: Path) -> int:
    """Bytes under *path*, an entry that cannot be stat'ed counted as nothing.

    Moved here from ``clone_storage`` with the three functions above it (F-898),
    and the ``try``/``except``/``pass`` it arrived with is spelled as a
    ``suppress`` rather than carried over with that file's blanket ``SIM105``
    exemption: a suppression is a permission, and one does not travel with the
    code it was written around.
    """
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with suppress(OSError):
                total += (Path(root) / name).stat().st_size
    return total


def regenerable_dirs(profile_dir: Path) -> list[Path]:
    """Regenerable cache/model directories in a profile — those named in
    :data:`REGENERABLE_NAMES`, at the profile root and one level down
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
                if child.is_dir() and child.name in REGENERABLE_NAMES:
                    found.append(child)
            except OSError:
                continue

    _scan(profile_dir)
    try:
        subdirs = [
            c
            for c in profile_dir.iterdir()
            if c.is_dir() and c.name not in REGENERABLE_NAMES
        ]
    except OSError:
        subdirs = []
    for sub in subdirs:
        _scan(sub)
    return found


def regenerable_size(profile_dir: Path) -> int:
    """Bytes a trim of *profile_dir* would reclaim (read-only)."""
    return sum(dir_size_bytes(d) for d in regenerable_dirs(profile_dir))


def trim_regenerable(profile_dir: Path) -> int:
    """Delete the regenerable cache/model dirs from a profile (see
    :func:`regenerable_dirs`) while preserving every session-state file
    (cookies, logins, Web Data, Local Storage, Preferences). Returns bytes freed.
    """
    freed = 0
    for directory in regenerable_dirs(profile_dir):
        size = dir_size_bytes(directory)
        rmtree_robust(directory)
        if not directory.exists():
            freed += size
    return freed
