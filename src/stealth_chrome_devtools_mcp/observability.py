"""Sentry error-shipping for stealth-chrome-devtools-mcp.

This module is error-SHIPPING only: it forwards already-logged or raised errors
to the project's Sentry. It is deliberately NOT the logging spine — plan M3's
``logging_setup.py`` owns log-WRITING (handlers, formatters, sinks). One home
each: ``observability.py`` decides where errors go; M3 decides how they are
recorded. Do not add file logging or an excepthook framework here.

ON by default, and configuration-free. The destination is the hardcoded
:data:`_DSN` below and the SDK is a first-class dependency, so a user who sets
no environment variable and writes no ``.env`` still gets a working server that
reports its own crashes. Opting out is one namespaced flag:
``STEALTH_MCP_NO_ERROR_REPORTING=true``.

There is deliberately no ``SENTRY_DSN`` knob (issue #55). The backend is a
shared singleton that MCP clients launch with the host project's cwd, so a
DSN read from the environment meant it silently adopted whatever ``SENTRY_DSN``
that project's own ``.env`` happened to hold and shipped this tool's errors to
someone else's app project. A Sentry DSN is public by design (it is an ingest
address, not a credential), so hardcoding ours costs nothing and removes the
collision entirely.
"""

import importlib.metadata
import logging

from stealth_chrome_devtools_mcp.settings import get_settings

_PACKAGE_NAME = "stealth-chrome-devtools-mcp"

#: THE destination for this tool's error reports. Public by design — a DSN is
#: an ingest endpoint that only accepts events; it grants no read access to the
#: project. Published in README.md since 2.0.x.
_DSN = "https://3206541bdab9246f00d7099e692e2ee2@sentry.devino.ca/34"

_log = logging.getLogger(__name__)


def _release() -> str | None:
    """Best-effort package version, used as the Sentry ``release`` tag."""
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def sentry_init() -> bool:
    """Initialize Sentry error shipping unless the operator opted out.

    Returns ``True`` when Sentry was initialized, ``False`` when it was a no-op
    — either ``STEALTH_MCP_NO_ERROR_REPORTING`` is set, or ``sentry_sdk`` could
    not be imported.

    Never raises. The previous contract raised ``RuntimeError`` when a DSN was
    set without the optional ``sentry`` extra installed, on the rationale that
    "opting into error reporting and then getting silence is worse than a loud,
    actionable startup error". That rationale is now obsolete: reporting is on
    by default with a bundled SDK, so there is no per-user opt-in left to
    betray — only a server that would refuse to boot over its own telemetry.
    A missing SDK degrades to a warning and unreported errors.
    """
    if get_settings().no_error_reporting:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        _log.warning(
            "sentry_sdk is not importable; error reporting is disabled for this "
            "process. Reinstall stealth-chrome-devtools-mcp to restore it, or "
            "set STEALTH_MCP_NO_ERROR_REPORTING=true to silence this warning."
        )
        return False

    sentry_sdk.init(
        dsn=_DSN,
        integrations=[
            LoggingIntegration(event_level=logging.ERROR),
            AsyncioIntegration(),
        ],
        release=_release(),
    )
    return True
