"""What a FAILED tool call leaves behind, besides the exception the client gets.

``logging_setup.with_correlation_id`` is the ONE wrapper every registered tool
passes through, and so THE one place a failed call is recorded (F-835). This
leaf is what it calls, and it is the only caller: :func:`record` puts the
failure in the in-memory debug ring and, for the one kind fastmcp stopped
reporting, restores the report (F-943). It was split out of ``logging_setup``
by F-943 to keep that file inside its 1000-LOC budget. It is not a second way
to record a failure, so never call it from a tool.

Two properties every function here guarantees:

* it **never raises**. It sits on the failure path of all 94 tools, and a
  recording problem must not replace (or mask) the error the client is owed.
  The recording is the only thing that can be lost here.
* it **never touches** ``error``. The exception continues to the client
  byte-identical — same type, same args, no attributes added (a pinned
  contract: ``test_observability`` asserts ``vars(exc) == {}``).

Every import past the standard library is deferred into the function that needs
it. ``debug_logger`` imports ``correlation_id_var`` from ``logging_setup``,
which imports this module, so a top-level import would close that cycle; and
the stdio proxy loads ``logging_setup`` and must not pay for pydantic on a path
it never runs. Deferred, function-local imports are the established fix for
exactly this shape (pyproject's PLC0415 rationale), and on the failure path
the cost is a dict lookup.
"""

from __future__ import annotations

import contextlib
import logging


def record(tool_name: str, error: Exception) -> None:
    """Record that a call of ``tool_name`` failed with ``error``.

    Each half is guarded on its own, so a broken ring never costs the report.
    """
    _into_debug_ring(tool_name, error)
    _report_own_validation_error(tool_name, error)


def _into_debug_ring(tool_name: str, error: Exception) -> None:
    """Put a failed tool call into the in-memory debug ring (F-835).

    A failure is visible in the product's own debug surface no matter which tool
    it came from — before this, a total ``spawn_browser`` outage (24 consecutive
    failures) left ``get_debug_view`` reporting ``total_errors: 0``.

    Note what it does NOT do: write the failure to the backend log. That is
    ``log_tool_failure``'s deliberate split (F-782's redaction condition), not
    an oversight — the ring is process-local, the log file is durable and
    Sentry-bridged, and a failure message echoes the caller's arguments.
    """
    with contextlib.suppress(Exception):
        from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

        debug_logger.log_tool_failure(tool_name, error)


def _report_own_validation_error(tool_name: str, error: Exception) -> None:
    """Report a pydantic ``ValidationError`` that a tool BODY raised (F-943).

    fastmcp 2.14's ``ToolManager.call_tool`` re-raised a ``ValidationError``
    without logging it, treating it as the caller's bad argument. fastmcp 3's
    ``FastMCP.call_tool`` still does not report it: it logs "Invalid arguments
    for tool" at WARNING, with no exception, and ``logging_setup`` holds that
    line back because it quotes the input whole (F-946). One that reaches the
    wrapper is never the caller's: ``FunctionTool`` validates the arguments
    before it calls the tool, so a caller's typo fails before the wrapper runs.
    What is left is ours (an unknown ``STEALTH_MCP_*`` key failing ``Settings()``
    on every spawn, a model built with a wrong field type), and unreported it
    would reach the caller and nothing else.

    So it is logged where and as fastmcp 2.11.2 logged it: on fastmcp's own
    tool-call logger, ``Error calling tool '<name>'``, exception attached. Its sinks
    are the ones these reports always had (fastmcp's handlers, and Sentry, which
    hooks ``Logger.callHandlers`` and so sees a non-propagating logger too), and
    ``expected_events`` judges it by the rule written for it: the frames say it
    is ours, so it ships. Not ``stealth.backend``: what a failure may write to
    that log is F-782's question, and this restores a report, not a new one.

    fastmcp still logs every other exception itself, so they are left alone; a
    second report would send each one twice.
    """
    with contextlib.suppress(Exception):
        import pydantic

        from stealth_chrome_devtools_mcp.expected_events import TOOL_CALL_LOGGER

        if isinstance(error, pydantic.ValidationError):
            logging.getLogger(TOOL_CALL_LOGGER).error(
                "Error calling tool %r", tool_name, exc_info=error
            )
