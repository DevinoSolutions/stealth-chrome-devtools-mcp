"""Robust process and temp-profile cleanup for browser instances."""

import atexit
import contextlib
import os
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

import psutil

from stealth_chrome_devtools_mcp.embedded import browser_pid_registry, singleton
from stealth_chrome_devtools_mcp.embedded.browser_pid_registry import Entries
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.singleton import STATE_DIR
from stealth_chrome_devtools_mcp.settings import get_settings

# What ``signal.signal`` returns: the disposition it displaced (a handler, or
# one of the SIG_DFL / SIG_IGN ints, or None for one it cannot describe).
SignalDisposition = Callable[[int, FrameType | None], object] | int | None


def _owner_identity() -> tuple[int, float | None]:
    """This process's (pid, create_time) — the owner stamped on the entries it
    records (see :mod:`browser_pid_registry` for what the stamp is for).

    A module function, not instance state: the answer cannot change within a
    process, and several tests build ProcessCleanup through ``__new__``.
    """
    pid = os.getpid()
    create_time = None
    with contextlib.suppress(psutil.Error, OSError):
        create_time = psutil.Process(pid).create_time()
    return pid, create_time


class ProcessCleanup:
    """Manage tracked browser process cleanup and orphan profile recovery."""

    PROFILE_SWEEP_PREFIX = "uc_"
    _MAX_CLEANUP_RETRIES = 5

    def __init__(self):
        """Initialize process cleanup state (side-effect-free)."""
        self.pid_file = STATE_DIR / "browser_pids.json"
        self.tracked_pids: set[int] = set()
        self.browser_processes: dict[str, dict[str, Any]] = {}
        self.orphan_profile_max_age_seconds = (
            get_settings().browser_orphan_profile_max_age
        )
        self._init_time = time.time()
        self._previous_signal_handlers: dict[int, SignalDisposition] = {}
        self._shutdown_in_progress = False

    def activate(self) -> None:
        """Install cleanup handlers and run orphan recovery once at serve startup."""
        if get_settings().no_auto_recovery:
            return
        self._setup_cleanup_handlers()
        self._recover_orphaned_processes()

    def recover_orphans(self, force: bool = False) -> None:
        """Public seam for CLI kill-orphans.

        ``force`` reaps every recorded browser whoever owns it — the operator
        override behind ``kill-orphans --force``, which has always meant "yes,
        including a live backend's". Startup recovery never passes it; sparing
        live owners there is the F-808 fix.
        """
        self._recover_orphaned_processes(force=force)

    @staticmethod
    def _normalize_path(path: str | None) -> str | None:
        """Normalize a filesystem path for safe comparison (registry leaf)."""
        return browser_pid_registry.normalize_path(path)

    @staticmethod
    def _is_browser_process_name(process_name: str) -> bool:
        """Whether a process name belongs to a Chromium-family browser."""
        normalized_name = (process_name or "").lower()
        return any(
            marker in normalized_name
            for marker in ("chrome", "chromium", "msedge", "edge", "brave")
        )

    @classmethod
    def _extract_profile_dir_from_cmdline(
        cls,
        cmdline: list[str],
    ) -> str | None:
        """
        Extract the user-data-dir argument from a browser command line.

        Args:
            cmdline (list[str]): Process command line.

        Returns:
            Optional[str]: Normalized user-data-dir path if present.
        """
        for index, arg in enumerate(cmdline):
            if arg.startswith("--user-data-dir="):
                return cls._normalize_path(arg.split("=", 1)[1])
            if arg == "--user-data-dir" and index + 1 < len(cmdline):
                return cls._normalize_path(cmdline[index + 1])
        return None

    def _setup_cleanup_handlers(self):
        """Register cleanup hooks for interpreter exit and termination signals.

        ``signal.signal`` returns the disposition it displaces; recording it is
        what lets ``_signal_handler`` hand the signal back (F-809) — under HTTP,
        uvicorn's ``handle_exit``, installed first and so REPLACED by ours. A
        re-install is skipped (runpy double-loads the module): it would record
        OUR handler as ``previous``, one delegating to itself. SIGBREAK
        (Windows-only, hence ``getattr``) is IGNORED, never handled (F-839): no
        product path sends it — stop/restart/evict all use TerminateProcess,
        which runs no handler — so obeying one serves only a console killer.
        """
        atexit.register(self._cleanup_all_tracked)

        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is None or signum in self._previous_signal_handlers:
                continue
            handler = signal.SIG_IGN if name == "SIGBREAK" else self._signal_handler
            self._previous_signal_handlers[signum] = signal.signal(signum, handler)

    def _signal_handler(self, signum, frame):
        """Clean up tracked browsers, then hand the signal back (F-809)."""
        debug_logger.log_info(
            "process_cleanup",
            "signal_handler",
            f"Received signal {signum}, initiating cleanup...",
        )
        previous = self._previous_signal_handlers.get(signum)
        # SIG_DFL / SIG_IGN are ints and nothing recorded is None — neither is a
        # loop to hand back to. ``default_int_handler`` is excluded deliberately:
        # under standalone stdio it IS SIGINT's prior disposition, and delegating
        # would swap today's clean exit 0 for a KeyboardInterrupt unwind.
        default_int = signal.default_int_handler
        if previous is None or isinstance(previous, int) or previous is default_int:
            self._run_shutdown_cleanup()  # verbatim 1.x standalone behaviour
            sys.exit(0)
        # Delegate FIRST (a cheap flag set — a slow or failing cleanup must not
        # strand the server), then RETURN into the interrupted frame so the
        # server's own graceful path unwinds the loop, instead of ``sys.exit``
        # tearing it down from inside ``select()``.
        previous(signum, frame)
        self._run_shutdown_cleanup()

    def _run_shutdown_cleanup(self) -> None:
        """Run the tracked-browser cleanup at most once per shutdown (a second
        SIGTERM must not re-enter it mid-``rmtree``)."""
        if self._shutdown_in_progress:
            return
        self._shutdown_in_progress = True
        try:
            self._cleanup_all_tracked()
        except Exception as error:
            debug_logger.log_error("process_cleanup", "shutdown_cleanup", error)

    def _load_tracked_pids(self) -> dict[str, dict[str, Any]]:
        """Every browser recorded in the shared PID file, ours and other
        backends' alike, keyed by instance id."""
        return browser_pid_registry.read_entries(self.pid_file)

    def _save_tracked_pids(self):
        """Persist this process's tracked browsers, merging into whatever the
        other backends on this machine have recorded.

        Only the entries being written are replaced; entries this process has
        stopped tracking are dropped by name in :meth:`untrack_browser_process`,
        never by omission here. Each one is stamped with this process's identity
        on the way out — at write time, so the owner has one source and cannot
        drift from the process actually holding the browser.
        """
        owner_pid, owner_create_time = _owner_identity()
        mine = {
            instance_id: browser_pid_registry.with_owner(
                metadata, owner_pid, owner_create_time
            )
            for instance_id, metadata in self.browser_processes.items()
        }
        self._rewrite_record("save_pids", lambda recorded: {**recorded, **mine})

    def _drop_recorded(self, instance_ids: set[str]) -> None:
        """Remove the named entries from the shared PID file, keeping the rest.

        The rest is the other backends' tracking, which is why this takes ids
        rather than deleting the file: an unlink here reads as "nothing is
        running anywhere" to the next backend that starts.
        """
        if not instance_ids:
            return
        self._rewrite_record(
            "drop_pids",
            lambda recorded: {
                instance_id: metadata
                for instance_id, metadata in recorded.items()
                if instance_id not in instance_ids
            },
        )

    def _rewrite_record(
        self, action: str, mutate: Callable[[Entries], Entries]
    ) -> None:
        """Apply *mutate* to the shared PID file, degrading to a logged skip.

        The one place a record write is attempted, so the two callers cannot
        drift on how a failure is handled. Skipping is safe: the next write
        repairs the entry, and the leaf leaves the record untouched on failure.
        """
        try:
            browser_pid_registry.update_entries(self.pid_file, mutate)
        except Exception as error:
            debug_logger.log_warning(
                "process_cleanup",
                action,
                f"Failed to update PID file: {error}",
            )

    def _get_active_browser_profile_dirs(self) -> set[str]:
        """
        Collect browser profile directories used by currently running browser processes.

        Returns:
            Set[str]: Normalized active browser profile directories.
        """
        active_profile_dirs: set[str] = set()
        # Enumerate only the cheap `name` field for every process; reading
        # `cmdline` for the whole process table is the dominant cost on Windows
        # (one PEB read per process). Pull cmdline lazily for the handful of
        # browser-named processes only.
        for process in psutil.process_iter(["name"]):
            try:
                process_name = process.info.get("name") or ""
                if not self._is_browser_process_name(process_name):
                    continue
                cmdline = process.cmdline() or []
                profile_dir = self._extract_profile_dir_from_cmdline(cmdline)
                if profile_dir:
                    active_profile_dirs.add(profile_dir)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "active_profiles",
                    f"Failed inspecting process "
                    f"{getattr(process, 'pid', 'unknown')}: {error}",
                )
        return active_profile_dirs

    def _get_browser_pids_for_profile(self, user_data_dir: str | None) -> set[int]:
        """
        Collect all live browser PIDs currently using a specific profile directory.

        Args:
            user_data_dir (Optional[str]): Browser profile directory to match.

        Returns:
            Set[int]: Matching browser process ids.
        """
        normalized_profile_dir = self._normalize_path(user_data_dir)
        if normalized_profile_dir is None:
            return set()

        matching_pids: set[int] = set()
        # See _get_active_browser_profile_dirs: enumerate cheap `name`/`pid`
        # only and read the expensive `cmdline` lazily for browser processes.
        for process in psutil.process_iter(["pid", "name"]):
            try:
                process_name = process.info.get("name") or ""
                if not self._is_browser_process_name(process_name):
                    continue
                cmdline = process.cmdline() or []
                profile_dir = self._extract_profile_dir_from_cmdline(cmdline)
                if profile_dir == normalized_profile_dir:
                    matching_pids.add(process.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "profile_pids",
                    f"Failed inspecting process "
                    f"{getattr(process, 'pid', 'unknown')}: {error}",
                )

        return matching_pids

    @staticmethod
    def _fallback_pid_identity_ok(
        fallback_pid: int, stored_create_time: float | None
    ) -> bool:
        """Check whether *fallback_pid* still belongs to the process we recorded.

        Returns True when ``stored_create_time`` is None (best-effort parity) or
        the live process's create_time matches the stored value within 1 second.
        Returns False when the process is gone, inaccessible, or the create_time
        diverges (recycled PID).
        """
        try:
            actual = psutil.Process(fallback_pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False
        return stored_create_time is None or abs(actual - stored_create_time) < 1.0

    def _owner_backend_alive(
        self, owner_pid: int, owner_create_time: float | None
    ) -> bool:
        """True when *owner_pid* is a live backend of ours AND is still the same
        process that recorded the entry. Passed to the registry leaf, which owns
        the record's schema and imports neither psutil nor ``singleton``.

        Composed from the two predicates that already exist rather than a third:
        ``_is_our_backend`` is what eviction trusts to never touch the wrong
        process (it excludes the stdio proxy), and
        :meth:`_fallback_pid_identity_ok` is this module's create_time tolerance
        for a recycled pid — a second tolerance constant here would be two
        answers to one question. Neither half suffices alone: identity would
        spare a pid recycled onto an unrelated backend, the tolerance one
        recycled onto any process at all.
        """
        return singleton._is_our_backend(owner_pid) and self._fallback_pid_identity_ok(
            owner_pid, owner_create_time
        )

    def _kill_processes_for_metadata(  # noqa: C901,PLR0912  plan_M11a
        self,
        instance_id: str,
        metadata: dict[str, Any],
        recovery: bool = False,
    ) -> bool:
        """
        Kill all browser processes associated with tracked metadata.

        Args:
            instance_id (str): Browser instance id.
            metadata (Dict[str, Any]): Tracked process metadata.
            recovery (bool): When True (startup orphan recovery), only kill processes
                that pre-date this server session.  Processes created after
                ``self._init_time`` belong to the current run and are never killed.

        Returns:
            bool: True if all associated browser processes were killed or
                already absent.
        """
        pids_to_kill = self._get_browser_pids_for_profile(metadata.get("user_data_dir"))
        fallback_pid = metadata.get("pid")
        stored_create_time = metadata.get("create_time")

        if recovery:
            # Safety net: never kill processes that started after this server
            # session began — they belong to the current run, not a previous one.
            safe_pids: set[int] = set()
            for pid in pids_to_kill:
                try:
                    pid_create_time = psutil.Process(pid).create_time()
                    if pid_create_time < self._init_time:
                        safe_pids.add(pid)
                    else:
                        debug_logger.log_info(
                            "process_cleanup",
                            "recovery",
                            f"Skipping PID {pid} for {instance_id}: "
                            f"started after server init",
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass  # gone or inaccessible — skip conservatively
            pids_to_kill = safe_pids

            if not pids_to_kill and isinstance(fallback_pid, int):
                # Use the stored PID only if it predates this session and
                # the identity check passes (shared predicate).
                if self._fallback_pid_identity_ok(fallback_pid, stored_create_time):
                    try:
                        if psutil.Process(fallback_pid).create_time() < self._init_time:
                            pids_to_kill = {fallback_pid}
                        else:
                            debug_logger.log_info(
                                "process_cleanup",
                                "recovery",
                                f"Skipping fallback PID {fallback_pid} for "
                                f"{instance_id}: started after server init",
                            )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                else:
                    debug_logger.log_info(
                        "process_cleanup",
                        "recovery",
                        f"Skipping fallback PID {fallback_pid} for {instance_id}: "
                        "create_time mismatch (recycled PID)",
                    )
        elif not pids_to_kill and isinstance(fallback_pid, int):
            if self._fallback_pid_identity_ok(fallback_pid, stored_create_time):
                pids_to_kill = {fallback_pid}
            else:
                debug_logger.log_info(
                    "process_cleanup",
                    "kill_browser_process",
                    f"Skipping fallback PID {fallback_pid} for {instance_id}: "
                    "create_time mismatch (recycled PID)",
                )

        if not pids_to_kill:
            return True

        success = True
        for pid in sorted(pids_to_kill):
            if not self._kill_process_by_pid(pid, instance_id):
                success = False
        return success

    def _profile_claimed_by_live_instance(self, normalized: str, skip_id: str) -> bool:
        """True when a tracked instance OTHER than *skip_id* holds *normalized*
        with a live pid.

        A deferred delete is decided at defer time and fired much later; in
        between, a concurrent spawn can become the live owner of that very
        directory (F-834 — concurrent spawns used to share one retry clone dir,
        and the loser's cleanup deleted the winner's live profile seconds after
        the tool reported it ready). Asking again HERE, at fire time, is what
        makes the answer current.
        """
        return any(
            other_id != skip_id
            and self._normalize_path(other.get("user_data_dir")) == normalized
            and isinstance(other.get("pid"), int)
            and psutil.pid_exists(other["pid"])
            for other_id, other in list(self.browser_processes.items())
        )

    def _cleanup_profile_dir(  # noqa: PLR0911  plan_M11a
        self,
        profile_dir: str,
        instance_id: str,
        active_profile_dirs: set[str] | None = None,
    ) -> bool:
        """Remove a browser temp profile directory when it is safe to do so.

        Returns True if the directory was removed or was already absent.
        ``active_profile_dirs`` is an optional pre-computed live-profile set;
        pass it only when it was measured just now (see the F-834 note on
        ``cleanup_deferred_profiles``).
        """
        normalized_profile_dir = self._normalize_path(profile_dir)
        if normalized_profile_dir is None:
            return False

        path = Path(profile_dir)
        if not path.exists():
            return True

        if self._profile_claimed_by_live_instance(normalized_profile_dir, instance_id):
            debug_logger.log_info(
                "process_cleanup",
                "cleanup_profile",
                f"Skipping profile a live instance owns ({instance_id}): {profile_dir}",
            )
            return False

        last = self._MAX_CLEANUP_RETRIES - 1
        for attempt in range(self._MAX_CLEANUP_RETRIES):
            current_active_profiles = (
                active_profile_dirs
                if active_profile_dirs is not None and attempt == 0
                else self._get_active_browser_profile_dirs()
            )
            if normalized_profile_dir in current_active_profiles:
                if attempt == last:
                    debug_logger.log_info(
                        "process_cleanup",
                        "cleanup_profile",
                        f"Skipping active profile directory for "
                        f"{instance_id}: {profile_dir}",
                    )
                    return False
                time.sleep(0.15)
                continue
            try:
                shutil.rmtree(path, ignore_errors=False)
                debug_logger.log_info(
                    "process_cleanup",
                    "cleanup_profile",
                    f"Removed temp profile for {instance_id}: {profile_dir}",
                )
                return True
            except FileNotFoundError:
                return True
            except (PermissionError, OSError) as error:
                if attempt == last:
                    debug_logger.log_warning(
                        "process_cleanup",
                        "cleanup_profile",
                        f"Failed to remove temp profile for {instance_id}: {error}",
                    )
                    return False
                time.sleep(0.15)

        return False

    def _cleanup_profile_for_metadata(
        self,
        instance_id: str,
        metadata: dict[str, Any],
        active_profile_dirs: set[str] | None = None,
    ) -> bool:
        """Remove the auto-generated profile directory *metadata* describes.

        Returns True if cleanup succeeded or nothing needed to be removed.
        """
        if metadata.get("uses_custom_data_dir") is True and not metadata.get(
            "auto_clone"
        ):
            return False

        profile_dir = metadata.get("user_data_dir")
        if not profile_dir:
            return False

        return self._cleanup_profile_dir(profile_dir, instance_id, active_profile_dirs)

    @staticmethod
    def _should_untrack_after_cleanup(metadata: dict[str, Any], cleaned: bool) -> bool:
        """Decide whether a tracked entry can be dropped after a cleanup attempt.

        Auto-clones stay tracked until their directory is actually removed, so a
        Windows-locked delete is retried later by ``cleanup_deferred_profiles``
        and startup recovery.  Named/master profiles (custom dir, not an
        auto-clone) are never deleted, so they are dropped immediately.
        """
        if cleaned:
            return True
        if not metadata.get("user_data_dir"):
            return True
        return bool(
            metadata.get("uses_custom_data_dir") is True
            and not metadata.get("auto_clone")
        )

    def _sweep_orphaned_temp_profiles(self) -> int:
        """
        Sweep stale nodriver temp profiles from the system temp directory on startup.

        Returns:
            int: Number of stale temp profile directories removed.
        """
        if self.orphan_profile_max_age_seconds == 0:
            return 0

        temp_root = Path(tempfile.gettempdir())
        if not temp_root.exists():
            return 0

        active_profile_dirs = self._get_active_browser_profile_dirs()
        removed_count = 0
        now = time.time()

        try:
            candidates = list(temp_root.glob(f"{self.PROFILE_SWEEP_PREFIX}*"))
        except Exception as error:
            debug_logger.log_warning(
                "process_cleanup",
                "sweep_profiles",
                f"Failed to enumerate temp profiles: {error}",
            )
            return 0

        for candidate in candidates:
            try:
                if not candidate.is_dir():
                    continue
                normalized_candidate = self._normalize_path(str(candidate))
                if normalized_candidate in active_profile_dirs:
                    continue
                age_seconds = now - candidate.stat().st_mtime
                if age_seconds < self.orphan_profile_max_age_seconds:
                    continue
                if self._cleanup_profile_dir(
                    str(candidate),
                    "startup-sweep",
                    active_profile_dirs=active_profile_dirs,
                ):
                    removed_count += 1
            except FileNotFoundError:
                continue
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "sweep_profiles",
                    f"Failed processing {candidate}: {error}",
                )

        if removed_count:
            debug_logger.log_info(
                "process_cleanup",
                "sweep_profiles",
                f"Removed {removed_count} stale temp profile directories",
            )

        return removed_count

    def _recover_orphaned_processes(self, force: bool = False):
        """Reap the browsers a previous run left behind, sparing every browser a
        live backend still owns unless *force* says otherwise.

        Recorded ownership is what separates "left behind" from "someone else's,
        right now" — the distinction the old create_time guard could not draw,
        since every already-running backend's browsers predate our import. See
        :mod:`browser_pid_registry` for the record's side of the rule.
        """
        saved_processes = self._load_tracked_pids()
        recovered_count = 0
        reaped: set[str] = set()

        for instance_id, metadata in saved_processes.items():
            # The ownership check is INSIDE the try because it can raise: it
            # reaches psutil through an injected callable, and a recorded pid
            # psutil rejects outright (a negative one) raises ValueError that
            # neither predicate converts. Escaping would abandon the remaining
            # entries and fail backend STARTUP through activate(); landing in
            # the except leaves the entry un-reaped instead — fail toward a
            # leak, never toward killing what we could not classify.
            try:
                if not force and not browser_pid_registry.is_reapable(
                    metadata, self._owner_backend_alive
                ):
                    continue
                # Recorded as reaped BEFORE the attempt, so a failed kill or a
                # locked profile dir still drops the entry, as the previous
                # unconditional wipe did. Retries: _sweep_orphaned_temp_profiles
                # for gettempdir profiles, clone_storage.enforce_session_storage
                # for session-root ones.
                reaped.add(instance_id)
                if self._kill_processes_for_metadata(
                    instance_id, metadata, recovery=True
                ):
                    recovered_count += 1
                self._cleanup_profile_for_metadata(instance_id, metadata)
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "recovery",
                    f"Failed recovering {instance_id}: {error}",
                )

        if recovered_count:
            debug_logger.log_info(
                "process_cleanup",
                "recovery",
                f"Killed {recovered_count} orphaned browser processes",
            )

        self._drop_recorded(reaped)
        self._sweep_orphaned_temp_profiles()

    def track_browser_process(
        self,
        instance_id: str,
        browser_process,
        user_data_dir: str | None = None,
        uses_custom_data_dir: bool | None = None,
        auto_clone: bool = False,
    ) -> bool:
        """
        Track a browser process and its profile metadata for future cleanup.

        Args:
            instance_id: Browser instance identifier.
            browser_process: Browser process object with `.pid`.
            user_data_dir: Browser profile directory.
            uses_custom_data_dir: Whether the profile directory was explicitly
                provided by the user.
            auto_clone: Whether the profile is a disposable auto-clone of master
                that should be deleted once its browser closes.

        Returns:
            bool: True if tracking was successful.
        """
        try:
            if not hasattr(browser_process, "pid") or not browser_process.pid:
                debug_logger.log_warning(
                    "process_cleanup",
                    "track_process",
                    f"Browser process for {instance_id} has no PID",
                )
                return False

            pid = browser_process.pid
            create_time = None
            with contextlib.suppress(
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
                OSError,
            ):
                create_time = psutil.Process(pid).create_time()
            metadata = browser_pid_registry.new_entry(
                pid,
                create_time=create_time,
                user_data_dir=user_data_dir,
                uses_custom_data_dir=uses_custom_data_dir,
                auto_clone=auto_clone,
            )
            self.browser_processes[instance_id] = metadata
            self.tracked_pids.add(pid)
            self._save_tracked_pids()

            debug_logger.log_info(
                "process_cleanup",
                "track_process",
                f"Tracking browser process {pid} for instance {instance_id}",
                metadata,
            )
            return True
        except Exception as error:
            debug_logger.log_error(
                "process_cleanup",
                "track_process",
                error,
            )
            return False

    def untrack_browser_process(self, instance_id: str) -> bool:
        """
        Stop tracking a browser process and persist the updated metadata file.

        Args:
            instance_id: Browser instance identifier.

        Returns:
            bool: True if untracking was successful.
        """
        try:
            metadata = self.browser_processes.get(instance_id)
            if metadata is None:
                return False

            pid = metadata.get("pid")
            if isinstance(pid, int):
                self.tracked_pids.discard(pid)
            del self.browser_processes[instance_id]
            self._drop_recorded({instance_id})

            debug_logger.log_info(
                "process_cleanup",
                "untrack_process",
                f"Stopped tracking process {pid} for instance {instance_id}",
            )
            return True
        except Exception as error:
            debug_logger.log_error(
                "process_cleanup",
                "untrack_process",
                error,
            )
            return False

    def kill_browser_process(self, instance_id: str) -> bool:
        """
        Kill a specific tracked browser process and clean its temp profile
        when appropriate.

        Args:
            instance_id: Browser instance identifier.

        Returns:
            bool: True if the process was killed or already gone.
        """
        metadata = self.browser_processes.get(instance_id)
        if metadata is None:
            return False

        success = self._kill_processes_for_metadata(instance_id, metadata)
        if success:
            active_profile_dirs = self._get_active_browser_profile_dirs()
            cleaned = self._cleanup_profile_for_metadata(
                instance_id,
                metadata,
                active_profile_dirs=active_profile_dirs,
            )
            if self._should_untrack_after_cleanup(metadata, cleaned):
                self.untrack_browser_process(instance_id)
            else:
                metadata["pid"] = None
                self.browser_processes[instance_id] = metadata
                self._save_tracked_pids()
        return success

    def finalize_browser_process(self, instance_id: str) -> bool:
        """Finalize tracked metadata after a browser was stopped elsewhere.

        Returns True if the tracked process was fully finalized.
        """
        metadata = self.browser_processes.get(instance_id)
        if metadata is None:
            return False

        pid = metadata.get("pid")
        profile_pids = self._get_browser_pids_for_profile(metadata.get("user_data_dir"))
        if profile_pids:
            return False
        if isinstance(pid, int) and psutil.pid_exists(pid):
            return False

        active_profile_dirs = self._get_active_browser_profile_dirs()
        cleaned = self._cleanup_profile_for_metadata(
            instance_id,
            metadata,
            active_profile_dirs=active_profile_dirs,
        )
        if self._should_untrack_after_cleanup(metadata, cleaned):
            self.untrack_browser_process(instance_id)
            return True

        metadata["pid"] = None
        self.browser_processes[instance_id] = metadata
        self._save_tracked_pids()
        return False

    def cleanup_deferred_profiles(self) -> int:
        """Retry cleanup for tracked temp profiles whose browser is already gone.

        Returns the number of deferred entries fully finalized. Deliberately
        passes NO pre-computed active-profile set: these deletes were deferred
        arbitrarily long ago, so ownership is re-measured per entry at FIRE
        time. A sweep-start snapshot is a defer-time answer (F-834).
        """
        finalized_count = 0

        for instance_id in list(self.browser_processes.keys()):
            metadata = self.browser_processes.get(instance_id)
            if metadata is None:
                continue

            pid = metadata.get("pid")
            if isinstance(pid, int) and psutil.pid_exists(pid):
                continue

            cleaned = self._cleanup_profile_for_metadata(instance_id, metadata)
            if self._should_untrack_after_cleanup(
                metadata, cleaned
            ) and self.untrack_browser_process(instance_id):
                finalized_count += 1

        if finalized_count:
            debug_logger.log_info(
                "process_cleanup",
                "cleanup_deferred_profiles",
                f"Finalized {finalized_count} deferred browser profile "
                f"cleanup entrie(s)",
            )

        return finalized_count

    def _kill_process_by_pid(self, pid: int, instance_id: str = "unknown") -> bool:  # noqa: PLR0911  plan_M11a
        """
        Kill a browser process by PID using escalating termination methods.

        Args:
            pid: Process ID to kill.
            instance_id: Instance identifier for diagnostics.

        Returns:
            bool: True if the process was killed or already absent.
        """
        try:
            if not psutil.pid_exists(pid):
                debug_logger.log_info(
                    "process_cleanup",
                    "kill_process",
                    f"Process {pid} for {instance_id} already terminated",
                )
                return True

            try:
                process = psutil.Process(pid)
                process_name = process.name()
                if not self._is_browser_process_name(process_name):
                    debug_logger.log_warning(
                        "process_cleanup",
                        "kill_process",
                        f"PID {pid} is not a browser process "
                        f"({process_name}), skipping",
                    )
                    return False
            except psutil.NoSuchProcess:
                return True
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "kill_process",
                    f"Could not verify process {pid}: {error}",
                )

            try:
                process = psutil.Process(pid)
                process.terminate()
                try:
                    process.wait(timeout=3)
                    debug_logger.log_info(
                        "process_cleanup",
                        "kill_process",
                        f"Process {pid} for {instance_id} terminated gracefully",
                    )
                    return True
                except psutil.TimeoutExpired:
                    pass
            except psutil.NoSuchProcess:
                return True
            except Exception as error:
                debug_logger.log_warning(
                    "process_cleanup",
                    "kill_process",
                    f"Failed to terminate process {pid} gracefully: {error}",
                )

            try:
                process = psutil.Process(pid)
                process.kill()
                try:
                    process.wait(timeout=2)
                    debug_logger.log_info(
                        "process_cleanup",
                        "kill_process",
                        f"Process {pid} for {instance_id} force killed",
                    )
                    return True
                except psutil.TimeoutExpired:
                    debug_logger.log_warning(
                        "process_cleanup",
                        "kill_process",
                        f"Process {pid} for {instance_id} did not die after force kill",
                    )
                    return False
            except psutil.NoSuchProcess:
                return True
            except Exception as error:
                debug_logger.log_error(
                    "process_cleanup",
                    "kill_process",
                    error,
                )
                return False
        except Exception as error:
            debug_logger.log_error(
                "process_cleanup",
                "kill_process",
                error,
            )
            return False

    def _cleanup_all_tracked(self):
        """Clean up every tracked browser process and temp profile for this run."""
        if not self.browser_processes:
            debug_logger.log_info(
                "process_cleanup",
                "cleanup_all",
                "No browser processes to clean up",
            )
            return

        debug_logger.log_info(
            "process_cleanup",
            "cleanup_all",
            f"Cleaning up {len(self.browser_processes)} browser processes...",
        )

        cleaned_count = 0
        for instance_id in list(self.browser_processes.keys()):
            if self.kill_browser_process(instance_id) or self.finalize_browser_process(
                instance_id
            ):
                cleaned_count += 1

        debug_logger.log_info(
            "process_cleanup",
            "cleanup_all",
            f"Cleaned up {cleaned_count} tracked browser process entries",
        )

        # Whatever survived stays recorded; what was cleaned up already dropped
        # its own entry through untrack. No wipe here, for the same reason there
        # is none in recovery — the file also holds other backends' browsers.
        if self.browser_processes:
            self._save_tracked_pids()

    def get_tracked_processes(self) -> dict[str, int]:
        """Currently tracked browser PIDs, keyed by instance id."""
        return {
            instance_id: metadata["pid"]
            for instance_id, metadata in self.browser_processes.items()
            if isinstance(metadata.get("pid"), int)
        }

    def is_process_alive(self, instance_id: str) -> bool:
        """Whether the tracked process for ``instance_id`` still exists."""
        metadata = self.browser_processes.get(instance_id)
        if metadata is None:
            return False

        pid = metadata.get("pid")
        if not isinstance(pid, int):
            return bool(
                self._get_browser_pids_for_profile(metadata.get("user_data_dir"))
            )

        return psutil.pid_exists(pid) or bool(
            self._get_browser_pids_for_profile(metadata.get("user_data_dir"))
        )


process_cleanup = ProcessCleanup()
