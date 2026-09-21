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

Owning log-WRITING means owning what the DEPENDENCIES may write too, so this
is also the one place third-party logger LEVELS are set:
:func:`apply_payload_log_floor` (F-906, F-908) holds every family in
:data:`PAYLOAD_LOG_FAMILIES` at WARNING — ``nodriver`` and ``websockets``
because they interpolate a raw CDP message into DEBUG/INFO text, cookie names
and values included, and ``sse_starlette`` and ``mcp.client`` because they do
the same to the two ENDS of one tool answer (the SSE frame the backend sends,
and the message the stdio proxy parses back out of it). One caller-side
``logging.basicConfig(level=DEBUG)`` is enough to route any of them to our
stderr and to a Sentry breadcrumb. It belongs HERE and not at the seam that
patches nodriver
(``cdp_transport``, which owns what nodriver may DO with a reply): a level is
log configuration, and log configuration has one home.

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

#: F-906. The logger families that interpolate a RAW CDP message into their own
#: log text, and so carry whatever the page holds — cookie names and values
#: included. Named by family ROOT, because both libraries build their loggers
#: from ``__name__`` (measured: ``nodriver.core.connection.logger.name`` IS
#: ``nodriver.core.connection``), so one explicit level on the root covers every
#: module under it, present and future.
#:
#: What each one does, measured against nodriver 0.47.0 / websockets 16.0:
#:
#: * ``nodriver`` — ``core/connection.py``:445 DEBUG-logs the whole reply
#:   (``"got answer for (message_id:%d) => %s"``, the parsed ``message`` dict),
#:   :451 INFO-logs the whole EVENT message when a field will not parse, and
#:   ``core/browser.py``:824/:869 DEBUG-log a cookie's name and value outright;
#: * ``websockets`` — ``protocol.py``:609 DEBUG-logs the frame, and a frame
#:   short enough is printed whole (truncation starts past ~75 characters), so
#:   this is the same payload one layer down. Capping ``nodriver`` alone would
#:   have left that door open.
#:
#: * ``sse_starlette`` — F-908. ``sse/sse.py``:362 is
#:   ``logger.debug("chunk: %s", chunk)``, and for THIS backend that chunk is
#:   the answer to a ``tools/call``, whole: ``get_cookies``' jar,
#:   ``get_page_content``'s HTML, ``get_instance_state``'s localStorage. The
#:   SSE frame is what carries every answer because FastMCP leaves
#:   ``json_response`` at its ``False`` default and nothing here asks
#:   otherwise (an inherited ``FASTMCP_JSON_RESPONSE`` cannot either —
#:   ``backend_env.scrub`` drops the prefix, F-890). Measured by driving the
#:   real ``EventSourceResponse``, not read off the call.
#:
#: * ``mcp.client`` — F-908 review M1, and it is the OTHER END of that same
#:   frame. The backend logs the SSE chunk it SENDS; the STDIO PROXY re-parses
#:   the identical bytes and logs the MESSAGE:
#:   ``client/streamable_http.py``:218 is ``logger.debug(f"SSE message:
#:   {message}")`` — the whole ``JSONRPCResponse``, i.e. the same tool result —
#:   and :547 is its argument-side twin on the POST leg. Both processes call
#:   :func:`configure_logging` (the proxy at ``singleton.py``:976), so capping
#:   only ``sse_starlette`` left the payload reaching a root handler in the
#:   proxy, which is what the review reproduced.
#:
#:   The entry is ``mcp.client`` and not ``mcp.client.streamable_http`` because
#:   a full census of the installed ``mcp/client`` package found a SECOND
#:   renderer of the same shape — ``client/sse.py``:114/:137, the legacy SSE
#:   client transport — and that module IS loaded (transitively, by importing
#:   the streamable one: measured), so the logger exists even though nothing
#:   here drives it. ``mcp.client`` is the narrowest name covering the measured
#:   set; it is not the whole ``mcp`` family, and the reason for that line is
#:   in the paragraph below. Its cost is zero on the same measurement as the
#:   others: every call at WARNING and above under ``mcp/client`` still passes.
#:
#: Deliberately NOT ``uc``: that is only the local alias this codebase imports
#: ``nodriver`` under, and no logger is ever named by it — capping a name that
#: does not exist would be a claim the evidence does not support.
#:
#: Deliberately NOT the whole ``mcp`` family, and not ``fastmcp``; the census
#: behind both is pinned rather than summarised
#: (``TestTheFamiliesDeliberatelyLeftOut``).
#:
#: The line between IN and OUT inside ``mcp`` is SERVER versus CLIENT, and it
#: was measured on both sides rather than inferred from one. The SERVER tree
#: renders nothing for a request: ``server/lowlevel/server.py``:676 logs the
#: whole incoming message and IS admitted at DEBUG, but for a REQUEST that
#: object is a ``RequestResponder``, which defines neither ``__repr__`` nor
#: ``__str__``, so ``%s`` yields ``<… object at 0x…>``. (The qualifier is
#: load-bearing: the same line's NOTIFICATION arm renders its pydantic model in
#: full. It is still out, because every notification a client may send is
#: enumerated from the SDK's own union and none carries a tool result or tool
#: arguments — pinned, so an SDK that adds one goes RED.) The CLIENT transport
#: renders the whole message and is capped, above. Capping ``mcp`` entire would
#: therefore silence the server SDK's own INFO diagnostics and close no door
#: that ``mcp.client`` does not already close.
#:
#: ``fastmcp``'s tool-ARGUMENT line (``server/server.py``:672) is real, and the
#: library already shields it: its loggers hang under a ``FastMCP`` root
#: carrying its own level and ``propagate = False``, so a caller's root DEBUG
#: never reaches them — and the bare ``fastmcp`` family, which DOES inherit
#: root, holds no payload line, so capping it would be the ``uc`` mistake
#: spelled differently.
#:
#: Two SITES are RECORDED and not fixed, because no family cap can reach either
#: however this tuple grows: ``mcp/shared/session.py``:383-384 and :430-432 use
#: module-level ``logging.warning`` / ``logging.debug``, i.e. the ROOT logger.
#: Both are on a validation-failure path and both are reachable AS SHIPPED,
#: with no ``basicConfig`` anywhere: :383 carries pydantic's middle-truncated
#: ``input_value=`` echo of a caller's arguments at WARNING (:384 renders the
#: whole message, but only at DEBUG), and :430-432 renders the whole
#: ``message.message.root`` AT WARNING in one line. They are named in the
#: finding (§6) as the F-911 candidate, which needs a different mechanism — a
#: root filter or a ``before_breadcrumb`` — exactly as F-907's does.
#:
#: Deliberately NOT ``starlette``, ``anyio`` (neither logs anything below
#: WARNING at all — an AST census, so "nothing" is measured rather than
#: grepped), ``uvicorn`` (its whole-ASGI-message logger replaces bodies with a
#: ``<N bytes>`` placeholder BY CONSTRUCTION and logs at TRACE, which
#: ``basicConfig(DEBUG)`` does not admit; the access log is already off, F-830)
#: or ``httpcore``/``httpx`` (the body traces set no ``return_value``, so their
#: message is the trace NAME alone — response HEADERS can appear, a tool result
#: cannot).
PAYLOAD_LOG_FAMILIES = ("nodriver", "websockets", "sse_starlette", "mcp.client")

#: The floor those families are held at. WARNING is not a new policy — it is
#: the effective level every shipped configuration of this product already had
#: (measured across backend, proxy, ``--debug`` and
#: ``STEALTH_MCP_LOG_LEVEL=DEBUG``), which is exactly why setting it changes
#: nothing an operator sees and only closes the one door that was open. It is
#: also the level at which those libraries stop quoting payloads and start
#: reporting faults: ``connection.py``:483's callback WARNING names the callback
#: and the event CLASS, never the message. For ``sse_starlette`` the floor
#: costs even less — it has no call at WARNING or above anywhere in the package
#: (measured), so there is nothing there for a floor to stand in front of.
PAYLOAD_LOG_FLOOR = logging.WARNING


def apply_payload_log_floor() -> None:
    """Hold the payload-carrying library loggers at :data:`PAYLOAD_LOG_FLOOR`.

    F-906. ``nodriver`` logs every raw CDP reply verbatim and ``websockets``
    logs the frame under it, so a page's cookies are one enabled DEBUG record
    away from our stderr — which for the backend is redirected into
    ``backend-boot.log``, a durable file — and, for the one line that sits at
    INFO, away from a Sentry breadcrumb on the next event.

    F-908 added two more under the identical premise, from the other end of the
    same request: ``sse_starlette`` logs the SSE chunk the BACKEND sends, and
    the chunk is the whole serialised answer to a ``tools/call``; ``mcp.client``
    logs the MESSAGE the STDIO PROXY parses back out of those same bytes, which
    is the same answer again in a second process that also calls
    :func:`configure_logging`. Where F-906's lines quote what CHROME said,
    these quote what WE said back — and one tool result has two ends, so
    capping one of them is half a fix. That is why this stays one list and one
    mechanism rather than a second floor beside the first.

    Until this, the only thing stopping them was that those loggers carry no
    level of their own and INHERIT root's. That is a real protection and it was
    measured to hold for every configuration this product ships — but it is
    root's to give away, and one ``logging.basicConfig(level=DEBUG)`` in a
    caller that embeds this backend, a notebook or a test gives it away for the
    whole process. MEASURED both ways round: every payload line reached a root
    handler and ``connection.py``:451 reached Sentry, whether the
    ``basicConfig`` came before or after our own setup.

    So the level is set EXPLICITLY on the family root. ``getEffectiveLevel``
    stops at the first ancestor carrying a non-``NOTSET`` level, and
    ``basicConfig`` only ever sets ROOT's — so ours wins in either order, and
    keeps winning. That is also why this is the whole fix and there is no
    filter and no ``before_breadcrumb`` rule beside it: Sentry's
    ``LoggingIntegration`` patches ``logging.Logger.callHandlers``, which
    ``Logger.handle`` only reaches for a record ``isEnabledFor`` has already
    admitted, so a level is upstream of every sink at once. A second mechanism
    would be a second home for one decision (convention 4), and a filter keyed
    on a library's message strings would be a pattern-match against text that
    library is free to reword.

    It never SILENCES: WARNING and above pass exactly as they did, because
    those are nodriver's real diagnostics and losing them would be this fix
    costing more than it saves. The residual is named rather than hidden — a
    caller who writes ``logging.getLogger("nodriver").setLevel(DEBUG)`` still
    gets DEBUG, because that is them asking for this library's payloads by
    name, which is a different act from turning DEBUG on globally.

    Called from :func:`configure_logging`, before anything that can fail: a
    process whose log directory could not be created still has stderr and
    Sentry, so it still needs the floor. It honours that caller's never-raises
    contract BY CONSTRUCTION rather than with a handler — these statements are
    a dict lookup and an integer assignment on a stdlib logger, with no I/O and
    nothing to fail — so there is no ``except`` here that could only ever hide
    a bug of ours. Adding a family costs one more of each, which is the other
    reason the list is the extension point and a per-library helper would not
    be.
    """
    for family in PAYLOAD_LOG_FAMILIES:
        logging.getLogger(family).setLevel(PAYLOAD_LOG_FLOOR)


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


def _record_tool_failure(tool_name: str, error: Exception) -> None:
    """Put a failed tool call into the in-memory debug ring (F-835).

    Called from the ONE wrapper every registered tool passes through, so a
    failure is visible in the product's own debug surface no matter which tool
    it came from — before this, a total ``spawn_browser`` outage (24 consecutive
    failures) left ``get_debug_view`` reporting ``total_errors: 0``.

    Two properties this helper exists to guarantee:

    * it **never raises**. It sits on the failure path of all 94 tools, and a
      debug-ring problem must not replace (or mask) the error the client is
      owed. The recording is the only thing that can be lost here.
    * it **never touches** ``error``. The exception continues to the client
      byte-identical — same type, same args, no attributes added (a pinned
      contract: ``test_observability`` asserts ``vars(exc) == {}``).

    Note what it does NOT do: write the failure to the backend log. That is
    ``log_tool_failure``'s deliberate split (F-782's redaction condition), not
    an oversight — the ring is process-local, the log file is durable and
    Sentry-bridged, and a failure message echoes the caller's arguments.

    ``debug_logger`` is imported here rather than at module scope because it
    imports ``correlation_id_var`` from THIS module; a top-level import would
    close the cycle. Deferred, function-local imports are the established fix
    for exactly this shape here (see the module docstring and pyproject's
    PLC0415 rationale) — and on the failure path the cost is a dict lookup.
    """
    with contextlib.suppress(Exception):
        from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

        debug_logger.log_tool_failure(tool_name, error)


def with_correlation_id(func: Callable[..., object]) -> Callable[..., object]:
    """Wrap a registered tool function (the ``section_tool`` chokepoint, F-308)
    so every call gets a fresh correlation id — stamped by
    :class:`CorrelationIdFilter` onto every log line emitted during the call,
    backend file and ``debug_logger`` entries alike — and one INFO start/end
    pair. ``functools.wraps`` preserves the schema FastMCP introspects
    (name/signature/docstring); a ``tools/list`` schema-snapshot test pins
    that this holds for a representative tool per section.

    It is also where a FAILED call is recorded (F-835,
    :func:`_record_tool_failure`): the same chokepoint argument that makes this
    the home of the correlation id makes it the home of "this call failed" —
    one place, all 94 tools, instead of a per-tool ``except`` nobody adds. The
    exception is recorded and re-raised unchanged.

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
            except Exception as error:
                # Record and re-raise, unchanged (F-835). Only Exception:
                # CancelledError is a shutdown signal, not a tool failure.
                _record_tool_failure(tool_name, error)
                raise
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
        except Exception as error:
            _record_tool_failure(tool_name, error)  # F-835, as above
            raise
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

    It is also where the payload-carrying library loggers are held down
    (:func:`apply_payload_log_floor`, F-906/F-908 — all four families in
    :data:`PAYLOAD_LOG_FAMILIES`). That call is FIRST — ahead of the
    idempotency guard and ahead of everything that can raise ``OSError`` —
    because the floor is about what may leave the process, and a process that
    failed to open its log file still has stderr and still has Sentry.
    """
    apply_payload_log_floor()

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
