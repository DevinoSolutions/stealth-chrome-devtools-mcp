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

The fingerprint is read from a CLOSED vocabulary, fixed below:
``tool_error_fingerprint.declared`` accepts a declaration only when it is made of
the kinds, sites and probe steps named here (and exception type names, which it
maps itself), because an attribute is something a caller's code can set on an
instance of any class. A new site or step is added here, in the same change that
adds its call site; a test reads the call sites against this list.
"""

HUNG_BROWSER, FROZEN_CLONE, FORENSIC = "hung-browser", "frozen-clone", "forensic"
#: What a fingerprint's first element may be: the subclasses' ``kind``.
KINDS = frozenset({HUNG_BROWSER, FROZEN_CLONE, FORENSIC})
#: Where a cause may say the report came from: the call sites of ``browser_health``
#: and ``clone_probe``, each the first word of the cause its site passes.
SITES = frozenset(
    {
        "probe",
        "heal",
        "starvation-ceiling",
        "close-left-running",
        "relaunch-cooldown",
        "relaunch-failed",
        "relaunch-probe-fails",
    }
)
#: The probe steps ``browser_health._describe`` can end a failure at.
STEPS = frozenset(
    {"connect", "createTarget", "attachToTarget", "evaluate", "closeTarget"}
)
#: What a failure reads when the instance was not there to probe.
NO_INSTANCE = "no instance"


class ForensicError(Exception):
    """A report logged and never raised. ``kind`` is the subclass's, *cause* the
    call site's (a site name, the failed step, the error type), never a value that
    differs per occurrence."""

    kind = FORENSIC

    def __init__(self, message: str, *cause: str) -> None:
        super().__init__(message)
        self.sentry_fingerprint: tuple[str, ...] = (self.kind, *cause)


class HungBrowserError(ForensicError):
    """A persistent-profile browser that stopped answering a probe (or its relaunch
    failed): the report ``browser_health`` logs."""

    kind = HUNG_BROWSER


class FrozenCloneError(ForensicError):
    """A disposable clone that missed its probe: the report ``clone_probe`` logs."""

    kind = FROZEN_CLONE
