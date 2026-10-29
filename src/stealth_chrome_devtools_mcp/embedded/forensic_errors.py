"""The exceptions this package LOGS at ERROR and never raises (T-10034).

``debug_logger.log_error`` is handed these so the ERROR line and its Sentry event
carry forensics in the message: an instance id, uptime, process counts, timings,
paths. With no traceback Sentry groups such an event by that message, so every
occurrence became a NEW issue (STEALTH-CHROME-DEVTOOLS-MCP-ES, -EJ, -EF, -EE, -DY,
-DQ, -DB: seven freezes, seven issues) and the freeze rate could not be read.

``ForensicError.sentry_fingerprint`` names the KIND and the stable cause the call
site passes instead; ``observability._stable_fingerprint`` reads it off the live
exception and ``_scrub_event`` sets it as the event's fingerprint. Only grouping
changes: ``str(error)`` is the full message, as before. A leaf: it imports nothing.
"""


class ForensicError(Exception):
    """A report logged and never raised. ``kind`` is the subclass's, *cause* the
    call site's (a site name, the failed step, the error type), never a value that
    differs per occurrence."""

    kind = "forensic"

    def __init__(self, message: str, *cause: str) -> None:
        super().__init__(message)
        self.sentry_fingerprint: tuple[str, ...] = (self.kind, *cause)


class HungBrowserError(ForensicError):
    """A persistent-profile browser that stopped answering a probe (or its relaunch
    failed): the report ``browser_health`` logs."""

    kind = "hung-browser"


class FrozenCloneError(ForensicError):
    """A disposable clone that missed its probe: the report ``clone_probe`` logs."""

    kind = "frozen-clone"
