import contextlib
import gzip
import json
import logging
import pickle
import sys
import threading
import traceback
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stealth_chrome_devtools_mcp.embedded.logging_setup import correlation_id_var

# The durable half of F-182/F-304: every log_error/log_warning/log_info call
# also emits here (unconditionally - see the gate removed in each method
# below), so the file has every record regardless of enable()/disable() or
# whether any debug tool is ever called. logging_setup.configure_logging
# installs the handler on this same logger name; if it hasn't run yet (e.g.
# under test), this is a normal Logger with no handlers - a safe no-op.
_backend_logger = logging.getLogger("stealth.backend")

#: The component every ``log_tool_failure`` entry is filed under (F-835), so
#: "which TOOL calls are failing" is one lookup in ``component_breakdown``
#: instead of a scan of whatever component each tool body happened to name.
TOOL_COMPONENT = "tool"


class DebugLogger:
    """Centralized debug logging system for the MCP server."""

    MAX_ERRORS = 500
    MAX_WARNINGS = 1000
    MAX_INFO = 2000
    MAX_SEEN_ERRORS = 1000
    _GZIP_THRESHOLD = 1000
    _PICKLE_THRESHOLD = 100
    #: How far back :meth:`log_tool_failure` looks for a record of the SAME
    #: exception already made by the tool body itself. A bound, not a full scan:
    #: the body's own entry (if any) is at most a handful of appends old, and
    #: this runs while the ring lock is held.
    _DUPLICATE_SCAN_DEPTH = 25

    def __init__(self):
        """
        Initializes the DebugLogger.

        Variables:
            self._errors: Stores error logs (capped at MAX_ERRORS).
            self._warnings: Stores warning logs (capped at MAX_WARNINGS).
            self._info: Stores info logs (capped at MAX_INFO).
            self._stats: Stores statistics for errors, warnings, and calls.
            self._lock: Ensures thread safety for logging.
            self._enabled: Indicates if logging is enabled.
            self._seen_errors: Track error signatures (capped at MAX_SEEN_ERRORS).
        """
        self._errors: list[dict[str, Any]] = []
        self._warnings: list[dict[str, Any]] = []
        self._info: list[dict[str, Any]] = []
        self._stats: dict[str, int] = defaultdict(int)
        # RLock, not Lock (Amendment A1 / F-764): export_to_file_paginated
        # acquires this lock and then calls get_debug_view_paginated, which
        # re-enters `with self._lock:` on the same thread. A plain Lock
        # self-deadlocks there unconditionally; only the lock TYPE changes
        # here (_lock_owner bookkeeping and export internals are unchanged).
        self._lock = threading.RLock()
        self._enabled = False
        self._lock_owner = "none"

        self._lock_acquired_time = 0
        # OrderedDict (values unused) rather than set: F-204's LRU eviction
        # needs move_to_end()/popitem(last=False), which plain dict/set don't
        # expose even though both are already insertion-ordered.
        self._seen_errors: OrderedDict[str, None] = OrderedDict()

    def _emit_stderr(self, message: str, force: bool = False):
        """
        Emit a debug line to stderr only when logging is enabled unless forced.

        Args:
            message (str): Message to emit.
            force (bool): Whether to emit regardless of current debug state.
        """
        if not force and not self._enabled:
            return
        with contextlib.suppress(OSError, ValueError):
            print(message, file=sys.stderr)  # noqa: T201  plan_M3

    def log_error(
        self,
        component: str,
        method: str,
        error: Exception,
        context: dict[str, Any] | None = None,
    ):
        """
        Log an error with full context.

        Args:
            component (str): Name of the component where the error occurred.
            method (str): Name of the method where the error occurred.
            error (Exception): The exception instance.
            context (Optional[Dict[str, Any]]): Additional context for the error.
        """
        with self._lock:
            # Unconditional and NOT deduped (F-182/F-204): every call reaches
            # the durable file regardless of enable() or in-memory dedup, so
            # a suppressed-in-memory repeat still has every occurrence on disk.
            _backend_logger.error("%s.%s: %s", component, method, error, exc_info=error)
            self._record_error(component, method, error, context)

    def _record_error(
        self,
        component: str,
        method: str,
        error: Exception,
        context: dict[str, Any] | None = None,
    ):
        """Put one error in the in-memory ring: dedup, cap, stats, stderr echo.

        The ring half of :meth:`log_error`, split out so :meth:`log_tool_failure`
        can reach the ring WITHOUT the durable ``_backend_logger.error`` line
        above (see that method for why). One ring-append implementation, two
        entry points that differ only in whether the record is also written to
        the backend log file.

        Caller must hold ``self._lock``.
        """
        error_signature = f"{component}.{method}.{type(error).__name__}.{error!s}"

        if error_signature in self._seen_errors:
            self._seen_errors.move_to_end(error_signature)
            self._stats[f"{component}.{method}.errors"] += 1
            return

        if len(self._seen_errors) >= self.MAX_SEEN_ERRORS:
            # F-204: evict the single oldest signature (LRU), not every
            # tracked signature - clearing the whole set used to make
            # hitting the cap re-log every still-recent error at once.
            self._seen_errors.popitem(last=False)
        self._seen_errors[error_signature] = None

        error_entry = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "component": component,
            "method": method,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "correlation_id": correlation_id_var.get(),
            "traceback": traceback.format_exc(),
            "context": context or {},
        }
        self._errors.append(error_entry)
        if len(self._errors) > self.MAX_ERRORS:
            self._errors = self._errors[-self.MAX_ERRORS :]
        self._stats[f"{component}.{method}.errors"] += 1
        self._emit_stderr(f"[DEBUG ERROR] {component}.{method}: {error}")

    def log_tool_failure(self, tool_name: str, error: Exception):
        """Record a tool call that FAILED (F-835) — the one entry point for the
        ``section_tool`` wrapper (``logging_setup.with_correlation_id``).

        Before this existed, a failing tool emitted the INFO start/end pair and
        nothing else: 24 consecutive failed ``spawn_browser`` calls left
        ``get_debug_view`` reporting ``total_errors: 0`` during a total outage.
        The wrapper now hands every escaping exception here on its way to the
        client; the exception itself is untouched.

        Filed under the ``tool`` component with the tool's own name as the
        method, so the entry says WHICH tool failed and WITH WHAT, and
        ``component_breakdown["tool"]`` counts the failures the client saw. The
        entry carries the call's correlation id, which is what joins it to the
        ``tool X start`` / ``tool X end`` pair in the backend log.

        **Ring only — deliberately NOT the durable ``_backend_logger.error``
        line ``log_error`` writes.** A tool's failure message echoes the
        caller's own arguments (paths, selectors, indices, URLs), and F-782's
        finding makes the condition explicit: logging the exception must not
        land before those records go through a redactor. The in-memory ring is
        local to the process and reaches only the client that made the failing
        call — bytes it already holds — while the log file is durable and is
        bridged to Sentry by ``LoggingIntegration(event_level=ERROR)``. So the
        ring half of F-782 is closed here (F-835) and its log half stays open
        until the redaction question is answered.

        Args:
            tool_name (str): The registered tool whose call failed.
            error (Exception): The exception on its way to the client.
        """
        with self._lock:
            # Not a blanket "this call already logged something": a body that
            # logs an unrelated error mid-call must still get its FAILURE
            # recorded. Only the same exception, in the same call, is a
            # duplicate - and log_error's own dedup cannot see it, because the
            # body files it under a different component/method signature.
            if self._recorded_in_this_call(error):
                return
            self._record_error(TOOL_COMPONENT, tool_name, error)

    def _recorded_in_this_call(self, error: Exception) -> bool:
        """Whether this exact exception is already in the ring for this call.

        Caller must hold ``self._lock``.
        """
        correlation_id = correlation_id_var.get()
        error_type = type(error).__name__
        message = str(error)
        for entry in reversed(self._errors[-self._DUPLICATE_SCAN_DEPTH :]):
            if (
                entry["correlation_id"] == correlation_id
                and entry["error_type"] == error_type
                and entry["error_message"] == message
            ):
                return True
        return False

    def log_warning(
        self,
        component: str,
        method: str,
        message: str,
        context: dict[str, Any] | None = None,
    ):
        """
        Log a warning.

        Args:
            component (str): Name of the component where the warning occurred.
            method (str): Name of the method where the warning occurred.
            message (str): Warning message.
            context (Optional[Dict[str, Any]]): Additional context for the warning.
        """
        with self._lock:
            _backend_logger.warning("%s.%s: %s", component, method, message)

            warning_entry = {
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "component": component,
                "method": method,
                "message": message,
                "context": context or {},
            }
            self._warnings.append(warning_entry)
            if len(self._warnings) > self.MAX_WARNINGS:
                self._warnings = self._warnings[-self.MAX_WARNINGS :]
            self._stats[f"{component}.{method}.warnings"] += 1
            self._emit_stderr(f"[DEBUG WARN] {component}.{method}: {message}")

    def log_info(
        self, component: str, method: str, message: str, data: Any | None = None
    ):
        """
        Log information for debugging.

        Args:
            component (str): Name of the component where the info is logged.
            method (str): Name of the method where the info is logged.
            message (str): Info message.
            data (Optional[Any]): Additional data for the info log.
        """
        with self._lock:
            _backend_logger.info("%s.%s: %s", component, method, message)

            info_entry = {
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "component": component,
                "method": method,
                "message": message,
                "data": data,
            }
            self._info.append(info_entry)
            if len(self._info) > self.MAX_INFO:
                self._info = self._info[-self.MAX_INFO :]
            self._stats[f"{component}.{method}.calls"] += 1
            self._emit_stderr(f"[DEBUG INFO] {component}.{method}: {message}")
            if data:
                self._emit_stderr(f"  Data: {data}")

    def log_debug(
        self,
        component: str,
        method: str,
        message: str,
        context: dict[str, Any] | None = None,
    ):
        """
        Log a debug-level message. M10a: unlike error/warning/info, this level
        keeps NO in-memory ring and never appears in get_debug_view/export - the
        file (via _backend_logger) is its only sink, and the stdlib logger's own
        level check drops it for free at the default level. This is deliberate:
        a ring here would (a) change get_debug_view's return shape, a tool
        contract that must stay byte-stable, and (b) pay lock+append on hot
        per-event paths even when the record is dropped - exactly what the
        droppable-by-default DEBUG sites (network/hook handlers) must not do.
        No lock is taken: no shared state is mutated, only a stdlib call is made.

        Args:
            component (str): Name of the component logging the message.
            method (str): Name of the method logging the message.
            message (str): Debug message.
            context (Optional[Dict[str, Any]]): Unused; accepted for call-site
                symmetry with log_warning/log_error.
        """
        del context
        _backend_logger.debug("%s.%s: %s", component, method, message)
        self._emit_stderr(f"[DEBUG] {component}.{method}: {message}")

    def get_debug_view(self) -> dict[str, Any]:
        """
        Get comprehensive debug view of all logged data.

        Returns:
            Dict[str, Any]: Summary, recent errors/warnings,
                all errors/warnings, and component breakdown.
        """
        return self.get_debug_view_paginated()

    def get_debug_view_paginated(
        self,
        max_errors: int | None = None,
        max_warnings: int | None = None,
        max_info: int | None = None,
    ) -> dict[str, Any]:
        """
        Get paginated debug view of logged data with size limits.

        Args:
            max_errors: Max errors to include. None for all.
            max_warnings: Max warnings to include. None for all.
            max_info: Max info logs to include. None for all.

        Returns:
            Dict[str, Any]: Summary, recent errors/warnings,
                limited errors/warnings, and component breakdown.
        """
        with self._lock:
            if max_errors is not None:
                limited_errors = self._errors[-max_errors:] if self._errors else []
                all_errors = limited_errors
            else:
                limited_errors = self._errors[-10:] if self._errors else []
                all_errors = self._errors

            if max_warnings is not None:
                limited_warnings = (
                    self._warnings[-max_warnings:] if self._warnings else []
                )
                all_warnings = limited_warnings
            else:
                limited_warnings = self._warnings[-10:] if self._warnings else []
                all_warnings = self._warnings

            if max_info is not None:
                limited_info = self._info[-max_info:] if self._info else []
                all_info = limited_info
            else:
                limited_info = self._info[-10:] if self._info else []
                all_info = self._info

            return {
                "summary": {
                    "total_errors": len(self._errors),
                    "total_warnings": len(self._warnings),
                    "total_info": len(self._info),
                    "returned_errors": len(all_errors),
                    "returned_warnings": len(all_warnings),
                    "returned_info": len(all_info),
                    "error_types": self._get_error_summary(),
                    "stats": dict(self._stats),
                },
                "recent_errors": limited_errors,
                "recent_warnings": limited_warnings,
                "recent_info": limited_info,
                "all_errors": all_errors,
                "all_warnings": all_warnings,
                "all_info": all_info,
                "component_breakdown": self._get_component_breakdown(),
            }

    def _get_error_summary(self) -> dict[str, int]:
        """
        Get summary of error types.

        Returns:
            Dict[str, int]: Dictionary mapping error type names to their counts.
        """
        error_types = defaultdict(int)
        for error in self._errors:
            error_types[error["error_type"]] += 1
        return dict(error_types)

    def _get_component_breakdown(self) -> dict[str, dict[str, int]]:
        """
        Get breakdown by component.

        Returns:
            Dict[str, Dict[str, int]]: Component names mapped
                to their error, warning, and call counts.
        """
        breakdown = defaultdict(lambda: {"errors": 0, "warnings": 0, "calls": 0})

        for error in self._errors:
            breakdown[error["component"]]["errors"] += 1

        for warning in self._warnings:
            breakdown[warning["component"]]["warnings"] += 1

        for info in self._info:
            breakdown[info["component"]]["calls"] += 1

        return dict(breakdown)

    def clear_debug_view(self):
        """
        Clear all debug logs with timeout protection.

        Variables:
            self._errors (List[Dict[str, Any]]): Cleared.
            self._warnings (List[Dict[str, Any]]): Cleared.
            self._info (List[Dict[str, Any]]): Cleared.
            self._stats (Dict[str, int]): Cleared.
        """
        try:
            if self._lock.acquire(timeout=5.0):
                try:
                    self._errors.clear()
                    self._warnings.clear()
                    self._info.clear()
                    self._stats.clear()
                    # F-835: the dedup set is a projection of the ring, so it
                    # goes with it. Left behind, "clear the view and watch"
                    # (the operator loop a live outage is diagnosed with)
                    # would show an empty ring forever: every repeat of an
                    # already-seen failure would be deduped against a
                    # signature whose entry no longer exists.
                    self._seen_errors.clear()
                    self._emit_stderr("[DEBUG] Debug logs cleared")
                finally:
                    self._lock.release()
            else:
                self._emit_stderr(
                    "[DEBUG] Failed to clear logs - timeout acquiring lock"
                )
        except Exception as e:  # noqa: BLE001  DEBT(F-181)
            self._emit_stderr(f"[DEBUG] Error clearing logs: {e}")

    def clear_debug_view_safe(self):
        """
        Safe version that recreates data structures if lock fails.
        """
        try:
            self.clear_debug_view()
        except Exception:  # noqa: BLE001  DEBT(F-181)
            self._errors = []
            self._warnings = []
            self._info = []
            self._stats = defaultdict(int)
            self._seen_errors = OrderedDict()  # F-835, same reason as above
            self._emit_stderr("[DEBUG] Debug logs force-cleared (lock bypass)")

    def enable(self):
        """
        Enable debug logging.

        Variables:
            self._enabled (bool): Set to True.
        """
        self._enabled = True
        self._emit_stderr("[DEBUG] Debug logging enabled", force=True)

    def disable(self):
        """
        Disable debug logging.

        Variables:
            self._enabled (bool): Set to False.
        """
        self._emit_stderr("[DEBUG] Debug logging disabled")
        self._enabled = False

    def get_lock_status(self) -> dict[str, Any]:
        """Get current lock status for debugging."""
        import time

        return {
            "lock_owner": self._lock_owner,
            "lock_held_duration": time.time() - self._lock_acquired_time
            if self._lock_acquired_time > 0
            else 0,
            "lock_acquired": self._lock.locked()
            if hasattr(self._lock, "locked")
            else "unknown",
        }

    def export_to_file(self, filepath: str = "debug_log.json"):
        """
        Export debug logs to a JSON file.

        Args:
            filepath (str): Path to the file where logs will be exported.

        Returns:
            str: The filepath where logs were exported.
        """
        return self.export_to_file_paginated(filepath)

    def export_to_file_paginated(
        self,
        filepath: str = "debug_log.json",
        max_errors: int | None = None,
        max_warnings: int | None = None,
        max_info: int | None = None,
        fmt: str = "auto",
    ):
        """
        Export paginated debug logs to a file using fastest method available.

        Args:
            filepath: Path to the output file.
            max_errors: Max errors to export. None for all.
            max_warnings: Max warnings to export. None for all.
            max_info: Max info logs to export. None for all.
            fmt: Export format ('json'/'pickle'/'gzip-pickle'/'auto').

        Returns:
            str: The filepath where logs were exported.
        """
        import time

        try:
            self._emit_stderr(
                "[DEBUG] export_debug_logs attempting lock acquisition..."
            )
            current_status = self.get_lock_status()
            self._emit_stderr(f"[DEBUG] Current lock status: {current_status}")

            acquired = self._lock.acquire(timeout=5.0)
            if not acquired:
                self._emit_stderr(
                    "[DEBUG] Lock timeout - falling back to lock-free export"
                )
                return self._export_lockfree(
                    filepath, max_errors, max_warnings, max_info, fmt
                )

            self._lock_owner = "export_debug_logs"
            self._lock_acquired_time = time.time()
            self._emit_stderr("[DEBUG] Lock acquired by export_debug_logs")

            try:
                debug_data = self.get_debug_view_paginated(
                    max_errors=max_errors, max_warnings=max_warnings, max_info=max_info
                )
            finally:
                self._lock_owner = "none"
                self._lock_acquired_time = 0
                self._lock.release()
                self._emit_stderr("[DEBUG] Lock released by export_debug_logs")
        except Exception as e:  # noqa: BLE001  DEBT(F-181)
            self._emit_stderr(f"[DEBUG] Exception in export: {e}")
            return self._export_lockfree(
                filepath, max_errors, max_warnings, max_info, fmt
            )

        if fmt == "auto":
            total_items = (
                debug_data["summary"]["returned_errors"]
                + debug_data["summary"]["returned_warnings"]
                + debug_data["summary"]["returned_info"]
            )
            if total_items > self._GZIP_THRESHOLD:
                fmt = "gzip-pickle"
            elif total_items > self._PICKLE_THRESHOLD:
                fmt = "pickle"
            else:
                fmt = "json"

        if fmt == "gzip-pickle":
            return self._export_gzip_pickle(debug_data, filepath)
        if fmt == "pickle":
            return self._export_pickle(debug_data, filepath)
        return self._export_json(debug_data, filepath)

    def _export_lockfree(
        self,
        filepath: str,
        max_errors: int | None,
        max_warnings: int | None,
        max_info: int | None,
        fmt: str,
    ) -> str:
        """
        Lock-free export method that creates a snapshot without acquiring locks.
        """
        errors_snapshot = list(self._errors)
        warnings_snapshot = list(self._warnings)
        info_snapshot = list(self._info)

        if max_errors is not None:
            errors_snapshot = errors_snapshot[:max_errors]
        if max_warnings is not None:
            warnings_snapshot = warnings_snapshot[:max_warnings]
        if max_info is not None:
            info_snapshot = info_snapshot[:max_info]

        debug_data = {
            "summary": {
                "total_errors": len(self._errors),
                "total_warnings": len(self._warnings),
                "total_info": len(self._info),
                "returned_errors": len(errors_snapshot),
                "returned_warnings": len(warnings_snapshot),
                "returned_info": len(info_snapshot),
            },
            "all_errors": errors_snapshot,
            "all_warnings": warnings_snapshot,
            "all_info": info_snapshot,
        }

        if fmt == "auto":
            total_items = (
                len(errors_snapshot) + len(warnings_snapshot) + len(info_snapshot)
            )
            if total_items > self._GZIP_THRESHOLD:
                fmt = "gzip-pickle"
            elif total_items > self._PICKLE_THRESHOLD:
                fmt = "pickle"
            else:
                fmt = "json"

        if fmt == "gzip-pickle":
            return self._export_gzip_pickle(debug_data, filepath)
        if fmt == "pickle":
            return self._export_pickle(debug_data, filepath)
        return self._export_json(debug_data, filepath)

    def _export_gzip_pickle(self, debug_data: dict[str, Any], filepath: str) -> str:
        if not filepath.endswith(".pkl.gz"):
            filepath = filepath.replace(".json", ".pkl.gz")

        with gzip.open(filepath, "wb") as f:
            pickle.dump(debug_data, f, protocol=pickle.HIGHEST_PROTOCOL)

        file_size = Path(filepath).stat().st_size
        self._emit_stderr(
            f"[DEBUG] Exported {debug_data['summary']['returned_errors']} errors, "
            f"{debug_data['summary']['returned_warnings']} warnings, "
            f"{debug_data['summary']['returned_info']} info logs to {filepath} "
            f"({file_size} bytes, gzip-pickle format)"
        )
        return filepath

    def _export_pickle(self, debug_data: dict[str, Any], filepath: str) -> str:
        """Export using pickle (fast for medium data)."""
        if not filepath.endswith(".pkl"):
            filepath = filepath.replace(".json", ".pkl")

        with Path(filepath).open("wb") as f:
            pickle.dump(debug_data, f, protocol=pickle.HIGHEST_PROTOCOL)

        file_size = Path(filepath).stat().st_size
        self._emit_stderr(
            f"[DEBUG] Exported {debug_data['summary']['returned_errors']} errors, "
            f"{debug_data['summary']['returned_warnings']} warnings, "
            f"{debug_data['summary']['returned_info']} info logs to {filepath} "
            f"({file_size} bytes, pickle format)"
        )
        return filepath

    def _export_json(self, debug_data: dict[str, Any], filepath: str) -> str:
        """Export using JSON (human readable but slower)."""
        with Path(filepath).open("w") as f:
            json.dump(debug_data, f, separators=(",", ":"), default=str)

        file_size = Path(filepath).stat().st_size
        self._emit_stderr(
            f"[DEBUG] Exported {debug_data['summary']['returned_errors']} errors, "
            f"{debug_data['summary']['returned_warnings']} warnings, "
            f"{debug_data['summary']['returned_info']} info logs to {filepath} "
            f"({file_size} bytes, JSON format)"
        )
        return filepath


debug_logger = DebugLogger()
