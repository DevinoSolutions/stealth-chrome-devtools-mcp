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
import functools
import inspect
import logging
import os
import re
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
    from collections.abc import Callable
    from types import TracebackType

LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(process)d [%(correlation_id)s] %(name)s: %(message)s"
)
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

# The shared raw-stream file every backend's Popen stdout/stderr is redirected
# into (singleton._start_server_process). One file for ALL boots, by design:
# an import-time crash happens before the process knows its own log name.
BOOT_LOG_NAME = "backend-boot.log"
# F-830: unrotated, this reached 794 MB on the reporting machine. 16 MB still
# holds many boots' worth of tracebacks while staying small enough to open in
# an editor; 2 backups caps the whole boot-log family at ~48 MB.
_BOOT_LOG_MAX_BYTES = 16 * 1024 * 1024
_BOOT_LOG_BACKUPS = 2

# F-840: a dead backend's log set IS the post-mortem. Keep the newest few
# unconditionally (age is exactly what an unattended crash accrues before
# anyone looks), and hold every fault log for a fortnight.
_KEEP_BACKEND_SETS = 3
_FAULT_LOG_KEEP_DAYS = 14
# ``backend-<pid>.log``, its ``.1``/``.2`` rotations, and the matching
# ``backend-<pid>-fault.log``. ``backend-boot.log`` deliberately does not match
# (``boot`` is not a pid): it is shared across backends, not one's post-mortem.
_BACKEND_LOG_RE = re.compile(r"^backend-(\d+)(?:-fault)?\.log")

# F-809: FastMCP hard-codes uvicorn's timeout_graceful_shutdown to 0, and a
# zero-second asyncio timeout always fires — so every clean HTTP stop ERROR-logs
# "timeout graceful shutdown exceeded" (and Sentry ships it). Sized against
# singleton._terminate_backend's 5 s wait; never None, uvicorn's "wait forever".
_GRACEFUL_SHUTDOWN_SECONDS = 2.0

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


def backend_uvicorn_config() -> dict[str, object]:
    """The ``uvicorn_config`` the backend's ``mcp.run(transport="http", …)``
    passes — the one home for how the backend's HTTP server logs and stops.

    ``access_log=False`` is F-830's first half. Uvicorn writes one INFO line
    per request to stdout, which ``singleton._start_server_process`` redirects
    into the shared boot log, and the client watchdog probes every live stdio
    proxy every ~2 s: ~13M lines / 794 MB of ``"POST /mcp/ 200"`` on the
    reporting machine, with zero diagnostic value. Only the HTTP access spam
    goes — the calls that matter are logged by :func:`with_correlation_id`
    against ``stealth.backend``, which this does not touch.
    """
    return {
        "timeout_graceful_shutdown": _GRACEFUL_SHUTDOWN_SECONDS,
        "access_log": False,
    }


def new_correlation_id() -> str:
    """A short id for one tool call, stamped on every log line emitted during
    it by :class:`CorrelationIdFilter`."""
    return uuid.uuid4().hex[:12]


class CorrelationIdFilter(logging.Filter):
    """Stamps ``record.correlation_id`` from the current context var."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


_tool_call_logger = logging.getLogger("stealth.backend")


def with_correlation_id(func: Callable[..., object]) -> Callable[..., object]:
    """Wrap a registered tool function (the ``section_tool`` chokepoint, F-308)
    so every call gets a fresh correlation id — stamped by
    :class:`CorrelationIdFilter` onto every log line emitted during the call,
    backend file and ``debug_logger`` entries alike — and one INFO start/end
    pair. ``functools.wraps`` preserves the schema FastMCP introspects
    (name/signature/docstring); a ``tools/list`` schema-snapshot test pins
    that this holds for a representative tool per section.

    91 of the 96 registered tools are ``async def`` and 5 are plain ``def``;
    Python has no single syntax that both ``await``s and doesn't, so this
    branches once on ``iscoroutinefunction`` to produce a matching wrapper.
    """
    # Not every Callable is guaranteed a __name__ (e.g. a callable class
    # instance); all 96 real registrations are plain def/async def, but this
    # keeps the wrapper honest for its declared, more general parameter type.
    tool_name = getattr(func, "__name__", repr(func))

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: object, **kwargs: object) -> object:
            token = correlation_id_var.set(new_correlation_id())
            start = time.monotonic()
            _tool_call_logger.info("tool %s start", tool_name)
            try:
                return await func(*args, **kwargs)
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000
                _tool_call_logger.info("tool %s end (%.1fms)", tool_name, elapsed_ms)
                correlation_id_var.reset(token)

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args: object, **kwargs: object) -> object:
        token = correlation_id_var.set(new_correlation_id())
        start = time.monotonic()
        _tool_call_logger.info("tool %s start", tool_name)
        try:
            return func(*args, **kwargs)
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            _tool_call_logger.info("tool %s end (%.1fms)", tool_name, elapsed_ms)
            correlation_id_var.reset(token)

    return sync_wrapper


def resolve_log_dir() -> Path:
    """``STEALTH_MCP_LOG_DIR`` override, else the existing per-user state-dir
    convention (``singleton.STATE_DIR / "logs"``). Pure — never creates the
    directory.
    """
    configured = get_settings().log_dir
    if configured and configured.strip():
        return Path(configured).expanduser()

    from stealth_chrome_devtools_mcp.embedded import singleton

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

    # Affirmative proof of boot, independent of any error occurring: without
    # this the structured log stays EMPTY until the first error/warning/info
    # call, so "did the backend boot at all" was answerable only from
    # backend-boot.log (plan_M3 §3 step-2 verify: "a backend-<pid>.log with
    # the ... startup line"). F-840 adds argv: the ONE thing that distinguishes
    # a console-attached `serve --http` birth from the detached
    # _start_server_process spawn, which a post-mortem otherwise cannot tell
    # apart. Local file only — this line is never shipped to Sentry.
    logger.info(
        "backend process starting (pid=%d, log=%s, argv=%r)",
        os.getpid(),
        log_path,
        sys.argv,
    )

    return log_path


def roll_boot_log(log_dir: Path) -> Path:
    """Rotate ``<logdir>/backend-boot.log`` if it has grown past
    :data:`_BOOT_LOG_MAX_BYTES`, then return its (now free) path.

    Called by ``singleton._start_server_process`` immediately before it opens
    the file for a NEW backend. That is the ONLY moment rotation is possible:
    the boot log is a raw ``Popen`` stdout/stderr redirect, so the running
    backend holds its descriptor open for its entire life — an in-process
    ``RotatingFileHandler`` never sees these bytes, and an external rotation
    would either fail (Windows sharing violation) or silently keep writing to
    the renamed inode (POSIX). The launcher is between two backends and holds
    no descriptor, so it is the one safe hand-off point.

    Keeps ``.1`` … ``.<_BOOT_LOG_BACKUPS>``, newest first, and drops the rest —
    a rotation that accumulated would only rename F-830, not fix it. Never
    raises: a boot log that cannot be rolled must not block a spawn (plan_M3
    §7's fail-open discipline, same as the caller's own OSError fallback).
    """
    boot_log = log_dir / BOOT_LOG_NAME
    try:
        if boot_log.stat().st_size <= _BOOT_LOG_MAX_BYTES:
            return boot_log
        (log_dir / f"{BOOT_LOG_NAME}.{_BOOT_LOG_BACKUPS}").unlink(missing_ok=True)
        for index in range(_BOOT_LOG_BACKUPS - 1, 0, -1):
            older = log_dir / f"{BOOT_LOG_NAME}.{index}"
            if older.exists():
                older.replace(log_dir / f"{BOOT_LOG_NAME}.{index + 1}")
        boot_log.replace(log_dir / f"{BOOT_LOG_NAME}.1")
    except OSError:
        pass
    return boot_log


def _post_mortem_exempt(files: list[Path]) -> set[Path]:
    """F-840: the subset of ``files`` (newest first) that age must not reach.

    A dead backend's ``backend-<pid>.log`` + ``backend-<pid>-fault.log`` pair
    is the whole post-mortem for that process, and the age at which it becomes
    interesting is exactly the age at which an unattended crash gets noticed —
    so a plain mtime sweep deletes evidence precisely when it is needed (the
    2026-08-30 OOM investigation started blind for this reason). Two rules,
    both narrow: keep the newest :data:`_KEEP_BACKEND_SETS` pid-sets whatever
    their age, and keep every fault log younger than
    :data:`_FAULT_LOG_KEEP_DAYS` (they are near-empty unless a hard crash
    actually wrote one, so this costs bytes, not megabytes).
    """
    fault_cutoff = time.time() - _FAULT_LOG_KEEP_DAYS * 86400
    exempt = {
        path
        for path in files
        if path.name.endswith("-fault.log") and path.stat().st_mtime >= fault_cutoff
    }
    recent_pids: list[str] = []
    for path in files:
        match = _BACKEND_LOG_RE.match(path.name)
        if match is not None and match.group(1) not in recent_pids:
            recent_pids.append(match.group(1))
    keep_pids = set(recent_pids[:_KEEP_BACKEND_SETS])
    for path in files:
        match = _BACKEND_LOG_RE.match(path.name)
        if match is not None and match.group(1) in keep_pids:
            exempt.add(path)
    return exempt


def prune_old_logs(
    log_dir: Path | None = None, keep_days: int = 7, keep_files: int = 50
) -> None:
    """Best-effort sweep of ``<logdir>`` so per-pid log files (one per proxy
    session) don't accumulate forever. Never raises.

    Dead-backend post-mortems are exempt — see :func:`_post_mortem_exempt`.
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
        exempt = _post_mortem_exempt(files)
        cutoff = time.time() - keep_days * 86400
        for index, path in enumerate(files):
            if path in exempt:
                continue
            if index >= keep_files or path.stat().st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    path.unlink()
    except OSError:
        pass
