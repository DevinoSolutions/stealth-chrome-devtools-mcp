"""File-based logging spine for stealth-chrome-devtools-mcp (plan M3).

This module is the ONE place log-WRITING is configured: handlers, formatters,
rotation, correlation-id stamping, log-dir resolution, and old-log pruning.
``observability.py`` (Sentry) is the separate error-SHIPPING home — do not
merge or duplicate either. Named ``logging_setup`` (not ``logging``) so it
never shadows the stdlib on the bare-name ``sys.path`` the embedded package
uses.

Two roles call :func:`configure_logging`: the backend process
(``role="backend"``, from ``embedded/server.py``'s ``__main__``) and the
stdio proxy (``role="proxy"``, from ``singleton.run_stdio_proxy``). Each gets
its own ``stealth.<role>`` logger writing to ``<logdir>/<role>-<pid>.log`` —
per-pid filenames sidestep Windows ``RotatingFileHandler`` rename contention
between two backends briefly coexisting (plan_M3 §2.2, rejected alternative 3).

``singleton.py`` also needs this module (the boot-log redirect and the
``configure_logging("proxy")`` call), while :func:`resolve_log_dir` reuses
``singleton.STATE_DIR``. Importing ``singleton`` here at module top level
would therefore create a cycle; the codebase's established fix for exactly
this shape (embedded/runpy/singleton architecture, see pyproject.toml's
PLC0415 rationale) is a deferred, function-local import — used below.
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import os
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.settings import get_settings

if TYPE_CHECKING:
    from types import TracebackType

LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(process)d [%(correlation_id)s] %(name)s: %(message)s"
)
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    """A short id for one tool call, stamped on every log line emitted during
    it by :class:`CorrelationIdFilter`."""
    return uuid.uuid4().hex[:12]


class CorrelationIdFilter(logging.Filter):
    """Stamps ``record.correlation_id`` from the current context var."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


def resolve_log_dir() -> Path:
    """``STEALTH_MCP_LOG_DIR`` override, else the existing per-user state-dir
    convention (``singleton.STATE_DIR / "logs"``). Pure — never creates the
    directory.
    """
    configured = get_settings().log_dir
    if configured and configured.strip():
        return Path(configured).expanduser()

    import singleton  # deferred: breaks the singleton<->logging_setup cycle

    return singleton.STATE_DIR / "logs"


def configure_logging(role: str) -> Path:
    """Idempotent: install one ``RotatingFileHandler`` for ``stealth.<role>``.

    Returns the log file path regardless of whether setup succeeded. Never
    raises — a logging-setup failure must not take down the backend/proxy
    (plan_M3 risk #7); on failure this degrades to a no-op.
    """
    log_dir = resolve_log_dir()
    log_path = log_dir / f"{role}-{os.getpid()}.log"
    logger = logging.getLogger(f"stealth.{role}")

    if logger.handlers:
        return log_path  # already configured in this process

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            delay=True,
            encoding="utf-8",
        )
        handler.addFilter(CorrelationIdFilter())
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
        logger.propagate = False
        level_name = get_settings().log_level.upper()
        logger.setLevel(getattr(logging, level_name, logging.INFO))
    except OSError:
        return log_path

    prune_old_logs(log_dir)
    return log_path


_bootstrapped_roles: set[str] = set()


def bootstrap_backend_process_logging() -> Path:
    """Backend boot-time wiring — the single call ``embedded/server.py``'s
    ``__main__`` makes before anything else, including ``sentry_init()``
    (F-303's in-process half). Installs the ``stealth.backend`` file handler,
    then a ``sys.excepthook``/``threading.excepthook`` pair that record a
    fatal exception before the process dies, plus ``faulthandler`` for
    hard/C-level faults that never reach Python's exception machinery at
    all. Idempotent (safe if ``embedded/server.py`` loads twice via
    ``runpy``). Returns the ``stealth.backend`` log path.
    """
    log_path = configure_logging("backend")
    logger = logging.getLogger("stealth.backend")

    if "backend" in _bootstrapped_roles:
        return log_path  # already wired in this process

    def _log_excepthook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        logger.critical(
            "Fatal unhandled exception", exc_info=(exc_type, exc_value, exc_tb)
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _log_thread_excepthook(args: threading.ExceptHookArgs) -> None:
        thread = args.thread
        thread_name = thread.name if thread is not None else "unknown"
        exc_value = args.exc_value
        if exc_value is None:
            threading.__excepthook__(args)
            return
        logger.critical(
            "Fatal unhandled exception in thread %r",
            thread_name,
            exc_info=(args.exc_type, exc_value, args.exc_traceback),
        )
        threading.__excepthook__(args)

    sys.excepthook = _log_excepthook
    threading.excepthook = _log_thread_excepthook
    _bootstrapped_roles.add("backend")

    # A dedicated, never-rotated file: faulthandler writes at the C level on a
    # hard crash, so sharing the RotatingFileHandler's file would add a second
    # open handle across the SAME path it may later os.rename() during
    # rotation (Windows WinError 32 risk, plan_M3 risk #1).
    fault_log_path = log_path.with_name(f"{log_path.stem}-fault.log")
    with contextlib.suppress(OSError):
        fault_log = fault_log_path.open("a", encoding="utf-8")
        faulthandler.enable(file=fault_log)

    return log_path


def prune_old_logs(
    log_dir: Path | None = None, keep_days: int = 7, keep_files: int = 50
) -> None:
    """Best-effort sweep of ``<logdir>`` so per-pid log files (one per proxy
    session) don't accumulate forever. Never raises.
    """
    try:
        target_dir = log_dir if log_dir is not None else resolve_log_dir()
        if not target_dir.is_dir():
            return
        files = sorted(
            (p for p in target_dir.glob("*.log*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        cutoff = time.time() - keep_days * 86400
        for index, path in enumerate(files):
            if index >= keep_files or path.stat().st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    path.unlink()
    except OSError:
        pass
